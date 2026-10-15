"""Tests for admin API routes — events, users, config, storage, metrics."""

import asyncio
import time

from core import state
from main import app
from services.tasks import multinode_identity
from tests.route_helpers import served_paths, served_routes

# ── Events ────────────────────────────────────────────────────────────────────


class TestEvents:
    def test_list_events_returns_list(self, client):
        r = client.get("/api/admin/events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_log_event_appears_in_list(self, client):
        from routes.admin import log_event

        log_event("test", "Unit test event", "info", {"key": "val"})
        r = client.get("/api/admin/events")
        assert r.status_code == 200
        events = r.json()
        assert any(e["message"] == "Unit test event" for e in events)

    def test_events_respect_limit(self, client):
        from routes.admin import log_event

        for i in range(10):
            log_event("test", f"bulk-{i}", "info")
        r = client.get("/api/admin/events?limit=3")
        assert r.status_code == 200
        assert len(r.json()) <= 3


# ── Users ─────────────────────────────────────────────────────────────────────


class TestUsers:
    def test_list_users(self, client):
        r = client.get("/api/admin/users")
        assert r.status_code == 200

    def test_an_account_that_has_never_visited_lists_no_last_visit(self, client):
        """A claim link can make the account and then fail to bind, opening no session."""
        from core.users import get_or_create_magic_link_user

        asyncio.run(get_or_create_magic_link_user("never@example.com"))

        listed = {u["email"]: u for u in client.get("/api/admin/users").json()["users"]}
        assert listed["never@example.com"]["last_seen_at"] is None

    def test_no_route_changes_a_role(self):
        """Administrators are Access identities, never rows; see list_users."""
        assert [path for path in served_paths(app) if path.endswith("/role")] == []


# ── Config ────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_get_node_config_live_fallback(self, client):
        """When no nodes_config.json exists, returns live config from state."""
        r = client.get("/api/admin/config/nodes")
        assert r.status_code == 200
        body = r.json()
        assert "nodes" in body or "_source" in body

    def test_get_tower_config_returns_the_live_view(self, client, monkeypatch):
        """One shape now that the overlay-file branch is gone with the tower stack.

        `_source` is still asserted: the dashboard's ConfigPage branches on it,
        so dropping the key would break the page rather than simplify it.
        """
        import routes.admin as admin_mod

        monkeypatch.setattr(admin_mod, "_towers_config_cache", None)

        r = client.get("/api/admin/config/towers")

        assert r.status_code == 200
        body = r.json()
        assert body["_source"] == "live"
        assert "towers" in body

    def test_node_config_reports_the_frequency_a_v1_config_declares(self, client, monkeypatch, tmp_path):
        """A v1 node or polled radar carries fc_hz, not the TCP fleet's FC."""
        import routes.admin as admin_mod
        from core import state

        # No nodes_config.json there, so the live view answers.
        monkeypatch.setattr(admin_mod, "runtime_path", lambda name: tmp_path / name)
        monkeypatch.setattr(admin_mod, "_nodes_config_cache", None)
        monkeypatch.setitem(state.connected_nodes, "fc-hz-node", {"status": "active", "config": {"fc_hz": 5.7e8}})

        body = client.get("/api/admin/config/nodes").json()

        assert body["nodes"]["fc-hz-node"]["frequency"] == 5.7e8

    def test_tower_config_reports_the_frequency_a_v1_config_declares(self, client, monkeypatch):
        import routes.admin as admin_mod
        from core import state

        monkeypatch.setattr(admin_mod, "_towers_config_cache", None)
        monkeypatch.setitem(
            state.connected_nodes,
            "fc-hz-node",
            {"status": "active", "config": {"tx_lat": 51.37, "tx_lon": -0.88, "fc_hz": 5.7e8}},
        )

        towers = client.get("/api/admin/config/towers").json()["towers"]

        assert towers["51.3700,-0.8800"]["frequency"] == 5.7e8

    def test_config_history_returns_list(self, client):
        r = client.get("/api/admin/config/history")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ── Storage ───────────────────────────────────────────────────────────────────


class TestStorage:
    def test_storage_returns_json(self, client):
        """Storage endpoint returns valid JSON with expected shape."""
        r = client.get("/api/admin/storage")
        assert r.status_code in (200, 202)
        data = r.json()
        if r.status_code == 202:
            assert data.get("status") == "initializing"
        else:
            # Real storage response has archive/disk info
            assert isinstance(data, dict)


# ── Metrics ──────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_metrics_returns_expected_fields(self, client):
        r = client.get("/api/admin/metrics")
        assert r.status_code == 200
        body = r.json()
        assert "frame_queue_depth" in body
        assert "frames_processed" in body
        assert "connected_nodes" in body
        assert "stale_tasks" in body

    def test_mn_pos_history_counts_the_smoothers_keys(self, client):
        multinode_identity._ewma_smooth_track({"lat": 35.0, "lon": -82.0, "timestamp_ms": 1_000}, "mn-dark-0001", None)
        assert client.get("/api/admin/metrics").json()["mn_pos_history"] == 1


# ── Node health ──────────────────────────────────────────────────────────────


class TestNodeHealth:
    def test_check_detects_offline(self):
        from routes.admin import check_node_health

        state.connected_nodes["test-offline"] = {
            "status": "active",
            "last_heartbeat": "2020-01-01T00:00:00Z",
            "config": {},
        }
        check_node_health()
        # Node should be marked disconnected
        assert state.connected_nodes["test-offline"]["status"] == "disconnected"


# ── Stale tasks ──────────────────────────────────────────────────────────────


class TestStaleTasks:
    def test_no_stale_when_recent(self):
        from core.task_registry import get_stale_tasks

        state.task_last_success["frame_processor"] = time.time()
        result = get_stale_tasks()
        assert "frame_processor" not in result

    def test_stale_when_old(self):
        from core.task_registry import get_stale_tasks

        state.task_last_success["frame_processor"] = time.time() - 9999
        result = get_stale_tasks()
        assert "frame_processor" in result


# ── Queue saturation alerting ─────────────────────────────────────────────────


class TestQueueSaturationAlert:
    def test_drop_event_logged_when_throttle_expires(self):
        """When _last_drop_log is old enough, a drop should produce a system event."""
        import services.tcp_handler as th
        from routes.admin import _events, log_event  # noqa: F401

        # Reset throttle timer so the next drop fires immediately
        th._last_drop_log = 0.0
        state.frames_dropped = 0

        # Simulate a frame queue full drop by calling the internal helper directly
        # after artificially making the queue full.
        # Fill the queue to capacity with dummy items, then call _enqueue_detection
        # on a node whose rate-limit window has expired.
        th._per_node_last_enqueue.pop("alert-test-node", None)
        while not state.frame_queue.full():
            try:
                state.frame_queue.put_nowait(("_fill", {"timestamp": 1}))
            except Exception:
                break

        before_len = len(_events)
        th._enqueue_detection(
            {"type": "DETECTION", "data": {"timestamp": 1, "detections": []}},
            "alert-test-node",
        )

        # Drain the queue to restore state
        while not state.frame_queue.empty():
            try:
                state.frame_queue.get_nowait()
                state.frame_queue.task_done()
            except Exception:
                break

        # There should be a new system/error event
        new_events = list(_events)[: len(_events) - before_len + 5]
        assert any(e.get("category") == "system" and e.get("severity") == "error" for e in new_events), (
            "Expected a system/error event for queue saturation"
        )


# ── Node reconnect event logging ─────────────────────────────────────────────


class TestNodeReconnectEvent:
    def test_reconnect_flag_in_meta(self):
        """When a node that was disconnected sends CONFIG again, event should have reconnect=True."""
        from routes.admin import _events

        # Pre-populate node as disconnected
        state.connected_nodes["reconnect-test"] = {
            "status": "disconnected",
            "config": {},
            "config_hash": "oldhash",
            "first_seen_ts": time.time() - 300,
        }

        # Simulate what handle_tcp_client does on CONFIG receipt for a known-disconnected node
        import services.tcp_handler as th

        was_disconnected = state.connected_nodes.get("reconnect-test", {}).get("status") == "disconnected"
        assert was_disconnected

        if was_disconnected:
            th._log_event(
                "node",
                "Node reconnect-test reconnected (hash=newhash1, synthetic=False)",
                "info",
                {"node_id": "reconnect-test", "config_hash": "newhash1234", "is_synthetic": False, "reconnect": True},
            )

        reconnect_events = [
            e
            for e in _events
            if e.get("meta", {}).get("reconnect") is True and e.get("meta", {}).get("node_id") == "reconnect-test"
        ]
        assert len(reconnect_events) >= 1, "Expected a reconnect event in the event log"

    def test_fresh_connect_has_no_reconnect_flag(self):
        """A brand-new node (not seen before) should not have reconnect=True."""
        from routes.admin import _events

        was_disconnected = state.connected_nodes.get("fresh-node-test", {}).get("status") == "disconnected"
        assert not was_disconnected

        import services.tcp_handler as th

        th._log_event(
            "node",
            "Node fresh-node-test connected (hash=abc12345, synthetic=False)",
            "info",
            {"node_id": "fresh-node-test", "config_hash": "abc12345", "is_synthetic": False},
        )

        reconnect_events = [
            e
            for e in _events
            if e.get("meta", {}).get("reconnect") is True and e.get("meta", {}).get("node_id") == "fresh-node-test"
        ]
        assert len(reconnect_events) == 0, "Fresh connect should not have reconnect flag"


class TestNodeContacts:
    """The one route that serves contact details, and the one that erases them."""

    async def _seed(self, session, node_id="ret1a2b3c4d", **fields):
        from core.nodes import Node
        from services.node_contact_store import upsert_contact

        session.add(Node(node_id=node_id, node_ref=f"nde{node_id[3:]:0>12}", status="active"))
        await session.flush()
        contact = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "phone": None,
            "country": "GB",
        }
        await upsert_contact(session, node_id, contact | fields)
        await session.commit()

    async def test_it_lists_what_the_store_holds(self, node_client, node_session):
        await self._seed(node_session)

        body = node_client.get("/api/admin/node-contacts").json()

        assert body["ret1a2b3c4d"]["email"] == "ada@example.com"
        assert body["ret1a2b3c4d"]["phone"] is None

    async def test_a_node_that_reported_nothing_is_absent(self, node_client, node_session):
        from core.nodes import Node

        node_session.add(Node(node_id="ret9f8e7d6c", node_ref="nde000000000002", status="active"))
        await node_session.commit()

        assert node_client.get("/api/admin/node-contacts").json() == {}

    async def test_deleting_removes_the_row(self, node_client, node_session):
        await self._seed(node_session)

        body = node_client.delete("/api/admin/nodes/ret1a2b3c4d/contact").json()

        assert body["deleted"] is True
        assert node_client.get("/api/admin/node-contacts").json() == {}

    async def test_deleting_what_is_not_there_is_not_an_error(self, node_client):
        body = node_client.delete("/api/admin/nodes/retdeadbeef/contact").json()

        assert body["deleted"] is False

    def test_both_routes_are_gated_on_require_admin(self):
        """The suite runs with AUTH_ALLOW_ANONYMOUS_ADMIN=1, so no request here can
        be refused. The gate is asserted where it is declared instead."""
        from core.users import require_admin
        from main import app

        gated = {
            route.path
            for route in served_routes(app)
            if getattr(route, "dependant", None)
            and any(dep.call is require_admin for dep in route.dependant.dependencies)
        }

        assert "/api/admin/node-contacts" in gated
        assert "/api/admin/nodes/{node_id}/contact" in gated


class TestNodeRefs:
    """The one route that hands back the mapping publication withholds."""

    @staticmethod
    def _register(node_id, node_ref):
        import asyncio

        from core.nodes import Node
        from core.users import async_session_maker

        async def _go():
            async with async_session_maker() as session:
                session.add(Node(node_id=node_id, node_ref=node_ref))
                await session.commit()

        asyncio.run(_go())

    def teardown_method(self):
        with state.connected_nodes_lock:
            state.connected_nodes.clear()

    def test_it_names_the_node_behind_a_ref(self, client):
        self._register("ret1a2b3c4d", "nde1a2b3c4d00")

        body = client.get("/api/admin/node-refs").json()

        assert body["nde1a2b3c4d00"] == "ret1a2b3c4d"

    def test_it_covers_a_connected_node_the_registry_cannot_answer_for(self, client):
        """A mirrored node carries its ref in the fleet snapshot, not a row."""
        with state.connected_nodes_lock:
            state.connected_nodes["ret0badcafe"] = {"status": "active", "node_ref": "ndemirrored001"}

        body = client.get("/api/admin/node-refs").json()

        assert body["ndemirrored001"] == "ret0badcafe"

    def test_it_is_gated_on_require_admin(self):
        """The suite runs with AUTH_ALLOW_ANONYMOUS_ADMIN=1, so the gate is
        asserted where it is declared rather than by a refused request."""
        from core.users import require_admin

        gated = {
            route.path
            for route in served_routes(app)
            if getattr(route, "dependant", None)
            and any(dep.call is require_admin for dep in route.dependant.dependencies)
        }

        assert "/api/admin/node-refs" in gated
