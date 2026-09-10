"""node_contacts, from the migrated schema up.

The row is mutable and one per node, unlike node_configs: a detection frame
references a configuration version forever, and personal data in an append-only
table could not be corrected or removed.
"""

import pytest
from sqlalchemy import select

from core.nodes import Node, NodeContact


async def _seed_node(session, node_id: str = "ret1a2b3c4d") -> str:
    session.add(Node(node_id=node_id, node_ref="nde000000000001", status="active"))
    await session.flush()
    return node_id


async def test_the_migrated_schema_holds_a_contact_row(node_session):
    node_id = await _seed_node(node_session)

    node_session.add(
        NodeContact(
            node_id=node_id,
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            phone="+44 20 7946 0000",
        )
    )
    await node_session.flush()
    node_session.expire_all()

    row = (await node_session.execute(select(NodeContact))).scalars().one()
    assert (row.first_name, row.last_name, row.email, row.phone) == (
        "Ada",
        "Lovelace",
        "ada@example.com",
        "+44 20 7946 0000",
    )
    assert row.updated_at is not None


async def test_every_contact_field_is_nullable(node_session):
    node_id = await _seed_node(node_session)

    node_session.add(NodeContact(node_id=node_id))
    await node_session.flush()
    node_session.expire_all()

    row = (await node_session.execute(select(NodeContact))).scalars().one()
    assert (row.first_name, row.last_name, row.email, row.phone) == (None, None, None, None)
