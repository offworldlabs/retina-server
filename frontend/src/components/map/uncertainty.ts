/* ------------------------------------------------------------------ */
/*  Position-uncertainty maths for multi-node solves                   */
/* ------------------------------------------------------------------ */

/**
 * Pure helpers behind the 95%-confidence disc drawn around every multi-node
 * solve (SolveUncertaintyLayer) and quoted in the detail panel.
 *
 * The backend publishes a calibrated per-axis position sigma at the solve
 * epoch (`pos_sigma_m`) plus the velocity sigma that governs how fast the
 * estimate decays while the frontend dead-reckons between solves
 * (`pos_sigma_vel_ms`).  See
 * docs/design-notes/2026-09-05-solve-uncertainty-disc.md for the fit.
 *
 * Radial error is modelled as Rayleigh with sigma per axis, so the radius
 * containing the aircraft with probability p is k_p·sigma.  No Leaflet, no
 * React — this module is unit-tested on its own.
 */

import type { Aircraft } from "../../types";

/** Rayleigh 95% radius factor (k_50 = 1.177 CEP, k_68 = 1.510). */
export const UNCERTAINTY_K95 = 2.4477;

/** Dead-reckoning growth is capped here: past 60 s the icon itself is stale
 *  and a disc that kept growing would just be a claim about nothing. */
export const UNCERTAINTY_DR_CAP_S = 60;

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

/**
 * Age of the solve behind this entry, in seconds.
 *
 * `seen` is the backend's age-of-solve at flush time; the `_updatedAt` term
 * adds the wall-clock time since the message reached us, which `seen` cannot
 * know about (a WS gap, a backgrounded tab).  Same composition as `drDriftM`.
 */
export function solveAgeS(ac: UncertaintyEntry | null | undefined, nowMs: number): number {
  const seen = ac?.seen ?? 0;
  const updatedAt = ac?._updatedAt ?? nowMs;
  return seen + Math.max(0, (nowMs - updatedAt) / 1000);
}

/**
 * Per-axis position sigma in metres at age `ageS`:
 * `sqrt(pos_sigma_m² + (pos_sigma_vel_ms · min(age, 60))²)`.
 *
 * Returns null when the entry carries no usable `pos_sigma_m` — an older
 * backend, or a solve whose node count was unknown.  A missing or non-finite
 * velocity sigma means no growth rather than no answer: the solve epoch
 * figure is still honest.
 */
export function solveSigmaM(ac: UncertaintyEntry | null | undefined, ageS: number): number | null {
  const sigma0 = ac?.pos_sigma_m;
  if (sigma0 == null || !Number.isFinite(sigma0) || sigma0 <= 0) return null;
  const sigmaV = ac?.pos_sigma_vel_ms;
  const growthRate = sigmaV != null && Number.isFinite(sigmaV) && sigmaV > 0 ? sigmaV : 0;
  const age = Number.isFinite(ageS) ? Math.min(Math.max(ageS, 0), UNCERTAINTY_DR_CAP_S) : 0;
  const drift = growthRate * age;
  return Math.sqrt(sigma0 * sigma0 + drift * drift);
}

/**
 * Radius in metres of the 95%-confidence disc for `ac` at wall-clock `nowMs`,
 * capped at UNCERTAINTY_MAX_RADIUS_M.
 *
 * 0 means "draw nothing": non-multi-node entries have no calibration behind
 * them, and a multi-node entry without `pos_sigma_m` would otherwise get a
 * disc asserting a precision the feed never stated.
 */
export function solveUncertaintyRadiusM(ac: UncertaintyEntry | null | undefined, nowMs: number): number {
  if (!isMultinodeSolve(ac)) return 0;
  const sigma = solveSigmaM(ac, solveAgeS(ac, nowMs));
  if (sigma == null) return 0;
  return Math.min(UNCERTAINTY_K95 * sigma, UNCERTAINTY_MAX_RADIUS_M);
}
