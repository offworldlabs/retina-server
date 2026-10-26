"""What the tracks and ADS-B archive stores read by, held once.

services/tracks_store.py and services/adsb_store.py read stores of one shape:
raw parts per UTC day of flush, ``<root>/year=YYYY/month=MM/day=DD/part-*``,
each named for the second it was flushed, and, once a day is complete, a
compact beside them (services/compaction.py) or, once that has left local
disk, in object storage (services/cold_store.py). Each store keeps what is its
own: its schema, how a row group is decoded, its compact's manifest check, its
publication rules, and its log messages. This module holds the rest:

- Cache, Warnings and IOPool: the LRU of footers and row groups, the warnings
  given once per file version, and the threads one call reads files on; a
  store has one of each;
- the days a window reads (window_days): its first day through the day of its
  end plus the store's slack, since a row is filed under the day its batch was
  flushed, which can be the day after it was taken;
- a raw part's flush time from its name (flushed_ms), and the parts a window
  skips by it: a part flushed before the window opened holds no row of it;
- cached: one cached read of a file, telling a file that has gone (VANISHED)
  from one that cannot be read (None, warned once);
- plan_day, what one day partition is read from, and read_window, which reads
  a window's days through it;
- capped: the row cap.

A raw part can vanish between a day's listing and its read: the lifecycle
(services/tasks/archive_lifecycle.py) deletes uploaded parts. It deletes a raw
part of these stores only once a fresh read of the day's sidecar lists it and
the compact that sidecar describes is proven, so the rows of a part that has
gone are in a compact the day now has, on local disk or in object storage.
read_window therefore plans such a day again and reads every source of it
again, dropping what it had read of the day, so no row is missed and none is
read twice. A day that lost nothing is not read again. A day that still loses
a part after REPLANS re-plans raises DayChangedError, which the routes answer
with 503, instead of answering without the part's rows. A file that is there
but cannot be read is skipped, warned once per version.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.compute

from services import cold_store, compaction

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The instants a partition day can be computed for.
_MIN_MS = (datetime(1, 1, 2, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)
_MAX_MS = (datetime(9999, 12, 31, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)

# Footer reads and row-group decodes of one call run on this many threads.
# Opening a file holds the GIL for most of its cost, so more do not help.
IO_THREADS = 4

# The file versions and row groups a store has warned about, the longest
# unused dropped first past this many. Kept out of the cache, whose bound can
# be 0 and whose evictions would otherwise bring a warning back on every poll.
WARNED_MAX = 4096

# How many times a day that lost a raw part while it was read is planned and
# read again. The first re-plan finds the part's rows in the compact whose
# sidecar let the lifecycle delete it. The second covers the day compacted
# again meanwhile (a late part, or scripts/compact_offloaded_days.py) and that
# part deleted during the first re-plan's read. A day still losing parts after
# that is changing faster than it can be read, and fails the call.
REPLANS = 2


class DayChangedError(cold_store.ColdStoreError):
    """A day whose raw parts kept vanishing while it was read; its message is safe to show a visitor."""

    def __init__(self, day: date) -> None:
        super().__init__(day)
        self.args = (f"The archived day {day.isoformat()} changed while it was read; try again.",)


class Vanished:
    """A file listed for a read and gone when the read came to it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "VANISHED"


VANISHED = Vanished()


# ── Cache, warnings, threads ─────────────────────────────────────────────────

MISS = object()


class Cache:
    """An LRU bounded by the bytes its entries are said to hold, ``bound()`` read on
    every put. Thread-safe; values are computed outside the lock, so two calls may
    compute one twice."""

    def __init__(self, bound: Callable[[], int]) -> None:
        self._bound = bound
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self._bytes = 0

    def get(self, key: tuple) -> Any:
        """The value kept for ``key``, or MISS."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return MISS
            self._entries.move_to_end(key)
            return entry[0]

    def put(self, key: tuple, value: Any, size: int) -> None:
        """Keep ``value`` as ``size`` bytes, evicting the least recently used past
        the bound; an entry larger than the bound is not kept."""
        bound = self._bound()
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


class Warnings:
    """The warnings a store gives once per key (a file version, or one of its row
    groups), on its own logger, remembering at most ``limit()`` keys."""

    def __init__(self, logger: logging.Logger, limit: Callable[[], int]) -> None:
        self.logger = logger
        self._limit = limit
        self._lock = threading.Lock()
        # The keys warned about, the most recently failed last.
        self.seen: OrderedDict[tuple, None] = OrderedDict()

    def warn_once(self, key: tuple, message: str, *args: object) -> None:
        """Log ``message`` with the exception being handled, the first time ``key`` fails."""
        with self._lock:
            if key in self.seen:
                self.seen.move_to_end(key)
                return
            self.seen[key] = None
            while len(self.seen) > self._limit():
                self.seen.popitem(last=False)
        self.logger.warning(message, *args, exc_info=True)

    def clear(self) -> None:
        with self._lock:
            self.seen.clear()


class IOPool:
    """The IO_THREADS threads a store's calls read files on, started on first use."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    def map[T, R](self, fn: Callable[[T], R], items: list[T]) -> list[R]:
        """``fn`` over ``items`` in order, on the threads when there is more than one.

        Returns, or raises the first item's exception, only once every item has
        finished, so nothing still reads a file the caller then closes or releases.
        """
        if len(items) < 2:
            return [fn(item) for item in items]
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=IO_THREADS, thread_name_prefix=self._name)
            pool = self._pool
        futures = [pool.submit(fn, item) for item in items]
        wait(futures)
        return [future.result() for future in futures]


# ── Files ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class File:
    """A listed raw part, or an opened compact, and the version of it a cache entry
    is for; ``flushed_ms`` is a raw part's flush time when its name gives one."""

    path: str
    size: int
    mtime_ns: int
    flushed_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Compact[G]:
    """A compact's footer as a store reads it: its row groups, and the raw part
    names its manifest says it holds."""

    groups: tuple[G, ...]
    parts: frozenset[str]


@dataclass(frozen=True, slots=True)
class Opened[G]:
    """A compact as one call reads it: the version the call opened, the
    handle every read of it goes through, and that version's footer."""

    file: File
    handle: pa.NativeFile
    compact: Compact[G]


def stat(path: str, flushed_ms: int | None, logger: logging.Logger, message: str) -> File | None | Vanished:
    """A raw part's current version; VANISHED when it has gone, None when it cannot
    be stat'ed, logged as ``message`` with the path."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return VANISHED
    except OSError:
        logger.warning(message, path, exc_info=True)
        return None
    return File(path, st.st_size, st.st_mtime_ns, flushed_ms)


def cached[T](
    cache: Cache,
    warnings: Warnings,
    key: tuple,
    read: Callable[[], T],
    size: Callable[[T], int],
    message: str,
    *args: object,
) -> T | None | Vanished:
    """The value ``cache`` keeps for ``key``, else ``read()``'s, then kept as ``size`` of it bytes.

    VANISHED when the file read has gone (FileNotFoundError); None when it
    cannot be read, warned as ``message`` once per ``key``.
    """
    value = cache.get(key)
    if value is not MISS:
        return value
    try:
        value = read()
    except FileNotFoundError:
        return VANISHED
    except Exception:
        warnings.warn_once(key, message, *args)
        return None
    cache.put(key, value, size(value))
    return value


def finite_or_null(column: pa.ChunkedArray) -> pa.ChunkedArray:
    """``column`` with every value that is not finite made null."""
    return pc.if_else(pc.is_finite(column), column, pa.scalar(None, column.type))


# ── Days ─────────────────────────────────────────────────────────────────────


def day_start_ms(day: date) -> int:
    """The UTC midnight that opens ``day``, in epoch ms."""
    return (datetime(day.year, day.month, day.day, tzinfo=UTC) - _EPOCH) // timedelta(milliseconds=1)


def day_of(ms: int) -> date:
    """The UTC day of an instant, clamped to the days a partition can be named for."""
    return (_EPOCH + timedelta(milliseconds=min(max(ms, _MIN_MS), _MAX_MS))).date()


def days(first: date, last: date) -> Iterator[date]:
    """first..last, inclusive."""
    day = first
    while day <= last:
        yield day
        if day == date.max:
            return
        day += timedelta(days=1)


def window_days(from_ms: int, to_ms: int, slack_ms: int) -> Iterator[date]:
    """The day partitions a window from_ms <= t < to_ms reads: from its first day
    through the day of its end plus ``slack_ms``, since a row is filed under the
    day its batch was flushed, which can be the day after it was taken."""
    return days(day_of(from_ms), day_of(to_ms - 1 + slack_ms))


def flushed_ms(day: date, name: str, pattern: re.Pattern[str]) -> int | None:
    """When a raw part of ``day`` was flushed, to the second, from its name:
    ``pattern`` matches the whole name and captures its hour, minute and second.
    None for any other name, which is still read but cannot be skipped by it."""
    match = pattern.fullmatch(name)
    if match is None:
        return None
    hour, minute, second = (int(group) for group in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return None
    return day_start_ms(day) + ((hour * 60 + minute) * 60 + second) * 1000


def flushed_before(flushed: int | None, from_ms: int) -> bool:
    """Whether a part flushed at ``flushed`` holds no row of a window opening at
    ``from_ms``: every row was taken before the second after its file's flush."""
    return flushed is not None and flushed + 1000 <= from_ms


def fetchable(store: compaction.Store, day: date, now_ms: int) -> bool:
    """Whether the day's compact can be read from object storage: a store is configured and the day is complete."""
    return cold_store.enabled() and compaction.is_complete(store, day, now_ms)


# ── Reading a window ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Layout[G]:
    """One store as plan_day and read_window read it, row groups of type ``G``.

    A store builds it on every call from its module's settings and functions
    as they are then, so its root and a function replaced since import are the
    ones used.
    """

    store: compaction.Store
    root: str
    # How far past a window's end a row can still be filed under the next day.
    slack_ms: int
    # A raw part's whole name, capturing the hour, minute and second of its flush.
    flushed: re.Pattern[str]
    logger: logging.Logger
    # Starts the store's log messages: "tracks archive".
    label: str
    pool: IOPool
    # A day partition's raw part names, sorted, from its directory and day; [] when it has none.
    part_names: Callable[[str, date], list[str]]
    # The compact at a path, of a day, open until the stack closes, with its footer;
    # None when there is none or it cannot be used.
    open_compact: Callable[[str, date, ExitStack], Opened[G] | None]
    # A raw part's current version, from its path and flush time.
    stat: Callable[[str, int | None], File | None | Vanished]
    # A raw part's row groups, from its footer.
    footer: Callable[[File], tuple[G, ...] | None | Vanished]
    # One row group decoded, of a raw part or of an opened compact, by index and footer.
    read_group: Callable[[Opened[G] | File, int, G], pa.Table | None | Vanished]


@dataclass(frozen=True, slots=True)
class DayPlan[G]:
    """What a read of one day partition reads: ``compact``, open until the stack
    plan_day was given closes, or None; and ``parts``, the raw part names read
    beside it (or alone), sorted, all in ``directory``."""

    day: date
    directory: str
    compact: Opened[G] | None
    parts: list[str]


def plan_day[G](layout: Layout[G], day: date, now_ms: int, stack: ExitStack) -> DayPlan[G]:
    """What one day partition is read from, as of now; ``now_ms`` decides whether the day is complete.

    With a usable local compact: it, and the raw parts its footer manifest does
    not list. Without one: what compaction.read_plan says, fetchable when an
    object store is configured and the day is complete: the compact in object
    storage (checked against the sidecar, and against the record of an upload
    under way when there is one) and the raw parts of the plan's that the
    copy's own footer manifest does not list, or the raw parts. Reading by the
    copy's footer keeps the answer exact whichever compact the store held. A
    plan with no copy from object storage (the store has none) reads every raw
    part. Raises cold_store.ColdStoreError when a day kept in object storage
    cannot be loaded, or its compact.json or record is broken.
    """
    directory = compaction.day_dir(layout.root, day)
    names = layout.part_names(directory, day)
    opened = layout.open_compact(os.path.join(directory, compaction.COMPACT_NAME), day, stack)
    if opened is not None:
        return DayPlan(day, directory, opened, [name for name in names if name not in opened.compact.parts])
    plan = compaction.read_plan(layout.store, directory, day, names, fetchable=fetchable(layout.store, day, now_ms))
    if plan.broken:
        raise cold_store.ColdStoreError(day)
    cold = _open_cold(layout, day, plan.sidecar, plan.pending, stack) if plan.cold else None
    if cold is not None:
        return DayPlan(day, directory, cold, [name for name in plan.names if name not in cold.compact.parts])
    return DayPlan(day, directory, None, names)


def _open_cold[G](
    layout: Layout[G],
    day: date,
    expected: compaction.Manifest | None,
    pending: compaction.Manifest | None,
    stack: ExitStack,
) -> Opened[G] | None:
    """The day's compact from object storage, checked against ``expected`` (its sidecar) and
    ``pending`` (its record of an upload under way) when given, held and open until ``stack``
    closes; None if there is none."""
    path = stack.enter_context(cold_store.open_day(layout.store, day, expected=expected, pending=pending))
    if path is None:
        return None
    opened = layout.open_compact(path, day, stack)
    if opened is None:
        layout.logger.warning("%s: the local copy of %s from object storage cannot be read", layout.label, day)
        raise cold_store.ColdStoreError(day)
    return opened


def read_window[G](layout: Layout[G], from_ms: int, to_ms: int, wanted: Callable[[G], bool]) -> list[pa.Table]:
    """The decoded row groups a window from_ms <= t < to_ms reads, by ``wanted`` of their footers.

    Day by day (window_days), each planned by plan_day: its compact, then the
    raw parts beside it in name order, less those flushed before the window
    opened. Raw parts are stat'ed on the calling thread; footers and row
    groups are read on the store's threads. Compacts stay open, and days from
    object storage held, until every row group is decoded.

    A day of which a raw part has gone by the time it is stat'ed, its footer
    read or a row group decoded is planned and read again, whole, up to
    REPLANS times; then DayChangedError. A file that cannot be read is
    skipped. Raises cold_store.ColdStoreError when a day kept in object
    storage cannot be loaded.
    """
    now_ms = int(time.time() * 1000)
    read: dict[date, list[pa.Table]] = {}
    pending = list(window_days(from_ms, to_ms, layout.slack_ms))
    with ExitStack() as stack:
        for _ in range(REPLANS + 1):
            stacks = {day: stack.enter_context(ExitStack()) for day in pending}
            lost = _read_days(
                layout, [plan_day(layout, day, now_ms, stacks[day]) for day in pending], from_ms, wanted, read
            )
            if not lost:
                break
            for day in lost:
                # Released before the day is planned again, which may fetch it anew.
                stacks[day].close()
            pending = [day for day in pending if day in lost]
        else:
            raise DayChangedError(pending[0])
        return [table for day in sorted(read) for table in read[day]]


def _read_days[G](
    layout: Layout[G],
    plans: list[DayPlan[G]],
    from_ms: int,
    wanted: Callable[[G], bool],
    read: dict[date, list[pa.Table]],
) -> set[date]:
    """Read the planned days into ``read``, in plan and source order; the days that lost a raw part, left out of it."""
    lost: set[date] = set()
    sources: list[tuple[date, Opened[G] | File]] = []
    for plan in plans:
        if plan.compact is not None:
            sources.append((plan.day, plan.compact))
        for name in plan.parts:
            flushed = flushed_ms(plan.day, name, layout.flushed)
            if flushed_before(flushed, from_ms):
                continue
            file = layout.stat(os.path.join(plan.directory, name), flushed)
            if isinstance(file, Vanished):
                lost.add(plan.day)
            elif file is not None:
                sources.append((plan.day, file))
    sources = [(day, source) for day, source in sources if day not in lost]

    def groups_of(item: tuple[date, Opened[G] | File]) -> tuple[G, ...] | None | Vanished:
        source = item[1]
        return source.compact.groups if isinstance(source, Opened) else layout.footer(source)

    footers = layout.pool.map(groups_of, sources)
    jobs: list[tuple[date, Opened[G] | File, int, G]] = []
    for (day, source), groups in zip(sources, footers):
        if isinstance(groups, Vanished):
            lost.add(day)
        elif groups is not None:
            jobs.extend((day, source, index, group) for index, group in enumerate(groups) if wanted(group))
    jobs = [job for job in jobs if job[0] not in lost]
    decoded = layout.pool.map(lambda job: layout.read_group(job[1], job[2], job[3]), jobs)
    tables: dict[date, list[pa.Table]] = {plan.day: [] for plan in plans}
    for (day, *_), table in zip(jobs, decoded):
        if isinstance(table, Vanished):
            lost.add(day)
        elif table is not None:
            tables[day].append(table)
    read.update((day, found) for day, found in tables.items() if day not in lost)
    return lost


def capped(table: pa.Table, cap: int) -> tuple[pa.Table, bool]:
    """The first ``cap`` rows of ``table``, and whether any were cut."""
    return table.slice(0, cap), table.num_rows > cap
