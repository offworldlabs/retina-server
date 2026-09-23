"""Admin-only API routes — user management, events, config, leaderboard."""

import asyncio
import concurrent.futures
import json
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# Dedicated executor for blocking admin operations so they never compete with
# the default thread pool used by frame processors.
_admin_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin-io")

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import (
    CONFIG_LIVE_CACHE_TTL_S,
    EVENT_LOG_MAX,
    NODE_HEALTH_CHECK_INTERVAL_S,
    NODE_OFFLINE_THRESHOLD_S,
)
from core import state
from core.auth import list_node_owners
from core.nodes import Node
from core.runtime_config import runtime_path, write_runtime_file
from core.task_registry import get_stale_tasks
from core.users import (
    User,
    get_async_session,
    get_current_user,
    get_optional_user,
    require_admin,
    user_to_dict,
)
from services import publication
from services.node_claim_store import set_owner
from services.node_refs import id_for_ref, public_identity, public_name, ref_to_id_map
from services.tasks import multinode_identity

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Persistent event log ─────────────────────────────────────────────────────

_EVENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "events.json"
_events: deque = deque(maxlen=EVENT_LOG_MAX)


def _load_events():
    """Load events from disk on startup."""
    if _EVENTS_FILE.exists():
        try:
            data = json.loads(_EVENTS_FILE.read_text())
            for ev in data:
                _events.append(ev)
        except Exception:
            logger.debug("could not load %s", _EVENTS_FILE, exc_info=True)


def _save_events():
    """Persist events to disk."""
    _EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _EVENTS_FILE.write_text(json.dumps(list(_events), default=str))


_load_events()


def log_event(category: str, message: str, severity: str = "info", meta: dict | None = None):
    _events.appendleft(
        {
            "ts": time.time(),
            "category": category,
            "message": message,
            "severity": severity,
            "meta": meta or {},
        }
    )
    # Persist every 10 events to avoid excessive I/O
    if len(_events) % 10 == 0:
        _save_events()


# ── Node health monitoring (auto-detect offline nodes) ───────────────────────

_OFFLINE_THRESHOLD_S = NODE_OFFLINE_THRESHOLD_S
_last_health_check = 0.0


def check_node_health():
    """Called periodically from background task to detect offline nodes."""
    global _last_health_check
    now = time.time()
    if now - _last_health_check < NODE_HEALTH_CHECK_INTERVAL_S:
        return
    _last_health_check = now

    with state.connected_nodes_lock:
        snapshot = list(state.connected_nodes.items())
    for node_id, info in snapshot:
        hb = info.get("last_heartbeat")
        if not hb:
            continue
        try:
            hb_time = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - hb_time).total_seconds()
        except Exception:
            continue
        if age_s > _OFFLINE_THRESHOLD_S and info.get("status") != "disconnected":
            with state.connected_nodes_lock:
                info["status"] = "disconnected"
            log_event(
                "node",
                f"Node {node_id} went offline (no heartbeat for {int(age_s)}s)",
                "warning",
                {"node_id": node_id, "age_s": int(age_s)},
            )


# ── Users ─────────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """The accounts opened by emailed links, read-only.

    Nothing here sets is_superuser: an administrator is a Cloudflare Access
    identity with no row, and a flagged row may not sign in by link at all
    (core.users.get_or_create_magic_link_user).
    """
    result = await session.execute(select(User))
    return [user_to_dict(u) for u in result.scalars().all()]


# ── Node ownership (admin override) ──────────────────────────────────────────


class NodeOwnerUpdate(BaseModel):
    user_id: str | None = None


@router.get("/node-owners")
async def admin_list_node_owners(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """Return {node_id: {user_id, email, name}} for every owned node."""
    owners = await list_node_owners()
    # Build users map in one DB query rather than N per-user lookups.
    all_users_result = await session.execute(select(User))
    users_map = {str(u.id): u for u in all_users_result.scalars().all()}
    result = {}
    for nid, uid in owners.items():
        u = users_map.get(uid)
        result[nid] = {
            "user_id": uid,
            "email": u.email if u else None,
            "name": u.name if u else None,
        }
    return result


@router.put("/nodes/{node_id}/owner")
async def admin_set_node_owner(
    node_id: str,
    body: NodeOwnerUpdate,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin=Depends(require_admin),
):
    """Assign or clear node ownership. Pass user_id=null to unassign.

    Only a registered node can have an owner, so an id that never came through
    v1 registration is 404 whichever way the call goes. This route is behind
    require_admin, whose caller can already list every node, so the answer
    tells them nothing new.
    """
    if await session.get(Node, node_id) is None:
        raise HTTPException(404, "Node not found")
    if body.user_id is not None:
        try:
            uid = uuid.UUID(body.user_id)
        except ValueError as e:
            raise HTTPException(404, "User not found") from e
        user = await session.get(User, uid)
        if not user:
            raise HTTPException(404, "User not found")
    # The checks above and the write in one transaction. A changed owner takes
    # the address and any pending challenge with it; set_owner says why.
    await set_owner(session, node_id, body.user_id)
    await session.commit()
    log_event(
        "user",
        f"Node {node_id} owner set to {body.user_id or '(unassigned)'}",
        "info",
        {"node_id": node_id, "user_id": body.user_id, "by": admin["email"]},
    )
    return {"ok": True, "node_id": node_id, "user_id": body.user_id}


# ── Node location privacy ─────────────────────────────────────────────────────
#
# The same override the owner sets from their own dashboard
# (routes/auth.py, /me/nodes/{node_id}/location-privacy), reachable for any node
# id rather than only an owned one.  That is the point of having it here: most
# of what a deployment carries has no owner row and never registered — the
# synthetic fleet, mirrored nodes, anything predating the v1 handshake — so on
# the test droplet this is the only way to make a node private at all.
#
# Admin sees the raw pieces the owner routes do not, because an admin is
# answering "why is this node in this state" rather than choosing their own.


class NodeLocationPrivacyUpdate(BaseModel):
    private: bool


@router.get("/nodes/{node_id}/location-privacy")
async def admin_get_node_location_privacy(node_id: str, _admin=Depends(require_admin)):
    """Effective state, its source, and both rows behind it.

    Answers for an id nothing has ever heard of rather than 404ing on one: the
    override table accepts any string, so "no registration, no override, public
    by default" is the true and useful answer for a node an admin is about to
    hide before it has ever connected.
    """
    return await publication.location_privacy(node_id)


@router.put("/nodes/{node_id}/location-privacy")
async def admin_set_node_location_privacy(
    node_id: str,
    body: NodeLocationPrivacyUpdate,
    admin=Depends(require_admin),
):
    """Set the override on any node, as an admin."""
    await publication.set_location_privacy(node_id, body.private, set_by=f"admin:{admin['email']}")
    publication.invalidate()
    # Logged like the owner-assignment route above: an admin changing what
    # another operator's node publishes is exactly the kind of act the event log
    # exists to make answerable afterwards.  The owner routes are not logged —
    # an owner acting on their own node is not an intervention.
    log_event(
        "user",
        f"Node {node_id} location set {'private' if body.private else 'public'}",
        "info",
        {"node_id": node_id, "private": body.private, "by": admin["email"]},
    )
    return {
        "node_id": node_id,
        "location_private": body.private,
        "location_privacy_source": publication.SOURCE_OVERRIDE,
    }


@router.delete("/nodes/{node_id}/location-privacy")
async def admin_clear_node_location_privacy(node_id: str, admin=Depends(require_admin)):
    """Drop the override, returning the node to its registration choice."""
    await publication.clear_location_privacy(node_id)
    publication.invalidate()
    after = await publication.location_privacy(node_id)
    log_event(
        "user",
        f"Node {node_id} location privacy override cleared",
        "info",
        {"node_id": node_id, "by": admin["email"], "location_private": after["location_private"]},
    )
    return {
        "node_id": node_id,
        "location_private": after["location_private"],
        "location_privacy_source": after["location_privacy_source"],
    }


@router.get("/node-refs")
async def admin_list_node_refs(_admin=Depends(require_admin)):
    """Return {node_ref: node_id} for the whole fleet.

    The one route that serves the mapping publication exists to withhold (D16),
    which is why it is gated on require_admin rather than on a session, and why
    it is the far side of the boundary from the leaderboard below, which any
    caller may read. The admin pages are built on the public,
    ref-keyed feeds, so this is what lets them name a node to an operator, join
    the node_id-keyed admin routes beside it, and link to the node's own site —
    which is named after the node_id, not the ref.

    AUTH_ALLOW_ANONYMOUS_ADMIN makes require_admin admit every caller, which
    publishes this mapping wholesale. No deployed environment sets it; only
    docker-compose.local.yml does, so treat a laptop's console as publishing the
    whole boundary rather than just this route.
    """
    with state.connected_nodes_lock:
        connected = list(state.connected_nodes)
    return ref_to_id_map(connected)


@router.get("/node-contacts")
async def admin_list_node_contacts(
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """Return {node_id: {first_name, last_name, email, phone, country, updated_at}} for every node that reported any.

    The one route that serves these. They are kept off the node and analytics
    payloads the map and dashboard poll broadly, so personal data has a single
    door rather than riding every refresh.
    """
    from services.node_contact_store import list_contacts

    return await list_contacts(session)


@router.delete("/nodes/{node_id}/contact")
async def admin_delete_node_contact(
    node_id: str,
    session: AsyncSession = Depends(get_async_session),
    admin=Depends(require_admin),
):
    """Erase a node's contact details. The path that exists so erasure does not need the database."""
    from services.node_contact_store import delete_contact

    deleted = await delete_contact(session, node_id)
    await session.commit()
    if deleted:
        log_event(
            "user",
            f"Contact details cleared for node {node_id}",
            "info",
            {"node_id": node_id, "by": admin["email"]},
        )
    return {"ok": True, "node_id": node_id, "deleted": deleted}


# ── Node retirement ───────────────────────────────────────────────────────────


@router.get("/nodes/stale")
async def admin_list_stale_nodes(_admin=Depends(require_admin)):
    """Per-node state held for nodes that are no longer in the fleet.

    Nothing removes a node automatically, so decommissioned, renamed, or
    superseded receivers accumulate here and are paid for on every analytics
    pass and snapshot write.  See services.node_retirement.
    """
    from services import node_retirement

    stale = node_retirement.stale_node_ids()
    return {"stale_nodes": stale, "count": len(stale), "live_nodes": len(node_retirement.live_node_ids())}


@router.delete("/nodes/{node_id}/state")
async def admin_retire_node(node_id: str, force: bool = False, admin=Depends(require_admin)):
    """Forget a node: fleet registry, analytics, coverage files, custody
    chain, reputation and the cached pipeline.

    Irreversible — the coverage polygon represents observation time that cannot
    be recreated.  Refuses a currently-connected node with 409 unless
    force=true, since the next registration would undo it anyway.  force=true
    is itself refused with 403 for a node id outside the configured
    NODE_FORCE_RETIRE_PREFIXES allowlist, when one is set.
    """
    from services import node_retirement

    try:
        report = node_retirement.retire_node(node_id, force=force)
    except node_retirement.NodeStillConnected as exc:
        raise HTTPException(409, f"Node {node_id} is connected; pass force=true to retire it anyway") from exc
    except node_retirement.ForceRetireNotAllowed as exc:
        raise HTTPException(
            403,
            f"force=true is restricted to node ids starting with {', '.join(exc.prefixes)}",
        ) from exc
    log_event(
        "node",
        f"Retired node state for {node_id}",
        "warning",
        {"node_id": node_id, "by": admin["email"], "report": report},
    )
    return report


@router.post("/nodes/retire-stale")
async def admin_retire_stale_nodes(admin=Depends(require_admin)):
    """Retire every node held in state but absent from the fleet."""
    from services import node_retirement

    result = node_retirement.retire_stale_nodes()
    # Skipped and failed nodes go in the event too. Recording only the retired
    # ids would show a clean sweep in the log while the accumulation the
    # operator ran it to clear is still there, with nothing saying why.
    log_event(
        "node",
        f"Retired {result['count']} stale node(s), skipped {len(result['skipped'])}, failed {len(result['failed'])}",
        "error" if result["failed"] else "warning",
        {
            "by": admin["email"],
            "nodes": [r["node_id"] for r in result["retired"]],
            "skipped": [r["node_id"] for r in result["skipped"]],
            "failed": [r["node_id"] for r in result["failed"]],
        },
    )
    if result["failed"]:
        # The sweep keeps going past a failure, so the status code is the only
        # thing telling a caller driving this by exit status that anything went
        # wrong. 500 when nothing was retired at all, since that is a sweep
        # that did not work; 207 when some went and some did not, so a partial
        # result reads as neither success nor total failure. The body is the
        # full result either way, so no record is lost.
        return JSONResponse(status_code=500 if not result["retired"] else 207, content=result)
    return result


# ── Events ────────────────────────────────────────────────────────────────────


@router.get("/events")
async def list_events(limit: int = 200, _admin=Depends(require_admin)):
    return list(_events)[:limit]


# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "data" / "config_history"


@router.get("/config/nodes")
async def get_node_config(_admin=Depends(require_admin)):
    global _nodes_config_cache
    fp = runtime_path("nodes_config.json")
    if fp.exists():
        return Response(content=fp.read_bytes(), media_type="application/json")
    # Live fallback with TTL cache — iterating 1000 nodes is O(n)
    now = time.time()
    if _nodes_config_cache is not None and now - _nodes_config_cache[0] < _CONFIG_LIVE_CACHE_TTL:
        return Response(content=_nodes_config_cache[1], media_type="application/json")
    nodes_cfg = {}
    with state.connected_nodes_lock:
        _nodes_items = list(state.connected_nodes.items())
    for nid, info in _nodes_items:
        cfg = info.get("config", {})
        nodes_cfg[nid] = {
            "name": cfg.get("name", nid),
            "frequency": cfg.get("FC", cfg.get("frequency")),
            "rx_lat": cfg.get("rx_lat"),
            "rx_lon": cfg.get("rx_lon"),
            "tx_lat": cfg.get("tx_lat"),
            "tx_lon": cfg.get("tx_lon"),
            "status": info.get("status"),
        }
    result_bytes = orjson.dumps({"_source": "live", "nodes": nodes_cfg, "total": len(nodes_cfg)})
    _nodes_config_cache = (now, result_bytes)
    return Response(content=result_bytes, media_type="application/json")


@router.get("/config/towers")
async def get_tower_config(_admin=Depends(require_admin)):
    """A live view of the transmitters the connected nodes are illuminated by.

    One response shape now: ``{"_source": "live", "towers": ...}``, derived from
    what nodes report over TCP. The overlay-file half of this endpoint went with
    the monolith's tower stack — ranking config lives in tower-finder-service,
    which owns its own copy and serves it at ``/api/config`` on every vhost. What
    is left is monolith-owned data with no equivalent there, so it stays.

    ``_source`` is still sent, and callers should still branch on it (the
    dashboard's ConfigPage does): dropping the key would break them for no gain,
    and the file shape may yet come back from the service side.
    """
    global _towers_config_cache
    # Live view with TTL cache
    now = time.time()
    if _towers_config_cache is not None and now - _towers_config_cache[0] < _CONFIG_LIVE_CACHE_TTL:
        return Response(content=_towers_config_cache[1], media_type="application/json")
    towers = {}
    with state.connected_nodes_lock:
        _tower_items = list(state.connected_nodes.items())
    for nid, info in _tower_items:
        cfg = info.get("config", {})
        tx_lat = cfg.get("tx_lat")
        tx_lon = cfg.get("tx_lon")
        # A transmitter on the equator or the prime meridian is a real tower.
        if tx_lat is not None and tx_lon is not None:
            key = f"{tx_lat:.4f},{tx_lon:.4f}"
            if key not in towers:
                towers[key] = {
                    "lat": tx_lat,
                    "lon": tx_lon,
                    "frequency": cfg.get("FC", cfg.get("frequency")),
                    "nodes_using": [],
                }
            towers[key]["nodes_using"].append(nid)
    result_bytes = orjson.dumps({"_source": "live", "towers": towers, "total": len(towers)})
    _towers_config_cache = (now, result_bytes)
    return Response(content=result_bytes, media_type="application/json")


class ConfigUpdate(BaseModel):
    config: dict


@router.put("/config/nodes")
async def update_node_config(body: ConfigUpdate, _admin=Depends(require_admin)):
    global _nodes_config_cache
    _nodes_config_cache = None  # invalidate live cache
    fp = runtime_path("nodes_config.json")
    fp.parent.mkdir(parents=True, exist_ok=True)
    # Save version history
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    if fp.exists():
        history_fp = _CONFIG_DIR / f"nodes_{ts}.json"
        write_runtime_file(history_fp, fp.read_text(encoding="utf-8"))
    # Atomic because this is a runtime overlay in a persistent volume: a
    # truncated write outlives the request that made it, and the next boot
    # reads whatever is on disk.
    write_runtime_file(fp, json.dumps(body.config, indent=2))
    log_event("config", "Node config updated", "info")
    return {"status": "ok", "saved_at": ts}


@router.get("/config/history")
async def config_history(_admin=Depends(require_admin)):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(_CONFIG_DIR.glob("*.json"), reverse=True)
    result = []
    for f in files[:50]:
        name = f.stem  # e.g. "nodes_1711234567"
        parts = name.rsplit("_", 1)
        config_type = parts[0] if len(parts) > 1 else name
        ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        result.append({"filename": f.name, "type": config_type, "timestamp": ts, "size": f.stat().st_size})
    return result


# ── Storage stats ─────────────────────────────────────────────────────────────
# Results are pre-computed by storage_refresh_task (services/tasks/storage_refresh.py)
# and stored in state.latest_storage_bytes. The endpoint just returns those bytes.

# TTL cache for live-generated node/tower config (active when JSON files absent)
_nodes_config_cache: tuple | None = None
_towers_config_cache: tuple | None = None
_CONFIG_LIVE_CACHE_TTL = CONFIG_LIVE_CACHE_TTL_S


@router.get("/storage")
async def storage_stats(_admin=Depends(require_admin)):
    if state.latest_storage_bytes == b"{}":
        # Background task hasn't completed its first scan yet (startup in progress).
        # Return 202 so the frontend knows to retry rather than treating it as an error.
        return Response(content=b'{"status":"initializing"}', status_code=202, media_type="application/json")
    return Response(content=state.latest_storage_bytes, media_type="application/json")


# ── Leaderboard ──────────────────────────────────────────────────────────────


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
    uptime_s: float
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


# Shared by every leaderboard request during the cold-start gap (see
# leaderboard() below): a burst of concurrent anonymous callers submits one
# work item to the two-worker _admin_executor rather than one each.
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
        _cold_start_summaries_task = loop.run_in_executor(_admin_executor, state.node_analytics.get_all_summaries)
        _cold_start_summaries_task.add_done_callback(_consume_result)
    # Shielded: an unshielded await lets one caller's cancellation (a client
    # disconnect) cancel this shared Future, handing CancelledError to every
    # other caller sharing it.
    return await asyncio.shield(_cold_start_summaries_task)


@router.get("/leaderboard")
async def leaderboard(caller=Depends(get_optional_user)):
    """Rankings by detections, uptime and trust, open to anyone.

    The odd one out under this prefix, which is otherwise the admin API. A
    caller with no session gets PublicLeaderboardRow, which is what lets the
    dashboard's /leaderboard render to a visitor; one with a session gets
    SignedInLeaderboardRow.

    No response_model: one would filter every caller's rows to the same
    shape. The row class is chosen per caller below and every field is passed
    by name, so what leaves is exactly what that class declares.
    """
    import orjson

    # Use the pre-computed analytics snapshot (refreshed every 30 s by the
    # background task) to avoid holding the analytics lock in this handler.
    raw = state.latest_analytics_bytes
    summaries: dict = {}
    if raw and raw != b"{}":
        try:
            summaries = orjson.loads(raw).get("nodes", {})
        except Exception:
            logger.debug("analytics snapshot bytes unparseable", exc_info=True)
    if summaries:
        # The snapshot is built for publication, so it is already keyed on
        # node_ref, which is what this route reports.  The node_id rides
        # alongside because everything else read below is node_id-keyed; a
        # synthetic node publishes as itself and has no reverse row, hence the
        # fallback.
        rows = [(ref, id_for_ref(ref) or ref, s) for ref, s in summaries.items()]
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
        rows = [(ref, nid, s) for nid, s in published.items() if (ref := public_identity(nid))]

    with state.connected_nodes_lock:
        connected = dict(state.connected_nodes)
    rows.sort(key=lambda row: row[2].get("metrics", {}).get("total_detections", 0), reverse=True)
    entries: list[PublicLeaderboardRow] = []
    for rank, (node_ref, node_id, s) in enumerate(rows, start=1):
        m = s.get("metrics", {})
        node = connected.get(node_id, {})
        row = PublicLeaderboardRow(
            node_ref=node_ref,
            name=public_name(node.get("config", {}).get("name"), node_ref, connected.keys()),
            rank=rank,
            detections=m.get("total_detections", 0),
            frames=m.get("total_frames", 0),
            tracks=m.get("total_tracks", 0),
            uptime_s=m.get("uptime_s", 0),
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


# ── User alerts (public, non-admin) ─────────────────────────────────────────


@router.get("/alerts")
async def user_alerts(_user=Depends(get_current_user)):
    """Return recent events visible to logged-in users."""
    visible = [
        e
        for e in _events
        if e.get("severity") in ("warning", "error", "critical") or e.get("category") in ("node", "config", "system")
    ]
    return visible[:100]


@router.get("/metrics")
async def system_metrics(_user=Depends(require_admin)):
    """Operational metrics: task health, error counts, queue depths."""
    import resource
    import shutil

    rusage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in KB on Linux, bytes on macOS — normalise to MB
    import sys

    rss_mb = rusage.ru_maxrss / 1024 if sys.platform == "linux" else rusage.ru_maxrss / (1024 * 1024)

    disk = shutil.disk_usage(state.COVERAGE_STORAGE_DIR)

    return {
        "task_last_success": dict(state.task_last_success),
        # Snapshot under the same lock bump_task_error takes: a bare dict()
        # over a dict a worker thread is inserting into can raise
        # "dictionary changed size during iteration" on this request path.
        "task_error_counts": state.task_error_snapshot(),
        "frame_queue_depth": state.frame_queue.qsize(),
        "frame_queue_max": state.frame_queue.maxsize,
        "frames_dropped": state.frames_dropped,
        "frames_processed": state.frames_processed,
        "solver_successes": state.solver_successes,
        "solver_failures": state.solver_failures,
        # Which gate is eating the solves — a per-reason split of the
        # aggregate above, not a sibling of it.
        "solver_failures_by_reason": {
            "exception": state.solver_fail_exception,
            "unconverged": state.solver_fail_unconverged,
            "rms_delay": state.solver_fail_rms_delay,
            "rms_doppler": state.solver_fail_rms_doppler,
            "beam": state.solver_fail_beam,
            "displacement": state.solver_fail_displacement,
            # Dark-lane subset of "displacement" above, not a sibling: a dark
            # reject increments both, so the aggregate stays comparable while
            # this line shows how much of it is dark.
            "displacement_dark": state.solver_fail_displacement_dark,
        },
        "solver_pool_timeouts": state.solver_pool_timeouts,
        "solver_queue_depth": state.solver_queue.qsize(),
        "solver_queue_drops": state.solver_queue_drops,
        "solver_stale_drops": state.solver_stale_drops,
        "solver_resolve_skips": state.solver_resolve_skips,
        "tracks_stale_skipped": state.tracks_stale_skipped,
        "solver_epoch_align_skipped": state.solver_epoch_align_skipped,
        "mn_superseded": state.mn_superseded,
        "solver_trimmed": state.solver_trimmed,
        # Overlap grids rebuilt because a node's observed coverage tightened,
        # and how many nodes triggered it.  Zero against populated polygons
        # means the prior is not reaching the grids.
        "coverage_rebuilds": state.coverage_rebuilds,
        "coverage_rebuild_nodes": state.coverage_rebuild_nodes,
        # Nodes whose digest has moved but whose grids are still queued behind
        # the per-cycle rebuild budget.  A depth that never returns to zero
        # means the budget is below the fleet's trigger rate.
        "coverage_rebuild_backlog": state.coverage_rebuild_backlog,
        # How long the front of that queue has waited, as of the last cycle.
        # Read it with the backlog, not instead of it: a steady depth whose
        # front turns over is the budget working, a front that waits longer
        # every cycle is a budget too small.  Same judgement /api/health makes
        # (services/health.py), exposed so a soak can watch it directly.
        "coverage_rebuild_oldest_wait_s": round(state.coverage_rebuild_oldest_wait_s, 1),
        # Teleporting emits (mis-association noise).  Debug counter only —
        # jumps no longer mark tracks anomalous.
        "position_jump_events": state.position_jump_events,
        "solver_last_latency_s": round(state.solver_last_latency_s, 3),
        "solver_avg_latency_s": round(state.solver_total_latency_s / max(state.solver_total_solved, 1), 2),
        "solver_queue_pct": round(state.solver_queue.qsize() / max(state.solver_queue.maxsize, 1) * 100, 1),
        "connected_nodes": len([n for n in list(state.connected_nodes.values()) if n.get("status") == "active"]),
        "peak_connected_nodes": state.peak_connected_nodes,
        "active_geo_aircraft": len(state.active_geo_aircraft),
        "multinode_tracks": len(state.multinode_tracks),
        "adsb_aircraft": len(state.adsb_aircraft),
        # Store sizes that used to grow without bound — exposed so a soak can
        # watch them plateau instead of trusting the fix.
        "track_arc_motion": len(state.track_arc_motion),
        "track_last_emit": len(state.track_last_emit),
        "track_gate_hold": len(state.track_gate_hold),
        "mn_pos_history": multinode_identity.mn_pos_history_size(),
        "track_histories": len(state.track_histories),
        "ground_truth_trails": len(state.ground_truth_trails),
        "ws_clients": len(state.ws_clients),
        "ws_send_timeouts": state.ws_send_timeouts,
        "ws_live_clients": len(state.ws_live_clients),
        "stale_tasks": get_stale_tasks(),
        "process_rss_mb": round(rss_mb, 1),
        "load_avg": list(os.getloadavg()),
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
    }


@router.post("/coverage/dump")
async def coverage_dump(_user=Depends(require_admin)):
    """Flush runtime coverage data to disk and generate an HTML report.

    Only meaningful when the server was started with ``COVERAGE_ENABLED=1``.
    Collection continues after the dump — no restart required.
    """
    import services.runtime_coverage as _rc

    html_dir = await asyncio.get_running_loop().run_in_executor(_admin_executor, _rc.save)
    if html_dir is None:
        return {"status": "disabled", "detail": "COVERAGE_ENABLED is not set"}
    return {"status": "ok", "html_report": html_dir}
