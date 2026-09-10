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
import hashlib
import os
import time
from types import SimpleNamespace

import orjson
import pytest
from fastapi.testclient import TestClient
from retina_custody.models import NodeIdentity
from retina_custody.packet_signer import canonicalize

os.environ.setdefault("RETINA_ENV", "test")

from core import state  # noqa: E402
from core.nodes import Node  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from main import app  # noqa: E402
from services import node_auth, node_refs, publication  # noqa: E402
from services.publication import (  # noqa: E402
    is_private,
    private_node_ids,
    public_aircraft_payload,
    public_summaries,
)

_PRIV = "privnode01"
_PUB = "pubnode01"


_MINTED: dict[str, str] = {}


def _seed_ref(node_id: str) -> str:
    """The ref a seeded row carries, minted once per node id and then stable.

    Minted the way node_auth does rather than derived from the node id: a ref
    that spells its own node id out makes "the id never reaches the wire"
    unassertable, because every such assertion matches the ref as well.
    """
    return _MINTED.setdefault(node_id, node_auth.mint_node_ref())


@pytest.fixture()
def seed_nodes():
    """seed_nodes(node_id="private"|"public", …) — write rows and drop the cache."""

    def _seed(**choices: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, choice in choices.items():
                    session.add(Node(node_id=nid, node_ref=_seed_ref(nid), publication=choice))
                await session.commit()

        asyncio.run(_go())
        # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
        # restores one for the same reason.
        asyncio.set_event_loop(asyncio.new_event_loop())
        publication._reset_for_tests()
        # Both caches sit in front of the same rows and both have a TTL, so a
        # previous test's map would otherwise answer for these ones.
        node_refs._reset_for_tests()

    return _seed


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _keys_named(value, name: str):
    """Every value carried under `name`, however deep.

    A structural walk rather than a named path, so a field this test has never
    heard of is caught by the same pass.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            if k == name:
                yield v
            yield from _keys_named(v, name)
    elif isinstance(value, list):
        for v in value:
            yield from _keys_named(v, name)


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
    def _private(self, monkeypatch, seed_nodes):
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))
        # The public node needs a node_ref row or the publication boundary
        # drops it after the redaction and these assertions read an empty feed
        # for the wrong reason (services/node_refs.py).
        seed_nodes(**{_PUB: "public"})

    def _broadcast(self):
        from services.tasks.aircraft_flush import broadcast_aircraft

        asyncio.run(broadcast_aircraft(_payload()))
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_the_public_bytes_have_no_private_entry(self):
        self._broadcast()
        served = orjson.loads(state.latest_aircraft_json_bytes)
        assert [ac["hex"] for ac in served["aircraft"]] == ["BBB222", "mnCCC333"]

    def test_the_owner_feed_still_shows_the_owner_their_own_node(self):
        from services.tasks.aircraft_flush import filter_payload_to_nodes

        self._broadcast()
        owner = filter_payload_to_nodes(state.latest_aircraft_json, {_PRIV})
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
            assert client.get(f"/api/radar/analytics/{_seed_ref(_PRIV)}").status_code == 404
        finally:
            state.node_analytics.retire_node(_PRIV)

    def test_a_public_node_still_answers(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        state.node_analytics.register_node(_PUB, {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            assert client.get(f"/api/radar/analytics/{_seed_ref(_PUB)}").status_code == 200
        finally:
            state.node_analytics.retire_node(_PUB)

    def test_the_private_node_id_is_not_accepted_as_a_path_parameter(self, client, seed_nodes):
        """The old identifier must stop resolving, or the rename buys nothing."""
        seed_nodes(**{_PUB: "public"})
        state.node_analytics.register_node(_PUB, {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            assert client.get(f"/api/radar/analytics/{_PUB}").status_code == 404
        finally:
            state.node_analytics.retire_node(_PUB)

    def test_a_miss_does_not_quote_what_was_asked_for(self, client, seed_nodes):
        """Unknown ref, private node and no-such-node give one indistinguishable answer."""
        seed_nodes(**{_PRIV: "private"})
        state.node_analytics.register_node(_PRIV, {"node_id": _PRIV, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            private = client.get(f"/api/radar/analytics/{_seed_ref(_PRIV)}")
            unknown = client.get("/api/radar/analytics/ndeffffffffff")
            raw_id = client.get(f"/api/radar/analytics/{_PRIV}")
        finally:
            state.node_analytics.retire_node(_PRIV)
        assert private.json() == unknown.json() == raw_id.json()
        assert _PRIV not in private.text

    def test_the_value_carries_no_node_id(self, client, seed_nodes):
        """The scrub the cached listing runs, on the route that builds its own.

        Re-keying the entry on the ref while leaving the id inside it publishes
        the mapping between the two, which is the whole disclosure.
        """
        seed_nodes(**{_PUB: "public"})
        ref = _seed_ref(_PUB)
        state.node_analytics.register_node(_PUB, {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0})
        try:
            body = client.get(f"/api/radar/analytics/{ref}")
        finally:
            state.node_analytics.retire_node(_PUB)
        assert body.status_code == 200
        assert body.json()["node_ref"] == ref
        assert _PUB not in body.text


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
        assert _seed_ref(_PRIV) not in body["nodes"]
        assert _seed_ref(_PUB) in body["nodes"]
        assert body["total"] == 1

    def test_the_listing_is_keyed_on_refs_and_names_fall_back_to_them(self, seed_nodes):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        state.connected_nodes["ret1a2b3c4d"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {**cfg, "node_id": "ret1a2b3c4d"},
        }
        try:
            _refresh_analytics_and_nodes()
        finally:
            state.connected_nodes.pop("ret1a2b3c4d", None)

        body = orjson.loads(state.latest_nodes_bytes)
        assert "ret1a2b3c4d" not in body["nodes"]
        (ref,) = body["nodes"].keys()
        assert ref.startswith("nde")
        assert body["nodes"][ref]["name"] == ref

    def test_a_name_that_is_a_node_id_is_replaced_by_the_ref(self, seed_nodes):
        """`name` is not in the v1 config schema, which forbids extra keys, but
        canonical_config passes unknown keys through unvalidated, so a node
        connecting over TCP supplies one and it goes out beside the ref
        standing in for its id.  Asserted against the bytes: a name is not the
        only place a value can surface."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        registered, unregistered, plain = "ret1a2b3c4d", "ret0badcafe", "ret2b3c4d5e"
        seed_nodes(**{registered: "public", plain: "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        names = {
            # Its neighbour's id, and the id of a node connected with no row.
            registered: unregistered,
            plain: registered,
            unregistered: "Example Site 2",
        }
        for nid, name in names.items():
            state.connected_nodes[nid] = {
                "status": "active",
                "is_synthetic": False,
                "config": {**cfg, "node_id": nid, "name": name},
            }
        try:
            _refresh_analytics_and_nodes()
        finally:
            for nid in names:
                state.connected_nodes.pop(nid, None)

        raw = state.latest_nodes_bytes
        for nid in names:
            assert nid.encode() not in raw
        nodes = orjson.loads(raw)["nodes"]
        assert nodes[_seed_ref(registered)]["name"] == _seed_ref(registered)
        assert nodes[_seed_ref(plain)]["name"] == _seed_ref(plain)

    def test_a_name_that_is_not_a_node_id_is_published_unchanged(self, seed_nodes):
        """The fallback is for the names that name a node, not for all of them."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public"})
        state.connected_nodes["ret1a2b3c4d"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {"rx_lat": 34.0, "rx_lon": -82.0, "node_id": "ret1a2b3c4d", "name": "Example Site 3"},
        }
        try:
            _refresh_analytics_and_nodes()
        finally:
            state.connected_nodes.pop("ret1a2b3c4d", None)

        nodes = orjson.loads(state.latest_nodes_bytes)["nodes"]
        assert nodes[_seed_ref("ret1a2b3c4d")]["name"] == "Example Site 3"

    def test_the_fuzzed_position_does_not_move_when_the_key_changes(self, seed_nodes):
        """The offset is HMAC-keyed on node_id; re-keying it would move the
        whole fleet, so the published position must be unchanged."""
        from services.public_location import public_latlon
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        state.connected_nodes["ret1a2b3c4d"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {**cfg, "node_id": "ret1a2b3c4d"},
        }
        try:
            _refresh_analytics_and_nodes()
        finally:
            state.connected_nodes.pop("ret1a2b3c4d", None)

        expected_lat, expected_lon = public_latlon(34.0, -82.0, "ret1a2b3c4d")
        (block,) = [n["location"] for n in orjson.loads(state.latest_nodes_bytes)["nodes"].values()]
        assert block["rx_lat"] == pytest.approx(expected_lat)
        assert block["rx_lon"] == pytest.approx(expected_lon)

    def test_a_node_with_no_registry_row_is_dropped(self, seed_nodes):
        """No ref, no entry: falling back to the id would publish the identifier
        this boundary exists to withhold."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        for nid in ("ret1a2b3c4d", "ret9f8e7d6c"):
            state.connected_nodes[nid] = {
                "status": "active",
                "is_synthetic": False,
                "config": {**cfg, "node_id": nid},
            }
        try:
            _refresh_analytics_and_nodes()
        finally:
            for nid in ("ret1a2b3c4d", "ret9f8e7d6c"):
                state.connected_nodes.pop(nid, None)

        body = orjson.loads(state.latest_nodes_bytes)
        assert list(body["nodes"]) == [_seed_ref("ret1a2b3c4d")]
        # The counts come from the published entries, so the dropped node is
        # not reinstated as an anonymous tally.
        assert body["total"] == 1
        assert body["connected"] == 1

    def test_the_real_only_analytics_variant_is_keyed_on_refs(self, seed_nodes):
        """The intersection is against connected_nodes, which is keyed on
        node_id, so re-keying before it would empty the variant."""
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public"})
        cfg = {"rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0}
        state.connected_nodes["ret1a2b3c4d"] = {
            "status": "active",
            "is_synthetic": False,
            "config": {**cfg, "node_id": "ret1a2b3c4d"},
        }
        state.node_analytics.register_node("ret1a2b3c4d", {"node_id": "ret1a2b3c4d", **cfg})
        state.node_analytics._summaries_cache = None
        try:
            _refresh_analytics_and_nodes()
        finally:
            state.node_analytics.retire_node("ret1a2b3c4d")
            state.node_analytics._summaries_cache = None
            state.connected_nodes.pop("ret1a2b3c4d", None)

        real = orjson.loads(state.latest_analytics_real_bytes)["nodes"]
        assert list(real) == [_seed_ref("ret1a2b3c4d")]


class TestAnalyticsPayloadIdentities:
    """/api/radar/analytics is the one surface that could give the map away.

    It is unauthenticated and it names the whole fleet, so an entry keyed on a
    ref while still carrying the node id behind it de-anonymises the aircraft
    feed, the arcs and the overlaps in a single request.
    """

    _A = "ret1a2b3c4d"
    _B = "ret9f8e7d6c"
    # Analytics knows this one; the registry does not, so it has no handle.
    _GHOST = "ret0badcafe"
    _CFG = {"rx_lat": 34.0, "rx_lon": -82.0, "tx_lat": 35.0, "tx_lon": -83.0}

    @pytest.fixture()
    def refreshed(self, seed_nodes):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{self._A: "public", self._B: "public"})
        for nid in (self._A, self._B, self._GHOST):
            state.connected_nodes[nid] = {
                "status": "active",
                "is_synthetic": False,
                "config": {**self._CFG, "node_id": nid},
            }
            state.node_analytics.register_node(nid, {"node_id": nid, **self._CFG})
        # Prose, not an identity field: a reputation penalty spells the
        # neighbour it disagreed with into a sentence.
        for nid in (self._B, self._GHOST):
            state.node_analytics.reputations[self._A].apply_penalty(
                0.08, f"Inconsistent with trusted neighbour {nid} (overlap=0.03)"
            )
        state.node_analytics._summaries_cache = None
        # Seeded rather than provoked: the pair overlaps are computed from real
        # geometry, and what is under test is the transform on the way out.
        state.node_analytics._cross_node_cache = {
            "pair_overlaps": [
                {"node_a": self._A, "node_b": self._B, "overlap_ratio": 0.4},
                {"node_a": self._A, "node_b": self._GHOST, "overlap_ratio": 0.4},
            ],
            "coverage_suggestions": [],
            "blocked_nodes": [self._B, self._GHOST],
        }
        state.node_analytics._cross_node_cache_ts = time.monotonic()
        try:
            _refresh_analytics_and_nodes()
            yield
        finally:
            for nid in (self._A, self._B, self._GHOST):
                state.connected_nodes.pop(nid, None)
                state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None
            state.node_analytics._cross_node_cache = None

    def test_no_node_id_reaches_the_published_bytes(self, refreshed):
        """At any depth, in either variant. The refs are minted, so this
        assertion means what it says (see _seed_ref)."""
        for raw in (state.latest_analytics_bytes, state.latest_analytics_real_bytes):
            for nid in (self._A, self._B, self._GHOST):
                assert nid.encode() not in raw

    def test_the_entry_names_the_node_once_and_by_ref(self, refreshed):
        """Every nested node_id repeated the map key, so all of them go."""
        entry = orjson.loads(state.latest_analytics_bytes)["nodes"][_seed_ref(self._A)]
        assert "node_id" not in entry
        for block in ("trust", "metrics", "reputation", "coverage_map", "detection_area"):
            assert "node_id" not in entry[block]

    def test_a_node_id_written_into_prose_is_rewritten_not_dropped(self, refreshed):
        entry = orjson.loads(state.latest_analytics_bytes)["nodes"][_seed_ref(self._A)]
        reasons = [p["reason"] for p in entry["reputation"]["recent_penalties"]]
        assert any(_seed_ref(self._B) in r for r in reasons)
        # The unresolvable neighbour loses its name, not the whole sentence.
        assert any("neighbour [unpublished node]" in r for r in reasons)

    def test_cross_node_carries_refs_and_drops_the_unresolvable(self, refreshed):
        cross = orjson.loads(state.latest_analytics_bytes)["cross_node"]
        assert cross["blocked_nodes"] == [_seed_ref(self._B)]
        # Half a named pair still describes a baseline, so the zone goes whole.
        assert cross["pair_overlaps"] == [
            {"node_a": _seed_ref(self._A), "node_b": _seed_ref(self._B), "overlap_ratio": 0.4}
        ]

    def test_the_fuzz_still_keys_on_the_node_id(self, refreshed):
        """The offset is HMAC-keyed on node_id; re-keying it would move the
        whole fleet, and the analytics payload is where the map reads rx."""
        from services.public_location import public_latlon

        entry = orjson.loads(state.latest_analytics_bytes)["nodes"][_seed_ref(self._A)]
        expected_lat, expected_lon = public_latlon(34.0, -82.0, self._A)
        assert entry["detection_area"]["rx"]["lat"] == pytest.approx(expected_lat)
        assert entry["detection_area"]["rx"]["lon"] == pytest.approx(expected_lon)


class TestAnalyticsPayloadIsSynthetic:
    """/api/radar/analytics carries is_synthetic per node so the client can stop
    guessing it from the identifier (see frontend/src/utils/nodeKind.ts): once
    identity is published as node_ref, no prefix survives to match. This must
    agree with /api/radar/nodes, which the same refresh cycle derives it for.
    """

    _REAL = "ret1a2b3c4d"
    # An id with no prefix a client would recognise as synthetic, but flagged
    # synthetic explicitly in connected_nodes: proves the flag comes from that
    # info, not a guess re-run on whatever the entry ends up keyed on.
    _FLAGGED = "ret9f8e7d6c"
    _CFG = {"rx_lat": 34.0, "rx_lon": -82.0, "tx_lat": 35.0, "tx_lon": -83.0}

    @pytest.fixture()
    def refreshed(self, seed_nodes):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{self._REAL: "public", self._FLAGGED: "public"})
        state.connected_nodes[self._REAL] = {
            "status": "active",
            "is_synthetic": False,
            "config": {**self._CFG, "node_id": self._REAL},
        }
        state.connected_nodes[self._FLAGGED] = {
            "status": "active",
            "is_synthetic": True,
            "config": {**self._CFG, "node_id": self._FLAGGED},
        }
        for nid in (self._REAL, self._FLAGGED):
            state.node_analytics.register_node(nid, {"node_id": nid, **self._CFG})
        state.node_analytics._summaries_cache = None
        try:
            _refresh_analytics_and_nodes()
            yield
        finally:
            for nid in (self._REAL, self._FLAGGED):
                state.connected_nodes.pop(nid, None)
                state.node_analytics.retire_node(nid)
            state.node_analytics._summaries_cache = None

    def test_full_variant_carries_the_flag_per_node(self, refreshed):
        nodes = orjson.loads(state.latest_analytics_bytes)["nodes"]
        assert nodes[_seed_ref(self._REAL)]["is_synthetic"] is False
        assert nodes[_seed_ref(self._FLAGGED)]["is_synthetic"] is True

    def test_the_flag_is_not_derived_from_the_published_ref(self, refreshed):
        """Neither ref starts with a recognised prefix, so a derivation that
        ran is_synthetic_node on the ref instead of the true node_id would
        report both nodes as real regardless of the override."""
        ref = _seed_ref(self._FLAGGED)
        assert not ref.startswith(("synth-", "e2e-", "test-", "realnode-"))
        nodes = orjson.loads(state.latest_analytics_bytes)["nodes"]
        assert nodes[ref]["is_synthetic"] is True

    def test_real_only_variant_also_carries_the_flag(self, refreshed):
        real = orjson.loads(state.latest_analytics_real_bytes)["nodes"]
        assert list(real) == [_seed_ref(self._REAL)]
        assert real[_seed_ref(self._REAL)]["is_synthetic"] is False

    def test_matches_the_nodes_payload_for_the_same_node(self, refreshed):
        """/api/radar/nodes and /api/radar/analytics feed the same client-side
        discriminator, so the two must not disagree about one node."""
        ref = _seed_ref(self._FLAGGED)
        analytics_entry = orjson.loads(state.latest_analytics_bytes)["nodes"][ref]
        nodes_entry = orjson.loads(state.latest_nodes_bytes)["nodes"][ref]
        assert analytics_entry["is_synthetic"] == nodes_entry["is_synthetic"] is True


class TestOverlapsPayload:
    """/api/radar/overlaps names nodes and nothing else, so the names are refs."""

    def test_pairs_and_the_registry_carry_refs_and_drop_the_unresolvable(self, seed_nodes, monkeypatch):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        seed_nodes(**{"ret1a2b3c4d": "public", "ret9f8e7d6c": "public"})

        class _Assoc:
            node_geometries = {"ret1a2b3c4d": object(), "ret9f8e7d6c": object(), "ret0badcafe": object()}

            def get_overlap_summary(self):
                return [
                    {"node_a": "ret1a2b3c4d", "node_b": "ret9f8e7d6c", "has_overlap": True},
                    # One side has no registry row: half a named pair still
                    # describes a baseline, so the whole zone goes.
                    {"node_a": "ret1a2b3c4d", "node_b": "ret0badcafe", "has_overlap": True},
                    {"node_a": "ret1a2b3c4d", "node_b": "ret9f8e7d6c", "has_overlap": False},
                ]

        monkeypatch.setattr(state, "node_associator", _Assoc())
        _refresh_analytics_and_nodes()

        body = orjson.loads(state.latest_overlaps_bytes)
        assert body["registered_nodes"] == [_seed_ref("ret1a2b3c4d"), _seed_ref("ret9f8e7d6c")]
        assert body["overlaps"] == [
            {"node_a": _seed_ref("ret1a2b3c4d"), "node_b": _seed_ref("ret9f8e7d6c"), "has_overlap": True}
        ]


class TestAssociationStatusPayload:
    """/api/radar/association/status names the same pairs as its sibling.

    Unauthenticated, and /api/radar/association/overlaps publishes that pair
    list under refs with a grid_points count that fingerprints each pair, so
    one payload naming a pair by id is a join on a shared key and the mapping
    falls out of the two together.
    """

    _A = "ret1a2b3c4d"
    _B = "ret9f8e7d6c"
    # Known to the associator, absent from the registry, so it has no handle.
    _GHOST = "ret0badcafe"

    class _Assoc:
        node_geometries: dict = {}
        overlap_zones: dict = {}
        claim_mode = "off"
        adsb_seed_mode = "off"

        def __init__(self, pending, overlaps):
            self._pending_tracks = pending
            self._overlaps = overlaps

        def get_overlap_summary(self):
            return self._overlaps

    @pytest.fixture()
    def body(self, client, seed_nodes, monkeypatch):
        seed_nodes(**{self._A: "public", self._B: "public"})
        monkeypatch.setattr(
            state,
            "node_associator",
            self._Assoc(
                {self._A: [object(), object()], self._B: [object()], self._GHOST: [object()]},
                [
                    {"node_a": self._A, "node_b": self._B, "grid_points": 512, "has_overlap": True},
                    {"node_a": self._A, "node_b": self._GHOST, "grid_points": 77, "has_overlap": True},
                ],
            ),
        )
        return client.get("/api/radar/association/status")

    def test_no_node_id_reaches_the_response(self, body):
        assert body.status_code == 200
        for nid in (self._A, self._B, self._GHOST):
            assert nid not in body.text
        assert not list(_keys_named(body.json(), "node_id"))

    def test_pending_tracks_is_keyed_on_refs_and_drops_the_unresolvable(self, body):
        assert body.json()["pending_tracks"] == {_seed_ref(self._A): 2, _seed_ref(self._B): 1}

    def test_the_pairs_carry_refs_and_the_half_named_zone_goes(self, body):
        assert body.json()["overlaps"] == [
            {"node_a": _seed_ref(self._A), "node_b": _seed_ref(self._B), "grid_points": 512, "has_overlap": True}
        ]


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

    def test_radar_status_names_no_node(self, client, seed_nodes):
        """The field names a process default with no registry row, so it is
        null whatever that default is pointed at."""
        from pipeline.passive_radar import DEFAULT_NODE_CONFIG

        seed_nodes(**{DEFAULT_NODE_CONFIG["node_id"]: "public"})
        body = client.get("/api/radar/status")
        assert body.json()["node_ref"] is None
        assert "node_id" not in body.json()
        assert DEFAULT_NODE_CONFIG["node_id"] not in body.text


class TestFuzzDoesNotGateThis:
    """Publication enforcement is a stronger, separate promise from the fuzz."""

    def test_a_private_node_is_still_withheld_with_fuzzing_off(self, monkeypatch):
        import services.public_location as pl

        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()
        monkeypatch.setattr(publication, "private_node_ids", lambda: frozenset({_PRIV}))
        out = public_aircraft_payload(_payload())
        assert [ac["hex"] for ac in out["aircraft"]] == ["BBB222", "mnCCC333"]


# ── The other public path parameters ──────────────────────────────────────────


class TestPublicPathParameters:
    """Custody and per-node test routes are addressed the same way.

    Each pair pins both halves of the rename: the ref reaches the node, and the
    node_id that used to reach it no longer does. An identifier that still
    answers is still published.
    """

    def _store_chain(self, node_id: str) -> None:
        state.chain_entries[node_id] = [{"node_id": node_id, "hour_utc": "2026-09-09T00", "_verified": True}]

    def test_custody_chain_takes_a_ref(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        self._store_chain(_PUB)
        try:
            body = client.get(f"/api/custody/chain/{_seed_ref(_PUB)}")
            assert body.status_code == 200
            assert body.json()["node_ref"] == _seed_ref(_PUB)
        finally:
            state.chain_entries.pop(_PUB, None)

    def test_custody_chain_does_not_take_the_node_id(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        self._store_chain(_PUB)
        try:
            assert client.get(f"/api/custody/chain/{_PUB}").status_code == 404
        finally:
            state.chain_entries.pop(_PUB, None)

    def test_the_signed_entry_bodies_are_withheld(self, client, seed_nodes):
        """Each entry's node_id is inside the ECDSA preimage the node signed,
        and the server holds only public keys, so it can be neither published
        under the ref nor rewritten.  The metadata goes out without it."""
        seed_nodes(**{_PUB: "public"})
        self._store_chain(_PUB)
        try:
            body = client.get(f"/api/custody/chain/{_seed_ref(_PUB)}")
        finally:
            state.chain_entries.pop(_PUB, None)
        assert "entries" not in body.json()
        assert body.json()["chain_length"] == 1
        assert body.json()["latest_hour"] == "2026-09-09T00"
        assert body.json()["latest_verified"] is True
        assert body.json()["verified_entries"] == 1

    def test_the_custody_chain_carries_no_node_id_at_any_depth(self, client, seed_nodes):
        """Refs are enumerable, so one request per ref would otherwise hand out
        the whole mapping."""
        seed_nodes(**{_PUB: "public"})
        state.node_identities[_PUB] = NodeIdentity(
            node_id=_PUB,
            public_key_pem="-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
            public_key_fingerprint="ff00",
            serial_number="SER-1",
            signing_mode="software",
            registered_at="2026-09-09T00:00:00Z",
        )
        self._store_chain(_PUB)
        try:
            body = client.get(f"/api/custody/chain/{_seed_ref(_PUB)}")
        finally:
            state.chain_entries.pop(_PUB, None)
            state.node_identities.pop(_PUB, None)
        assert body.status_code == 200
        assert body.json()["identity"]["node_ref"] == _seed_ref(_PUB)
        assert _PUB not in body.text
        assert not list(_keys_named(body.json(), "node_id"))

    def test_custody_verify_issues_carry_no_node_id(self, client, seed_nodes):
        """The verifier spells the node's id into an issue when it holds no key
        for it; the boundary rewrites it inside the sentence."""
        seed_nodes(**{_PUB: "public"})
        state.node_identities[_PUB] = NodeIdentity(
            node_id=_PUB,
            public_key_pem="",
            public_key_fingerprint="",
            serial_number="",
            signing_mode="software",
        )
        entry = {
            "node_id": _PUB,
            "hour_utc": "2026-09-09T00:00:00Z",
            "prev_hash": "genesis",
            "detections_hash": "d" * 64,
            "n_detections": 1,
            "node_config_hash": "c" * 64,
            "firmware_version": "1.0",
            "timestamp_utc": "2026-09-09T00:00:00Z",
        }
        entry_hash = hashlib.sha256(canonicalize(entry)).hexdigest()
        state.chain_entries[_PUB] = [{**entry, "entry_hash": entry_hash, "signature": "", "signing_mode": "software"}]
        try:
            body = client.get(f"/api/custody/verify/{_seed_ref(_PUB)}")
        finally:
            state.chain_entries.pop(_PUB, None)
            state.node_identities.pop(_PUB, None)
        assert body.status_code == 200
        # The issue is the one that names the node, so this asserts the rename
        # rather than the absence of any issue at all.
        assert any("no public key" in i for i in body.json()["issues"])
        assert _PUB not in body.text
        assert _seed_ref(_PUB) in body.text

    def test_custody_verify_takes_a_ref_and_not_the_node_id(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        self._store_chain(_PUB)
        try:
            # 400 rather than 404: the chain is found, no public key is registered.
            assert client.get(f"/api/custody/verify/{_seed_ref(_PUB)}").status_code == 400
            assert client.get(f"/api/custody/verify/{_PUB}").status_code == 404
        finally:
            state.chain_entries.pop(_PUB, None)

    def test_custody_status_is_keyed_on_refs(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        self._store_chain(_PUB)
        try:
            body = client.get("/api/custody/status")
        finally:
            state.chain_entries.pop(_PUB, None)
        assert list(body.json()["chain_entries"]) == [_seed_ref(_PUB)]
        assert _PUB not in body.text

    def test_node_verification_takes_a_ref_and_not_the_node_id(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        state.latest_node_verification_bytes[_PUB] = orjson.dumps({"node_id": _PUB, "n_tracks": 3})
        try:
            body = client.get(f"/api/test/node/{_seed_ref(_PUB)}/verification")
            assert body.json() == {"n_tracks": 3, "node_ref": _seed_ref(_PUB)}
            # An unresolvable ref answers exactly as an unknown node does.
            assert client.get(f"/api/test/node/{_PUB}/verification").json() == {}
        finally:
            state.latest_node_verification_bytes.pop(_PUB, None)

    def test_detection_range_takes_a_ref_and_not_the_node_id(self, client, seed_nodes):
        seed_nodes(**{_PUB: "public"})
        # Full geometry: without both pairs the node gets no detection area.
        state.node_analytics.register_node(
            _PUB, {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0, "tx_lat": 34.1, "tx_lon": -82.1}
        )
        try:
            body = client.get(f"/api/test/node/{_seed_ref(_PUB)}/detection-range")
            assert body.status_code == 200
            assert body.json()["node_ref"] == _seed_ref(_PUB)
            assert _PUB not in body.text
            assert client.get(f"/api/test/node/{_PUB}/detection-range").status_code == 404
        finally:
            state.node_analytics.retire_node(_PUB)

    def test_a_404_does_not_quote_what_was_asked_for(self, client):
        for path in (
            f"/api/custody/chain/{_PUB}",
            f"/api/custody/verify/{_PUB}",
            f"/api/test/node/{_PUB}/detection-range",
        ):
            assert _PUB not in client.get(path).text, path

    def test_the_hardcoded_radar3_routes_are_gone(self, client):
        """They named a production site in the route path, which no
        response-body change reaches."""
        assert client.get("/api/test/radar3/verification").status_code == 404
        assert client.get("/api/test/radar3/detection-range").status_code == 404


# ── The solver's diagnostic stores ───────────────────────────────────────────


class TestMlatHistoryPayload:
    """/api/test/mlat-history is unauthenticated and dumps solver records whole.

    Two things ride out on them. Every record carries ``adsb_hex``, which the
    aircraft feed publishes beside ``contributing_node_refs``, so one record
    naming its nodes by node_id hands over the mapping for the whole fleet in
    two anonymous requests. And a beam entry measures a range and a bearing
    from the node's true receiver to an aircraft the same record locates, which
    is a position fix whatever identifier it is filed under.
    """

    _A = "ret1a2b3c4d"
    _B = "ret9f8e7d6c"
    # Named by a record and absent from the registry, so it has no handle.
    _GHOST = "ret0badcafe"

    def _beam(self, node_id):
        """A beam entry with every field the gate stamps on one."""
        return {
            "node_id": node_id,
            "range_km": 6.2,
            "max_range_km": 150.0,
            "bistatic_km": 12.4,
            "max_bistatic_range_km": 200.0,
            "bearing_off_deg": 73.9,
            "half_width_deg": 30.0,
            "rule": "bearing",
            "fov_state": "closed",
            "fov_limit_km": 41.5,
        }

    @pytest.fixture()
    def seeded(self, seed_nodes):
        seed_nodes(**{self._A: "public", self._B: "public"})
        now_ms = int(time.time() * 1000)
        state.mlat_solve_history.extend(
            [
                {
                    "ts_ms": now_ms,
                    "outcome": "published",
                    "solve_key": "mn-dark-0123456789",
                    "solver_hex": "mn0123456789",
                    "adsb_hex": None,
                    "n_nodes": 2,
                    "contributing_node_ids": [self._A, self._B],
                    "trimmed_node_ids": [self._B],
                    # The contamination stamp and the point it was taken at:
                    # solver._stamp_foreign_nodes writes both onto a dark
                    # record that matched ground truth.
                    "foreign_node_ids": [self._B],
                    "contaminated": True,
                    "gt_hex": "gt1",
                    "gt_lat": 34.0,
                    "gt_lon": -82.0,
                    "adopt_meta": {
                        "pool_n": 3,
                        "adopted_node_ids": [self._A],
                        "dropped_node_id": self._B,
                        "outcome": "widened",
                    },
                    "raw_lat": 34.0,
                    "raw_lon": -82.0,
                },
                {
                    "ts_ms": now_ms,
                    "outcome": "rejected_beam",
                    "solve_key": None,
                    "solver_hex": None,
                    "adsb_hex": "abc123",
                    "n_nodes": 2,
                    "contributing_node_ids": [self._A],
                    "beam_failures": [self._beam(self._A), self._beam(self._GHOST)],
                    "fov_verdict": [{"node_id": self._A, "today_pass": False, "fov_verdict": True}],
                    "raw_lat": 34.0,
                    "raw_lon": -82.0,
                },
                {
                    "ts_ms": now_ms,
                    "outcome": "rejected_rms_delay",
                    "solve_key": None,
                    "solver_hex": None,
                    "adsb_hex": "def456",
                    "n_nodes": 2,
                    "contributing_node_ids": [self._A, self._GHOST],
                    "raw_lat": 34.0,
                    "raw_lon": -82.0,
                },
            ]
        )
        state.solver_resolve_skips_recent.append(
            {
                "ts_ms": now_ms,
                "lane": "dark",
                "track_ids": ["260909-00001A"],
                "n_nodes": 3,
                "blocking": [{"track_id": "260909-00001A", "held_ts": 1.5, "held_n": 3}],
                "guess_lat": 34.0,
                "guess_lon": -82.0,
            }
        )
        try:
            yield
        finally:
            state.mlat_solve_history.clear()
            state.solver_resolve_skips_recent.clear()

    @pytest.fixture()
    def bodies(self, client, seeded):
        """Every shape the route answers in: the dump, one marker, the skips."""
        return {
            "all": client.get("/api/test/mlat-history?all=1"),
            "hex": client.get("/api/test/mlat-history?hex=mn0123456789"),
            "skips": client.get("/api/test/mlat-history?kind=resolve_skips"),
        }

    def test_no_node_id_reaches_the_response(self, bodies):
        """Values and field names both. Every identity key the solver writes
        (node_id, contributing_node_ids, trimmed_node_ids, foreign_node_ids,
        adopted_node_ids, dropped_node_id) carries that substring, so one
        assertion covers the lot, including a key added upstream later."""
        for shape, body in bodies.items():
            assert body.status_code == 200, shape
            assert "node_id" not in body.text, shape
            for nid in (self._A, self._B, self._GHOST):
                assert nid not in body.text, (shape, nid)

    def test_the_identity_fields_move_with_their_values(self, bodies):
        rec = {r["outcome"]: r for r in bodies["all"].json()["records"]}["published"]
        assert rec["contributing_node_refs"] == [_seed_ref(self._A), _seed_ref(self._B)]
        assert rec["trimmed_node_refs"] == [_seed_ref(self._B)]
        assert rec["adopt_meta"]["adopted_node_refs"] == [_seed_ref(self._A)]
        assert rec["adopt_meta"]["dropped_node_ref"] == _seed_ref(self._B)

    def test_a_node_with_no_handle_loses_its_place_in_the_listing(self, bodies):
        by_outcome = {r["outcome"]: r for r in bodies["all"].json()["records"]}
        assert by_outcome["rejected_rms_delay"]["contributing_node_refs"] == [_seed_ref(self._A)]
        # ...and its own beam entry goes rather than being published anonymously.
        assert [b["node_ref"] for b in by_outcome["rejected_beam"]["beam_failures"]] == [_seed_ref(self._A)]

    def test_the_beam_entry_keeps_the_verdict_and_loses_the_geometry(self, bodies):
        """Exact equality, so it pins what survives as well as what does not.

        What is left is the gate that refused the node and the envelope
        /api/radar/analytics already publishes for it per node; what goes is
        every margin, each measured from the true receiver to the aircraft this
        same record gives the position of.
        """
        by_outcome = {r["outcome"]: r for r in bodies["all"].json()["records"]}
        assert by_outcome["rejected_beam"]["beam_failures"] == [
            {
                "node_ref": _seed_ref(self._A),
                "max_range_km": 150.0,
                "max_bistatic_range_km": 200.0,
                "half_width_deg": 30.0,
                "rule": "bearing",
            }
        ]

    def test_no_receiver_relative_geometry_at_any_depth(self, bodies):
        """A structural walk rather than a text scan: `range_km` is a substring
        of the `max_range_km` that legitimately stays."""
        for shape, body in bodies.items():
            for field in (
                "range_km",
                "bearing_off_deg",
                "bistatic_km",
                "fov_limit_km",
                "fov_state",
                "fov_verdict",
                "today_pass",
                "foreign_node_ids",
                "foreign_node_refs",
                "contaminated",
            ):
                assert not list(_keys_named(body.json(), field)), (shape, field)

    def test_the_contamination_stamp_goes_with_the_geometry_it_measures(self, bodies):
        """`foreign_node_ids` is a per-node in-or-out verdict against the beam
        /api/radar/analytics publishes per node, taken at the ground-truth point
        the same record still carries. That makes it a labelled sample of a
        known shape rather than only an identity field."""
        rec = {r["outcome"]: r for r in bodies["all"].json()["records"]}["published"]
        assert (rec["gt_lat"], rec["gt_lon"]) == (34.0, -82.0)
        assert "foreign_node_refs" not in rec
        assert "contaminated" not in rec

    def test_the_shadow_verdicts_go_and_the_refusal_stays(self, bodies):
        """Each shadow entry is a containment test at the true bearing and
        range, stamped for the nodes that PASSED as well as the ones that
        failed, so it bounds the receiver where a failure only excludes it.
        With both verdicts out the entry says nothing, so the block goes."""
        by_outcome = {r["outcome"]: r for r in bodies["all"].json()["records"]}
        assert "fov_verdict" not in by_outcome["rejected_beam"]
        assert by_outcome["rejected_beam"]["beam_failures"]

    def test_one_markers_solves_are_published_the_same_way(self, bodies):
        solves = bodies["hex"].json()["solves"]
        assert [s["contributing_node_refs"] for s in solves] == [[_seed_ref(self._A), _seed_ref(self._B)]]

    def test_the_skip_store_goes_through_the_same_boundary_intact(self, bodies):
        """It names no node today; it is republished anyway, and must come back
        unchanged rather than mangled by the walk."""
        records = bodies["skips"].json()["records"]
        assert records == [
            {
                "ts_ms": records[0]["ts_ms"],
                "lane": "dark",
                "track_ids": ["260909-00001A"],
                "n_nodes": 3,
                "blocking": [{"track_id": "260909-00001A", "held_ts": 1.5, "held_n": 3}],
                "guess_lat": 34.0,
                "guess_lon": -82.0,
            }
        ]


class TestNodeVerificationPayload:
    """/api/test/node/{node_ref}/verification is unauthenticated and per node.

    Everything in a track entry is measured from that one node's true receiver.
    The delays are bistatic ranges to a position the same entry publishes, so
    each entry is a locus the receiver sits on. solver_lat/solver_lon is the
    node's own single-node solve in the true frame, and the aircraft feed
    publishes those same two fields displaced with the icon
    (services/track_gates.py), so the pair differences to the displacement.

    The payload is built by the refresh task rather than written out here: an
    invented fixture pins a shape the route never answers in, which is how
    solver_lat/solver_lon reached production unnoticed.
    """

    # One decimal place: far enough out that no fixture here sits inside the
    # published fuzz annulus of a real site (tests/test_no_real_identities.py).
    _RX = (34.9, -82.4)
    _TX = (35.0, -82.2)
    _TRUTH = (34.8, -82.3)
    _SOLVED = (34.7, -82.2)

    _WITHHELD = ("solver_lat", "solver_lon", "measured_delay_us", "delay_match_us")

    @pytest.fixture()
    def stored(self, seed_nodes):
        """One real verification record, from the task that writes them.

        The delay carried by the track is the bistatic range to the ADS-B
        candidate, so the matcher pairs the two exactly as it does live.
        """
        from services.geo import bistatic_delay_us
        from services.tasks.analytics_refresh import _refresh_node_verification

        seed_nodes(**{_PUB: "public"})
        cfg = {
            "node_id": _PUB,
            "rx_lat": self._RX[0],
            "rx_lon": self._RX[1],
            "tx_lat": self._TX[0],
            "tx_lon": self._TX[1],
        }
        track = SimpleNamespace(
            latest_delay_us=bistatic_delay_us(*self._TX, *self._RX, *self._TRUTH),
            wall_clock_ts=time.time(),
            lat=self._SOLVED[0],
            lon=self._SOLVED[1],
            vel_east=180.0,
            vel_north=60.0,
            alt_m=10050.0,
        )
        state.active_geo_aircraft["abc123"] = (track, cfg)
        state.adsb_aircraft["abc123"] = {
            "lat": self._TRUTH[0],
            "lon": self._TRUTH[1],
            "alt_baro": 33000,
            "gs": 400,
            "last_seen_ms": int(time.time() * 1000),
        }
        try:
            _refresh_node_verification(_PUB)
            yield orjson.loads(state.latest_node_verification_bytes[_PUB])
        finally:
            state.active_geo_aircraft.clear()
            state.adsb_aircraft.clear()
            state.latest_node_verification_bytes.pop(_PUB, None)

    def test_the_store_keeps_what_the_route_must_not_publish(self, stored):
        """Both halves of the assertion below: the fields have to be in the
        record for their absence from the response to mean anything, and the
        store is where an authenticated surface would still find them."""
        (entry,) = stored["tracks"]
        assert set(self._WITHHELD) <= set(entry)
        assert entry["solver_lat"] == self._SOLVED[0]

    def test_the_published_entry_is_the_stored_one_minus_those_fields(self, client, stored):
        """Exact equality against the real record, so it pins what stays as
        well as what goes."""
        (entry,) = stored["tracks"]
        body = client.get(f"/api/test/node/{_seed_ref(_PUB)}/verification")
        assert body.json()["tracks"] == [{k: v for k, v in entry.items() if k not in self._WITHHELD}]

    def test_no_true_frame_position_at_any_depth(self, client, stored):
        body = client.get(f"/api/test/node/{_seed_ref(_PUB)}/verification")
        for field in self._WITHHELD:
            assert not list(_keys_named(body.json(), field)), field


class TestMlatVerificationPayload:
    """/api/test/mlat-verification serves the refresh task's bytes unchanged, so
    what must not be published cannot be in them."""

    _A = "ret1a2b3c4d"

    def test_the_bistatic_angle_is_computed_and_not_published(self):
        """Both halves matter: the angle has to have been computed for the
        assertion to mean anything, since it is absent from a track that never
        had one."""
        from services.tasks.analytics_refresh import _refresh_mlat_verification

        now = time.time()
        state.connected_nodes[self._A] = {
            "status": "active",
            "config": {"node_id": self._A, "rx_lat": 34.0, "rx_lon": -82.0, "tx_lat": 35.0, "tx_lon": -83.0},
        }
        state.multinode_tracks["mn-dark-0123456789"] = {
            "lat": 34.5,
            "lon": -82.5,
            "alt_m": 10000.0,
            "vel_east": 200.0,
            "vel_north": 50.0,
            "n_nodes": 2,
            "rms_delay": 0.5,
            "rms_doppler": 5.0,
            "contributing_node_ids": [self._A],
            "timestamp_ms": int(now * 1000),
        }
        state.ground_truth_trails["abc123"] = [[34.5, -82.5, 10000.0, now - 5.0]]
        state.ground_truth_meta["abc123"] = {"speed_ms": 206.0, "object_type": "aircraft"}
        try:
            _refresh_mlat_verification()
            data = orjson.loads(state.latest_mlat_verification_bytes)
            assert data["n_matched"] == 1
            assert state.mlat_samples[-1]["max_bistatic_deg"] is not None
            assert "max_bistatic_angle_deg" not in data["tracks"][0]
            # A multinode position has no single receiver behind it, so it is
            # not node-scoped and keeps the solve the map draws its error line
            # from.  Withholding it here would be a different route's rule
            # applied to this one.
            assert data["tracks"][0]["solver_lat"] == 34.5
        finally:
            state.connected_nodes.pop(self._A, None)
            state.multinode_tracks.clear()
            state.ground_truth_trails.clear()
            state.ground_truth_meta.clear()
            state.mlat_samples.clear()
            state.latest_mlat_verification_bytes = b"{}"
