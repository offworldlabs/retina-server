"""Persisting a node's owner contact details, one mutable row per node.

Separate from services/node_contact.py, which is a leaf that knows nothing of
the database. The node's own PUT writes it, and two admin routes read and
clear it. Two things erase it unasked: the node registering again, and the
node being retired, however that happens (services/node_retirement.py,
erase_contacts, and services/polled_radars.py, retire_polled_radar).

Nothing commits. The routes own their transactions, as they do for
node_config_store.
"""

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeContact

# The five columns a contact document consists of: everything on node_contacts
# bar the node it belongs to and the timestamp. Exactly validate_contact's
# output keys.
_CONTACT_FIELDS = ("first_name", "last_name", "email", "phone", "country")


async def upsert_contact(session: AsyncSession, node_id: str, contact: dict[str, Any]) -> datetime:
    """Store the contact document and return the row's `updated_at`.

    `contact` is `validate_contact`'s output: exactly the wire fields, normalised,
    and every one of them a column here. Passing a raw request body would write
    whatever keys it happened to carry.

    Replaced wholesale rather than merged: the wire document is the whole of what
    the owner has, so a field it omits has been cleared rather than left alone.
    """
    row = await session.get(NodeContact, node_id)
    if row is None:
        row = NodeContact(node_id=node_id, updated_at=datetime.now(UTC), **{f: contact[f] for f in _CONTACT_FIELDS})
        session.add(row)
        await session.flush()
        return row.updated_at

    if any(getattr(row, field) != contact[field] for field in _CONTACT_FIELDS):
        for field in _CONTACT_FIELDS:
            setattr(row, field, contact[field])
        row.updated_at = datetime.now(UTC)
        await session.flush()
    return row.updated_at


async def list_contacts(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Every stored contact, keyed by node. Nodes with no row are absent."""
    rows = (await session.execute(select(NodeContact))).scalars().all()
    return {
        row.node_id: {**{field: getattr(row, field) for field in _CONTACT_FIELDS}, "updated_at": row.updated_at}
        for row in rows
    }


async def delete_contact(session: AsyncSession, node_id: str) -> bool:
    """Remove a node's contact row. True when there was one."""
    return node_id in await delete_contacts(session, [node_id])


async def delete_contacts(session: AsyncSession, node_ids: Collection[str]) -> set[str]:
    """Remove these nodes' contact rows in one statement. The ids that had one.

    The one statement that erases a contact, so every retirement, whichever
    path it takes, drops exactly what this does.
    """
    if not node_ids:
        return set()
    result = await session.execute(
        delete(NodeContact).where(NodeContact.node_id.in_(node_ids)).returning(NodeContact.node_id)
    )
    return set(result.scalars())
