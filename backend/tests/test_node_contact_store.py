"""node_contacts, from the migrated schema up.

The row is mutable and one per node, unlike node_configs: a detection frame
references a configuration version forever, and personal data in an append-only
table could not be corrected or removed.
"""

from datetime import datetime

from sqlalchemy import select

from core.nodes import Node, NodeContact
from services.node_contact_store import delete_contact, list_contacts, upsert_contact


async def _seed_node(session, node_id: str = "ret1a2b3c4d") -> str:
    # A Node row first: the connection runs with PRAGMA foreign_keys=ON, so a
    # contact for a node that does not exist fails on the foreign key.
    session.add(Node(node_id=node_id, node_ref=f"nde{node_id[3:]:0>12}", status="active"))
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


CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+44 20 7946 0000",
}


async def test_upsert_writes_a_row_for_a_node_that_had_none(node_session):
    node_id = await _seed_node(node_session)

    await upsert_contact(node_session, node_id, dict(CONTACT))
    node_session.expire_all()

    row = (await node_session.execute(select(NodeContact))).scalars().one()
    assert {field: getattr(row, field) for field in CONTACT} == CONTACT


async def test_upsert_replaces_wholesale_rather_than_merging(node_session):
    node_id = await _seed_node(node_session)
    await upsert_contact(node_session, node_id, dict(CONTACT))

    await upsert_contact(node_session, node_id, dict(CONTACT, phone=None, email="ada@example.org"))
    node_session.expire_all()

    row = (await node_session.execute(select(NodeContact))).scalars().one()
    assert (row.email, row.phone) == ("ada@example.org", None)


async def test_upsert_leaves_updated_at_alone_when_nothing_moved(node_session):
    node_id = await _seed_node(node_session)
    first = await upsert_contact(node_session, node_id, dict(CONTACT))

    again = await upsert_contact(node_session, node_id, dict(CONTACT))

    assert again == first


async def test_upsert_moves_updated_at_when_a_field_changes(node_session):
    node_id = await _seed_node(node_session)
    first = await upsert_contact(node_session, node_id, dict(CONTACT))

    moved = await upsert_contact(node_session, node_id, dict(CONTACT, first_name="Augusta"))

    assert moved > first


async def test_updated_at_comes_back_aware_on_the_unchanged_path(node_session):
    """The route's response model is typed AwareDatetime, and SQLite returns a
    naive datetime whichever way the column is declared."""
    node_id = await _seed_node(node_session)
    await upsert_contact(node_session, node_id, dict(CONTACT))
    node_session.expire_all()

    again = await upsert_contact(node_session, node_id, dict(CONTACT))

    assert again.tzinfo is not None


async def test_upsert_holds_one_row_per_node(node_session):
    node_id = await _seed_node(node_session)
    await upsert_contact(node_session, node_id, dict(CONTACT))
    await upsert_contact(node_session, node_id, dict(CONTACT, first_name="Augusta"))
    node_session.expire_all()

    rows = (await node_session.execute(select(NodeContact))).scalars().all()
    assert len(rows) == 1


async def test_list_contacts_returns_every_row_keyed_by_node(node_session):
    first = await _seed_node(node_session, "ret1a2b3c4d")
    second = await _seed_node(node_session, "ret9f8e7d6c")
    node_session.add(Node(node_id="ret2b3c4d5e", node_ref="nde000000000003", status="active"))
    await node_session.flush()
    await upsert_contact(node_session, first, dict(CONTACT))
    await upsert_contact(node_session, second, dict(CONTACT, first_name="Grace"))

    listed = await list_contacts(node_session)

    assert sorted(listed) == [first, second]
    assert listed[second]["first_name"] == "Grace"
    assert isinstance(listed[first]["updated_at"], datetime)


async def test_delete_contact_removes_the_row_and_says_so(node_session):
    node_id = await _seed_node(node_session)
    await upsert_contact(node_session, node_id, dict(CONTACT))

    assert await delete_contact(node_session, node_id) is True
    node_session.expire_all()
    assert (await node_session.execute(select(NodeContact))).scalars().all() == []


async def test_delete_contact_is_false_when_there_was_nothing_to_delete(node_session):
    node_id = await _seed_node(node_session)

    assert await delete_contact(node_session, node_id) is False
