import { describe, expect, it } from "vitest";

import { GT_FEED_STALE_MS, groundTruthKey } from "../constants";
import {
  applyGroundTruthFixes,
  groundTruthPruneGraceMs,
  pruneGroundTruthFixes,
  sweepStaleGroundTruthFixes,
} from "../groundTruthFixes";
import { truthClass } from "../truthColor";
import {
  ADSB,
  ADSB_CLOCK_MS,
  GAP_HEX,
  KNOWN_LANE_HEX,
  NO_CALLSIGN_HEX,
  NO_SOLVE_HEX,
} from "./__fixtures__/adsb";
import { KEYED } from "./__fixtures__/scenarios";
import { adsbTraffic, buildAdsbWindow, type TrafficCache } from "./adsbTraffic";
import type { AdsbResponse } from "./types";

const at = (h: number, m: number, s = 0, ms = 0) => Date.UTC(2026, 9, 18, h, m, s, ms);
/** A window as the replay holds it after asking for exactly what the fixture answered. */
const held = (resp: AdsbResponse, edge?: number) => buildAdsbWindow(resp, resp.from, resp.to, edge);
const TRAFFIC = held(ADSB.traffic);
const hexesAt = (ms: number) => Object.keys(adsbTraffic([TRAFFIC], ms).ground_truth).sort();

describe("an ADS-B window", () => {
  it("holds a whole answer's rows up to its own end", () => {
    expect(TRAFFIC).toMatchObject({ from: at(19, 30), to: at(20, 30), servedEnd: at(20, 30), truncated: false, rowCount: 283 });
    expect(TRAFFIC.hexes.slice().sort()).toEqual([KNOWN_LANE_HEX, NO_SOLVE_HEX, GAP_HEX, NO_CALLSIGN_HEX].sort());
  });

  it("serves a cut answer up to its last snapshot, whose rows the cut may have left out", () => {
    const cut = held(ADSB.truncated);
    expect(cut).toMatchObject({ truncated: true, servedEnd: at(19, 30, 10), rowCount: 148 });
    expect(Math.max(...cut.ts)).toBe(at(19, 30));
  });

  it("serves no further than the archive's edge when it landed: later rows never reach it", () => {
    const early = held(ADSB.traffic, at(19, 58));
    expect(early.servedEnd).toBe(at(19, 58) + 1);
    expect(Math.max(...early.ts)).toBe(at(19, 58));
    // Four aircraft in each of the 37 snapshots 19:52:00Z..19:58:00Z.
    expect(early.rowCount).toBe(4 * 37);
  });

  it("cannot be read from an answer without rows, or with a row lacking a hex or a snapshot time", () => {
    const row = ADSB.traffic.rows[0];
    for (const body of [
      {},
      { ...ADSB.traffic, rows: "rows" },
      { ...ADSB.traffic, rows: [{ ...row, hex: null }] },
      { ...ADSB.traffic, rows: [{ ...row, ts_ms: null }] },
    ]) {
      expect(() => held(body as unknown as AdsbResponse)).toThrow(TypeError);
    }
  });
});

describe("the ground truth of a frame", () => {
  it("holds every aircraft whose latest snapshot is at most 20 s old, the one with no solve among them", () => {
    // adbeaa's last snapshot is 50 s old at 20:00:00Z.
    expect(hexesAt(ADSB_CLOCK_MS)).toEqual([NO_SOLVE_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX].sort());
    // The tracks store holds a solve of a1dda8 and none of a07caf.
    const solved = new Set(KEYED.known.rows.map((r) => r.adsb_hex));
    expect(solved.has(KNOWN_LANE_HEX)).toBe(true);
    expect(solved.has(NO_SOLVE_HEX)).toBe(false);

    const { ground_truth, ground_truth_meta } = adsbTraffic([TRAFFIC], ADSB_CLOCK_MS);
    const noSolve = ground_truth[NO_SOLVE_HEX];
    const [lat, lon, altM, tsS] = noSolve[noSolve.length - 1];
    // The 20:00:00Z snapshot's fix, received at 19:59:59Z.
    expect([lat, lon, tsS]).toEqual([34.936916, -82.100763, 1792353599]);
    expect(altM).toBeCloseTo(1371.6, 9); // 4,500 ft
    expect(ground_truth_meta[NO_SOLVE_HEX]).toEqual({
      has_adsb: true,
      adsb_callsign: "EDV3481",
      speed_ms: expect.closeTo(69.45, 2), // 135 kt
      heading: 70,
      object_type: "aircraft",
      source: "adsb",
      seen_ms: 1792353599000,
    });
    expect(ground_truth_meta[NO_CALLSIGN_HEX].adsb_callsign).toBeNull();
  });

  it("drops an aircraft 20 s after its last snapshot, and shows it again from its next", () => {
    const shown = (ms: number) => hexesAt(ms).includes(GAP_HEX);
    expect(shown(at(19, 59, 10))).toBe(true);
    expect(shown(at(19, 59, 30))).toBe(true);
    expect(shown(at(19, 59, 30, 1))).toBe(false);
    expect(shown(at(20, 0, 9, 999))).toBe(false);
    expect(shown(at(20, 0, 10))).toBe(true);
  });

  it("hands over at most 30 points of a trail, oldest first, each at the time its fix was received", () => {
    // Between two snapshots: the fixes of the 30 up to 20:00:00Z (19:55:10Z..), the one at 20:00:10Z not yet.
    const trail = adsbTraffic([TRAFFIC], at(20, 0, 5)).ground_truth[KNOWN_LANE_HEX];
    expect(trail.map((p) => p[3])).toEqual([
      1792353306, 1792353317, 1792353326, 1792353338, 1792353347, 1792353356, 1792353368, 1792353377, 1792353386,
      1792353398, 1792353408, 1792353416, 1792353428, 1792353438, 1792353448, 1792353457, 1792353466, 1792353475,
      1792353487, 1792353496, 1792353508, 1792353517, 1792353526, 1792353536, 1792353547, 1792353556, 1792353568,
      1792353577, 1792353586, 1792353594,
    ]);
    expect(trail[0].slice(0, 2)).toEqual([35.15537368397376, -82.02190577353686]);
    expect(trail[0][2]).toBeCloseTo(10726.5216, 9); // 35,192 ft

    // Every frame over the fixture's 12 minutes, a second apart: no point
    // after the clock, and the shown fix at most 20 s past its snapshot plus
    // the 45 s a snapshot keeps a fix for.
    for (let ms = at(19, 52); ms < at(20, 4); ms += 1_000) {
      for (const points of Object.values(adsbTraffic([TRAFFIC], ms).ground_truth)) {
        const times = points.map((p) => p[3] * 1000);
        expect(points.length).toBeLessThanOrEqual(30);
        expect(times.every((t, k) => k === 0 || times[k - 1] < t)).toBe(true);
        expect(times[times.length - 1]).toBeLessThanOrEqual(ms);
        expect(times[times.length - 1]).toBeGreaterThanOrEqual(ms - 65_000);
      }
    }
  });

  it("hands over a fix the snapshots repeat once, at the time it was received, and says when that was", () => {
    // adbeaa's snapshots 19:58:30Z..19:59:10Z all hold the fix received at
    // 19:58:26Z; at 19:59:25Z the latest of them is 15 s old, its fix 59 s.
    const clock = at(19, 59, 25);
    const { ground_truth, ground_truth_meta } = adsbTraffic([TRAFFIC], clock);
    const trail = ground_truth[GAP_HEX];
    expect(trail.map((p) => p[3])).toEqual([
      1792353257, 1792353266, 1792353278, 1792353287, 1792353297, 1792353308, 1792353316, 1792353328, 1792353338,
      1792353346, 1792353355, 1792353366, 1792353376, 1792353388, 1792353397, 1792353406, 1792353415, 1792353427,
      1792353439, 1792353448, 1792353456, 1792353467, 1792353476, 1792353488, 1792353497, 1792353506,
    ]);
    expect(trail[trail.length - 1].slice(0, 2)).toEqual([34.6112301713875, -82.22163134890368]);
    expect(ground_truth_meta[GAP_HEX].seen_ms).toBe(Date.UTC(2026, 9, 18, 19, 58, 26));
    expect(clock - ground_truth_meta[GAP_HEX].seen_ms).toBe(59_000);
  });

  it("keeps an aircraft on the map however old its fix: the map ages entries on its own clock, not the fix's", () => {
    // adbeaa's fix is 59 s old at 19:59:25Z, past the 30 s the map keeps an entry its feed stopped vouching for.
    const clock = at(19, 59, 25);
    const { ground_truth, ground_truth_meta } = adsbTraffic([TRAFFIC], clock);
    const fixes: Record<string, Record<string, unknown>> = {};
    const keys = applyGroundTruthFixes(fixes, ground_truth, ground_truth_meta, clock);
    const stores = {
      fixes,
      smooth: {}, svgElems: {}, svgMiss: {}, latLng: {},
      trails: {}, lastTrailSample: {}, solveTrails: {},
      markerRegistry: new Map(),
    };
    pruneGroundTruthFixes(stores, keys, clock, groundTruthPruneGraceMs(true));
    sweepStaleGroundTruthFixes(stores, clock, GT_FEED_STALE_MS);
    expect(GT_FEED_STALE_MS).toBe(30_000);
    expect(fixes[groundTruthKey(GAP_HEX)]).toMatchObject({ seen_ms: Date.UTC(2026, 9, 18, 19, 58, 26), _updatedAt: clock });
  });

  it("stamps a fix received after its snapshot, or with no receipt time, at the snapshot, never after the clock", () => {
    const rows = ADSB.traffic.rows.map((r) => {
      if (r.hex === NO_SOLVE_HEX) return { ...r, seen_ms: r.ts_ms + 1_500 };
      if (r.hex === KNOWN_LANE_HEX) return { ...r, seen_ms: undefined as unknown as number };
      return r;
    });
    const { ground_truth, ground_truth_meta } = adsbTraffic([held({ ...ADSB.traffic, rows })], ADSB_CLOCK_MS);
    for (const hex of [NO_SOLVE_HEX, KNOWN_LANE_HEX]) {
      const trail = ground_truth[hex];
      expect(trail[trail.length - 1][3]).toBe(1792353600);
      expect(ground_truth_meta[hex].seen_ms).toBe(ADSB_CLOCK_MS);
    }
  });

  it("is never anomalous, and the map keys it as live ADS-B traffic at the archived fix", () => {
    const { ground_truth, ground_truth_meta } = adsbTraffic([TRAFFIC], ADSB_CLOCK_MS);
    const fixes: Record<string, Record<string, unknown>> = {};
    applyGroundTruthFixes(fixes, ground_truth, ground_truth_meta, ADSB_CLOCK_MS);
    const fix = fixes[groundTruthKey(NO_SOLVE_HEX)];
    expect(fix).toMatchObject({
      hex: NO_SOLVE_HEX,
      lat: 34.936916,
      lon: -82.100763,
      alt_baro: 4500,
      gs: 135,
      track: 70,
      has_adsb: true,
      source: "adsb",
      adsb_callsign: "EDV3481",
      seen_ms: 1792353599000,
      _updatedAt: ADSB_CLOCK_MS,
    });
    expect(fix.is_anomalous).toBeUndefined();
    expect(Object.values(fixes).map((f) => truthClass(f))).toEqual(["live_adsb", "live_adsb", "live_adsb"]);
  });

  it("hands over a missing altitude as missing", () => {
    const rows = ADSB.traffic.rows.map((r) => (r.hex === NO_SOLVE_HEX ? { ...r, alt_baro: null } : r));
    const { ground_truth } = adsbTraffic([held({ ...ADSB.traffic, rows })], ADSB_CLOCK_MS);
    expect(ground_truth[NO_SOLVE_HEX].every((p) => p[2] === null)).toBe(true);
    const fixes: Record<string, Record<string, unknown>> = {};
    applyGroundTruthFixes(fixes, ground_truth, {}, ADSB_CLOCK_MS);
    expect(fixes[groundTruthKey(NO_SOLVE_HEX)].alt_baro).toBeUndefined();
  });

  it("hands over the same trail while an aircraft's latest snapshot is the same, and a new one after it", () => {
    const cache: TrafficCache = new Map();
    const first = adsbTraffic([TRAFFIC], at(20, 0, 1), cache);
    const same = adsbTraffic([TRAFFIC], at(20, 0, 9), cache);
    const next = adsbTraffic([TRAFFIC], at(20, 0, 10), cache);
    expect(same.ground_truth[NO_SOLVE_HEX]).toBe(first.ground_truth[NO_SOLVE_HEX]);
    expect(same.ground_truth_meta[NO_SOLVE_HEX]).toBe(first.ground_truth_meta[NO_SOLVE_HEX]);
    expect(next.ground_truth[NO_SOLVE_HEX]).not.toBe(first.ground_truth[NO_SOLVE_HEX]);
    expect(next.ground_truth[NO_SOLVE_HEX]).toEqual(adsbTraffic([TRAFFIC], at(20, 0, 10)).ground_truth[NO_SOLVE_HEX]);
  });

  it("reads the snapshot at a cut once, from the window after the cut, with the trail before it from the cut one", () => {
    const windows = [held(ADSB.truncated), held(ADSB.afterTruncation)];
    const { ground_truth } = adsbTraffic(windows, at(19, 30, 15));
    // Every aircraft of the snapshot at the cut, which the cut answer held 52 of.
    const atCut = ADSB.afterTruncation.rows.filter((r) => r.ts_ms === at(19, 30, 10));
    expect(atCut).toHaveLength(149);
    const before = new Map(ADSB.truncated.rows.filter((r) => r.ts_ms === at(19, 30)).map((r) => [r.hex, r]));
    let twoPoints = 0;
    for (const r of atCut) {
      const prev = before.get(r.hex);
      const fixes = prev ? [prev, r] : [r];
      twoPoints += fixes.length === 2 ? 1 : 0;
      expect(ground_truth[r.hex].map((p) => [p[0], p[1], p[3] * 1000])).toEqual(fixes.map((x) => [x.lat, x.lon, x.seen_ms]));
    }
    expect(twoPoints).toBe(147);
  });
});
