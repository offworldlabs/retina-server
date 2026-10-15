"""Admin-only API routes — user management, events, config."""

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
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import (
    CONFIG_LIVE_CACHE_TTL_S,
    EVENT_LOG_MAX,
    NODE_HEALTH_CHECK_INTERVAL_S,
    NODE_OFFLINE_THRESHOLD_S,
)
from core import state
from core.auth import create_magic_link_in, list_node_owners, sweep_magic_links
from core.node_ids import POLLED_BLAH2, system_of
from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.runtime_config import runtime_path, write_runtime_file
from core.task_registry import get_stale_tasks
from core.users import (
    User,
    accounts_by_id,
    async_session_maker,
    get_async_session,
    normalise_email,
    require_admin,
    user_to_dict,
)
from services import account_email
from services.node_claim_store import claim_status, read_owner, set_owner
from services.node_config import carrier_hz
from services.node_refs import owner_identity, ref_to_id_map
from services.node_report_store import all_reports
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


def _admin_user(user: User, node_count: int) -> dict:
    """An account as the console's user pages show it: user_to_dict, whether
    the address was ever proved, and how many nodes the account owns."""
    return {**user_to_dict(user), "is_verified": user.is_verified, "node_count": node_count}


async def _account(session: AsyncSession, user_id: str) -> User:
    """The account `user_id` names. 404 for any id that is not an account's,
    malformed or not, as admin_set_node_owner answers one."""
    try:
        account_id = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(404, "User not found") from None
    account = await session.get(User, account_id)
    if account is None:
        raise HTTPException(404, "User not found")
    return account


# Far above any real table and inside every database's integer range, so an
# offset past it is a 422 rather than a driver overflow.
MAX_USER_OFFSET = 1_000_000_000


@router.get("/users")
async def list_users(
    request: Request,
    q: str = "",
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_USER_OFFSET),
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """A page of accounts, newest first, and how many match in all.

    Every account, whether a redeemed link or an administrator connecting a
    radar to the address made it. `q` keeps the addresses that contain it, in
    any case, with `%` and `_` matched as themselves. created_at is filled in
    by the database to the second, so ties go by id and a page stays put.

    Nothing here sets is_superuser, and whatever a row holds there grants and
    refuses nothing: an administrator is a Cloudflare Access identity with no
    row (core.users.require_admin). An administrator can sign an account out
    and back in (deactivate, reactivate), send it a sign-in link, start a
    change of its address or delete it.
    """
    term = q.strip()
    matching = [User.email.icontains(term, autoescape=True)] if term else []
    total = await session.scalar(select(func.count()).select_from(User).where(*matching))
    page = (
        await session.scalars(
            select(User).where(*matching).order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
    ).all()
    # One grouped query for the page rather than one per row.
    counts: dict[str | None, int] = {}
    if page:
        rows = await session.execute(
            select(NodeClaim.user_id, func.count())
            .where(NodeClaim.user_id.in_([str(user.id) for user in page]))
            .group_by(NodeClaim.user_id)
        )
        counts = {user_id: count for user_id, count in rows.tuples()}
    return {"users": [_admin_user(user, counts.get(str(user.id), 0)) for user in page], "total": total or 0}


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """One account, the nodes it owns, and the claims on other nodes that
    carry its address: in progress, expired or bounced, so a claim that is
    stuck can be read from the account's side."""
    account = await _account(session, user_id)
    email = normalise_email(account.email)
    owned = set(await session.scalars(select(NodeClaim.node_id).where(NodeClaim.user_id == str(account.id))))
    named = set(await session.scalars(select(NodeClaim.node_id).where(NodeClaim.email == email)))
    named |= set(await session.scalars(select(NodeClaimChallenge.node_id).where(NodeClaimChallenge.email == email)))
    now = time.time()
    return {
        "user": _admin_user(account, len(owned)),
        "nodes": await _admin_claims(session, sorted(owned), now),
        "claims": await _admin_claims(session, sorted(named - owned), now),
    }


@router.post("/users/{user_id}/sign-in-link", status_code=202)
async def send_sign_in_link(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    admin=Depends(require_admin),
):
    """Mail the account a sign-in link, as its owner asking for one would.

    The same link, mail and outbox as the self-service route, but answered
    once the mail is queued: the caller is an administrator, so there is no
    address oracle to hide and they are told what happened. Mail that cannot
    leave is refused before a link is minted, as that route refuses it.

    The link goes to the address the account has as it is minted, read again
    under the write lock it is minted under, so a retired address is never
    mailed, as the self-service route forwards one (routes/auth.py,
    _mail_sign_in_link). A change of address confirmed meanwhile has either
    committed before that read, and the link goes to the new address, or waits
    for the link's commit and kills it with the rest of the old mailbox's links
    (services/account_email.py).
    """
    from routes.auth import queue_sign_in_mail, sign_in_landing

    account = await _account(session, user_id)
    if not account.is_active:
        raise HTTPException(409, "Account is deactivated")
    landing = sign_in_landing()
    now = time.time()
    async with async_session_maker() as db:
        async with db.begin():
            # The write lock, before the account is read again.
            await sweep_magic_links(db, account.email, now)
            current = await db.get(User, account.id)
            if current is None:
                raise HTTPException(404, "User not found")
            if not current.is_active:
                raise HTTPException(409, "Account is deactivated")
            email = normalise_email(current.email)
            token = await create_magic_link_in(db, email, now=now)
    if token is None:
        raise HTTPException(429, "This address already holds the maximum number of outstanding sign-in links")
    if not await queue_sign_in_mail(email, token, landing + token, now):
        raise HTTPException(500, "Could not queue the sign-in mail")
    log_event(
        "user",
        f"Sign-in link queued for account {account.id}",
        "info",
        {"user_id": str(account.id), "by": admin["email"]},
    )
    return {"status": "queued", "email": email}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin=Depends(require_admin)):
    """Delete an account as its owner deletes their own (services/accounts.py,
    delete_account): every node it owns handed back as its owner's release
    would, every session ended and its row gone. The core records it, naming
    the administrator. The answer says how many nodes this call handed back.

    404 for an id that is not an account's, malformed or not. 409 when claims
    kept binding nodes to it while it was being deleted: it is still there,
    still signs in and holds only those, and deleting it again finishes the job.
    """
    from core import sessions
    from services import accounts

    try:
        released = await accounts.delete_account(user_id, by=admin["email"])
    except sessions.AccountHoldsNodes:
        raise HTTPException(409, "A node was claimed for this account while it was being deleted. Try again.") from None
    if released is None:
        raise HTTPException(404, "User not found")
    return {"ok": True, "released": released}


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(user_id: str, admin=Depends(require_admin)):
    """Shut an account out: no link signs it in again, and every cookie it holds stops working.

    The flag, the deletion of its sessions and the retirement of its JWT
    cookies from before sessions are one transaction
    (core.sessions.deactivate_account), so no request that starts after this
    answers is still signed in as it. The nodes it owns stay its own.

    404 for an id that is not an account's, malformed or not, as
    admin_set_node_owner answers one.
    """
    from core import sessions

    account = await sessions.deactivate_account(user_id)
    if account is None:
        raise HTTPException(404, "User not found")
    log_event(
        "user",
        f"Account {account.id} deactivated",
        "warning",
        {"user_id": str(account.id), "by": admin["email"]},
    )
    return user_to_dict(account)


@router.post("/users/{user_id}/reactivate")
async def reactivate_user(user_id: str, admin=Depends(require_admin)):
    """Let a deactivated account sign in again, by a new link.

    Nothing it held comes back. The sessions deactivation ended stay ended and
    its JWT cookies stay retired, so the person signs in afresh.
    """
    from core import sessions

    account = await sessions.reactivate_account(user_id)
    if account is None:
        raise HTTPException(404, "User not found")
    log_event(
        "user",
        f"Account {account.id} reactivated",
        "info",
        {"user_id": str(account.id), "by": admin["email"]},
    )
    return user_to_dict(account)


class AddressChange(BaseModel):
    email: str


@router.post("/users/{user_id}/email", status_code=202)
async def change_user_address(user_id: str, body: AddressChange, admin=Depends(require_admin)):
    """Start a change of address for an account whose owner lost theirs.

    The same change the account can ask for itself (services/account_email.py),
    with the same rules and limits: a link goes to the new address, and the
    address changes when that link is opened, so the account only ever moves
    to an address somebody has shown they read.

    404 for an id that is not an account's, malformed or not, as
    deactivate_user answers one. 409 with the reason for a deactivated
    account, whose address does not change until it is reactivated.
    """
    try:
        requested = await account_email.request_change(user_id, body.email)
    except account_email.Refused as refused:
        headers = {"Retry-After": str(refused.retry_after_s)} if refused.retry_after_s is not None else None
        raise HTTPException(refused.status_code, refused.detail, headers=headers) from None
    log_event(
        "user",
        f"Address change for account {requested.user_id} sent to the new address",
        "info",
        {"user_id": requested.user_id, "by": admin["email"]},
    )
    return {"status": "sent", "email": requested.email}


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


async def _admin_claims(session: AsyncSession, node_ids: list[str], now: float) -> list[dict]:
    """Each node's claim as an administrator reads it, in the order given.

    `status` is what the node itself is told (node_claim_store.claim_status);
    the rest is what it is derived from, the challenge included whether or not
    it has expired, and who the owner is.
    """
    if not node_ids:
        return []
    claims = {c.node_id: c for c in await session.scalars(select(NodeClaim).where(NodeClaim.node_id.in_(node_ids)))}
    challenges = {
        c.node_id: c
        for c in await session.scalars(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id.in_(node_ids)))
    }
    owners = await accounts_by_id(session, [c.user_id for c in claims.values() if c.user_id is not None])
    result = []
    for node_id in node_ids:
        claim, challenge = claims.get(node_id), challenges.get(node_id)
        owner = None
        if claim is not None and claim.user_id is not None:
            account = owners.get(claim.user_id)
            owner = {
                "user_id": claim.user_id,
                "email": account.email if account else None,
                "name": account.name if account else None,
            }
        result.append(
            {
                "node_id": node_id,
                "node_ref": owner_identity(node_id),
                "status": (await claim_status(session, node_id, now)).state,
                "owner": owner,
                "email": claim.email if claim else None,
                "verified": bool(claim and claim.verified),
                "undeliverable": bool(claim and claim.undeliverable),
                "updated_at": claim.updated_at.timestamp() if claim and claim.updated_at else None,
                "owned_since": claim.owned_since.timestamp() if claim and claim.owned_since else None,
                "challenge": None
                if challenge is None
                else {
                    "email": challenge.email,
                    "created_at": challenge.created_at,
                    "expires_at": challenge.expires_at,
                    "expired": challenge.expires_at <= now,
                },
            }
        )
    return result


@router.get("/nodes/{node_id}/claim")
async def admin_node_claim(
    node_id: str,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """A node's owner, the address it was claimed with and its pending
    challenge, which is what a stuck or bounced claim is diagnosed from.
    404 for a node that never came through registration."""
    if await session.get(Node, node_id) is None:
        raise HTTPException(404, "Node not found")
    [claim] = await _admin_claims(session, [node_id], time.time())
    return claim


@router.get("/node-reports")
async def admin_node_reports(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _admin=Depends(require_admin),
):
    """Each v1 node's own account of itself, from its last heartbeat.

    Administrators only: it is keyed by node_id, and `errors` carries
    node-internal detail. Untrusted, and read by nothing that judges whether a
    node is working.
    """
    return await all_reports(session)


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
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Node not found")
    if body.user_id is None and system_of(node_id) == POLLED_BLAH2 and node.status == "active":
        # Nothing can claim a polled radar afterwards, and the poller would
        # carry on polling it with nobody to answer for it.
        raise HTTPException(409, f"{node_id} is a polled radar: reassign it, or have its owner remove it")
    user_id = None
    if body.user_id is not None:
        try:
            uid = uuid.UUID(body.user_id)
        except ValueError as e:
            raise HTTPException(404, "User not found") from e
        user = await session.get(User, uid)
        if not user:
            raise HTTPException(404, "User not found")
        # The account's own id, not the spelling sent: node_claims is matched
        # against it exactly, so an upper-case or undashed id would bind an
        # owner that no read by account ever finds.
        user_id = str(user.id)
    # The checks above and the write in one transaction. A changed owner takes
    # the address and any pending challenge with it; set_owner says why.
    previous = await read_owner(session, node_id)
    await set_owner(session, node_id, user_id)
    await session.commit()
    log_event(
        "user",
        f"Node {node_id} owner set to {user_id or '(unassigned)'}",
        "info",
        {"node_id": node_id, "user_id": user_id, "previous_user_id": previous, "by": admin["email"]},
    )
    return {"ok": True, "node_id": node_id, "user_id": user_id}


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
async def list_events(limit: int = 200, user_id: str | None = None, _admin=Depends(require_admin)):
    """The event log, newest first. With `user_id`, only the events about that
    account: naming it as the account acted on, or as the owner a node had
    before an assignment. Filtered before `limit` is applied."""
    events = list(_events)
    if user_id is not None:
        # Any spelling of an account's id, as the user routes take; recorded ids are canonical.
        try:
            user_id = str(uuid.UUID(user_id))
        except ValueError:
            pass
        events = [e for e in events if _names_account(e, user_id)]
    return events[:limit]


def _names_account(event: dict, user_id: str) -> bool:
    meta = event.get("meta") or {}
    return user_id in (meta.get("user_id"), meta.get("previous_user_id"))


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
            "frequency": carrier_hz(cfg),
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
                    "frequency": carrier_hz(cfg),
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


def _reset_for_tests() -> None:
    """Reset the leaderboard's in-flight state, which moved to routes/radar.py
    with GET /api/radar/leaderboard; tests/conftest.py still resets this module
    by name."""
    from routes import radar

    radar._reset_for_tests()


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
