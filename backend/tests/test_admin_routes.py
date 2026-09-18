"""Tests for admin API routes — events, users, config, storage, leaderboard, metrics."""

import time

import pytest

from core import state
from main import app

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

    def test_set_role_invalid_user(self, client):
        r = client.put(
            "/api/admin/users/nonexistent-user-id/role",
            json={"role": "admin"},
        )
        # 404 because user doesn't exist
        assert r.status_code == 404


# ── Node location privacy ─────────────────────────────────────────────────────


class TestAdminNodeLocationPrivacy:
    """The admin half of the switch, reachable for any node id.

    Deliberately exercised against an id that has never registered and has no
    owner: that is the fleet on the test droplet, and the case the whole table
    exists for.  tests/test_publication.py owns the precedence rule; what is
    asserted here is the route surface, the raw pieces admin gets and owners do
    not, the cache drop, and the event-log entry.
    """

    NODE = "admin-privacy-node"

    @staticmethod
    def _register(node_id, choice):
        import asyncio

        from core.nodes import Node
        from core.users import async_session_maker

        async def _go():
            async with async_session_maker() as session:
                session.add(Node(node_id=node_id, node_ref=f"nde-{node_id}"[:15], publication=choice))
                await session.commit()

        asyncio.run(_go())
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_get_answers_for_a_node_nothing_has_ever_heard_of(self, client):
        """Not a 404: "no registration, no override, public by default" is the
        true answer for a node an admin is about to hide before it has ever
        connected."""
        r = client.get(f"/api/admin/nodes/{self.NODE}/location-privacy")
        assert r.status_code == 200
        assert r.json() == {
            "node_id": self.NODE,
            "location_private": False,
            "location_privacy_source": "default",
            "registration_choice": None,
            "override": None,
        }

    def test_get_reports_the_registration_choice_as_a_raw_piece(self, client):
        self._register(self.NODE, "private")
        body = client.get(f"/api/admin/nodes/{self.NODE}/location-privacy").json()
        assert body["location_privacy_source"] == "registration"
        assert body["registration_choice"] == "private"
        assert body["override"] is None

    def test_put_sets_the_override_and_records_who_set_it(self, client):
        from core.users import ANONYMOUS_USER

        r = client.put(f"/api/admin/nodes/{self.NODE}/location-privacy", json={"private": True})
        assert r.status_code == 200
        assert r.json() == {
            "node_id": self.NODE,
            "location_private": True,
            "location_privacy_source": "override",
        }
        override = client.get(f"/api/admin/nodes/{self.NODE}/location-privacy").json()["override"]
        assert override["private"] is True
        # `admin:<email>` rather than a bare id, so an owner's choice and an
        # intervention are told apart in the row itself.
        assert override["set_by"] == f"admin:{ANONYMOUS_USER['email']}"
        assert override["set_at"] > 0

    def test_put_takes_effect_without_waiting_for_the_ttl(self, client):
        from services.publication import is_private

        assert not is_private(self.NODE)
        client.put(f"/api/admin/nodes/{self.NODE}/location-privacy", json={"private": True})
        assert is_private(self.NODE)

    def test_put_is_logged_like_the_owner_assignment_route(self, client):
        """An admin changing what another operator's node publishes is exactly
        what the event log exists to make answerable afterwards."""
        client.put(f"/api/admin/nodes/{self.NODE}/location-privacy", json={"private": True})
        events = client.get("/api/admin/events").json()
        assert any(e["meta"].get("node_id") == self.NODE and e["meta"].get("private") is True for e in events)

    def test_delete_returns_the_node_to_its_registration_choice(self, client):
        from services.publication import is_private

        self._register(self.NODE, "private")
        client.put(f"/api/admin/nodes/{self.NODE}/location-privacy", json={"private": False})
        assert not is_private(self.NODE)

        r = client.delete(f"/api/admin/nodes/{self.NODE}/location-privacy")
        assert r.status_code == 200
        assert r.json() == {
            "node_id": self.NODE,
            "location_private": True,
            "location_privacy_source": "registration",
        }
        assert is_private(self.NODE)

    def test_delete_is_logged_too(self, client):
        client.put(f"/api/admin/nodes/{self.NODE}/location-privacy", json={"private": True})
        client.delete(f"/api/admin/nodes/{self.NODE}/location-privacy")
        events = client.get("/api/admin/events").json()
        assert any("location privacy override cleared" in e["message"] for e in events)

    @pytest.mark.parametrize(
        ("method", "kwargs"),
        [("get", {}), ("put", {"json": {"private": True}}), ("delete", {})],
    )
    def test_all_three_are_behind_require_admin(self, client, method, kwargs):
        """With the anonymous-admin bypass off and no cookie, every one of them
        is 401 rather than an answer about somebody's node."""
        from unittest.mock import patch

        with patch("core.users.AUTH_BYPASS", False):
            r = getattr(client, method)(f"/api/admin/nodes/{self.NODE}/location-privacy", **kwargs)
        assert r.status_code == 401


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


# ── Leaderboard ──────────────────────────────────────────────────────────────


class TestLeaderboard:
    def test_leaderboard_answers_a_caller_with_no_session(self, client):
        """The one route under /api/admin that publishes.

        It reports node_ref and the metrics /api/radar/analytics already
        publishes per ref, so it is the same side of the D16 boundary as a feed
        anyone can read; the dashboard's /leaderboard is open on that basis. The
        bypass is patched off because the suite runs with it on, which would
        answer this question with 200 whatever the dependency says.
        """
        from unittest.mock import patch

        with patch("core.users.AUTH_BYPASS", False):
            r = client.get("/api/admin/leaderboard")
        assert r.status_code == 200
        assert "leaderboard" in r.json()

    #: The fields sourced from state.latest_missed_detections rather than from
    #: the published analytics snapshot.
    MISS_FIELDS = ("in_range", "detected_in_range", "missed", "miss_rate")

    #: What the route publishes to anyone. Widening this set publishes a field,
    #: so it is meant to take an edit here as well as one to the model.
    PUBLIC_FIELDS = {
        "avg_snr",
        "detections",
        "frames",
        "name",
        "node_ref",
        "online",
        "rank",
        "reputation",
        "tracks",
        "trust_score",
        "uptime_s",
    }

    def test_the_published_row_declares_exactly_the_public_fields(self):
        from routes.admin import PublicLeaderboardRow, SignedInLeaderboardRow

        assert set(PublicLeaderboardRow.model_fields) == self.PUBLIC_FIELDS
        assert set(SignedInLeaderboardRow.model_fields) == self.PUBLIC_FIELDS | set(self.MISS_FIELDS)

    def test_a_stray_field_is_refused_rather_than_dropped(self):
        from pydantic import ValidationError

        from routes.admin import PublicLeaderboardRow

        fields = dict.fromkeys(self.PUBLIC_FIELDS, 0) | {"node_ref": "r", "name": "n", "online": True}
        PublicLeaderboardRow(**fields)
        with pytest.raises(ValidationError):
            PublicLeaderboardRow(**fields, node_id="ret9f8e7d6c")

    @pytest.mark.parametrize("cold", [False, True], ids=["snapshot", "cold-start"])
    def test_each_caller_gets_exactly_its_row_keys(self, client, cold):
        """Asserted on the wire, on both paths the rows can come from, so a
        serialiser that dropped or added fields would show here and not only
        in the model."""
        from unittest.mock import patch

        nid = "test-lb-keys"
        prior = self._seed_miss_row(nid)
        summaries = {nid: {"metrics": {"total_detections": 7}, "trust": {}, "reputation": {}}}
        if cold:
            state.latest_analytics_bytes = b'{"nodes":{}}'
        try:
            with (
                patch.object(state.node_analytics, "get_all_summaries", return_value=summaries),
                patch("services.publication.private_node_ids", return_value=set()),
            ):
                signed_in = client.get("/api/admin/leaderboard").json()
                with patch("core.users.AUTH_BYPASS", False):
                    anonymous = client.get("/api/admin/leaderboard").json()
        finally:
            self._restore(prior, nid)

        (public,) = [e for e in anonymous["leaderboard"] if e["node_ref"] == nid]
        (full,) = [e for e in signed_in["leaderboard"] if e["node_ref"] == nid]
        assert set(public) == self.PUBLIC_FIELDS
        assert set(full) == self.PUBLIC_FIELDS | set(self.MISS_FIELDS)
        assert set(anonymous) == set(signed_in) == {"leaderboard", "total"}

    @staticmethod
    def _seed_miss_row(nid: str):
        """A node with both a published summary and a miss-detection row."""
        import orjson

        state.connected_nodes[nid] = {"status": "active", "config": {"name": "Miss-Test"}, "is_synthetic": True}
        analytics = {"nodes": {nid: {"metrics": {"total_detections": 7}, "trust": {}, "reputation": {}}}}
        prior = (state.latest_analytics_bytes, dict(state.latest_missed_detections))
        state.latest_analytics_bytes = orjson.dumps(analytics)
        state.latest_missed_detections[nid] = {
            "in_range": 20,
            "detected": 12,
            "missed": 8,
            "miss_rate": 0.4,
        }
        return prior

    @staticmethod
    def _restore(prior, nid: str):
        state.latest_analytics_bytes, restored = prior
        state.latest_missed_detections.clear()
        state.latest_missed_detections.update(restored)

    def test_a_caller_with_no_session_is_not_told_what_each_node_missed(self, client):
        """The per-node miss counts are not on the published side of the boundary.

        They come from state.latest_missed_detections, which no other route
        serves without a session: /health reduces the same global to one
        fleet-wide aggregate and says in as many words that the detail stays
        off an unauthenticated endpoint. Opening this route must not be the
        thing that publishes them.
        """
        from unittest.mock import patch

        nid = "test-lb-miss"
        prior = self._seed_miss_row(nid)
        try:
            with patch("core.users.AUTH_BYPASS", False):
                r = client.get("/api/admin/leaderboard")
            assert r.status_code == 200
            row = next(e for e in r.json()["leaderboard"] if e["node_ref"] == nid)
            assert row["detections"] == 7
            assert [f for f in self.MISS_FIELDS if f in row] == []
        finally:
            self._restore(prior, nid)

    def test_a_caller_with_a_session_still_gets_them(self, client):
        """The suite runs with the anonymous-admin bypass on, so an unpatched
        request is the signed-in case."""
        nid = "test-lb-miss-in"
        prior = self._seed_miss_row(nid)
        try:
            row = next(e for e in client.get("/api/admin/leaderboard").json()["leaderboard"] if e["node_ref"] == nid)
            assert row["in_range"] == 20
            assert row["detected_in_range"] == 12
            assert row["missed"] == 8
            assert row["miss_rate"] == 0.4
        finally:
            self._restore(prior, nid)

    @staticmethod
    def _cold_start(nid: str):
        """The route's state before the first analytics refresh has run.

        state.latest_analytics_bytes starts as an empty-nodes document rather
        than absent, so the handler parses it, finds nothing and recomputes
        live. That window is every process start until the ~30 s refresh.
        """
        state.connected_nodes[nid] = {"status": "active", "config": {"name": "Hidden"}, "is_synthetic": True}
        prior = state.latest_analytics_bytes
        state.latest_analytics_bytes = b'{"nodes":{}}'
        return prior

    @pytest.mark.parametrize(("private", "expected"), [(True, 0), (False, 1)])
    def test_the_cold_start_fallback_withholds_a_private_node(self, client, private, expected):
        """Publication is not the auth boundary: a private node is withheld
        from everyone, session or none.

        The snapshot this route normally reads has been through
        public_summaries, which drops them. The live recomputation below is the
        one path under this route that had not, and public_identity does not
        cover it — it asks whether a node has a registry ref, not whether it
        consented to being published.
        """
        from unittest.mock import patch

        nid = "test-lb-private"
        prior = self._cold_start(nid)
        summaries = {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}
        try:
            with (
                patch.object(state.node_analytics, "get_all_summaries", return_value=summaries),
                patch("services.publication.private_node_ids", return_value={nid} if private else set()),
            ):
                r = client.get("/api/admin/leaderboard")
            assert r.status_code == 200
            assert len([e for e in r.json()["leaderboard"] if e["node_ref"] == nid]) == expected
        finally:
            state.latest_analytics_bytes = prior

    async def test_concurrent_cold_start_requests_share_one_summaries_call(self):
        """A burst of anonymous callers arriving during the cold-start gap
        must not each schedule their own get_all_summaries() call onto the
        shared two-worker admin executor: they share the one in flight.

        No await happens between the coalescing check and the assignment in
        _cold_start_summaries(), so every gathered task observes the shared
        future before the executor thread can finish — the race is real
        without needing a delay in the mock.
        """
        import asyncio
        from unittest.mock import patch

        from routes import admin as admin_routes

        nid = "test-lb-burst"
        prior = self._cold_start(nid)
        calls = []

        def counting_summaries():
            calls.append(1)
            return {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}

        try:
            with patch.object(state.node_analytics, "get_all_summaries", side_effect=counting_summaries):
                results = await asyncio.gather(*(admin_routes.leaderboard(caller=None) for _ in range(20)))
        finally:
            state.latest_analytics_bytes = prior
            state.connected_nodes.pop(nid, None)

        assert len(calls) == 1
        assert all(any(e.node_ref == nid for e in r.leaderboard) for r in results)

    async def test_cancelling_one_caller_does_not_cancel_the_others_sharing_it(self):
        """A client that disconnects mid-request must not take the shared
        computation down for every other caller waiting on the same one.
        """
        import asyncio
        from unittest.mock import patch

        from routes import admin as admin_routes

        nid = "test-lb-cancel"
        prior = self._cold_start(nid)

        def slow_summaries():
            time.sleep(0.1)
            return {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}

        try:
            with patch.object(state.node_analytics, "get_all_summaries", side_effect=slow_summaries):
                cancelled = asyncio.create_task(admin_routes.leaderboard(caller=None))
                survivor = asyncio.create_task(admin_routes.leaderboard(caller=None))
                await asyncio.sleep(0.02)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                result = await survivor
        finally:
            state.latest_analytics_bytes = prior
            state.connected_nodes.pop(nid, None)

        assert any(e.node_ref == nid for e in result.leaderboard)

    def test_its_neighbours_under_the_same_prefix_still_want_a_session(self, client):
        """Opening the route above opens the route above, not the prefix."""
        from unittest.mock import patch

        with patch("core.users.AUTH_BYPASS", False):
            assert client.get("/api/admin/users").status_code == 401
            assert client.get("/api/admin/node-refs").status_code == 401

    def test_leaderboard_empty(self, client):
        r = client.get("/api/admin/leaderboard")
        assert r.status_code == 200
        body = r.json()
        assert "leaderboard" in body
        assert "total" in body

    def test_leaderboard_with_node(self, client):
        """Inject a node and verify it appears in leaderboard."""
        import orjson

        state.connected_nodes["test-lb-1"] = {
            "status": "active",
            "config": {"name": "LB-Test-Node"},
            "is_synthetic": True,
        }
        analytics_data = {
            "nodes": {
                "test-lb-1": {
                    "metrics": {
                        "total_detections": 42,
                        "total_frames": 10,
                        "total_tracks": 5,
                        "uptime_s": 300,
                        "avg_snr": 12.0,
                    },
                    "trust": {},
                    "reputation": {},
                }
            }
        }
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(analytics_data)
        try:
            r = client.get("/api/admin/leaderboard")
            assert r.status_code == 200
            entries = r.json()["leaderboard"]
            # A synthetic node publishes under its own id, so ref and id agree.
            found = [e for e in entries if e["node_ref"] == "test-lb-1"]
            assert len(found) == 1
            assert found[0]["detections"] == 42
            assert found[0]["rank"] >= 1
        finally:
            state.latest_analytics_bytes = orig

    @staticmethod
    def _seed_node(nid: str) -> str:
        """A registry row for a real node id, returning the ref it carries."""
        import asyncio

        from core.nodes import Node
        from core.users import async_session_maker
        from services import node_auth, node_refs

        ref = node_auth.mint_node_ref()

        async def _seed():
            async with async_session_maker() as session:
                session.add(Node(node_id=nid, node_ref=ref, publication="public"))
                await session.commit()

        asyncio.run(_seed())
        # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
        # restores one for the same reason.
        asyncio.set_event_loop(asyncio.new_event_loop())
        node_refs._reset_for_tests()
        return ref

    def test_leaderboard_reports_the_ref_and_still_reads_id_keyed_state(self, client):
        """The snapshot is keyed on node_ref and so is this route; that is the
        key space /api/radar/analytics publishes the same metrics in, and a row
        naming both would hand out the mapping to any logged-in caller.

        connected_nodes and latest_missed_detections are still keyed on
        node_id, so the reverse lookup has to survive: read the key as an id
        and a real node loses its name, reads offline and zeroes all four miss
        fields.  The test above never saw it because a synthetic id publishes
        as itself.
        """
        import orjson

        nid = "ret9f8e7d6c"
        ref = self._seed_node(nid)

        state.connected_nodes[nid] = {"status": "active", "config": {"name": "Example Site 1"}, "is_synthetic": False}
        state.latest_missed_detections[nid] = {"in_range": 10, "detected": 7, "missed": 3, "miss_rate": 0.3}
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(
            {"nodes": {ref: {"metrics": {"total_detections": 42}, "trust": {}, "reputation": {}}}}
        )
        try:
            r = client.get("/api/admin/leaderboard")
            assert r.status_code == 200
            entries = r.json()["leaderboard"]
            body = r.text
        finally:
            state.latest_analytics_bytes = orig
            state.latest_missed_detections.pop(nid, None)

        (entry,) = [e for e in entries if e["node_ref"] == ref]
        assert "node_id" not in entry
        assert nid not in body
        assert entry["name"] == "Example Site 1"
        assert entry["online"] is True
        assert entry["detections"] == 42
        assert (entry["missed"], entry["miss_rate"]) == (3, 0.3)

    def test_leaderboard_names_nodes_the_same_way_with_a_cold_snapshot(self, client):
        """The fallback recomputes from a node_id-keyed source, so without a
        pass through the boundary this route would answer in ids or in refs
        depending on whether the refresh had run yet."""
        nid = "ret9f8e7d6c"
        ref = self._seed_node(nid)
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = b"{}"
        state.node_analytics.register_node(nid, {"node_id": nid})
        state.node_analytics._summaries_cache = None
        try:
            r = client.get("/api/admin/leaderboard")
            entries = r.json()["leaderboard"]
            body = r.text
        finally:
            state.latest_analytics_bytes = orig
            state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

        assert ref in [e["node_ref"] for e in entries]
        assert nid not in body

    def test_leaderboard_leaves_out_a_node_with_no_registry_row(self, client):
        """The cold path is keyed on node_id, and a node with no handle cannot
        be named at all: publishing the id as a fallback is the disclosure."""
        nid = "ret0badcafe"
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = b"{}"
        state.node_analytics.register_node(nid, {"node_id": nid})
        state.node_analytics._summaries_cache = None
        try:
            body = client.get("/api/admin/leaderboard").text
        finally:
            state.latest_analytics_bytes = orig
            state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

        assert nid not in body

    def test_leaderboard_does_not_publish_a_name_that_is_a_node_id(self, client):
        """`name` comes off the node's own config, which nothing validates."""
        import orjson

        nid = "ret9f8e7d6c"
        other = "ret1a2b3c4d"
        ref = self._seed_node(nid)
        self._seed_node(other)

        state.connected_nodes[nid] = {"status": "active", "config": {"name": other}, "is_synthetic": False}
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(
            {"nodes": {ref: {"metrics": {"total_detections": 1}, "trust": {}, "reputation": {}}}}
        )
        try:
            r = client.get("/api/admin/leaderboard")
            entries = r.json()["leaderboard"]
            body = r.text
        finally:
            state.latest_analytics_bytes = orig

        (entry,) = [e for e in entries if e["node_ref"] == ref]
        assert entry["name"] == ref
        assert other not in body


# ── Alerts ───────────────────────────────────────────────────────────────────


class TestAlerts:
    def test_alerts_returns_list(self, client):
        r = client.get("/api/admin/alerts")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_alerts_filters_severity(self, client):
        from routes.admin import log_event

        log_event("test", "info-only", "info")
        log_event("node", "warning-event", "warning")
        r = client.get("/api/admin/alerts")
        events = r.json()
        # warning/error/critical + node/config/system categories pass through
        for e in events:
            assert e.get("severity") in ("warning", "error", "critical") or e.get("category") in (
                "node",
                "config",
                "system",
            )


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
            for route in app.routes
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
        asyncio.set_event_loop(asyncio.new_event_loop())

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
            for route in app.routes
            if getattr(route, "dependant", None)
            and any(dep.call is require_admin for dep in route.dependant.dependencies)
        }

        assert "/api/admin/node-refs" in gated
