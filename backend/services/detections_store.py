"""Reads the detection archive for the public route in routes/detections_archive.py.

The store is services/parquet_writer.py's, under services.storage._LOCAL_ARCHIVE_DIR
(read on every call):
``<root>/year=YYYY/month=MM/day=DD/node_id=<id>/part-HHMMSS-<batch>.parquet``,
one row per detection and one file per node per flush, partitioned on the UTC
day the node's buffer was flushed (hourly) and named for the second it was.

What a window reads:

- the day partitions from the day of its start through the day of its end plus
  DETECTIONS_PARTITION_SLACK_MS, because a frame is filed under the day its
  batch was flushed, which can be the day after it was taken;
- in each, only ``node_id=<id>`` directories, and of those only the nodes the
  caller's scope publishes in the fleet asked for, decided from the directory
  name before anything in it is opened, so a withheld node's files are never
  read. The layouts the Data Explorer still lists from before the Hive
  partitioning (JSON files, a bare ``<id>`` directory) are not read at all;
- only ``*.parquet`` names, never the writer's ``.parquet-*.tmp`` or the
  lifecycle's ``.uploaded`` sentinels, and no file whose name says it was
  flushed more than DETECTIONS_CLOCK_SKEW_MS before the window opened: a flush
  writes frames taken before it, by a node clock at most that far ahead;
- only files stamped as carrying the published receiver position
  (parquet_writer.is_rx_published). The Data Explorer serves an unstamped file,
  written before the stamp, as it is; this route promises the published rx and
  the arcs it feeds are a best-effort layer, so such a file contributes no rows
  and its data is never decoded;
- of the rest, the row groups whose frame_ts_ms footer statistics overlap the
  window. The window and the box are then applied row by row.

Publication is the Data Explorer's public scope (services/archive_access.py),
built by the route on every request and applied here on every call, never
cached: a node registered private, or a real node with no published identity,
contributes nothing, and every row names its node by the scope's identity for
the directory it was read from, never by the node_id column inside the file.

A file can vanish at any moment (the lifecycle deletes uploaded ones); one that
does is skipped quietly. One that cannot be read is skipped with one warning per
version of the file (or per row group that fails).

Two flags say what the archive may not hold, whatever rows were found: ``local``
is false once services/tasks/archive_lifecycle.py may have deleted some of the
window's files, which the object store keeps, and ``pending`` is true while the
hourly write may not have reached all of the window yet, judged from the last
write this process recorded.

Cache: one LRU of its own (tracks_store's _Cache, a separate instance with the
same TRACKS_CACHE_MAX_BYTES bound) holds footer summaries and decoded row groups
of the columns a row is built from, each keyed by the file's path, size and
mtime, so a rewritten file is read again. Partitions are listed on every call,
so a new file is seen by the next call and a removed one is never served from
the cache. Nothing per request is kept. A call also holds its own window's rows
while it runs, outside the bound.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

from config.constants import (
    ARCHIVE_RETENTION_DAYS,
    DETECTIONS_CLOCK_SKEW_MS,
    DETECTIONS_FLUSH_ALLOWANCE_MS,
    DETECTIONS_PARTITION_SLACK_MS,
    DETECTIONS_ROW_CAP,
)
from core import state
from services import r2_client, storage
from services.archive_access import ArchiveScope
from services.node_kind import is_synthetic_node
from services.parquet_writer import SCHEMA, is_rx_published
from services.tracks_store import _MISS, BBox, _Cache

logger = logging.getLogger(__name__)

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

FLEETS = ("real", "synthetic", "all")

# What a decoded row group keeps: the columns a published row is built from.
# node_id is not among them; a row is named from its directory.
_COLUMNS = ("frame_ts_ms", "delay_us", "doppler_hz", "snr_db", "adsb_hex", "rx_lat", "rx_lon", "tx_lat", "tx_lon")
_SCHEMA = pa.schema([SCHEMA.field(name) for name in _COLUMNS])
# Published as null when not finite.
_FLOATS = ("delay_us", "doppler_hz", "snr_db", "rx_lat", "rx_lon", "tx_lat", "tx_lon")

_NODE_DIR = "node_id="
_FLUSHED = re.compile(r"part-([0-9]{2})([0-9]{2})([0-9]{2})-")

_DAY_MS = 24 * 3_600_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The instants a partition day can be computed for.
_MIN_MS = (datetime(1, 1, 2, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
_MAX_MS = (datetime(9999, 12, 31, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)


@dataclass(frozen=True, slots=True)
class _Group:
    """One row group, from its footer: rows, and frame_ts_ms bounds when the statistics give them."""

    rows: int
    ts_min: int | None
    ts_max: int | None


@dataclass(frozen=True, slots=True)
class _File:
    """A listed part file, the version of it a cache entry is for, and the node it is published under."""

    path: str
    size: int
    mtime_ns: int
    node_ref: str


# ── The cache ────────────────────────────────────────────────────────────────

# Separate from tracks_store's, with the same bound: one archive's windows do
# not evict the other's.
_cache = _Cache()

# The file versions and row groups already warned about, the longest unused
# dropped first past _WARNED_MAX. Kept out of _cache, whose bound can be 0 and
# whose evictions would otherwise bring a warning back on every request.
_WARNED_MAX = 4096
_warned_lock = threading.Lock()
_warned: OrderedDict[tuple, None] = OrderedDict()


def _reset_for_tests() -> None:
    """Drop every cached footer and row group, and every warning given. Tests only."""
    _cache.clear()
    with _warned_lock:
        _warned.clear()


def _warn_once(key: tuple, message: str, *args: object) -> None:
    """Log a warning the first time ``key`` (a file version, or one of its row groups) fails."""
    with _warned_lock:
        if key in _warned:
            _warned.move_to_end(key)
            return
        _warned[key] = None
        while len(_warned) > _WARNED_MAX:
            _warned.popitem(last=False)
    logger.warning(message, *args, exc_info=True)


# ── Listing ──────────────────────────────────────────────────────────────────


def _day_of(ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=min(max(ms, _MIN_MS), _MAX_MS))).date()


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _partition(root: str, day: date) -> str:
    return os.path.join(root, f"year={day.year:04d}", f"month={day.month:02d}", f"day={day.day:02d}")


def _flushed_ms(day: date, name: str) -> int | None:
    """When a part file was flushed, to the second, from its partition and name."""
    match = _FLUSHED.match(name)
    if match is None:
        return None
    hour, minute, second = (int(group) for group in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    midnight = (datetime(day.year, day.month, day.day, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
    return midnight + ((hour * 60 + minute) * 60 + second) * 1000


def _entries(directory: str, *, dirs: bool) -> list[str]:
    """The names of a directory's subdirectories or files, sorted. A missing directory has none."""
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if (e.is_dir() if dirs else e.is_file()))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        logger.warning("detections archive: cannot list %s", directory, exc_info=True)
        return []


def _published_nodes(
    directory: str, scope: ArchiveScope, fleet: str, refs: dict[str, str | None]
) -> list[tuple[str, str]]:
    """(directory, node_ref) of each node partition in a day the scope publishes in ``fleet``.

    Judged from the directory name alone. ``refs`` memoises the identities
    across the call's days.
    """
    found = []
    for name in _entries(directory, dirs=True):
        if not name.startswith(_NODE_DIR):
            continue
        node_id = name[len(_NODE_DIR) :]
        if not scope.admits(node_id):
            continue
        if fleet != "all" and ("synthetic" if is_synthetic_node(node_id) else "real") != fleet:
            continue
        if node_id not in refs:
            refs[node_id] = scope.identity(node_id)
        ref = refs[node_id]
        if ref is not None:
            found.append((os.path.join(directory, name), ref))
    return found


def _listed(root: str, from_ms: int, to_ms: int, scope: ArchiveScope, fleet: str) -> list[_File]:
    """Every part file of a published node the window can need, by day, node directory and name."""
    files = []
    refs: dict[str, str | None] = {}
    for day in _days(_day_of(from_ms), _day_of(to_ms - 1 + DETECTIONS_PARTITION_SLACK_MS)):
        for directory, ref in _published_nodes(_partition(root, day), scope, fleet, refs):
            for name in _entries(directory, dirs=False):
                if not name.endswith(".parquet") or name.startswith("."):
                    continue
                flushed = _flushed_ms(day, name)
                # Every frame was taken before the second after its file's
                # flush, by a clock at most DETECTIONS_CLOCK_SKEW_MS ahead.
                if flushed is not None and flushed + 1000 + DETECTIONS_CLOCK_SKEW_MS <= from_ms:
                    continue
                path = os.path.join(directory, name)
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                except OSError:
                    logger.warning("detections archive: cannot stat %s", path, exc_info=True)
                    continue
                files.append(_File(path, st.st_size, st.st_mtime_ns, ref))
    return files


# ── Footers ──────────────────────────────────────────────────────────────────


def _read_groups(path: str) -> tuple[_Group, ...]:
    """Each row group's footer facts, or none for an unstamped file. Reads the footer and nothing else."""
    with pq.ParquetFile(path) as parquet:
        published = is_rx_published(parquet.schema_arrow)
        meta = parquet.metadata
    if not published:
        return ()
    index = {meta.schema.column(i).path: i for i in range(meta.num_columns)}
    ts_col = index.get("frame_ts_ms")
    groups = []
    for g in range(meta.num_row_groups):
        group = meta.row_group(g)
        ts_min = ts_max = None
        if ts_col is not None:
            stats = group.column(ts_col).statistics
            if stats is not None and stats.has_min_max:
                ts_min, ts_max = int(stats.min), int(stats.max)
        groups.append(_Group(group.num_rows, ts_min, ts_max))
    return tuple(groups)


def _footer(file: _File) -> tuple[_Group, ...] | None:
    """A file's row groups, cached; None if it has gone or cannot be read."""
    key = ("footer", file.path, file.size, file.mtime_ns)
    groups = _cache.get(key)
    if groups is not _MISS:
        return groups
    try:
        groups = _read_groups(file.path)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "detections archive: skipping %s, its footer cannot be read", file.path)
        return None
    _cache.put(key, groups, 512 + len(file.path) + 128 * len(groups))
    return groups


# ── Row groups ───────────────────────────────────────────────────────────────


def _normalise(table: pa.Table) -> pa.Table:
    """A row group in the reader's schema.

    A column the file lacks reads as null. Non-finite measurements and
    positions become null, as does an empty adsb_hex. Rows with no frame time
    are dropped here, since no window serves them.
    """
    rows = table.num_rows
    columns = {}
    for field in _SCHEMA:
        if field.name in table.column_names:
            column = table.column(field.name)
            columns[field.name] = column if column.type == field.type else column.cast(field.type)
        else:
            columns[field.name] = pa.chunked_array([pa.nulls(rows, field.type)])
    for name in _FLOATS:
        column = columns[name]
        columns[name] = pc.if_else(pc.is_finite(column), column, pa.scalar(None, column.type))
    hexes = columns["adsb_hex"]
    columns["adsb_hex"] = pc.if_else(pc.equal(hexes, ""), pa.scalar(None, pa.string()), hexes)
    out = pa.table(columns, schema=_SCHEMA)
    return out.filter(pc.is_valid(out.column("frame_ts_ms"))).combine_chunks()


def _read_group(file: _File, index: int) -> pa.Table | None:
    """One row group, decoded and normalised, cached; None if the file has gone or cannot be read."""
    key = ("group", file.path, file.size, file.mtime_ns, index)
    table = _cache.get(key)
    if table is not _MISS:
        return table
    try:
        with pq.ParquetFile(file.path) as parquet:
            present = set(parquet.schema_arrow.names)
            raw = parquet.read_row_group(index, columns=[c for c in _COLUMNS if c in present], use_threads=False)
        table = _normalise(raw)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "detections archive: skipping row group %d of %s, it cannot be read", index, file.path)
        return None
    _cache.put(key, table, 1024 + table.get_total_buffer_size())
    return table


# ── Windows ──────────────────────────────────────────────────────────────────


def _overlaps(group: _Group, from_ms: int, to_ms: int) -> bool:
    """Whether a row group can hold rows of the window, by its footer alone."""
    if group.rows == 0:
        return False
    return group.ts_min is None or group.ts_max is None or (group.ts_max >= from_ms and group.ts_min < to_ms)


def _inside(table: pa.Table, end: str, bbox: BBox) -> pa.ChunkedArray:
    """Whether each row's ``end`` ("rx" or "tx") position is in the box; false where it is null."""
    lat, lon = table.column(f"{end}_lat"), table.column(f"{end}_lon")
    keep = pc.and_(pc.greater_equal(lat, bbox.south), pc.less_equal(lat, bbox.north))
    east_of_west, west_of_east = pc.greater_equal(lon, bbox.west), pc.less_equal(lon, bbox.east)
    if bbox.west <= bbox.east:
        keep = pc.and_(keep, pc.and_(east_of_west, west_of_east))
    else:
        keep = pc.and_(keep, pc.or_(east_of_west, west_of_east))
    return keep.fill_null(False)


def _select(table: pa.Table, from_ms: int, to_ms: int, bbox: BBox | None, rank: int) -> pa.Table:
    """The rows of a decoded row group in the window and the box, with their node's sort rank."""
    ts = table.column("frame_ts_ms")
    table = table.filter(pc.and_(pc.greater_equal(ts, from_ms), pc.less(ts, to_ms)))
    if bbox is not None and table.num_rows:
        table = table.filter(pc.or_(_inside(table, "rx", bbox), _inside(table, "tx", bbox)))
    return table.append_column("node", pa.repeat(pa.scalar(rank, pa.int32()), table.num_rows))


def _rows(table: pa.Table, refs: list[str]) -> list[dict]:
    """The published rows of ``table``, as plain Python values."""
    col = {name: table.column(name).to_pylist() for name in (*_COLUMNS, "node")}
    return [
        {
            "node_ref": refs[node],
            "frame_ts_ms": ts,
            "delay_us": delay,
            "doppler_hz": doppler,
            "snr_db": snr,
            "adsb_hex": adsb_hex,
            "rx_lat": rx_lat,
            "rx_lon": rx_lon,
            "tx_lat": tx_lat,
            "tx_lon": tx_lon,
        }
        for ts, delay, doppler, snr, adsb_hex, rx_lat, rx_lon, tx_lat, tx_lon, node in zip(
            *(col[name] for name in (*_COLUMNS, "node"))
        )
    ]


def _now_ms() -> int:
    """The server's clock, epoch ms."""
    return int(time.time() * 1000)


def _written_ms() -> int | None:
    """When the archive's last hourly write finished, epoch ms; None before the first since the server started.

    services/tasks/periodic.archive_flush_task records it, in this process,
    after a flush of every node's buffer that did not fail as a whole.
    """
    written = state.task_last_success.get("archive_flush")
    return None if written is None else int(written * 1000)


def _local(from_ms: int, now_ms: int) -> bool:
    """Whether every file the window's detections can be in is still where the archive lifecycle keeps it locally.

    False when an object store is configured, local deletion is on, and the
    lifecycle may have deleted one: it deletes a file whose mtime, the time
    it was flushed, is older than its deletion age, and a frame taken at
    ``from_ms`` by a node clock DETECTIONS_CLOCK_SKEW_MS ahead was flushed no
    earlier than that much before it.
    """
    if not r2_client.is_enabled() or ARCHIVE_RETENTION_DAYS <= 0:
        return True
    return from_ms - DETECTIONS_CLOCK_SKEW_MS >= now_ms - ARCHIVE_RETENTION_DAYS * _DAY_MS


def _pending(to_ms: int, now_ms: int, written_ms: int | None) -> bool:
    """Whether the archive may not have written all of the window yet.

    A detection reaches the archive only when its node's buffer is flushed,
    once an hour, so a write that finished DETECTIONS_FLUSH_ALLOWANCE_MS after
    a frame was taken holds it (the allowance covers a node clock running
    behind and the flush itself). Every frame before ``to_ms`` is therefore
    written by DETECTIONS_PARTITION_SLACK_MS after it, and nothing is pending
    from then on. Before that the window is pending until the last write the
    server recorded (``written_ms``) is that allowance past ``to_ms``, and with
    none recorded since the server started, it is pending.
    """
    if to_ms + DETECTIONS_PARTITION_SLACK_MS <= now_ms:
        return False
    return written_ms is None or to_ms + DETECTIONS_FLUSH_ALLOWANCE_MS > written_ms


def query_detections(
    from_ms: int, to_ms: int, *, fleet: str = "real", bbox: BBox | None = None, scope: ArchiveScope
) -> dict:
    """The published detections with from_ms <= frame_ts_ms < to_ms, as the 200 body of /api/data/detections.

    ``scope`` is services.archive_access.public_scope(), built by the caller
    once per request: a node it does not admit, or one it gives no identity,
    contributes nothing, and the rest go out as ``node_ref``, its identity. A
    scope bounded by ``since`` is not supported. ``fleet`` keeps the nodes of
    one kind, by id (services.node_kind). ``bbox`` keeps a row whose published
    rx or true tx position is inside it.

    Rows are sorted by frame_ts_ms, node_ref and delay_us, then the order the
    archive holds them in, and cut at DETECTIONS_ROW_CAP after every filter,
    with ``truncated`` saying so. Whatever rows were found, ``local`` is false
    when the lifecycle may have deleted some of the window's files (_local),
    and ``pending`` is true when the archive may not have written all of it
    yet (_pending).
    """
    if fleet not in FLEETS:
        raise ValueError(f"fleet must be one of {FLEETS}")
    if scope.since:
        raise ValueError("a scope bounded by since is not supported")
    # Read before the listing: a write that lands after it is not in these rows.
    written_ms = _written_ms()
    files = _listed(storage._LOCAL_ARCHIVE_DIR, from_ms, to_ms, scope, fleet)
    order = sorted({file.node_ref for file in files})
    rank = {ref: i for i, ref in enumerate(order)}
    selected = []
    for file in files:
        for index, group in enumerate(_footer(file) or ()):
            if not _overlaps(group, from_ms, to_ms):
                continue
            table = _read_group(file, index)
            if table is not None:
                selected.append(_select(table, from_ms, to_ms, bbox, rank[file.node_ref]))
    schema = _SCHEMA.append(pa.field("node", pa.int32()))
    table = pa.concat_tables(selected) if selected else schema.empty_table()
    # A stable sort: rows tied on all three keep the order they were read in.
    table = table.sort_by([("frame_ts_ms", "ascending"), ("node", "ascending"), ("delay_us", "ascending")])
    cap = DETECTIONS_ROW_CAP
    truncated = table.num_rows > cap
    rows = _rows(table.slice(0, cap), order)
    now_ms = _now_ms()
    return {
        "from": from_ms,
        "to": to_ms,
        "fleet": fleet,
        "count": len(rows),
        "truncated": truncated,
        "local": _local(from_ms, now_ms),
        "pending": _pending(to_ms, now_ms, written_ms),
        "rows": rows,
    }
