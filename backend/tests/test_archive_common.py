"""What the public archive routes share: the query-parameter rules, and the
thread pool the tracks and ADS-B routes read their stores on."""

import asyncio
import importlib
import threading

import httpx
import pytest

from main import app
from services import adsb_store, tracks_store

BBOX_DETAIL = "bbox must be south,west,north,east in degrees, with south <= north"


def test_shared_instant_rule_parses_ms_and_iso_and_rejects_garbage():
    from routes.archive_common import parse_instant_ms

    assert parse_instant_ms("1700000000123", "from") == 1_700_000_000_123
    assert parse_instant_ms("2023-11-14T22:13:20Z", "from") == 1_700_000_000_000
    with pytest.raises(Exception) as caught:
        parse_instant_ms("yesterday", "to")
    assert caught.value.status_code == 422
    assert caught.value.detail == "to must be epoch milliseconds or an ISO-8601 time"


def test_shared_bbox_rule_parses_corners_and_rejects_bad_boxes():
    from routes.archive_common import parse_bbox

    box = parse_bbox("-10,20.5,30,40")
    assert (box.south, box.west, box.north, box.east) == (-10.0, 20.5, 30.0, 40.0)
    for bad in ("1,2,3", "30,0,10,1", "a,b,c,d", "0,-181,1,1"):
        with pytest.raises(Exception) as caught:
            parse_bbox(bad)
        assert caught.value.status_code == 422
        assert caught.value.detail == BBOX_DETAIL


def test_shared_invalid_parameter_is_a_422_with_the_given_detail():
    from routes.archive_common import invalid_parameter

    error = invalid_parameter("to must be after from")
    assert error.status_code == 422
    assert error.detail == "to must be after from"


def test_both_archive_routes_use_the_shared_parameter_rules():
    common = importlib.import_module("routes.archive_common")
    tracks = importlib.import_module("routes.tracks_archive")
    detections = importlib.import_module("routes.detections_archive")
    for module in (tracks, detections):
        assert module.parse_bbox is common.parse_bbox
        assert module.parse_instant_ms is common.parse_instant_ms
        assert module.invalid_parameter is common.invalid_parameter
        assert module.Fleet is common.Fleet
        for private in ("_bbox", "_instant_ms", "_invalid"):
            assert not hasattr(module, private)


async def test_store_work_runs_on_threads_named_for_the_archives():
    from routes.archive_common import in_store_pool

    def thread_name() -> bytes:
        return threading.current_thread().name.encode()

    assert (await in_store_pool(thread_name)).startswith(b"archive-store")


async def test_tracks_and_adsb_reads_share_one_pool_of_four_threads(monkeypatch):
    held, release, adsb_ran = threading.Semaphore(0), threading.Semaphore(0), threading.Event()

    def held_tracks_read(*args, **kwargs):
        held.release()
        release.acquire(timeout=10)
        return {"rows": []}

    def adsb_read(*args, **kwargs):
        adsb_ran.set()
        return {"rows": []}

    monkeypatch.setattr(tracks_store, "query_tracks", held_tracks_read)
    monkeypatch.setattr(adsb_store, "query_adsb", adsb_read)
    for route in ("routes.tracks_archive", "routes.adsb_archive"):
        monkeypatch.setattr(importlib.import_module(route), "private_node_ids", frozenset)
    window = {"from": "2026-10-18T10:00:00Z", "to": "2026-10-18T10:30:00Z"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tracks = [asyncio.create_task(client.get("/api/data/tracks", params=window)) for _ in range(4)]
        try:
            for _ in range(4):
                assert await asyncio.to_thread(held.acquire, timeout=5)
            adsb = asyncio.create_task(client.get("/api/data/adsb", params=window))
            # Every thread is held by a tracks read, so the ADS-B read waits for one.
            assert not await asyncio.to_thread(adsb_ran.wait, 0.5)
            release.release()
            assert await asyncio.to_thread(adsb_ran.wait, 5)
            assert (await adsb).status_code == 200
        finally:
            for _ in range(4):
                release.release()
            answers = await asyncio.gather(*tracks, return_exceptions=True)
    assert [answer.status_code for answer in answers] == [200] * 4
