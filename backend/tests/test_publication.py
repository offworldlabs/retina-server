"""Tests for services/publication.py — the Node.publication choice, enforced.

``publication`` records what the owner said at registration and, until now,
nothing read it.  These pin the two halves of making it mean something: the
cache that answers "which nodes are private" without a database round trip per
aircraft, and the boundaries that act on the answer.

The route tests seed real rows rather than patching the lookup.  The functions
are imported by name into the route modules, so a patch on this module would
not reach them — and seeding is what actually proves the wiring.
"""

import asyncio
import os

import orjson
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")

from core import state  # noqa: E402
from core.nodes import Node, NodeLocationPrivacy  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from main import app  # noqa: E402
from services import publication  # noqa: E402
from services.public_location import public_node_summary  # noqa: E402
from services.publication import (  # noqa: E402
    effective_privacy,
    is_private,
    private_node_ids,
    public_aircraft_payload,
    public_summaries,
)

_PRIV = "privnode01"
_PUB = "pubnode01"


@pytest.fixture()
def seed_nodes():
    """seed_nodes(node_id="private"|"public", …) — write rows and drop the cache."""

    def _seed(**choices: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, choice in choices.items():
                    session.add(Node(node_id=nid, node_ref=f"nde-{nid}"[:15], publication=choice))
                await session.commit()

        asyncio.run(_go())
        # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
        # restores one for the same reason.
        asyncio.set_event_loop(asyncio.new_event_loop())
        publication._reset_for_tests()

    return _seed


@pytest.fixture()
def seed_override():
    """seed_override(node_id=True|False, …) — write override rows, drop the cache.

    Writes the row directly rather than going through the route, so the
    precedence tests below fail on the precedence rule rather than on anything
    the routes do around it.
    """

    def _seed(**overrides: bool) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, private in overrides.items():
                    session.add(
                        NodeLocationPrivacy(node_id=nid, private=private, set_by="test", set_at=1_700_000_000.0)
                    )
                await session.commit()

        asyncio.run(_go())
        asyncio.set_event_loop(asyncio.new_event_loop())
        publication._reset_for_tests()

    return _seed


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── The cache ────────────────────────────────────────────────────────────────


class TestPrivateNodeIds:
    def test_a_private_registration_is_listed(self, seed_nodes):
        seed_nodes(**{_PRIV: "private", _PUB: "public"})
        assert private_node_ids() == frozenset({_PRIV})

    def test_a_public_registration_is_not(self, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        assert not is_private(_PUB)

    def test_a_node_that_never_registered_is_public(self, seed_nodes):
        """The synthetic fleet and anything predating the column.

        Registration is what records the choice, so absence is not a choice to
        withhold — and defaulting the other way would retire most of the map on
        a schema reading.
        """
        seed_nodes(**{_PRIV: "private"})
        assert not is_private("some-node-nobody-registered")

    def test_a_missing_node_id_is_public(self):
        assert not is_private(None)
        assert not is_private("")

    def test_the_answer_is_cached_within_the_ttl(self, seed_nodes, monkeypatch):
        seed_nodes(**{_PRIV: "private"})
        assert private_node_ids() == frozenset({_PRIV})
        # A second query would say something different; the cache must not ask.
        monkeypatch.setattr(publication, "_query", lambda: frozenset({"someone-else"}))
        assert private_node_ids() == frozenset({_PRIV})

    def test_the_cache_expires(self, seed_nodes, monkeypatch):
        seed_nodes(**{_PRIV: "private"})
        assert private_node_ids() == frozenset({_PRIV})
        monkeypatch.setattr(publication, "_query", lambda: frozenset({"someone-else"}))
        monkeypatch.setattr(publication, "_expires_at", 0.0)
        assert private_node_ids() == frozenset({"someone-else"})

    def test_a_failed_query_serves_the_last_known_set(self, seed_nodes, monkeypatch):
        """A database hiccup must neither blank the map nor publish a private node."""
        seed_nodes(**{_PRIV: "private"})
        assert private_node_ids() == frozenset({_PRIV})

        def _boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(publication, "_query", _boom)
        monkeypatch.setattr(publication, "_expires_at", 0.0)
        assert private_node_ids() == frozenset({_PRIV})

    def test_a_failure_before_any_answer_is_the_empty_set(self, monkeypatch):
        """Boot with an unreachable database: an answer, not an exception."""

        def _boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(publication, "_query", _boom)
        assert private_node_ids() == frozenset()

    def test_a_failure_backs_off_rather_than_querying_every_call(self, monkeypatch):
        calls = []

        def _boom():
            calls.append(1)
            raise RuntimeError("database is gone")

        monkeypatch.setattr(publication, "_query", _boom)
        private_node_ids()
        private_node_ids()
        assert len(calls) == 1


# ── The override and the precedence rule ─────────────────────────────────────


class TestEffectivePrivacy:
    """The rule itself, on its own, before anything reads a database.

    Four inputs, and the pair that looks redundant is the one worth pinning: a
    node registered public and a node that never registered are both published,
    and only the source distinguishes them.  The dashboard's wording depends on
    that difference — "set at onboarding" is a lie about a node that never
    onboarded.
    """

    def test_an_override_wins_over_the_registration_choice(self):
        assert effective_privacy("private", False) == (False, "override")
        assert effective_privacy("public", True) == (True, "override")

    def test_without_an_override_the_registration_choice_stands(self):
        assert effective_privacy("private", None) == (True, "registration")
        assert effective_privacy("public", None) == (False, "registration")

    def test_with_neither_the_node_is_public_by_default(self):
        assert effective_privacy(None, None) == (False, "default")

    def test_an_override_alone_needs_no_registration(self):
        assert effective_privacy(None, True) == (True, "override")


class TestPrecedenceOverTheFleet:
    """The same rule composed by _query over both tables."""

    def test_a_private_registration_with_no_override_is_private(self, seed_nodes):
        seed_nodes(**{_PRIV: "private"})
        assert is_private(_PRIV)

    def test_a_public_override_unhides_a_private_registration(self, seed_nodes, seed_override):
        """The owner changed their mind after onboarding."""
        seed_nodes(**{_PRIV: "private"})
        seed_override(**{_PRIV: False})
        assert not is_private(_PRIV)

    def test_a_private_override_hides_a_node_that_never_registered(self, seed_override):
        """The synthetic fleet and every mirrored node: no row in `nodes` at all.

        This is the case the table exists for — on the test droplet it is the
        only way to make anything private.
        """
        seed_override(**{"never-registered": True})
        assert is_private("never-registered")

    def test_a_private_override_hides_a_publicly_registered_node(self, seed_nodes, seed_override):
        seed_nodes(**{_PUB: "public"})
        seed_override(**{_PUB: True})
        assert is_private(_PUB)

    def test_deleting_the_override_returns_the_registration_choice(self, seed_nodes, seed_override):
        """A reflash rewrites Node.publication and does not touch the override,
        so the fallback has to still be there when the override goes."""
        seed_nodes(**{_PRIV: "private"})
        seed_override(**{_PRIV: False})
        assert not is_private(_PRIV)

        asyncio.run(publication.clear_location_privacy(_PRIV))
        asyncio.set_event_loop(asyncio.new_event_loop())
        publication.invalidate()
        assert is_private(_PRIV)

    def test_deleting_an_override_on_an_unregistered_node_leaves_it_public(self, seed_override):
        seed_override(**{"never-registered": True})
        assert is_private("never-registered")

        asyncio.run(publication.clear_location_privacy("never-registered"))
        asyncio.set_event_loop(asyncio.new_event_loop())
        publication.invalidate()
        assert not is_private("never-registered")


class TestInvalidate:
    def test_invalidate_makes_the_next_call_re_query(self, seed_nodes, monkeypatch):
        """Without this a dashboard change is honoured somewhere in the next
        30 s, which on a switch that hides a node reads as nothing happening."""
        seed_nodes(**{_PRIV: "private"})
        assert private_node_ids() == frozenset({_PRIV})
        monkeypatch.setattr(publication, "_query", lambda: frozenset({"someone-else"}))
        assert private_node_ids() == frozenset({_PRIV})

        publication.invalidate()
        assert private_node_ids() == frozenset({"someone-else"})

    def test_invalidate_does_not_blank_the_answer_it_is_dropping(self, seed_nodes, monkeypatch):
        """The window between the drop and the next refresh must not publish a
        private node, and a failure in that window must still be a failure
        rather than a silent fail-open."""
        seed_nodes(**{_PRIV: "private"})
        assert private_node_ids() == frozenset({_PRIV})

        def _boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(publication, "_query", _boom)
        publication.invalidate()
        assert private_node_ids() == frozenset({_PRIV})


class TestLocationPrivacyStorage:
    """The async accessors the routes are built on."""

    def _state(self, node_id):
        out = asyncio.run(publication.location_privacy(node_id))
        asyncio.set_event_loop(asyncio.new_event_loop())
        return out

    def test_an_unknown_node_reports_the_default_with_no_rows(self):
        assert self._state("never-heard-of-it") == {
            "node_id": "never-heard-of-it",
            "location_private": False,
            "location_privacy_source": "default",
            "registration_choice": None,
            "override": None,
        }

    def test_the_registration_choice_is_reported_raw_beside_the_effective_state(self, seed_nodes):
        seed_nodes(**{_PRIV: "private"})
        assert self._state(_PRIV) == {
            "node_id": _PRIV,
            "location_private": True,
            "location_privacy_source": "registration",
            "registration_choice": "private",
            "override": None,
        }

    def test_an_override_is_reported_with_its_provenance(self, seed_nodes, seed_override):
        seed_nodes(**{_PRIV: "private"})
        seed_override(**{_PRIV: False})
        state_now = self._state(_PRIV)
        assert state_now["location_private"] is False
        assert state_now["location_privacy_source"] == "override"
        assert state_now["registration_choice"] == "private"
        assert state_now["override"] == {"private": False, "set_by": "test", "set_at": 1_700_000_000.0}

    def test_setting_twice_corrects_the_row_rather_than_adding_one(self):
        asyncio.run(publication.set_location_privacy(_PUB, True, set_by="user-a"))
        asyncio.run(publication.set_location_privacy(_PUB, False, set_by="user-b"))
        asyncio.set_event_loop(asyncio.new_event_loop())
        override = self._state(_PUB)["override"]
        assert override["private"] is False
        assert override["set_by"] == "user-b"

    def test_clearing_a_node_with_no_override_is_not_an_error(self):
        asyncio.run(publication.clear_location_privacy("never-heard-of-it"))
        asyncio.set_event_loop(asyncio.new_event_loop())
        assert self._state("never-heard-of-it")["override"] is None


# ── The aircraft feed ────────────────────────────────────────────────────────


def _payload():
    return {
        "now": 100.0,
        "messages": 3,
        "aircraft": [
            {"hex": "AAA111", "node_id": _PRIV, "lat": 1.0, "lon": 2.0, "multinode": False},
            {"hex": "BBB222", "node_id": _PUB, "lat": 3.0, "lon": 4.0, "multinode": False},
            {
                "hex": "mnCCC333",
                "node_id": None,
                "lat": 5.0,
                "lon": 6.0,
                "multinode": True,
                "contributing_node_ids": [_PRIV, _PUB],
            },
        ],
        "detection_arcs": [
            {"hex": "AAA111", "node_id": _PRIV, "ambiguity_arc": [[1.0, 2.0], [1.1, 2.1]]},
            {"hex": "BBB222", "node_id": _PUB, "ambiguity_arc": [[3.0, 4.0], [3.1, 4.1]]},
        ],
        "detecting_nodes": {"AAA111": [_PRIV], "BBB222": [_PUB], "mnCCC333": [_PRIV, _PUB]},
        "ground_truth": {"BBB222": [[3.0, 4.0, 9000, 100.0]]},
        "anomaly_hexes": [],
    }


class TestPublicAircraftPayload:
    @pytest.fixture(autouse=True)
    def _private(self, monkeypatch):
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))

    def test_a_private_nodes_single_node_entry_is_dropped(self):
        out = public_aircraft_payload(_payload())
        assert [ac["hex"] for ac in out["aircraft"]] == ["BBB222", "mnCCC333"]

    def test_a_multinode_solve_survives_without_the_private_member(self):
        """The position is the network's product; the membership list is not."""
        mn = next(ac for ac in public_aircraft_payload(_payload())["aircraft"] if ac["hex"] == "mnCCC333")
        assert (mn["lat"], mn["lon"]) == (5.0, 6.0)
        assert mn["contributing_node_ids"] == [_PUB]

    def test_an_all_private_multinode_solve_is_dropped(self):
        data = _payload()
        data["aircraft"] = [
            {"hex": "mnDDD444", "multinode": True, "contributing_node_ids": [_PRIV], "lat": 1.0, "lon": 1.0}
        ]
        assert public_aircraft_payload(data)["aircraft"] == []

    def test_the_source_entry_is_not_mutated(self):
        data = _payload()
        public_aircraft_payload(data)
        mn = data["aircraft"][2]
        assert mn["contributing_node_ids"] == [_PRIV, _PUB]

    def test_a_private_nodes_pending_arc_is_dropped(self):
        """An ambiguity arc is an ellipse with the receiver at a focus."""
        out = public_aircraft_payload(_payload())
        assert [a["node_id"] for a in out["detection_arcs"]] == [_PUB]

    def test_detecting_nodes_loses_private_ids_and_empty_hexes(self):
        out = public_aircraft_payload(_payload())
        assert out["detecting_nodes"] == {"BBB222": [_PUB], "mnCCC333": [_PUB]}

    def test_messages_follows_what_is_served(self):
        """A stale count would say how many entries were removed."""
        assert public_aircraft_payload(_payload())["messages"] == 2

    def test_untouched_keys_ride_along(self):
        out = public_aircraft_payload(_payload())
        assert out["now"] == 100.0
        assert out["ground_truth"] == {"BBB222": [[3.0, 4.0, 9000, 100.0]]}

    def test_no_private_nodes_returns_the_same_object(self, monkeypatch):
        """The 1 Hz path reuses the bytes it already serialised."""
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset())
        data = _payload()
        assert public_aircraft_payload(data) is data


class TestPublicOwnerSplit:
    """The owner still sees their own private node; nobody else does.

    This is the placement decision, asserted: the redaction happens on the way
    to the public payload, not inside the feed build, so the per-owner filter
    can still see the entry before deciding the caller owns it.
    """

    @pytest.fixture(autouse=True)
    def _private(self, monkeypatch):
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))

    def _broadcast(self):
        from services.tasks.aircraft_flush import broadcast_aircraft

        data = _payload()
        asyncio.run(broadcast_aircraft(data, orjson.dumps(data)))
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_the_public_bytes_have_no_private_entry(self):
        self._broadcast()
        served = orjson.loads(state.latest_aircraft_json_bytes)
        assert [ac["hex"] for ac in served["aircraft"]] == ["BBB222", "mnCCC333"]

    def test_the_owner_feed_still_shows_the_owner_their_own_node(self):
        from services.tasks.aircraft_flush import filter_payload_to_nodes

        self._broadcast()
        owner = orjson.loads(filter_payload_to_nodes(state.latest_aircraft_json, {_PRIV}))
        assert [ac["hex"] for ac in owner["aircraft"]] == ["AAA111", "mnCCC333"]

    def test_the_unredacted_dict_is_kept_for_that_purpose(self):
        self._broadcast()
        assert [ac["hex"] for ac in state.latest_aircraft_json["aircraft"]] == ["AAA111", "BBB222", "mnCCC333"]
        assert [ac["hex"] for ac in state.latest_aircraft_json_public["aircraft"]] == ["BBB222", "mnCCC333"]


# ── Analytics and nodes ──────────────────────────────────────────────────────


class TestPublicSummaries:
    def test_a_private_node_has_no_summary(self, monkeypatch):
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))
        assert public_summaries({_PRIV: {"node_id": _PRIV}, _PUB: {"node_id": _PUB}}) == {_PUB: {"node_id": _PUB}}

    def test_nothing_private_returns_the_same_object(self, monkeypatch):
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset())
        summaries = {_PUB: {"node_id": _PUB}}
        assert public_summaries(summaries) is summaries


class TestPerNodeAnalyticsRoute:
    def test_a_private_node_is_404(self, client, seed_nodes):
        """404 rather than 403: the two differ only in confirming it exists."""
        seed_nodes(**{_PRIV: "private"})
        state.node_analytics.register_node(_PRIV, {"node_id": _PRIV, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            assert client.get(f"/api/radar/analytics/{_PRIV}").status_code == 404
        finally:
            state.node_analytics.retire_node(_PRIV)

    def test_a_public_node_still_answers(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        state.node_analytics.register_node(_PUB, {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            assert client.get(f"/api/radar/analytics/{_PUB}").status_code == 200
        finally:
            state.node_analytics.retire_node(_PUB)


class TestRadarNodesPayload:
    def test_a_private_node_is_absent_from_the_listing_and_the_counts(self, seed_nodes):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{_PRIV: "private", _PUB: "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        state.connected_nodes[_PRIV] = {"status": "active", "config": {**cfg, "node_id": _PRIV}}
        state.connected_nodes[_PUB] = {"status": "active", "config": {**cfg, "node_id": _PUB}}
        try:
            _refresh_analytics_and_nodes()
        finally:
            state.connected_nodes.pop(_PRIV, None)
            state.connected_nodes.pop(_PUB, None)

        body = orjson.loads(state.latest_nodes_bytes)
        assert _PRIV not in body["nodes"]
        assert _PUB in body["nodes"]
        assert body["total"] == 1


class TestOwnerSeesTheirOwnPrivateNodeInAnalytics:
    """/api/radar/analytics is where the map gets its node markers, discs and
    coverage, so dropping a private node from it hides that node from its own
    owner's dashboard as well as from the public.  These pin the exception: the
    owner's node comes back, in the same fuzzed frame everyone else would have
    got, and nothing about the unauthenticated answer moves.

    The suite runs with core.users' anonymous-admin bypass opted in, so "the
    logged-in caller" here is that anonymous admin and ownership is a
    ``node_owners`` row against its all-zero uuid.  The unauthenticated test
    below switches the bypass off in the route module to get a genuinely
    anonymous caller.
    """

    # Both coordinate pairs: without a transmitter the analytics manager
    # builds no detection area, and the fuzzed `rx` block this asserts on
    # lives inside it.
    RX = {"node_id": _PRIV, "rx_lat": 34.0, "rx_lon": -82.0, "tx_lat": 34.2, "tx_lon": -82.3}
    # Whitespace orjson would never emit, so a route that parsed and re-dumped
    # this cannot hand it back unchanged.  That is the whole assertion for the
    # unauthenticated path: identity, not equality of meaning.
    CACHED = b'{"nodes":   {"other-node": {"node_id": "other-node"}},   "cross_node": {}}'

    @pytest.fixture()
    def analytics_node(self):
        import services.public_location as pl

        # The fuzz is what the owner's copy has to come back through, and
        # another test in this file turns it off by environment; reset the
        # module so this one cannot inherit that answer.
        pl._reset_for_tests()
        state.node_analytics.register_node(_PRIV, dict(self.RX))
        state.latest_analytics_bytes = self.CACHED
        state.latest_analytics_real_bytes = self.CACHED
        try:
            yield _PRIV
        finally:
            state.node_analytics.retire_node(_PRIV)

    @staticmethod
    def _own(node_id, user_id):
        from core.auth import set_node_owner

        asyncio.run(set_node_owner(node_id, user_id))
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_an_unauthenticated_caller_gets_the_cached_bytes_verbatim(
        self, client, seed_nodes, analytics_node, monkeypatch
    ):
        import routes.analytics as an

        monkeypatch.setattr(an, "AUTH_BYPASS", False)
        seed_nodes(**{_PRIV: "private"})
        assert client.get("/api/radar/analytics").content == self.CACHED

    def test_a_public_fleet_costs_no_identity_check_at_all(self, client, analytics_node, monkeypatch):
        """Nothing private, nothing to add — and the ownership lookup is skipped
        rather than run and discarded, which is what keeps the common case one
        cached-bytes write."""
        import routes.analytics as an

        def _never(_request):
            raise AssertionError("the owner lookup ran with nothing private")

        monkeypatch.setattr(an, "_optional_owned_nodes", _never)
        assert client.get("/api/radar/analytics").content == self.CACHED

    def test_an_owner_gets_their_private_nodes_summary_back(self, client, seed_nodes, analytics_node):
        from core.users import ANONYMOUS_USER

        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, ANONYMOUS_USER["id"])
        try:
            body = client.get("/api/radar/analytics").json()
        finally:
            self._own(_PRIV, None)
        assert _PRIV in body["nodes"]
        # The rest of the cached payload rides along untouched.
        assert body["nodes"]["other-node"] == {"node_id": "other-node"}

    def test_the_owners_copy_is_the_same_fuzzed_frame_the_public_would_get(self, client, seed_nodes, analytics_node):
        """An owner is not an admin.  They already know where their own receiver
        is, so serving the truth here buys them nothing and makes this route a
        second, quieter place the real geometry is published from."""
        from core.users import ANONYMOUS_USER

        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, ANONYMOUS_USER["id"])
        try:
            body = client.get("/api/radar/analytics").json()
        finally:
            self._own(_PRIV, None)

        expected = orjson.loads(
            orjson.dumps(
                public_node_summary(_PRIV, state.node_analytics.get_node_summary(_PRIV)),
                option=orjson.OPT_SERIALIZE_NUMPY,
            )
        )
        assert body["nodes"][_PRIV] == expected
        rx = body["nodes"][_PRIV]["detection_area"]["rx"]
        assert rx["lat"] != self.RX["rx_lat"]
        assert "location_uncertainty_km" in rx

    def test_a_logged_in_non_owner_does_not(self, client, seed_nodes, analytics_node):
        """The node is owned — by somebody else."""
        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, "11111111-1111-1111-1111-111111111111")
        try:
            assert client.get("/api/radar/analytics").content == self.CACHED
        finally:
            self._own(_PRIV, None)

    def test_the_real_only_variant_merges_the_same_way(self, client, seed_nodes, analytics_node):
        from core.users import ANONYMOUS_USER

        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, ANONYMOUS_USER["id"])
        try:
            body = client.get("/api/radar/analytics?real_only=true").json()
        finally:
            self._own(_PRIV, None)
        assert _PRIV in body["nodes"]

    def test_the_per_node_route_answers_the_owner(self, client, seed_nodes, analytics_node):
        from core.users import ANONYMOUS_USER

        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, ANONYMOUS_USER["id"])
        try:
            r = client.get(f"/api/radar/analytics/{_PRIV}")
        finally:
            self._own(_PRIV, None)
        assert r.status_code == 200
        assert r.json()["node_id"] == _PRIV

    def test_the_per_node_route_is_still_404_for_everyone_else(self, client, seed_nodes, analytics_node):
        seed_nodes(**{_PRIV: "private"})
        self._own(_PRIV, "11111111-1111-1111-1111-111111111111")
        try:
            assert client.get(f"/api/radar/analytics/{_PRIV}").status_code == 404
        finally:
            self._own(_PRIV, None)


# ── The archive ──────────────────────────────────────────────────────────────


class TestArchiveKeyParsing:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("year=2026/month=08/day=27/node_id=ret01/part-120000.parquet", "ret01"),
            ("2026/08/27/ret01/part-120000.json", "ret01"),
            ("part-120000.parquet", ""),
            ("", ""),
        ],
    )
    def test_node_id_comes_out_of_the_key(self, key, expected):
        from routes.archive import _key_node_id

        assert _key_node_id(key) == expected


class TestArchiveRoutes:
    KEY = f"year=2026/month=08/day=27/node_id={_PRIV}/part-120000.parquet"

    def test_a_private_nodes_file_is_not_listed(self, client, seed_nodes, monkeypatch):
        import routes.archive as ar

        seed_nodes(**{_PRIV: "private"})
        pub_key = self.KEY.replace(_PRIV, _PUB)
        monkeypatch.setattr(
            ar,
            "list_archived_files",
            lambda **kw: {
                "files": [{"key": self.KEY, "size_bytes": 1}, {"key": pub_key, "size_bytes": 1}],
                "count": 2,
                "total": 2,
            },
        )
        body = client.get("/api/data/archive").json()
        assert [f["key"] for f in body["files"]] == [pub_key]
        assert body["count"] == 1

    def test_a_private_nodes_file_does_not_download(self, client, seed_nodes, monkeypatch):
        """Same answer as a key that does not exist, so it cannot enumerate."""
        import routes.archive as ar

        seed_nodes(**{_PRIV: "private"})
        called = []
        monkeypatch.setattr(ar, "read_archived_file", lambda key: called.append(key) or {"frames": []})
        assert client.get(f"/api/data/archive/{self.KEY}").status_code == 404
        # 404 decided before the read, not by discarding what came back.
        assert called == []

    def test_a_public_nodes_file_still_downloads(self, client, seed_nodes, monkeypatch):
        import routes.archive as ar

        seed_nodes(**{_PRIV: "private"})
        monkeypatch.setattr(ar, "read_archived_file", lambda key: {"frames": []})
        assert client.get(f"/api/data/archive/{self.KEY.replace(_PRIV, _PUB)}").status_code == 200


# ── The single-node documents ────────────────────────────────────────────────


class TestSingleNodeSurfaces:
    """receiver.json and /api/radar/status quote exactly one node's receiver."""

    def test_receiver_json_withholds_a_private_nodes_position(self, seed_nodes):
        from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline

        seed_nodes(**{DEFAULT_NODE_CONFIG["node_id"]: "private"})
        out = PassiveRadarPipeline(dict(DEFAULT_NODE_CONFIG)).generate_receiver_json()
        assert out["lat"] is None and out["lon"] is None

    def test_receiver_json_publishes_a_public_nodes_position(self, seed_nodes):
        from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline

        seed_nodes(**{DEFAULT_NODE_CONFIG["node_id"]: "public"})
        out = PassiveRadarPipeline(dict(DEFAULT_NODE_CONFIG)).generate_receiver_json()
        assert out["lat"] is not None and out["lon"] is not None

    def test_radar_status_withholds_a_private_nodes_config_block(self, client, seed_nodes):
        from pipeline.passive_radar import DEFAULT_NODE_CONFIG

        seed_nodes(**{DEFAULT_NODE_CONFIG["node_id"]: "private"})
        cfg = client.get("/api/radar/status").json()["config"]
        assert cfg["rx_lat"] is None and cfg["rx_lon"] is None
        # TX is a licensed broadcast tower and is unaffected either way.
        assert cfg["tx_lat"] == DEFAULT_NODE_CONFIG["tx_lat"]


class TestFuzzDoesNotGateThis:
    """Publication enforcement is a stronger, separate promise from the fuzz."""

    def test_a_private_node_is_still_withheld_with_fuzzing_off(self, monkeypatch):
        import services.public_location as pl

        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))
        out = public_aircraft_payload(_payload())
        assert [ac["hex"] for ac in out["aircraft"]] == ["BBB222", "mnCCC333"]
