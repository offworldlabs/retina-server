"""Integration tests for /api/radar/detections and /api/radar/detections/bulk,
plus unit tests for _check_rate_limit.
"""

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from main import app

VALID_KEY = "test-key-abc123"
HEADERS_OK = {"X-API-Key": VALID_KEY}


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_radar_state():
    """Remove any nodes / rate buckets touched by a test."""
    from core import state

    yield

    for node_id in list(state.connected_nodes.keys()):
        if node_id.startswith("test-") or node_id.startswith("http-") or node_id.startswith("bulk-"):
            state.connected_nodes.pop(node_id, None)
    for node_id in list(state.node_pipelines.keys()):
        if node_id.startswith("bulk-"):
            state.node_pipelines.pop(node_id, None)
    state.rate_buckets.clear()


# ── Auth tests ────────────────────────────────────────────────────────────────


class TestRadarDetectionsAuth:
    def test_missing_api_key_returns_401(self, client):
        r = client.post("/api/radar/detections", json={"node_id": "test-node"})
        assert r.status_code == 401

    def test_wrong_api_key_returns_401(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-node"},
            headers={"X-API-Key": "bad-key"},
        )
        assert r.status_code == 401

    def test_correct_api_key_returns_200(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-node"},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200


# ── Ingestion tests ───────────────────────────────────────────────────────────


class TestRadarDetectionsIngestion:
    def test_new_node_registered_in_connected_nodes(self, client):
        from core import state

        node_id = "test-new-node"
        assert node_id not in state.connected_nodes

        r = client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert node_id in state.connected_nodes

    def test_frame_without_timestamp_not_queued(self, client):
        r = client.post(
            "/api/radar/detections",
            json={"node_id": "test-no-ts", "frames": [{"value": 42}]},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert r.json()["frames_queued"] == 0

    def test_frame_with_timestamp_is_queued(self, client):
        r = client.post(
            "/api/radar/detections",
            json={
                "node_id": "test-ts-node",
                "frames": [{"timestamp": 1234567890.0, "value": 1}],
            },
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert r.json()["frames_queued"] == 1

    def test_existing_node_status_updated_not_duplicated(self, client):
        from core import state

        node_id = "test-existing"
        # First registration
        client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert node_id in state.connected_nodes

        # Second request — still only one entry, status stays "active"
        r = client.post(
            "/api/radar/detections",
            json={"node_id": node_id},
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        assert state.connected_nodes[node_id]["status"] == "active"
        # Confirm there is still exactly one entry with this id
        assert list(state.connected_nodes).count(node_id) == 1


# ── Bulk endpoint tests ───────────────────────────────────────────────────────


class TestRadarDetectionsBulk:
    def test_bulk_auth_wrong_key_returns_401(self, client):
        r = client.post(
            "/api/radar/detections/bulk",
            json={"nodes": []},
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_bulk_two_nodes_registered_and_frames_queued(self, client):
        payload = {
            "nodes": [
                {
                    "node_id": "bulk-node-a",
                    "frames": [{"timestamp": 1.0, "data": "x"}],
                },
                {
                    "node_id": "bulk-node-b",
                    "frames": [{"timestamp": 2.0, "data": "y"}],
                },
            ]
        }
        r = client.post(
            "/api/radar/detections/bulk",
            json=payload,
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["nodes_registered"] == 2
        assert body["frames_queued"] == 2

    def test_bulk_frame_without_timestamp_skipped(self, client):
        payload = {
            "nodes": [
                {
                    "node_id": "bulk-skip-node",
                    "frames": [{"no_timestamp": True}],
                },
            ]
        }
        r = client.post(
            "/api/radar/detections/bulk",
            json=payload,
            headers=HEADERS_OK,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["frames_queued"] == 0
        assert body["nodes_registered"] == 1

    def test_bulk_changed_config_re_registers_the_node(self, client):
        """A node that moves must not keep its old geometry until a restart."""
        from core import state

        first = {
            "nodes": [
                {
                    "node_id": "bulk-moving-node",
                    "config": {"rx_lat": 33.9, "rx_lon": -84.6, "tx_lat": 34.0, "tx_lon": -84.7},
                    "frames": [{"timestamp": 1, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }
        second = {
            "nodes": [
                {
                    "node_id": "bulk-moving-node",
                    "config": {"rx_lat": 40.0, "rx_lon": -84.6, "tx_lat": 34.0, "tx_lon": -84.7},
                    "frames": [{"timestamp": 2, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }

        assert client.post("/api/radar/detections/bulk", json=first, headers=HEADERS_OK).json()["nodes_registered"] == 1

        # The live TestClient runs frame_processor_loop, which can rebuild and
        # re-cache the pipeline from the queued frame at any point after
        # eviction. A sentinel keeps the check race-free: whichever way that
        # race falls, the stale pipeline cannot still be the cached entry.
        sentinel = object()
        state.node_pipelines["bulk-moving-node"] = sentinel
        r = client.post("/api/radar/detections/bulk", json=second, headers=HEADERS_OK)

        assert r.json()["nodes_registered"] == 1
        assert state.connected_nodes["bulk-moving-node"]["config"]["rx_lat"] == 40.0
        assert state.connected_nodes["bulk-moving-node"]["config_hash"] != ""
        assert state.node_pipelines.get("bulk-moving-node") is not sentinel

    def test_bulk_unchanged_config_does_not_re_register(self, client):
        body = {
            "nodes": [
                {
                    "node_id": "bulk-still-node",
                    "config": {"rx_lat": 33.9, "rx_lon": -84.6, "tx_lat": 34.0, "tx_lon": -84.7},
                    "frames": [{"timestamp": 1, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }

        assert client.post("/api/radar/detections/bulk", json=body, headers=HEADERS_OK).json()["nodes_registered"] == 1
        assert client.post("/api/radar/detections/bulk", json=body, headers=HEADERS_OK).json()["nodes_registered"] == 0

    def test_bulk_missing_config_on_a_known_node_does_not_re_register(self, client):
        """A follow-up batch that omits config entirely is silent about the
        node's geometry, not a claim that its config is now the trivial
        {"node_id": ...} dict. That trivial dict never hashes to match the
        stored config, so it used to re-register the node on every config-less
        call and evict its cached pipeline, silently falling it back to the
        shared default receiver and transmitter."""
        from core import state

        node_id = "bulk-quiet-node"
        first = {
            "nodes": [
                {
                    "node_id": node_id,
                    "config": {"rx_lat": 33.9, "rx_lon": -84.6, "tx_lat": 34.0, "tx_lon": -84.7},
                    "frames": [{"timestamp": 1, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }
        second = {"nodes": [{"node_id": node_id}]}

        assert client.post("/api/radar/detections/bulk", json=first, headers=HEADERS_OK).json()["nodes_registered"] == 1
        stored_hash = state.connected_nodes[node_id]["config_hash"]

        r = client.post("/api/radar/detections/bulk", json=second, headers=HEADERS_OK)

        assert r.json()["nodes_registered"] == 0
        assert state.connected_nodes[node_id]["config"]["rx_lat"] == 33.9
        assert state.connected_nodes[node_id]["config_hash"] == stored_hash

    def test_bulk_does_not_overwrite_a_node_registered_by_another_path(self, client):
        """A caller holding RADAR_API_KEY must not be able to strip a live v1 or
        TCP node's geometry by naming it in a bulk entry: only a node this
        endpoint itself registered may be re-registered on a hash mismatch."""
        from core import state

        node_id = "bulk-not-mine"
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {
                "config_hash": "original-hash",
                "config": {"rx_lat": 1.0, "rx_lon": 2.0},
                "status": "active",
                "last_heartbeat": "",
                "peer": "v1",
                "is_synthetic": False,
                "capabilities": {"adsb_report": True},
            }
        body = {
            "nodes": [
                {
                    "node_id": node_id,
                    "config": {"rx_lat": 99.0, "rx_lon": 99.0},
                    "frames": [{"timestamp": 1, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }

        r = client.post("/api/radar/detections/bulk", json=body, headers=HEADERS_OK)

        assert r.status_code == 200
        body = r.json()
        assert body["nodes_registered"] == 0
        assert body["frames_queued"] == 1
        assert state.connected_nodes[node_id]["config"]["rx_lat"] == 1.0
        assert state.connected_nodes[node_id]["config_hash"] == "original-hash"
        assert state.connected_nodes[node_id]["peer"] == "v1"

    def test_bulk_first_registration_of_a_new_node_is_unaffected(self, client):
        """The gate must not block a node's first bulk registration, only a
        re-registration of a node created elsewhere."""
        body = {
            "nodes": [
                {
                    "node_id": "bulk-fresh-node",
                    "config": {"rx_lat": 5.0, "rx_lon": 6.0},
                    "frames": [{"timestamp": 1, "delay": [1.0], "doppler": [2.0], "snr": [3.0]}],
                }
            ]
        }

        r = client.post("/api/radar/detections/bulk", json=body, headers=HEADERS_OK)

        assert r.json()["nodes_registered"] == 1
        from core import state

        assert state.connected_nodes["bulk-fresh-node"]["peer"] == "http-bulk"


# ── _check_rate_limit unit tests ─────────────────────────────────────────────


class TestCheckRateLimit:
    def test_first_call_succeeds(self):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        # Should not raise
        radar_mod._check_rate_limit("192.0.2.1")

    def test_exceeding_rate_limit_raises_429(self, monkeypatch):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        monkeypatch.setattr(radar_mod, "_RATE_LIMIT", 2)

        ip = "192.0.2.2"
        radar_mod._check_rate_limit(ip)  # call 1
        radar_mod._check_rate_limit(ip)  # call 2 — hits limit on next
        with pytest.raises(HTTPException) as exc_info:
            radar_mod._check_rate_limit(ip)  # call 3 → 429
        assert exc_info.value.status_code == 429

    def test_expired_timestamps_cleaned_up(self, monkeypatch):
        import routes.radar as radar_mod
        from core import state

        state.rate_buckets.clear()
        ip = "192.0.2.3"

        # Inject an old timestamp well outside the rate window
        old_ts = time.monotonic() - 9999
        state.rate_buckets[ip].append(old_ts)

        # _check_rate_limit should evict the expired entry; after the call
        # the bucket should contain exactly one fresh timestamp (just added).
        radar_mod._check_rate_limit(ip)
        bucket = state.rate_buckets.get(ip, [])
        # Only the fresh timestamp added at the end of _check_rate_limit remains
        assert len(bucket) == 1
        assert bucket[0] > old_ts
