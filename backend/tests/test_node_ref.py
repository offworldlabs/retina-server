"""Tests for services/node_ref.py — the public handle for every node.

The property under test is that a public payload never carries a node id, for
registered and unregistered nodes alike.  A ref that is derived for one node
and looked up for another must be indistinguishable in form, or the shape of
the string says which nodes are on the registry.
"""

import asyncio
import os
import re

import pytest

os.environ.setdefault("RETINA_ENV", "test")

from core.nodes import Node  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from services import node_ref, public_location  # noqa: E402
from services.node_ref import public_node_ref  # noqa: E402

_SALT = "test-salt-for-node-ref"
_ID = "radar3-retnode"

# What mint_node_ref produces: "nde" and twelve base36 characters.
_REF_RE = re.compile(r"^nde[0-9a-z]{12}$")


@pytest.fixture(autouse=True)
def _fixed_salt(monkeypatch):
    """A salt fixed here, so a failure never depends on the runtime salt file."""
    monkeypatch.setenv("NODE_FUZZ_SALT", _SALT)
    public_location._reset_for_tests()
    node_ref._reset_for_tests()
    yield
    public_location._reset_for_tests()
    node_ref._reset_for_tests()


@pytest.fixture()
def seed_node():
    """seed_node(node_id, node_ref) — write a registry row and drop the cache."""

    def _seed(node_id: str, ref: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                session.add(Node(node_id=node_id, node_ref=ref))
                await session.commit()

        asyncio.run(_go())
        # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
        # restores one for the same reason.
        asyncio.set_event_loop(asyncio.new_event_loop())
        node_ref._reset_for_tests()

    return _seed


class TestDerivedRef:
    """The unregistered case, which on this deployment is every live node."""

    def test_it_has_the_shape_of_a_minted_ref(self):
        assert _REF_RE.match(public_node_ref(_ID))

    def test_it_is_stable_across_calls(self):
        first = public_node_ref(_ID)
        node_ref._reset_for_tests()  # not merely reading the memo back
        assert public_node_ref(_ID) == first

    def test_different_nodes_get_different_refs(self):
        assert public_node_ref(_ID) != public_node_ref("radar3a-retnode")

    def test_the_salt_moves_every_ref(self, monkeypatch):
        """Rotating the fuzz salt re-anonymises nodes, as it does positions."""
        before = public_node_ref(_ID)
        monkeypatch.setattr(public_location, "_salt", lambda: "a-different-salt")
        node_ref._reset_for_tests()
        assert public_node_ref(_ID) != before

    def test_it_is_never_the_node_id(self):
        for node_id in (_ID, "ret7e2ca6f6", "", "nde000000000000"):
            assert public_node_ref(node_id) != node_id

    def test_a_missing_id_does_not_pass_through(self):
        """A config fault must not publish whatever was in the field."""
        assert _REF_RE.match(public_node_ref(""))

    def test_the_domain_prefix_separates_it_from_the_location_hmac(self):
        """Both frames hash a node id under the fuzz salt.

        Without the "node_ref|" domain the two messages would be the same
        string, and a published ref would be a published sample of the digest
        the location offset is drawn from.
        """
        import hashlib
        import hmac

        undomained = hmac.new(_SALT.encode(), _ID.encode(), hashlib.sha256).digest()
        assert hmac.new(_SALT.encode(), f"node_ref|{_ID}".encode(), hashlib.sha256).digest() != undomained
        # And no node id can spell another frame's message: the derivation is
        # keyed on a prefix a node id cannot start with.
        assert public_node_ref(_ID) != public_node_ref(f"node_ref|{_ID}")


class TestRegisteredRef:
    def test_the_stored_ref_wins_over_derivation(self, seed_node):
        derived = public_node_ref(_ID)
        seed_node(_ID, "nde0123456789ab")
        assert public_node_ref(_ID) == "nde0123456789ab"
        assert public_node_ref(_ID) != derived

    def test_an_unregistered_node_still_derives(self, seed_node):
        seed_node(_ID, "nde0123456789ab")
        other = public_node_ref("ret7e2ca6f6")
        assert _REF_RE.match(other)
        assert other != "nde0123456789ab"


class TestPerNodeAnalyticsRoute:
    """GET /api/radar/analytics/{node_id} is built fresh, not from the cache.

    The cached listing's coverage is in test_analytics_refresh.py; this is the
    other surface, which has to be wired separately or the two disagree.
    """

    def test_the_route_carries_the_ref(self):
        from fastapi.testclient import TestClient

        from core import state
        from main import app

        state.node_analytics.register_node(_ID, {"rx_lat": 34.85, "rx_lon": -82.40, "max_range_km": 50})
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                body = client.get(f"/api/radar/analytics/{_ID}").json()
            assert body["node_ref"] == public_node_ref(_ID)
            assert body["node_ref"] != _ID
        finally:
            state.node_analytics.retire_node(_ID)
