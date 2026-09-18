// Screen-space trimming of a polyline down to a short section centred on a
// point of interest.
//
// Used by ClaimedArcs to cut a fixed-pixel-length piece out of a node's full
// bistatic locus, centred where that locus passes the aircraft's ADS-B fix.
// Everything here works in PROJECTED PIXEL space (Leaflet layer points), which
// is what makes the result zoom-invariant; the caller projects in and
// unprojects out.
//
// The backend locus carries only 37 or 73 vertices across tens of kilometres,
// so a 45–75 px section almost always falls INSIDE a single segment.  Vertex-
// granular cutting would therefore be useless — it would return either nothing
// or a whole multi-km segment.  Both the closest-approach search and the two
// end cuts consequently interpolate within segments.

export interface XY {
  x: number;
  y: number;
}

function lerp(a: XY, b: XY, t: number): XY {
  return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
}

/**
 * Arc-length position (in px along the polyline from pts[0]) of the point on
 * the polyline closest to `anchor`.  Returns 0 for a degenerate input.
 */
function closestArcLength(pts: XY[], cum: number[], anchor: XY): number {
  let bestDistSq = Infinity;
  let bestS = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const segLenSq = dx * dx + dy * dy;
    // Project the anchor onto the segment, clamped to the segment's extent so
    // the foot of the perpendicular can't escape past an endpoint.
    const t = segLenSq > 0 ? Math.max(0, Math.min(1, ((anchor.x - a.x) * dx + (anchor.y - a.y) * dy) / segLenSq)) : 0;
    const px = a.x + dx * t;
    const py = a.y + dy * t;
    const distSq = (anchor.x - px) ** 2 + (anchor.y - py) ** 2;
    if (distSq < bestDistSq) {
      bestDistSq = distSq;
      bestS = cum[i] + t * Math.sqrt(segLenSq);
    }
  }
  return bestS;
}

/** Point at arc-length `s` px along the polyline, interpolated within its segment. */
function pointAtArcLength(pts: XY[], cum: number[], s: number): XY {
  const total = cum[cum.length - 1];
  if (s <= 0) return pts[0];
  if (s >= total) return pts[pts.length - 1];
  // Linear scan: these polylines are 37/73 points, so a binary search would
  // only add branches.
  for (let i = 0; i < pts.length - 1; i++) {
    const segLen = cum[i + 1] - cum[i];
    if (segLen > 0 && s <= cum[i + 1]) return lerp(pts[i], pts[i + 1], (s - cum[i]) / segLen);
  }
  return pts[pts.length - 1];
}

/**
 * Cut `lengthPx` of polyline out of `pts`, centred on the closest approach to
 * `anchor`.  The section is clipped at the polyline's own ends, so a target
 * near a locus endpoint gets a shorter (not off-locus) arc.
 *
 * Returns the trimmed point list (interpolated ends included, ≥ 2 points), or
 * null when there is nothing drawable: fewer than two vertices, or a polyline
 * of zero total length.
 */
export function trimAroundAnchor(pts: XY[], anchor: XY, lengthPx: number): XY[] | null {
  if (!Array.isArray(pts) || pts.length < 2 || !(lengthPx > 0)) return null;

  const cum: number[] = new Array(pts.length);
  cum[0] = 0;
  for (let i = 1; i < pts.length; i++) {
    cum[i] = cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
  }
  const total = cum[pts.length - 1];
  if (total <= 0) return null;

  const centre = closestArcLength(pts, cum, anchor);
  const half = lengthPx / 2;
  // Clamp the window to the polyline, then re-widen from the clamped edge, so
  // a centre within half a length of an end still yields the full requested
  // section (shifted) rather than a half-length stub.
  let start = centre - half;
  let end = centre + half;
  if (start < 0) end = Math.min(total, end - start);
  if (end > total) start = Math.max(0, start - (end - total));
  start = Math.max(0, start);
  end = Math.min(total, end);

  const out: XY[] = [pointAtArcLength(pts, cum, start)];
  for (let i = 0; i < pts.length; i++) {
    if (cum[i] > start && cum[i] < end) out.push(pts[i]);
  }
  out.push(pointAtArcLength(pts, cum, end));
  return out;
}
