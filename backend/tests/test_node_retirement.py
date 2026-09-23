"""Retiring nodes that have left the fleet.

The per-node stores in the server accumulate without bound.  A fleet layout
change on staging left 10 receivers still held in analytics, custody and
reputation, still being iterated by the refresh task and still being written to
disk on every save, with the node count reading 26 against 16 live.  Pruning
removes stale test-prefixed nodes from the fleet registry only after 7 days
disconnected; retirement is the only path that clears every store.
"""

import asyncio
import contextlib
from datetime import datetime, timezone

import pytest

from config.constants import DISPOSABLE_SWEEP_INTERVAL_S
from core import state
from core.env_parsing import parse_comma_list
from core.task_registry import TASK_EXPECTED_INTERVAL_S
from services import node_retirement
from services.tasks import periodic

_CFG = dict(rx_lat=34.85, rx_lon=-82.40, tx_lat=34.90, tx_lon=-82.30, max_range_km=50, max_bistatic_range_km=50)


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

    def test_a_disconnected_node_is_still_held_and_still_refused(self, fleet):
        """The refusal tests registry presence, not the entry's status field.
        check_node_health only marks a dark receiver disconnected, and the
        prune only removes test-prefixed ids after seven days, so it is still
        held and still needs force.  Reading the check as "currently
        streaming" would let every dark node through unforced."""
        state.connected_nodes["dark-node"] = {"status": "disconnected"}
        state.node_analytics.register_node("dark-node", dict(_CFG))
        try:
            with pytest.raises(node_retirement.NodeStillConnected):
                node_retirement.retire_node("dark-node")
            assert "dark-node" in state.connected_nodes
        finally:
            state.node_analytics.retire_node("dark-node")

    def test_it_evicts_the_cached_pipeline(self, fleet):
        state.node_pipelines["departed"] = object()
        report = node_retirement.retire_node("departed")

        assert "departed" not in state.node_pipelines
        assert report["pipeline_evicted"] is True

    def test_retiring_a_node_that_was_never_registered_is_a_no_op(self, fleet):
        report = node_retirement.retire_node("never-existed")

        assert report["was_connected"] is False
        assert report["pipeline_evicted"] is False

    def test_a_failure_partway_re_raises_and_leaves_the_rest_retirable(self, fleet, monkeypatch):
        """analytics.retire_node deletes files and can fail.  The registry is
        already cleared by then, so the caller must see the error rather than
        a report describing a retirement that only half happened."""

        def boom(_node_id):
            raise OSError("coverage file is not writable")

        monkeypatch.setattr(state.node_analytics, "retire_node", boom)

        with pytest.raises(OSError):
            node_retirement.retire_node("live-node", force=True)

        assert "live-node" not in state.connected_nodes
        # Untouched, so the node is now stale and a retire-stale pass finishes
        # what this call started.
        assert "live-node" in state.node_analytics.trust_scores
        assert "live-node" in node_retirement.stale_node_ids()

    def test_retire_stale_clears_everything_absent(self, fleet):
        result = node_retirement.retire_stale_nodes()

        assert result["count"] >= 1
        assert "departed" in [r["node_id"] for r in result["retired"]]
        assert node_retirement.stale_node_ids() == []
        assert "live-node" in state.node_analytics.trust_scores

    def test_retire_stale_records_a_failure_and_keeps_going(self, fleet, monkeypatch):
        """A genuine fault on one node must not abandon the rest, and must be
        reported separately from a node that merely re-registered."""
        state.node_analytics.register_node("also-departed", dict(_CFG))
        real_retire = node_retirement.retire_node

        def fail_on_departed(node_id, **kwargs):
            if node_id == "departed":
                raise OSError("coverage file is not writable")
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", fail_on_departed)
        try:
            result = node_retirement.retire_stale_nodes()

            assert [r["node_id"] for r in result["failed"]] == ["departed"]
            assert "is not writable" in result["failed"][0]["error"]
            assert result["skipped"] == []
            # The rest of the sweep still ran.
            assert "also-departed" in [r["node_id"] for r in result["retired"]]
        finally:
            state.node_analytics.retire_node("also-departed")

    def test_retire_stale_skips_a_node_that_reconnected_and_finishes_the_rest(self, fleet, monkeypatch):
        """A node registering between the target list and its turn is one node
        changing its mind, not a failed sweep: it is reported and the rest
        still run, rather than aborting partway with the caller unable to tell
        which nodes went."""
        state.node_analytics.register_node("also-departed", dict(_CFG))
        real_retire = node_retirement.retire_node

        def reconnect_departed_first(node_id, **kwargs):
            if node_id == "departed":
                state.connected_nodes["departed"] = {"status": "active"}
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", reconnect_departed_first)
        try:
            result = node_retirement.retire_stale_nodes()

            assert [r["node_id"] for r in result["skipped"]] == ["departed"]
            assert "also-departed" in [r["node_id"] for r in result["retired"]]
            assert result["count"] == len(result["retired"])
            assert result["failed"] == []
            assert "also-departed" not in state.node_analytics.trust_scores
        finally:
            # The fleet fixture restores the other stores but not the analytics
            # manager's, and it cleans only the two ids it created itself. This
            # test adds a third, so a failing assertion above would otherwise
            # leave it in trust_scores for every later test that reads the
            # stale list.
            state.node_analytics.retire_node("also-departed")


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

    def test_a_sweep_that_retires_nothing_and_fails_returns_500(self, client, fleet, monkeypatch):
        """The sweep keeps going past a failure, so the status code is the only
        signal a caller driving this by exit status gets."""

        def boom(_node_id, **_kwargs):
            raise OSError("coverage file is not writable")

        monkeypatch.setattr(node_retirement, "retire_node", boom)
        r = client.post("/api/admin/nodes/retire-stale")

        assert r.status_code == 500
        body = r.json()
        assert body["retired"] == []
        # Membership rather than equality: earlier test modules leave their own
        # ids in the analytics stores, which the fleet fixture does not clear,
        # so the stale list is not just this fixture's nodes.
        assert "departed" in [x["node_id"] for x in body["failed"]]

    def test_a_sweep_that_partly_fails_returns_207(self, client, fleet, monkeypatch):
        state.node_analytics.register_node("also-departed", dict(_CFG))
        real_retire = node_retirement.retire_node

        def fail_on_departed(node_id, **kwargs):
            if node_id == "departed":
                raise OSError("coverage file is not writable")
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", fail_on_departed)
        try:
            r = client.post("/api/admin/nodes/retire-stale")

            assert r.status_code == 207
            body = r.json()
            assert [x["node_id"] for x in body["failed"]] == ["departed"]
            assert "also-departed" in [x["node_id"] for x in body["retired"]]
        finally:
            state.node_analytics.retire_node("also-departed")

    def test_retire_stale_sweeps_and_reports(self, client, fleet):
        r = client.post("/api/admin/nodes/retire-stale")
        assert r.status_code == 200
        assert "departed" in [x["node_id"] for x in r.json()["retired"]]
        assert client.get("/api/admin/nodes/stale").json()["count"] == 0


class TestForceRetireAllowlist:
    def test_force_is_unrestricted_when_no_prefixes_are_configured(self, fleet, monkeypatch):
        """Production's posture: an operator retiring a decommissioned board
        needs force, because nothing else ever evicts it from the registry."""
        monkeypatch.delenv("NODE_FORCE_RETIRE_PREFIXES", raising=False)
        node_retirement.retire_node("live-node", force=True)
        assert "live-node" not in state.connected_nodes

    def test_force_is_refused_outside_the_allowlist(self, fleet, monkeypatch):
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        with pytest.raises(node_retirement.ForceRetireNotAllowed):
            node_retirement.retire_node("live-node", force=True)
        assert "live-node" in state.connected_nodes

    def test_force_is_permitted_inside_the_allowlist(self, fleet, monkeypatch):
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        state.connected_nodes["e2e-bulk-a-abc"] = {"status": "active"}
        node_retirement.retire_node("e2e-bulk-a-abc", force=True)
        assert "e2e-bulk-a-abc" not in state.connected_nodes

    def test_an_unforced_retire_is_never_restricted(self, fleet, monkeypatch):
        """The allowlist gates force alone. Retiring a genuinely stale node
        must keep working whatever the node is called."""
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        node_retirement.retire_node("departed")
        assert "departed" not in state.node_analytics.trust_scores

    def test_the_route_refuses_with_403_and_names_the_prefixes(self, client, fleet, monkeypatch):
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-,synth-e2e-")
        r = client.delete("/api/admin/nodes/live-node/state?force=true")

        assert r.status_code == 403
        assert "e2e-" in r.json()["detail"]
        assert "live-node" in state.connected_nodes

    def test_the_allowlist_gates_a_disconnected_node_too(self, fleet, monkeypatch):
        """Force is needed for a dark receiver, since it is still held, so the
        allowlist has to gate it as well.  A status-based check here would take
        a real decommissioned board out from behind the guard entirely, which
        is the case the guard exists for."""
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        state.connected_nodes["dark-node"] = {"status": "disconnected"}

        with pytest.raises(node_retirement.ForceRetireNotAllowed):
            node_retirement.retire_node("dark-node", force=True)
        assert "dark-node" in state.connected_nodes

    def test_force_on_a_node_already_absent_is_not_restricted(self, fleet, monkeypatch):
        """The allowlist gates force only where force overrides the live-node
        refusal.  `departed` is not in the registry, so it retires without
        force mattering; refusing it would tell the operator to drop a flag
        that changed nothing."""
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        node_retirement.retire_node("departed", force=True)
        assert "departed" not in state.node_analytics.trust_scores

    def test_the_route_permits_a_force_retire_inside_the_allowlist(self, client, fleet, monkeypatch):
        """A configured allowlist plus a force-retire made through the admin
        route rather than by calling retire_node directly."""
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        state.connected_nodes["e2e-bulk-a-abc"] = {"status": "active"}

        r = client.delete("/api/admin/nodes/e2e-bulk-a-abc/state?force=true")

        assert r.status_code == 200
        assert "e2e-bulk-a-abc" not in state.connected_nodes


def _heard(seconds_ago: float, now: float) -> str:
    return datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()


class TestDisposableSweep:
    """Staging names its E2E ids disposable; the server retires them itself
    once they go quiet, since the suite holds no admin identity to do it."""

    NOW = 1_800_000_000.0
    QUIET = node_retirement.DISPOSABLE_QUIET_S

    @pytest.fixture()
    def disposable(self, fleet, monkeypatch):
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-,synth-e2e-")
        monkeypatch.setattr(node_retirement, "_stale_last_pass", set())
        ids = ("e2e-bulk-a-old", "synth-e2e-old", "e2e-real-now")
        for nid in ids:
            state.node_analytics.register_node(nid, dict(_CFG))
        yield
        for nid in ids:
            state.node_analytics.retire_node(nid)

    def sweep(self):
        return node_retirement.retire_disposable_nodes(now=self.NOW)

    def test_nothing_is_swept_where_no_prefixes_are_configured(self, disposable, monkeypatch):
        """Production and the test droplet: an unset allowlist names nothing
        disposable, whereas for force it means no restriction at all."""
        monkeypatch.delenv("NODE_FORCE_RETIRE_PREFIXES")
        assert self.sweep()["count"] == 0
        assert "e2e-bulk-a-old" in state.node_analytics.trust_scores

    def test_a_stale_disposable_node_is_retired_on_its_second_stale_pass(self, disposable):
        assert self.sweep()["count"] == 0
        result = self.sweep()

        assert {"e2e-bulk-a-old", "synth-e2e-old"} <= {r["node_id"] for r in result["retired"]}
        assert "e2e-bulk-a-old" not in state.node_analytics.trust_scores

    def test_a_stale_id_that_registers_between_passes_is_spared(self, disposable):
        """Custody can arrive before registration, so stale is not proof that
        nothing is using an id."""
        self.sweep()
        state.connected_nodes["e2e-real-now"] = {"status": "active", "last_heartbeat": _heard(5, self.NOW)}
        self.sweep()
        assert "e2e-real-now" in state.node_analytics.trust_scores

    def test_a_stale_node_outside_the_prefixes_is_left(self, disposable):
        """A real receiver's history is only discarded by an explicit retire."""
        self.sweep()
        assert "departed" in state.node_analytics.trust_scores

    def test_a_held_disposable_node_is_retired_once_quiet(self, disposable):
        state.connected_nodes["e2e-bulk-a-old"] = {
            "status": "disconnected",
            "last_heartbeat": _heard(self.QUIET + 1, self.NOW),
        }
        self.sweep()
        assert "e2e-bulk-a-old" not in state.connected_nodes
        assert "e2e-bulk-a-old" not in state.node_analytics.trust_scores

    def test_a_disposable_node_heard_recently_is_left_to_its_run(self, disposable):
        """A run still asserting on its node must not lose it mid-test."""
        state.connected_nodes["e2e-real-now"] = {
            "status": "active",
            "last_heartbeat": _heard(self.QUIET - 60, self.NOW),
        }
        self.sweep()
        assert "e2e-real-now" in state.connected_nodes
        assert "e2e-real-now" in state.node_analytics.trust_scores

    def test_a_quiet_node_outside_the_prefixes_is_left(self, disposable):
        state.connected_nodes["dark-node"] = {
            "status": "disconnected",
            "last_heartbeat": _heard(30 * 86400, self.NOW),
        }
        self.sweep()
        assert "dark-node" in state.connected_nodes

    @pytest.mark.parametrize("stamp", ["disconnected_ts", "first_seen_ts"])
    def test_a_node_without_a_heartbeat_is_aged_by_its_other_stamps(self, disposable, stamp):
        state.connected_nodes["e2e-bulk-a-old"] = {"status": "disconnected", stamp: self.NOW - self.QUIET - 1}
        self.sweep()
        assert "e2e-bulk-a-old" not in state.connected_nodes

    def test_a_held_node_that_cannot_be_aged_is_left(self, disposable):
        state.connected_nodes["e2e-bulk-a-old"] = {"status": "disconnected", "last_heartbeat": "not a time"}
        self.sweep()
        assert "e2e-bulk-a-old" in state.connected_nodes

    def test_a_heartbeat_stamp_that_is_not_a_string_leaves_the_node_and_spares_the_sweep(self, disposable):
        """A TCP heartbeat stores the node's own timestamp as sent, which may
        be a number; one such node must not abort the pass for the rest."""
        state.connected_nodes["e2e-bulk-a-old"] = {"status": "active", "last_heartbeat": 1_800_000_000_000}
        self.sweep()
        result = self.sweep()

        assert "e2e-bulk-a-old" in state.connected_nodes
        assert "synth-e2e-old" in [r["node_id"] for r in result["retired"]]

    def test_a_connected_node_without_a_heartbeat_is_not_aged_by_when_it_was_first_seen(self, disposable):
        state.connected_nodes["e2e-real-now"] = {"status": "active", "first_seen_ts": self.NOW - 30 * 86400}
        self.sweep()
        assert "e2e-real-now" in state.connected_nodes

    def test_a_heartbeat_without_an_offset_is_read_as_utc(self):
        """Left naive, it would be read as the host's local time, which a UTC
        host could not tell apart, so the offset itself is what is checked."""
        naive = datetime.fromtimestamp(self.NOW, timezone.utc).replace(tzinfo=None).isoformat()
        heard = node_retirement._parse_heartbeat(naive)
        assert heard.tzinfo == timezone.utc
        assert heard.timestamp() == self.NOW

    def test_a_held_node_heard_again_before_its_turn_is_spared(self, disposable, monkeypatch):
        """The pass can spend a while on stale ids before it reaches the held
        ones, so quietness is judged again at the node's own turn."""
        monkeypatch.setattr(node_retirement, "_stale_last_pass", {"synth-e2e-old"})
        state.connected_nodes["e2e-bulk-a-old"] = {
            "status": "disconnected",
            "last_heartbeat": _heard(self.QUIET + 1, self.NOW),
        }
        real_retire = node_retirement.retire_node

        def beat_while_busy(node_id, **kwargs):
            if node_id == "synth-e2e-old":
                state.connected_nodes["e2e-bulk-a-old"]["last_heartbeat"] = _heard(0, self.NOW)
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", beat_while_busy)
        self.sweep()
        assert "e2e-bulk-a-old" in state.connected_nodes

    def test_a_failure_is_reported_and_the_rest_still_go(self, disposable, monkeypatch):
        real_retire = node_retirement.retire_node

        def fail_on_one(node_id, **kwargs):
            if node_id == "synth-e2e-old":
                raise OSError("coverage file is not writable")
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", fail_on_one)
        self.sweep()
        result = self.sweep()

        assert [r["node_id"] for r in result["failed"]] == ["synth-e2e-old"]
        assert "e2e-bulk-a-old" in [r["node_id"] for r in result["retired"]]


class TestDisposableSweepTask:
    @pytest.fixture(autouse=True)
    def _no_recorded_success(self):
        state.task_last_success.pop("retire_disposable_nodes", None)
        yield
        state.task_last_success.pop("retire_disposable_nodes", None)

    def test_the_loop_reports_success_to_the_task_registry(self, monkeypatch):
        monkeypatch.setattr(periodic, "DISPOSABLE_SWEEP_INTERVAL_S", 0.0)

        async def _until_first_pass():
            task = asyncio.create_task(periodic.retire_disposable_nodes_task())
            for _ in range(500):
                await asyncio.sleep(0.01)
                if "retire_disposable_nodes" in state.task_last_success:
                    break
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_until_first_pass())

        assert "retire_disposable_nodes" in state.task_last_success

    def test_it_is_in_the_staleness_registry(self):
        assert TASK_EXPECTED_INTERVAL_S["retire_disposable_nodes"] == DISPOSABLE_SWEEP_INTERVAL_S


class TestParseCommaList:
    """The allowlist tests above now set the environment variable rather than
    the parsed tuple, so they already exercise this via force_retire_prefixes.
    These stay as direct coverage of the split, strip and empty-segment
    behaviour on its own, against the module that owns it rather than the
    re-export config.constants happens to carry."""

    def test_a_normal_two_prefix_value(self):
        assert parse_comma_list("e2e-,synth-e2e-") == ("e2e-", "synth-e2e-")

    def test_an_empty_string_yields_no_prefixes(self):
        assert parse_comma_list("") == ()

    def test_whitespace_around_entries_is_stripped(self):
        assert parse_comma_list(" e2e- , synth-e2e- ") == ("e2e-", "synth-e2e-")

    def test_a_trailing_comma_does_not_yield_an_empty_prefix(self):
        """An empty prefix would match every node id via str.startswith("")."""
        assert parse_comma_list("e2e-,") == ("e2e-",)
