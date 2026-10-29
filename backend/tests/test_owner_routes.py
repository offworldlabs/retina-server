"""The owner routes under /api/auth/me: the node list, one node, its edits,
its release and the owner feed.

Signed in for real throughout (tests/owner_helpers.py): under the suite's
anonymous-admin bypass neither a 401 nor somebody else's 404 can be seen.
"""

import asyncio
from datetime import UTC, datetime

import numpy as np
import orjson
import pytest
from sqlalchemy import select, update

from core import state
from core.nodes import Node, NodeConfig, NodeEvent, NodeLabel
from core.users import async_session_maker
from services import node_refs, owner_nodes, public_location, publication
from services.node_config import canonical_config
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import get_node_owner, own, register_node_row, set_node_owner

MINE = "ret0a1b2c3d"
THEIRS = "ret9f8e7d6c"
UNKNOWN = "ret0123abcd"

# The true geometry and radio of MINE, as registration stores it.
CONFIG = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

# What a primed v1 node carries live (services/node_pipeline.py, _WIRE_FIELDS):
# no callsign, CPI or tolerances.
WIRE = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft", "fc_hz", "fs_hz")
WIRE += ("beam_width_deg", "beam_azimuth_deg", "max_range_km")

OWNER_NODE_KEYS = {
    "node_id",
    "node_ref",
    "name",
    "label",
    "status",
    "last_heartbeat",
    "config_hash",
    "is_synthetic",
    "rx_lat",
    "rx_lon",
    "position_status",
    "frequency",
    "sample_rate",
    "location",
    "rf",
    "location_private",
    "claimed_with",
    "polled",
}
LOCATION_KEYS = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft")
RF_KEYS = (
    "tx_callsign",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
)


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


@pytest.fixture
def fuzz_on(monkeypatch):
    """Public coordinates displaced, as every deployment runs: the owner's view
    must be the truth regardless."""
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    public_location._reset_for_tests()
    assert public_location.fuzz_enabled()


@pytest.fixture
def ada(client, real):
    """The owner: (token, user id)."""
    token = sign_in(client, "ada@example.com")
    return token, user_id_of("ada@example.com")


@pytest.fixture
def bob(client, real):
    """Somebody else, who owns THEIRS: (token, user id)."""
    token = sign_in(client, "bob@example.com")
    return token, user_id_of("bob@example.com")


@pytest.fixture
def mine(client, ada, bob):
    """MINE registered public with CONFIG stored and owned by ada, THEIRS owned
    by bob, and ada signed in on the client."""
    own(MINE, ada[1])
    own(THEIRS, bob[1])
    _store_config(MINE)
    node_refs._reset_for_tests()
    as_user(client, ada[0])
    return MINE


def _store_config(node_id: str, version: int = 1, superseded: bool = False, **values) -> None:
    async def _go():
        async with async_session_maker() as session:
            async with session.begin():
                session.add(
                    NodeConfig(
                        node_id=node_id,
                        version=version,
                        superseded_at=datetime.now(UTC) if superseded else None,
                        **{**CONFIG, **values},
                    )
                )

    asyncio.run(_go())


def _connect(monkeypatch, node_id: str, config: dict, **entry) -> None:
    monkeypatch.setitem(
        state.connected_nodes,
        node_id,
        {
            "status": "active",
            "config": config,
            "config_hash": "0123456789abcdef",
            "last_heartbeat": "2026-10-12T09:00:00+00:00",
            "is_synthetic": False,
            **entry,
        },
    )


def _events(node_id: str = MINE) -> list[tuple]:
    async def _go():
        async with async_session_maker() as session:
            rows = await session.scalars(select(NodeEvent).where(NodeEvent.node_id == node_id).order_by(NodeEvent.id))
            return [(e.kind, e.actor, e.epoch) for e in rows]

    return asyncio.run(_go())


def _label_rows() -> list[tuple]:
    async def _go():
        async with async_session_maker() as session:
            rows = await session.scalars(select(NodeLabel).order_by(NodeLabel.node_id))
            return [(r.node_id, r.user_id, r.label) for r in rows]

    return asyncio.run(_go())


def _node_row(node_id: str = MINE) -> Node:
    async def _go():
        async with async_session_maker() as session:
            return await session.get(Node, node_id)

    return asyncio.run(_go())


def _listed(client, node_id: str = MINE) -> dict:
    return next(n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == node_id)


def _patch(client, body, node_id: str = MINE):
    return client.patch(f"/api/auth/me/nodes/{node_id}", json=body)


# ── The move out of routes/auth.py ───────────────────────────────────────────


class TestTheMove:
    @pytest.mark.parametrize(
        "name", ["my_nodes", "my_aircraft", "release_my_node", "_owned_node", "_remove_polled_radar"]
    )
    def test_auth_keeps_none_of_the_moved_code(self, name):
        import routes.auth

        assert not hasattr(routes.auth, name)

    @pytest.mark.parametrize(
        "method, path",
        [
            ("GET", "/api/auth/me/nodes"),
            ("GET", "/api/auth/me/nodes/{node_id}"),
            ("PATCH", "/api/auth/me/nodes/{node_id}"),
            ("DELETE", "/api/auth/me/nodes/{node_id}/claim"),
            ("GET", "/api/auth/me/aircraft"),
            ("GET", "/api/auth/me/analytics"),
            ("GET", "/api/auth/me/nodes/{node_id}/analytics"),
            ("GET", "/api/auth/me/nodes/{node_id}/report"),
            ("GET", "/api/auth/me/nodes/{node_id}/config-history"),
            ("GET", "/api/auth/me/nodes/{node_id}/events"),
            ("GET", "/api/auth/me/nodes/{node_id}/snr"),
            ("GET", "/api/auth/me/archive"),
            ("GET", "/api/auth/me/archive/{key:path}"),
        ],
    )
    def test_each_owner_route_is_served_once_from_the_owner_module(self, method, path):
        from main import app
        from tests.route_helpers import as_served

        served = [
            r
            for r in as_served(app.routes)
            if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ())
        ]
        assert len(served) == 1
        assert served[0].endpoint.__module__ == "routes.owner"
        assert served[0].tags == ["auth"]


# ── OwnerNode ────────────────────────────────────────────────────────────────


class TestOwnerNode:
    def test_a_listed_node_carries_every_key_of_the_contract(self, client, mine):
        node = _listed(client)
        assert set(node) == OWNER_NODE_KEYS
        assert set(node["location"]) == set(LOCATION_KEYS)
        assert set(node["rf"]) == set(RF_KEYS)

    def test_one_node_answers_as_the_listing_does(self, client, mine, monkeypatch):
        _connect(monkeypatch, MINE, canonical_config({k: CONFIG[k] for k in WIRE}))
        r = client.get(f"/api/auth/me/nodes/{MINE}")
        assert r.status_code == 200
        assert r.json() == _listed(client)

    def test_a_private_node_is_shown_where_it_is_with_the_fuzz_on(self, client, mine, fuzz_on, monkeypatch):
        asyncio.run(register_node_row(MINE, "private"))
        _connect(monkeypatch, MINE, canonical_config({k: CONFIG[k] for k in WIRE}))
        # The control: a public surface would have moved this receiver.
        assert public_location.public_latlon(CONFIG["rx_lat"], CONFIG["rx_lon"], MINE) != (
            CONFIG["rx_lat"],
            CONFIG["rx_lon"],
        )

        for node in (_listed(client), client.get(f"/api/auth/me/nodes/{MINE}").json()):
            assert node["location_private"] is True
            assert node["status"] == "active"
            assert (node["rx_lat"], node["rx_lon"]) == (CONFIG["rx_lat"], CONFIG["rx_lon"])
            assert node["location"] == {key: CONFIG[key] for key in LOCATION_KEYS}
            assert node["rf"] == {key: CONFIG[key] for key in RF_KEYS}
            assert (node["frequency"], node["sample_rate"]) == (CONFIG["fc_hz"], CONFIG["fs_hz"])
            assert node["position_status"] == "positioned"

    def test_a_node_that_is_not_connected_shows_its_active_stored_version(self, client, mine, fuzz_on):
        """After a restart, say, or for a node that is retired. Version 1 is
        superseded and placed elsewhere, so reading it would show."""
        asyncio.run(register_node_row(MINE, "private"))
        asyncio.run(_supersede(MINE))
        _store_config(MINE, version=2, rx_lat=51.5, tx_callsign="Wrotham")

        node = client.get(f"/api/auth/me/nodes/{MINE}").json()

        assert MINE not in state.connected_nodes
        assert node["status"] == "never_connected"
        assert (node["last_heartbeat"], node["config_hash"]) == (None, None)
        assert node["location"] == {**{key: CONFIG[key] for key in LOCATION_KEYS}, "rx_lat": 51.5}
        assert node["rx_lat"] == 51.5
        assert node["rf"] == {**{key: CONFIG[key] for key in RF_KEYS}, "tx_callsign": "Wrotham"}
        assert (node["frequency"], node["sample_rate"]) == (CONFIG["fc_hz"], CONFIG["fs_hz"])
        assert node["position_status"] == "positioned"
        assert node == _listed(client)

    def test_a_connected_node_shows_its_live_config_over_the_stored_one(self, client, mine, monkeypatch):
        """What the pipeline runs on wins; what a primed node does not carry live
        comes from its stored version."""
        _connect(monkeypatch, MINE, canonical_config({**{k: CONFIG[k] for k in WIRE}, "rx_lat": 51.6, "fc_hz": 4.9e8}))

        node = client.get(f"/api/auth/me/nodes/{MINE}").json()

        assert node["location"]["rx_lat"] == node["rx_lat"] == 51.6
        assert node["frequency"] == 4.9e8
        assert node["rf"]["tx_callsign"] == "Crystal Palace"
        assert node["rf"]["cpi_s"] == 0.5
        assert node["rf"]["doppler_tolerance_hz"] == 5.0

    def test_a_tcp_node_with_nothing_stored_reads_its_own_keys(self, client, mine, monkeypatch):
        """The TCP fleet spells the carrier FC and the sample rate Fs, and
        declares none of the RF fields, which are then unknown."""
        own(THEIRS, user_id_of("ada@example.com"))
        _connect(monkeypatch, THEIRS, canonical_config({"rx_lat": 40.0, "rx_lon": -3.0, "FC": 2.0e8, "Fs": 1.0e6}))

        node = client.get(f"/api/auth/me/nodes/{THEIRS}").json()

        assert (node["frequency"], node["sample_rate"]) == (2.0e8, 1.0e6)
        assert node["rf"] == dict.fromkeys(RF_KEYS)
        assert node["location"] == {**dict.fromkeys(LOCATION_KEYS), "rx_lat": 40.0, "rx_lon": -3.0}
        assert node["position_status"] == "missing_tx"

    def test_numpy_and_non_finite_values_answer_as_plain_json(self, client, mine, monkeypatch):
        """The framework's encoder refuses a numpy integer, and NaN is not JSON."""
        config = canonical_config({k: CONFIG[k] for k in WIRE})
        config.update(
            fc_hz=np.int64(570_000_000),
            fs_hz=np.float32(2_000_000.0),
            max_range_km=np.int64(50),
            beam_width_deg=float("nan"),
            cpi_s=np.float64("inf"),
            delay_tolerance_us="6.67",
            doppler_tolerance_hz="fast",
        )
        _connect(monkeypatch, MINE, config)

        for r in (client.get("/api/auth/me/nodes"), client.get(f"/api/auth/me/nodes/{MINE}")):
            assert r.status_code == 200
            assert orjson.loads(r.content) == r.json()
        node = client.get(f"/api/auth/me/nodes/{MINE}").json()
        assert (node["frequency"], node["sample_rate"]) == (570_000_000, 2_000_000.0)
        assert node["rf"]["max_range_km"] == 50
        assert (node["rf"]["beam_width_deg"], node["rf"]["cpi_s"]) == (None, None)
        assert node["rf"]["delay_tolerance_us"] == 6.67
        assert node["rf"]["doppler_tolerance_hz"] is None

    def test_an_unnamed_node_is_called_by_its_ref_and_a_named_one_by_its_name(self, client, mine, monkeypatch):
        ref = _node_row().node_ref
        assert _listed(client)["name"] == ref
        _connect(monkeypatch, MINE, {"name": "Roof dish"})
        assert _listed(client)["name"] == "Roof dish"
        assert _listed(client)["label"] is None


async def _supersede(node_id: str) -> None:
    """Retire the fixture's version 1, moved elsewhere so reading it would show."""
    async with async_session_maker() as session:
        async with session.begin():
            await session.execute(
                update(NodeConfig)
                .where(NodeConfig.node_id == node_id, NodeConfig.version == 1)
                .values(superseded_at=datetime.now(UTC), rx_lat=10.0)
            )


# ── Who may ask ──────────────────────────────────────────────────────────────

ONE_NODE_ROUTES = [
    ("GET", "/api/auth/me/nodes/{}", None),
    ("PATCH", "/api/auth/me/nodes/{}", {"name": "Shed", "location_private": True}),
    ("DELETE", "/api/auth/me/nodes/{}/claim", None),
]


class TestWhoMayAsk:
    @pytest.mark.parametrize("method, path, body", ONE_NODE_ROUTES)
    def test_a_signed_out_caller_is_401(self, client, mine, method, path, body):
        as_user(client, None)
        r = client.request(method, path.format(MINE), json=body)
        assert (r.status_code, r.json()) == (401, {"detail": "Not authenticated"})

    @pytest.mark.parametrize("path", ["/api/auth/me/nodes", "/api/auth/me/aircraft"])
    def test_the_lists_refuse_a_signed_out_caller(self, client, mine, path):
        as_user(client, None)
        r = client.get(path)
        assert (r.status_code, r.json()) == (401, {"detail": "Not authenticated"})

    @pytest.mark.parametrize("method, path, body", ONE_NODE_ROUTES)
    def test_somebody_elses_node_answers_exactly_as_an_unknown_one(self, client, mine, bob, method, path, body):
        as_user(client, bob[0])

        theirs = client.request(method, path.format(MINE), json=body)
        unknown = client.request(method, path.format(UNKNOWN), json=body)

        assert theirs.status_code == unknown.status_code == 404
        assert theirs.content == unknown.content == b'{"detail":"Node not found"}'
        assert theirs.headers["content-type"] == unknown.headers["content-type"]
        # And nothing happened to it.
        assert asyncio.run(get_node_owner(MINE)) == user_id_of("ada@example.com")
        assert (_label_rows(), _events(), _node_row().publication) == ([], [], "public")

    def test_a_bad_body_for_somebody_elses_node_still_says_only_not_found(self, client, mine, bob):
        """Ownership is decided before the body is read, so a refused body
        cannot tell an owned id from an unknown one either."""
        as_user(client, bob[0])
        theirs = _patch(client, {"name": 5, "colour": "red"})
        unknown = _patch(client, {"name": 5, "colour": "red"}, node_id=UNKNOWN)
        assert theirs.status_code == 404
        assert theirs.content == unknown.content

    def test_the_owner_is_answered(self, client, mine):
        assert client.get(f"/api/auth/me/nodes/{MINE}").status_code == 200
        assert [n["node_id"] for n in client.get("/api/auth/me/nodes").json()] == [MINE]


# ── PATCH ────────────────────────────────────────────────────────────────────

REFUSED_BODIES = [
    pytest.param({}, "Nothing to change.", id="empty"),
    pytest.param({"nickname": "Shed"}, None, id="unknown-key"),
    pytest.param({"name": "Shed", "colour": "red"}, None, id="unknown-key-beside-a-known-one"),
    pytest.param({"name": "x" * 65}, "A name is at most 64 characters.", id="65-chars"),
    pytest.param({"name": "Roof\x00dish"}, "A name cannot contain control characters.", id="nul"),
    pytest.param({"name": "Roof\ndish"}, "A name cannot contain control characters.", id="newline"),
    pytest.param({"name": "Roof\x7f"}, "A name cannot contain control characters.", id="del"),
    pytest.param({"name": "Roof\x85dish"}, "A name cannot contain control characters.", id="c1"),
    pytest.param({"name": "Roof\u2028dish"}, "A name cannot contain control characters.", id="line-separator"),
    pytest.param({"name": 5}, None, id="name-number"),
    pytest.param({"name": True}, None, id="name-bool"),
    pytest.param({"name": ["Shed"]}, None, id="name-list"),
    pytest.param({"location_private": "true"}, None, id="private-string"),
    pytest.param({"location_private": 1}, None, id="private-number"),
    pytest.param({"location_private": None}, None, id="private-null"),
    pytest.param(["Shed"], None, id="body-list"),
    pytest.param("Shed", None, id="body-string"),
]


class TestEditRefusals:
    @pytest.mark.parametrize("body, reason", REFUSED_BODIES)
    def test_is_422_and_changes_nothing(self, client, mine, body, reason):
        r = _patch(client, body)

        assert r.status_code == 422
        if reason is not None:
            assert r.json() == {"detail": reason}
        assert (_label_rows(), _events(), _node_row().publication) == ([], [], "public")

    @pytest.mark.parametrize("raw", [b"null", b"", b"{", b'{"name": "Roof\\ud800"}'])
    def test_a_body_that_is_not_an_object_of_text_is_422(self, client, mine, raw):
        r = client.patch(f"/api/auth/me/nodes/{MINE}", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 422
        assert _label_rows() == []

    @pytest.mark.parametrize("spelt", ["own", "owned-by-another", "connected-unregistered", "padded"])
    def test_a_name_that_names_a_node_is_422(self, client, mine, monkeypatch, spelt):
        stray = "ret0badcafe"
        _connect(monkeypatch, stray, {})
        name = {"own": MINE, "owned-by-another": THEIRS, "connected-unregistered": stray, "padded": f"  {MINE} "}[spelt]

        r = _patch(client, {"name": name})

        assert (r.status_code, r.json()) == (422, {"detail": "A name cannot be a node id."})
        assert _label_rows() == []

    @pytest.mark.parametrize("which", ["connected", "registered", "registered-and-connected"])
    def test_a_name_that_is_a_synthetic_nodes_id_is_422(self, client, mine, monkeypatch, which):
        """A synthetic node publishes under its own id, which is why public_name
        lets a node-supplied name equal to one through. A label is held to every
        node id, synthetic ones included."""
        synthetic = "synth-roof-07"
        if which != "registered":
            _connect(monkeypatch, synthetic, {}, is_synthetic=True)
        if which != "connected":
            asyncio.run(register_node_row(synthetic))

        r = _patch(client, {"name": synthetic})

        assert (r.status_code, r.json()) == (422, {"detail": "A name cannot be a node id."})
        assert (_label_rows(), _events()) == ([], [])

    def test_a_registered_node_that_is_not_connected_is_422(self, client, mine):
        """Registered after the registry cache was last read, and connected
        nowhere: only the table knows it."""
        node_refs.ref_for(MINE)
        offline = "ret2b3c4d5e"
        asyncio.run(register_node_row(offline))

        r = _patch(client, {"name": offline})

        assert (r.status_code, r.json()) == (422, {"detail": "A name cannot be a node id."})
        assert _label_rows() == []

    @pytest.mark.parametrize(
        "spelt",
        [
            pytest.param(MINE.upper(), id="capitals"),
            pytest.param("".join(chr(ord(c) + 0xFEE0) for c in MINE), id="full-width"),
            pytest.param("SYNTH-ROOF-07", id="synthetic-in-capitals"),
        ],
    )
    def test_a_node_id_in_another_case_or_form_is_422(self, client, mine, monkeypatch, spelt):
        _connect(monkeypatch, "synth-roof-07", {}, is_synthetic=True)

        r = _patch(client, {"name": spelt})

        assert (r.status_code, r.json()) == (422, {"detail": "A name cannot be a node id."})
        assert _label_rows() == []

    @pytest.mark.parametrize("name", ["synth-roof-08", "test-rig", "ret0a1b2c3"])
    def test_a_name_that_only_looks_like_an_id_is_a_name(self, client, mine, monkeypatch, name):
        """The rule is about the nodes there are, not the shape of their ids: a
        synthetic prefix on no node, or an id one digit short, names nothing."""
        _connect(monkeypatch, "synth-roof-07", {}, is_synthetic=True)

        r = _patch(client, {"name": name})

        assert r.status_code == 200
        assert r.json()["label"] == name

    def test_a_refused_name_beside_a_good_choice_changes_neither(self, client, mine):
        assert _patch(client, {"name": "x" * 65, "location_private": True}).status_code == 422
        assert (_events(), _node_row().publication) == ([], "public")


class TestEditLabel:
    def test_the_answer_is_the_node_as_it_now_is(self, client, mine):
        r = _patch(client, {"name": "Shed"})

        assert r.status_code == 200
        assert r.json()["label"] == r.json()["name"] == "Shed"
        assert r.json() == client.get(f"/api/auth/me/nodes/{MINE}").json() == _listed(client)

    def test_the_label_is_shown_before_the_name_the_node_gives(self, client, mine, monkeypatch):
        _connect(monkeypatch, MINE, {"name": "Roof dish"})
        _patch(client, {"name": "Shed"})
        assert (_listed(client)["name"], _listed(client)["label"]) == ("Shed", "Shed")

    def test_surrounding_whitespace_is_trimmed(self, client, mine):
        assert _patch(client, {"name": "  Roof dish \t"}).json()["label"] == "Roof dish"
        assert _label_rows() == [(MINE, user_id_of("ada@example.com"), "Roof dish")]

    def test_sixty_four_characters_are_accepted_once_trimmed(self, client, mine):
        assert _patch(client, {"name": " " + "x" * 64 + " "}).json()["label"] == "x" * 64

    def test_text_beyond_ascii_is_a_name(self, client, mine):
        name = "Dach über der Garage \U0001f4e1"
        assert _patch(client, {"name": name}).json()["label"] == name

    @pytest.mark.parametrize("cleared", ["", None, "   "])
    def test_empty_and_null_clear_the_label(self, client, mine, cleared):
        _patch(client, {"name": "Shed"})

        r = _patch(client, {"name": cleared})

        assert r.status_code == 200
        assert r.json()["label"] is None
        assert r.json()["name"] == _node_row().node_ref
        assert _label_rows() == []

    def test_a_label_is_never_the_name_a_node_supplies(self, client, mine, monkeypatch):
        """Clearing the label shows the node's own name again, untouched."""
        _connect(monkeypatch, MINE, {"name": "Roof dish"})
        _patch(client, {"name": "Shed"})
        _patch(client, {"name": None})
        assert _listed(client)["name"] == "Roof dish"
        assert state.connected_nodes[MINE]["config"] == {"name": "Roof dish"}

    def test_an_owner_with_no_account_row_can_label_their_node(self, client, real):
        """An administrator's Access identity can own a node and has no row in
        `user`, which is why a label holds its owner as node_claims does, with
        no foreign key to `user` to refuse it."""
        import uuid
        from unittest.mock import patch

        class Access:
            def is_configured(self):
                return True

            async def identity(self, token):
                return "operator@example.com" if token else None

        operator = str(uuid.uuid5(uuid.NAMESPACE_URL, "mailto:operator@example.com"))
        own(MINE, operator)
        as_user(client, None)
        with patch("core.users.access_identity", Access()):
            r = client.patch(
                f"/api/auth/me/nodes/{MINE}", json={"name": "Shed"}, headers={"Cf-Access-Jwt-Assertion": "a-token"}
            )

        assert (r.status_code, r.json()["label"]) == (200, "Shed")
        assert _events() == [("renamed", operator, None)]


class TestEditEvents:
    def test_a_rename_is_one_event_by_the_owner(self, client, mine, ada):
        _patch(client, {"name": "Shed"})
        assert _events() == [("renamed", ada[1], None)]

    def test_clearing_is_a_rename_too(self, client, mine, ada):
        _patch(client, {"name": "Shed"})
        _patch(client, {"name": ""})
        assert _events() == [("renamed", ada[1], None), ("renamed", ada[1], None)]

    def test_a_name_and_a_choice_together_are_two_events(self, client, mine, ada):
        r = _patch(client, {"name": "Shed", "location_private": True})
        assert r.status_code == 200
        assert _events() == [("renamed", ada[1], None), ("made_private", ada[1], None)]

    def test_making_a_node_public_again_has_its_own_kind(self, client, mine, ada):
        asyncio.run(register_node_row(MINE, "private"))
        _patch(client, {"location_private": False})
        assert _events() == [("made_public", ada[1], None)]

    @pytest.mark.parametrize(
        "again",
        [
            {"name": "Shed"},
            {"name": "  Shed "},
            {"location_private": False},
            {"name": "Shed", "location_private": False},
        ],
    )
    def test_a_request_that_changes_nothing_records_nothing(self, client, mine, ada, again):
        _patch(client, {"name": "Shed"})

        r = _patch(client, again)

        assert r.status_code == 200
        assert (r.json()["label"], r.json()["location_private"]) == ("Shed", False)
        assert _events() == [("renamed", ada[1], None)]

    @pytest.mark.parametrize("nothing", [{"name": None}, {"name": ""}, {"location_private": False}])
    def test_clearing_what_is_not_there_records_nothing(self, client, mine, nothing):
        assert _patch(client, nothing).status_code == 200
        assert _events() == []


class TestEditPublication:
    @pytest.fixture
    def published(self, client, mine):
        """MINE in the analytics manager, so the public per-node route, which
        filters on every request, has something to serve."""
        state.node_analytics.register_node(
            MINE, {"rx_lat": CONFIG["rx_lat"], "rx_lon": CONFIG["rx_lon"], "max_range_km": 50}
        )
        try:
            yield _node_row().node_ref
        finally:
            state.node_analytics.retire_node(MINE)

    def test_a_node_made_private_leaves_a_per_request_route_at_once(self, client, published, ada):
        as_user(client, None)
        assert client.get(f"/api/radar/analytics/{published}").status_code == 200
        # Cached as public, for the full 30 s TTL.
        assert publication.is_private(MINE) is False

        as_user(client, ada[0])
        before = datetime.now(UTC)
        r = _patch(client, {"location_private": True})
        after = datetime.now(UTC)

        assert (r.status_code, r.json()["location_private"]) == (200, True)
        assert publication.is_private(MINE) is True
        as_user(client, None)
        assert client.get(f"/api/radar/analytics/{published}").status_code == 404
        row = _node_row()
        assert (row.publication, row.publication_version) == ("private", None)
        assert before <= row.publication_chosen_at <= after

    def test_a_node_made_public_again_returns_at_once(self, client, published):
        asyncio.run(register_node_row(MINE, "private"))
        # As registration does after its commit.
        publication.invalidate()
        assert publication.is_private(MINE) is True

        _patch(client, {"location_private": False})

        assert publication.is_private(MINE) is False
        as_user(client, None)
        assert client.get(f"/api/radar/analytics/{published}").status_code == 200

    def test_a_choice_that_changes_nothing_keeps_when_it_was_made(self, client, mine):
        _patch(client, {"location_private": True})
        chosen = _node_row().publication_chosen_at
        _patch(client, {"location_private": True})
        assert _node_row().publication_chosen_at == chosen


class TestReregistration:
    """A reflash registers again and carries the node's own publication record,
    which replaces whatever was chosen before it, a console choice included,
    as it replaces any earlier registration's (routes/node_register.py)."""

    @pytest.fixture
    def limiter(self):
        from services.node_rate_limits import registration_limiter

        registration_limiter.reset()
        yield
        registration_limiter.reset()

    def test_the_nodes_own_record_replaces_the_owners_choice(
        self, client, mine, ada, accepted_in_mender, alerts, limiter
    ):
        _patch(client, {"name": "Shed", "location_private": True})
        assert publication.is_private(MINE) is True
        accepted_in_mender(MINE)
        record = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
        body = {
            "node_id": MINE,
            "board_model": "raspberrypi5-4gb",
            "agreements": {
                "licence": record,
                "remote_management": record,
                "publication": record | {"choice": "public"},
            },
            "config": CONFIG,
        }

        assert client.post("/v1/nodes/register", json=body).status_code == 200

        row = _node_row()
        assert (row.publication, row.publication_version) == ("public", "2026-07-01")
        assert row.publication_chosen_at == datetime(2026, 7, 31, 9, 12, tzinfo=UTC)
        assert publication.is_private(MINE) is False
        node = client.get(f"/api/auth/me/nodes/{MINE}").json()
        # The owner keeps the node and the label: a registration touches neither.
        assert (node["location_private"], node["label"]) == (False, "Shed")
        assert [event[0] for event in _events()] == ["renamed", "made_private"]


class TestLabelLifetime:
    def test_a_new_owner_does_not_see_the_last_ones_label(self, client, mine, bob):
        _patch(client, {"name": "Ada's shed"})
        assert client.delete(f"/api/auth/me/nodes/{MINE}/claim").json() == {"ok": True}
        own(MINE, bob[1])

        as_user(client, bob[0])
        node = client.get(f"/api/auth/me/nodes/{MINE}").json()

        assert (node["label"], node["name"]) == (None, _node_row().node_ref)
        assert _listed(client, MINE)["label"] is None

    def test_a_new_owner_by_a_claim_link_does_not_see_it_either(self, client, mine):
        """The release and the claim the console drives: ada hands the node
        back, and carol claims it by the link mailed to her."""
        import time

        from services import claim_links
        from services.node_claim_store import put_challenge, set_claim_address

        _patch(client, {"name": "Ada's shed"})
        assert client.delete(f"/api/auth/me/nodes/{MINE}/claim").status_code == 200

        async def _mail_carol():
            challenge = await claim_links.issue(email="carol@example.com", node_id=MINE, now=time.time())
            async with async_session_maker() as session:
                async with session.begin():
                    await set_claim_address(session, MINE, "carol@example.com")
                    await put_challenge(session, MINE, "carol@example.com", challenge.handle, challenge.expires_at)
            return challenge.token

        clicked = client.post("/api/auth/claim/consume", json={"token": asyncio.run(_mail_carol())})
        assert clicked.status_code == 200
        as_user(client, clicked.cookies["auth_token"])

        node = client.get(f"/api/auth/me/nodes/{MINE}").json()

        assert asyncio.run(get_node_owner(MINE)) == user_id_of("carol@example.com")
        assert (node["label"], node["name"]) == (None, _node_row().node_ref)

    def test_an_administrators_reassignment_drops_it_too(self, client, mine, bob):
        _patch(client, {"name": "Ada's shed"})
        asyncio.run(set_node_owner(MINE, bob[1]))

        as_user(client, bob[0])
        assert client.get(f"/api/auth/me/nodes/{MINE}").json()["label"] is None

    def test_the_new_owners_label_replaces_the_row(self, client, mine, bob):
        _patch(client, {"name": "Shed"})
        asyncio.run(set_node_owner(MINE, bob[1]))
        as_user(client, bob[0])

        r = _patch(client, {"name": "Shed"})

        assert r.json()["label"] == "Shed"
        assert _label_rows() == [(MINE, bob[1], "Shed")]
        assert [event[:2] for event in _events()] == [("renamed", user_id_of("ada@example.com")), ("renamed", bob[1])]

    def test_a_new_owner_clearing_leaves_the_last_ones_unseen_row(self, client, mine, bob):
        _patch(client, {"name": "Shed"})
        asyncio.run(set_node_owner(MINE, bob[1]))
        as_user(client, bob[0])

        _patch(client, {"name": None})

        assert _label_rows() == [(MINE, user_id_of("ada@example.com"), "Shed")]
        assert len(_events()) == 1

    def test_the_owner_who_set_it_sees_it_again_on_taking_the_node_back(self, client, mine, ada):
        _patch(client, {"name": "Shed"})
        client.delete(f"/api/auth/me/nodes/{MINE}/claim")
        own(MINE, ada[1])
        assert client.get(f"/api/auth/me/nodes/{MINE}").json()["label"] == "Shed"

    def test_an_edit_by_someone_who_no_longer_owns_the_node_writes_nothing(self, client, mine, bob):
        """The race the route's check cannot close: ownership moves between the
        check and the write. Every write carries the check itself."""
        _patch(client, {"name": "Shed"})

        asyncio.run(owner_nodes.edit(MINE, bob[1], rename=True, label="Bob's", private=True))
        asyncio.run(owner_nodes.edit(MINE, bob[1], rename=True, label=None))

        assert _label_rows() == [(MINE, user_id_of("ada@example.com"), "Shed")]
        assert _node_row().publication == "public"
        assert len(_events()) == 1


class TestLabelStaysOwnerOnly:
    def test_no_public_payload_carries_it(self, client, mine, fuzz_on, monkeypatch):
        from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

        label = "Ada's secret shed label"
        _connect(monkeypatch, MINE, {**canonical_config({k: CONFIG[k] for k in WIRE}), "name": "Roof dish"})
        state.node_analytics.register_node(
            MINE, {"rx_lat": CONFIG["rx_lat"], "rx_lon": CONFIG["rx_lon"], "max_range_km": 50}
        )
        try:
            assert _patch(client, {"name": label}).json()["label"] == label
            _refresh_analytics_and_nodes()
            ref = _node_row().node_ref

            for caller in (None, client.cookies.get("auth_token")):
                as_user(client, caller)
                nodes = client.get("/api/radar/nodes")
                assert nodes.status_code == 200
                assert nodes.json()["nodes"][ref]["name"] == "Roof dish"
                for path in ("/api/radar/nodes", "/api/radar/analytics", f"/api/radar/analytics/{ref}"):
                    r = client.get(path)
                    assert r.status_code == 200, path
                    assert label not in r.text, path
        finally:
            state.node_analytics.retire_node(MINE)


# ── The moved routes, as they were ───────────────────────────────────────────


class TestRelease:
    def test_the_owner_releases_their_node(self, client, mine):
        assert client.delete(f"/api/auth/me/nodes/{MINE}/claim").json() == {"ok": True}
        assert asyncio.run(get_node_owner(MINE)) is None
        assert client.get("/api/auth/me/nodes").json() == []

    def test_a_released_node_is_not_found_the_second_time(self, client, mine):
        client.delete(f"/api/auth/me/nodes/{MINE}/claim")
        r = client.delete(f"/api/auth/me/nodes/{MINE}/claim")
        assert (r.status_code, r.json()) == (404, {"detail": "Node not found"})


class TestMyAircraft:
    def test_serves_the_callers_tracks(self, client, mine, monkeypatch):
        monkeypatch.setattr(
            state,
            "latest_aircraft_json",
            {
                "now": 1.0,
                "aircraft": [
                    {"hex": "aa0001", "node_id": MINE, "multinode": False},
                    {"hex": "aa0002", "node_id": THEIRS, "multinode": False},
                ],
            },
        )
        body = client.get("/api/auth/me/aircraft").json()
        assert [ac["hex"] for ac in body["aircraft"]] == ["aa0001"]
        assert MINE not in client.get("/api/auth/me/aircraft").text
