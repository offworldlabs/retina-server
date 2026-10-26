"""services/cold_store.py: archived days fetched from object storage into a bounded local cache.

The object store is moto's in-process S3, configured as test_r2_client.py
configures it, and every compact is a real one built by services.compaction
from a raw part, so validation reads real footers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import unittest.mock
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from config import constants
from services import adsb_writer, cold_store, compaction
from services.cold_store import ColdStoreError
from services.compaction import ADSB, COMPACT_NAME, PARQUET_CONTENT_TYPE, TRACKS, Manifest
from services.parquet_io import write_parquet_batch
from services.tasks import archive_lifecycle
from services.track_writer import SCHEMA

DAY = date(2026, 10, 18)
OTHER_DAY = date(2026, 10, 17)
# 2026-10-19 00:30Z: the end of DAY plus the 30-minute settle margin.
READY_MS = 1_792_369_800_000
DAY_KEY = "tracks/year=2026/month=10/day=18/compact.parquet"
OTHER_DAY_KEY = "tracks/year=2026/month=10/day=17/compact.parquet"
DEFAULT_CACHE_DIR = cold_store._CACHE_DIR

_FAKE_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "AKIATEST",
    "R2_SECRET_ACCESS_KEY": "testsecret",
    "R2_BUCKET": "retina-data",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "testsecret",
}


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Iterator[Path]:
    path = tmp_path / "cold_cache"
    monkeypatch.setattr(cold_store, "_CACHE_DIR", str(path))
    cold_store._reset_for_tests()
    yield path
    cold_store._reset_for_tests()


@pytest.fixture
def bucket(cache_dir, monkeypatch) -> Iterator[None]:
    """A configured object store with one empty bucket."""
    client = cold_store._client
    for name, value in (
        ("_ENABLED", True),
        ("_BUCKET", "retina-data"),
        ("_ACCOUNT_ID", "testaccount"),
        ("_ACCESS_KEY", "AKIATEST"),
        ("_SECRET_KEY", "testsecret"),
        ("_ENDPOINT_URL", None),
    ):
        monkeypatch.setattr(client, name, value)
    with unittest.mock.patch.dict(os.environ, _FAKE_ENV), mock_aws():
        client._clear_cache()
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        yield
        client._clear_cache()


@pytest.fixture
def downloads(bucket, monkeypatch) -> list[str]:
    """The keys downloaded from the store, in order."""
    keys: list[str] = []
    real = cold_store._client.download_bytes

    def counting(key: str) -> bytes | None:
        keys.append(key)
        return real(key)

    monkeypatch.setattr(cold_store._client, "download_bytes", counting)
    return keys


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(cold_store, "_clock", lambda: now[0])
    return now


def build_compact(tmp_path: Path, day: date, lats: list[float]) -> tuple[Path, Manifest]:
    """A compact of ``day`` built from one raw part holding a row per lat."""
    work = tmp_path / uuid.uuid4().hex
    rows = []
    for second, lat in enumerate(lats):
        ts = int(datetime(day.year, day.month, day.day, 10, 0, second, tzinfo=UTC).timestamp() * 1000)
        rows.append(
            {
                "solve_ts_ms": ts,
                "frame_ts_ms": ts - 700,
                "lat": lat,
                "lon": -82.39,
                "n_nodes": 2,
                "contributing_node_ids": "ret1a2b3c4d",
                "node_kind": "real",
                "track_key": f"mn-dark-{lat}",
            }
        )
    table = pa.table({f.name: [r.get(f.name) for r in rows] for f in SCHEMA}, schema=SCHEMA)
    part = write_parquet_batch(table, work, datetime(day.year, day.month, day.day, 12, tzinfo=UTC))
    out = work / "built.bin"
    manifest = compaction.build(TRACKS, day, [str(work / part)], str(out))
    return out, manifest


def upload(day: date, data: bytes) -> None:
    assert cold_store.upload_bytes(compaction.object_key(TRACKS, day, COMPACT_NAME), data, PARQUET_CONTENT_TYPE)


def open_path(day: date, expected: Manifest | None = None, now_ms: int = READY_MS) -> str | None:
    with cold_store.open_day(TRACKS, day, expected=expected, now_ms=now_ms) as path:
        return path


def open_adsb(day: date) -> str | None:
    with cold_store.open_day(ADSB, day, expected=None, now_ms=READY_MS) as path:
        return path


def day_path(cache_dir: Path, day: date) -> str:
    return str(cache_dir / "tracks" / f"{day.isoformat()}.parquet")


def cached(cache_dir: Path) -> list[str]:
    return sorted(os.listdir(cache_dir / "tracks"))


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "services.cold_store" and r.levelno == logging.WARNING]


# ── Fetching and serving a day ───────────────────────────────────────────────


def test_the_cache_lives_outside_the_lifecycle_roots():
    cache = Path(DEFAULT_CACHE_DIR).resolve()
    assert cache == (archive_lifecycle._COVERAGE_DIR / "cold_cache").resolve()
    for root, _prefix in archive_lifecycle._archive_roots():
        assert not cache.is_relative_to(Path(root).resolve())


def test_a_first_hit_downloads_the_day_and_later_hits_are_served_from_the_cache(tmp_path, cache_dir, downloads):
    built, manifest = build_compact(tmp_path, DAY, [1.0, 2.0, 3.0])
    upload(DAY, built.read_bytes())

    path = day_path(cache_dir, DAY)

    assert open_path(DAY) == path
    assert Path(path).read_bytes() == built.read_bytes()
    assert pq.read_table(path).equals(pq.read_table(built))
    assert downloads == [DAY_KEY]
    assert open_path(DAY) == path
    assert open_path(DAY, manifest) == path
    assert downloads == [DAY_KEY]

    # A restarted process validates the file it finds once and serves it without a download.
    cold_store._reset_for_tests()
    assert open_path(DAY, manifest) == path
    assert open_path(DAY) == path
    assert downloads == [DAY_KEY]
    assert cached(cache_dir) == ["2026-10-18.parquet"]


def test_concurrent_requests_for_a_day_share_one_download(tmp_path, cache_dir, downloads, monkeypatch):
    built, _ = build_compact(tmp_path, DAY, [1.0, 2.0])
    upload(DAY, built.read_bytes())
    counting = cold_store._client.download_bytes
    arrived = threading.Condition()
    asked: list[str] = []

    def slow(key: str) -> bytes | None:
        # Keep the first download open until all 8 requests ask for it, or for
        # 0.5 s: a request that does not wait for it arrives here meanwhile.
        with arrived:
            asked.append(key)
            arrived.notify_all()
            arrived.wait_for(lambda: len(asked) >= 8, timeout=0.5)
        return counting(key)

    monkeypatch.setattr(cold_store._client, "download_bytes", slow)
    start = threading.Barrier(8)
    paths: list[str | None] = [None] * 8

    def request(i: int) -> None:
        start.wait()
        paths[i] = open_path(DAY)

    threads = [threading.Thread(target=request, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert downloads == [DAY_KEY]
    assert paths == [day_path(cache_dir, DAY)] * 8
    assert Path(day_path(cache_dir, DAY)).read_bytes() == built.read_bytes()
    assert cached(cache_dir) == ["2026-10-18.parquet"]


def test_requests_for_different_days_do_not_wait_on_each_other(tmp_path, cache_dir, downloads, monkeypatch):
    for day in (DAY, OTHER_DAY):
        upload(day, build_compact(tmp_path, day, [1.0])[0].read_bytes())
    counting = cold_store._client.download_bytes
    started, release = threading.Event(), threading.Event()

    def blocked(key: str) -> bytes | None:
        if key == DAY_KEY:
            started.set()
            release.wait(timeout=10)
        return counting(key)

    monkeypatch.setattr(cold_store._client, "download_bytes", blocked)
    result: dict[str, str | None] = {}
    first = threading.Thread(target=lambda: result.setdefault("day", open_path(DAY)))
    first.start()
    try:
        assert started.wait(timeout=10)
        assert open_path(OTHER_DAY) == day_path(cache_dir, OTHER_DAY)
        assert "day" not in result
    finally:
        release.set()
        first.join(timeout=10)

    assert result == {"day": day_path(cache_dir, DAY)}
    assert sorted(downloads) == [OTHER_DAY_KEY, DAY_KEY]


def test_a_request_behind_a_slow_fetch_of_its_day_gives_up_without_fetching(
    tmp_path, cache_dir, downloads, monkeypatch
):
    built, manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    upload(DAY, built.read_bytes())
    counting = cold_store._client.download_bytes
    asked: list[str] = []
    started, release = threading.Event(), threading.Event()

    def blocked(key: str) -> bytes | None:
        asked.append(key)
        started.set()
        release.wait(timeout=10)
        return counting(key)

    monkeypatch.setattr(cold_store._client, "download_bytes", blocked)
    monkeypatch.setattr(cold_store, "_LOCK_WAIT_S", 0.2)
    first: dict[str, str | None] = {}
    outcome: list[str | None | ColdStoreError] = []
    waited: list[float] = []

    def wait_behind() -> None:
        began = time.monotonic()
        try:
            outcome.append(open_path(DAY, manifest))
        except ColdStoreError as exc:
            outcome.append(exc)
        waited.append(time.monotonic() - began)

    fetching = threading.Thread(target=lambda: first.setdefault("path", open_path(DAY, manifest)))
    fetching.start()
    try:
        assert started.wait(timeout=10)
        waiter = threading.Thread(target=wait_behind, daemon=True)
        waiter.start()
        # Bounded, so a waiter that never gives up fails the test instead of hanging it.
        waiter.join(timeout=5)
        assert not waiter.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], ColdStoreError)
        assert 0.15 <= waited[0] < 2
        assert first == {}
        assert asked == [DAY_KEY]
    finally:
        release.set()
        fetching.join(timeout=10)

    assert first == {"path": day_path(cache_dir, DAY)}
    assert open_path(DAY, manifest) == day_path(cache_dir, DAY)
    assert downloads == [DAY_KEY]
    assert cold_store._day_locks == {}


@pytest.mark.parametrize("bad", ["garbage", "another day's compact", "not the sidecar's sha256"])
def test_a_fetched_object_that_fails_validation_leaves_nothing_and_is_fetched_again(
    tmp_path, cache_dir, downloads, caplog, bad
):
    built, manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    expected = None
    if bad == "garbage":
        upload(DAY, b"PAR1 not a compact PAR1")
    elif bad == "another day's compact":
        upload(DAY, build_compact(tmp_path, OTHER_DAY, [1.0, 2.0])[0].read_bytes())
    else:
        upload(DAY, built.read_bytes())
        expected = replace(manifest, sha256="0" * 64)

    with pytest.raises(ColdStoreError) as raised:
        open_path(DAY, expected)

    assert str(raised.value) == "The archived day 2026-10-18 could not be loaded from cold storage."
    assert cached(cache_dir) == []
    assert len(warnings(caplog)) == 1
    upload(DAY, built.read_bytes())
    assert open_path(DAY, manifest) == day_path(cache_dir, DAY)
    assert downloads == [DAY_KEY, DAY_KEY]
    assert cached(cache_dir) == ["2026-10-18.parquet"]


def test_leftover_tmp_files_are_removed_on_first_use(tmp_path, cache_dir, downloads):
    built, _ = build_compact(tmp_path, DAY, [1.0])
    upload(DAY, built.read_bytes())
    (cache_dir / "tracks").mkdir(parents=True)
    (cache_dir / "tracks" / f".fetch-{uuid.uuid4().hex}.tmp").write_bytes(b"half a compact")

    assert open_path(DAY) == day_path(cache_dir, DAY)
    assert cached(cache_dir) == ["2026-10-18.parquet"]


# ── Missing objects ──────────────────────────────────────────────────────────


def test_a_missing_object_is_remembered_for_ten_minutes(cache_dir, downloads, clock):
    assert open_path(DAY) is None
    clock[0] += 599
    assert open_path(DAY) is None
    assert downloads == [DAY_KEY]

    clock[0] += 1
    assert open_path(DAY) is None
    assert downloads == [DAY_KEY, DAY_KEY]
    assert cached(cache_dir) == []


def test_each_store_remembers_a_missing_object_for_its_own_ttl(cache_dir, downloads, clock, monkeypatch):
    monkeypatch.setattr(constants, "ADSB_COLD_MISS_TTL_S", 5)
    monkeypatch.setattr(constants, "TRACKS_COLD_MISS_TTL_S", 600)
    adsb_key = "adsb/year=2026/month=10/day=18/compact.parquet"

    assert open_path(DAY) is None
    assert open_adsb(DAY) is None
    assert downloads == [DAY_KEY, adsb_key]

    clock[0] += 6
    assert open_path(DAY) is None
    assert downloads == [DAY_KEY, adsb_key]
    assert open_adsb(DAY) is None
    assert downloads == [DAY_KEY, adsb_key, adsb_key]


def test_an_expired_miss_is_forgotten_behind_another_stores_live_one(cache_dir, downloads, clock, monkeypatch):
    monkeypatch.setattr(constants, "ADSB_COLD_MISS_TTL_S", 5)
    monkeypatch.setattr(constants, "TRACKS_COLD_MISS_TTL_S", 600)

    assert open_path(DAY) is None
    assert open_adsb(DAY) is None
    clock[0] += 6
    assert open_adsb(OTHER_DAY) is None

    # ADS-B's miss of DAY ended at 1005 and is gone; the older tracks miss lasts until 1600.
    assert cold_store._misses == {"tracks": {"2026-10-18": 1600.0}, "adsb": {"2026-10-17": 1011.0}}
    assert open_path(DAY) is None
    assert downloads == [
        DAY_KEY,
        "adsb/year=2026/month=10/day=18/compact.parquet",
        "adsb/year=2026/month=10/day=17/compact.parquet",
    ]


def test_a_full_miss_memory_forgets_the_oldest_miss_of_its_own_store_only(cache_dir, downloads, clock, monkeypatch):
    monkeypatch.setattr(cold_store, "_MEMORY_MAX", 2)

    assert open_path(DAY) is None
    for day in (DAY, OTHER_DAY, date(2026, 10, 16)):
        assert open_adsb(day) is None
    # The third ADS-B miss pushed ADS-B's oldest, DAY, out; the older tracks miss of DAY stays.
    assert open_adsb(DAY) is None
    assert open_path(DAY) is None
    assert downloads == [
        DAY_KEY,
        "adsb/year=2026/month=10/day=18/compact.parquet",
        "adsb/year=2026/month=10/day=17/compact.parquet",
        "adsb/year=2026/month=10/day=16/compact.parquet",
        "adsb/year=2026/month=10/day=18/compact.parquet",
    ]


def test_a_missing_object_its_sidecar_promises_fails_fast_for_30_seconds(tmp_path, cache_dir, downloads, clock):
    _, manifest = build_compact(tmp_path, DAY, [1.0])

    with pytest.raises(ColdStoreError):
        open_path(DAY, manifest)
    clock[0] += 29
    with pytest.raises(ColdStoreError):
        open_path(DAY, manifest)
    assert downloads == [DAY_KEY]

    clock[0] += 1
    with pytest.raises(ColdStoreError):
        open_path(DAY, manifest)
    assert downloads == [DAY_KEY, DAY_KEY]
    assert cached(cache_dir) == []


def test_nothing_is_fetched_without_a_store_or_for_a_day_not_yet_complete(tmp_path, cache_dir, downloads, monkeypatch):
    upload(DAY, build_compact(tmp_path, DAY, [1.0])[0].read_bytes())

    assert open_path(DAY, now_ms=READY_MS - 1) is None
    monkeypatch.setattr(cold_store._client, "_ENABLED", False)
    assert not cold_store.enabled()
    assert open_path(DAY) is None
    assert downloads == []


# ── Objects that fail validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["not the sidecar's sha256", "garbage without a sidecar"])
def test_an_object_that_fails_validation_is_not_downloaded_again_for_30_seconds(
    tmp_path, cache_dir, downloads, clock, caplog, bad
):
    built, manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    expected: Manifest | None = None
    if bad == "garbage without a sidecar":
        upload(DAY, b"PAR1 not a compact PAR1")
    else:
        upload(DAY, built.read_bytes())
        expected = replace(manifest, sha256="0" * 64)

    for _ in range(4):
        with pytest.raises(ColdStoreError):
            open_path(DAY, expected)
    clock[0] += 29
    with pytest.raises(ColdStoreError):
        open_path(DAY, expected)
    assert downloads == [DAY_KEY]
    assert len(warnings(caplog)) == 1

    clock[0] += 1
    with pytest.raises(ColdStoreError):
        open_path(DAY, expected)
    assert downloads == [DAY_KEY, DAY_KEY]
    assert cached(cache_dir) == []


def test_a_changed_sidecar_is_fetched_at_once_despite_a_failure_remembered_for_the_old_one(
    tmp_path, cache_dir, downloads, clock
):
    _, old_manifest = build_compact(tmp_path, DAY, [1.0])
    new, new_manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    # The store already holds the new compact while the local sidecar still describes the old one.
    upload(DAY, new.read_bytes())
    for _ in range(2):
        with pytest.raises(ColdStoreError):
            open_path(DAY, old_manifest)
    assert downloads == [DAY_KEY]

    assert open_path(DAY, new_manifest) == day_path(cache_dir, DAY)
    assert Path(day_path(cache_dir, DAY)).read_bytes() == new.read_bytes()
    assert downloads == [DAY_KEY, DAY_KEY]


def test_a_fetch_that_succeeds_forgets_the_failure_it_disproves(tmp_path, cache_dir, downloads, clock, monkeypatch):
    built, manifest = build_compact(tmp_path, DAY, [1.0])
    # With a bound of 0 no copy outlives its request, so every open asks the store or its memory.
    monkeypatch.setattr(constants, "TRACKS_COLD_CACHE_MAX_BYTES", 0)
    upload(DAY, b"PAR1 not a compact PAR1")
    with pytest.raises(ColdStoreError):
        open_path(DAY)
    upload(DAY, built.read_bytes())

    assert open_path(DAY, manifest) == day_path(cache_dir, DAY)
    assert open_path(DAY) == day_path(cache_dir, DAY)
    assert downloads == [DAY_KEY, DAY_KEY, DAY_KEY]


# ── The bound ────────────────────────────────────────────────────────────────


def test_the_least_recently_used_day_is_evicted_past_the_bound_unless_held(tmp_path, cache_dir, downloads, monkeypatch):
    day_built, _ = build_compact(tmp_path, DAY, [1.0, 2.0])
    other_built, _ = build_compact(tmp_path, OTHER_DAY, [3.0, 4.0])
    upload(DAY, day_built.read_bytes())
    upload(OTHER_DAY, other_built.read_bytes())
    # Room for either day, not both.
    monkeypatch.setattr(
        constants, "TRACKS_COLD_CACHE_MAX_BYTES", max(day_built.stat().st_size, other_built.stat().st_size)
    )

    day_file, other_file = day_path(cache_dir, DAY), day_path(cache_dir, OTHER_DAY)

    assert open_path(DAY) == day_file
    fetched_mtime = os.stat(day_file).st_mtime_ns
    assert open_path(DAY) == day_file
    assert os.stat(day_file).st_mtime_ns == fetched_mtime
    assert open_path(OTHER_DAY) == other_file
    assert cached(cache_dir) == ["2026-10-17.parquet"]

    with cold_store.open_day(TRACKS, DAY, expected=None, now_ms=READY_MS) as held:
        assert held == day_file
        assert cached(cache_dir) == ["2026-10-18.parquet"]
        refetched_mtime = os.stat(day_file).st_mtime_ns
        # The other day is now the most recent; when its hold ends it is the one evicted.
        assert open_path(OTHER_DAY) == other_file
        assert cached(cache_dir) == ["2026-10-18.parquet"]
        assert open_path(DAY) == day_file
    assert cached(cache_dir) == ["2026-10-18.parquet"]
    assert os.stat(day_file).st_mtime_ns == refetched_mtime
    assert downloads == [DAY_KEY, OTHER_DAY_KEY, DAY_KEY, OTHER_DAY_KEY]


def test_with_a_bound_of_0_a_day_is_removed_when_its_last_hold_ends(tmp_path, cache_dir, downloads, monkeypatch):
    upload(DAY, build_compact(tmp_path, DAY, [1.0])[0].read_bytes())
    monkeypatch.setattr(constants, "TRACKS_COLD_CACHE_MAX_BYTES", 0)

    with cold_store.open_day(TRACKS, DAY, expected=None, now_ms=READY_MS) as outer:
        assert outer == day_path(cache_dir, DAY)
        assert open_path(DAY) == outer
        assert cached(cache_dir) == ["2026-10-18.parquet"]
    assert cached(cache_dir) == []
    assert downloads == [DAY_KEY]


def test_each_store_caches_its_days_apart_under_its_own_bound(tmp_path, cache_dir, downloads, monkeypatch):
    upload(DAY, build_compact(tmp_path, DAY, [1.0])[0].read_bytes())
    noon = int(datetime(2026, 10, 18, 12, tzinfo=UTC).timestamp() * 1000)
    fix = {"ts_ms": noon, "hex": "a1b2c3", "lat": 34.85, "lon": -82.39, "seen_ms": noon - 500}
    fix |= {"world": "real", "source": "node", "source_node_id": "ret1a2b3c4d"}
    key = adsb_writer.write_adsb_parquet(
        rows=[fix], base_dir=tmp_path / "adsb", write_ts=datetime.fromtimestamp(noon / 1000, UTC)
    )
    assert key is not None
    built = tmp_path / "adsb-built.bin"
    compaction.build(ADSB, DAY, [str(tmp_path / "adsb" / key)], str(built))
    assert cold_store.upload_bytes(
        compaction.object_key(ADSB, DAY, COMPACT_NAME), built.read_bytes(), PARQUET_CONTENT_TYPE
    )
    monkeypatch.setattr(constants, "ADSB_COLD_CACHE_MAX_BYTES", 0)

    assert open_path(DAY) == day_path(cache_dir, DAY)
    with cold_store.open_day(ADSB, DAY, expected=None, now_ms=READY_MS) as held:
        assert held == str(cache_dir / "adsb" / "2026-10-18.parquet")
        assert Path(held).read_bytes() == built.read_bytes()
    # The ADS-B day leaves with its last hold; the tracks day of the same date stays.
    assert os.listdir(cache_dir / "adsb") == []
    assert cached(cache_dir) == ["2026-10-18.parquet"]
    assert downloads == [DAY_KEY, "adsb/year=2026/month=10/day=18/compact.parquet"]


# ── A newer compact ──────────────────────────────────────────────────────────


def test_a_cached_day_its_sidecar_no_longer_describes_is_replaced_by_a_fresh_download(tmp_path, cache_dir, downloads):
    old, old_manifest = build_compact(tmp_path, DAY, [1.0])
    new, new_manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    path = day_path(cache_dir, DAY)
    upload(DAY, old.read_bytes())
    assert open_path(DAY, old_manifest) == path
    upload(DAY, new.read_bytes())

    assert open_path(DAY, new_manifest) == path
    assert Path(path).read_bytes() == new.read_bytes()
    assert open_path(DAY, new_manifest) == path
    assert downloads == [DAY_KEY, DAY_KEY]


def test_a_held_day_is_not_replaced_while_its_hold_lasts(tmp_path, cache_dir, downloads, monkeypatch):
    old, old_manifest = build_compact(tmp_path, DAY, [1.0])
    new, new_manifest = build_compact(tmp_path, DAY, [1.0, 2.0])
    upload(DAY, old.read_bytes())
    monkeypatch.setattr(cold_store, "_HELD_WAIT_S", 0.05)

    path = day_path(cache_dir, DAY)

    with cold_store.open_day(TRACKS, DAY, expected=old_manifest, now_ms=READY_MS) as held:
        assert held == path
        upload(DAY, new.read_bytes())
        with pytest.raises(ColdStoreError):
            open_path(DAY, new_manifest)
        assert Path(path).read_bytes() == old.read_bytes()

    assert open_path(DAY, new_manifest) == path
    assert Path(path).read_bytes() == new.read_bytes()


# ── Pass-throughs ────────────────────────────────────────────────────────────


def test_download_to_writes_the_object_atomically_or_reports_it_missing(tmp_path, bucket):
    upload(DAY, b"compact bytes")
    target = tmp_path / "out" / "compact.parquet"

    assert cold_store.download_to(DAY_KEY, str(target))
    assert target.read_bytes() == b"compact bytes"
    assert not cold_store.download_to(OTHER_DAY_KEY, str(tmp_path / "out" / "missing.parquet"))
    assert sorted(os.listdir(tmp_path / "out")) == ["compact.parquet"]
    assert cold_store.list_keys("tracks/") == [DAY_KEY]
