"""The signed-in owner's own nodes: what they see of each, and what they may change.

Every route here is scoped to the caller's nodes and answers under the node_id,
which is theirs to see. A route about one node takes `Owned`, so a node somebody
else owns answers exactly as one that does not exist, and the check cannot be
left out of a route added later. services/owner_nodes.py builds what is shown,
services/owner_views.py and services/snr_series.py what is shown beyond it.

Two answers name nodes by node_ref instead: the analytics map, keyed as the
public one is, and the archive, whose keys are the public archive's
(services/archive_access.py).
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from core import state
from core.auth import get_user_nodes, get_user_tenures
from core.node_ids import POLLED_BLAH2, system_of
from core.users import async_session_maker, get_current_user
from services import owner_nodes, owner_views, polled_registration
from services.archive_access import list_files, owner_scope, read_file
from services.node_claiming import release_node
from services.polled_radars import remove_polled_radar
from services.snr_series import MAX_HOURS, snr_series_async
from services.tasks.aircraft_flush import owner_bytes

router = APIRouter(prefix="/api/auth/me", tags=["auth"])

#: The caller, for a route about all of their nodes. A dependency, as `Owned`
#: is, so a signed-out caller is 401 before any query value is looked at.
SignedIn = Annotated[dict, Depends(get_current_user)]


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
    removed rather than handed back, since nothing can claim one afterwards
    (services/polled_radars.py, remove_polled_radar).
    """
    if system_of(owned.node_id) == POLLED_BLAH2:
        released = await _remove_polled_radar(owned.node_id, owned.user["id"])
    else:
        released = await release_node(owned.node_id, owned.user["id"])
    if not released:
        # Owned a moment ago and not now: a release that raced another release,
        # or an administrator reassigning it. Either way it is gone, which is
        # what was asked for.
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


async def _remove_polled_radar(node_id: str, user_id: str) -> bool:
    async with async_session_maker() as session:
        async with session.begin():
            if not await remove_polled_radar(session, node_id, user_id=user_id):
                return False
    polled_registration.let_go(node_id)
    return True


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
