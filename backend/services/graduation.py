"""An administrator graduating a polled radar, or returning it to probation.

Graduation releases an unvetted operator's data into the solve, the archive and
the public map, so it applies only to the epoch the administrator was shown.
The poller starts a new epoch, back on probation, whenever what a radar
declares may have come from another box; a decision taken against the old
epoch is refused rather than applied to whatever answers now.

Nothing here commits, as in services/polled_radars.py: the route owns the
transaction, and expires the probation and publication caches after committing.
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node, NodeEvent, PolledRadar
from services import probation

TrustState = Literal["probation", "graduated"]

# The node_events kind each TrustState is recorded as, keyed on the value
# the probation fence reads so the two cannot drift apart.
_KINDS = {probation.GRADUATED: "graduated", "probation": "returned_to_probation"}


class NotPolled(LookupError):
    """No polled radar has this node id."""


class EpochMoved(Exception):
    """The radar is no longer at the epoch the decision was taken against."""

    def __init__(self, epoch: int, trust_state: str):
        super().__init__(f"the radar is at epoch {epoch}, {trust_state}")
        self.epoch = epoch
        self.trust_state = trust_state


async def set_trust(session: AsyncSession, node_id: str, *, trust_state: str, epoch: int, actor: str) -> None:
    """Put the radar's current epoch in `trust_state` and record who did. The caller commits.

    The state the radar already holds is recorded as nothing. Raises NotPolled
    for an id no polled radar holds, and EpochMoved when the radar is not at
    `epoch`.
    """
    if trust_state not in _KINDS:
        raise ValueError(f"trust_state must be one of {tuple(_KINDS)}, not {trust_state!r}")
    # The epoch goes in the write's own condition: the poller can start a new
    # epoch between a read and a later write.
    changed = await session.execute(
        update(PolledRadar)
        .where(PolledRadar.node_id == node_id, PolledRadar.epoch == epoch, PolledRadar.trust_state != trust_state)
        .values(trust_state=trust_state)
    )
    if changed.rowcount == 1:
        session.add(NodeEvent(node_id=node_id, kind=_KINDS[trust_state], epoch=epoch, actor=actor))
        await session.flush()
        return
    radar = await session.get(PolledRadar, node_id)
    if radar is None:
        raise NotPolled(node_id)
    if radar.epoch != epoch:
        raise EpochMoved(radar.epoch, radar.trust_state)


def _iso(when: datetime | None) -> str | None:
    # SQLite hands timezone-aware columns back naive; every value written is UTC.
    if when is None:
        return None
    return (when if when.tzinfo is not None else when.replace(tzinfo=UTC)).isoformat()


async def listing(session: AsyncSession) -> list[dict]:
    """Every polled radar with its trust and the decisions about it, newest first.

    The address is shown, since only administrators and the radar's owner may
    see it. The credential never is.
    """
    radars = (
        await session.execute(
            select(PolledRadar, Node.node_ref)
            .join(Node, Node.node_id == PolledRadar.node_id)
            .order_by(PolledRadar.created_at, PolledRadar.node_id)
        )
    ).all()
    events: defaultdict[str, list[dict]] = defaultdict(list)
    rows = await session.scalars(
        select(NodeEvent)
        .where(NodeEvent.node_id.in_([radar.node_id for radar, _ in radars]))
        .order_by(NodeEvent.id.desc())
    )
    for event in rows:
        events[event.node_id].append(
            {"kind": event.kind, "epoch": event.epoch, "actor": event.actor, "at": _iso(event.occurred_at)}
        )
    return [
        {
            "node_id": radar.node_id,
            "node_ref": node_ref,
            "endpoint": radar.endpoint_key,
            "liveness": radar.liveness,
            "last_frame_at": _iso(radar.last_frame_at),
            "epoch": radar.epoch,
            "trust_state": radar.trust_state,
            "events": events[radar.node_id],
        }
        for radar, node_ref in radars
    ]
