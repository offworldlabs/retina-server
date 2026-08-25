"""Tests for GET /api/health."""

import os
import unittest.mock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_degraded_stale_task(self, client):
        import time

        state.task_last_success["frame_processor"] = time.time() - 9999
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.task_last_success.pop("frame_processor", None)

    def test_health_strict_returns_503_when_degraded(self, client):
        import time

        state.task_last_success["frame_processor"] = time.time() - 9999
        try:
            r = client.get("/api/health?strict=1")
            assert r.status_code == 503
            assert r.json()["status"] == "degraded"
        finally:
            state.task_last_success.pop("frame_processor", None)

    def test_health_strict_returns_200_when_ok(self, client):
        r = client.get("/api/health?strict=1")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_degraded_queue_saturated(self, client):
        """Fill the frame queue past 90% to trigger saturation warning."""
        import queue

        orig_queue = state.frame_queue
        # Create a small queue and fill it
        small_q = queue.Queue(maxsize=10)
        for i in range(10):
            small_q.put(i)
        state.frame_queue = small_q
        try:
            r = client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert "issues" not in body  # details are logged, not exposed publicly
        finally:
            state.frame_queue = orig_queue

    def test_health_degraded_disk_low(self, client):
        """shutil.disk_usage returns <500 MB free → disk_low appended → degraded."""
        import shutil

        mock_disk = unittest.mock.MagicMock()
        mock_disk.free = 100 * 1024 * 1024  # 100 MB < 500 MB threshold
        with unittest.mock.patch.object(shutil, "disk_usage", return_value=mock_disk):
            r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

    def test_health_disk_usage_exception_does_not_crash(self, client):
        """shutil.disk_usage raises OSError → exception pass → endpoint still 200."""
        import shutil

        with unittest.mock.patch.object(shutil, "disk_usage", side_effect=OSError("no disk")):
            r = client.get("/api/health")
        assert r.status_code == 200
