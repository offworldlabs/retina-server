"""What the unauthenticated node routes leave out.

/api/radar/nodes stops publishing ``peer``, which for a node on the TCP protocol
is the address its connection came from.  /api/custody/status leaves a private
node out of every block, and the chain and verify routes answer a private node's
ref exactly as they answer a ref nothing publishes as.  The association overlaps
and status payloads leave a private node out of every pair, list and count.

Each block pins the public node beside the private one, so an absence cannot
pass by the payload being empty.  Everything goes through the routes, on a
TestClient without the lifespan: the lifespan starts the refresh loop, which
would rewrite the cached payloads these tests build.
"""

import asyncio
import hashlib

import orjson
import pytest
from fastapi.testclient import TestClient
from retina_analytics.association import OverlapZone
from retina_custody.canonical import canonicalize
from retina_custody.models import NodeIdentity

from core import state
from core.nodes import Node
from core.users import async_session_maker
from main import app
from services import node_auth, node_refs, publication
from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

# Real-node ids: none of the synthetic prefixes, which publish under their own
# ids, and not the polled prefix, which probation answers for.
_PRIV = "retprv00001"
_PUB = "retpub00001"
_PUB2 = "retpub00002"

# str() of a peername tuple, which is how services/tcp_handler.py stores it.
# 192.0.2.0/24 is the documentation range.
_TCP_HOST = "192.0.2.10"
_TCP_PEER = f"('{_TCP_HOST}', 54321)"

_PRIV_FINGERPRINT = "a1b2c3d4e5f60718"
_PRIV_SERIAL = "SER-PRV-0001"

_MINTED: dict[str, str] = {}


def _ref(node_id: str) -> str:
    """The ref a seeded row carries, minted the way registration mints one."""
    return _MINTED.setdefault(node_id, node_auth.mint_node_ref())


# Minted and never seeded, so nothing publishes as it.
_UNKNOWN_REF = node_auth.mint_node_ref()


@pytest.fixture()
def seed():
    """seed(node_id="private"|"public", …): write registry rows and drop both caches."""

    def _seed(**choices: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, choice in choices.items():
                    session.add(Node(node_id=nid, node_ref=_ref(nid), publication=choice))
                await session.commit()

        asyncio.run(_go())
        publication._reset_for_tests()
        node_refs._reset_for_tests()

    return _seed


@pytest.fixture()
def api():
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


def _body(response) -> dict:
    return orjson.loads(response.content)


# ── /api/radar/nodes ─────────────────────────────────────────────────────────


class TestNodesListingPeer:
    @pytest.fixture()
    def listing(self, api, seed):
        seed(**{_PUB: "public"})
        state.connected_nodes[_PUB] = {
            "status": "active",
            "peer": _TCP_PEER,
            "is_synthetic": False,
            "config": {"node_id": _PUB, "rx_lat": 34.0, "rx_lon": -82.0, "rx_alt_ft": 100.0},
        }
        _refresh_analytics_and_nodes()
        return api.get("/api/radar/nodes")

    def test_the_node_is_still_listed(self, listing):
        assert listing.status_code == 200
        assert _ref(_PUB) in _body(listing)["nodes"]

    def test_its_entry_has_no_peer(self, listing):
        assert "peer" not in _body(listing)["nodes"][_ref(_PUB)]

    def test_the_address_appears_nowhere_in_the_body(self, listing):
        assert _TCP_HOST.encode() not in listing.content


# ── /api/custody ─────────────────────────────────────────────────────────────


def _store_custody(node_id: str, *, fingerprint: str, serial: str, with_identity: bool = True) -> None:
    """A registration, one chain entry and one IQ commitment for `node_id`.

    The entry hashes to its own entry_hash, so a node with a registered
    identity verifies to a 200 rather than to a parse failure.
    """
    if with_identity:
        state.node_identities[node_id] = NodeIdentity(
            node_id=node_id,
            public_key_pem="",
            public_key_fingerprint=fingerprint,
            serial_number=serial,
            signing_mode="software",
            registered_at="2026-10-01T00:00:00Z",
        )
    entry = {
        "node_id": node_id,
        "hour_utc": "2026-10-01T00:00:00Z",
        "prev_hash": "genesis",
        "detections_hash": "d" * 64,
        "n_detections": 1,
        "node_config_hash": "c" * 64,
        "firmware_version": "1.0",
        "timestamp_utc": "2026-10-01T00:00:00Z",
    }
    entry_hash = hashlib.sha256(canonicalize(entry)).hexdigest()
    state.chain_entries[node_id] = [
        {**entry, "entry_hash": entry_hash, "signature": "", "signing_mode": "software", "_verified": True}
    ]
    state.iq_commitments[node_id] = [{"node_id": node_id, "capture_id": f"cap-{node_id}"}]


@pytest.fixture()
def custody(seed):
    seed(**{_PRIV: "private", _PUB: "public"})
    _store_custody(_PRIV, fingerprint=_PRIV_FINGERPRINT, serial=_PRIV_SERIAL)
    _store_custody(_PUB, fingerprint="0f1e2d3c4b5a6978", serial="SER-PUB-0001")


class TestCustodyStatus:
    @pytest.fixture()
    def status(self, api, custody):
        return api.get("/api/custody/status")

    def test_a_public_node_is_still_in_every_block(self, status):
        assert status.status_code == 200
        body = _body(status)
        for block in ("node_keys", "chain_entries", "iq_commitments"):
            assert _ref(_PUB) in body[block], block

    def test_a_private_node_is_in_no_block(self, status):
        body = _body(status)
        for block in ("node_keys", "chain_entries", "iq_commitments"):
            assert list(body[block]) == [_ref(_PUB)], block

    def test_the_count_leaves_it_out_too(self, status):
        assert _body(status)["registered_nodes"] == 1

    def test_nothing_identifying_it_reaches_the_body(self, status):
        for value in (_PRIV, _ref(_PRIV), _PRIV_FINGERPRINT, _PRIV_SERIAL):
            assert value.encode() not in status.content, value


class TestCustodyPathParameters:
    @pytest.mark.parametrize("route", ["chain", "verify"])
    def test_a_public_node_still_answers(self, api, custody, route):
        response = api.get(f"/api/custody/{route}/{_ref(_PUB)}")
        assert response.status_code == 200
        assert _body(response)["node_ref"] == _ref(_PUB)

    @pytest.mark.parametrize("route", ["chain", "verify"])
    def test_a_private_node_answers_as_an_unknown_ref(self, api, custody, route):
        private = api.get(f"/api/custody/{route}/{_ref(_PRIV)}")
        unknown = api.get(f"/api/custody/{route}/{_UNKNOWN_REF}")
        assert unknown.status_code == 404
        assert private.status_code == unknown.status_code
        assert private.content == unknown.content
        assert private.headers["content-type"] == unknown.headers["content-type"]

    def test_a_private_node_without_a_key_answers_as_an_unknown_ref(self, api, seed):
        """Without a registered key, verify answers a found chain with 400.

        The private check comes first, so that status does not tell a private
        node with a chain apart from a ref nothing publishes as.
        """
        seed(**{_PRIV: "private"})
        _store_custody(_PRIV, fingerprint=_PRIV_FINGERPRINT, serial=_PRIV_SERIAL, with_identity=False)
        private = api.get(f"/api/custody/verify/{_ref(_PRIV)}")
        unknown = api.get(f"/api/custody/verify/{_UNKNOWN_REF}")
        assert (private.status_code, private.content) == (unknown.status_code, unknown.content)


# ── /api/radar/association ───────────────────────────────────────────────────


def _zone(node_a: str, node_b: str, points: int) -> OverlapZone:
    return OverlapZone(node_a_id=node_a, node_b_id=node_b, grid_points=[(34.0, -82.0, 1.0)] * points)


@pytest.fixture()
def associator(seed):
    """Two public nodes and a private one, each paired with the other two.

    Written into the live associator's stores rather than registered: a real
    registration builds the overlap grids from geometry, and these tests are
    about which zones are published, not which ones overlap.
    """
    seed(**{_PRIV: "private", _PUB: "public", _PUB2: "public"})
    assoc = state.node_associator
    for nid in (_PUB, _PRIV, _PUB2):
        assoc.node_geometries[nid] = object()
    assoc.overlap_zones[(_PUB, _PUB2)] = _zone(_PUB, _PUB2, 5)
    assoc.overlap_zones[(_PUB, _PRIV)] = _zone(_PUB, _PRIV, 7)
    assoc.overlap_zones[(_PRIV, _PUB2)] = _zone(_PRIV, _PUB2, 11)
    assoc._pending_tracks[_PUB] = [object(), object()]
    assoc._pending_tracks[_PRIV] = [object()]
    assoc._pending_tracks[_PUB2] = [object()]
    return assoc


def _pairs(body: dict) -> list[tuple[str, str]]:
    return [(z["node_a"], z["node_b"]) for z in body["overlaps"]]


class TestAssociationOverlaps:
    @pytest.fixture()
    def overlaps(self, api, associator):
        _refresh_analytics_and_nodes()
        return api.get("/api/radar/association/overlaps")

    def test_the_public_pair_and_nodes_are_still_listed(self, overlaps):
        assert overlaps.status_code == 200
        body = _body(overlaps)
        assert (_ref(_PUB), _ref(_PUB2)) in _pairs(body)
        assert {_ref(_PUB), _ref(_PUB2)} <= set(body["registered_nodes"])

    def test_no_zone_names_a_private_node(self, overlaps):
        assert _pairs(_body(overlaps)) == [(_ref(_PUB), _ref(_PUB2))]

    def test_a_private_node_is_not_registered(self, overlaps):
        assert _ref(_PRIV) not in _body(overlaps)["registered_nodes"]

    def test_nothing_names_it_anywhere_in_the_body(self, overlaps):
        for value in (_PRIV, _ref(_PRIV)):
            assert value.encode() not in overlaps.content, value


class TestAssociationStatus:
    @pytest.fixture()
    def status(self, api, associator):
        return api.get("/api/radar/association/status")

    def test_the_public_nodes_and_pair_are_still_reported(self, status):
        assert status.status_code == 200
        body = _body(status)
        assert body["pending_tracks"][_ref(_PUB)] == 2
        assert body["pending_tracks"][_ref(_PUB2)] == 1
        assert (_ref(_PUB), _ref(_PUB2)) in _pairs(body)

    def test_a_private_node_has_no_pending_tracks_entry(self, status):
        assert set(_body(status)["pending_tracks"]) == {_ref(_PUB), _ref(_PUB2)}

    def test_no_zone_names_a_private_node(self, status):
        assert _pairs(_body(status)) == [(_ref(_PUB), _ref(_PUB2))]

    def test_the_counts_leave_it_out(self, status):
        body = _body(status)
        assert body["registered_nodes"] == 2
        assert body["overlap_zones"] == 1

    def test_nothing_names_it_anywhere_in_the_body(self, status):
        for value in (_PRIV, _ref(_PRIV)):
            assert value.encode() not in status.content, value


# ── Before the policy has been read ──────────────────────────────────────────


class TestBeforeThePolicyIsKnown:
    """No private set has been read yet, so none of these can tell which
    nodes to leave out.  The listings answer the retryable 503 the other
    policy-dependent routes do; a path parameter answers as an unknown ref."""

    @pytest.fixture(autouse=True)
    def unavailable(self, seed, monkeypatch):
        seed(**{_PUB: "public"})
        _store_custody(_PUB, fingerprint="0f1e2d3c4b5a6978", serial="SER-PUB-0001")
        state.node_associator._pending_tracks[_PUB] = [object()]

        def fail():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(publication, "_query", fail)

    @pytest.mark.parametrize("path", ["/api/custody/status", "/api/radar/association/status"])
    def test_a_listing_is_a_retryable_503(self, api, path):
        response = api.get(path)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        assert _ref(_PUB).encode() not in response.content

    @pytest.mark.parametrize("route", ["chain", "verify"])
    def test_a_public_nodes_ref_answers_as_an_unknown_one(self, api, route):
        public = api.get(f"/api/custody/{route}/{_ref(_PUB)}")
        unknown = api.get(f"/api/custody/{route}/{_UNKNOWN_REF}")
        assert (public.status_code, public.content) == (unknown.status_code, unknown.content)
