"""Own a node the way production can: register it first.

The owner is `node_claims.user_id`, and `node_claims` carries a foreign key to
`nodes`, so only a node that came through registration can have one. A test
that owns a bare id fails on the key rather than on what it meant to test.

get_node_owner and set_node_owner read and write the owner outside any
request, which only a test needs: production writes it as part of a claim
(services/node_claiming.py) or an administrator's assignment (routes/admin.py).
"""

import asyncio
from datetime import datetime

from sqlalchemy import select, update

from core.nodes import Node, NodeClaim
from core.users import async_session_maker
from services import node_auth
from services.node_claim_store import set_owner


async def get_node_owner(node_id: str) -> str | None:
    """The owner recorded for `node_id`, or None."""
    async with async_session_maker() as session:
        return (
            await session.execute(select(NodeClaim.user_id).where(NodeClaim.node_id == node_id))
        ).scalar_one_or_none()


async def set_node_owner(node_id: str, user_id: str | None) -> None:
    """Assign or clear the owner in a transaction of its own.

    services/node_claim_store.set_owner is the write, and says what a change of
    owner clears. The node must be registered, or this raises IntegrityError.
    """
    async with async_session_maker() as session:
        async with session.begin():
            await set_owner(session, node_id, user_id)


async def register_node_row(node_id: str, publication: str | None = None) -> str:
    """Give `node_id` a registration if it has none, and return its ref.

    A new registration is public unless told otherwise. Given `publication`, an
    existing one keeps its ref and takes the new choice, which is what a
    reflash does to `Node.publication`; without it, an existing one is left as
    it is.
    """
    async with async_session_maker() as session:
        async with session.begin():
            node = await session.get(Node, node_id)
            if node is None:
                node = Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), publication=publication or "public")
                session.add(node)
            elif publication is not None:
                node.publication = publication
            return node.node_ref


async def own_registered_node(node_id: str, user_id: str, *, since: datetime | None = None) -> str:
    """Register `node_id` if it is not, make `user_id` its owner, and return its ref.

    `since` is when they took it, for a test whose records of the node predate
    the call: an owner is shown nothing from before then. Left out, it is now,
    as an assignment writes it.
    """
    ref = await register_node_row(node_id)
    await set_node_owner(node_id, user_id)
    if since is not None:
        async with async_session_maker() as session:
            async with session.begin():
                await session.execute(update(NodeClaim).where(NodeClaim.node_id == node_id).values(owned_since=since))
    return ref


def own(node_id: str, user_id: str, *, since: datetime | None = None) -> str:
    """own_registered_node for a synchronous test."""
    return asyncio.run(own_registered_node(node_id, user_id, since=since))
