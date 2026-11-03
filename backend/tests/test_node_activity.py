"""A node's days of activity: marked by the frame worker, written by the flush."""

import asyncio
from datetime import UTC, date, datetime

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import delete, select

from core import state
from core.api_access import NodeActivityDay
from core.frame_queue import ShardedFrameQueue
from core.users import async_session_maker
from services import activity_backfill, node_activity, object_store, storage
from services.tasks import frame_loop
from tests.object_store_env import FAKE_AWS_ENV, store_env

_REAL = "ret1a2b3c4d"
_DETECTIONS = {"delay": [12.4], "doppler": [-118.0], "snr": [14.2]}


@pytest.fixture(autouse=True)
async def _no_activity(monkeypatch):
    """No marks or rows left from other tests, the probation fence on, and a fixed day."""
    monkeypatch.setattr(node_activity, "_pending", set())
    monkeypatch.setattr(node_activity, "_written", set())
    monkeypatch.setattr(node_activity, "_utc_today", lambda: date(2026, 11, 2))
    monkeypatch.delenv("POLLED_RADAR_PROBATION_ENABLED", raising=False)
    async with async_session_maker() as session, session.begin():
        await session.execute(delete(NodeActivityDay))


async def _deliver(monkeypatch, frames: list[tuple[str, dict]]) -> list[tuple[str, date]]:
    """Run frames through a frame worker, with process_one_frame stubbed out,
    then flush and read the table back."""
    queue = ShardedFrameQueue(maxsize=10, shards=1)
    monkeypatch.setattr(state, "frame_queue", queue)
    taken = []
    monkeypatch.setattr(frame_loop, "process_one_frame", lambda node_id, _frame, _pipeline: taken.append(node_id))
    for item in frames:
        queue.put_nowait(item)
    worker = asyncio.create_task(frame_loop.frame_processor_loop(object(), 0))
    try:
        await asyncio.wait_for(queue.shard(0).join(), timeout=10)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    assert taken == [node_id for node_id, _ in frames]
    await node_activity.flush_activity()
    async with async_session_maker() as session:
        result = await session.execute(select(NodeActivityDay.node_id, NodeActivityDay.day))
        return [(node_id, day) for node_id, day in result]


async def test_a_real_node_with_detections_gets_one_row_for_the_day(monkeypatch):
    rows = await _deliver(monkeypatch, [(_REAL, _DETECTIONS), (_REAL, _DETECTIONS)])
    assert rows == [(_REAL, date(2026, 11, 2))]


async def test_synthetic_empty_and_probation_frames_get_no_row(monkeypatch):
    rows = await _deliver(
        monkeypatch,
        [
            ("synth-0001", _DETECTIONS),
            ("ret5e6f7a8b", {"delay": [], "doppler": [], "snr": []}),
            # A polled radar that has not graduated.
            ("blaffff0000", _DETECTIONS),
            # The one frame that counts, so the frames above went the same way.
            (_REAL, _DETECTIONS),
        ],
    )
    assert rows == [(_REAL, date(2026, 11, 2))]


_NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)


def _parts(root, *paths: str) -> None:
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(b"")


async def _table() -> set[tuple[str, date]]:
    async with async_session_maker() as session:
        return set((await session.execute(select(NodeActivityDay.node_id, NodeActivityDay.day))).tuples())


async def test_backfill_records_each_real_node_day_in_the_archive_once(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(tmp_path))
    monkeypatch.setenv("CONTRIBUTOR_WINDOW_DAYS", "30")
    _parts(
        tmp_path,
        "year=2026/month=10/day=31/node_id=ret1a2b3c4d/part-230000-a.parquet",
        "year=2026/month=11/day=01/node_id=ret1a2b3c4d/part-000000-b.parquet",
        "year=2026/month=11/day=02/node_id=ret1a2b3c4d/part-080000-c.parquet",
        "year=2026/month=11/day=02/node_id=ret5e6f7a8b/part-080000-d.parquet",
        "2026/10/30/ret9c0d1e2f/detections_120000.json",
        "year=2026/month=11/day=01/node_id=synth-0001/part-000000-e.parquet",
        # Its part is gone, only the upload sentinel is left.
        "year=2026/month=11/day=01/node_id=ret3a4b5c6d/part-000000-f.parquet.uploaded",
    )
    expected = {
        ("ret1a2b3c4d", date(2026, 10, 3)),
        ("ret1a2b3c4d", date(2026, 10, 31)),
        ("ret1a2b3c4d", date(2026, 11, 1)),
        ("ret1a2b3c4d", date(2026, 11, 2)),
        ("ret5e6f7a8b", date(2026, 10, 30)),
        ("ret5e6f7a8b", date(2026, 11, 2)),
        ("ret9c0d1e2f", date(2026, 10, 30)),
        ("ret9c0d1e2f", date(2026, 10, 31)),
    }
    with store_env(FAKE_AWS_ENV), mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        for key in (
            "archive/year=2026/month=10/day=03/node_id=ret1a2b3c4d/part-120000-g.parquet",
            # The day before the window's first.
            "archive/year=2026/month=10/day=02/node_id=ret1a2b3c4d/part-120000-h.parquet",
            # Uploaded and still on local disk.
            "archive/year=2026/month=11/day=01/node_id=ret1a2b3c4d/part-000000-b.parquet",
            "archive/year=2026/month=10/day=30/node_id=ret5e6f7a8b/part-120000-i.parquet",
            "archive/2026/10/31/ret9c0d1e2f/detections_120000.json",
        ):
            assert object_store.upload_bytes(key, b"")
        assert await activity_backfill.backfill_activity_days(now=_NOW) == 8
        assert await _table() == expected
        assert await activity_backfill.backfill_activity_days(now=_NOW) == 0
    assert await _table() == expected


async def test_backfill_if_empty_runs_past_todays_marks_and_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(tmp_path))
    monkeypatch.setenv("CONTRIBUTOR_WINDOW_DAYS", "30")
    _parts(tmp_path, "year=2026/month=10/day=31/node_id=ret1a2b3c4d/part-120000-a.parquet")
    # The frame loop has already marked today.
    await node_activity.record_days([("ret1a2b3c4d", date(2026, 11, 2))])
    assert await activity_backfill.backfill_if_empty(now=_NOW) == 1
    _parts(tmp_path, "year=2026/month=11/day=01/node_id=ret5e6f7a8b/part-120000-b.parquet")
    assert await activity_backfill.backfill_if_empty(now=_NOW) == 0
    assert await _table() == {("ret1a2b3c4d", date(2026, 10, 31)), ("ret1a2b3c4d", date(2026, 11, 2))}
