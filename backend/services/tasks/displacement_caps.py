"""How far a multinode solve may land from the guess it was seeded with.

The solver's displacement gate judges each solve against one of these caps,
chosen by lane (_is_dark_solver_input) and anchoring (_is_anchored_n2).  The
solve-history record stamps which cap judged it, and the known lane labels its
own solves against the lane caps.
"""

import os

from config.constants import ASSOC_GRID_STEP_KM
from services.id_utils import is_transponder_hex

# Reject n=2 solver results whose position moved more than this many km from
# the initial_guess supplied by the association layer.
#
# For n=2 (exactly-determined position), the LM solver can converge to the
# false bistatic ellipse intersection (the mirror point) instead of the true
# one.  The solver's beam-coverage gate catches mirror points that land outside
# a node's detection beam; this check catches the remainder.
#
# The association grid step is 3 km, so the initial_guess is within ~3 km of
# the true aircraft position (the delay-residual-minimising grid point is
# always close to the real bistatic intersection).  A good solve therefore
# stays within a few km of the initial_guess.  Mirror points are typically
# 15–50 km from the true position, meaning they are ≥12 km from an
# initial_guess that was placed near the truth.
#
# The cap is per-lane, because the anchor's own uncertainty is: what the
# gate actually measures is |solve − anchor|, and that is only a proxy for
# position error when the anchor is trustworthy.
#
# ADS-B-anchored inputs (_is_dark_solver_input false).  With the ADS-B
# position override in find_associations(), the initial_guess is within
# ~100 m of the true aircraft position, so displacement from it approximates
# the position error directly.  With σ_delay = 0.1 µs the displacement =
# GDOP × 0.1 × 0.3 km, so 2.0 km → GDOP ≤ 67 (reasonable bistatic geometry).
# Mirror-point ghosts land 15–50 km away and are safely rejected.
#
# Originally N=2-only because that's where mirror-points dominate. Production
# /api/test/mlat-accuracy stats showed N=2 medians 4-6× tighter than N≥4
# medians even after dedup-by-aircraft (964 unique N=2 vs 68 unique N=4 — N=2
# median 0.47 km, N=4 median 2.48 km). Root cause: the gate was the only
# stage rejecting solver-vs-ADS-B disagreements, so N≥3 stats kept every bad
# convergence (wrong-frame association, local-minimum trap), while N=2 was
# pre-filtered. Generalising the gate to every N puts the comparison on the
# same footing — bad N≥3 solves are now rejected on the same criterion.
#
# This constant has a second consumer: known_lane.py labels its solves
# truth_match/ghost against it (there the distance is to a known ADS-B fix,
# so the ADS-B-anchored reading is the right one).  Retune it with that in
# mind — the dark cap below is the one to move for dark-lane recall.
_MAX_DISPLACEMENT_KM = 2.0


# DARK inputs (_is_dark_solver_input true — no usable transponder identity on
# the solver input).  The ~100 m premise above does not hold: nothing
# overrode the guess with a transponder position, so the anchor is a
# quantised ASSOC_GRID_STEP_KM (3 km) lattice point, averaged over a
# candidate cluster up to the association layer's merge radius (6 km) wide.
# The anchor's own uncertainty is therefore of order the grid step, and
# judging a dark solve at 2 km measures the anchor, not the solve.
#
# Live (2026-09, 10 min): displacement was 41% of dark-lane solver attempts
# (109/265).  Of 31 recent dark rejected_displacement records carrying a
# ground-truth stamp, median GT error was 2.1 km and 20/31 were under 3 km —
# only 1/31 was ≥ 10 km (a real ghost) — against a displacement_km median of
# 3.26 km (min 2.06, 23/31 ≤ 4 km).  Published dark solves sit at a 0.98 km
# GT-error median, so most of what the 2 km cap killed was of a quality
# comparable to what it passed.
#
# Default 6.0 km = 2 × ASSOC_GRID_STEP_KM: one grid step for the
# quantisation, one for the cluster-merge spread.  This widens the allowance
# for anchor uncertainty, not for bad convergence — mirror points and
# wrong-frame solves land 15–50 km out and are still rejected with room to
# spare.  The env override is an absolute km value, not a grid multiple, so
# live tuning does not have to reason about the association grid.
def _dark_displacement_cap_km() -> float:
    """Resolve the dark-lane displacement cap from the environment.

    A function rather than an inline ``float(os.getenv(...))`` (solver.py's
    _SOLVER_RMS_DELAY_MAX_US idiom) only because the default is derived
    from ASSOC_GRID_STEP_KM rather than being a literal — and so a test can
    exercise the env plumbing without reimporting this module.
    """
    raw = os.getenv("SOLVER_MAX_DISPLACEMENT_KM_DARK")
    if raw:
        return float(raw)
    return max(_MAX_DISPLACEMENT_KM, 2.0 * ASSOC_GRID_STEP_KM)


_MAX_DISPLACEMENT_KM_DARK = _dark_displacement_cap_km()


# ANCHORED n=2 inputs are judged far more tightly than either lane cap above.
#
# Letting an anchored dark-follow input publish at n=2 (the change solver.py's
# _n2_anchor_admits makes) bought coverage and cost accuracy.  Measured on the
# test droplet over 20-minute captures, with the bypass on: dark 2-node time
# solved went 35% → 57% and 4+-node fresh 65% → 77%, but follow-lane n=2
# publishes landed a median 1.43 km (p90 4.6 km) from truth, dark ghosts
# (an entry > 5 km from any ground truth) went 4.1% → 9.7% overall, and the
# n=2 entries themselves went 6% → 22% ghosts.
#
# The tight cap is meaningful precisely BECAUSE the input is anchored.  The
# wide dark cap exists for anchor uncertainty: an ordinary dark guess is a
# quantised ASSOC_GRID_STEP_KM lattice point averaged over a 6 km-wide cluster,
# so judging the solve against it at 2 km measures the anchor.  A follow
# input's guess is not that — it is the follow lane's own dead-reckoned
# prediction of a track with DARK_FOLLOW_MIN_SOLVES solves behind it, a real
# position estimate whose own error budget is well under a kilometre.  And the
# fit on the other side is weaker, not stronger: with altitude pinned an n=2
# solve fits 5 unknowns against 4 residuals, so the residual gates cannot see a
# wrong answer (see solver.py's _N2_REQUIRE_CONFIRMED) and the LM is free to
# wander along the under-determined direction.  Past 1.5 km we are therefore
# looking at that wandering rather than at anchor uncertainty.
#
# Rejecting is the intended outcome here, not a loss of coverage: the reject
# counts toward the follow lane's two-consecutive-rejects drop
# (services/dark_follow.py record_outcome), which is exactly the guard that
# should fire once the follow can no longer be corroborated.
_DARK_FOLLOW_N2_MAX_DISP_KM = float(os.getenv("DARK_FOLLOW_N2_MAX_DISP_KM", "1.5"))


def _is_anchored_n2(s, r) -> bool:
    """True if this is an anchored solver input that solved at exactly n=2.

    Both the displacement gate and the history record ask this — the gate to
    pick the cap, the record to stamp which cap judged the solve — and they
    must not be able to disagree, so the predicate lives in one place.
    """
    if not isinstance(s, dict) or not s.get("anchor_key"):
        return False
    # Result first, input as the fallback — the same precedence the history
    # record's own "n_nodes" field uses, so a solve_fn that does not restate
    # n_nodes (the trim path is the only producer that changes it) is still
    # judged by the node count it actually had.
    return int((r or {}).get("n_nodes") or s.get("n_nodes") or 0) == 2


def _is_dark_solver_input(s_in) -> bool:
    """True when a solver input carries no usable ADS-B identity.

    The same predicate multinode_key_decision mints keys with: an id that is
    not transponder-shaped (a simulator object id, a claim against a poisoned
    adsb_aircraft entry) is not an ADS-B anchor, so it must be judged as dark
    rather than inherit the ADS-B lane's tight displacement cap on a guess
    that nothing overrode.  Keeping the two in step is what makes
    displacement_cap_km on a history record agree with the mn-dark-/mn-adsb-
    lane the same solve is keyed into.
    """
    hx = s_in.get("adsb_hex") if isinstance(s_in, dict) else None
    return not (hx and is_transponder_hex(hx))
