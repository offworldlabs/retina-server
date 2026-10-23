"""Reads the ADS-B snapshot archive for the public route in routes/adsb_archive.py.

The store is the one services/adsb_writer.py names:
``ADSB_DIR/year=YYYY/month=MM/day=DD/part-HHMMSS-<uuid32>.parquet``, partitioned
on the UTC day a batch was flushed and named for the second it was flushed.
Every row of a part was snapshotted no later than that second, and its rows are
sorted by ts_ms. The root is adsb_writer.ADSB_DIR, read on every call.

Once a day is complete, services/compaction.py (compaction.ADSB) rewrites its
raw parts into ``compact.parquet`` beside them: one row group per UTC hour of
ts_ms and world, sorted by ts_ms, and a manifest in its own footer naming the
raw parts it holds. Its copy, ``compact.json`` (the sidecar), stays when the
lifecycle deletes the local compact, which then lives only in object storage.

What a window reads, by the tracks store's rules (services/tracks_store.py):

- the day partitions from the day it opens through the day of its end plus
  ADSB_PARTITION_SLACK_MS, because a batch flushed just after midnight files
  the snapshots taken before it under the new day;
- in a partition with compact.parquet: its row groups, and the raw parts its
  footer manifest does not list (late parts, flushed after it was built, and
  parts compaction could not read). The parts it lists are superseded and
  never stat'ed or read. A compact whose footer or manifest cannot be read is
  warned about once per version, and its partition read as if it had none;
- in a partition without one, what compaction.read_plan says for the day,
  fetchable when an object store is configured (cold_store.enabled()) and the
  day is complete: its raw parts, or the compact in object storage and the
  raw parts its sidecar does not list. A compact from object storage is read
  through cold_store.open_day, which checks it against the sidecar when there
  is one and holds a local copy until the call has decoded every row group it
  needs. No copy is no rows; ColdStoreError is raised through;
- of raw parts, the names _day_parts finds, except those whose name says they
  were flushed before the window opened;
- of the rest, only the row groups whose ts_ms footer statistics overlap the
  window and whose world statistics do not rule out the fleet asked for. The
  window and the fleet are then applied row by row.

So a day answers with the same rows before and after it is compacted, and
after its compact leaves local disk. A call opens each compact it reads once
and reads its footer and row groups through that handle, so a re-compaction
that replaces the file mid-call is seen whole by the next call and never half
by this one.

Privacy: source_node_id says who a row is attributed to, and it, source and
world never leave the server. A node's own fix names the node that reported
it, and is served only while that node may be published (not private, and
either synthetic or registered). An ADSBHub fix is ADSBHub's, not a node's: it
lists, sorted and comma-joined, the nodes whose published range holds it, and
is served while at least one of them may be published. A node fix with no
node id is the simulator's own fleet, served only in world "sim". Rows are
dropped before one fix per aircraft and snapshot is picked, so a withheld
node's fresher fix never hides a public one of the same aircraft: the gaps it
would leave in that aircraft's public track would trace the withheld node's
coverage. In world "real" every public node that held a fresh fix of an
aircraft has a row of its own (services/tasks/adsb_snapshot.py). So once a
node is withheld, even one made private after its rows were written, the
answer is what it would be had that node never reported: the aircraft stays
wherever another public node heard it, from that node's own fixes, and
nothing the withheld node reported is served. A compact holds every row of
its parts, source_node_id and seen_ms as written, so these rules see the same
rows whichever file holds them: a raw part, a local compact or one read from
object storage.

seen_ms is served never after its snapshot, and rounded down to the whole
second, which is all a fix's age needs. Rows carry no node id, but their
timing can still tell public reporters apart: a node stamps every aircraft of
one frame with that frame's time on its own clock, and ts_ms, to the
millisecond, shows where in the second the served second steps. That says
nothing about a withheld node, since nothing it reported is served.

A file can vanish at any moment (the lifecycle deletes uploaded ones); one
that does is skipped quietly. One that cannot be read is skipped with one
warning per version of the file or row group.

Cache: an LRU of this module's own, bounded like the tracks store's by
TRACKS_CACHE_MAX_BYTES, holds footer summaries and decoded row groups, keyed by
path, size and mtime (a compact's by those of the file its call opened), so a
rewritten file is read again. Partitions are listed on every call. What a call
answers is never cached: the private set it is filtered by is read per request.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Container, Iterable
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

from config.constants import ADSB_PARTITION_SLACK_MS, ADSB_ROW_CAP
from services import adsb_writer, cold_store, compaction, node_refs
from services.tracks_store import BBox, _Cache

logger = logging.getLogger(__name__)

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

# The world each fleet reads.
_WORLDS = {"real": ("real",), "synthetic": ("sim",), "all": ("real", "sim")}

# Every column is read: the published ones, and world, source and
# source_node_id, which filter rows and are never served. seen_ms picks the
# freshest fix too, at full precision; what goes out is _served_seen_ms's.
_COLUMNS = (
    "ts_ms",
    "hex",
    "lat",
    "lon",
    "alt_baro",
    "gs",
    "track",
    "flight",
    "seen_ms",
    "world",
    "source",
    "source_node_id",
)
_PUBLISHED = ("ts_ms", "hex", "lat", "lon", "alt_baro", "gs", "track", "flight", "seen_ms")
_SCHEMA = pa.schema([pa.field(name, adsb_writer.SCHEMA.field(name).type) for name in _COLUMNS])

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The instants a partition day can be computed for.
_MIN_MS = (datetime(1, 1, 2, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
_MAX_MS = (datetime(9999, 12, 31, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)

# A raw part as services/parquet_io.py names it, the flush time captured. A
# part file named otherwise is still read; it just cannot be skipped by its name.
_FLUSHED = re.compile(r"part-([0-9]{6})-[0-9a-f]{32}\.parquet")

# Footer reads and row-group decodes of one call run on this many threads.
_IO_THREADS = 4

# What a cache entry holds besides its Arrow buffers: the key, the entry and the
# table object. Also keeps an empty row group from being a free entry.
_ENTRY_OVERHEAD = 1024


@dataclass(frozen=True, slots=True)
class _Group:
    """One row group, from its footer: rows, and the ts_ms and world bounds
    when the statistics give them."""

    rows: int
    ts_min: int | None
    ts_max: int | None
    world_min: str | None
    world_max: str | None


@dataclass(frozen=True, slots=True)
class _File:
    """A listed part file, or an opened compact, and the version of it a cache entry is for."""

    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Compact:
    """A compact's footer as the reader uses it: its row groups, and the raw
    part names its manifest says it holds."""

    groups: tuple[_Group, ...]
    parts: frozenset[str]


@dataclass(frozen=True, slots=True)
class _Opened:
    """A compact as one call reads it: the version the call opened, the
    handle every read of it goes through, and that version's footer."""

    file: _File
    handle: pa.NativeFile
    compact: _Compact


_cache = _Cache()

# The file versions and row groups already warned about, the longest unused
# dropped first past _WARNED_MAX. Kept out of _cache, whose bound can be 0.
_WARNED_MAX = 4096
_warned_lock = threading.Lock()
_warned: OrderedDict[tuple, None] = OrderedDict()

_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _reset_for_tests() -> None:
    """Drop every cached footer and row group, and every warning given. Tests only."""
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
            _pool = ThreadPoolExecutor(max_workers=_IO_THREADS, thread_name_prefix="adsb-store")
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


def _day_of(ms: int) -> date:
    return (_EPOCH + timedelta(milliseconds=min(max(ms, _MIN_MS), _MAX_MS))).date()


def _days(first: date, last: date) -> Iterable[date]:
    day = first
    while day <= last:
        yield day
        if day == date.max:
            return
        day += timedelta(days=1)


def _partition(day: date) -> str:
    return compaction.day_dir(adsb_writer.ADSB_DIR, day)


def _day_parts(day: date) -> list[str]:
    """The paths of one day partition's raw part files, sorted. A missing partition has none.

    Every read of the store finds its raw parts here and nowhere else: the
    files compaction takes for raw parts (compaction.raw_part_names), never the
    writer's ``.parquet-*.tmp`` temporaries, the lifecycle's ``.uploaded``
    sentinels or a compact.
    """
    directory = _partition(day)
    try:
        names = compaction.raw_part_names(directory)
    except OSError:
        logger.warning("adsb archive: cannot list %s", directory, exc_info=True)
        return []
    return [os.path.join(directory, name) for name in names]


def _flushed_ms(day: date, path: str) -> int | None:
    """When a part was flushed, to the second, from its partition and name; None for any other name."""
    match = _FLUSHED.fullmatch(os.path.basename(path))
    if match is None:
        return None
    stamp = match.group(1)
    hour, minute, second = int(stamp[0:2]), int(stamp[2:4]), int(stamp[4:6])
    if hour > 23 or minute > 59 or second > 59:
        return None
    midnight = (datetime(day.year, day.month, day.day, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
    return midnight + ((hour * 60 + minute) * 60 + second) * 1000


def _fetchable(day: date, now_ms: int) -> bool:
    """Whether the day's compact can be read from object storage: a store is configured and the day is complete."""
    return cold_store.enabled() and compaction.is_complete(compaction.ADSB, day, now_ms)


def _listed(from_ms: int, to_ms: int, stack: ExitStack) -> list[_Opened | str]:
    """What the window can need, partition by partition: each compact read, opened
    until ``stack`` closes, and the path of each raw part, in flush order.

    Raises cold_store.ColdStoreError when a day kept in object storage
    cannot be loaded.
    """
    found: list[_Opened | str] = []
    now_ms = int(time.time() * 1000)
    for day in _days(_day_of(from_ms), _day_of(to_ms - 1 + ADSB_PARTITION_SLACK_MS)):
        directory = _partition(day)
        paths = _day_parts(day)
        opened = _open_compact(os.path.join(directory, compaction.COMPACT_NAME), day, stack)
        if opened is not None:
            found.append(opened)
            paths = [path for path in paths if os.path.basename(path) not in opened.compact.parts]
        else:
            names = [os.path.basename(path) for path in paths]
            plan = compaction.read_plan(compaction.ADSB, directory, day, names, fetchable=_fetchable(day, now_ms))
            cold = _open_cold(day, plan.sidecar, stack) if plan.cold else None
            # Without a copy (the store yields none for a day it cannot fetch), every raw part is read.
            if cold is not None:
                found.append(cold)
                kept = set(plan.names)
                paths = [path for path in paths if os.path.basename(path) in kept]
        for path in paths:
            flushed = _flushed_ms(day, path)
            # Every row was snapshotted before the second after its file's flush.
            if flushed is not None and flushed + 1000 <= from_ms:
                continue
            found.append(path)
    return found


def _stat(path: str) -> _File | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("adsb archive: cannot stat %s", path, exc_info=True)
        return None
    return _File(path, st.st_size, st.st_mtime_ns)


# ── Footers ──────────────────────────────────────────────────────────────────


def _groups_of(meta: pq.FileMetaData) -> tuple[_Group, ...]:
    """Each row group's footer facts, from a file's metadata."""
    index = {meta.schema.column(i).path: i for i in range(meta.num_columns)}
    ts_col, world_col = index.get("ts_ms"), index.get("world")
    groups = []
    for g in range(meta.num_row_groups):
        group = meta.row_group(g)
        ts_min = ts_max = world_min = world_max = None
        if ts_col is not None:
            stats = group.column(ts_col).statistics
            if stats is not None and stats.has_min_max:
                ts_min, ts_max = int(stats.min), int(stats.max)
        if world_col is not None:
            stats = group.column(world_col).statistics
            if stats is not None and stats.has_min_max and isinstance(stats.min, str) and isinstance(stats.max, str):
                world_min, world_max = stats.min, stats.max
        groups.append(_Group(group.num_rows, ts_min, ts_max, world_min, world_max))
    return tuple(groups)


def _read_footer(path: str) -> tuple[_Group, ...]:
    """A raw part's row groups' footer facts. Reads the footer and nothing else."""
    with pq.ParquetFile(path) as parquet:
        meta = parquet.metadata
    return _groups_of(meta)


def _footer(file: _File) -> tuple[_Group, ...] | None:
    """A raw part's row groups, cached; None if it has gone or cannot be read."""
    key = ("footer", file.path, file.size, file.mtime_ns)
    cached = _cache.get(key)
    # The cache answers a miss with a sentinel of its own: only a stored value is a tuple.
    if isinstance(cached, tuple):
        return cached
    try:
        groups = _read_footer(file.path)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "adsb archive: skipping %s, its footer cannot be read", file.path)
        return None
    _cache.put(key, groups, 512 + len(file.path) + 192 * len(groups))
    return groups


def _compact_of(meta: pq.FileMetaData, day: date) -> _Compact:
    """A compact's footer facts; ValueError unless it carries the manifest of
    this day's ADS-B compact, one entry per row group with its row count."""
    raw = (meta.metadata or {}).get(compaction.MANIFEST_KEY)
    if raw is None:
        raise ValueError("its footer carries no compact manifest")
    manifest = compaction.Manifest.from_json(raw)
    if (manifest.store, manifest.day) != (compaction.ADSB.name, day.isoformat()):
        raise ValueError(f"its manifest is of {manifest.store} {manifest.day}")
    groups = _groups_of(meta)
    if [group.rows for group in groups] != [entry.rows for entry in manifest.groups]:
        raise ValueError("its row groups are not the ones its manifest lists")
    return _Compact(groups, frozenset(manifest.parts))


def _compact_footer(file: _File, handle: pa.NativeFile, day: date) -> _Compact | None:
    """The footer of the compact open as ``handle``, which is ``file``, cached; None, warned once, if it is unusable."""
    key = ("compact", file.path, file.size, file.mtime_ns)
    cached = _cache.get(key)
    if isinstance(cached, _Compact):
        return cached
    try:
        with pq.ParquetFile(handle) as parquet:
            compact = _compact_of(parquet.metadata, day)
    except Exception:
        _warn_once(key, "adsb archive: %s cannot be used, its footer or manifest cannot be read", file.path)
        return None
    size = 512 + len(file.path) + 192 * len(compact.groups) + sum(96 + len(name) for name in compact.parts)
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
        _warn_once(("compact", path), "adsb archive: cannot open %s", path)
        return None
    stack.callback(handle.close)
    st = os.fstat(handle.fileno())
    file = _File(path, st.st_size, st.st_mtime_ns)
    compact = _compact_footer(file, handle, day)
    return None if compact is None else _Opened(file, handle, compact)


def _open_cold(day: date, expected: compaction.Manifest | None, stack: ExitStack) -> _Opened | None:
    """The day's compact from object storage, checked against ``expected`` (its sidecar)
    when given, held and open until ``stack`` closes; None if there is none."""
    path = stack.enter_context(cold_store.open_day(compaction.ADSB, day, expected=expected))
    if path is None:
        return None
    opened = _open_compact(path, day, stack)
    if opened is None:
        logger.warning("adsb archive: the local copy of %s from object storage cannot be read", day)
        raise cold_store.ColdStoreError(day)
    return opened


def _could_hold(group: _Group, from_ms: int, to_ms: int, worlds: tuple[str, ...]) -> bool:
    """Whether a row group's statistics leave room for a row of the window and the worlds."""
    if group.rows == 0:
        return False
    if group.ts_min is not None and group.ts_max is not None and (group.ts_max < from_ms or group.ts_min >= to_ms):
        return False
    if group.world_min is not None and group.world_max is not None:
        return any(group.world_min <= world <= group.world_max for world in worlds)
    return True


# ── Row groups ───────────────────────────────────────────────────────────────


def _finite_or_null(column: pa.ChunkedArray) -> pa.ChunkedArray:
    return pc.if_else(pc.is_finite(column), column, pa.scalar(None, column.type))


def _decode(source: str | pa.NativeFile, index: int) -> pa.Table:
    """One row group in the reader's schema, from a part's path or an open compact.

    A column the file lacks reads as null. A non-finite speed or track becomes
    null, and a row with no time, no hex or a non-finite position is dropped,
    since nothing can serve it.
    """
    with pq.ParquetFile(source) as parquet:
        present = set(parquet.schema_arrow.names)
        raw = parquet.read_row_group(index, columns=[c for c in _COLUMNS if c in present], use_threads=False)
    columns = {}
    for field in _SCHEMA:
        if field.name in raw.column_names:
            column = raw.column(field.name)
            columns[field.name] = column if column.type == field.type else column.cast(field.type)
        else:
            columns[field.name] = pa.chunked_array([pa.nulls(raw.num_rows, field.type)])
    for name in ("gs", "track"):
        columns[name] = _finite_or_null(columns[name])
    table = pa.table(columns, schema=_SCHEMA)
    keep = pc.and_(
        pc.and_(pc.is_valid(table.column("ts_ms")), pc.is_valid(table.column("hex"))),
        pc.and_(pc.is_finite(table.column("lat")), pc.is_finite(table.column("lon"))),
    )
    return table.filter(keep.fill_null(False)).combine_chunks()


def _read_group(file: _File, index: int, handle: pa.NativeFile | None = None) -> pa.Table | None:
    """One decoded row group, cached; None if the file has gone or cannot be read.

    A compact's is read through ``handle``, the version ``file`` describes.
    """
    key = ("group", file.path, file.size, file.mtime_ns, index)
    cached = _cache.get(key)
    if isinstance(cached, pa.Table):
        return cached
    try:
        table = _decode(file.path if handle is None else handle, index)
    except FileNotFoundError:
        return None
    except Exception:
        _warn_once(key, "adsb archive: skipping row group %d of %s, it cannot be read", index, file.path)
        return None
    _cache.put(key, table, _ENTRY_OVERHEAD + table.nbytes)
    return table


def _source_groups(source: _Opened | _File) -> tuple[_Group, ...] | None:
    return source.compact.groups if isinstance(source, _Opened) else _footer(source)


def _decode_job(job: tuple[_Opened | _File, int]) -> pa.Table | None:
    source, index = job
    if isinstance(source, _Opened):
        return _read_group(source.file, index, source.handle)
    return _read_group(source, index)


# ── Windows ──────────────────────────────────────────────────────────────────


def _in_window(table: pa.Table, from_ms: int, to_ms: int, worlds: tuple[str, ...]) -> pa.Table:
    ts = table.column("ts_ms")
    keep = pc.and_(
        pc.and_(pc.greater_equal(ts, from_ms), pc.less(ts, to_ms)),
        pc.is_in(table.column("world"), value_set=pa.array(worlds, pa.string())),
    )
    return table.filter(keep)


def _published(table: pa.Table, private: Container[str]) -> pa.Table:
    """The rows whose attribution may be published.

    A node may be published when it is not in ``private`` and has a public
    identity (services/node_refs.publishable). A synthetic node always has
    one, a real one only with a registry row, as the Data Explorer withholds
    one without. A node's own fix is kept only while every node
    its source_node_id names may be published: the writer names one, the
    node that reported it. An ADSBHub fix is kept while one of the nodes it
    lists may be. Ids are looked up exactly as stored. Each distinct
    (source, source_node_id) is judged once, and the rows are then filtered
    by it. A value is only ever split at its commas, never looked up whole,
    so no comma-joined list is taken for a node with no identity.
    """
    ids, source, world = table.column("source_node_id"), table.column("source"), table.column("world")
    verdicts: dict[str, bool] = {}

    def publishable(node_id: str) -> bool:
        if node_id not in verdicts:
            verdicts[node_id] = node_refs.publishable(node_id, private)
        return verdicts[node_id]

    def kept(src: str, judge: Callable[[Iterable[bool]], bool]) -> pa.ChunkedArray:
        of_source = pc.equal(source, src)
        ok = []
        for value in pc.unique(ids.filter(of_source)).to_pylist():
            parts = [part for part in (value or "").split(",") if part]
            if parts and judge(publishable(part) for part in parts):
                ok.append(value)
        return pc.and_(of_source, pc.is_in(ids, value_set=pa.array(ok, pa.string())))

    # A node fix with no node id is the simulator's own fleet, and only in its world.
    fleet_kept = pc.and_(pc.equal(source, "node"), pc.and_(pc.is_null(ids), pc.equal(world, "sim")))
    keep = pc.or_(pc.or_(kept("node", all), kept("aggregator", any)), fleet_kept)
    return table.filter(keep.fill_null(False))


def _one_fix_per_aircraft(table: pa.Table) -> pa.Table:
    """One row per (ts_ms, hex), sorted by ts_ms, then hex.

    The freshest fix (greatest seen_ms) wins; on a tie a node's own fix beats
    ADSBHub's, then the smaller source_node_id.
    """
    if table.num_rows == 0:
        return table
    rank = pc.if_else(pc.equal(table.column("source"), "node"), 0, 1)
    order = pc.sort_indices(
        table.append_column("_rank", rank),
        sort_keys=[
            ("ts_ms", "ascending"),
            ("hex", "ascending"),
            ("seen_ms", "descending"),
            ("_rank", "ascending"),
            ("source_node_id", "ascending"),
        ],
    )
    table = table.take(order).combine_chunks()
    n = table.num_rows
    ts, hex_ = table.column("ts_ms"), table.column("hex")
    starts = pc.or_(
        pc.not_equal(ts.slice(1), ts.slice(0, n - 1)),
        pc.not_equal(hex_.slice(1), hex_.slice(0, n - 1)),
    )
    first = pa.concat_arrays([pa.array([True]), *starts.chunks])
    return table.filter(first)


def _served_seen_ms(ts_ms: list[int], seen_ms: list[int | None]) -> list[int]:
    """When each fix was received, as served: never after its snapshot, to the whole second below.

    A node fix's stamp is its own clock, which may run up to a couple of
    seconds ahead (services/feed_helpers.adsb_capture_ts_ms); the snapshot held
    the fix at ts_ms, so it was received by then. A row with no capture time
    is taken as received at its snapshot.
    """
    return [min(t if s is None else s, t) // 1000 * 1000 for t, s in zip(ts_ms, seen_ms)]


def _inside(table: pa.Table, bbox: BBox) -> pa.Table:
    lat, lon = table.column("lat"), table.column("lon")
    keep = pc.and_(pc.greater_equal(lat, bbox.south), pc.less_equal(lat, bbox.north))
    east_of_west, west_of_east = pc.greater_equal(lon, bbox.west), pc.less_equal(lon, bbox.east)
    if bbox.west <= bbox.east:
        keep = pc.and_(keep, pc.and_(east_of_west, west_of_east))
    else:
        keep = pc.and_(keep, pc.or_(east_of_west, west_of_east))
    return table.filter(keep)


def query_adsb(
    from_ms: int, to_ms: int, *, fleet: str = "real", bbox: BBox | None = None, private: Container[str]
) -> dict:
    """The known aircraft with from_ms <= ts_ms < to_ms, as the 200 body of /api/data/adsb.

    ``private`` is services.publication.private_node_ids(), read by the caller
    once per request. In order: the window and the fleet's world; the rows
    whose attribution may be published (_published); one fix per aircraft and
    snapshot (_one_fix_per_aircraft); the box; then the cut at ADSB_ROW_CAP,
    with ``truncated`` saying so. Rows are sorted by ts_ms, then hex, and carry
    only the published columns, as plain Python values, seen_ms as
    _served_seen_ms gives it. Raises cold_store.ColdStoreError when a day of
    the window is kept only in object storage and cannot be loaded from it.
    """
    worlds = _WORLDS.get(fleet)
    if worlds is None:
        raise ValueError(f"fleet must be one of {tuple(_WORLDS)}")
    # Compacts stay open, and days from object storage held, until every row group is decoded.
    with ExitStack() as stack:
        sources: list[_Opened | _File] = []
        for item in _listed(from_ms, to_ms, stack):
            if isinstance(item, _Opened):
                sources.append(item)
            elif (file := _stat(item)) is not None:
                sources.append(file)
        footers = _map(_source_groups, sources)
        wanted = [
            (source, index)
            for source, groups in zip(sources, footers)
            if groups is not None
            for index, group in enumerate(groups)
            if _could_hold(group, from_ms, to_ms, worlds)
        ]
        decoded = _map(_decode_job, wanted)
    selected = [_in_window(table, from_ms, to_ms, worlds) for table in decoded if table is not None]
    table = pa.concat_tables(selected) if selected else _SCHEMA.empty_table()

    table = _published(table, private)
    table = _one_fix_per_aircraft(table)
    if bbox is not None:
        # A filter keeps the order the pick sorted the rows in.
        table = _inside(table, bbox)

    cap = ADSB_ROW_CAP
    truncated = table.num_rows > cap
    table = table.slice(0, cap)
    columns = {name: table.column(name).to_pylist() for name in _PUBLISHED}
    columns["seen_ms"] = _served_seen_ms(columns["ts_ms"], columns["seen_ms"])
    rows = [dict(zip(_PUBLISHED, values)) for values in zip(*columns.values())]
    return {"from": from_ms, "to": to_ms, "fleet": fleet, "count": len(rows), "truncated": truncated, "rows": rows}
