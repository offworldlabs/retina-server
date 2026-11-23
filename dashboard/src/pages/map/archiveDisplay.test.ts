import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DR_MAX_S,
  archiveAvailable,
  archiveClockMs,
  archiveEntryMs,
  archiveTrailBuffer,
  displayPose,
  isArchiveMode,
  mapNowMs,
  skipInitialFit,
  solveTrailTsS,
  type Pose,
  type PoseFix,
} from "./archiveDisplay";
import { ARC_DR_MAX_S, POSITION_SOURCE_ARC_ONLY } from "./constants";
import type { TrailPoint } from "./types";

afterEach(() => vi.restoreAllMocks());

const at = (iso: string) => Date.parse(iso);

/**
 * The live map's per-frame pose before archive mode existed, transcribed from
 * the render loop it was lifted out of (LiveAircraftMap.tsx at 3982febe): the
 * live rule has to give exactly what this gave.
 */
function referenceLivePose(fix, prev: Pose | undefined, now: number, alpha: number): Pose {
  const DEG_PER_M = 1 / 111_320;
  const KNOTS_TO_MS = 0.514444;
  const drCapS = fix.position_source === POSITION_SOURCE_ARC_ONLY ? ARC_DR_MAX_S : 60;
  const elapsed = Math.min((now - fix._fixTs) / 1000, drCapS);
  const gs = fix.gs || 0;
  let targetLat = fix._fixLat;
  let targetLon = fix._fixLon;
  if (elapsed > 0 && gs > 0) {
    const gs_m_s = gs * KNOTS_TO_MS;
    const track_rad = (fix.track || 0) * (Math.PI / 180);
    const cos_lat = Math.cos(fix._fixLat * (Math.PI / 180)) || 1e-9;
    targetLat = fix._fixLat + gs_m_s * Math.cos(track_rad) * DEG_PER_M * elapsed;
    targetLon = fix._fixLon + (gs_m_s * Math.sin(track_rad)) / (111_320 * cos_lat) * elapsed;
  }
  const sLat = prev ? prev.lat + (targetLat - prev.lat) * alpha : targetLat;
  const sLon = prev ? prev.lon + (targetLon - prev.lon) * alpha : targetLon;
  const targetTrack = fix.track || 0;
  const prevTrack = prev ? prev.track : targetTrack;
  const dTrack = ((targetTrack - prevTrack + 540) % 360) - 180;
  const sTrack = (prevTrack + dTrack * alpha + 360) % 360;
  return { lat: sLat, lon: sLon, track: sTrack };
}

const FIX_TS = at("2026-10-18T20:00:00Z");

function fix(over: Partial<PoseFix> = {}): PoseFix {
  return { _fixLat: 34.85, _fixLon: -82.39, _fixTs: FIX_TS, gs: 420, track: 63, position_source: "multinode_solve", ...over };
}

describe("which pages have archive mode", () => {
  it("is the real-fleet map's, never /sim's", () => {
    expect(archiveAvailable("real")).toBe(true);
    expect(archiveAvailable("all")).toBe(true);
    expect(archiveAvailable("synthetic")).toBe(false);
  });

  it("is on when the hash names an instant, on a page that has it", () => {
    const t = at("2026-10-18T20:00:00Z");
    expect(isArchiveMode("real", t)).toBe(true);
    expect(isArchiveMode("all", t)).toBe(true);
    expect(isArchiveMode("synthetic", t)).toBe(false);
    expect(isArchiveMode("real", null)).toBe(false);
    expect(isArchiveMode("real", undefined)).toBe(false);
  });
});

describe("the map's clock", () => {
  it("is the last frame's instant in archive mode, else the replay clock, and none live", () => {
    const frame = at("2026-10-18T20:00:01Z");
    const clock = at("2026-10-18T20:00:03Z");
    expect(archiveClockMs(true, frame, clock)).toBe(frame);
    expect(archiveClockMs(true, null, clock)).toBe(clock);
    expect(archiveClockMs(false, frame, clock)).toBeNull();
    expect(archiveClockMs(false, null, clock)).toBeNull();
  });

  it("reads the archive clock when there is one and the wall clock otherwise", () => {
    const wall = at("2026-10-19T12:00:00Z");
    const now = vi.spyOn(Date, "now").mockReturnValue(wall);
    const replayed = at("2026-10-18T20:00:01Z");
    expect(mapNowMs(replayed)).toBe(replayed);
    expect(now).not.toHaveBeenCalled();
    expect(mapNowMs(null)).toBe(wall);
    expect(mapNowMs(undefined)).toBe(wall);
  });
});

describe("Open archive", () => {
  it("lands an hour back, on the minute", () => {
    expect(archiveEntryMs(at("2026-10-19T12:34:56.789Z"))).toBe(at("2026-10-19T11:34:00Z"));
    expect(archiveEntryMs(at("2026-10-19T12:00:00Z"))).toBe(at("2026-10-19T11:00:00Z"));
    expect(archiveEntryMs(at("2026-10-19T00:00:59.999Z"))).toBe(at("2026-10-18T23:00:00Z"));
  });
});

describe("the first-data fit", () => {
  it("gives way only to an archive link that names a centre", () => {
    expect(skipInitialFit(true, { lat: 34.85, lon: -82.39 })).toBe(true);
    expect(skipInitialFit(true, { lat: 0, lon: 0 })).toBe(true);
    expect(skipInitialFit(true, { lat: 34.85 })).toBe(false);
    expect(skipInitialFit(true, { lon: -82.39 })).toBe(false);
    expect(skipInitialFit(true, {})).toBe(false);
    expect(skipInitialFit(false, { lat: 34.85, lon: -82.39 })).toBe(false);
  });
});

describe("a solve-trail point's timestamp", () => {
  const now = at("2026-10-18T20:00:05Z");
  const epoch = at("2026-10-18T20:00:01.250Z");

  it("is the solve's epoch in archive mode", () => {
    expect(solveTrailTsS({ frame_ts_ms: epoch }, now, true)).toBe(epoch / 1000);
  });

  it("is the ingest time live, whatever the entry carries, and in archive without an epoch", () => {
    expect(solveTrailTsS({ frame_ts_ms: epoch }, now, false)).toBe(now / 1000);
    expect(solveTrailTsS({}, now, false)).toBe(now / 1000);
    expect(solveTrailTsS({}, now, true)).toBe(now / 1000);
    expect(solveTrailTsS({ frame_ts_ms: Number.NaN }, now, true)).toBe(now / 1000);
    expect(solveTrailTsS({ frame_ts_ms: "1792353600000" }, now, true)).toBe(now / 1000);
  });
});

describe("where an icon is drawn in archive mode", () => {
  const prev = { lat: 34.7, lon: -82.1, track: 270 };

  it.each([0, 1, 5, 30, DR_MAX_S, 3600, -10])("is the fix itself, %s s after it", (elapsedS) => {
    for (const alpha of [0, 0.03, 0.5, 1]) {
      const out = { lat: Number.NaN, lon: Number.NaN, track: Number.NaN };
      const pose = displayPose(fix(), { ...prev }, FIX_TS + elapsedS * 1000, alpha, true, out);
      expect(pose).toBe(out);
      expect(pose).toEqual({ lat: 34.85, lon: -82.39, track: 63 });
    }
  });

  it("is the fix for an arc-only or slow entry too, and heads north when the feed gave no track", () => {
    const later = FIX_TS + 20_000;
    const out = { lat: 0, lon: 0, track: 0 };
    expect(displayPose(fix({ position_source: POSITION_SOURCE_ARC_ONLY }), prev, later, 0.2, true, out))
      .toEqual({ lat: 34.85, lon: -82.39, track: 63 });
    expect(displayPose(fix({ gs: undefined, track: undefined }), prev, later, 0.2, true, out))
      .toEqual({ lat: 34.85, lon: -82.39, track: 0 });
  });

  it("overwrites the smoothing entry it is handed, as the render loop passes it", () => {
    const sm = { ...prev };
    displayPose(fix(), sm, FIX_TS + 9_000, 0.03, true, sm);
    expect(sm).toEqual({ lat: 34.85, lon: -82.39, track: 63 });
  });
});

describe("where an icon is drawn live", () => {
  const fixes: PoseFix[] = [
    fix(),
    fix({ gs: 0 }),
    fix({ gs: undefined }),
    fix({ track: undefined }),
    fix({ track: 359.5, gs: 140 }),
    fix({ position_source: POSITION_SOURCE_ARC_ONLY, gs: 300, track: 190 }),
    fix({ _fixLat: 89.9999, gs: 500, track: 10 }),
    fix({ _fixLat: -33.9, _fixLon: 151.2, gs: 260, track: 225 }),
  ];
  const prevs: (Pose | undefined)[] = [
    undefined,
    { lat: 34.8499, lon: -82.3902, track: 61 },
    { lat: 34.9, lon: -82.2, track: 2 },
    { lat: 34.85, lon: -82.39, track: 358 },
  ];
  const elapsed = [-5, 0, 0.016, 0.5, 3, 9.99, 10, 11, 59, 60, 61, 600];
  const alphas = [0, 0.0287, 0.1, 0.5, 1];

  it("is exactly what the render loop computed before archive mode, over the whole grid", () => {
    let cases = 0;
    for (const f of fixes) {
      for (const p of prevs) {
        for (const e of elapsed) {
          for (const alpha of alphas) {
            const now = FIX_TS + e * 1000;
            const want = referenceLivePose(f, p && { ...p }, now, alpha);
            const got = displayPose(f, p && { ...p }, now, alpha, false, { lat: 0, lon: 0, track: 0 });
            expect(got).toEqual(want);
            // Mutated in place, as the loop does it: the same numbers.
            if (p) {
              const sm = { ...p };
              displayPose(f, sm, now, alpha, false, sm);
              expect(sm).toEqual(want);
            }
            cases++;
          }
        }
      }
    }
    expect(cases).toBe(fixes.length * prevs.length * elapsed.length * alphas.length);
  });

  it("dead-reckons a fix that has not moved, which the archive rule never does", () => {
    const later = FIX_TS + 5_000;
    const live = displayPose(fix(), undefined, later, 1, false, { lat: 0, lon: 0, track: 0 });
    const archived = displayPose(fix(), undefined, later, 1, true, { lat: 0, lon: 0, track: 0 });
    expect(live.lat).toBeGreaterThan(34.85);
    expect(live.lon).toBeGreaterThan(-82.39);
    expect(archived).toEqual({ lat: 34.85, lon: -82.39, track: 63 });
  });

  it("glides toward a new fix, which the archive rule never does", () => {
    const prev = { lat: 34.8, lon: -82.4, track: 40 };
    const live = displayPose(fix(), { ...prev }, FIX_TS, 0.1, false, { lat: 0, lon: 0, track: 0 });
    expect(live.lat).toBeCloseTo(34.8 + (34.85 - 34.8) * 0.1, 12);
    expect(live.track).toBeCloseTo(40 + (63 - 40) * 0.1, 12);
    expect(displayPose(fix(), { ...prev }, FIX_TS, 0.1, true, { lat: 0, lon: 0, track: 0 }))
      .toEqual({ lat: 34.85, lon: -82.39, track: 63 });
  });
});

describe("a trail buffer rebuilt from a replayed entry's trail", () => {
  const T0 = at("2026-10-18T20:00:00Z") / 1000;
  /** `n` feed points, [lat, lon, alt_ft, ts_s], ascending one solve every 2 s. */
  const feedTrail = (n: number): TrailPoint[] =>
    Array.from({ length: n }, (_, i) => [34.8 + i * 0.001, -82.4 + i * 0.002, 12_000 + i, T0 + 2 * i]);

  it("is the last maxPoints points as [lat, lon, ts_s], in order, without the altitude", () => {
    const trail = feedTrail(50);
    const buf = archiveTrailBuffer(trail, 40);
    expect(buf).toHaveLength(40);
    expect(buf).toEqual(trail.slice(10).map(([lat, lon, , ts]) => [lat, lon, ts]));
    expect(buf.every((p) => p.length === 3)).toBe(true);
    expect(archiveTrailBuffer(trail, 60)).toEqual(trail.map(([lat, lon, , ts]) => [lat, lon, ts]));
    expect(archiveTrailBuffer(trail, 50)).toHaveLength(50);
    expect(archiveTrailBuffer(trail, 1)).toEqual([[trail[49][0], trail[49][1], trail[49][3]]]);
  });

  it("puts the entry's sigma on the last point alone, and only a positive number", () => {
    const trail = feedTrail(5);
    const buf = archiveTrailBuffer(trail, 40, 87.5);
    expect(buf.slice(0, -1).every((p) => p.length === 3)).toBe(true);
    expect(buf[4]).toEqual([trail[4][0], trail[4][1], trail[4][3], 87.5]);
    // Capped, the sigma still goes on the newest point kept.
    expect(archiveTrailBuffer(trail, 2, 87.5)).toEqual([
      [trail[3][0], trail[3][1], trail[3][3]],
      [trail[4][0], trail[4][1], trail[4][3], 87.5],
    ]);
    for (const sigma of [undefined, null, 0, -12, Number.NaN, "87.5"]) {
      expect(archiveTrailBuffer(trail, 40, sigma).every((p) => p.length === 3)).toBe(true);
    }
  });

  it("is empty for an empty or missing trail, and one point for a one-point trail", () => {
    expect(archiveTrailBuffer([], 40, 50)).toEqual([]);
    expect(archiveTrailBuffer(null, 40, 50)).toEqual([]);
    expect(archiveTrailBuffer(undefined, 40)).toEqual([]);
    expect(archiveTrailBuffer(feedTrail(3), 0, 50)).toEqual([]);
    const [only] = feedTrail(1);
    expect(archiveTrailBuffer([only], 40)).toEqual([[only[0], only[1], only[3]]]);
    expect(archiveTrailBuffer([only], 40, 50)).toEqual([[only[0], only[1], only[3], 50]]);
  });

  it("builds new tuples and leaves the frame's trail as it was", () => {
    // The frame's points are shared with the replay index and every other
    // frame showing the same row; the map's buffers are its own.
    const trail = feedTrail(4);
    const before = JSON.stringify(trail);
    const buf = archiveTrailBuffer(trail, 40, 30);
    buf[3].push(1);
    buf[0][0] = 0;
    expect(JSON.stringify(trail)).toBe(before);
    expect(buf.some((p) => trail.includes(p as unknown as TrailPoint))).toBe(false);
  });
});
