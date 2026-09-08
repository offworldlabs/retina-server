"""Known-target claiming (KNOWN_LANE_MODE): bind detections to known ADS-B
aircraft BEFORE the dark lane ever sees them.

Today an ADS-B tag is advisory — a tagged detection flows through the same
tracker and association gates a dark one does, so a lit airliner's echo can
still cross-pair with a dark tracklet into a phantom solve.  This stage runs
per frame, ahead of the tracker: detections that match a known transponder
are recorded in state.known_claims (the known lane's input), and in binding
mode they are removed from the frame the dark lane processes, so they cannot
cross-pair at all.  The dark pool is then dark by construction, not by
gate-luck.

Assignment is GLOBAL one-to-one (scipy linear_sum_assignment) rather than the
greedy pass associate_detections_to_adsb uses: two aircraft whose predicted
observations sit within one gate width of each other are exactly the case
this lane exists for, and greedy resolves that crossing by letting the best
single score steal the only aircraft the other detection could bind to —
leaving a real lit echo in the dark pool.  Hungarian minimises the summed
score, so a feasible complete matching always beats a partial one.

Gate/score shape follows associate_detections_to_adsb (per-axis gates, score
= d_res/d_gate + f_res/f_gate, same constants as defaults) with one addition:
the allowance grows with fix age.  Dead-reckoning error is ~linear in coast
time (a 10 m/s velocity error is ~450 m at the 45 s age cap, i.e. 1.5–3 µs of
delay — comparable to the base gate itself), so a fixed gate is simultaneously
too loose for a fresh fix and too tight for an old one.

A path-2 candidate must also be somewhere the node can actually see.  Delay
and Doppler are two numbers a wrong aircraft can match by coincidence, so a
cached state whose dead-reckoned position falls outside the node's detection
area is no candidate at all, whatever its residuals say.  Node tags (path 1)
carry the node's own evidence that it saw the aircraft and stay ungated.

A path-2 candidate must also come from the node's own WORLD.  The ADS-B cache
is fed by simulated aircraft and by real traffic (hardware receivers, the
simulator's adsb.lol relay) alike, and when real traffic flies over the same
footprint the simulated fleet occupies, the visibility gate passes it
trivially — every real aircraft becomes a decoy the assignment can bind a
synthetic echo to, and each such bind puts a plane icon on the map at a
position no radar in either world measured.  Entries carry a "world" tag
("sim"/"real") stamped where they are written; claiming skips candidates
tagged with the other world and counts them (known_claims_world_rejects).

THE HOLD (path H).  Both ADS-B paths ask the same question every frame from
scratch: does a transponder fix explain this detection right now?  When the
tags stop and the cached fix ages past KNOWN_CLAIM_MAX_FIX_AGE_S the answer
becomes "no" for an aircraft that has not moved, changed, or gone anywhere —
its echoes fall into the dark pool, the tracker forms a track, and the dark
solver mints a new key beside (or on top of) the aircraft the lane was
tracking a second earlier.  But a claim is evidence in its own right: it says
this node's echo of this hex sat at that (delay, Doppler).  The next frame's
echo of the same aircraft is one frame of motion away from it, and Doppler
says how far — so the node's OWN measured track can predict the next
observation without any transponder at all.  Path H does exactly that, from
state.known_track_holds, and runs after path 1 and before path 2 so that a
link, once made, cannot be peeled off by another hex's dead-reckoned fix.
It has no maximum duration: as long as the track keeps matching frame after
frame it stays linked.  The one thing it may not do is contradict a live
transponder — see the consistency rule in _claim_holds.
"""

import logging
import math
import os
from collections import deque

import numpy as np
from retina_analytics.association import (
    _V_MAX_MS,
    ADSB_SEED_DELAY_GATE_US,
    ADSB_SEED_DOPPLER_GATE_HZ,
    ADSB_SEED_MAX_DR_AGE_S,
    CLAIM_DELAY_GATE_US,
    CLAIM_DOPPLER_GATE_HZ,
    CLAIM_MAX_DR_AGE_S,
    CLAIM_MAX_GLOBAL_TRACKS,
    _point_in_beam,
    claim_eligible,
    predict_observation,
)
from retina_analytics.constants import KM_PER_DEG_LAT, km_per_deg_lon, offset_latlon_m
from scipy.optimize import linear_sum_assignment

from config.constants import FT_TO_M, as_num
from core import state
from services import dark_follow, track_filter
from services.id_utils import normalize_hex_key

# Same base constants as the seeding path: the comparison is the identical
# "measurement vs dead-reckoned ADS-B fix" shape, so a different base gate
# here would just be a second opinion on the same physics.
KNOWN_CLAIM_DELAY_GATE_US = ADSB_SEED_DELAY_GATE_US
KNOWN_CLAIM_DOPPLER_GATE_HZ = ADSB_SEED_DOPPLER_GATE_HZ
KNOWN_CLAIM_MAX_FIX_AGE_S = ADSB_SEED_MAX_DR_AGE_S

# Sentinel cost for a gated-out (detection, aircraft) cell.  A large finite
# value rather than inf: linear_sum_assignment raises "cost matrix is
# infeasible" when a full matching cannot avoid inf, and an unclaimable
# detection is the normal case (clutter, dark targets), not an error.  Any
# real score is <= 2.0 by construction, so >= this means "not assigned".
_GATE_INFEASIBLE = 1.0e6

# Slack on the path-2 range prescreen's radius, so the cheap test can only ever
# be looser than the visibility gate it stands in front of, never tighter.  2%
# against a projection error that is a third-order term in the angular
# separation (well under 0.1% at any range this gate passes) — the margin is
# not tuned to the error, it is large enough that the error cannot reach it.
_SCREEN_MARGIN = 1.02

# ── Hold gates (path H) ──────────────────────────────────────────────────────
# The maximum frame-time gap a hold may bridge, and the feature's rollback
# lever: 0 disables path H entirely and leaves claiming byte-identical to the
# behaviour that predates it.  Measured in FRAME time (frame["timestamp"]),
# never wall clock — a replay or a backlogged node must see the same gates the
# live path saw.  8 s: a node's frames arrive ~1 s apart and the simulator's
# SNR-dependent miss rate reaches 40% at the detection threshold, so 2–4 s
# gaps are routine and a bound below that would break the link on ordinary
# misses; past ~8 s the propagated Doppler rate is extrapolation rather than
# measurement.
KNOWN_HOLD_MAX_GAP_S = float(os.getenv("KNOWN_HOLD_MAX_GAP_S", "8"))
# Gate = base + rate * dt, per axis.  The bases are set off the measurement
# noise, not off aircraft motion (motion is what the prediction models):
# retina_simulation.world.generate_detections_for_node adds sigma 0.1–0.2 µs
# of delay noise and 2–4 Hz of Doppler noise depending on SNR, and the hold
# compares a prediction built from ONE past sample against a new one, so the
# relevant sigma is sqrt(2) of that — ~0.3 µs and ~5.7 Hz at the noisy end.
# A gate must sit at least 5 sigma above it or ordinary noise breaks the link,
# which puts the bases at 1.5 µs and 20 Hz.  The rate terms cover what the
# constant-rate propagation itself cannot: turns and altitude changes, which
# grow with the gap.
KNOWN_HOLD_DELAY_GATE_US = float(os.getenv("KNOWN_HOLD_DELAY_GATE_US", "1.5"))
KNOWN_HOLD_DELAY_RATE_US_PER_S = float(os.getenv("KNOWN_HOLD_DELAY_RATE_US_PER_S", "1.0"))
KNOWN_HOLD_DOPPLER_GATE_HZ = float(os.getenv("KNOWN_HOLD_DOPPLER_GATE_HZ", "20"))
KNOWN_HOLD_DOPPLER_RATE_HZ_PER_S = float(os.getenv("KNOWN_HOLD_DOPPLER_RATE_HZ_PER_S", "10"))
# Ceiling on the propagated Doppler rate, and the maximum age of the sample
# pair it may be derived from.  An airliner's bistatic Doppler rate stays well
# inside +-15 Hz/s outside a hard turn; a larger apparent rate is two samples
# of DIFFERENT aircraft (or a miss-riddled pair straddling a manoeuvre), and
# propagating it would walk the prediction off the track it is holding.
KNOWN_HOLD_MAX_DOPPLER_RATE_HZ_S = float(os.getenv("KNOWN_HOLD_MAX_DOPPLER_RATE_HZ_S", "15"))
KNOWN_HOLD_RATE_MAX_SPAN_S = float(os.getenv("KNOWN_HOLD_RATE_MAX_SPAN_S", "5"))

# ── Follow gates (path 2's synthetic candidates) ─────────────────────────────
# A hold only helps the nodes that already had the aircraft.  The node that
# ACQUIRES a silent aircraft mid-silence has neither a tag, nor a fresh fix,
# nor a hold — so its detections go to the dark pool and mint a twin beside
# the very entry the known lane is still publishing.  The lane's own published
# position is the missing candidate: a radar measurement of that aircraft,
# seconds old, on a key every node can read.
#
# Maximum age of that published entry, for the same reason (and the same
# default) as dark_follow.DARK_FOLLOW_MAX_AGE_S: past ~20 s the entry is being
# extrapolated rather than followed.  0 disables the path entirely and leaves
# path 2 byte-identical to what it was before.
KNOWN_FOLLOW_MAX_AGE_S = float(os.getenv("KNOWN_FOLLOW_MAX_AGE_S", "20"))
# ...and the minimum number of solves behind it.  A one- or two-solve entry is
# a position the lane has not yet confirmed against itself, and offering it as
# a claiming candidate would let a single bad solve capture detections on
# every node at once.
KNOWN_FOLLOW_MIN_SOLVES = int(os.getenv("KNOWN_FOLLOW_MIN_SOLVES", "3"))

_logger = logging.getLogger(__name__)

# ── services.node_bias (trust slice) — optional at import time ────────────────
# The residual consumer is developed in a sibling slice and may not exist in
# this tree yet.  Resolved once, lazily: retrying the import per frame would
# put an exception on the hot path forever in trees where it never lands.
_node_bias_mod = None
_node_bias_unavailable = False


def _node_bias():
    global _node_bias_mod, _node_bias_unavailable
    if _node_bias_mod is None and not _node_bias_unavailable:
        try:
            from services import node_bias

            _node_bias_mod = node_bias
        except ImportError:
            _node_bias_unavailable = True
            _logger.info("services.node_bias not present — claim residuals unrecorded")
    return _node_bias_mod


def _reset_for_tests() -> None:
    """Forget the node_bias import verdict.  Tests only — lets a test inject
    a fake services.node_bias after an earlier test already cached the
    ImportError."""
    global _node_bias_mod, _node_bias_unavailable
    _node_bias_mod = None
    _node_bias_unavailable = False


def _gate_scale(age_s: float) -> float:
    """Gate allowance multiplier for a fix age: 1.0 fresh, 2.0 at the age cap.

    Linear because the dominant error it covers is linear: dead-reckoning
    drift is velocity error × coast time.  Doubling (not more) at the cap
    keeps a stale fix from claiming across a neighbouring aircraft's gate —
    past 2× the base gates the two failure modes trade places.
    """
    return 1.0 + min(abs(age_s), KNOWN_CLAIM_MAX_FIX_AGE_S) / KNOWN_CLAIM_MAX_FIX_AGE_S


def _tag_velocity(tag: dict) -> tuple[float, float]:
    """(vel_east, vel_north) m/s from a node adsb entry's gs (kt) / track (deg).

    Unlike the cache path, these are not coerced: no non-numeric gs or track has
    been observed from a node.  86cb9t7c4 tracks closing that gap.
    """
    gs_ms = (tag.get("gs", 0) or 0) * 0.514444
    trk = math.radians(tag.get("track", 0) or 0)
    return gs_ms * math.sin(trk), gs_ms * math.cos(trk)


def _dark_global_projections(geo, frame_ts_s: float) -> list[tuple[float, float]]:
    """(pred_delay_us, pred_doppler_hz) of every established dark global at
    this node, dead-reckoned to frame time — the contention reference set.

    Reuses the top-down claiming machinery verbatim (claim_eligible, the
    CLAIM_MAX_GLOBAL_TRACKS cap, the CLAIM_* gates' DR-age cap, the same
    projection) so "gates against a dark track" means exactly what it means
    in _claim_round: a claim is contested when ASSOC_CLAIM_MODE's claimer
    would ALSO have taken this detection.
    """
    out = []
    # state._global_tracks_for_claiming is a dumb snapshot; the eligibility
    # filter and the CLAIM_MAX_GLOBAL_TRACKS truncation are the LIBRARY's, and
    # this call site reaches the provider directly rather than through the
    # associator — so it has to apply them itself or the reference set (and
    # this loop's predict_observation count) is unbounded in the dark-global
    # population.  Newest fix first, exactly _claim_round's ordering, so the
    # two paths cap to the same 200 tracks rather than to two different ones.
    for g in sorted(
        (g for g in state._global_tracks_for_claiming() if claim_eligible(g)),
        key=lambda g: g.get("timestamp_ms", 0),
        reverse=True,
    )[:CLAIM_MAX_GLOBAL_TRACKS]:
        dt = frame_ts_s - g.get("timestamp_ms", 0) / 1000.0
        if not (0.0 <= dt <= CLAIM_MAX_DR_AGE_S):
            continue
        dr_lat, dr_lon = offset_latlon_m(
            g["lat"],
            g["lon"],
            east_m=g.get("vel_east", 0.0) * dt,
            north_m=g.get("vel_north", 0.0) * dt,
        )
        out.append(
            predict_observation(
                geo,
                dr_lat,
                dr_lon,
                g.get("alt_m", 0.0) / 1000.0,
                g.get("vel_east", 0.0),
                g.get("vel_north", 0.0),
                g.get("vel_up", 0.0),
            )
        )
    return out


def _is_contested(delay_us: float, doppler_hz: float, projections: list[tuple[float, float]]) -> bool:
    return any(
        abs(pd - delay_us) <= CLAIM_DELAY_GATE_US and abs(pf - doppler_hz) <= CLAIM_DOPPLER_GATE_HZ
        for pd, pf in projections
    )


def _claim_dark_follow(
    node_id: str,
    geo,
    frame_ts_s: float,
    ts_ms: int,
    delays: list,
    dopplers: list,
    free: list[int],
) -> set[int]:
    """Path 3: claim the leftover detections against followed dark tracks.

    The ADS-B paths' assignment, run a second time with the dark pseudo-states
    (services/dark_follow.py) standing in for cached transponder fixes — same
    dead-reckoning, same visibility gate, same Hungarian one-to-one, same
    per-axis normalised score.  Only the gate widths differ, because a dark
    pseudo-state carries its own uncertainty and an ADS-B fix is treated as
    truth (see dark_follow.follow_gates).

    Runs on ``free`` — what the ADS-B paths did not take — which IS the
    precedence rule: an aircraft with a transponder can never lose a detection
    to a dark track's prediction, whatever the residuals say.  The reverse is
    tolerable; a dark track that loses a detection is solved from its other
    nodes, and a wrong ADS-B claim would charge a fix the node never saw to
    that node's trust.

    Claims land in state.known_claims under the mn-dark-* KEY rather than a
    hex, marked ``dark_follow`` so known_lane's two passes can tell them apart.
    They carry ``follow_fix`` rather than ``adsb_fix`` deliberately: every
    other reader of the registry (the feed's single-node ADS-B section, the
    per-node trust residuals) keys on ``adsb_fix``, and a follow claim has no
    transponder fix to offer them — its absence is what keeps those readers
    unchanged.
    """
    if not free or dark_follow.mode() == "off":
        return set()
    targets = dark_follow.follow_targets()
    if not targets:
        return set()

    node_world = state.node_world(node_id)
    cands: list[tuple[dict, float, float, float, float, float, float]] = []
    for t in targets:
        # Same world gate as path 2, same reason: a synthetic node's echoes are
        # only ever of simulated aircraft.  Untagged targets pass.
        if t["world"] is not None and t["world"] != node_world:
            continue
        dt = frame_ts_s - t["timestamp_ms"] / 1000.0
        if not (0.0 <= dt <= dark_follow.DARK_FOLLOW_MAX_AGE_S):
            continue
        dr_lat, dr_lon = offset_latlon_m(
            t["lat"],
            t["lon"],
            east_m=t["vel_east"] * dt,
            north_m=t["vel_north"] * dt,
        )
        # The associator's own visibility predicate, applied whole — the same
        # call path 2 makes, for the same asymmetry: a false accept binds a
        # detection to an aircraft this node cannot see and takes it out of the
        # lane that would have disagreed.
        if not _point_in_beam(dr_lat, dr_lon, geo):
            continue
        alt_km = t["alt_m"] / 1000.0
        pred_d, pred_f = predict_observation(
            geo,
            dr_lat,
            dr_lon,
            alt_km,
            t["vel_east"],
            t["vel_north"],
        )
        d_gate, f_gate = dark_follow.follow_gates(
            t,
            dt,
            KNOWN_CLAIM_DELAY_GATE_US,
            KNOWN_CLAIM_DOPPLER_GATE_HZ,
            geo.fc_hz,
        )
        cands.append((t, pred_d, pred_f, d_gate, f_gate, dr_lat, dr_lon))
    if not cands:
        return set()

    cost = np.full((len(free), len(cands)), _GATE_INFEASIBLE)
    for c, (_t, pred_d, pred_f, d_gate, f_gate, _dr_lat, _dr_lon) in enumerate(cands):
        for r, i in enumerate(free):
            d_res = abs(pred_d - float(delays[i]))
            f_res = abs(pred_f - float(dopplers[i]))
            if d_res > d_gate or f_res > f_gate:
                continue
            cost[r, c] = d_res / d_gate + f_res / f_gate
    rows, cols = linear_sum_assignment(cost)

    claimed: set[int] = set()
    for r, c in zip(rows, cols):
        if cost[r, c] >= _GATE_INFEASIBLE:
            continue
        i = free[r]
        t, pred_d, pred_f, _d_gate, _f_gate, dr_lat, dr_lon = cands[c]
        dq = state.known_claims.get(t["key"])
        if dq is None:
            dq = state.known_claims.setdefault(t["key"], deque(maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX))
        dq.append(
            {
                "node_id": node_id,
                "delay_us": float(delays[i]),
                "doppler_hz": float(dopplers[i]),
                "pred_delay_us": float(pred_d),
                "pred_doppler_hz": float(pred_f),
                "ts_ms": ts_ms,
                "dark_follow": True,
                # The prediction itself, at the frame epoch — this is what the
                # follow solve uses as its initial guess, which is the second
                # thing this lane exists for (the first being the key).  Unlike
                # path 2's REPORTED-position rule there is no reported position
                # to prefer: the dead-reckoned state is the only estimate there
                # has ever been.
                "follow_fix": {
                    "lat": dr_lat,
                    "lon": dr_lon,
                    "alt_km": t["alt_m"] / 1000.0,
                    "vel_east": t["vel_east"],
                    "vel_north": t["vel_north"],
                    "fix_ts_ms": ts_ms,
                },
                # Contention is an ADS-B-vs-dark question (identity evidence
                # beating a dark projection).  A follow claim IS the dark
                # projection, so there is nothing for it to contend with, and
                # leaving the flag false is what lets known_lane's selection
                # reuse _select_claims unchanged.
                "contested": False,
            }
        )
        claimed.add(i)
        state.bump_counter("dark_follow_claims")
    return claimed


def _touch_hold(
    node_id: str,
    hexn: str,
    d_meas: float,
    f_meas: float,
    ts_ms: int,
    fix: dict | None,
    world: str | None,
    is_hold: bool,
) -> None:
    """Record this claim as the node's newest measurement of the hex's track.

    Called for EVERY claim — node tag, cached-fix assignment, or hold — because
    the store's whole job is to remember that this node's echo of this hex was
    here, whichever path established it.  Keeps the previous sample beside the
    newest one: a Doppler RATE needs a difference, and taking that rate from
    ADS-B would put the transponder back in the loop the hold exists to
    survive without.

    ``fix`` is None on a hold claim, which is what keeps the ORIGINAL fix (and
    its fix_ts_ms) on the entry: downstream readers age the fix to see how long
    the aircraft has been silent, and refreshing the timestamp without a new
    transponder report would hide exactly that.
    """
    if KNOWN_HOLD_MAX_GAP_S <= 0:
        # The rollback lever is total: with the feature off nothing is written
        # either, so an off backend carries no store and claiming is exactly
        # what it was before path H existed.
        return
    holds = state.known_track_holds.setdefault(node_id, {})
    e = holds.get(hexn)
    if e is None:
        e = {
            "delay_us": d_meas,
            "doppler_hz": f_meas,
            "ts_ms": ts_ms,
            "prev_delay_us": None,
            "prev_doppler_hz": None,
            "prev_ts_ms": None,
            "fix": fix,
            "world": world,
            "n_claims": 0,
            "n_hold": 0,
        }
        holds[hexn] = e
    else:
        if ts_ms != e["ts_ms"]:
            e["prev_delay_us"] = e["delay_us"]
            e["prev_doppler_hz"] = e["doppler_hz"]
            e["prev_ts_ms"] = e["ts_ms"]
        e["delay_us"] = d_meas
        e["doppler_hz"] = f_meas
        e["ts_ms"] = ts_ms
        if fix is not None:
            e["fix"] = fix
        e["world"] = world
    e["n_claims"] += 1
    if is_hold:
        e["n_hold"] += 1


def _hold_predict(entry: dict, fc_hz: float, frame_ts_s: float) -> tuple[float, float, float]:
    """(pred_delay_us, pred_doppler_hz, dt_s) for a held track at frame time.

    Delay is propagated from Doppler rather than from a delay difference:
    Doppler IS the range rate, measured in this same frame, so it gives a
    first-order prediction from a SINGLE past sample (the case that matters —
    the frame right after the tags stop) instead of needing two.  The bistatic
    range closes when the Doppler is positive, so the delay shrinks:

        d(delay_us)/dt = -doppler_hz * 1e6 / fc_hz

    (delay_us = R_km / C_KM_US, dR/dt = -doppler * C_KM_S / fc_hz, and
    C_KM_S = C_KM_US * 1e6, so the C_KM_US cancels.)  The sign is checked
    empirically against the simulator in test_known_track_hold.py rather than
    trusted from this derivation — a convention flip anywhere between the
    generator and here would double the error instead of cancelling it.

    Doppler is propagated at the rate of the last two samples, clipped, and
    only when they are recent enough to be one manoeuvre; otherwise it is held
    flat, which is the honest zero-information answer.
    """
    dt = frame_ts_s - entry["ts_ms"] / 1000.0
    delay_rate = -entry["doppler_hz"] * 1.0e6 / fc_hz
    doppler_rate = 0.0
    prev_ts_ms = entry.get("prev_ts_ms")
    if prev_ts_ms is not None:
        span = (entry["ts_ms"] - prev_ts_ms) / 1000.0
        if 0.0 < span <= KNOWN_HOLD_RATE_MAX_SPAN_S:
            raw = (entry["doppler_hz"] - entry["prev_doppler_hz"]) / span
            doppler_rate = max(-KNOWN_HOLD_MAX_DOPPLER_RATE_HZ_S, min(KNOWN_HOLD_MAX_DOPPLER_RATE_HZ_S, raw))
    return (
        entry["delay_us"] + delay_rate * dt,
        entry["doppler_hz"] + doppler_rate * dt,
        dt,
    )


def _fix_record(st: dict) -> dict:
    """The REPORTED fix a claim carries, built from one cached ADS-B state —
    same rule as associate_detections_to_adsb, so a claim and a node tag for
    one aircraft carry the same position and downstream consumers need not
    know which path produced it."""
    return {
        "lat": st["lat"],
        "lon": st["lon"],
        "alt_baro": st.get("alt_baro"),
        "gs": st.get("gs"),
        "track": st.get("track"),
        "fix_ts_ms": st.get("timestamp_ms", 0),
    }


def _fresh_fix_prediction(
    hexn: str, geo, frame_ts_s: float, node_world: str
) -> tuple[float, float, float, dict] | None:
    """Path 2's own prediction for one hex plus the fix record it would carry,
    or None when no fresh usable fix exists.  The consistency rule's
    reference — see _claim_holds."""
    st = state._adsb_for_seeding().get(hexn)
    if st is None:
        return None
    cand_world = st.get("world")
    if cand_world is not None and cand_world != node_world:
        return None
    age_s = frame_ts_s - st.get("timestamp_ms", 0) / 1000.0
    if abs(age_s) > KNOWN_CLAIM_MAX_FIX_AGE_S:
        return None
    dr_lat, dr_lon = offset_latlon_m(
        st["lat"],
        st["lon"],
        east_m=st.get("vel_east", 0.0) * age_s,
        north_m=st.get("vel_north", 0.0) * age_s,
    )
    pred_d, pred_f = predict_observation(
        geo,
        dr_lat,
        dr_lon,
        st.get("alt_m", 0.0) / 1000.0,
        st.get("vel_east", 0.0),
        st.get("vel_north", 0.0),
    )
    return pred_d, pred_f, _gate_scale(age_s), _fix_record(st)


def _follow_fix_record(hexn: str, lat: float, lon: float, alt_m: float, ve: float, vn: float, ts_ms: int) -> dict:
    """The ``adsb_fix`` a follow claim carries.

    Preferably the hex's ORIGINAL transponder fix, taken from its hold on any
    node that still has one: the known lane reads fix_ts_ms to decide how long
    the aircraft has been silent (and therefore whether to seed from the fix or
    from its own last solve — see known_lane._build_solver_input), and handing
    it a freshly stamped fix would tell it the transponder is live.  The node
    is irrelevant to that question — the fix is the aircraft's, not the node's
    — so the first hold that has one answers.

    With no hold anywhere, the entry itself is the only evidence there is: its
    position and altitude, its velocity re-expressed as the gs/track a fix
    carries, and its own timestamp, which correctly reads as "current" because
    that is exactly what it is.
    """
    for holds in list(state.known_track_holds.values()):
        e = holds.get(hexn)
        if isinstance(e, dict) and isinstance(e.get("fix"), dict):
            return dict(e["fix"])
    speed_ms = math.hypot(ve, vn)
    return {
        "lat": lat,
        "lon": lon,
        # Feet, the unit a transponder reports and every reader of a fix
        # expects (known_lane multiplies alt_baro by FT_TO_M).
        "alt_baro": alt_m / FT_TO_M,
        "gs": speed_ms / 0.514444,
        "track": math.degrees(math.atan2(ve, vn)) % 360.0,
        "fix_ts_ms": ts_ms,
    }


def _follow_states(frame_ts_s: float, claimed_hexes: set[str]) -> dict[str, dict]:
    """Synthetic path-2 candidates built from the known lane's own published
    entries, for hexes whose transponder has gone stale or silent.

    Shaped exactly like a cached ADS-B state so path 2 can treat them as one
    population: the visibility gate, the range prescreen, the age-scaled gates
    and the Hungarian assignment all apply unchanged, and a follow candidate
    competes with the real fixes rather than being claimed beside them.  The
    eligibility rules are dark_follow._build_targets': recent enough, solved
    often enough, and with a velocity to dead-reckon by.

    A hex with a FRESH fix is skipped — path 2 already has the better
    candidate, and offering both would put the same aircraft in the assignment
    twice.
    """
    if KNOWN_FOLLOW_MAX_AGE_S <= 0:
        return {}
    cache = state._adsb_for_seeding()
    out: dict[str, dict] = {}
    for key, rec in list(state.multinode_tracks.items()):
        if not key.startswith("mn-adsb-") or not isinstance(rec, dict):
            continue
        hexn = normalize_hex_key(key[len("mn-adsb-") :])
        if not hexn or hexn in claimed_hexes:
            continue
        cached = cache.get(hexn)
        if isinstance(cached, dict) and abs(frame_ts_s - cached.get("timestamp_ms", 0) / 1000.0) <= (
            KNOWN_CLAIM_MAX_FIX_AGE_S
        ):
            continue
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        ts_ms = int(rec.get("timestamp_ms") or 0)
        if not (0.0 <= frame_ts_s - ts_ms / 1000.0 <= KNOWN_FOLLOW_MAX_AGE_S):
            continue
        if int(rec.get("solve_count") or 0) < KNOWN_FOLLOW_MIN_SOLVES:
            continue
        # The filter's velocity first — it is learned from the position
        # sequence and survives the transponder, which the entry's own solved
        # velocity (under-determined at n<=3) does not always.
        lv = track_filter.learned_velocity(key)
        if lv is not None:
            ve, vn = float(lv[0]), float(lv[1])
        else:
            ve = float(rec.get("vel_east") or 0.0)
            vn = float(rec.get("vel_north") or 0.0)
        alt_m = rec.get("alt_m")
        if alt_m is None:
            alt_m = float(rec.get("alt_km") or 0.0) * 1000.0
        alt_m = float(alt_m or 0.0)
        # World tag: the cache's while the stale entry is still there, else
        # the hold's (every hold stamps the claiming node's world).  A follow
        # candidate with NO world would pass the world gate on every node,
        # and real traffic flies over the simulated fleet's footprint —
        # exactly the decoy case that gate exists for.
        world = cached.get("world") if isinstance(cached, dict) else None
        if world is None:
            for holds in list(state.known_track_holds.values()):
                e = holds.get(hexn)
                if isinstance(e, dict) and e.get("world") is not None:
                    world = e["world"]
                    break
        out[hexn] = {
            "lat": float(lat),
            "lon": float(lon),
            "alt_m": alt_m,
            "vel_east": ve,
            "vel_north": vn,
            "timestamp_ms": ts_ms,
            # The hex's world tag while the cache still remembers it; once the
            # entry ages out there is nothing to tag with, and an untagged
            # candidate passes every world — the same rule the cache path
            # applies to an entry written before worlds existed.
            "world": world,
            # Marks this candidate as synthetic for the claim-building step
            # below; no other reader of a cached state ever sees it.
            "_follow_fix": _follow_fix_record(hexn, float(lat), float(lon), alt_m, ve, vn, ts_ms),
        }
    return out


def _claim_holds(
    node_id: str,
    geo,
    frame_ts_s: float,
    delays: list,
    dopplers: list,
    free: list[int],
    claimed_hexes: set[str],
) -> list[tuple[int, str, dict, float, float, dict]]:
    """Path H: claim leftover detections against this node's own held tracks.

    Runs between path 1 and path 2, which is the precedence rule the user's
    requirement names: once a node track is linked to a hex it may not be
    peeled off to another hex's dead-reckoned fix, and running after path 2
    would let exactly that happen every time a neighbouring aircraft's fix
    reached this detection first.  Path 1 still outranks it — a node's own
    correlation is newer evidence about the same question than yesterday's
    match.

    THE CONSISTENCY RULE (the ghost-lock guard).  A hold that may never be
    contradicted is a self-feeding loop of the kind dark_follow's guard exists
    to stop: the hold claims a detection, the claim refreshes the hold, and
    nothing can ever disagree because binding mode has already taken the
    detection out of the lane that would.  So while the transponder is still
    reporting, the hold must AGREE with it: if the hex has a fresh cached fix,
    the hold-matched detection has to fall inside path 2's gate for that hex
    too, or the hold is dropped and the hex falls through to path 2 as it does
    today.  When the fix is stale or gone there is nothing to disagree with,
    and the hold stands on its own — which is the entire point of the feature.
    """
    if KNOWN_HOLD_MAX_GAP_S <= 0:
        return []
    holds = state.known_track_holds.get(node_id)
    if not holds:
        return []

    # Expiry first, on this node's own frame clock, so a held track that has
    # gone quiet for longer than the gap can bridge is gone before it can be
    # matched (and cannot linger in the store either).
    expired = [
        h for h, e in list(holds.items()) if not (0.0 <= frame_ts_s - e["ts_ms"] / 1000.0 <= KNOWN_HOLD_MAX_GAP_S)
    ]
    for h in expired:
        holds.pop(h, None)
    if expired:
        state.bump_counter("known_hold_expired", len(expired))
    if not free or not holds:
        return []

    cands = []
    for hexn, e in list(holds.items()):
        if hexn in claimed_hexes:
            continue
        pred_d, pred_f, dt = _hold_predict(e, geo.fc_hz, frame_ts_s)
        d_gate = KNOWN_HOLD_DELAY_GATE_US + KNOWN_HOLD_DELAY_RATE_US_PER_S * dt
        f_gate = KNOWN_HOLD_DOPPLER_GATE_HZ + KNOWN_HOLD_DOPPLER_RATE_HZ_PER_S * dt
        cands.append((hexn, e, pred_d, pred_f, d_gate, f_gate, dt))
    if not cands:
        return []

    cost = np.full((len(free), len(cands)), _GATE_INFEASIBLE)
    for c, (_hexn, _e, pred_d, pred_f, d_gate, f_gate, _dt) in enumerate(cands):
        for r, i in enumerate(free):
            d_res = abs(pred_d - float(delays[i]))
            f_res = abs(pred_f - float(dopplers[i]))
            if d_res > d_gate or f_res > f_gate:
                continue
            cost[r, c] = d_res / d_gate + f_res / f_gate
    rows, cols = linear_sum_assignment(cost)

    node_world = state.node_world(node_id)
    out: list[tuple[int, str, dict, float, float, dict]] = []
    for r, c in zip(rows, cols):
        if cost[r, c] >= _GATE_INFEASIBLE:
            continue
        i = free[r]
        hexn, e, pred_d, pred_f, _d_gate, _f_gate, dt = cands[c]
        ref = _fresh_fix_prediction(hexn, geo, frame_ts_s, node_world)
        extra = {"hold": True, "hold_gap_s": round(dt, 3)}
        if ref is not None:
            ref_d, ref_f, scale, fresh_fix = ref
            if (
                abs(ref_d - float(delays[i])) > KNOWN_CLAIM_DELAY_GATE_US * scale
                or abs(ref_f - float(dopplers[i])) > KNOWN_CLAIM_DOPPLER_GATE_HZ * scale
            ):
                holds.pop(hexn, None)
                state.bump_counter("known_hold_dropped_disagree")
                continue
            # The transponder is live and agrees, so the claim carries THAT
            # fix, exactly as path 2 would have — and refreshes the hold with
            # it (see the caller).  Without this, path H outranking path 2
            # every frame would freeze the entry's fix at the first claim on
            # a node without tags, and the lane would read a live aircraft
            # as 45 s silent.
            fix = fresh_fix
            extra["fix_refreshed"] = True
        else:
            fix = e.get("fix")
        if not isinstance(fix, dict):
            # A hold with no fix behind it has nothing to seed the known lane
            # with, and every reader of a claim keys on adsb_fix.  Cannot
            # happen from the paths above; dropped rather than published as a
            # half-claim.
            continue
        out.append((i, hexn, fix, pred_d, pred_f, extra))
        state.bump_counter("known_hold_claims")
    return out


def claim_known_targets(node_id: str, frame: dict, follow_claimed: set[int] | None = None) -> set[int]:
    """Run the claiming stage for one frame; return the claimed detection
    indices.

    Records every claim in state.known_claims and bumps known_claims_made /
    known_claim_contentions regardless of mode — the caller decides whether
    the returned indices actually leave the dark pool (binding only).

    Two claim paths, in precedence order:
      1. Node-supplied frame["adsb"] entries become claims directly.  The
         node's own correlation is authoritative (existing invariant — the
         backend never overwrites a node-provided list), so it is not
         re-gated; the prediction is still computed so the record carries
         the residual the trust path needs.
      H. This node's own HELD tracks (state.known_track_holds): hexes this
         node has claimed before, predicted forward from their last measured
         (delay, Doppler) rather than from a transponder fix.  Ahead of path 2
         so a linked track cannot be peeled off to another hex, and therefore
         ahead of path 3 as well.  See _claim_holds.
      2. Remaining detections × fresh cached ADS-B states whose dead-reckoned
         position this node can see, global one-to-one via
         linear_sum_assignment under age-scaled gates.
      3. Dark track following (DARK_FOLLOW_MODE) — the same assignment again,
         against established dark tracks' predicted observations instead of
         ADS-B fixes.  See _claim_dark_follow.

    ``follow_claimed``, when given, is the set path 3's indices are written
    into.  They are deliberately NOT part of the return value: the two lanes
    have independent binding modes, so the caller must be able to strip one
    lane's claims from the frame without the other's.  Omit it and path 3 does
    not run at all — a caller that cannot receive the split cannot honour it.

    Claims nothing without a registered geometry: the registry contract
    requires the predicted observation, and there is nothing to predict
    with.  Fail toward dark, the same discipline every ADS-B doubt-case in
    this pipeline follows.
    """
    delays = frame.get("delay") or []
    dopplers = frame.get("doppler") or []
    if not delays:
        return set()
    geo = state.node_associator.node_geometries.get(node_id)
    if geo is None:
        return set()

    ts_ms = int(frame.get("timestamp", 0))
    frame_ts_s = ts_ms / 1000.0

    # (det_idx, hexn, adsb_fix, pred_delay_us, pred_doppler_hz, extra)
    claims: list[tuple[int, str, dict, float, float, dict]] = []
    claimed_idx: set[int] = set()
    claimed_hexes: set[str] = set()

    # ── Path 1: node-supplied tags ────────────────────────────────────────────
    node_tags = frame.get("adsb")
    if node_tags:
        for i, tag in enumerate(node_tags):
            if i >= len(delays):
                break
            if not isinstance(tag, dict):
                continue
            hexn = normalize_hex_key(tag.get("hex") or tag.get("icao"))
            if not hexn or hexn in claimed_hexes:
                continue
            lat, lon = tag.get("lat"), tag.get("lon")
            if lat is None or lon is None or not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            ve, vn = _tag_velocity(tag)
            # The node correlated this fix against this frame, so the fix is
            # taken as current — no dead-reckoning, fix_ts_ms = frame time.
            # Raw node input: alt_baro is the string "ground" on the deck.
            alt_km = as_num(tag.get("alt_baro")) * FT_TO_M / 1000.0
            pred_d, pred_f = predict_observation(geo, lat, lon, alt_km, ve, vn)
            fix = {
                "lat": lat,
                "lon": lon,
                "alt_baro": tag.get("alt_baro"),
                "gs": tag.get("gs"),
                "track": tag.get("track"),
                "fix_ts_ms": ts_ms,
            }
            claims.append((i, hexn, fix, pred_d, pred_f, {}))
            claimed_idx.add(i)
            claimed_hexes.add(hexn)

    # ── Path H: this node's own held tracks ──────────────────────────────────
    # Between path 1 and path 2 on purpose — see _claim_holds.
    for hold_claim in _claim_holds(
        node_id,
        geo,
        frame_ts_s,
        delays,
        dopplers,
        [i for i in range(len(delays)) if i not in claimed_idx],
        claimed_hexes,
    ):
        claims.append(hold_claim)
        claimed_idx.add(hold_claim[0])
        claimed_hexes.add(hold_claim[1])

    # ── Path 2: assignment over untagged detections × fresh cached states ────
    free = [i for i in range(len(delays)) if i not in claimed_idx]
    if free:
        cands = []
        visibility_rejects = 0
        world_rejects = 0
        node_world = state.node_world(node_id)
        # Prescreen constants, hoisted: geo is fixed for the whole loop, and
        # these cost a haversine and a cos each.  See the prescreen below.
        screen_r0_km = geo.effective_radius_km * _SCREEN_MARGIN
        screen_r_max_km = screen_r0_km + _V_MAX_MS * KNOWN_CLAIM_MAX_FIX_AGE_S / 1000.0
        # km per degree of longitude at the highest |latitude| any candidate
        # inside the screen could sit at, not at rx_lat: cos shrinks away from
        # the equator, so this is the SMALLEST scale factor in play and the
        # east-west term can only ever be understated.  Understating widens
        # the screen, which is the safe direction; using rx_lat would overstate
        # it for a candidate poleward of the node and could reject one the gate
        # would have passed.
        screen_km_per_lon = km_per_deg_lon(abs(geo.rx_lat) + screen_r_max_km / KM_PER_DEG_LAT)
        # The cached fixes plus the lane's own published positions for hexes
        # whose fix has gone stale (see _follow_states).  update() rather than
        # a second loop so each hex appears exactly once in the assignment;
        # the snapshot _adsb_for_seeding returns is freshly built per call, so
        # writing into it cannot touch the cache.
        cand_states = state._adsb_for_seeding()
        cand_states.update(_follow_states(frame_ts_s, claimed_hexes))
        for hexn, st in cand_states.items():
            if hexn in claimed_hexes:
                continue
            # World gate: a synthetic node's echoes can only ever be of
            # simulated aircraft, and a hardware node's only of real ones, so
            # a candidate from the other world is no candidate whatever its
            # residuals say — delay/Doppler are two numbers a wrong aircraft
            # matches by coincidence, and the visibility gate cannot help
            # when real traffic is injected over the same footprint the
            # simulated fleet flies in.  Untagged entries pass: no writer in
            # this tree leaves world unset, so an untagged entry is prior
            # state (tests, a not-yet-updated pusher) where rejecting would
            # silently disable the lane rather than fail toward dark.
            cand_world = st.get("world")
            if cand_world is not None and cand_world != node_world:
                world_rejects += 1
                continue
            age_s = frame_ts_s - st.get("timestamp_ms", 0) / 1000.0
            if abs(age_s) > KNOWN_CLAIM_MAX_FIX_AGE_S:
                continue
            # Range prescreen on the REPORTED position, ahead of the DR offset
            # and _point_in_beam's haversine + bearing.  Provably weaker than
            # the gate, so it can only reject what the gate rejects too:
            #   * every branch of _point_in_beam starts by failing anything
            #     farther from rx than effective_radius_km (footprint widened
            #     to the learned FOV's reach) and only tightens from there, so
            #     that radius is the gate's hard ceiling;
            #   * the gate tests the DEAD-RECKONED position, which sits at most
            #     _V_MAX_MS * |age_s| from the reported one — no aircraft in
            #     this system exceeds that speed (association._V_MAX_MS is the
            #     library's own ceiling on a physically possible velocity);
            #   * equirectangular distance overstates the great-circle one only
            #     by a third-order term in the angular separation (well under
            #     0.1% at these ranges) once the longitude scale is taken at the
            #     poleward end as above, and _SCREEN_MARGIN leaves 2% on top.
            # Squared comparison — the sqrt buys nothing a squared radius can't
            # answer, and this runs per cached aircraft per frame per node.
            dy = (st["lat"] - geo.rx_lat) * KM_PER_DEG_LAT
            # Wrapped to (-180, 180]: the raw difference reads ~359 degrees for
            # a close neighbour across the antimeridian, which would fail the
            # screen for a candidate the gate's haversine (which measures the
            # short way round) passes.
            dlon = st["lon"] - geo.rx_lon
            if dlon > 180.0:
                dlon -= 360.0
            elif dlon < -180.0:
                dlon += 360.0
            dx = dlon * screen_km_per_lon
            screen_r_km = screen_r0_km + _V_MAX_MS * abs(age_s) / 1000.0
            if dx * dx + dy * dy > screen_r_km * screen_r_km:
                # A prescreen failure IS a visibility reject — same event, same
                # tally, so the published rate keeps meaning what it did.
                visibility_rejects += 1
                continue
            dr_lat, dr_lon = offset_latlon_m(
                st["lat"],
                st["lon"],
                east_m=st.get("vel_east", 0.0) * age_s,
                north_m=st.get("vel_north", 0.0) * age_s,
            )
            # A false reject costs a claim the dark lane can still solve; a
            # false accept puts a fix this node never saw into the known lane
            # and charges its residual to the node's trust.  The asymmetry is
            # why this is the associator's own visibility predicate applied
            # whole (beam wedge, footprint, learned FOV, coverage prior)
            # rather than a looser bespoke one — claiming and the dark lane
            # must mean the same thing by "this node can see there".
            if not _point_in_beam(dr_lat, dr_lon, geo):
                visibility_rejects += 1
                continue
            pred_d, pred_f = predict_observation(
                geo,
                dr_lat,
                dr_lon,
                st.get("alt_m", 0.0) / 1000.0,
                st.get("vel_east", 0.0),
                st.get("vel_north", 0.0),
            )
            cands.append((hexn, st, pred_d, pred_f, _gate_scale(age_s)))

        # Once per frame, not per candidate: one lock acquisition on a path
        # that runs for every frame every node sends.
        if visibility_rejects:
            state.bump_counter("known_claims_visibility_rejects", visibility_rejects)
        if world_rejects:
            state.bump_counter("known_claims_world_rejects", world_rejects)

        if cands:
            cost = np.full((len(free), len(cands)), _GATE_INFEASIBLE)
            for c, (_hexn, _st, pred_d, pred_f, scale) in enumerate(cands):
                d_gate = KNOWN_CLAIM_DELAY_GATE_US * scale
                f_gate = KNOWN_CLAIM_DOPPLER_GATE_HZ * scale
                for r, i in enumerate(free):
                    d_res = abs(pred_d - float(delays[i]))
                    f_res = abs(pred_f - float(dopplers[i]))
                    if d_res > d_gate or f_res > f_gate:
                        continue
                    cost[r, c] = d_res / d_gate + f_res / f_gate
            rows, cols = linear_sum_assignment(cost)
            for r, c in zip(rows, cols):
                if cost[r, c] >= _GATE_INFEASIBLE:
                    continue
                i = free[r]
                hexn, st, pred_d, pred_f, _scale = cands[c]
                # A follow candidate carries the hex's ORIGINAL (stale) fix
                # rather than a fix record built from itself — see
                # _follow_fix_record.  "follow": True marks the claim for the
                # lane and for the operator; everything else about it is an
                # ordinary path-2 claim, including the hold it goes on to
                # create, which is the point: from the next frame this node
                # holds the track on its own measurements.
                follow_fix = st.get("_follow_fix")
                claims.append(
                    (
                        i,
                        hexn,
                        follow_fix if isinstance(follow_fix, dict) else _fix_record(st),
                        pred_d,
                        pred_f,
                        {"follow": True} if isinstance(follow_fix, dict) else {},
                    )
                )
                if isinstance(follow_fix, dict):
                    state.bump_counter("known_follow_claims")
                claimed_idx.add(i)

    # ── Contention, registry, counters, residual hook ─────────────────────────
    projections = _dark_global_projections(geo, frame_ts_s) if claims else []
    nb = _node_bias() if claims else None
    node_world_tag = state.node_world(node_id) if claims else None
    for i, hexn, fix, pred_d, pred_f, extra in claims:
        d_meas = float(delays[i])
        f_meas = float(dopplers[i])
        # A detection both a known hex and an established dark track can
        # explain is a genuine ambiguity: claim it (identity evidence beats a
        # dark projection) but say so, and leave the dark track alone — if
        # the dark track is real, its other nodes keep feeding it; if it is
        # this aircraft's own ghost, it expires on its own.  Deleting it here
        # would let a claim silently erase a possibly-real dark target.
        contested = _is_contested(d_meas, f_meas, projections)
        dq = state.known_claims.get(hexn)
        if dq is None:
            dq = state.known_claims.setdefault(hexn, deque(maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX))
        dq.append(
            {
                "node_id": node_id,
                "delay_us": d_meas,
                "doppler_hz": f_meas,
                "pred_delay_us": float(pred_d),
                "pred_doppler_hz": float(pred_f),
                "ts_ms": ts_ms,
                "adsb_fix": fix,
                "contested": contested,
                # "hold": True / "hold_gap_s" on a path-H claim; absent
                # otherwise, so every existing reader is unchanged.
                **extra,
            }
        )
        # Every claim is a fresh measurement of this node's track of this hex,
        # whichever path made it — that is what the hold store holds.  A hold
        # claim passes fix=None so the stored (older) fix and its fix_ts_ms
        # survive, keeping the silence visible downstream.
        _touch_hold(
            node_id,
            hexn,
            d_meas,
            f_meas,
            ts_ms,
            None if extra.get("hold") and not extra.get("fix_refreshed") else fix,
            node_world_tag,
            bool(extra.get("hold")),
        )
        state.bump_counter("known_claims_made")
        if contested:
            state.bump_counter("known_claim_contentions")
        if nb is not None:
            # Signed, measured minus predicted: the trust path estimates
            # per-node bias, and |residual| throws away the direction that
            # makes a bias a bias.
            nb.record_claim_residual(node_id, hexn, d_meas - pred_d, f_meas - pred_f, ts_ms)

    # ── Path 3: dark track following ─────────────────────────────────────────
    # Last, on what the ADS-B paths left behind — see _claim_dark_follow for
    # why that ordering is the precedence rule rather than an implementation
    # detail.
    if follow_claimed is not None:
        follow_claimed |= _claim_dark_follow(
            node_id,
            geo,
            frame_ts_s,
            ts_ms,
            delays,
            dopplers,
            [i for i in range(len(delays)) if i not in claimed_idx],
        )

    return claimed_idx


# Frame keys aligned by detection index.  snr and adsb may legitimately be
# absent; anything absent or non-list is passed through untouched.
_INDEXED_FRAME_KEYS = ("delay", "doppler", "snr", "adsb")


def strip_claimed_detections(frame: dict, claimed_idx: set[int]) -> dict:
    """Copy of `frame` with the claimed indices removed from every
    index-aligned list.

    A copy, never in-place: the original frame still feeds the archive and
    the ADS-B cache extraction downstream, and both must see what the node
    actually sent — binding changes which lane processes a detection, not
    the record of its existence.
    """
    n = len(frame.get("delay") or [])
    keep = [i for i in range(n) if i not in claimed_idx]
    out = dict(frame)
    for key in _INDEXED_FRAME_KEYS:
        v = frame.get(key)
        if isinstance(v, list):
            out[key] = [v[i] for i in keep if i < len(v)]
    return out
