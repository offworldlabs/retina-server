"""Daily compaction of a Hive-partitioned Parquet archive store.

A store (Store: TRACKS and ADSB) is written as many small raw parts per UTC day,
``<root>/year=YYYY/month=MM/day=DD/part-*.parquet``, the day being the one the
part was flushed on. Once a day is complete (is_complete: its UTC end plus the
store's settle margin has passed), sweep() rewrites the parts of that
partition into one file beside them, which readers use in their place.

A compacted partition holds, beside any raw parts still on disk:

- ``compact.parquet``: every row of the parts it supersedes, in the store's
  current schema (a column a part lacks is null; a part that lacks a column
  the schema requires, or holds a null in one, cannot be read), zstd level 3,
  with statistics. One row group per (UTC hour of the time column, kind,
  legacy): hours ascending and the rows with a null time last; within an hour
  the store's known kinds in their order, any other kind values sorted, null,
  and then the legacy group. Each group's rows are sorted by time, stably: ties
  keep the order of the previous compact, then of the parts by name. Legacy
  rows come from parts written before the kind column existed. They are
  stored with a null kind, and a reader treats their group as it treats a
  legacy part, classifying each row from its ids.
- The manifest (Manifest.to_json), in the compact's own footer under the key
  ``compact_manifest`` of its key-value metadata, with size and sha256 null:
  store; day; parts, the raw part names whose rows the compact holds;
  unreadable, the raw part names that could not be read in full, which stay
  raw parts; rows; one entry per row group in file order (hour_ms, kind,
  legacy, rows, ts_min, ts_max); and version.
- ``compact.json``, the sidecar: the same manifest with the compact's size and
  sha256 set. It stays when the local compact is deleted, which then lives
  only in object storage, under object_key(store, day, COMPACT_NAME).

A raw part is superseded when the sidecar lists it in ``parts``
(superseded()). Readers skip superseded parts and read every other one, so a
part flushed after its day was compacted is read raw until the next sweep
compacts it in. An unreadable part is never superseded.

What a reader reads of a partition with no usable local compact is
read_plan's to say; services/tracks_store.py and services/adsb_store.py both
ask it.

Crash guarantees: no crash or short write makes a raw part look superseded
while its rows are not in a readable compact.

- The compact is written to ``.compact-<uuid hex>.tmp`` in the partition (no
  reader and no lifecycle picks that name up), fsynced, and verified by
  reading it back: row counts, one row group per manifest entry, the time and
  kind statistics of each, the footer manifest, every row group decoded, and
  each group's times in order. Only then is it renamed over compact.parquet
  and the directory fsynced. A failure before that rename leaves the old
  compact and sidecar as they were.
- The sidecar is written after that rename, through its own tmp file, fsync,
  rename and directory fsync. A crash between the two renames leaves a
  compact whose footer lists more parts than the sidecar: those parts are not
  yet superseded, so a reader that takes the sidecar alone reads their rows
  twice (raw and compacted) until the next sweep sees the sidecar disagree
  with the footer and rewrites it from the footer.
- compact_day, and any tool that compacts a root, serialise on an exclusive
  flock of ``<root>/.compaction.lock``. A compaction tmp file older than an
  hour is left over from a crash and removed; a younger one may be another
  writer's and is kept.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

from config import constants
from services import adsb_writer, track_writer

logger = logging.getLogger(__name__)

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

COMPACT_NAME = "compact.parquet"
SIDECAR_NAME = "compact.json"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"
# The key of the manifest in the compact's key-value metadata.
MANIFEST_KEY = b"compact_manifest"
# Every tmp file this module writes is f"{TMP_PREFIX}<uuid hex>.tmp" in the
# directory it is renamed into. It does not end in .parquet, so the lifecycle,
# which globs for that, never uploads one.
TMP_PREFIX = ".compact-"

_LOCK_NAME = ".compaction.lock"
# Days compacted per sweep; repaired and unchanged days are not counted.
_MAX_COMPACT_PER_SWEEP = 12
# A compaction tmp file this much older than now is left over from a crash.
_STALE_TMP_S = 3600
_MANIFEST_VERSION = 1
_HOUR_MS = 3_600_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_HASH_CHUNK = 1 << 20

# A raw part's name, as the writers publish it (services/parquet_io.py) and
# services/tracks_store.py lists it.
_PART = re.compile(r"part-.*\.parquet", re.S)
_TMP = re.compile(re.escape(TMP_PREFIX) + r"[0-9a-f]{32}\.tmp")
_YEAR = re.compile(r"year=([0-9]{4})")
_MONTH = re.compile(r"month=([0-9]{2})")
_DAY = re.compile(r"day=([0-9]{2})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class CompactionError(Exception):
    """A compaction that must not be installed: nothing it wrote is marked."""


# ── Stores ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Store:
    """One archive store: where it lives, how it is keyed, and what its rows look like.

    The callables are read on every call, so a test or a setting that changes
    the root or a constant takes effect at once.
    """

    name: str
    # The object-store key prefix, the lifecycle's prefix for the same root.
    prefix: str
    # An int64 epoch-ms column: rows are grouped by its UTC hour and sorted by it.
    time_column: str
    # A string column; a row group holds one value of it.
    kind_column: str
    # The known kind values, in row-group order.
    kinds: tuple[str, ...]
    schema: Callable[[], pa.Schema]
    root: Callable[[], str]
    settle_ms: Callable[[], int]
    retention_days: Callable[[], int]
    cold_cache_max_bytes: Callable[[], int]


def _tracks_root() -> str:
    # Imported here: services.tasks pulls in every task module, and tools that
    # compact outside the app import this one.
    from services.tasks import track_archive

    return track_archive._TRACKS_DIR


TRACKS = Store(
    name="tracks",
    prefix="tracks",
    time_column="solve_ts_ms",
    kind_column="node_kind",
    kinds=("real", "synthetic", "mixed"),
    schema=lambda: track_writer.SCHEMA,
    root=_tracks_root,
    settle_ms=lambda: constants.TRACKS_COMPACT_SETTLE_MS,
    retention_days=lambda: constants.TRACKS_COMPACT_RETENTION_DAYS,
    cold_cache_max_bytes=lambda: constants.TRACKS_COLD_CACHE_MAX_BYTES,
)
ADSB = Store(
    name="adsb",
    prefix="adsb",
    time_column="ts_ms",
    kind_column="world",
    kinds=adsb_writer.WORLDS,
    schema=lambda: adsb_writer.SCHEMA,
    root=lambda: adsb_writer.ADSB_DIR,
    settle_ms=lambda: constants.ADSB_COMPACT_SETTLE_MS,
    retention_days=lambda: constants.ADSB_COMPACT_RETENTION_DAYS,
    cold_cache_max_bytes=lambda: constants.ADSB_COLD_CACHE_MAX_BYTES,
)
STORES: tuple[Store, ...] = (TRACKS, ADSB)


def store_for_prefix(prefix: str) -> Store | None:
    """The store whose object-store prefix is ``prefix``, if any."""
    return next((store for store in STORES if store.prefix == prefix), None)


def store_named(name: str) -> Store | None:
    """The store called ``name``, if any."""
    return next((store for store in STORES if store.name == name), None)


# ── The manifest ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Group:
    """One row group of a compact, in file order.

    hour_ms is the UTC hour start of every row's time, None for the group of
    rows whose time is null. kind is every row's kind value (None: null).
    legacy marks rows from parts that had no kind column: their kind is
    stored null and proves nothing, so readers classify them from their ids.
    """

    hour_ms: int | None
    kind: str | None
    legacy: bool
    rows: int
    ts_min: int | None
    ts_max: int | None


_GROUP_KEYS = ("hour_ms", "kind", "legacy", "rows", "ts_min", "ts_max")
_MANIFEST_KEYS = ("version", "store", "day", "parts", "unreadable", "rows", "groups", "size", "sha256")


@dataclass(frozen=True)
class Manifest:
    """What a compact holds; see the module docstring for the format."""

    store: str
    day: str
    parts: tuple[str, ...]
    unreadable: tuple[str, ...]
    rows: int
    groups: tuple[Group, ...]
    size: int | None
    sha256: str | None
    version: int = _MANIFEST_VERSION

    def to_json(self) -> bytes:
        """The manifest as compact JSON, keys in a fixed order."""
        obj = {
            "version": self.version,
            "store": self.store,
            "day": self.day,
            "parts": list(self.parts),
            "unreadable": list(self.unreadable),
            "rows": self.rows,
            "groups": [{key: getattr(group, key) for key in _GROUP_KEYS} for group in self.groups],
            "size": self.size,
            "sha256": self.sha256,
        }
        return json.dumps(obj, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Manifest:
        """A manifest from to_json's output; ValueError on anything malformed."""
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"manifest is not JSON: {exc}") from exc
        if not isinstance(obj, dict) or set(obj) != set(_MANIFEST_KEYS):
            raise ValueError("manifest keys are not the expected ones")
        groups = obj["groups"]
        if not isinstance(groups, list) or not all(isinstance(g, dict) and set(g) == set(_GROUP_KEYS) for g in groups):
            raise ValueError("manifest groups are malformed")
        for name in ("parts", "unreadable"):
            if not isinstance(obj[name], list):
                raise ValueError(f"manifest {name} is not a list")
        manifest = cls(
            store=obj["store"],
            day=obj["day"],
            parts=tuple(obj["parts"]),
            unreadable=tuple(obj["unreadable"]),
            rows=obj["rows"],
            groups=tuple(Group(**{key: g[key] for key in _GROUP_KEYS}) for g in groups),
            size=obj["size"],
            sha256=obj["sha256"],
            version=obj["version"],
        )
        manifest._check()
        return manifest

    def _check(self) -> None:
        """ValueError unless every field is well formed and they agree."""
        if self.version != _MANIFEST_VERSION or not _is_int(self.version):
            raise ValueError(f"manifest version {self.version!r} is not {_MANIFEST_VERSION}")
        if not isinstance(self.store, str) or not self.store:
            raise ValueError("manifest store is not a name")
        if not isinstance(self.day, str):
            raise ValueError("manifest day is not a date")
        try:
            canonical = date.fromisoformat(self.day).isoformat() == self.day
        except ValueError:
            canonical = False
        if not canonical:
            raise ValueError(f"manifest day {self.day!r} is not YYYY-MM-DD")
        for name, names in (("parts", self.parts), ("unreadable", self.unreadable)):
            if not all(isinstance(n, str) and _is_part_name(n) for n in names):
                raise ValueError(f"manifest {name} holds a name that is not a raw part's")
            if list(names) != sorted(set(names)):
                raise ValueError(f"manifest {name} is not sorted and unique")
        if set(self.parts) & set(self.unreadable):
            raise ValueError("a part is listed both as compacted and as unreadable")
        seen: set[tuple[int | None, str | None, bool]] = set()
        last: tuple[bool, int] | None = None
        for group in self.groups:
            _check_group(group)
            order = (group.hour_ms is None, group.hour_ms or 0)
            key = (group.hour_ms, group.kind, group.legacy)
            if (last is not None and order < last) or key in seen:
                raise ValueError("manifest groups are not in hour order, one per hour, kind and legacy")
            last = order
            seen.add(key)
        if not _is_int(self.rows) or self.rows != sum(group.rows for group in self.groups):
            raise ValueError("manifest rows are not the sum of its groups'")
        if (self.size is None) != (self.sha256 is None):
            raise ValueError("manifest has a size or a sha256 without the other")
        if self.size is not None and not (_is_int(self.size) and self.size >= 0):
            raise ValueError("manifest size is not a byte count")
        if self.sha256 is not None and not (isinstance(self.sha256, str) and _SHA256.fullmatch(self.sha256)):
            raise ValueError("manifest sha256 is not a lowercase hex digest")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_part_name(name: str) -> bool:
    return bool(_PART.fullmatch(name)) and "/" not in name and os.sep not in name and "\0" not in name


def _check_group(group: Group) -> None:
    if not (group.kind is None or isinstance(group.kind, str)) or not isinstance(group.legacy, bool):
        raise ValueError("manifest group kind or legacy is malformed")
    if group.legacy and group.kind is not None:
        raise ValueError("a legacy group has a kind")
    if not (_is_int(group.rows) and group.rows > 0):
        raise ValueError("manifest group rows are not a positive count")
    if group.hour_ms is None:
        if group.ts_min is not None or group.ts_max is not None:
            raise ValueError("the null-time group has time bounds")
        return
    if not (_is_int(group.hour_ms) and _is_int(group.ts_min) and _is_int(group.ts_max)):
        raise ValueError("manifest group hour or time bounds are malformed")
    assert group.ts_min is not None and group.ts_max is not None
    if group.hour_ms % _HOUR_MS or not group.hour_ms <= group.ts_min <= group.ts_max < group.hour_ms + _HOUR_MS:
        raise ValueError("manifest group time bounds are outside its hour")


# ── Partitions ───────────────────────────────────────────────────────────────


def day_dir(root: str, day: date) -> str:
    """``root/year=YYYY/month=MM/day=DD``."""
    return os.path.join(root, f"year={day.year:04d}", f"month={day.month:02d}", f"day={day.day:02d}")


def day_of_dir(path: str) -> date | None:
    """The day of a partition directory day_dir names; None for any other path."""
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) < 3:
        return None
    year, month, day = _YEAR.fullmatch(parts[-3]), _MONTH.fullmatch(parts[-2]), _DAY.fullmatch(parts[-1])
    if year is None or month is None or day is None:
        return None
    try:
        return date(int(year[1]), int(month[1]), int(day[1]))
    except ValueError:
        return None


def object_key(store: Store, day: date, name: str) -> str:
    """The object-store key of ``name`` in a day partition, as the lifecycle uploads it."""
    return f"{store.prefix}/year={day.year:04d}/month={day.month:02d}/day={day.day:02d}/{name}"


def is_complete(store: Store, day: date, now_ms: int) -> bool:
    """True once ``now_ms`` is at least the UTC midnight after ``day`` plus the store's settle margin."""
    if day == date.max:
        return False
    after = day + timedelta(days=1)
    end_ms = (datetime(after.year, after.month, after.day, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
    return now_ms >= end_ms + store.settle_ms()


def raw_part_names(directory: str) -> list[str]:
    """The raw part files in a partition, by name; [] for a missing partition."""
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if _PART.fullmatch(e.name) and e.is_file())
    except (FileNotFoundError, NotADirectoryError):
        return []


def read_sidecar(directory: str) -> Manifest | None:
    """The partition's sidecar manifest; None when there is none, or, with a warning, when it cannot be read."""
    path = os.path.join(directory, SIDECAR_NAME)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("compaction: cannot read %s", path, exc_info=True)
        return None
    try:
        return Manifest.from_json(data)
    except ValueError as exc:
        logger.warning("compaction: ignoring malformed %s: %s", path, exc)
        return None


def read_footer_manifest(path: str) -> Manifest | None:
    """The manifest in a compact's footer; None if it has none or a malformed one.

    OSError if the file cannot be opened or its footer cannot be read.
    """
    try:
        with pq.ParquetFile(path) as parquet:
            metadata = parquet.metadata.metadata or {}
    except OSError:
        raise
    except (pa.ArrowException, ValueError) as exc:
        raise OSError(f"{path}: cannot read the Parquet footer: {exc}") from exc
    raw = metadata.get(MANIFEST_KEY)
    if raw is None:
        return None
    try:
        return Manifest.from_json(raw)
    except ValueError:
        return None


def superseded(directory: str) -> frozenset[str]:
    """The raw part names readers skip in a partition: the sidecar's parts, or none."""
    manifest = read_sidecar(directory)
    return frozenset(manifest.parts) if manifest is not None else frozenset()


def sidecar_of(store: Store, directory: str, day: date) -> Manifest | None:
    """The partition's sidecar; None when there is none, it cannot be read, or it is another store's or day's."""
    manifest = read_sidecar(directory)
    if manifest is None or (manifest.store, manifest.day) != (store.name, day.isoformat()):
        return None
    return manifest


class ReadPlan(NamedTuple):
    """How a reader reads a partition with no usable local compact: through
    object storage or not, checked against which sidecar, and which raw parts."""

    cold: bool
    sidecar: Manifest | None
    names: list[str]


def read_plan(store: Store, directory: str, day: date, names: Sequence[str], *, fetchable: bool) -> ReadPlan:
    """What a reader reads of a partition that has no usable local compact.

    ``names`` are its raw parts; ``fetchable`` says whether the day's compact
    can be read from object storage (a store is configured and the day is
    complete). In order:

    - the day cannot be fetched: its raw parts, all of them. A host with no
      object store deletes no part, so they are all there is;
    - no sidecar of the store's day (none, one that cannot be read, or another
      store's or day's): the compact in object storage when there is no raw
      part, else the raw parts;
    - a sidecar whose parts are all still on disk: the raw parts, which hold
      its rows, and nothing fetched;
    - otherwise the compact in object storage, checked against the sidecar,
      and the raw parts the sidecar does not list (late or unreadable). The
      parts it lists are not read, those still on disk included, so a day
      whose listed parts are partly gone answers whole.

    The sidecar is read only when the day can be fetched.
    """
    if not fetchable:
        return ReadPlan(False, None, list(names))
    sidecar = sidecar_of(store, directory, day)
    if sidecar is None:
        return ReadPlan(not names, None, list(names))
    listed = frozenset(sidecar.parts)
    if listed.issubset(names):
        return ReadPlan(False, None, list(names))
    return ReadPlan(True, sidecar, [name for name in names if name not in listed])


# ── Building a compact ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Source:
    """A raw part read in full: its rows per (hour, kind) as (rows, ts_min, ts_max)."""

    name: str
    path: str
    legacy: bool
    rows: int
    tally: dict[tuple[int | None, str | None], tuple[int, int | None, int | None]]


@dataclass(frozen=True, slots=True)
class _Previous:
    path: str
    manifest: Manifest


def _conform(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """``table`` in ``schema``: missing columns null, the rest cast safely.

    Raises on a column not in it, and on a column the schema requires
    (not nullable) that is missing or holds a null, which no compact could hold.
    """
    extra = [name for name in table.column_names if schema.get_field_index(name) < 0]
    if extra or len(set(table.column_names)) != len(table.column_names):
        raise ValueError(f"columns {sorted(table.column_names)} do not fit the store's schema")
    columns = []
    for field in schema:
        if field.name in table.column_names:
            column = table.column(field.name)
            columns.append(column if column.type == field.type else column.cast(field.type, safe=True))
        elif field.nullable:
            columns.append(pa.chunked_array([pa.nulls(table.num_rows, field.type)], type=field.type))
        else:
            raise ValueError(f"column {field.name}, which the store's schema requires, is missing")
        if not field.nullable and columns[-1].null_count:
            raise ValueError(f"column {field.name}, which the store's schema requires, holds nulls")
    return pa.Table.from_arrays(columns, schema=schema)


def _read_part(path: str, schema: pa.Schema) -> tuple[pa.Table, list[str]]:
    """A raw part decoded in full and conformed, and the columns it has. Raises if any of it cannot be read."""
    with pq.ParquetFile(path) as parquet:
        meta = parquet.metadata
        present = parquet.schema_arrow.names
        table = parquet.read(use_threads=False)
        footer_rows = sum(meta.row_group(i).num_rows for i in range(meta.num_row_groups))
    if table.num_rows != meta.num_rows or footer_rows != meta.num_rows:
        raise ValueError(f"decoded {table.num_rows} rows, the footer says {meta.num_rows}")
    return _conform(table, schema), present


def _hours(times: pa.ChunkedArray) -> pa.ChunkedArray:
    """The UTC hour start of each epoch-ms time, null for null; integer arithmetic, floored."""
    start = pc.multiply_checked(pc.divide_checked(times, _HOUR_MS), _HOUR_MS)
    # Integer division truncates towards zero; a time before 1970 floors one hour lower.
    return pc.if_else(pc.greater(start, times), pc.subtract_checked(start, _HOUR_MS), start)


def _tally(store: Store, table: pa.Table) -> dict[tuple[int | None, str | None], tuple[int, int | None, int | None]]:
    times = table.column(store.time_column)
    keyed = pa.table({"hour": _hours(times), "kind": table.column(store.kind_column), "ts": times})
    grouped = keyed.group_by(["hour", "kind"], use_threads=False).aggregate(
        [("ts", "count", pc.CountOptions(mode="all")), ("ts", "min"), ("ts", "max")]
    )
    return {(r["hour"], r["kind"]): (r["ts_count"], r["ts_min"], r["ts_max"]) for r in grouped.to_pylist()}


def _scan(store: Store, schema: pa.Schema, name: str, path: str) -> _Source:
    table, present = _read_part(path, schema)
    return _Source(name, path, store.kind_column not in present, table.num_rows, _tally(store, table))


def _open_previous(store: Store, day: date, path: str, schema: pa.Schema) -> _Previous:
    try:
        manifest = read_footer_manifest(path)
        if manifest is None:
            raise CompactionError(f"previous compact {path} has no manifest")
        if manifest.store != store.name or manifest.day != day.isoformat():
            raise CompactionError(f"previous compact {path} is {manifest.store} {manifest.day}")
        with pq.ParquetFile(path) as parquet:
            meta = parquet.metadata
            names = parquet.schema_arrow.names
        if meta.num_rows != manifest.rows or [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] != [
            group.rows for group in manifest.groups
        ]:
            raise CompactionError(f"previous compact {path} does not match its manifest")
        if any(schema.get_field_index(name) < 0 for name in names):
            raise CompactionError(f"previous compact {path} has columns the store's schema does not")
    except CompactionError:
        raise
    except Exception as exc:
        raise CompactionError(f"previous compact {path} cannot be read: {exc}") from exc
    return _Previous(path, manifest)


def _group_order(store: Store, key: tuple[int | None, str | None, bool]) -> tuple:
    hour, kind, legacy = key
    if legacy:
        rank: tuple = (3, 0, "")
    elif kind is None:
        rank = (2, 0, "")
    elif kind in store.kinds:
        rank = (0, store.kinds.index(kind), "")
    else:
        rank = (1, 0, kind)
    return (hour is None, hour or 0, *rank)


def _plan(store: Store, previous: _Previous | None, sources: list[_Source]) -> tuple[Group, ...]:
    """The compact's row groups, in file order, from the previous compact's manifest and each source's tally."""
    totals: dict[tuple[int | None, str | None, bool], tuple[int, int | None, int | None]] = {}

    def add(key: tuple[int | None, str | None, bool], rows: int, lo: int | None, hi: int | None) -> None:
        old = totals.get(key)
        if old is not None:
            rows += old[0]
            lo = old[1] if lo is None else lo if old[1] is None else min(lo, old[1])
            hi = old[2] if hi is None else hi if old[2] is None else max(hi, old[2])
        totals[key] = (rows, lo, hi)

    if previous is not None:
        for group in previous.manifest.groups:
            add((group.hour_ms, group.kind, group.legacy), group.rows, group.ts_min, group.ts_max)
    for source in sources:
        for (hour, kind), (rows, lo, hi) in source.tally.items():
            add((hour, kind, source.legacy), rows, lo, hi)
    return tuple(
        Group(hour_ms=key[0], kind=key[1], legacy=key[2], rows=rows, ts_min=lo, ts_max=hi)
        for key, (rows, lo, hi) in sorted(totals.items(), key=lambda item: _group_order(store, item[0]))
    )


def _hour_pieces(
    store: Store,
    schema: pa.Schema,
    hour: int | None,
    previous: tuple[pq.ParquetFile, Manifest] | None,
    sources: list[_Source],
) -> list[tuple[pa.Table, bool]]:
    """Every input row of one hour as (table, legacy) pieces.

    The previous compact's groups come first, then the sources by name.
    """
    pieces = []
    if previous is not None:
        parquet, manifest = previous
        for index, group in enumerate(manifest.groups):
            if group.hour_ms != hour:
                continue
            try:
                table = _conform(parquet.read_row_group(index, use_threads=False), schema)
            except Exception as exc:
                raise CompactionError(f"previous compact row group {index} cannot be read: {exc}") from exc
            if table.num_rows != group.rows:
                raise CompactionError(f"previous compact row group {index} decoded {table.num_rows} of {group.rows}")
            pieces.append((table, group.legacy))
    for source in sources:
        if not any(key[0] == hour for key in source.tally):
            continue
        try:
            table, _ = _read_part(source.path, schema)
        except Exception as exc:
            raise CompactionError(f"{source.path} could be read once and not again: {exc}") from exc
        hours = _hours(table.column(store.time_column))
        pieces.append((table.filter(pc.is_null(hours) if hour is None else pc.equal(hours, hour)), source.legacy))
    return pieces


def _group_rows(store: Store, schema: pa.Schema, pieces: list[tuple[pa.Table, bool]], group: Group) -> pa.Table:
    """One row group's rows from an hour's pieces, sorted by time, stably."""
    selected = []
    for table, legacy in pieces:
        if legacy != group.legacy:
            continue
        kinds = table.column(store.kind_column)
        rows = table.filter(pc.is_null(kinds) if group.kind is None else pc.equal(kinds, group.kind))
        if rows.num_rows:
            selected.append(rows)
    if not selected:
        return schema.empty_table()
    rows = pa.concat_tables(selected)
    return rows.take(pc.sort_indices(rows, sort_keys=[(store.time_column, "ascending")]))


def _write_group(writer: pq.ParquetWriter, table: pa.Table) -> None:
    """``table`` as exactly one row group."""
    if table.num_rows:
        writer.write_table(table, row_group_size=table.num_rows)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _write(
    store: Store,
    schema: pa.Schema,
    manifest: Manifest,
    previous: _Previous | None,
    sources: list[_Source],
    out_path: str,
) -> int:
    """Write the compact one hour at a time; fsync it; its size."""
    footer_schema = schema.with_metadata({**(schema.metadata or {}), MANIFEST_KEY: manifest.to_json()})
    hours = list(dict.fromkeys(group.hour_ms for group in manifest.groups))
    with ExitStack() as stack:
        opened = None
        if previous is not None:
            opened = (stack.enter_context(pq.ParquetFile(previous.path)), previous.manifest)
        f = stack.enter_context(open(out_path, "wb"))
        with pq.ParquetWriter(
            f, footer_schema, compression="zstd", compression_level=3, write_statistics=True
        ) as writer:
            for hour in hours:
                pieces = _hour_pieces(store, schema, hour, opened, sources)
                for group in manifest.groups:
                    if group.hour_ms == hour:
                        _write_group(writer, _group_rows(store, schema, pieces, group))
        f.flush()
        os.fsync(f.fileno())
        return os.fstat(f.fileno()).st_size


def _verify(store: Store, schema: pa.Schema, path: str, manifest: Manifest) -> None:
    """CompactionError unless the file on disk is exactly what ``manifest`` describes."""

    def fail(why: str) -> CompactionError:
        return CompactionError(f"{path} failed verification: {why}")

    try:
        with pq.ParquetFile(path) as parquet:
            meta = parquet.metadata
            if not parquet.schema_arrow.equals(schema, check_metadata=False):
                raise fail("its schema is not the store's")
            if meta.num_rows != manifest.rows:
                raise fail(f"{meta.num_rows} rows, expected {manifest.rows}")
            if meta.num_row_groups != len(manifest.groups):
                raise fail(f"{meta.num_row_groups} row groups, expected {len(manifest.groups)}")
            footer = (meta.metadata or {}).get(MANIFEST_KEY)
            if footer is None or Manifest.from_json(footer) != manifest:
                raise fail("its footer manifest does not round-trip")
            index = {meta.schema.column(i).path: i for i in range(meta.num_columns)}
            time_at, kind_at = index[store.time_column], index[store.kind_column]
            for i, group in enumerate(manifest.groups):
                row_group = meta.row_group(i)
                if row_group.num_rows != group.rows:
                    raise fail(f"row group {i} has {row_group.num_rows} rows, expected {group.rows}")
                if not _time_stats_match(row_group.column(time_at).statistics, group):
                    raise fail(f"row group {i} time statistics do not match {group}")
                if not _kind_stats_match(row_group.column(kind_at).statistics, group):
                    raise fail(f"row group {i} kind statistics do not match {group}")
                table = parquet.read_row_group(i, use_threads=False)
                if table.num_rows != group.rows:
                    raise fail(f"row group {i} decoded {table.num_rows} rows, expected {group.rows}")
                if group.hour_ms is not None and not _non_decreasing(table.column(store.time_column)):
                    raise fail(f"row group {i} is not sorted by {store.time_column}")
    except CompactionError:
        raise
    except Exception as exc:
        raise fail(f"it cannot be read back: {exc}") from exc


def _time_stats_match(stats: Any, group: Group) -> bool:
    if stats is None or not stats.has_null_count:
        return False
    if group.hour_ms is None:
        return stats.null_count == group.rows and not stats.has_min_max
    return (
        stats.null_count == 0
        and stats.has_min_max
        and (stats.min, stats.max) == (group.ts_min, group.ts_max)
        and group.hour_ms <= stats.min <= stats.max < group.hour_ms + _HOUR_MS
    )


def _kind_stats_match(stats: Any, group: Group) -> bool:
    if stats is None or not stats.has_null_count:
        return False
    if group.kind is None:
        return stats.null_count == group.rows
    return stats.null_count == 0 and stats.has_min_max and stats.min == group.kind == stats.max


def _non_decreasing(column: pa.ChunkedArray) -> bool:
    values = column.combine_chunks()
    if len(values) < 2:
        return True
    return pc.all(pc.greater_equal(values.slice(1), values.slice(0, len(values) - 1))).as_py() is True


def build(store: Store, day: date, sources: Sequence[str], out_path: str, *, previous: str | None = None) -> Manifest:
    """Write the compact of ``day`` to ``out_path`` from raw part paths and an optional previous compact.

    The compact holds the previous compact's rows (its parts, unreadable
    parts and legacy flags carry over) and every row of each source that can
    be read in full. A source that cannot (its footer, a row group, a safe
    cast to the store's schema, a column the schema does not have, a column
    the schema requires missing or null, or fewer decoded rows than its footer
    says) is left out, listed in ``unreadable`` and warned about once; a
    source that was unreadable before and reads now moves to ``parts``. A source that has vanished, or whose name is not a
    raw part's, repeats another's or is already in the previous compact,
    raises CompactionError, as does a previous compact that cannot be read.

    Reads each source in full once to check it and count its rows per hour
    and kind, then writes one hour at a time, re-reading the sources with
    rows in it, so memory holds one source or one hour's rows. The file is
    fsynced and verified (see _verify) before the manifest is returned, with
    the file's size and sha256 set. Touches nothing but ``out_path``; on any
    exception the caller deletes it.
    """
    out_name = os.path.basename(out_path)
    if out_name in (COMPACT_NAME, SIDECAR_NAME) or _PART.fullmatch(out_name):
        raise ValueError(f"build writes a tmp file, not {out_name}")
    schema = store.schema()
    named = sorted((os.path.basename(path), path) for path in sources)
    names = [name for name, _ in named]
    if not all(_is_part_name(name) for name in names):
        raise CompactionError(f"a source is not a raw part: {names}")
    if len(set(names)) != len(names):
        raise CompactionError(f"two sources share a name: {names}")
    prior = _open_previous(store, day, previous, schema) if previous is not None else None
    if prior is not None and set(names) & set(prior.manifest.parts):
        raise CompactionError(f"sources already compacted: {sorted(set(names) & set(prior.manifest.parts))}")

    read: list[_Source] = []
    failed: list[str] = []
    for name, path in named:
        try:
            read.append(_scan(store, schema, name, path))
        except FileNotFoundError as exc:
            raise CompactionError(f"{path} vanished while compacting") from exc
        except Exception:
            logger.warning(
                "compaction %s %s: %s cannot be read in full, keeping it raw", store.name, day, path, exc_info=True
            )
            failed.append(name)

    read_names = {source.name for source in read}
    before = prior.manifest if prior is not None else None
    manifest = Manifest(
        store=store.name,
        day=day.isoformat(),
        parts=tuple(sorted(set(before.parts if before else ()) | read_names)),
        unreadable=tuple(sorted((set(before.unreadable if before else ()) - read_names) | set(failed))),
        rows=(before.rows if before else 0) + sum(source.rows for source in read),
        groups=_plan(store, prior, read),
        size=None,
        sha256=None,
    )
    try:
        manifest._check()
    except ValueError as exc:
        raise CompactionError(f"the planned manifest is malformed: {exc}") from exc
    size = _write(store, schema, manifest, prior, read, out_path)
    digest = _sha256(out_path)
    _verify(store, schema, out_path, manifest)
    return replace(manifest, size=size, sha256=digest)


# ── Installing it ────────────────────────────────────────────────────────────


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(directory: str, built_path: str, manifest: Manifest) -> None:
    """Rename a built compact over ``directory/compact.parquet``, fsync the directory, then write the sidecar.

    ``manifest`` is build's, with size and sha256 set; the file must still be
    that size and carry that manifest in its footer, or CompactionError and
    nothing is renamed. The sidecar follows the compact, so it never lists a
    part whose rows are not in the compact on disk.
    """
    if manifest.size is None or manifest.sha256 is None:
        raise ValueError("install needs the manifest build returned, with size and sha256")
    if os.stat(built_path).st_size != manifest.size:
        raise CompactionError(f"{built_path} is not the size its manifest says")
    if read_footer_manifest(built_path) != replace(manifest, size=None, sha256=None):
        raise CompactionError(f"{built_path} does not carry the manifest it is installed with")
    os.replace(built_path, os.path.join(directory, COMPACT_NAME))
    _fsync_dir(directory)
    write_sidecar(directory, manifest)


def write_sidecar(directory: str, manifest: Manifest) -> None:
    """Write ``manifest`` (size and sha256 set) to ``directory/compact.json``: tmp, fsync, rename, directory fsync."""
    if manifest.size is None or manifest.sha256 is None:
        raise ValueError("a sidecar carries the compact's size and sha256")
    data = manifest.to_json()
    tmp = os.path.join(directory, f"{TMP_PREFIX}{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(directory, SIDECAR_NAME))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


@contextmanager
def lock(root: str) -> Iterator[None]:
    """Hold an exclusive flock on ``<root>/.compaction.lock``, waiting for it; creates root if missing.

    Not re-entrant: a second lock(root) in the same process while one is held
    waits forever.
    """
    os.makedirs(root, exist_ok=True)
    fd = os.open(os.path.join(root, _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ── The sweep ────────────────────────────────────────────────────────────────


def _remove_stale_tmp(directory: str) -> None:
    """Remove this module's tmp files older than _STALE_TMP_S (by the wall clock); keep younger ones."""
    try:
        with os.scandir(directory) as entries:
            stale = [
                e.path
                for e in entries
                if _TMP.fullmatch(e.name)
                and e.is_file(follow_symlinks=False)
                and time.time() - e.stat(follow_symlinks=False).st_mtime > _STALE_TMP_S
            ]
    except (FileNotFoundError, NotADirectoryError):
        return
    for path in stale:
        try:
            os.unlink(path)
            logger.info("compaction: removed stale %s", path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("compaction: cannot remove stale %s", path, exc_info=True)


def _repair_sidecar(directory: str, compact: str, footer: Manifest) -> bool:
    """Rewrite the sidecar from the compact's footer unless it already agrees; whether it did."""
    size = os.stat(compact).st_size
    sidecar = read_sidecar(directory)
    if sidecar is not None and sidecar.sha256 is not None and sidecar.size == size:
        if replace(sidecar, size=None, sha256=None) == footer:
            return False
    write_sidecar(directory, replace(footer, size=size, sha256=_sha256(compact)))
    logger.warning("compaction: %s disagreed with its compact; rewrote it from the footer", directory)
    return True


def _compact_locked(store: Store, root: str, day: date, now_ms: int, may_build: bool) -> str:
    directory = day_dir(root, day)
    _remove_stale_tmp(directory)
    compact = os.path.join(directory, COMPACT_NAME)
    sidecar_path = os.path.join(directory, SIDECAR_NAME)
    repaired = False
    local = os.path.lexists(compact)
    if local:
        footer = read_footer_manifest(compact)
        if footer is None or footer.store != store.name or footer.day != day.isoformat():
            logger.warning("compaction: %s has no manifest of %s %s; leaving the day alone", compact, store.name, day)
            return "error"
        repaired = _repair_sidecar(directory, compact, footer)
        manifest: Manifest | None = footer
    else:
        manifest = read_sidecar(directory)
        if manifest is None and os.path.lexists(sidecar_path):
            # The compact may live in object storage: a new one built from the
            # late parts alone would replace it there.
            logger.warning(
                "compaction: %s cannot be read and there is no local compact; leaving the day alone", sidecar_path
            )
            return "error"
        if manifest is not None and (manifest.store != store.name or manifest.day != day.isoformat()):
            logger.warning(
                "compaction: %s is not a manifest of %s %s; leaving the day alone", sidecar_path, store.name, day
            )
            return "error"

    raw = set(raw_part_names(directory))
    known = set(manifest.parts) | set(manifest.unreadable) if manifest is not None else set()
    late = sorted(raw - known)
    if not late:
        return "repaired" if repaired else "unchanged"
    retried = [name for name in manifest.unreadable if name in raw] if manifest is not None else []
    if not is_complete(store, day, now_ms):
        return "skipped"
    if not local and manifest is not None:
        logger.warning(
            "compaction: %s %s has %d late parts but its compact is only in object storage; leaving them raw",
            store.name,
            day,
            len(late),
        )
        return "skipped"
    if not may_build:
        return "skipped"

    tmp = os.path.join(directory, f"{TMP_PREFIX}{uuid.uuid4().hex}.tmp")
    try:
        sources = [os.path.join(directory, name) for name in sorted(late + retried)]
        built = build(store, day, sources, tmp, previous=compact if local else None)
        install(directory, tmp, built)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("compaction: cannot remove %s", tmp, exc_info=True)
    logger.info(
        "compaction %s %s: %d rows in %d row groups from %d parts, %d unreadable",
        store.name,
        day,
        built.rows,
        len(built.groups),
        len(built.parts),
        len(built.unreadable),
    )
    return "compacted"


def _compact_day(store: Store, root: str, day: date, now_ms: int, may_build: bool) -> str:
    try:
        with lock(root):
            return _compact_locked(store, root, day, now_ms, may_build)
    except CompactionError as exc:
        logger.warning("compaction %s %s: %s; nothing marked", store.name, day, exc)
    except Exception:
        logger.exception("compaction %s %s failed; nothing marked", store.name, day)
    return "error"


def compact_day(store: Store, root: str, day: date, *, now_ms: int) -> str:
    """Bring one day partition up to date, under lock(root).

    Returns "compacted" (a compact was built and installed), "repaired" (the
    sidecar disagreed with the compact's footer and was rewritten from it, and
    no part was late), "unchanged", "skipped" (the day is not complete, or its
    compact lives only in object storage and late parts stay raw), or "error"
    (logged; nothing marked, and the tmp file removed).

    Late parts are raw parts neither compacted nor known unreadable; when
    there are any, the known unreadable parts still on disk are retried with
    them, and the local compact, if any, is the previous one.
    """
    return _compact_day(store, root, day, now_ms, may_build=True)


def _subdirs(path: str, pattern: re.Pattern[str]) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return [e.path for e in entries if pattern.fullmatch(e.name) and e.is_dir()]
    except (FileNotFoundError, NotADirectoryError):
        return []


def _partition_days(root: str) -> list[date]:
    """The days of root's partition directories, newest first."""
    days = {
        day
        for year in _subdirs(root, _YEAR)
        for month in _subdirs(year, _MONTH)
        for directory in _subdirs(month, _DAY)
        if (day := day_of_dir(directory)) is not None
    }
    return sorted(days, reverse=True)


def sweep(store: Store, root: str | None = None, *, now_ms: int | None = None, max_days: int | None = None) -> dict:
    """compact_day for every day partition of ``root`` (the store's root by default), newest first.

    Compacts at most ``max_days`` days per call (_MAX_COMPACT_PER_SWEEP by
    default); days past that are counted skipped and wait for the next sweep,
    while repairs and unchanged days are not capped. Returns the counts of
    each result, errors as "errors". Never raises.
    """
    counts = {"compacted": 0, "repaired": 0, "skipped": 0, "unchanged": 0, "errors": 0}
    try:
        root = store.root() if root is None else root
        now = int(time.time() * 1000) if now_ms is None else now_ms
        cap = _MAX_COMPACT_PER_SWEEP if max_days is None else max_days
        days = _partition_days(root)
    except Exception:
        logger.exception("compaction %s: cannot list the partitions", store.name)
        counts["errors"] += 1
        return counts
    for day in days:
        result = _compact_day(store, root, day, now, may_build=counts["compacted"] < cap)
        counts["errors" if result == "error" else result] += 1
    if counts["compacted"] or counts["repaired"] or counts["errors"]:
        logger.info("compaction %s: %s", store.name, counts)
    return counts
