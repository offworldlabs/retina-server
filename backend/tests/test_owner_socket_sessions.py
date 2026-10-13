"""An open owner feed is closed when its own session ends.

/ws/aircraft/owner reads the auth_token cookie at the handshake and then
streams for as long as the socket stays open, so routes/streaming.py watches
the account (core/sessions.py, watch_account) and closes the socket with 1008
once that cookie no longer signs the account in.

Through the app with real sessions and the anonymous-admin bypass off, as a
deployment runs: with it on, the feed reads no cookie at all. The check on the
clock is put an hour off unless a test says otherwise, so a socket that closes
within the two seconds asserted here was closed by the wake, not the backstop.

The cookie goes by header: it is Secure and the test client speaks plain HTTP.
"""

import asyncio
import hashlib
import queue
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie

import jwt
import orjson
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocket

import routes.admin as admin
import routes.streaming as streaming
from config.constants import EVENT_LOG_MAX
from core import sessions, state, users
from core.auth import create_magic_link
from core.database import DATABASE_URL
from core.users import ACCESS_ASSERTION_HEADER, JWT_SECRET, User, UserSession, async_session_maker
from services import node_refs, publication
from services.tasks.aircraft_flush import broadcast_aircraft
from tests.ownership_helpers import own_registered_node

OWNER = "owner@example.com"
OPERATOR = "operator@example.com"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}
LAPTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
PHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
# What "promptly" is held to: well under the half minute the backstop takes.
PROMPTLY = 2.0
FRAME_NOW = 1_760_000_000.0

_NODE = "ownsock0001"
_DB_PATH = DATABASE_URL.split("///", 1)[1]


class _Access:
    """Stands in for core.users.access_identity, verifying ASSERTION as OPERATOR."""

    def is_configured(self) -> bool:
        return True

    async def identity(self, token: str | None) -> str | None:
        return OPERATOR if token == ASSERTION else None


class _Checks:
    """The owner feed's read-only checks of its cookie, as sessions.resolve sees them.

    `results` holds what each check resolved to, or the error it raised. With
    `failing` set, a check raises as a locked database would. A resolve that
    slides, as every route's does, is passed through and not recorded.
    """

    def __init__(self) -> None:
        self.results: list = []
        self.failing = False

    def failures(self) -> int:
        return sum(isinstance(result, OperationalError) for result in self.results)


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch, tmp_path):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    # Imported by name there, so the patch above does not reach it.
    monkeypatch.setattr(streaming, "AUTH_BYPASS", False)
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
    monkeypatch.setattr(streaming, "SESSION_RECHECK_S", 3600.0)
    monkeypatch.setattr(users, "access_identity", _Access())
    # This test's events alone, and saved under tmp_path rather than backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


@pytest.fixture(autouse=True)
def checks(monkeypatch) -> _Checks:
    seen = _Checks()
    resolve = sessions.resolve

    async def recorded(token, **kwargs):
        if kwargs.get("slide", True):
            return await resolve(token, **kwargs)
        if seen.failing:
            error = OperationalError("SELECT user_sessions", {}, Exception("database is locked"))
            seen.results.append(error)
            raise error
        resolved = await resolve(token, **kwargs)
        seen.results.append(resolved)
        return resolved

    monkeypatch.setattr(sessions, "resolve", recorded)
    return seen


@pytest.fixture(autouse=True)
def _no_watch_outlives_its_test():
    yield
    _eventually(lambda: not sessions._watches, "every watch released")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _eventually(condition: Callable[[], bool], what: str, *, within: float = PROMPTLY) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"Not within {within}s: {what}")
        time.sleep(0.01)


def _as(cookie: str) -> dict[str, str]:
    return {"Cookie": f"{sessions.COOKIE_NAME}={cookie}"}


def _sign_in(client: TestClient, user_agent: str = LAPTOP) -> str:
    """Redeem a sign-in link for OWNER through the app, returning the cookie it set."""
    token = asyncio.run(create_magic_link(OWNER))
    assert token is not None
    r = client.post("/api/auth/magic-link/consume", json={"token": token}, headers={"User-Agent": user_agent})
    assert r.status_code == 200
    jar = SimpleCookie()
    for header in r.headers.get_list("set-cookie"):
        jar.load(header)
    return jar[sessions.COOKIE_NAME].value


def _legacy_cookie(user: User) -> str:
    """A fastapi-users JWT as minted before sessions, a minute old."""
    exp = datetime.now(UTC) - timedelta(minutes=1) + sessions.LEGACY_TOKEN_LIFETIME
    return jwt.encode({"sub": str(user.id), "aud": ["fastapi-users:auth"], "exp": exp}, JWT_SECRET, "HS256")


def _session_of(cookie: str) -> UserSession:
    async def _read() -> UserSession:
        token_hash = hashlib.sha256(cookie.encode()).hexdigest()
        async with async_session_maker() as db:
            return (await db.scalars(select(UserSession).where(UserSession.token_hash == token_hash))).one()

    return asyncio.run(_read())


def _account(user_id: str) -> User:
    async def _read() -> User:
        async with async_session_maker() as db:
            account = await db.get(User, uuid.UUID(user_id))
            assert account is not None
            return account

    return asyncio.run(_read())


def _own_node(user_id: str) -> str:
    """Register _NODE to the account and return its ref."""
    ref = asyncio.run(own_registered_node(_NODE, user_id))
    # Read afresh: both were primed as the app started, before the node existed.
    publication._reset_for_tests()
    node_refs._reset_for_tests()
    return ref


def _frame() -> dict:
    """One frame as the flush builds it, with an aircraft _NODE heard."""
    return {
        "now": FRAME_NOW,
        "messages": 1,
        "aircraft": [{"hex": "abc123", "node_id": _NODE, "lat": 34.0, "lon": -82.0}],
        "detection_arcs": [],
        "ground_truth": {},
    }


def _broadcast(client: TestClient) -> None:
    """Send _frame() down the path aircraft_flush_task does, on the app's own loop."""
    portal = client.portal
    assert portal is not None
    portal.call(broadcast_aircraft, _frame())


def _guards(client: TestClient) -> int:
    """How many owner-feed guards are running on the app's loop."""

    async def _count() -> int:
        return sum(
            getattr(task.get_coro(), "__name__", "") == streaming._close_when_signed_out.__name__
            for task in asyncio.all_tasks()
        )

    portal = client.portal
    assert portal is not None
    return portal.call(_count)


@contextmanager
def _owner_feed(client: TestClient, cookie: str, checks: _Checks) -> Iterator[tuple[WebSocketTestSession, WebSocket]]:
    """An owner feed on `cookie`, once it is admitted and its cookie checked once.

    Yields the test's end of the socket and the server's, which is what
    state.ws_owner_clients is keyed on.
    """
    before = set(state.ws_owner_clients)
    checked = len(checks.results)
    with client.websocket_connect("/ws/aircraft/owner", headers=_as(cookie)) as ws:
        _eventually(
            lambda: len(state.ws_owner_clients) > len(before) and len(checks.results) > checked,
            "the socket admitted and its cookie checked",
        )
        (server_side,) = set(state.ws_owner_clients) - before
        yield ws, server_side


def _sent(ws: WebSocketTestSession, within: float) -> dict:
    """The next message the server sends.

    Read off the session's queue with a timeout: its receive() waits for ever,
    which would make a socket left open a hang rather than a failure.
    """
    try:
        message = ws._send_queue.get(timeout=max(0.0, within))
    except queue.Empty:
        pytest.fail(f"Nothing sent within {within}s")
    if isinstance(message, BaseException):
        raise message
    return message


def _close_code(ws: WebSocketTestSession) -> int:
    """The code the server closes the socket with, past any frame sent before it."""
    deadline = time.monotonic() + PROMPTLY
    while (message := _sent(ws, deadline - time.monotonic()))["type"] != "websocket.close":
        pass
    return message["code"]


def _next_frame(ws: WebSocketTestSession) -> dict:
    message = _sent(ws, PROMPTLY)
    assert message["type"] == "websocket.send", message
    return orjson.loads(message["text"])


def _assert_closed_promptly(ws: WebSocketTestSession, server_side: WebSocket, user_id: object, started: float) -> None:
    assert _close_code(ws) == 1008
    assert time.monotonic() - started < PROMPTLY
    # Gone by the time the close reaches the client, not only once the
    # handler has returned.
    assert server_side not in state.ws_owner_clients
    assert str(user_id) not in sessions._watches


# ── Ending the socket's own session closes it ────────────────────────────────


class TestTheSocketsOwnSessionEnding:
    def test_deactivating_the_account_closes_it(self, client, checks):
        cookie = _sign_in(client)
        user_id = _session_of(cookie).user_id

        with _owner_feed(client, cookie, checks) as (ws, server_side):
            started = time.monotonic()
            r = client.post(f"/api/admin/users/{user_id}/deactivate", headers=AS_ADMIN)

            assert r.status_code == 200
            _assert_closed_promptly(ws, server_side, user_id, started)

    def test_revoking_its_session_from_another_closes_it(self, client, checks):
        laptop = _sign_in(client)
        phone = _sign_in(client, PHONE)
        row = _session_of(laptop)

        with _owner_feed(client, laptop, checks) as (ws, server_side):
            started = time.monotonic()
            r = client.delete(f"/api/auth/me/sessions/{row.id}", headers=_as(phone))

            assert r.json() == {"ok": True}
            _assert_closed_promptly(ws, server_side, row.user_id, started)

    def test_signing_out_everywhere_else_from_another_session_closes_it(self, client, checks):
        laptop = _sign_in(client)
        phone = _sign_in(client, PHONE)
        user_id = _session_of(laptop).user_id

        with _owner_feed(client, laptop, checks) as (ws, server_side):
            started = time.monotonic()
            r = client.post("/api/auth/me/sessions/revoke-others", headers=_as(phone))

            assert r.json() == {"revoked": 1}
            _assert_closed_promptly(ws, server_side, user_id, started)

    def test_signing_out_with_its_own_cookie_closes_it(self, client, checks):
        cookie = _sign_in(client)
        user_id = _session_of(cookie).user_id

        with _owner_feed(client, cookie, checks) as (ws, server_side):
            started = time.monotonic()
            r = client.post("/api/auth/logout", headers=_as(cookie))

            assert r.json() == {"ok": True}
            _assert_closed_promptly(ws, server_side, user_id, started)

    def test_a_sign_out_written_from_another_event_loop_closes_it_too(self, client, checks):
        """The store driven from the test's own loop and thread, not the app's,
        so the wake has to cross into the socket's loop."""
        cookie = _sign_in(client)
        row = _session_of(cookie)

        with _owner_feed(client, cookie, checks) as (ws, server_side):
            started = time.monotonic()
            assert asyncio.run(sessions.revoke_session(row.user_id, row.id)) is True

            _assert_closed_promptly(ws, server_side, row.user_id, started)

    def test_a_sign_out_between_the_handshake_and_the_watch_closes_it(self, client, checks, monkeypatch):
        """It wakes nobody, as nothing watches the account yet: the guard's
        first check, made at once rather than after the clock, is what sees it."""
        cookie = _sign_in(client)
        row = _session_of(cookie)
        nodes_of = streaming.get_user_nodes

        async def signed_out_meanwhile(user_id: str) -> list[str]:
            assert await sessions.revoke_session(row.user_id, row.id) is True
            return await nodes_of(user_id)

        monkeypatch.setattr(streaming, "get_user_nodes", signed_out_meanwhile)

        started = time.monotonic()
        with client.websocket_connect("/ws/aircraft/owner", headers=_as(cookie)) as ws:
            assert _close_code(ws) == 1008
            assert time.monotonic() - started < PROMPTLY
            assert state.ws_owner_clients == {}
            assert sessions._watches == {}

    def test_every_socket_on_the_cookie_closes_and_the_watch_goes_with_the_last(self, client, checks):
        cookie = _sign_in(client)
        user_id = _session_of(cookie).user_id

        with _owner_feed(client, cookie, checks) as (first, first_server_side):
            with _owner_feed(client, cookie, checks) as (second, second_server_side):
                assert len(sessions._watches[str(user_id)]) == 2
                started = time.monotonic()
                client.post("/api/auth/logout", headers=_as(cookie))

                assert (_close_code(first), _close_code(second)) == (1008, 1008)
                assert time.monotonic() - started < PROMPTLY
                assert state.ws_owner_clients == {}
                assert sessions._watches == {}


# ── Another session ending leaves it streaming ───────────────────────────────


class TestAnotherSessionEnding:
    def test_revoking_another_session_of_the_account_leaves_it_streaming(self, client, checks):
        laptop = _sign_in(client)
        phone = _sign_in(client, PHONE)
        row = _session_of(laptop)
        ref = _own_node(row.user_id)

        with _owner_feed(client, laptop, checks) as (ws, server_side):
            checked = len(checks.results)
            r = client.delete(f"/api/auth/me/sessions/{_session_of(phone).id}", headers=_as(laptop))
            assert r.json() == {"ok": True}

            # Woken and checked again, and found still signed in: with the
            # clock an hour off, nothing but the revocation wakes it.
            _eventually(lambda: len(checks.results) > checked, "a check after the revocation")
            assert checks.results[-1] is not None
            _broadcast(client)

            frame = _next_frame(ws)
            assert frame["now"] == FRAME_NOW
            assert [(ac["hex"], ac["node_ref"]) for ac in frame["aircraft"]] == [("abc123", ref)]
            assert server_side in state.ws_owner_clients
            assert str(row.user_id) in sessions._watches


# ── JWT cookies from before sessions ─────────────────────────────────────────


class TestALegacyCookieSocket:
    @pytest.mark.parametrize("sign_out", ["revoke-others", "deactivate"])
    def test_it_closes_when_the_account_signs_out_everywhere(self, client, checks, sign_out):
        account = asyncio.run(users.get_or_create_magic_link_user(OWNER))
        legacy = _legacy_cookie(account)
        elsewhere = _sign_in(client, PHONE)

        with _owner_feed(client, legacy, checks) as (ws, server_side):
            started = time.monotonic()
            if sign_out == "revoke-others":
                r = client.post("/api/auth/me/sessions/revoke-others", headers=_as(elsewhere))
                # No session ended: the JWT was all the account had open.
                assert r.json() == {"revoked": 0}
            else:
                r = client.post(f"/api/admin/users/{account.id}/deactivate", headers=AS_ADMIN)
                assert r.status_code == 200

            _assert_closed_promptly(ws, server_side, account.id, started)

    def test_once_exchanged_it_closes_with_the_session_it_became(self, client, checks):
        """The console's next load swaps the JWT for a session, which the
        socket's copy of the JWT then aliases."""
        account = asyncio.run(users.get_or_create_magic_link_user(OWNER))
        legacy = _legacy_cookie(account)
        elsewhere = _sign_in(client, PHONE)

        with _owner_feed(client, legacy, checks) as (ws, server_side):
            exchanged = client.get("/api/auth/me", headers=_as(legacy))
            fresh = SimpleCookie(exchanged.headers["set-cookie"])[sessions.COOKIE_NAME].value
            started = time.monotonic()
            r = client.delete(f"/api/auth/me/sessions/{_session_of(fresh).id}", headers=_as(elsewhere))

            assert r.json() == {"ok": True}
            _assert_closed_promptly(ws, server_side, account.id, started)


# ── The check on the clock ───────────────────────────────────────────────────


async def _delete_session(session_id: str) -> None:
    """Delete a session's row straight from the table, which wakes nobody."""
    async with async_session_maker() as db:
        await db.execute(delete(UserSession).where(UserSession.id == session_id))
        await db.commit()


async def _backdate(session_id: str, user_id: str, to: datetime) -> None:
    """Make the session and the account look last used at `to`, as another process would."""
    async with async_session_maker() as db:
        await db.execute(update(UserSession).where(UserSession.id == session_id).values(last_used_at=to))
        await db.execute(update(User).where(User.id == uuid.UUID(user_id)).values(last_seen_at=to))
        await db.commit()


class TestTheCheckOnTheClock:
    @pytest.fixture(autouse=True)
    def _often(self, monkeypatch):
        monkeypatch.setattr(streaming, "SESSION_RECHECK_S", 0.2)

    def test_a_session_deleted_behind_the_stores_back_closes_it_at_the_next_check(self, client, checks):
        cookie = _sign_in(client)
        row = _session_of(cookie)

        with _owner_feed(client, cookie, checks) as (ws, server_side):
            started = time.monotonic()
            asyncio.run(_delete_session(row.id))

            _assert_closed_promptly(ws, server_side, row.user_id, started)

    def test_the_checks_neither_slide_the_session_nor_record_a_visit(self, client, checks):
        cookie = _sign_in(client)
        row = _session_of(cookie)
        # Past both the hour a use waits to slide a session and the minutes a
        # visit waits to be recorded, so a check that counted as a use would
        # write both.
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)

        with _owner_feed(client, cookie, checks) as (_ws, server_side):
            asyncio.run(_backdate(row.id, row.user_id, two_hours_ago))
            before = _session_of(cookie)
            checked = len(checks.results)
            _eventually(lambda: len(checks.results) >= checked + 3, "three more checks")

            after = _session_of(cookie)
            assert all(result is not None for result in checks.results)
            assert (after.last_used_at, after.expires_at) == (before.last_used_at, before.expires_at)
            assert _account(row.user_id).last_seen_at == before.last_used_at
            assert server_side in state.ws_owner_clients

        # Whereas a use of the cookie moves both, so the stamps held still
        # above for the checks' sake rather than for want of a reason to move.
        assert client.get("/api/auth/me", headers=_as(cookie)).status_code == 200
        assert _session_of(cookie).last_used_at > before.last_used_at
        last_seen = _account(row.user_id).last_seen_at
        assert last_seen is not None and last_seen > before.last_used_at

    def test_a_check_that_fails_keeps_the_socket_until_one_reads_it_signed_out(self, client, checks, caplog):
        laptop = _sign_in(client)
        phone = _sign_in(client, PHONE)
        row = _session_of(laptop)

        with _owner_feed(client, laptop, checks) as (ws, server_side):
            checks.failing = True
            # Its own session ended, and woken for it, but no check can read that.
            r = client.delete(f"/api/auth/me/sessions/{row.id}", headers=_as(phone))
            assert r.json() == {"ok": True}
            _eventually(lambda: checks.failures() >= 3, "three failed checks")

            assert server_side in state.ws_owner_clients
            _broadcast(client)
            assert _next_frame(ws)["now"] == FRAME_NOW
            assert "Could not check an owner feed's session" in caplog.text

            started = time.monotonic()
            checks.failing = False

            _assert_closed_promptly(ws, server_side, row.user_id, started)


# ── What a socket leaves behind ──────────────────────────────────────────────


class TestNothingOutlivesTheSocket:
    def test_a_socket_the_client_closes_leaves_no_watch(self, client, checks):
        cookie = _sign_in(client)
        user_id = _session_of(cookie).user_id

        with _owner_feed(client, cookie, checks):
            assert len(sessions._watches[str(user_id)]) == 1
            assert _guards(client) == 1

        _eventually(lambda: not state.ws_owner_clients and not sessions._watches, "the handler to let go")
        assert _guards(client) == 0

    def test_a_socket_the_bypass_admits_is_not_watched(self, client, monkeypatch):
        """The anonymous admin holds no session that could end."""
        monkeypatch.setattr(streaming, "AUTH_BYPASS", True)

        with client.websocket_connect("/ws/aircraft/owner"):
            _eventually(lambda: len(state.ws_owner_clients) == 1, "the socket admitted")
            assert sessions._watches == {}

    def test_a_watch_whose_loop_has_closed_is_dropped_by_the_next_wake(self):
        user_id = uuid.uuid4()

        async def _register() -> sessions.AccountWatch:
            return sessions.watch_account(user_id)

        asyncio.run(_register())
        assert str(user_id) in sessions._watches

        sessions._wake_watches(user_id)

        assert sessions._watches == {}

    def test_a_wake_from_another_thread_reaches_a_loop_with_nothing_else_to_do(self):
        """An idle loop sleeps in select() until a callback is handed to it
        thread-safely, so a wake that only set the event would never be seen."""
        user_id = uuid.uuid4()
        waiting = threading.Event()
        woken = []

        async def _wait() -> None:
            watch = sessions.watch_account(user_id)
            # Runs once the wait below has parked the loop.
            asyncio.get_running_loop().call_soon(waiting.set)
            try:
                woken.append(await watch.wait(60))
            finally:
                watch.release()

        thread = threading.Thread(target=asyncio.run, args=(_wait(),), daemon=True)
        thread.start()
        assert waiting.wait(PROMPTLY)

        sessions._wake_watches(user_id)
        thread.join(PROMPTLY)

        assert woken == [True]
        assert sessions._watches == {}


# ── Woken only once the write has committed ──────────────────────────────────


def _committed(session_id: str, user_id: str) -> tuple[int, int]:
    """The session's rows and the account's is_active, as another connection reads them.

    WAL hands a reader only what has committed, so this sees a write in
    progress as not yet made.
    """
    with closing(sqlite3.connect(_DB_PATH)) as db:
        (rows,) = db.execute("SELECT count(*) FROM user_sessions WHERE id = ?", (session_id,)).fetchone()
        (active,) = db.execute('SELECT is_active FROM "user" WHERE id = ?', (user_id,)).fetchone()
    return rows, active


@pytest.mark.parametrize("sign_out", ["end", "revoke_session", "revoke_other_sessions", "deactivate_account"])
async def test_the_account_is_woken_only_once_the_sign_out_has_committed(monkeypatch, sign_out):
    account = await users.get_or_create_magic_link_user(OWNER)
    token, row = await sessions.open_session(account, user_agent=LAPTOP, ip=None)
    seen = []
    monkeypatch.setattr(
        sessions, "_wake_watches", lambda user_id: seen.append((user_id, _committed(row.id, row.user_id)))
    )

    if sign_out == "end":
        resolved = await sessions.resolve(token)
        assert resolved is not None
        await sessions.end(resolved)
    elif sign_out == "revoke_session":
        assert await sessions.revoke_session(account.id, row.id) is True
    elif sign_out == "revoke_other_sessions":
        assert await sessions.revoke_other_sessions(account.id, None) == 1
    else:
        assert await sessions.deactivate_account(account.id) is not None

    ((woken, (rows, active)),) = seen
    assert str(woken) == str(account.id)
    assert rows == 0
    assert active == (0 if sign_out == "deactivate_account" else 1)


async def test_a_revocation_that_ends_nothing_wakes_nobody(monkeypatch):
    account = await users.get_or_create_magic_link_user(OWNER)
    seen = []
    monkeypatch.setattr(sessions, "_wake_watches", seen.append)

    assert await sessions.revoke_session(account.id, str(uuid.uuid4())) is False

    assert seen == []
