"""Per-bucket SNR of one node's detections, read back from the detection archive.

The server keeps no SNR time series of its own.  What it does keep is every
archived detection's ``snr_db``, in the Parquet files services/parquet_writer
writes, so the owner dashboard's trend is derived from those: two columns
(``frame_ts_ms``, ``snr_db``) of the node's files, plus the frames still
waiting in services/frame_processor's archive buffer for the next hourly flush.
Local retention is three days from a file's write (services/tasks/
archive_lifecycle.py), and a file is never written before the frames it holds,
so the longest window asked for, MAX_HOURS, is always on local disk.

The rules, each pinned by tests/test_snr_series.py:

- The window is ``[now - hours * 3600 s, now]``, both ends inclusive, on the
  detection's own ``frame_ts_ms``, and starts no earlier than ``since``, when
  the node became the caller's (core/auth.py, get_user_tenures): what it
  detected before then was its last owner's.  Detections outside it are left
  out.
- Buckets are BUCKET_S long and aligned to multiples of BUCKET_S since the
  epoch; ``t_ms`` is the bucket's start.  The first and last bucket of a window
  are usually partial.  A bucket with no detection is omitted, not sent empty.
- ``median_db`` and ``p90_db`` use linear interpolation between closest ranks
  (numpy's "linear" method, Hyndman & Fan type 7): with the bucket's n values
  sorted, the q-th percentile sits at index ``q * (n - 1)``, interpolated
  between its two neighbours.  The median of an even count is therefore the
  mean of the middle two.
- A row whose ``snr_db`` is null or not finite is not a detection here: it is
  left out of the counts, the percentiles and ``through_ms``.  orjson would
  send NaN and infinity as ``null`` where the contract promises a number.
- ``through_ms`` is the newest included detection's ``frame_ts_ms``, or None
  when nothing is included.  Points are oldest first.

A 72-hour window of a busy node is millions of rows, so they are never all in
memory at once: files, and runs of buffered frames, are read oldest first and
each bucket is reduced to its point, and its values dropped, as soon as
nothing still to be read can reach it.

The read is blocking file I/O.  An async route awaits ``snr_series_async``,
which answers a cache hit in place and runs the read on a worker thread
otherwise; ``snr_series`` itself must never be called on the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from services import frame_processor, storage

logger = logging.getLogger(__name__)

BUCKET_S = 300
MAX_HOURS = 72
_BUCKET_MS = BUCKET_S * 1000
_COLUMNS = ["frame_ts_ms", "snr_db"]

# Reuse an answer for this long.  The newest data is an hourly flush or a live
# buffer that grows at about a frame a second, so a minute is invisible on a
# 5-minute bucket and spares the archive read for every poll and every tab.
_CACHE_TTL_S = 60.0
# One answer per (node, hours, tenure).  A 72-hour answer is 865 points, about
# 260 kB as dicts, so this holds the cache to about 17 MB at worst and far less
# for the usual one window per node.
_CACHE_MAX = 64

# How often to look again when an archive flush of the node overlaps the read
# (see _collect), and how long to wait first.  The waits double: 0.05 s to
# 0.4 s, 0.75 s in all, against a flush that writes one node's hour in tens of
# milliseconds.
_READ_ATTEMPTS = 5
_RETRY_WAIT_S = 0.05
# Unflushed frames are read in runs of this many, each run a source of its own
# like a file: ten minutes at a frame a second, so a full buffer is never
# converted at once.
_BUFFER_CHUNK = 600
_NO_ROWS = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_cache_lock = threading.Lock()
# Keyed on the tenure as well, so an answer read for one owner is never handed
# to the next one the node passes to within the TTL.
_cache: OrderedDict[tuple[str, int, int], tuple[float, dict]] = OrderedDict()


def _reset_for_tests() -> None:
    """Drop every cached answer.  Tests only."""
    with _cache_lock:
        _cache.clear()


def _since_ms(since: datetime) -> int:
    """`since` in epoch milliseconds, rounded up: a detection stamped in the
    millisecond before it is not after it."""
    return -((_EPOCH - since) // timedelta(milliseconds=1))


def _check_hours(hours: int) -> None:
    # bool is an int; True hours is a caller bug, not a window.
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"hours must be an integer from 1 to {MAX_HOURS}, got {hours!r}")


# ── Cache ────────────────────────────────────────────────────────────────────


def _copy(answer: dict) -> dict:
    """A copy the caller may change without changing the cached answer."""
    return {**answer, "points": [dict(p) for p in answer["points"]]}


def _cache_get(key: tuple[str, int, int]) -> dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if entry[0] <= time.monotonic():
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return _copy(entry[1])


def _cache_put(key: tuple[str, int, int], answer: dict) -> None:
    with _cache_lock:
        now = time.monotonic()
        for stale in [k for k, (expires, _) in _cache.items() if expires <= now]:
            del _cache[stale]
        _cache[key] = (now + _CACHE_TTL_S, answer)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


# ── Flushed detections: the node's Parquet files ─────────────────────────────


def _partition_files(base: Path, node_id: str, start_ms: int, now_ms: int) -> list[Path]:
    """The node's archive files from every UTC day the window touches.

    Files are partitioned by the UTC day they were *written* (the flush), not
    by the frames inside them.  A flush only writes frames that have already
    arrived, so a file is written no earlier than the frames it holds and no
    later than now: a frame inside the window can only be in a file written on
    a day from the window's first to its last.  ``frame_ts_ms`` is the node's
    clock, though, and a node running ahead of the server's by more than the
    flush delay can land a frame in the day before; such a frame is missed at
    the window's oldest edge, which is accepted.
    """
    # A node id is one path component in the archive.  Anything else would
    # reach outside the node's own directory.
    if not node_id or node_id in (".", "..") or "/" in node_id or "\\" in node_id:
        return []
    day = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
    last = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    paths: list[Path] = []
    while day <= last:
        node_dir = base / f"year={day:%Y}" / f"month={day:%m}" / f"day={day:%d}" / f"node_id={node_id}"
        try:
            # part-*.parquet only: the writer's temporaries are .parquet-*.tmp
            # and the lifecycle's upload sentinels end .parquet.uploaded.
            paths.extend(sorted(node_dir.glob("part-*.parquet")))
        except (OSError, ValueError):
            pass
        day += timedelta(days=1)
    return paths


def _time_range(path: Path) -> tuple[int, int] | None:
    """Oldest and newest ``frame_ts_ms`` in a file, from its footer statistics alone.

    None when the footer cannot say (no statistics, or unreadable); the file
    is then read without knowing its range.
    """
    try:
        meta = pq.read_metadata(path)
        lo = hi = None
        for g in range(meta.num_row_groups):
            group = meta.row_group(g)
            for c in range(group.num_columns):
                column = group.column(c)
                if column.path_in_schema != "frame_ts_ms":
                    continue
                stats = column.statistics
                if stats is None or not stats.has_min_max:
                    return None
                lo = stats.min if lo is None else min(lo, stats.min)
                hi = stats.max if hi is None else max(hi, stats.max)
    except (OSError, pa.ArrowException):
        return None
    if lo is None or hi is None:
        return None
    return int(lo), int(hi)


def _read_file(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """``frame_ts_ms`` and ``snr_db`` of one archive file, or None if unreadable.

    ParquetFile reads the one file as it is: no Hive partition discovery, so
    the ``node_id=`` directory cannot clash with the column of the same name
    (see storage._read_parquet_as_legacy_json).  A null ``snr_db`` arrives as
    NaN and is dropped with the other non-finite values.
    """
    try:
        with pq.ParquetFile(path) as parquet:
            table = parquet.read(columns=_COLUMNS)
        ts = table.column("frame_ts_ms").to_numpy(zero_copy_only=False)
        snr = table.column("snr_db").to_numpy(zero_copy_only=False)
    except FileNotFoundError:
        # Pruned by the archive lifecycle after the listing; anything it
        # prunes is older than any window.
        return None
    except (OSError, KeyError, pa.ArrowException):
        logger.warning("snr series: skipping unreadable archive file %s", path, exc_info=True)
        return None
    return ts, snr


# ── Unflushed detections: frame_processor's archive buffer ───────────────────
#
# The buffer is frame_processor._archive_buffer (node id -> frames, oldest
# first), guarded by _archive_buffer_lock.  Frame workers append to it under
# that lock as each frame is processed; _flush_archive_node takes a copy of a
# node's frames under it, writes them to a file with the lock released, and
# then takes the lock again to drop the prefix it wrote.  It marks the node in
# _archive_inflight for the whole of that cycle.  Reading the buffer here costs
# what the flusher's own copy costs: one list copy under the lock, no I/O.
#
# Read naively, buffer and files can overlap.  A flush that has published its
# file but not yet dropped those frames from the buffer shows them twice, and
# one that runs between reading the buffer and listing the files moves frames
# into a file the listing then sees.  _collect therefore reads the buffer, then
# lists the files, then checks that no flush of the node touched either in
# between.  The files' contents can be read after that check: a published
# archive file never changes, so the listing fixes what will be counted.


def _buffer_snapshot(node_id: str) -> list[dict] | None:
    """The node's unflushed frames, or None while a flush of the node is in flight."""
    with frame_processor._archive_buffer_lock:
        if node_id in frame_processor._archive_inflight:
            return None
        # .get, never [...]: the buffer is a defaultdict, and a key created by
        # a read would never go away (only a flush that wrote frames pops one).
        return list(frame_processor._archive_buffer.get(node_id, ()))


def _buffer_unchanged(node_id: str, frames: list[dict]) -> bool:
    """Whether no flush of the node can have moved ``frames`` to disk since they were read.

    A flush always writes from the head of the buffer and then drops what it
    wrote, so a buffer that still starts with the same frame object has lost
    nothing to a completed flush, and an empty inflight mark rules out one
    that has published a file but not yet dropped its frames.  An empty
    snapshot can overlap nothing.
    """
    with frame_processor._archive_buffer_lock:
        if node_id in frame_processor._archive_inflight:
            return False
        if not frames:
            return True
        buf = frame_processor._archive_buffer.get(node_id)
        return bool(buf) and buf[0] is frames[0]


def _buffer_chunks(frames: list[dict], start_ms: int, now_ms: int) -> list[list[tuple[int, object]]]:
    """The window's buffered frames as ``(frame_ts_ms, snr)`` pairs, in runs of _BUFFER_CHUNK.

    The frame time is the writer's own, ``int(timestamp or 0)``
    (parquet_writer._flatten); a frame whose time does not convert, which
    the writer could not archive either, is skipped.
    """
    pairs: list[tuple[int, object]] = []
    for frame in frames:
        try:
            frame_ts = int(frame.get("timestamp", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if start_ms <= frame_ts <= now_ms:
            pairs.append((frame_ts, frame.get("snr")))
    return [pairs[i : i + _BUFFER_CHUNK] for i in range(0, len(pairs), _BUFFER_CHUNK)]


def _buffer_rows(chunk: list[tuple[int, object]]) -> tuple[np.ndarray, np.ndarray]:
    """``(frame_ts_ms, snr_db)`` rows of buffered frames, as the writer would store them.

    Mirrors parquet_writer._flatten: each detection's SNR is ``float(value)``,
    null when it is missing or does not convert.  Null and non-finite values
    are dropped here, as _in_window drops them from a file.
    """
    ts: list[int] = []
    snr: list[float] = []
    for frame_ts, values in chunk:
        if not isinstance(values, list | tuple):
            continue
        for value in values:
            try:
                db = float(value) if value is not None else None
            except (TypeError, ValueError, OverflowError):
                db = None
            if db is not None and math.isfinite(db):
                ts.append(frame_ts)
                snr.append(db)
    return np.array(ts, dtype=np.int64), np.array(snr, dtype=np.float64)


def _collect(node_id: str, start_ms: int, now_ms: int) -> tuple[list[Path], list[dict], bool]:
    """The node's files and unflushed frames, and whether the two were read consistently.

    Retries while a flush of the node overlaps the read.  If one is still in
    the way after _READ_ATTEMPTS, gives the files alone, which can miss the
    unflushed frames but never counts one twice; the caller does not cache
    that answer.
    """
    base = Path(storage._LOCAL_ARCHIVE_DIR)
    for attempt in range(_READ_ATTEMPTS):
        if attempt:
            time.sleep(_RETRY_WAIT_S * 2 ** (attempt - 1))
        frames = _buffer_snapshot(node_id)
        if frames is None:
            continue
        paths = _partition_files(base, node_id, start_ms, now_ms)
        if _buffer_unchanged(node_id, frames):
            return paths, frames, True
    logger.warning(
        "snr series for %s: an archive flush overlapped %d reads; answering from flushed files only",
        node_id,
        _READ_ATTEMPTS,
    )
    return _partition_files(base, node_id, start_ms, now_ms), [], False


# ── The series ───────────────────────────────────────────────────────────────


def _in_window(ts: np.ndarray, snr: np.ndarray, start_ms: int, now_ms: int) -> tuple[np.ndarray, np.ndarray]:
    snr = snr.astype(np.float64, copy=False)
    keep = np.isfinite(snr) & (ts >= start_ms) & (ts <= now_ms)
    return ts[keep].astype(np.int64, copy=False), snr[keep]


def _file_rows(path: Path, start_ms: int, now_ms: int) -> tuple[np.ndarray, np.ndarray]:
    raw = _read_file(path)
    return _NO_ROWS if raw is None else _in_window(*raw, start_ms, now_ms)


class _Series:
    """Buckets filled source by source, each reduced to its point once complete."""

    def __init__(self) -> None:
        self.open: dict[int, list[np.ndarray]] = {}
        self.points: list[dict] = []
        self.through_ms: int | None = None

    def add(self, ts: np.ndarray, snr: np.ndarray) -> None:
        if not ts.size:
            return
        newest = int(ts.max())
        self.through_ms = newest if self.through_ms is None else max(self.through_ms, newest)
        buckets = ts // _BUCKET_MS
        order = np.argsort(buckets, kind="stable")
        buckets = buckets[order]
        snr = snr[order]
        edges = np.flatnonzero(np.diff(buckets)) + 1
        for bucket, values in zip(buckets[np.concatenate(([0], edges))], np.split(snr, edges)):
            self.open.setdefault(int(bucket), []).append(values)

    def close_before(self, horizon_ms: float) -> None:
        """Reduce every bucket that ends at or before ``horizon_ms``."""
        for bucket in sorted(b for b in self.open if (b + 1) * _BUCKET_MS <= horizon_ms):
            values = np.concatenate(self.open.pop(bucket))
            median, p90 = np.percentile(values, (50, 90), method="linear")
            self.points.append(
                {
                    "t_ms": bucket * _BUCKET_MS,
                    "median_db": float(median),
                    "p90_db": float(p90),
                    "count": int(values.size),
                }
            )


def _compute(node_id: str, hours: int, now_ms: int, since_ms: int) -> tuple[dict, bool]:
    start_ms = max(now_ms - hours * 3_600_000, since_ms)
    paths, frames, consistent = _collect(node_id, start_ms, now_ms)
    # Every file and every run of buffered frames is a source, read oldest
    # first by its oldest frame, so after each one every bucket that ends
    # before the next source's oldest frame is complete.  A file whose footer
    # cannot say sorts first and holds nothing up; one that lies wholly outside
    # the window is never read.
    sources: list[tuple[float, int, Callable[[], tuple[np.ndarray, np.ndarray]]]] = []
    for path in paths:
        span = _time_range(path)
        if span is not None and (span[1] < start_ms or span[0] > now_ms):
            continue
        oldest = span[0] if span is not None else -math.inf
        sources.append((oldest, len(sources), partial(_file_rows, path, start_ms, now_ms)))
    for chunk in _buffer_chunks(frames, start_ms, now_ms):
        sources.append((min(ts for ts, _ in chunk), len(sources), partial(_buffer_rows, chunk)))
    sources.sort(key=lambda s: (s[0], s[1]))
    series = _Series()
    for i, (_, _, rows) in enumerate(sources):
        series.add(*rows())
        series.close_before(sources[i + 1][0] if i + 1 < len(sources) else math.inf)
    series.points.sort(key=lambda p: p["t_ms"])
    answer = {
        "hours": hours,
        "bucket_s": BUCKET_S,
        "through_ms": series.through_ms,
        "points": series.points,
    }
    return answer, consistent


def snr_series(node_id: str, hours: int, *, since: datetime, now_ms: int | None = None) -> dict:
    """The contract's SnrSeries for one node over the last ``hours`` hours,
    from ``since`` at the earliest.

    Blocking: call it from a worker thread, or await snr_series_async.  The
    caller proves ownership first, and when it began; the answer is cached per
    (node, hours, since) for up to _CACHE_TTL_S, so it is only ever served for
    that node, window and tenure.  ``now_ms`` pins the clock instead of reading
    it, and such a call neither reads nor fills the cache, whose answers belong
    to the wall clock.  Raises ValueError for hours outside 1..MAX_HOURS.
    """
    _check_hours(hours)
    since_ms = _since_ms(since)
    key = (node_id, hours, since_ms)
    if now_ms is None:
        hit = _cache_get(key)
        if hit is not None:
            return hit
        answer, consistent = _compute(node_id, hours, int(time.time() * 1000), since_ms)
        if consistent:
            _cache_put(key, answer)
            return _copy(answer)
        return answer
    return _compute(node_id, hours, int(now_ms), since_ms)[0]


async def snr_series_async(node_id: str, hours: int, *, since: datetime) -> dict:
    """snr_series for an async route: a cache hit straight away, the archive read on a worker thread."""
    _check_hours(hours)
    hit = _cache_get((node_id, hours, _since_ms(since)))
    if hit is not None:
        return hit
    return await asyncio.to_thread(snr_series, node_id, hours, since=since)
