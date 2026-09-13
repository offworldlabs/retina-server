"""Persisting a node's owner contact details, one mutable row per node.

Separate from services/node_contact.py, which is a leaf that knows nothing of
the database. Three callers: the node's own PUT, and the two admin routes that
read and clear.

Nothing commits. The routes own their transactions, as they do for
node_config_store.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeContact

# The four columns a contact document consists of: everything on node_contacts
# bar the node it belongs to and the timestamp. Exactly validate_contact's
# output keys.
_CONTACT_FIELDS = ("first_name", "last_name", "email", "phone")


def _aware(when: datetime) -> datetime:
    """SQLite hands back a naive datetime whatever `DateTime(timezone=True)` says,
    and the response model this feeds is typed `AwareDatetime`. Everything stored
    here is written in UTC, so the zone is restated rather than converted."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


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
        return _aware(row.updated_at)

    if any(getattr(row, field) != contact[field] for field in _CONTACT_FIELDS):
        for field in _CONTACT_FIELDS:
            setattr(row, field, contact[field])
        row.updated_at = datetime.now(UTC)
        await session.flush()
    return _aware(row.updated_at)


async def list_contacts(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Every stored contact, keyed by node. Nodes with no row are absent."""
    rows = (await session.execute(select(NodeContact))).scalars().all()
    return {
        row.node_id: {**{field: getattr(row, field) for field in _CONTACT_FIELDS}, "updated_at": _aware(row.updated_at)}
        for row in rows
    }


async def delete_contact(session: AsyncSession, node_id: str) -> bool:
    """Remove a node's contact row. True when there was one."""
    result = await session.execute(delete(NodeContact).where(NodeContact.node_id == node_id))
    return bool(result.rowcount)
