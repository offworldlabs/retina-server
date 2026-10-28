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
- With ``pending`` (the day's record of an upload under way,
  compaction.PENDING_NAME) the store holds the record's compact or the one
  before it, so the cached copy is served without asking only when it has the
  record's size and sha256, or is the copy the store last answered with for
  that record, within the store's miss TTL; any other copy is discarded and
  the store asked. That answer (the copy it held, or none) is remembered per
  day and record for the store's miss TTL, so a burst of reads, and a day
  whose record waits up to an hour for the sweep, download the compact at
  most once a TTL (6 times an hour at the default 600 s, not on every read).
  Either copy is exact: a reader reads beside it only the raw parts its own
  footer does not list. A record that settles changes ``expected``, which is
  then fetched at once.
- Otherwise the result of one fetch, under a lock per day: concurrent callers
  for a day wait for that one fetch and then share its file, and callers for
  different days never wait on each other. A caller waits at most _LOCK_WAIT_S
  for the day's lock; one that cannot take it in time raises ColdStoreError
  without fetching, and the fetch it waited on goes on for the callers after it.

A fetch writes the object's bytes to a tmp file, fsyncs it and validates it
before renaming it over the day's path. Valid means: with ``pending``, the
record's size and sha256, or else those of ``expected`` when given; without
it, those of ``expected`` when given; always, that it opens as Parquet and its
footer manifest names the store and the day and agrees with the file's row
count and row groups. A file that fails is removed, logged, and raises
ColdStoreError, a failure remembered as below. Tmp files left by a crash are
removed when a process first uses the directory (the backend runs as one
process).

A missing object (download_bytes returned None, which the client also
returns for any error of the store; its contract is left as it is) means:

- with no ``expected``, a listing of that exact key is asked. Listed, the
  object is there and its download failed: ColdStoreError, a failure. Not
  listed, the day has no compact: open_day yields None and remembers the miss
  for the store's miss TTL (TRACKS_COLD_MISS_TTL_S, ADSB_COLD_MISS_TTL_S), or,
  with ``pending``, as that record's answer, before asking again. A listing
  that fails reads as not listed, so a store down for both calls still reads
  as a miss;
- with ``expected``, that the compact the sidecar promises cannot be had (gone,
  or the store is down): ColdStoreError, a failure.

A failure, an object missing as above or failing validation, is remembered for
_FAILURE_TTL_S against the sidecar and record its fetch was for (the size and
sha256 of each, or none), so a burst of requests fails fast without each
downloading the object: open_day given the same sidecar and record raises
ColdStoreError for the day without asking the store. A changed sidecar or
record (the day compacted again) is fetched at once. A fetch that succeeds
forgets the failures it disproves: the one with neither, the one for the size
and sha256 it fetched, and its own. A local error (the cache directory cannot
be written) is not remembered.

REMOTE is what compaction asks the store to settle a record and restore a
sidecar (compaction.Remote): whether the store's compact of a day is a given
one, and its compact.json; and it uploads a record whose compact the store holds
as that compact.json.

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
client is imported on one line, as _client, and nowhere else in this feature
but the lifecycle (services/tasks/archive_lifecycle.py), which imports
is_enabled, upload_file and upload_bytes from it: list_keys, download_bytes,
download_to, upload_file and upload_bytes pass through to it for the other
modules that need it.
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

from services import compaction
from services import r2_client as _client
from services.compaction import COMPACT_NAME, SIDECAR_NAME, Manifest, Store

logger = logging.getLogger(__name__)

# Read on every call, so a test can point it elsewhere.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "coverage_data", "cold_cache")

# How long a day whose fetch failed keeps failing, for the same sidecar, without asking the store.
_FAILURE_TTL_S = 30.0
# How long a discard waits for the requests holding its file.
_HELD_WAIT_S = 30.0
# How long a request waits for another request's fetch of the same day.
_LOCK_WAIT_S = 20.0
# Entries each memory keeps at most (each store's misses, the failures); past it the oldest are forgotten.
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
# A manifest's (size, sha256), or None for none.
_Ident = tuple[int | None, str | None] | None
# Store name -> {ISO day: the _clock time the memory ends}, oldest first. One
# memory per store, made on its first miss, so all of a memory's entries last
# one ttl: the store's miss TTL, which config.constants sets once per process.
_misses: dict[str, dict[str, float]] = {}
# Store name -> {(ISO day, the record's (size, sha256), the (size, sha256) of
# the copy the store answered with, or None for none): the _clock time the
# memory ends}, oldest first; kept per store, as _misses, for its miss TTL.
_answers: dict[str, dict[tuple[str, _Ident, _Ident], float]] = {}
# (store name, ISO day, the sidecar's ident, the record's ident) -> the _clock
# time the memory ends, oldest first; every entry lasts _FAILURE_TTL_S.
_failures: dict[tuple[str, str, _Ident, _Ident], float] = {}


def _reset_for_tests() -> None:
    """Forget every hold, record, use order, miss, answer, failure and day lock."""
    with _lock:
        _day_locks.clear()
        _prepared.clear()
        _order.clear()
        _records.clear()
        _holds.clear()
        _misses.clear()
        _answers.clear()
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


def _holds_compact(store: Store, day: date, manifest: Manifest) -> bool | None:
    """Whether the store's compact of the day is the one ``manifest`` describes: compaction.Remote.holds.

    True when the store's compact.json is ``manifest`` (it is uploaded after
    its compact) or its compact.parquet has the manifest's size and sha256;
    False when it holds another compact, or none as a listing of the day
    proves; None with no store, or when a download failed and a listing
    cannot tell (the object is listed, or the listing is empty: the day holds
    objects, its raw parts at least, so an empty listing is a failed one).
    """
    if not _client.is_enabled():
        return None
    data = download_bytes(compaction.object_key(store, day, SIDECAR_NAME))
    if data is not None:
        try:
            if Manifest.from_json(data) == manifest:
                return True
        except ValueError:
            pass
    key = compaction.object_key(store, day, COMPACT_NAME)
    data = download_bytes(key)
    if data is not None:
        return (len(data), hashlib.sha256(data).hexdigest()) == (manifest.size, manifest.sha256)
    listed = list_keys(compaction.object_key(store, day, ""))
    return False if listed and key not in listed else None


def _stored_sidecar(store: Store, day: date) -> Manifest | None:
    """The store's compact.json of the day if it is a manifest of that store and day with size and sha256."""
    data = download_bytes(compaction.object_key(store, day, SIDECAR_NAME))
    if data is None:
        return None
    try:
        manifest = Manifest.from_json(data)
    except ValueError:
        return None
    if (manifest.store, manifest.day) != (store.name, day.isoformat()) or manifest.size is None:
        return None
    return manifest


def _put_sidecar(store: Store, day: date, manifest: Manifest) -> bool:
    """Upload ``manifest`` as the store's compact.json of the day: compaction.Remote.put_sidecar."""
    return upload_bytes(compaction.object_key(store, day, SIDECAR_NAME), manifest.to_json(), "application/json")


REMOTE = compaction.Remote(holds=_holds_compact, sidecar=_stored_sidecar, put_sidecar=_put_sidecar)


# ── Reading a day ────────────────────────────────────────────────────────────


@contextmanager
def open_day(
    store: Store,
    day: date,
    *,
    expected: Manifest | None,
    pending: Manifest | None = None,
    now_ms: int | None = None,
) -> Iterator[str | None]:
    """Hold a validated local copy of a day's compact from object storage.

    Yields its path, held until the block exits, or None when no store is
    configured, the day is not complete at ``now_ms`` (the wall clock by
    default), or, with no ``expected``, the store has no compact of the day.
    ``expected`` is the day's local sidecar manifest when there is one: the
    copy then has its size and sha256. ``pending`` is the day's record of an
    upload under way when there is one: the copy then has its size and
    sha256, or else ``expected``'s. Raises ColdStoreError when the fetched
    object fails validation or the store does not return the object
    ``expected`` promises, or one it lists (either is remembered for
    _FAILURE_TTL_S), or when another request's fetch of the day outlasts
    _LOCK_WAIT_S. See the module docstring.
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
    want = _Want(store, day, expected, pending)
    if not _acquire(want, directory, path):
        yield None
        return
    try:
        yield path
    finally:
        _release(store, directory, path)


@dataclass(frozen=True, slots=True)
class _Want:
    """What one open_day asks for: a store's day, its sidecar and its record of an upload under way."""

    store: Store
    day: date
    expected: Manifest | None
    pending: Manifest | None

    def accepts(self, record: _Record) -> bool:
        """Whether a copy can be served without asking the store. Under _lock."""
        if self.pending is None:
            return self.expected is None or (record.size, record.sha256) == _ident(self.expected)
        if (record.size, record.sha256) == _ident(self.pending):
            return True
        return _live(self._answers(), self.answer_key((record.size, record.sha256)), _clock())

    def answer_key(self, copy: _Ident) -> tuple[str, _Ident, _Ident]:
        return self.day.isoformat(), _ident(self.pending), copy

    def _answers(self) -> dict[tuple[str, _Ident, _Ident], float]:
        return _answers.get(self.store.name, {})

    def remember_answer(self, copy: _Ident) -> None:
        """Remember what the store held for this record: ``copy``'s ident, or None. Under _lock."""
        _remember(_answers.setdefault(self.store.name, {}), self.answer_key(copy), self.store.cold_miss_ttl_s())

    def failure_key(self) -> tuple[str, str, _Ident, _Ident]:
        """A failure is remembered against the sidecar and record its fetch was for."""
        return self.store.name, self.day.isoformat(), _ident(self.expected), _ident(self.pending)


def _ident(manifest: Manifest | None) -> _Ident:
    return None if manifest is None else (manifest.size, manifest.sha256)


def _acquire(want: _Want, directory: str, path: str) -> bool:
    """Hold the day's file, fetching it if need be; False when the store has no compact of the day."""
    with _lock:
        try:
            _prepare(directory)
        except OSError:
            logger.warning("cold store: cannot use the cache directory %s", directory, exc_info=True)
            raise ColdStoreError(want.day) from None
        if _pin_known(want, directory, path):
            return True
    with _day_lock(want.day, path):
        with _lock:
            if _pin_known(want, directory, path):
                return True
        if _adopt(want, directory, path):
            return True
        if _remembered(want):
            return False
        return _fetch(want, directory, path)


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


def _hold(directory: str, path: str, size: int) -> None:
    """Hold ``path`` and make it the most recently used. Under _lock."""
    _holds[path] = _holds.get(path, 0) + 1
    order = _order.setdefault(directory, OrderedDict())
    order[path] = size
    order.move_to_end(path)


def _pin_known(want: _Want, directory: str, path: str) -> bool:
    """Hold the day's file if this process validated it, it is unchanged since, and it is one ``want`` accepts. Under _lock."""
    record = _records.get(path)
    if record is None or not want.accepts(record) or _identity(path) != (record.size, record.mtime_ns):
        return False
    _hold(directory, path, record.size)
    return True


def _adopt(want: _Want, directory: str, path: str) -> bool:
    """Hold the day's file already on disk if it is good and one ``want`` accepts, else discard it. Under the day's lock."""
    store, day = want.store, want.day
    identity = _identity(path)
    if identity is None:
        return False
    with _lock:
        record = _records.get(path)
    if record is None or (record.size, record.mtime_ns) != identity:
        # Left by an earlier process, or changed since this one validated it.
        try:
            digest = _file_sha256(path)
            _validate(store, day, path, identity[0], digest, None, None)
        except FileNotFoundError:
            return False
        except (OSError, _Invalid) as exc:
            logger.warning("cold store: discarding the cached %s %s: %s", store.name, day, exc)
            _discard(day, directory, path)
            return False
        record = _Record(identity[0], identity[1], digest)
    with _lock:
        accepted = want.accepts(record)
    if not accepted:
        logger.info(
            "cold store: the cached %s %s is not the compact its sidecar or upload record describes; fetching it",
            store.name,
            day,
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


def _remembered(want: _Want) -> bool:
    """ColdStoreError for a remembered failure of this sidecar and record; True for a remembered miss (no ``expected``).

    With a record, the miss is the one remembered as the store's answer for
    it; a miss remembered before the record was written says nothing of it.
    """
    now = _clock()
    with _lock:
        if _live(_failures, want.failure_key(), now):
            raise ColdStoreError(want.day)
        if want.expected is not None:
            return False
        if want.pending is not None:
            return _live(_answers.get(want.store.name, {}), want.answer_key(None), now)
        return _live(_misses.get(want.store.name, {}), want.day.isoformat(), now)


def _live[K](memory: dict[K, float], key: K, now: float) -> bool:
    until = memory.get(key)
    if until is None:
        return False
    if now < until:
        return True
    del memory[key]
    return False


def _remember[K](memory: dict[K, float], key: K, ttl_s: float) -> None:
    """Remember ``key`` for ``ttl_s`` from now; forget expired entries and the oldest past _MEMORY_MAX. Under _lock."""
    now = _clock()
    memory.pop(key, None)
    memory[key] = now + ttl_s
    # Entries are in the order they were remembered. Every entry of one memory
    # lasts the same ttl and the clock is read under _lock, so that is also the
    # order they end in: the expired entries are a run at the head, and the one
    # the bound forgets is the live entry closest to its end.
    while memory and (len(memory) > _MEMORY_MAX or memory[next(iter(memory))] <= now):
        del memory[next(iter(memory))]


def _fetch(want: _Want, directory: str, path: str) -> bool:
    """Download, validate and install the day's compact, and hold it. Under the day's lock, with no file at ``path``."""
    store, day, expected = want.store, want.day, want.expected
    key = (store.name, day.isoformat())
    object_key = compaction.object_key(store, day, COMPACT_NAME)
    data = _client.download_bytes(object_key)
    if data is None:
        if expected is None and object_key not in _client.list_keys(object_key):
            with _lock:
                if want.pending is None:
                    _remember(_misses.setdefault(store.name, {}), day.isoformat(), store.cold_miss_ttl_s())
                else:
                    want.remember_answer(None)
            logger.debug("cold store: %s %s has no compact in object storage", store.name, day)
            return False
        with _lock:
            _remember(_failures, want.failure_key(), _FAILURE_TTL_S)
        if expected is None:
            logger.warning("cold store: %s %s: the store lists its compact, whose download failed", store.name, day)
        else:
            logger.warning("cold store: %s %s: the compact its sidecar lists could not be fetched", store.name, day)
        raise ColdStoreError(day)
    digest = hashlib.sha256(data).hexdigest()
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = _write_tmp(directory, data)
        _validate(store, day, tmp, len(data), digest, expected, want.pending)
        # The rename keeps the size and mtime the reader keys on.
        st = os.stat(tmp)
        with _lock:
            os.replace(tmp, path)
            tmp = None
            _records[path] = _Record(st.st_size, st.st_mtime_ns, digest)
            _hold(directory, path, st.st_size)
            _misses.get(store.name, {}).pop(day.isoformat(), None)
            # The object is a good compact of the day, and the one with this size and sha256.
            _failures.pop((*key, None, None), None)
            _failures.pop((*key, (len(data), digest), None), None)
            _failures.pop(want.failure_key(), None)
            if want.pending is not None and (len(data), digest) != _ident(want.pending):
                want.remember_answer((len(data), digest))
            _evict(directory, store.cold_cache_max_bytes())
    except _Invalid as exc:
        with _lock:
            _remember(_failures, want.failure_key(), _FAILURE_TTL_S)
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


def _validate(
    store: Store,
    day: date,
    path: str,
    size: int,
    digest: str,
    expected: Manifest | None,
    pending: Manifest | None,
) -> None:
    """_Invalid unless ``path`` is a whole compact of the store's day, and the one ``pending`` describes, or else
    ``expected``, when given."""
    is_pending = pending is not None and (size, digest) == (pending.size, pending.sha256)
    if not is_pending and expected is not None and (size, digest) != (expected.size, expected.sha256):
        raise _Invalid(
            f"it is {size} bytes with sha256 {digest}, its sidecar says {expected.size} bytes with {expected.sha256}"
            + ("" if pending is None else f", its upload record {pending.size} bytes with {pending.sha256}")
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
