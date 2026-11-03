"""Which UTC days each node delivered accepted detections on.

One `node_activity_days` row per node per UTC day on which it delivered
accepted detections, which the contributor rule counts. The frame loop marks a
frame once process_one_frame has returned without raising, and only when the
node is not on probation; a frame with no detection and a synthetic node never
count. The day is the server's UTC date at the mark, not the frame's own
timestamp, which is only as good as the node's clock.

Marks collect in memory as (node_id, day) pairs and are written every
ACTIVITY_FLUSH_INTERVAL_S, so a node already marked today costs a frame one set
lookup, and the database one insert per node per day. A crash loses at most the
minute not yet written, and the node's next frame marks the day again. Writes
ignore a row already there, so writing a pair twice is harmless.

Marks and the flush both run on the event loop, so swapping the pending set
out needs no lock.
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import core.users
from core import state
from core.api_access import NodeActivityDay

ACTIVITY_FLUSH_INTERVAL_S = 60

_DETECTION_FIELDS = ("delay", "doppler", "snr")

# Marked and not yet written.
_pending: set[tuple[str, date]] = set()
# Written by an earlier flush; only today and yesterday are kept.
_written: set[tuple[str, date]] = set()


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _has_detection(frame: dict) -> bool:
    """Whether any detection list is non-empty, the archive writer's count.

    A value that has no length counts as no detections, never as an error.
    """
    for field in _DETECTION_FIELDS:
        value = frame.get(field)
        if value is None:
            continue
        try:
            if len(value) > 0:
                return True
        except TypeError:
            continue
    return False


def mark_frame(node_id: str, frame: dict) -> None:
    """Record that `node_id` delivered `frame` today, if it counts.

    For the frame loop alone, on the event loop: after process_one_frame has
    returned, and only for a node not on probation.
    """
    pair = (node_id, _utc_today())
    if pair in _pending or pair in _written:
        return
    if not _has_detection(frame) or state.node_world(node_id) == "sim":
        return
    _pending.add(pair)


async def record_days(pairs: Iterable[tuple[str, date]]) -> int:
    """Insert (node_id, day) pairs in one transaction, ignoring rows already there.

    Returns how many rows were new.
    """
    rows = [{"node_id": node_id, "day": day} for node_id, day in pairs]
    if not rows:
        return 0
    statement = sqlite_insert(NodeActivityDay).on_conflict_do_nothing()
    async with core.users.async_session_maker() as session, session.begin():
        # On the connection: the session would take a list of rows as an ORM
        # bulk insert, whose result carries no row count.
        connection = await session.connection()
        result = await connection.execute(statement, rows)
    return result.rowcount


async def flush_activity() -> int:
    """Write the pending marks. Returns how many rows were new.

    A failed or cancelled write puts its pairs back for the next flush and
    re-raises.
    """
    global _pending, _written
    batch, _pending = _pending, set()
    try:
        added = await record_days(batch)
    except BaseException:
        _pending |= batch
        raise
    # A pair marked again while the write was in flight is written now.
    _pending -= batch
    oldest = _utc_today() - timedelta(days=1)
    _written = {pair for pair in _written | batch if pair[1] >= oldest}
    return added


async def activity_flush_task() -> None:
    """Flush the marks every ACTIVITY_FLUSH_INTERVAL_S until cancelled.

    Whoever cancels it calls flush_activity() once more, or up to an interval
    of marks is lost.
    """
    while True:
        await asyncio.sleep(ACTIVITY_FLUSH_INTERVAL_S)
        try:
            await flush_activity()
            state.task_last_success["activity_flush"] = time.time()
        except Exception:
            state.bump_task_error("activity_flush")
            logging.exception("Node activity flush failed")
