"""Failed flushes retain a bounded batch and report failure to task health."""

import asyncio
from collections import defaultdict, deque

import pyarrow.parquet as pq
import pytest

from core import state
from services.tasks import track_archive as ta


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    monkeypatch.setattr(state, "track_archive_buffer", deque(maxlen=3))
    # Pending records belong to the archive writer, not the producer's deque.
    monkeypatch.setattr(ta, "_pending_records", [], raising=False)
    return tmp_path


def _rec(solve_ts_ms: int, **extra) -> dict:
    """A record the writer archives, known by its solve_ts_ms."""
    return {
        "solve_ts_ms": solve_ts_ms,
        "timestamp_ms": 1_700_000_000_000,
        "lat": 34.85,
        "lon": -82.39,
        "n_nodes": 2,
        **extra,
    }


def test_write_failure_retries_old_batch_before_new_records(archive, monkeypatch):
    writer = ta.write_tracks_parquet
    state.track_archive_buffer.extend([_rec(1), _rec(2)])

    def fail(**kwargs):
        state.track_archive_buffer.append(_rec(3))
        raise OSError("disk full")

    monkeypatch.setattr(ta, "write_tracks_parquet", fail)
    with pytest.raises(OSError, match="disk full"):
        ta.flush_track_archive_buffer()
    monkeypatch.setattr(ta, "write_tracks_parquet", writer)
    retry_key = ta.flush_track_archive_buffer()
    new_key = ta.flush_track_archive_buffer()
    assert pq.read_table(archive / retry_key).column("solve_ts_ms").to_pylist() == [1, 2]
    assert pq.read_table(archive / new_key).column("solve_ts_ms").to_pylist() == [3]
    assert ta.flush_track_archive_buffer() is None


def test_flush_writes_the_stamped_identity_columns(archive):
    state.track_archive_buffer.extend(
        [
            _rec(1, track_key="mn-dark-a", contributing_node_ids=["node-a", "synth-b"]),
            _rec(2, track_key="mn-dark-b", follow_lane=True, contributing_node_ids=["synth-a", "synth-b"]),
            _rec(3, track_key="mn-adsb-abc123", contributing_node_ids=["node-a", "node-b"]),
        ]
    )
    key = ta.flush_track_archive_buffer()
    rows = pq.read_table(archive / key, columns=["solve_ts_ms", "track_key", "lane", "node_kind"]).to_pylist()
    assert rows == [
        {"solve_ts_ms": 3, "track_key": "mn-adsb-abc123", "lane": "known", "node_kind": "real"},
        {"solve_ts_ms": 2, "track_key": "mn-dark-b", "lane": "follow", "node_kind": "synthetic"},
        {"solve_ts_ms": 1, "track_key": "mn-dark-a", "lane": "dark", "node_kind": "mixed"},
    ]


def test_directory_failure_retains_batch(archive):
    archive.rmdir()
    archive.write_text("not a directory")
    state.track_archive_buffer.append(_rec(4))
    with pytest.raises(OSError):
        ta.flush_track_archive_buffer()
    archive.unlink()
    key = ta.flush_track_archive_buffer()
    assert pq.read_table(archive / key).column("solve_ts_ms").to_pylist() == [4]


def test_repeated_failures_do_not_grow_pending_batch(archive, monkeypatch):
    writer = ta.write_tracks_parquet
    state.track_archive_buffer.extend(_rec(n) for n in range(1, 4))

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ta, "write_tracks_parquet", fail)
    for n in range(4, 11):
        with pytest.raises(OSError):
            ta.flush_track_archive_buffer()
        state.track_archive_buffer.append(_rec(n))
    monkeypatch.setattr(ta, "write_tracks_parquet", writer)
    retry_key = ta.flush_track_archive_buffer()
    new_key = ta.flush_track_archive_buffer()
    assert pq.read_table(archive / retry_key).column("solve_ts_ms").to_pylist() == [1, 2, 3]
    # The producer retains its existing bounded, newest-records policy.
    assert pq.read_table(archive / new_key).column("solve_ts_ms").to_pylist() == [8, 9, 10]


@pytest.mark.asyncio
async def test_failed_flush_does_not_advance_success_health(archive, monkeypatch):
    monkeypatch.setattr(state, "task_last_success", {"track_archive_flush": 123.0})
    monkeypatch.setattr(state, "task_error_counts", defaultdict(int))
    state.track_archive_buffer.append(_rec(1))
    sleeps = 0

    async def sleep(interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ta.asyncio, "sleep", sleep)
    monkeypatch.setattr(ta, "write_tracks_parquet", fail)
    with pytest.raises(asyncio.CancelledError):
        await ta.track_flush_task()
    assert state.task_last_success["track_archive_flush"] == 123.0
    assert state.task_error_counts["track_archive_flush"] == 1
