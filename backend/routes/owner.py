"""The signed-in owner's own nodes: what they see of each, and what they may change.

Every route here is scoped to the caller's nodes and answers under the node_id,
which is theirs to see. A route about one node takes `Owned`, so a node somebody
else owns answers exactly as one that does not exist, and the check cannot be
left out of a route added later. services/owner_nodes.py builds what is shown,
services/owner_views.py and services/snr_series.py what is shown beyond it.

Two answers name nodes by node_ref instead: the analytics map, keyed as the
public one is, and the archive, whose keys are the public archive's
(services/archive_access.py).

The claims addressed to the caller are about nodes that are not theirs yet, so
they name those by node_ref too, and say nothing else about them.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from core import state
from core.auth import get_user_nodes, get_user_tenures
from core.users import User, UserSession, get_async_session, get_current_user
from routes.auth import _claim_mail_answer, _signed_in_session
from services import accounts, claim_resend, owner_nodes, owner_views
from services.archive_access import list_files, owner_scope, read_file
from services.snr_series import MAX_HOURS, snr_series_async
from services.tasks.aircraft_flush import owner_bytes

router = APIRouter(prefix="/api/auth/me", tags=["auth"])

#: The caller, for a route about all of their nodes. A dependency, as `Owned`
#: is, so a signed-out caller is 401 before any query value is looked at.
SignedIn = Annotated[dict, Depends(get_current_user)]

#: The account the auth_token cookie signs in, and the session asking, through
#: the check the session routes use (routes/auth.py). For the claims addressed
#: to an account, which an Access identity or the anonymous admin has no
#: address for.
CookieAccount = Annotated[tuple[User, UserSession], Depends(_signed_in_session)]


@dataclass(frozen=True)
class OwnedNode:
    """A node the caller owns, the caller, and when the node became theirs.

    `since` bounds every record of the node they are shown: what it recorded
    before then was its last owner's (core/auth.py, get_user_tenures).
    """

    node_id: str
    user: dict
    since: datetime


async def owned_node(request: Request, node_id: str) -> OwnedNode:
    """The caller and `node_id`, having established they own it.  404 if they do not.

    404 rather than 403, deliberately, and the same wording as a node that does
    not exist: the two answers differ only in confirming the id is real, node
    ids are guessable, and an id that resolves is already a hint about where a
    receiver is.  The per-node analytics route makes the same trade for the same
    reason.
    """
    user = await get_current_user(request)
    since = (await get_user_tenures(user["id"])).get(node_id)
    if since is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return OwnedNode(node_id=node_id, user=user, since=since)


#: The `{node_id}` of a route about one node, once the caller is shown to own it.
Owned = Annotated[OwnedNode, Depends(owned_node)]


async def _view(owned: OwnedNode) -> dict:
    """The node as the listing shows it, or 404 for one that changed hands since
    the check, as a release that loses that race answers."""
    mine = await get_user_nodes(owned.user["id"])
    if owned.node_id not in mine:
        raise HTTPException(status_code=404, detail="Node not found")
    [view] = await owner_nodes.views([owned.node_id], mine)
    return view


@router.get("/nodes")
async def my_nodes(request: Request):
    """The caller's own nodes, in full: private ones, true geometry and RF
    configuration included. services/owner_nodes.views says what each carries."""
    user = await get_current_user(request)
    mine = await get_user_nodes(user["id"])
    return await owner_nodes.views(mine, mine)


@router.get("/nodes/{node_id}")
async def my_node(owned: Owned):
    """One of the caller's nodes, as the listing has it."""
    return await _view(owned)


class NodeEdit(BaseModel):
    """What an owner may change about their node. A key left out is left alone."""

    # Strict, so neither "true" nor 1 is taken for a choice. Unknown keys are
    # refused rather than ignored: a misspelt one would otherwise answer 200
    # having changed nothing.
    model_config = ConfigDict(extra="forbid", strict=True)

    #: The owner's own name for the node. Null or blank clears it.
    name: str | None = None
    #: Whether the node is withheld from every public surface. The default is
    #: never applied: a key left out is not in model_fields_set, and is left alone.
    location_private: bool = False


@router.patch("/nodes/{node_id}")
async def edit_my_node(owned: Owned, body: NodeEdit):
    """Name the caller's node for themselves, or choose whether it is published,
    and answer with the node as it now is.

    The name is a label only its owner sees (core/nodes.py, NodeLabel); the
    name a node supplies, which is published, is left as it is. A refused name
    is 422 with the reason as the detail, for the console to show as it is.

    A node made private leaves every public surface that asks per request at
    once, the publication cache being dropped with the write, and the cached
    public payloads at their next rebuild.
    """
    given = body.model_fields_set
    if not given:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    label = None
    if "name" in given:
        try:
            label = await owner_nodes.label_from(body.name)
        except owner_nodes.LabelRefused as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    await owner_nodes.edit(
        owned.node_id,
        owned.user["id"],
        rename="name" in given,
        label=label,
        private=body.location_private if "location_private" in given else None,
    )
    return await _view(owned)


@router.delete("/nodes/{node_id}/claim")
async def release_my_node(owned: Owned):
    """Hand a node back, so its next owner can claim it, or remove a polled radar.

    Guarded the same way every owner-scoped route here is, so a node somebody
    else owns answers exactly as one that does not exist. A polled radar is
    removed rather than handed back, since nothing can claim one afterwards.
    Deleting an account hands its nodes back the same way
    (services/accounts.py, release_owned_node).
    """
    if not await accounts.release_owned_node(owned.node_id, owned.user["id"]):
        # Owned a moment ago and not now: a release that raced another release,
        # or an administrator reassigning it. Either way it is gone, which is
        # what was asked for.
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


@router.get("/aircraft")
async def my_aircraft(request: Request):
    """The owner feed's current frame: aircraft and arcs from the caller's nodes.

    The snapshot /ws/aircraft/owner sends on connect, for a page that polls.
    Filtered from the unredacted frame, because the public feed drops a private
    node's single-node tracks, and a polled radar on probation counts as
    private, so its owner would find nothing of their own there.
    """
    user = await get_current_user(request)
    owned = set(await get_user_nodes(user["id"]))
    return Response(content=owner_bytes(state.latest_aircraft_json, owned), media_type="application/json")


# ── Claims waiting on the caller ─────────────────────────────────────────────


@router.get("/claims")
async def my_claims(account: CookieAccount, session: AsyncSession = Depends(get_async_session)):
    """The open claims addressed to the caller, at the address they have or one
    they retired: nodes that named them, are not theirs yet, and were not
    declined from that address.

    Each by node_ref, with the address it was given, and whether a link is out
    and until when. Nothing else about the node: what it is and where it is
    become the caller's to see once a link binds it.
    """
    user, _ = account
    return [
        {
            "node_ref": claim.node_ref,
            "email": claim.email,
            "pending": claim.expires_at is not None,
            "expires_at": claim.expires_at,
        }
        for claim in await claim_resend.open_claims_for(session, user, time.time())
    ]


@router.post("/claims/{node_ref}/resend", status_code=202)
async def resend_my_claim(
    node_ref: str, account: CookieAccount, response: Response, session: AsyncSession = Depends(get_async_session)
):
    """Send one of those claims' mail again, under the node resend's rules and
    against its allowance (services/claim_resend.py).

    A claim that is not open, or not addressed to the caller, is one 404, so
    another account's claim reads exactly as a node that does not exist.
    """
    user, _ = account
    outcome = await claim_resend.resend_for_account(session, user, node_ref, time.time())
    return _claim_mail_answer(response, outcome, not_found="Claim not found", owned="That node already has an owner")


# ── What the owner is shown beyond the node itself ───────────────────────────
#
# The ownership check is a dependency, and FastAPI resolves dependencies before
# it validates a route's own query values, so a node somebody else owns is the
# same 404 as an unknown one whatever is asked about it: a refused `limit`
# cannot tell the two apart either.
#
# The report, the history, the events, the SNR trend and the archive are each
# bounded by when the node became the caller's: a node that changes hands
# keeps what it recorded for its last owner, and it is shown to nobody else.


@router.get("/analytics")
async def my_analytics(user: SignedIn):
    """The analytics of every one of the caller's nodes that has any, keyed by
    node_ref, private ones included and nothing moved.

    On a worker thread, as is the per-node one below: on a cold cache the
    manager builds every node's summary under its lock, which the refresh task
    may be holding.
    """
    mine = await get_user_nodes(user["id"])
    return {"nodes": await asyncio.to_thread(owner_views.owner_summaries, mine)}


@router.get("/nodes/{node_id}/analytics")
async def my_node_analytics(owned: Owned):
    """The node's analytics with nothing moved, or null before it has any."""
    return await asyncio.to_thread(owner_views.owner_summary, owned.node_id)


@router.get("/nodes/{node_id}/report")
async def my_node_report(owned: Owned):
    """The node's last heartbeat report, or null if it has sent none since it
    became the caller's."""
    return await owner_views.latest_report(owned.node_id, since=owned.since)


@router.get("/nodes/{node_id}/config-history")
async def my_node_config_history(owned: Owned, limit: int = Query(50, ge=1, le=200)):
    """The node's configuration versions since it became the caller's, newest first."""
    return {"versions": await owner_views.config_history(owned.node_id, limit, since=owned.since)}


@router.get("/nodes/{node_id}/events")
async def my_node_events(owned: Owned, limit: int = Query(100, ge=1, le=500)):
    """The decisions recorded about the node since it became the caller's,
    newest first, each one's actor reduced to whose side it was on as the
    caller reads it."""
    return {"events": await owner_views.owner_events(owned.node_id, limit, owned.user["id"], since=owned.since)}


@router.get("/nodes/{node_id}/snr")
async def my_node_snr(owned: Owned, hours: int = Query(24, ge=1, le=MAX_HOURS)):
    """The node's detection SNR over the last `hours`, per 5-minute bucket,
    from when it became the caller's at the earliest."""
    return await snr_series_async(owned.node_id, hours, since=owned.since)


@router.get("/archive")
async def my_archive(
    user: SignedIn,
    date: str | None = Query(None, description="Date prefix, e.g. 2025/06/21"),
    node_ref: str | None = Query(None, description="Filter by node_ref"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """The public archive's listing over the caller's own nodes, private ones
    included, each from when it became theirs. A node_ref that is not theirs
    lists nothing, as an unknown one does."""
    scope = owner_scope(await get_user_tenures(user["id"]))
    return await list_files(scope, date=date, node_ref=node_ref, limit=limit, offset=offset)


@router.get("/archive/{key:path}")
async def my_archive_file(user: SignedIn, key: str):
    """One of the caller's archive files, as the public archive serves a file.

    A key that is not theirs, or was written before the node became theirs,
    is the public archive's 404 for a key it does not have. The read is
    blocking file I/O, so it runs on a worker thread.
    """
    scope = owner_scope(await get_user_tenures(user["id"]))
    return await asyncio.to_thread(read_file, scope, key)
