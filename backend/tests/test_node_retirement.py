"""Retiring nodes that have left the fleet.

The per-node stores in the server accumulate without bound.  A fleet layout
change on staging left 10 receivers still held in analytics, custody and
reputation, still being iterated by the refresh task and still being written to
disk on every save, with the node count reading 26 against 16 live.  Pruning
removes stale test-prefixed nodes from the fleet registry only after 7 days
disconnected; retirement is the only path that clears every store.
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from config import constants  # noqa: E402
from core import state  # noqa: E402
from main import app  # noqa: E402
from services import node_retirement  # noqa: E402

_CFG = dict(rx_lat=34.85, rx_lon=-82.40, tx_lat=34.90, tx_lon=-82.30, max_range_km=50, max_bistatic_range_km=50)


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def fleet():
    """One connected node and one that has left, across every store."""
    saved = (
        dict(state.connected_nodes),
        dict(state.node_identities),
        dict(state.chain_entries),
        dict(state.iq_commitments),
        dict(state.node_pipelines),
    )
    state.connected_nodes.clear()
    state.node_identities.clear()
    state.chain_entries.clear()
    state.iq_commitments.clear()
    state.node_pipelines.clear()

    state.connected_nodes["live-node"] = {"status": "active"}
    state.node_analytics.register_node("live-node", dict(_CFG))
    state.node_analytics.register_node("departed", dict(_CFG))
    state.chain_entries["departed"] = [{"seq": 1}, {"seq": 2}]
    state.iq_commitments["departed"] = [{"hash": "abc"}]

    yield

    state.node_analytics.retire_node("departed")
    state.node_analytics.retire_node("live-node")
    state.connected_nodes.clear()
    state.connected_nodes.update(saved[0])
    state.node_identities.clear()
    state.node_identities.update(saved[1])
    state.chain_entries.clear()
    state.chain_entries.update(saved[2])
    state.iq_commitments.clear()
    state.iq_commitments.update(saved[3])
    state.node_pipelines.clear()
    state.node_pipelines.update(saved[4])


class TestStaleDetection:
    def test_a_departed_node_is_reported_stale(self, fleet):
        assert "departed" in node_retirement.stale_node_ids()

    def test_a_connected_node_is_not(self, fleet):
        assert "live-node" not in node_retirement.stale_node_ids()

    def test_custody_alone_is_enough_to_be_held(self, fleet):
        """A node can survive in one store and not another; reporting the
        union is what makes the cleanup complete."""
        state.chain_entries["custody-only"] = [{"seq": 1}]
        try:
            assert "custody-only" in node_retirement.stale_node_ids()
        finally:
            state.chain_entries.pop("custody-only", None)


class TestRetireNode:
    def test_it_clears_analytics_and_custody(self, fleet):
        report = node_retirement.retire_node("departed")

        assert "departed" not in state.node_analytics.trust_scores
        assert "departed" not in state.node_analytics.detection_areas
        assert "departed" not in state.chain_entries
        assert "departed" not in state.iq_commitments
        assert report["custody"]["chain_entries"] == 2
        assert report["custody"]["iq_commitments"] == 1

    def test_it_refuses_a_connected_node(self, fleet):
        """The next registration would undo it, and it would strip the custody
        chain of a live source mid-session."""
        with pytest.raises(node_retirement.NodeStillConnected):
            node_retirement.retire_node("live-node")
        assert "live-node" in state.node_analytics.trust_scores

    def test_force_overrides_the_refusal(self, fleet):
        node_retirement.retire_node("live-node", force=True)
        assert "live-node" not in state.node_analytics.trust_scores

    def test_it_evicts_the_node_from_the_fleet_registry(self, fleet):
        """connected_nodes is what /api/radar/nodes counts and what
        live_node_ids reads, so a node left in it is neither forgotten nor
        ever eligible for the stale sweep again."""
        report = node_retirement.retire_node("live-node", force=True)

        assert "live-node" not in state.connected_nodes
        assert report["was_connected"] is True
        assert "live-node" not in node_retirement.live_node_ids()

    def test_it_evicts_the_cached_pipeline(self, fleet):
        state.node_pipelines["departed"] = object()
        report = node_retirement.retire_node("departed")

        assert "departed" not in state.node_pipelines
        assert report["pipeline_evicted"] is True

    def test_retiring_a_node_that_was_never_registered_is_a_no_op(self, fleet):
        report = node_retirement.retire_node("never-existed")

        assert report["was_connected"] is False
        assert report["pipeline_evicted"] is False

    def test_retire_stale_clears_everything_absent(self, fleet):
        result = node_retirement.retire_stale_nodes()

        assert result["count"] >= 1
        assert "departed" in [r["node_id"] for r in result["retired"]]
        assert node_retirement.stale_node_ids() == []
        assert "live-node" in state.node_analytics.trust_scores


class TestAdminRoutes:
    def test_stale_listing_names_the_departed_node(self, client, fleet):
        r = client.get("/api/admin/nodes/stale")
        assert r.status_code == 200
        body = r.json()
        assert "departed" in body["stale_nodes"]
        assert body["live_nodes"] == 1

    def test_delete_retires_the_node(self, client, fleet):
        r = client.delete("/api/admin/nodes/departed/state")
        assert r.status_code == 200
        assert r.json()["custody"]["chain_entries"] == 2
        assert "departed" not in state.node_analytics.trust_scores

    def test_delete_conflicts_on_a_connected_node(self, client, fleet):
        r = client.delete("/api/admin/nodes/live-node/state")
        assert r.status_code == 409
        assert "live-node" in state.node_analytics.trust_scores

    def test_delete_with_force_retires_a_connected_node(self, client, fleet):
        r = client.delete("/api/admin/nodes/live-node/state?force=true")
        assert r.status_code == 200
        assert "live-node" not in state.node_analytics.trust_scores

    def test_retire_stale_sweeps_and_reports(self, client, fleet):
        r = client.post("/api/admin/nodes/retire-stale")
        assert r.status_code == 200
        assert "departed" in [x["node_id"] for x in r.json()["retired"]]
        assert client.get("/api/admin/nodes/stale").json()["count"] == 0


class TestForceRetireAllowlist:
    def test_force_is_unrestricted_when_no_prefixes_are_configured(self, fleet, monkeypatch):
        """Production's posture: an operator retiring a decommissioned board
        needs force, because nothing else ever evicts it from the registry."""
        monkeypatch.setattr(constants, "NODE_FORCE_RETIRE_PREFIXES", ())
        node_retirement.retire_node("live-node", force=True)
        assert "live-node" not in state.connected_nodes

    def test_force_is_refused_outside_the_allowlist(self, fleet, monkeypatch):
        monkeypatch.setattr(constants, "NODE_FORCE_RETIRE_PREFIXES", ("e2e-",))
        with pytest.raises(node_retirement.ForceRetireNotAllowed):
            node_retirement.retire_node("live-node", force=True)
        assert "live-node" in state.connected_nodes

    def test_force_is_permitted_inside_the_allowlist(self, fleet, monkeypatch):
        monkeypatch.setattr(constants, "NODE_FORCE_RETIRE_PREFIXES", ("e2e-",))
        state.connected_nodes["e2e-bulk-a-abc"] = {"status": "active"}
        node_retirement.retire_node("e2e-bulk-a-abc", force=True)
        assert "e2e-bulk-a-abc" not in state.connected_nodes

    def test_an_unforced_retire_is_never_restricted(self, fleet, monkeypatch):
        """The allowlist gates force alone. Retiring a genuinely stale node
        must keep working whatever the node is called."""
        monkeypatch.setattr(constants, "NODE_FORCE_RETIRE_PREFIXES", ("e2e-",))
        node_retirement.retire_node("departed")
        assert "departed" not in state.node_analytics.trust_scores

    def test_the_route_refuses_with_403_and_names_the_prefixes(self, client, fleet, monkeypatch):
        monkeypatch.setattr(constants, "NODE_FORCE_RETIRE_PREFIXES", ("e2e-", "synth-e2e-"))
        r = client.delete("/api/admin/nodes/live-node/state?force=true")

        assert r.status_code == 403
        assert "e2e-" in r.json()["detail"]
        assert "live-node" in state.connected_nodes


class TestParsePrefixList:
    """Every allowlist test above monkeypatches the parsed tuple directly, so
    this is the only coverage of the parse itself, the one coupling between
    staging's compose value and the E2E teardown working at all."""

    def test_a_normal_two_prefix_value(self):
        assert constants._parse_prefix_list("e2e-,synth-e2e-") == ("e2e-", "synth-e2e-")

    def test_an_empty_string_yields_no_prefixes(self):
        assert constants._parse_prefix_list("") == ()

    def test_whitespace_around_entries_is_stripped(self):
        assert constants._parse_prefix_list(" e2e- , synth-e2e- ") == ("e2e-", "synth-e2e-")

    def test_a_trailing_comma_does_not_yield_an_empty_prefix(self):
        """An empty prefix would match every node id via str.startswith("")."""
        assert constants._parse_prefix_list("e2e-,") == ("e2e-",)
