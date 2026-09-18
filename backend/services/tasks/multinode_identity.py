"""Multinode track identity: which state.multinode_tracks entry a solve belongs to.

An entry is one aircraft, not one solve, so each published solve is keyed onto
a live entry or mints a new one (multinode_key_decision), a key found to
duplicate another is merged into it (_supersession_match,
_stale_coast_candidate), and a retired key is erased from every store
(_forget_mn_key).  The EWMA smoother lives here as well because its history is
keyed by the same track key and _forget_mn_key has to erase it.

The keying and supersession calls run under state.multinode_tracks_lock, taken
by their callers; _MN_POS_HISTORY_LOCK nests inside it.
"""

import logging
import math
import os
import threading
from collections import deque
from collections.abc import Iterable

from config.constants import (
    ARC_ONLY_ANOMALY_ALLOWLIST,
    MN_STALE_COAST_MANOEUVRE,
    MN_STALE_COAST_MAX_KM,
    MN_STALE_COAST_MAX_S,
    MN_STALE_COAST_MIN_S,
    MN_STALE_COAST_VMAX_MS,
)
from core import state
from services import dark_follow, track_filter
from services.geo import haversine_km as _haversine_km
from services.geo import offset_latlon_m
from services.id_utils import is_transponder_hex, multinode_hex_from_key

# ── Multi-epoch EWMA position smoother (all N) ───────────────────────────────
# This EWMA machinery is now the TRACK_SMOOTHER=ewma fallback; the default
# smoother is the Kalman filter in services/track_filter.py.
#
# Each multinode solve has position error σ_pos = GDOP × σ_delay.  By
# accumulating K successive solver positions for the same aircraft (identified
# by ICAO hex) and dead-reckoning earlier positions forward to the current
# solve time using ADS-B velocity, we average K independent noise realisations
# and reduce the effective σ_pos by 1/√K.
#
# K=3 frames (every ~40 s) reduces mean error by ~√3 = 1.73×.
#
# Originally gated on n_nodes == 2 only, but the math is N-agnostic — a single
# bad measurement at any N drags that frame's solve off by O(σ × GDOP), and
# multi-frame averaging on the same aircraft pulls the result back to the
# track-mean for the same √K reason.  Production stats showed the apparent
# "N=2 better than N=3" inversion was an artefact of the smoother only being
# applied to N=2; lifting the gate puts every solve on the same footing.
#
# Dead-reckoning prefers ADS-B ground-speed + track (independent of solver
# error) and falls back to the solved velocity when the target is dark.  Dark
# targets are the population where MLAT is the only position source — and they
# used to be the only population excluded from this smoothing (the old code
# required an ADS-B hex AND a live ADS-B entry, returning dark solves raw).
# History is keyed by the multinode track key so identity is shared with
# state.multinode_tracks by construction.

# Per-hex rolling buffer: hex → deque of (lat, lon, timestamp_s)
_MN_POS_HISTORY: dict[str, deque] = {}
_MN_POS_HISTORY_LOCK = threading.Lock()
_MN_HISTORY_K = 3  # number of past frames to average (including current)
_MN_DR_MAX_AGE_S = 160.0  # discard history entries older than 4 frame intervals
# Dict-level TTL sweep (the deques cap per-hex growth, but the dict itself
# grew one entry per distinct hex for the process lifetime — same shape as
# _TRACK_CLAIMS, which already expires).  Swept opportunistically on insert.
_MN_HISTORY_TTL_S = 600.0
_mn_history_last_sweep = 0.0


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    global _mn_history_last_sweep
    with _MN_POS_HISTORY_LOCK:
        _MN_POS_HISTORY.clear()
        _mn_history_last_sweep = 0.0


def mn_pos_history_size() -> int:
    """How many track keys the smoother holds history for."""
    with _MN_POS_HISTORY_LOCK:
        return len(_MN_POS_HISTORY)


def _sweep_mn_history(now_s: float) -> None:
    """Drop hexes whose newest sample is stale.  Caller holds the lock."""
    global _mn_history_last_sweep
    if now_s - _mn_history_last_sweep < _MN_HISTORY_TTL_S / 10:
        return
    _mn_history_last_sweep = now_s
    for h in [h for h, dq in _MN_POS_HISTORY.items() if not dq or now_s - dq[-1][2] > _MN_HISTORY_TTL_S]:
        del _MN_POS_HISTORY[h]


def _ewma_smooth_track(result: dict, track_key: str, adsb_hex: str | None) -> dict:
    """Apply dead-reckoned multi-epoch averaging to a multinode solver result.

    Dead-reckon previous solve positions for the same track to the current
    solve timestamp, then return the simple mean of the dead-reckoned history
    and the current solve.  Thread-safe via lock.

    History is keyed by the multinode track key, so dark solves accumulate
    and average exactly like ADS-B-tagged ones.  The DR velocity prefers live
    ADS-B ground-speed/track (known independently of the solver) and falls
    back to the solved velocity for dark targets — association-seeded and
    CV-confirmed for n=2, and honest for n≥3 now that vz cannot absorb the
    Doppler misfit.
    """
    r_lat = result["lat"]
    r_lon = result["lon"]
    r_ts = result.get("timestamp_ms", 0) / 1000.0

    # Aged against this solve's own epoch, not wall clock, and by the same
    # rule the KF smoother applies — a fix older than the cap is a heading
    # from before the aircraft went silent, and dead-reckoning the history
    # along it walks the smoothed position off the track.  See
    # track_filter._adsb_velocity.
    has_adsb_vel, v_east_ms, v_north_ms = track_filter._adsb_velocity(adsb_hex, result.get("timestamp_ms"))
    if has_adsb_vel:
        vel_east_kms = v_east_ms / 1000.0
        vel_north_kms = v_north_ms / 1000.0
    else:
        vel_east_kms = float(result.get("vel_east") or 0.0) / 1000.0
        vel_north_kms = float(result.get("vel_north") or 0.0) / 1000.0

    with _MN_POS_HISTORY_LOCK:
        _sweep_mn_history(r_ts)
        hist = _MN_POS_HISTORY.setdefault(track_key, deque(maxlen=_MN_HISTORY_K))

        # Dead-reckon each past position forward to the current solve time
        # and collect valid points (not too stale, not too far after dr).
        positions: list[tuple[float, float]] = [(r_lat, r_lon)]

        for prev_lat, prev_lon, prev_ts in hist:
            dt = r_ts - prev_ts
            if dt <= 0 or dt > _MN_DR_MAX_AGE_S:
                continue  # skip future or stale entries
            dr_lat, dr_lon = offset_latlon_m(
                prev_lat,
                prev_lon,
                east_m=vel_east_kms * 1000.0 * dt,
                north_m=vel_north_kms * 1000.0 * dt,
            )
            positions.append((dr_lat, dr_lon))

        # Push current position before averaging (so it is included next time).
        hist.append((r_lat, r_lon, r_ts))

    if len(positions) < 2:
        return result  # no usable history yet—return raw solve

    avg_lat = sum(p[0] for p in positions) / len(positions)
    avg_lon = sum(p[1] for p in positions) / len(positions)

    smoothed = dict(result)
    smoothed["lat"] = round(avg_lat, 6)
    smoothed["lon"] = round(avg_lon, 6)
    logging.debug(
        "EWMA: key=%s K=%d raw=(%.4f,%.4f) → smooth=(%.4f,%.4f)",
        track_key,
        len(positions),
        r_lat,
        r_lon,
        avg_lat,
        avg_lon,
    )
    return smoothed


# ── Multinode track identity ─────────────────────────────────────────────────
# An entry in state.multinode_tracks is ONE AIRCRAFT, not one solve.  Every node
# runs its own association round (ASSOC_MIN_INTERVAL_S), so the same aircraft is
# re-solved repeatedly from different nodes' frames; those solves must update a
# single entry.  Keying on the solve timestamp and latitude — the original
# behaviour — made every solve a distinct "aircraft": one target rendered as 4-6
# overlapping icons that drifted apart and then tripped the position-mismatch
# and supersonic anomaly detectors.

# Dark-target association gate, at dt=0.  retina_analytics.association uses the
# same 6 km (_MERGE_DIST_KM) for within-round clustering, so this keeps the
# cross-round gate no tighter than the one already applied per round.
_MN_ASSOC_MAX_DIST_KM = 6.0

# ...and how that gate GROWS with the age of the entry being matched against.
#
# The proximity branch does not compare two simultaneous positions: it compares
# this solve against an existing entry dead-reckoned forward over dt seconds.
# The error in that prediction is dominated by the velocity it was dead-reckoned
# with, which is measured (aircraft_feed.py's DR block, 2026-08-09, n=93) at a
# median 127 m/s vector error — 0.13 km of extra uncertainty per second of age,
# on top of the ~1–2 km the solve positions themselves carry (published dark
# solves: 0.98 km median GT error, p90 2.5–7.9 km).  A flat 6 km therefore
# judges a 2 s-old entry far too loosely and a 40 s-old one far too tightly.
#
# Measured on this deployment (26 min): 57 dark key births, only 3 of them
# while the same aircraft already had a live key — so simultaneous duplicates
# are not the problem, sequential re-keying is.  Classified by distance from the
# new key to the nearest dark entry alive in the previous frame, 11 of the 57
# landed at 6–10 km with that predecessor then disappearing within 10 s: the
# same target, re-keyed purely because its predecessor's dead-reckoned position
# had drifted past the flat 6 km.  Those 11 are what this slope is for.  The
# 23 at 10–20 km and the 20 with nothing within 20 km are out of its reach by
# construction, and deliberately so.
#
# 0.13 km/s is the measured velocity error itself, not a multiple of it: one
# sigma of DR drift, added to a 6 km base that already covers the solve error.
# 6 km at dt=0, ~9.9 km at 30 s, and the cap from ~46 s to the 60 s age limit.
_MN_ASSOC_DRIFT_KM_PER_S = 0.13
# Ceiling on the grown gate.  Simulated aircraft are deconflicted to >=5 km
# horizontally at spawn (retina_simulation.world), and two real targets closer
# than a few km are not separable by this pipeline anyway — but a gate that
# grew unbounded would eventually swallow a genuinely different aircraft in
# the same sector.  12 km = twice the flat gate: past the 6–10 km band the
# measurement puts the re-keys in, short of the 10–20 km band where the same
# measurement shows predecessors that mostly lived on (13 of 23).
_MN_ASSOC_MAX_DIST_CAP_KM = 12.0
# Never associate to an entry the map has already dropped (the 60 s expiry in
# frame_processor.build_combined_aircraft_json).
_MN_ASSOC_MAX_AGE_S = 60.0
# ...and how far the other way.  A solve may be matched to an entry whose
# measurement epoch is up to this much LATER than the solve's own — dt < 0,
# the entry stamped from a measurement taken after this one.
#
# Solves do not arrive in measurement order.  Two lanes now solve the same
# dark aircraft from different epochs (the top-down dark-follow lane and the
# bottom-up association lane) and the solver pool runs several workers, so a
# solve 1-4 s OLDER than the entry just published for the same aircraft is
# routine: measured live on test (2026-09-06, 927 published dark solves in
# 22 min), 56 of 920 consecutive dark publishes had measurement time going
# backwards.  Under the old `0.0 <= dt` rule the scan refused to even
# consider those entries: 16 of the 67 fresh mn-dark-* keys minted in the
# window were for an aircraft whose key had been published 0.0-1.8 s of wall
# time earlier and 0.1-3.8 km away — well inside the 6 km gate — with a
# measurement dt of -1 to -4 s.  Every one of those is a duplicate track for
# one aircraft (the map draws two icons until the old key expires) and an
# inflated dark_keys_minted.
#
# 10 s bounds it well clear of that 1-4 s jitter while keeping the case the
# old rule was really about: past that, the ordering is not pool/lane jitter
# but a stale queue item (_SOLVER_MAX_QUEUE_AGE_S is 45 s), and running an
# entry backwards over ten-plus seconds of dead reckoning to manufacture a
# match is exactly the fabrication the old rule refused.
_MN_ASSOC_MAX_NEG_DT_S = 10.0

# ── Node-track continuity ────────────────────────────────────────────────────
# Every solver input carries the node-level tracker track ids it was built from
# (s_in["track_ids"], from retina_analytics association).  Two dark solves that
# were built from some of the SAME node tracks are usually the same aircraft,
# and that is evidence the distance-only keying rule above throws away.
#
# Measured on the test deployment (captures 16-18, 2026-09-07) over pairs of
# dark solves keyed differently within 40 s, by shared node track ids and
# dead-reckoned distance — "precision" is P(same ground-truth aircraft):
#
#   shared   <2 km      2-4 km     4-6 km     6-10 km
#   0        0.67       0.49       0.24       -
#   1        1.00 (14)  0.71 (28)  0.92 (13)  0.15 (47)
#   >=2      1.00 (21)  0.89 (28)  0.60 (15)  0.42 (31)
#
# Two things follow.  Inside the association gate a shared track id is strong
# evidence of identity (84-94% pooled) and worth more than a couple of km of
# proximity; OUTSIDE it the precision collapses to 0.15-0.42, i.e. shared node
# tracks are ALSO what cross-aircraft association candidates look like at
# range.  So this evidence is only ever used to re-rank candidates the gate
# already admits, and the gate itself is not widened by so much as a metre.
#
# The memory window is short because the ids are: a node track id appears in
# consecutive dark solves for a median of 7 s (p90 32 s) before the tracker
# retires or re-numbers it, so beyond ~40 s an id match is a coincidence of
# id reuse rather than continuity.  40 s also sits just under the 60 s entry
# expiry, so no entry carries ids from a life it has otherwise forgotten.
TRACK_LINK_AGE_S = float(os.getenv("TRACK_LINK_AGE_S", "40"))
# How many shared node tracks it takes to join a key the follow lane owns.
# Ownership (dark_follow.DARK_FOLLOW_OWN_S) exists to stop a DIFFERENT aircraft
# stealing an established key, and at >=2 shared tracks inside the gate the
# measurement says that is 89-100% not what is happening — it is the same
# aircraft, solved bottom-up, 2-3 km from where the follow lane put it (n=3
# solve scatter).  15 of 35 mints that had a same-aircraft predecessor within
# 40 s were exactly this: follow-owned predecessor, inside the gate, beyond the
# 2 km shadow radius, and 11 of the 15 shared node tracks with it.
TRACK_LINK_MIN_SHARED_JOIN = int(os.getenv("TRACK_LINK_MIN_SHARED_JOIN", "2"))
# Cap on the per-entry id memory.  A dark solve carries 2-6 track ids and the
# window holds ~40 s of them, so this bounds a pathological key (a merged or
# thrashing entry) rather than the normal case.
TRACK_LINK_MAX_IDS = int(os.getenv("TRACK_LINK_MAX_IDS", "24"))

# ── Supersession gate ────────────────────────────────────────────────────────
# Supersession is DESTRUCTIVE in a way keying is not.  Keying to the wrong
# entry writes one bad position that the next solve corrects; superseding the
# wrong entry deletes a live aircraft's key outright, with its Kalman state,
# its position history and its follow-lane identity, and that aircraft's next
# solve mints a fresh key and restarts from nothing.  So the same evidence that
# is good enough to key is NOT good enough to pop, and this gate is tighter
# than _MN_ASSOC_MAX_DIST_KM on purpose.
#
# Measured over three 20 min ground-truth captures on the test deployment
# (2026-09-05, 129 supersessions): 63 of the 129 popped a DIFFERENT ground-truth
# aircraft's key.  The shape is always the same — three dark aircraft 5-6 km
# apart inside the same nodes' cones, a 3-4 node solve of B carrying 4.5-10 km
# of position error landing inside the 6 km gate of A's established key (up to
# 71 solves old) and popping it, after which A is drawn 5+ km off for several
# seconds under a brand-new key.  For well-covered dark aircraft this is now
# the dominant miss.
#
# 4 km is where the replay puts the knee.  Replaying the recorded 129
# supersessions offline: base 4 km AND |dalt| <= 1000 m gives 7 cross-aircraft
# pops (from 56), 33 same-aircraft pops (from 64) and 1 pop by a bad new solve
# (from 12).  Distance alone at 3 km is worse on every axis that matters
# (9 / 41 / 3), and altitude alone at the old 6 km base leaves 8 cross-aircraft
# pops while keeping 26 same-aircraft ones — cheaper, but it does not close the
# neighbour-pop hole this exists for.  The 4 km base still grows with entry age
# through _mn_assoc_gate_km, so an old entry is not judged by a dt=0 number.
_MN_SUPERSEDE_BASE_KM = 4.0
# ...and the altitude half of the same gate.  Horizontal error is what a bad
# multinode solve has a lot of; altitude is the axis on which two aircraft in
# the same sector actually differ, and the one a wrong solve does not usually
# reproduce.  In the same captures, cross-aircraft pops sat at a median
# |daltitude| of ~2.7 km between the old entry and the new solve while
# same-aircraft pops sat at ~0.4 km, and the same-aircraft pops that did exceed
# 1 km were almost all bad solves (new solve error > 2 km) — pops worth losing.
# Fail-open when either altitude is missing, like the rest of the pipeline's
# optional fields: an entry with no altitude is judged on distance alone rather
# than being made unpoppable.
_MN_SUPERSEDE_MAX_ALT_DIFF_M = 1000.0


def _mn_assoc_gate_km(dt_s: float, base_km: float = _MN_ASSOC_MAX_DIST_KM) -> float:
    """Proximity gate for an entry last solved ``dt_s`` seconds ago.

    ``base_km + _MN_ASSOC_DRIFT_KM_PER_S * dt``, capped at
    _MN_ASSOC_MAX_DIST_CAP_KM — the constants above carry the measurement.
    The cap is floored at ``base_km`` so a caller widening the base (tests,
    the bench) can never end up with a gate tighter than the one it asked for.
    """
    return min(base_km + _MN_ASSOC_DRIFT_KM_PER_S * max(dt_s, 0.0), max(_MN_ASSOC_MAX_DIST_CAP_KM, base_km))


def _entry_dr_velocity(key: str, entry: dict, learned_vel_fn) -> tuple[float, float]:
    """(vel_east_ms, vel_north_ms) to dead-reckon an existing entry with.

    The same choice services/aircraft_feed.py makes for the position it
    DRAWS — the KF's learned velocity when the filter has state for this key,
    the raw solved velocity otherwise, and the raw one unconditionally under
    TRACK_DR_SOURCE=solve.  Deliberately shared semantics: if the key decision
    dead-reckoned an entry somewhere other than where the feed draws it, a
    solve could match an entry that is not under it on the map (or fail to
    match one that is), and the two would disagree about the same aircraft.

    ``learned_vel_fn`` is injected (defaulting to track_filter.learned_velocity
    at the call site) so this stays testable without KF state, and so the
    offline bench — which has no filter — takes the fallback naturally rather
    than through a mode flag.  learned_velocity takes _KF_LOCK, a leaf lock;
    the established solver.py -> track_filter order is what the caller already
    uses for smooth_solve under state.multinode_tracks_lock.
    """
    if learned_vel_fn is not None and (os.getenv("TRACK_DR_SOURCE", "kf") or "kf").strip().lower() != "solve":
        lv = learned_vel_fn(key)
        if lv is not None:
            return float(lv[0]), float(lv[1])
    return float(entry.get("vel_east") or 0.0), float(entry.get("vel_north") or 0.0)


def _collect_track_anomalies(s_in, result: dict) -> None:
    """Stamp contributing-track anomaly flags onto a multinode result.

    A multinode solve is built from per-node tracker tracks; if any of them
    flagged an anomaly (supersonic Doppler for dark targets, the ADS-B streams
    otherwise), the solved track should carry it — before this, multinode
    entries hardcoded is_anomalous False, so a dark anomalous target went
    quiet the moment it was solved.  Dark solves are restricted to the same
    physically-loud allowlist as arc-only tracks.  The caller latches the
    flags with the previous entry under state.multinode_tracks_lock so a
    one-frame tracker flag survives for the multinode track's lifetime.
    """
    track_ids = set(s_in.get("track_ids") or []) if isinstance(s_in, dict) else set()
    anom_types: set[str] = set()
    is_anom = False
    max_vel = 0.0
    if track_ids:
        for nid in result.get("contributing_node_ids", []):
            pipeline = state.node_pipelines.get(nid)
            if pipeline is None:
                continue
            try:
                tracks = list(pipeline.tracker.tracks)
            except Exception:
                continue
            for t in tracks:
                if getattr(t, "track_id", None) not in track_ids:
                    continue
                anom_types |= set(getattr(t, "anomaly_types", None) or ())
                is_anom = is_anom or bool(getattr(t, "is_anomalous", False))
                max_vel = max(max_vel, getattr(t, "max_velocity_ms", 0.0) or 0.0)
    if not result.get("adsb_hex"):
        anom_types &= ARC_ONLY_ANOMALY_ALLOWLIST
        is_anom = bool(anom_types)
    result["anomaly_types"] = sorted(anom_types)
    result["is_anomalous"] = is_anom or bool(anom_types)
    if max_vel:
        result["max_velocity_ms"] = round(max_vel, 1)


def merge_recent_track_ids(
    prev: dict | None,
    track_ids: Iterable[str] | None,
    ts_s: float,
    max_age_s: float = TRACK_LINK_AGE_S,
    max_ids: int = TRACK_LINK_MAX_IDS,
) -> dict[str, float]:
    """This solve's node track ids merged into the entry's short id memory.

    ``{track_id: last_seen_ts_s}``, carried on the multinode entry as
    ``recent_track_ids`` so the next solve's keying decision can ask whether it
    was built from any of the same node tracks (see TRACK_LINK_AGE_S).  Pruned
    on every write rather than on read — the entry is written far less often
    than the keying scan reads it, and an unpruned dict would keep growing for
    a long-lived key.  Newest ids win the cap, since the whole point of the
    memory is recency.
    """
    out: dict[str, float] = {}
    for tid, seen in (prev or {}).items():
        try:
            seen_f = float(seen)
        except (TypeError, ValueError):
            continue
        # Symmetric in time for the same reason dark_follow.recently_followed
        # is: solves arrive out of measurement order, so an id stamped by a
        # LATER solve is still evidence about this one.
        if abs(ts_s - seen_f) <= max_age_s:
            out[str(tid)] = seen_f
    fresh = {str(tid): ts_s for tid in track_ids or ()}
    if len(out) + len(fresh) > max_ids:
        # This solve's own ids are the freshest evidence the next decision has,
        # so they are never the ones the cap drops — the inherited memory is
        # trimmed to fit around them, oldest first.
        room = max(max_ids - len(fresh), 0)
        keep = sorted(
            ((tid, seen) for tid, seen in out.items() if tid not in fresh), key=lambda kv: kv[1], reverse=True
        )
        out = dict(keep[:room])
    out.update(fresh)
    return out


def _shared_recent_tracks(prev: dict, solve_ids: set[str], ts_s: float, max_age_s: float = TRACK_LINK_AGE_S) -> int:
    """How many of this solve's node track ids the entry has seen recently."""
    if not solve_ids:
        return 0
    recent = prev.get("recent_track_ids")
    if not isinstance(recent, dict):
        return 0
    n = 0
    for tid in solve_ids:
        seen = recent.get(tid)
        if seen is None:
            continue
        try:
            if abs(ts_s - float(seen)) <= max_age_s:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def multinode_key_decision(
    tracks: dict[str, dict],
    result: dict,
    adsb_hex: str | None,
    anchor_key: str | None,
    max_dist_km: float = _MN_ASSOC_MAX_DIST_KM,
    max_age_s: float = _MN_ASSOC_MAX_AGE_S,
    learned_vel_fn=track_filter.learned_velocity,
    anchor_dr: bool = False,
    track_ids: Iterable[str] | None = None,
) -> tuple[str, str, float | None, float | None]:
    """The keying rule itself, clock-free — the multinode-track analogue of
    solver.py's claim_decision.  Extracted so the offline bench measures the
    SHIPPED rule by construction (the same reason claim_decision is imported
    at association_bench.py's top-of-file import), and so solver.py's
    _process_solver_item can observe which branch fired for its anchor
    counters.

    Clock-free but no longer strictly pure: the proximity scan asks the KF
    for each candidate's learned velocity (``learned_vel_fn``, injectable and
    defaulting to the real accessor) and reads TRACK_DR_SOURCE, both so its
    dead-reckoning matches the position the feed draws — see
    _entry_dr_velocity.  Every time-of-day still arrives in `result` and
    `tracks`, so the rule remains replayable against recorded data.  The DR
    horizon deliberately is NOT clamped to the feed's 30 s display cap: that
    cap limits how far a stale marker may slide across the map, while what
    this scan needs is the best available estimate of where the aircraft
    actually is, and the max_age_s window already bounds dt.

    Caller holds state.multinode_tracks_lock — it reads `tracks` and the
    caller writes back into it under the same lock.  Returns
    (key, how, dist_km, dt_s) with
    how in {"adsb", "anchor", "proximity", "tracks", "shadowed", "minted"};
    dist_km is
    how far this solve landed from the entry it was keyed onto (dead-reckoned,
    for the proximity, tracks and shadowed branches) and None where nothing was
    matched — "adsb" and "minted".  dt_s is the SIGNED age of that entry at
    this solve's epoch (positive = the entry was measured first, negative =
    this solve's measurement is the older of the two), None wherever dist_km
    is.  The caller stamps all three onto the solve-history record, which is
    the only way to tell a re-key apart from a
    fragment after the fact.  "shadowed" is the one verdict that is NOT a key:
    it names the followed key this solve was refused in favour of, and the
    caller must treat it as a rejection (see solver.py's _process_solver_item).

    Order:
      1. ADS-B-tagged solves key on the transponder hex — unconditional, and
         this key is also the smoother's history key (_ewma_smooth_track),
         so the smoother and the track store agree by construction.
      2. Anchor honoring.  anchor_key is set only by an anchored solver
         input (top-down claiming, ASSOC_CLAIM_MODE=active) — mn-dark-*,
         still live in `tracks`, and within max_dist_km of THIS solve's own
         result.  That distance check is what closes the
         consensus-anchored-displacement edge case: the n>=3 displacement
         gate at :~1320 already re-anchors to the consensus centroid rather
         than the claim guess for a consensus-selected solve, so an anchor
         whose claim guess was wrong but whose consensus-corrected result
         still landed near the claimed track is legitimate, while one that
         converged somewhere else entirely is not honored just because a
         claim was attempted.
      3. DR proximity scan: only dark tracks are claimable — an untagged
         solve must never steal the identity of an ADS-B-tagged aircraft
         that happens to be nearby — dead-reckoned to this solve's epoch so
         a fast target is not rejected purely for having moved since its
         last solve.  The gate each candidate is judged against grows with
         ITS OWN age (_mn_assoc_gate_km), because that is what the
         dead-reckoning error does; candidates compete on distance
         normalised by their own gate, so a fresh close entry beats an old
         far one rather than the scan simply taking whichever is nearer in
         kilometres.

         The dt window is SIGNED: -_MN_ASSOC_MAX_NEG_DT_S <= dt <=
         max_age_s, and the dead reckoning uses the signed dt, so a
         candidate measured AFTER this solve is offset BACKWARDS along its
         velocity — which is exactly where that aircraft was at this solve's
         epoch, not a fabrication.  Solves reach here out of measurement
         order as a matter of course (two lanes, several pool workers); see
         _MN_ASSOC_MAX_NEG_DT_S for the measurement and for why 10 s is the
         bound.  _mn_assoc_gate_km clamps dt at 0, so a negative-dt
         candidate is judged against the base gate with no drift allowance —
         deliberate: the DR is short and the gate should not be widened for
         a direction the drift measurement never covered.

         KEY OWNERSHIP (DARK_FOLLOW_MODE=binding only).  A key the follow
         lane published on within dark_follow.DARK_FOLLOW_OWN_S is removed
         from this scan's candidates entirely, and if the nearest such key is
         within DARK_FOLLOW_SHADOW_KM the solve is refused ("shadowed")
         instead of keyed at all.  The follow lane already supplies every
         solve an established track needs, so a bottom-up solve arriving at
         one of its keys is either a duplicate — competing with the anchored
         solve and dragging the filter — or a different aircraft stealing the
         key; 21% of proximity joins measured on test were the latter.  See
         dark_follow.DARK_FOLLOW_OWN_S for why a tighter gate cannot separate
         the two.

         NODE-TRACK CONTINUITY ("tracks").  ``track_ids`` are this solve's
         node-level tracker track ids, and each entry remembers the ones its
         recent solves were built from (recent_track_ids, TRACK_LINK_AGE_S).
         Sharing them is direct evidence that two solves are of one aircraft,
         and it is used ONLY to re-rank candidates the gate already admits —
         see TRACK_LINK_AGE_S for the measured precision, which is 84-94%
         inside the gate and 0.15-0.42 outside it, so the gate is never
         widened by this and a shared id can never rescue an out-of-gate
         candidate.  Two things change inside the gate:
           - A follow-owned candidate sharing >= TRACK_LINK_MIN_SHARED_JOIN
             ids is JOINED rather than skipped (how "tracks").  Ownership is
             there to stop a different aircraft stealing the key, and >=2
             shared node tracks in-gate say this is the same aircraft the
             follow lane is already solving, landing 2-3 km out on n=3 solve
             scatter.  One shared id is weaker (0.71-0.92) — not enough to
             take the key, but enough to refuse the solve as a duplicate, so
             it shadows beyond DARK_FOLLOW_SHADOW_KM instead of minting.
           - Other candidates score d/gate divided by (1 + shared, capped at
             4), so a track-sharing entry outranks a nearer stranger; the
             winner is reported as "tracks" when it shared anything at all
             and "proximity" when it did not.
      4. Mint.  This key only needs to be unique at birth; every later solve
         associates to it above (by proximity, or by anchor once a claim
         forms), so it stays stable.
    """
    # Transponder-shaped ids only.  adsb_hex is whatever upstream association
    # produced, and a non-transponder id here (a simulator object id, a claim
    # against a poisoned adsb_aircraft entry) would put a dark target in the
    # ADS-B lane — adsb_assisted=true on the feed, and the mn-dark-* store
    # (so the anchor and proximity branches below) starved forever.
    if adsb_hex and is_transponder_hex(adsb_hex):
        return f"mn-adsb-{adsb_hex}", "adsb", None, None

    lat, lon = result["lat"], result["lon"]
    ts_s = result.get("timestamp_ms", 0) / 1000.0

    # The anchor branch keeps the FLAT gate.  It is not a dead-reckoning
    # question: the claim named this entry as the aircraft this solve is of,
    # and the distance check exists only to refuse an anchor whose solve
    # converged somewhere else entirely.  Nothing here is predicting where the
    # anchor drifted to, so there is no drift term to allow for.
    #
    # ...unless the caller says otherwise (anchor_dr).  A dark-follow input
    # (services/dark_follow.py) breaks that premise by construction: its guess
    # IS a prediction of where the anchor drifted to, so its solve is compared
    # against an entry the follow lane already knows to be stale.  The numbers
    # make it more than a nicety — the dark displacement cap is 6.0 km and the
    # flat anchor gate is 6.0 km, so a solve at the edge of the gate that let
    # it through is at the edge of the gate that must key it, before any drift
    # is added; at the follow lane's 20 s staleness limit a 270 m/s target adds
    # another 5.4 km of it.  Without this the anchor would be refused exactly
    # when the aircraft is moving fastest, and the solve would fall through to
    # the proximity scan the whole lane exists to stop relying on.  Same DR and
    # same age-scaled gate as that scan, so "near the anchor" means one thing.
    if anchor_key and anchor_key.startswith("mn-dark-") and anchor_key in tracks:
        anchor = tracks[anchor_key]
        a_lat, a_lon = anchor.get("lat"), anchor.get("lon")
        if a_lat is not None and a_lon is not None:
            a_gate_km = max_dist_km
            a_dt = ts_s - anchor.get("timestamp_ms", 0) / 1000.0
            if anchor_dr and 0.0 < a_dt <= max_age_s:
                a_vel_east, a_vel_north = _entry_dr_velocity(anchor_key, anchor, learned_vel_fn)
                a_lat, a_lon = offset_latlon_m(
                    a_lat,
                    a_lon,
                    east_m=a_vel_east * a_dt,
                    north_m=a_vel_north * a_dt,
                )
                a_gate_km = _mn_assoc_gate_km(a_dt, max_dist_km)
            a_dist = _haversine_km(lat, lon, a_lat, a_lon)
            if a_dist <= a_gate_km:
                return anchor_key, "anchor", a_dist, round(a_dt, 3)

    # Candidates compete on d / gate_km, not on d: an entry solved 2 s ago at
    # 5 km is a worse match than one solved 40 s ago at 8 km only if you
    # ignore that the second one's position is a 40 s extrapolation.  A score
    # < 1.0 is inside that candidate's own gate; the initial 1.0 is therefore
    # the "no candidate" sentinel and keeps the old strict-inequality
    # behaviour at exactly the gate distance.
    best_key: str | None = None
    best_score = 1.0
    best_dist: float | None = None
    best_dt: float | None = None
    best_shared = 0
    # This solve's node track ids, and the best follow-owned candidate that
    # shares enough of them to be joined outright (see TRACK_LINK_AGE_S).
    solve_ids = {str(t) for t in (track_ids or ())}
    link_key: str | None = None
    link_score = 1.0
    link_dist: float | None = None
    link_dt: float | None = None
    # A follow-owned candidate sharing exactly one id: too weak to take the
    # key, strong enough to refuse the solve however far out it landed.
    weak_link_key: str | None = None
    weak_link_dist: float | None = None
    weak_link_dt: float | None = None
    # Key ownership: the nearest key the follow lane is currently answering
    # for, and how far this solve landed from it.  Only collected for a
    # bottom-up solve in binding mode — an anchored or ADS-B solve names the
    # aircraft it is of, and neither branch above reaches this scan.
    shadow_scan = not anchor_key and dark_follow.mode() == "binding"
    shadow_key: str | None = None
    shadow_dist: float | None = None
    shadow_dt: float | None = None

    for key, prev in tracks.items():
        # Only dark tracks are claimable; an untagged solve must never steal the
        # identity of an ADS-B-tagged aircraft that happens to be nearby.
        if not key.startswith("mn-dark-"):
            continue
        dt = ts_s - prev.get("timestamp_ms", 0) / 1000.0
        if not (-_MN_ASSOC_MAX_NEG_DT_S <= dt <= max_age_s):
            continue
        p_lat, p_lon = prev.get("lat"), prev.get("lon")
        if p_lat is None or p_lon is None:
            continue
        # Dead-reckon the existing track to THIS solve's epoch before
        # measuring, so a fast target is not rejected purely for having moved
        # between the two measurements.  Same velocity the feed draws this
        # entry with (_entry_dr_velocity), and the signed dt: a negative one
        # walks the entry backwards along its own velocity, to where the
        # aircraft was when this (older) measurement was taken.
        vel_east_ms, vel_north_ms = _entry_dr_velocity(key, prev, learned_vel_fn)
        p_lat, p_lon = offset_latlon_m(
            p_lat,
            p_lon,
            east_m=vel_east_ms * dt,
            north_m=vel_north_ms * dt,
        )
        d = _haversine_km(lat, lon, p_lat, p_lon)
        gate_km = _mn_assoc_gate_km(dt, max_dist_km)
        # How much of this solve's node-track evidence this entry has seen.
        shared = _shared_recent_tracks(prev, solve_ids, ts_s)
        # A key the follow lane just published on is not joinable bottom-up on
        # distance alone, whatever the distance says — see
        # dark_follow.DARK_FOLLOW_OWN_S for the measurement.  It still competes
        # to SHADOW this solve below, so the scan has to remember the nearest
        # one rather than skipping it; and shared node tracks inside the gate
        # answer the one question ownership was standing in for (is this the
        # same aircraft?) directly, so they can join it after all.
        if shadow_scan and dark_follow.recently_followed(key, ts_s, dark_follow.DARK_FOLLOW_OWN_S):
            if shared >= TRACK_LINK_MIN_SHARED_JOIN and d <= gate_km:
                l_score = d / gate_km
                if link_key is None or l_score < link_score:
                    link_key, link_score, link_dist, link_dt = key, l_score, d, dt
            elif shared >= 1 and d <= gate_km and weak_link_key is None:
                weak_link_key, weak_link_dist, weak_link_dt = key, d, dt
            if shadow_dist is None or d < shadow_dist:
                shadow_key, shadow_dist, shadow_dt = key, d, dt
            continue
        # Shared node tracks discount the distance score so a track-sharing
        # entry beats a nearer stranger, but only among candidates the gate
        # already admits: outside it the same evidence is 0.15-0.42 precise,
        # so the gate check stays explicit rather than riding on the score
        # being < 1.0 as it did when score was exactly d / gate.
        if d >= gate_km:
            continue
        score = (d / gate_km) / (1 + min(shared, 3))
        if score < best_score:
            best_key, best_score, best_dist, best_dt, best_shared = key, score, d, dt, shared

    # Close enough to a followed key that this solve is the same aircraft the
    # follow lane is already solving: refuse it outright rather than mint a
    # second key for a target that already has one.  Farther away it falls
    # through to the non-followed candidates and, failing those, mints — the
    # one thing it may never do is join the followed key.
    #
    # ...unless the node tracks say this IS that aircraft, in which case the
    # solve joins the followed key instead of being thrown away: >=2 shared
    # ids inside the gate is 89-100% the same target, and refusing the solve
    # there costs the track a position rather than protecting it.  Checked
    # before the distance shadow so the evidence outranks the radius.
    if link_key is not None:
        return link_key, "tracks", link_dist, (None if link_dt is None else round(link_dt, 3))
    # One shared id is not enough to take a followed key, but it is enough to
    # say this solve is a duplicate of one the follow lane already made, so it
    # is refused at any distance inside the gate rather than minting the second
    # key for that aircraft that the 2 km radius alone would have allowed.
    if weak_link_key is not None:
        return weak_link_key, "shadowed", weak_link_dist, (None if weak_link_dt is None else round(weak_link_dt, 3))
    if shadow_key is not None and shadow_dist <= dark_follow.DARK_FOLLOW_SHADOW_KM:
        return shadow_key, "shadowed", shadow_dist, (None if shadow_dt is None else round(shadow_dt, 3))
    if best_key is not None:
        return (
            best_key,
            "tracks" if best_shared >= 1 else "proximity",
            best_dist,
            (None if best_dt is None else round(best_dt, 3)),
        )
    # No claimant — a genuinely new target.
    return f"mn-dark-{result.get('timestamp_ms', 0)}-{lat:.3f}-{lon:.3f}", "minted", None, None


def _forget_mn_key(old_key: str) -> None:
    """Erase every trace of ``old_key`` from the multinode stores.

    The four stores are the entry itself, its anomaly hex (the feed reads that
    set independently of multinode_tracks, so an entry popped without it keeps
    flagging a hex nothing renders), the smoother's position history, and the
    Kalman state.  Every removal path has to erase all four or the next key
    minted at the same place inherits the dead one's filter.  Caller holds
    state.multinode_tracks_lock; _MN_POS_HISTORY_LOCK is taken inside, the
    order documented at that lock's declaration in core.state.
    """
    state.multinode_tracks.pop(old_key, None)
    with state.anomaly_lock:
        state.anomaly_hexes.discard(multinode_hex_from_key(old_key))
    with _MN_POS_HISTORY_LOCK:
        _MN_POS_HISTORY.pop(old_key, None)
    track_filter.drop_key(old_key)


def _supersession_match(
    old_key: str,
    old_r: dict,
    new_ids: set,
    raw_lat: float,
    raw_lon: float,
    ts_ms: float,
    learned_vel_fn=track_filter.learned_velocity,
    max_age_s: float = _MN_ASSOC_MAX_AGE_S,
    alt_m: float | None = None,
) -> tuple[bool, float | None]:
    """Is the existing entry ``old_key`` the same aircraft as this new solve?

    The second half of the supersession rule in solver.py's
    _process_solver_item.  The caller has already established the two cheap
    conditions — a different key, and at least one shared source track id — and
    this decides whether the shared id means anything.  Clock-free with an
    injectable ``learned_vel_fn``, for the same reasons multinode_key_decision
    is: unit-testable without KF state or the whole publish path.

    Sharing a source track id is NOT on its own evidence of same-aircraft.
    Single-node tracker tracks are genuinely shared between the association
    candidates of DIFFERENT aircraft (a 6 min live window: 74 of 178 track ids
    appeared in published solves of more than one ground-truth aircraft), so
    the bare shared-id rule this replaces popped another aircraft's key 36
    times in 44 supersessions, 41 of them beyond the association gate and 43
    of them under 15 s old.  The victim's next solve then found no key and
    minted a fresh one — dark keys churning at 7.4/min with a 7 s median
    lifetime, against 2.4/min and 33 s before the dark publish rate rose.
    Replayed over those 139 live solves, this guard cuts mints 47 -> 22 and
    cross-aircraft pops 36 -> 7.

    Nor is proximity on its own: on three 20 min ground-truth captures
    (2026-09-05, 129 supersessions) 63 popped a different ground-truth
    aircraft, almost always a 3-4 node solve of B with 4.5-10 km of position
    error landing inside neighbour A's 6 km gate and deleting A's established
    key.  So this predicate is gated tighter than the keying rule on BOTH
    axes — _MN_SUPERSEDE_BASE_KM (4 km, still age-scaled) and
    _MN_SUPERSEDE_MAX_ALT_DIFF_M (1000 m) carry those measurements — because a
    wrong pop costs a live aircraft's key while a refusal costs one deferred
    merge.  Replayed over those 129: cross-aircraft pops 56 -> 7, pops by a
    bad new solve 12 -> 1, at the price of 64 -> 33 same-aircraft merges.

    Two ways to match, either sufficient — and BOTH subject to the altitude
    gate, which is fail-open when either side has no altitude:

      (a) SPATIAL.  Dead-reckon ``old_r`` to this solve's timestamp and ask
          whether it lands within the age-scaled gate (_mn_assoc_gate_km)
          grown from _MN_SUPERSEDE_BASE_KM.  Same DR as
          multinode_key_decision — _entry_dr_velocity + offset_latlon_m, so
          the entry is judged where the feed DRAWS it — and measured against
          the solve's RAW position, which is what that gate was tuned
          against.  The dt window is the keying rule's, signed the same way:
          an entry older than ``max_age_s`` is past the map's own expiry,
          and an entry stamped up to _MN_ASSOC_MAX_NEG_DT_S AFTER this solve
          is dead-reckoned backwards over the signed dt to where that
          aircraft was at this solve's epoch.  Out-of-order epochs between
          the two dark lanes and across the pool's workers are routine at
          1-4 s (see _MN_ASSOC_MAX_NEG_DT_S for the live measurement), so
          refusing them here made a duplicate key un-mergeable as well as
          un-preventable: the next solve could not supersede the duplicate
          either whenever its own epoch was the earlier of the two.  The
          10 s bound is what keeps the case the old rule was really about (a
          stale queue item, not lane jitter) out of the backwards DR.

      (b) IDENTICAL INPUTS.  ``old_r``'s source track ids are non-empty and a
          subset of this solve's.  Built from the same measurements, so the
          same aircraft by construction — this is the anchor-merge case,
          where a bottom-up fragment minted from exactly these tracks is
          absorbed into the anchor.  A merely OVERLAPPING set is not enough:
          overlap is the contamination described above.

          Bounded, though, at twice the branch (a) gate and by the same
          altitude test.  Identical inputs converging many kilometres and
          thousands of metres apart is a multi-modal solve — the same
          measurements admitting two solutions — not one aircraft seen twice,
          and picking the wrong mode still costs a key.  Branch (b) fired 7
          times in those captures: 4 were cross-aircraft (dead-reckoned 6.2,
          6.3, 9.0 and 16.5 km apart, |daltitude| 10764, 1596, 1033 and 739 m)
          and only one of the 3 same-aircraft ones was a legitimate merge
          (1.8 km, 31 m).  The real anchor-merge case is always close, so the
          bound costs it nothing.  A dt window that never opened leaves
          ``dr_dist_km`` unknown, and an unknown distance cannot fail a
          distance bound — the ids still have to be a subset.

    Returns (matched, dr_dist_km).  dr_dist_km is the dead-reckoned distance
    in km when it could be computed (the dt window held and the entry has a
    position), None otherwise — reported for the same reason
    multinode_key_decision returns its dist_km: after the fact it is the only
    way to tell a tight merge from a rescued far one.
    """
    # The altitude half of the gate, applied to both branches below.  Missing
    # or non-finite on either side is "unknown", and unknown passes — an entry
    # that never carried an altitude must stay poppable on distance alone.
    old_alt, new_alt = old_r.get("alt_m"), alt_m
    alt_ok = (
        old_alt is None
        or new_alt is None
        or not math.isfinite(old_alt)
        or not math.isfinite(new_alt)
        or abs(new_alt - old_alt) <= _MN_SUPERSEDE_MAX_ALT_DIFF_M
    )

    dr_dist_km: float | None = None
    dt = ts_ms / 1000.0 - float(old_r.get("timestamp_ms") or 0) / 1000.0
    if -_MN_ASSOC_MAX_NEG_DT_S <= dt <= max_age_s:
        p_lat, p_lon = old_r.get("lat"), old_r.get("lon")
        if p_lat is not None and p_lon is not None:
            vel_east_ms, vel_north_ms = _entry_dr_velocity(old_key, old_r, learned_vel_fn)
            p_lat, p_lon = offset_latlon_m(
                p_lat,
                p_lon,
                east_m=vel_east_ms * dt,
                north_m=vel_north_ms * dt,
            )
            dr_dist_km = _haversine_km(raw_lat, raw_lon, p_lat, p_lon)
            if alt_ok and dr_dist_km <= _mn_assoc_gate_km(dt, _MN_SUPERSEDE_BASE_KM):
                return True, dr_dist_km

    old_ids = set(old_r.get("source_track_ids") or ())
    if (
        alt_ok
        and old_ids
        and old_ids.issubset(new_ids)
        and (dr_dist_km is None or dr_dist_km <= 2.0 * _mn_assoc_gate_km(dt, _MN_SUPERSEDE_BASE_KM))
    ):
        return True, dr_dist_km
    return False, dr_dist_km


# Solve-error allowance in the mint-time coast gate, kilometres, added to the
# ballistic travel term (MN_STALE_COAST_VMAX_MS * dt).  Two dark solves of the
# same aircraft sit ~1 km from truth each (the 2026-09-05 dark-lane fit), so a
# 2 km allowance covers the pair without opening the gate to a neighbour that
# the travel term has not already reached.
_MN_STALE_COAST_BASE_KM = 2.0

# Which counter a mint with no retirement lands on, by why it found none.
_MN_STALE_COAST_COUNTERS = {
    "alt": "mn_stale_coast_blocked_alt",
    "evidence": "mn_stale_coast_blocked_evidence",
    "none": "mn_stale_coast_none",
}


def _stale_coast_candidate(
    tracks: dict,
    new_key: str,
    raw_lat: float,
    raw_lon: float,
    ts_ms: float,
    alt_m: float | None = None,
    dropped_fn=None,
    manoeuvre_fn=None,
) -> tuple[str | None, str]:
    """Which existing dark key is this freshly MINTED solve's predecessor?

    The hard-turn re-key, closed at the one moment it is visible.  When a dark
    aircraft turns, the KF's manoeuvre boost inflates its velocity sigma past
    DARK_FOLLOW_MAX_VEL_SIGMA_MS, dark_follow drops the key, and the next
    bottom-up solve of the same aircraft mints a second one.  Supersession
    cannot see it: the shared-source-track-id prefilter is empty (node tracks
    renumber through a turn) and _supersession_match's spatial branch measures
    against the old entry's DEAD-RECKONED position, which is exactly what the
    turn has invalidated — the old key is coasting off on the frozen pre-turn
    velocity.  Measured over four 20-minute ground-truth captures (101 hard
    dark turns, 25 with the aircraft already on the map): 13 re-keyed, and 9
    of those 13 left the old key drawn for a median 52 s of icon-visible
    ghost, 11.3 km median from the aircraft whose identity it carried.

    So this asks the same "is it the same aircraft" question on evidence the
    turn does not destroy:

    * **Raw against raw.**  Distance is between this solve's raw position and
      the candidate's own last SOLVED position — never the dead-reckoned one.
      Both are measurements; the dead reckoning between them is the error.
    * **A coasting age band.**  MN_STALE_COAST_MIN_S to MN_STALE_COAST_MAX_S.
      The floor is most of the selectivity: dark solves land every 1-3 s, so a
      key last solved 1 s ago is being tracked, not coasted, and a key 4 km
      from it is a neighbour.  The ceiling is past the dark entry expiry,
      where there is no longer a rendered ghost to retire.
    * **Altitude**, on _MN_SUPERSEDE_MAX_ALT_DIFF_M, failing open when either
      side is unknown — the same rule and the same reasoning as
      _supersession_match's.
    * **Turn evidence, required.**  Proximity alone is what popped 63 of 129
      neighbours in the 2026-09-05 capture, and this gate is looser in space
      than that one was, so it does not get to decide anything by itself.
      Either dark_follow says it dropped this key inside its cooldown window,
      or the KF says the key's manoeuvre engagement is still above
      MN_STALE_COAST_MANOEUVRE.  Both are statements about the candidate, made
      before this solve existed, and both are precisely the turn signature.

    Returns ``(old_key, reason)``.  ``old_key`` is the CLOSEST fully-qualified
    candidate — one retirement per mint, because a mint replaces one key, and
    the nearest is the one the aircraft actually flew out of.  ``reason`` says
    why there is none, for the counter: "evidence" if some candidate reached
    the evidence test and failed it, else "alt" if one was refused on altitude
    alone, else "none".  "evidence" outranks "alt" deliberately — a candidate
    at the right place and height with no turn behind it is the interesting
    refusal, and the one to watch if this ever needs loosening.

    Clock-free and accessor-injected for the same reason _supersession_match
    and multinode_key_decision are: testable without a live KF, a live follow
    lane, or the publish path.
    """
    # Resolved here rather than as def-time defaults (the way
    # _supersession_match binds its learned_vel_fn) because the only caller
    # never passes them: a def-time default would freeze the accessor at
    # import and a test could only swap it through __defaults__.
    dropped_fn = dark_follow.was_dropped if dropped_fn is None else dropped_fn
    manoeuvre_fn = track_filter.manoeuvre_level if manoeuvre_fn is None else manoeuvre_fn
    best_key: str | None = None
    best_dist = float("inf")
    saw_alt_refusal = False
    saw_evidence_refusal = False
    for old_key, old_r in tracks.items():
        if old_key == new_key or not old_key.startswith("mn-dark-"):
            continue
        dt = ts_ms / 1000.0 - float(old_r.get("timestamp_ms") or 0) / 1000.0
        if not (MN_STALE_COAST_MIN_S <= dt <= MN_STALE_COAST_MAX_S):
            continue
        old_lat, old_lon = old_r.get("lat"), old_r.get("lon")
        if old_lat is None or old_lon is None:
            continue
        dist_km = _haversine_km(raw_lat, raw_lon, old_lat, old_lon)
        if dist_km > min(
            MN_STALE_COAST_MAX_KM,
            MN_STALE_COAST_VMAX_MS * dt / 1000.0 + _MN_STALE_COAST_BASE_KM,
        ):
            continue
        old_alt = old_r.get("alt_m")
        if (
            old_alt is not None
            and alt_m is not None
            and math.isfinite(old_alt)
            and math.isfinite(alt_m)
            and abs(alt_m - old_alt) > _MN_SUPERSEDE_MAX_ALT_DIFF_M
        ):
            saw_alt_refusal = True
            continue
        manoeuvre = manoeuvre_fn(old_key)
        if not (dropped_fn(old_key) or (manoeuvre is not None and manoeuvre > MN_STALE_COAST_MANOEUVRE)):
            saw_evidence_refusal = True
            continue
        if dist_km < best_dist:
            best_key, best_dist = old_key, dist_km
    if best_key is not None:
        return best_key, ""
    if saw_evidence_refusal:
        return None, "evidence"
    return None, "alt" if saw_alt_refusal else "none"
