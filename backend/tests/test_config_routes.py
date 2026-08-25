"""Tests for GET/PUT /api/config.

The validate → apply → write ordering is covered in test_routes.py; these are
the endpoint's own guards.
"""

import os
import unittest.mock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from main import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestTowerConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_update_config_too_large_returns_413(self, client):
        """PUT /api/config with a body > 1 MB → 413 before writing to disk."""
        huge_body = {"data": "x" * 1_100_000}
        with unittest.mock.patch("routes.config.require_admin", return_value=None):
            r = client.put("/api/config", json=huge_body)
        assert r.status_code == 413
        assert "too large" in r.json()["detail"].lower()
