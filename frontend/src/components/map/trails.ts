import { distanceKm } from "./distance";

const MAX_TRAIL_POINTS = 400;

export function mergeTrailPositions(existing = [], incoming = []) {
  if (!incoming.length) return existing;

  const normalizedIncoming = incoming
    .filter((point) => Array.isArray(point) && point.length >= 2)
    .slice()
    .sort((a, b) => (a[3] || 0) - (b[3] || 0));

  if (!existing.length) return normalizedIncoming.slice(-MAX_TRAIL_POINTS);

  const merged = [...existing];
  let last = merged[merged.length - 1];
  let lastTs = last?.[3] || 0;

  for (const point of normalizedIncoming) {
    const pointTs = point[3] || 0;

    if (pointTs <= lastTs) continue;

    if (
      last &&
      Math.abs(last[0] - point[0]) < 0.00001 &&
      Math.abs(last[1] - point[1]) < 0.00001
    ) {
      lastTs = pointTs;
      continue;
    }

    merged.push(point);
    last = point;
    lastTs = pointTs;
  }

  // Cap trail length — keep most recent points
  return merged.length > MAX_TRAIL_POINTS ? merged.slice(-MAX_TRAIL_POINTS) : merged;
}

export function sampleTrailPositions(positions, maxPoints = 240) {
  if (!Array.isArray(positions) || positions.length <= maxPoints) return positions || [];

  const stride = Math.ceil(positions.length / maxPoints);
  const sampled = positions.filter((_, index) => index % stride === 0);
  const last = positions[positions.length - 1];
  const tail = sampled[sampled.length - 1];

  if (!tail || tail[0] !== last[0] || tail[1] !== last[1]) {
    sampled.push(last);
  }

  return sampled;
}

export function buildTrailSegments(positions, numSegments = 8) {
  if (!positions || positions.length < 2) return [];
  const segs = [];
  const total = positions.length;
  const step = Math.max(1, Math.ceil(total / numSegments));
  for (let i = 0; i < numSegments; i++) {
    const start = i * step;
    const end = Math.min(start + step + 1, total);
    if (end - start < 2) break;
    const t = (i + 1) / numSegments;
    segs.push({
      positions: positions.slice(start, end),
      opacity: 0.08 + t * 0.92,
      weight: 1.5 + t * 3.5,
    });
  }
  return segs;
}

/* ── Centred moving average over the per-SOLVE trail buffer (dark multinode).
 *
 * Substrate matters more than the filter here.  The 2 Hz frontendTrailsRef
 * buffer samples the ICON, so it carries the backend's dead-reckoning sawtooth
 * (re-anchor step p90 116 m) and the 0.55 s glide hook on top of the solve
 * scatter.  This function runs over solve-epoch points instead
 * (solve_lat/solve_lon, which only move when a new solve lands), so both of
 * those disappear from the drawn trail and only the solver's own scatter is
 * left to average down.
 *
 * Centred, not causal: a trailing filter cannot cancel the lateral overshoot
 * of a bad solve, it only delays it.  The price is that the newest
 * floor(k/2) points have no right-hand neighbours yet — they are returned as
 * `head` for the caller to draw dashed, which is honest about their being
 * un-averaged, and the live icon covers the ~1.2 s of lag at k=3.
 *
 * The window RESETS at a step longer than max(jumpGateM, 3σ): a supersession
 * re-key or a mis-association moves the track kilometres in one solve, and
 * averaging across that would draw a smooth line through a position the
 * aircraft was never at.  Each run either side of the gate is smoothed
 * independently, and the raw points at a run's edges keep the discontinuity
 * exactly where the solver put it.
 * ── */

/** [lat, lon, tsSec, sigmaM?] — one solve-epoch point. */
export type SolveTrailPoint = [number, number, number, number?];
type LatLng = [number, number];

export function smoothTrailPositions(
  points: SolveTrailPoint[] | null | undefined,
  {
    k = 3,
    jumpGateM = 500,
    sigmaFallbackM = 300,
    maxSpeedMs = 350,
  }: { k?: number; jumpGateM?: number; sigmaFallbackM?: number; maxSpeedMs?: number } = {},
): { smoothed: LatLng[]; head: LatLng[] } {
  const pts = Array.isArray(points) ? points : [];
  const half = Math.floor(Math.max(1, k) / 2);
  if (!pts.length) return { smoothed: [], head: [] };

  // Split at teleports.  σ is taken as the larger of the two endpoints'
  // sigmas so a step out of a badly-conditioned solve is judged against that
  // solve's own stated accuracy, not its neighbour's; a point with no stated
  // sigma is judged at sigmaFallbackM (gate only — see the weighting below).
  // The gate also allows for the aircraft's own motion over the interval:
  // at 250 m/s a 3 s solve gap is 750 m of honest travel, which on its own
  // would clear a bare max(500 m, 3σ) floor and split the window on
  // ordinary straight flight.
  const runs: SolveTrailPoint[][] = [[pts[0]]];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    const sigma = Math.max(a[3] ?? sigmaFallbackM, b[3] ?? sigmaFallbackM);
    const dtS = Math.max(0, (b[2] ?? 0) - (a[2] ?? 0));
    const gateM = Math.max(jumpGateM, 3 * sigma) + maxSpeedMs * dtS;
    if (distanceKm(a[0], a[1], b[0], b[1]) * 1000 > gateM) runs.push([b]);
    else runs[runs.length - 1].push(b);
  }

  const flat: LatLng[] = [];
  for (const run of runs) {
    for (let i = 0; i < run.length; i++) {
      // Edges of a run have no full window; they stay raw so the run starts
      // and ends where the solver said it did.
      if (half === 0 || i < half || i + half > run.length - 1) {
        flat.push([run[i][0], run[i][1]]);
        continue;
      }
      // Inverse-variance weights when every point in the window states a
      // sigma; a plain mean otherwise (pos_sigma_m is absent on ~3/4 of
      // tracks, and mixing 1/σ² with an assumed σ would silently re-rank them).
      let weighted = true;
      for (let j = i - half; j <= i + half; j++) {
        const s = run[j][3];
        if (!(typeof s === "number" && s > 0 && Number.isFinite(s))) { weighted = false; break; }
      }
      let sumLat = 0;
      let sumLon = 0;
      let sumW = 0;
      for (let j = i - half; j <= i + half; j++) {
        const w = weighted ? 1 / (run[j][3] as number) ** 2 : 1;
        sumLat += run[j][0] * w;
        sumLon += run[j][1] * w;
        sumW += w;
      }
      flat.push([sumLat / sumW, sumLon / sumW]);
    }
  }

  // The un-centreable tail belongs to the LAST run only: a head that reached
  // back across a teleport gate would draw the dashed segment through the gap
  // the split exists to preserve.
  const tailLen = Math.min(half, runs[runs.length - 1].length);
  if (tailLen === 0) return { smoothed: flat, head: [] };
  return { smoothed: flat.slice(0, flat.length - tailLen), head: flat.slice(flat.length - tailLen) };
}
