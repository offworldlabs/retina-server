"""Tests for analytics API routes — analytics, overlaps, accuracy, anomalies, adsb-report."""

import asyncio
import time

import orjson
import pytest

from core import state, users
from services.tasks.aircraft_flush import _flush_once
from services.tasks.analytics_refresh import _refresh_accuracy_stats, _refresh_analytics_and_nodes
from tests.ownership_helpers import register_node_row
from tests.route_helpers import entitled_key_headers

# ── Analytics ─────────────────────────────────────────────────────────────────


class TestAnalytics:
    def test_analytics_returns_bytes(self, client):
        r = client.get("/api/radar/analytics")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"

    def test_analytics_real_only(self, client):
        import orjson

        test_data = orjson.dumps({"nodes": {"real-1": {"metrics": {}}}})
        orig = state.latest_analytics_real_bytes
        state.latest_analytics_real_bytes = test_data
        try:
            r = client.get("/api/radar/analytics?real_only=true")
            assert r.status_code == 200
            assert r.json()["nodes"]["real-1"] == {"metrics": {}}
        finally:
            state.latest_analytics_real_bytes = orig

    def test_node_analytics_not_found(self, client):
        r = client.get("/api/radar/analytics/nonexistent-node-xyz")
        assert r.status_code == 404

    def test_node_analytics_found(self, client):
        """Register a node and verify it returns analytics.

        A synthetic node publishes under its own id, so that id is its ref and
        the route answers to it; the payload names it as a ref either way.
        """
        state.node_analytics.register_node("test-an-1", {"name": "Test"})
        try:
            r = client.get("/api/radar/analytics/test-an-1")
            assert r.status_code == 200
            body = r.json()
            assert body["node_ref"] == "test-an-1"
            assert "node_id" not in body
        finally:
            state.node_analytics.metrics.pop("test-an-1", None)
            state.node_analytics.trust_scores.pop("test-an-1", None)


# ── ADS-B Report ─────────────────────────────────────────────────────────────


class TestAdsbReport:
    _HEADERS = {"X-API-Key": "test-key-abc123"}

    def test_missing_fields(self, client):
        r = client.post("/api/radar/analytics/adsb-report", json={"node_id": "x"}, headers=self._HEADERS)
        assert r.status_code == 400
        assert "Missing" in r.json()["detail"]

    def test_valid_report(self, client):
        r = client.post(
            "/api/radar/analytics/adsb-report",
            json={
                "node_id": "test-adsb-1",
                "predicted_delay": 100.0,
                "measured_delay": 102.0,
                "adsb_hex": "ABC123",
            },
            headers=self._HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "recorded"
        assert "trust_score" in body


# ── Overlaps ─────────────────────────────────────────────────────────────────


class TestOverlaps:
    def test_overlaps_returns_bytes(self, client):
        r = client.get("/api/radar/association/overlaps")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/json"


# ── Accuracy ─────────────────────────────────────────────────────────────────


class TestAccuracy:
    def test_accuracy_returns_bytes(self, client):
        r = client.get("/api/radar/accuracy")
        assert r.status_code == 200

    def test_accuracy_with_data(self, client):
        import orjson

        data = orjson.dumps({"mean_error_km": 2.5, "p95_error_km": 5.0})
        orig = state.latest_accuracy_bytes
        state.latest_accuracy_bytes = data
        try:
            r = client.get("/api/radar/accuracy")
            assert r.status_code == 200
            assert r.json()["mean_error_km"] == 2.5
        finally:
            state.latest_accuracy_bytes = orig


# ── Association status ────────────────────────────────────────────────────────


class TestAssociationStatus:
    def test_status_returns_expected_fields(self, client):
        r = client.get("/api/radar/association/status")
        assert r.status_code == 200
        body = r.json()
        assert "registered_nodes" in body
        assert "overlap_zones" in body
        assert "overlaps" in body

    def test_track_pairs_block_reports_the_live_counters(self, client):
        """superseded and cluster_splits belong to the LIVE block, not the
        inline-only one.

        They used to be structurally zero in production because the only
        exclusivity stage ran on a chi2 nothing computes with cv_fit=None.
        The deferred path now prunes on implied-velocity conflict and splits
        clusters that hold two tracks of one node, so both counters move on a
        live fleet and reading them as "inline only" would be wrong.
        """
        _a = state.node_associator
        _a.track_pairs_superseded += 5
        _a.cluster_splits += 3
        try:
            body = client.get("/api/radar/association/status").json()
            assert body["track_pairs"].keys() == {
                "gated",
                "unfitted",
                "deferred",
                "superseded",
                "cluster_splits",
            }
            assert body["track_pairs"]["superseded"] == 5
            assert body["track_pairs"]["cluster_splits"] == 3
            assert body["track_pairs_inline_only"].keys() == {"accepted", "rejected"}
        finally:
            _a.track_pairs_superseded -= 5
            _a.cluster_splits -= 3

    def test_status_reports_world_skipped_pairs(self, client):
        """The world gate on overlap zones is otherwise invisible: a fleet
        whose sim/real pairs are being refused looks exactly like a fleet whose
        pairs never overlapped, and only this counter separates them."""
        _a = state.node_associator
        _a.assoc_world_skipped_pairs += 7
        try:
            body = client.get("/api/radar/association/status").json()
            assert body["assoc_world_skipped_pairs"] == 7
        finally:
            state._reset_for_tests()

    def test_status_includes_claiming_block(self, client):
        """Top-down claiming (ASSOC_CLAIM_MODE) since boot — off by default
        in tests, so this pins the shape rather than any particular mode."""
        r = client.get("/api/radar/association/status")
        assert r.status_code == 200
        claiming = r.json()["claiming"]
        assert claiming.keys() == {
            "mode",
            "rounds",
            "matched",
            "conflicts",
            "anchored_inputs",
            "tracklets_excluded",
        }
        assert claiming["mode"] == state.node_associator.claim_mode

    def test_claiming_counters_reflect_the_associator(self, client):
        _a = state.node_associator
        _a.claim_rounds += 3
        _a.claims_matched += 2
        _a.claim_conflicts += 1
        _a.anchored_inputs_emitted += 1
        _a.tracklets_excluded += 4
        try:
            body = client.get("/api/radar/association/status").json()
            assert body["claiming"] == {
                "mode": _a.claim_mode,
                "rounds": 3,
                "matched": 2,
                "conflicts": 1,
                "anchored_inputs": 1,
                "tracklets_excluded": 4,
            }
        finally:
            state._reset_for_tests()


# ── Anomalies ────────────────────────────────────────────────────────────────


class TestAnomalies:
    def test_anomalies_empty(self, client):
        r = client.get("/api/radar/anomalies")
        assert r.status_code == 200
        body = r.json()
        assert "summary" in body
        assert "by_type" in body
        assert "timeline" in body
        assert "geographic_clusters" in body

    def test_anomalies_with_data(self, client):
        import time

        with state.anomaly_lock:
            state.anomaly_log.append(
                {
                    "ts": time.time(),
                    "hex": "ANOM01",
                    "reason": "speed_violation",
                    "lat": 33.45,
                    "lon": -112.07,
                }
            )
            state.anomaly_hexes.add("ANOM01")
        try:
            r = client.get("/api/radar/anomalies")
            assert r.status_code == 200
            body = r.json()
            assert body["summary"]["active_count"] >= 1
            assert body["summary"]["unique_hexes"] >= 1
        finally:
            with state.anomaly_lock:
                state.anomaly_hexes.discard("ANOM01")
                state.anomaly_log.clear()


# ── Per-node analytics ────────────────────────────────────────────────────────


class TestPerNodeAnalyticsRoute:
    """GET /api/radar/analytics/{node_ref} is built fresh, not from the cache.

    The cached listing's coverage is in test_analytics_refresh.py; this is the
    other surface, which has to be wired separately or the two disagree.

    Addressed by the ref as well as answered in it: the route resolves its path
    parameter through services/node_refs.py, so the private id is not a way in.
    """

    _ID = "alpha-site-node"
    _REF = "nde0123456789ab"

    @pytest.fixture()
    def _a_registered_node(self):
        """A registry row for a node whose id has no synthetic prefix."""
        import asyncio

        from core.nodes import Node
        from core.users import async_session_maker
        from services import node_refs

        async def _go():
            async with async_session_maker() as session:
                session.add(Node(node_id=self._ID, node_ref=self._REF))
                await session.commit()

        asyncio.run(_go())
        node_refs._reset_for_tests()
        state.node_analytics.register_node(self._ID, {"rx_lat": 34.85, "rx_lon": -82.40, "max_range_km": 50})
        yield
        state.node_analytics.retire_node(self._ID)
        node_refs._reset_for_tests()

    def test_the_route_carries_the_ref(self, client, _a_registered_node):
        body = client.get(f"/api/radar/analytics/{self._REF}").json()
        assert body["node_ref"] == self._REF
        assert body["node_ref"] != self._ID

    def test_the_id_the_ref_stands_in_for_is_not_an_address_for_it(self, client, _a_registered_node):
        assert client.get(f"/api/radar/analytics/{self._ID}").status_code == 404


# ── A keyed caller ────────────────────────────────────────────────────────────


def test_a_key_is_shown_no_synthetic_node_nor_the_simulation(client):
    """Internal access (the suite's anonymous administrator) sees the synthetic
    node and the simulation's own records beside the real ones; a key does not."""
    for node_id, synthetic in (("test-real-1", False), ("test-real-2", False), ("synth-node-1", True)):
        state.connected_nodes[node_id] = {"is_synthetic": synthetic, "status": "active"}
    state.node_analytics.register_node("synth-node-1", {"name": "Sim"})
    state.latest_analytics_bytes = orjson.dumps({"nodes": {"test-real-1": {}, "synth-node-1": {}}})
    state.latest_analytics_real_bytes = orjson.dumps({"nodes": {"test-real-1": {}}})
    zone = {"node_a": "test-real-1", "node_b": "test-real-2", "grid_points": 4, "has_overlap": True}
    registered = ["test-real-1", "test-real-2", "synth-node-1"]
    zones = [zone, {**zone, "node_b": "synth-node-1"}]
    state.latest_overlaps_bytes = orjson.dumps({"overlaps": zones, "registered_nodes": registered})
    state.anomaly_log.append({"ts": 1.0, "hex": "SIM001", "reason": "anomalous_behavior"})
    state.anomaly_hexes.update({"SIM001", "SYNTH01"})
    flagged = {"is_anomalous": True, "anomaly_types": ["supersonic"]}
    real, synthetic = {"hex": "REAL01", "node_id": "test-real-1"}, {"hex": "SYNTH01", "node_id": "synth-node-1"}
    _flush_once({"aircraft": [{**real, **flagged}, {**synthetic, **flagged}]})
    state.ground_truth_meta["SIM001"] = {"object_type": "aircraft"}
    state.accuracy_samples.extend(
        [
            {"hex": "4ca1e2", "error_km": 0.5, "position_source": "solver_adsb_seed"},
            {"hex": "SIM001", "error_km": 3.0, "position_source": "solver_adsb_seed"},
            {"hex": "mn0001", "error_km": 2.0, "position_source": "multinode_solve", "lane": "dark"},
        ]
    )
    _refresh_accuracy_stats()
    key = asyncio.run(entitled_key_headers("analyst@example.com"))

    assert client.get("/api/radar/analytics?real_only=false").json()["nodes"].keys() == {"test-real-1", "synth-node-1"}
    assert client.get("/api/radar/analytics?real_only=false", headers=key).json() == {"nodes": {"test-real-1": {}}}
    assert client.get("/api/radar/analytics/synth-node-1").status_code == 200
    unknown = client.get("/api/radar/analytics/nde0000000000", headers=key)
    synthetic = client.get("/api/radar/analytics/synth-node-1", headers=key)
    assert (synthetic.status_code, synthetic.content) == (404, unknown.content) == (404, b'{"detail":"Node not found"}')
    assert len(client.get("/api/radar/association/overlaps").json()["overlaps"]) == 2
    overlaps = client.get("/api/radar/association/overlaps", headers=key).json()
    assert overlaps == {"overlaps": [zone], "registered_nodes": ["test-real-1", "test-real-2"]}
    assert client.get("/api/radar/anomalies").json()["summary"]["total_events"] == 1
    anomalies = client.get("/api/radar/anomalies", headers=key).json()
    summary = {"active_count": 1, "total_events": 0, "unique_hexes": 0, "most_common_type": "supersonic"}
    assert (anomalies["summary"], anomalies["by_type"], anomalies["recent_events"]) == (summary, {"supersonic": 1}, [])
    assert client.get("/api/radar/accuracy").json()["n_samples"] == 3
    stats = {"n_samples": 1, "mean_km": 0.5, "median_km": 0.5, "p95_km": 0.5, "max_km": 0.5}
    expected = {**stats, "by_source": {"solver_adsb_seed": stats}, "velocity": {}}
    assert client.get("/api/radar/accuracy", headers=key).json() == expected


def test_a_key_reads_the_analytics_of_a_mixed_fleet_with_the_synthetic_nodes_named_by_nothing(client, penalties_on):
    """The cross-node analysis is the whole fleet's. A key reads it as it reads a
    private node: no pair or blocked entry for the synthetic one, and a real
    node's penalty quoting it reads "[unpublished node]"."""
    real, synthetic = "ret2b3c4d5e", "synth-mast-01"
    ref = asyncio.run(register_node_row(real))
    site = {"rx_lat": 52.33, "tx_lat": 52.4103, "tx_lon": 0.3551, "fc_hz": 195_936_000.0, "max_range_km": 60.0}
    frame = {"timestamp": int(time.time() * 1000), "delay": [20.0, 30.0], "doppler": [5.0, -5.0], "snr": [9.0, 9.0]}
    for node_id, rx_lon in ((real, 0.2577), (synthetic, 0.4122)):
        state.connected_nodes[node_id] = {"is_synthetic": node_id == synthetic, "status": "active"}
        state.node_analytics.register_node(node_id, {"node_id": node_id, **site, "rx_lon": rx_lon})
        state.node_analytics.record_detection_frame(node_id, frame)
    state.node_analytics.reputations[synthetic].blocked = True
    state.node_analytics.reputations[real].evaluate_neighbour_consistency(0.0, 0.9, neighbour_id=synthetic)
    _refresh_analytics_and_nodes()

    headers = asyncio.run(entitled_key_headers("analyst@example.com"))
    listing = client.get("/api/radar/analytics", headers=headers)
    single = client.get(f"/api/radar/analytics/{ref}", headers=headers)

    assert synthetic not in listing.text, listing.json()["cross_node"]
    assert synthetic not in single.text
    listed = listing.json()["nodes"][ref]["reputation"]["recent_penalties"]
    read = single.json()["reputation"]["recent_penalties"]
    assert (
        [p["reason"] for p in listed]
        == [p["reason"] for p in read]
        == ["Inconsistent with trusted neighbour [unpublished node] (overlap=0.00)"]
    )


def test_the_map_reads_real_nodes_without_credentials_and_only_what_it_draws(client, monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    location = {"rx_lat": 52.5, "rx_lon": 0.5}
    state.latest_nodes_bytes = orjson.dumps(
        {
            "nodes": {
                "ret1": {
                    "status": "active",
                    "node_ref": "ret1",
                    "name": "Roof",
                    "is_synthetic": False,
                    "location": location,
                },
                "sim1": {"status": "active", "node_ref": "sim1", "is_synthetic": True, "location": location},
            }
        }
    )
    state.latest_analytics_real_bytes = orjson.dumps(
        {
            "nodes": {
                "ret1": {
                    "metrics": {"snr": 9},
                    "detection_area": {
                        "rx": {"lat": 52.5, "lon": 0.5, "alt": 30, "extra": 1},
                        "tx": {"lat": 52.7, "lon": 0.4, "name": "mast"},
                        "beam_azimuth_deg": 90,
                        "max_range_km": 60,
                        "grid": [[1]],
                    },
                    "empirical_coverage": {
                        "polygon": [[0, 0]],
                        "n_points": 7,
                        "polygon_source": "empirical",
                        "raw": [1],
                    },
                }
            }
        }
    )

    r = client.get("/api/map/nodes")

    assert r.status_code == 200
    assert r.json() == {
        "nodes": {
            "ret1": {
                "node_ref": "ret1",
                "status": "active",
                "is_synthetic": False,
                "location": location,
                "detection_area": {
                    "rx": {"lat": 52.5, "lon": 0.5, "alt": 30},
                    "tx": {"lat": 52.7, "lon": 0.4},
                    "beam_azimuth_deg": 90,
                    "max_range_km": 60,
                },
                "empirical_coverage": {"polygon": [[0, 0]], "n_points": 7, "polygon_source": "empirical"},
            }
        }
    }
