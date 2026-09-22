import { describe, it, expect } from "vitest";
import { smoothTrailPositions, stitchPredecessorTrail, type SolveTrailPoint } from "./trails";
import { distanceKm } from "../../utils/geo";

const M_PER_DEG_LAT = 111_320;

/** A straight eastbound leg at ~fixed spacing, one point per second. */
function leg(n: number, stepDeg = 0.001, lat0 = 34.85, lon0 = -82.4): SolveTrailPoint[] {
  return Array.from({ length: n }, (_, i) => [lat0, lon0 + i * stepDeg, 1000 + i] as SolveTrailPoint);
}

/** Lateral deviation of a point from the chord through its neighbours, metres. */
function jagM(pts: [number, number][]): number {
  let total = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const midLat = (pts[i - 1][0] + pts[i + 1][0]) / 2;
    total += Math.abs(pts[i][0] - midLat) * M_PER_DEG_LAT;
  }
  return total;
}

describe("smoothTrailPositions", () => {
  it("leaves a straight line unchanged", () => {
    const pts = leg(10);
    const { smoothed, head } = smoothTrailPositions(pts, { k: 3 });
    expect(smoothed.length + head.length).toBe(10);
    const all = [...smoothed, ...head];
    for (let i = 0; i < 10; i++) {
      expect(all[i][0]).toBeCloseTo(pts[i][0], 9);
      expect(all[i][1]).toBeCloseTo(pts[i][1], 9);
    }
  });

  it("reduces a zig-zag", () => {
    // ±40 m alternating lateral oscillation on a straight track — the shape
    // the capture showed in turns.
    const amp = 40 / M_PER_DEG_LAT;
    const pts: SolveTrailPoint[] = leg(12).map(
      (p, i) => [p[0] + (i % 2 ? amp : -amp), p[1], p[2]] as SolveTrailPoint,
    );
    const raw = pts.map((p) => [p[0], p[1]] as [number, number]);
    const { smoothed, head } = smoothTrailPositions(pts, { k: 3 });
    const out = [...smoothed, ...head];
    expect(out.length).toBe(12);
    // Interior points only: the run edges stay raw by design.
    expect(jagM(out.slice(1, -1))).toBeLessThan(jagM(raw.slice(1, -1)) / 2);
  });

  it("splits runs at a step past the jump gate so a teleport stays sharp", () => {
    const before = leg(4);
    // 5 km jump — a supersession re-key, far past the 500 m floor.
    const after = leg(4, 0.001, 34.9, -82.3).map(
      (p, i) => [p[0], p[1], 1010 + i] as SolveTrailPoint,
    );
    const { smoothed, head } = smoothTrailPositions([...before, ...after], { k: 3 });
    const out = [...smoothed, ...head];
    expect(out.length).toBe(8);
    // The point either side of the gate is the raw solve, untouched: no
    // averaged point sits in the 5 km gap.
    expect(out[3][0]).toBeCloseTo(before[3][0], 9);
    expect(out[3][1]).toBeCloseTo(before[3][1], 9);
    expect(out[4][0]).toBeCloseTo(after[0][0], 9);
    expect(out[4][1]).toBeCloseTo(after[0][1], 9);
    expect(distanceKm(out[3][0], out[3][1], out[4][0], out[4][1])).toBeGreaterThan(4);
    // head comes from the last run only.
    expect(head.length).toBe(1);
    expect(head[0][1]).toBeCloseTo(after[3][1], 9);
  });

  it("keeps a large step below 3σ inside one run", () => {
    // σ = 400 m → gate 1200 m, so a 900 m step is scatter, not a teleport.
    const stepDeg = 900 / M_PER_DEG_LAT;
    const pts: SolveTrailPoint[] = [
      [34.85, -82.4, 1000, 400],
      [34.85 + stepDeg, -82.4, 1001, 400],
      [34.85 + 2 * stepDeg, -82.4, 1002, 400],
      [34.85 + 3 * stepDeg, -82.4, 1003, 400],
    ];
    // One run of 4 → one centred point at index 1 and one at index 2.
    const { smoothed, head } = smoothTrailPositions(pts, { k: 3 });
    expect(smoothed.length).toBe(3);
    expect(head.length).toBe(1);
    // Index 1 averaged over 0..2 on an evenly spaced line = its own position.
    expect(smoothed[1][0]).toBeCloseTo(pts[1][0], 9);
  });

  it("keeps an honest 3 s step of straight flight inside one run", () => {
    // 250 m/s for 3 s = 750 m of honest travel with no stated sigma.  That
    // clears the bare 500 m floor (and 3 x a 100 m fallback = 300 m), so
    // only the motion allowance keeps it in one run: gate = 500 + 350*3 =
    // 1550 m.
    const stepDeg = 750 / M_PER_DEG_LAT;
    const pts: SolveTrailPoint[] = Array.from({ length: 5 }, (_, i) =>
      [34.85 + i * stepDeg, -82.4, 1000 + 3 * i] as SolveTrailPoint);
    const { smoothed, head } = smoothTrailPositions(pts, { k: 3, sigmaFallbackM: 100 });
    // One run of 5: edges raw, three centred, one head.
    expect(smoothed.length).toBe(4);
    expect(head.length).toBe(1);
    expect(smoothed[2][0]).toBeCloseTo(pts[2][0], 9);
    // The same 750 m step with NO time elapsed is a teleport.
    const instant = pts.map((p) => [p[0], p[1], 1000] as SolveTrailPoint);
    const split = smoothTrailPositions(instant, { k: 3, sigmaFallbackM: 100 });
    expect(split.smoothed.length + split.head.length).toBe(5);
    expect(split.head.length).toBe(1); // last run is a single raw point
  });

  it("does not let the gate's fallback sigma change the mean", () => {
    const off = 0.001;
    const noSigma: SolveTrailPoint[] = [
      [34.85 + off, -82.401, 1000],
      [34.85, -82.4, 1001],
      [34.85 + off, -82.399, 1002],
    ];
    const a = smoothTrailPositions(noSigma, { k: 3, sigmaFallbackM: 10 }).smoothed[1][0];
    const b = smoothTrailPositions(noSigma, { k: 3, sigmaFallbackM: 5000 }).smoothed[1][0];
    expect(a).toBeCloseTo(34.85 + (2 * off) / 3, 9);
    expect(b).toBeCloseTo(a, 12);
  });

  it("passes through unchanged at k=1 with no head", () => {
    const pts = leg(6).map((p, i) => [p[0] + (i % 2 ? 0.0005 : 0), p[1], p[2]] as SolveTrailPoint);
    const { smoothed, head } = smoothTrailPositions(pts, { k: 1 });
    expect(head).toEqual([]);
    expect(smoothed).toEqual(pts.map((p) => [p[0], p[1]]));
  });

  it("weights by inverse variance when every point in the window states a sigma", () => {
    // Middle point is 100x more precise than its neighbours, which are
    // displaced symmetrically the other way — the weighted mean must sit far
    // closer to the middle point than the plain mean does.
    const off = 0.001;
    const withSigma: SolveTrailPoint[] = [
      [34.85 + off, -82.401, 1000, 1000],
      [34.85, -82.4, 1001, 10],
      [34.85 + off, -82.399, 1002, 1000],
    ];
    const plain: SolveTrailPoint[] = withSigma.map((p) => [p[0], p[1], p[2]] as SolveTrailPoint);
    const w = smoothTrailPositions(withSigma, { k: 3, jumpGateM: 1e9 }).smoothed[1][0];
    const u = smoothTrailPositions(plain, { k: 3, jumpGateM: 1e9 }).smoothed[1][0];
    expect(Math.abs(w - 34.85)).toBeLessThan(Math.abs(u - 34.85) / 10);
    // Plain mean of (+off, 0, +off) = 2/3 off.
    expect(u).toBeCloseTo(34.85 + (2 * off) / 3, 9);
  });

  it("returns floor(k/2) head points", () => {
    for (const k of [1, 2, 3, 5, 7]) {
      const { head } = smoothTrailPositions(leg(20), { k });
      expect(head.length).toBe(Math.floor(k / 2));
    }
  });

  it("handles empty, single-point and short buffers", () => {
    expect(smoothTrailPositions([], { k: 3 })).toEqual({ smoothed: [], head: [] });
    expect(smoothTrailPositions(null)).toEqual({ smoothed: [], head: [] });
    // One point: nothing centreable, and it is the newest, so it is the head.
    expect(smoothTrailPositions(leg(1), { k: 3 })).toEqual({ smoothed: [], head: [[34.85, -82.4]] });
    const two = smoothTrailPositions(leg(2), { k: 3 });
    expect(two.smoothed.length).toBe(1);
    expect(two.head.length).toBe(1);
  });
});

describe("stitchPredecessorTrail", () => {
  /** n points one second apart ending at tsEnd, at a fixed place. */
  const pts = (n: number, tsEnd: number, lat = 35): SolveTrailPoint[] =>
    Array.from({ length: n }, (_, i) => [lat, -82, tsEnd - (n - 1 - i)] as SolveTrailPoint);

  it("seeds an empty buffer with the predecessor's tail, newest points kept", () => {
    const pred = pts(30, 1000);
    const out = stitchPredecessorTrail([], pred, 24, 40);
    expect(out).toHaveLength(24);
    // The 24 MOST RECENT of the 30 — a trail loses its old end, not its new one.
    expect(out[0][2]).toBe(pred[6][2]);
    expect(out[23][2]).toBe(1000);
  });

  it("never lets the seed exceed the buffer depth", () => {
    // Even asked for more seed than the buffer holds, the join is capped and
    // the newest points are the ones that survive.
    const out = stitchPredecessorTrail(pts(10, 2000), pts(60, 1000), 50, 40);
    expect(out).toHaveLength(40);
    expect(out[39][2]).toBe(2000);
  });

  it("is a no-op without a predecessor buffer", () => {
    const prev = pts(3, 500);
    expect(stitchPredecessorTrail(prev, undefined, 24, 40)).toEqual(prev);
    expect(stitchPredecessorTrail(prev, [], 24, 40)).toEqual(prev);
    expect(stitchPredecessorTrail(null, null, 24, 40)).toEqual([]);
  });

  it("drops predecessor points that are not older than the existing ones", () => {
    // The two keys overlap for a solve or two around the turn.  A trail that
    // is not monotonic in time would make smoothTrailPositions average across
    // a fold, so the overlap is dropped rather than interleaved.
    const prev = pts(2, 1005);          // ts 1004, 1005
    const pred = pts(6, 1006);          // ts 1001..1006
    const out = stitchPredecessorTrail(prev, pred, 24, 40);
    expect(out.map((p) => p[2])).toEqual([1001, 1002, 1003, 1004, 1005]);
  });

  it("borrows nothing when the seed allowance is zero", () => {
    expect(stitchPredecessorTrail([], pts(5, 900), 0, 40)).toEqual([]);
  });

  it("hands smoothTrailPositions a usable trail across the seam", () => {
    // The stitched buffer is just a trail: #366's smoother runs on it
    // unchanged, and the jump gate splitting the run at the seam (a real gap
    // in the measurements) is expected, not a defect.
    const pred: SolveTrailPoint[] = Array.from(
      { length: 8 },
      (_, i) => [34.85, -82.4 + i * 0.001, 1000 + i] as SolveTrailPoint,
    );
    const prev: SolveTrailPoint[] = Array.from(
      { length: 8 },
      (_, i) => [34.86, -82.39 + i * 0.001, 1030 + i] as SolveTrailPoint,
    );
    const out = stitchPredecessorTrail(prev, pred, 24, 40);
    expect(out).toHaveLength(16);
    // smoothed + head is every point back: with k=3 the un-centreable tail is
    // one point, and the seam split does not lose any.
    const { smoothed, head } = smoothTrailPositions(out, { k: 3 });
    expect(smoothed.length + head.length).toBe(16);
    expect([...smoothed, ...head].every(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon))).toBe(true);
  });
});
