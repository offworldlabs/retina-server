"""Reads the tracks archive for the public routes in routes/tracks_archive.py.

The store is services/track_writer.py's:
``<root>/year=YYYY/month=MM/day=DD/part-HHMMSS-<batch>.parquet``, partitioned on
the UTC day a batch was flushed, named for the second it was flushed, with one
row group per node_kind in files written since that column existed and only the
14 older columns in files written before ("legacy"). Files written before
contributing_node_refs was appended have 21 columns. A column a file lacks reads
as null. The root is services.tasks.track_archive._TRACKS_DIR, read on every
call.

Once a day is complete, services/compaction.py rewrites its raw parts into
``compact.parquet`` beside them: one row group per UTC hour of solve_ts_ms and
node_kind, the rows of legacy parts in groups of their own (stored with a null
node_kind, flagged ``legacy`` in its manifest), and a manifest in its own
footer naming the raw parts it holds. Its copy, ``compact.json`` (the
sidecar), stays when the lifecycle deletes the local compact, which then lives
only in object storage.

What a window reads:

- the day partitions from its first day through the day of its end plus
  TRACKS_PARTITION_SLACK_MS, because a row is filed under the day its batch
  was flushed, which can be the day after it was solved;
- in a partition with compact.parquet: its row groups, and the raw parts its
  footer manifest does not list (late parts, flushed after it was built, and
  parts compaction could not read). The parts it lists are superseded and
  never stat'ed or read. Only the footer counts: after a crash the sidecar
  can list fewer parts. A compact whose footer or manifest cannot be read is
  warned about once per version, and its partition read as if it had none;
- in a partition without one, of a complete day (compaction.is_complete) on
  a host with an object store (cold_store.enabled()), whose sidecar is this
  day's tracks manifest and lists a raw part no longer on disk: the compact
  in object storage in its place, and the raw parts the sidecar does not list
  (late or unreadable). The parts it lists are not read, those still on disk
  included, so a day whose listed parts are partly gone (an upload cap, a
  torn part, a short retention) answers whole. The sidecar decides here: the
  copy is held only once its size and sha256 are the sidecar's, so its
  footer lists the same parts;
- in a partition with no raw part and no sidecar of the day (none, one that
  cannot be read, or another store's or day's), of a complete day on a host
  with an object store: the compact in object storage, if the store has one;
- a compact from object storage is read through cold_store.open_day, which
  checks it against the sidecar when there is one and holds a local copy
  until the call has decoded every row group it needs. No copy is no rows;
  ColdStoreError is raised through;
- in any other partition without a compact: its raw parts, as before
  compaction existed. A sidecar whose parts are all still on disk changes
  nothing, since they hold its rows. On a host with no object store, which
  deletes no part, they are all there is: a listed part gone anyway (deleted
  by hand, or the store unset since) is not served;
- of raw parts, only ``part-*.parquet`` names, never the writer's
  ``.parquet-*.tmp`` or the lifecycle's ``.uploaded`` sentinels, and no file
  whose name says it was flushed before the window opened: every row in a
  file was solved no later than the second its name carries;
- of the rest, the row groups whose solve_ts_ms footer statistics overlap the
  window and whose node_kind statistics do not rule out the fleet asked for.
  A compact's legacy groups, like a legacy file's, prove no kind, and their
  rows are classified from their ids when decoded. The window and the fleet
  are then applied row by row.

Which files serve a partition without a usable compact is
compaction.read_plan's to say, the rule services/adsb_store.py reads by too.

So a day answers with the same rows before and after it is compacted, and
after its compact leaves local disk.

A call opens each compact it reads once and reads its footer, manifest and
row groups through that handle, so a re-compaction that replaces the file
mid-call is seen whole by the next call and never half by this one.

A file can vanish at any moment (the lifecycle deletes uploaded ones); one
that does is skipped quietly. One that cannot be read is skipped, with one
warning per version of the file (or per row group that fails), whatever the
cache keeps: the versions already warned about are remembered apart from it,
up to _WARNED_MAX of them.

Cache: one LRU holds decoded row groups (counted by their Arrow buffers),
footer summaries and per-day hour counts (counted by estimate), and evicts
past TRACKS_CACHE_MAX_BYTES (256 MiB unless set), so that is its memory bound.
An entry larger than the bound is used once and not kept. Each file entry is
keyed by path, size and mtime (a compact's by those of the file its call
opened), so a rewritten file is read again; partitions are listed on every
call, so a new file is seen by the next call and a removed one is never served
from the cache. A day's hour counts are keyed by the directory's mtime, its
file names, the size and mtime of its compact and sidecar, and, with a
sidecar, whether the day can be fetched (_fetchable), which change whenever a
file is added, replaced or removed, since the writer and compaction only ever
rename complete files in. A call also holds its own window's rows while it
runs, outside the bound.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Container, Iterable
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

from config.constants import TRACKS_CACHE_MAX_BYTES, TRACKS_PARTITION_SLACK_MS, TRACKS_ROW_CAP
from services import cold_store, compaction, node_refs
from services.node_kind import node_kind
from services.tasks import track_archive
from services.track_writer import SCHEMA

logger = logging.getLogger(__name__)

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

FLEETS = ("real", "synthetic", "all")

# What a decoded row group keeps: every column a published row is built from,
# contributing_node_ids for the privacy rules, and contributing_node_refs, which
# node_refs come from when a row has them. target_class and solve_count are
# never read.
_COLUMNS = (
    "solve_ts_ms",
    "frame_ts_ms",
    "lat",
    "lon",
    "alt_m",
    "vel_east_ms",
    "vel_north_ms",
    "vel_up_ms",
    "n_nodes",
    "contributing_node_ids",
    "contributing_node_refs",
    "adsb_hex",
    "rms_delay_us",
    "rms_doppler_hz",
    "track_key",
    "lane",
    "pos_sigma_m",
    "is_anomalous",
    "anomaly_types",
    "node_kind",
)
_SCHEMA = pa.schema([SCHEMA.field(name) for name in _COLUMNS])
# Published as null when not finite.
_FLOATS = ("alt_m", "vel_east_ms", "vel_north_ms", "vel_up_ms", "pos_sigma_m", "rms_delay_us", "rms_doppler_hz")
# Anything else in these columns is published as null.
_LANES = pa.array(["dark", "known", "follow"], pa.string())
_KINDS = pa.array(["real", "synthetic", "mixed"], pa.string())

_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The instants a partition day can be computed for.
_MIN_MS = (datetime(1, 1, 2, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
_MAX_MS = (datetime(9999, 12, 31, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)

# A flush's file name (services/parquet_io.py). A part file without the time
# is still read; it just cannot be skipped by its name.
_PART = re.compile(r"part-.*\.parquet", re.S)
_FLUSHED = re.compile(r"part-([0-9]{2})([0-9]{2})([0-9]{2})-")

# Footer reads and row-group decodes of one call run on this many threads.
# Opening a file holds the GIL for most of its cost, so more do not help.
_IO_THREADS = 4


class BBox(NamedTuple):
    """south <= lat <= north; west <= lon <= east, or, with west > east, across the antimeridian."""

    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True, slots=True)
class _Group:
    """One row group, from its footer: rows, solve_ts_ms bounds when the
    statistics give them, and the node_kind every row has when they prove one
    (``proven``; ``kind`` None then means every row's is null). ``legacy``
    marks a compact's group of rows from legacy parts: it proves nothing, and
    its rows are classified from their ids as a legacy file's are."""

    rows: int
    ts_min: int | None
    ts_max: int | None
    proven: bool
    kind: str | None
    legacy: bool = False


@dataclass(frozen=True, slots=True)
class _File:
    """A listed part file, or an opened compact, and the version of it a cache entry is for."""

    path: str
    size: int
    mtime_ns: int
    flushed_ms: int | None


@dataclass(frozen=True, slots=True)
class _Compact:
    """A compact's footer as the reader uses it: its row groups, legacy ones
    unproven, and the raw part names its manifest says it holds."""

    groups: tuple[_Group, ...]
    parts: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Opened:
    """A compact as one call reads it: the version the call opened, the
    handle every read of it goes through, and that version's footer."""

    file: _File
    handle: pa.NativeFile
    compact: _Compact


class _Day(NamedTuple):
    """One partition's rows per hour, and whether a read of it fetches its compact from object storage."""

    hours: dict[int, _Tally]
    cold: bool


# ── The cache ────────────────────────────────────────────────────────────────

_MISS = object()


class _Cache:
    """An LRU bounded by the bytes its entries are said to hold. Thread-safe;
    values are computed outside the lock, so two calls may compute one twice."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return _MISS
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key: tuple, value: Any, size: int) -> None:
        bound = TRACKS_CACHE_MAX_BYTES
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            if size > bound:
                return
            self._entries[key] = (value, size)
            self._bytes += size
            while self._bytes > bound:
                _, (_, evicted) = self._entries.popitem(last=False)
                self._bytes -= evicted

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


_cache = _Cache()

# The file versions and row groups already warned about, the longest unused
# dropped first past _WARNED_MAX. Kept out of _cache, whose bound can be 0 and
# whose evictions would otherwise bring a warning back on every poll.
_WARNED_MAX = 4096
_warned_lock = threading.Lock()
_warned: OrderedDict[tuple, None] = OrderedDict()

_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _reset_for_tests() -> None:
    """Drop every cached footer, row group and day, and every warning given. Tests only."""
    _cache.clear()
    with _warned_lock:
        _warned.clear()


def _map[T, R](fn: Callable[[T], R], items: list[T]) -> list[R]:
    """``fn`` over ``items`` in order, on the I/O threads when there is more than one.

    Returns, or raises the first item's exception, only once every item has
    finished, so nothing still reads a file the caller then closes or releases.
    """
    global _pool
    if len(items) < 2:
        return [fn(item) for item in items]
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_IO_THREADS, thread_name_prefix="tracks-store")
        pool = _pool
    futures = [pool.submit(fn, item) for item in items]
    wait(futures)
    return [future.result() for future in futures]


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


def _ms(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _day_of(ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=min(max(ms, _MIN_MS), _MAX_MS))).date()


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
    return _ms(datetime(day.year, day.month, day.day, tzinfo=UTC)) + ((hour * 60 + minute) * 60 + second) * 1000


def _part_names(directory: str) -> list[str]:
    """The part files in one partition, by name. A missing partition has none."""
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if _PART.fullmatch(e.name) and e.is_file())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        logger.warning("tracks archive: cannot list %s", directory, exc_info=True)
        return []


def _stat(path: str, flushed_ms: int | None) -> _File | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("tracks archive: cannot stat %s", path, exc_info=True)
        return None
    return _File(path, st.st_size, st.st_mtime_ns, flushed_ms)


def _identity(path: str) -> tuple[int, int] | None:
    """A file's size and mtime, or None when it is not there."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


# ── Footers ──────────────────────────────────────────────────────────────────


def _groups_of(meta: pq.FileMetaData) -> tuple[_Group, ...]:
    """Each row group's footer facts, from a file's metadata."""
    index = {meta.schema.column(i).path: i for i in range(meta.num_columns)}
    ts_col, kind_col = index.get("solve_ts_ms"), index.get("node_kind")
    groups = []
    for g in range(meta.num_row_groups):
        group = meta.row_group(g)
        rows = group.num_rows
        ts_min = ts_max = None
        if ts_col is not None:
            stats = group.column(ts_col).statistics
            if stats is not None and stats.has_min_max:
                ts_min, ts_max = int(stats.min), int(stats.max)
        proven, kind = False, None
        if kind_col is not None:
            stats = group.column(kind_col).statistics
            if stats is not None and stats.has_null_count:
                if stats.null_count == rows:
                    proven = True
                elif stats.null_count == 0 and stats.has_min_max and stats.min == stats.max:
                    proven, kind = True, sys.intern(str(stats.min))
        groups.append(_Group(rows, ts_min, ts_max, proven, kind))
    return tuple(groups)


def _read_groups(path: str) -> tuple[_Group, ...]:
    """Each row group's footer facts. Reads the footer and nothing else."""
    with pq.ParquetFile(path) as parquet:
        meta = parquet.metadata
    return _groups_of(meta)


def _footer(file: _File) -> tuple[_Group, ...] | None:
    """A raw part's row groups, cached; None if it has gone or cannot be read."""
    key = ("footer", file.path, file.size, file.mtime_ns)
    groups = _cache.get(key)
    if groups is not _MISS:
        return groups
    try:
        groups = _read_groups(file.path)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "tracks archive: skipping %s, its footer cannot be read", file.path)
        return None
    # Measured at about 940 bytes for a three-group file, key included.
    _cache.put(key, groups, 512 + len(file.path) + 128 * len(groups))
    return groups


def _compact_of(meta: pq.FileMetaData, day: date) -> _Compact:
    """A compact's footer facts; ValueError unless it carries the manifest of
    this day's tracks compact, one entry per row group with its row count."""
    raw = (meta.metadata or {}).get(compaction.MANIFEST_KEY)
    if raw is None:
        raise ValueError("its footer carries no compact manifest")
    manifest = compaction.Manifest.from_json(raw)
    if (manifest.store, manifest.day) != (compaction.TRACKS.name, day.isoformat()):
        raise ValueError(f"its manifest is of {manifest.store} {manifest.day}")
    groups = _groups_of(meta)
    if [group.rows for group in groups] != [entry.rows for entry in manifest.groups]:
        raise ValueError("its row groups are not the ones its manifest lists")
    return _Compact(
        tuple(
            replace(group, proven=False, kind=None, legacy=True) if entry.legacy else group
            for group, entry in zip(groups, manifest.groups)
        ),
        frozenset(manifest.parts),
    )


def _compact_footer(file: _File, handle: pa.NativeFile, day: date) -> _Compact | None:
    """The footer of the compact open as ``handle``, which is ``file``, cached; None, warned once, if it is unusable."""
    key = ("compact", file.path, file.size, file.mtime_ns)
    compact = _cache.get(key)
    if compact is not _MISS:
        return compact
    try:
        with pq.ParquetFile(handle) as parquet:
            compact = _compact_of(parquet.metadata, day)
    except Exception:
        _warn_once(key, "tracks archive: %s cannot be used, its footer or manifest cannot be read", file.path)
        return None
    size = 512 + len(file.path) + 128 * len(compact.groups) + sum(96 + len(name) for name in compact.parts)
    _cache.put(key, compact, size)
    return compact


def _open_compact(path: str, day: date, stack: ExitStack) -> _Opened | None:
    """The compact at ``path``, open until ``stack`` closes, with its footer;
    None when there is none, or when it cannot be used (warned once)."""
    try:
        handle = pa.OSFile(path)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(("compact", path), "tracks archive: cannot open %s", path)
        return None
    stack.callback(handle.close)
    st = os.fstat(handle.fileno())
    file = _File(path, st.st_size, st.st_mtime_ns, None)
    compact = _compact_footer(file, handle, day)
    return None if compact is None else _Opened(file, handle, compact)


# ── Row groups ───────────────────────────────────────────────────────────────


def _finite_or_null(column: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.if_else(pc.is_finite(column), column, pa.scalar(None, column.type))


def _known_or_null(column: pa.ChunkedArray, known: pa.Array) -> pa.ChunkedArray:
    return pc.if_else(pc.is_in(column, value_set=known), column, pa.scalar(None, pa.string()))


def _kinds_of(ids: pa.ChunkedArray) -> pa.Array:
    """node_kind for rows written before the column existed: the writer's own classifier, per distinct value."""
    distinct = pc.unique(ids)
    kinds = pa.array([node_kind(value) for value in distinct.to_pylist()], pa.string())
    return kinds.take(pc.index_in(ids, value_set=distinct))


def _normalise(table: pa.Table, legacy: bool = False) -> pa.Table:
    """A row group in the reader's schema, legacy or not.

    Missing columns read as null, except node_kind in a legacy file, or in a
    compact's legacy group (``legacy``), which is classified from the ids.
    Non-finite measurements become null, and a lane or node_kind outside the
    published values becomes null. Rows with no solve time or a non-finite
    position are dropped here, since no window serves them.
    """
    rows = table.num_rows
    columns = {}
    for field in _SCHEMA:
        if field.name in table.column_names:
            column = table.column(field.name)
            columns[field.name] = column if column.type == field.type else column.cast(field.type)
        else:
            columns[field.name] = pa.chunked_array([pa.nulls(rows, field.type)])
    if legacy or "node_kind" not in table.column_names:
        columns["node_kind"] = pa.chunked_array([_kinds_of(columns["contributing_node_ids"])])
    for name in _FLOATS:
        columns[name] = _finite_or_null(columns[name])
    columns["lane"] = _known_or_null(columns["lane"], _LANES)
    columns["node_kind"] = _known_or_null(columns["node_kind"], _KINDS)
    columns["frame_ts_ms"] = columns["frame_ts_ms"].fill_null(0)
    columns["n_nodes"] = columns["n_nodes"].fill_null(0)
    out = pa.table(columns, schema=_SCHEMA)
    keep = pc.and_(
        pc.is_valid(out.column("solve_ts_ms")),
        pc.and_(pc.is_finite(out.column("lat")), pc.is_finite(out.column("lon"))),
    )
    return out.filter(keep).combine_chunks()


def _read_group(file: _File, index: int, legacy: bool = False, handle: pa.NativeFile | None = None) -> pa.Table | None:
    """One row group, decoded and normalised, cached; None if the file has gone or cannot be read.

    A compact's is read through ``handle``, the version ``file`` describes.
    """
    key = ("group", file.path, file.size, file.mtime_ns, index)
    table = _cache.get(key)
    if table is not _MISS:
        return table
    try:
        with pq.ParquetFile(file.path if handle is None else handle) as parquet:
            present = set(parquet.schema_arrow.names)
            raw = parquet.read_row_group(index, columns=[c for c in _COLUMNS if c in present], use_threads=False)
        table = _normalise(raw, legacy)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "tracks archive: skipping row group %d of %s, it cannot be read", index, file.path)
        return None
    _cache.put(key, table, 1024 + table.get_total_buffer_size())
    return table


# ── Windows ──────────────────────────────────────────────────────────────────


def _overlaps(group: _Group, from_ms: int, to_ms: int, fleet: str) -> bool:
    if group.rows == 0:
        return False
    if group.ts_min is not None and group.ts_max is not None and (group.ts_max < from_ms or group.ts_min >= to_ms):
        return False
    return fleet == "all" or not group.proven or group.kind == fleet


def _select(table: pa.Table, from_ms: int, to_ms: int, fleet: str, bbox: BBox | None) -> pa.Table:
    """The rows of a decoded row group in the window, the fleet and the box."""
    solve = table.column("solve_ts_ms")
    keep = pc.and_(pc.greater_equal(solve, from_ms), pc.less(solve, to_ms))
    if fleet != "all":
        keep = pc.and_(keep, pc.equal(table.column("node_kind"), fleet))
    if bbox is not None:
        lat, lon = table.column("lat"), table.column("lon")
        keep = pc.and_(keep, pc.and_(pc.greater_equal(lat, bbox.south), pc.less_equal(lat, bbox.north)))
        east_of_west, west_of_east = pc.greater_equal(lon, bbox.west), pc.less_equal(lon, bbox.east)
        if bbox.west <= bbox.east:
            keep = pc.and_(keep, pc.and_(east_of_west, west_of_east))
        else:
            keep = pc.and_(keep, pc.or_(east_of_west, west_of_east))
    return table.filter(keep)


def _published_refs(ids: str | None, stored: str | None, private: Container[str]) -> tuple[str, ...] | None:
    """A row's contributors as published, or None if the row must be dropped.

    The stored ids are split on "," and only empty parts are skipped. Parts
    are not trimmed: each is looked up exactly as stored, as the live feed
    looks up an id, so a padded id has no identity and drops its row instead
    of passing as the registered node it resembles.

    Dropped when any contributor is private (``private`` answers probation
    too, so it is asked with ``in`` and never copied), whichever way its refs
    are found.

    ``stored`` is the row's contributing_node_refs: the refs the live feed
    published its contributors under when it was archived, which outlive a
    mirrored node's connection. They are split on "," untrimmed, and the row
    is dropped unless they line up one to one with the ids. A row without
    them (null, or a file from before the column) has an empty slot per id,
    as does a contributor that had no published identity when the row was
    archived (the registry could not be read, or the node registered later).

    Each contributor then goes out as node_refs.contributor_handle gives it
    for its slot, the rule the live feed publishes contributors by, and the
    row is dropped if any has none. An empty slot is resolved now, so on test
    and staging a row without stored refs is served for a mirrored node only
    while that node is connected.
    """
    contributors = [node for node in ids.split(",") if node] if ids else []
    if any(node in private for node in contributors):
        return None
    if stored is None:
        slots = [""] * len(contributors)
    else:
        # "" is no slots or one empty slot: the writer joins [] and [""] alike.
        slots = stored.split(",") if stored or contributors else []
        if len(slots) != len(contributors):
            return None
    refs = []
    for node, slot in zip(contributors, slots):
        ref = node_refs.contributor_handle(node, slot or None)
        if ref is None:
            return None
        refs.append(ref)
    return tuple(refs)


def _fetchable(day: date, now_ms: int) -> bool:
    """Whether the day's compact can be read from object storage: a store is configured and the day is complete."""
    return cold_store.enabled() and compaction.is_complete(compaction.TRACKS, day, now_ms)


def _listed(root: str, from_ms: int, to_ms: int, stack: ExitStack) -> list[_Opened | tuple[str, int | None]]:
    """What the window can need, partition by partition: each compact read, opened
    until ``stack`` closes, and the (path, flush time) of each raw part, in flush order.

    Raises cold_store.ColdStoreError when a day kept in object storage
    cannot be loaded.
    """
    found: list[_Opened | tuple[str, int | None]] = []
    now_ms = int(time.time() * 1000)
    for day in _days(_day_of(from_ms), _day_of(to_ms - 1 + TRACKS_PARTITION_SLACK_MS)):
        directory = _partition(root, day)
        names = _part_names(directory)
        opened = _open_compact(os.path.join(directory, compaction.COMPACT_NAME), day, stack)
        if opened is not None:
            found.append(opened)
            names = [name for name in names if name not in opened.compact.parts]
        else:
            plan = compaction.read_plan(compaction.TRACKS, directory, day, names, fetchable=_fetchable(day, now_ms))
            cold = _open_cold(day, plan.sidecar, stack) if plan.cold else None
            # Without a copy (the store yields none for a day it cannot fetch), every raw part is read.
            if cold is not None:
                found.append(cold)
                names = plan.names
        for name in names:
            flushed = _flushed_ms(day, name)
            # Every row was solved before the second after its file's flush.
            if flushed is not None and flushed + 1000 <= from_ms:
                continue
            found.append((os.path.join(directory, name), flushed))
    return found


def _open_cold(day: date, expected: compaction.Manifest | None, stack: ExitStack) -> _Opened | None:
    """The day's compact from object storage, checked against ``expected`` (its sidecar)
    when given, held and open until ``stack`` closes; None if there is none."""
    path = stack.enter_context(cold_store.open_day(compaction.TRACKS, day, expected=expected))
    if path is None:
        return None
    opened = _open_compact(path, day, stack)
    if opened is None:
        logger.warning("tracks archive: the local copy of %s from object storage cannot be read", day)
        raise cold_store.ColdStoreError(day)
    return opened


def _rows(table: pa.Table, refs: dict[tuple[str | None, str | None], tuple[str, ...] | None]) -> list[dict]:
    """The published rows of ``table``, as plain Python values."""
    col = {name: table.column(name).to_pylist() for name in _COLUMNS}
    anomaly_lists: dict[str | None, tuple[str, ...]] = {}
    for value in set(col["anomaly_types"]):
        anomaly_lists[value] = tuple(t for t in (part.strip() for part in value.split(",")) if t) if value else ()
    return [
        {
            "frame_ts_ms": frame_ts,
            "solve_ts_ms": solve_ts,
            "track_key": track_key,
            "lane": lane,
            "node_kind": kind,
            "lat": lat,
            "lon": lon,
            "alt_m": alt,
            "vel_east_ms": vel_east,
            "vel_north_ms": vel_north,
            "vel_up_ms": vel_up,
            "n_nodes": n_nodes,
            "node_refs": list(refs[ids, stored] or ()),
            "adsb_hex": adsb_hex,
            "pos_sigma_m": sigma,
            "rms_delay_us": rms_delay,
            "rms_doppler_hz": rms_doppler,
            "is_anomalous": anomalous,
            "anomaly_types": list(anomaly_lists[anomaly_types]),
        }
        for (
            solve_ts,
            frame_ts,
            lat,
            lon,
            alt,
            vel_east,
            vel_north,
            vel_up,
            n_nodes,
            ids,
            stored,
            adsb_hex,
            rms_delay,
            rms_doppler,
            track_key,
            lane,
            sigma,
            anomalous,
            anomaly_types,
            kind,
        ) in zip(*(col[name] for name in _COLUMNS))
    ]


def _pairs(a: pa.ChunkedArray, b: pa.ChunkedArray) -> tuple[list[tuple[Any, Any]], pa.ChunkedArray]:
    """The distinct (a, b) value pairs of two columns, nulls included, and each row's index into that list."""
    a_values, b_values = pc.unique(a), pc.unique(b)
    width = len(b_values)
    codes = pc.add(
        pc.multiply(pc.index_in(a, value_set=a_values).cast(pa.int64()), width),
        pc.index_in(b, value_set=b_values).cast(pa.int64()),
    )
    distinct = pc.unique(codes)
    a_list, b_list = a_values.to_pylist(), b_values.to_pylist()
    pairs = [(a_list[code // width], b_list[code % width]) for code in distinct.to_pylist()]
    return pairs, pc.index_in(codes, value_set=distinct)


def _source_groups(source: _Opened | _File) -> tuple[_Group, ...] | None:
    return source.compact.groups if isinstance(source, _Opened) else _footer(source)


def _decode(job: tuple[_Opened | _File, int, _Group]) -> pa.Table | None:
    source, index, group = job
    if isinstance(source, _Opened):
        return _read_group(source.file, index, group.legacy, source.handle)
    return _read_group(source, index)


def query_tracks(
    from_ms: int, to_ms: int, *, fleet: str = "real", bbox: BBox | None = None, private: Container[str]
) -> dict:
    """The published solves with from_ms <= solve_ts_ms < to_ms, as the 200 body of /api/data/tracks.

    ``private`` is services.publication.private_node_ids(), read by the
    caller once per request. A row is dropped if any contributor is in it,
    and its contributors go out as ``node_refs``: the refs stamped when it was
    published, or, for a row or slot without them, the ones they resolve to now
    (_published_refs).
    Rows are sorted by frame_ts_ms, then solve_ts_ms, and cut at
    TRACKS_ROW_CAP after every filter, with ``truncated`` saying so.
    Raises cold_store.ColdStoreError when a day of the window is kept only in
    object storage and cannot be loaded from it.
    """
    if fleet not in FLEETS:
        raise ValueError(f"fleet must be one of {FLEETS}")
    root = track_archive._TRACKS_DIR
    # Compacts stay open, and days from object storage held, until every row group is decoded.
    with ExitStack() as stack:
        sources: list[_Opened | _File] = []
        for item in _listed(root, from_ms, to_ms, stack):
            if isinstance(item, _Opened):
                sources.append(item)
            elif (file := _stat(*item)) is not None:
                sources.append(file)
        footers = _map(_source_groups, sources)
        wanted = [
            (source, index, group)
            for source, groups in zip(sources, footers)
            if groups is not None
            for index, group in enumerate(groups)
            if _overlaps(group, from_ms, to_ms, fleet)
        ]
        decoded = _map(_decode, wanted)
    selected = [_select(table, from_ms, to_ms, fleet, bbox) for table in decoded if table is not None]
    table = pa.concat_tables(selected) if selected else _SCHEMA.empty_table()

    pairs, index = _pairs(table.column("contributing_node_ids"), table.column("contributing_node_refs"))
    refs = {pair: _published_refs(*pair, private) for pair in pairs}
    publishable = pa.array([refs[pair] is not None for pair in pairs], pa.bool_())
    table = table.filter(publishable.take(index))

    table = table.sort_by([("frame_ts_ms", "ascending"), ("solve_ts_ms", "ascending")])
    cap = TRACKS_ROW_CAP
    truncated = table.num_rows > cap
    rows = _rows(table.slice(0, cap), refs)
    return {"from": from_ms, "to": to_ms, "fleet": fleet, "count": len(rows), "truncated": truncated, "rows": rows}


# ── Availability ─────────────────────────────────────────────────────────────


class _Tally(NamedTuple):
    """One hour's rows: of every kind, proven real, proven synthetic, and in
    row groups whose kind the footer does not prove."""

    rows: int = 0
    real: int = 0
    synthetic: int = 0
    unproven: int = 0

    @classmethod
    def of(cls, group: _Group) -> _Tally:
        if not group.proven:
            return cls(rows=group.rows, unproven=group.rows)
        if group.kind == "real":
            return cls(rows=group.rows, real=group.rows)
        if group.kind == "synthetic":
            return cls(rows=group.rows, synthetic=group.rows)
        return cls(rows=group.rows)

    def plus(self, other: _Tally) -> _Tally:
        return _Tally(
            self.rows + other.rows,
            self.real + other.real,
            self.synthetic + other.synthetic,
            self.unproven + other.unproven,
        )


_NO_DAY = _Day({}, False)


def _count(hours: dict[int, _Tally], groups: Iterable[_Group], flushed_ms: int | None, day_start: int) -> None:
    """Add each row group to the hour of its earliest solve, or of ``flushed_ms``, or of the day, in that order."""
    for group in groups:
        if group.rows == 0:
            continue
        first = group.ts_min if group.ts_min is not None else flushed_ms
        hour = ((first if first is not None else day_start) // _HOUR_MS) * _HOUR_MS
        hours[hour] = hours.get(hour, _Tally()).plus(_Tally.of(group))


def _sidecar_groups(manifest: compaction.Manifest) -> tuple[_Group, ...]:
    """A compact's row groups as its sidecar lists them, legacy ones unproven."""
    return tuple(
        _Group(entry.rows, entry.ts_min, entry.ts_max, not entry.legacy, entry.kind, entry.legacy)
        for entry in manifest.groups
    )


def _hour_counts(root: str, day: date) -> _Day:
    """One partition's rows per hour of their earliest solve, from footers and names alone.

    A row group counts in the hour of its solve_ts_ms minimum, or of its file's
    flush when the footer has none (a compact has no flush: the day's first
    hour). A raw part holds one row group per kind whatever the hours of its
    rows, so a part that straddles an hour boundary counts in its first hour;
    once compacted, each of its rows counts in its own hour, and the day's
    total is the same.

    A partition counts what query_tracks reads of it: with a usable
    compact.parquet, its row groups and the raw parts its footer manifest
    does not list; read through object storage with a sidecar
    (compaction.read_plan), the sidecar's row groups and the raw parts it
    does not list, every hour of them cold, since a read of any of them
    fetches the day; otherwise its raw parts. A day read from object storage
    without a sidecar counts nothing, since only a fetch could say what it
    holds. A legacy group proves no kind either way.
    """
    directory = _partition(root, day)
    try:
        version = os.stat(directory).st_mtime_ns
    except FileNotFoundError:
        return _NO_DAY
    except OSError:
        logger.warning("tracks archive: cannot stat %s", directory, exc_info=True)
        return _NO_DAY
    names = _part_names(directory)
    compact_path = os.path.join(directory, compaction.COMPACT_NAME)
    compact_id = _identity(compact_path)
    sidecar_id = _identity(os.path.join(directory, compaction.SIDECAR_NAME))
    # Without a sidecar the counts are the raw parts' whether the day can be fetched or not.
    fetchable = _fetchable(day, int(time.time() * 1000)) if sidecar_id is not None else None
    stamp = (version, "\n".join(names), compact_id, sidecar_id, fetchable)
    cached = _cache.get(("day", directory))
    if cached is not _MISS and cached[0] == stamp:
        return cached[1]

    day_start = _ms(datetime(day.year, day.month, day.day, tzinfo=UTC))
    hours: dict[int, _Tally] = {}
    complete, cold = True, False
    opened = None
    if compact_id is not None:
        with ExitStack() as stack:
            opened = _open_compact(compact_path, day, stack)
        # Gone since it was seen, or unusable: counted as if there were none, until a retry.
        complete = opened is not None
    if opened is not None:
        _count(hours, opened.compact.groups, None, day_start)
        names = [name for name in names if name not in opened.compact.parts]
    else:
        plan = compaction.read_plan(compaction.TRACKS, directory, day, names, fetchable=fetchable is True)
        if plan.cold and plan.sidecar is not None:
            _count(hours, _sidecar_groups(plan.sidecar), None, day_start)
            cold = True
            names = plan.names

    listed = [_stat(os.path.join(directory, name), _flushed_ms(day, name)) for name in names]
    files = [f for f in listed if f is not None]
    footers = _map(_footer, files)
    complete = complete and len(files) == len(listed) and all(g is not None for g in footers)
    for file, groups in zip(files, footers):
        _count(hours, groups or (), file.flushed_ms, day_start)
    counted = _Day(hours, cold)
    # A file that vanished or failed to read leaves the counts short; they are
    # kept only when every listed file was counted, so a retry can fill them.
    if complete:
        _cache.put(("day", directory), (stamp, counted), 512 + len(directory) + len(stamp[1]) + 256 * len(hours))
    return counted


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        if day == date.max:
            return
        day += timedelta(days=1)


def hourly_availability(first_day: date, last_day: date, *, fleet: str = "real") -> dict:
    """Rows per hour over UTC days first_day..last_day, as the 200 body of /api/data/tracks/availability.

    Read from Parquet footers, sidecars and file names only (_hour_counts).
    Under ``all`` every row counts. Under ``real`` or ``synthetic`` a row
    group counts when its node_kind statistics prove it that kind, and
    ``unclassified`` holds the rows of row groups they prove nothing about
    (legacy files, and a compact's legacy groups). The partition after
    last_day is read for row groups solved inside the range. ``cold`` is true
    for an hour that a partition read through object storage has rows in,
    raw parts read beside its compact included. Counts are not filtered for
    private nodes.
    """
    if fleet not in FLEETS:
        raise ValueError(f"fleet must be one of {FLEETS}")
    root = track_archive._TRACKS_DIR
    start = _ms(datetime(first_day.year, first_day.month, first_day.day, tzinfo=UTC))
    end = _ms(datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC)) + _DAY_MS
    after = last_day + timedelta(days=1) if last_day < date.max else last_day
    totals: dict[int, _Tally] = {}
    cold: set[int] = set()
    for day in _days(first_day, after):
        counted = _hour_counts(root, day)
        for hour, tally in counted.hours.items():
            if start <= hour < end:
                totals[hour] = totals.get(hour, _Tally()).plus(tally)
                if counted.cold:
                    cold.add(hour)
    hours = []
    for hour in sorted(totals):
        tally = totals[hour]
        if fleet == "all":
            count, unclassified = tally.rows, 0
        else:
            count, unclassified = (tally.real if fleet == "real" else tally.synthetic), tally.unproven
        if count + unclassified > 0:
            hours.append({"start_ms": hour, "count": count, "unclassified": unclassified, "cold": hour in cold})
    return {
        "from": first_day.isoformat(),
        "to": last_day.isoformat(),
        "fleet": fleet,
        "hours": hours,
        "total": sum(h["count"] for h in hours),
    }
