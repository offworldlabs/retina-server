"""Test network dashboard, ground-truth validation endpoints."""

import logging
import math
import os
import time
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import Response

from config.constants import ANALYTICS_REFRESH_INTERVAL_S, FT_TO_M, is_num
from core import state
from core.task_registry import get_stale_tasks
from core.users import require_admin
from services import known_claiming
from services.frame_processor import resolve_ground_truth_hex
from services.geo import haversine_km
from services.id_utils import normalize_hex_key
from services.node_refs import id_for_identity
from services.public_geometry import without_receiver_geometry
from services.public_location import fuzz_enabled, public_latlon, translate_polygon
from services.solver_report import (
    _LANES,
    _cap_per_lane,
    _merged_solve_history,
    _published_records,
    _record_lane,
    _solver_window_stats,
    _window_effective_minutes,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")
_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()

# In production the simulation-injection endpoints would otherwise fail-open
# (any caller can push fake aircraft / ground truth) if RADAR_API_KEY is left
# unset. Refuse to start rather than silently expose them.
if not _RADAR_API_KEY and _RETINA_ENV == "production":
    raise RuntimeError(
        "RADAR_API_KEY environment variable is required when "
        "RETINA_ENV=production. /api/test/ground-truth/push and "
        "/api/sim/adsb/push would otherwise accept unauthenticated callers."
    )
if not _RADAR_API_KEY:
    logger.warning(
        "RADAR_API_KEY is not set (RETINA_ENV=%r) — /api/test/ground-truth/push "
        "and /api/sim/adsb/push accept ANY caller without authentication.",
        _RETINA_ENV or "unset",
    )


def _verify_sim_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    """Require X-API-Key for simulation data injection endpoints.

    Production fails fast at import time when no key is configured, so by the
    time we reach this function in prod a key always exists and we always
    enforce it. Outside production the check is opt-in for dev convenience.
    """
    if _RADAR_API_KEY and x_api_key != _RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# Module-level reference set from main.py at startup
_default_pipeline = None


def init(pipeline):
    global _default_pipeline
    _default_pipeline = pipeline


@router.get("/api/test/dashboard")
async def test_network_dashboard():
    body = _build_dashboard_data()
    return Response(content=body, media_type="application/json")


def _build_dashboard_data() -> bytes:
    from services.real_capture import status as capture_status

    # Snapshot mutable dicts to avoid RuntimeError from concurrent mutation
    with state.connected_nodes_lock:
        _cn_snapshot = list(state.connected_nodes.values())
    _pipelines_snapshot = list(state.node_pipelines.values())

    total_nodes = len(_cn_snapshot)
    active_nodes = sum(1 for n in _cn_snapshot if n.get("status") not in ("disconnected",))
    synthetic_nodes = sum(1 for n in _cn_snapshot if n.get("is_synthetic"))
    # A disconnect sets the status and leaves the entry (services/tcp_handler.py),
    # so `synthetic` counts nodes that have gone away. Deploy verification needs
    # the ones answering now, which neither `synthetic` nor `active` gives: the
    # first keeps the dead, the second counts every non-fleet registration.
    synthetic_active = sum(
        1 for n in _cn_snapshot if n.get("is_synthetic") and n.get("status") not in ("disconnected",)
    )

    total_tracks = sum(len(p.tracker.tracks) for p in _pipelines_snapshot) if _pipelines_snapshot else 0
    total_tracks += (
        len(_default_pipeline.tracker.tracks) if _default_pipeline and hasattr(_default_pipeline, "tracker") else 0
    )
    geolocated = sum(len(p.geolocated_tracks) for p in _pipelines_snapshot) if _pipelines_snapshot else 0
    geolocated += (
        len(_default_pipeline.geolocated_tracks)
        if _default_pipeline and hasattr(_default_pipeline, "geolocated_tracks")
        else 0
    )
    mn_tracks = len(state.multinode_tracks)
    adsb_tracks = len(state.adsb_aircraft)
    n_aircraft = len(state.latest_aircraft_json.get("aircraft", []))

    analytics_nodes = len(state.node_analytics.trust_scores)
    avg_trust = 0.0
    if state.node_analytics.trust_scores:
        scores = [ts.score for ts in list(state.node_analytics.trust_scores.values()) if hasattr(ts, "score")]
        avg_trust = sum(scores) / len(scores) if scores else 0

    blocked_nodes = sum(
        1 for r in list(state.node_analytics.reputations.values()) if hasattr(r, "reputation") and r.reputation < 0.1
    )
    n_overlaps = len(state.node_associator.overlap_zones) if hasattr(state.node_associator, "overlap_zones") else 0
    ws_clients = len(state.ws_clients)
    ext_adsb = len(state.external_adsb_cache)

    return orjson.dumps(
        {
            "status": "running",
            "environment": os.getenv("RETINA_ENV", "production"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes": {
                "total": total_nodes,
                "active": active_nodes,
                "synthetic": synthetic_nodes,
                "synthetic_active": synthetic_active,
                "real": total_nodes - synthetic_nodes,
            },
            "pipeline": {
                "active_tracks": total_tracks,
                "geolocated_tracks": geolocated,
                "multinode_tracks": mn_tracks,
                "adsb_aircraft": adsb_tracks,
                "node_pipelines": len(state.node_pipelines),
                "aircraft_on_map": n_aircraft,
            },
            "analytics": {
                "nodes_with_analytics": analytics_nodes,
                "average_trust_score": round(avg_trust, 4),
                "blocked_nodes": blocked_nodes,
            },
            "association": {"overlap_zones": n_overlaps},
            # Published so e2e/specs/nodes.spec.ts can size its node-cache wait
            # from the running server rather than from a copy of this constant,
            # which is what went stale before (86cb5b4tt).
            "cadence": {"analytics_refresh_interval_s": ANALYTICS_REFRESH_INTERVAL_S},
            "streaming": {
                "websocket_clients": ws_clients,
                "external_adsb_cached": ext_adsb,
                "service_adsb_cached": len(state.service_adsb_cache),
            },
            "server_health": {
                "frame_queue_depth": state.frame_queue.qsize(),
                "frame_queue_max": state.frame_queue.maxsize,
                "frames_dropped": state.frames_dropped,
                "frame_queue_utilization_pct": round(
                    state.frame_queue.qsize() / max(state.frame_queue.maxsize, 1) * 100, 1
                ),
                # Rises when a node's frames carry no usable capture timestamp,
                # so its ADS-B positions are aged against our clock rather than
                # its own.  A node-clock signal, not a feed one.
                "adsb_capture_ts_fallback": state.adsb_capture_ts_fallback,
                "real_data_capture": capture_status(),
            },
            "chain_of_custody": {
                "registered_keys": len(state.node_identities),
                "chain_entries_total": sum(len(e) for e in list(state.chain_entries.values())),
                "iq_commitments_total": sum(len(c) for c in list(state.iq_commitments.values())),
                "nodes_with_chains": len(state.chain_entries),
            },
            "subsystem_health": {
                "tcp_server": "ok",
                "radar_pipeline": "ok" if _default_pipeline and hasattr(_default_pipeline, "tracker") else "error",
                "node_analytics": "ok" if analytics_nodes > 0 or total_nodes == 0 else "waiting",
                "inter_node_association": "ok" if n_overlaps > 0 or active_nodes < 2 else "waiting",
                "data_archival": "ok",
                "websocket_broadcast": "ok",
                "aircraft_feed": "ok",
                "chain_of_custody": "ok" if len(state.node_identities) > 0 or total_nodes == 0 else "waiting",
            },
            "mlat_verification": _mlat_verification_summary(),
            "task_health": {
                "last_success": dict(state.task_last_success),
                "error_counts": state.task_error_snapshot(),
                "stale_tasks": get_stale_tasks(),
            },
        }
    )


@router.post("/api/test/validate")
async def validate_ground_truth(body: dict = Body(...), _key=Depends(_verify_sim_key)):
    # _verify_sim_key: this was the one POST here with no auth dependency at
    # all — an unauthenticated compute endpoint over the full aircraft list.
    truth_list = body.get("ground_truth", [])
    if not truth_list:
        raise HTTPException(status_code=400, detail="ground_truth list required")

    server_aircraft = state.latest_aircraft_json.get("aircraft", [])
    matches = []
    unmatched_truth = []
    matched_server_indices: set[int] = set()

    for gt in truth_list:
        gt_lat = gt.get("lat", 0)
        gt_lon = gt.get("lon", 0)
        gt_alt = gt.get("alt_km", 0) * 1000

        best_match = None
        best_dist = float("inf")
        for i, sa in enumerate(server_aircraft):
            if i in matched_server_indices:
                continue
            sa_lat, sa_lon = sa.get("lat", 0), sa.get("lon", 0)
            if sa_lat == 0 and sa_lon == 0:
                continue
            dist_km = haversine_km(gt_lat, gt_lon, sa_lat, sa_lon)
            if dist_km < best_dist and dist_km < 50:
                best_dist = dist_km
                best_match = (i, sa)

        if best_match:
            idx, sa = best_match
            matched_server_indices.add(idx)
            # "ground" means on the surface — field elevation, not 0 m MSL — so
            # it is no altitude truth.  Null here rather than a fabricated error;
            # the match still scores position.
            _sa_alt = sa.get("alt_baro")
            alt_err_m = abs(gt_alt - _sa_alt * FT_TO_M) if is_num(_sa_alt) else None
            matches.append(
                {
                    "truth_id": gt.get("id"),
                    "server_hex": sa.get("hex"),
                    "position_error_km": round(best_dist, 2),
                    "altitude_error_m": round(alt_err_m, 0) if alt_err_m is not None else None,
                    "position_source": sa.get("position_source", "unknown"),
                    "has_adsb": gt.get("has_adsb", False),
                    "is_anomalous": gt.get("is_anomalous", False),
                }
            )
        else:
            unmatched_truth.append(gt.get("id", "unknown"))

    false_tracks = len(server_aircraft) - len(matched_server_indices)

    if matches:
        pos_errors = [m["position_error_km"] for m in matches]
        avg_pos_err = sum(pos_errors) / len(pos_errors)
        max_pos_err = max(pos_errors)
        accuracy_pct = len(matches) / len(truth_list) * 100
        sorted_pos = sorted(pos_errors)
        p50_pos = sorted_pos[len(sorted_pos) // 2]
        p95_pos = sorted_pos[int(len(sorted_pos) * 0.95)]
    else:
        avg_pos_err = max_pos_err = 0
        p50_pos = p95_pos = 0
        accuracy_pct = 0

    # Denominator is the matches that HAD altitude truth, not all of them, so
    # these stand apart from the position ones.  Null rather than 0 when none
    # did: 0 m of altitude error reads as perfect accuracy.
    alt_errors = [m["altitude_error_m"] for m in matches if m["altitude_error_m"] is not None]
    if alt_errors:
        avg_alt_err = sum(alt_errors) / len(alt_errors)
        sorted_alt = sorted(alt_errors)
        p50_alt = sorted_alt[len(sorted_alt) // 2]
        p95_alt = sorted_alt[int(len(sorted_alt) * 0.95)]
    else:
        avg_alt_err = p50_alt = p95_alt = None

    # Per-position_source breakdown
    by_source: dict[str, list[float]] = {}
    for m in matches:
        src = m.get("position_source", "unknown")
        by_source.setdefault(src, []).append(m["position_error_km"])
    source_breakdown = {}
    for src, errs in by_source.items():
        errs.sort()
        sn = len(errs)
        source_breakdown[src] = {
            "count": sn,
            "mean_km": round(sum(errs) / sn, 2),
            "median_km": round(errs[sn // 2], 2),
            "p95_km": round(errs[int(sn * 0.95)], 2),
        }

    return {
        "validation": {
            "truth_aircraft": len(truth_list),
            "server_aircraft": len(server_aircraft),
            "matched": len(matches),
            "unmatched_truth": len(unmatched_truth),
            "false_tracks": false_tracks,
            "detection_rate_pct": round(accuracy_pct, 1),
        },
        "accuracy": {
            "avg_position_error_km": round(avg_pos_err, 2),
            "median_position_error_km": round(p50_pos, 2),
            "p95_position_error_km": round(p95_pos, 2),
            "max_position_error_km": round(max_pos_err, 2),
            "n_altitude_samples": len(alt_errors),
            "avg_altitude_error_m": round(avg_alt_err, 0) if avg_alt_err is not None else None,
            "median_altitude_error_m": round(p50_alt, 0) if p50_alt is not None else None,
            "p95_altitude_error_m": round(p95_alt, 0) if p95_alt is not None else None,
        },
        "by_source": source_breakdown,
        "matches": matches[:50],
        "unmatched_ids": unmatched_truth[:20],
    }


@router.get("/api/test/ground-truth/{hex_code}")
async def get_ground_truth_trail(hex_code: str):
    norm_hex = normalize_hex_key(hex_code)
    solved_trail = list(state.track_histories.get(hex_code, [])) or list(state.track_histories.get(norm_hex, []))
    matched_hex = norm_hex
    gt_trail = list(state.ground_truth_trails.get(matched_hex, []))
    if not gt_trail and solved_trail:
        last = solved_trail[-1]
        fallback_hex = resolve_ground_truth_hex(norm_hex, last[0], last[1])
        if fallback_hex:
            matched_hex = fallback_hex
            gt_trail = list(state.ground_truth_trails.get(fallback_hex, []))

    if not gt_trail and not solved_trail:
        raise HTTPException(status_code=404, detail=f"No trail data for {hex_code}")

    # No ground truth to compare against means the only thing left to serve is
    # the solved trail, and for a single-node arc track that trail is a run of
    # boresight crossings in the TRUE frame — rays from the operator's receiver,
    # unauthenticated, for any hex a caller cares to name.  The feed's own
    # entries are translated into the public frame; this endpoint reads
    # state.track_histories directly and would be the way around that.  It
    # exists to score solves against simulation ground truth, so without ground
    # truth it has no job to do and 404s like any other empty comparison.
    if fuzz_enabled() and not gt_trail:
        raise HTTPException(status_code=404, detail=f"No ground truth trail for {hex_code}")

    position_error_km = None
    if gt_trail and solved_trail:
        gt_last = gt_trail[-1]
        sol_last = solved_trail[-1]
        position_error_km = round(haversine_km(sol_last[0], sol_last[1], gt_last[0], gt_last[1]), 3)

    return {
        "hex": hex_code,
        "ground_truth_hex": matched_hex,
        "ground_truth_trail": gt_trail,
        "solved_trail": solved_trail,
        "position_error_km": position_error_km,
        "ground_truth_points": len(gt_trail),
        "solved_points": len(solved_trail),
    }


# ── Known-track hold (path H) ─────────────────────────────────────────────────


@router.get("/api/test/known-hold")
async def get_known_hold():
    """Current hold window, in seconds of frame time (0 = feature off)."""
    return {"max_gap_s": known_claiming.KNOWN_HOLD_MAX_GAP_S}


@router.put("/api/test/known-hold")
async def put_known_hold(body: dict = Body(...), _admin=Depends(require_admin)):
    """Set the hold window live, so the feature can be A/B'd on a running
    backend without a redeploy.  0 turns path H off entirely — the store stops
    being written as well, so "off" is the behaviour that predates the hold
    rather than a hold that never matches.

    Admin-gated on the same precedent as put_simulation_config: this changes
    which detections leave the dark pool for every node at once.
    """
    v = body.get("max_gap_s")
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 300):
        raise HTTPException(400, detail="max_gap_s must be 0-300")
    known_claiming.KNOWN_HOLD_MAX_GAP_S = float(v)
    if v == 0:
        # Off means off: a store left behind would come back the moment the
        # window was reopened, holding tracks from before the experiment.
        state.known_track_holds.clear()
    return {"max_gap_s": known_claiming.KNOWN_HOLD_MAX_GAP_S}


# ── Simulation physics config ─────────────────────────────────────────────────


@router.get("/api/simulation/config")
async def get_simulation_config():
    """Return current simulation physics configuration plus live object-type counts."""
    counts: dict[str, int] = {
        "anomalous": 0,
        "drone": 0,
        "aircraft": 0,
        "dark": 0,
        # Transponder-equipped aircraft currently inside an outage.  Counted
        # alongside (not instead of) its type bucket: a silent aircraft is
        # still a commercial aircraft, it just is not broadcasting, so this
        # is the one count that overlaps the others.
        "adsb_silent": 0,
        # Aircraft mirrored from the live ADS-B feed, split by the cast the
        # simulator gave them.  Like adsb_silent these overlap the type
        # buckets above (a live aircraft is also an "aircraft" or "dark"),
        # so the frac_live_dark knob is verifiable in one call.
        "live": 0,
        "live_adsb": 0,
        "live_dark": 0,
        "total": 0,
    }
    for meta in list(state.ground_truth_meta.values()):
        counts["total"] += 1
        if meta.get("adsb_silent"):
            counts["adsb_silent"] += 1
        if meta.get("source") == "live":
            counts["live"] += 1
            counts["live_adsb" if meta.get("has_adsb") else "live_dark"] += 1
        if meta.get("is_anomalous"):
            counts["anomalous"] += 1
        elif meta.get("object_type") == "drone":
            counts["drone"] += 1
        elif not meta.get("has_adsb"):
            # Dark: an aircraft flying without a transponder.  Counted apart
            # from "aircraft" so the frac_dark knob is verifiable in one call.
            counts["dark"] += 1
        else:
            counts["aircraft"] += 1
    return Response(
        content=orjson.dumps({**state.simulation_config, "ground_truth_counts": counts}),
        media_type="application/json",
    )


@router.put("/api/simulation/config")
async def put_simulation_config(body: dict = Body(...)):
    """Update simulation physics fractions.

    Open to anyone, like the GET beside it and the page that calls both: the
    simulator is a public demo, the console has no identity provider yet, and
    the fleet it reconfigures is synthetic. Nothing here touches a real node.

    Accepted keys: frac_anomalous, frac_drone, frac_dark (0.0–1.0 each).
    Sum of the three must not exceed 1.0 — the remainder is commercial aircraft.
    frac_adsb_outage (0.0–1.0) is deliberately OUTSIDE that sum: it is the
    fraction OF the ADS-B aircraft that go transponder-silent mid-flight,
    orthogonal to the spawn-type roll.
    frac_live_dark (0.0–1.0) is likewise outside it: the share of the
    aircraft the simulator mirrors from the live ADS-B feed that it casts
    as dark.  live_adsb_enabled (bool) pauses that feed.
    Optional: max_range_km (0 = auto, or 10–400), min_aircraft (1–500),
    max_aircraft (1–500).

    Also accepted — fleet scene keys, deliberately NO defaults (state.py's
    only-if-set pattern: a fresh backend never ships these, so the fleet
    container falls back to its own env; applying one is an orchestrator
    self-restart, see fleet-entrypoint.sh / retina_simulation.orchestrator):
    n_nodes (int, 4–100), dual_fraction (0.0–1.0).
    """
    allowed = {
        "frac_anomalous",
        "frac_drone",
        "frac_dark",
        "frac_adsb_outage",
        "frac_live_dark",
        "live_adsb_enabled",
        "max_range_km",
        "min_aircraft",
        "max_aircraft",
        "n_nodes",
        "dual_fraction",
    }
    updated = {}
    for k in allowed:
        if k in body:
            v = body[k]
            if k == "live_adsb_enabled":
                if not isinstance(v, bool):
                    raise HTTPException(400, detail=f"{k} must be true or false")
            elif k.startswith("frac_"):
                if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                    raise HTTPException(400, detail=f"{k} must be 0.0–1.0")
            elif k in ("max_range_km",):
                # 0 = no uniform override; every node keeps its generated
                # per-node range — matches FLEET_MAX_RANGE_KM=0 deployment
                # semantics.
                if not isinstance(v, (int, float)) or not (v == 0 or 10 <= v <= 400):
                    raise HTTPException(400, detail=f"{k} must be 0 or 10–400")
            elif k in ("min_aircraft", "max_aircraft"):
                if not isinstance(v, int) or not (1 <= v <= 500):
                    raise HTTPException(400, detail=f"{k} must be int 1–500")
            elif k == "n_nodes":
                # bool is an int subclass in Python — True/False must not
                # sneak through as 1/0.
                if not isinstance(v, int) or isinstance(v, bool) or not (4 <= v <= 100):
                    raise HTTPException(400, detail=f"{k} must be int 4–100")
            elif k == "dual_fraction":
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= v <= 1.0):
                    raise HTTPException(400, detail=f"{k} must be 0.0–1.0")
            updated[k] = v

    # frac_adsb_outage and frac_live_dark are intentionally absent here —
    # see the docstring.
    total_frac = (
        updated.get("frac_anomalous", state.simulation_config["frac_anomalous"])
        + updated.get("frac_drone", state.simulation_config["frac_drone"])
        + updated.get("frac_dark", state.simulation_config["frac_dark"])
    )
    if total_frac > 1.0:
        raise HTTPException(400, detail="Sum of frac_anomalous + frac_drone + frac_dark must be ≤ 1.0")

    state.simulation_config.update(updated)
    state.simulation_config["_updated_at"] = time.time()
    return Response(
        content=orjson.dumps({"ok": True, "config": state.simulation_config}),
        media_type="application/json",
    )


@router.get("/api/simulation/ground-truth")
async def get_simulation_ground_truth():
    """Return current ground truth aircraft positions (last known fix, max 30 s old)
    plus a lightweight solver-performance summary computed from server state.
    """
    now = time.time()
    gt_aircraft = []
    for hx, trail in list(state.ground_truth_trails.items()):
        if not trail:
            continue
        trail_list = list(trail)
        lat, lon, alt_m, ts = trail_list[-1]
        if now - ts > 30:
            continue
        # Derive heading/speed from last 2 trail points for frontend dead-reckoning
        gs_knots = 0.0
        track_deg = 0.0
        if len(trail_list) >= 2:
            p1, p2 = trail_list[-2], trail_list[-1]
            dt = p2[3] - p1[3]
            if dt > 0.1:
                dlat_m = (p2[0] - p1[0]) * 111_320
                dlon_m = (p2[1] - p1[1]) * 111_320 * math.cos(math.radians(p1[0] or 1e-9))
                dist_m = math.hypot(dlat_m, dlon_m)
                gs_knots = round(dist_m / dt * 1.94384, 1)
                track_deg = round(math.degrees(math.atan2(dlon_m, dlat_m)) % 360, 1)
        meta = state.ground_truth_meta.get(hx, {})
        gt_aircraft.append(
            {
                "hex": hx,
                "lat": lat,
                "lon": lon,
                "alt_m": alt_m,
                "gs": gs_knots,
                "track": track_deg,
                "speed_ms": meta.get("speed_ms", 0),
                "heading": meta.get("heading", 0),
                "ts": round(ts, 3),
                "object_type": meta.get("object_type", "aircraft"),
                "is_anomalous": meta.get("is_anomalous", False),
                "has_adsb": meta.get("has_adsb", False),
                "source": meta.get("source", "sim"),
            }
        )

    # ── solver performance ────────────────────────────────────────────────────
    gt_hex_set = {a["hex"] for a in gt_aircraft}
    gt_total = len(gt_hex_set)

    # Latest aircraft solved by the pipeline (what the map shows)
    solved_aircraft = state.latest_aircraft_json.get("aircraft", [])

    # Build solved-hex lookup (direct hex match + ground_truth_hex link)
    solved_by_hex: dict[str, list] = {}
    for ac in solved_aircraft:
        hx = ac.get("hex", "")
        if hx and "lat" in ac and "lon" in ac:
            solved_by_hex[hx] = [ac["lat"], ac["lon"]]
        gt_hx = ac.get("ground_truth_hex")
        if gt_hx and gt_hx not in solved_by_hex and "lat" in ac and "lon" in ac:
            solved_by_hex[gt_hx] = [ac["lat"], ac["lon"]]

    # Count unique GT objects that have at least one matching solved position
    # (by direct hex match or ground_truth_hex proximity link).
    # This avoids double-counting: multiple per-node tracks for the same
    # physical aircraft, or ADS-B + solver entries for the same target.
    matched_gt_hexes: set[str] = set()
    pos_errors: list[float] = []
    for hx in gt_hex_set:
        if hx in solved_by_hex:
            matched_gt_hexes.add(hx)
            trail = state.ground_truth_trails.get(hx)
            if trail:
                gt_last = list(trail)[-1]
                sol = solved_by_hex[hx]
                pos_errors.append(haversine_km(sol[0], sol[1], gt_last[0], gt_last[1]))
            if len(pos_errors) >= 200:
                break

    detected_count = len(matched_gt_hexes)
    avg_err = round(sum(pos_errors) / len(pos_errors), 2) if pos_errors else None

    return Response(
        content=orjson.dumps(
            {
                "aircraft": gt_aircraft,
                "total": gt_total,
                "performance": {
                    "gt_total": gt_total,
                    "detected": detected_count,
                    "detection_rate_pct": round(detected_count / gt_total * 100, 1) if gt_total else 0.0,
                    "avg_position_error_km": avg_err,
                    "multinode_tracks": len(state.multinode_tracks),
                    "tracked_with_error": len(pos_errors),
                },
            }
        ),
        media_type="application/json",
    )


def _mlat_verification_summary() -> dict:
    """Return a lightweight summary of the latest MLAT verification for the dashboard."""
    try:
        data = orjson.loads(state.latest_mlat_verification_bytes)
        return {
            "n_solves": data.get("n_solves", 0),
            "n_matched": data.get("n_matched", 0),
            "match_rate_pct": data.get("match_rate_pct", 0.0),
            "position_mean_km": data.get("position", {}).get("mean_km", 0),
            "position_p95_km": data.get("position", {}).get("p95_km", 0),
            "altitude_mean_m": data.get("altitude", {}).get("mean_m", 0),
        }
    except Exception:
        return {}


# ── Per-node solver verification ──────────────────────────────────────────────


@router.get("/api/test/node/{node_ref}/verification")
async def node_verification(node_ref: str):
    """Return pre-computed solver-vs-ADS-B verification stats for one node.

    Unauthenticated, so it is addressed and answered in published identities: a
    ref that resolves to nothing gets the empty body an unknown node already
    gets, and the payload names the node by the ref rather than by the id the
    refresh task keyed it under.

    Everything in a track entry is measured from this one node's true receiver,
    so the entries are served node-scoped (services/public_geometry.py): the
    per-track delays and the node's own solve position go, the errors beside
    them stay.  The store keeps the whole record for an authenticated surface.
    """
    node_id = id_for_identity(node_ref)
    raw = state.latest_node_verification_bytes.get(node_id, b"{}") if node_id else b"{}"
    payload = without_receiver_geometry(orjson.loads(raw), node_scoped=True)
    if "node_id" in payload:
        payload = {k: v for k, v in payload.items() if k != "node_id"}
        payload["node_ref"] = node_ref
    return Response(content=orjson.dumps(payload), media_type="application/json")


@router.get("/api/test/mlat-verification")
async def mlat_verification():
    """Return pre-computed multinode (MLAT) solver-vs-ground-truth verification stats."""
    return Response(
        content=state.latest_mlat_verification_bytes,
        media_type="application/json",
    )


@router.get("/api/test/mlat-history")
async def mlat_history(
    hex: str | None = None,
    all: int = 0,
    minutes: float = 30.0,
    lane: str = "all",
    limit: int = 1000,
    kind: str = "solves",
):
    """Per-solve MLAT history from the last ~30 minutes.

    ``?hex=mn...`` returns the published solves behind one map marker
    (matched on the solver-minted mn<sha256[:10]> hex), newest first, plus
    ``rejects_nearby``: gate-rejected solves within 10 km of the marker's
    newest published position — the signal for "the gates starved this track
    and the display held a stale point".  ``?all=1`` dumps the whole window
    for scripted debugging.  Records are written by the solver worker
    (services.tasks.solve_history._record_solve_history) into one deque per
    lane and merged here, so both lanes answer either query exactly as they
    did when they shared a deque.

    ``?lane=dark|known|adsb`` narrows the answer to one lane (default
    ``all``, classified by ``_record_lane``); ``?limit=`` caps the record
    list (default 1 000, max 5 000) and is applied PER LANE, so a known-lane
    burst can never push dark records out of an ``all`` response — see
    _cap_per_lane.

    ``?kind=resolve_skips`` dumps a different store entirely: the solver's
    recent resolve-slot refusals (state.solver_resolve_skips_recent), each
    with the claims that blocked it.  Those are not solve outcomes and
    deliberately do not live in the solve-history deques.

    ``window_effective_minutes`` is how much of the requested window the
    stores actually hold — below ``window_minutes`` the answer is truncated.

    Unauthenticated, so every record list below is served in published
    identities and without the receiver-relative geometry; the counts beside
    them are taken from the stores and so are unaffected by a record dropped
    for naming a node with no handle.  See _published_records.
    """
    if lane not in ("all", *_LANES):
        return Response(
            content=orjson.dumps({"error": f"lane must be one of all,{','.join(_LANES)}"}),
            media_type="application/json",
            status_code=400,
        )
    if kind not in ("solves", "resolve_skips"):
        return Response(
            content=orjson.dumps({"error": "kind must be solves or resolve_skips"}),
            media_type="application/json",
            status_code=400,
        )
    minutes = max(0.0, min(minutes, 35.0))
    limit = max(1, min(int(limit), 5000))
    cutoff_ms = int((time.time() - minutes * 60.0) * 1000)

    if kind == "resolve_skips":
        skips = [
            s
            for s in list(state.solver_resolve_skips_recent)
            if s["ts_ms"] >= cutoff_ms and (lane == "all" or s["lane"] == lane)
        ]
        skips.reverse()  # newest first
        payload = {
            "kind": "resolve_skips",
            "window_minutes": minutes,
            "lane": lane,
            "lane_counts": {ln: sum(1 for s in skips if s["lane"] == ln) for ln in _LANES},
            "n_records": len(skips),
            "records": _published_records(skips[:limit]),
        }
        return Response(content=orjson.dumps(payload), media_type="application/json")

    merged = _merged_solve_history()
    effective_minutes = _window_effective_minutes(merged, minutes)
    records = [r for r in merged if r["ts_ms"] >= cutoff_ms]
    records.reverse()  # newest first
    if lane != "all":
        records = [r for r in records if _record_lane(r) == lane]
    lane_counts = dict.fromkeys(_LANES, 0)
    for r in records:
        lane_counts[_record_lane(r)] += 1

    if all:
        payload = {
            "window_minutes": minutes,
            "window_effective_minutes": effective_minutes,
            "lane": lane,
            # Pre-cap, so a truncated `records` can be read against what the
            # window actually held.
            "lane_counts": lane_counts,
            "n_records": len(records),
            "records": _published_records(_cap_per_lane(records, limit)),
        }
        return Response(content=orjson.dumps(payload), media_type="application/json")

    norm = (hex or "").strip().lower()
    if not norm:
        return Response(
            content=orjson.dumps({"error": "pass ?hex=mn... or ?all=1"}),
            media_type="application/json",
            status_code=400,
        )
    solves = [r for r in records if r.get("solver_hex") == norm]
    rejects_nearby: list[dict] = []
    if solves:
        ref = solves[0]
        ref_lat, ref_lon = ref.get("raw_lat"), ref.get("raw_lon")
        if ref_lat is not None and ref_lon is not None:
            rejects_nearby = [
                r
                for r in records
                if r.get("outcome") != "published"
                and r.get("raw_lat") is not None
                and haversine_km(ref_lat, ref_lon, r["raw_lat"], r["raw_lon"]) <= 10.0
            ]
    payload = {
        "hex": norm,
        "window_minutes": minutes,
        "window_effective_minutes": effective_minutes,
        "lane": lane,
        "lane_counts": lane_counts,
        "n_solves": len(solves),
        "solves": _published_records(solves[:500]),
        "rejects_nearby": {
            "n": len(rejects_nearby),
            "by_outcome": {
                o: sum(1 for r in rejects_nearby if r["outcome"] == o)
                for o in sorted({r["outcome"] for r in rejects_nearby})
            },
            "records": _published_records(rejects_nearby[:200]),
        },
    }
    return Response(content=orjson.dumps(payload), media_type="application/json")


@router.get("/api/test/solver-stats")
async def solver_stats(minutes: float = 10.0):
    """Full solver picture for the Solver Report panel: publication funnel
    (n=2 vs n>=3), reject-reason breakdown, position-error percentiles,
    ghost/false-track precision, and consensus/since-boot counters.

    The funnel, the error percentiles, the fragmentation block and the ghost
    precision are all the DARK lane — ``lane_split`` gives the per-lane record
    counts for the window and ``known_lane`` the known lane's own numbers.
    See ``_solver_window_stats`` for why, plus the ghost definition and the
    gate constants.

    Two blocks answer "why is the dark lane doing this?" rather than "what is
    it doing":

    ``by_n_nodes`` (and ``by_n_nodes_known``) re-splits the same windowed
    records by the node count of each attempt — attempts, published,
    publish_rate, the top four reject reasons and the median GT error per
    bucket (2 / 3 / 4 / 5+, with "<2" for records carrying no node count).
    The dark lane is not uniform in n and the aggregate funnel hides it.

    ``dark_follow`` is the follow lane's funnel plus ``ineligible``, a
    per-reason tally of the dark keys the lane refused to follow, one entry
    per gate in ``dark_follow._build_targets``.  Those counters are
    KEY-SECONDS (the target list is rebuilt once a second and re-tests every
    dark key), so they are read against each other and against
    ``targets_now``, never against ``inputs``/``published``.
    """
    minutes = max(1.0, min(minutes, 35.0))
    payload = _solver_window_stats(minutes)
    return Response(content=orjson.dumps(payload), media_type="application/json")


@router.get("/api/test/mlat-accuracy")
async def mlat_accuracy():
    """Rolling MLAT solver accuracy stats aggregated from the last 5 000 matched tracks.

    Mirrors GET /api/radar/accuracy (single-node) but broken down by node count
    instead of position_source.  Updates every 30 s alongside the main verification
    refresh and is useful for detecting long-term accuracy degradation.
    """
    return Response(
        content=state.latest_mlat_accuracy_bytes,
        media_type="application/json",
    )


@router.get("/api/test/node/{node_ref}/detection-range")
async def node_detection_range(node_ref: str):
    """Return one node's empirical detection range and coverage polygon.

    Unauthenticated, so both the identity and the geometry are the published
    ones: the node is addressed and named by its ref, an unresolvable ref gets
    the same answer as an unregistered node, ``rx`` is the fuzzed coordinate
    and the polygon is translated rigidly by the same offset, exactly as on
    /api/radar/analytics.

    ``furthest_detections`` used to ride along and no longer does.  Each entry
    was a real aircraft's lat/lon together with its distance from the true
    receiver — a ranging circle per detection, three of which intersect at the
    receiver regardless of what coordinate the payload claims.  That is a
    sharper disclosure than the position field it sat next to, and no caller
    (frontend, dashboard, or test) reads it.
    """
    node_id = id_for_identity(node_ref)
    area = state.node_analytics.detection_areas.get(node_id) if node_id else None
    if not area:
        return Response(
            content=orjson.dumps({"error": "node not registered"}),
            media_type="application/json",
            status_code=404,
        )

    # Geometry first (services/public_geometry.py withholds furthest_detections
    # and anything else receiver-relative, at any depth), then the identity: the
    # node is named by the ref it was addressed as, never by the id the store
    # keys it under.
    summary = {k: v for k, v in without_receiver_geometry(area.summary()).items() if k != "node_id"}
    summary["node_ref"] = node_ref
    rx = summary.get("rx") or {}
    pub_lat, pub_lon = public_latlon(rx.get("lat"), rx.get("lon"), node_id)
    summary["rx"] = {**rx, "lat": pub_lat, "lon": pub_lon}

    # Empirical coverage polygon — apex is the true RX, so it moves with it.
    ecov = state.node_analytics.empirical_coverages.get(node_id)
    polygon = None
    if ecov:
        polygon = translate_polygon(
            ecov.to_polygon(
                beam_azimuth_deg=area.beam_azimuth_deg,
                beam_width_deg=area.beam_width_deg,
            ),
            node_id,
            anchor_lat=rx.get("lat"),
        )

    return Response(
        content=orjson.dumps(
            {
                **summary,
                "empirical_coverage_polygon": polygon,
            }
        ),
        media_type="application/json",
    )
