"""The session routes in routes/auth.py: signing in and out, the list of an
account's own sessions, and the JWT cookies from before sessions.

Through the app, with the anonymous-admin bypass off: the suite opts into it
(conftest), and with it on /api/auth/me answers the anonymous admin whatever
cookie is presented, so these would pass for the wrong reason. The store under
the routes is driven directly in tests/test_sessions.py.

The cookie goes by header, as in tests/test_magic_link_routes.py: it is Secure,
and the test client speaks plain HTTP, so its own jar never sends it.
"""

import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import Morsel, SimpleCookie
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from starlette.websockets import WebSocketDisconnect

from core import sessions, users
from core.auth import create_magic_link
from core.nodes import Node
from core.sessions import ABSOLUTE_LIFETIME, COOKIE_NAME, IDLE_LIFETIME, LEGACY_TOKEN_LIFETIME
from core.users import JWT_SECRET, RetiredLegacyToken, User, UserSession, async_session_maker
from main import app
from services import claim_links
from services.node_claim_store import put_challenge, read_challenge, set_claim_address
from tests.nginx_helpers import locations, render
from tests.ownership_helpers import get_node_owner

ADA = "ada@example.com"
BOB = "bob@example.com"
NODE_ID = "ret1a2b3c4d"
NODE_REF = "nde1a2b3c4d00"
FIREFOX_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
SAFARI_ON_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
DEAD_SIGN_IN_LINK = {"detail": "That sign-in link is no longer valid"}
DEAD_CLAIM_LINK = {"detail": "That link is no longer valid"}
SESSION_ROUTES = [
    ("GET", "/api/auth/me/sessions"),
    ("DELETE", f"/api/auth/me/sessions/{uuid.UUID(int=1)}"),
    ("POST", "/api/auth/me/sessions/revoke-others"),
]


@pytest.fixture(autouse=True)
def _cookies_are_read(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)


def _as(cookie: str, **headers: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE_NAME}={cookie}", **headers}


def _set_cookies(response) -> list[Morsel]:
    """Every auth_token the response sets, in the order it sets them."""
    found = []
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        if COOKIE_NAME in jar:
            found.append(jar[COOKIE_NAME])
    return found


def _cleared(response) -> bool:
    """Whether the browser is left with no auth_token after this response."""
    set_cookies = _set_cookies(response)
    return bool(set_cookies) and set_cookies[-1]["max-age"] == "0" and set_cookies[-1].value == ""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _account(email: str = ADA) -> User:
    return await users.get_or_create_magic_link_user(email)


async def _sign_in(client, email: str = ADA, *, user_agent: str = FIREFOX_ON_WINDOWS) -> str:
    """Redeem a sign-in link for `email`, returning the cookie it set."""
    token = await create_magic_link(email)
    r = client.post("/api/auth/magic-link/consume", json={"token": token}, headers={"User-Agent": user_agent})
    assert r.status_code == 200
    (cookie,) = _set_cookies(r)
    return cookie.value


def _legacy_cookie(user: User, *, issued_at: datetime | None = None) -> str:
    """A cookie as fastapi-users minted one (tests/test_sessions.py, _legacy_cookie)."""
    issued_at = datetime.now(UTC) if issued_at is None else issued_at
    payload = {"sub": str(user.id), "aud": ["fastapi-users:auth"], "exp": issued_at + LEGACY_TOKEN_LIFETIME}
    return jwt.encode(payload, JWT_SECRET, "HS256")


async def _rows(model) -> list:
    async with async_session_maker() as db:
        return list(await db.scalars(select(model)))


async def _session_of(cookie: str) -> UserSession:
    async with async_session_maker() as db:
        return (await db.scalars(select(UserSession).where(UserSession.token_hash == _hash(cookie)))).one()


async def _claim_link(email: str = ADA) -> str:
    """A live claim link for a registered node, as tests/test_claim_routes.py sets one up."""
    challenge = await claim_links.issue(email=email, node_id=NODE_ID, now=time.time())
    assert challenge is not None
    async with async_session_maker() as db:
        async with db.begin():
            if await db.get(Node, NODE_ID) is None:
                db.add(Node(node_id=NODE_ID, node_ref=NODE_REF, board_model="raspberrypi5-4gb"))
            await set_claim_address(db, NODE_ID, email)
            await put_challenge(db, NODE_ID, email, challenge.handle, challenge.expires_at)
    return challenge.token


def _me(client, cookie: str):
    return client.get("/api/auth/me", headers=_as(cookie))


def _sessions(client, cookie: str) -> list[dict]:
    r = client.get("/api/auth/me/sessions", headers=_as(cookie))
    assert r.status_code == 200
    return r.json()


def _from(address: str) -> TestClient:
    """A client whose transport peer is `address`, as the ASGI server reports
    it after its trusted-proxy handling. Not entered, so it runs no lifespan
    of its own beside the `client` fixture's."""

    async def connected(scope, receive, send):
        await app({**scope, "client": (address, 50000)}, receive, send)

    return TestClient(connected, raise_server_exceptions=False)


class _AccessVerifier:
    def is_configured(self):
        return True

    async def identity(self, token):
        return "admin@example.com" if token else None


# ── Signing in and out ───────────────────────────────────────────────────────


class TestSigningInAndOut:
    async def test_signing_in_sets_a_thirty_day_cookie_whose_token_is_stored_only_hashed(self, client):
        token = await create_magic_link(ADA)

        r = client.post("/api/auth/magic-link/consume", json={"token": token})

        assert r.status_code == 200
        (cookie,) = _set_cookies(r)
        assert ABSOLUTE_LIFETIME.total_seconds() - 60 < int(cookie["max-age"]) <= ABSOLUTE_LIFETIME.total_seconds()
        assert (cookie["httponly"], cookie["secure"], cookie["samesite"].lower(), cookie["path"]) == (
            True,
            True,
            "lax",
            "/",
        )
        async with async_session_maker() as db:
            stored = (await db.execute(text("SELECT * FROM user_sessions"))).all()
        assert len(stored) == 1
        assert not any(cookie.value in str(value) for value in stored[0])
        assert (await _session_of(cookie.value)).token_hash == _hash(cookie.value)
        assert _me(client, cookie.value).json()["email"] == ADA

    async def test_signing_out_ends_the_session_so_a_kept_copy_of_the_cookie_stops_working(self, client):
        cookie = await _sign_in(client)
        assert _me(client, cookie).status_code == 200

        r = client.post("/api/auth/logout", headers=_as(cookie))

        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert _cleared(r)
        assert await _rows(UserSession) == []
        assert _me(client, cookie).status_code == 401
        assert client.get("/api/auth/me/nodes", headers=_as(cookie)).status_code == 401

    async def test_signing_out_ends_only_the_session_asking(self, client):
        laptop = await _sign_in(client)
        phone = await _sign_in(client, user_agent=SAFARI_ON_IOS)

        client.post("/api/auth/logout", headers=_as(laptop))

        assert _me(client, laptop).status_code == 401
        assert _me(client, phone).status_code == 200

    @pytest.mark.parametrize("cookie", [None, "never-issued", "a.b.c"])
    def test_signing_out_with_nothing_to_end_still_clears_the_cookie(self, client, cookie):
        r = client.post("/api/auth/logout", headers=_as(cookie) if cookie else {})

        assert r.status_code == 200
        assert _cleared(r)

    async def test_signing_out_twice_answers_the_same(self, client):
        cookie = await _sign_in(client)

        first = client.post("/api/auth/logout", headers=_as(cookie))
        second = client.post("/api/auth/logout", headers=_as(cookie))

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()

    async def test_signing_out_on_an_admin_hostname_ends_the_cookie_session_as_well(self, client):
        """Both credentials go, whichever one authenticated the request."""
        cookie = await _sign_in(client)

        with patch("core.users.access_identity", _AccessVerifier()):
            r = client.post("/api/auth/logout", headers=_as(cookie, **{"Cf-Access-Jwt-Assertion": "a-token"}))

        assert r.json() == {"ok": True, "redirect": "/cdn-cgi/access/logout"}
        assert _cleared(r)
        assert _me(client, cookie).status_code == 401

    async def test_a_sign_in_records_the_browser_and_the_network_never_the_address(self):
        caller = _from("203.0.113.57")
        token = await create_magic_link(ADA)

        cookie = _set_cookies(
            caller.post("/api/auth/magic-link/consume", json={"token": token}, headers={"User-Agent": SAFARI_ON_IOS})
        )[0].value

        (listed,) = _sessions(caller, cookie)
        assert (listed["user_agent"], listed["ip"]) == ("Safari on iOS", "203.0.113.0/24")
        async with async_session_maker() as db:
            stored = (await db.execute(text("SELECT * FROM user_sessions"))).all()
        assert not any("203.0.113.57" in str(value) for value in stored[0])

    async def test_an_ipv6_sign_in_records_its_slash_48(self):
        caller = _from("2001:db8:1:2::1")
        token = await create_magic_link(ADA)

        cookie = _set_cookies(caller.post("/api/auth/magic-link/consume", json={"token": token}))[0].value

        assert _sessions(caller, cookie)[0]["ip"] == "2001:db8:1::/48"

    async def test_a_claim_link_opens_a_session_too(self, client):
        await _account()
        token = await _claim_link()

        r = client.post("/api/auth/claim/consume", json={"token": token}, headers={"User-Agent": FIREFOX_ON_WINDOWS})

        assert r.status_code == 200
        (cookie,) = _set_cookies(r)
        assert ABSOLUTE_LIFETIME.total_seconds() - 60 < int(cookie["max-age"]) <= ABSOLUTE_LIFETIME.total_seconds()
        assert _me(client, cookie.value).json()["email"] == ADA
        assert _sessions(client, cookie.value)[0]["user_agent"] == "Firefox on Windows"


# ── The sessions API ─────────────────────────────────────────────────────────


class TestSessionsApi:
    async def test_each_browser_lists_both_sessions_and_itself_as_current(self, client):
        laptop = await _sign_in(client, user_agent=FIREFOX_ON_WINDOWS)
        phone = await _sign_in(client, user_agent=SAFARI_ON_IOS)

        for cookie in (laptop, phone):
            listed = _sessions(client, cookie)

            assert [entry["user_agent"] for entry in listed] == ["Safari on iOS", "Firefox on Windows"]
            assert [entry["id"] for entry in listed if entry["current"]] == [(await _session_of(cookie)).id]

    async def test_an_entry_carries_its_times_as_epoch_seconds_and_nothing_secret(self, client):
        before = time.time()
        cookie = await _sign_in(client)

        (entry,) = _sessions(client, cookie)

        assert set(entry) == {"id", "created_at", "last_used_at", "expires_at", "user_agent", "ip", "current"}
        assert before - 1 <= entry["created_at"] <= entry["last_used_at"] <= time.time()
        assert entry["expires_at"] == pytest.approx(entry["last_used_at"] + IDLE_LIFETIME.total_seconds())
        row = await _session_of(cookie)
        assert entry["id"] == row.id
        assert cookie not in str(entry)
        assert row.token_hash not in str(entry)

    async def test_revoking_another_of_ones_own_sessions_signs_that_browser_out(self, client):
        laptop = await _sign_in(client)
        phone = await _sign_in(client, user_agent=SAFARI_ON_IOS)

        r = client.delete(f"/api/auth/me/sessions/{(await _session_of(phone)).id}", headers=_as(laptop))

        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert _set_cookies(r) == []
        assert _me(client, phone).status_code == 401
        assert _me(client, laptop).status_code == 200
        assert [entry["current"] for entry in _sessions(client, laptop)] == [True]

    async def test_another_accounts_session_is_not_found_and_survives(self, client):
        ada = await _sign_in(client, ADA)
        bob = await _sign_in(client, BOB)

        r = client.delete(f"/api/auth/me/sessions/{(await _session_of(bob)).id}", headers=_as(ada))
        unknown = client.delete(f"/api/auth/me/sessions/{uuid.uuid4()}", headers=_as(ada))

        assert r.status_code == unknown.status_code == 404
        assert r.json() == unknown.json() == {"detail": "Session not found"}
        assert _me(client, bob).status_code == 200
        assert len(_sessions(client, bob)) == 1
        assert _me(client, ada).status_code == 200

    async def test_signing_out_everywhere_else_ends_every_other_session_of_the_account(self, client):
        here = await _sign_in(client)
        elsewhere = [await _sign_in(client, user_agent=SAFARI_ON_IOS) for _ in range(2)]
        bob = await _sign_in(client, BOB)

        r = client.post("/api/auth/me/sessions/revoke-others", headers=_as(here))

        assert r.status_code == 200
        assert r.json() == {"revoked": 2}
        assert _set_cookies(r) == []
        assert [_me(client, cookie).status_code for cookie in elsewhere] == [401, 401]
        assert _me(client, here).status_code == 200
        assert _me(client, bob).status_code == 200
        assert [entry["current"] for entry in _sessions(client, here)] == [True]

    async def test_signing_out_everywhere_else_with_nothing_else_ends_nothing(self, client):
        here = await _sign_in(client)

        assert client.post("/api/auth/me/sessions/revoke-others", headers=_as(here)).json() == {"revoked": 0}
        assert _me(client, here).status_code == 200

    async def test_revoking_the_current_session_signs_out_and_clears_the_cookie(self, client):
        cookie = await _sign_in(client)

        r = client.delete(f"/api/auth/me/sessions/{(await _session_of(cookie)).id}", headers=_as(cookie))

        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert _cleared(r)
        assert _me(client, cookie).status_code == 401
        assert client.get("/api/auth/me/sessions", headers=_as(cookie)).status_code == 401

    @pytest.mark.parametrize(("method", "path"), SESSION_ROUTES)
    @pytest.mark.parametrize("cookie", [None, "never-issued", "a.b.c"])
    def test_without_a_session_cookie_every_route_is_401(self, client, method, path, cookie):
        r = client.request(method, path, headers=_as(cookie) if cookie else {})

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}
        assert _set_cookies(r) == []

    @pytest.mark.parametrize(("method", "path"), SESSION_ROUTES)
    def test_an_administrator_with_no_cookie_session_has_none_to_list_or_end(self, client, monkeypatch, method, path):
        """Their identity is an Access assertion or the bypass, neither of which
        is a session here."""
        with patch("core.users.access_identity", _AccessVerifier()):
            access = client.request(method, path, headers={"Cf-Access-Jwt-Assertion": "a-token"})
        monkeypatch.setattr(users, "AUTH_BYPASS", True)
        anonymous = client.request(method, path)

        assert access.status_code == anonymous.status_code == 401
        assert access.json() == anonymous.json() == {"detail": "Not authenticated"}

    async def test_the_subject_is_the_cookie_whoever_else_the_request_is(self, client, monkeypatch):
        """With the bypass on, every other route answers the anonymous admin.
        These answer the account whose sessions they are."""
        cookie = await _sign_in(client)
        monkeypatch.setattr(users, "AUTH_BYPASS", True)

        listed = _sessions(client, cookie)

        assert [entry["id"] for entry in listed] == [(await _session_of(cookie)).id]

    async def test_a_session_of_a_deactivated_account_lists_nothing(self, client):
        cookie = await _sign_in(client)
        await sessions.deactivate_account((await _session_of(cookie)).user_id)

        assert client.get("/api/auth/me/sessions", headers=_as(cookie)).status_code == 401


# ── JWT cookies from before sessions ─────────────────────────────────────────


class TestLegacyCookies:
    async def test_me_exchanges_a_legacy_cookie_and_signing_out_ends_both(self, client):
        legacy = _legacy_cookie(await _account())

        r = _me(client, legacy)

        assert r.status_code == 200
        assert r.json()["email"] == ADA
        (fresh,) = _set_cookies(r)
        assert fresh.value and "." not in fresh.value and fresh.value != legacy
        assert (await _session_of(fresh.value)).token_hash == _hash(fresh.value)

        again = _me(client, fresh.value)
        assert again.status_code == 200
        assert _set_cookies(again) == []

        client.post("/api/auth/logout", headers=_as(fresh.value))

        assert _me(client, legacy).status_code == 401
        assert _me(client, fresh.value).status_code == 401

    async def test_an_exchanged_legacy_cookie_aliases_its_session_rather_than_opening_another(self, client):
        legacy = _legacy_cookie(await _account())
        _me(client, legacy)

        alias = _me(client, legacy)

        assert alias.status_code == 200
        assert _set_cookies(alias) == []
        assert len(await _rows(UserSession)) == 1

    async def test_the_exchanged_cookie_ends_when_the_legacy_one_would_have_ended_its_sign_in(self, client):
        """Thirty days from the sign-in the JWT was issued at, not from today."""
        three_days_ago = datetime.now(UTC) - timedelta(days=3)
        legacy = _legacy_cookie(await _account(), issued_at=three_days_ago)

        (fresh,) = _set_cookies(_me(client, legacy))

        remaining = (ABSOLUTE_LIFETIME - timedelta(days=3)).total_seconds()
        assert remaining - 60 < int(fresh["max-age"]) <= remaining

    async def test_an_expired_legacy_cookie_is_refused(self, client):
        legacy = _legacy_cookie(await _account(), issued_at=datetime.now(UTC) - timedelta(days=8))

        r = _me(client, legacy)

        assert r.status_code == 401
        assert _set_cookies(r) == []
        assert client.get("/api/auth/me/sessions", headers=_as(legacy)).status_code == 401
        assert await _rows(UserSession) == []

    async def test_me_answers_without_a_cookie_when_another_request_exchanged_it_first(self, client, monkeypatch):
        legacy = _legacy_cookie(await _account())
        exchange = sessions.exchange_legacy

        async def beaten(resolved, **device):
            await exchange(resolved, **device)
            return await exchange(resolved, **device)

        monkeypatch.setattr(sessions, "exchange_legacy", beaten)

        r = _me(client, legacy)

        assert r.status_code == 200
        assert r.json()["email"] == ADA
        assert _set_cookies(r) == []
        assert len(await _rows(UserSession)) == 1

    async def test_the_sessions_list_exchanges_a_legacy_cookie_first(self, client):
        legacy = _legacy_cookie(await _account())

        r = client.get("/api/auth/me/sessions", headers=_as(legacy))

        assert r.status_code == 200
        (fresh,) = _set_cookies(r)
        (entry,) = r.json()
        assert entry["current"] is True
        assert entry["id"] == (await _session_of(fresh.value)).id

    async def test_a_legacy_cookie_another_request_exchanged_first_lists_that_session_as_current(
        self, client, monkeypatch
    ):
        legacy = _legacy_cookie(await _account())
        exchange = sessions.exchange_legacy
        opened = []

        async def beaten(resolved, **device):
            first = await exchange(resolved, **device)
            assert first is not None
            opened.append(first[1].id)
            return await exchange(resolved, **device)

        monkeypatch.setattr(sessions, "exchange_legacy", beaten)

        r = client.get("/api/auth/me/sessions", headers=_as(legacy))

        assert r.status_code == 200
        assert _set_cookies(r) == []
        assert [(entry["id"], entry["current"]) for entry in r.json()] == [(opened[0], True)]

    async def test_a_legacy_cookie_signed_out_by_another_request_first_is_401(self, client, monkeypatch):
        legacy = _legacy_cookie(await _account())

        async def signed_out_first(resolved, **_device):
            await sessions.end(resolved)
            return None

        monkeypatch.setattr(sessions, "exchange_legacy", signed_out_first)

        r = client.get("/api/auth/me/sessions", headers=_as(legacy))

        assert r.status_code == 401
        assert _set_cookies(r) == []

    async def test_a_not_found_revocation_still_hands_over_the_exchanged_cookie(self, client):
        legacy = _legacy_cookie(await _account())

        r = client.delete(f"/api/auth/me/sessions/{uuid.uuid4()}", headers=_as(legacy))

        assert r.status_code == 404
        assert r.json() == {"detail": "Session not found"}
        (fresh,) = _set_cookies(r)
        assert _me(client, fresh.value).status_code == 200

    async def test_signing_out_everywhere_else_from_a_legacy_cookie_keeps_its_new_session_only(self, client):
        ada = await _account()
        legacy, other_legacy = _legacy_cookie(ada), _legacy_cookie(ada, issued_at=datetime.now(UTC) - timedelta(days=1))
        elsewhere = await _sign_in(client)

        r = client.post("/api/auth/me/sessions/revoke-others", headers=_as(legacy))

        assert r.json() == {"revoked": 1}
        (fresh,) = _set_cookies(r)
        assert _me(client, fresh.value).status_code == 200
        assert [_me(client, cookie).status_code for cookie in (legacy, other_legacy, elsewhere)] == [401, 401, 401]

    async def test_signing_out_a_legacy_cookie_nobody_exchanged_retires_it(self, client):
        legacy = _legacy_cookie(await _account())

        r = client.post("/api/auth/logout", headers=_as(legacy))

        assert _cleared(r)
        assert _me(client, legacy).status_code == 401
        assert await _rows(UserSession) == []

    async def test_the_owner_feed_accepts_a_legacy_cookie_read_only(self, client, monkeypatch):
        monkeypatch.setattr("routes.streaming.AUTH_BYPASS", False)
        ada = await _account()

        with client.websocket_connect("/ws/aircraft/owner", headers=_as(_legacy_cookie(ada))):
            pass

        assert await _rows(UserSession) == []
        assert await _rows(RetiredLegacyToken) == []

    async def test_the_owner_feed_refuses_an_expired_legacy_cookie(self, client, monkeypatch):
        """So that the connection above says something about the cookie."""
        monkeypatch.setattr("routes.streaming.AUTH_BYPASS", False)
        expired = _legacy_cookie(await _account(), issued_at=datetime.now(UTC) - timedelta(days=8))

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/aircraft/owner", headers=_as(expired)):
                pass


# ── Deactivated accounts ─────────────────────────────────────────────────────


class TestDeactivatedAccounts:
    async def test_a_sign_in_link_reads_as_a_dead_link_and_opens_nothing(self, client):
        ada = await _account()
        token = await create_magic_link(ADA)
        await sessions.deactivate_account(ada.id)

        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        never = client.post("/api/auth/magic-link/consume", json={"token": "never-issued"})

        assert r.status_code == never.status_code == 400
        assert r.json() == never.json() == DEAD_SIGN_IN_LINK
        assert "set-cookie" not in r.headers
        assert await _rows(UserSession) == []

    async def test_a_claim_link_binds_nothing_opens_nothing_and_stays_unspent(self, client):
        ada = await _account()
        await sessions.deactivate_account(ada.id)
        token = await _claim_link()

        r = client.post("/api/auth/claim/consume", json={"token": token})

        assert r.status_code == 400
        assert r.json() == DEAD_CLAIM_LINK
        assert "set-cookie" not in r.headers
        assert await get_node_owner(NODE_ID) is None
        assert await _rows(UserSession) == []
        assert await claim_links.peek(token) is not None
        async with async_session_maker() as db:
            challenge = await read_challenge(db, NODE_ID)
        assert challenge is not None
        assert challenge.handle == claim_links.handle_for(token)
        # Still the recipient's to decline.
        assert client.post("/api/auth/claim/decline", json={"token": token}).status_code == 200

    async def test_an_account_deactivated_as_its_session_opens_reads_as_a_dead_link(self, client, monkeypatch):
        """An administrator deactivating it after the route's own check, just
        before the session is written."""
        ada = await _account()
        sign_in, claim = await create_magic_link(ADA), await _claim_link()
        open_session = sessions.open_session

        async def deactivated_meanwhile(user, **device):
            await sessions.deactivate_account(user.id)
            return await open_session(user, **device)

        monkeypatch.setattr(sessions, "open_session", deactivated_meanwhile)

        signed_in = client.post("/api/auth/magic-link/consume", json={"token": sign_in})
        await sessions.reactivate_account(ada.id)
        claimed = client.post("/api/auth/claim/consume", json={"token": claim})

        assert (signed_in.status_code, signed_in.json()) == (400, DEAD_SIGN_IN_LINK)
        assert (claimed.status_code, claimed.json()) == (400, DEAD_CLAIM_LINK)
        assert "set-cookie" not in signed_in.headers
        assert "set-cookie" not in claimed.headers
        assert await _rows(UserSession) == []
        (account,) = await _rows(User)
        assert account.last_seen_at is None


# ── An account whose row says superuser ──────────────────────────────────────


class TestSuperuserRows:
    """is_superuser on a row grants nothing: an address that can receive a link
    signs in as the account it is, and the console is Cloudflare Access's to
    grant. A row that says superuser is not refused either, so whoever reads
    that mailbox holds an ordinary session and no more."""

    async def test_a_superuser_row_signs_in_owns_its_node_and_is_no_administrator(self, client):
        ada = await _account()
        async with async_session_maker() as db:
            await db.execute(update(User).where(User.id == ada.id).values(is_superuser=True))
            await db.commit()

        token = await create_magic_link(ADA)
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 200
        (cookie,) = _set_cookies(r)
        assert (r.json()["user"]["role"], r.json()["user"]["is_superuser"]) == ("user", False)

        me = _me(client, cookie.value).json()
        assert (me["role"], me["is_superuser"]) == ("user", False)

        claimed = client.post("/api/auth/claim/consume", json={"token": await _claim_link()})
        assert claimed.status_code == 200
        assert await get_node_owner(NODE_ID) == str(ada.id)
        nodes = client.get("/api/auth/me/nodes", headers=_as(cookie.value)).json()
        assert [node["node_id"] for node in nodes] == [NODE_ID]

        refused = client.get("/api/admin/users", headers=_as(cookie.value))
        assert refused.status_code == 403


class TestSessionRoutesInNginx:
    """The routes under /api/auth/me are session reads, not credential surface:
    nginx has to match them to the `location /api/auth/me` block, which uses the
    looser `session` zone, rather than to `/api/auth/` and its tight `auth` one."""

    PATHS = (
        "/api/auth/me/sessions",
        "/api/auth/me/sessions/5b0f2c1e-8d3a-4e6f-9a7b-1c2d3e4f5a6b",
        "/api/auth/me/sessions/revoke-others",
    )

    @staticmethod
    def _vhosts_with_the_credential_prefix() -> list[list[tuple[str, str]]]:
        servers = re.split(r"(?m)^server\s*\{", render())[1:]
        found = [locations(server) for server in servers]
        return [
            blocks for blocks in found if any(re.fullmatch(r"location\s+/api/auth/\s*", header) for header, _ in blocks)
        ]

    @staticmethod
    def _matched(blocks: list[tuple[str, str]], path: str) -> tuple[str, str]:
        prefixes = [
            (header.split()[-1], body)
            for header, body in blocks
            if len(header.split()) == 2 and path.startswith(header.split()[-1])
        ]
        return max(prefixes, key=lambda pair: len(pair[0]))

    def test_the_credential_prefix_is_on_three_vhosts(self):
        assert len(self._vhosts_with_the_credential_prefix()) == 3

    @pytest.mark.parametrize("path", PATHS)
    def test_each_route_lands_in_the_session_zone(self, path):
        for blocks in self._vhosts_with_the_credential_prefix():
            prefix, body = self._matched(blocks, path)
            assert prefix == "/api/auth/me", path
            assert "zone=session" in body, body
            assert "zone=auth" not in body, body


class TestLegacyExchangeWithoutTheWriteLock:
    """The exchange on /api/auth/me is a courtesy: a write that cannot happen
    leaves the answer as it was, and the next page load tries again."""

    @pytest.fixture(autouse=True)
    async def _impatient_writers(self):
        """Writers give up after 200 ms rather than the pragma's 5 s."""

        def impatient(dbapi_conn, _conn_rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=200")
            cur.close()

        await users.engine.dispose()
        event.listen(users.engine.sync_engine, "connect", impatient)
        yield
        event.remove(users.engine.sync_engine, "connect", impatient)
        await users.engine.dispose()

    @staticmethod
    @contextmanager
    def _write_lock_held():
        """Another connection holds the write lock, as any long writer would."""
        holder = sqlite3.connect(users.engine.url.database, timeout=5, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    async def test_me_answers_a_legacy_cookie_unexchanged_while_the_database_is_locked(self, client):
        legacy = _legacy_cookie(await _account())

        with self._write_lock_held():
            r = _me(client, legacy)
            sessions_written = await _rows(UserSession)

        assert r.status_code == 200
        assert r.json()["email"] == ADA
        assert _set_cookies(r) == []
        assert sessions_written == []

        retried = _me(client, legacy)

        assert retried.status_code == 200
        (fresh,) = _set_cookies(retried)
        assert (await _session_of(fresh.value)).token_hash == _hash(fresh.value)
        assert len(await _rows(UserSession)) == 1
