"""A node's own account of itself, as its heartbeat last gave it.

Read by administrators and by nothing that decides whether a node is working:
the server judges that from its own record of frame arrivals.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node, NodeReport

if TYPE_CHECKING:
    from routes.node_schemas import HeartbeatRequest

# How many errors are kept across beats. A node sends at most 32 a beat, each at
# most 512 characters, so this bounds the row at a few tens of kilobytes while
# still holding more than one bad beat's worth.
ERRORS_KEPT = 64


def _aware(when: datetime) -> datetime:
    """SQLite hands back a naive datetime, and everything stored here is UTC."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


async def record_report(session: AsyncSession, node_id: str, beat: "HeartbeatRequest", now: datetime) -> None:
    """Store this beat's self-report over the last one. The caller commits."""
    row = await session.get(NodeReport, node_id)
    if row is None:
        row = NodeReport(node_id=node_id, errors=[])
        session.add(row)
    if row.state != beat.state or row.boot_id != beat.boot_id:
        row.state_since = now
    row.received_at = now
    row.boot_id = beat.boot_id
    row.state = beat.state
    row.uptime_s = beat.uptime_s
    row.config_version = beat.config_version
    row.health = beat.health.model_dump() if beat.health is not None else None
    row.versions = beat.versions.model_dump() if beat.versions is not None else None
    if beat.errors:
        stamped = [{"at": now.isoformat(), "message": message} for message in beat.errors]
        # Reassigned rather than appended to: a JSON column does not see an
        # in-place change, and the append would never be written.
        row.errors = [*(row.errors or []), *stamped][-ERRORS_KEPT:]


async def all_reports(session: AsyncSession) -> list[dict]:
    """Every node's last report, as the admin route serves it."""
    rows = await session.execute(
        select(NodeReport, Node.node_ref).join(Node, Node.node_id == NodeReport.node_id).order_by(NodeReport.node_id)
    )
    return [
        {
            "node_id": row.node_id,
            "node_ref": node_ref,
            "received_at": _aware(row.received_at),
            "boot_id": row.boot_id,
            "state": row.state,
            "state_since": _aware(row.state_since),
            "uptime_s": row.uptime_s,
            "config_version": row.config_version,
            "health": row.health,
            "versions": row.versions,
            "errors": row.errors,
        }
        for row, node_ref in rows
    ]
