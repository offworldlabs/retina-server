"""Node analytics and inter-node association endpoints."""

import os
import time
from collections import Counter

import orjson
from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import Response
from retina_analytics.trust import AdsReportEntry

from core import state
from services import node_bias
from services.public_location import public_node_summary
from services.publication import is_private

_RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")

router = APIRouter()


@router.get("/api/radar/analytics")
async def radar_analytics(real_only: bool = False):
    if real_only:
        return Response(content=state.latest_analytics_real_bytes, media_type="application/json")
    return Response(content=state.latest_analytics_bytes, media_type="application/json")


@router.get("/api/radar/analytics/{node_id}")
async def radar_node_analytics(node_id: str):
    # A node whose owner registered it private is 404 here, not 403: the two
    # answers differ only in whether they confirm the node exists, and this
    # route is reachable by anyone with a node id to try.  Same status the
    # cached listing produces by omission, so the two surfaces agree.
    if is_private(node_id):
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    summary = state.node_analytics.get_node_summary(node_id)
    if summary.keys() == {"node_id"}:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    # Same blocks as the cached /api/radar/analytics payload, built fresh — so
    # the same receiver-geometry rewrite has to happen here too, or this route
    # is the hole the cached one closed.  See services/public_location.py.
    summary = public_node_summary(node_id, summary)
    # Backend-computed bias estimate from claim residuals — same conditional
    # shape as the manager's own blocks: present only once the node has
    # residual history.  The trust block above already blends backend-fed
    # samples (see its samples_by_provenance breakdown).
    bias = node_bias.node_summary(node_id)
    if bias is not None:
        summary = {**summary, "node_bias": bias}
    return summary


@router.post("/api/radar/analytics/adsb-report")
async def submit_adsb_report(
    body: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if _RADAR_API_KEY and x_api_key != _RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    required = ["node_id", "predicted_delay", "measured_delay"]
    missing = [k for k in required if k not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing: {missing}")

    entry = AdsReportEntry(
        timestamp_ms=body.get("timestamp_ms", 0),
        predicted_delay=body["predicted_delay"],
        predicted_doppler=body.get("predicted_doppler", 0),
        measured_delay=body["measured_delay"],
        measured_doppler=body.get("measured_doppler", 0),
        adsb_hex=body.get("adsb_hex", ""),
        adsb_lat=body.get("adsb_lat", 0),
        adsb_lon=body.get("adsb_lon", 0),
    )
    state.node_analytics.record_adsb_correlation(body["node_id"], entry)
    ts = state.node_analytics.trust_scores.get(body["node_id"])
    return {
        "status": "recorded",
        "trust_score": round(ts.score, 4) if ts else 0.0,
        "n_samples": len(ts.samples) if ts else 0,
    }


@router.get("/api/radar/association/overlaps")
async def association_overlaps():
    """Which node pairs have an overlapping detection zone, and how big it is.

    Serves no geometry, so nothing here is fuzzed and it stays public.  The
    overlap zones are computed FROM node positions, but
    ``NodeAssociator.get_overlap_summary()`` emits only the two node ids, a
    grid-point count, the delay/Doppler gates and a has_overlap flag — no
    coordinates, no grid, no extent.  ``registered_nodes`` is a list of ids.
    A pair count and a gate width constrain the inter-node distance far too
    loosely to locate either node, so admin-gating this would cost the map its
    coverage layer and buy nothing.  If this payload ever grows a lat/lon (a
    zone centroid, a bounding box, the grid itself), it has to be rebuilt from
    published positions or moved behind require_admin before it ships.
    """
    return Response(content=state.latest_overlaps_bytes, media_type="application/json")


@router.get("/api/radar/accuracy")
async def radar_accuracy():
    """Solver-vs-ADS-B accuracy stats (mean, median, P95, per-source breakdown)."""
    return Response(content=state.latest_accuracy_bytes, media_type="application/json")


@router.get("/api/radar/association/status")
async def association_status():
    """What association is currently doing.

    This used to report ``pending_frames``, ``pending_frame_age_ms`` and a
    ``frame_skew`` block.  All three read state that only ``submit_frame``
    writes, and the server has run ``submit_tracks`` since the track path took
    over — so all three had been reporting empty or zero, indefinitely, while
    looking like live telemetry.  They are replaced by the counters that do
    move on the path in use.
    """
    _a = state.node_associator
    return {
        "registered_nodes": len(_a.node_geometries),
        "overlap_zones": len(_a.overlap_zones),
        # Confirmed single-node tracks each node last submitted; these are what
        # pairings are drawn from.
        "pending_tracks": {nid: len(tracks) for nid, tracks in list(_a._pending_tracks.items())},
        # Track-pairing outcomes since boot.  gated is everything past the
        # coarse delay grid; unfitted counts the pairings handed to the solver
        # worker (which runs the fit and the n=2 gate); deferred counts rounds
        # a budget cut short.  Those three are the live production surface.
        "track_pairs": {
            "gated": getattr(_a, "track_pairs_gated", 0),
            "unfitted": getattr(_a, "track_pairs_unfitted", 0),
            "deferred": getattr(_a, "track_pairs_deferred", 0),
        },
        # Inline-fit counters — permanently zero in production BY DESIGN
        # (state.py builds the associator with cv_fit=None; only the offline
        # bench's inline mode exercises stage-2 selection).  Split out so
        # nobody reads a structural zero as "no rejections happening".
        "track_pairs_inline_only": {
            "accepted": getattr(_a, "track_pairs_accepted", 0),
            "rejected": getattr(_a, "track_pairs_rejected", 0),
            "superseded": getattr(_a, "track_pairs_superseded", 0),
        },
        # Top-down claiming (ASSOC_CLAIM_MODE) since boot.  rounds/matched/
        # conflicts/anchored_inputs are all live in shadow too — _claim_round
        # computes and counts them unconditionally; shadow only forces the
        # RETURNED anchored-input list (and claimed ids) back to empty
        # afterward, in submit_tracks_round, which is what makes it provably
        # inert downstream.  tracklets_excluded is the one that only ever
        # moves in active mode, since the exclusion filter never runs
        # against an empty claimed-ids map.  off mode reads zero everywhere
        # here BY DESIGN, not because nothing is happening.
        "claiming": {
            "mode": _a.claim_mode,
            "rounds": getattr(_a, "claim_rounds", 0),
            "matched": getattr(_a, "claims_matched", 0),
            "conflicts": getattr(_a, "claim_conflicts", 0),
            "anchored_inputs": getattr(_a, "anchored_inputs_emitted", 0),
            "tracklets_excluded": getattr(_a, "tracklets_excluded", 0),
        },
        # ADS-B seeding (ADSB_SEED_MODE), same "cumulative since boot,
        # shadow moves everything but tracklets_excluded" shape as claiming
        # above: tagged/no_state/gate_rejects/inputs_emitted move in shadow
        # too — _adsb_seed_round computes and counts them unconditionally;
        # tracklets_excluded only moves in active, since the exclusion
        # filter never runs against an empty tagged-ids map.  off reads
        # zero everywhere here BY DESIGN.
        "adsb_seed": {
            "mode": _a.adsb_seed_mode,
            "rounds": getattr(_a, "adsb_seed_rounds", 0),
            "tagged": getattr(_a, "adsb_tracklets_tagged", 0),
            "no_state": getattr(_a, "adsb_seed_no_state", 0),
            "gate_rejects": getattr(_a, "adsb_seed_gate_rejects", 0),
            "world_rejects": getattr(_a, "adsb_seed_world_rejects", 0),
            "tracklets_excluded": getattr(_a, "adsb_tracklets_excluded", 0),
            "inputs_emitted": getattr(_a, "adsb_inputs_emitted", 0),
        },
        "overlaps": _a.get_overlap_summary(),
    }


@router.get("/api/radar/anomalies")
async def radar_anomalies():
    """Anomaly metrics: summary, breakdown by type, timeline, geographic clusters, recent events."""
    now = time.time()

    with state.anomaly_lock:
        log_snapshot = list(state.anomaly_log)
        active_hexes = set(state.anomaly_hexes)

    # --- Live anomaly types from aircraft.json ---
    # The redacted feed: this route is unauthenticated, and an anomaly count is
    # still a statement about a private node's detections.
    live_aircraft = state.latest_aircraft_json_public.get("aircraft", [])
    live_type_counts: Counter = Counter()
    for ac in live_aircraft:
        if ac.get("is_anomalous"):
            for atype in ac.get("anomaly_types", []):
                live_type_counts[atype] += 1

    # --- Breakdown by type from log + live ---
    log_type_counts: Counter = Counter()
    for ev in log_snapshot:
        log_type_counts[ev.get("reason", "unknown")] += 1

    by_type = dict(log_type_counts + live_type_counts)

    # --- Unique hexes in log ---
    unique_hexes = {ev.get("hex") for ev in log_snapshot if ev.get("hex")}

    # --- Timeline: 1-hour buckets over last 24h ---
    bucket_size = 3600
    cutoff = now - 86400
    buckets: Counter = Counter()
    for ev in log_snapshot:
        ts = ev.get("ts", 0)
        if ts >= cutoff:
            b = int(ts // bucket_size) * bucket_size
            buckets[b] += 1

    # Fill empty buckets so the chart is continuous
    timeline = []
    if buckets:
        first = min(buckets)
        last = max(buckets)
    else:
        first = int(cutoff // bucket_size) * bucket_size
        last = int(now // bucket_size) * bucket_size
    b = first
    while b <= last:
        timeline.append({"ts": b, "count": buckets.get(b, 0)})
        b += bucket_size

    # --- Geographic clusters: 0.1° grid ---
    geo_grid: dict[tuple, list] = {}
    for ev in log_snapshot:
        lat = ev.get("lat")
        lon = ev.get("lon")
        if lat is None or lon is None:
            continue
        key = (round(lat, 1), round(lon, 1))
        geo_grid.setdefault(key, []).append(ev.get("reason", "unknown"))

    clusters = []
    for (glat, glon), reasons in geo_grid.items():
        dominant = Counter(reasons).most_common(1)[0][0] if reasons else "unknown"
        clusters.append(
            {
                "lat": glat,
                "lon": glon,
                "count": len(reasons),
                "dominant_type": dominant,
            }
        )
    clusters.sort(key=lambda c: c["count"], reverse=True)

    # --- Most common anomaly type ---
    all_types = log_type_counts + live_type_counts
    most_common = all_types.most_common(1)[0][0] if all_types else None

    payload = {
        "summary": {
            "active_count": len(active_hexes),
            "total_events": len(log_snapshot),
            "unique_hexes": len(unique_hexes),
            "most_common_type": most_common,
        },
        "by_type": by_type,
        "timeline": timeline,
        "geographic_clusters": clusters[:50],
        "recent_events": log_snapshot,
    }
    return Response(
        content=orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY),
        media_type="application/json",
    )
