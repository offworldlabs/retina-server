/* ------------------------------------------------------------------ */
/*  Position-uncertainty maths for multi-node solves                   */
/* ------------------------------------------------------------------ */

/**
 * Pure helpers behind the 95%-confidence disc drawn around every multi-node
 * solve (SolveUncertaintyLayer) and quoted in the detail panel.
 *
 * The model (2026-09-06): **the disc is the calibrated accuracy of the last
 * solve, drawn where that solve was.**  The backend publishes the solve-epoch
 * per-axis sigma (`pos_sigma_m`) and the position that sigma belongs to
 * (`solve_lat`/`solve_lon`, the fix before the feed dead-reckons `lat`/`lon`
 * forward).  The radius is `UNCERTAINTY_K95 · pos_sigma_m` — a constant for
 * as long as the solve stands — and the centre is the solve epoch, so the
 * icon visibly walks out of its own disc as it dead-reckons.  That separation
 * IS the extrapolation: it says "the last thing anyone measured was there,
 * this well; the plane in front of you is a guess since."
 *
 * The disc used to grow with age (`sqrt(sigma0² + (sigma_v·t)²)`).  It was
 * dropped because the growth was neither honest nor useful: dark solves carry
 * the KF's 150 m/s velocity-sigma clamp, so the disc hit the 10 km ceiling
 * within ~30 s and swallowed the map, while measured coverage of the grown
 * disc stayed near 50% — worse than the ungrown one, not better.  Comparable
 * systems (ATC coasting, MIL-STD-2525 area of uncertainty, GPS accuracy
 * circles) draw measurement accuracy at the measurement and convey staleness
 * through the symbol; `drIconState` is where staleness lives here.  See
 * docs/design-notes/2026-09-05-solve-uncertainty-disc.md for the fit and the
 * 2026-09-06 section for this change.
 *
 * Radial error is modelled as Rayleigh with sigma per axis, so the radius
 * containing the aircraft with probability p is k_p·sigma.  No Leaflet, no
 * React — this module is unit-tested on its own.
 */

import type { Aircraft } from "../../types";

/** Rayleigh 95% radius factor (k_50 = 1.177 CEP, k_68 = 1.510). */
export const UNCERTAINTY_K95 = 2.4477;

/** Hard ceiling on the drawn radius.  A degenerate solve (near-parallel
 *  baselines) can report an astronomically large formal sigma; without a cap
 *  it would paint the whole viewport. */
export const UNCERTAINTY_MAX_RADIUS_M = 10000;

/** A feed entry as stored in `fixesRef` / the 2 Hz display array: the wire
 *  aircraft plus the arrival timestamp the map stamps on it. */
export interface UncertaintyEntry extends Partial<Aircraft> {
  /** ms epoch when this entry was last ingested from the feed. */
  _updatedAt?: number;
}

/** True for entries the disc applies to — the multi-node lane only. */
function isMultinodeSolve(ac: UncertaintyEntry | null | undefined): boolean {
  return !!ac && (ac.position_source === "multinode_solve" || !!ac.multinode);
}

/** A usable coordinate: present, numeric and finite. */
function isFiniteNum(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

/**
 * Age of the solve behind this entry, in seconds.
 *
 * `seen` is the backend's age-of-solve at flush time; the `_updatedAt` term
 * adds the wall-clock time since the message reached us, which `seen` cannot
 * know about (a WS gap, a backgrounded tab).  Same composition as `drDriftM`.
 *
 * The disc no longer uses this — it is the icon's staleness clock
 * (`drIconState`), kept here so the icon and the disc share one definition of
 * how old a solve is.
 */
export function solveAgeS(ac: UncertaintyEntry | null | undefined, nowMs: number): number {
  const seen = ac?.seen ?? 0;
  const updatedAt = ac?._updatedAt ?? nowMs;
  return seen + Math.max(0, (nowMs - updatedAt) / 1000);
}

/**
 * Per-axis position sigma in metres at the solve epoch, i.e. `pos_sigma_m`
 * as published, sanity-checked.
 *
 * Returns null when the entry carries no usable `pos_sigma_m` — an older
 * backend, or a solve whose node count was unknown — which the caller reads
 * as "draw nothing".
 */
export function solveSigmaM(ac: UncertaintyEntry | null | undefined): number | null {
  const sigma0 = ac?.pos_sigma_m;
  if (!isFiniteNum(sigma0) || sigma0 <= 0) return null;
  return sigma0;
}

/**
 * Where the disc belongs: the position the solve actually measured.
 *
 * `solve_lat`/`solve_lon` when the feed carries both (they are the stored
 * track's fix, before the backend dead-reckons `lat`/`lon` forward), else
 * `lat`/`lon` — an older backend publishes no solve-epoch pair, and its
 * dead-reckoned position is the only one on offer.  Null when neither pair is
 * usable, so a caller never plots a NaN.
 */
export function solveDiscCenter(ac: UncertaintyEntry | null | undefined): [number, number] | null {
  const solveLat = ac?.solve_lat;
  const solveLon = ac?.solve_lon;
  if (isFiniteNum(solveLat) && isFiniteNum(solveLon)) return [solveLat, solveLon];
  const lat = ac?.lat;
  const lon = ac?.lon;
  if (isFiniteNum(lat) && isFiniteNum(lon)) return [lat, lon];
  return null;
}

/**
 * Radius in metres of the 95%-confidence disc for `ac`, capped at
 * UNCERTAINTY_MAX_RADIUS_M.  Independent of age: the number describes the
 * last solve, and the last solve does not change while it stands.
 *
 * 0 means "draw nothing": non-multi-node entries have no calibration behind
 * them, and a multi-node entry without `pos_sigma_m` would otherwise get a
 * disc asserting a precision the feed never stated.
 */
export function solveUncertaintyRadiusM(ac: UncertaintyEntry | null | undefined): number {
  if (!isMultinodeSolve(ac)) return 0;
  const sigma = solveSigmaM(ac);
  if (sigma == null) return 0;
  return Math.min(UNCERTAINTY_K95 * sigma, UNCERTAINTY_MAX_RADIUS_M);
}
