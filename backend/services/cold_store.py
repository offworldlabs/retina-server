"""Read-through of archived days whose compact lives only in object storage.

Once the lifecycle has deleted a day's local compact (services/compaction.py),
the compact is kept only in object storage, under
compaction.object_key(store, day, COMPACT_NAME). open_day() fetches it on first
use into a bounded local cache and yields the path of the local copy, so a
reader answers from local Parquet as it does for any other day.

The cache is ``<_CACHE_DIR>/<store name>/YYYY-MM-DD.parquet``, one file per
day, under coverage_data/cold_cache: outside the lifecycle's roots
(coverage_data/archive, coverage_data/tracks, coverage_data/adsb), so nothing
but this module uploads, supersedes or deletes a file in it. A fetch writes
``.fetch-<uuid hex>.tmp`` in the same directory, a name the cache never takes
for a day.

What open_day yields, in order:

- None, without asking the object store, when no store is configured or the
  day is not complete yet (compaction.is_complete: today, the future).
- The cached copy, without asking the object store, when it is known good:
  validated by this process, which then records its size, mtime and sha256,
  or, for a file an earlier process left, hashed and validated once on first
  use. When ``expected`` (the day's local sidecar) is given, the copy must also
  have its size and sha256; one that does not is an older compact, and it is
  discarded and fetched again.
- Otherwise the result of one fetch, under a lock per day: concurrent callers
  for a day wait for that one fetch and then share its file, and callers for
  different days never wait on each other. A caller waits at most _LOCK_WAIT_S
  for the day's lock; one that cannot take it in time raises ColdStoreError
  without fetching, and the fetch it waited on goes on for the callers after it.

A fetch writes the object's bytes to a tmp file, fsyncs it and validates it
before renaming it over the day's path. Valid means: when ``expected`` is
given, its size and sha256; always, that it opens as Parquet and its footer
manifest names the store and the day and agrees with the file's row count and
row groups. A file that fails is removed, logged, and raises ColdStoreError, a
failure remembered as below. Tmp files left by a crash are removed when a
process first uses the directory (the backend runs as one process).

A missing object (download_bytes returned None, which also stands for any
error of the store) means:

- with no ``expected``, that the day has no compact: open_day yields None and
  remembers the miss for TRACKS_COLD_MISS_TTL_S before asking again;
- with ``expected``, that the compact the sidecar promises cannot be had (gone,
  or the store is down): ColdStoreError, a failure.

A failure, an object missing as above or failing validation, is remembered for
_FAILURE_TTL_S against the sidecar its fetch was for (its size and sha256, or
none), so a burst of requests fails fast without each downloading the object:
open_day given the same sidecar, or again none, raises ColdStoreError for the
day without asking the store. A changed sidecar (the day compacted again) is
fetched at once. A fetch that succeeds forgets the failures it disproves: the
one without a sidecar and the one for the size and sha256 it fetched. A local
error (the cache directory cannot be written) is not remembered.

Bound: the days a store has cached are kept to store.cold_cache_max_bytes() in
total, evicting the least recently used first, after each fetch and whenever a
hold ends. The use order is kept in memory, seeded from file mtimes when a
process first uses the directory; a file's mtime is never touched, since the
reader keys what it decodes by path, size and mtime. A file is held from
open_day's yield until its block exits, and is never evicted, discarded or
replaced while held: the cache may exceed its bound meanwhile, and with a bound
of 0 a file is removed when its last hold ends. A discard that finds its file
held waits up to _HELD_WAIT_S for the holds to end, then raises ColdStoreError.

Thread-safe, for the readers' executor threads; no asyncio. The object-store
client is imported on one line, as _client, and nowhere else in this feature:
list_keys, download_bytes, download_to, upload_file and upload_bytes pass
through to it for the modules that need it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pyarrow.parquet as pq

from config import constants
from services import compaction
from services import r2_client as _client
from services.compaction import COMPACT_NAME, Manifest, Store

logger = logging.getLogger(__name__)

# Read on every call, so a test can point it elsewhere.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "cold_cache")

# How long a day whose fetch failed keeps failing, for the same sidecar, without asking the store.
_FAILURE_TTL_S = 30.0
# How long a discard waits for the requests holding its file.
_HELD_WAIT_S = 30.0
# How long a request waits for another request's fetch of the same day.
_LOCK_WAIT_S = 20.0
# Misses and failures remembered at most; past it the oldest are forgotten.
_MEMORY_MAX = 4096
_HASH_CHUNK = 1 << 20
_TMP_PREFIX = ".fetch-"
_TMP = re.compile(re.escape(_TMP_PREFIX) + r"[0-9a-f]{32}\.tmp")
_DAY_FILE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.parquet")

# The clock misses and failures expire on.
_clock = time.monotonic


class ColdStoreError(Exception):
    """A day that could not be loaded from object storage; its message is safe to show a visitor."""

    def __init__(self, day: date) -> None:
        super().__init__(f"The archived day {day.isoformat()} could not be loaded from cold storage.")
        self.day = day


class _Invalid(Exception):
    """A file that must not be served; the message says why."""


@dataclass(frozen=True, slots=True)
class _Record:
    """A day file as this process validated it."""

    size: int
    mtime_ns: int
    sha256: str


@dataclass(slots=True)
class _DayLock:
    lock: threading.Lock
    users: int = 0


# Everything below is guarded by _lock.
_lock = threading.Lock()
# Notified whenever a file's last hold ends.
_released = threading.Condition(_lock)
# Day file path -> its fetch lock, while anyone uses it.
_day_locks: dict[str, _DayLock] = {}
# Directories whose leftover tmp files are removed and whose use order is seeded.
_prepared: set[str] = set()
# Directory -> {day file path: size}, least recently used first.
_order: dict[str, OrderedDict[str, int]] = {}
_records: dict[str, _Record] = {}
# Day file path -> how many open_day blocks hold it.
_holds: dict[str, int] = {}
# (store name, ISO day) -> the _clock time the memory ends, oldest first.
_misses: dict[tuple[str, str], float] = {}
# (store name, ISO day, the sidecar's (size, sha256) or None without one) -> the same.
_failures: dict[tuple[str, str, tuple[int | None, str | None] | None], float] = {}


def _reset_for_tests() -> None:
    """Forget every hold, record, use order, miss, failure and day lock."""
    with _lock:
        _day_locks.clear()
        _prepared.clear()
        _order.clear()
        _records.clear()
        _holds.clear()
        _misses.clear()
        _failures.clear()


# ── The object store ─────────────────────────────────────────────────────────


def enabled() -> bool:
    """True when an object store is configured."""
    return _client.is_enabled()


def list_keys(prefix: str) -> list[str]:
    """The keys under ``prefix``; [] with no store or on any error."""
    return _client.list_keys(prefix)


def download_bytes(key: str) -> bytes | None:
    """The object at ``key``; None when it is missing, on any error, or with no store."""
    return _client.download_bytes(key)


def upload_file(key: str, path: str, content_type: str) -> bool:
    """Upload a local file to ``key``; whether it was stored."""
    return _client.upload_file(key, path, content_type=content_type)


def upload_bytes(key: str, data: bytes, content_type: str) -> bool:
    """Upload ``data`` to ``key``; whether it was stored."""
    return _client.upload_bytes(key, data, content_type=content_type)


def download_to(key: str, path: str) -> bool:
    """Download ``key`` to ``path``: a tmp file in path's directory, fsync, rename. False if missing or failed."""
    data = _client.download_bytes(key)
    if data is None:
        return False
    directory = os.path.dirname(os.path.abspath(path))
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = _write_tmp(directory, data)
        os.replace(tmp, path)
        tmp = None
        _fsync_dir(directory)
    except OSError:
        logger.warning("cold store: cannot write %s to %s", key, path, exc_info=True)
        return False
    finally:
        if tmp is not None:
            _unlink_quietly(tmp)
    return True


# ── Reading a day ────────────────────────────────────────────────────────────


@contextmanager
def open_day(store: Store, day: date, *, expected: Manifest | None, now_ms: int | None = None) -> Iterator[str | None]:
    """Hold a validated local copy of a day's compact from object storage.

    Yields its path, held until the block exits, or None when no store is
    configured, the day is not complete at ``now_ms`` (the wall clock by
    default), or, with no ``expected``, the store has no compact of the day.
    ``expected`` is the day's local sidecar manifest when there is one: the
    copy then has its size and sha256. Raises ColdStoreError when the fetched
    object fails validation or the store does not return the object
    ``expected`` promises (either is remembered for _FAILURE_TTL_S), or when
    another request's fetch of the day outlasts _LOCK_WAIT_S. See the module
    docstring.
    """
    if not _client.is_enabled():
        yield None
        return
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not compaction.is_complete(store, day, now):
        yield None
        return
    directory = os.path.join(_CACHE_DIR, store.name)
    path = os.path.join(directory, f"{day.isoformat()}.parquet")
    if not _acquire(store, day, directory, path, expected):
        yield None
        return
    try:
        yield path
    finally:
        _release(store, directory, path)


def _acquire(store: Store, day: date, directory: str, path: str, expected: Manifest | None) -> bool:
    """Hold the day's file, fetching it if need be; False when the store has no compact of the day."""
    with _lock:
        try:
            _prepare(directory)
        except OSError:
            logger.warning("cold store: cannot use the cache directory %s", directory, exc_info=True)
            raise ColdStoreError(day) from None
        if _pin_known(directory, path, expected):
            return True
    with _day_lock(day, path):
        with _lock:
            if _pin_known(directory, path, expected):
                return True
        if _adopt(store, day, directory, path, expected):
            return True
        if _remembered(store, day, expected):
            return False
        return _fetch(store, day, directory, path, expected)


def _release(store: Store, directory: str, path: str) -> None:
    with _lock:
        count = _holds.get(path, 1) - 1
        if count:
            _holds[path] = count
        else:
            _holds.pop(path, None)
            _released.notify_all()
        _evict(directory, store.cold_cache_max_bytes())


def _prepare(directory: str) -> None:
    """On a process's first use of ``directory``: create it, remove leftover tmp files, seed the use order. Under _lock."""
    if directory in _prepared:
        return
    os.makedirs(directory, exist_ok=True)
    days: list[tuple[int, str, int]] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if _TMP.fullmatch(entry.name):
                _unlink_quietly(entry.path)
                logger.info("cold store: removed %s, left by an interrupted fetch", entry.path)
            elif _is_day_file(entry.name):
                st = entry.stat(follow_symlinks=False)
                days.append((st.st_mtime_ns, entry.path, st.st_size))
    _order[directory] = OrderedDict((path, size) for _, path, size in sorted(days))
    _prepared.add(directory)


def _is_day_file(name: str) -> bool:
    if not _DAY_FILE.fullmatch(name):
        return False
    try:
        date.fromisoformat(name.removesuffix(".parquet"))
    except ValueError:
        return False
    return True


@contextmanager
def _day_lock(day: date, path: str) -> Iterator[None]:
    """Hold the fetch lock of one day file; ColdStoreError if it cannot be taken within _LOCK_WAIT_S.

    The entry lives while anyone waits on it or holds it.
    """
    with _lock:
        entry = _day_locks.get(path)
        if entry is None:
            entry = _day_locks[path] = _DayLock(threading.Lock())
        entry.users += 1
    try:
        if not entry.lock.acquire(timeout=_LOCK_WAIT_S):
            logger.warning(
                "cold store: another request is still loading %s after %s s; not waiting", path, _LOCK_WAIT_S
            )
            raise ColdStoreError(day)
        try:
            yield
        finally:
            entry.lock.release()
    finally:
        with _lock:
            entry.users -= 1
            if not entry.users and _day_locks.get(path) is entry:
                del _day_locks[path]


def _identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _matches(record: _Record, expected: Manifest | None) -> bool:
    return expected is None or (record.size, record.sha256) == (expected.size, expected.sha256)


def _hold(directory: str, path: str, size: int) -> None:
    """Hold ``path`` and make it the most recently used. Under _lock."""
    _holds[path] = _holds.get(path, 0) + 1
    order = _order.setdefault(directory, OrderedDict())
    order[path] = size
    order.move_to_end(path)


def _pin_known(directory: str, path: str, expected: Manifest | None) -> bool:
    """Hold the day's file if this process validated it, it is unchanged since, and it is the one expected. Under _lock."""
    record = _records.get(path)
    if record is None or not _matches(record, expected) or _identity(path) != (record.size, record.mtime_ns):
        return False
    _hold(directory, path, record.size)
    return True


def _adopt(store: Store, day: date, directory: str, path: str, expected: Manifest | None) -> bool:
    """Hold the day's file already on disk if it is good and the one expected, else discard it. Under the day's lock."""
    identity = _identity(path)
    if identity is None:
        return False
    with _lock:
        record = _records.get(path)
    if record is None or (record.size, record.mtime_ns) != identity:
        # Left by an earlier process, or changed since this one validated it.
        try:
            digest = _file_sha256(path)
            _validate(store, day, path, identity[0], digest, None)
        except FileNotFoundError:
            return False
        except (OSError, _Invalid) as exc:
            logger.warning("cold store: discarding the cached %s %s: %s", store.name, day, exc)
            _discard(day, directory, path)
            return False
        record = _Record(identity[0], identity[1], digest)
    if not _matches(record, expected):
        logger.info(
            "cold store: the cached %s %s is not the compact its sidecar describes; fetching it", store.name, day
        )
        _discard(day, directory, path)
        return False
    with _lock:
        if _identity(path) != (record.size, record.mtime_ns):
            return False
        _records[path] = record
        _hold(directory, path, record.size)
    return True


def _discard(day: date, directory: str, path: str) -> None:
    """Remove the day's file once nothing holds it; ColdStoreError if something still does after _HELD_WAIT_S."""
    with _released:
        if not _released.wait_for(lambda: path not in _holds, timeout=_HELD_WAIT_S):
            logger.warning("cold store: %s is still in use after %s s; not replacing it", path, _HELD_WAIT_S)
            raise ColdStoreError(day)
        _forget(directory, path)


def _forget(directory: str, path: str) -> bool:
    """Delete a day file and everything known about it; whether it is gone. Under _lock."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("cold store: cannot remove %s", path, exc_info=True)
        return False
    _records.pop(path, None)
    order = _order.get(directory)
    if order is not None:
        order.pop(path, None)
    return True


def _evict(directory: str, bound: int) -> None:
    """Delete unheld days, least recently used first, until the directory's days total at most ``bound``. Under _lock."""
    order = _order.get(directory)
    if not order:
        return
    total = sum(order.values())
    for path, size in list(order.items()):
        if total <= bound:
            return
        if path not in _holds and _forget(directory, path):
            total -= size
            logger.info("cold store: evicted %s", path)


def _remembered(store: Store, day: date, expected: Manifest | None) -> bool:
    """ColdStoreError for a remembered failure of this sidecar; True for a remembered miss (no ``expected``)."""
    now = _clock()
    with _lock:
        if _live(_failures, _failure_key(store, day, expected), now):
            raise ColdStoreError(day)
        return expected is None and _live(_misses, (store.name, day.isoformat()), now)


def _failure_key(
    store: Store, day: date, expected: Manifest | None
) -> tuple[str, str, tuple[int | None, str | None] | None]:
    """A failure is remembered against the sidecar its fetch was for: its size and sha256, or None without one."""
    return store.name, day.isoformat(), None if expected is None else (expected.size, expected.sha256)


def _live[K](memory: dict[K, float], key: K, now: float) -> bool:
    until = memory.get(key)
    if until is None:
        return False
    if now < until:
        return True
    del memory[key]
    return False


def _remember[K](memory: dict[K, float], key: K, ttl_s: float) -> None:
    now = _clock()
    with _lock:
        memory.pop(key, None)
        memory[key] = now + ttl_s
        # Oldest first, which with a fixed ttl is also the first to end.
        while memory and (len(memory) > _MEMORY_MAX or memory[next(iter(memory))] <= now):
            del memory[next(iter(memory))]


def _fetch(store: Store, day: date, directory: str, path: str, expected: Manifest | None) -> bool:
    """Download, validate and install the day's compact, and hold it. Under the day's lock, with no file at ``path``."""
    key = (store.name, day.isoformat())
    data = _client.download_bytes(compaction.object_key(store, day, COMPACT_NAME))
    if data is None:
        if expected is None:
            _remember(_misses, key, constants.TRACKS_COLD_MISS_TTL_S)
            logger.debug("cold store: %s %s has no compact in object storage", store.name, day)
            return False
        _remember(_failures, _failure_key(store, day, expected), _FAILURE_TTL_S)
        logger.warning("cold store: %s %s: the compact its sidecar lists could not be fetched", store.name, day)
        raise ColdStoreError(day)
    digest = hashlib.sha256(data).hexdigest()
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = _write_tmp(directory, data)
        _validate(store, day, tmp, len(data), digest, expected)
        # The rename keeps the size and mtime the reader keys on.
        st = os.stat(tmp)
        with _lock:
            os.replace(tmp, path)
            tmp = None
            _records[path] = _Record(st.st_size, st.st_mtime_ns, digest)
            _hold(directory, path, st.st_size)
            _misses.pop(key, None)
            # The object is a good compact of the day, and the one with this size and sha256.
            _failures.pop((*key, None), None)
            _failures.pop((*key, (len(data), digest)), None)
            _evict(directory, store.cold_cache_max_bytes())
    except _Invalid as exc:
        _remember(_failures, _failure_key(store, day, expected), _FAILURE_TTL_S)
        logger.warning("cold store: %s %s: the fetched compact is discarded: %s", store.name, day, exc)
        raise ColdStoreError(day) from exc
    except OSError as exc:
        logger.warning("cold store: %s %s: the fetched compact is discarded: %s", store.name, day, exc)
        raise ColdStoreError(day) from exc
    finally:
        if tmp is not None:
            _unlink_quietly(tmp)
    try:
        _fsync_dir(directory)
    except OSError:
        logger.warning("cold store: cannot fsync %s", directory, exc_info=True)
    return True


def _validate(store: Store, day: date, path: str, size: int, digest: str, expected: Manifest | None) -> None:
    """_Invalid unless ``path`` is a whole compact of the store's day, and the one ``expected`` describes if given."""
    if expected is not None and (size, digest) != (expected.size, expected.sha256):
        raise _Invalid(
            f"it is {size} bytes with sha256 {digest}, its sidecar says {expected.size} bytes with {expected.sha256}"
        )
    try:
        manifest = compaction.read_footer_manifest(path)
        with pq.ParquetFile(path) as parquet:
            meta = parquet.metadata
            rows = meta.num_rows
            groups = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise _Invalid(f"it does not open as Parquet: {exc}") from exc
    if manifest is None:
        raise _Invalid("its footer carries no compact manifest")
    if (manifest.store, manifest.day) != (store.name, day.isoformat()):
        raise _Invalid(f"its footer manifest is of {manifest.store} {manifest.day}")
    if manifest.rows != rows or [group.rows for group in manifest.groups] != groups:
        raise _Invalid(
            f"its {rows} rows in {len(groups)} row groups disagree with its footer manifest's "
            f"{manifest.rows} in {len(manifest.groups)}"
        )


# ── Files ────────────────────────────────────────────────────────────────────


def _write_tmp(directory: str, data: bytes) -> str:
    """``data`` in a new fsynced tmp file in ``directory``; its path. Removed again if the write fails."""
    tmp = os.path.join(directory, f"{_TMP_PREFIX}{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return tmp


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("cold store: cannot remove %s", path, exc_info=True)
