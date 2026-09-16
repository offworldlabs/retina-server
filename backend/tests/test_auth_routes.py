"""HTTP endpoint tests for auth routes.

Covers:
  /api/auth/me, /api/auth/logout
  /api/auth/me/claim-codes  (GET / POST / DELETE)
  /api/auth/me/nodes
  /api/auth/me/nodes/{id}/location-privacy (PUT / DELETE)
  OAuth state token (CSRF + open-redirect guards)
  /api/admin/invites         (GET / POST / DELETE)
  /api/admin/node-owners     (GET)
  /api/admin/nodes/{id}/owner (PUT)
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
            with state.connected_nodes_lock:
                state.connected_nodes.pop(node_id, None)


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


# ── OAuth state token (CSRF + open-redirect) ──────────────────────────────────


@pytest.fixture()
def oauth_context(monkeypatch):
    """Exercise only the auth router, with no app workers or provider traffic."""
    from routes import auth

    application = FastAPI()
    application.include_router(auth.router)
    provider = AsyncMock()
    provider.post.return_value = httpx.Response(200, json={"access_token": "provider-token"})
    provider.get.return_value = httpx.Response(200, json={"email": "user@example.com", "name": "User"})
    provider.__aenter__.return_value = provider
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kwargs: provider)
    monkeypatch.setattr(auth, "get_or_create_oauth_user", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(auth, "monotonic", lambda: clock.now, raising=False)
    monkeypatch.setattr(auth, "_oauth_states", {}, raising=False)
    with TestClient(application, base_url="https://dashboard.example.test", follow_redirects=False) as browser:
        yield SimpleNamespace(browser=browser, provider=provider, clock=clock)


def _start_oauth(browser, provider="google", redirect="/dashboard"):
    response = browser.get(f"/api/auth/login/{provider}", params={"redirect": redirect})
    assert response.status_code == 307
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def _finish_oauth(browser, state, provider="google"):
    return browser.get(f"/api/auth/callback/{provider}", params={"code": "provider-code", "state": state})


def _oauth_client_from(browser, source, *, trusted_proxy=False):
    """Set the transport peer independently of attacker-controlled headers."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    application = browser.app
    if trusted_proxy:
        application = ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1")

    async def connected(scope, receive, send):
        await application({**scope, "client": (source, 12345)}, receive, send)

    return TestClient(connected, base_url=str(browser.base_url), follow_redirects=False)


class TestOAuthSourceQuota:
    def test_one_source_cannot_fill_store_by_switching_providers_or_spoofing_headers(self, oauth_context, monkeypatch):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES_PER_SOURCE", 2, raising=False)
        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES", 4)
        with _oauth_client_from(oauth_context.browser, "192.0.2.1", trusted_proxy=True) as source:
            _start_oauth(source, "google")
            _start_oauth(source, "github")
            for suffix in range(4):
                # This direct peer is not a trusted proxy; arbitrary forwarded
                # addresses and fresh cookies must not buy it another quota.
                source.cookies.clear()
                denied = source.get(
                    "/api/auth/login/google",
                    headers={
                        "X-Forwarded-For": f"198.51.100.{suffix}",
                        "X-Real-IP": f"198.51.100.{suffix}",
                        "CF-Connecting-IP": f"198.51.100.{suffix}",
                        "Host": f"surface-{suffix}.example.test",
                    },
                )
                assert denied.status_code == 429
                assert denied.headers["Retry-After"] == "600"
            assert len(auth._oauth_states) == 2
        with _oauth_client_from(oauth_context.browser, "192.0.2.2") as other:
            _start_oauth(other)
        assert len(auth._oauth_states) == 3

    def test_consumption_releases_source_quota_even_if_callback_ip_changed(self, oauth_context, monkeypatch):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES_PER_SOURCE", 1, raising=False)
        with _oauth_client_from(oauth_context.browser, "192.0.2.1") as source:
            state = _start_oauth(source)
            assert source.get("/api/auth/login/github").status_code == 429
            with _oauth_client_from(oauth_context.browser, "192.0.2.2") as roaming:
                roaming.cookies.update(dict(source.cookies))
                assert _finish_oauth(roaming, state).headers["location"] == "/dashboard"
            _start_oauth(source)
            assert len(auth._oauth_states) == 1

    def test_expiration_releases_source_quota(self, oauth_context, monkeypatch):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES_PER_SOURCE", 1, raising=False)
        _start_oauth(oauth_context.browser)
        oauth_context.clock.now += 300
        denied = oauth_context.browser.get("/api/auth/login/google")
        assert denied.status_code == 429
        assert denied.headers["Retry-After"] == "300"
        oauth_context.clock.now += 300
        _start_oauth(oauth_context.browser)
        assert len(auth._oauth_states) == 1

    def test_trusted_proxy_uses_last_untrusted_hop(self, oauth_context, monkeypatch):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES_PER_SOURCE", 1, raising=False)
        with _oauth_client_from(oauth_context.browser, "127.0.0.1", trusted_proxy=True) as proxy:
            assert (
                proxy.get("/api/auth/login/google", headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.1"}).status_code
                == 307
            )
            assert (
                proxy.get("/api/auth/login/google", headers={"X-Forwarded-For": "198.51.100.2, 192.0.2.1"}).status_code
                == 429
            )
            assert (
                proxy.get("/api/auth/login/google", headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.2"}).status_code
                == 307
            )

    @pytest.mark.parametrize(
        ("first", "same_source", "other"),
        [
            ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:3::1"),
            ("192.0.2.1", "::ffff:192.0.2.1", "192.0.2.2"),
        ],
    )
    def test_address_variants_share_quota(self, oauth_context, monkeypatch, first, same_source, other):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES_PER_SOURCE", 1, raising=False)
        with _oauth_client_from(oauth_context.browser, first) as browser:
            _start_oauth(browser)
        with _oauth_client_from(oauth_context.browser, same_source) as browser:
            assert browser.get("/api/auth/login/google").status_code == 429
        with _oauth_client_from(oauth_context.browser, other) as browser:
            _start_oauth(browser)


@pytest.mark.parametrize("provider", ["google", "github"])
class TestOAuthBrowserBinding:
    def test_login_cookie_is_host_only_and_short_lived(self, oauth_context, provider):
        browser = oauth_context.browser
        response = browser.get(f"/api/auth/login/{provider}")
        cookies = SimpleCookie()
        cookies.load(response.headers.get("set-cookie", ""))
        cookie = cookies[f"__Host-retina-oauth-{provider}"]
        assert cookie["secure"]
        assert cookie["httponly"]
        assert cookie["samesite"] == "lax"
        assert cookie["path"] == "/"
        assert cookie["domain"] == ""
        assert cookie["max-age"] == "600"

    def test_state_requires_the_initiating_browser(self, oauth_context, provider):
        browser = oauth_context.browser
        state = _start_oauth(browser, provider)
        with TestClient(browser.app, base_url=str(browser.base_url), follow_redirects=False) as stranger:
            response = _finish_oauth(stranger, state, provider)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()
        assert "auth_token" not in response.cookies
        assert _finish_oauth(browser, state, provider).headers["location"] == "/dashboard"

    def test_state_expires_even_if_browser_keeps_cookie(self, oauth_context, provider):
        state = _start_oauth(oauth_context.browser, provider)
        oauth_context.clock.now += 601
        response = _finish_oauth(oauth_context.browser, state, provider)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()

    def test_old_callback_does_not_clear_a_newer_login_cookie(self, oauth_context, provider):
        browser = oauth_context.browser
        previous = _start_oauth(browser, provider)
        current = _start_oauth(browser, provider)
        rejected = _finish_oauth(browser, previous, provider)
        assert rejected.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()
        assert _finish_oauth(browser, current, provider).headers["location"] == "/dashboard"

    def test_accepted_slow_callback_does_not_clear_a_newer_login_cookie(self, oauth_context, provider):
        browser = oauth_context.browser
        previous = _start_oauth(browser, provider)
        exchange_started = Event()
        finish_exchange = Event()

        async def slow_exchange(*args, **kwargs):
            exchange_started.set()
            assert await asyncio.to_thread(finish_exchange.wait, 5)
            return httpx.Response(200, json={"access_token": "provider-token"})

        oauth_context.provider.post.side_effect = slow_exchange
        with ThreadPoolExecutor(max_workers=1) as executor:
            previous_callback = executor.submit(_finish_oauth, browser, previous, provider)
            try:
                assert exchange_started.wait(5)
                current = _start_oauth(browser, provider)
            finally:
                finish_exchange.set()
            assert previous_callback.result(timeout=5).headers["location"] == "/dashboard"

        assert _finish_oauth(browser, current, provider).headers["location"] == "/dashboard"

    def test_state_and_cookie_cannot_be_replayed(self, oauth_context, provider):
        browser = oauth_context.browser
        state = _start_oauth(browser, provider)
        response = _finish_oauth(browser, state, provider)
        assert response.headers["location"] == "/dashboard"
        assert "auth_token" in response.cookies
        assert f"__Host-retina-oauth-{provider}" in browser.cookies

        repeated = _finish_oauth(browser, state, provider)
        assert repeated.headers["location"] == "/login?error=invalid_state"
        assert oauth_context.provider.post.await_count == 1

    def test_concurrent_callbacks_consume_state_once(self, oauth_context, provider):
        browser = oauth_context.browser
        state = _start_oauth(browser, provider)
        cookies = dict(browser.cookies)

        def callback():
            with TestClient(browser.app, base_url=str(browser.base_url), follow_redirects=False) as copy:
                copy.cookies.update(cookies)
                return _finish_oauth(copy, state, provider).headers["location"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: callback(), range(2)))
        assert sorted(results) == ["/dashboard", "/login?error=invalid_state"]
        assert oauth_context.provider.post.await_count == 1

    def test_provider_error_also_consumes_state(self, oauth_context, provider):
        browser = oauth_context.browser
        state = _start_oauth(browser, provider)
        oauth_context.provider.post.return_value = httpx.Response(400, json={"error": "invalid_code"})
        response = _finish_oauth(browser, state, provider)
        assert response.headers["location"] == f"/login?error={provider}_token_failed"
        assert f"__Host-retina-oauth-{provider}" in browser.cookies
        assert _finish_oauth(browser, state, provider).headers["location"] == "/login?error=invalid_state"
        assert oauth_context.provider.post.await_count == 1

    def test_state_cannot_move_to_another_host(self, oauth_context, provider):
        browser = oauth_context.browser
        state = _start_oauth(browser, provider)
        with TestClient(browser.app, base_url="https://other.example.test", follow_redirects=False) as other:
            # Even deliberately copying the cookie cannot move the callback URI.
            other.cookies.update(dict(browser.cookies))
            response = _finish_oauth(other, state, provider)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()


class TestOAuthChallengeStore:
    def test_state_is_bound_to_its_provider(self, oauth_context):
        browser = oauth_context.browser
        state = _start_oauth(browser, "google")
        _start_oauth(browser, "github")
        response = _finish_oauth(browser, state, "github")
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()
        assert _finish_oauth(browser, state, "google").headers["location"] == "/dashboard"

    def test_pending_challenges_are_bounded_and_expired_entries_are_pruned(self, oauth_context, monkeypatch):
        from routes import auth

        monkeypatch.setattr(auth, "_MAX_OAUTH_STATES", 2, raising=False)
        browser = oauth_context.browser
        _start_oauth(browser)
        _start_oauth(browser)
        assert browser.get("/api/auth/login/google").status_code == 503
        oauth_context.clock.now += 601
        state = _start_oauth(browser)
        assert len(auth._oauth_states) == 1
        assert _finish_oauth(browser, state).headers["location"] == "/dashboard"

    def test_restart_invalidates_pending_challenges(self, oauth_context, monkeypatch):
        from routes import auth

        state = _start_oauth(oauth_context.browser)
        monkeypatch.setattr(auth, "_oauth_states", {})
        response = _finish_oauth(oauth_context.browser, state)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()


class TestOAuthStateToken:
    def test_valid_state_roundtrip(self, oauth_context):
        state = _start_oauth(oauth_context.browser)
        assert _finish_oauth(oauth_context.browser, state).headers["location"] == "/dashboard"

    def test_tampered_state_rejected(self, oauth_context):
        state = _start_oauth(oauth_context.browser)
        response = _finish_oauth(oauth_context.browser, "tampered-" + state)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()

    @pytest.mark.parametrize("state", ["notvalid", "", "a:b"])
    def test_invalid_format_state_rejected(self, oauth_context, state):
        response = _finish_oauth(oauth_context.browser, state)
        assert response.headers["location"] == "/login?error=invalid_state"
        oauth_context.provider.post.assert_not_awaited()

    def test_open_redirect_blocked_by_safe_redirect(self):
        from routes.auth import _safe_redirect

        assert _safe_redirect("//evil.com") == "/"
        assert _safe_redirect("https://evil.com/steal") == "/"
        assert _safe_redirect("/dashboard") == "/dashboard"

    def test_open_redirect_in_login_is_sanitized(self, oauth_context):
        state = _start_oauth(oauth_context.browser, redirect="//evil.com/steal")
        assert _finish_oauth(oauth_context.browser, state).headers["location"] == "/"

    @pytest.mark.parametrize(
        "redirect", ["/\\evil.example", "/\t/evil.example", "/\r/evil.example", "/\n/evil.example", "/\x00", "/\x7f"]
    )
    def test_ambiguous_redirect_paths_are_rejected(self, oauth_context, redirect):
        state = _start_oauth(oauth_context.browser, redirect=redirect)
        assert _finish_oauth(oauth_context.browser, state).headers["location"] == "/"

    def test_local_redirect_preserves_query_parameters(self, oauth_context):
        redirect = "/dashboard?tab=nodes&next=%2Fmap#ownership"
        state = _start_oauth(oauth_context.browser, redirect=redirect)
        assert _finish_oauth(oauth_context.browser, state).headers["location"] == redirect


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
        import uuid

        r = client.put(
            "/api/admin/nodes/some-node/owner",
            json={"user_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404
