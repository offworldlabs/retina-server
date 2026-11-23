"""Fill the activity record from the detection archive, for the days before it.

The contributor rule counts `node_activity_days` rows, which the frame loop
writes (services/node_activity.py) from the day it is deployed. For the days
before, the detection archive is the record: services/parquet_writer.py writes
a node's accepted detections into a partition named for the UTC day, so a part
file there says the node delivered on that day. Only which part files exist is
read, never what is in them.

Both places the archive lives are listed, a day at a time: the local root
(services.storage._LOCAL_ARCHIVE_DIR), which keeps raw parts for about three
days, and the object store under `archive/`, which keeps every day uploaded to
it (services/tasks/archive_lifecycle.py). Both layouts count:
`year=YYYY/month=MM/day=DD/node_id=<id>/` and the older `YYYY/MM/DD/<node>/`.
A part file is a `.parquet` or `.json` file directly inside a node's directory.
A node directory holding none, and a path of any other shape, count for
nothing. A synthetic node (core.state.node_world) never gets a row, as it never
gets a live one.

The partition is the day of the hourly flush that wrote it, not of the frames:
a node that delivered only in the last hour before midnight UTC can be
recorded for the next day instead.

A row already there is left as it is, so a second run adds only days the
first did not find.

Public:

- archive_activity_days(first, last): the (node_id, day) pairs from `first` to
  `last` inclusive with a part file. Blocks on disk and network.
- backfill_activity_days(now=): record the pairs of the contributor window as
  of `now`, today included; returns how many rows were new.
- backfill_if_empty(now=): the same, but only while the record holds no row
  for a day of the window before today. Never raises; 0 when it did not run or
  failed.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

import core.users
from core import state
from core.api_access import NodeActivityDay
from services import entitlements, node_activity, object_store, storage

logger = logging.getLogger(__name__)

# The object store keeps the local archive's paths under this prefix
# (services/tasks/archive_lifecycle.py, _archive_roots).
_STORE_PREFIX = "archive"
_PART_SUFFIXES = (".parquet", ".json")
_HIVE_NODE = "node_id="


def _partitions(day: date) -> tuple[tuple[str, bool], ...]:
    """The day's partition in each layout, relative to the archive root, and
    whether it is the Hive one."""
    return (
        (f"year={day:%Y}/month={day:%m}/day={day:%d}", True),
        (f"{day:%Y}/{day:%m}/{day:%d}", False),
    )


def _node_id(dirname: str, hive: bool) -> str | None:
    """The node a directory of a day's partition belongs to, or None when its
    name is not one.

    `node_id=<id>` in either layout, as services.storage reads it; a bare name
    only in the older layout, where it was the node id itself.
    """
    if dirname.startswith(_HIVE_NODE):
        return dirname[len(_HIVE_NODE) :] or None
    if hive or "=" in dirname:
        return None
    return dirname


def _local_nodes(day_dir: Path, hive: bool) -> set[str]:
    nodes: set[str] = set()
    try:
        node_dirs = list(day_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return nodes
    for node_dir in node_dirs:
        node_id = _node_id(node_dir.name, hive)
        if node_id is None or node_id in nodes:
            continue
        try:
            if any(f.name.endswith(_PART_SUFFIXES) and f.is_file() for f in node_dir.iterdir()):
                nodes.add(node_id)
        except (FileNotFoundError, NotADirectoryError):
            # Not a directory, or emptied and pruned by the archive lifecycle
            # since the day was listed.
            continue
    return nodes


def _store_nodes(prefix: str, hive: bool) -> set[str]:
    nodes: set[str] = set()
    for key in object_store.list_keys(prefix):
        parts = key[len(prefix) :].split("/")
        if len(parts) != 2 or not parts[1].endswith(_PART_SUFFIXES):
            continue
        node_id = _node_id(parts[0], hive)
        if node_id is not None:
            nodes.add(node_id)
    return nodes


def archive_activity_days(first: date, last: date) -> set[tuple[str, date]]:
    """Every (node_id, day) from `first` to `last` inclusive on which a real
    node has a part file, on local disk or in the object store.

    The object store is listed one day's partition at a time, never as a whole,
    since it keeps every day. A listing that fails is logged by
    services.object_store and reads as an empty day.
    """
    root = Path(storage._LOCAL_ARCHIVE_DIR)
    use_store = object_store.is_enabled()
    pairs: set[tuple[str, date]] = set()
    day = first
    while day <= last:
        nodes: set[str] = set()
        for relative, hive in _partitions(day):
            nodes |= _local_nodes(root / relative, hive)
            if use_store:
                nodes |= _store_nodes(f"{_STORE_PREFIX}/{relative}/", hive)
        pairs.update((node_id, day) for node_id in nodes if state.node_world(node_id) != "sim")
        day += timedelta(days=1)
    return pairs


async def backfill_activity_days(now: datetime | None = None) -> int:
    """Record the archive's days of the contributor window as of `now`, from
    its first day through today. Returns how many rows were new."""
    first, today = entitlements.contributor_window(datetime.now(UTC) if now is None else now)
    pairs = await asyncio.to_thread(archive_activity_days, first, today)
    added = await node_activity.record_days(pairs)
    logger.info("Activity backfill: %d node days in the archive from %s to %s, %d new", len(pairs), first, today, added)
    return added


async def backfill_if_empty(now: datetime | None = None) -> int:
    """backfill_activity_days(now), but only while no row holds a day of the
    window before today. Returns how many rows were new.

    Today's rows do not count: the frame loop starts marking today within a
    minute of a boot, and those marks say nothing about the days before.
    Logs a failure and returns 0 rather than raising.
    """
    try:
        now = datetime.now(UTC) if now is None else now
        first, today = entitlements.contributor_window(now)
        async with core.users.async_session_maker() as session:
            held = await session.scalar(
                select(NodeActivityDay.day).where(NodeActivityDay.day >= first, NodeActivityDay.day < today).limit(1)
            )
        if held is not None:
            logger.info("Activity backfill skipped: the record already holds %s", held)
            return 0
        return await backfill_activity_days(now)
    except Exception:
        logger.exception("Activity backfill failed")
        return 0
