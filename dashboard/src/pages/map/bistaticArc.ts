// Bistatic-ellipse ambiguity arc builder — JS port of
// backend/services/track_gates.py::_build_single_node_arc.
//
// Used to rebuild the displayed arc client-side from the MEASURED delay_us.
// The arc is the full delay ellipse clipped to the node's detection area
// (differential-range limit or monostatic circle, plus the beam wedge when
// the node genuinely has one) — it is NOT trimmed around the aircraft
// position, so it represents every position consistent with the measured
// delay.
//
// Ground-projected, altitude-agnostic by design (2026-08 direction): the
// arc is a pure function of the measured delay and the node's detection-area
// geometry — target altitude never enters it.  A high-altitude target's arc
// therefore sits a few km outside its ground position; that is the intended
// raw-measurement picture, not a bug.

export interface NodeGeometry {
  /**
   * The receiver coordinate as the map holds it. For a public node it is the
   * server-published one, displaced from the operator's true position by the
   * backend (backend/services/public_location.py), and the backend builds its
   * own published arcs around this same anchor, so an arc rebuilt here lands
   * on the served curve by construction. The browser has nothing more
   * accurate for that node. A signed-in owner's own node comes from the owner
   * API instead (map/nodeListing.ts): there it is the true receiver position,
   * and the arc rebuilt here is the true ellipse.
   */
  rx_lat: number;
  rx_lon: number;
  tx_lat: number;
  tx_lon: number;
  /**
   * RX/TX altitudes (m ASL).  No longer read by this builder (arcs are
   * altitude-agnostic, 2026-08) — kept on the type because other callers
   * populate and pass through NodeGeometry objects carrying them (see
   * hooks.ts / types.ts).
   */
  rx_alt_m?: number | null;
  tx_alt_m?: number | null;
  beam_azimuth_deg?: number | null;
  beam_width_deg?: number | null;
  max_range_km?: number | null;
  /** Differential-range detection limit (km), when the node declares one. */
  max_bistatic_range_km?: number | null;
}

export function bearingDeg(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const lat1r = (lat1 * Math.PI) / 180;
  const lat2r = (lat2 * Math.PI) / 180;
  const dlonr = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dlonr) * Math.cos(lat2r);
  const x =
    Math.cos(lat1r) * Math.sin(lat2r) -
    Math.sin(lat1r) * Math.cos(lat2r) * Math.cos(dlonr);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

/**
 * A sweep's bearings as unit vectors: east (sine) and north (cosine) of
 * each, from the first edge to the last.
 */
interface BearingFan {
  sweepWidthDeg: number;
  steps: number;
  sin: Float64Array;
  cos: Float64Array;
}

/**
 * Fans by centre bearing.  A node's bearings follow from its beam alone,
 * so every arc of the node shares one fan; cleared wholesale past
 * BEARING_FANS_MAX sweeps, far more nodes than a map shows.
 */
const bearingFans = new Map<number, BearingFan>();
const BEARING_FANS_MAX = 512;

function bearingFan(centreBearing: number, sweepWidthDeg: number, steps: number): BearingFan {
  const held = bearingFans.get(centreBearing);
  if (held && held.sweepWidthDeg === sweepWidthDeg && held.steps === steps) return held;
  const sin = new Float64Array(steps + 1);
  const cos = new Float64Array(steps + 1);
  const halfSweep = sweepWidthDeg / 2;
  for (let step = 0; step <= steps; step++) {
    const bearing = centreBearing - halfSweep + sweepWidthDeg * (step / steps);
    const bearingRad = (bearing * Math.PI) / 180;
    sin[step] = Math.sin(bearingRad);
    cos[step] = Math.cos(bearingRad);
  }
  const fan = { sweepWidthDeg, steps, sin, cos };
  if (bearingFans.size >= BEARING_FANS_MAX) bearingFans.clear();
  bearingFans.set(centreBearing, fan);
  return fan;
}

/**
 * Build the bistatic-ambiguity arc for one detection.
 *
 * The arc is the locus of points where the bistatic delay equals delayUs,
 * clipped to the node's detection area: the differential-range limit when
 * the node declares max_bistatic_range_km (else the monostatic max_range_km
 * circle), and the beam wedge when the node genuinely has one (a declared
 * width >= 360° means omnidirectional → the full closed ellipse).
 *
 * Mirrors backend/services/track_gates.py::_build_single_node_arc — the arc
 * is deliberately NOT trimmed around the aircraft's own position: it shows
 * every position the aircraft could occupy given the measured delay.
 *
 * Ground-projected, altitude-agnostic by design (2026-08 direction): this
 * builder is a pure function of delayUs and the node's detection-area
 * geometry — target altitude is never consulted, so a high-altitude
 * target's arc sits a few km outside its ground position.  That is the
 * intended raw-measurement picture, not a bug; see the backend docstring
 * for the reasoning.
 *
 * Returns null when geometry is missing or the arc can't be constructed
 * (e.g. delay too large for any in-area range).
 */
export function buildBistaticArc(
  delayUs: number,
  node: NodeGeometry,
): [number, number][] | null {
  if (!delayUs || delayUs <= 0) return null;
  const { rx_lat, rx_lon, tx_lat, tx_lon } = node;
  if (rx_lat == null || rx_lon == null || tx_lat == null || tx_lon == null) {
    return null;
  }

  const beamWidthDeg = Number(node.beam_width_deg ?? 42);
  const maxRangeKm = Number(node.max_range_km ?? 50);
  let beamAzimuthDeg = node.beam_azimuth_deg;
  if (beamAzimuthDeg == null) {
    beamAzimuthDeg = (bearingDeg(rx_lat, rx_lon, tx_lat, tx_lon) + 90) % 360;
  }

  const cosLat = Math.max(0.1, Math.cos(((rx_lat + tx_lat) / 2 * Math.PI) / 180));
  const txEastKm = (tx_lon - rx_lon) * 111.32 * cosLat;
  const txNorthKm = (tx_lat - rx_lat) * 111.32;
  const baselineKm = Math.hypot(txEastKm, txNorthKm);
  const differentialRangeKm = delayUs * 0.299792458;

  // Differential range at rangeKm along the bearing whose sine and cosine
  // are given: the TX-target-RX path minus the baseline.
  const differentialAt = (rangeKm: number, sinB: number, cosB: number): number => {
    const eastKm = sinB * rangeKm;
    const northKm = cosB * rangeKm;
    const txDistKm = Math.hypot(eastKm - txEastKm, northKm - txNorthKm);
    return txDistKm + rangeKm - baselineKm;
  };

  // differentialAt(0, ·) is exactly 0 for every bearing in the 2-D locus
  // (RX's own ground point is always inside it), and differentialRangeKm is
  // strictly positive here — so this can never actually reject.  Kept as
  // the explicit invariant check rather than assumed silently.
  if (differentialAt(0, 0, 1) > differentialRangeKm) return null;

  // Per-bearing range ceiling.  With a declared differential limit the
  // whole ellipse passes or fails at once (every point shares the measured
  // differential); the ceiling is then D/2 + baseline, a bound the locus
  // provably never exceeds.  Without one, the monostatic circle clips per
  // bearing.
  let ceilingKm: number;
  const maxBistaticKm = node.max_bistatic_range_km;
  if (maxBistaticKm != null && Number.isFinite(maxBistaticKm)) {
    if (differentialRangeKm > Number(maxBistaticKm)) return null;
    ceilingKm = differentialRangeKm / 2 + baselineKm;
  } else {
    ceilingKm = maxRangeKm;
  }

  // Sweep exactly the in-area bearing interval, centred on the boresight so
  // the midpoint sits on the boresight crossing (matches the backend, whose
  // arc midpoint becomes the track's displayed position).
  let sweepWidthDeg: number;
  let steps: number;
  if (beamWidthDeg >= 360) {
    sweepWidthDeg = 360;
    steps = 72;
  } else {
    sweepWidthDeg = beamWidthDeg;
    steps = 36;
  }
  const centreBearing = beamAzimuthDeg;

  // Each bearing's range comes in closed form.  With RX at the origin, unit
  // bearing u and the TX at T, the locus point at range r satisfies
  // |r*u - T| = D + L - r; squaring both sides cancels r^2 and leaves
  //   r = D * (D + 2L) / (2 * (D + L - u.T)),
  // the polar equation of the ellipse about its RX focus.  The denominator
  // is at least D > 0, so every bearing has exactly one crossing, and the
  // differential never falls as r grows, so it is the crossing the
  // backend's per-bearing binary search converges to.
  const rangeNumerator = differentialRangeKm * (differentialRangeKm + 2 * baselineKm);
  const lonKmPerDeg = 111.32 * Math.max(0.1, Math.cos((rx_lat * Math.PI) / 180));
  const fan = bearingFan(centreBearing, sweepWidthDeg, steps);
  // The differential at range r is at least 2r - 2L, so no crossing lies
  // past D/2 + L.  A ceiling clear of that keeps every bearing, and the
  // per-bearing test below is skipped; the margin is far above the
  // rounding on either side, so the test would have kept each one too.
  const ceilingClearsLocus =
    ceilingKm - baselineKm - differentialRangeKm / 2 > 1e-9 * (ceilingKm + baselineKm);
  const points: [number, number][] = [];
  for (let step = 0; step <= steps; step++) {
    const sinB = fan.sin[step];
    const cosB = fan.cos[step];
    // A bearing is kept on the same test the backend applies (the
    // differential at the ceiling against the measured one), so the kept
    // bearings agree with it even where the crossing sits on the ceiling
    // itself, as it does toward the TX under a declared differential limit.
    if (
      !ceilingClearsLocus &&
      differentialAt(ceilingKm, sinB, cosB) < differentialRangeKm
    ) continue;
    let rangeKm = rangeNumerator / (
      2 * (differentialRangeKm + baselineKm - (sinB * txEastKm + cosB * txNorthKm))
    );
    // A crossing on the ceiling may round a hair past it; it is held there,
    // as the backend's search, which starts from the ceiling, holds it.
    if (rangeKm > ceilingKm) rangeKm = ceilingKm;
    points.push([
      rx_lat + (rangeKm * cosB) / 111.32,
      rx_lon + (rangeKm * sinB) / lonKmPerDeg,
    ]);
  }

  if (points.length < 2) return null;
  return points;
}
