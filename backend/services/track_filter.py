"""Multinode display-track smoother: a per-track-key constant-velocity Kalman
filter over solve positions.

services.tasks.solver publishes one multinode solve at a time, keyed by the
same track key state.multinode_tracks uses (see _MN_TRACKS_LOCK / the
supersession block there).  Each solve has its own position error — GDOP ×
delay noise, worse at n=2 than n=4 — and until now that error was smoothed by
_ewma_smooth_track: dead-reckon up to _MN_HISTORY_K past positions forward to
the current solve time and take an unweighted mean.  That treats every solve
as equally trustworthy, which they are not, and it has no velocity STATE of
its own — the "dead reckoning" is really "recompute from ADS-B/solved
velocity every call and average the results."

This module replaces that with a proper 4-state (east, v_east, north, v_north)
CV Kalman filter, one instance per track key, for three reasons a mean cannot
give you:

  1. Weighted trust.  The solver now attaches a per-solve position covariance
     (cov_en_km2, from retina_geolocator.multinode_solver's Jacobian) — a bad
     GDOP geometry produces a wide covariance and this filter believes that
     solve less, automatically, instead of averaging it in at full weight.
     That Jacobian covariance is only a measurement-noise-propagation LOWER
     BOUND on true solve error, though — it cannot see inter-node frame-time
     skew, association contamination, or pinned-altitude error, which are
     INDEPENDENT of the fit noise, not a scaled version of it.  So the
     covariance is both inflated (_KF_R_INFLATE, for relative weighting
     between a good solve and a bad one) AND added to an unmodeled-error
     floor (_KF_DEFAULT_POS_SIGMA_M, now unconditional) rather than replaced
     by a single flat multiplier — see _measurement_R and the _KF_R_INFLATE
     comment for the staging measurements that made the additive form
     necessary.
  2. Principled dead-reckoning.  The predict step is real KF prediction, not
     a recomputed-every-time projection: the filter's OWN velocity state
     accumulates evidence from every solve's POSITION update (plus an
     explicit velocity measurement from ADS-B when live — see
     _KF_VEL_SIGMA_SOLVE_MS for why the solved CV fit is deliberately NOT
     fed in the same way: it is an init prior only, never a measurement), so
     between-solve gaps are bridged with a growing, honestly-tracked
     uncertainty rather than a fresh point estimate each call.
  3. Honest uncertainty out.  kf_pos_sigma_m on the smoothed result is the
     filter's actual position marginal, not an assumption — callers that want
     to gate on confidence (map rendering, anomaly detectors) get a real
     number instead of nothing.

The CV model is CONSTANT-VELOCITY, not constant-anything-else, and an
aircraft in a coordinated turn is neither: at 250 m/s a 1 deg/s turn pulls
4.4 m/s^2 and a standard-rate 3 deg/s turn 13.1 m/s^2 of lateral
acceleration, an order of magnitude past what a process noise of
_KF_SIGMA_A_MS2 = 1.5 m^2/s^3 admits.  Simulated against the shipped
constants, a converged filter entering a 3 deg/s turn used to breach the
chi-squared gate after 108-110 degrees of turn at every solve cadence
(2.3 s, 4.6 s, 12 s) with a velocity error of 280-290 m/s — more than the
aircraft's own speed — and the gate then re-anchored the track, because the
code read a breach as an identity break by definition.  Downstream that
mints a second mn-dark-* key for one aircraft (learned_velocity is what
solver.py's _entry_dr_velocity dead-reckons the key decision with) and drops
the dark_follow target for its 30 s cooldown (the re-anchored velocity sigma
of 150 m/s is above DARK_FOLLOW_MAX_VEL_SIGMA_MS = 60).

So sigma_a is MANOEUVRE-ADAPTIVE rather than constant, in two coupled parts
(see _entry_sigma_a, _update_manoeuvre and the gate block in _smooth_kf):
an entry that gets a surprising update — normalised innovation d^2 past
_KF_MANOEUVRE_D2 — raises its process noise toward
_KF_SIGMA_A_MANOEUVRE_MS2 and holds it there, decaying back on a
_KF_MANOEUVRE_TAU_S clock once the surprises stop; and a gate breach is
RETRIED once with the manoeuvre process noise before it is allowed to
re-anchor, so only an innovation no plausible acceleration explains is
treated as an identity break.  Both are inert in straight flight — an entry
that has never been surprised runs the same sigma_a = 1.5 CV filter as
before, which is what keeps TestStoneSoupOracle's step-by-step equality
valid — and both are env-tunable (TRACK_KF_SIGMA_A_MANOEUVRE,
TRACK_KF_MANOEUVRE_D2, TRACK_KF_MANOEUVRE_TAU_S), with the manoeuvre sigma
set at or below the base disabling the whole path.  Measured on the same
3 deg/s / 180-degree synthetic turn: re-anchors 1 -> 0 at all three
cadences, peak in-turn velocity error 346-353 -> 261-286 m/s, and the
straight-flight RMSE gain against raw solves 37.4% -> 35.1%.

The filter is EWMA-compatible where it needs to be: first solve for a key is
raw passthrough (no prior to smooth against, exactly like _ewma_smooth_track
returning raw below len(positions) < 2), a gap past _KF_MAX_GAP_S re-anchors
instead of bridging — the same threshold solver.py's _MN_DR_MAX_AGE_S uses
for the same reason (a multi-minute gap is not the same aircraft's
continuous track) — and duplicate/out-of-order timestamps (dt <= 0) are raw
passthrough, same as the EWMA.  That last case is not a rare edge condition
in production: it fires mostly as an association burst, several solves for
one key sharing a single measurement_ts_ms because overlapping single-node
association rounds re-solved the same epoch's measurements.  Fusing those
would double-count the same underlying evidence rather than add independent
information, so a no-op here is intentional, not a gap in the model — see
the comment at the dt <= 0 check in _smooth_kf.

Env gate — TRACK_SMOOTHER, read PER CALL (not cached) so tests can flip it
without reimporting:
    "kf"   (default) — this module's Kalman filter.
    "ewma" — delegate to the caller-supplied legacy _ewma_smooth_track, kept
             around as a fallback in case the KF misbehaves in production in
             a way the EWMA never did.
    "off"  — raw passthrough, no smoothing at all.
    anything else — treated as "kf".

Lock-order constraint: every caller of smooth_solve() holds solver.py's
_MN_TRACKS_LOCK (see the comment at that call site).  _KF_LOCK below is a
LEAF lock — nothing under it may call back into solver.py, acquire
_MN_TRACKS_LOCK, or acquire _MN_POS_HISTORY_LOCK.  Keeping that one-directional
means solver.py -> track_filter is the only lock order that ever exists, so
there is no deadlock to reason about.
"""

import logging
import math
import os
import threading
from dataclasses import dataclass

import numpy as np

from core import state
from services.geo import M_PER_DEG_LAT, km_per_deg_lon, offset_latlon_m

# ── Tunables ──────────────────────────────────────────────────────────────
# Read once at import — these are physical/model constants, not per-call
# behaviour switches (unlike TRACK_SMOOTHER below, which tests flip live).

# White-noise-acceleration process spectral density (Stone-Soup calls this
# "noise_diff_coeff").  NOT squared again in the Q formula below — despite
# the name, this constant IS the coefficient the discretisation multiplies,
# matching ConstantVelocity(noise_diff_coeff) in Stone-Soup exactly (see
# TestStoneSoupOracle in tests/test_track_filter.py, which asserts this
# module's Q against Stone-Soup's own to 1e-6).
_KF_SIGMA_A_MS2 = float(os.getenv("TRACK_KF_SIGMA_A", "1.5"))

# ...but 1.5 is a STRAIGHT-FLIGHT number, and a turning aircraft is not in
# straight flight.  A coordinated turn at 250 m/s pulls 4.4 m/s^2 at 1 deg/s
# and 13.1 m/s^2 at the standard 3 deg/s — an order of magnitude more lateral
# acceleration than a spectral density of 1.5 m^2/s^3 admits.  Simulated
# against the shipped constants (R sigma 1200 m, gate 13.8), a converged
# filter entering a 3 deg/s turn breaches the chi-squared gate after 108-110
# degrees of turn at EVERY solve cadence (2.3 s, 4.6 s, 12 s), with the
# filter's velocity error already at 280-290 m/s — larger than the aircraft's
# own speed — by the time it does; at 1 deg/s the breach lands after 60-80
# degrees.  The live CV batch fit tells the same story from the other side: a
# chi2/dof of 1.8 median in straight flight against 17.6 in turns >= 2 deg/s.
#
# The old gate comment read that breach as an identity break and re-anchored.
# That premise is false for the manoeuvre band, and the re-anchor is
# expensive: it resets the velocity state to the solver's own (untrusted on
# ~54% of records) with sigma _KF_VEL_SIGMA_SOLVE_MS = 150 m/s, which is above
# dark_follow's DARK_FOLLOW_MAX_VEL_SIGMA_MS = 60, so the follow lane drops
# the target for its 30 s cooldown; and learned_velocity — what solver.py's
# _entry_dr_velocity dead-reckons the key decision with, and what
# aircraft_feed draws with — is wrong by up to the aircraft's own speed on the
# way there, which is what mints a second mn-dark-* key for one aircraft.
#
# So sigma_a is now ADAPTIVE rather than constant: each entry carries an EWMA
# of the normalised innovation d^2 (Mahalanobis, 2 dof, so a well-matched
# filter sits at ~2 and this one, with its deliberately inflated R, sits well
# below), and sigma_a ramps from the base toward _KF_SIGMA_A_MANOEUVRE_MS2 as
# that EWMA rises above _KF_MANOEUVRE_D2, saturating at twice the threshold.
# Straight flight never reaches the threshold, so the shipped CV model — and
# the Stone-Soup oracle that pins it — is bit-for-bit unchanged there.
_KF_SIGMA_A_MANOEUVRE_MS2 = float(os.getenv("TRACK_KF_SIGMA_A_MANOEUVRE", "800"))

# Trigger level for the normalised-innovation EWMA above.  d^2 is chi-squared
# with 2 dof for a correctly-specified filter (mean 2), and this filter's R is
# deliberately inflated well past the true measurement noise (see
# _KF_R_INFLATE), so straight flight runs an order of magnitude under this.
# The ramp is linear from here to 2x here, where sigma_a saturates at the
# manoeuvre value.
_KF_MANOEUVRE_D2 = float(os.getenv("TRACK_KF_MANOEUVRE_D2", "2.0"))

# Time constant of the innovation EWMA, in seconds — it sets BOTH how fast
# the filter enters manoeuvre mode (a couple of solves at any cadence, since
# one surprising update alone engages it fully) and how fast it leaves: once
# the surprises stop, engagement decays as exp(-t/tau), so it is under a tenth
# of full 2.3 tau (35 s) later and under a hundredth inside 70 s.  Note that
# "engagement near zero" and "sigma_a exactly back at base" are not the same
# claim — with a manoeuvre sigma ~500x the base, a residual engagement of 5%
# is still a sigma_a of ~40 — which is why the tests measure the decay on the
# engagement level rather than on sigma_a.
_KF_MANOEUVRE_TAU_S = float(os.getenv("TRACK_KF_MANOEUVRE_TAU_S", "15"))

# Base position-noise floor, applied to EVERY solve (variance = this
# squared) — see _measurement_R below for why this is now additive rather
# than a fallback-only default.  cov absent -> R is EXACTLY this, the cov=0
# limit of the same formula, not a separate code path.
_KF_DEFAULT_POS_SIGMA_M = float(os.getenv("TRACK_KF_POS_SIGMA_M", "1200"))

# cov_en_km2, when present, comes from the LM fit's Jacobian: a formal
# measurement-noise-propagation covariance, and ONLY that.  It has no way to
# see inter-node frame-time skew, association contamination between the
# single-node tracks that fed the solve, or pinned-altitude error — every
# multinode solve carries all three, and they are INDEPENDENT of the formal
# fit noise, not a scaled-up version of it.  Two rounds of staging
# measurement made the shape of the gap unmistakable:
#
#   2026-08-09, round 1: raw Jacobian sigma fed straight into the gate ->
#   only 1/19 multi-solve records actually smoothed (median displacement
#   0.000 km) — ordinary innovations looked like chi²>13.8 identity breaks,
#   so the filter re-anchored almost every solve.  End-to-end error
#   (gt_error_km against frozen ground truth) ran medians of ~3 km at n=3-4.
#
#   round 2 (flat 4x sigma inflation): much better (11/13 smoothed,
#   kf_pos_sigma_m median 407 m) but a live probe of formal pos_sigma_km on
#   real solves showed WHY a single multiplier cannot close this honestly:
#   formal sigma runs a median of 86 m (14 m .. 1.1 km across the
#   distribution) against a true error still measured at ~2.7 km median — a
#   ~31x ratio.  Multiplying the formal sigma by 31x to match the median
#   would nuke the very thing cov_en_km2 is for: a 1.1 km formal sigma and a
#   14 m formal sigma would both saturate to multi-kilometre R and become
#   indistinguishable, throwing away the relative weighting between a good
#   solve and a bad one.  A handful of solves still gate-re-anchored on
#   borderline d² ~14-15 for the same reason — the tail of the formal
#   distribution was still too tight relative to what a flat multiplier could
#   cover without wrecking the head.
#
# The honest model is additive, not multiplicative-only: true solve error is
# the formal (relatively-weighted, inflated) measurement noise term PLUS an
# INDEPENDENT unmodeled-error floor that every solve carries regardless of
# its own GDOP — frame-time skew, association contamination, altitude
# pinning do not shrink just because a solve's baselines happened to be
# well-conditioned.  R = (_KF_R_INFLATE**2) * cov_m2 + diag(_KF_DEFAULT_POS_SIGMA_M**2, ...):
# the base term carries the unmodeled-error floor (same role
# _KF_DEFAULT_POS_SIGMA_M always had as the no-cov fallback — this is why
# cov-absent is now literally the cov=0 limit of one formula, not a separate
# branch), the inflated formal term still carries meaningful relative
# weighting on top of it.  With the defaults above: formal sigma 86 m ->
# R sigma = sqrt((4*86)² + 1200²) ≈ 1248 m; formal sigma 1.1 km -> R sigma =
# sqrt((4*1100)² + 1200²) ≈ 4561 m — a good solve and a bad one four
# quality-orders apart in the formal fit still land roughly 3.6x apart in R,
# not saturated to the same clamp-capped number.
#
# _KF_R_INFLATE is env-tunable so it can be retuned without a deploy as the
# sensor model — frame-time sync, association, altitude pinning — improves
# and the formal covariance becomes a less severe underestimate of the
# INDEPENDENT (not additive-floor) portion of the error it's meant to weight.
_KF_R_INFLATE = float(os.getenv("TRACK_KF_R_INFLATE", "4.0"))

# Clamp band for the final per-solve position sigma (after the additive
# composition above) — a degenerate Jacobian (near-parallel baselines) can
# still produce a covariance that is absurdly tight or absurdly loose;
# neither should be taken at face value.  The floor is now mostly inert by
# construction: with the additive base term always contributing
# _KF_DEFAULT_POS_SIGMA_M (1200 m) on its own, R sigma cannot fall below that
# regardless of cov, and 1200 > 500 already — the floor only still matters if
# _KF_DEFAULT_POS_SIGMA_M itself is ever env-tuned below it.
_KF_MIN_POS_SIGMA_M = 500.0
_KF_MAX_POS_SIGMA_M = 8000.0

# Velocity: ADS-B ground-speed/track is a genuine MEASUREMENT, independent
# of the solver — trusted at sigma=5 m/s both at init and as an ongoing
# Kalman update on every solve that has a live entry.
#
# Solved vel_east/vel_north (the CV fit) is NOT a measurement.  It used to be
# fed back in as one (sigma=25) every solve for dark targets, on the theory
# that it was a reasonable independent estimate.  2026-08-09 staging measured
# otherwise: solved-velocity vector error vs frozen gt_speed_ms/gt_heading_deg
# (n=93 matched solves) ran a median of 127 m/s (p90 336, max 512) — 84% of
# solves exceeded the sigma=25 m/s it was being trusted at.  Feeding that
# biased a velocity estimate into the predict step every solve dragged the
# position estimate AWAY from the measurements that actually deserved trust:
# smoothed positions on moved records got WORSE than raw (median error
# 2.01 -> 2.46 km).  Solved velocity is now used ONLY as a one-shot
# INITIALIZATION PRIOR (_init_entry, dark-target branch) — outweighed by the
# very next position update and never re-applied, so its bias is harmless
# there.  For a dark target (the only case this fires; ADS-B always wins
# when live) the filter's own velocity state is otherwise learned purely
# from the position sequence — which is what a Kalman filter is for.
_KF_VEL_SIGMA_ADSB_MS = 5.0
# Init-prior sigma only — NOT a measurement sigma, see above.  Widened from
# the old 25 (which had been sized as if this were a trustworthy measurement)
# to an honest 150: a prior this loose still beats zero information, but it
# is outweighed by real position evidence within one or two solves instead of
# anchoring the filter to a biased velocity for its whole early life.
_KF_VEL_SIGMA_SOLVE_MS = float(os.getenv("TRACK_KF_VEL_SIGMA_SOLVE", "150"))

# Mirrors solver.py's _MN_DR_MAX_AGE_S: a gap this long is not a continuous
# track to bridge, it is a new one to start.
_KF_MAX_GAP_S = 160.0

# 99.9th percentile of chi-squared, 2 degrees of freedom.  An innovation this
# far outside the filter's own predicted uncertainty is either a manoeuvre the
# process noise did not admit or a measurement that does not belong to the
# track being carried at all — and those two need different answers, so the
# gate is applied TWICE (see _smooth_kf): once against the current sigma_a,
# and, if that breaches, once more against a predict redone with the manoeuvre
# sigma_a.  Surviving the second attempt means the innovation was a turn and
# the filter simply had too little process noise; breaching it as well means
# the measurement really is somewhere the aircraft could not have got to under
# any plausible acceleration, and re-anchoring is right.  The VALUE stays 13.8
# for both attempts on purpose: a 10 km jump breaches either way.
_KF_GATE_CHI2 = 13.8

# Dict-level TTL, mirrors solver.py's _MN_HISTORY_TTL_S / _sweep_mn_history —
# same shape of problem (one entry per distinct key for the process lifetime
# unless swept), same fix.
_KF_TTL_S = 600.0


# ── Filter state ──────────────────────────────────────────────────────────


@dataclass
class _TrackKF:
    """One track key's filter state.

    The ENU frame is anchored at ref_lat/ref_lon — the position of the FIRST
    solve this key ever saw (or the position of the solve that most recently
    re-anchored it, on a gap or a gate breach).  The anchor never moves for
    the life of the entry: every subsequent solve is converted to an offset
    from this one fixed point, so nothing here ever needs to compare two
    different ENU frames against each other.
    """

    ref_lat: float
    ref_lon: float
    x: np.ndarray  # state [e_m, ve_ms, n_m, vn_ms] — THIS ORDER, see module doc
    P: np.ndarray  # 4x4 covariance, same ordering
    last_ts_s: float
    # Manoeuvre engagement, 0.0 (straight flight, base process noise) to 1.0
    # (fully inflated).  Raised by a surprising update, then HELD and decayed
    # on a clock rather than re-derived from the innovations — see
    # _update_manoeuvre for why a plain innovation EWMA cannot work here.
    # 0.0 on a fresh entry is the honest "no evidence of a manoeuvre yet"
    # start, and it is deliberately not carried across a re-anchor: a
    # re-anchor means the track identity is in question, so its innovation
    # history is too.
    manoeuvre: float = 0.0


# key -> _TrackKF.  Same shape as solver.py's _MN_POS_HISTORY: one entry per
# multinode track key, swept opportunistically rather than expired eagerly.
_KF_TRACKS: dict[str, _TrackKF] = {}
_KF_LOCK = threading.Lock()
_kf_last_sweep = 0.0

# Since-boot outcome counters for the chi-squared gate, both guarded by
# _KF_LOCK (they are only ever touched inside _smooth_kf's critical section).
# Module-level rather than core.state counters because nothing else in this
# module touches state's counter block and there is no lock-order story to
# get wrong here; routes/test.py reads them through filter_stats().
#
# _kf_reanchors counts gate breaches that survived the manoeuvre retry and
# therefore re-anchored — the honest identity-break tally.  _kf_manoeuvre_
# rescues counts the ones the retry saved, which before this existed were
# indistinguishable from the first group and were the dominant half of it:
# every turn past ~60-110 degrees produced one.  A rescue rate that collapses
# to zero means the adaptive path has stopped firing; a re-anchor rate that
# climbs back to the old level means the manoeuvre sigma is too small for the
# turns being flown.
_kf_reanchors = 0
_kf_manoeuvre_rescues = 0

# H matrices are fixed by the state ordering, never rebuilt per call.
_H_POS = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]])
_H_VEL = np.array([[0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])


def reset() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    global _kf_last_sweep, _kf_reanchors, _kf_manoeuvre_rescues
    with _KF_LOCK:
        _KF_TRACKS.clear()
        _kf_last_sweep = 0.0
        _kf_reanchors = 0
        _kf_manoeuvre_rescues = 0


def filter_stats() -> dict:
    """Since-boot chi-squared-gate outcomes plus a live manoeuvre gauge.

    Surfaced on /api/test/solver-stats under "display_filter" (see
    routes/test.py's _solver_window_stats) because the two counters are the
    only way to tell a turn from an identity break from outside this module —
    the per-solve payload carries neither, and both look identical downstream
    (a raw, unsmoothed result).

    reanchors / manoeuvre_rescues are cumulative, matching that endpoint's
    since-boot convention for counters.  manoeuvre_active is a GAUGE: how many
    live entries are currently running an inflated sigma_a, out of `tracks`.
    """
    with _KF_LOCK:
        active = sum(1 for e in _KF_TRACKS.values() if _entry_sigma_a(e) > _KF_SIGMA_A_MS2)
        return {
            "reanchors": _kf_reanchors,
            "manoeuvre_rescues": _kf_manoeuvre_rescues,
            "manoeuvre_active": active,
            "tracks": len(_KF_TRACKS),
        }


def drop_key(track_key: str) -> None:
    """Forget a track key's filter state.

    Called from solver.py's supersession block, right beside the
    _MN_POS_HISTORY.pop for the same old_key: when a re-solved aircraft's
    fragments merge under a different key, the superseded key's filter would
    otherwise sit in _KF_TRACKS until the TTL sweep collects it — harmless,
    but there is no reason to keep a dead track's velocity estimate around.
    """
    with _KF_LOCK:
        _KF_TRACKS.pop(track_key, None)


def learned_velocity(track_key: str) -> tuple[float, float, float, float] | None:
    """(v_east_ms, v_north_ms, vel_sigma_ms, last_update_ts_s) for this key's
    filter entry, or None when the key has no live filter state (never
    smoothed, TTL-swept, or TRACK_SMOOTHER != kf so the KF never ran).

    Read-only: exposes the filter's velocity state for display-side
    dead-reckoning (services/aircraft_feed.py) without any way to mutate the
    filter.  _KF_LOCK is a leaf lock (see module docstring), so taking it here
    keeps the established solver.py -> track_filter order; callers must not
    hold it already.

    The sqrt is clamped for the same reason _smooth_kf's kf_pos_sigma_m one
    is: this is a read-only accessor on a hot display path with two callers
    that each lose real work when it throws — solver.py's
    multinode_key_decision (drops that solve) and aircraft_feed's
    multinode_to_aircraft (drops the whole broadcast) — so a pathological
    filter state must degrade to "sigma 0", never to a ValueError.  An
    accessor is the wrong place to discover a covariance is sick; the
    invariant is enforced upstream in _measurement_R and _kf_correct.
    """
    with _KF_LOCK:
        entry = _KF_TRACKS.get(track_key)
        if entry is None:
            return None
        vel_sigma = math.sqrt(max(0.0, 0.5 * (entry.P[1, 1] + entry.P[3, 3])))
        return float(entry.x[1]), float(entry.x[3]), float(vel_sigma), float(entry.last_ts_s)


def _kf_reanchor() -> None:
    """Count a gate breach that the manoeuvre retry could not explain.
    Caller holds _KF_LOCK."""
    global _kf_reanchors
    _kf_reanchors += 1


def _kf_manoeuvre_rescue() -> None:
    """Count a gate breach the manoeuvre retry rescued.  Caller holds
    _KF_LOCK."""
    global _kf_manoeuvre_rescues
    _kf_manoeuvre_rescues += 1


def _sweep(now_s: float) -> None:
    """Drop keys whose last update is stale.  Caller holds _KF_LOCK.

    Copies solver.py's _sweep_mn_history pattern: cheap early-out most calls,
    real sweep at most once per _KF_TTL_S/10.
    """
    global _kf_last_sweep
    if now_s - _kf_last_sweep < _KF_TTL_S / 10:
        return
    _kf_last_sweep = now_s
    for k in [k for k, e in _KF_TRACKS.items() if now_s - e.last_ts_s > _KF_TTL_S]:
        del _KF_TRACKS[k]


# ── ENU conversion ────────────────────────────────────────────────────────


def _enu_offset_m(ref_lat: float, ref_lon: float, lat: float, lon: float) -> tuple[float, float]:
    """(east_m, north_m) of (lat, lon) relative to (ref_lat, ref_lon).

    The exact algebraic inverse of services.geo.offset_latlon_m.  That
    function is:

        offset_latlon_m(lat, lon, east_m, north_m) =
            (lat + north_m / M_PER_DEG_LAT,
             lon + east_m / (km_per_deg_lon(lat) * 1000))

    — note it evaluates km_per_deg_lon at the BASE point's latitude (the
    "lat" argument), not the offset result's latitude.  Read backwards from a
    (ref_lat, ref_lon) base point, that inverts cleanly with no iteration:

        north_m = (lat - ref_lat) * M_PER_DEG_LAT
        east_m  = (lon - ref_lon) * km_per_deg_lon(ref_lat) * 1000

    using ref_lat (not lat) in km_per_deg_lon, matching the forward call's
    convention exactly.  Round-trip through offset_latlon_m is therefore
    exact to float precision — see TestEnuRoundTrip.
    """
    north_m = (lat - ref_lat) * M_PER_DEG_LAT
    east_m = (lon - ref_lon) * km_per_deg_lon(ref_lat) * 1000.0
    return east_m, north_m


# ── Kalman math ───────────────────────────────────────────────────────────


def _entry_sigma_a(entry: "_TrackKF") -> float:
    """Process-noise spectral density for this entry's NEXT predict.

    A linear blend from the straight-flight base to
    _KF_SIGMA_A_MANOEUVRE_MS2 on the entry's manoeuvre engagement level.  An
    entry that has never been surprised sits at engagement 0.0 and therefore
    at exactly the shipped CV model — which is what keeps straight flight, and
    the Stone-Soup oracle that pins it, bit-for-bit unchanged.  A blend rather
    than a step because the detector is noisy at a 2.3 s cadence and a step
    would chatter between two very different filters on the borderline; a
    blend just makes a marginally surprising track marginally less rigid.

    Configuring the manoeuvre sigma at or below the base disables the whole
    adaptive path (the ramp can then only ever return the base), which is what
    the tests use to reproduce the pre-adaptive behaviour.
    """
    if _KF_SIGMA_A_MANOEUVRE_MS2 <= _KF_SIGMA_A_MS2 or _KF_MANOEUVRE_D2 <= 0:
        return _KF_SIGMA_A_MS2
    return _KF_SIGMA_A_MS2 + entry.manoeuvre * (_KF_SIGMA_A_MANOEUVRE_MS2 - _KF_SIGMA_A_MS2)


def _update_manoeuvre(entry: "_TrackKF", dt: float, d2: float) -> None:
    """Re-arm or decay this entry's manoeuvre engagement after an update.

    RE-ARM on surprise: an update whose normalised innovation exceeds
    _KF_MANOEUVRE_D2 raises the engagement immediately, linearly to full at
    twice the threshold.  DECAY on the clock: exp(-dt/_KF_MANOEUVRE_TAU_S),
    so an entry that stops being surprised is under a tenth of full engagement
    2.3 tau (35 s) later and under a hundredth inside 70 s.

    Why a HELD, clock-decayed level rather than the obvious EWMA of d^2: the
    inflated Q suppresses the very statistic that raised it.  Once sigma_a is
    up, S widens, d^2 collapses to a fraction of its pre-inflation value, and
    an EWMA-driven sigma_a would disengage a second or two into the turn and
    re-engage only after the velocity error had rebuilt to hundreds of m/s —
    measured, at 3 deg/s and a 4.6 s cadence: an EWMA-driven version left the
    peak in-turn velocity error at ~330 m/s, barely better than the 353 m/s
    of the fixed-sigma_a filter, while the held version lands at ~155 m/s.
    Holding is also the physically honest reading: aircraft turns last tens of
    seconds, not one solve interval, so "was surprised recently" is a much
    better predictor of "is manoeuvring now" than "is surprised right now" —
    and at these R sigmas (1200 m floor) a filter that IS keeping up with a
    3 deg/s turn only sees ~140 m of per-step innovation, which is not
    detectable against the measurement noise at all.  The detector can only
    ever fire on the accumulated error, so the response has to outlive it.
    """
    decayed = entry.manoeuvre * math.exp(-dt / _KF_MANOEUVRE_TAU_S) if _KF_MANOEUVRE_TAU_S > 0 else 0.0
    trigger = 0.0
    if _KF_MANOEUVRE_D2 > 0 and d2 > _KF_MANOEUVRE_D2:
        trigger = min(1.0, (d2 - _KF_MANOEUVRE_D2) / _KF_MANOEUVRE_D2)
    entry.manoeuvre = max(decayed, trigger)


def _f_q(dt: float, sigma_a: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """State-transition F and process-noise Q for a CV model over dt seconds.

    State order [e, ve, n, vn]: the east axis occupies indices (0, 1), the
    north axis (2, 3), so the per-axis 2x2 blocks below land on those index
    pairs directly with no permutation needed.

    F is the standard CV transition, per axis [[1, dt], [0, 1]].

    Q is the standard discrete white-noise-acceleration model, per axis
    sigma_a * [[dt^3/3, dt^2/2], [dt^2/2, dt]] — EXACTLY this discretisation,
    unsquared sigma_a, matching Stone-Soup's ConstantVelocity(noise_diff_coeff)
    to 1e-6 (see TestStoneSoupOracle).

    sigma_a defaults to the straight-flight base _KF_SIGMA_A_MS2; callers pass
    the entry's adaptive value (_entry_sigma_a) so Q is rebuilt per predict
    rather than frozen at import.
    """
    f = np.eye(4)
    f[0, 1] = dt
    f[2, 3] = dt

    q_block = (_KF_SIGMA_A_MS2 if sigma_a is None else sigma_a) * np.array(
        [
            [dt**3 / 3.0, dt**2 / 2.0],
            [dt**2 / 2.0, dt],
        ]
    )
    q = np.zeros((4, 4))
    q[0:2, 0:2] = q_block  # e, ve
    q[2:4, 2:4] = q_block  # n, vn
    return f, q


def _kf_correct(
    x: np.ndarray, P: np.ndarray, z: np.ndarray, h: np.ndarray, r: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """One linear Kalman measurement update.  Returns (x, P, innovation, S) —
    callers that need to gate on the innovation (the position update below)
    get it for free instead of recomputing Hx twice.

    The covariance update is the JOSEPH stabilized form, and must stay that
    way — the cheaper standard form (I - KH) @ P is algebraically identical
    and numerically is not.  Joseph is a SUM of two congruence transforms of
    PSD matrices ((I-KH) P (I-KH)^T and K R K^T), so it preserves
    positive-semidefiniteness for ANY gain K given PSD P and R; the standard
    form is a DIFFERENCE of two nearly-equal matrices and loses PSD to
    roundoff on an ill-conditioned update.  2026-08-26 droplet: that loss put
    a negative diagonal on the position covariance 6 times in ~6 h, each one
    a math domain error out of _smooth_kf's sqrt below.
    """
    y = z - h @ x
    s = h @ P @ h.T + r
    k = P @ h.T @ np.linalg.inv(s)
    x_new = x + k @ y
    i_kh = np.eye(P.shape[0]) - k @ h
    p_new = i_kh @ P @ i_kh.T + k @ r @ k.T
    p_new = (p_new + p_new.T) / 2.0  # symmetrize — guards against drift from repeated float ops
    return x_new, p_new, y, s


def _measurement_R(result: dict) -> np.ndarray:
    """Per-solve position measurement covariance, in m^2.

    ADDITIVE composition (see the _KF_R_INFLATE comment above for the
    staging measurements this is built from): the formal LM-fit covariance,
    cov_en_km2, and the unmodeled-error floor are INDEPENDENT noise sources,
    not one scaled version of the other, so they add rather than one
    replacing the other:

        cov present and PSD:   R = (_KF_R_INFLATE**2) * cov_m2 + base
        cov absent/degenerate: R = base                          (cov=0 limit)

    "Degenerate" now includes a cov that is not positive-semidefinite, which
    an ill-conditioned solve really does produce — see the determinant check
    in the body for why that has to be rejected rather than passed through.

    where base = diag(_KF_DEFAULT_POS_SIGMA_M**2, _KF_DEFAULT_POS_SIGMA_M**2)
    is added UNCONDITIONALLY — the no-cov fallback is not a separate branch,
    it is exactly this same formula evaluated at cov_m2 = 0.  This is what
    lets a well-conditioned solve (small cov_m2) and a poorly-conditioned one
    (large cov_m2) still land at meaningfully different R after inflation,
    instead of a flat multiplier saturating both to the same multi-kilometre
    number once the unmodeled-error floor dominates either way.

    Either way, clamps the implied sigma to [_KF_MIN_POS_SIGMA_M,
    _KF_MAX_POS_SIGMA_M] by uniform scaling — this preserves whatever
    correlation the solver reported instead of stomping it with a fresh
    diagonal.  The floor is mostly inert now (see that constant's comment);
    the cap still matters for a badly-conditioned Jacobian.
    """
    base = np.diag([_KF_DEFAULT_POS_SIGMA_M**2, _KF_DEFAULT_POS_SIGMA_M**2])
    r = base
    cov = result.get("cov_en_km2")
    if cov is not None:
        arr = np.asarray(cov, dtype=float) * 1e6  # km^2 -> m^2
        if arr.shape == (2, 2) and np.all(np.isfinite(arr)):
            arr = (arr + arr.T) / 2.0
            # PSD, not just positive-diagonal.  A 2x2 symmetric matrix is PSD
            # iff both diagonals are >= 0 AND the determinant is >= 0; the
            # determinant is the half that was missing, and it is not a
            # theoretical gap.  cov_en_km2 is the top-left 2x2 block of
            # s2 * inv(JtJ) for the solver's 5-state fit, and the solver
            # falls back to pinv only on an outright LinAlgError — an
            # ill-conditioned-but-not-singular JtJ (near-parallel baselines,
            # the same degenerate tail that puts the formal sigma's p99 at
            # 3.8e6 km) inverts to numerical garbage that is INDEFINITE while
            # still having both diagonals positive, so it passed both this
            # check and the solver's own.
            #
            # An indefinite R is not survivable downstream.  _kf_correct's
            # Joseph form preserves positive-semidefiniteness for any gain,
            # but only GIVEN PSD P and R — its K R K^T term inherits R's
            # negative eigenvalue directly — and _init_entry seeds P's
            # position block from this matrix, so a sick R poisons the filter
            # at birth as well as on every update.  Once P's velocity
            # diagonals go negative, learned_velocity's sqrt raises: on the
            # test droplet that was 91 tracebacks in 40 minutes, each one
            # costing either a solve or an entire feed broadcast.
            #
            # Rejecting is the honest response rather than repairing by
            # eigenvalue clipping: a covariance this degenerate carries no
            # trustworthy relative weighting to preserve, and "R = base" is
            # already this function's documented answer for a degenerate cov.
            det = arr[0, 0] * arr[1, 1] - arr[0, 1] * arr[1, 0]
            if arr[0, 0] > 0 and arr[1, 1] > 0 and det >= 0:
                r = (_KF_R_INFLATE**2) * arr + base  # independent noise sources -> variances add

    s = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))
    if s < _KF_MIN_POS_SIGMA_M:
        r = r * (_KF_MIN_POS_SIGMA_M / s) ** 2
    elif s > _KF_MAX_POS_SIGMA_M:
        r = r * (_KF_MAX_POS_SIGMA_M / s) ** 2
    return r


def _adsb_velocity(adsb_hex: str | None) -> tuple[bool, float, float]:
    """(has_live_entry, v_east_ms, v_north_ms) from core.state.adsb_aircraft.

    Mirrors _ewma_smooth_track's ADS-B branch exactly: gs in knots * 0.514444
    -> m/s, track in degrees (0=N, 90=E) -> (sin, cos) east/north components.
    """
    adsb = state.adsb_aircraft.get(adsb_hex) if adsb_hex else None
    if not adsb:
        return False, 0.0, 0.0
    gs_knots = float(adsb.get("gs", 0) or 0)
    track_deg = float(adsb.get("track", 0) or 0)
    v_ms = gs_knots * 0.514444
    v_east = v_ms * math.sin(math.radians(track_deg))
    v_north = v_ms * math.cos(math.radians(track_deg))
    return True, v_east, v_north


def _init_entry(
    r_lat: float, r_lon: float, ts_s: float, result: dict, has_adsb_vel: bool, v0e: float, v0n: float, r_pos: np.ndarray
) -> _TrackKF:
    """Fresh filter state anchored at this solve.

    Velocity seed prefers live ADS-B ground-speed/track (a real measurement,
    tight prior sigma=5) over the solved CV fit (an honest, loose ONE-SHOT
    prior, sigma=150 — never re-applied as a measurement, see the
    _KF_VEL_SIGMA_SOLVE_MS comment for the staging finding that made solved
    velocity untrustworthy as anything more than a starting guess) over zero
    (same loose prior sigma — with neither source, zero is simply the
    least-wrong starting guess).  Position uncertainty is seeded straight
    from this solve's own R, preserving whatever correlation it carries.
    """
    if has_adsb_vel:
        ve0, vn0 = v0e, v0n
        p_vel = _KF_VEL_SIGMA_ADSB_MS**2
    else:
        ve0 = float(result.get("vel_east") or 0.0)
        vn0 = float(result.get("vel_north") or 0.0)
        p_vel = _KF_VEL_SIGMA_SOLVE_MS**2

    x = np.array([0.0, ve0, 0.0, vn0])
    p = np.zeros((4, 4))
    p[0, 0], p[0, 2] = r_pos[0, 0], r_pos[0, 1]
    p[2, 0], p[2, 2] = r_pos[1, 0], r_pos[1, 1]
    p[1, 1] = p_vel
    p[3, 3] = p_vel
    return _TrackKF(ref_lat=r_lat, ref_lon=r_lon, x=x, P=p, last_ts_s=ts_s)


def _predict_update(
    entry: _TrackKF, dt: float, z: np.ndarray, r_pos: np.ndarray, sigma_a: float
) -> tuple[np.ndarray, np.ndarray, float]:
    """One predict+update pass at a given process-noise density.

    Returns (x, P, d2).  Split out of _smooth_kf so the manoeuvre retry can
    run the SAME arithmetic again with a bigger Q instead of a near-copy of
    it — the two attempts must not be able to drift apart.  Reads entry.x /
    entry.P and writes nothing: the caller decides which attempt (if either)
    becomes the new state.
    """
    f, q = _f_q(dt, sigma_a)
    x_pred = f @ entry.x
    p_pred = f @ entry.P @ f.T + q
    # Symmetrize here too, not only in _kf_correct: F P F^T is symmetric
    # in exact arithmetic but drifts by roundoff, and this filter composes
    # predict and update thousands of times over a track's life.
    p_pred = (p_pred + p_pred.T) / 2.0

    x_upd, p_upd, y, s_cov = _kf_correct(x_pred, p_pred, z, _H_POS, r_pos)
    d2 = float(y @ np.linalg.solve(s_cov, y))
    return x_upd, p_upd, d2


def _smooth_kf(result: dict, track_key: str, adsb_hex: str | None) -> dict:
    """The "kf" branch of smooth_solve — see that function's docstring for
    the mode dispatch this is reached through."""
    r_lat = result["lat"]
    r_lon = result["lon"]
    ts_s = result.get("timestamp_ms", 0) / 1000.0

    has_adsb_vel, v0e, v0n = _adsb_velocity(adsb_hex)
    r_pos = _measurement_R(result)

    with _KF_LOCK:
        _sweep(ts_s)
        entry = _KF_TRACKS.get(track_key)

        if entry is None:
            _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
            return result  # first solve for this key — nothing to smooth against yet

        dt = ts_s - entry.last_ts_s
        if dt <= 0:
            # Known/intentional, not a gap in the model — see the module
            # docstring.  In production this fires mostly as an association
            # burst: several solves for the same key sharing one
            # measurement_ts_ms because overlapping single-node association
            # rounds re-solved the same epoch.  Fusing them would
            # double-count the same underlying measurements rather than add
            # independent evidence, so this stays a no-op, same as the EWMA.
            return result

        if dt > _KF_MAX_GAP_S:
            # Too long a gap to bridge with any confidence — start over here,
            # same threshold and same rationale as solver.py's _MN_DR_MAX_AGE_S.
            _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
            return result

        z_e, z_n = _enu_offset_m(entry.ref_lat, entry.ref_lon, r_lat, r_lon)
        z = np.array([z_e, z_n])

        # sigma_a is per-predict, from this entry's own innovation history:
        # base in straight flight, ramped toward the manoeuvre value while
        # the filter is being consistently surprised.  See _entry_sigma_a.
        sigma_a = _entry_sigma_a(entry)
        x_upd, p_upd, d2 = _predict_update(entry, dt, z, r_pos, sigma_a)
        d2_observed = d2

        if d2 > _KF_GATE_CHI2:
            # A breach is NOT self-evidently an identity break — see the
            # _KF_GATE_CHI2 comment for the simulation and the live chi2/dof
            # measurement that killed that premise.  Before writing the track
            # off, redo the same predict+update once with the manoeuvre
            # process noise: if the innovation is explained by an
            # acceleration this aircraft could actually have pulled, the
            # aircraft turned and the filter merely had too little Q.
            x_try, p_try, d2_try = _predict_update(entry, dt, z, r_pos, _KF_SIGMA_A_MANOEUVRE_MS2)
            if d2_try <= _KF_GATE_CHI2 and _KF_SIGMA_A_MANOEUVRE_MS2 > _KF_SIGMA_A_MS2:
                _kf_manoeuvre_rescue()
                x_upd, p_upd = x_try, p_try
                # The EWMA records the ORIGINAL d2, not the retry's: the
                # entry needs to remember how surprising this update was
                # against the Q it had, which is what keeps sigma_a inflated
                # for the rest of the turn instead of collapsing back to base
                # the moment the retry succeeds.
            else:
                # Beyond any plausible acceleration too — the measurement
                # does not belong to the track this filter has been carrying
                # (a merge, a re-association, a genuinely different
                # aircraft).  Re-anchor rather than smear the estimate across
                # two aircraft.
                _kf_reanchor()
                _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
                return result

        # Velocity measurement update: ADS-B ONLY.  Solved vel_east/vel_north
        # is deliberately NOT applied here — see the _KF_VEL_SIGMA_SOLVE_MS
        # comment above for the 2026-08-09 staging finding (median vector
        # error 127 m/s against a sigma=25 trust level) that made feeding it
        # back in every solve actively harmful: a biased velocity state drags
        # predict away from the position measurements that deserve the trust.
        # Solved velocity still seeds the INIT prior (_init_entry) — a
        # one-shot, loose-sigma guess that the next position update quickly
        # outweighs is harmless in a way a recurring measurement update is
        # not.  For a dark target with no live ADS-B, the velocity state is
        # otherwise learned purely from the position sequence via the
        # predict/update cycle above — no separate update needed or wanted.
        if has_adsb_vel:
            zv = np.array([v0e, v0n])
            r_vel = np.eye(2) * (_KF_VEL_SIGMA_ADSB_MS**2)
            x_upd, p_upd, _, _ = _kf_correct(x_upd, p_upd, zv, _H_VEL, r_vel)

        entry.x = x_upd
        entry.P = p_upd
        entry.last_ts_s = ts_s
        _update_manoeuvre(entry, dt, d2_observed)

        lat_s, lon_s = offset_latlon_m(entry.ref_lat, entry.ref_lon, east_m=float(x_upd[0]), north_m=float(x_upd[2]))
        smoothed = dict(result)
        smoothed["lat"] = round(lat_s, 6)
        smoothed["lon"] = round(lon_s, 6)
        smoothed["smoother"] = "kf"
        # Clamped: a sqrt of a float subtraction result must never be able to
        # throw on this hot path — see _kf_correct for the roundoff mode this
        # is the second line of defence against.
        smoothed["kf_pos_sigma_m"] = round(math.sqrt(max(0.0, 0.5 * (p_upd[0, 0] + p_upd[2, 2]))), 1)
        logging.debug(
            "KF: key=%s dt=%.1f raw=(%.4f,%.4f) -> smooth=(%.4f,%.4f) sigma=%.1fm",
            track_key,
            dt,
            r_lat,
            r_lon,
            lat_s,
            lon_s,
            smoothed["kf_pos_sigma_m"],
        )
        return smoothed


# ── Public API ────────────────────────────────────────────────────────────


def smooth_solve(result: dict, track_key: str, adsb_hex: str | None, *, ewma_fn=None) -> dict:
    """Smooth a multinode solver result for display.  Called under
    solver.py's _MN_TRACKS_LOCK — see this module's docstring for the lock
    order this implies.

    TRACK_SMOOTHER selects the strategy, read fresh on every call (tests flip
    it with monkeypatch and expect the next call to see the change):
      "kf"   (default, and the fallback for any unrecognised value) — the
             Kalman filter in this module.
      "ewma" — delegate to ewma_fn (solver.py's legacy _ewma_smooth_track),
               the pre-KF behaviour kept as an escape hatch.
      "off"  — return result unchanged.
    """
    mode = os.getenv("TRACK_SMOOTHER", "kf")
    mode = (mode or "kf").strip().lower()
    if mode not in ("kf", "ewma", "off"):
        mode = "kf"

    if mode == "off":
        return result
    if mode == "ewma":
        return ewma_fn(result, track_key, adsb_hex) if ewma_fn else result
    return _smooth_kf(result, track_key, adsb_hex)
