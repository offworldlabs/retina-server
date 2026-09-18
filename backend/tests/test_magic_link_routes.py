"""The magic-link endpoints in routes/auth.py.

The invariant most of these exist to protect: /api/auth/magic-link answers the
same way for an address that owns receivers and one that has never been seen,
so it cannot be used to find out which addresses do. tests/test_magic_links.py
covers the token store underneath.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.auth as _auth
from core.auth import create_magic_link
from main import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _mail_configured(monkeypatch):
    """Mail resolves and the link has a host, and nothing is actually sent.

    HOST_APP belongs here rather than in the tests that assert on the link:
    without it the endpoint refuses outright, so every test that expects a 202
    depends on it being set."""
    monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@retina.fm>")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "t")
    monkeypatch.setenv("HOST_APP", "app.retina.fm")


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Record what would have been mailed, without a thread or a socket."""
    posted = []
    monkeypatch.setattr(
        _auth.mail,
        "send_in_background",
        lambda to, subject, body: posted.append((to, subject, body)),
    )
    return posted


@pytest.fixture(autouse=True)
def _clear_source_quota():
    """The per-source window is module state shared by every test in the
    session, and TestClient presents the same client address to all of them."""
    _auth._magic_link_requests.clear()
    yield
    _auth._magic_link_requests.clear()


# ── Requesting a link ─────────────────────────────────────────────────────────


class TestRequestMagicLink:
    def test_accepts_and_mails_a_link(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        assert len(sent) == 1
        to, subject, body = sent[0]
        assert to == "owner@example.com"
        assert "/dash/auth/link/" in body

    def test_the_mailed_link_carries_the_token_and_nothing_else_does(self, client, sent):
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        _, _, body = sent[0]
        token = body.split("/dash/auth/link/")[1].split()[0]
        assert len(token) > 20

    def test_the_response_body_never_carries_the_token(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        _, _, body = sent[0]
        token = body.split("/dash/auth/link/")[1].split()[0]
        assert token not in r.text

    def test_the_link_host_comes_from_configuration_not_the_request(self, client, sent, monkeypatch):
        """Otherwise: ask for a link to somebody else's address with a forged
        Host header, and the mail they receive points at your server carrying a
        working token."""
        monkeypatch.setenv("HOST_APP", "app.retina.fm")
        client.post(
            "/api/auth/magic-link",
            json={"email": "victim@example.com"},
            headers={"Host": "evil.example.com"},
        )
        _, _, body = sent[0]
        assert "https://app.retina.fm/dash/auth/link/" in body
        assert "evil.example.com" not in body

    def test_the_link_lands_on_the_dashboard_mount_not_the_map(self, client, sent, monkeypatch):
        """HOST_APP serves the map bundle at / and mounts the dashboard under
        /dash/. A link to /auth/link/... renders the map, which has no router,
        and the token is never redeemed."""
        monkeypatch.setenv("HOST_APP", "app.retina.fm")
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        _, _, body = sent[0]
        assert "/dash/auth/link/" in body
        assert "app.retina.fm/auth/link/" not in body

    def test_an_unknown_address_gets_the_same_answer_as_a_known_one(self, client):
        """The whole point. A differing status, body or shape here says which
        addresses have accounts."""
        first = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        client.post("/api/auth/magic-link/consume", json={"token": "x"})
        second = client.post("/api/auth/magic-link", json={"email": "nobody@example.com"})
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()

    def test_a_capped_address_still_gets_202(self, client, sent):
        """Reaching the outstanding-link cap must not be visible either: it
        would say the address had been asked for before."""
        with patch.object(_auth, "create_magic_link", return_value=None):
            r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        assert sent == []

    def test_a_malformed_address_is_rejected(self, client, sent):
        r = client.post("/api/auth/magic-link", json={"email": "not-an-address"})
        assert r.status_code == 422
        assert sent == []

    def test_unavailable_when_the_link_host_is_unknown(self, client, monkeypatch, sent):
        """Without HOST_APP the only other source of a host is the request, and
        the caller controls that. Refusing beats mailing a link to wherever the
        Host header said. Checked before a token is minted, so nothing is left
        behind."""
        monkeypatch.delenv("HOST_APP", raising=False)
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 503
        assert sent == []

    @pytest.mark.asyncio
    async def test_a_refused_request_mints_no_token(self, client, monkeypatch):
        from sqlalchemy import select

        from core.users import MagicLink, async_session_maker

        monkeypatch.delenv("HOST_APP", raising=False)
        client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        async with async_session_maker() as session:
            assert (await session.execute(select(MagicLink))).scalars().all() == []

    def test_unavailable_when_mail_is_not_configured(self, client, monkeypatch, sent):
        monkeypatch.delenv("MAIL_TRANSPORT", raising=False)
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 503
        assert sent == []

    def test_no_session_cookie_is_set_by_asking(self, client):
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert "auth_token" not in r.cookies


class TestSourceQuota:
    def test_beyond_the_quota_nothing_is_mailed(self, client, sent):
        for _ in range(_auth._MAX_MAGIC_LINK_REQUESTS_PER_SOURCE):
            client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        before = len(sent)
        r = client.post("/api/auth/magic-link", json={"email": "someone@example.com"})
        assert r.status_code == 202, "the quota must not be visible in the response"
        assert len(sent) == before

    def test_a_full_source_table_refuses_new_sources(self, client, sent, monkeypatch):
        """The table is bounded, or a flood from many addresses grows it until
        the process dies. Full, sign-in stops for a window rather than for good."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_SOURCES", 0)
        r = client.post("/api/auth/magic-link", json={"email": "owner@example.com"})
        assert r.status_code == 202
        assert sent == []

    def test_the_quota_counts_requests_not_addresses(self, client, sent):
        """Otherwise it bounds nothing: the abuse is mailing many strangers."""
        for i in range(_auth._MAX_MAGIC_LINK_REQUESTS_PER_SOURCE):
            client.post("/api/auth/magic-link", json={"email": f"target{i}@example.com"})
        before = len(sent)
        client.post("/api/auth/magic-link", json={"email": "one-more@example.com"})
        assert len(sent) == before


def _peer(application, source, *, trusted_proxy=False):
    """A caller whose transport peer is `source`, whatever headers it sends."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    target = ProxyHeadersMiddleware(application, trusted_hosts="127.0.0.1") if trusted_proxy else application

    async def connected(scope, receive, send):
        await target({**scope, "client": (source, 12345)}, receive, send)

    return TestClient(connected, base_url="https://dash.example.test")


class TestSourceDerivation:
    """What counts as one source. The quota above bounds nothing unless a caller
    is unable to widen it, so the address comes from the transport peer after the
    ASGI server's trusted-proxy handling and never from a forwarding header.
    """

    @pytest.fixture()
    def router_app(self, monkeypatch):
        """Only the auth router, with no token store behind it."""
        monkeypatch.setattr(_auth, "create_magic_link", AsyncMock(return_value="a-token"))
        application = FastAPI()
        application.include_router(_auth.router)
        return application

    @staticmethod
    def _mailed(caller, sent, **kwargs):
        """Whether asking for a link got one, without reading the response: the
        answer is 202 either way, which is the point of the endpoint."""
        before = len(sent)
        response = caller.post("/api/auth/magic-link", json={"email": "owner@example.com"}, **kwargs)
        assert response.status_code == 202
        return len(sent) > before

    def test_a_forwarding_header_from_an_untrusted_peer_buys_no_quota(self, router_app, sent, monkeypatch):
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, "192.0.2.1") as caller:
            assert self._mailed(caller, sent)
            for suffix in range(4):
                spoofed = f"198.51.100.{suffix}"
                assert not self._mailed(
                    caller,
                    sent,
                    headers={
                        "X-Forwarded-For": spoofed,
                        "X-Real-IP": spoofed,
                        "CF-Connecting-IP": spoofed,
                        "Host": f"surface-{suffix}.example.test",
                    },
                )
        with _peer(router_app, "192.0.2.2") as other:
            assert self._mailed(other, sent)

    def test_trusted_proxy_uses_last_untrusted_hop(self, router_app, sent, monkeypatch):
        """Deployed, nginx is the peer and appends the address it validated, so
        the rightmost hop it did not add is the client."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, "127.0.0.1", trusted_proxy=True) as proxy:
            assert self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.1"})
            assert not self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.2, 192.0.2.1"})
            assert self._mailed(proxy, sent, headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.2"})

    @pytest.mark.parametrize(
        ("first", "same_source", "other"),
        [
            ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:3::1"),
            ("192.0.2.1", "::ffff:192.0.2.1", "192.0.2.2"),
        ],
    )
    def test_address_variants_share_quota(self, router_app, sent, monkeypatch, first, same_source, other):
        """One /64 is one source, and privacy-address rotation inside it moves
        nobody to a fresh quota. A v4-mapped address is the v4 address."""
        monkeypatch.setattr(_auth, "_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE", 1)
        with _peer(router_app, first) as caller:
            assert self._mailed(caller, sent)
        with _peer(router_app, same_source) as caller:
            assert not self._mailed(caller, sent)
        with _peer(router_app, other) as caller:
            assert self._mailed(caller, sent)


# ── Redeeming a link ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConsumeMagicLink:
    async def test_redeeming_opens_a_session(self, client):
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 200
        assert r.json()["user"]["email"] == "owner@example.com"
        assert "auth_token" in r.cookies

    async def test_the_cookie_is_a_session_for_that_person(self, client):
        """Read the cookie back through the same strategy the WebSocket
        handshake uses, rather than through /api/auth/me: the suite runs with
        AUTH_ALLOW_ANONYMOUS_ADMIN=1 (conftest), so that route answers with the
        anonymous admin whatever cookie is presented and would pass here for
        the wrong reason."""
        from core.users import read_user_from_token

        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})

        user = await read_user_from_token(r.cookies["auth_token"])
        assert user is not None
        assert user.email == "owner@example.com"
        assert user.is_superuser is False

    async def test_the_cookie_is_httponly_and_secure(self, client):
        """It is the whole session and /ws/aircraft/owner reads it off the
        handshake, so script access to it would be script access to the feed."""
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        header = r.headers["set-cookie"]
        assert "HttpOnly" in header
        assert "Secure" in header

    async def test_the_account_is_never_an_administrator(self, client):
        """Admin is Cloudflare Access. Receiving mail is not a claim to it."""
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        user = r.json()["user"]
        assert user["is_superuser"] is False
        assert user["role"] == "user"

    async def test_the_signed_in_user_carries_what_me_would_say_about_the_fleet(self, client, monkeypatch):
        """The page adopts this user without asking /me, so it must not lack
        anything /me would have told it: the physics layer reads the fleet flag."""
        monkeypatch.setenv("SYNTHETIC_FLEET_ENABLED", "1")
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.json()["user"]["synthetic_fleet"] is True

    async def test_the_account_records_how_it_was_made(self, client):
        token = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.json()["user"]["provider"] == "magic-link"

    async def test_an_existing_superuser_cannot_sign_in_by_link(self, client):
        """Administrator identity is Cloudflare Access. If a superuser row
        exists for the address, reading that mailbox must not be a way to hold
        its session: the guard has to cover a row that already exists, not only
        one created here."""
        from sqlalchemy import select

        from core.users import User, async_session_maker

        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        async with async_session_maker() as session:
            user = (await session.execute(select(User))).scalars().one()
            user.is_superuser = True
            await session.commit()

        again = await create_magic_link("owner@example.com")
        r = client.post("/api/auth/magic-link/consume", json={"token": again})
        assert r.status_code == 400
        assert "auth_token" not in r.cookies

    async def test_the_superuser_refusal_is_indistinguishable_from_a_dead_link(self, client):
        """A distinct answer would say which addresses are privileged."""
        from sqlalchemy import select

        from core.users import User, async_session_maker

        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        async with async_session_maker() as session:
            user = (await session.execute(select(User))).scalars().one()
            user.is_superuser = True
            await session.commit()

        again = await create_magic_link("owner@example.com")
        refused = client.post("/api/auth/magic-link/consume", json={"token": again})
        never = client.post("/api/auth/magic-link/consume", json={"token": "never-issued"})
        assert refused.status_code == never.status_code
        assert refused.json() == never.json()

    async def test_a_second_redemption_fails(self, client):
        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        again = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert again.status_code == 400

    @pytest.mark.parametrize("token", ["never-issued", "", "   ", "a" * 200, "../../etc/passwd", "%00"])
    async def test_a_bad_token_is_refused_the_same_way(self, client, token):
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 400
        assert "auth_token" not in r.cookies

    async def test_the_failure_message_does_not_distinguish_the_reason(self, client):
        """Unknown, expired and already-redeemed must read identically, or the
        message says which guesses were once real."""
        token = await create_magic_link("owner@example.com")
        client.post("/api/auth/magic-link/consume", json={"token": token})
        replayed = client.post("/api/auth/magic-link/consume", json={"token": token})
        never = client.post("/api/auth/magic-link/consume", json={"token": "never-issued"})
        assert replayed.status_code == never.status_code
        assert replayed.json() == never.json()

    async def test_redeeming_twice_does_not_create_two_accounts(self, client):
        first = await create_magic_link("owner@example.com")
        one = client.post("/api/auth/magic-link/consume", json={"token": first})
        second = await create_magic_link("owner@example.com")
        two = client.post("/api/auth/magic-link/consume", json={"token": second})
        assert one.json()["user"]["id"] == two.json()["user"]["id"]

    async def test_a_get_on_the_consume_route_does_not_redeem(self, client):
        """The mailed URL is a GET that a mail scanner may prefetch. Only the
        POST from the page may consume, or the token is gone before the
        recipient clicks it."""
        token = await create_magic_link("owner@example.com")
        assert client.get(f"/api/auth/magic-link/consume?token={token}").status_code in (404, 405)
        assert client.post("/api/auth/magic-link/consume", json={"token": token}).status_code == 200
