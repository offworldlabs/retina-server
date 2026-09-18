"""Multinode solver worker threads — drain state.solver_queue → solve_multinode."""

import logging
import os
import queue
import threading
import time

from retina_analytics.association import predict_observation

from config.constants import (
    CV_VEL_ADOPT_CHI2_MAX,
    MN_STALE_COAST_ENABLED,
    N2_CONFIRM_CHI2_MAX,
    N2_CONFIRM_MIN_EPOCHS,
    N2_TRACK_ASSOCIATION,
)
from core import state
from services import dark_follow, track_filter

# Beam-coverage geometry, used to reject solver results whose range or (at
# n=2) bearing fall outside a contributing node's detection area.  This
# module carried its own haversine, bearing and in-beam rule until those
# were consolidated into services.geo.
from services.feed_helpers import adopt_track_history
from services.geo import bearing_deg, bistatic_differential_km, node_beam_params, offset_latlon_m
from services.geo import haversine_km as _haversine_km
from services.id_utils import multinode_hex_from_key
from services.node_config import position_status
from services.tasks import displacement_caps, multinode_identity, solve_history, solver_pool

# Worker-thread lifecycle, owned by start_solver_workers / stop_solver_workers.
_solver_workers_lock = threading.Lock()
_solver_workers: list[threading.Thread] = []
_solver_stop = threading.Event()


# Altitude layers (km) swept when n_nodes ≥ 3.  For an overdetermined system
# (3+ delay equations, 2 unknowns after altitude pinning) only the correct
# altitude layer yields rms_delay ≈ 0; wrong layers give rms > 0, so picking
# the minimum selects the true altitude.  Range 1.5–11 km covers simulation
# aircraft (0.3–15 km spawns) and commercial aviation.  The 1.5 and 3.0 km
# layers fix systematic 7–10 km errors for low-altitude aircraft where the old
# [5, 7, 9, 11] set forced a wrong altitude.
#
# These happen to equal the association grid's default layers
# (ASSOC_ALT_LAYERS_KM) but are not tied to them: the initial guess is a
# delay-residual weighted MEAN across the grid's layers and so has never been
# a layer value anyway, and _solve_best_altitude folds the guess altitude into
# the sweep as its own extra layer.  Doubling this list would double every
# n>=3 solve's cost to refine an altitude the overdetermined residual already
# resolves.
_SOLVER_ALT_LAYERS_KM = [1.5, 3.0, 5.0, 7.0, 9.0, 11.0]

# Reject solver results whose RMS delay residual exceeds this value.
# For n≥3 nodes with altitude pinned (overdetermined: 3 equations, 2 unknowns),
# a true association converges with rms_delay ≈ measurement_noise ≈ 1-2 µs.
# False associations (delay measurements from different aircraft) produce
# inconsistent equations → rms_delay = 3-10 µs.
# For n=2, rms=0 at BOTH the true and mirror positions (exactly determined),
# so the threshold can't distinguish mirror from truth — keep generous.
# A single threshold of 3.0 µs cleans up false n≥3 associations while
# letting all n=2 results through (n=2 mirrors always have rms ≈ 0).
#
# 2026-08 live re-evaluation at n≥4: this blanket gate had near-zero
# discrimination — the bad-solve-through rate sat flat at ~2% across every
# threshold tried, while raising it enough to matter halved the good solves
# let through.  Cause: one contaminated measurement (a single-node track
# from a different aircraft, bundled in by association) inflates rms_delay
# without moving the Huber-fitted position, so rms stopped tracking position
# quality at n≥4.  _trim_and_resolve below drops the offending node instead
# of the whole solve, which restores the gate's original meaning.  Kept
# env-overridable for live tuning while that policy beds in.
_SOLVER_RMS_DELAY_MAX_US = float(os.getenv("SOLVER_RMS_DELAY_MAX_US", "3.0"))

# Consensus hypothesis stage (retina_geolocator.consensus.select_consensus):
# runs once at n≥3, ahead of the LM altitude sweep, and picks the subset of
# contributing nodes whose pairwise delay-ellipse intersections corroborate
# each other.  Bench measurement (association_bench.py --estimator
# consensus-refine vs lm, 5 seeds × default + dense scenes): n≥3 ghost
# solves ~3x down (13-16 vs ~46), position error slightly better, zero real
# tracks lost, LM-equivalent per-solve cost (consensus picks the subset,
# the existing LM solve still runs — this is not a solver replacement).
#   off    (default) — consensus never runs.
#   shadow — consensus runs and its selection is recorded on the history
#            record (consensus_meta), but the LM sees the unfiltered input;
#            for comparing the two without touching what publishes.
#   active — consensus's selection replaces the LM's input measurements
#            when it corroborates >= _CONSENSUS_MIN_NODES nodes; otherwise
#            (abstain, too few nodes, or an exception) it falls back to the
#            unfiltered input silently — see _consensus_select.
_CONSENSUS_MODE = os.getenv("SOLVER_CONSENSUS_MODE", "off").lower()
# Below this many corroborated nodes, a consensus selection is not trusted
# enough to filter on — the blanket rms_delay gate (and, if it still fails,
# trimming) is left to make the call on the unfiltered input instead.
_CONSENSUS_MIN_NODES = 3

# Node-trimming policy for the rms_delay gate at n≥4 (see above).  Iteratively
# drop the worst-residual node(s) and re-solve, down to a floor of
# _TRIM_MIN_NODES — below that the geometry is too thin to trust a trim's own
# residuals, and the blanket gate is left to make the call.
_TRIM_MAX_ROUNDS = 4
_TRIM_RESID_FACTOR = 1.5  # drop nodes with |res| > factor × rms_delay
_TRIM_MIN_NODES = 3

# Reject solver results whose RMS Doppler residual exceeds this value.
# Physics: for FM illuminators (fc ≈ 98–108 MHz, λ ≈ 2.8–3.1 m), the maximum
# bistatic Doppler for any real aircraft is 2 × v_max / λ ≈ 2 × 300 / 3.06 ≈ 196 Hz.
# For a true n-node association the solver fits velocity to n Doppler equations;
# with n=2 the system is exactly determined → rms_doppler ≈ 0 regardless.
# With n≥3 it is overdetermined → rms_doppler reflects measurement noise (< 20 Hz).
# False associations (delays/Dopplers from different aircraft) leave large, physically
# unrealisable Doppler residuals (observed: 248 Hz for confirmed false associations).
# Threshold at 200 Hz = max bistatic Doppler + 2% margin; only rejects impossible cases.
_SOLVER_RMS_DOPPLER_MAX_HZ = 200.0
# ...except at n=3, where 200 Hz is no gate at all.  Three nodes give six
# measurements for six unknowns (position + velocity), so the free fit is
# exactly determined and a genuine 3-node solve leaves ~0 Hz of Doppler
# residual whatever the noise.  A residual appears only when the fit hits a
# bound (vz saturation, a pinned altitude) — and a bounded fit that STILL
# misses by tens of Hz is not one aircraft: it is two aircraft's echoes
# clustered together (the dark lane's cluster-contamination defect, see
# _stamp_foreign_nodes), which is exactly the case a 3-node solve has no
# spare equation to expose any other way.  Test droplet, 35 min, 313 n=3
# dark publishes: no solve within 1 km of truth exceeded 58 Hz (p99 54 Hz),
# while 6 of the 8 above 60 Hz were > 3 km off and the worst (173 Hz, 5.2 km)
# was a verified two-aircraft mix that the 200 Hz gate passed.  Above n=3
# the extra equations make a bad cluster show up in rms_delay and the trim
# (_trim_and_resolve) instead, and real n>=4 solves do reach 100+ Hz in
# turns, so the physical ceiling stays the gate there.
_SOLVER_N3_RMS_DOPPLER_MAX_HZ = float(os.getenv("SOLVER_N3_RMS_DOPPLER_MAX_HZ", "60.0"))


def _rms_doppler_max_hz(n_nodes) -> float:
    """The rms_doppler ceiling a solve with this many nodes must pass."""
    return _SOLVER_N3_RMS_DOPPLER_MAX_HZ if n_nodes == 3 else _SOLVER_RMS_DOPPLER_MAX_HZ


# How close an established dark key must be, after dead reckoning to this
# solve's epoch, for an n=2 input to inherit its altitude.  Default 6.0 km is
# the keying rule's own proximity gate (_mn_assoc_gate_km's base): a key this
# solve would be allowed to JOIN is a key whose altitude it may borrow, and
# anything looser would let one aircraft's cruise altitude pin another's
# position.
N2_ALT_INHERIT_KM = float(os.getenv("N2_ALT_INHERIT_KM", "6.0"))
# The donor must have been solved at 3+ nodes at some point (an n=2 donor
# knows the altitude no better than the grid does — it IS a grid altitude,
# and inheriting it would launder a guess into a measurement) and must have
# solved recently, or the inherited altitude is older than the climb it is
# meant to track.
N2_ALT_INHERIT_MIN_NODES = 3
N2_ALT_INHERIT_MAX_AGE_S = 60.0


# An n=2 solve is published only once its track pairing has justified itself.
#
# The residual gates above are structurally blind here: the solver fits
# [x, y, vx, vy, vz] with altitude pinned, 5 unknowns against 4 residuals, so
# rms_delay and rms_doppler go to ~0 for a cross pairing exactly as they do for
# a real target — which _SOLVER_RMS_DELAY_MAX_US already documents ("letting all
# n=2 results through").  Watching the phantom does not help either: its own
# motion is identically its Doppler-implied velocity, so it stays self-consistent
# for as long as both aircraft are tracked.
#
# What does discriminate is fitting one constant-velocity trajectory to the whole
# observation window of both single-node tracks — 4K measurements against 6
# unknowns instead of 4 against 6.  The associator does that and attaches
# chi2_per_dof; this gate reads it.  Solving still happens either way, so the
# position fix stays available to the display; only the *track* is withheld.
# Only meaningful when association is producing chi2 values at all; with
# the track path parked, requiring one would withhold every n=2 track.
_N2_REQUIRE_CONFIRMED = N2_TRACK_ASSOCIATION
_N2_CONFIRM_CHI2_MAX = N2_CONFIRM_CHI2_MAX

# ...and once a pairing has passed that gate, publish the position the fit
# itself produced rather than the single-epoch LM one.  Both describe the same
# target from the same measurements, but not from the same NUMBER of them: the
# LM fixes x, y from one epoch's 4 delay/Doppler residuals with altitude pinned
# to a guess, while the confirmation fit ties every epoch the pairing has
# accumulated to one constant-velocity trajectory — 4K measurements against 6
# unknowns, altitude free — and evaluates it at the last epoch.  Measured
# against ground truth on the test droplet, published n=2 solves sit a median
# 2.6 km from truth, and still 1.6 km on the subset where the altitude pin
# happened to be right, so the pin is not the whole of it: the rest is the
# single epoch.  The fit is already computed for every confirmed pairing (it is
# what confirms it), so this costs nothing and uses history the track already
# has.
#
# Env-gated because it changes what lands on the map: if the fit position turns
# out worse live, a restart with N2_PUBLISH_FIT_POSITION=0 is the way back.
# Either way the history record carries both positions and their separation
# (pos_source / solve_raw_lat / solve_raw_lon / fit_vs_solve_km), so gt_error
# can be split by pos_source without a second deploy.
_N2_PUBLISH_FIT_POSITION = os.getenv("N2_PUBLISH_FIT_POSITION", "1").strip().lower() not in ("0", "false", "off")

# ...but published with its altitude PINNED, not the one it solved for.
#
# The fit that confirms the pairing solves a free z, correctly: a free
# parameter in the chi2 test would hand a crossed pairing slack it should not
# get.  For POSITION the same freedom is a defect.  At n=2 altitude is not
# observable, so the fit spends z wherever the noise points, and a wrong z
# leans on x and y through the bistatic geometry and drags the horizontal
# answer with it.  Measured over the 14 published n=2 solves of a 20-minute
# synthetic capture: published altitudes of -1721 m, -466 m and 15249 m, with
# |published alt - truth| at a 4.57 km median against 2.94 km for the ladder
# guess the swap replaced, and fit_vs_solve_km at a 5.4 km median.
#
# So the position swap uses a SECOND fit of the same epochs with
# fix_altitude=True (cached separately as _cv_fit_pinned).  The confirmation
# gate keeps reading the free fit: its chi2 threshold was calibrated against a
# 6-state dof and re-deciding the gate is not this change.  Two fits of the
# same epochs is one extra ~86 ms pool call per confirmed n=2 solve, paid on
# the worker's own threads.
_N2_FIT_FIX_ALTITUDE = os.getenv("N2_FIT_FIX_ALTITUDE", "1").strip().lower() not in ("0", "false", "off")

# The calibration age rule moved to config.constants.CAL_MAX_ADSB_AGE_S and is
# applied by services.calibration, which both recording sites now go through.
# It lived here while the frame path had its own, looser, unstated one.


# ── Measurement epoch alignment ──────────────────────────────────────────────
# The solver's residual model evaluates every measurement against ONE target
# state: the measurement set is assumed simultaneous.  It is not.  Each node
# samples on its own free-running cadence (~0.74-1 Hz on the fleet), so the
# delays in one solver input were captured at times spread over up to a frame
# interval, and association hands over each track's newest sample regardless of
# when that was.  A 250 m/s target moves ~250 m per second of skew, which shows
# up as up to ~1 us of bistatic delay error per second — charged in full to the
# 3 us rms_delay gate, where it is indistinguishable from a contaminated node
# and drives the trim to throw away legitimately in-cone nodes.
#
# The correction is closed-form and needs nothing the measurement does not
# already carry.  Writing d_tx / d_rx for the TX->target and target->RX ranges,
# the bistatic delay is (d_tx + d_rx - baseline)/c and the bistatic Doppler is
# (fc/c)(v_tx + v_rx), where v_tx / v_rx are the target's velocity components
# along the unit vectors pointing FROM the target TOWARD the TX and the RX
# (retina_geolocator.multinode_solver._residual_function; the simulator's
# _bistatic_delay / _bistatic_doppler in retina_simulation.world use the
# identical convention).  Moving toward a site shortens that leg, so
# d(d_tx)/dt = -v_tx and d(d_rx)/dt = -v_rx, and therefore
#
#     d(delay_us)/dt = -(v_tx + v_rx) / C_KM_US
#                    = -doppler_hz * (C_KM_S / fc_hz) / C_KM_US
#                    = -doppler_hz * 1e6 / fc_hz
#
# i.e. positive Doppler is a closing target and its delay is DECREASING.  The
# unit test test_epoch_alignment.py checks the sign against a target flown
# through the simulator's own geometry helpers at two times, rather than
# against this derivation.
_DELAY_RATE_HZ_TO_US_PER_S = 1e6


def align_measurement_epochs(s_in: dict, node_cfgs: dict) -> tuple[dict, dict]:
    """Dead-reckon every measurement's delay onto the newest one's epoch.

    Pure: returns a new solver input (shallow copy, fresh measurement dicts)
    and a metadata dict for the history record; *s_in* is never mutated, so a
    caller can drop the result and keep the untouched input.

    Alignment is all-or-nothing per input.  A partially aligned set is worse
    than an unaligned one — the residual model has no way to know which
    measurements share an epoch, so mixing corrected and uncorrected delays
    just moves the error onto a different node.  Any measurement missing t_s
    or doppler_hz, or whose node has no config to read fc_hz from, therefore
    skips the whole input and counts solver_epoch_align_skipped.

    Returns (s_in, meta) where meta carries epoch_aligned and, when it ran,
    epoch_skew_s — the widest gap the correction closed.
    """
    meas = s_in.get("measurements") or []
    if len(meas) < 2:
        return s_in, {"epoch_aligned": False}

    rates = []
    for m in meas:
        t_s = m.get("t_s")
        doppler = m.get("doppler_hz")
        cfg = node_cfgs.get(m.get("node_id")) or {}
        # Same fallback chain the geolocator uses when it builds its NodeSetup,
        # so a node whose config spells the carrier "FC" aligns on exactly the
        # frequency the solve will predict against.
        fc_hz = cfg.get("fc_hz", cfg.get("FC"))
        if t_s is None or doppler is None or not fc_hz:
            state.bump_counter("solver_epoch_align_skipped")
            return s_in, {"epoch_aligned": False}
        rates.append((float(t_s), -float(doppler) * _DELAY_RATE_HZ_TO_US_PER_S / float(fc_hz)))

    # The newest sample, not the input's timestamp_ms: t0 has to be a time some
    # measurement was actually taken, or every delay is extrapolated and the
    # freshest node — the one that needed no correction — acquires an error.
    t0 = max(t for t, _ in rates)
    skew_s = t0 - min(t for t, _ in rates)

    aligned = dict(s_in)
    aligned["measurements"] = [
        {**m, "delay_us": float(m["delay_us"]) + rate * (t0 - t_s)} for m, (t_s, rate) in zip(meas, rates)
    ]
    if "timestamp_ms" in aligned:
        # The set now describes t0, so everything downstream that ages this
        # solve (multinode expiry, the dead-reckoning gates, the history
        # record's measurement_ts_ms) should date it from t0 too.
        aligned["timestamp_ms"] = int(round(t0 * 1000.0))
    return aligned, {"epoch_aligned": True, "epoch_skew_s": round(skew_s, 3)}


def _sweep_altitudes(s_in: dict, node_cfgs: dict, solve_fn, layers_km: list[float], metric: str) -> dict | None:
    """Try each altitude layer; return the result with lowest value of `metric`.

    Args:
        metric: Solver output key to minimise across layers.  Currently always
                'rms_delay' (used by n≥3 where the overdetermined system gives
                rms≈0 at the correct altitude).
    """
    base_guess = s_in["initial_guess"]
    best_result: dict | None = None
    best_rms = float("inf")
    last_exc: BaseException | None = None

    for alt_km in layers_km:
        s_try = dict(s_in)
        s_try["initial_guess"] = dict(base_guess, alt_km=alt_km)
        try:
            result = solve_fn(s_try, node_cfgs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        if result and result.get("success"):
            rms_raw = result.get(metric)
            rms = float("inf") if rms_raw is None else float(rms_raw)
            logging.debug(
                "altitude sweep: z=%.1fkm %s=%.3f (best so far=%.3f)",
                alt_km,
                metric,
                rms,
                best_rms,
            )
            if rms < best_rms:
                best_rms = rms
                best_result = result

    if best_result is None and last_exc is not None:
        raise last_exc

    return best_result


# Fewest measurements the free mode is used at.  Below this altitude is not
# observable and retina_geolocator pins it anyway; the sweep is left in place
# so the n=2 path keeps its documented behaviour exactly.
_FREE_ALT_MIN_NODES = 3


def _free_alt_starts(ig_alt_km, layers: list[float]) -> list[float]:
    """The start altitudes the free mode hands the multi-start helper.

    state.SOLVER_FREE_ALT_STARTS of them, clamped into [1, len(layers)] — read
    per call, like the mode flag, so a test and a config reload both see what
    they set.  One start is the layer nearest ``ig_alt_km``, which is the
    altitude spliced into ``layers`` when the input carries a non-layer one of
    its own (ADS-B), exactly as the sweep treats it.  Several are a window
    centred on that layer, clamped to the ends of the ladder so the count never
    shrinks there (the top and bottom layers are where a wrong start is least
    recoverable, not most).

    Freeing z removes the ladder's quantisation but not the LM's locality, and
    the extra starts are what would stop a solve settling on the wrong side of
    a bistatic ellipse.  On this fleet's geometry they had almost nothing to
    stop: over 1019 free-mode solves on test, three starts' rms_delay differed
    by more than 0.1 µs in 13 of them, and the nearest-layer start was more
    than 0.5 µs worse than the best in 2 — so the default is one start and the
    other two are bought explicitly, by a deployment whose geometry shows it
    needs them.  See core/state.py for the numbers and the trade.
    """
    if not layers:
        return []
    n = max(1, min(int(state.SOLVER_FREE_ALT_STARTS), len(layers)))
    alt = float(ig_alt_km) if ig_alt_km is not None else 7.0
    nearest = min(range(len(layers)), key=lambda i: abs(layers[i] - alt))
    lo = max(0, min(nearest - (n - 1) // 2, len(layers) - n))
    return layers[lo : lo + n]


def _solve_best_altitude(
    s_in: dict,
    node_cfgs: dict,
    solve_fn,
    multistart_fn=solver_pool._pool_solve_multistart,
) -> dict | None:
    """Altitude for n≥3, by whichever rule state.SOLVER_ALT_MODE names.

    sweep (default): solve once per layer, pick by minimum rms_delay.  If the
    initial_guess already carries an ADS-B altitude (not one of the fixed grid
    layers), include it in the sweep so the correct exact altitude is tried.

    free: one call to the multi-start helper, which solves altitude as a sixth
    unknown from _free_alt_starts.  The sweep cannot do better than half its
    2 km layer spacing, and on noise-free replay of this fleet's geometry that
    quantisation alone left rms_delay at a 1.76 µs median against the 3.0 µs
    reject gate — spending most of the gate's budget on an altitude the
    measurements themselves determine, and provoking _trim_and_resolve to drop
    nodes that were never the problem.  Costs one pool round trip per
    candidate instead of six.

    The mode is read per call rather than captured at import, so a test (and a
    live config reload) sees the value it set.  Read here and not inside
    _process_solver_item because _trim_and_resolve re-enters through this same
    function: a trim must re-solve under the mode its first solve used, or the
    residuals it is comparing are not the same quantity.
    """
    ig_alt = s_in.get("initial_guess", {}).get("alt_km")
    if ig_alt is not None and ig_alt not in _SOLVER_ALT_LAYERS_KM:
        layers = sorted(set(_SOLVER_ALT_LAYERS_KM + [round(float(ig_alt), 3)]))
    else:
        layers = _SOLVER_ALT_LAYERS_KM
    n_meas = len({m.get("node_id") for m in (s_in.get("measurements") or [])})
    if state.SOLVER_ALT_MODE == "free" and n_meas >= _FREE_ALT_MIN_NODES:
        # No fall back to the sweep when this returns None: a helper that got
        # no solve out of its starts is reporting the same thing the sweep
        # reports when every layer fails, and sweeping anyway would cost the
        # six round trips this mode exists to avoid on exactly the candidates
        # that are least likely to repay them.
        return multistart_fn(s_in, node_cfgs, _free_alt_starts(ig_alt, layers))
    return _sweep_altitudes(s_in, node_cfgs, solve_fn, layers, "rms_delay")


def _solve_best_altitude_n2(s_in: dict, node_cfgs: dict, solve_fn) -> dict | None:
    """Altitude solve for n=2: use the initial_guess altitude from association.

    For n=2 the solver state [x, y, vx, vy, vz] with altitude fixed is:
    - Exactly determined by the 2 delay equations for (x, y)
    - Underdetermined for (vx, vy, vz): 2 Doppler equations, 3 unknowns

    Both rms_delay and rms_doppler are ≈0 at every altitude layer (the solver
    always finds a zero-residual solution within bounds).  Neither metric can
    discriminate altitude.

    The initial_guess.alt_km from association.py is set to the delay-residual
    weighted mean of all candidate altitudes in the group.  When the correct
    altitude layer has smaller delay residuals it is upweighted; when all
    layers tie (high altitude ambiguity), the mean falls back to the middle of
    the ladder, which covers the typical commercial cruise band.

    That is the best a single association round can do, and it is not very
    good: measured over a 20-minute capture against ground truth, n=2 solves
    had a median altitude error of 1.46 km (p90 5.25), and their position
    error was 1.61 km when the altitude landed within 1 km of truth against
    2.71 km when it did not.  But an aircraft that has ALREADY been solved at
    three or more nodes has a measured altitude, not a grid guess — and dark
    targets drop from n>=3 to n=2 coverage constantly.  So before solving,
    look for an established dark key sitting under this input and inherit its
    altitude (``_inherit_key_altitude``); the grid altitude stays the
    fallback.  Which one was used is stamped on the input as ``alt_source``
    so the history records can be split by it.
    """
    _inherit_key_altitude(s_in)
    return solve_fn(s_in, node_cfgs)


def _inherit_key_altitude(s_in: dict, learned_vel_fn=track_filter.learned_velocity) -> None:
    """Point an n=2 input's initial-guess altitude at an established dark key.

    Mutates ``s_in`` in place: sets ``initial_guess["alt_km"]`` when a donor
    is found and always stamps ``alt_source`` — "key" when inherited, "anchor"
    for a follow-lane input (whose guess already carries the anchor's own
    solved altitude, see tasks/known_lane.py's target builder — there is
    nothing to inherit and overwriting it would replace a per-key prediction
    with a neighbour's altitude), "grid" otherwise.

    Donor rules, all three necessary:

    * within ``N2_ALT_INHERIT_KM`` of the guess after being dead-reckoned to
      this solve's epoch — the same DR the keying rule uses
      (``_entry_dr_velocity`` + ``offset_latlon_m``), so a key this solve
      would be allowed to join is a key it may borrow from;
    * ``max_n_nodes`` (or the current ``n_nodes``) at or above
      ``N2_ALT_INHERIT_MIN_NODES``, so the altitude being inherited was
      actually measured rather than being another grid guess;
    * last solved within ``N2_ALT_INHERIT_MAX_AGE_S``, so it is not older
      than the climb it is meant to track.

    Nearest wins when several qualify.  No lock: this runs on the solve path,
    which does not hold state.multinode_tracks_lock here, and taking it would
    put a lock acquisition in front of every n=2 solve to read a dict that the
    keying block rewrites wholesale a moment later anyway.  ``list(...items())``
    snapshots under the GIL, and a donor that goes stale between this read and
    the solve costs an initial guess, not a correctness property.
    """
    ig = s_in.get("initial_guess") if isinstance(s_in, dict) else None
    if not isinstance(ig, dict):
        return
    if s_in.get("anchor_key"):
        s_in["alt_source"] = "anchor"
        return
    s_in["alt_source"] = "grid"
    lat, lon = ig.get("lat"), ig.get("lon")
    if lat is None or lon is None:
        return
    ts_s = float(s_in.get("timestamp_ms") or 0) / 1000.0
    if not ts_s:
        return

    best_alt_m: float | None = None
    best_dist = N2_ALT_INHERIT_KM
    for key, entry in list(state.multinode_tracks.items()):
        if not key.startswith("mn-dark-"):
            continue
        n_nodes = max(int(entry.get("max_n_nodes") or 0), int(entry.get("n_nodes") or 0))
        if n_nodes < N2_ALT_INHERIT_MIN_NODES:
            continue
        alt_m = entry.get("alt_m")
        if alt_m is None:
            continue
        e_lat, e_lon = entry.get("lat"), entry.get("lon")
        if e_lat is None or e_lon is None:
            continue
        dt = ts_s - float(entry.get("timestamp_ms") or 0) / 1000.0
        if not (0.0 <= dt <= N2_ALT_INHERIT_MAX_AGE_S):
            continue
        vel_east_ms, vel_north_ms = multinode_identity._entry_dr_velocity(key, entry, learned_vel_fn)
        d_lat, d_lon = offset_latlon_m(
            e_lat,
            e_lon,
            east_m=vel_east_ms * dt,
            north_m=vel_north_ms * dt,
        )
        d = _haversine_km(float(lat), float(lon), d_lat, d_lon)
        if d < best_dist:
            best_dist, best_alt_m = d, float(alt_m)

    if best_alt_m is None:
        return
    ig["alt_km"] = round(best_alt_m / 1000.0, 3)
    s_in["alt_source"] = "key"
    state.bump_counter("solver_n2_alt_inherited")


def _filter_s_in_to_nodes(s_in: dict, survivors) -> dict:
    """Filter a solver input down to a node subset, rebuilding provenance.

    Shared rebuild idiom: filter ``measurements`` to the surviving node ids,
    recompute ``n_nodes``, and rebuild ``track_ids_by_node``/``track_ids``
    from it when the input carries per-node track provenance (else
    ``track_ids`` passes through unchanged — the supersession block reads
    ``s_in["track_ids"]`` into ``result["source_track_ids"]`` regardless of
    which caller narrowed the node set).  Used by both _trim_and_resolve
    (dropping the worst-residual node after a failed rms gate) and
    _consensus_select (dropping nodes consensus's cross-pair corroboration
    didn't support) — so provenance is identical-by-construction between
    the two.
    """
    survivors = set(survivors)
    measurements = s_in.get("measurements") or []
    s_next = dict(s_in)
    s_next["measurements"] = [m for m in measurements if m.get("node_id") in survivors]
    s_next["n_nodes"] = len({m.get("node_id") for m in s_next["measurements"]})
    by_node = s_in.get("track_ids_by_node")
    if by_node:
        surviving_by_node = {nid: ids for nid, ids in by_node.items() if nid in survivors}
        s_next["track_ids_by_node"] = surviving_by_node
        s_next["track_ids"] = sorted({tid for ids in surviving_by_node.values() for tid in ids})
    return s_next


def _trim_and_resolve(
    s_in: dict,
    node_cfgs: dict,
    solve_fn,
    result: dict,
    multistart_fn=solver_pool._pool_solve_multistart,
) -> tuple[dict, dict, dict | None]:
    """Drop the worst-residual node(s) and re-solve, down to _TRIM_MIN_NODES.

    Called only when a solve has failed the rms_delay gate at n≥4 and carries
    per-node residuals (see _SOLVER_RMS_DELAY_MAX_US).  A contaminated
    measurement inflates rms_delay without moving the Huber-fitted position,
    so re-solving on the survivors after dropping the offending node recovers
    a solve the blanket gate would otherwise discard outright.

    Re-solves through _solve_best_altitude, so it inherits whichever altitude
    mode is in force — the loop compares this round's rms against the previous
    round's, and mixing a swept altitude with a free one would make that
    comparison meaningless.

    Returns (final_result, final_s_in, trim_meta).  trim_meta is None only
    when no round ever produced a successful re-solve — i.e. no trimming was
    actually performed — never when trimming ran but rms stayed high (that
    case still returns the (last-trimmed result, s_in, trim_meta) so the
    caller's gates see the best attempt reached, and the reject record still
    carries what was tried).  A re-solve that raises, returns None, or does
    not converge abandons trimming and returns whatever was reached in the
    previous round — a failed re-solve is never surfaced as the result.
    """
    trimmed_ids: list[str] = []
    trim_meta: dict | None = None
    pre_trim_rms_delay = result.get("rms_delay")
    pre_trim_n_nodes = result.get("n_nodes")

    for round_idx in range(_TRIM_MAX_ROUNDS):
        rms_delay = result.get("rms_delay") or 0
        if rms_delay <= _SOLVER_RMS_DELAY_MAX_US:
            break
        if (result.get("n_nodes") or 0) <= _TRIM_MIN_NODES:
            break
        residuals = result.get("per_node_delay_res_us")
        if not residuals:
            break

        threshold = _TRIM_RESID_FACTOR * rms_delay
        candidates = {nid for nid, res in residuals.items() if res > threshold}
        if not candidates:
            # Nothing clears the factor — the single worst node is still the
            # best lead available.
            candidates = {max(residuals, key=residuals.get)}

        survivors = [nid for nid in residuals if nid not in candidates]
        if len(survivors) < _TRIM_MIN_NODES:
            # Dropping every candidate would starve the fit below the floor:
            # keep the _TRIM_MIN_NODES lowest-residual nodes instead and drop
            # the rest, so a round never re-solves with fewer than that.
            survivors = sorted(residuals, key=residuals.get)[:_TRIM_MIN_NODES]
            candidates = {nid for nid in residuals if nid not in survivors}
        if not candidates:
            break

        s_next = _filter_s_in_to_nodes(s_in, survivors)

        try:
            new_result = _solve_best_altitude(s_next, node_cfgs, solve_fn, multistart_fn)
        except Exception:
            logging.exception("Solver trim re-solve failed")
            break
        if not new_result or not new_result.get("success"):
            break

        trimmed_ids.extend(sorted(candidates))
        trim_meta = {
            "trimmed_node_ids": sorted(set(trimmed_ids)),
            "trim_rounds": round_idx + 1,
            "pre_trim_rms_delay": pre_trim_rms_delay,
            "pre_trim_n_nodes": pre_trim_n_nodes,
        }
        result, s_in = new_result, s_next

    return result, s_in, trim_meta


# ── Pool adoption (dark bottom-up) ───────────────────────────────────────────
# The association round often pairs more nodes for an aircraft than the input
# the solver is handed uses: pool_n_nodes is the node set of the shared-track
# component the input was clustered out of (InterNodeAssociator.
# _shared_track_pools), and 21 minutes of live instrumentation put 72% of
# published dark solves below it, mean shortfall 2.27 nodes.  The costly half
# is at the bottom: 390 of 481 rejected n=2 candidates had a pool of 3+, and
# an n=3 candidate publishes 81% of the time against 6% for n=2.  That is the
# largest single block of detections the pipeline throws away.
#
# The fix is NOT to merge more aggressively upstream — sweeping the position
# merge radius to 6 km raised cross-aircraft contamination from 25% to 32%,
# because at that radius two aircraft are as close as one aircraft's own
# pairings.  Instead the solve itself vouches for the extra node: predict what
# that node should have measured at the position and velocity just solved, and
# adopt it only if what it actually measured agrees.  A node that agrees on
# delay AND Doppler at an independently solved point is evidence about the
# same aircraft, which is precisely what the n=2 confirmation gate is asking
# for and cannot get from two nodes alone.
#
# Gate defaults.  An n=2 dark solve sits ~2 km from truth, and 2 km of range
# error is 2/c ≈ 6.7 µs of bistatic delay, so 6.0 µs admits a genuine node at
# the accuracy this stage actually has without opening the door to an
# unrelated target (a wrong aircraft is normally tens of µs away).  60 Hz on
# Doppler is the same order as the n=2 velocity error against a ~600 MHz
# carrier.  Both are env-tunable, and SOLVER_ADOPT_POOL=0 turns the whole
# stage off.
_ADOPT_POOL_ENABLED = os.getenv("SOLVER_ADOPT_POOL", "1").strip().lower() not in ("0", "false", "off")
_ADOPT_DELAY_GATE_US = float(os.getenv("SOLVER_ADOPT_DELAY_GATE_US", "6.0"))
_ADOPT_DOPPLER_GATE_HZ = float(os.getenv("SOLVER_ADOPT_DOPPLER_GATE_HZ", "60"))
# A widened solve that lands more than this from the narrow one is not the
# same aircraft solved better — it is a different geometry the adopted node
# dragged the fit into, and keeping it would publish a position no measurement
# in the original input supports.
_ADOPT_MAX_JUMP_KM = float(os.getenv("SOLVER_ADOPT_MAX_JUMP_KM", "5.0"))


def _s_in_with_adopted(s_in: dict, adopted: list[dict], first: dict) -> dict:
    """s_in plus the adopted pool measurements, ready to re-solve.

    Pure — s_in is never mutated, so a rejected widening leaves the caller
    holding the untouched input.  The initial guess becomes the FIRST solve's
    position rather than the association grid centroid it came in with: the
    narrow solve is the best estimate anything has of where this aircraft is,
    and it is also the point the adopted measurements were just gated against,
    so starting anywhere else would judge them from a different place than the
    one that admitted them.
    """
    s_wide = dict(s_in)
    meas = [dict(m) for m in (s_in.get("measurements") or [])]
    meas.extend({k: m.get(k) for k in ("node_id", "delay_us", "doppler_hz", "snr", "t_s")} for m in adopted)
    s_wide["measurements"] = meas
    s_wide["n_nodes"] = len({m.get("node_id") for m in meas})
    # Provenance follows the measurement: a published solve names the tracklets
    # it was built from, and an adopted node's track is now one of them.
    by_node = {nid: list(ids) for nid, ids in (s_in.get("track_ids_by_node") or {}).items()}
    for m in adopted:
        tid = m.get("track_id")
        if tid and tid not in by_node.setdefault(m["node_id"], []):
            by_node[m["node_id"]].append(tid)
    s_wide["track_ids_by_node"] = {nid: sorted(ids) for nid, ids in by_node.items()}
    s_wide["track_ids"] = sorted(set(s_in.get("track_ids") or []) | {t for ids in by_node.values() for t in ids})
    s_wide["initial_guess"] = {
        "lat": first.get("lat"),
        "lon": first.get("lon"),
        "alt_km": float(first.get("alt_m") or 0.0) / 1000.0,
    }
    if first.get("vel_east") is not None:
        s_wide["initial_velocity"] = {
            "vel_east_ms": first.get("vel_east"),
            "vel_north_ms": first.get("vel_north"),
        }
    return s_wide


def _adopt_pool_nodes(
    s_in: dict,
    node_cfgs: dict,
    result: dict,
    solve_fn,
    multistart_fn=solver_pool._pool_solve_multistart,
) -> tuple[dict, dict, dict | None]:
    """Widen a narrow dark solve with pool nodes the solve itself vouches for.

    Runs immediately after the first successful solve and BEFORE trimming and
    before every gate, so a candidate that adopts a third node is judged as an
    n=3 solve throughout — including by the n=2 confirmation gate, which no
    longer applies to it.  That is the point rather than a side effect: the
    gate exists because two nodes cannot corroborate each other's identity,
    and a third node whose measured delay matches what the two-node solve
    predicts for it is exactly the corroboration it was demanding.

    Only bottom-up dark inputs qualify.  Anchored and known-lane inputs have a
    transponder identity and were never clustered by
    format_track_pairs_for_solver, so they carry no pool to adopt from.

    Returns (result, s_in, meta).  meta is None when the stage did not apply
    at all; otherwise it records what was tried, so a history record can be
    read for adoption's true and false positives rather than just its count.
    Cost is bounded at two extra LM solves per eligible candidate (~60-90 ms
    each in the pool): the first widening, plus at most one retry with the
    worst adopted node dropped.
    """
    if not _ADOPT_POOL_ENABLED or not isinstance(s_in, dict) or not isinstance(result, dict):
        return result, s_in, None
    if not displacement_caps._is_dark_solver_input(s_in) or s_in.get("anchor_key"):
        return result, s_in, None
    pool_n = s_in.get("pool_n_nodes")
    pool_meas = s_in.get("pool_measurements") or []
    if not pool_meas or not pool_n or (result.get("n_nodes") or 0) >= pool_n:
        return result, s_in, None
    lat, lon = result.get("lat"), result.get("lon")
    if lat is None or lon is None:
        return result, s_in, None
    have = {m.get("node_id") for m in (s_in.get("measurements") or [])}
    cands = [m for m in pool_meas if m.get("node_id") not in have and m.get("delay_us") is not None]
    if not cands:
        return result, s_in, None

    state.bump_counter("solver_adopt_eligible")
    meta: dict = {"pool_n": int(pool_n), "candidates": len(cands), "adopted_node_ids": [], "outcome": "none_passed"}
    alt_km = float(result.get("alt_m") or 0.0) / 1000.0
    vel_east = float(result.get("vel_east") or 0.0)
    vel_north = float(result.get("vel_north") or 0.0)
    geometries = state.node_associator.node_geometries if state.node_associator else {}

    adopted: list[dict] = []
    pred_resid: dict[str, float] = {}
    for m in cands:
        geo = geometries.get(m["node_id"])
        if geo is None:
            # Same abstention rule as everywhere else: a node with no
            # registered geometry cannot be predicted for, so it is not
            # adopted rather than adopted unchecked.
            continue
        try:
            pred_delay, pred_doppler = predict_observation(geo, lat, lon, alt_km, vel_east, vel_north)
        except Exception:
            logging.exception("Solver pool adoption: prediction failed for node %s", m["node_id"])
            continue
        d_delay = abs(float(m["delay_us"]) - pred_delay)
        if d_delay > _ADOPT_DELAY_GATE_US:
            continue
        # Doppler abstains rather than blocks when the pool pairing carried
        # none: the delay agreement is the stronger of the two claims and a
        # missing measurement is not a disagreement.
        if m.get("doppler_hz") is not None and abs(float(m["doppler_hz"]) - pred_doppler) > _ADOPT_DOPPLER_GATE_HZ:
            continue
        adopted.append(m)
        pred_resid[m["node_id"]] = d_delay

    if not adopted:
        state.bump_counter("solver_adopt_rejected")
        return result, s_in, meta

    for attempt in range(2):
        meta["adopted_node_ids"] = sorted(m["node_id"] for m in adopted)
        s_wide = _s_in_with_adopted(s_in, adopted, result)
        if state.SOLVER_EPOCH_ALIGN:
            s_wide, _ = align_measurement_epochs(s_wide, node_cfgs)
        try:
            wide = _solve_best_altitude(s_wide, node_cfgs, solve_fn, multistart_fn)
        except Exception:
            logging.exception("Solver pool adoption re-solve failed")
            wide = None
        if not wide or not wide.get("success"):
            meta["outcome"] = "rejected_solve"
            break
        rms_delay = wide.get("rms_delay") or 0
        if rms_delay > _SOLVER_RMS_DELAY_MAX_US:
            # One cheap second chance, and only one: with two or more adopted
            # nodes the rms is a sum over both, so a single contaminated
            # adoption can sink a widening the other node would have carried.
            # Beyond that, keep the narrow solve — this is a bonus path, not a
            # search.
            if attempt == 0 and len(adopted) >= 2:
                residuals = wide.get("per_node_delay_res_us") or {}
                worst = max(adopted, key=lambda m: residuals.get(m["node_id"], pred_resid[m["node_id"]]))
                meta["dropped_node_id"] = worst["node_id"]
                adopted = [m for m in adopted if m["node_id"] != worst["node_id"]]
                continue
            meta["outcome"] = "rejected_rms"
            meta["wide_rms_delay"] = round(float(rms_delay), 3)
            break
        jump_km = _haversine_km(float(lat), float(lon), float(wide["lat"]), float(wide["lon"]))
        if jump_km > _ADOPT_MAX_JUMP_KM:
            meta["outcome"] = "rejected_jump"
            meta["jump_km"] = round(jump_km, 2)
            break
        meta["outcome"] = "widened"
        meta["jump_km"] = round(jump_km, 2)
        meta["wide_rms_delay"] = round(float(rms_delay), 3)
        state.bump_counter("solver_adopt_widened")
        state.bump_counter("solver_adopt_nodes_added", len(adopted))
        return wide, s_wide, meta

    meta["adopted_node_ids"] = sorted(m["node_id"] for m in adopted)
    state.bump_counter("solver_adopt_rejected")
    return result, s_in, meta


def _reset_for_tests() -> None:
    """Restore the state a solve writes outside core.state to boot values.  Tests only."""
    global _recent_solves_last_sweep
    with _TRACK_CLAIMS_LOCK:
        _TRACK_CLAIMS.clear()
    with _RECENT_SOLVES_LOCK:
        _RECENT_SOLVES.clear()
        _recent_solves_last_sweep = 0.0
    multinode_identity._reset_for_tests()
    track_filter.reset()


# Maximum age (seconds) of a solver queue item before it is discarded without
# solving.  Items older than this are already stale — the multinode_tracks
# expiry is 60 s, and a solve itself can take a few seconds — so spending CPU
# on them can never produce a visible result.  Raising this number allows a
# deeper backlog but increases latency; lowering it drops items too aggressively.
_SOLVER_MAX_QUEUE_AGE_S = 45.0

# ── Re-solve suppression ─────────────────────────────────────────────────────
# Association is rate-limited per *node*, not per aircraft: every node that can
# see an aircraft emits its own candidate for it inside one
# ASSOC_MIN_INTERVAL_S window, and the fleet's rounds are staggered across that
# window, so one aircraft arrives as several near-identical candidates.
# Measured on the test deployment's fleet shape (50 nodes, 25-40 aircraft,
# 30 s association interval): 139 candidates over 240 s carrying 49 distinct
# (aircraft, window) pairs — 2.84 candidates per aircraft per window, against a
# solver that drains ~0.7/s.
#
# Solving all of them buys nothing.  The extra solves are superseded by the
# next one for the same aircraft (state.mn_superseded), while the queue ages
# past _SOLVER_MAX_QUEUE_AGE_S and aircraft with no solve at all wait behind
# them — the coverage collapse this suppression exists to undo.
#
# A candidate is skipped when EVERY single-node track it is built from was
# already solved within this window at no fewer nodes.  Both halves matter:
# "every track" means a candidate carrying a track nothing has solved yet
# always runs, so an aircraft entering coverage is never suppressed; "no fewer
# nodes" means a wider — better conditioned — view of the same aircraft is
# never held back by a narrower one that happened to arrive first.
#
# "Every" and not "any", which is the rule supersession uses downstream and the
# obvious way to write this.  Measured over 579 candidates / 600 s of the test
# fleet's shape, against a baseline that solves every candidate instantly,
# counting (aircraft, 30 s window) slots that still get at least one solve:
#
#     rule    candidates solved   coverage kept
#     any            21%              58.5%
#     every          47%              87.8%
#
# Duplicates for one aircraft usually carry *overlapping* track sets rather
# than identical ones — each node's round pairs its own tracks with a rotating
# slice of its neighbours' — so "any" also swallows the candidate that is the
# first sighting of a genuinely different aircraft sharing one contaminated
# track.  Half the extra saving comes out of coverage, which is the thing this
# is for.
#
# Sized against the map, not the association cadence: multinode_tracks expire
# at 60 s, so refreshing an aircraft every 12 s leaves four solves' worth of
# margin.  0 disables the suppression entirely.
#
# The claim is recorded ON PUBLICATION, not on admission, and from the
# POST-TRIM survivors.  Claiming on admission made a candidate that never
# reached the map suppress every later candidate sharing any of its track ids
# for the full window — including other aircraft's, since tracker track ids
# are shared across the association candidates of different aircraft (74 of
# 178 ids in a 6 min live window appeared in solves of more than one
# ground-truth aircraft; the same finding that forced _supersession_match's
# spatial guard).  A rejected candidate, or a contaminated superset that the
# gates sank, therefore blacked out the clean subsets behind it for 12 s and
# nothing was refreshed at all.  Live that cost ~1 537 skips per 646 dark
# attempts per 30 min — more candidates suppressed than solved, by a factor
# of two.  The rule this suppression is FOR is "an aircraft already on the map
# at this width does not need re-solving yet", and only a publication puts an
# aircraft on the map.
#
# Two consequences, both accepted deliberately:
#   * the check no longer claims under the same lock, so two workers can now
#     both solve duplicates of one aircraft that arrived together.  The pair
#     costs one extra solve and is resolved downstream by keying and
#     supersession, which already handle exactly this; the alternative is the
#     starvation above.
#   * trimmed nodes' track ids are NOT claimed (_filter_s_in_to_nodes rebuilds
#     track_ids from the surviving track_ids_by_node, so result's
#     source_track_ids are the survivors).  A node dropped for a bad residual
#     was probably another aircraft's — claiming its track would suppress that
#     aircraft's own candidate on the strength of a measurement this solve
#     threw away.
#
# One exception, sized separately below: at 3 or more nodes a claim older than
# _SOLVER_RESOLVE_REFRESH_S stops covering a candidate even at equal width.
# The rule this suppression enforces is "already on the map at this width",
# and that stays true for the whole window — but the map entry it points at
# is dead-reckoning, so late in the window "already on the map" and "in the
# right place" have come apart.
_SOLVER_RESOLVE_INTERVAL_S = float(os.getenv("SOLVER_RESOLVE_INTERVAL_S", "12"))


# When a claim stops covering a candidate that is wide enough to be worth
# re-solving.  Dark position error against ground truth grows with the age of
# the solve behind the entry (test fleet, median error by solve age):
#
#     <= 3 s    0.34 km
#     3 – 8 s   0.80 km
#     8 – 12 s  1.10 km
#
# and for an aircraft the dark-follow lane does not carry — every 3-node
# aircraft — a re-solve is the only thing that refreshes the entry at all.
# Live over a 5 min window, 487 of 500 dark skips were blocked by a
# wider-or-equal claim, 118 of them at 3 nodes, and the blocking claim was
# median 3.9 s / p90 9.0 s old when it did the blocking: the candidates being
# thrown away are arriving exactly where the error curve above turns.  So a
# candidate at 3+ nodes is admitted once every claim blocking it has aged past
# this, whatever the claim's width.
#
# 3 nodes and not 2 deliberately.  An n=2 candidate publishes only 6–9% of the
# time and lands 2.7 km from truth when it does, which is worse than letting
# the 0.34 km solve it would displace dead-reckon; n <= 2 therefore stays on
# the full _SOLVER_RESOLVE_INTERVAL_S rule.  0 disables the refresh entirely
# and restores that rule at every width.
_SOLVER_RESOLVE_REFRESH_S = float(os.getenv("SOLVER_RESOLVE_REFRESH_S", "6"))
_RECENT_SOLVES: dict[str, tuple[float, int]] = {}  # track_id → (solved_at, n_nodes)
_RECENT_SOLVES_LOCK = threading.Lock()
_recent_solves_last_sweep = 0.0


def _sweep_recent_solves(now_s: float) -> None:
    """Drop track ids whose claim has aged out.  Caller holds the lock."""
    global _recent_solves_last_sweep
    if now_s - _recent_solves_last_sweep < _SOLVER_RESOLVE_INTERVAL_S:
        return
    _recent_solves_last_sweep = now_s
    cutoff = now_s - _SOLVER_RESOLVE_INTERVAL_S
    for tid in [tid for tid, (ts, _) in _RECENT_SOLVES.items() if ts <= cutoff]:
        del _RECENT_SOLVES[tid]


def _resolve_slot_state(s_in, now_s: float) -> tuple[bool, list[dict], bool]:
    """The whole resolve-slot decision: (covered, blocking, refreshed).

    Pure, like the _resolve_slot_covered wrapper below it, and for the same
    reason.  Split out from that wrapper so the caller can tell the two ways
    of being admitted apart: a candidate no claim covers at all, and a 3+-node
    candidate admitted only because every claim on it had aged past
    _SOLVER_RESOLVE_REFRESH_S (``refreshed``, counted as
    solver_resolve_refresh).  Deciding that at the call site instead would
    mean a second pass over the same claims under the same lock.

    ``blocking`` is the claims that covered the candidate, for the skip
    record; it is empty whenever ``covered`` is False, refreshed or not — a
    refreshed candidate is not skipped, so nothing records it.  An input with
    no track provenance (detection-level, or an anchored input carrying none)
    is never covered: there is nothing to match it against.
    """
    if _SOLVER_RESOLVE_INTERVAL_S <= 0 or not isinstance(s_in, dict):
        return False, [], False
    track_ids = s_in.get("track_ids")
    if not track_ids:
        return False, [], False
    n_nodes = int(s_in.get("n_nodes") or 0)
    cutoff = now_s - _SOLVER_RESOLVE_INTERVAL_S
    # Claims at or before this are old enough for a wide candidate to refresh
    # the map entry they stand for.  Starts true for a wide enough candidate
    # and is cleared by the first claim too young to refresh.
    refresh_cutoff = now_s - _SOLVER_RESOLVE_REFRESH_S
    refreshed = _SOLVER_RESOLVE_REFRESH_S > 0 and n_nodes >= 3
    blocking: list[dict] = []
    with _RECENT_SOLVES_LOCK:
        for tid in track_ids:
            held = _RECENT_SOLVES.get(tid)
            if held is None or held[0] <= cutoff or held[1] < n_nodes:
                return False, [], False
            if held[0] > refresh_cutoff:
                refreshed = False
            blocking.append({"track_id": tid, "held_ts": round(held[0], 3), "held_n": held[1]})
    if refreshed:
        return False, [], True
    return True, blocking, False


def _resolve_slot_covered(s_in, now_s: float) -> tuple[bool, list[dict]]:
    """Is every track this candidate carries already ON THE MAP at this width?

    Pure: it reads the claims and mutates nothing, so a candidate that is
    admitted here and then rejected by the gate stack leaves no trace.  The
    claim is made afterwards by _record_resolve_slot, from the publish path
    only — see the block comment above for why, and for what the loss of
    atomic test-and-claim costs.

    Returns (covered, blocking) from _resolve_slot_state, dropping the
    refresh flag: the answer to "must this candidate be skipped", for callers
    that do not count the refresh.
    """
    covered, blocking, _refreshed = _resolve_slot_state(s_in, now_s)
    return covered, blocking


def _record_resolve_slot(track_ids, n_nodes: int, now_s: float) -> None:
    """Record that ``track_ids`` are covered by a PUBLISHED solve at n_nodes.

    Called from the publish path alone, with the post-trim survivors
    (``result["source_track_ids"]``).  Nothing else may call it: a claim is a
    statement that this aircraft is on the map, and a rejected solve puts
    nothing there.
    """
    if _SOLVER_RESOLVE_INTERVAL_S <= 0 or not track_ids:
        return
    n_nodes = int(n_nodes or 0)
    cutoff = now_s - _SOLVER_RESOLVE_INTERVAL_S
    with _RECENT_SOLVES_LOCK:
        for tid in track_ids:
            held = _RECENT_SOLVES.get(tid)
            # Keep the widest claim of the window: a narrow publish after a
            # wide one must not lower the bar the next copy is tested against.
            held_nodes = held[1] if held is not None and held[0] > cutoff else 0
            _RECENT_SOLVES[tid] = (now_s, max(n_nodes, held_nodes))
        _sweep_recent_solves(now_s)


def _record_resolve_skip(s_in, now_s: float, blocking: list[dict]) -> None:
    """Count and remember one resolve-slot refusal.

    The counter alone could not answer the question the suppression rule
    raises — *whose* claim blocked this, and was it even the same aircraft.
    Live on the test droplet the rule refuses ~1 537 candidates per 646 dark
    attempts per 30 min, and nothing recorded which claim did it, so a skip
    that suppressed a genuinely different aircraft (tracker track ids are
    shared across candidates — see _supersession_match) was indistinguishable
    from one that suppressed a duplicate.  The deque carries the blocking
    claims and the candidate's own guess position so the two can be told apart
    after the fact.

    Deliberately NOT a solve-history record: skips outrun real dark records
    roughly two to one, and writing them into that deque would evict the
    solves the same investigation needs (see state.solver_resolve_skips_recent).
    """
    s = s_in if isinstance(s_in, dict) else {}
    track_ids = list(s.get("track_ids") or [])
    dark = displacement_caps._is_dark_solver_input(s)
    state.bump_counter("solver_resolve_skips")
    if dark:
        state.bump_counter("solver_resolve_skips_dark")
    ig = s.get("initial_guess") or {}
    state.solver_resolve_skips_recent.append(
        {
            "ts_ms": int(now_s * 1000),
            # No key is minted for a candidate that never solves, so lane is
            # the same fallback solver_report._record_lane uses for a reject.
            "lane": "dark" if dark else "adsb",
            "track_ids": track_ids,
            "n_nodes": int(s.get("n_nodes") or 0),
            "blocking": blocking,
            "guess_lat": round(float(ig["lat"]), 6) if ig.get("lat") else None,
            "guess_lon": round(float(ig["lon"]), 6) if ig.get("lon") else None,
        }
    )


# Which single-node track pair currently owns a published n=2 track, and how
# well it fitted.  One track is one aircraft, so two pairings sharing a track
# are mutually exclusive; the better chi2 wins and the loser is withheld.
#
# On the frame path this was a sort within one association round.  Here the
# pairings arrive as separate queue items, possibly on different worker
# threads, so ownership is a claim against shared state instead — same rule,
# different shape.  Entries expire on the same window the map does, so a claim
# cannot outlive the track it was made for.
_TRACK_CLAIMS: dict[str, tuple[float, float]] = {}  # track_id → (chi2, claimed_at)
_TRACK_CLAIMS_LOCK = threading.Lock()
_TRACK_CLAIM_TTL_S = 60.0


def _claim_track_pair(s_in: dict, chi2: float) -> bool:
    """Take ownership of both single-node tracks, or refuse if outbid.

    The held score is a **best-ever high-water mark**, not the holder's current
    score, and a refusal deliberately does not refresh it.  Two consequences,
    both measured rather than assumed:

    1. A pairing can be refused by its *own* earlier claim.  chi2 is refitted
       every association round over a growing epoch set, so a stable winning
       pairing's score wanders — median drift +0.11 between rounds — and
       upward drift loses to its own high-water mark until the TTL expires.
       Self-lockouts are roughly 30% of all refusals (707 of 2,403 pooled
       over 6 seeds); the rest are genuine competitor losses.

    2. It is nonetheless the better behaviour.  Letting a pairing renew its own
       claim at a worse score raises the bar competitors must beat, so tracks
       change hands more often and every hand-over publishes another track.
       Measured offline, blind, dual/vhf, 6 seeds, pooled AND paired by seed
       (association_bench.py --claim-policy self-refresh): never better on any
       seed, worse on 5 of 6, +3.2 pooled points of track ghost rate
       (73.4% -> 76.6%) and +9.5 at n=2-only (76.2% -> 85.7%; false n=2
       tracks 16 -> 30), at no gain in real tracks (29 both ways).
       Competitor refusals rose 1,696 -> 1,854 — the churn showing up
       directly.

    So the high-water mark is hysteresis: a pairing must keep fitting at least
    as well as its own best to stay published, and a pairing whose fit is
    degrading is one whose accumulated evidence is turning against it.  Keep it,
    and do not "fix" the self-refusal without re-running that sweep.

    Against no arbitration at all (--claim-policy off) the claim is worth
    8.7 pooled points of track ghost rate (82.1% -> 73.4%), better or tied
    on every seed, and cuts false n=2-only tracks 63 -> 16.

    (Earlier revisions of this docstring quoted "refusals 1 -> 21" and
    "worth 3.7 points": both were read off a --repeat run that printed only
    the LAST seed's counters — see Result.merge in association_bench.py.
    The numbers above are pooled across all six seeds, measured after the
    stage-1 simulation fixes changed the scenes.)
    """
    pairs = s_in.get("track_pair_ids") or []
    if not pairs:
        return True  # detection-level input: nothing to arbitrate
    # Note this claims only the first pair of a cluster: format_track_pairs_for_solver
    # truncates track_pair_ids to [:1], so a cluster spanning three tracks leaves
    # the third unclaimed.  s_in["track_ids"] carries the full set if that is
    # ever worth closing — see --claim-policy all-tracks.
    track_ids = [tid for pair in pairs for tid in pair]
    with _TRACK_CLAIMS_LOCK:
        return claim_decision(_TRACK_CLAIMS, track_ids, chi2, time.time(), _TRACK_CLAIM_TTL_S)


def claim_decision(
    claims: dict[str, tuple[float, float]],
    track_ids: list[str],
    chi2: float,
    now: float,
    ttl_s: float,
) -> bool:
    """The claim rule itself, pure and clock-free: expire, compare, record.

    Extracted so the offline bench measures the SHIPPED rule by construction
    (association_bench.DeferredN2Gate used to carry its own copy — the exact
    drift the bench exists to rule out).  Caller holds whatever lock guards
    `claims`; production passes _TRACK_CLAIMS under _TRACK_CLAIMS_LOCK, the
    bench passes its own dict on simulated time.
    """
    for tid, (_held_chi2, held_at) in list(claims.items()):
        if now - held_at > ttl_s:
            del claims[tid]
    for tid in track_ids:
        held = claims.get(tid)
        if held is not None and held[0] < chi2:
            return False
    for tid in track_ids:
        claims[tid] = (chi2, now)
    return True


def _resolve_cv_fit(s_in: dict, node_cfgs, *, fix_altitude: bool = False) -> dict | None:
    """Constant-velocity fit over the whole observation window, cached.

    Fits one constant-velocity trajectory to the associated track pairing's
    full epoch history — an ~86 ms LM solve.  Association runs inside the
    frame worker, so doing it there is frame latency: measured on staging at
    92% frame-queue depth with the processor 21 s behind a 6 frame/s feed.
    This worker already has its own threads and a queue with a staleness
    drop, which is exactly the place for it — so association hands over the
    epochs and the fit happens on this side.

    Cached on the input as ``s_in["_cv_fit"]`` so a caller reached twice for
    the same solve (the n=2 confirmation gate, then velocity adoption) pays
    for the pool call once.  ``chi2_per_dof``/``n_epochs`` are cached onto
    ``s_in`` exactly as before this fit dict existed, so association-fitted
    inputs (chi2_per_dof arrives already set) keep short-circuiting through
    _resolve_n2_chi2 without ever reaching here — which is also the one case
    this function cannot reconstruct a fit dict for: with the fit already
    done inline, no epochs survive on the input to refit from.

    ``fix_altitude`` selects the pinned 4-state fit (z held at the initial
    guess, vz = 0) and caches it under a SEPARATE key, ``_cv_fit_pinned``.
    The two fits answer different questions over the same epochs — the free one
    is the confirmation test, the pinned one is the position (see
    _N2_FIT_FIX_ALTITUDE) — and they have different dof, so the pinned fit
    deliberately does not write chi2_per_dof/n_epochs back onto the input:
    those feed the n=2 gate, whose threshold was calibrated against the
    6-state.
    """
    cache_key = "_cv_fit_pinned" if fix_altitude else "_cv_fit"
    cached = s_in.get(cache_key)
    if cached is not None:
        return cached
    epochs = s_in.get("cv_epochs")
    if not epochs or not isinstance(node_cfgs, dict):
        return None
    try:
        from retina_geolocator.multinode_solver import fit_constant_velocity

        fit = solver_pool._pool_call(
            fit_constant_velocity,
            {
                "initial_guess": s_in.get("initial_guess"),
                "initial_velocity": s_in.get("initial_velocity"),
                "epochs": epochs,
                "timestamp_ms": s_in.get("timestamp_ms", 0),
            },
            node_cfgs,
            fix_altitude=fix_altitude,
        )
    except Exception:
        logging.exception("constant-velocity fit failed")
        return None
    if not fit or not fit.get("success"):
        return None
    s_in[cache_key] = fit
    if fix_altitude:
        return fit
    # Cache it so a retry of the same input does not refit.
    s_in["chi2_per_dof"] = fit["chi2_per_dof"]
    s_in["n_epochs"] = fit["n_epochs"]
    return fit


def _resolve_n2_chi2(s_in: dict, node_cfgs) -> float | None:
    """chi2/dof for an n=2 pairing, fitting here if association deferred it.

    Association may fit inline (the offline bench does, having no queue), in
    which case chi2_per_dof arrives already set and this is a no-op.
    """
    chi2 = s_in.get("chi2_per_dof")
    if chi2 is not None:
        return chi2
    fit = _resolve_cv_fit(s_in, node_cfgs)
    return fit["chi2_per_dof"] if fit else None


# Public alias: the offline bench resolves chi2 through the same code path the
# worker uses, rather than carrying a copy of the fit plumbing.
resolve_n2_chi2 = _resolve_n2_chi2


def _n2_anchor_admits(s_in: dict) -> bool:
    """True if this n=2 input is an anchored follow of an established track.

    The n=2 confirmation gate below is a defence against pairing two
    single-node tracks that belong to DIFFERENT aircraft, and the
    constant-velocity fit is how it decides they are one.  An anchored input
    has already answered that question by a stronger route: top-down claiming
    tested each detection against the followed track's own predicted delay and
    Doppler (services/dark_follow.py), and the track it was tested against has
    at least DARK_FOLLOW_MIN_SOLVES solves behind it.  Re-asking with a fit
    there is redundant, and it is not a cheap redundancy — measured on the test
    droplet, 14 follow inputs per capture were rejected n2_unconfirmed, and two
    consecutive rejects drop the followed target, so an aircraft that flew out
    of 3-node coverage was dropped rather than followed through it.

    Deliberately narrow: the anchor must still name a live ``mn-dark-*`` entry
    with the solve count that made it followable in the first place, so a stale
    or freshly minted key admits nothing.  Bumps the counter here rather than
    at the call site so the gate itself stays a single expression; the counter
    is the rate to watch if the bypass ever needs turning off.
    """
    if not dark_follow.DARK_FOLLOW_N2_ADMIT:
        return False
    anchor_key = s_in.get("anchor_key")
    if not anchor_key or not str(anchor_key).startswith("mn-dark-"):
        return False
    rec = state.multinode_tracks.get(anchor_key)
    if not isinstance(rec, dict):
        return False
    if int(rec.get("solve_count") or 0) < dark_follow.DARK_FOLLOW_MIN_SOLVES:
        return False
    state.bump_counter("n2_anchored_admitted")
    return True


def _apply_n2_fit_position(s_in: dict, result: dict, node_cfgs) -> None:
    """Swap in the constant-velocity fit's position on a published n=2 solve.

    Called only after the confirmation gate above has passed the pairing, so
    the epochs this refits are the ones that decided the pairing is a single
    aircraft — and on the anchored bypass path, where the claim round decided
    that instead and the fit is used for position alone.

    With _N2_FIT_FIX_ALTITUDE (the default) this is a SECOND fit of those same
    epochs, with altitude pinned to the solve's own initial guess.  The gate
    keeps the free-z fit it was calibrated against; only the position comes
    from the pinned one.  Altitude is never taken from a free fit at all — see
    the assignment below.

    Runs deliberately AFTER every gate, and changes none of them.  The beam and
    displacement gates ran on the LM position because that is the position
    their caps were tuned against (an anchored n=2 is judged at 1.5 km, see
    _DARK_FOLLOW_N2_MAX_DISP_KM), and the track-pair claim is decided on the
    fit's chi2, not on either position.  The only thing that changes here is
    which position gets published; rms_delay, rms_doppler and n_nodes stay the
    single-epoch solve's, since they describe that solve and not this one.

    The LM position is kept on the result as solve_raw_lat/solve_raw_lon (and
    stamped into the history record) so the swap is measurable live rather than
    only in the offline bench: gt_error split by pos_source over published n=2
    records is the whole experiment.

    Downstream, track_filter.smooth_solve consumes result lat/lon like any
    other solve, so kf_pos_sigma_m now reflects the fit position — which is
    the intent, the smoother should be seeing the better estimate.
    """
    # Stamped for every confirmed n=2 whether or not the swap happens, so an
    # absent fit is distinguishable in the dump from a fit that was used.
    result["solve_raw_lat"] = result.get("lat")
    result["solve_raw_lon"] = result.get("lon")
    result["pos_source"] = "solve"
    if not _N2_PUBLISH_FIT_POSITION:
        return
    fit = _resolve_cv_fit(s_in, node_cfgs, fix_altitude=_N2_FIT_FIX_ALTITUDE)
    if not fit or not fit.get("success"):
        # No fit to publish.  Normal, not an error: a pairing association
        # fitted inline arrives with chi2_per_dof already set and no cv_epochs
        # to refit from (see _resolve_cv_fit), and an anchored input that never
        # carried epochs has nothing here either.  Those keep the LM position.
        return
    if fit.get("lat") is None or fit.get("lon") is None or fit.get("alt_m") is None:
        return
    if (fit.get("n_epochs") or 0) < N2_CONFIRM_MIN_EPOCHS:
        return
    lat = float(fit["lat"])
    lon = float(fit["lon"])
    # The fit evaluates its trajectory at its OWN last epoch, which is normally
    # this solve's measurement epoch — so this is usually a no-op.  But they are
    # two separate inputs and nothing enforces that, and publishing a position
    # from a different instant than the timestamp it is published under is
    # exactly the error the epoch-alignment work above exists to prevent, so
    # propagate on the fit's own velocity rather than assuming.
    _fit_ts = float(fit.get("timestamp_ms") or 0)
    _res_ts = float(result.get("timestamp_ms") or 0)
    if _fit_ts > 0 and _res_ts > 0 and _fit_ts != _res_ts:
        dt_s = (_res_ts - _fit_ts) / 1000.0
        lat, lon = offset_latlon_m(
            lat,
            lon,
            east_m=float(fit.get("vel_east") or 0.0) * dt_s,
            north_m=float(fit.get("vel_north") or 0.0) * dt_s,
        )
    result["fit_vs_solve_km"] = _haversine_km(float(result["lat"]), float(result["lon"]), lat, lon)
    result["lat"] = lat
    result["lon"] = lon
    # Altitude comes from the fit ONLY when the fit was told to pin it — in
    # which case it is the solve's own pinned altitude anyway, and the
    # assignment is a no-op kept for symmetry.  A FREE fit's altitude is never
    # published: at n=2 it is an unobservable direction the optimiser has
    # filled with noise, and it measured worse (4.57 km median |alt - truth|)
    # than the ladder guess the solve pinned (2.94 km).  Position is worth
    # taking from the fit because 4K measurements beat 4; altitude is not,
    # because neither of them constrains it.
    fit_alt_fixed = bool(fit.get("altitude_fixed"))
    if fit_alt_fixed:
        result["alt_m"] = float(fit["alt_m"])
    result["fit_altitude_fixed"] = fit_alt_fixed
    result["pos_source"] = "cv_fit"
    state.bump_counter("n2_fit_position_published")


def _consensus_select(
    s_in: dict,
    node_cfgs: dict,
    select_fn,
) -> tuple[dict, dict | None]:
    """Run the consensus hypothesis stage once, ahead of the LM altitude
    sweep, and decide whether to act on its selection.

    Called only at n>=3 with an initial_guess and _CONSENSUS_MODE != "off"
    (see the dispatch in _process_solver_item) — n=2 and detection-level
    (no-guess) inputs never reach this.  select_fn is select_consensus by
    default, routed through the spawn pool exactly like solve_fn; tests
    substitute a stub.

    Every non-selecting outcome (an exception, an abstain, or too few
    corroborated nodes) returns the ORIGINAL s_in unchanged — a consensus
    failure must never block a solve the unfiltered input could still
    produce.  initial_guess is never touched here either way: the
    displacement gate downstream measures the LM's own solve against the
    association guess regardless of what consensus decided, and
    centroid_offset_km in the meta is the observability channel for how
    often the two disagree.

    Returns (s_in_for_solve, consensus_meta).  consensus_meta is always a
    dict (never None) once this runs, carrying at least "outcome" —
    _process_solver_item threads it into every history record for this
    item, published or rejected.
    """
    try:
        selection = select_fn(s_in, node_cfgs)
    except Exception:
        logging.exception("Consensus selection failed")
        state.bump_counter("solver_consensus_fallback")
        return s_in, {"outcome": "fallback_error"}

    if selection is None:
        state.bump_counter("solver_consensus_fallback")
        return s_in, {"outcome": "fallback_abstained"}

    node_ids = selection.get("node_ids") or []
    if len(node_ids) < _CONSENSUS_MIN_NODES:
        meta = dict(selection)
        meta["outcome"] = "fallback_small"
        state.bump_counter("solver_consensus_fallback")
        return s_in, meta

    input_node_ids = selection.get("input_node_ids") or []
    meta = dict(selection)
    meta["input_node_ids"] = input_node_ids
    meta["selected_node_ids"] = sorted(node_ids)
    meta["dropped_node_ids"] = sorted(set(input_node_ids) - set(node_ids))

    if _CONSENSUS_MODE == "shadow":
        meta["outcome"] = "shadow_selected"
        state.bump_counter("solver_consensus_shadow")
        return s_in, meta

    meta["outcome"] = "selected"
    state.bump_counter("solver_consensus_selected")
    if meta["dropped_node_ids"]:
        state.bump_counter("solver_consensus_filtered")
    return _filter_s_in_to_nodes(s_in, node_ids), meta


def fov_gate_verdict(fov, n_nodes: int, brg: float, dist_km: float, range_rule_pass: bool) -> bool:
    """The FOV_MODE beam-gate rule for one contributing node, pure over an
    already-resolved fov (EmpiricalCoverageState) and today's range-rule
    outcome.  Shared by the live solver gate below and the offline bench's
    --fov leg (association_bench.py) so the bench measures the rule that
    actually ships, not a reimplementation of it.

    n == 2: fov.contains alone.  This REPLACES today's rules entirely — the
    autopsy behind this feature found the invented broadside-+90 azimuth
    (geo.node_beam_params, for a node with no declared aim) killed 66% of
    good real-data n=2 solves, and the wedge test is the n=2 mirror-
    disambiguation gate that invented azimuth feeds.

    n >= 3: range_rule_pass OR fov.contains — FOV only ever WIDENS here.  A
    separate autopsy showed the bearing wedge is structurally wrong at n>=3
    (a node's wedge constrains where a detection was MADE, not where a
    multi-epoch track is NOW), so this must never cost a solve the range
    rule already keeps; it can only rescue one the range rule alone would
    have killed.
    """
    fov_pass = fov.contains(brg, dist_km)
    if n_nodes == 2:
        return fov_pass
    return range_rule_pass or fov_pass


def _process_solver_item(
    item: tuple,
    solve_fn,
    select_fn=solver_pool._pool_select_consensus,
    multistart_fn=solver_pool._pool_solve_multistart,
) -> dict | None:
    """Process a single solver queue entry. Returns the solver result (or None).

    Extracted from the worker loop so the success/failure/latency bookkeeping
    can be unit-tested without spinning up daemon threads.

    select_fn is the consensus hypothesis stage
    (solver_pool._pool_select_consensus by default; tests substitute a stub).
    It only ever runs at n>=3 with an initial_guess and _CONSENSUS_MODE !=
    "off" — n=2 (mirror-disambiguation is the displacement/beam gates' job,
    not consensus's) and detection-level inputs (no initial_guess to pin an
    altitude with) never call it.

    multistart_fn is the free-altitude solve
    (solver_pool._pool_solve_multistart by default; tests substitute a stub),
    reached only when state.SOLVER_ALT_MODE is "free" — see
    _solve_best_altitude.
    """
    s_in, node_cfgs = item[0], item[1]
    enqueued_at: float | None = item[2] if len(item) > 2 else None
    # Discard items that have been waiting too long in the queue.  By the time
    # they are solved, the result's timestamp_ms will be > 60 s old and the
    # entry will be immediately pruned from multinode_tracks — wasting CPU.
    age_s = time.time() - enqueued_at if enqueued_at is not None else 0.0
    if enqueued_at is not None and age_s > _SOLVER_MAX_QUEUE_AGE_S:
        state.bump_counter("solver_stale_drops")
        logging.debug(
            "Solver: dropping stale item (age=%.1fs > %.1fs, n_nodes=%d)",
            age_s,
            _SOLVER_MAX_QUEUE_AGE_S,
            s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0,
        )
        return None
    # Duplicate copies of an aircraft already solved this window are dropped
    # here rather than at enqueue: the frame path must not carry solver state,
    # and a copy that queued before its twin was solved can only be recognised
    # once it reaches a worker.
    _now_s = time.time()
    _covered, _blocking, _refreshed = _resolve_slot_state(s_in, _now_s)
    if _covered:
        _record_resolve_skip(s_in, _now_s, _blocking)
        return None
    if _refreshed:
        # Admitted only by the 3+-node refresh rule: every claim on this
        # candidate was older than _SOLVER_RESOLVE_REFRESH_S, so the entry it
        # would have been suppressed behind has been dead-reckoning.  These
        # are the extra solves that rule costs, and the ones it buys.
        state.bump_counter("solver_resolve_refresh")
    n_nodes = s_in.get("n_nodes", 0) if isinstance(s_in, dict) else 0
    # Before anything reads a delay: the nodes did not sample simultaneously,
    # and every gate below (rms_delay first among them) assumes they did.  Runs
    # ahead of consensus and the altitude sweep so both judge the same aligned
    # numbers the published solve is fitted to.
    epoch_meta: dict = {"epoch_aligned": False}
    if state.SOLVER_EPOCH_ALIGN and isinstance(s_in, dict):
        s_in, epoch_meta = align_measurement_epochs(s_in, node_cfgs)
    consensus_meta: dict | None = None
    try:
        if "initial_guess" not in s_in:
            result = solve_fn(s_in, node_cfgs)
        elif n_nodes >= 3:
            if _CONSENSUS_MODE != "off":
                s_in, consensus_meta = _consensus_select(s_in, node_cfgs, select_fn)
                n_nodes = s_in.get("n_nodes", n_nodes)
            result = _solve_best_altitude(s_in, node_cfgs, solve_fn, multistart_fn)
        else:
            result = _solve_best_altitude_n2(s_in, node_cfgs, solve_fn)
    except Exception:
        state.bump_task_error("solver")
        state.bump_counter("solver_failures")
        state.bump_counter("solver_fail_exception")
        logging.exception("Multinode solver failed")
        result = None
    if result and result.get("success"):
        # Trim before the rms_delay gate reads it: a contaminated measurement
        # inflates rms_delay without moving the Huber-fitted position (see
        # _SOLVER_RMS_DELAY_MAX_US), so at n≥4 a re-solve on the surviving
        # nodes can recover a solve the blanket gate would otherwise sink.
        # The rest of the gate stack below runs on whatever this reaches —
        # the trimmed result/s_in on a successful trim, the original
        # otherwise — unchanged.  A consensus-filtered n=3 input skips this
        # (below the n≥4 floor) by construction, which is intended: consensus
        # already chose the subset it trusts.
        # Widen before anything judges the solve.  An n=2 candidate that
        # adopts a pool node the solve itself vouches for reaches every gate
        # below as an n=3 solve — including the n=2 confirmation gate, which
        # is asking for exactly the corroboration the adopted node supplied.
        n_nodes_pre_adopt = result.get("n_nodes")
        result, s_in, adopt_meta = _adopt_pool_nodes(s_in, node_cfgs, result, solve_fn, multistart_fn)
        if adopt_meta:
            n_nodes = result.get("n_nodes", n_nodes)

        trim_meta: dict | None = None
        if (
            "initial_guess" in s_in
            and result.get("n_nodes", 0) >= 4
            and (result.get("rms_delay") or 0) > _SOLVER_RMS_DELAY_MAX_US
            and result.get("per_node_delay_res_us")
        ):
            result, s_in, trim_meta = _trim_and_resolve(s_in, node_cfgs, solve_fn, result, multistart_fn)
            n_nodes = result.get("n_nodes", n_nodes)

        # Built once and threaded through every history record below
        # (published or rejected) so a bad map marker can be traced back to
        # both what trimming tried and what consensus selected.
        _extra: dict | None = dict(trim_meta) if trim_meta else {}
        if adopt_meta:
            _extra["adopt_meta"] = adopt_meta
            _extra["n_nodes_pre_adopt"] = n_nodes_pre_adopt
        if consensus_meta is not None:
            _extra["consensus_meta"] = consensus_meta
        # How this solve got its altitude, and — in free mode — what each
        # start altitude fitted to.  Stamped on every record, published or
        # rejected, and in BOTH modes (the sweep's solves report
        # altitude_mode "pinned"), because the only way to judge SOLVER_ALT_MODE
        # live is to compare the two lanes' rms_delay and gt_error_km over the
        # same history buffer.  The per-start list is what says whether the
        # three starts were worth keeping or one would have done.
        if result.get("altitude_mode"):
            _extra["altitude_mode"] = result["altitude_mode"]
        if result.get("rms_by_start") is not None:
            _extra["alt_starts_km"] = result.get("alt_starts_km")
            _extra["alt_start_rms_us"] = [None if v is None else round(float(v), 3) for v in result["rms_by_start"]]
        if result.get("z_saturated"):
            _extra["z_saturated"] = True
        # Always stamped, aligned or not: "this solve was not aligned" is the
        # fact /api/test/mlat-history needs to separate a residual the
        # correction could not have helped from one it was applied to.
        _extra.update(epoch_meta)
        _extra = _extra or None

        rms_delay = result.get("rms_delay", 0) or 0
        if rms_delay > _SOLVER_RMS_DELAY_MAX_US:
            logging.debug(
                "Solver result rejected: rms_delay=%.1f µs > %.1f µs threshold (n_nodes=%d, lat=%.3f, lon=%.3f)",
                rms_delay,
                _SOLVER_RMS_DELAY_MAX_US,
                result.get("n_nodes", 0),
                result.get("lat", 0),
                result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_rms_delay")
            solve_history._record_solve_history(
                "rejected_rms_delay",
                s_in,
                result,
                extra=_extra,
            )
            return result
        rms_doppler = result.get("rms_doppler", 0) or 0
        # Post-trim node count: an n=4 candidate trimmed to three nodes is a
        # three-node fit and gets the three-node ceiling.
        rms_doppler_max = _rms_doppler_max_hz(int(result.get("n_nodes") or 0))
        if rms_doppler > rms_doppler_max:
            logging.debug(
                "Solver result rejected: rms_doppler=%.1f Hz > %.1f Hz threshold "
                "(n_nodes=%d, lat=%.3f, lon=%.3f) — physically unrealisable Doppler",
                rms_doppler,
                rms_doppler_max,
                result.get("n_nodes", 0),
                result.get("lat", 0),
                result.get("lon", 0),
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_rms_doppler")
            solve_history._record_solve_history(
                "rejected_rms_doppler",
                s_in,
                result,
                # Which ceiling fired: the history reader cannot tell a 65 Hz
                # n=3 reject from the 200 Hz physical gate without it.
                extra={**(_extra or {}), "rms_doppler_max_hz": rms_doppler_max},
            )
            return result
        # Beam gate: range and bearing are two different physical claims, and
        # a fresh 35-min instrumentation autopsy (474 failing-node
        # evaluations) showed they do not deserve the same treatment at
        # every N.
        #
        # RANGE — differential-range ceiling when the node declares a
        # bistatic limit, else the monostatic circle.  Applied at every N and
        # never false-fired: 0 of 474 failing-node evaluations were bad
        # range rejects.  Mirrors point_in_beam's range half exactly.
        #
        # BEARING — the beam-wedge azimuth test.  At n>=3 it killed 105 good
        # solves (<3 km from truth) to stop 8 bad ones (>8 km) — and half of
        # those 8 the displacement gate below catches anyway.  Worse: 316 of
        # the 474 failing-node evaluations had the TRUE detected target
        # outside the gate's wedge even under the most favorable azimuth
        # candidate.  The reason is structural, not a tuning problem — for
        # multi-epoch track association a node's wedge constrains where its
        # detection was MADE, not where the target is *now*, and by
        # publication time it may well have flown out of that beam.  So the
        # bearing test is now n=2-only, where it is the documented
        # mirror-disambiguation gate (92 bad vs 9 good rejected) and stays
        # exactly as before.
        #
        # FOV_MODE — a further autopsy of the SAME 474 evaluations found the
        # bearing wedge's azimuth was itself often invented: a node with no
        # declared aim and a known TX falls back to broadside+90 (see
        # geo.node_beam_params), and that guess alone killed 66% of good
        # real-data n=2 solves.  The learned FOV (empirical_coverage.py)
        # replaces the invented wedge with what the node has actually been
        # seen to detect, broadening fast off ADS-B and shrinking only on
        # sustained negative evidence.  active: per node with a learned fov,
        # fov_gate_verdict (above) decides instead of range+bearing above —
        # n=2 replaces both rules, n>=3 only ever widens.  shadow: today's
        # rules keep deciding; the verdict is only counted/tagged.  off:
        # untouched — no fov is ever looked up.  A node with no learned fov
        # yet (state.node_analytics.learned_fov_for returns None) falls back
        # to today's rules verbatim regardless of mode.
        #
        # Evaluates every contributing node (not just the first failure) and
        # records per-node geometry for each one that fails, tagged with
        # which rule tripped ("range", "bearing" or "fov"; "range" when both
        # range and bearing do — the physical impossibility dominates).
        contributing_ids = result.get("contributing_node_ids", [])
        beam_failures: list[dict] = []
        _n2_bearing_check = result.get("n_nodes") == 2
        _fov_mode = state.FOV_MODE
        _fov_n_nodes = result.get("n_nodes")
        # Per-node {node_id, today_pass, fov_verdict} records in shadow mode
        # only — attached to the rejected_beam history entry below.  Shadow's
        # whole purpose is this comparison, so it must be visible after the
        # fact, not just folded into a counter.
        _fov_shadow_records: list[dict] = []
        if contributing_ids and isinstance(node_cfgs, dict):
            for nid in contributing_ids:
                cfg = node_cfgs.get(nid)
                if not cfg:
                    continue
                # A receiver is enough: the range circle and the bearing wedge
                # are both about it, and the bistatic branch below tests its
                # transmitter separately. Gated at all because node_cfgs is an
                # unfiltered snapshot of every connected node and nothing from
                # submit_tracks_round to here checks placement, so a node
                # re-registered without its position while its retained tracks
                # were being paired arrives here unplaced.
                if position_status(cfg) not in ("positioned", "missing_tx"):
                    continue
                p = node_beam_params(cfg)
                rx_lat, rx_lon = p["rx_lat"], p["rx_lon"]
                range_km = _haversine_km(rx_lat, rx_lon, result["lat"], result["lon"])

                raw_bistatic_km = None
                if p["max_bistatic_range_km"] and p["tx_lat"] is not None:
                    raw_bistatic_km = bistatic_differential_km(
                        p["tx_lat"],
                        p["tx_lon"],
                        rx_lat,
                        rx_lon,
                        result["lat"],
                        result["lon"],
                    )
                    range_fail = raw_bistatic_km > p["max_bistatic_range_km"]
                else:
                    range_fail = range_km > p["max_range_km"]
                bistatic_km = round(raw_bistatic_km, 1) if raw_bistatic_km is not None else None

                bearing_off_deg = None
                bearing_fail = False
                if p["beam_azimuth_deg"] is not None:
                    brg = bearing_deg(rx_lat, rx_lon, result["lat"], result["lon"])
                    raw_offset = abs((brg - p["beam_azimuth_deg"] + 180.0) % 360.0 - 180.0)
                    bearing_off_deg = round(raw_offset, 1)
                    bearing_fail = _n2_bearing_check and raw_offset > p["beam_width_deg"] / 2.0

                node_fail = range_fail or bearing_fail
                fov_rule: str | None = None
                fov_state: str | None = None
                fov_limit_km: float | None = None

                if _fov_mode != "off":
                    fov = state.node_analytics.learned_fov_for(nid)
                    if fov is not None:
                        brg_for_fov = bearing_deg(rx_lat, rx_lon, result["lat"], result["lon"])
                        fov_verdict = fov_gate_verdict(
                            fov,
                            _fov_n_nodes,
                            brg_for_fov,
                            range_km,
                            range_rule_pass=not range_fail,
                        )
                        if _fov_mode == "active":
                            node_fail = not fov_verdict
                            if node_fail:
                                fov_state = fov.wedge_state(brg_for_fov)
                                fov_limit_km = round(fov.limit_km(brg_for_fov), 1)
                                if _fov_n_nodes == 2:
                                    fov_rule = "fov"
                        else:  # shadow
                            today_pass = not (range_fail or bearing_fail)
                            if today_pass == fov_verdict:
                                state.bump_counter("fov_shadow_agree")
                            elif fov_verdict:
                                # Today rejects, FOV would pass — the radar3
                                # recovery number.
                                state.bump_counter("fov_shadow_would_pass")
                            else:
                                state.bump_counter("fov_shadow_would_reject")
                            _fov_shadow_records.append(
                                {
                                    "node_id": nid,
                                    "today_pass": today_pass,
                                    "fov_verdict": fov_verdict,
                                }
                            )

                if not node_fail:
                    continue
                beam_failures.append(
                    {
                        "node_id": nid,
                        "range_km": round(range_km, 1),
                        "max_range_km": p["max_range_km"],
                        "bistatic_km": bistatic_km,
                        "max_bistatic_range_km": p["max_bistatic_range_km"],
                        "bearing_off_deg": bearing_off_deg,
                        "half_width_deg": p["beam_width_deg"] / 2.0,
                        "rule": fov_rule or ("range" if range_fail else "bearing"),
                        **({"fov_state": fov_state, "fov_limit_km": fov_limit_km} if fov_state is not None else {}),
                    }
                )
        if beam_failures:
            _bf = beam_failures[0]
            logging.debug(
                "Solver result rejected: outside beam of node %s (lat=%.3f lon=%.3f range_km=%s bearing_off_deg=%s)",
                _bf["node_id"],
                result["lat"],
                result["lon"],
                _bf["range_km"],
                _bf["bearing_off_deg"],
            )
            state.bump_counter("solver_failures")
            state.bump_counter("solver_fail_beam")
            _beam_extra = {**(_extra or {}), "beam_failures": beam_failures}
            if _fov_mode == "shadow" and _fov_shadow_records:
                _beam_extra["fov_verdict"] = _fov_shadow_records
            solve_history._record_solve_history(
                "rejected_beam",
                s_in,
                result,
                extra=_beam_extra,
            )
            return None
        # Reject if the solution drifted more than the lane's displacement
        # cap from its sanity anchor. For N=2 this catches mirror-point
        # ghosts (false bistatic ellipse intersection 15-50 km away). For N≥3
        # it catches solves where the inter-node associator bound a wrong
        # frame and the LM converged on a non-target position — production
        # stats showed those were the dominant source of the per-N inversion
        # in /api/test/mlat-accuracy.
        #
        # The cap is chosen by LANE, not by anchor label: an ADS-B-anchored
        # input is judged at _MAX_DISPLACEMENT_KM because its guess was
        # overridden onto a transponder fix, a dark one at the wider
        # _MAX_DISPLACEMENT_KM_DARK because its guess is a 3 km-lattice grid
        # point.  Both constants' comments carry the measurement.
        #
        # The anchor is the association guess by default (that mis-
        # association protection).  But for a consensus-vetted solve
        # (consensus_meta outcome == "selected" — active mode actually
        # filtered this solve's input on a corroborated node subset) the
        # corroborated centroid replaces guess-proximity as the sanity
        # reference instead.  Measured in the first live active-mode window:
        # 131 displacement kills, 80 of them <3 km from truth and 110
        # consensus-selected, median centroid offset 3.04 km against the
        # then-uniform 2 km cap — the gate was anchored to the contaminated
        # guess consensus+LM exist to correct, so it was killing the corrections
        # rather than the mis-associations.  shadow mode and every
        # fallback_* outcome keep the guess anchor: consensus either never
        # ran against this solve's input or was not acted on, so its
        # centroid is not a vetted reference here.
        #
        # An anchored n=2 result overrides both lane caps with the much
        # tighter _DARK_FOLLOW_N2_MAX_DISP_KM — see that constant for the
        # live accuracy measurements.  It is the one case where the guess is
        # a real position estimate (the follow lane's dead-reckoned
        # prediction) rather than an association lattice point, so the wide
        # dark allowance for anchor uncertainty does not apply, while the fit
        # itself is under-determined and needs the tighter leash.
        _disp_km: float | None = None
        _dark_input = displacement_caps._is_dark_solver_input(s_in)
        _anchored_n2 = displacement_caps._is_anchored_n2(s_in, result)
        _disp_cap_km = displacement_caps.displacement_cap_km(s_in, result, dark=_dark_input)
        if "initial_guess" in s_in:
            _ig = s_in["initial_guess"]
            _anchor_lat, _anchor_lon = _ig.get("lat"), _ig.get("lon")
            _anchor_label = "guess"
            if consensus_meta is not None:
                if consensus_meta.get("outcome") == "selected":
                    _anchor_lat = consensus_meta.get("lat")
                    _anchor_lon = consensus_meta.get("lon")
                    _anchor_label = "consensus"
                consensus_meta["displacement_anchor"] = _anchor_label
            if _anchor_lat and _anchor_lon:
                _disp_km = _haversine_km(
                    float(_anchor_lat),
                    float(_anchor_lon),
                    result["lat"],
                    result["lon"],
                )
                if _disp_km > _disp_cap_km:
                    logging.debug(
                        "n=%d result rejected: %.1f km from %s (%s lane cap "
                        "%.1f km, lat=%.3f lon=%.3f) — likely mirror or "
                        "wrong-frame convergence",
                        n_nodes,
                        _disp_km,
                        "consensus centroid" if _anchor_label == "consensus" else "initial_guess",
                        "anchored n=2" if _anchored_n2 else ("dark" if _dark_input else "adsb"),
                        _disp_cap_km,
                        result["lat"],
                        result["lon"],
                    )
                    state.bump_counter("solver_failures")
                    state.bump_counter("solver_fail_displacement")
                    # Subset of the line above, not an alternative to it: the
                    # aggregate keeps its old meaning for anything reading it,
                    # and the dark split is what tells us live whether raising
                    # the dark cap actually moved the rejects it was raised for.
                    if _dark_input:
                        state.bump_counter("solver_fail_displacement_dark")
                    solve_history._record_solve_history(
                        "rejected_displacement",
                        s_in,
                        result,
                        displacement_km=_disp_km,
                        extra=_extra,
                    )
                    return None
        # n=2 publication gate.  The pairing must have been fitted and passed;
        # an unfitted one (chi2_per_dof None — too short an observation span so
        # far) is not yet evidence of anything.  Association re-tests it every
        # round, so a real target is published as soon as it has the history to
        # earn it rather than being discarded.
        if _N2_REQUIRE_CONFIRMED and n_nodes == 2 and isinstance(s_in, dict) and not _n2_anchor_admits(s_in):
            _chi2 = _resolve_n2_chi2(s_in, node_cfgs)
            if _chi2 is None or _chi2 > _N2_CONFIRM_CHI2_MAX:
                logging.debug(
                    "n=2 solve withheld: chi2/dof=%s (limit %.1f, span %s epochs) — lat=%.3f lon=%.3f",
                    "unfitted" if _chi2 is None else f"{_chi2:.2f}",
                    _N2_CONFIRM_CHI2_MAX,
                    s_in.get("n_epochs"),
                    result.get("lat", 0),
                    result.get("lon", 0),
                )
                state.bump_counter("n2_unconfirmed")
                solve_history._record_solve_history(
                    "n2_unconfirmed",
                    s_in,
                    result,
                    chi2_per_dof=_chi2,
                    # Two populations hide under this one outcome and nothing
                    # separated them before: 142 of 165 rejects in a droplet
                    # capture had no fit at all (association never attached
                    # cv_epochs, because only 55% of dark 2-node time has two
                    # node tracks spanning N2_CONFIRM_MIN_SPAN_S), and 23 had a
                    # fit at chi2/dof 18-43 — every one of those a real
                    # aircraft the constant-velocity model does not describe.
                    # They want opposite fixes, so the reason is recorded.
                    extra={**(_extra or {}), "n2_reason": "unfitted" if _chi2 is None else "chi2"},
                )
                return result
            if not _claim_track_pair(s_in, _chi2):
                # A better-fitting pairing already owns one of these two
                # single-node tracks.  One track is one aircraft, so the two are
                # mutually exclusive hypotheses — and the chi2 gate alone cannot
                # separate them when both clear it, which is exactly the case
                # this catches.  Measured offline, competition of this kind is
                # worth ~9 points of n=2 ghost rate at no cost in real tracks.
                state.bump_counter("n2_unconfirmed")
                solve_history._record_solve_history(
                    "n2_outbid",
                    s_in,
                    result,
                    chi2_per_dof=_chi2,
                    extra=_extra,
                )
                return result
        # Confirmed at n=2 — publish the fit's position rather than the
        # single-epoch one.  Reached by both n=2 publish paths: the gate above
        # returns on every reject, and an anchor-admitted input skips the gate
        # body entirely (the `not _n2_anchor_admits` in its condition), so
        # arriving here at n=2 means this solve is about to be published.
        if _N2_REQUIRE_CONFIRMED and n_nodes == 2 and isinstance(s_in, dict):
            _apply_n2_fit_position(s_in, result, node_cfgs)
        state.bump_counter("solver_successes")
        with state.solver_latency_lock:
            state.solver_total_solved += 1
        if enqueued_at is not None:
            latency = time.time() - enqueued_at
            with state.solver_latency_lock:
                state.solver_last_latency_s = latency
                state.solver_total_latency_s += latency
            if latency > 30.0:
                logging.warning("Solver latency high: %.1fs for %d-node candidate", latency, s_in.get("n_nodes", 0))
                from services.alerting import send_alert

                send_alert(
                    "solver_latency_high",
                    f"Solver latency {latency:.1f}s — pipeline may be falling behind",
                    {"latency_s": round(latency, 1), "n_nodes": s_in.get("n_nodes", 0)},
                )
        state.task_last_success["solver"] = time.time()
        _adsb_hex = s_in.get("adsb_hex") if isinstance(s_in, dict) else None
        # Propagate the input ADS-B hex into the result so verification can match
        # the solve back to the *aircraft that produced the measurements* rather
        # than guessing by proximity. Critical for spoofed targets: their solver
        # converges near the frozen ADS-B init position, far from real position;
        # the proximity matcher would otherwise bind them to whatever innocent
        # aircraft happens to be near the spoof point and tag the result with
        # the wrong hex's is_anomalous=False — pollutting the normal_only stats.
        if _adsb_hex:
            result["adsb_hex"] = _adsb_hex
        # Publish-path calibration is banned: attribution rides on the very
        # association the coverage polygon is used to judge, and under an
        # active FOV gate it formed a feedback loop — a ghost publish (wrong
        # tracklet pairing tagged with a real hex) recorded positives for
        # BOTH contributing nodes at another aircraft's real position, which
        # opened bins, widened the gate, and produced more ghosts.  Staging
        # 2026-08-09: ghost precision 25%, 15/29 synthetic nodes with
        # out-of-wedge bins within ~25 min of the active flip.  The frame
        # path (services/track_gates.py) records the same aircraft per node,
        # gated on an actual fresh detection instead of a solve result.
        multinode_identity._collect_track_anomalies(s_in, result)
        # Adopt the constant-velocity fit's velocity for display, when it
        # clears its own quality gate.  Velocity is Doppler-determined — one
        # projection per node — so n=2 is underdetermined and n=3 exactly
        # determined, and single-epoch noise maps straight into the vector;
        # the CV fit ties the whole observation window to one
        # constant-velocity trajectory instead and is materially better.
        # Outside the lock deliberately: for n=2 the confirmation gate above
        # already ran this fit and cached it on s_in, so this is free; for
        # n≥3 it is a fresh ~86 ms pool call (publish rate here is ~0.2/s,
        # so affordable) and must not run while other solver workers are
        # blocked on state.multinode_tracks_lock.  Gates and rms residuals
        # above are untouched — they already ran on the single-epoch values.
        fit = _resolve_cv_fit(s_in, node_cfgs) if isinstance(s_in, dict) else None
        result["solver_vel_east"] = result.get("vel_east")
        result["solver_vel_north"] = result.get("vel_north")
        if (
            fit
            and fit.get("success")
            and (fit.get("n_epochs") or 0) >= 4
            and fit.get("chi2_per_dof") is not None
            and fit["chi2_per_dof"] <= CV_VEL_ADOPT_CHI2_MAX
        ):
            result["vel_east"] = fit["vel_east"]
            result["vel_north"] = fit["vel_north"]
            result["vel_up"] = fit["vel_up"]
            result["vel_source"] = "cv_fit"
        else:
            result["vel_source"] = "solve"
        # Adopted-fit velocity is only untrusted when the same-epoch solve
        # pinned vz — that is where the fit's error tail lives (fit p90
        # 274 vs 59 m/s unsat).  Raw solve velocity is additionally
        # untrusted at n<=3, where Doppler is under/exactly-determined
        # (median vector error 81 vs 13 m/s unflagged).
        result["vel_untrusted"] = bool(result.get("vz_saturated")) or (
            result["vel_source"] == "solve" and int(result.get("n_nodes") or 0) <= 3
        )
        if result["vel_untrusted"]:
            state.bump_counter("solver_vel_untrusted_published")
        with state.multinode_tracks_lock:
            # Identity before smoothing: the track key is the smoother's
            # history key, so dark targets accumulate history too.  Key
            # association uses the raw solve position — the same position its
            # own dead-reckoned 6 km gate was tuned against.  Both steps stay
            # under this lock so two workers solving the same aircraft cannot
            # each mint a fresh key.  Lock order
            # state.multinode_tracks_lock → _MN_POS_HISTORY_LOCK (inside the
            # smoother) is never taken in reverse anywhere.
            _anchor_key = s_in.get("anchor_key") if isinstance(s_in, dict) else None
            key, _key_how, _key_dist_km, _key_dt_s = multinode_identity.multinode_key_decision(
                state.multinode_tracks,
                result,
                _adsb_hex,
                _anchor_key,
                # Only the follow lane's anchor is a stale position by
                # construction — see the anchor branch for why that changes
                # the distance check it must be judged by.
                anchor_dr=bool(isinstance(s_in, dict) and s_in.get("follow_key")),
                # Node-track continuity: the tracker track ids this solve was
                # built from, matched against what each candidate entry
                # remembers (TRACK_LINK_AGE_S).  Pre-trim ids on purpose — the
                # question is which node tracks this solve CAME from, not which
                # survived the residual trim, and result["source_track_ids"] is
                # not built until below.
                track_ids=(s_in.get("track_ids") if isinstance(s_in, dict) else None),
            )
            # Key ownership (DARK_FOLLOW_MODE=binding).  A bottom-up solve
            # that landed on a key the follow lane is answering for is not
            # keyed at all: it is either a duplicate of the anchored solve
            # already refreshing that key or a different aircraft about to
            # steal it, and both drag the entry and its filter.  The verdict
            # is taken here, under the same lock as the decision, so nothing
            # about the entry can change between deciding and refusing; the
            # record and the counter are emitted outside it, as every other
            # outcome's are.
            _shadow_key = key if _key_how == "shadowed" else None
            if _shadow_key is None:
                # Dark-lane key births vs re-keys.  The fragmentation question is
                # "how often does one aircraft get a second key", and the only
                # place that is decided is right here — solver_successes counts
                # solves, distinct_keys counts survivors, neither counts the
                # decision.  Dark only: the ADS-B lane keys off the transponder
                # hex unconditionally and has no decision to observe.  Anchor
                # hits are deliberately in neither counter; solver_anchor_hits
                # already carries them, and double-counting them here would make
                # minted + proximity stop summing to the dark decisions this
                # gate actually made.
                if key.startswith("mn-dark-"):
                    if _key_how == "minted":
                        state.bump_counter("solver_key_minted_dark")
                    elif _key_how == "tracks":
                        # Re-keys that the node-track evidence decided: either a
                        # candidate that shared ids and outranked a nearer
                        # stranger, or a follow-owned key joined on >=2 shared
                        # ids.  Counted apart from proximity so the two rules
                        # can be read against each other — every one of these
                        # was a mint or a discarded solve before.
                        state.bump_counter("solver_key_tracks")
                    elif _key_how == "proximity":
                        state.bump_counter("solver_key_proximity_dark")
                        # ...and, of those, the ones that matched an entry
                        # whose measurement epoch was LATER than this solve's
                        # — the out-of-order case _MN_ASSOC_MAX_NEG_DT_S
                        # opened.  Every one of these was a fresh mint (a
                        # duplicate track for one aircraft) under the old
                        # rule, so this counter is how much of the measured
                        # fragmentation the signed window actually reclaims.
                        if _key_dt_s is not None and _key_dt_s < 0:
                            state.bump_counter("solver_key_proximity_negdt")
                if _anchor_key:
                    # s_in["anchor_key"] is set by exactly two producers: top-down
                    # claiming in active mode, and a dark-follow input in binding
                    # mode (services/dark_follow.py).  Both are off by default, so
                    # this block stays inert by construction with no mode read
                    # here.  The counters do not split the two, deliberately: they
                    # measure the same thing either way — how often an anchor
                    # named the track the solve actually landed on — and
                    # follow_key on the history record separates them after the
                    # fact for anyone who needs it.
                    state.bump_counter("solver_anchored_published")
                    state.bump_counter("solver_anchor_hits" if _key_how == "anchor" else "solver_anchor_fallbacks")
                # Raw solve position, before smoothing — the history record keeps
                # both so display-side drift can be separated from solver error.
                _raw_lat, _raw_lon = result["lat"], result["lon"]
                # Multi-epoch averaging cuts single-frame noise by ~√K.  Originally
                # n=2-with-ADS-B only — production showed dark targets (where MLAT
                # is the only position source) were the one population left raw.
                # Smoothing now runs through the env-gated KF in
                # services/track_filter.py, with this module's EWMA kept as the
                # TRACK_SMOOTHER=ewma fallback.
                result = track_filter.smooth_solve(
                    result, key, _adsb_hex, ewma_fn=multinode_identity._ewma_smooth_track
                )
                prev = state.multinode_tracks.get(key)
                if prev:
                    # Latch: a tracker flag raised on an earlier solve holds for
                    # the multinode track's lifetime (≤60 s expiry) even if the
                    # contributing track has since despawned or gone quiet.
                    result["is_anomalous"] = bool(result.get("is_anomalous")) or bool(prev.get("is_anomalous"))
                    result["anomaly_types"] = sorted(
                        set(result.get("anomaly_types", [])) | set(prev.get("anomaly_types", []))
                    )
                # Source-track identity: the single-node track ids this solve was
                # built from.  Used below for supersession and carried into the
                # history record so a bad map marker can be traced to its inputs.
                result["source_track_ids"] = sorted(s_in.get("track_ids") or []) if isinstance(s_in, dict) else []

                # Supersession: an earlier entry that is THIS aircraft, under a
                # key the proximity match (multinode_key_decision) missed, is
                # replaced now rather than left rendering beside the new one for
                # up to 60 s.  solve_count carries forward so the re-solved
                # aircraft does not fall back under the n=2 gate below.
                #
                # A shared source track id is the cheap filter, not the rule.  The
                # premise this block used to carry — "one aircraft is one set of
                # source tracks" — is false: single-node tracker tracks are shared
                # between the association candidates of DIFFERENT aircraft (74 of
                # 178 track ids in a 6 min live window appeared in published solves
                # of more than one ground-truth aircraft), so popping on the shared
                # id alone destroyed a live neighbour's key 36 times in 44
                # supersessions — 41 of them beyond the association gate, 43 under
                # 15 s old — and the victim's next solve minted a fresh key (dark
                # keys churning at 7.4/min with a 7 s median lifetime).  Now
                # _supersession_match has to agree: the old entry dead-reckons
                # into the gate, or its inputs are a subset of this solve's.
                # Replayed over the same solves that cuts mints 47 -> 22 and
                # cross-aircraft pops 36 -> 7.  Refusals are counted
                # (mn_superseded_blocked), not
                # silent — the shared-id signal is mostly contamination and the
                # panel has to be able to see that.
                #
                # Proximity alone was not enough either: a later ground-truth
                # capture (2026-09-05) found 63 of 129 supersessions still popping
                # a neighbour, a solve of one aircraft with kilometres of error
                # landing inside another's gate.  The predicate now runs on the
                # tighter _MN_SUPERSEDE_BASE_KM and an altitude gate — see its
                # docstring — which is why the solve's own alt_m goes in here.
                #
                # Unchanged by anchor honoring: `old_key == key: continue` below
                # already protects an anchor from superseding itself, and a
                # proximity-minted fragment built from exactly the anchor's source
                # tracks merging INTO the anchor (old_key != key, key ==
                # anchor_key) is the identical-inputs branch (b) of the predicate —
                # exactly the fragmentation-collapse this whole feature exists for.
                max_superseded_count = 0
                max_superseded_n_nodes = 0
                _superseded_keys: list[str] = []
                _superseded_blocked = 0
                if result["source_track_ids"]:
                    new_ids = set(result["source_track_ids"])
                    _ts_ms = result.get("timestamp_ms") or 0
                    for old_key, old_r in list(state.multinode_tracks.items()):
                        if old_key == key:
                            continue
                        if not new_ids.intersection(old_r.get("source_track_ids") or ()):
                            continue
                        matched, _ = multinode_identity._supersession_match(
                            old_key, old_r, new_ids, _raw_lat, _raw_lon, _ts_ms, alt_m=result.get("alt_m")
                        )
                        if not matched:
                            _superseded_blocked += 1
                            state.bump_counter("mn_superseded_blocked")
                            # Split out the refusals the altitude gate alone made:
                            # re-asking with the altitudes withheld (which the
                            # predicate fails open on) is the same question minus
                            # that test.  Worth separating because the two
                            # refusals mean different things — a distance refusal
                            # is a neighbour far away, an altitude one is a
                            # neighbour the solve landed right on top of, and only
                            # the second says how much of the new gate's work is
                            # being done by altitude.
                            if multinode_identity._supersession_match(
                                old_key, old_r, new_ids, _raw_lat, _raw_lon, _ts_ms
                            )[0]:
                                state.bump_counter("mn_superseded_blocked_alt")
                            continue
                        multinode_identity._forget_mn_key(old_key)
                        max_superseded_count = max(max_superseded_count, old_r.get("solve_count", 0))
                        max_superseded_n_nodes = max(
                            max_superseded_n_nodes,
                            int(old_r.get("max_n_nodes") or 0),
                            int(old_r.get("n_nodes") or 0),
                        )
                        _superseded_keys.append(old_key)
                        state.bump_counter("mn_superseded")

                # Mint-time retirement of the key this mint replaced.  The
                # supersession block above cannot reach the hard-turn re-key —
                # its shared-id prefilter is empty through a turn and its
                # spatial branch measures the dead reckoning the turn broke —
                # so a MINTED dark key asks _stale_coast_candidate the same
                # question on raw positions plus turn evidence.  See that
                # predicate for the gates and the measurement behind them.
                #
                # Placed here, after supersession, so the two cannot fight over
                # one entry: anything supersession popped is already out of
                # state.multinode_tracks by now, and a key that qualified for
                # supersession was never a mint's problem in the first place.
                _coast_recent_ids = None
                if MN_STALE_COAST_ENABLED and _key_how == "minted" and key.startswith("mn-dark-"):
                    _coast_key, _coast_reason = multinode_identity._stale_coast_candidate(
                        state.multinode_tracks,
                        key,
                        _raw_lat,
                        _raw_lon,
                        result.get("timestamp_ms") or 0,
                        result.get("alt_m"),
                    )
                    if _coast_key is None:
                        state.bump_counter(multinode_identity._MN_STALE_COAST_COUNTERS[_coast_reason])
                    else:
                        _coast_r = state.multinode_tracks.get(_coast_key) or {}
                        # The trail is the whole point of retiring rather than
                        # just expiring: a new key is a new hex, so without
                        # this the drawn track restarts at the turn (measured
                        # median 40 s of history on a re-keyed turn against
                        # 113 s when the key survives one) while the old hex's
                        # 60 points sit unreachable until feed_gc collects
                        # them.  The KF state is deliberately NOT carried:
                        # the old filter's velocity is the pre-turn one, which
                        # is the error this exists to end, and _forget_mn_key
                        # drops it below.
                        adopt_track_history(multinode_hex_from_key(_coast_key), multinode_hex_from_key(key))
                        # Everything the existing supersession path carries
                        # forward, for the same reasons it does: the anomaly
                        # latch (a flag raised under the old key holds), the
                        # solve_count and the n_nodes high-water mark (so the
                        # re-keyed aircraft is not hidden again by the n=2 gate
                        # or dropped by DARK_FOLLOW_MIN_NODES), and the node
                        # track memory the next keying decision reads.
                        result["is_anomalous"] = bool(result.get("is_anomalous")) or bool(_coast_r.get("is_anomalous"))
                        result["anomaly_types"] = sorted(
                            set(result.get("anomaly_types", [])) | set(_coast_r.get("anomaly_types", []))
                        )
                        max_superseded_count = max(max_superseded_count, _coast_r.get("solve_count", 0))
                        max_superseded_n_nodes = max(
                            max_superseded_n_nodes,
                            int(_coast_r.get("max_n_nodes") or 0),
                            int(_coast_r.get("n_nodes") or 0),
                        )
                        _coast_recent_ids = _coast_r.get("recent_track_ids")
                        # Named on the entry so the feed can hand the frontend
                        # the hex whose trail buffer this key inherits
                        # (aircraft_feed's predecessor_hex), and appended to
                        # the superseded list so mlat-history shows the
                        # retirement beside the ordinary ones.
                        result["predecessor_key"] = _coast_key
                        _superseded_keys.append(_coast_key)
                        multinode_identity._forget_mn_key(_coast_key)
                        state.bump_counter("mn_stale_coast_retired")
                result["solve_count"] = max(prev.get("solve_count", 0) if prev else 0, max_superseded_count) + 1
                # High-water mark of geometry, carried forward with the key.
                # dark_follow._build_targets asks whether a track was ever
                # overdetermined enough to be trusted as an identity, and
                # n_nodes alone answers only for the LAST solve: once a
                # followed track publishes at n=2 (the anchored bypass at the
                # n=2 gate) the next rebuild would drop the key on
                # DARK_FOLLOW_MIN_NODES, which is precisely the aircraft this
                # lane exists to follow out of 3-node coverage.
                # Folded across supersession for the same reason solve_count is
                # (above): the winning key is not always the key the history is
                # on.  An established 3-node track absorbed into a freshly
                # minted key has prev None, so without the superseded term a
                # merge that happened to be n=2 would reset the mark to 2 and
                # the next rebuild would drop the key — this bug, reached by
                # the other path.
                result["max_n_nodes"] = max(
                    int(prev.get("max_n_nodes") or 0) if prev else 0,
                    int(prev.get("n_nodes") or 0) if prev else 0,
                    max_superseded_n_nodes,
                    int(result.get("n_nodes") or 0),
                )
                # Node-track memory, carried with the key: the ids this solve
                # was built from merged into the ones the entry has seen in the
                # last TRACK_LINK_AGE_S, so the NEXT solve's keying decision can
                # ask whether it shares any of them (see TRACK_LINK_AGE_S and
                # multinode_key_decision).  Pre-trim ids for the same reason the
                # decision reads them pre-trim, pruned and capped on write so a
                # long-lived key cannot accumulate an unbounded dict.  Feed
                # entries are built field-by-field (aircraft_feed's
                # multinode_to_aircraft) and the Parquet archive writes a fixed
                # schema, so this field reaches neither the map nor the archive.
                result["recent_track_ids"] = multinode_identity.merge_recent_track_ids(
                    (prev or {}).get("recent_track_ids") or _coast_recent_ids,
                    s_in.get("track_ids") if isinstance(s_in, dict) else None,
                    result.get("timestamp_ms", 0) / 1000.0,
                )
                state.multinode_tracks[key] = result
                if trim_meta:
                    state.bump_counter("solver_trimmed")
        if _shadow_key is not None:
            state.bump_counter("dark_bottomup_shadowed")
            solve_history._record_solve_history(
                "shadowed_by_follow",
                s_in,
                result,
                follow_key=_shadow_key,
                key_how=_key_how,
                key_dist_km=_key_dist_km,
                key_dt_s=_key_dt_s,
                extra=_extra,
            )
            return result
        # Append a snapshot to the track-archive buffer for Parquet persistence.
        # solve_ts_ms records when the solve completed (server wallclock) so
        # analysts can measure end-to-end latency vs. result["timestamp_ms"].
        archive_record = dict(result)
        archive_record["solve_ts_ms"] = int(time.time() * 1000)
        state.track_archive_buffer.append(archive_record)
        # The re-solve claim, taken here and nowhere else: this aircraft is now
        # on the map at this width, which is the only thing that makes a
        # duplicate not worth solving.  Survivors only — source_track_ids is
        # rebuilt from the post-trim node set.  Outside
        # state.multinode_tracks_lock on purpose, so _RECENT_SOLVES_LOCK is
        # never nested inside it.
        _record_resolve_slot(result.get("source_track_ids"), result.get("n_nodes"), time.time())
        solve_history._record_solve_history(
            "published",
            s_in,
            result,
            solve_key=key,
            raw_lat=_raw_lat,
            raw_lon=_raw_lon,
            chi2_per_dof=s_in.get("chi2_per_dof") if isinstance(s_in, dict) else None,
            displacement_km=_disp_km,
            key_how=_key_how,
            key_dist_km=_key_dist_km,
            key_dt_s=_key_dt_s,
            superseded_keys=_superseded_keys,
            superseded_blocked=_superseded_blocked,
            extra=_extra,
        )
    elif result is not None:
        # The LM ran but did not converge (success=False).  Previously this
        # path incremented nothing — staging showed hundreds of solves
        # vanishing with no observable reason.  trim_meta never applies here
        # (trimming only starts once a solve has already succeeded), but
        # consensus_meta can — the hypothesis stage ran before this solve
        # was even attempted.
        state.bump_counter("solver_failures")
        state.bump_counter("solver_fail_unconverged")
        solve_history._record_solve_history(
            "unconverged",
            s_in,
            result,
            extra={"consensus_meta": consensus_meta} if consensus_meta is not None else None,
        )
    return result


def _solver_worker_iteration(timeout: float = 1.0, q=None) -> bool:
    """One queue-drain step; True if an item was taken (processed or failed).

    ``q`` defaults to state.solver_queue; a worker generation captures its
    queue at startup, and tests can supply an isolated queue.
    """
    if q is None:
        q = state.solver_queue
    try:
        item = q.get(timeout=timeout)
    except queue.Empty:
        return False
    try:
        _process_solver_item(item, solver_pool._pool_solve_multinode)
    except Exception:
        # A worker thread must survive any single bad item: when the
        # trail-snapshot race killed both workers, the queue silently
        # filled and publishing stopped for hours with only the health
        # check noticing.
        state.bump_counter("solver_worker_errors")
        logging.exception("Solver worker: unhandled error for one item")
    return True


def _run_solver_worker(stop: threading.Event, work_queue):
    """Drain state.solver_queue and run solve_multinode. Runs as a daemon thread."""
    # Deferred import: known_lane reuses this module's gates, record store and
    # publication lock, so a top-of-file import here would be circular.
    from services.tasks import known_lane

    # Mode flags are boot-static. Capture both them and the queue for this
    # worker generation, so a later lifespan cannot redirect an old worker.
    known_lane_armed = known_lane.lanes_armed()
    while not stop.is_set():
        _solver_worker_iteration(timeout=0.1, q=work_queue)
        if known_lane_armed and not stop.is_set():
            # Known-lane pass (identity-first claims → per-hex solves), plus
            # the dark-follow pass behind the same lock and interval.
            # Ridden on the worker loop rather than its own thread so the
            # solve compute stays on the threads that already own the solver
            # locks and pool; interval- and concurrency-gated inside, and it
            # never raises — a worker thread must survive any single bad pass
            # (see the handler in _solver_worker_iteration for what happens
            # when one doesn't).
            known_lane.maybe_run_pass(solver_pool._pool_solve_multinode)


def solver_workers_stopping() -> bool:
    """Whether an unfinished old generation makes application restart unsafe."""
    with _solver_workers_lock:
        return _solver_stop.is_set() and any(worker.is_alive() for worker in _solver_workers)


def start_solver_workers():
    """Start the solve process pool and N daemon threads draining the queue."""
    with _solver_workers_lock:
        _solver_workers[:] = [t for t in _solver_workers if t.is_alive()]
        if _solver_workers:
            if _solver_stop.is_set():
                raise RuntimeError("Previous solver workers are still stopping")
            return
        _solver_stop.clear()
        _start_solver_workers()


def _start_solver_workers():
    """Called with the worker lifecycle lock held."""
    pool_on = solver_pool.start_pool()
    for i in range(solver_pool._N_SOLVER_WORKERS):
        t = threading.Thread(
            target=_run_solver_worker,
            args=(_solver_stop, state.solver_queue),
            daemon=True,
            name=f"solver-{i}",
        )
        t.start()
        _solver_workers.append(t)
    logging.info(
        "Started %d multinode solver worker(s) (process pool: %s)",
        solver_pool._N_SOLVER_WORKERS,
        "on" if pool_on else "off",
    )


def stop_solver_workers(timeout: float = 5.0) -> bool:
    """Stop accepting work and wait up to one shared deadline for workers.

    Python cannot interrupt an inline native solve. Keep unfinished thread
    handles and refuse a new generation until they exit, rather than silently
    creating competing workers. Process-pool shutdown cancels queued calls;
    already running calls are still subject to their existing solve timeout.
    """
    with _solver_workers_lock:
        _solver_stop.set()
        deadline = time.monotonic() + max(timeout, 0.0)
        for worker in _solver_workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        _solver_workers[:] = [t for t in _solver_workers if t.is_alive()]
        solver_pool.stop_pool()
        if _solver_workers:
            logging.warning("%d solver worker(s) still running after shutdown deadline", len(_solver_workers))
        return not _solver_workers
