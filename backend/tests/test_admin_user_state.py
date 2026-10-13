"""Deactivating and reactivating an account from the admin API.

The routes are in routes/admin.py, over core/sessions.py. Every test here runs
as a deployment does: the anonymous-admin bypass off, the administrator a
verified Access identity, and the account its auth_token cookie, so each route
below reads the cookie rather than answering the bypass.
"""

import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import routes.admin as admin
import routes.analytics as analytics
import routes.streaming as streaming
from config.constants import EVENT_LOG_MAX
from core import sessions, state, users
from core.auth import create_magic_link
from core.users import ACCESS_ASSERTION_HEADER, ANONYMOUS_USER, JWT_SECRET, User, async_session_maker
from services import node_refs, publication
from tests.ownership_helpers import register_node_row, set_node_owner

OPERATOR = "operator@example.com"
OWNER = "owner@example.com"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}
ACTIONS = ("deactivate", "reactivate")

_NODE = "deactnode01"


class _Access:
    """Stands in for core.users.access_identity, verifying ASSERTION as OPERATOR."""

    def is_configured(self) -> bool:
        return True

    async def identity(self, token: str | None) -> str | None:
        return OPERATOR if token == ASSERTION else None


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch, tmp_path):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    # Both import the flag by name, so the patch above does not reach them.
    monkeypatch.setattr(analytics, "AUTH_BYPASS", False)
    monkeypatch.setattr(streaming, "AUTH_BYPASS", False)
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
    monkeypatch.setattr(users, "access_identity", _Access())
    # This test's events alone, and saved under tmp_path rather than backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


@pytest.fixture
def account() -> User:
    return asyncio.run(users.get_or_create_magic_link_user(OWNER))


def _cookie(token: str) -> dict[str, str]:
    # By header: the cookie is Secure and the test client speaks plain HTTP.
    return {"Cookie": f"{sessions.COOKIE_NAME}={token}"}


def _sign_in(user: User) -> dict[str, str]:
    """Headers carrying the cookie of a session opened for the account now."""
    token, _ = asyncio.run(sessions.open_session(user, user_agent="Mozilla/5.0 (X11; Linux x86_64)", ip=None))
    return _cookie(token)


def _legacy_cookie(user: User) -> dict[str, str]:
    """Headers carrying a fastapi-users JWT, as minted before sessions, a minute old."""
    exp = datetime.now(UTC) - timedelta(minutes=1) + sessions.LEGACY_TOKEN_LIFETIME
    return _cookie(jwt.encode({"sub": str(user.id), "aud": ["fastapi-users:auth"], "exp": exp}, JWT_SECRET, "HS256"))


def _is_active(user: User) -> bool:
    async def _read() -> bool:
        async with async_session_maker() as db:
            row = await db.get(User, user.id)
            assert row is not None
            return row.is_active

    return asyncio.run(_read())


def _call(client: TestClient, action: str, user_id: object, headers: dict[str, str] | None = None):
    return client.post(f"/api/admin/users/{user_id}/{action}", headers=AS_ADMIN if headers is None else headers)


def _events(client: TestClient) -> list[dict]:
    r = client.get("/api/admin/events", headers=AS_ADMIN)
    assert r.status_code == 200
    return r.json()


def _owner_feed(client: TestClient, headers: dict[str, str]):
    return client.websocket_connect("/ws/aircraft/owner", headers=headers)


# ── a. the two routes, and what they record ──────────────────────────────────


class TestDeactivateAndReactivate:
    def test_deactivating_answers_the_account_inactive(self, client, account):
        r = _call(client, "deactivate", account.id)

        assert r.status_code == 200
        body = r.json()
        assert (body["id"], body["email"], body["is_active"]) == (str(account.id), OWNER, False)
        assert body["role"] == "user"
        assert _is_active(account) is False
        listed = {u["id"]: u for u in client.get("/api/admin/users", headers=AS_ADMIN).json()}
        assert listed[str(account.id)]["is_active"] is False

    def test_deactivation_is_recorded_naming_the_administrator(self, client, account):
        _call(client, "deactivate", account.id)

        (event,) = _events(client)
        assert event["category"] == "user"
        assert event["severity"] == "warning"
        assert str(account.id) in event["message"]
        assert event["meta"] == {"user_id": str(account.id), "by": OPERATOR}

    def test_reactivating_answers_the_account_active(self, client, account):
        _call(client, "deactivate", account.id)

        r = _call(client, "reactivate", account.id)

        assert r.status_code == 200
        assert (r.json()["id"], r.json()["is_active"]) == (str(account.id), True)
        assert _is_active(account) is True

    def test_reactivation_is_recorded_naming_the_administrator(self, client, account):
        _call(client, "deactivate", account.id)
        _call(client, "reactivate", account.id)

        # Newest first.
        reactivated, deactivated = _events(client)
        assert (reactivated["category"], reactivated["severity"]) == ("user", "info")
        assert str(account.id) in reactivated["message"]
        assert reactivated["meta"] == {"user_id": str(account.id), "by": OPERATOR}
        assert deactivated["meta"] == {"user_id": str(account.id), "by": OPERATOR}

    def test_the_account_id_is_recorded_as_stored_whatever_the_path_spelled(self, client, account):
        r = _call(client, "deactivate", str(account.id).upper())

        assert r.status_code == 200
        assert _events(client)[0]["meta"]["user_id"] == str(account.id)

    def test_the_anonymous_admin_is_recorded_as_itself(self, client, account, monkeypatch):
        """A laptop's console, where the bypass is opted into."""
        monkeypatch.setattr(users, "AUTH_BYPASS", True)

        assert _call(client, "deactivate", account.id, headers={}).status_code == 200

        assert _events(client)[0]["meta"]["by"] == ANONYMOUS_USER["email"]


# ── b. a deactivated account is signed out everywhere ────────────────────────


@pytest.fixture
def private_node(account):
    """The ref of a node registered private, owned by the account, with analytics to serve.

    Private because only then does the per-node analytics route read the
    cookie at all: a public node answers everyone alike.
    """
    ref = asyncio.run(register_node_row(_NODE, "private"))
    asyncio.run(set_node_owner(_NODE, str(account.id)))
    publication._reset_for_tests()
    node_refs._reset_for_tests()
    state.node_analytics.register_node(_NODE, {"node_id": _NODE, "rx_lat": 34.0, "rx_lon": -82.0})
    try:
        yield ref
    finally:
        state.node_analytics.retire_node(_NODE)


class TestDeactivationSignsTheAccountOut:
    """Each test first shows the cookie working, so its refusal afterwards is
    the deactivation's doing and not a cookie that never worked."""

    def test_its_session_cookie_is_refused_by_me_and_my_nodes(self, client, account):
        cookie = _sign_in(account)
        assert client.get("/api/auth/me", headers=cookie).json()["email"] == OWNER
        assert client.get("/api/auth/me/nodes", headers=cookie).status_code == 200

        _call(client, "deactivate", account.id)

        assert client.get("/api/auth/me", headers=cookie).status_code == 401
        assert client.get("/api/auth/me/nodes", headers=cookie).status_code == 401

    def test_its_jwt_cookie_from_before_sessions_is_refused_too(self, client, account):
        cookie = _legacy_cookie(account)
        assert client.get("/api/auth/me", headers=cookie).status_code == 200

        _call(client, "deactivate", account.id)

        assert client.get("/api/auth/me", headers=cookie).status_code == 401
        assert client.get("/api/auth/me/nodes", headers=cookie).status_code == 401

    def test_the_owner_feed_refuses_its_cookie(self, client, account):
        cookie = _sign_in(account)
        with _owner_feed(client, cookie):
            pass

        _call(client, "deactivate", account.id)

        with pytest.raises(WebSocketDisconnect) as refused:
            with _owner_feed(client, cookie):
                pass
        assert refused.value.code == 1008

    def test_per_node_analytics_stops_answering_it_for_its_private_node(self, client, account, private_node):
        """404, the route's one answer for a private node to anyone but its owner."""
        cookie = _sign_in(account)
        assert client.get(f"/api/radar/analytics/{private_node}", headers=cookie).status_code == 200
        assert client.get("/api/auth/me/nodes", headers=cookie).json()[0]["node_id"] == _NODE

        _call(client, "deactivate", account.id)

        r = client.get(f"/api/radar/analytics/{private_node}", headers=cookie)
        assert r.status_code == 404
        assert r.json() == {"detail": "Node not found"}

    def test_a_sign_in_link_mailed_before_signs_nobody_in(self, client, account):
        # Shown live by the preview, which does not spend it: redeeming another
        # link would delete this one and prove nothing.
        token = asyncio.run(create_magic_link(OWNER))
        assert client.get(f"/api/auth/magic-link/{token}").json() == {"email": OWNER}

        _call(client, "deactivate", account.id)

        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 400
        assert "set-cookie" not in r.headers

    def test_reactivation_does_not_bring_its_cookies_back(self, client, account):
        session_cookie = _sign_in(account)
        legacy_cookie = _legacy_cookie(account)

        _call(client, "deactivate", account.id)
        _call(client, "reactivate", account.id)

        assert client.get("/api/auth/me", headers=session_cookie).status_code == 401
        assert client.get("/api/auth/me", headers=legacy_cookie).status_code == 401
        with pytest.raises(WebSocketDisconnect) as refused:
            with _owner_feed(client, session_cookie):
                pass
        assert refused.value.code == 1008

    def test_a_session_opened_after_reactivation_works(self, client, account):
        _call(client, "deactivate", account.id)
        _call(client, "reactivate", account.id)

        cookie = _sign_in(account)

        assert client.get("/api/auth/me", headers=cookie).json()["email"] == OWNER
        assert client.get("/api/auth/me/nodes", headers=cookie).status_code == 200
        with _owner_feed(client, cookie):
            pass


# ── c. who may call them ─────────────────────────────────────────────────────


class TestOnlyAnAdministratorMay:
    @pytest.mark.parametrize("action", ACTIONS)
    def test_a_signed_in_account_is_403_even_for_itself(self, client, account, action):
        cookie = _sign_in(account)

        r = _call(client, action, account.id, headers=cookie)

        assert r.status_code == 403
        assert _is_active(account) is True
        assert client.get("/api/auth/me", headers=cookie).status_code == 200
        assert _events(client) == []

    @pytest.mark.parametrize("action", ACTIONS)
    def test_no_credentials_is_401(self, client, account, action):
        r = _call(client, action, account.id, headers={})

        assert r.status_code == 401
        assert _is_active(account) is True
        assert _events(client) == []

    @pytest.mark.parametrize("action", ACTIONS)
    @pytest.mark.parametrize("user_id", [uuid.uuid4(), "not-a-uuid", "0"], ids=["unknown", "malformed", "short"])
    def test_an_id_that_is_no_account_is_404(self, client, account, action, user_id):
        r = _call(client, action, user_id)

        assert r.status_code == 404
        assert r.json() == {"detail": "User not found"}
        assert _is_active(account) is True
        assert _events(client) == []
