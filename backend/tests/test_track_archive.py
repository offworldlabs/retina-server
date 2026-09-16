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


def test_write_failure_retries_old_batch_before_new_records(archive, monkeypatch):
    writer = ta.write_tracks_parquet
    state.track_archive_buffer.extend([{"solve_ts_ms": 1}, {"solve_ts_ms": 2}])

    def fail(**kwargs):
        state.track_archive_buffer.append({"solve_ts_ms": 3})
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


def test_directory_failure_retains_batch(archive):
    archive.rmdir()
    archive.write_text("not a directory")
    state.track_archive_buffer.append({"solve_ts_ms": 4})
    with pytest.raises(OSError):
        ta.flush_track_archive_buffer()
    archive.unlink()
    key = ta.flush_track_archive_buffer()
    assert pq.read_table(archive / key).column("solve_ts_ms").to_pylist() == [4]


def test_repeated_failures_do_not_grow_pending_batch(archive, monkeypatch):
    writer = ta.write_tracks_parquet
    state.track_archive_buffer.extend({"solve_ts_ms": n} for n in range(3))

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ta, "write_tracks_parquet", fail)
    for n in range(3, 10):
        with pytest.raises(OSError):
            ta.flush_track_archive_buffer()
        state.track_archive_buffer.append({"solve_ts_ms": n})
    monkeypatch.setattr(ta, "write_tracks_parquet", writer)
    retry_key = ta.flush_track_archive_buffer()
    new_key = ta.flush_track_archive_buffer()
    assert pq.read_table(archive / retry_key).column("solve_ts_ms").to_pylist() == [0, 1, 2]
    # The producer retains its existing bounded, newest-records policy.
    assert pq.read_table(archive / new_key).column("solve_ts_ms").to_pylist() == [7, 8, 9]


@pytest.mark.asyncio
async def test_failed_flush_does_not_advance_success_health(archive, monkeypatch):
    monkeypatch.setattr(state, "task_last_success", {"track_archive_flush": 123.0})
    monkeypatch.setattr(state, "task_error_counts", defaultdict(int))
    state.track_archive_buffer.append({"solve_ts_ms": 1})
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
