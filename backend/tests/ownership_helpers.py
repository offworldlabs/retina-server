"""Own a node the way production can: register it first.

The owner is `node_claims.user_id`, and `node_claims` carries a foreign key to
`nodes`, so only a node that came through registration can have one. A test
that owns a bare id fails on the key rather than on what it meant to test.
"""

import asyncio

from core.auth import set_node_owner
from core.nodes import Node
from core.users import async_session_maker
from services import node_auth


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


async def own_registered_node(node_id: str, user_id: str) -> str:
    """Register `node_id` if it is not, make `user_id` its owner, and return its ref."""
    ref = await register_node_row(node_id)
    await set_node_owner(node_id, user_id)
    return ref


def own(node_id: str, user_id: str) -> str:
    """own_registered_node for a synchronous test.

    asyncio.run() clears the event loop on exit (3.12), and the async test that
    follows expects one, as conftest's _clean_db also restores.
    """
    ref = asyncio.run(own_registered_node(node_id, user_id))
    asyncio.set_event_loop(asyncio.new_event_loop())
    return ref
