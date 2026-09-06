"""The combined aircraft.json builder — sections, caches, assembly.

Extracted from frame_processor.py.  This module owns the 1 Hz feed: merging
per-node pipelines, the default pipeline, multinode solves and ADS-B into one
tar1090-compatible payload.  The per-track machinery lives in
services.track_gates; shared helpers in services.feed_helpers; stale-store GC
in services.feed_gc.
"""

import logging
import math
import os
import time

from retina_tracker.track import TrackState

from config.constants import (
    ARC_REFRESH_S,
    CLAIMED_DISPLAY_FRESH_S,
    GT_REFRESH_S,
    MN_DARK_EXPIRY_S,
    MN_DR_CAP_S,
    MN_N2_MIN_SOLVES,
    MN_ONESHOT_TTL_S,
    STALE_TRACK_S,
)
from core import state
from pipeline.passive_radar import PassiveRadarPipeline
from services import track_filter
from services.feed_gc import prune_stale_stores
from services.feed_helpers import (
    append_track_history,
    dedup_aircraft,
    resolve_ground_truth_hex,
)
from services.geo import offset_latlon_m
from services.id_utils import (
    multinode_hex_from_key,
    normalize_hex_key,
    passive_track_hex,
)
from services.public_location import fuzz_node_cfg
from services.solve_uncertainty import solve_sigma_m, velocity_sigma_ms
from services.track_gates import (
    _build_single_node_arc,
    _public_arc_cache,
    _single_node_arc_cache,
    track_entry,
)


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only.

    The wall-clock feed caches are the sharp edge: two tests calling
    build_combined_aircraft_json within ARC_REFRESH_S / GT_REFRESH_S of each
    other used to receive the PREVIOUS test's detection_arcs and
    ground_truth verbatim.
    """
    global _cached_pending_arcs, _cached_detecting_nodes, _arcs_last_ts
    global _cached_gt_snapshot, _cached_gt_meta, _gt_last_ts
    global _mn_entry_fail_logged_at, _mn_entry_fail_count
    _cached_pending_arcs = []
    _cached_detecting_nodes = {}
    _arcs_last_ts = 0.0
    _cached_gt_snapshot = {}
    _cached_gt_meta = {}
    _gt_last_ts = 0.0
    # Same reason as the wall-clock caches above: the log throttle is
    # monotonic-time state, so one test's skipped entry would otherwise
    # silence the next test's.
    _mn_entry_fail_logged_at = 0.0
    _mn_entry_fail_count = 0


_MN_ADSB_PREFIX = "mn-adsb-"


def multinode_to_aircraft(key: str, r: dict) -> dict:
    _solve_speed_ms = math.sqrt(r["vel_east"] ** 2 + r["vel_north"] ** 2)
    _solve_heading_deg = math.degrees(math.atan2(r["vel_east"], r["vel_north"])) % 360
    # No velocity-plausibility flag on the solve speed itself: supersonic
    # targets are in scope, so a high solved speed is reported as-is in
    # max_velocity_ms.  Anomaly flags come from the contributing tracker
    # tracks, stamped onto the result by the solver (_collect_track_anomalies)
    # — a dark anomalous target must not go quiet the moment it graduates
    # from arc to solve.
    _mn_hex = multinode_hex_from_key(key)
    _is_anom = bool(r.get("is_anomalous"))
    _anom_types = r.get("anomaly_types") or []
    with state.anomaly_lock:
        if _is_anom:
            state.anomaly_hexes.add(_mn_hex)
        else:
            # The discard also clears any hex left by an earlier release.
            # (No GT-meta guard needed: mn* hexes are solver-minted and never
            # appear in ground_truth_meta.)
            state.anomaly_hexes.discard(_mn_hex)
    # TRACK_GS_SOURCE, read per call like TRACK_DR_SOURCE: "kf" (default)
    # displays the KF display filter's LEARNED velocity for gs/track instead
    # of the raw solve vector — the same learned vector the TRACK_DR_SOURCE
    # block below (build_combined_aircraft_json) already dead-reckons with,
    # so the icon's motion and its speed/heading readout agree.  "solve"
    # restores the raw vel_east/vel_north arithmetic (rollback, env only).
    # The KF accessor returns None whenever it never saw this key (smoother
    # in ewma/off mode, first solve, TTL-swept), which is also the natural
    # fallback to solve display, not a separate mode.  max_velocity_ms stays
    # on the solve speed regardless: it is the max-observed-solve-speed latch
    # for anomaly display, not a current-motion field.
    speed_ms = _solve_speed_ms
    heading = _solve_heading_deg
    _gs_source_kf = False
    if (os.getenv("TRACK_GS_SOURCE", "kf") or "kf").strip().lower() != "solve":
        _lv = track_filter.learned_velocity(key)
        if _lv is not None:
            speed_ms = math.sqrt(_lv[0] ** 2 + _lv[1] ** 2)
            heading = math.degrees(math.atan2(_lv[0], _lv[1])) % 360
            _gs_source_kf = True
    _vel_untrusted = bool(r.get("vel_untrusted"))
    # VEL_TRUST_MODE, read per call like TRACK_DR_SOURCE: "off" (default)
    # changes nothing — the flag rides along but gs/track stay populated.
    # "active" additionally drops gs/track, but only when BOTH vel_untrusted
    # is set AND the displayed gs/track is solve-sourced (no KF vector was
    # used): vel_untrusted describes the reliability of the solve/fit
    # vector, and a KF-sourced gs/track has already replaced that vector
    # rather than needing to be hidden alongside it.
    _vel_trust_mode = (os.getenv("VEL_TRUST_MODE") or "off").strip().lower()
    entry = {
        "hex": _mn_hex,
        "type": "multinode_solve",
        "flight": f"MN{r['n_nodes']}N",
        "alt_baro": round(r["alt_m"] / 0.3048),
        "alt_geom": round(r["alt_m"] / 0.3048),
        "gs": round(speed_ms * 1.94384, 1),
        "track": round(heading, 1),
        "lat": round(r["lat"], 5),
        "lon": round(r["lon"], 5),
        # Real age of the solve, not 0 — see the matching note on the tracker
        # path.  Guarded because timestamp_ms is absent in some fixtures.
        "seen": round(max(0.0, time.time() - r["timestamp_ms"] / 1000.0), 1) if r.get("timestamp_ms") else 0,
        "messages": r["n_measurements"],
        "rssi": -round(1.0 / max(r.get("rms_delay", 1), 0.01), 1),
        "multinode": True,
        "n_nodes": r["n_nodes"],
        "contributing_node_ids": r.get("contributing_node_ids", []),
        "rms_delay": round(r["rms_delay"], 3),
        "rms_doppler": round(r["rms_doppler"], 2),
        "position_source": "multinode_solve",
        "is_anomalous": _is_anom,
        "anomaly_types": sorted(_anom_types),
        "max_velocity_ms": round(max(_solve_speed_ms, r.get("max_velocity_ms", 0.0) or 0.0), 1),
    }
    # Lane, straight off the key prefix (multinode_key_decision): an ADS-B-tagged
    # solve — the regular pipeline's and every known-lane publish — keys
    # mn-adsb-<icao hex>, a dark solve mn-dark-*.  The emitted hex is the sha of
    # the key, so without this the frontend cannot tell the two lanes apart, and
    # cannot pair an mn entry with the adsb_single_node entry for the same
    # transponder.  The key is authoritative: r may be smoother output, which is
    # not guaranteed to carry adsb_hex.
    _assisted = key.startswith(_MN_ADSB_PREFIX)
    entry["adsb_assisted"] = _assisted
    if _assisted:
        entry["adsb_hex"] = key[len(_MN_ADSB_PREFIX) :]
    # Calibrated position uncertainty for the map's disc and the detail
    # panel — see services/solve_uncertainty.py for the model and the
    # 2026-09-05 fit.  Both fields are optional on the wire: sigma is None
    # (and the pair omitted) only when the solve carries no n_nodes, which
    # leaves no floor to apply.  Lane matters: the dark lane had no ADS-B fix
    # seeding the guess and no pinned altitude, so it is inflated — and
    # _assisted is the same key-prefix truth the lane fields above use, not
    # anything read off r.  Shipped AT THE SOLVE EPOCH, ungrown: `seen`
    # already carries the solve age and the frontend grows the disc itself
    # with pos_sigma_vel_ms at its own display tick, which is finer-grained
    # than this 1 Hz flush can be.
    _pos_sigma_m = solve_sigma_m(r, dark=not _assisted)
    if _pos_sigma_m is not None:
        entry["pos_sigma_m"] = round(_pos_sigma_m, 1)
        entry["pos_sigma_vel_ms"] = round(velocity_sigma_ms(key), 1)
    if _gs_source_kf:
        entry["gs_source"] = "kf"
    if _vel_untrusted:
        entry["vel_untrusted"] = True
        if _vel_trust_mode == "active" and not _gs_source_kf:
            del entry["gs"]
            del entry["track"]
    return entry


# One multinode entry failing is a bug worth a log line, but the feed builds
# at 1 Hz over ~40 live keys, so an unguarded logger would turn one sick
# aircraft into thousands of identical lines an hour and bury everything else.
# Same shape of throttle detection_mirror.py uses for its dropped-frame line.
_MN_ENTRY_FAIL_LOG_INTERVAL_S = 60.0
_mn_entry_fail_logged_at = 0.0
_mn_entry_fail_count = 0


def _note_multinode_entry_failure(key: str) -> None:
    """Count a skipped multinode entry, logging at most once a minute."""
    global _mn_entry_fail_logged_at, _mn_entry_fail_count
    _mn_entry_fail_count += 1
    now = time.monotonic()
    if now - _mn_entry_fail_logged_at < _MN_ENTRY_FAIL_LOG_INTERVAL_S:
        return
    _mn_entry_fail_logged_at = now
    logging.exception(
        "Multinode feed entry failed for key=%s, skipping it (%d total since boot)",
        key,
        _mn_entry_fail_count,
    )


def _multinode_entry(key: str, r: dict, now: float) -> dict:
    """One multinode solve as a feed entry, dead-reckoned to ``now``.

    Split out of build_combined_aircraft_json so the whole per-entry
    computation — multinode_to_aircraft, the learned-velocity lookup, the
    dead-reckon — sits behind one try/except there.  Inline, an exception
    from any of them took the entire flush with it.
    """
    ac = multinode_to_aircraft(key, r)
    # Dead-reckon position using solver velocity (vel_east/vel_north in
    # m/s), capped at MN_DR_CAP_S: beyond that a velocity error dominates
    # any solve accuracy, so an old solve holds its last dead-reckoned
    # point until the entry expiry rather than drifting further.
    ts_fix = r.get("timestamp_ms", 0) / 1000.0
    elapsed = min(now - ts_fix, MN_DR_CAP_S)
    vel_east_m_s = r.get("vel_east", 0.0)
    vel_north_m_s = r.get("vel_north", 0.0)
    # TRACK_DR_SOURCE, read per call like TRACK_SMOOTHER: "kf" (default)
    # dead-reckons with the display filter's LEARNED velocity when one
    # exists — the solved velocity this block used to trust was measured
    # (2026-08-09, n=93) at median 127 m/s vector error, i.e. ~3.8 km of
    # drift at the 30 s cap below, worse than the solve error itself.
    # "solve" restores the old behaviour (rollback, env only).  The KF
    # accessor returns None whenever the KF never saw this key (smoother
    # in ewma/off mode, first solve, TTL-swept) so the fallback below is
    # also the natural off-path, not a separate mode.
    if (os.getenv("TRACK_DR_SOURCE", "kf") or "kf").strip().lower() != "solve":
        _lv = track_filter.learned_velocity(key)
        if _lv is not None:
            vel_east_m_s, vel_north_m_s = _lv[0], _lv[1]
    if elapsed > 0.0 and (vel_east_m_s != 0.0 or vel_north_m_s != 0.0):
        _dr_lat, _dr_lon = offset_latlon_m(
            ac["lat"],
            ac["lon"],
            east_m=vel_east_m_s * elapsed,
            north_m=vel_north_m_s * elapsed,
        )
        ac["lat"], ac["lon"] = round(_dr_lat, 5), round(_dr_lon, 5)
    return ac


def _claimed_single_node_entries(now: float) -> list[dict]:
    """Feed entries for hexes exactly ONE node is currently claiming.

    A claim (services/known_claiming.py) pairs a node's raw delay/Doppler
    measurement with the ADS-B fix of the transponder it was bound to, and in
    binding mode that detection leaves the dark pool — so nothing else in the
    feed ever renders it.  Two or more claiming nodes are the known-lane
    solver's case: it needs n>=2 and publishes its own ``mn-adsb-<hex>`` entry
    on convergence, so emitting here as well would double-draw the aircraft.
    One claiming node reaches nobody, which is what this section exists for.

    The position is the ADS-B fix itself, not an estimate — the radar
    contribution is the identity of the detecting node and the ambiguity arc,
    which is why ``position_source`` names the anchor rather than a solver and
    why health.py must not read these as radar solves.

    The arc is rebuilt rather than memoised through ``_single_node_arc_cache``:
    that cache is keyed (hex, node_id) on the *track's* latest delay, and the
    same hex can carry a tracker track on the same node, so sharing the key
    would make the two fingerprints evict each other every build.  A rebuild
    costs one binary search per bearing for the handful of qualifying hexes —
    less than section 5 already spends unconditionally, every build, on every
    promoted track in the fleet.
    """
    fresh_cutoff_ms = (now - CLAIMED_DISPLAY_FRESH_S) * 1000.0
    entries: list[dict] = []
    for hexn, dq in list(state.known_claims.items()):
        newest: dict | None = None
        node_ids: set[str] = set()
        for c in list(dq):
            if not isinstance(c, dict):
                continue
            try:
                ts_ms = float(c["ts_ms"])
                node_id = c["node_id"]
            except (KeyError, TypeError, ValueError):
                continue
            if not node_id or ts_ms < fresh_cutoff_ms:
                continue
            node_ids.add(node_id)
            if len(node_ids) > 1:
                break
            if newest is None or ts_ms > float(newest["ts_ms"]):
                newest = c
        if newest is None or len(node_ids) != 1:
            continue

        fix = newest.get("adsb_fix") or {}
        lat, lon = fix.get("lat"), fix.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        node_id = newest["node_id"]
        delay_us = float(newest.get("delay_us") or 0.0)

        # node_cfg is the geometry the arc is solved in; a node that has since
        # disconnected leaves the entry without one, which is the same
        # icon-only outcome as the builder declining.
        pipeline = state.node_pipelines.get(node_id)
        node_cfg = getattr(pipeline, "config", None)
        # Built around the PUBLISHED receiver: this arc goes straight onto the
        # wire, and an ellipse drawn from the true one names the operator's
        # house at its focus.  Same delay, same real transmitter.
        arc = _build_single_node_arc(delay_us, fuzz_node_cfg(node_cfg)) if node_cfg else None

        # The claim's own fix carries no callsign (it is copied from the frame's
        # ADS-B block, which reports position and kinematics only).
        _ae = state.adsb_aircraft.get(hexn)
        flight = ((_ae.get("flight") if _ae else "") or "").strip() or None

        fix_ts_ms = fix.get("fix_ts_ms") or 0
        entries.append(
            {
                "hex": hexn,
                "type": "adsb_icao",
                "flight": flight,
                "lat": lat,
                "lon": lon,
                "alt_baro": fix.get("alt_baro"),
                "gs": fix.get("gs"),
                "track": fix.get("track"),
                "seen": round(max(0.0, now - float(newest["ts_ms"]) / 1000.0), 1),
                "multinode": False,
                "position_source": "adsb_single_node",
                # Mandatory: the live/owner WS feeds drop any entry whose
                # node_id is not in the connection's node set.
                "node_id": node_id,
                "delay_us": round(delay_us, 3),
                "doppler_hz": round(float(newest.get("doppler_hz") or 0.0), 2),
                # The FULL locus.  The frontend trims it to a fixed screen
                # length around the icon; trimming here would bake one zoom
                # level into the wire format.
                "ambiguity_arc": arc,
                "adsb_fix_age_s": (round(max(0.0, now - fix_ts_ms / 1000.0), 1) if fix_ts_ms else None),
                "target_class": "aircraft",
            }
        )
    return entries


# How often to recompute detection arcs and GT snapshot (seconds).
# Iterating 915 pipelines × 5000 tracks every second is the #1 GIL hog;
# caching at 5 s cuts that penalty by 4/5.
_ARC_REFRESH_S = ARC_REFRESH_S
_GT_REFRESH_S = GT_REFRESH_S
_cached_pending_arcs: list[dict] = []
_cached_detecting_nodes: dict[str, list[str]] = {}
_arcs_last_ts: float = 0.0
_cached_gt_snapshot: dict = {}
_cached_gt_meta: dict = {}
_gt_last_ts: float = 0.0


def build_combined_aircraft_json(default_pipeline: PassiveRadarPipeline) -> dict:
    """Merge per-node pipelines, default pipeline, multinode, ADS-B into one feed."""
    now = time.time()
    seen_hex: set[str] = set()
    _touched_arc_keys: set[tuple[str, str | None]] = set()
    aircraft: list[dict] = []

    # Staleness threshold: skip geolocated tracks not updated in the last
    # 120 s.  Without this, geolocated_tracks accumulates dead entries
    # between prune cycles and causes O(N_stale) work per flush — arc
    # computation, ground-truth resolution, history writes — all holding
    # the GIL and starving frame workers.
    _STALE_TRACK_S_LOCAL = STALE_TRACK_S

    # 1. Pre-aggregated geolocated aircraft (O(active) ≈ 275 instead of
    #    O(pipelines × tracks) ≈ 915 × 2.4).  Stale entries are pruned here.
    stale_geo = []
    with state.geo_aircraft_lock:
        for ac_hex, (track, cfg) in list(state.active_geo_aircraft.items()):
            if (now - getattr(track, "wall_clock_ts", 0)) > _STALE_TRACK_S_LOCAL:
                stale_geo.append(ac_hex)
                continue
            if ac_hex in seen_hex:
                continue
            seen_hex.add(ac_hex)
            entry = track_entry(ac_hex, track, cfg, now, _touched_arc_keys)
            if entry is not None:
                aircraft.append(entry)
        for k in stale_geo:
            state.active_geo_aircraft.pop(k, None)
            state.track_last_emit.pop(k, None)
            state.track_gate_hold.pop(k, None)
            state.track_arc_motion.pop(k, None)
        with state.anomaly_lock:
            for k in stale_geo:
                state.anomaly_hexes.discard(k)

    # 2. Default pipeline — catch any tracks not in the pre-aggregated dict
    for track in list(default_pipeline.geolocated_tracks.values()):
        if (now - getattr(track, "wall_clock_ts", 0)) > _STALE_TRACK_S_LOCAL:
            continue
        ac_hex = track.adsb_hex or track.hex_id
        if ac_hex in seen_hex:
            continue
        seen_hex.add(ac_hex)
        entry = track_entry(ac_hex, track, default_pipeline.config, now, _touched_arc_keys)
        if entry is not None:
            aircraft.append(entry)

    # 3. Multi-node solver
    stale_mn = []
    for key, r in list(state.multinode_tracks.items()):
        age_s = now - r.get("timestamp_ms", 0) / 1000
        # Lane-aware expiry, on the same key-prefix truth multinode_to_aircraft
        # reads adsb_assisted off.  An assisted entry is anchored to a
        # transponder hex, so a long gap is the ADS-B feed breathing and the
        # historic 60 s still fits it.  A dark entry has nothing holding it in
        # place: at the current 1–3 s dark solve cadence a 30 s gap is a lost
        # track, and the 30–60 s band measured 3.99 km median error (32% over
        # 5 km) — a confident icon kilometres from any aircraft.
        _expiry_s = 60.0 if key.startswith(_MN_ADSB_PREFIX) else MN_DARK_EXPIRY_S
        if age_s > _expiry_s:
            stale_mn.append(key)
            continue
        # Display gates below are NOT staleness — a gated entry stays in
        # state.multinode_tracks so the next solve can confirm it (n=2) or
        # supersede it, and only the expiry branch above discards its
        # anomaly hex.  A one-shot solve renders nothing at all: a 2-node
        # track needs a second solve to prove it isn't a mirror-point ghost,
        # and a 3+-node one-shot gets a short preview window instead of the
        # full entry lifetime before it either confirms or expires.
        solve_count = int(r.get("solve_count") or 1)
        if r.get("n_nodes") == 2 and solve_count < MN_N2_MIN_SOLVES:
            continue
        if r.get("n_nodes", 0) >= 3 and solve_count == 1 and age_s > MN_ONESHOT_TTL_S:
            continue
        # One sick solve must not cost the whole broadcast.  Everything from
        # here to the append reads a single multinode entry, and an exception
        # anywhere in it used to propagate out of the flush task ("Aircraft
        # flush failed") and drop the ENTIRE tick's feed — every other
        # aircraft with it — for one bad key.  A skipped entry ages out of
        # state.multinode_tracks on its own within 60 s, so degrading to
        # "this one aircraft is missing for a few ticks" is strictly better
        # than an empty map.
        try:
            ac = _multinode_entry(key, r, now)
        except Exception:
            _note_multinode_entry_failure(key)
            continue
        if ac["hex"] not in seen_hex:
            seen_hex.add(ac["hex"])
            append_track_history(ac["hex"], ac["lat"], ac["lon"], ac["alt_baro"], now)
            # A multinode position is an aircraft estimate standing on its own
            # geometry, so this frame's append lands identically in both stores
            # — but the TRAIL behind it need not.  The same hex can have been
            # an arc-only track under one node minutes ago, and those points
            # are boresight crossings in the true frame: a ray from the
            # operator's receiver.  Serving the public store hands over the
            # trail as each point was published, rather than re-exposing the
            # frames this aircraft happened to spend on one node's arc.
            ac["recent_positions"] = list(state.track_histories_public.get(ac["hex"], []))
            ac["ground_truth_hex"] = resolve_ground_truth_hex(ac["hex"], ac["lat"], ac["lon"])
            aircraft.append(ac)
    for k in stale_mn:
        # Must use the same derivation as insertion (multinode_to_aircraft →
        # multinode_hex_from_key). This previously used an obsolete 4-char
        # format that never matched the mn<sha256[:10]> actually inserted, so
        # no multinode anomaly hex was ever evicted and anomaly_hexes grew
        # without bound — enough to trip the anomaly_flood health check.
        with state.anomaly_lock:
            state.anomaly_hexes.discard(multinode_hex_from_key(k))
        state.multinode_tracks.pop(k, None)

    # 3b. Singly-claimed ADS-B targets — no seen_hex guard on purpose.  A
    # partially-claimed aircraft can still carry a tracker track keyed by the
    # same hex, and the ADS-B fix is the better of the two positions, so the
    # collision is left for dedup to settle by source rank rather than decided
    # here by append order.
    aircraft.extend(_claimed_single_node_entries(now))

    # 4/4b. Stale-store GC — services.feed_gc.
    prune_stale_stores(now)

    # 5. Pending detection arcs from tracker tracks not yet geolocated.
    # These arcs appear immediately on each detection without waiting for
    # M-of-N promotion + LM solver convergence.
    # Recompute only every _ARC_REFRESH_S seconds — iterating 915 pipelines
    # × ~5000 tracks per second is a GIL-heavy operation that starves frame
    # workers.  Cached arcs are good enough for the 1-Hz map refresh.
    global _cached_pending_arcs, _cached_detecting_nodes, _arcs_last_ts
    if now - _arcs_last_ts >= _ARC_REFRESH_S:
        _arcs_last_ts = now
        pending_arcs = []
        # hex → nodes currently holding a promoted track for it.  The feed's
        # aircraft entries are one-per-hex (last writer wins), so this is the
        # only place the full per-node fan-out is visible — the debug map's
        # "detected by" list reads it.
        detecting: dict[str, set[str]] = {}
        for pipeline in list(state.node_pipelines.values()):
            node_cfg = pipeline.config
            for track in list(pipeline.tracker.tracks):
                # TENTATIVE tracks haven't been promoted via M-of-N yet — they
                # may still be flagged for deletion.  Skip them so spurious
                # short-lived tracks don't produce arcs.  ACTIVE and COASTING
                # tracks both produce arcs: at 22 fps a single missed frame
                # flips ACTIVE → COASTING and the next frame flips it back, so
                # filtering COASTING here would cause the arc to flicker at
                # ~11 Hz for any aircraft with intermittent detection.  The
                # natural lifecycle (track deleted after N_DELETE missed frames)
                # is what stops the arc when the aircraft truly leaves the beam.
                if track.state_status == TrackState.TENTATIVE:
                    continue
                _ae = None
                if track.adsb_hex:
                    _nid = node_cfg.get("node_id")
                    if _nid:
                        detecting.setdefault(normalize_hex_key(track.adsb_hex), set()).add(_nid)
                    _ae = state.adsb_aircraft.get(normalize_hex_key(track.adsb_hex))
                    # ADS-B tracks used to `continue` here (no pending arc) —
                    # the single-node measurement is real regardless of the
                    # transponder, and skipping it left ADS-B-correlated
                    # aircraft with no arc at all once dedup collapsed their
                    # per-node entries.  Every promoted track now emits its
                    # measured-delay arc.
                meas = track.history.get("measurements")
                if not meas:
                    continue
                latest = next((m for m in reversed(meas) if m is not None), None)
                if latest is None:
                    continue
                delay_us = latest.get("delay", 0)
                if delay_us <= 0:
                    continue
                # detection_arcs ships verbatim to every websocket client, so
                # this is a published arc: fuzzed receiver focus, true TX.
                arc = _build_single_node_arc(delay_us, fuzz_node_cfg(node_cfg))
                if not arc or len(arc) < 2:
                    continue
                pending_arcs.append(
                    {
                        "ambiguity_arc": arc,
                        "node_id": node_cfg.get("node_id"),
                        # hex + delay_us make the entry ingestible by the
                        # frontend arc buffer, which keys by (hex, node,
                        # quantized delay).  delay_us rounding must match the
                        # per-aircraft track_entry emission so the same
                        # measurement arriving via both channels collides onto
                        # one buffer key instead of double-drawing.
                        "hex": normalize_hex_key(track.adsb_hex) if track.adsb_hex else passive_track_hex(track.id),
                        "delay_us": round(delay_us, 3),
                        "doppler_hz": round(latest.get("doppler", 0), 2),
                        "alt_baro": _ae.get("alt_baro") if _ae else None,
                        "target_class": getattr(track, "target_class", None),
                    }
                )
        _cached_pending_arcs = pending_arcs
        _cached_detecting_nodes = {h: sorted(nids) for h, nids in detecting.items()}
    else:
        pending_arcs = _cached_pending_arcs

    # Ground-truth snapshot — recompute every _GT_REFRESH_S seconds.
    global _cached_gt_snapshot, _cached_gt_meta, _gt_last_ts
    if now - _gt_last_ts >= _GT_REFRESH_S:
        _gt_last_ts = now
        _cached_gt_snapshot = {h: list(trail)[-30:] for h, trail in list(state.ground_truth_trails.items()) if trail}
        _cached_gt_meta = dict(state.ground_truth_meta)

    # Evict arc-cache entries for tracks not present this build, bounding the
    # cache to the live fleet with no timer: a plane is either in this snapshot
    # or it is not.  An empty touched set (no arc tracks this build) prunes all.
    for _stale_key in [k for k in _single_node_arc_cache if k not in _touched_arc_keys]:
        del _single_node_arc_cache[_stale_key]
    # The published-arc memo is keyed identically and filled from the same
    # touched set, so it is evicted on the same rule — otherwise it would be
    # the one unbounded cache on this path.
    for _stale_key in [k for k in _public_arc_cache if k not in _touched_arc_keys]:
        del _public_arc_cache[_stale_key]

    # Collapse multiple entries describing one aircraft. Runs last, after all
    # three sections have appended, so a multinode solve can displace a
    # single-node arc that was appended before it.
    aircraft = dedup_aircraft(aircraft)

    return {
        "now": now,
        "messages": len(aircraft),
        "aircraft": aircraft,
        "detection_arcs": pending_arcs,
        "detecting_nodes": _cached_detecting_nodes,
        "ground_truth": _cached_gt_snapshot,
        "ground_truth_meta": _cached_gt_meta,
        "anomaly_hexes": sorted(state.anomaly_hexes),
    }
