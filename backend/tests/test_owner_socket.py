"""The owner feed, /ws/aircraft/owner, on real session cookies.

It signs in on the session cookie alone, and keeps to what that session's
account owns for as long as the socket is open: ownership is read again before
every broadcast, for every connection in one query (services/owner_feed.py),
and a session that stops resolving has its socket closed (routes/streaming.py,
_close_when_signed_out).

A broadcast tick is broadcast_aircraft called on the TestClient's own event
loop, the one its sockets live on, so each frame a socket receives is one the
test sent. The node ids carry the synthetic prefix, which publishes them as
themselves: what is asserted is which entries a socket is served, not node_ref
resolution.
"""

import asyncio
import hashlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from sqlalchemy import event, select, update
from starlette.websockets import WebSocketDisconnect

from core import sessions, state
from core.auth import get_user_nodes
from core.users import ANONYMOUS_USER, User, UserSession, async_session_maker, engine, normalise_email
from services.tasks.aircraft_flush import broadcast_aircraft
from tests.owner_helpers import as_user, real_sessions, recheck_sessions_every, sign_in, user_id_of
from tests.ownership_helpers import own, own_registered_node, set_node_owner

_WS_TOKEN = "shared-ws-token"
_OWNER_FEED = "/ws/aircraft/owner"

_KEPT = "test-owner-kept"
_RELEASED = "test-owner-released"
_GAINED = "test-owner-gained"
_OTHERS = "test-someone-elses"


def _frame(*node_ids: str) -> dict:
    """One aircraft per node, its hex the node id, so a frame reads as the nodes it carries."""
    return {
        "now": 1.0,
        "aircraft": [{"hex": nid, "node_id": nid} for nid in node_ids],
        "detection_arcs": [],
        "ground_truth": {},
    }


def _nodes(text: str) -> list[str]:
    """The nodes a served frame carries, by the hex _frame gave each."""
    return sorted(ac["hex"] for ac in orjson.loads(text)["aircraft"])


def _tick(client, frame: dict) -> None:
    """One broadcast, on the loop the client's sockets run on."""
    assert client.portal is not None
    client.portal.call(broadcast_aircraft, frame)


def _wait_for(condition, timeout_s: float = 2.0) -> float:
    """Poll until `condition()` holds and return how long that took; fail at `timeout_s`."""
    started = time.monotonic()
    while not condition():
        if time.monotonic() - started > timeout_s:
            raise AssertionError(f"not within {timeout_s}s")
        time.sleep(0.005)
    return time.monotonic() - started


def _deactivate(email: str) -> None:
    async def _go():
        async with async_session_maker() as session:
            await session.execute(update(User).where(User.email == normalise_email(email)).values(is_active=False))
            await session.commit()

    asyncio.run(_go())


def _session_watchers(client) -> int:
    """How many session guards (routes/streaming.py) are running on the client's loop."""
    from routes import streaming

    async def _count() -> int:
        name = streaming._close_when_signed_out.__name__
        return sum(1 for task in asyncio.all_tasks() if getattr(task.get_coro(), "__name__", "") == name)

    assert client.portal is not None
    return client.portal.call(_count)


@pytest.fixture(autouse=True)
def _only_the_tests_frames(monkeypatch):
    """No flush of the lifespan's own reaches a socket, and no snapshot unless a test sets one."""
    monkeypatch.setattr(state, "aircraft_dirty", False)
    monkeypatch.setattr(state, "latest_aircraft_json", {"now": 0, "aircraft": [], "messages": 0})


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


@pytest.fixture
def ws_token(monkeypatch):
    """The shared token the public feeds require, set."""
    from routes import streaming

    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", _WS_TOKEN)


@pytest.fixture
def no_ws_token(monkeypatch):
    from routes import streaming

    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")


# ── (a) The cookie alone ─────────────────────────────────────────────────────


class TestTheCookieAloneSignsIn:
    def test_the_owner_feed_opens_on_the_cookie_with_the_shared_token_set(self, client, real, ws_token, monkeypatch):
        token = sign_in(client, "a@example.com")
        own(_KEPT, user_id_of("a@example.com"))
        monkeypatch.setattr(state, "latest_aircraft_json", _frame(_KEPT, _OTHERS))
        as_user(client, token)

        with client.websocket_connect(_OWNER_FEED) as ws:
            assert _nodes(ws.receive_text()) == [_KEPT]
            _tick(client, _frame(_KEPT, _OTHERS))
            assert _nodes(ws.receive_text()) == [_KEPT]

    @pytest.mark.parametrize("path", ["/ws/aircraft", "/ws/aircraft/live"])
    def test_a_public_feed_still_refuses_a_session_without_the_token(self, client, real, ws_token, path):
        as_user(client, sign_in(client, "a@example.com"))
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(path) as ws:
                ws.receive_text()
        assert refused.value.code == 1008

    @pytest.mark.parametrize("path", ["/ws/aircraft", "/ws/aircraft/live"])
    def test_a_public_feed_still_opens_with_the_token(self, client, real, ws_token, path):
        """The control for the refusal above: the token is what it was missing."""
        with client.websocket_connect(f"{path}?token={_WS_TOKEN}"):
            pass

    @pytest.mark.parametrize("cookie", [None, "not-a-session"])
    def test_the_owner_feed_refuses_a_caller_without_a_valid_session(self, client, real, ws_token, cookie):
        as_user(client, cookie)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(_OWNER_FEED):
                pass
        assert refused.value.code == 1008
        assert not state.ws_owner_clients

    def test_the_shared_token_is_not_a_session(self, client, real, ws_token):
        as_user(client, None)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(f"{_OWNER_FEED}?token={_WS_TOKEN}"):
                pass
        assert refused.value.code == 1008

    def test_a_deactivated_account_is_refused(self, client, real, no_ws_token):
        token = sign_in(client, "a@example.com")
        _deactivate("a@example.com")
        as_user(client, token)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(_OWNER_FEED):
                pass
        assert refused.value.code == 1008


# ── (b) Ownership followed while open ────────────────────────────────────────


class TestOwnershipIsFollowedWhileOpen:
    def test_a_released_node_stops_on_the_next_frame_and_a_kept_one_does_not(
        self, client, real, no_ws_token, monkeypatch
    ):
        token = sign_in(client, "a@example.com")
        owner = user_id_of("a@example.com")
        own(_KEPT, owner)
        own(_RELEASED, owner)
        frame = _frame(_KEPT, _RELEASED, _OTHERS)
        monkeypatch.setattr(state, "latest_aircraft_json", frame)
        as_user(client, token)

        with client.websocket_connect(_OWNER_FEED) as ws:
            assert _nodes(ws.receive_text()) == sorted([_KEPT, _RELEASED])
            asyncio.run(set_node_owner(_RELEASED, None))
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == [_KEPT]
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == [_KEPT]

    def test_a_node_gained_while_open_starts_on_the_next_frame(self, client, real, no_ws_token):
        token = sign_in(client, "a@example.com")
        owner = user_id_of("a@example.com")
        own(_KEPT, owner)
        frame = _frame(_KEPT, _GAINED)
        as_user(client, token)

        with client.websocket_connect(_OWNER_FEED) as ws:
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == [_KEPT]
            own(_GAINED, owner)
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == sorted([_KEPT, _GAINED])

    def test_the_bypass_admin_follows_the_all_zero_accounts_rows(self, client, no_ws_token):
        """The suite's own mode: AUTH_BYPASS on, no cookie, the anonymous admin."""
        own(_KEPT, ANONYMOUS_USER["id"])
        own(_RELEASED, ANONYMOUS_USER["id"])
        frame = _frame(_KEPT, _RELEASED, _OTHERS)
        as_user(client, None)

        with client.websocket_connect(_OWNER_FEED) as ws:
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == sorted([_KEPT, _RELEASED])
            asyncio.run(set_node_owner(_RELEASED, None))
            _tick(client, frame)
            assert _nodes(ws.receive_text()) == [_KEPT]


# ── (c) A session that stops resolving ───────────────────────────────────────


class TestAnEndedSessionClosesTheSocket:
    """The check on the clock, for a session that ends where no sign-out wakes
    the socket: the account deactivated by a write of its own, or the session
    gone. A sign-out through core/sessions.py closes it at once
    (tests/test_owner_socket_sessions.py)."""

    RECHECK_S = 0.02

    @pytest.fixture(autouse=True)
    def _fast_recheck(self, monkeypatch):
        recheck_sessions_every(monkeypatch, self.RECHECK_S)

    @pytest.fixture
    def checks(self, monkeypatch):
        """Each session check's verdict, True for a session still good.

        A check reads without sliding; a resolve that slides, as the handshake's
        and every route's do, is passed through and not counted.
        """
        verdicts: list[bool] = []
        resolve = sessions.resolve

        async def _counted(token, **kwargs):
            resolved = await resolve(token, **kwargs)
            if not kwargs.get("slide", True):
                verdicts.append(resolved is not None and resolved.user.is_active)
            return resolved

        monkeypatch.setattr(sessions, "resolve", _counted)
        return verdicts

    def _open(self, client, email: str) -> str:
        token = sign_in(client, email)
        own(_KEPT, user_id_of(email))
        as_user(client, token)
        return token

    def test_a_deactivated_account_is_closed_within_the_recheck(self, client, real, no_ws_token, checks):
        self._open(client, "a@example.com")
        with client.websocket_connect(_OWNER_FEED) as ws:
            # A good session survives its checks and goes on streaming.
            _wait_for(lambda: len(checks) >= 2)
            assert all(checks)
            _tick(client, _frame(_KEPT))
            assert _nodes(ws.receive_text()) == [_KEPT]

            _deactivate("a@example.com")
            took = _wait_for(lambda: not state.ws_owner_clients)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008
        assert checks[-1] is False
        # Within the bound: one recheck interval and one lookup, with room for a
        # loaded machine but nowhere near the 30 s default.
        assert took < self.RECHECK_S + 1.0

    def test_a_session_that_stops_resolving_is_closed(self, client, real, no_ws_token, monkeypatch):
        """A session ended elsewhere (signed out, expired) resolves to None."""
        calls: list[str] = []
        resolve = sessions.resolve

        async def _ended_after_two(token, **kwargs):
            if kwargs.get("slide", True):
                return await resolve(token, **kwargs)
            calls.append(token)
            return None if len(calls) > 2 else await resolve(token, **kwargs)

        monkeypatch.setattr(sessions, "resolve", _ended_after_two)
        self._open(client, "a@example.com")
        with client.websocket_connect(_OWNER_FEED) as ws:
            _wait_for(lambda: not state.ws_owner_clients)
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 1008
        assert len(calls) == 3

    def test_the_checks_are_no_use_of_the_session(self, client, real, no_ws_token, checks):
        """An open socket is not its owner at the console: however often its
        session is checked, the session is not slid and no visit is recorded.
        Backdated past the hour a use waits to slide a session and the minutes
        a visit waits to be recorded, so a check that counted as a use would
        write both."""
        token = self._open(client, "a@example.com")
        user_id = user_id_of("a@example.com")
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)

        with client.websocket_connect(_OWNER_FEED):
            _wait_for(lambda: len(state.ws_owner_clients) == 1)
            _backdate(token, user_id, two_hours_ago)
            before = _stamps(token, user_id)
            checked = len(checks)
            _wait_for(lambda: len(checks) >= checked + 5)

            assert all(checks)
            assert _stamps(token, user_id) == before
            assert len(state.ws_owner_clients) == 1

        # Whereas a use of the cookie moves both, so the stamps held still above
        # for the checks' sake and not for want of a reason to move.
        assert client.get("/api/auth/me").status_code == 200
        last_used_at, _, last_seen_at = _stamps(token, user_id)
        assert last_used_at > before[0]
        assert last_seen_at is not None and last_seen_at > before[0]

    def test_no_frame_reaches_a_socket_once_its_session_ended(self, client, real, no_ws_token, checks):
        self._open(client, "a@example.com")
        with client.websocket_connect(_OWNER_FEED) as ws:
            _deactivate("a@example.com")
            _wait_for(lambda: not state.ws_owner_clients)
            _tick(client, _frame(_KEPT))
            # The close, and not a frame, is the next thing the socket gets.
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_each_session_socket_has_one_watcher_and_it_ends_with_the_socket(self, client, real, no_ws_token):
        self._open(client, "a@example.com")
        with client.websocket_connect(_OWNER_FEED):
            assert _session_watchers(client) == 1
        _wait_for(lambda: _session_watchers(client) == 0)

    def test_the_bypass_admin_has_no_session_to_watch(self, client, no_ws_token):
        as_user(client, None)
        with client.websocket_connect(_OWNER_FEED):
            _wait_for(lambda: len(state.ws_owner_clients) == 1)
            assert _session_watchers(client) == 0


def _session_row(token: str):
    return select(UserSession).where(UserSession.token_hash == hashlib.sha256(token.encode()).hexdigest())


def _backdate(token: str, user_id: str, to: datetime) -> None:
    """Make the session and its account look last used at `to`."""

    async def _go():
        async with async_session_maker() as session:
            row = (await session.execute(_session_row(token))).scalars().one()
            row.last_used_at = to
            await session.execute(update(User).where(User.id == uuid.UUID(user_id)).values(last_seen_at=to))
            await session.commit()

    asyncio.run(_go())


def _stamps(token: str, user_id: str) -> tuple[datetime, datetime, datetime | None]:
    """The session's last use and expiry, and the account's last visit."""

    async def _read():
        async with async_session_maker() as session:
            row = (await session.execute(_session_row(token))).scalars().one()
            account = await session.get(User, uuid.UUID(user_id))
            assert account is not None
            return row.last_used_at, row.expires_at, account.last_seen_at

    return asyncio.run(_read())


# ── (d) One ownership read per frame, for every connection ───────────────────


class _NodeClaimReads:
    """The node_claims SELECTs the app's engine runs while it is listening.

    `batched` is the owner feed's read (user_id IN ...), `per_account` the
    shape core.auth.get_user_nodes issues (user_id = ?).
    """

    def __init__(self):
        self.batched: list[tuple] = []
        self.per_account: list[tuple] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        if "FROM node_claims" not in statement or not statement.lstrip().upper().startswith("SELECT"):
            return
        if "node_claims.user_id IN" in statement:
            self.batched.append(tuple(parameters))
        elif "node_claims.user_id = " in statement:
            self.per_account.append(tuple(parameters))

    def __enter__(self):
        event.listen(engine.sync_engine, "before_cursor_execute", self)
        return self

    def __exit__(self, *exc):
        event.remove(engine.sync_engine, "before_cursor_execute", self)


class TestOneReadPerFrame:
    _A = "test-owner-a"
    _B = "test-owner-b"
    _MOVED = "test-owner-moved"

    def test_two_accounts_on_three_sockets_are_read_in_one_query_per_frame(self, client, real, no_ws_token):
        token_a = sign_in(client, "a@example.com")
        token_b = sign_in(client, "b@example.com")
        user_a, user_b = user_id_of("a@example.com"), user_id_of("b@example.com")
        own(self._A, user_a)
        own(self._MOVED, user_a)
        own(self._B, user_b)
        frame = _frame(self._A, self._B, self._MOVED)

        per_frame: list[tuple[int, int]] = []
        with _NodeClaimReads() as reads:

            def tick() -> None:
                batched, per_account = len(reads.batched), len(reads.per_account)
                _tick(client, frame)
                per_frame.append((len(reads.batched) - batched, len(reads.per_account) - per_account))

            as_user(client, token_a)
            with client.websocket_connect(_OWNER_FEED) as a1, client.websocket_connect(_OWNER_FEED) as a2:
                as_user(client, token_b)
                with client.websocket_connect(_OWNER_FEED) as b1:
                    tick()
                    assert _nodes(a1.receive_text()) == sorted([self._A, self._MOVED])
                    assert _nodes(a2.receive_text()) == sorted([self._A, self._MOVED])
                    assert _nodes(b1.receive_text()) == [self._B]

                    # Handed from one account to the other: both see it on the
                    # same frame, from the same single read.
                    asyncio.run(set_node_owner(self._MOVED, user_b))
                    tick()
                    assert _nodes(a1.receive_text()) == [self._A]
                    assert _nodes(a2.receive_text()) == [self._A]
                    assert _nodes(b1.receive_text()) == sorted([self._B, self._MOVED])

        # One batched read per frame and no per-account ones, for three sockets.
        assert per_frame == [(1, 0), (1, 0)]
        # Each read named both accounts once: distinct accounts, not sockets.
        for parameters in reads.batched[-2:]:
            assert sorted(parameters) == sorted([user_a, user_b])


# ── The read itself ──────────────────────────────────────────────────────────


class _Socket:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True


class TestOwnedBy:
    async def test_it_answers_what_get_user_nodes_answers_for_each_account(self):
        from services import owner_feed

        await own_registered_node("test-a1", "account-a")
        await own_registered_node("test-a2", "account-a")
        await own_registered_node("test-b1", "account-b")
        await own_registered_node("test-free", "account-c")
        await set_node_owner("test-free", None)

        accounts = ["account-a", "account-b", "account-c", "account-none"]
        owned = await owner_feed.owned_by(accounts)

        assert set(owned) == set(accounts)
        for account in accounts:
            assert owned[account] == set(await get_user_nodes(account))
        assert owned["account-a"] == {"test-a1", "test-a2"}
        assert owned["account-c"] == set()
        assert owned["account-none"] == set()

    async def test_no_accounts_is_no_query(self):
        from services import owner_feed

        with _NodeClaimReads() as reads:
            assert await owner_feed.owned_by([]) == {}
        assert reads.batched == []


class TestTheReadIsBounded:
    @pytest.fixture(autouse=True)
    def _short_wait(self, monkeypatch):
        from services import owner_feed

        monkeypatch.setattr(owner_feed, "OWNERSHIP_WAIT_S", 0.05)

    async def test_a_stalled_read_does_not_hold_the_frame_and_lands_for_the_next(self, monkeypatch):
        from services import owner_feed

        answer = asyncio.Event()

        async def _stalled(user_ids):
            await answer.wait()
            return {user_id: set() for user_id in user_ids}

        monkeypatch.setattr(owner_feed, "owned_by", _stalled)
        socket = _Socket()
        owner_feed.admit(socket, "account-a", {"test-a"})
        try:
            started = time.monotonic()
            await broadcast_aircraft(_frame("test-a"))
            assert time.monotonic() - started < 1.0
            # Served from the set it already held.
            assert _nodes(socket.sent[0]) == ["test-a"]

            answer.set()
            for _ in range(100):
                if state.ws_owner_clients[socket] == set():
                    break
                await asyncio.sleep(0)
            await broadcast_aircraft(_frame("test-a"))
            assert _nodes(socket.sent[1]) == []
        finally:
            owner_feed.forget(socket)

    async def test_a_failed_read_keeps_the_last_known_sets_and_the_next_frame_reads_again(self, monkeypatch):
        from services import owner_feed

        reads: list[set[str]] = []

        async def _failing(user_ids):
            reads.append(set(user_ids))
            raise RuntimeError("database is locked")

        monkeypatch.setattr(owner_feed, "owned_by", _failing)
        socket = _Socket()
        owner_feed.admit(socket, "account-a", {"test-a"})
        try:
            await broadcast_aircraft(_frame("test-a"))
            await broadcast_aircraft(_frame("test-a"))
        finally:
            owner_feed.forget(socket)
        assert reads == [{"account-a"}, {"account-a"}]
        assert [_nodes(text) for text in socket.sent] == [["test-a"], ["test-a"]]

    async def test_a_socket_that_closes_during_the_read_is_not_brought_back(self, monkeypatch):
        from services import owner_feed

        leaving = _Socket()

        async def _slow(user_ids):
            owner_feed.forget(leaving)
            return {user_id: {"test-a"} for user_id in user_ids}

        monkeypatch.setattr(owner_feed, "owned_by", _slow)
        staying = _Socket()
        owner_feed.admit(leaving, "account-a", set())
        owner_feed.admit(staying, "account-a", set())
        try:
            await broadcast_aircraft(_frame("test-a"))
        finally:
            owner_feed.forget(staying)
        assert leaving not in state.ws_owner_clients
        assert leaving.sent == []
        assert _nodes(staying.sent[0]) == ["test-a"]

    async def test_a_socket_with_no_account_keeps_its_own_set_and_costs_no_read(self, monkeypatch):
        """A connection registered straight on state, as the broadcast tests do."""
        from services import owner_feed

        async def _unexpected(user_ids):
            raise AssertionError("read with no account to read for")

        monkeypatch.setattr(owner_feed, "owned_by", _unexpected)
        socket = _Socket()
        state.ws_owner_clients[socket] = {"test-a"}
        try:
            await broadcast_aircraft(_frame("test-a"))
        finally:
            state.ws_owner_clients.pop(socket, None)
        assert _nodes(socket.sent[0]) == ["test-a"]


class TestTheSessionCheckIsOffTheBroadcastPath:
    async def test_a_stalled_session_lookup_does_not_hold_a_frame(self, monkeypatch):
        from routes import streaming
        from services import owner_feed

        looked_up = asyncio.Event()

        async def _stalled(token, **kwargs):
            looked_up.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(sessions, "resolve", _stalled)
        account = uuid.uuid4()
        # Owned for real: the broadcast reads ownership before it sends.
        await own_registered_node("test-a", str(account))
        socket = _Socket()
        owner_feed.admit(socket, str(account), {"test-a"})
        watch = sessions.watch_account(account)
        guard = asyncio.create_task(streaming._close_when_signed_out(socket, "token", account, watch))
        try:
            await asyncio.wait_for(looked_up.wait(), timeout=1.0)
            await asyncio.wait_for(broadcast_aircraft(_frame("test-a")), timeout=1.0)
        finally:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
            watch.release()
            owner_feed.forget(socket)
        assert _nodes(socket.sent[0]) == ["test-a"]
        assert socket.closed is False
