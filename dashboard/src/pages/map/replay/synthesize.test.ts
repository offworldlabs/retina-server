import { describe, expect, it } from "vitest";
import { MAX_TRAIL_POINTS } from "../trails";
import { groupTrackRows, mintedDarkKey } from "./group";
import { multinodeHexFromKey } from "./hex";
import {
  ADSB_EXPIRY_MS,
  DARK_EXPIRY_MS,
  TRAIL_DEDUP_DEG,
  TRAIL_STALE_MS,
  buildReplayIndex,
  synthesizeFrame,
  type ReplayIndex,
} from "./synthesize";
import type { ReplayFrame, TrackRow, TracksResponse } from "./types";
import { FLEET_ALL, KEYED, LEGACY } from "./__fixtures__/scenarios";

/* ---- helpers ---------------------------------------------------- */

const T0 = Date.UTC(2026, 9, 18, 20, 0, 0);
const published = (r: TrackRow) => Math.max(r.solve_ts_ms, r.frame_ts_ms);
const byPublication = (a: TrackRow, b: TrackRow) => published(a) - published(b) || a.frame_ts_ms - b.frame_ts_ms;
/** Python's round(), as the backend rounds. */
const py = (x: number, digits: number) => Number(x.toFixed(digits));
const ft = (m: number) => Math.round(m / 0.3048);

function response(rows: TrackRow[]): TracksResponse {
  const sorted = rows.slice().sort((a, b) => a.frame_ts_ms - b.frame_ts_ms || a.solve_ts_ms - b.solve_ts_ms);
  return { from: T0 - 900_000, to: T0 + 900_000, fleet: "real", count: sorted.length, truncated: false, rows: sorted };
}

/** A dark keyed row (mn-dark-test) unless overridden. */
function row(over: Partial<TrackRow>): TrackRow {
  return {
    frame_ts_ms: T0,
    solve_ts_ms: T0 + 500,
    track_key: "mn-dark-test",
    lane: "dark",
    node_kind: "real",
    lat: 34.85,
    lon: -82.39,
    alt_m: 1500,
    vel_east_ms: 0,
    vel_north_ms: 100,
    vel_up_ms: 0,
    n_nodes: 2,
    node_refs: ["nde0000000001", "nde0000000002"],
    adsb_hex: null,
    pos_sigma_m: 500,
    rms_delay_us: 0.3,
    rms_doppler_hz: 2,
    is_anomalous: false,
    anomaly_types: [],
    ...over,
  };
}

/** `n` rows of one track, one per second, moving 0.001 degree north each. */
function run(n: number, over: (k: number) => Partial<TrackRow> = () => ({})): TrackRow[] {
  return Array.from({ length: n }, (_, k) =>
    row({ frame_ts_ms: T0 + k * 1000, solve_ts_ms: T0 + k * 1000 + 500, lat: 34.85 + k * 0.001, ...over(k) }),
  );
}

/** A response's tracks, by hex, each in publication order. Grouping itself is group.test.ts's subject. */
function tracksByHex(resp: TracksResponse): Map<string, TrackRow[]> {
  const out = new Map<string, TrackRow[]>();
  for (const g of groupTrackRows(resp.rows)) {
    out.set(multinodeHexFromKey(g.key), g.rows.map((i) => resp.rows[i]).sort(byPublication));
  }
  return out;
}

/** A keyed response's tracks, by key, each in publication order. */
function tracksByKey(resp: TracksResponse): Map<string, TrackRow[]> {
  const out = new Map<string, TrackRow[]>();
  for (const r of resp.rows) {
    const key = r.track_key ?? `mn-adsb-${r.adsb_hex}`;
    out.set(key, [...(out.get(key) ?? []), r]);
  }
  for (const rows of out.values()) rows.sort(byPublication);
  return out;
}

const expiryOf = (key: string) => (key.startsWith("mn-adsb-") ? ADSB_EXPIRY_MS : DARK_EXPIRY_MS);
const entryOf = (frame: ReplayFrame, hex: string) => frame.aircraft.find((a) => a.hex === hex);
const pointOf = (r: TrackRow) => [py(r.lat, 6), py(r.lon, 6), r.alt_m == null ? 0 : ft(r.alt_m), py(r.frame_ts_ms / 1000, 1)];

/** The rows' largest silence between consecutive solves: the rows either side of it. */
function widestGap(rows: TrackRow[]): [TrackRow, TrackRow] {
  let at = 1;
  for (let i = 2; i < rows.length; i++) {
    if (rows[i].frame_ts_ms - rows[i - 1].frame_ts_ms > rows[at].frame_ts_ms - rows[at - 1].frame_ts_ms) at = i;
  }
  return [rows[at - 1], rows[at]];
}

/** The response's track with the largest silence, as grouping keys it, its rows in publication order. */
function mostSilentTrack(resp: TracksResponse): { key: string; rows: TrackRow[] } {
  const tracks = groupTrackRows(resp.rows).map((g) => ({ key: g.key, rows: g.rows.map((i) => resp.rows[i]).sort(byPublication) }));
  const silence = ({ rows }: { rows: TrackRow[] }) => {
    const [before, after] = widestGap(rows);
    return after.frame_ts_ms - before.frame_ts_ms;
  };
  return tracks.reduce((a, b) => (silence(b) > silence(a) ? b : a));
}

const ALL: [string, TracksResponse][] = [
  ...Object.entries(KEYED).map(([name, resp]) => [`keyed ${name}`, resp] as [string, TracksResponse]),
  ...Object.entries(LEGACY).map(([name, resp]) => [`legacy ${name}`, resp] as [string, TracksResponse]),
  ...Object.entries(FLEET_ALL).map(([name, resp]) => [`fleet=all ${name}`, resp] as [string, TracksResponse]),
];

/* ---- the index -------------------------------------------------- */

describe("buildReplayIndex", () => {
  it("reports the window, its rows and its tracks", () => {
    const resp = KEYED.known;
    const index: ReplayIndex = buildReplayIndex(resp);
    expect(index).toMatchObject({
      from: resp.from,
      to: resp.to,
      truncated: false,
      rowCount: resp.rows.length,
      trackCount: new Set(resp.rows.map((r) => r.track_key)).size,
    });
  });

  it("indexes an empty window", () => {
    const index = buildReplayIndex(response([]));
    expect(index).toMatchObject({ rowCount: 0, trackCount: 0 });
    expect(synthesizeFrame(index, T0).aircraft).toEqual([]);
  });
});

describe("buildReplayIndex: where a cut window stops", () => {
  it("knows the last instant any row was published at, and -Infinity with none", () => {
    for (const [, resp] of ALL) expect(buildReplayIndex(resp).lastPublishedAt).toBe(Math.max(...resp.rows.map(published)));
    // Not the last row's: rows come in frame order, and a later frame can be published sooner.
    const late = row({ frame_ts_ms: T0, solve_ts_ms: T0 + 9_000 });
    const quick = row({ frame_ts_ms: T0 + 1_000, solve_ts_ms: T0 + 1_300, lat: 34.86 });
    expect(buildReplayIndex(response([late, quick])).lastPublishedAt).toBe(T0 + 9_000);
    // A row stamped before its own epoch is published at its epoch.
    expect(buildReplayIndex(response([row({ solve_ts_ms: T0 - 200 })])).lastPublishedAt).toBe(T0);
    expect(buildReplayIndex(response([])).lastPublishedAt).toBe(-Infinity);
  });

  it("knows the latest frame time of any row, and -Infinity with none", () => {
    for (const [, resp] of ALL) expect(buildReplayIndex(resp).lastFrameTs).toBe(Math.max(...resp.rows.map((r) => r.frame_ts_ms)));
    // Not the publication: a row published well after its frame is still cut by its frame time.
    const late = row({ frame_ts_ms: T0, solve_ts_ms: T0 + 9_000 });
    const quick = row({ frame_ts_ms: T0 + 1_000, solve_ts_ms: T0 + 1_300, lat: 34.86 });
    const index = buildReplayIndex(response([late, quick]));
    expect(index.lastFrameTs).toBe(T0 + 1_000);
    expect(index.lastPublishedAt).toBe(T0 + 9_000);
    expect(buildReplayIndex(response([])).lastFrameTs).toBe(-Infinity);
  });

  it("reads truncated as a boolean", () => {
    expect(buildReplayIndex({ ...KEYED.known, truncated: true }).truncated).toBe(true);
    expect(buildReplayIndex({ ...KEYED.known, truncated: undefined as unknown as boolean }).truncated).toBe(false);
  });
});

describe("buildReplayIndex: a legacy track across windows", () => {
  const MIN = 60_000;
  const A = Date.UTC(2026, 9, 18, 10, 0, 0);
  /** One legacy dark target (no track_key, no adsb_hex, no lane) solved every 2 s from A - 20 min to A + 50 min, east at 100 m/s. */
  const LEGACY_RUN: TrackRow[] = [];
  for (let f = A - 20 * MIN; f < A + 50 * MIN; f += 2_000) {
    const s = (f - (A - 20 * MIN)) / 1000;
    LEGACY_RUN.push(
      row({
        frame_ts_ms: f, solve_ts_ms: f + 800, track_key: null, lane: null, adsb_hex: null, pos_sigma_m: null, is_anomalous: null,
        lat: 34.8, lon: -82.6 + (s * 100) / (111_320 * Math.cos((34.8 * Math.PI) / 180)),
      }),
    );
  }
  /** What GET /api/data/tracks returns for [from, to): the rows solved inside it. */
  const windowOf = (from: number, to: number): TracksResponse => {
    const rows = LEGACY_RUN.filter((r) => r.solve_ts_ms >= from && r.solve_ts_ms < to);
    return { from, to, fleet: "real", count: rows.length, truncated: false, rows };
  };
  const hexesAt = (index: ReplayIndex, clock: number) => synthesizeFrame(index, clock).aircraft.map((a) => a.hex);

  it("keeps the hex of the index before when the windows share rows, and on through one that shares rows only with that", () => {
    const first = buildReplayIndex(windowOf(A - 15 * MIN, A + 15 * MIN));
    const [hex] = hexesAt(first, A);
    expect(hex).toBe(multinodeHexFromKey(mintedDarkKey(windowOf(A - 15 * MIN, A + 15 * MIN).rows[0])));
    // The window playback asks for a minute before the first one ends, at 1x.
    const second = buildReplayIndex(windowOf(A - MIN, A + 29 * MIN), first);
    expect(hexesAt(first, A + 14.5 * MIN)).toEqual([hex]);
    expect(hexesAt(second, A + 14.5 * MIN)).toEqual([hex]);
    const third = buildReplayIndex(windowOf(A + 16 * MIN, A + 46 * MIN), second);
    expect(hexesAt(third, A + 30 * MIN)).toEqual([hex]);
    // On its own, a window mints the key from its own first row.
    const alone = buildReplayIndex(windowOf(A - MIN, A + 29 * MIN));
    expect(hexesAt(alone, A + 14.5 * MIN)).toEqual([multinodeHexFromKey(mintedDarkKey(windowOf(A - MIN, A + 29 * MIN).rows[0]))]);
    expect(hexesAt(alone, A + 14.5 * MIN)).not.toEqual([hex]);
  });

  it("carries nothing from an index that shares no row", () => {
    const first = buildReplayIndex(windowOf(A - 15 * MIN, A + 5 * MIN));
    const far = windowOf(A + 20 * MIN, A + 45 * MIN);
    expect(hexesAt(buildReplayIndex(far, first), A + 30 * MIN)).toEqual([multinodeHexFromKey(mintedDarkKey(far.rows[0]))]);
  });

  it("leaves keyed tracks as they are, though the index before linked their rows", () => {
    const resp = KEYED.dark;
    const unkeyed = buildReplayIndex({ ...resp, rows: resp.rows.map((r) => ({ ...r, track_key: null, lane: null })) });
    expect(unkeyed.trackCount).toBeGreaterThan(0);
    const withPrevious = buildReplayIndex(resp, unkeyed);
    const alone = buildReplayIndex(resp);
    for (const r of resp.rows.filter((_, i) => i % 7 === 0)) {
      expect(synthesizeFrame(withPrevious, published(r))).toEqual(synthesizeFrame(alone, published(r)));
    }
  });
});

/* ---- rule 2: publication gate ----------------------------------- */

describe("synthesizeFrame: rule 2, a row exists once it is published", () => {
  it("hides a row whose solve epoch has passed until the server published it", () => {
    const resp = KEYED.known;
    const index = buildReplayIndex(resp);
    let checked = 0;
    for (const [key, rows] of tracksByKey(resp)) {
      const hex = multinodeHexFromKey(key);
      for (let i = 1; i < rows.length; i++) {
        const prev = rows[i - 1];
        const p = rows[i];
        const from = Math.max(published(prev), p.frame_ts_ms);
        if (from >= published(p)) continue;
        expect(p.frame_ts_ms).toBeLessThanOrEqual(from);
        expect(entryOf(synthesizeFrame(index, from), hex).frame_ts_ms).toBe(prev.frame_ts_ms);
        expect(entryOf(synthesizeFrame(index, published(p) - 1), hex).frame_ts_ms).toBe(prev.frame_ts_ms);
        expect(entryOf(synthesizeFrame(index, published(p)), hex).frame_ts_ms).toBe(p.frame_ts_ms);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(0);
  });

  it("shows nothing of a track before its first row is published", () => {
    const resp = KEYED.known;
    const index = buildReplayIndex(resp);
    for (const [key, rows] of tracksByKey(resp)) {
      const first = rows[0];
      const hex = multinodeHexFromKey(key);
      expect(first.solve_ts_ms).toBeGreaterThan(first.frame_ts_ms);
      expect(entryOf(synthesizeFrame(index, first.frame_ts_ms), hex)).toBeUndefined();
      expect(entryOf(synthesizeFrame(index, published(first) - 1), hex)).toBeUndefined();
      expect(entryOf(synthesizeFrame(index, published(first)), hex)?.frame_ts_ms).toBe(first.frame_ts_ms);
    }
  });

  it("publishes a row stamped before its own epoch at its epoch", () => {
    const index = buildReplayIndex(response([row({ solve_ts_ms: T0 - 200 })]));
    expect(synthesizeFrame(index, T0 - 1).aircraft).toEqual([]);
    expect(synthesizeFrame(index, T0).aircraft).toHaveLength(1);
  });

  it("shows the latest published row when publication order differs from frame order", () => {
    // Measured second, published first.
    const late = row({ frame_ts_ms: T0, solve_ts_ms: T0 + 3000, lat: 34.85 });
    const early = row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1300, lat: 34.86 });
    const index = buildReplayIndex(response([late, early]));
    const first = synthesizeFrame(index, T0 + 1300).aircraft[0];
    expect(first.frame_ts_ms).toBe(early.frame_ts_ms);
    const second = synthesizeFrame(index, T0 + 3000).aircraft[0];
    expect(second.frame_ts_ms).toBe(late.frame_ts_ms);
    expect(second.lat).toBe(34.85);
    // The trail only grows forward in time, so the older epoch adds no point.
    expect(second.recent_positions).toEqual([pointOf(early)]);
  });
});

/* ---- rule 3: expiry --------------------------------------------- */

describe("synthesizeFrame: rule 3, expiry", () => {
  const keyed: [string, TracksResponse][] = [...ALL.filter(([name]) => !name.startsWith("legacy")), ["legacy adsb", LEGACY.adsb]];

  it.each(keyed)("keeps each %s track for its expiry after its last solve epoch, and not 1 ms longer", (_n, resp) => {
    const index = buildReplayIndex(resp);
    for (const [key, rows] of tracksByKey(resp)) {
      const last = rows[rows.length - 1];
      const hex = multinodeHexFromKey(key);
      const expiry = expiryOf(key);
      expect(published(last)).toBeLessThanOrEqual(last.frame_ts_ms + expiry);
      const kept = entryOf(synthesizeFrame(index, last.frame_ts_ms + expiry), hex);
      expect(kept?.frame_ts_ms).toBe(last.frame_ts_ms);
      expect(kept.seen).toBe(expiry / 1000);
      expect(entryOf(synthesizeFrame(index, last.frame_ts_ms + expiry + 1), hex)).toBeUndefined();
    }
  });

  it("expires dark and follow tracks after 30 s and ADS-B-keyed ones after 60 s", () => {
    const lanes = new Map<string, number>();
    for (const resp of [KEYED.known, KEYED.follow, LEGACY.adsb]) {
      for (const [key, rows] of tracksByKey(resp)) {
        for (const r of rows) lanes.set(`${r.lane ?? "legacy"}:${key.slice(0, 8)}`, expiryOf(key));
      }
    }
    expect(Object.fromEntries(lanes)).toEqual({
      "known:mn-adsb-": 60_000,
      "dark:mn-dark-": 30_000,
      "follow:mn-dark-": 30_000,
      "legacy:mn-adsb-": 60_000,
    });
  });

  it("keeps one track visible while the other in its window has expired", () => {
    const resp = KEYED.dark;
    const tracks = [...tracksByKey(resp)].sort(([, a], [, b]) => a[a.length - 1].frame_ts_ms - b[b.length - 1].frame_ts_ms);
    const [[endedKey, ended], [onKey]] = tracks;
    const index = buildReplayIndex(resp);
    const after = synthesizeFrame(index, ended[ended.length - 1].frame_ts_ms + DARK_EXPIRY_MS + 1);
    expect(after.aircraft.map((a) => a.hex)).toEqual([multinodeHexFromKey(onKey)]);
    expect(multinodeHexFromKey(endedKey)).not.toBe(multinodeHexFromKey(onKey));
  });

  // The server gives a dark track silent past its expiry a new key (the next
  // case), so the dark track here is a legacy one, which linking carries
  // across a silence of up to 60 s.
  it.each([
    ["legacy dark", LEGACY.linking],
    ["known", KEYED.knownGap],
    ["legacy known", LEGACY.adsb],
  ])("drops a %s track inside a gap longer than its expiry and brings it back under its hex", (_lane, resp) => {
    const { key, rows } = mostSilentTrack(resp);
    const hex = multinodeHexFromKey(key);
    const expiry = expiryOf(key);
    const [before, after] = widestGap(rows);
    const gap = after.frame_ts_ms - before.frame_ts_ms;
    expect(gap).toBeGreaterThan(expiry);
    expect(gap).toBeLessThan(TRAIL_STALE_MS);

    const index = buildReplayIndex(resp);
    const at = (c: number) => entryOf(synthesizeFrame(index, c), hex);
    expect(at(before.frame_ts_ms + expiry)?.frame_ts_ms).toBe(before.frame_ts_ms);
    expect(at(before.frame_ts_ms + expiry + 1)).toBeUndefined();
    expect(at((before.frame_ts_ms + expiry + published(after)) / 2)).toBeUndefined();
    expect(at(published(after) - 1)).toBeUndefined();
    const back = at(published(after));
    expect(back?.frame_ts_ms).toBe(after.frame_ts_ms);
    // Shorter than a trail's life, so the trail spans the gap.
    expect(back.recent_positions[0][3]).toBeLessThan(before.frame_ts_ms / 1000);
    expect(back.recent_positions[back.recent_positions.length - 2]).toEqual(pointOf(before));
  });

  it("expires a dark track silent past its expiry, and shows the solves after the silence under the new key the server gave them", () => {
    const resp = KEYED.darkRemint;
    const tracks = [...tracksByKey(resp)].sort(([, a], [, b]) => a[0].frame_ts_ms - b[0].frame_ts_ms);
    expect(tracks).toHaveLength(2);
    const [[oldKey, old], [newKey, resumed]] = tracks;
    const last = old[old.length - 1];
    const first = resumed[0];
    expect(first.frame_ts_ms - last.frame_ts_ms).toBeGreaterThan(DARK_EXPIRY_MS);
    const [oldHex, newHex] = [multinodeHexFromKey(oldKey), multinodeHexFromKey(newKey)];
    expect(newHex).not.toBe(oldHex);

    const index = buildReplayIndex(resp);
    const at = (c: number) => synthesizeFrame(index, c).aircraft;
    expect(at(last.frame_ts_ms + DARK_EXPIRY_MS).map((a) => [a.hex, a.frame_ts_ms])).toEqual([[oldHex, last.frame_ts_ms]]);
    expect(at(last.frame_ts_ms + DARK_EXPIRY_MS + 1)).toEqual([]);
    expect(at(published(first) - 1)).toEqual([]);
    const shown = at(published(first));
    expect(shown).toHaveLength(1);
    const [back] = shown;
    expect(back).toMatchObject({ hex: newHex, frame_ts_ms: first.frame_ts_ms });
    // A trail of its own: nothing of the expired track's.
    expect(back.recent_positions).toEqual([pointOf(first)]);
  });
});

/* ---- rule 4: entry fields --------------------------------------- */

describe("synthesizeFrame: rule 4, entry fields", () => {
  // Expected values from backend/services/aircraft_feed.py multinode_to_aircraft,
  // run on these rows (TRACK_GS_SOURCE=solve: the archive has no KF velocity).
  // The rows sit on awkward rounding points: 34.851065, -82.394845 and 1.175
  // are decimal ties Python rounds by their binary value (Math.round(x * 1e5)
  // gets the first two wrong), and a heading of -0.02 degrees wraps to 360.0.
  it("matches the backend's entry for a known row", () => {
    const prev = row({
      track_key: "mn-adsb-a1b2c3", lane: "known", adsb_hex: "a1b2c3",
      frame_ts_ms: T0 - 1000, solve_ts_ms: T0 - 300, lat: 34.849, lon: -82.396, vel_east_ms: 230, vel_north_ms: -120,
    });
    const known = row({
      track_key: "mn-adsb-a1b2c3", lane: "known", adsb_hex: "a1b2c3",
      frame_ts_ms: T0, solve_ts_ms: T0 + 830, lat: 34.851065, lon: -82.394845, alt_m: 10469.3,
      vel_east_ms: 201.93, vel_north_ms: -107.24, vel_up_ms: 0.4, n_nodes: 4,
      node_refs: ["nde632b92409d", "ndea08ff49b6f", "ndede10d4e8eb", "nde43069c43a8"],
      pos_sigma_m: 131.94, rms_delay_us: 0.21245, rms_doppler_hz: 1.175, is_anomalous: false, anomaly_types: [],
    });
    const frame = synthesizeFrame(buildReplayIndex(response([prev, known])), T0 + 2345);
    expect(frame.aircraft).toHaveLength(1);
    const { recent_positions, ...entry } = frame.aircraft[0];
    expect(entry).toStrictEqual({
      hex: "mnf2707d1d71",
      type: "multinode_solve",
      flight: "MN4N",
      alt_baro: 34348,
      alt_geom: 34348,
      gs: 444.4,
      track: 118.0,
      lat: 34.85106,
      lon: -82.39485,
      solve_lat: 34.85106,
      solve_lon: -82.39485,
      seen: 2.3,
      multinode: true,
      n_nodes: 4,
      contributing_node_refs: ["nde632b92409d", "ndea08ff49b6f", "ndede10d4e8eb", "nde43069c43a8"],
      rms_delay: 0.212,
      rms_doppler: 1.18,
      position_source: "multinode_solve",
      is_anomalous: false,
      anomaly_types: [],
      max_velocity_ms: 259.4,
      adsb_assisted: true,
      adsb_hex: "a1b2c3",
      pos_sigma_m: 131.9,
      frame_ts_ms: T0,
      solve_ts_ms: T0 + 830,
    });
    expect(recent_positions).toEqual([pointOf(prev), pointOf(known)]);
  });

  it("matches the backend's entry for a dark row", () => {
    const dark = row({
      track_key: "mn-dark-1760817600000-34.850--82.390", lane: "dark",
      frame_ts_ms: T0, solve_ts_ms: T0 + 640, lat: 34.85, lon: -82.39, alt_m: 2310.0,
      vel_east_ms: -0.04, vel_north_ms: 108.4, n_nodes: 3,
      node_refs: ["nde0123abcdef", "nde4567abcdef", "nde89abcdef01"],
      pos_sigma_m: 870.0, rms_delay_us: 0.41, rms_doppler_hz: 3.2, is_anomalous: true, anomaly_types: ["speed", "altitude"],
    });
    const { recent_positions, ...entry } = synthesizeFrame(buildReplayIndex(response([dark])), T0 + 640).aircraft[0];
    expect(entry).toStrictEqual({
      hex: "mn3fe22f694d",
      type: "multinode_solve",
      flight: "MN3N",
      alt_baro: 7579,
      alt_geom: 7579,
      gs: 210.7,
      track: 360.0,
      lat: 34.85,
      lon: -82.39,
      solve_lat: 34.85,
      solve_lon: -82.39,
      seen: 0.6,
      multinode: true,
      n_nodes: 3,
      contributing_node_refs: ["nde0123abcdef", "nde4567abcdef", "nde89abcdef01"],
      rms_delay: 0.41,
      rms_doppler: 3.2,
      position_source: "multinode_solve",
      is_anomalous: true,
      anomaly_types: ["altitude", "speed"],
      max_velocity_ms: 108.4,
      adsb_assisted: false,
      pos_sigma_m: 870,
      frame_ts_ms: T0,
      solve_ts_ms: T0 + 640,
    });
    expect(recent_positions).toEqual([pointOf(dark)]);
  });

  it("builds each fixture track's entry from the row it shows", () => {
    for (const [, resp] of ALL) {
      const index = buildReplayIndex(resp);
      for (const [hex, rows] of tracksByHex(resp)) {
        const k = Math.floor(rows.length / 2);
        const r = rows[k];
        const clock = published(r);
        expect(published(rows[k + 1])).toBeGreaterThan(clock);
        const { recent_positions, ...e } = entryOf(synthesizeFrame(index, clock), hex);
        const speeds = rows.slice(0, k + 1).map((x) => Math.sqrt(x.vel_east_ms ** 2 + x.vel_north_ms ** 2));
        const deg = Math.atan2(r.vel_east_ms, r.vel_north_ms) * (180 / Math.PI);
        const key = groupTrackRows(resp.rows).find((g) => multinodeHexFromKey(g.key) === hex).key;
        const assisted = key.startsWith("mn-adsb-");
        expect(e).toStrictEqual({
          hex,
          type: "multinode_solve",
          flight: `MN${r.n_nodes}N`,
          alt_baro: ft(r.alt_m),
          alt_geom: ft(r.alt_m),
          gs: py(Math.sqrt(r.vel_east_ms ** 2 + r.vel_north_ms ** 2) * 1.94384, 1),
          track: py(deg < 0 ? deg + 360 : deg, 1),
          lat: py(r.lat, 5),
          lon: py(r.lon, 5),
          solve_lat: py(r.lat, 5),
          solve_lon: py(r.lon, 5),
          seen: py((clock - r.frame_ts_ms) / 1000, 1),
          multinode: true,
          n_nodes: r.n_nodes,
          contributing_node_refs: r.node_refs,
          rms_delay: py(r.rms_delay_us, 3),
          rms_doppler: py(r.rms_doppler_hz, 2),
          position_source: "multinode_solve",
          is_anomalous: r.is_anomalous === true,
          anomaly_types: r.anomaly_types.slice().sort(),
          max_velocity_ms: py(Math.max(...speeds), 1),
          adsb_assisted: assisted,
          ...(assisted ? { adsb_hex: key.slice(8) } : {}),
          ...(r.pos_sigma_m == null ? {} : { pos_sigma_m: py(r.pos_sigma_m, 1) }),
          frame_ts_ms: r.frame_ts_ms,
          solve_ts_ms: r.solve_ts_ms,
        });
        expect(recent_positions[recent_positions.length - 1]).toEqual(pointOf(r));
      }
    }
  });

  it("omits what a row leaves null, and keeps the track's max speed across a row without velocity", () => {
    const bare = { alt_m: null, vel_east_ms: null, vel_north_ms: null, rms_delay_us: null, rms_doppler_hz: null, pos_sigma_m: null };
    const rows = [
      row({ ...bare }),
      row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.851, vel_east_ms: 30, vel_north_ms: 40 }),
      row({ frame_ts_ms: T0 + 2000, solve_ts_ms: T0 + 2500, lat: 34.852, ...bare }),
    ];
    const index = buildReplayIndex(response(rows));
    const at = (c: number) => synthesizeFrame(index, c).aircraft[0];
    const omitted = ["alt_baro", "alt_geom", "gs", "track", "rms_delay", "rms_doppler", "pos_sigma_m"];

    const first = at(T0 + 500);
    for (const field of [...omitted, "max_velocity_ms", "adsb_hex"]) expect(first, field).not.toHaveProperty(field);
    expect(first.recent_positions).toEqual([[34.85, -82.39, 0, py(T0 / 1000, 1)]]);

    expect(at(T0 + 1500)).toMatchObject({ gs: py(50 * 1.94384, 1), track: 36.9, max_velocity_ms: 50 });

    const third = at(T0 + 2500);
    for (const field of omitted) expect(third, field).not.toHaveProperty(field);
    expect(third.max_velocity_ms).toBe(50);
  });

  it("reads a null is_anomalous as false", () => {
    const entry = synthesizeFrame(buildReplayIndex(response([row({ is_anomalous: null })])), T0 + 500).aircraft[0];
    expect(entry.is_anomalous).toBe(false);
  });

  it("marks legacy ADS-B rows assisted under the hex of their mn-adsb key", () => {
    const resp = LEGACY.adsb;
    const index = buildReplayIndex(resp);
    const hexes = [...new Set(resp.rows.map((r) => r.adsb_hex))];
    const shown = new Set<string>();
    for (const r of resp.rows) {
      for (const a of synthesizeFrame(index, published(r)).aircraft) {
        shown.add(JSON.stringify([a.hex, a.adsb_assisted, a.adsb_hex]));
        expect(a.is_anomalous).toBe(false);
        expect(a.anomaly_types).toEqual([]);
        expect(a).not.toHaveProperty("pos_sigma_m");
      }
    }
    expect([...shown].sort()).toEqual(hexes.map((h) => JSON.stringify([multinodeHexFromKey(`mn-adsb-${h}`), true, h])).sort());
  });

  it("builds fleet=all's mixed and synthetic rows as it builds real ones: node_kind plays no part, and synth- refs pass through", () => {
    const resp = FLEET_ALL.mixed;
    expect(new Set(resp.rows.map((r) => r.node_kind))).toEqual(new Set(["real", "synthetic", "mixed"]));
    const index = buildReplayIndex(resp);
    let synthetic = 0;
    for (const r of resp.rows) {
      const entry = entryOf(synthesizeFrame(index, published(r)), multinodeHexFromKey(r.track_key));
      expect(entry.frame_ts_ms).toBe(r.frame_ts_ms);
      expect(entry.contributing_node_refs).toEqual(r.node_refs);
      if (r.node_refs.some((ref) => ref.startsWith("synth-"))) synthetic++;
    }
    expect(synthetic).toBeGreaterThan(0);
    // The same rows under any other node_kind give the same frames.
    for (const kind of ["real", null] as const) {
      const relabelled = buildReplayIndex({ ...resp, rows: resp.rows.map((r) => ({ ...r, node_kind: kind })) });
      for (const r of resp.rows) expect(synthesizeFrame(relabelled, published(r))).toEqual(synthesizeFrame(index, published(r)));
    }
  });

  it("never emits what the public feed withholds or the archive cannot know", () => {
    const withheld = ["contributing_node_ids", "messages", "rssi", "ground_truth_hex", "pos_sigma_vel_ms", "gs_source"];
    let seen = 0;
    for (const [, resp] of ALL) {
      const index = buildReplayIndex(resp);
      for (const r of resp.rows) {
        for (const a of synthesizeFrame(index, published(r)).aircraft) {
          for (const field of withheld) expect(a).not.toHaveProperty(field);
          seen++;
        }
      }
    }
    expect(seen).toBeGreaterThan(1000);
  });
});

/* ---- rule 5: recent_positions ----------------------------------- */

describe("synthesizeFrame: rule 5, recent_positions", () => {
  it.each(ALL)("keeps every %s trail ascending, deduped, bounded and no later than the clock", (_n, resp) => {
    const index = buildReplayIndex(resp);
    const tracks = tracksByHex(resp);
    const first = Math.min(...resp.rows.map(published));
    const last = Math.max(...resp.rows.map((r) => r.frame_ts_ms)) + ADSB_EXPIRY_MS;
    let entries = 0;
    for (let clock = first; clock <= last; clock += 700) {
      for (const a of synthesizeFrame(index, clock).aircraft) {
        const rows = tracks.get(a.hex).filter((r) => published(r) <= clock);
        const shown = rows[rows.length - 1];
        expect(a.frame_ts_ms).toBe(shown.frame_ts_ms);
        const pts = a.recent_positions;
        const points = new Set(rows.map((r) => JSON.stringify(pointOf(r))));
        expect(pts.length).toBeGreaterThan(0);
        expect(pts.length).toBeLessThanOrEqual(MAX_TRAIL_POINTS);
        for (const p of pts) expect(points.has(JSON.stringify(p))).toBe(true);
        for (let i = 1; i < pts.length; i++) {
          expect(pts[i][3]).toBeGreaterThan(pts[i - 1][3]);
          expect(pts[i][3] - pts[i - 1][3]).toBeLessThanOrEqual(TRAIL_STALE_MS / 1000 + 0.1);
          const moved = Math.abs(pts[i][0] - pts[i - 1][0]) >= TRAIL_DEDUP_DEG || Math.abs(pts[i][1] - pts[i - 1][1]) >= TRAIL_DEDUP_DEG;
          expect(moved).toBe(true);
        }
        const end = pts[pts.length - 1];
        expect(end[3]).toBeLessThanOrEqual(clock / 1000);
        const isShown = JSON.stringify(end) === JSON.stringify(pointOf(shown));
        const dedupedShown = Math.abs(end[0] - shown.lat) < TRAIL_DEDUP_DEG && Math.abs(end[1] - shown.lon) < TRAIL_DEDUP_DEG;
        expect(isShown || dedupedShown).toBe(true);
        entries++;
      }
    }
    expect(entries).toBeGreaterThan(50);
  });

  it("drops a row within 0.00005 degree of the last point in both axes, and ends on that point while it is current", () => {
    const rows = [
      row({ lat: 34.85, lon: -82.39 }),
      row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.85004, lon: -82.39004 }),
      row({ frame_ts_ms: T0 + 2000, solve_ts_ms: T0 + 2500, lat: 34.8501, lon: -82.39 }),
      // Within the band in latitude only: a point.
      row({ frame_ts_ms: T0 + 3000, solve_ts_ms: T0 + 3500, lat: 34.85014, lon: -82.3902 }),
    ];
    const index = buildReplayIndex(response(rows));
    const deduped = synthesizeFrame(index, T0 + 1500).aircraft[0];
    expect(deduped.lat).toBe(34.85004);
    expect(deduped.recent_positions).toEqual([pointOf(rows[0])]);
    expect(synthesizeFrame(index, T0 + 3500).aircraft[0].recent_positions).toEqual([rows[0], rows[2], rows[3]].map(pointOf));
  });

  it("cuts the trail at a gap over 300 s, and not at exactly 300 s", () => {
    const trailAfter = (gapMs: number, stationary = false) => {
      const before = run(5);
      const lastBefore = before[before.length - 1];
      const after = run(3, (k) => ({
        frame_ts_ms: lastBefore.frame_ts_ms + gapMs + k * 1000,
        solve_ts_ms: lastBefore.frame_ts_ms + gapMs + k * 1000 + 500,
        lat: stationary && k === 0 ? lastBefore.lat : 35 + k * 0.001,
      }));
      const index = buildReplayIndex(response([...before, ...after]));
      return synthesizeFrame(index, published(after[2])).aircraft[0].recent_positions;
    };
    expect(trailAfter(TRAIL_STALE_MS + 1)).toHaveLength(3);
    expect(trailAfter(TRAIL_STALE_MS)).toHaveLength(8);
    // The live trail was collected, so a target back where it left starts a new one.
    const restarted = trailAfter(TRAIL_STALE_MS + 1, true);
    expect(restarted).toHaveLength(3);
    expect(restarted[0][3]).toBeGreaterThan(T0 / 1000 + 300);
  });

  it("keeps the last 400 points", () => {
    const rows = run(450);
    const index = buildReplayIndex(response(rows));
    const full = synthesizeFrame(index, published(rows[399])).aircraft[0].recent_positions;
    expect(full).toHaveLength(MAX_TRAIL_POINTS);
    expect(full[0]).toEqual(pointOf(rows[0]));
    const capped = synthesizeFrame(index, published(rows[449])).aircraft[0].recent_positions;
    expect(capped).toHaveLength(MAX_TRAIL_POINTS);
    expect(capped).toEqual(rows.slice(50).map(pointOf));
  });

  it("holds each trail point as [lat 6 dp, lon 6 dp, alt ft, epoch s 1 dp]", () => {
    const r = row({ lat: 34.85123449, lon: -82.39876551, alt_m: 3048.2, frame_ts_ms: T0 + 1249, solve_ts_ms: T0 + 1900 });
    const [point] = synthesizeFrame(buildReplayIndex(response([r])), T0 + 1900).aircraft[0].recent_positions;
    expect(point).toEqual([34.851234, -82.398766, 10001, py((T0 + 1249) / 1000, 1)]);
  });
});

/* ---- rule 6: the frame ------------------------------------------ */

describe("synthesizeFrame: rule 6, the frame", () => {
  it("has the live feed's frame shape, with aircraft sorted by hex", () => {
    const resp = KEYED.known;
    const index = buildReplayIndex(resp);
    const both = [...tracksByKey(resp).values()];
    const clock = Math.max(...both.map((rows) => published(rows[0]))) + 5_000;
    const frame = synthesizeFrame(index, clock);
    expect(Object.keys(frame).sort()).toEqual(
      ["aircraft", "anomaly_hexes", "detecting_nodes", "detection_arcs", "ground_truth", "ground_truth_meta", "now"].sort(),
    );
    expect(frame).toMatchObject({
      now: clock / 1000,
      detection_arcs: [],
      detecting_nodes: {},
      anomaly_hexes: [],
      ground_truth: {},
      ground_truth_meta: {},
    });
    const hexes = frame.aircraft.map((a) => a.hex);
    expect(hexes).toHaveLength(both.length);
    expect(both.length).toBeGreaterThan(1);
    expect(hexes).toEqual([...hexes].sort());
  });

  it("sorts every frame's aircraft by hex", () => {
    for (const [, resp] of ALL) {
      const index = buildReplayIndex(resp);
      for (const r of resp.rows) {
        const hexes = synthesizeFrame(index, published(r)).aircraft.map((a) => a.hex);
        for (let i = 1; i < hexes.length; i++) expect(hexes[i] > hexes[i - 1]).toBe(true);
      }
    }
  });

  it("is empty before the window's first publish and after its last track expired", () => {
    const resp = KEYED.known;
    const index = buildReplayIndex(resp);
    const first = Math.min(...resp.rows.map(published));
    const lastFrame = Math.max(...resp.rows.map((r) => r.frame_ts_ms));
    expect(synthesizeFrame(index, first - 1).aircraft).toEqual([]);
    expect(synthesizeFrame(index, lastFrame + ADSB_EXPIRY_MS + 1)).toEqual({
      now: (lastFrame + ADSB_EXPIRY_MS + 1) / 1000,
      aircraft: [],
      detection_arcs: [],
      detecting_nodes: {},
      anomaly_hexes: [],
      ground_truth: {},
      ground_truth_meta: {},
    });
  });

  it("lists a hex in anomaly_hexes while its current row is anomalous, with that row's types", () => {
    const resp = KEYED.anomalous;
    const [[key, rows]] = [...tracksByKey(resp)];
    const hex = multinodeHexFromKey(key);
    const index = buildReplayIndex(resp);
    // The track turns anomalous mid-cut and later adds a second type.
    const firstBad = rows.findIndex((r) => r.is_anomalous === true);
    const grown = rows.findIndex((r) => r.anomaly_types.length > rows[firstBad].anomaly_types.length);
    expect(firstBad).toBeGreaterThan(0);
    expect(grown).toBeGreaterThan(firstBad);

    const before = synthesizeFrame(index, published(rows[firstBad]) - 1);
    expect(before.anomaly_hexes).toEqual([]);
    expect(entryOf(before, hex)).toMatchObject({ is_anomalous: false, anomaly_types: [] });

    for (const r of rows) {
      const frame = synthesizeFrame(index, published(r));
      expect(frame.anomaly_hexes).toEqual(r.is_anomalous ? [hex] : []);
      expect(entryOf(frame, hex)).toMatchObject({ is_anomalous: r.is_anomalous, anomaly_types: r.anomaly_types.slice().sort() });
    }
  });

  it("drops a hex from anomaly_hexes once its current row is no longer anomalous", () => {
    // The fixture's track stays anomalous to its end, as the seed latches the flag.
    const rows = run(6, (k) => (k === 2 || k === 3 ? { is_anomalous: true, anomaly_types: ["speed", "altitude"] } : {}));
    const index = buildReplayIndex(response(rows));
    const hex = multinodeHexFromKey("mn-dark-test");
    expect(synthesizeFrame(index, published(rows[2]) - 1).anomaly_hexes).toEqual([]);
    expect(synthesizeFrame(index, published(rows[2])).anomaly_hexes).toEqual([hex]);
    expect(synthesizeFrame(index, published(rows[4]) - 1).anomaly_hexes).toEqual([hex]);
    const after = synthesizeFrame(index, published(rows[4]));
    expect(after.anomaly_hexes).toEqual([]);
    expect(entryOf(after, hex)).toMatchObject({ is_anomalous: false, anomaly_types: [] });
  });

  it("depends only on the index and the clock", () => {
    const resp = KEYED.known;
    const clocks = resp.rows.filter((_, i) => i % 17 === 0).map(published);
    const index = buildReplayIndex(resp);
    const forward = clocks.map((c) => synthesizeFrame(index, c));
    const backward = clocks.slice().reverse().map((c) => synthesizeFrame(index, c)).reverse();
    expect(backward).toEqual(forward);
    const fresh = buildReplayIndex(resp);
    expect(clocks.map((c) => synthesizeFrame(fresh, c))).toEqual(forward);
  });

  it("does not hold on to the response's rows", () => {
    const resp = structuredClone(KEYED.known);
    const index = buildReplayIndex(resp);
    const clock = published(resp.rows[100]);
    const before = synthesizeFrame(index, clock);
    for (const r of resp.rows) {
      r.lat = 0;
      r.node_refs.length = 0;
      r.anomaly_types.push("mutated");
    }
    resp.rows.length = 0;
    expect(synthesizeFrame(index, clock)).toEqual(before);
  });
});

