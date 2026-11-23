"""Radar pipeline endpoints: receiver/aircraft JSON, detections, nodes, status, leaderboard."""

import asyncio
import concurrent.futures
import logging
import os
import time
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from config.constants import RATE_BUCKETS_MAX_IPS
from core import state
from core.users import get_optional_user
from pipeline.passive_radar import PassiveRadarPipeline
from routes.node_schemas import NodeRef
from services import node_refs, node_registration, publication
from services.node_config import canonical_config
from services.node_pipeline import config_hash
from services.public_location import public_latlon
from services.publication import is_private
from services.tcp_handler import is_synthetic_node

router = APIRouter(tags=["radar"])


# ── Request models ────────────────────────────────────────────────────────────


class DetectionRequest(BaseModel):
    node_id: str = Field(default="http-node", max_length=128)
    frames: list[dict] | None = None
    # Allow extra fields — individual frames are dicts with variable keys
    model_config = {"extra": "allow"}


class BulkNodeEntry(BaseModel):
    node_id: str = Field(default="http-node", max_length=128)
    # The handle the sending environment publishes this node under. Mirrored
    # nodes have no row here, so it is the only ref this server can name one
    # by (services/node_refs._mirrored_ref), which is why it is validated
    # against the same pattern a minted ref must match: a value shaped like a
    # node_id would otherwise be published as one.
    node_ref: NodeRef | None = None
    config: dict | None = None
    frames: list[dict] = Field(default_factory=list)


class BulkDetectionRequest(BaseModel):
    nodes: list[BulkNodeEntry] = Field(..., max_length=500)


RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")
_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()
if not RADAR_API_KEY:
    _msg = "RADAR_API_KEY is not set — detection/custody endpoints have no API key protection"
    if _RETINA_ENV not in ("dev", "test"):
        logging.error(_msg)  # loud in production/staging, doesn't crash the server
    else:
        logging.warning(_msg)
_RATE_LIMIT = int(os.getenv("RADAR_RATE_LIMIT", "60"))
_RATE_WINDOW = int(os.getenv("RADAR_RATE_WINDOW", "60"))

# Module-level reference to default pipeline; set from main.py at startup
_default_pipeline: PassiveRadarPipeline | None = None


def init(pipeline: PassiveRadarPipeline):
    global _default_pipeline
    _default_pipeline = pipeline


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = state.rate_buckets[ip]
    recent = [t for t in bucket if now - t < _RATE_WINDOW]
    if recent:
        state.rate_buckets[ip] = recent
    else:
        # All timestamps expired — free the key; defaultdict recreates on next append
        del state.rate_buckets[ip]
    if len(recent) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    # Prevent unbounded memory growth from unique IPs — evict oldest half
    if len(state.rate_buckets) > RATE_BUCKETS_MAX_IPS:
        evict_count = len(state.rate_buckets) // 2
        for old_ip in list(state.rate_buckets)[:evict_count]:
            del state.rate_buckets[old_ip]
    state.rate_buckets[ip].append(now)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/radar/data/receiver.json")
async def tar1090_receiver():
    return _default_pipeline.generate_receiver_json()


@router.get("/api/radar/data/aircraft.json")
async def tar1090_aircraft():
    return Response(content=state.latest_aircraft_json_bytes, media_type="application/json")


@router.get("/api/radar/data/aircraft-live.json")
async def tar1090_aircraft_live():
    """Real-node-only aircraft data for app.retina.fm HTTP polling fallback."""
    return Response(content=state.latest_real_aircraft_json_bytes, media_type="application/json")


@router.post("/api/radar/detections")
async def ingest_detections(
    request: Request,
    body: DetectionRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    if not (RADAR_API_KEY and x_api_key == RADAR_API_KEY):
        client_ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        _check_rate_limit(client_ip)

    node_id = body.node_id
    body_dict = body.model_dump(exclude_none=True)
    frames = body.frames if body.frames is not None else [body_dict]

    if node_id not in state.connected_nodes:
        # This path carries no geometry at all: the node is counted, and stays
        # unplaced until it configures itself over TCP or the v1 API.
        legacy_config = canonical_config({"node_id": node_id})
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {
                "config_hash": "",
                "config": legacy_config,
                "status": "active",
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "peer": "http",
                "is_synthetic": is_synthetic_node(node_id),
                "capabilities": {},
            }
        await node_registration.register_node(node_id, legacy_config)
    else:
        with state.connected_nodes_lock:
            state.connected_nodes[node_id]["status"] = "active"
            state.connected_nodes[node_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

    processed = 0
    for frame in frames:
        if "timestamp" not in frame:
            continue
        frame["_node_id"] = node_id
        try:
            state.frame_queue.put_nowait((node_id, frame))
            processed += 1
        except asyncio.QueueFull:
            logging.warning("Frame queue full, dropping frame from %s", node_id)

    return {
        "status": "ok",
        "frames_queued": processed,
        "tracks": len(state.latest_aircraft_json.get("aircraft", [])),
    }


def _record_mirrored_ref(node_id: str, node_ref: str | None) -> None:
    """Record the handle the sending environment publishes this node under.

    Refused when the local registry already names another node under it: two
    nodes sharing a handle would both answer to it on every surface while the
    reverse map named only one, so the collision is dropped rather than
    resolved silently. Only the registry is consulted, so two mirrored nodes
    that both arrive claiming one ref, neither of them registered here, are not
    caught; the sending environment's registry is what keeps refs unique.
    """
    if not node_ref or node_refs.id_for_ref(node_ref) not in (None, node_id):
        return
    with state.connected_nodes_lock:
        known = state.connected_nodes.get(node_id)
        if known is not None:
            known["node_ref"] = node_ref


@router.post("/api/radar/detections/bulk")
async def ingest_detections_bulk(
    request: Request,
    body: BulkDetectionRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    registered = 0
    queued = 0
    for entry in body.nodes:
        node_id = entry.node_id
        frames = entry.frames

        with state.connected_nodes_lock:
            known = state.connected_nodes.get(node_id)
            if known is not None and entry.config is None:
                # A follow-up call that omits config says nothing about the
                # node's geometry; it is not a claim that the config is now
                # the trivial {"node_id": node_id} dict. That dict never
                # hashes to match a real stored config, so treat this as no
                # change rather than re-registering on every such call.
                changed = False
            else:
                entry_config = entry.config or {"node_id": node_id}
                # Hashed as declared, stored canonical: see register_with_pipeline.
                entry_hash = config_hash(entry_config)
                entry_config = canonical_config(entry_config)
                # A hash mismatch only triggers re-registration for a node this
                # endpoint itself created. Otherwise a caller holding RADAR_API_KEY
                # could strip a live v1 or TCP node's geometry by naming it in a
                # bulk entry, until that node's next config PUT restores it.
                changed = (
                    known is not None and known.get("peer") == "http-bulk" and known.get("config_hash") != entry_hash
                )

        if known is None or changed:
            with state.connected_nodes_lock:
                state.connected_nodes[node_id] = {
                    "config_hash": entry_hash,
                    "config": entry_config,
                    "status": "active",
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "peer": "http-bulk",
                    "is_synthetic": is_synthetic_node(node_id),
                    "capabilities": {},
                }
            # Sole writer of node_ref on both branches: setting it in the
            # literal above would seat a colliding ref before the guard runs,
            # and the guard refuses by returning rather than clearing.
            _record_mirrored_ref(node_id, entry.node_ref)
            # A cached pipeline was built from the config that was active when
            # it was created, and nothing else refreshes it.
            node_registration.evict_pipeline(node_id)
            await node_registration.register_node(node_id, entry_config)
            registered += 1
        else:
            with state.connected_nodes_lock:
                state.connected_nodes[node_id]["status"] = "active"
                state.connected_nodes[node_id]["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            # Also on the unchanged path: a sender that starts sending refs
            # against entries this server already holds moves neither `known`
            # nor `changed`, so writing it only at registration would leave
            # every existing node without one until a restart.
            _record_mirrored_ref(node_id, entry.node_ref)

        for frame in frames:
            if "timestamp" not in frame:
                continue
            frame["_node_id"] = node_id
            try:
                state.frame_queue.put_nowait((node_id, frame))
                queued += 1
            except asyncio.QueueFull:
                break

    return {"status": "ok", "nodes_registered": registered, "frames_queued": queued}


@router.get("/api/radar/status")
async def radar_status():
    # Unauthenticated, and the config block quotes the default pipeline's
    # receiver — so it quotes the published coordinate, like every other
    # public surface.  Not in the original privacy sweep's list of sites; it
    # is the same disclosure by the same route class, and nothing reads it.
    #
    # This route names exactly one node — the process-wide default pipeline,
    # whose geometry is DEFAULT_NODE_CONFIG rather than a registered fleet
    # member — so publication is a yes/no on that one node rather than a
    # listing filter.  A deployment that points the default pipeline at a real
    # node that registered private must not have that node's coordinate quoted
    # here, published or otherwise; the block is served with nulls instead of
    # omitted, so a client parsing it still finds the keys it expects.
    if is_private(_default_pipeline.config.get("node_id")):
        _rx_lat, _rx_lon = None, None
    else:
        _rx_lat, _rx_lon = public_latlon(
            _default_pipeline.config["rx_lat"],
            _default_pipeline.config["rx_lon"],
            _default_pipeline.config.get("node_id"),
        )
    return {
        # Always null: the default pipeline is a process-wide fallback with no
        # registry row, so it has no ref, and its node_id must not stand in for
        # one here.  The key stays present so a consumer reads an explicit null
        # rather than a missing field.
        "node_ref": None,
        "total_tracks": len(_default_pipeline.tracker.tracks),
        "geolocated_tracks": len(_default_pipeline.geolocated_tracks),
        "multinode_tracks": len(state.multinode_tracks),
        "track_events": len(_default_pipeline.event_writer.get_events()),
        "external_adsb_cached": len(state.external_adsb_cache),
        "config": {
            "rx_lat": _rx_lat,
            "rx_lon": _rx_lon,
            "tx_lat": _default_pipeline.config["tx_lat"],
            "tx_lon": _default_pipeline.config["tx_lon"],
            "FC": _default_pipeline.config["FC"],
            "Fs": _default_pipeline.config["Fs"],
        },
    }


@router.get("/api/radar/nodes")
async def radar_nodes():
    return Response(content=state.latest_nodes_bytes, media_type="application/json")


# ── Leaderboard ───────────────────────────────────────────────────────────────


class PublicLeaderboardRow(BaseModel):
    """One leaderboard row as published to anyone, session or none.

    Keyed on node_ref and carrying only the per-ref metrics
    /api/radar/analytics already publishes, so it stands on the publication
    side of D16. A field declared here is published by that act; node_id in
    particular would let this feed and /api/radar/analytics be joined and
    recover the mapping the boundary exists to withhold.

    `extra="forbid"` makes a stray keyword at construction an error, which on
    this route is a 500 that publishes nothing rather than a field that
    quietly does or does not go out.
    """

    model_config = ConfigDict(extra="forbid")

    node_ref: str
    name: str
    rank: int
    detections: int
    frames: int
    tracks: int
    # Share of the last week's minutes the node delivered in; None until a
    # whole minute has been measured.
    availability_7d: float | None
    avg_snr: float
    trust_score: float
    reputation: float
    online: bool


class SignedInLeaderboardRow(PublicLeaderboardRow):
    """A row with the per-node miss counts, for a caller with a session.

    These come from state.latest_missed_detections rather than from the
    published analytics snapshot, and nothing else serves them per node
    without one: /health reduces the same global to a fleet-wide average
    precisely so the breakdown stays off an unauthenticated endpoint.
    """

    in_range: int
    detected_in_range: int
    missed: int
    miss_rate: float


class Leaderboard[LeaderboardRow: PublicLeaderboardRow](BaseModel):
    """Parametrised by row class because pydantic serialises a field by its
    declared type: `Leaderboard[PublicLeaderboardRow]` sends only the public
    fields whatever rows it holds, and would strip a signed-in caller's miss
    counts in the same way."""

    model_config = ConfigDict(extra="forbid")

    leaderboard: list[LeaderboardRow]
    total: int


# The cold-start recomputation below runs off the event loop on its own
# executor, so it never competes with the default thread pool used by frame
# processors. One worker is enough: the coalescing below never has more than
# one call in flight.
_leaderboard_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="leaderboard")

# Shared by every leaderboard request during the cold-start gap (see
# radar_leaderboard() below): a burst of concurrent anonymous callers submits
# one work item to _leaderboard_executor rather than one each.
# get_all_summaries() already caches its own result, so this needs no
# freshness window of its own, unlike services.infrastructure.snapshot()'s
# lock-plus-TTL-cache single-flight — only the one in-flight call to avoid
# duplicating.
_cold_start_summaries_task: asyncio.Future | None = None


def _reset_for_tests() -> None:
    """Drop the in-flight task reference.

    The happy path is self-clearing (a completed future is always replaced),
    but a test that fails mid-await can leave this pending across its closed
    event loop; the next test to touch it would then hit a confusing "Future
    attached to a different loop" error that masks the original failure.
    """
    global _cold_start_summaries_task
    _cold_start_summaries_task = None


def _consume_result(future: asyncio.Future) -> None:
    """Mark an abandoned Future's exception as read, so asyncio does not log
    it as never retrieved (services.tasks.executor guards the same way)."""
    if not future.cancelled():
        future.exception()


async def _cold_start_summaries() -> dict:
    """The raw {node_id: summary} map, coalesced across concurrent callers."""
    global _cold_start_summaries_task
    # No lock: nothing awaits between the check and the assignment, so the
    # single-threaded event loop makes this atomic.
    if _cold_start_summaries_task is None or _cold_start_summaries_task.done():
        loop = asyncio.get_running_loop()
        _cold_start_summaries_task = loop.run_in_executor(_leaderboard_executor, state.node_analytics.get_all_summaries)
        _cold_start_summaries_task.add_done_callback(_consume_result)
    # Shielded: an unshielded await lets one caller's cancellation (a client
    # disconnect) cancel this shared Future, handing CancelledError to every
    # other caller sharing it.
    return await asyncio.shield(_cold_start_summaries_task)


@router.get("/api/radar/leaderboard")
async def radar_leaderboard(caller=Depends(get_optional_user)):
    """Rankings by detections, availability and trust, open to anyone.

    A caller with no session gets PublicLeaderboardRow, which is what lets the
    dashboard's /leaderboard render to a visitor; one with a session gets
    SignedInLeaderboardRow. Which account the session belongs to changes
    nothing: an owner's nodes are ranked, and withheld, exactly as anyone
    else's.

    No response_model: one would filter every caller's rows to the same
    shape. The row class is chosen per caller below and every field is passed
    by name, so what leaves is exactly what that class declares.
    """
    # Use the pre-computed analytics snapshot (refreshed every 30 s by the
    # background task) to avoid holding the analytics lock in this handler.
    raw = state.latest_analytics_bytes
    summaries: dict = {}
    if raw and raw != b"{}":
        try:
            summaries = orjson.loads(raw).get("nodes", {})
        except Exception:
            logging.debug("analytics snapshot bytes unparseable", exc_info=True)
    if summaries:
        # The snapshot is built for publication, so it is already keyed on
        # node_ref, which is what this route reports.  The node_id rides
        # alongside because everything else read below is node_id-keyed; a
        # synthetic node publishes as itself and has no reverse row, hence the
        # fallback.
        rows = [(ref, node_refs.id_for_ref(ref) or ref, s) for ref, s in summaries.items()]
    else:
        # Fall back to live computation only if the snapshot is empty, which is
        # every process start until the first refresh.  Keyed on node_id, so it
        # has to pass both halves of the boundary the snapshot has already been
        # through: public_summaries drops a node that withheld its location,
        # and public_identity leaves out one with no handle rather than naming
        # it.  The second does not imply the first — it asks whether a node has
        # a registry ref, not whether it consented to being published.
        live = await _cold_start_summaries()
        published = publication.public_summaries(live)
        rows = [(ref, nid, s) for nid, s in published.items() if (ref := node_refs.public_identity(nid))]

    with state.connected_nodes_lock:
        connected = dict(state.connected_nodes)
    rows.sort(key=lambda row: row[2].get("metrics", {}).get("total_detections", 0), reverse=True)
    entries: list[PublicLeaderboardRow] = []
    for rank, (node_ref, node_id, s) in enumerate(rows, start=1):
        m = s.get("metrics", {})
        node = connected.get(node_id, {})
        row = PublicLeaderboardRow(
            node_ref=node_ref,
            # public_name hands back the ref itself in place of a name it will
            # not publish, so the fallback here only narrows the type.
            name=node_refs.public_name(node.get("config", {}).get("name"), node_ref, connected.keys()) or node_ref,
            rank=rank,
            detections=m.get("total_detections", 0),
            frames=m.get("total_frames", 0),
            tracks=m.get("total_tracks", 0),
            availability_7d=m.get("availability_7d"),
            avg_snr=m.get("avg_snr", 0),
            trust_score=s.get("trust", {}).get("trust_score", 0),
            reputation=s.get("reputation", {}).get("reputation", 0),
            online=node.get("status") not in ("disconnected", None),
        )
        if caller is not None:
            miss = state.latest_missed_detections.get(node_id, {})
            # The spread is bounded by PublicLeaderboardRow's declared fields.
            row = SignedInLeaderboardRow(
                **row.model_dump(),
                in_range=miss.get("in_range", 0),
                detected_in_range=miss.get("detected", 0),
                missed=miss.get("missed", 0),
                miss_rate=miss.get("miss_rate", 0.0),
            )
        entries.append(row)
    row_class = PublicLeaderboardRow if caller is None else SignedInLeaderboardRow
    return Leaderboard[row_class](leaderboard=entries, total=len(entries))
