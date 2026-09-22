"""Seed accounts and node ownership into the laptop stack, or mint a sign-in link.

Runs inside the server container, from the repo root:

    docker exec -i -w /app/backend retina-local-server python - seed < .claude/skills/run-retina-server/seed.py
    docker exec -i -w /app/backend retina-local-server python - link you@example.com < .claude/skills/run-retina-server/seed.py

The ids are fixed rather than read from /api/radar/nodes, which drops private
nodes: a re-run would see a different list and move ownership between accounts.
They are what docker-compose.local.yml's fleet layout produces. Idempotent.
"""

import asyncio
import sys

from core.auth import create_magic_link, get_user_nodes
from core.nodes import Node
from core.users import async_session_maker, get_or_create_magic_link_user
from services.node_auth import mint_node_ref
from services.node_claim_store import set_owner
from services.publication import set_location_privacy

YOU = "you@example.com"
NEIGHBOUR = "neighbour@example.com"
YOURS = ["synth-GVL-0001", "synth-GVL-0002", "synth-GVL-0003", "synth-GVL-0004"]
PRIVATE = "synth-GVL-0002"
# Not synthetic, so it publishes as its minted node_ref, and it never connects:
# a board registered before its owner has set it up.
NEVER_CONNECTED = "ret0c0ffee0001"
THEIRS = ["synth-GVL-0005", "synth-GVL-0006", "synth-GVL-0007"]
LINK_BASE = "http://app.localhost:8080/auth/link/"


async def _own(node_id: str, user_id: str) -> None:
    # Only a registered node can be owned (node_claims has a foreign key to nodes).
    async with async_session_maker() as session, session.begin():
        if await session.get(Node, node_id) is None:
            session.add(Node(node_id=node_id, node_ref=mint_node_ref(), publication="public"))
        await session.flush()
        await set_owner(session, node_id, user_id)


async def seed() -> None:
    you = await get_or_create_magic_link_user(YOU)
    neighbour = await get_or_create_magic_link_user(NEIGHBOUR)
    for node_id in [*YOURS, NEVER_CONNECTED]:
        await _own(node_id, str(you.id))
    for node_id in THEIRS:
        await _own(node_id, str(neighbour.id))
    await set_location_privacy(PRIVATE, True, str(you.id))
    for email, user in ((YOU, you), (NEIGHBOUR, neighbour)):
        print(f"{email} owns {', '.join(sorted(await get_user_nodes(str(user.id))))}")


async def link(email: str) -> None:
    token = await create_magic_link(email)
    print(LINK_BASE + token if token else f"five links already outstanding for {email}; redeem one or wait 15 min")


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["seed"]:
            asyncio.run(seed())
        case ["link", email]:
            asyncio.run(link(email))
        case _:
            sys.exit("usage: python - seed | python - link <email>")
