"""Keeping each owner-feed connection to what its account may see.

/ws/aircraft/owner admits a connection on its session cookie (routes/streaming.py)
and every broadcast filters the frame to the nodes that account owns
(services/tasks/aircraft_flush.py). Both answers can change while the socket is
open: a node is released or handed to another account, a session ends or its
account is deactivated. Read once at the handshake, as they used to be, a
released node went on streaming to its former owner until the page was
reloaded, and an ended session kept its feed.

Ownership is read again for every connection at once, one query per broadcast
(`refresh_ownership`). The session is watched per connection, by a task of its
own beside the connection (routes/streaming.py, _close_when_signed_out), so a
slow lookup never holds a frame.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from fastapi import WebSocket
from sqlalchemy import select

from core import state
from core.nodes import NodeClaim
from core.users import async_session_maker

logger = logging.getLogger(__name__)

# How long a broadcast waits for the ownership read before it sends with the
# sets it already holds. The read is one indexed select and answers in about a
# millisecond; this bounds a stalled database (a pool checkout alone may wait
# 30 s) to a short delay per frame instead of a frozen feed. A read that
# overruns is not cancelled: it applies its answer when it has one, in time for
# the next frame.
OWNERSHIP_WAIT_S = 0.5

# The account each open connection was admitted as. state.ws_owner_clients,
# keyed the same way, holds what that account owned at the last read.
_accounts: dict[WebSocket, str] = {}

# The ownership read in flight. One at a time: a read that outlasts its
# broadcast is waited on again by the next one rather than joined by another.
_inflight: asyncio.Task | None = None

# Whether the last read failed, so an outage is logged once rather than per frame.
_read_failing = False


def admit(ws: WebSocket, user_id: str, owned: set[str]) -> None:
    """Serve `ws` as `user_id`, starting from `owned`, read at its handshake."""
    _accounts[ws] = user_id
    state.ws_owner_clients[ws] = owned


def forget(ws: WebSocket) -> None:
    """Stop serving `ws`, whichever side ended it."""
    _accounts.pop(ws, None)
    state.ws_owner_clients.pop(ws, None)


async def owned_by(user_ids: Iterable[str]) -> dict[str, set[str]]:
    """The node ids each of `user_ids` owns, read in one query.

    core.auth.get_user_nodes for many accounts at once: the same rows, the
    node_claims whose user_id is the account's, so an account that owns nothing
    maps to the empty set rather than being left out.
    """
    owned: dict[str, set[str]] = {user_id: set() for user_id in user_ids}
    if not owned:
        return owned
    async with async_session_maker() as session:
        rows = await session.execute(
            select(NodeClaim.node_id, NodeClaim.user_id).where(NodeClaim.user_id.in_(list(owned)))
        )
        for node_id, user_id in rows.tuples():
            if user_id in owned:
                owned[user_id].add(node_id)
    return owned


async def _reread(accounts: dict[WebSocket, str]) -> None:
    """Read ownership for `accounts` and give each connection its account's set.

    A failed read leaves every set as it was: ownership is written to the same
    database, so while it cannot be read it is not changing either, and the next
    broadcast reads again.
    """
    global _read_failing
    try:
        owned = await owned_by(set(accounts.values()))
    except Exception:
        if not _read_failing:
            logger.exception("owner feed: could not read node ownership, serving the last known sets")
        _read_failing = True
        return
    if _read_failing:
        logger.info("owner feed: node ownership is being read again")
    _read_failing = False
    for ws, user_id in accounts.items():
        # A connection that closed while the read ran stays closed.
        if ws in state.ws_owner_clients:
            state.ws_owner_clients[ws] = owned[user_id]


async def refresh_ownership() -> None:
    """Bring every open connection's node set up to date, before a broadcast.

    One query however many connections and accounts are open, and at most one
    in flight. Returns when it has answered or after OWNERSHIP_WAIT_S,
    whichever is first. A connection admitted while the read runs keeps the set
    its own handshake read, which is no older.
    """
    global _inflight
    loop = asyncio.get_running_loop()
    # A read left pending on another event loop (a test's, since closed) will
    # never finish, so it is replaced rather than waited on.
    if _inflight is None or _inflight.done() or _inflight.get_loop() is not loop:
        accounts = {ws: user_id for ws, user_id in _accounts.items() if ws in state.ws_owner_clients}
        if not accounts:
            return
        _inflight = loop.create_task(_reread(accounts))
    await asyncio.wait({_inflight}, timeout=OWNERSHIP_WAIT_S)
