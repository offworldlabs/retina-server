"""HTTP endpoint tests for auth routes.

Covers:
  /api/auth/me, /api/auth/logout
  /api/auth/me/nodes
  /api/admin/node-owners     (GET)
  /api/admin/nodes/{id}/owner (PUT)
"""

import asyncio
import uuid
from http.cookies import SimpleCookie

import pytest

from tests.ownership_helpers import own, register_node_row

# ── /api/auth/me + /api/auth/logout ──────────────────────────────────────────


class TestMeEndpoint:
    def test_me_returns_anonymous_admin_in_test_mode(self, client):
        """AUTH_BYPASS=True (conftest opts in via AUTH_ALLOW_ANONYMOUS_ADMIN) → /me returns anonymous admin."""
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        body = r.json()
        assert body["role"] == "admin"
        assert body["auth_enabled"] is False

    def test_me_reports_the_access_identity_rather_than_the_anonymous_admin(self, client):
        """The dashboard asks /me who it is talking to, so this must agree with
        what require_admin would decide. Answering "Admin (no auth)" while the
        admin routes are attributing a real person is the inconsistency that
        makes the console show the wrong thing about its own session."""
        from unittest.mock import patch

        class Stub:
            def is_configured(self):
                return True

            async def identity(self, token):
                return "someone@offworldlab.com" if token else None

        with patch("core.users.access_identity", Stub()):
            r = client.get("/api/auth/me", headers={"Cf-Access-Jwt-Assertion": "a-token"})
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "someone@offworldlab.com"
        assert body["auth_enabled"] is True

    def test_me_says_whether_this_server_runs_a_fleet(self, client, monkeypatch):
        """The console shows the physics layer only where there is a fleet to draw."""
        monkeypatch.setenv("SYNTHETIC_FLEET_ENABLED", "1")
        assert client.get("/api/auth/me").json()["synthetic_fleet"] is True

    def test_me_says_there_is_no_fleet_when_the_flag_is_unset(self, client, monkeypatch):
        monkeypatch.delenv("SYNTHETIC_FLEET_ENABLED", raising=False)
        assert client.get("/api/auth/me").json()["synthetic_fleet"] is False

    def test_logout_returns_ok(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_logout_clears_auth_cookie(self, client):
        r = client.post("/api/auth/logout")
        assert "auth_token" in r.headers.get("set-cookie", "")


class TestLogoutEndsTheAccessSession:
    """Deleting auth_token does not end a session Access established.

    On the admin hostnames identity comes from the Cf-Access-Jwt-Assertion
    header, which Cloudflare re-injects from a cookie on its own domain that
    this app can neither read nor delete. Clearing only auth_token leaves the
    next request verified and admitted, so the caller has to be told to visit
    the edge's logout endpoint as well.
    """

    def _stub(self, email):
        class Stub:
            def is_configured(self):
                return True

            async def identity(self, token):
                return email if token else None

        return Stub()

    def test_an_access_session_is_told_to_visit_the_edge_logout(self, client):
        from unittest.mock import patch

        with patch("core.users.access_identity", self._stub("someone@offworldlab.com")):
            r = client.post("/api/auth/logout", headers={"Cf-Access-Jwt-Assertion": "a-token"})
        assert r.status_code == 200
        assert r.json()["redirect"] == "/cdn-cgi/access/logout"

    def test_an_access_session_still_has_its_auth_cookie_cleared(self, client):
        """Both credentials may be present, so ending one must not skip the other."""
        from unittest.mock import patch

        with patch("core.users.access_identity", self._stub("someone@offworldlab.com")):
            r = client.post("/api/auth/logout", headers={"Cf-Access-Jwt-Assertion": "a-token"})
        assert "auth_token" in r.headers.get("set-cookie", "")

    def test_a_request_without_an_assertion_is_not_redirected(self, client):
        """The dash case, and every environment where Access is not configured.

        One bundle serves admin and dash, and dash will authenticate by magic
        link, where the edge holds no session to end.
        """
        r = client.post("/api/auth/logout")
        assert "redirect" not in r.json()

    def test_a_service_token_is_not_redirected(self, client):
        """CI authenticates with a Cloudflare service token, which carries no
        email claim and so yields no identity. Redirecting it would send a
        non-interactive client to an interactive page."""
        from unittest.mock import patch

        with patch("core.users.access_identity", self._stub(None)):
            r = client.post("/api/auth/logout", headers={"Cf-Access-Jwt-Assertion": "a-token"})
        assert "redirect" not in r.json()


# ── /api/auth/me/nodes ────────────────────────────────────────────────────────


class TestMyNodes:
    def test_my_nodes_empty_when_no_ownership(self, client):
        r = client.get("/api/auth/me/nodes")
        assert r.status_code == 200
        assert r.json() == []

    def test_my_nodes_lists_owned_node(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        own("my-owned-node", ANONYMOUS_USER["id"])
        try:
            r = client.get("/api/auth/me/nodes")
            assert r.status_code == 200
            node_ids = [n["node_id"] for n in r.json()]
            assert "my-owned-node" in node_ids
        finally:
            asyncio.run(set_node_owner("my-owned-node", None))

    def test_my_nodes_entry_has_expected_fields(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        own("field-check-node", ANONYMOUS_USER["id"])
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == "field-check-node")
            for field in ("node_id", "node_ref", "name", "status", "is_synthetic", "position_status"):
                assert field in node, f"Missing field: {field}"
        finally:
            asyncio.run(set_node_owner("field-check-node", None))

    def test_an_owned_node_carries_the_ref_the_public_surfaces_key_on(self, client):
        """Authenticated and scoped to the owner, so node_id stays; the ref
        rides along so a consumer can join this list with a public one."""
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "ref-join-node"
        ref = own(node_id, ANONYMOUS_USER["id"])
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == node_id)
            assert node["node_ref"] == ref
        finally:
            asyncio.run(set_node_owner(node_id, None))

    @pytest.mark.parametrize("configured", [None, "ref-name-node"])
    def test_a_node_without_a_name_of_its_own_is_called_by_its_ref(self, configured, client):
        """An unnamed node, and one whose name is its own id, read as the ref:
        the console never shows the node_id."""
        from core import state
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "ref-name-node"
        ref = own(node_id, ANONYMOUS_USER["id"])
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {"status": "active", "config": {"name": configured}}
        try:
            node = next(n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == node_id)
            assert node["name"] == ref
        finally:
            with state.connected_nodes_lock:
                state.connected_nodes.pop(node_id, None)
            asyncio.run(set_node_owner(node_id, None))

    def test_a_node_keeps_the_name_it_was_given(self, client):
        from core import state
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "named-node"
        own(node_id, ANONYMOUS_USER["id"])
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {"status": "active", "config": {"name": "Roof dish"}}
        try:
            node = next(n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == node_id)
            assert node["name"] == "Roof dish"
        finally:
            with state.connected_nodes_lock:
                state.connected_nodes.pop(node_id, None)
            asyncio.run(set_node_owner(node_id, None))

    def test_my_nodes_entry_carries_position_status_for_a_private_node(self, client):
        """A private node is filtered out of /api/radar/nodes entirely, so
        its owner has nowhere else to learn it needs a position."""
        from core import state
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "position-status-node"
        own(node_id, ANONYMOUS_USER["id"])
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {
                "status": "active",
                "config": {"rx_lat": None, "rx_lon": None, "tx_lat": None, "tx_lon": None},
            }
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == node_id)
            assert node["position_status"] == "missing_both"
        finally:
            asyncio.run(set_node_owner(node_id, None))

    def test_a_node_that_is_not_a_polled_radar_has_none_in_its_row(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        own("fleet-row-node", ANONYMOUS_USER["id"])
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            assert next(n for n in nodes if n["node_id"] == "fleet-row-node")["polled"] is None
        finally:
            asyncio.run(set_node_owner("fleet-row-node", None))

    def test_my_nodes_reports_the_frequency_a_v1_config_declares(self, client, monkeypatch):
        """A v1 node or polled radar carries fc_hz, not the TCP fleet's FC."""
        from core import state
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "fc-hz-node"
        own(node_id, ANONYMOUS_USER["id"])
        monkeypatch.setitem(state.connected_nodes, node_id, {"status": "active", "config": {"fc_hz": 5.7e8}})
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == node_id)
            assert node["frequency"] == 5.7e8
        finally:
            asyncio.run(set_node_owner(node_id, None))


# ── /api/auth/me/nodes: location privacy ─────────────────────────────────────


class TestMyNodesCarriesLocationPrivacy:
    """The registration choice, on the listing the dashboard badges from. Only a
    registered node can be owned, so every node listed here made that choice."""

    @pytest.fixture()
    def owned(self):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "privacy-listing-node"
        own(node_id, ANONYMOUS_USER["id"])
        try:
            yield node_id
        finally:
            asyncio.run(set_node_owner(node_id, None))

    def _entry(self, client, node_id):
        return next(n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == node_id)

    def test_a_node_registered_public_reads_public(self, client, owned):
        assert self._entry(client, owned)["location_private"] is False

    def test_a_node_registered_private_reads_private(self, client, owned):
        asyncio.run(register_node_row(owned, "private"))
        assert self._entry(client, owned)["location_private"] is True


# ── /api/admin/node-owners + /api/admin/nodes/{id}/owner ─────────────────────


class TestAdminNodeOwnerRoutes:
    def test_list_node_owners_empty(self, client):
        r = client.get("/api/admin/node-owners")
        assert r.status_code == 200
        assert r.json() == {}

    def test_list_node_owners_shows_owned(self, client):
        from core.auth import set_node_owner

        own("admin-test-node", "some-user-id")
        try:
            owners = client.get("/api/admin/node-owners").json()
            assert "admin-test-node" in owners
            assert owners["admin-test-node"]["user_id"] == "some-user-id"
        finally:
            asyncio.run(set_node_owner("admin-test-node", None))

    def test_set_node_owner_null_clears_ownership(self, client):
        from core.auth import get_node_owner

        own("clear-me-node", "some-user-id")
        r = client.put("/api/admin/nodes/clear-me-node/owner", json={"user_id": None})
        assert r.status_code == 200
        assert r.json()["user_id"] is None
        assert asyncio.run(get_node_owner("clear-me-node")) is None

    def test_set_node_owner_invalid_uuid_returns_404(self, client):
        asyncio.run(register_node_row("some-node"))
        r = client.put("/api/admin/nodes/some-node/owner", json={"user_id": "not-a-uuid"})
        assert r.status_code == 404
        assert r.json()["detail"] == "User not found"

    def test_set_node_owner_nonexistent_user_returns_404(self, client):
        asyncio.run(register_node_row("some-node"))
        r = client.put(
            "/api/admin/nodes/some-node/owner",
            json={"user_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "User not found"

    def test_a_node_that_never_registered_cannot_be_given_an_owner(self, client):
        """Ownership lives beside the claim, and only a registered node can be
        claimed. Refused before anything is written, whichever way the call
        goes, rather than failing on the foreign key as a 500."""
        from core.auth import get_node_owner
        from core.users import ANONYMOUS_USER

        assign = client.put("/api/admin/nodes/never-registered/owner", json={"user_id": ANONYMOUS_USER["id"]})
        clear = client.put("/api/admin/nodes/never-registered/owner", json={"user_id": None})

        assert (assign.status_code, assign.json()["detail"]) == (404, "Node not found")
        assert (clear.status_code, clear.json()["detail"]) == (404, "Node not found")
        assert asyncio.run(get_node_owner("never-registered")) is None


# ── /api/auth/claim/* and the owner's release ────────────────────────────────


class TestClaimRoutes:
    """The four routes the click and the dashboard reach.

    The behaviour behind them is pinned in test_claim_routes.py. What is here is
    what only the routes decide: the status codes, and the session cookie a
    successful click opens.
    """

    @staticmethod
    def _mailed(node_id="ret1a2b3c4d", node_ref="nde1a2b3c4d00", email="ada@example.com"):
        """A live claim link for a registered node, and the token that was mailed."""
        import time

        from core.nodes import Node
        from core.users import async_session_maker
        from services import claim_links
        from services.node_claim_store import put_challenge, set_claim_address

        async def _seed():
            challenge = await claim_links.issue(email=email, node_id=node_id, now=time.time())
            async with async_session_maker() as session:
                async with session.begin():
                    if await session.get(Node, node_id) is None:
                        session.add(Node(node_id=node_id, node_ref=node_ref, board_model="raspberrypi5-4gb"))
                    await set_claim_address(session, node_id, email)
                    await put_challenge(session, node_id, email, challenge.handle, challenge.expires_at)
            return challenge.token

        token = asyncio.run(_seed())
        return token

    def test_the_preview_names_the_node_without_spending_the_link(self, client):
        token = self._mailed()

        first = client.get(f"/api/auth/claim/{token}")
        second = client.get(f"/api/auth/claim/{token}")

        assert first.status_code == 200
        assert first.json() == {"node_ref": "nde1a2b3c4d00"}
        assert second.status_code == 200

    def test_the_preview_of_an_unknown_link_is_404(self, client):
        assert client.get("/api/auth/claim/not-a-token").status_code == 404

    def test_a_click_binds_and_opens_a_session(self, client):
        token = self._mailed()

        r = client.post("/api/auth/claim/consume", json={"token": token})

        assert r.status_code == 200
        assert r.json()["node_ref"] == "nde1a2b3c4d00"
        assert r.json()["user"]["email"] == "ada@example.com"
        assert "auth_token" in SimpleCookie(r.headers.get("set-cookie", ""))

    def test_the_signed_in_user_carries_the_fleet_flag(self, client, monkeypatch):
        """ClaimPage adopts this user without asking /me."""
        monkeypatch.setenv("SYNTHETIC_FLEET_ENABLED", "1")
        r = client.post("/api/auth/claim/consume", json={"token": self._mailed()})
        assert r.json()["user"]["synthetic_fleet"] is True

    def test_a_spent_link_is_400_rather_than_saying_which_of_three_it_was(self, client):
        token = self._mailed()
        client.post("/api/auth/claim/consume", json={"token": token})

        r = client.post("/api/auth/claim/consume", json={"token": token})

        assert r.status_code == 400

    def test_an_unknown_link_answers_exactly_as_a_spent_one(self, client):
        assert client.post("/api/auth/claim/consume", json={"token": "not-a-token"}).status_code == 400

    def test_a_link_for_a_node_claimed_meanwhile_is_409(self, client):
        from core.auth import set_node_owner

        token = self._mailed()
        asyncio.run(set_node_owner("ret1a2b3c4d", "11111111-1111-1111-1111-111111111111"))

        r = client.post("/api/auth/claim/consume", json={"token": token})

        assert r.status_code == 409

    def test_a_decline_answers_ok_and_then_stops_working(self, client):
        token = self._mailed()

        assert client.post("/api/auth/claim/decline", json={"token": token}).status_code == 200
        assert client.post("/api/auth/claim/decline", json={"token": token}).status_code == 400

    def test_an_admin_clearing_an_owner_also_clears_the_claim(self, client):
        """Clearing an owner is a release performed on their behalf, so it
        clears what a release clears: the next owner must not see the last
        one's address, and a link already in a mailbox must not rebind a node
        an administrator has just freed."""
        from core.users import async_session_maker
        from services.node_claim_store import read_challenge, read_claim

        token = self._mailed()
        client.post("/api/auth/claim/consume", json={"token": token})

        r = client.put("/api/admin/nodes/ret1a2b3c4d/owner", json={"user_id": None})

        assert r.status_code == 200

        async def _read():
            async with async_session_maker() as session:
                return await read_claim(session, "ret1a2b3c4d"), await read_challenge(session, "ret1a2b3c4d")

        claim, challenge = asyncio.run(_read())
        assert claim is None
        assert challenge is None

    @staticmethod
    def _claim_on_file(node_id="ret1a2b3c4d"):
        from core.users import async_session_maker
        from services.node_claim_store import read_claim

        async def _read():
            async with async_session_maker() as session:
                return await read_claim(session, node_id)

        claim = asyncio.run(_read())
        return claim

    def test_an_admin_reassigning_a_node_clears_the_claim_that_bound_it(self, client):
        """A reassignment is a release and a new owner at once, so the new owner
        must not be shown the address that claimed the node for somebody else.
        The row stays, since it now carries the owner, and carries nothing else."""
        from core.users import get_or_create_magic_link_user

        token = self._mailed()
        client.post("/api/auth/claim/consume", json={"token": token})
        bob = asyncio.run(get_or_create_magic_link_user("bob@example.com"))

        r = client.put("/api/admin/nodes/ret1a2b3c4d/owner", json={"user_id": str(bob.id)})

        assert r.status_code == 200
        claim = self._claim_on_file()
        assert (claim.user_id, claim.email, claim.verified, claim.undeliverable) == (str(bob.id), None, False, False)

    def test_an_admin_reassigning_a_node_to_its_own_owner_keeps_the_claim(self, client):
        token = self._mailed()
        ada_id = client.post("/api/auth/claim/consume", json={"token": token}).json()["user"]["id"]

        r = client.put("/api/admin/nodes/ret1a2b3c4d/owner", json={"user_id": ada_id})

        assert r.status_code == 200
        assert self._claim_on_file().email == "ada@example.com"

    @staticmethod
    def _owned_by_the_caller(verified: bool, node_id="ret1a2b3c4d"):
        """The node bound to the test client's account, beside the address that
        was offered for it, either confirmed or not. Written as the click writes
        it, on the claim row: an administrator's assignment would clear the
        address instead."""
        from core.users import ANONYMOUS_USER, async_session_maker
        from services.node_claim_store import mark_verified, read_claim

        async def _own():
            async with async_session_maker() as session:
                async with session.begin():
                    (await read_claim(session, node_id)).user_id = ANONYMOUS_USER["id"]
                    if verified:
                        await mark_verified(session, node_id, "ada@example.com")

        asyncio.run(_own())

    def test_the_owner_list_names_the_address_a_node_was_claimed_with(self, client):
        self._mailed()
        self._owned_by_the_caller(verified=True)

        [node] = [n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == "ret1a2b3c4d"]

        assert node["claimed_with"] == "ada@example.com"

    def test_an_address_nobody_confirmed_is_not_what_a_node_was_claimed_with(self, client):
        """An owner beside an address that was offered and never confirmed,
        which a nomination racing the click that binds the node can leave: that
        address is a stranger's as far as this owner is concerned."""
        self._mailed()
        self._owned_by_the_caller(verified=False)

        [node] = [n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == "ret1a2b3c4d"]

        assert node["claimed_with"] is None

    def test_releasing_a_node_the_caller_does_not_own_is_404(self, client):
        """The same answer a node that does not exist gets: an id that resolves
        is already a hint about where a receiver is."""
        assert client.delete("/api/auth/me/nodes/ret1a2b3c4d/claim").status_code == 404
