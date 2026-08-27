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
from core.nodes import Node  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from main import app  # noqa: E402
from services import publication  # noqa: E402
from services.publication import (  # noqa: E402
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
