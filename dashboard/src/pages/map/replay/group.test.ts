import { describe, expect, it } from "vitest";
import { distanceKm } from "../../../utils/geo";
import {
  ADSB_KEY_PREFIX,
  LINK_MAX_DISTANCE_M,
  LINK_MAX_GAP_MS,
  groupTrackRows,
  mintedDarkKey,
  mintedRowsOf,
  type MintedRows,
} from "./group";
import { multinodeHexFromKey } from "./hex";
import type { TrackRow } from "./types";
import { FIXTURES, FLEET_ALL, KEYED, LEGACY, LINKED_TRACKS, PRIVATE_TRACK } from "./__fixtures__/scenarios";

const T0 = Date.UTC(2026, 9, 18, 10, 0, 0);
const M_PER_DEG_LAT = (6_371_000 * Math.PI) / 180; // distanceKm's sphere

/** A legacy dark row (no key, no hex, no lane) unless overridden. */
function row(over: Partial<TrackRow>): TrackRow {
  return {
    frame_ts_ms: T0,
    solve_ts_ms: T0 + 500,
    track_key: null,
    lane: null,
    node_kind: "real",
    lat: 34.85,
    lon: -82.39,
    alt_m: 1000,
    vel_east_ms: 0,
    vel_north_ms: 100,
    vel_up_ms: 0,
    n_nodes: 2,
    node_refs: ["nde0000000001", "nde0000000002"],
    adsb_hex: null,
    pos_sigma_m: null,
    rms_delay_us: 0.3,
    rms_doppler_hz: 2,
    is_anomalous: null,
    anomaly_types: [],
    ...over,
  };
}

/** Each group's rows, as the rows themselves. */
const members = (rows: TrackRow[]) => groupTrackRows(rows).map((g) => ({ ...g, members: g.rows.map((i) => rows[i]) }));

const metres = (a: TrackRow, b: TrackRow) => distanceKm(a.lat, a.lon, b.lat, b.lon) * 1000;

/** The longest silence between a track's consecutive rows, in frame time. */
const widestGapMs = (rows: TrackRow[]) => Math.max(...rows.slice(1).map((r, i) => r.frame_ts_ms - rows[i].frame_ts_ms));

describe("the fixtures", () => {
  it.each(Object.entries(FIXTURES))("%s is a response: count is its rows, each solved in [from, to), in the API's order", (_name, resp) => {
    expect(resp.count).toBe(resp.rows.length);
    expect(resp.rows.length).toBeGreaterThan(0);
    for (const [i, r] of resp.rows.entries()) {
      expect(r.solve_ts_ms).toBeGreaterThanOrEqual(resp.from);
      expect(r.solve_ts_ms).toBeLessThan(resp.to);
      if (i > 0) {
        const p = resp.rows[i - 1];
        expect(p.frame_ts_ms < r.frame_ts_ms || (p.frame_ts_ms === r.frame_ts_ms && p.solve_ts_ms <= r.solve_ts_ms)).toBe(true);
      }
    }
  });

  it.each(Object.entries(FIXTURES))("%s holds nothing of the private node's track, and no private id", (_name, resp) => {
    for (const r of resp.rows) {
      expect(r.node_refs).not.toContain(PRIVATE_TRACK.nodeRef);
      expect(r.track_key).not.toBe(PRIVATE_TRACK.trackKey);
      expect(r.adsb_hex).not.toBe(PRIVATE_TRACK.adsbHex);
    }
    const text = JSON.stringify(resp);
    expect(text).not.toContain(PRIVATE_TRACK.nodeRef);
    expect(text).not.toContain(PRIVATE_TRACK.adsbHex);
    // A node's private id (ret + 8 hex digits) never leaves the server.
    expect(text).not.toMatch(/ret[0-9a-f]{8}/);
  });
});

describe("groupTrackRows: rule 1a, track_key", () => {
  it.each(Object.entries({ ...KEYED, ...FLEET_ALL }))("groups the %s scenario by track_key", (_name, resp) => {
    const groups = members(resp.rows);
    const keys = new Set(resp.rows.map((r) => r.track_key));
    expect(keys.has(null)).toBe(false);
    expect(groups.map((g) => g.key).sort()).toEqual([...keys].sort());
    for (const g of groups) {
      expect(g.linked).toBe(false);
      expect(g.members.every((r) => r.track_key === g.key)).toBe(true);
    }
    expect(groups.reduce((n, g) => n + g.rows.length, 0)).toBe(resp.rows.length);
  });

  it("prefers track_key over adsb_hex", () => {
    const groups = groupTrackRows([row({ track_key: "mn-dark-3f9a", adsb_hex: "a1b2c3", lane: "dark" })]);
    expect(groups.map((g) => g.key)).toEqual(["mn-dark-3f9a"]);
  });

  it("puts rows with one key in one track however far apart they are", () => {
    const rows = [
      row({ track_key: "mn-dark-x", lane: "dark" }),
      row({ track_key: "mn-dark-x", lane: "dark", frame_ts_ms: T0 + 200_000, solve_ts_ms: T0 + 200_500, lat: 35.5 }),
    ];
    expect(groupTrackRows(rows)).toEqual([{ key: "mn-dark-x", linked: false, rows: [0, 1] }]);
  });

  it("keeps a dark key whose solves move to the follow lane in one track", () => {
    const { rows } = KEYED.follow;
    const lanes = rows.map((r) => r.lane);
    // One run of dark rows, then one of follow rows.
    expect(lanes[0]).toBe("dark");
    expect(lanes.filter((lane, i) => i > 0 && lane !== lanes[i - 1])).toEqual(["follow"]);
    expect(groupTrackRows(rows)).toEqual([{ key: rows[0].track_key, linked: false, rows: rows.map((_, i) => i) }]);
  });
});

describe("groupTrackRows: rule 1b, adsb_hex", () => {
  it("keys legacy rows that carry an adsb_hex as mn-adsb-<hex>", () => {
    const { rows } = LEGACY.adsb;
    const groups = members(rows);
    const hexes = new Set(rows.map((r) => r.adsb_hex));
    expect(hexes.has(null)).toBe(false);
    expect(rows.every((r) => r.track_key === null)).toBe(true);
    expect(groups.map((g) => g.key).sort()).toEqual([...hexes].map((h) => ADSB_KEY_PREFIX + h).sort());
    for (const g of groups) {
      expect(g.linked).toBe(false);
      expect(g.members.every((r) => ADSB_KEY_PREFIX + r.adsb_hex === g.key)).toBe(true);
    }
  });

  it("keeps an aircraft in one track by hex across a silence linking would have split it at", () => {
    const { rows } = LEGACY.adsb;
    const gapped = members(rows).find((g) => widestGapMs(g.members) > LINK_MAX_GAP_MS);
    expect(gapped).toBeDefined();
    // Without its hex, the same rows link into more than one track.
    const linked = groupTrackRows(rows.map((r) => ({ ...r, adsb_hex: null })));
    const owners = new Set(gapped.rows.map((i) => linked.findIndex((g) => g.rows.includes(i))));
    expect(owners.size).toBeGreaterThan(1);
  });

  it("keeps two aircraft apart by hex even where linking would have merged them", () => {
    // 110 m and a second apart.
    const rows = [
      row({ adsb_hex: "a1b2c3" }),
      row({ adsb_hex: "d4e5f6", frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.851 }),
    ];
    expect(groupTrackRows(rows).map((g) => [g.key, g.rows])).toEqual([
      ["mn-adsb-a1b2c3", [0]],
      ["mn-adsb-d4e5f6", [1]],
    ]);
    expect(groupTrackRows(rows.map((r) => ({ ...r, adsb_hex: null })))).toHaveLength(1);
  });
});

describe("groupTrackRows: rule 1c, linking legacy dark rows", () => {
  it("keeps two concurrent targets more than 3 km apart in two tracks", () => {
    const { rows } = LEGACY.linking;
    expect(rows.every((r) => r.track_key === null && r.adsb_hex === null)).toBe(true);
    const groups = members(rows);
    expect(groups).toHaveLength(LINKED_TRACKS.linking);
    expect(groups.every((g) => g.linked)).toBe(true);
    const [a, b] = groups;
    // Concurrent: each starts before the other ends.
    expect(a.members[0].frame_ts_ms).toBeLessThan(b.members[b.members.length - 1].frame_ts_ms);
    expect(b.members[0].frame_ts_ms).toBeLessThan(a.members[a.members.length - 1].frame_ts_ms);
    // Interleaved in the response, so they really were linked row by row.
    const owner = new Map<number, string>();
    for (const g of groups) for (const i of g.rows) owner.set(i, g.key);
    const switches = rows.slice(1).filter((_, i) => owner.get(i) !== owner.get(i + 1)).length;
    expect(switches).toBeGreaterThan(20);
    // Every row of one is over 3 km from the other's row nearest in time.
    for (const ra of a.members) {
      const nearest = b.members.reduce((best, rb) =>
        Math.abs(rb.frame_ts_ms - ra.frame_ts_ms) < Math.abs(best.frame_ts_ms - ra.frame_ts_ms) ? rb : best,
      );
      expect(metres(ra, nearest)).toBeGreaterThan(LINK_MAX_DISTANCE_M);
    }
  });

  it("links across one target's silence of over 30 s, up to 60 s", () => {
    const groups = members(LEGACY.linking.rows);
    const gaps = groups.flatMap((g) => g.members.slice(1).map((r, i) => r.frame_ts_ms - g.members[i].frame_ts_ms));
    const long = gaps.filter((ms) => ms > 30_000);
    expect(long).toHaveLength(1);
    expect(long[0]).toBeLessThanOrEqual(LINK_MAX_GAP_MS);
    for (const g of groups) {
      for (let i = 1; i < g.members.length; i++) {
        expect(metres(g.members[i - 1], g.members[i])).toBeLessThanOrEqual(LINK_MAX_DISTANCE_M);
      }
    }
  });

  it("splits two runs within 3 km of each other but more than 60 s apart", () => {
    const groups = members(LEGACY.split.rows);
    expect(groups).toHaveLength(LINKED_TRACKS.split);
    const [first, second] = groups;
    const end = first.members[first.members.length - 1];
    const start = second.members[0];
    expect(start.frame_ts_ms - end.frame_ts_ms).toBeGreaterThan(LINK_MAX_GAP_MS);
    expect(metres(end, start)).toBeLessThanOrEqual(LINK_MAX_DISTANCE_M);
  });

  it("mints the backend's dark-key format from the first row", () => {
    const first = row({ frame_ts_ms: 1760817600000, solve_ts_ms: 1760817600700, lat: 34.85, lon: -82.39 });
    expect(mintedDarkKey(first)).toBe("mn-dark-1760817600000-34.850--82.390");
    const [g] = groupTrackRows([first, row({ frame_ts_ms: 1760817601000, solve_ts_ms: 1760817601500, lat: 34.851 })]);
    expect(g).toEqual({ key: "mn-dark-1760817600000-34.850--82.390", linked: true, rows: [0, 1] });
    expect(multinodeHexFromKey(g.key)).toBe("mn3fe22f694d");
    // The key the server minted for a dark track's solves after a silence, from the first of them.
    const { rows } = KEYED.darkRemint;
    const resumed = rows.filter((r) => r.track_key !== rows[0].track_key);
    expect(resumed.length).toBeGreaterThan(0);
    expect(mintedDarkKey(resumed[0])).toBe(resumed[0].track_key);
  });

  it("links at exactly 60 s and not 1 ms later", () => {
    const at = (ms: number) => [row({}), row({ frame_ts_ms: T0 + ms, solve_ts_ms: T0 + ms + 500, lat: 34.851 })];
    expect(groupTrackRows(at(LINK_MAX_GAP_MS))).toHaveLength(1);
    expect(groupTrackRows(at(LINK_MAX_GAP_MS + 1))).toHaveLength(2);
  });

  it("links within 3 km and not beyond, north-south and east-west", () => {
    const M_PER_DEG_LON = M_PER_DEG_LAT * Math.cos((34.85 * Math.PI) / 180);
    const away = (dLat: number, dLon: number) => [
      row({}),
      row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.85 + dLat, lon: -82.39 + dLon }),
    ];
    for (const [inside, outside] of [
      [away((LINK_MAX_DISTANCE_M - 0.5) / M_PER_DEG_LAT, 0), away((LINK_MAX_DISTANCE_M + 0.5) / M_PER_DEG_LAT, 0)],
      [away(0, (LINK_MAX_DISTANCE_M - 2) / M_PER_DEG_LON), away(0, (LINK_MAX_DISTANCE_M + 2) / M_PER_DEG_LON)],
    ]) {
      expect(metres(inside[0], inside[1])).toBeLessThanOrEqual(LINK_MAX_DISTANCE_M);
      expect(metres(outside[0], outside[1])).toBeGreaterThan(LINK_MAX_DISTANCE_M);
      expect(groupTrackRows(inside)).toHaveLength(1);
      expect(groupTrackRows(outside)).toHaveLength(2);
    }
  });

  it("never links two rows of the same frame", () => {
    // 220 m apart, so each mints its own key.
    const rows = [row({}), row({ solve_ts_ms: T0 + 600, lat: 34.852 })];
    expect(groupTrackRows(rows).map((g) => g.rows)).toEqual([[0], [1]]);
  });

  it("links only within a lane, a null lane counting as dark", () => {
    const rows = [
      row({ lane: null }),
      row({ lane: "follow", frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.851 }),
      row({ lane: "dark", frame_ts_ms: T0 + 2000, solve_ts_ms: T0 + 2500, lat: 34.852 }),
    ];
    expect(groupTrackRows(rows).map((g) => g.rows)).toEqual([[0, 2], [1]]);
  });

  it("never links to a track the server keyed", () => {
    const rows = [
      row({ track_key: "mn-dark-keyed", lane: "dark" }),
      row({ adsb_hex: "a1b2c3" }),
      row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.8501 }),
    ];
    const groups = groupTrackRows(rows);
    expect(groups.map((g) => [g.key, g.rows])).toEqual([
      ["mn-dark-keyed", [0]],
      ["mn-adsb-a1b2c3", [1]],
      [mintedDarkKey(rows[2]), [2]],
    ]);
  });

  it("links to the nearest candidate", () => {
    const rows = [
      row({ lat: 34.85 }),
      row({ lat: 34.87, solve_ts_ms: T0 + 600 }),
      // In range of both tracks (1.8 km and 0.4 km), nearer the second.
      row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.8665 }),
    ];
    expect(metres(rows[2], rows[1])).toBeLessThan(metres(rows[2], rows[0]));
    expect(metres(rows[2], rows[0])).toBeLessThanOrEqual(LINK_MAX_DISTANCE_M);
    expect(groupTrackRows(rows).map((g) => g.rows)).toEqual([[0], [1, 2]]);
  });

  it("breaks a distance tie toward the most recent last row", () => {
    // Dyadic latitudes 1/64 degree either side of 34.875 on one meridian, so the
    // two distances are equal to the bit. The older track is listed first.
    const rows = [
      row({ lat: 34.859375 }),
      row({ lat: 34.890625, frame_ts_ms: T0 + 500, solve_ts_ms: T0 + 900 }),
      row({ lat: 34.875, frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500 }),
    ];
    expect(metres(rows[2], rows[0])).toBe(metres(rows[2], rows[1]));
    expect(metres(rows[2], rows[0])).toBeLessThanOrEqual(LINK_MAX_DISTANCE_M);
    expect(groupTrackRows(rows).map((g) => g.rows)).toEqual([[0], [1, 2]]);
  });

  it("follows a target from row to row, not from its first row", () => {
    // 2.5 km per row: each row is in range of the last, none of the first.
    const step = 2_500 / M_PER_DEG_LAT;
    const rows = [0, 1, 2, 3].map((k) =>
      row({ frame_ts_ms: T0 + k * 10_000, solve_ts_ms: T0 + k * 10_000 + 500, lat: 34.85 + k * step }),
    );
    expect(groupTrackRows(rows).map((g) => g.rows)).toEqual([[0, 1, 2, 3]]);
  });
});

describe("groupTrackRows: rule 4, a linked track keeps the key of the window before", () => {
  /** One legacy dark target: `n` rows `stepMs` apart from `t0`, 200 m apart. */
  const chain = (n: number, t0 = T0, lat0 = 34.85, stepMs = 2_000) =>
    Array.from({ length: n }, (_, k) =>
      row({ frame_ts_ms: t0 + k * stepMs, solve_ts_ms: t0 + k * stepMs + 500, lat: lat0 + k * 0.0018 }),
    );
  const mintedOf = (rows: TrackRow[]) => mintedRowsOf(groupTrackRows(rows), rows);
  const keysOf = (rows: TrackRow[], previous?: MintedRows) => groupTrackRows(rows, previous).map((g) => g.key);
  /** Several targets' rows in the API's order. */
  const merged = (...targets: TrackRow[][]) =>
    targets.flat().sort((x, y) => x.frame_ts_ms - y.frame_ts_ms || x.solve_ts_ms - y.solve_ts_ms);
  /** A window before whose linked tracks held these rows, each under its key. */
  const heldBefore = (...held: [string, TrackRow][]) =>
    mintedRowsOf(
      held.map(([key], i) => ({ key, linked: true, rows: [i] })),
      held.map(([, r]) => r),
    );
  /** Groups `rows` against `previous`, checking no two tracks share a key; `keyOf` names the track holding a row. */
  const carried = (rows: TrackRow[], previous: MintedRows) => {
    const groups = groupTrackRows(rows, previous);
    const keys = groups.map((g) => g.key);
    expect(new Set(keys).size).toBe(keys.length);
    const keyOf = (r: TrackRow) => groups.find((g) => g.rows.some((i) => rows[i] === r))?.key;
    return { groups, keyOf };
  };

  it("carries the key into a window that shares a row with the one before, and on into one that shares a row only with that", () => {
    const rows = chain(10);
    const first = rows.slice(0, 7);
    const second = rows.slice(4);
    // On its own, each window mints the key from its own first row.
    expect(keysOf(second)).toEqual([mintedDarkKey(rows[4])]);
    expect(keysOf(second, mintedOf(first))).toEqual([mintedDarkKey(rows[0])]);
    const third = rows.slice(8);
    const secondMinted = mintedRowsOf(groupTrackRows(second, mintedOf(first)), second);
    expect(keysOf(third, secondMinted)).toEqual([mintedDarkKey(rows[0])]);
    expect(keysOf(third, mintedOf(first))).toEqual([mintedDarkKey(rows[8])]);
  });

  it("knows a row by its frame_ts_ms, lat and lon exactly, and not by its solve time", () => {
    const rows = chain(10);
    const first = mintedOf(rows.slice(0, 5));
    for (const nudge of [{ frame_ts_ms: rows[4].frame_ts_ms + 1 }, { lat: rows[4].lat + 1e-9 }, { lon: rows[4].lon - 1e-9 }]) {
      const next = [{ ...rows[4], ...nudge }, ...rows.slice(5)];
      expect(keysOf(next, first)).toEqual([mintedDarkKey(next[0])]);
    }
    const resolved = [{ ...rows[4], solve_ts_ms: rows[4].solve_ts_ms + 100 }, ...rows.slice(5)];
    expect(keysOf(resolved, first)).toEqual([mintedDarkKey(rows[0])]);
  });

  it("leaves tracks the server keyed alone", () => {
    const rows = chain(6);
    const first = mintedOf(rows);
    expect(keysOf(rows.map((r) => ({ ...r, track_key: "mn-dark-server", lane: "dark" as const })), first)).toEqual(["mn-dark-server"]);
    expect(keysOf(rows.map((r) => ({ ...r, adsb_hex: "a1b2c3" })), first)).toEqual(["mn-adsb-a1b2c3"]);
  });

  it("keeps the rows of linked tracks only", () => {
    const rows = chain(6);
    const keyed = mintedOf(rows.map((r) => ({ ...r, track_key: "mn-dark-server", lane: "dark" as const })));
    expect(keyed.frameTs).toHaveLength(0);
    expect(keysOf(rows.slice(2), keyed)).toEqual([mintedDarkKey(rows[2])]);
  });

  it("gives a key two tracks ask for to the one whose matching row comes first; the other mints its own", () => {
    // Two targets 16 km apart, which the window before held as one track.
    const a = chain(2, T0, 34.85);
    const b = chain(2, T0 + 500, 35.0);
    const rows = [a[0], b[0], a[1], b[1]];
    const both = mintedRowsOf([{ key: "mn-dark-prev", linked: true, rows: [0, 1, 2, 3] }], rows);
    expect(keysOf(rows, both)).toEqual(["mn-dark-prev", mintedDarkKey(b[0])]);
    // Held from b's first row and a's second, b's match comes first.
    const later = mintedRowsOf([{ key: "mn-dark-prev", linked: true, rows: [1, 2] }], rows);
    expect(keysOf(rows, later)).toEqual([mintedDarkKey(a[0]), "mn-dark-prev"]);
  });

  it("lets a track's earliest matching row choose between two keys", () => {
    const rows = chain(4);
    const two = mintedRowsOf(
      [
        { key: "mn-dark-q", linked: true, rows: [3] },
        { key: "mn-dark-p", linked: true, rows: [1] },
      ],
      rows,
    );
    expect(keysOf(rows, two)).toEqual(["mn-dark-p"]);
  });

  it("never gives a track a key another track of the window holds", () => {
    const rows = chain(3);
    const first = mintedRowsOf([{ key: "mn-dark-server", linked: true, rows: [0, 1, 2] }], rows);
    const next = [row({ track_key: "mn-dark-server", lane: "dark", lat: 30 }), ...rows];
    expect(keysOf(next, first)).toEqual(["mn-dark-server", mintedDarkKey(rows[0])]);
  });

  it("refuses a claim on a key a server-keyed track holds: each keeps its key", () => {
    // The window before linked the target's rows under k0; this one has the
    // server's track under k0 and the target's later rows, which mint their own.
    const rows = chain(4);
    const k0 = mintedDarkKey(rows[0]);
    const server = row({ track_key: k0, lane: "dark", lat: 30 });
    const { groups, keyOf } = carried([server, ...rows.slice(1)], mintedOf(rows.slice(0, 3)));
    expect(groups).toHaveLength(2);
    expect([keyOf(server), keyOf(rows[1])]).toEqual([k0, mintedDarkKey(rows[1])]);
  });

  // In the tests below, the window before held a key this window mints under
  // another target's rows. A single grouping gives that only through two solves
  // of one frame that round to one key: its keys come from its tracks' first
  // rows, and the track minting the key here holds that row first, so it asks
  // for its own key. Keys carried over several windows (a seek back) give it
  // plainly, so `previous` is built by hand.

  it.each([
    ["the track leaving the key starts first", 0, 500],
    ["the track taking the key starts first", 500, 0],
  ])("gives a key its holder leaves in the same pass to the track asking for it: %s", (_order, aMs, cMs) => {
    // Two targets 16 km apart. A minted ka and asks for a key no track holds; C asks for ka.
    const a = chain(3, T0 + aMs, 34.85);
    const c = chain(3, T0 + cMs, 35.0);
    const ka = mintedDarkKey(a[0]);
    const { groups, keyOf } = carried(merged(a, c), heldBefore(["mn-dark-prev", a[1]], [ka, c[1]]));
    expect(groups).toHaveLength(2);
    // Claims are made in track order, so the two cases make them in opposite orders.
    expect(groups[0].key).toBe(keyOf(aMs < cMs ? a[0] : c[0]));
    expect([keyOf(a[0]), keyOf(c[0])]).toEqual(["mn-dark-prev", ka]);
  });

  it.each([
    [0, 500, 1_000],
    [0, 1_000, 500],
    [500, 0, 1_000],
    [500, 1_000, 0],
    [1_000, 0, 500],
    [1_000, 500, 0],
  ])("hands keys down a chain of three whatever order the tracks start in (%i, %i, %i ms)", (xMs, yMs, zMs) => {
    // X asks for a free key, Y for X's, Z for Y's.
    const x = chain(3, T0 + xMs, 34.85);
    const y = chain(3, T0 + yMs, 35.0);
    const z = chain(3, T0 + zMs, 35.15);
    const [kx, ky] = [mintedDarkKey(x[0]), mintedDarkKey(y[0])];
    const { groups, keyOf } = carried(merged(x, y, z), heldBefore(["mn-dark-prev", x[1]], [kx, y[1]], [ky, z[1]]));
    expect(groups).toHaveLength(3);
    expect([keyOf(x[0]), keyOf(y[0]), keyOf(z[0])]).toEqual(["mn-dark-prev", kx, ky]);
  });

  it("swaps the keys of two tracks that ask for each other's", () => {
    const a = chain(3, T0, 34.85);
    const b = chain(3, T0 + 500, 35.0);
    const [ka, kb] = [mintedDarkKey(a[0]), mintedDarkKey(b[0])];
    const { groups, keyOf } = carried(merged(a, b), heldBefore([kb, a[1]], [ka, b[1]]));
    expect(groups).toHaveLength(2);
    expect([keyOf(a[0]), keyOf(b[0])]).toEqual([kb, ka]);
  });

  it("rotates the keys of three tracks whose claims form a cycle", () => {
    const a = chain(3, T0, 34.85);
    const b = chain(3, T0 + 500, 35.0);
    const c = chain(3, T0 + 1_000, 35.15);
    const [ka, kb, kc] = [mintedDarkKey(a[0]), mintedDarkKey(b[0]), mintedDarkKey(c[0])];
    const { groups, keyOf } = carried(merged(a, b, c), heldBefore([kb, a[1]], [kc, b[1]], [ka, c[1]]));
    expect(groups).toHaveLength(3);
    expect([keyOf(a[0]), keyOf(b[0]), keyOf(c[0])]).toEqual([kb, kc, ka]);
  });

  it("refuses a claim on a key a linked track holds and asks nothing for", () => {
    const a = chain(3, T0, 34.85);
    const c = chain(3, T0 + 500, 35.0);
    const ka = mintedDarkKey(a[0]);
    const { groups, keyOf } = carried(merged(a, c), heldBefore([ka, c[1]]));
    expect(groups).toHaveLength(2);
    expect([keyOf(a[0]), keyOf(c[0])]).toEqual([ka, mintedDarkKey(c[0])]);
  });

  it.each([
    ["a server-keyed track", true],
    ["a linked track that asks for nothing", false],
  ])("refuses every claim of a chain whose last key is held by %s", (_tail, serverKeyed) => {
    // D asks for C's key, C for A's, A for B's; B stays where it is.
    const b = serverKeyed ? [row({ track_key: "mn-dark-server", lane: "dark", lat: 30 })] : chain(3, T0 + 1_500, 35.45);
    const a = chain(3, T0, 34.85);
    const c = chain(3, T0 + 500, 35.0);
    const d = chain(3, T0 + 1_000, 35.15);
    const [ka, kb, kc, kd] = [a, b, c, d].map((t) => t[0].track_key ?? mintedDarkKey(t[0]));
    const { groups, keyOf } = carried(merged(a, b, c, d), heldBefore([kb, a[1]], [ka, c[1]], [kc, d[1]]));
    expect(groups).toHaveLength(4);
    expect([keyOf(a[0]), keyOf(b[0]), keyOf(c[0]), keyOf(d[0])]).toEqual([ka, kb, kc, kd]);
  });

  it("holds each linked row as frame time, lat and lon under its track's key, sorted by frame time: 28 bytes a row", () => {
    const a = chain(3, T0, 34.85);
    const b = chain(3, T0 + 1_000, 35.0);
    const rows = [...b, ...a, row({ track_key: "mn-dark-server", lane: "dark", lat: 30 })];
    const m = mintedOf(rows);
    const frames = [...a, ...b].map((r) => r.frame_ts_ms).sort((x, y) => x - y);
    expect([...m.frameTs]).toEqual(frames);
    expect([...m.track].map((t) => m.keys[t])).toEqual(frames.map((f) => mintedDarkKey((f - T0) % 2_000 === 0 ? a[0] : b[0])));
    expect(m.frameTs.byteLength + m.lat.byteLength + m.lon.byteLength + m.track.byteLength).toBe(6 * 28);
  });
});

describe("groupTrackRows: input handling", () => {
  it("processes rows in frame order even when the response is not sorted", () => {
    const a = row({});
    const b = row({ frame_ts_ms: T0 + 1000, solve_ts_ms: T0 + 1500, lat: 34.851 });
    const [g] = groupTrackRows([b, a]);
    // The track is minted from the earlier row, which is listed first.
    expect(g).toEqual({ key: mintedDarkKey(a), linked: true, rows: [1, 0] });
  });

  it("drops rows without a finite position or time", () => {
    const rows = [row({}), row({ lat: null }), row({ frame_ts_ms: Number.NaN }), row({ lon: undefined })];
    expect(groupTrackRows(rows).flatMap((g) => g.rows)).toEqual([0]);
  });

  it("joins a minted key already in use instead of opening a second track under it", () => {
    const a = row({});
    const rows = [row({ track_key: mintedDarkKey(a), lane: "dark", lat: 30 }), a];
    expect(groupTrackRows(rows)).toEqual([{ key: mintedDarkKey(a), linked: false, rows: [0, 1] }]);
  });
});
