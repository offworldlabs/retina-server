"""GET /api/radar/leaderboard: the network's rankings, open to anyone.

It used to sit under /api/admin, the one route there that answered a caller
with no session. That path is gone, and the prefix is the admin API alone
(tests/test_admin_prefix_gate.py). A caller with no session gets the
public rows, one with a session the per-node miss counts as well, and which
account the session belongs to changes nothing.
"""

import asyncio
import time
from unittest.mock import patch

import orjson
import pytest

import core.users as users
from core import state
from services import node_refs, publication
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import own, register_node_row

PATH = "/api/radar/leaderboard"

#: The fields sourced from state.latest_missed_detections rather than from the
#: published analytics snapshot.
MISS_FIELDS = ("in_range", "detected_in_range", "missed", "miss_rate")

#: What the route publishes to anyone. Widening this set publishes a field,
#: so it is meant to take an edit here as well as one to the model.
PUBLIC_FIELDS = {
    "availability_7d",
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
}


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


def _seed_miss_row(nid: str):
    """A node with both a published summary and a miss-detection row."""
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


def _restore(prior):
    state.latest_analytics_bytes, restored = prior
    state.latest_missed_detections.clear()
    state.latest_missed_detections.update(restored)


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


def _seed_node(nid: str) -> str:
    """A registry row for a real node id, returning the ref it carries."""
    ref = asyncio.run(register_node_row(nid))
    node_refs._reset_for_tests()
    return ref


class TestTheRoute:
    def test_it_answers_a_caller_with_no_session(self, client, real):
        r = client.get(PATH)
        assert r.status_code == 200
        assert set(r.json()) == {"leaderboard", "total"}

    def test_total_counts_the_rows(self, client):
        r = client.get(PATH)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == len(body["leaderboard"])

    @pytest.mark.parametrize("who", ["administrator", "nobody"])
    def test_the_admin_path_is_gone(self, client, monkeypatch, who):
        """Gone rather than gated: an administrator is not answered either."""
        if who == "nobody":
            real_sessions(monkeypatch)
        assert client.get("/api/admin/leaderboard").status_code == 404

    def test_a_cookie_that_names_no_one_is_answered_as_no_session(self, client, real):
        """A public route never answers 401: a lapsed or forged session reads
        what a visitor reads rather than being refused."""
        nid = "test-lb-forged"
        prior = _seed_miss_row(nid)
        try:
            as_user(client, "not-a-session")
            r = client.get(PATH)
        finally:
            _restore(prior)
        assert r.status_code == 200
        (row,) = [e for e in r.json()["leaderboard"] if e["node_ref"] == nid]
        assert set(row) == PUBLIC_FIELDS


class TestRowKeys:
    def test_the_published_row_declares_exactly_the_public_fields(self):
        from routes.radar import PublicLeaderboardRow, SignedInLeaderboardRow

        assert set(PublicLeaderboardRow.model_fields) == PUBLIC_FIELDS
        assert set(SignedInLeaderboardRow.model_fields) == PUBLIC_FIELDS | set(MISS_FIELDS)

    def test_a_stray_field_is_refused_rather_than_dropped(self):
        from pydantic import ValidationError

        from routes.radar import PublicLeaderboardRow

        fields = dict.fromkeys(PUBLIC_FIELDS, 0) | {"node_ref": "r", "name": "n", "online": True}
        PublicLeaderboardRow(**fields)
        with pytest.raises(ValidationError):
            PublicLeaderboardRow(**fields, node_id="ret9f8e7d6c")

    @pytest.mark.parametrize("cold", [False, True], ids=["snapshot", "cold-start"])
    def test_each_caller_gets_exactly_its_row_keys(self, client, real, cold):
        """Asserted on the wire, on both paths the rows can come from, so a
        serialiser that dropped or added fields would show here and not only
        in the model."""
        nid = "test-lb-keys"
        prior = _seed_miss_row(nid)
        summaries = {nid: {"metrics": {"total_detections": 7}, "trust": {}, "reputation": {}}}
        if cold:
            state.latest_analytics_bytes = b'{"nodes":{}}'
        token = sign_in(client, "member@example.com")
        try:
            with (
                patch.object(state.node_analytics, "get_all_summaries", return_value=summaries),
                patch("services.publication.private_node_ids", return_value=set()),
            ):
                as_user(client, token)
                signed_in = client.get(PATH).json()
                as_user(client, None)
                anonymous = client.get(PATH).json()
        finally:
            _restore(prior)

        (public,) = [e for e in anonymous["leaderboard"] if e["node_ref"] == nid]
        (full,) = [e for e in signed_in["leaderboard"] if e["node_ref"] == nid]
        assert set(public) == PUBLIC_FIELDS
        assert set(full) == PUBLIC_FIELDS | set(MISS_FIELDS)
        assert set(anonymous) == set(signed_in) == {"leaderboard", "total"}


class TestMissCounts:
    def test_a_caller_with_no_session_is_not_told_what_each_node_missed(self, client, real):
        """The per-node miss counts are not on the published side of the boundary.

        They come from state.latest_missed_detections, which no other route
        serves without a session: /health reduces the same global to one
        fleet-wide aggregate and says in as many words that the detail stays
        off an unauthenticated endpoint. Opening this route must not be the
        thing that publishes them.
        """
        nid = "test-lb-miss"
        prior = _seed_miss_row(nid)
        try:
            r = client.get(PATH)
        finally:
            _restore(prior)
        assert r.status_code == 200
        row = next(e for e in r.json()["leaderboard"] if e["node_ref"] == nid)
        assert row["detections"] == 7
        assert [f for f in MISS_FIELDS if f in row] == []

    def test_a_caller_with_a_session_still_gets_them(self, client, real):
        nid = "test-lb-miss-in"
        prior = _seed_miss_row(nid)
        try:
            as_user(client, sign_in(client, "member@example.com"))
            row = next(e for e in client.get(PATH).json()["leaderboard"] if e["node_ref"] == nid)
        finally:
            _restore(prior)
        assert (row["in_range"], row["detected_in_range"], row["missed"], row["miss_rate"]) == (20, 12, 8, 0.4)

    def test_the_consoles_access_identity_gets_them_too(self, client, real, monkeypatch):
        """The runbook reads per-node miss rates here with the console's Access
        assertion (docs/runbook.md, `adm`), which is a session of its own."""

        async def _access(_request):
            return users._access_user_dict("operator@example.com")

        monkeypatch.setattr(users, "_access_user_from_request", _access)
        nid = "test-lb-miss-access"
        prior = _seed_miss_row(nid)
        try:
            row = next(e for e in client.get(PATH).json()["leaderboard"] if e["node_ref"] == nid)
        finally:
            _restore(prior)
        assert row["miss_rate"] == 0.4


class TestWhoIsAsking:
    """The split is session or none, never whose session.

    Nothing in a row depends on who owns the node or who is asking: the
    dashboard marks an owner's rows itself, from /api/auth/me/nodes. So one
    account learns nothing from this route that any other account does not,
    and a private node is withheld from everyone, its owner included.
    """

    OWNER = "owner@example.com"
    OTHER = "other@example.com"
    PUBLIC_ID = "ret1a2b3c4d"
    PRIVATE_ID = "ret9f8e7d6c"

    @pytest.fixture(params=[False, True], ids=["snapshot", "cold-start"])
    def fleet(self, request, client, real):
        """One owner with a public and a private node, both ranked by the
        analytics manager and both with miss counts, on either path."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        tokens = {email: sign_in(client, email) for email in (self.OWNER, self.OTHER)}
        owner_id = user_id_of(self.OWNER)
        public_ref = own(self.PUBLIC_ID, owner_id)
        asyncio.run(register_node_row(self.PRIVATE_ID, publication="private"))
        private_ref = own(self.PRIVATE_ID, owner_id)
        publication.invalidate()
        node_refs._reset_for_tests()

        prior = (state.latest_analytics_bytes, dict(state.latest_missed_detections))
        for nid in (self.PUBLIC_ID, self.PRIVATE_ID):
            state.node_analytics.register_node(nid, {"node_id": nid})
        state.node_analytics._summaries_cache = None
        if request.param:
            state.latest_analytics_bytes = b"{}"
        else:
            # The snapshot as the background refresh builds it, not a stand-in.
            _refresh_analytics_and_nodes()
        state.latest_missed_detections[self.PUBLIC_ID] = {"in_range": 10, "detected": 7, "missed": 3, "miss_rate": 0.3}
        state.latest_missed_detections[self.PRIVATE_ID] = {"in_range": 9, "detected": 1, "missed": 8, "miss_rate": 0.89}
        try:
            yield tokens, public_ref, private_ref
        finally:
            _restore(prior)
            for nid in (self.PUBLIC_ID, self.PRIVATE_ID):
                state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

    def _read(self, client, token):
        as_user(client, token)
        r = client.get(PATH)
        assert r.status_code == 200
        return r

    def test_a_private_node_is_withheld_from_every_caller(self, client, fleet):
        tokens, public_ref, private_ref = fleet
        for token in (None, tokens[self.OWNER], tokens[self.OTHER]):
            body = self._read(client, token).text
            assert public_ref in body
            assert private_ref not in body
            assert self.PRIVATE_ID not in body
            assert self.PUBLIC_ID not in body

    def test_an_owner_and_a_stranger_read_the_same_rows(self, client, fleet):
        tokens, public_ref, _ = fleet
        owner = self._read(client, tokens[self.OWNER]).json()
        other = self._read(client, tokens[self.OTHER]).json()
        anonymous = self._read(client, None).json()

        assert owner == other
        (row,) = [e for e in owner["leaderboard"] if e["node_ref"] == public_ref]
        assert (row["missed"], row["miss_rate"]) == (3, 0.3)
        # What a session adds over no session is the miss counts and nothing else.
        assert [{k: v for k, v in e.items() if k not in MISS_FIELDS} for e in owner["leaderboard"]] == anonymous[
            "leaderboard"
        ]


class TestColdStart:
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
        nid = "test-lb-private"
        prior = _cold_start(nid)
        summaries = {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}
        try:
            with (
                patch.object(state.node_analytics, "get_all_summaries", return_value=summaries),
                patch("services.publication.private_node_ids", return_value={nid} if private else set()),
            ):
                r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = prior
        assert r.status_code == 200
        assert len([e for e in r.json()["leaderboard"] if e["node_ref"] == nid]) == expected

    async def test_concurrent_cold_start_requests_share_one_summaries_call(self):
        """A burst of anonymous callers arriving during the cold-start gap
        must not each schedule their own get_all_summaries() call onto the
        route's executor: they share the one in flight.

        No await happens between the coalescing check and the assignment in
        _cold_start_summaries(), so every gathered task observes the shared
        future before the executor thread can finish — the race is real
        without needing a delay in the mock.
        """
        from routes import radar as radar_routes

        nid = "test-lb-burst"
        prior = _cold_start(nid)
        calls = []

        def counting_summaries():
            calls.append(1)
            return {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}

        try:
            with patch.object(state.node_analytics, "get_all_summaries", side_effect=counting_summaries):
                results = await asyncio.gather(*(radar_routes.radar_leaderboard(caller=None) for _ in range(20)))
        finally:
            state.latest_analytics_bytes = prior
            state.connected_nodes.pop(nid, None)

        assert len(calls) == 1
        assert all(any(e.node_ref == nid for e in r.leaderboard) for r in results)

    async def test_cancelling_one_caller_does_not_cancel_the_others_sharing_it(self):
        """A client that disconnects mid-request must not take the shared
        computation down for every other caller waiting on the same one.
        """
        from routes import radar as radar_routes

        nid = "test-lb-cancel"
        prior = _cold_start(nid)

        def slow_summaries():
            time.sleep(0.1)
            return {nid: {"metrics": {"total_detections": 5}, "trust": {}, "reputation": {}}}

        try:
            with patch.object(state.node_analytics, "get_all_summaries", side_effect=slow_summaries):
                cancelled = asyncio.create_task(radar_routes.radar_leaderboard(caller=None))
                survivor = asyncio.create_task(radar_routes.radar_leaderboard(caller=None))
                await asyncio.sleep(0.02)
                cancelled.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled
                result = await survivor
        finally:
            state.latest_analytics_bytes = prior
            state.connected_nodes.pop(nid, None)

        assert any(e.node_ref == nid for e in result.leaderboard)


class TestNaming:
    def test_a_node_appears_with_its_metrics(self, client):
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
                        "availability_7d": 0.9731,
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
            r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = orig
        assert r.status_code == 200
        # A synthetic node publishes under its own id, so ref and id agree.
        found = [e for e in r.json()["leaderboard"] if e["node_ref"] == "test-lb-1"]
        assert len(found) == 1
        assert found[0]["detections"] == 42
        assert found[0]["availability_7d"] == 0.9731
        assert found[0]["name"] == "LB-Test-Node"
        assert found[0]["rank"] >= 1

    def test_it_reports_the_ref_and_still_reads_id_keyed_state(self, client):
        """The snapshot is keyed on node_ref and so is this route; that is the
        key space /api/radar/analytics publishes the same metrics in, and a row
        naming both would hand out the mapping to any caller with a session.

        connected_nodes and latest_missed_detections are still keyed on
        node_id, so the reverse lookup has to survive: read the key as an id
        and a real node loses its name, reads offline and zeroes all four miss
        fields. A synthetic node never shows it, because its id publishes as
        itself.
        """
        nid = "ret9f8e7d6c"
        ref = _seed_node(nid)

        state.connected_nodes[nid] = {"status": "active", "config": {"name": "Example Site 1"}, "is_synthetic": False}
        state.latest_missed_detections[nid] = {"in_range": 10, "detected": 7, "missed": 3, "miss_rate": 0.3}
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(
            {"nodes": {ref: {"metrics": {"total_detections": 42}, "trust": {}, "reputation": {}}}}
        )
        try:
            r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = orig
            state.latest_missed_detections.pop(nid, None)
        assert r.status_code == 200

        (entry,) = [e for e in r.json()["leaderboard"] if e["node_ref"] == ref]
        assert "node_id" not in entry
        assert nid not in r.text
        assert entry["name"] == "Example Site 1"
        assert entry["online"] is True
        assert entry["detections"] == 42
        assert (entry["missed"], entry["miss_rate"]) == (3, 0.3)

    def test_it_names_nodes_the_same_way_with_a_cold_snapshot(self, client):
        """The fallback recomputes from a node_id-keyed source, so without a
        pass through the boundary this route would answer in ids or in refs
        depending on whether the refresh had run yet."""
        nid = "ret9f8e7d6c"
        ref = _seed_node(nid)
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = b"{}"
        state.node_analytics.register_node(nid, {"node_id": nid})
        state.node_analytics._summaries_cache = None
        try:
            r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = orig
            state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

        assert ref in [e["node_ref"] for e in r.json()["leaderboard"]]
        assert nid not in r.text

    def test_it_leaves_out_a_node_with_no_registry_row(self, client):
        """The cold path is keyed on node_id, and a node with no handle cannot
        be named at all: publishing the id as a fallback is the disclosure."""
        nid = "ret0badcafe"
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = b"{}"
        state.node_analytics.register_node(nid, {"node_id": nid})
        state.node_analytics._summaries_cache = None
        try:
            r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = orig
            state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

        assert r.status_code == 200
        assert nid not in r.text

    def test_it_does_not_publish_a_name_that_is_a_node_id(self, client):
        """`name` comes off the node's own config, which nothing validates."""
        nid = "ret9f8e7d6c"
        other = "ret1a2b3c4d"
        ref = _seed_node(nid)
        _seed_node(other)

        state.connected_nodes[nid] = {"status": "active", "config": {"name": other}, "is_synthetic": False}
        orig = state.latest_analytics_bytes
        state.latest_analytics_bytes = orjson.dumps(
            {"nodes": {ref: {"metrics": {"total_detections": 1}, "trust": {}, "reputation": {}}}}
        )
        try:
            r = client.get(PATH)
        finally:
            state.latest_analytics_bytes = orig

        (entry,) = [e for e in r.json()["leaderboard"] if e["node_ref"] == ref]
        assert entry["name"] == ref
        assert other not in r.text
