"""Calibrated position uncertainty for a multinode solve — the number the map
draws its disc from and the detail panel quotes.

The solver already reports `pos_sigma_km`, the Gauss-Newton `s²·(JᵀJ)⁻¹`
sigma of the LM fit.  That is a measurement-noise-propagation LOWER BOUND and
nothing more: it cannot see inter-node frame-time skew, association
contamination, or pinned-altitude error, exactly the same blindness
services/track_filter.py documents at length for `cov_en_km2` (they are the
same covariance).  Taken at face value it under-states real error by 2.8x
(n>=4) to 5.2x (n=2) at the median, and it has a degenerate tail — near-parallel
baselines put its p99 at 3.8e6 km — so it needs both a floor and a cap before
it can be shown to anyone.

`track_filter.kf_pos_sigma_m` is not the answer either: the filter's 1200 m
unmodeled-error floor dominates its post-update marginal, so it sits near 1 km
whatever the solve quality and over-states n>=3 by ~3x.  It is tuned for
weighting solves against each other inside the filter, not for display.

Model (see docs/design-notes/2026-09-05-solve-uncertainty-disc.md for the fit):

    sigma_solve = sqrt( (A * min(sigma_formal, CAP))^2 + B(n_nodes)^2 )
                  * (DARK_GAIN if dark lane else 1)

fitted on 944 solves with ground truth from staging + the test droplet
(2026-09-05).  B per node-count bucket was chosen so that exactly 95% of raw
errors fall inside `2.448·sigma` (the Rayleigh 95% quantile), then rounded up:
650 / 210 / 180 m for n=2 / n=3 / n>=4.

A stays at UNIT gain deliberately.  The formal sigma is informative — its
quartiles (27 m -> 300 m) map monotonically onto raw-error medians (0.22 km ->
0.82 km) — but larger A buys almost no sharpness in the median R95 while
doubling the n=2 p90 by A=3.  Unit gain is enough to let a genuinely
ill-conditioned solve show a bigger disc without letting the degenerate tail
drive the display.

DARK_GAIN is a PRIOR, not a measurement: the calibration sample is almost
entirely known-lane (ADS-B seeded the initial guess and pinned the altitude),
and only 14 dark solves had ground truth.  It is env-tunable for that reason.

Growth while the display dead-reckons between solves is the same variance
addition against the velocity sigma — see grown_sigma_m.  The feed does not
apply it: it ships sigma at the solve epoch plus `seen` (the solve age) and
`pos_sigma_vel_ms`, and the frontend grows the disc at its own 2 Hz tick.
"""

import math
import os

from services import track_filter

# ── Tunables ──────────────────────────────────────────────────────────────
# Read once at import — model constants from the 2026-09-05 fit, not per-call
# behaviour switches.  Tests monkeypatch the module attributes rather than
# reimporting.

# Gain on the formal LM-fit sigma.  Unit by choice, see the module docstring.
_FORMAL_GAIN = float(os.getenv("SOLVE_SIGMA_FORMAL_GAIN", "1.0"))

# The formal sigma's tail is degenerate (p99 3.8e6 km, max 3e8 km, from
# near-parallel baselines), so it is capped before it ever enters the sum.
_FORMAL_CAP_M = float(os.getenv("SOLVE_SIGMA_FORMAL_CAP_M", "3000"))

# Per-node-count error floors: the unmodeled error the formal sigma cannot
# see, sized for 95% coverage of raw solve error against ADS-B truth.
# Staging (real hardware, B95 718 m) and test (synthetic fleet, 738 m) agreed
# at n=2, so one set of floors serves both environments.
_FLOOR_N2_M = float(os.getenv("SOLVE_SIGMA_FLOOR_N2_M", "650"))
_FLOOR_N3_M = float(os.getenv("SOLVE_SIGMA_FLOOR_N3_M", "210"))
_FLOOR_N4_M = float(os.getenv("SOLVE_SIGMA_FLOOR_N4_M", "180"))

# Dark-lane inflation.  A prior, not a fit — see the module docstring.
_DARK_GAIN = float(os.getenv("SOLVE_SIGMA_DARK_GAIN", "1.5"))

# Velocity sigma used for growth when the KF has no state for the key.
_VEL_DEFAULT_MS = float(os.getenv("SOLVE_SIGMA_VEL_DEFAULT_MS", "25"))

# Final clamp on sigma_solve.  Neither end should ever bind on a sane solve
# (the floors alone put every bucket above 180 m); they exist so a pathological
# formal sigma or a mis-set env key cannot produce a disc that claims
# centimetre accuracy or swallows the map.
_SIGMA_MIN_M = 50.0
_SIGMA_MAX_M = 5000.0

# Velocity-sigma clamp band and the growth horizon.  Past the horizon the
# frontend stops dead-reckoning entirely, so growing the disc further would
# describe a position nothing is drawing.  It tracks the frontend's
# UNCERTAINTY_DR_CAP_S, which in turn tracks MN_DARK_EXPIRY_S: a dark entry no
# longer reaches 60 s at all, so the old 60 s horizon described entries that
# cannot exist.
_VEL_MIN_MS = 5.0
_VEL_MAX_MS = 150.0
_GROWTH_MAX_AGE_S = 30.0


def _floor_m(n_nodes: int) -> float:
    """Unmodeled-error floor for a solve with this many nodes."""
    if n_nodes <= 2:
        return _FLOOR_N2_M
    if n_nodes == 3:
        return _FLOOR_N3_M
    return _FLOOR_N4_M


def _formal_m(result: dict) -> float:
    """The formal LM-fit sigma in metres, capped, or 0.0 when unusable.

    Absent, None, non-finite and non-positive all collapse to 0.0 — the same
    treatment track_filter._measurement_R gives a degenerate cov_en_km2, and
    the cov=0 limit of the same formula, so there is no separate code path for
    "no formal sigma": the answer is simply the floor.
    """
    raw = result.get("pos_sigma_km")
    if raw is None:
        return 0.0
    try:
        sigma_m = float(raw) * 1000.0
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(sigma_m) or sigma_m <= 0.0:
        return 0.0
    return min(sigma_m, _FORMAL_CAP_M)


def solve_sigma_m(result: dict, *, dark: bool) -> float | None:
    """Per-axis position sigma, in metres, for one solve at its own epoch.

    ``result`` is a solver-result dict (or the smoother's copy of one).
    Returns None when it carries no ``n_nodes`` — without a node count there
    is no floor to apply, and a disc drawn off the formal sigma alone would be
    dishonestly tight.  Callers omit the wire field entirely in that case
    rather than shipping a guess.

    ``dark`` inflates by DARK_GAIN: a dark-lane solve had no ADS-B fix seeding
    its initial guess and no pinned altitude.
    """
    n_raw = result.get("n_nodes")
    if n_raw is None:
        return None
    try:
        n_nodes = int(n_raw)
    except (TypeError, ValueError):
        return None

    formal_m = _FORMAL_GAIN * _formal_m(result)
    floor_m = _floor_m(n_nodes)
    sigma = math.sqrt(formal_m**2 + floor_m**2)
    if dark:
        sigma *= _DARK_GAIN
    return float(min(max(sigma, _SIGMA_MIN_M), _SIGMA_MAX_M))


def grown_sigma_m(sigma_m: float, vel_sigma_ms: float, age_s: float) -> float:
    """``sigma_m`` grown for ``age_s`` seconds of dead-reckoning.

    Position error at the solve epoch and velocity error over the coast are
    independent, so their variances add:

        sigma(t) = sqrt( sigma_solve^2 + (sigma_v * min(t, _GROWTH_MAX_AGE_S))^2 )

    Age is clamped to [0, _GROWTH_MAX_AGE_S]: negative is a clock artefact, and
    past the horizon the frontend has stopped dead-reckoning, so a bigger disc
    would not correspond to anything on screen.  The frontend's own
    uncertainty.ts is the mirror of this function; this one exists for backend
    callers and for the tests that pin the two to the same shape.
    """
    t = min(max(age_s, 0.0), _GROWTH_MAX_AGE_S)
    return float(math.sqrt(sigma_m**2 + (vel_sigma_ms * t) ** 2))


def velocity_sigma_ms(track_key: str) -> float:
    """Velocity sigma, m/s, for growing this key's disc between solves.

    Prefers the display filter's own velocity marginal
    (``track_filter.learned_velocity(key)[2]``) — the filter knows whether it
    has accumulated real evidence for this track or is still carrying its
    150 m/s init prior, and a fresh dark solve seeded from the CV fit SHOULD
    grow fast (solved velocity was measured at 127 m/s median vector error on
    2026-08-09).  Falls back to SOLVE_SIGMA_VEL_DEFAULT_MS whenever the KF has
    no state for the key: never smoothed, TTL-swept, or TRACK_SMOOTHER not in
    "kf" mode, all of which learned_velocity reports as None.

    Clamped to [5, 150] m/s so neither a filter that has over-converged nor
    one still at its prior can dominate the growth term.
    """
    lv = track_filter.learned_velocity(track_key)
    if lv is None:
        return _VEL_DEFAULT_MS
    vel_sigma = float(lv[2])
    if not math.isfinite(vel_sigma):
        return _VEL_DEFAULT_MS
    return float(min(max(vel_sigma, _VEL_MIN_MS), _VEL_MAX_MS))
