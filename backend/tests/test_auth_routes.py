"""HTTP endpoint tests for auth routes.

Covers:
  /api/auth/me, /api/auth/logout
  /api/auth/me/claim-codes  (GET / POST / DELETE)
  /api/auth/me/nodes
  /api/auth/me/nodes/{id}/location-privacy (PUT / DELETE)
  /api/admin/invites         (GET / POST / DELETE)
  /api/admin/node-owners     (GET)
  /api/admin/nodes/{id}/owner (PUT)
"""

import asyncio
import uuid
from http.cookies import SimpleCookie

import pytest

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


# ── /api/auth/me/claim-codes ─────────────────────────────────────────────────


class TestClaimCodeRoutes:
    def test_create_claim_code_returns_code(self, client):
        r = client.post("/api/auth/me/claim-codes")
        assert r.status_code == 200
        body = r.json()
        assert "code" in body
        assert len(body["code"]) == 12
        assert body["code"] == body["code"].upper()
        assert body["used_at"] is None

    def test_list_claim_codes_includes_created(self, client):
        client.post("/api/auth/me/claim-codes")
        r = client.get("/api/auth/me/claim-codes")
        assert r.status_code == 200
        codes = r.json()
        assert isinstance(codes, list)
        assert len(codes) == 1

    def test_list_claim_codes_sorted_newest_first(self, client):
        client.post("/api/auth/me/claim-codes")
        client.post("/api/auth/me/claim-codes")
        r = client.get("/api/auth/me/claim-codes")
        codes = r.json()
        timestamps = [c.get("created_at", 0) for c in codes]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_revoke_claim_code(self, client):
        code = client.post("/api/auth/me/claim-codes").json()["code"]
        r = client.delete(f"/api/auth/me/claim-codes/{code}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_revoke_removes_code_from_list(self, client):
        code = client.post("/api/auth/me/claim-codes").json()["code"]
        client.delete(f"/api/auth/me/claim-codes/{code}")
        codes = client.get("/api/auth/me/claim-codes").json()
        assert not any(c["code"] == code for c in codes)

    def test_revoke_nonexistent_code_returns_404(self, client):
        r = client.delete("/api/auth/me/claim-codes/DOESNOTEXIST")
        assert r.status_code == 404

    def test_revoke_used_code_returns_404(self, client):
        from core.auth import consume_claim_code

        code = client.post("/api/auth/me/claim-codes").json()["code"]
        asyncio.run(consume_claim_code(code, "some-node"))
        r = client.delete(f"/api/auth/me/claim-codes/{code}")
        assert r.status_code == 404

    def test_claim_code_cap_returns_429(self, client):
        from core.auth import _MAX_ACTIVE_CLAIM_CODES_PER_USER

        for _ in range(_MAX_ACTIVE_CLAIM_CODES_PER_USER):
            client.post("/api/auth/me/claim-codes")
        r = client.post("/api/auth/me/claim-codes")
        assert r.status_code == 429


# ── /api/auth/me/nodes ────────────────────────────────────────────────────────


class TestMyNodes:
    def test_my_nodes_empty_when_no_ownership(self, client):
        r = client.get("/api/auth/me/nodes")
        assert r.status_code == 200
        assert r.json() == []

    def test_my_nodes_lists_owned_node(self, client):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        asyncio.run(set_node_owner("my-owned-node", ANONYMOUS_USER["id"]))
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

        asyncio.run(set_node_owner("field-check-node", ANONYMOUS_USER["id"]))
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

        node_id = "test-ref-join-node"  # synthetic: publishes under its own id
        asyncio.run(set_node_owner(node_id, ANONYMOUS_USER["id"]))
        try:
            nodes = client.get("/api/auth/me/nodes").json()
            node = next(n for n in nodes if n["node_id"] == node_id)
            assert node["node_ref"] == node_id
        finally:
            asyncio.run(set_node_owner(node_id, None))

    def test_my_nodes_entry_carries_position_status_for_a_private_node(self, client):
        """A private node is filtered out of /api/radar/nodes entirely, so
        its owner has nowhere else to learn it needs a position."""
        from core import state
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "position-status-node"
        asyncio.run(set_node_owner(node_id, ANONYMOUS_USER["id"]))
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


# ── /api/auth/me/nodes/{id}/location-privacy ─────────────────────────────────


class TestMyNodeLocationPrivacy:
    """The owner's half of the location-privacy switch.

    The suite runs with core.users' anonymous-admin bypass opted in, so the
    caller is that admin and "owning" a node is a node_owners row against its
    all-zero uuid.  These go through the HTTP routes rather than the storage
    helpers — tests/test_publication.py owns the precedence rule; what is
    asserted here is the ownership gate, the shape on the wire, and that the
    cache is dropped so a change is visible immediately.
    """

    NODE = "privacy-route-node"

    @pytest.fixture()
    def owned(self):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        asyncio.run(set_node_owner(self.NODE, ANONYMOUS_USER["id"]))
        try:
            yield self.NODE
        finally:
            asyncio.run(set_node_owner(self.NODE, None))

    @staticmethod
    def _register(node_id, choice):
        from core.nodes import Node
        from core.users import async_session_maker
        from services import publication

        async def _go():
            async with async_session_maker() as session:
                session.add(Node(node_id=node_id, node_ref=f"nde-{node_id}"[:15], publication=choice))
                await session.commit()

        asyncio.run(_go())
        # Startup has already primed the cache; mirror the registration route's
        # invalidation after this fixture writes directly to the database.
        publication.invalidate()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_setting_privacy_on_a_node_you_do_not_own_is_404(self, client):
        """404 rather than 403: the two answers differ only in confirming the id
        exists, and node ids are guessable."""
        r = client.put("/api/auth/me/nodes/not-mine/location-privacy", json={"private": True})
        assert r.status_code == 404
        assert r.json()["detail"] == "Node not found"

    def test_clearing_privacy_on_a_node_you_do_not_own_is_404(self, client):
        r = client.delete("/api/auth/me/nodes/not-mine/location-privacy")
        assert r.status_code == 404
        assert r.json()["detail"] == "Node not found"

    def test_setting_private_returns_the_override_state(self, client, owned):
        r = client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": True})
        assert r.status_code == 200
        assert r.json() == {
            "node_id": owned,
            "location_private": True,
            "location_privacy_source": "override",
        }

    def test_setting_private_takes_effect_without_waiting_for_the_ttl(self, client, owned):
        """The route calls publication.invalidate() after its commit, so the
        next 1 Hz flush honours the change rather than one up to 30 s later."""
        from services.publication import is_private

        assert not is_private(owned)
        client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": True})
        assert is_private(owned)

    def test_setting_public_overrides_a_private_registration(self, client, owned):
        from services.publication import is_private

        self._register(owned, "private")
        assert is_private(owned)
        r = client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": False})
        assert r.json()["location_private"] is False
        assert not is_private(owned)

    def test_clearing_returns_the_node_to_its_registration_choice(self, client, owned):
        from services.publication import is_private

        self._register(owned, "private")
        client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": False})
        assert not is_private(owned)

        r = client.delete(f"/api/auth/me/nodes/{owned}/location-privacy")
        assert r.status_code == 200
        assert r.json() == {
            "node_id": owned,
            "location_private": True,
            "location_privacy_source": "registration",
        }
        assert is_private(owned)

    def test_clearing_a_node_that_never_registered_falls_back_to_the_default(self, client, owned):
        client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": True})
        r = client.delete(f"/api/auth/me/nodes/{owned}/location-privacy")
        assert r.json() == {
            "node_id": owned,
            "location_private": False,
            "location_privacy_source": "default",
        }

    def test_a_body_without_private_is_rejected(self, client, owned):
        assert client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={}).status_code == 422


class TestMyNodesCarriesLocationPrivacy:
    """All three sources, on the listing the dashboard renders the card from."""

    @pytest.fixture()
    def owned(self):
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER

        node_id = "privacy-listing-node"
        asyncio.run(set_node_owner(node_id, ANONYMOUS_USER["id"]))
        try:
            yield node_id
        finally:
            asyncio.run(set_node_owner(node_id, None))

    def _entry(self, client, node_id):
        return next(n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == node_id)

    def test_a_node_with_neither_row_reads_public_by_default(self, client, owned):
        entry = self._entry(client, owned)
        assert entry["location_private"] is False
        assert entry["location_privacy_source"] == "default"

    def test_a_registered_node_names_its_registration(self, client, owned):
        TestMyNodeLocationPrivacy._register(owned, "private")
        entry = self._entry(client, owned)
        assert entry["location_private"] is True
        assert entry["location_privacy_source"] == "registration"

    def test_an_override_outranks_it_and_says_so(self, client, owned):
        TestMyNodeLocationPrivacy._register(owned, "private")
        client.put(f"/api/auth/me/nodes/{owned}/location-privacy", json={"private": False})
        entry = self._entry(client, owned)
        assert entry["location_private"] is False
        assert entry["location_privacy_source"] == "override"


# ── /api/admin/invites ────────────────────────────────────────────────────────


class TestAdminInviteRoutes:
    def test_list_invites_empty(self, client):
        r = client.get("/api/admin/invites")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_invite(self, client):
        r = client.post("/api/admin/invites", json={"email": "alice@example.com", "role": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "alice@example.com"
        assert body["role"] == "user"
        assert body["used_at"] is None

    def test_create_invite_appears_in_list(self, client):
        client.post("/api/admin/invites", json={"email": "bob@example.com", "role": "user"})
        invites = client.get("/api/admin/invites").json()
        assert any(i["email"] == "bob@example.com" for i in invites)

    def test_create_invite_invalid_email_returns_400(self, client):
        r = client.post("/api/admin/invites", json={"email": "not-an-email", "role": "user"})
        assert r.status_code == 400

    def test_create_invite_invalid_role_returns_400(self, client):
        r = client.post("/api/admin/invites", json={"email": "c@example.com", "role": "owner"})
        assert r.status_code == 400

    def test_revoke_invite(self, client):
        token = client.post("/api/admin/invites", json={"email": "d@example.com", "role": "user"}).json()["token"]
        r = client.delete(f"/api/admin/invites/{token}")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_revoke_invite_removes_from_list(self, client):
        token = client.post("/api/admin/invites", json={"email": "e@example.com", "role": "user"}).json()["token"]
        client.delete(f"/api/admin/invites/{token}")
        invites = client.get("/api/admin/invites").json()
        assert not any(i["token"] == token for i in invites)

    def test_revoke_nonexistent_invite_returns_404(self, client):
        r = client.delete("/api/admin/invites/does-not-exist")
        assert r.status_code == 404


# ── /api/admin/node-owners + /api/admin/nodes/{id}/owner ─────────────────────


class TestAdminNodeOwnerRoutes:
    def test_list_node_owners_empty(self, client):
        r = client.get("/api/admin/node-owners")
        assert r.status_code == 200
        assert r.json() == {}

    def test_list_node_owners_shows_owned(self, client):
        from core.auth import set_node_owner

        asyncio.run(set_node_owner("admin-test-node", "some-user-id"))
        try:
            owners = client.get("/api/admin/node-owners").json()
            assert "admin-test-node" in owners
            assert owners["admin-test-node"]["user_id"] == "some-user-id"
        finally:
            asyncio.run(set_node_owner("admin-test-node", None))

    def test_set_node_owner_null_clears_ownership(self, client):
        from core.auth import get_node_owner, set_node_owner

        asyncio.run(set_node_owner("clear-me-node", "some-user-id"))
        r = client.put("/api/admin/nodes/clear-me-node/owner", json={"user_id": None})
        assert r.status_code == 200
        assert r.json()["user_id"] is None
        assert asyncio.run(get_node_owner("clear-me-node")) is None

    def test_set_node_owner_invalid_uuid_returns_404(self, client):
        r = client.put("/api/admin/nodes/some-node/owner", json={"user_id": "not-a-uuid"})
        assert r.status_code == 404

    def test_set_node_owner_nonexistent_user_returns_404(self, client):

        r = client.put(
            "/api/admin/nodes/some-node/owner",
            json={"user_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404


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

        challenge = claim_links.issue(intent=claim_links.INTENT_CLAIM, node_id=node_id, now=time.time())

        async def _seed():
            async with async_session_maker() as session:
                async with session.begin():
                    if await session.get(Node, node_id) is None:
                        session.add(Node(node_id=node_id, node_ref=node_ref, board_model="raspberrypi5-4gb"))
                    await set_claim_address(session, node_id, email)
                    await put_challenge(session, node_id, email, challenge.handle, challenge.expires_at)

        asyncio.run(_seed())
        asyncio.set_event_loop(asyncio.new_event_loop())
        return challenge.token

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
        asyncio.set_event_loop(asyncio.new_event_loop())

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
        asyncio.set_event_loop(asyncio.new_event_loop())
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
        asyncio.set_event_loop(asyncio.new_event_loop())
        return claim

    def test_an_admin_reassigning_a_node_clears_the_claim_that_bound_it(self, client):
        """A reassignment is a release and a new owner at once, so the new owner
        must not be shown the address that claimed the node for somebody else."""
        from core.users import get_or_create_magic_link_user

        token = self._mailed()
        client.post("/api/auth/claim/consume", json={"token": token})
        bob = asyncio.run(get_or_create_magic_link_user("bob@example.com"))
        asyncio.set_event_loop(asyncio.new_event_loop())

        r = client.put("/api/admin/nodes/ret1a2b3c4d/owner", json={"user_id": str(bob.id)})

        assert r.status_code == 200
        assert self._claim_on_file() is None

    def test_an_admin_reassigning_a_node_to_its_own_owner_keeps_the_claim(self, client):
        token = self._mailed()
        ada_id = client.post("/api/auth/claim/consume", json={"token": token}).json()["user"]["id"]

        r = client.put("/api/admin/nodes/ret1a2b3c4d/owner", json={"user_id": ada_id})

        assert r.status_code == 200
        assert self._claim_on_file().email == "ada@example.com"

    @staticmethod
    def _owned_by_the_caller(verified: bool, node_id="ret1a2b3c4d"):
        """The node owned by the test client's account, with the address that
        was offered for it either confirmed or not."""
        from core.auth import set_node_owner
        from core.users import ANONYMOUS_USER, async_session_maker
        from services.node_claim_store import mark_verified

        async def _own():
            if verified:
                async with async_session_maker() as session:
                    async with session.begin():
                        await mark_verified(session, node_id, "ada@example.com")
            await set_node_owner(node_id, ANONYMOUS_USER["id"])

        asyncio.run(_own())
        asyncio.set_event_loop(asyncio.new_event_loop())

    def test_the_owner_list_names_the_address_a_node_was_claimed_with(self, client):
        self._mailed()
        self._owned_by_the_caller(verified=True)

        [node] = [n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == "ret1a2b3c4d"]

        assert node["claimed_with"] == "ada@example.com"

    def test_an_address_nobody_confirmed_is_not_what_a_node_was_claimed_with(self, client):
        """An owner reached some other way, such as a claim code, beside an
        address that was offered and never confirmed: that address is a
        stranger's as far as this owner is concerned."""
        self._mailed()
        self._owned_by_the_caller(verified=False)

        [node] = [n for n in client.get("/api/auth/me/nodes").json() if n["node_id"] == "ret1a2b3c4d"]

        assert node["claimed_with"] is None

    def test_releasing_a_node_the_caller_does_not_own_is_404(self, client):
        """The same answer a node that does not exist gets: an id that resolves
        is already a hint about where a receiver is."""
        assert client.delete("/api/auth/me/nodes/ret1a2b3c4d/claim").status_code == 404
