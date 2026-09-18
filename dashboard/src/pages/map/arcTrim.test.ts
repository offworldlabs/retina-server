import { describe, it, expect } from "vitest";
import { trimAroundAnchor, type XY } from "./arcTrim";

const len = (pts: XY[]) =>
  pts.slice(1).reduce((acc, p, i) => acc + Math.hypot(p.x - pts[i].x, p.y - pts[i].y), 0);

/** Distance from `p` to the nearest point of polyline `pts` (segment-wise). */
function distToPolyline(p: XY, pts: XY[]): number {
  let best = Infinity;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const l2 = dx * dx + dy * dy;
    const t = l2 > 0 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2)) : 0;
    best = Math.min(best, Math.hypot(p.x - (a.x + dx * t), p.y - (a.y + dy * t)));
  }
  return best;
}

/** Coarse arc of `n` vertices spanning `spanPx`, curved like a real locus. */
function fixtureArc(n: number, spanPx: number): XY[] {
  return Array.from({ length: n }, (_, i) => {
    const u = i / (n - 1);
    return { x: u * spanPx, y: 0.15 * spanPx * Math.sin(u * Math.PI) };
  });
}

describe("trimAroundAnchor", () => {
  it("cuts the requested length centred on the closest approach", () => {
    const line: XY[] = [{ x: 0, y: 0 }, { x: 1000, y: 0 }];
    const out = trimAroundAnchor(line, { x: 500, y: 20 }, 100)!;
    expect(out).not.toBeNull();
    expect(out[0].x).toBeCloseTo(450, 6);
    expect(out[out.length - 1].x).toBeCloseTo(550, 6);
    expect(len(out)).toBeCloseTo(100, 6);
  });

  it("interpolates inside a single segment rather than snapping to vertices", () => {
    // The real case: 37 vertices over tens of km, a 60 px cut. A vertex-
    // granular implementation would return a segment hundreds of px long.
    const arc = fixtureArc(37, 2400);
    const anchor = { x: 1210, y: 400 };
    const out = trimAroundAnchor(arc, anchor, 60)!;
    expect(len(out)).toBeCloseTo(60, 4);
    // Two or three points: the two interpolated cuts, plus at most the one
    // original vertex the window can straddle at this vertex spacing.
    expect(out.length).toBeLessThanOrEqual(3);
    // And every returned point must still lie ON the original locus.
    for (const p of out) expect(distToPolyline(p, arc)).toBeLessThan(1e-6);
  });

  it("keeps original vertices that fall strictly inside the window", () => {
    const line: XY[] = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 20, y: 0 }, { x: 30, y: 0 }];
    const out = trimAroundAnchor(line, { x: 15, y: 5 }, 16)!;
    expect(out.map((p) => p.x)).toEqual([7, 10, 20, 23]);
  });

  it("shifts, not shortens, when the closest approach sits near an end", () => {
    const line: XY[] = [{ x: 0, y: 0 }, { x: 1000, y: 0 }];
    const out = trimAroundAnchor(line, { x: 5, y: 0 }, 100)!;
    expect(len(out)).toBeCloseTo(100, 6);
    expect(out[0].x).toBeCloseTo(0, 6);
    expect(out[out.length - 1].x).toBeCloseTo(100, 6);
  });

  it("returns the whole polyline when it is shorter than the requested cut", () => {
    const line: XY[] = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 20, y: 0 }];
    const out = trimAroundAnchor(line, { x: 10, y: 3 }, 500)!;
    expect(len(out)).toBeCloseTo(20, 6);
    expect(out[0].x).toBeCloseTo(0, 6);
    expect(out[out.length - 1].x).toBeCloseTo(20, 6);
  });

  it("finds the closest approach on a curve, not the closest vertex", () => {
    // Anchor sits off the apex of a wide V whose vertices are far from it;
    // the cut must centre on the foot of the perpendicular.
    const line: XY[] = [{ x: 0, y: 0 }, { x: 100, y: 100 }, { x: 200, y: 0 }];
    const out = trimAroundAnchor(line, { x: 20, y: 40 }, 20)!;
    const mid = { x: (out[0].x + out[out.length - 1].x) / 2, y: (out[0].y + out[out.length - 1].y) / 2 };
    expect(mid.x).toBeCloseTo(30, 0);
    expect(mid.y).toBeCloseTo(30, 0);
  });

  it("declines degenerate input", () => {
    expect(trimAroundAnchor([], { x: 0, y: 0 }, 50)).toBeNull();
    expect(trimAroundAnchor([{ x: 1, y: 1 }], { x: 0, y: 0 }, 50)).toBeNull();
    // Zero-length polyline: every vertex coincident (fully zoomed out).
    expect(trimAroundAnchor([{ x: 5, y: 5 }, { x: 5, y: 5 }], { x: 0, y: 0 }, 50)).toBeNull();
    expect(trimAroundAnchor([{ x: 0, y: 0 }, { x: 10, y: 0 }], { x: 5, y: 0 }, 0)).toBeNull();
  });
});
