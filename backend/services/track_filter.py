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

Two further env flags, also read PER CALL and both defaulting to today's
behaviour, change how much a solve is trusted and what happens when one is
implausible.  Both exist because of the same 2026-09-05 measurement of the
dark lane on the test droplet: feed entries more than 5 km from any aircraft
("ghosts") are 10-14% of dark entries, and 58% of those ghost frames sit on
keys that had ALREADY accumulated four-or-more-node solves — what moved them
was a two- or three-node solve joining by proximity.  Those joins are wrong
far more often than their node count suggests (n=3: 25% land >3 km off; n=2:
36-62%; n=4: 5%; n>=5: 2%), and nothing at solve time separates a good one
from a bad one: a three-node free-altitude solve is exactly determined (six
measurements, six unknowns), so its rms is ~0 either way.  What CAN separate
them is the track itself — see TRACK_KF_OUTLIER_MODE below.

    TRACK_KF_R_SOURCE:
      "flat"        (default) — the additive base term in _measurement_R is
                    the flat _KF_DEFAULT_POS_SIGMA_M for every solve.
      "uncertainty" — the base term is instead the calibrated per-solve sigma
                    from services/solve_uncertainty.solve_sigma_m, which knows
                    the node count (650/210/180 m floors at n=2/3/>=4).  The
                    filter then trusts a solve exactly as much as the map's
                    uncertainty disc claims to, instead of pricing an n=2 and
                    an n=5 solve identically.

    TRACK_KF_OUTLIER_MODE:
      "reanchor"    (default) — a gate breach re-anchors the whole filter at
                    the outlier, as it always has.
      "hold"        — for DARK keys only, an established track outvotes a
                    single implausible solve: see the gate-breach block in
                    _smooth_kf and the _KF_HOLD_* constants.

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
# far outside the filter's own predicted uncertainty means the measurement
# does not belong to the track being carried — not that the aircraft turned
# hard.  Smoothing across a jump that size would be actively wrong.
_KF_GATE_CHI2 = 13.8

# TRACK_KF_OUTLIER_MODE="hold" thresholds.  A hold says "the track is right
# and this solve is wrong", which is only ever a defensible claim when the
# track has earned it and the disagreement is too large to be flight.
#
# _KF_HOLD_MIN_UPDATES — position updates accepted since the entry was
# anchored.  A key with two or more of them has a velocity state built from
# real evidence and a position good to a few hundred metres; a key with none
# has only its init prior and no standing to overrule anything, so a breach
# there re-anchors exactly as it always did.
#
# _KF_HOLD_MIN_INNOV_M — the innovation magnitude, in metres, below which a
# breach is never held whatever the chi² says.  The gate is scaled by the
# filter's OWN covariance and the KF velocity sigma is known to be optimistic
# (it is learned from a position sequence that shares the solver's biases), so
# a well-converged entry can call a perfectly ordinary 800 m correction a
# breach.  1500 m is above that noise and still far below the thing this is
# for: the measured bad-join population sits at ~4 km, while a genuine
# manoeuvre over one solve interval cannot reach it (250 m/s x 5 s = 1.25 km).
#
# _KF_HOLD_MAX_STREAK — consecutive held breaches before the filter concedes.
# Holding is a bet that the outlier is a one-off; when the new position keeps
# being confirmed, the identity really did change (a merge, a re-association)
# and re-anchoring is right.  Three keeps the wrong-bet cost at two solve
# intervals (~10 s, inside the 60 s entry expiry) while still absorbing the
# isolated bad join that motivates the mode.
_KF_HOLD_MIN_UPDATES = int(os.getenv("TRACK_KF_HOLD_MIN_UPDATES", "2"))
_KF_HOLD_MIN_INNOV_M = float(os.getenv("TRACK_KF_HOLD_MIN_INNOV_M", "1500"))
_KF_HOLD_MAX_STREAK = int(os.getenv("TRACK_KF_HOLD_MAX_STREAK", "3"))

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

    n_updates/breach_streak exist for TRACK_KF_OUTLIER_MODE="hold" (see the
    _KF_HOLD_* constants): n_updates is how many position updates this
    anchoring has ACCEPTED, which is what makes a track established enough to
    overrule a solve, and breach_streak is how many consecutive gate breaches
    have been held since the last accepted update.  Both are reset by
    construction whenever _init_entry runs, which is the whole point — a
    re-anchored entry has no accumulated evidence and no streak.
    """

    ref_lat: float
    ref_lon: float
    x: np.ndarray  # state [e_m, ve_ms, n_m, vn_ms] — THIS ORDER, see module doc
    P: np.ndarray  # 4x4 covariance, same ordering
    last_ts_s: float
    n_updates: int = 0
    breach_streak: int = 0


# key -> _TrackKF.  Same shape as solver.py's _MN_POS_HISTORY: one entry per
# multinode track key, swept opportunistically rather than expired eagerly.
_KF_TRACKS: dict[str, _TrackKF] = {}
_KF_LOCK = threading.Lock()
_kf_last_sweep = 0.0

# H matrices are fixed by the state ordering, never rebuilt per call.
_H_POS = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]])
_H_VEL = np.array([[0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])


def reset() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    global _kf_last_sweep
    with _KF_LOCK:
        _KF_TRACKS.clear()
        _kf_last_sweep = 0.0


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


def _r_source() -> str:
    """TRACK_KF_R_SOURCE, read per call (tests flip it with monkeypatch and
    expect the next call to see it), unrecognised values falling back to the
    default — same contract as TRACK_SMOOTHER in smooth_solve."""
    mode = (os.getenv("TRACK_KF_R_SOURCE", "flat") or "flat").strip().lower()
    return mode if mode in ("flat", "uncertainty") else "flat"


def _outlier_mode() -> str:
    """TRACK_KF_OUTLIER_MODE, read per call — see _r_source."""
    mode = (os.getenv("TRACK_KF_OUTLIER_MODE", "reanchor") or "reanchor").strip().lower()
    return mode if mode in ("reanchor", "hold") else "reanchor"


def _stamp(result: dict, action: str, d2: float | None = None, innov_m: float | None = None) -> dict:
    """Record WHICH branch of _smooth_kf produced this result, and the two
    numbers that branch decided on.

    Every return path stamps, in both outlier modes, because these fields are
    the measurement: solver.py copies them onto the history record, so a
    capture taken under one policy can say what the other would have shown for
    the same solves (kf_d2 and kf_innov_m are exactly the two quantities the
    hold decision is made from).  d2/innov_m are None on the paths that never
    computed an innovation — first solve, gap re-init, duplicate timestamp —
    rather than 0, which would read as "the innovation was zero".

    Stamped IN PLACE on the raw-passthrough paths: those return the caller's
    own dict by identity (the EWMA contract this filter kept), and copying
    just to add three diagnostic fields would break that.
    """
    result["kf_action"] = action
    result["kf_d2"] = round(d2, 2) if d2 is not None else None
    result["kf_innov_m"] = round(innov_m, 1) if innov_m is not None else None
    return result


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


def _f_q(dt: float) -> tuple[np.ndarray, np.ndarray]:
    """State-transition F and process-noise Q for a CV model over dt seconds.

    State order [e, ve, n, vn]: the east axis occupies indices (0, 1), the
    north axis (2, 3), so the per-axis 2x2 blocks below land on those index
    pairs directly with no permutation needed.

    F is the standard CV transition, per axis [[1, dt], [0, 1]].

    Q is the standard discrete white-noise-acceleration model, per axis
    sigma_a * [[dt^3/3, dt^2/2], [dt^2/2, dt]] — EXACTLY this discretisation,
    unsquared sigma_a, matching Stone-Soup's ConstantVelocity(noise_diff_coeff)
    to 1e-6 (see TestStoneSoupOracle).
    """
    f = np.eye(4)
    f[0, 1] = dt
    f[2, 3] = dt

    q_block = _KF_SIGMA_A_MS2 * np.array(
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


def _base_sigma_m(result: dict, dark: bool) -> float:
    """The additive base sigma _measurement_R composes on top of the formal
    covariance, in metres.

    TRACK_KF_R_SOURCE picks where it comes from:

      "flat" (default) — _KF_DEFAULT_POS_SIGMA_M, 1200 m for every solve
      regardless of node count.  That is what the filter has always used, and
      it is why a two-node solve and a five-node solve are priced identically
      here even though the two-node one is measured to land more than 3 km off
      36-62% of the time against 2% at n>=5.

      "uncertainty" — services/solve_uncertainty.solve_sigma_m for this solve,
      the same calibrated number the map draws the uncertainty disc from
      (per-node-count floors 650/210/180 m at n=2/3/>=4, the formal sigma
      added in quadrature, x1.5 on the dark lane).  It was fitted on 944
      solves with ground truth for exactly this job — saying how wrong a solve
      of this shape usually is — so using it here makes the filter trust a
      solve as much as the display already claims to, no more.

    solve_sigma_m returns None for a result with no n_nodes; there is no
    calibrated floor to apply then, so the flat default is the honest answer
    rather than a guess derived from the formal sigma alone.

    The import is deliberately function-local: solve_uncertainty imports this
    module at module scope (for velocity_sigma_ms -> learned_velocity), so a
    top-level import here would be a cycle.
    """
    if _r_source() != "uncertainty":
        return _KF_DEFAULT_POS_SIGMA_M

    from services.solve_uncertainty import solve_sigma_m

    sigma = solve_sigma_m(result, dark=dark)
    return _KF_DEFAULT_POS_SIGMA_M if sigma is None else float(sigma)


def _measurement_R(result: dict, *, dark: bool = False) -> np.ndarray:
    """Per-solve position measurement covariance, in m^2.

    ``dark`` is the lane of the track key this solve is being smoothed onto
    (solver.py keys the dark lane "mn-dark-*"), threaded in from _smooth_kf
    because the calibrated base sigma is lane-dependent — it defaults to False
    so that the ``flat`` R source, which never looks at the lane, keeps a
    one-argument call site.

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

    where base = diag(base_sigma**2, base_sigma**2) — base_sigma being
    _KF_DEFAULT_POS_SIGMA_M or the calibrated per-solve sigma depending on
    TRACK_KF_R_SOURCE, see _base_sigma_m — is added UNCONDITIONALLY — the no-cov fallback is not a separate branch,
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
    base_sigma = _base_sigma_m(result, dark)
    base = np.diag([base_sigma**2, base_sigma**2])
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


def _smooth_kf(result: dict, track_key: str, adsb_hex: str | None) -> dict:
    """The "kf" branch of smooth_solve — see that function's docstring for
    the mode dispatch this is reached through."""
    r_lat = result["lat"]
    r_lon = result["lon"]
    ts_s = result.get("timestamp_ms", 0) / 1000.0
    # The lane, from the key solver.py keyed this solve onto (the same
    # authority multinode_to_aircraft uses).  Both new behaviours are
    # lane-aware: the calibrated base sigma carries a dark gain, and holding
    # is dark-only because the ADS-B lane keys off the transponder hex and so
    # never suffers the wrong-key proximity join this defends against.
    dark = track_key.startswith("mn-dark-")

    has_adsb_vel, v0e, v0n = _adsb_velocity(adsb_hex)
    r_pos = _measurement_R(result, dark=dark)

    with _KF_LOCK:
        _sweep(ts_s)
        entry = _KF_TRACKS.get(track_key)

        if entry is None:
            _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
            # First solve for this key — nothing to smooth against yet.
            return _stamp(result, "init")

        dt = ts_s - entry.last_ts_s
        if dt <= 0:
            # Known/intentional, not a gap in the model — see the module
            # docstring.  In production this fires mostly as an association
            # burst: several solves for the same key sharing one
            # measurement_ts_ms because overlapping single-node association
            # rounds re-solved the same epoch.  Fusing them would
            # double-count the same underlying measurements rather than add
            # independent evidence, so this stays a no-op, same as the EWMA.
            return _stamp(result, "passthrough")

        if dt > _KF_MAX_GAP_S:
            # Too long a gap to bridge with any confidence — start over here,
            # same threshold and same rationale as solver.py's _MN_DR_MAX_AGE_S.
            _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
            return _stamp(result, "init")

        f, q = _f_q(dt)
        x_pred = f @ entry.x
        p_pred = f @ entry.P @ f.T + q
        # Symmetrize here too, not only in _kf_correct: F P F^T is symmetric
        # in exact arithmetic but drifts by roundoff, and this filter composes
        # predict and update thousands of times over a track's life.
        p_pred = (p_pred + p_pred.T) / 2.0

        z_e, z_n = _enu_offset_m(entry.ref_lat, entry.ref_lon, r_lat, r_lon)
        z = np.array([z_e, z_n])
        x_upd, p_upd, y, s_cov = _kf_correct(x_pred, p_pred, z, _H_POS, r_pos)
        d2 = float(y @ np.linalg.solve(s_cov, y))
        innov_m = float(math.hypot(float(y[0]), float(y[1])))

        if d2 > _KF_GATE_CHI2:
            # This measurement is not a plausible continuation of the track
            # this filter has been carrying — track identity broke (a merge,
            # a re-association, a genuinely different aircraft), not a sharp
            # manoeuvre.
            #
            # Which of those two it is, the solve cannot say and the track
            # can.  TRACK_KF_OUTLIER_MODE="reanchor" (the default, and every
            # non-dark key) believes the solve and re-anchors at it.  "hold"
            # believes an ESTABLISHED dark track instead, because that is what
            # the 2026-09-05 ghost tracing found the failure to be: 58% of
            # ghost frames sat on keys with four-or-more-node solves already
            # behind them, moved there by a two- or three-node solve that
            # joined by proximity and was simply wrong (25% of n=3 joins land
            # >3 km off; an exactly-determined three-node fit has rms ~0
            # whether or not it is right, so nothing at solve time catches
            # it).  A key with several four-plus-node solves behind it knows
            # its position to a few hundred metres, and a 4 km innovation
            # against that is not flight — 250 m/s x 5 s is 1.25 km.
            if (
                _outlier_mode() == "hold"
                and dark
                and entry.n_updates >= _KF_HOLD_MIN_UPDATES
                and innov_m > _KF_HOLD_MIN_INNOV_M
            ):
                entry.breach_streak += 1
                if entry.breach_streak < _KF_HOLD_MAX_STREAK:
                    # HOLD: the update is not applied and the anchor does not
                    # move; the state advances by the coast (x_pred, p_pred)
                    # alone, so the key is refreshed at its own predicted
                    # position, learned_velocity keeps working, and the
                    # growing p_pred makes the filter progressively easier to
                    # convince if the new position is real after all.
                    entry.x = x_pred
                    entry.P = p_pred
                    entry.last_ts_s = ts_s
                    lat_h, lon_h = offset_latlon_m(
                        entry.ref_lat, entry.ref_lon, east_m=float(x_pred[0]), north_m=float(x_pred[2])
                    )
                    held = dict(result)
                    held["lat"] = round(lat_h, 6)
                    held["lon"] = round(lon_h, 6)
                    held["smoother"] = "kf"
                    held["kf_pos_sigma_m"] = round(math.sqrt(max(0.0, 0.5 * (p_pred[0, 0] + p_pred[2, 2]))), 1)
                    return _stamp(held, "held", d2, innov_m)
                # The streak ran out: the new position has been confirmed
                # _KF_HOLD_MAX_STREAK times running, so this is not one bad
                # join, the identity genuinely changed.  Fall through and
                # re-anchor — _init_entry builds a fresh entry, which is also
                # what resets the streak.

            # Re-anchor at the new position instead of smearing the estimate
            # across two aircraft.
            _KF_TRACKS[track_key] = _init_entry(r_lat, r_lon, ts_s, result, has_adsb_vel, v0e, v0n, r_pos)
            return _stamp(result, "reanchored", d2, innov_m)

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
        # An accepted update is what makes a track established (and, in hold
        # mode, what gives it standing to overrule a later solve); it also
        # ends any run of held breaches, since the track and the solves agree
        # again.
        entry.n_updates += 1
        entry.breach_streak = 0

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
        return _stamp(smoothed, "smoothed", d2, innov_m)


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

    The "kf" branch additionally honours TRACK_KF_R_SOURCE and
    TRACK_KF_OUTLIER_MODE (see the module docstring); both default to the
    behaviour this filter has always had.
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
