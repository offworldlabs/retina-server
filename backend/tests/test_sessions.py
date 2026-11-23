"""The session store in core/sessions.py.

Driven directly, with the clock passed in: opening, resolving and sliding a
session, ending one, and the fastapi-users JWT cookies from before sessions,
accepted read-only until exchanged. The routes that use the store are tested
with them.
"""

import asyncio
import hashlib
import os
import string
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from http.cookies import Morsel, SimpleCookie

import jwt
import pytest
from fastapi import Response
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import OperationalError
from starlette.requests import Request
from starlette.websockets import WebSocket

from core import sessions
from core.sessions import (
    ABSOLUTE_LIFETIME,
    COOKIE_NAME,
    IDLE_LIFETIME,
    LEGACY_TOKEN_LIFETIME,
    REFRESH_INTERVAL,
    InactiveAccount,
    Resolved,
    clear_session_cookie,
    coarse_ip,
    deactivate_account,
    describe_user_agent,
    end,
    exchange_legacy,
    list_sessions,
    open_session,
    reactivate_account,
    resolve,
    resolve_request,
    revoke_other_sessions,
    revoke_session,
    set_session_cookie,
)
from core.users import JWT_SECRET, RetiredLegacyToken, User, UserSession, async_session_maker, engine
from tests.migration_helpers import BACKEND

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
FIREFOX_ON_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
MINUTE = timedelta(minutes=1)


async def _account(email: str = "ada@example.com") -> User:
    user = User(id=uuid.uuid4(), email=email, hashed_password="unused", is_active=True)
    async with async_session_maker() as db:
        async with db.begin():
            db.add(user)
    return user


async def _set_active(user: User, active: bool) -> None:
    """Flip the flag on the row alone, as another process would."""
    async with async_session_maker() as db:
        await db.execute(update(User).where(User.id == user.id).values(is_active=active))
        await db.commit()


async def _open(user: User, *, at: datetime = T0) -> tuple[str, UserSession]:
    return await open_session(user, user_agent=FIREFOX_ON_WINDOWS, ip="203.0.113.0/24", now=at)


async def _stored(model) -> list:
    async with async_session_maker() as db:
        return list(await db.scalars(select(model)))


def _legacy_cookie(
    user: User,
    *,
    issued_at: datetime = T0,
    secret: str = JWT_SECRET,
    aud: tuple[str, ...] = ("fastapi-users:auth",),
    **claims,
) -> str:
    """A cookie as fastapi-users minted one: fastapi_users.jwt.generate_jwt adds
    only `exp`, seven days on, to JWTStrategy's `sub` and `aud`."""
    payload = {"sub": str(user.id), "aud": list(aud), "exp": issued_at + LEGACY_TOKEN_LIFETIME} | claims
    return jwt.encode(payload, secret, "HS256")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cookie(response: Response) -> Morsel:
    jar = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    return jar[COOKIE_NAME]


@pytest.fixture
def writes():
    """Every INSERT, UPDATE and DELETE the app's engine sends, in order."""
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


# ── Storage ──────────────────────────────────────────────────────────────────


async def test_opening_stores_the_tokens_hash_and_never_the_token():
    ada = await _account()
    token, row = await _open(ada)

    assert row.token_hash == _hash(token)
    async with async_session_maker() as db:
        stored = [
            *(await db.execute(text("SELECT * FROM user_sessions"))).all(),
            *(await db.execute(text("SELECT * FROM retired_legacy_tokens"))).all(),
        ]
    assert stored
    assert not any(token in str(value) for stored_row in stored for value in stored_row)


async def test_every_column_is_filled():
    ada = await _account()
    _, row = await open_session(ada, user_agent=FIREFOX_ON_WINDOWS, ip=coarse_ip("203.0.113.57"), now=T0)

    (stored,) = await _stored(UserSession)
    assert stored.id == row.id
    assert uuid.UUID(stored.id).version == 4
    assert stored.user_id == str(ada.id)
    assert len(stored.token_hash) == 64
    assert (stored.created_at, stored.last_used_at, stored.expires_at) == (T0, T0, T0 + IDLE_LIFETIME)
    assert stored.user_agent == "Firefox on Windows"
    assert stored.ip == "203.0.113.0/24"


async def test_an_unknown_user_agent_and_address_are_stored_as_nothing_much():
    ada = await _account()
    await open_session(ada, user_agent=None, ip=None, now=T0)

    (stored,) = await _stored(UserSession)
    assert (stored.user_agent, stored.ip) == ("Unknown", None)


async def test_an_inactive_account_cannot_open_a_session():
    """Checked on the row: the caller's copy of the account still says active."""
    ada = await _account()
    await _set_active(ada, False)

    with pytest.raises(InactiveAccount):
        await _open(ada)
    assert ada.is_active is True
    assert await _stored(UserSession) == []


async def test_an_account_that_is_gone_cannot_open_a_session():
    ghost = User(id=uuid.uuid4(), email="ghost@example.com", hashed_password="unused", is_active=True)

    with pytest.raises(InactiveAccount):
        await _open(ghost)


async def test_deleting_an_account_takes_its_sessions_and_retired_cookies_with_it():
    ada = await _account()
    await revoke_other_sessions(ada.id, None, now=T0)
    await _open(ada)
    assert await _stored(UserSession) and await _stored(RetiredLegacyToken)

    async with async_session_maker() as db:
        await db.execute(delete(User).where(User.id == ada.id))
        await db.commit()

    assert await _stored(UserSession) == []
    assert await _stored(RetiredLegacyToken) == []


# ── Resolving ────────────────────────────────────────────────────────────────


async def test_a_session_token_resolves_to_its_account_and_session():
    ada = await _account()
    token, row = await _open(ada)

    resolved = await resolve(token, now=T0 + MINUTE)

    assert "." not in token
    assert resolved is not None
    assert resolved.user.id == ada.id
    assert resolved.session is not None and resolved.session.id == row.id
    assert (resolved.legacy_hash, resolved.legacy_expires_at) == (None, None)


@pytest.mark.parametrize("token", [None, "", "not-a-token-anyone-issued"])
async def test_an_unknown_token_resolves_to_nothing(token):
    await _open(await _account())

    assert await resolve(token, now=T0) is None


async def test_the_live_session_of_a_deactivated_account_resolves_to_nothing():
    ada = await _account()
    token, _ = await _open(ada)
    await _set_active(ada, False)

    assert await resolve(token, now=T0 + MINUTE) is None
    # Refused, not deleted: the row is still on file.
    assert len(await _stored(UserSession)) == 1


def _request(cookie: str | None) -> Request:
    headers = [(b"cookie", f"{COOKIE_NAME}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def test_a_request_resolves_its_cookie_once():
    ada = await _account()
    token, _ = await open_session(ada, user_agent=None, ip=None)
    request = _request(token)

    first = await resolve_request(request)
    assert first is not None
    await end(first)

    # The answer this request already had stands; a new request sees the end.
    assert await resolve_request(request) is first
    assert await resolve_request(_request(token)) is None


async def test_a_request_without_a_session_is_resolved_once_too(monkeypatch):
    calls = []

    async def _counting(token, *, now=None, slide=True):
        calls.append(token)

    monkeypatch.setattr(sessions, "resolve", _counting)
    request = _request(None)

    assert await resolve_request(request) is None
    assert await resolve_request(request) is None
    assert calls == [None]


async def test_a_websocket_resolves_its_cookie_as_a_request_does():
    ada = await _account()
    token, row = await open_session(ada, user_agent=None, ip=None)

    async def _never():
        raise AssertionError("nothing is sent or received here")

    socket = WebSocket(
        {"type": "websocket", "path": "/ws", "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())]},
        receive=_never,
        send=_never,
    )

    resolved = await resolve_request(socket)
    assert resolved is not None and resolved.session is not None
    assert resolved.session.id == row.id


# ── Sliding expiry ───────────────────────────────────────────────────────────


async def test_a_use_within_the_hour_writes_nothing(writes):
    ada = await _account()
    token, _ = await _open(ada)
    writes.clear()

    resolved = await resolve(token, now=T0 + REFRESH_INTERVAL - timedelta(seconds=1))

    assert resolved is not None and resolved.session is not None
    assert writes == []
    (stored,) = await _stored(UserSession)
    assert (stored.last_used_at, stored.expires_at) == (T0, T0 + IDLE_LIFETIME)


async def test_a_use_after_the_hour_moves_both_stamps():
    ada = await _account()
    token, _ = await _open(ada)
    later = T0 + REFRESH_INTERVAL

    resolved = await resolve(token, now=later)

    assert resolved is not None and resolved.session is not None
    assert (resolved.session.last_used_at, resolved.session.expires_at) == (later, later + IDLE_LIFETIME)
    (stored,) = await _stored(UserSession)
    assert (stored.created_at, stored.last_used_at, stored.expires_at) == (T0, later, later + IDLE_LIFETIME)


@pytest.mark.parametrize(
    ("idle", "live"),
    [
        (IDLE_LIFETIME - timedelta(seconds=1), True),
        (IDLE_LIFETIME, False),
        (IDLE_LIFETIME + timedelta(seconds=1), False),
    ],
)
async def test_seven_idle_days_end_a_session(idle, live):
    ada = await _account()
    token, _ = await _open(ada)

    assert (await resolve(token, now=T0 + idle) is not None) is live


async def test_continuous_use_ends_thirty_days_after_the_sign_in():
    ada = await _account()
    token, _ = await _open(ada)

    for hours in range(23, 30 * 24, 23):
        assert await resolve(token, now=T0 + timedelta(hours=hours)) is not None
    assert await resolve(token, now=T0 + ABSOLUTE_LIFETIME - timedelta(seconds=1)) is not None
    (stored,) = await _stored(UserSession)
    assert stored.expires_at == T0 + ABSOLUTE_LIFETIME

    assert await resolve(token, now=T0 + ABSOLUTE_LIFETIME) is None


async def test_a_failed_slide_is_logged_and_the_session_still_resolves(monkeypatch, caplog):
    ada = await _account()
    token, _ = await _open(ada)

    def _locked(*_args, **_kwargs):
        raise OperationalError("UPDATE user_sessions", {}, Exception("database is locked"))

    monkeypatch.setattr(sessions, "update", _locked)
    resolved = await resolve(token, now=T0 + REFRESH_INTERVAL)

    assert resolved is not None and resolved.session is not None
    assert resolved.session.last_used_at == T0
    assert "Could not extend a session" in caplog.text


# ── The cookie ───────────────────────────────────────────────────────────────


async def test_the_cookie_lasts_until_the_absolute_end():
    ada = await _account()
    token, row = await _open(ada)

    at_open = Response()
    set_session_cookie(at_open, token, row, now=T0)
    cookie = _cookie(at_open)
    assert cookie.value == token
    assert cookie["max-age"] == str(30 * 86400)
    assert (cookie["path"], cookie["samesite"]) == ("/", "lax")
    assert cookie["httponly"] is True and cookie["secure"] is True

    # Used along the way, so the idle end has moved and the cookie's has not.
    assert await resolve(token, now=T0 + timedelta(days=5)) is not None
    later = T0 + timedelta(days=10)
    resolved = await resolve(token, now=later)
    assert resolved is not None and resolved.session is not None
    assert resolved.session.expires_at == later + IDLE_LIFETIME
    again = Response()
    set_session_cookie(again, token, resolved.session, now=later)
    assert _cookie(again)["max-age"] == str(20 * 86400)


def test_clearing_the_cookie_names_the_same_cookie():
    response = Response()

    clear_session_cookie(response)

    cookie = _cookie(response)
    assert (cookie.value, cookie["max-age"]) == ("", "0")
    assert (cookie["path"], cookie["samesite"]) == ("/", "lax")
    assert cookie["httponly"] is True and cookie["secure"] is True


# ── Ending sessions ──────────────────────────────────────────────────────────


async def test_ending_a_session_stops_its_token():
    ada = await _account()
    token, _ = await _open(ada)
    resolved = await resolve(token, now=T0)
    assert resolved is not None

    await end(resolved, now=T0)

    assert await resolve(token, now=T0) is None
    assert await _stored(UserSession) == []
    # Ending it again has nothing left to do.
    await end(resolved, now=T0)


async def test_revoking_a_session_is_scoped_to_its_owner():
    ada = await _account()
    bob = await _account("bob@example.com")
    token, row = await _open(ada)

    assert await revoke_session(bob.id, row.id) is False
    assert await resolve(token, now=T0) is not None

    assert await revoke_session(str(ada.id), row.id) is True
    assert await resolve(token, now=T0) is None
    assert await revoke_session(ada.id, row.id) is False
    assert await revoke_session("not-an-id", row.id) is False


async def test_signing_out_everywhere_else_keeps_only_the_current_session():
    ada = await _account()
    bob = await _account("bob@example.com")
    current, kept = await _open(ada)
    others = [(await _open(ada))[0] for _ in range(2)]
    bobs, _ = await _open(bob)
    adas_cookie, bobs_cookie = _legacy_cookie(ada), _legacy_cookie(bob)
    later = T0 + 5 * MINUTE

    assert await revoke_other_sessions(ada.id, kept.id, now=later) == 2

    resolved = await resolve(current, now=later)
    assert resolved is not None and resolved.session is not None and resolved.session.id == kept.id
    for token in others:
        assert await resolve(token, now=later) is None
    assert await resolve(adas_cookie, now=later) is None
    assert await resolve(bobs, now=later) is not None
    assert await resolve(bobs_cookie, now=later) is not None


async def test_signing_out_everywhere_with_no_session_kept_ends_them_all():
    ada = await _account()
    tokens = [(await _open(ada))[0] for _ in range(2)]

    assert await revoke_other_sessions(str(ada.id), None, now=T0) == 2
    assert await revoke_other_sessions(ada.id, None, now=T0 + MINUTE) == 0

    for token in tokens:
        assert await resolve(token, now=T0 + MINUTE) is None
    # The newer retirement replaces the older rather than piling up beside it.
    (retired,) = await _stored(RetiredLegacyToken)
    assert (retired.token_hash, retired.retired_at) == (None, T0 + MINUTE)


async def test_listing_shows_live_sessions_most_recently_used_first():
    ada = await _account()
    bob = await _account("bob@example.com")
    first_token, first = await _open(ada, at=T0)
    _, second = await _open(ada, at=T0 + timedelta(hours=1))
    # Expired a day ago, and not yet swept.
    await _open(ada, at=T0 - IDLE_LIFETIME - timedelta(days=1))
    await _open(bob)
    now = T0 + timedelta(hours=3)
    await resolve(first_token, now=now)

    listed = await list_sessions(ada.id, now=now)

    assert [row.id for row in listed] == [first.id, second.id]
    assert await list_sessions("not-an-id", now=now) == []


async def test_deactivation_ends_every_session_and_cookie_and_reactivation_revives_none():
    ada = await _account()
    token, _ = await _open(ada)
    exchanged_cookie = _legacy_cookie(ada)
    pending_cookie = _legacy_cookie(ada, issued_at=T0 + MINUTE)
    legacy = await resolve(exchanged_cookie, now=T0 + 2 * MINUTE)
    assert legacy is not None
    exchanged = await exchange_legacy(legacy, user_agent=None, ip=None, now=T0 + 2 * MINUTE)
    assert exchanged is not None
    everything = (token, exchanged[0], exchanged_cookie, pending_cookie)
    later = T0 + 10 * MINUTE

    deactivated = await deactivate_account(ada.id, now=later)

    assert deactivated is not None and deactivated.is_active is False
    assert await _stored(UserSession) == []
    for credential in everything:
        assert await resolve(credential, now=later) is None

    reactivated = await reactivate_account(str(ada.id))

    assert reactivated is not None and reactivated.is_active is True
    for credential in everything:
        assert await resolve(credential, now=later + MINUTE) is None
    # Signing in afresh works.
    fresh, _ = await _open(ada, at=later + 2 * MINUTE)
    assert await resolve(fresh, now=later + 2 * MINUTE) is not None


@pytest.mark.parametrize("user_id", [str(uuid.UUID(int=1)), "not-an-id"])
async def test_there_is_nobody_to_deactivate_or_reactivate(user_id):
    assert await deactivate_account(user_id, now=T0) is None
    assert await reactivate_account(user_id) is None
    assert await _stored(RetiredLegacyToken) == []


# ── JWT cookies from before sessions ─────────────────────────────────────────


async def test_a_legacy_cookie_is_accepted_read_only(writes):
    ada = await _account()
    cookie = _legacy_cookie(ada)
    writes.clear()

    resolved = await resolve(cookie, now=T0 + timedelta(hours=2))

    assert resolved is not None
    assert resolved.user.id == ada.id
    assert resolved == Resolved(
        user=resolved.user,
        session=None,
        legacy_hash=_hash(cookie),
        legacy_expires_at=T0 + LEGACY_TOKEN_LIFETIME,
    )
    assert writes == []
    assert await _stored(UserSession) == []
    assert await _stored(RetiredLegacyToken) == []


def _tampered(cookie: str) -> str:
    signed, signature = cookie.rsplit(".", 1)
    middle = len(signature) // 2
    return f"{signed}.{signature[:middle]}{'A' if signature[middle] != 'A' else 'B'}{signature[middle + 1 :]}"


@pytest.mark.parametrize(
    "mint",
    [
        pytest.param(lambda ada: _legacy_cookie(ada, issued_at=T0 - LEGACY_TOKEN_LIFETIME), id="expired-just-now"),
        pytest.param(lambda ada: _legacy_cookie(ada, issued_at=T0 - timedelta(days=8)), id="expired"),
        pytest.param(lambda ada: _legacy_cookie(ada, secret="another-secret-entirely-32-bytes!!"), id="wrong-secret"),
        pytest.param(lambda ada: _legacy_cookie(ada, aud=("somebody-else",)), id="wrong-audience"),
        pytest.param(lambda ada: _legacy_cookie(ada, aud=()), id="no-audience"),
        pytest.param(lambda ada: _legacy_cookie(ada, sub=str(uuid.UUID(int=1))), id="unknown-account"),
        pytest.param(lambda ada: _legacy_cookie(ada, sub="ada"), id="subject-not-an-id"),
        pytest.param(lambda ada: _legacy_cookie(ada, sub=12345), id="subject-not-a-string"),
        pytest.param(lambda ada: _legacy_cookie(ada, exp="tomorrow"), id="expiry-not-a-number"),
        pytest.param(
            lambda ada: jwt.encode({"sub": str(ada.id), "aud": ["fastapi-users:auth"]}, JWT_SECRET, "HS256"),
            id="no-expiry",
        ),
        pytest.param(
            lambda ada: jwt.encode(
                {"sub": str(ada.id), "aud": ["fastapi-users:auth"], "exp": T0 + LEGACY_TOKEN_LIFETIME},
                None,
                algorithm="none",
            ),
            id="unsigned",
        ),
        pytest.param(lambda ada: _tampered(_legacy_cookie(ada)), id="tampered"),
        pytest.param(lambda ada: "a.b.c", id="not-a-jwt"),
    ],
)
async def test_a_legacy_cookie_fastapi_users_did_not_sign_for_a_live_account_is_refused(mint):
    ada = await _account()

    assert await resolve(mint(ada), now=T0) is None


async def test_a_legacy_cookie_of_a_deactivated_account_is_refused():
    ada = await _account()
    cookie = _legacy_cookie(ada)
    await _set_active(ada, False)

    assert await resolve(cookie, now=T0) is None


async def test_an_exchanged_cookie_aliases_its_session_and_ends_with_it():
    ada = await _account()
    cookie = _legacy_cookie(ada)
    now = T0 + timedelta(hours=2)
    legacy = await resolve(cookie, now=now)
    assert legacy is not None

    exchanged = await exchange_legacy(legacy, user_agent=FIREFOX_ON_WINDOWS, ip="198.51.100.0/24", now=now)

    assert exchanged is not None
    token, row = exchanged
    assert (row.user_agent, row.ip) == ("Firefox on Windows", "198.51.100.0/24")
    (retired,) = await _stored(RetiredLegacyToken)
    assert (retired.user_id, retired.token_hash, retired.session_id, retired.expires_at) == (
        str(ada.id),
        legacy.legacy_hash,
        row.id,
        T0 + LEGACY_TOKEN_LIFETIME,
    )
    by_token, by_cookie = await resolve(token, now=now), await resolve(cookie, now=now)
    assert by_token is not None and by_token.session is not None and by_token.session.id == row.id
    assert by_cookie is not None and by_cookie.session is not None and by_cookie.session.id == row.id
    assert (by_cookie.legacy_hash, by_cookie.legacy_expires_at) == (legacy.legacy_hash, T0 + LEGACY_TOKEN_LIFETIME)

    await end(by_token, now=now)

    assert await resolve(token, now=now) is None
    assert await resolve(cookie, now=now) is None


async def test_signing_out_through_the_alias_ends_the_session():
    ada = await _account()
    cookie = _legacy_cookie(ada)
    legacy = await resolve(cookie, now=T0)
    assert legacy is not None
    exchanged = await exchange_legacy(legacy, user_agent=None, ip=None, now=T0)
    assert exchanged is not None
    alias = await resolve(cookie, now=T0)
    assert alias is not None and alias.session is not None

    await end(alias, now=T0)

    assert await resolve(exchanged[0], now=T0) is None
    assert await resolve(cookie, now=T0) is None


async def test_an_exchanged_session_counts_its_lifetime_from_the_cookies_issue():
    ada = await _account()
    now = T0 + timedelta(days=2)
    legacy = await resolve(_legacy_cookie(ada), now=now)
    assert legacy is not None

    exchanged = await exchange_legacy(legacy, user_agent=None, ip=None, now=now)

    assert exchanged is not None
    token, row = exchanged
    assert (row.created_at, row.last_used_at, row.expires_at) == (T0, now, now + IDLE_LIFETIME)
    response = Response()
    set_session_cookie(response, token, row, now=now)
    assert _cookie(response)["max-age"] == str(28 * 86400)


async def test_a_second_exchange_of_one_cookie_opens_nothing():
    ada = await _account()
    legacy = await resolve(_legacy_cookie(ada), now=T0)
    assert legacy is not None

    first = await exchange_legacy(legacy, user_agent=None, ip=None, now=T0)
    second = await exchange_legacy(legacy, user_agent=None, ip=None, now=T0)

    assert first is not None and second is None
    assert [row.id for row in await _stored(UserSession)] == [first[1].id]
    assert len(await _stored(RetiredLegacyToken)) == 1


async def test_concurrent_exchanges_of_one_cookie_open_one_session():
    ada = await _account()
    legacy = await resolve(_legacy_cookie(ada), now=T0)
    assert legacy is not None

    results = await asyncio.gather(*(exchange_legacy(legacy, user_agent=None, ip=None, now=T0) for _ in range(4)))

    won = [result for result in results if result is not None]
    assert len(won) == 1
    assert [row.id for row in await _stored(UserSession)] == [won[0][1].id]
    assert len(await _stored(RetiredLegacyToken)) == 1


async def test_only_an_unexchanged_cookie_can_be_exchanged():
    ada = await _account()
    token, _ = await _open(ada)
    by_session = await resolve(token, now=T0)
    assert by_session is not None

    assert await exchange_legacy(by_session, user_agent=None, ip=None, now=T0) is None
    assert len(await _stored(UserSession)) == 1


async def test_a_cookie_cannot_be_exchanged_once_its_account_is_deactivated():
    ada = await _account()
    legacy = await resolve(_legacy_cookie(ada), now=T0)
    assert legacy is not None
    await _set_active(ada, False)

    assert await exchange_legacy(legacy, user_agent=None, ip=None, now=T0) is None
    assert await _stored(UserSession) == []
    assert await _stored(RetiredLegacyToken) == []


async def test_a_cookie_cannot_be_exchanged_after_signing_out_everywhere():
    """Resolved before the sign-out, exchanged after it: the exchange checks again."""
    ada = await _account()
    legacy = await resolve(_legacy_cookie(ada), now=T0)
    assert legacy is not None
    await revoke_other_sessions(ada.id, None, now=T0 + MINUTE)

    assert await exchange_legacy(legacy, user_agent=None, ip=None, now=T0 + 2 * MINUTE) is None
    assert await _stored(UserSession) == []


async def test_signing_out_an_unexchanged_cookie_retires_it():
    ada = await _account()
    cookie = _legacy_cookie(ada)
    legacy = await resolve(cookie, now=T0)
    assert legacy is not None

    await end(legacy, now=T0)

    assert await resolve(cookie, now=T0) is None
    (retired,) = await _stored(RetiredLegacyToken)
    assert (retired.token_hash, retired.session_id, retired.retired_at, retired.expires_at) == (
        legacy.legacy_hash,
        None,
        T0,
        T0 + LEGACY_TOKEN_LIFETIME,
    )
    await end(legacy, now=T0)
    assert len(await _stored(RetiredLegacyToken)) == 1
    assert await exchange_legacy(legacy, user_agent=None, ip=None, now=T0) is None
    assert await _stored(UserSession) == []


_BASE64URL = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def _respelled(cookie: str) -> list[str]:
    """Other spellings of a cookie whose signature decodes to the same bytes.

    A 32-byte HMAC is 43 characters of base64url, the last carrying 2 spare
    bits, and the decoder takes padding as well.
    """
    signed, signature = cookie.rsplit(".", 1)
    last = _BASE64URL.index(signature[-1])
    spare = [f"{signed}.{signature[:-1]}{_BASE64URL[(last & ~3) | bits]}" for bits in range(4)]
    return [spelling for spelling in spare if spelling != cookie] + [f"{cookie}=", f"{cookie}=="]


async def test_a_retired_cookie_stays_retired_however_its_signature_is_spelled():
    ada = await _account()
    cookie = _legacy_cookie(ada)
    spellings = _respelled(cookie)
    assert len(spellings) == 5
    for spelling in spellings:
        # Each is accepted, and as the same cookie.
        resolved = await resolve(spelling, now=T0)
        assert resolved is not None and resolved.legacy_hash == _hash(cookie)

    retiring = await resolve(spellings[0], now=T0)
    assert retiring is not None
    await end(retiring, now=T0)

    for spelling in [cookie, *spellings]:
        assert await resolve(spelling, now=T0) is None


async def test_signing_out_a_cookie_exchanged_since_it_was_resolved_ends_its_session():
    """A browser's sign-out racing its own exchange: the sign-out still wins."""
    ada = await _account()
    cookie = _legacy_cookie(ada)
    stale = await resolve(cookie, now=T0)
    assert stale is not None
    exchanged = await exchange_legacy(stale, user_agent=None, ip=None, now=T0)
    assert exchanged is not None

    await end(stale, now=T0)

    assert await resolve(exchanged[0], now=T0) is None
    assert await resolve(cookie, now=T0) is None


async def test_signing_out_everywhere_refuses_older_cookies_but_not_newer_ones():
    ada = await _account()
    retired_at = T0 + timedelta(hours=1)
    older = _legacy_cookie(ada, issued_at=T0)
    same_moment = _legacy_cookie(ada, issued_at=retired_at)
    newer = _legacy_cookie(ada, issued_at=retired_at + timedelta(seconds=1))

    await revoke_other_sessions(ada.id, None, now=retired_at)

    later = T0 + timedelta(hours=2)
    assert await resolve(older, now=later) is None
    assert await resolve(same_moment, now=later) is None
    assert await resolve(newer, now=later) is not None


async def test_signing_out_everywhere_else_ends_the_cookie_the_kept_session_came_from():
    """Whoever else holds a copy of that cookie must not keep the session through it."""
    ada = await _account()
    cookie = _legacy_cookie(ada)
    legacy = await resolve(cookie, now=T0)
    assert legacy is not None
    exchanged = await exchange_legacy(legacy, user_agent=None, ip=None, now=T0)
    assert exchanged is not None
    token, kept = exchanged

    await revoke_other_sessions(ada.id, kept.id, now=T0 + MINUTE)

    resolved = await resolve(token, now=T0 + 2 * MINUTE)
    assert resolved is not None and resolved.session is not None and resolved.session.id == kept.id
    assert await resolve(cookie, now=T0 + 2 * MINUTE) is None


# ── Sweeping ─────────────────────────────────────────────────────────────────


async def test_opening_a_session_sweeps_what_has_expired():
    ada = await _account()
    # Every cookie issued until T0, retired until T0 + 7 days.
    await revoke_other_sessions(ada.id, None, now=T0)
    # One cookie issued after that, retired until T0 + 7 days and a second.
    legacy = await resolve(_legacy_cookie(ada, issued_at=T0 + timedelta(seconds=1)), now=T0 + MINUTE)
    assert legacy is not None
    await end(legacy, now=T0 + MINUTE)
    _, expired = await _open(ada, at=T0)
    _, live = await _open(ada, at=T0 + timedelta(days=5))
    assert len(await _stored(RetiredLegacyToken)) == 2

    _, opened = await _open(ada, at=T0 + timedelta(days=8))

    assert {row.id for row in await _stored(UserSession)} == {live.id, opened.id}
    assert expired.id not in {row.id for row in await _stored(UserSession)}
    assert await _stored(RetiredLegacyToken) == []


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ua", "label"),
    [
        (FIREFOX_ON_WINDOWS, "Firefox on Windows"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36",
            "Chrome on macOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.6 Mobile/15E148 Safari/604.1",
            "Safari on iOS",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.6 Safari/605.1.15",
            "Safari on macOS",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
            "Edge on Windows",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36 OPR/114.0.0.0",
            "Opera on Windows",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Mobile Safari/537.36",
            "Chrome on Android",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Mobile Safari/537.36 EdgA/129.0.0.0",
            "Edge on Android",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
            "Samsung Internet on Android",
        ),
        ("Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0", "Firefox on Android"),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "CriOS/129.0.6668.69 Mobile/15E148 Safari/604.1",
            "Chrome on iOS",
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "FxiOS/131.0 Mobile/15E148 Safari/605.1.15",
            "Firefox on iOS",
        ),
        (
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36",
            "Chrome on ChromeOS",
        ),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Firefox on Linux"),
        ("curl/8.7.1", "curl"),
        ("python-httpx/0.27.0", "python-httpx"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Mozilla on Windows"),
        ("a" * 40 + "/1.0", "a" * 40),
        ("a" * 41 + "/1.0", "Unknown"),
        ("Some Client/1.0", "Unknown"),
        ("\x00\x1b[31m/1.0", "Unknown"),
        ("no product token here", "Unknown"),
        ("   ", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ],
)
def test_describe_user_agent(ua, label):
    assert describe_user_agent(ua) == label


@pytest.mark.parametrize(
    "ua",
    [
        "X" * 10_000,
        "x" * 40 + "/1 (" + "Windows; " * 2_000 + ")",
        "Mozilla/5.0 (" + "iPhone; " * 2_000 + ") " + "Edg/1 " * 2_000,
    ],
)
def test_a_user_agent_label_never_outgrows_its_column(ua):
    assert len(describe_user_agent(ua)) <= 120


@pytest.mark.parametrize(
    ("host", "network"),
    [
        ("203.0.113.57", "203.0.113.0/24"),
        ("198.51.100.255", "198.51.100.0/24"),
        ("::ffff:192.0.2.10", "192.0.2.0/24"),
        ("2001:db8:abcd:12::1", "2001:db8:abcd::/48"),
        ("2001:db8::1", "2001:db8::/48"),
        ("fe80::1%eth0", "fe80::/48"),
        ("203.0.113.256", None),
        ("2001:db8::/48", None),
        (" 203.0.113.57", None),
        ("testclient", None),
        ("", None),
        (None, None),
    ],
)
def test_coarse_ip(host, network):
    assert coarse_ip(host) == network


@pytest.mark.parametrize(
    "probe",
    [
        # First, so a module-level import of core.sessions in core.users would
        # meet a half-initialised module here.
        "import core.sessions",
        "import sys, core.users; assert 'core.sessions' not in sys.modules, 'core.users imports core.sessions'",
    ],
)
def test_the_store_imports_without_a_cycle(probe):
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND,
        env=os.environ | {"RETINA_ENV": "test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── The cookie's domain, and a request carrying more than one ────────────────


async def test_the_cookie_is_host_only_unless_a_domain_is_configured(monkeypatch):
    ada = await _account()
    token, row = await open_session(ada, user_agent=None, ip=None, now=T0)

    monkeypatch.delenv("SESSION_COOKIE_DOMAIN", raising=False)
    response = Response()
    set_session_cookie(response, token, row, now=T0)
    assert _cookie(response)["domain"] == ""

    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", ".retina.example")
    response = Response()
    set_session_cookie(response, token, row, now=T0)
    assert _cookie(response)["domain"] == "retina.example"
    cleared = Response()
    clear_session_cookie(cleared)
    assert _cookie(cleared)["domain"] == "retina.example"
    assert _cookie(cleared)["max-age"] == "0"


def _request_with_header(cookie_header: str) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", cookie_header.encode())]}
    return Request(scope)


async def test_whichever_of_two_auth_token_cookies_signs_in_does():
    """A host-only cookie this environment set and one production set on the
    parent domain both arrive, under one name. The stale one first."""
    ada = await _account()
    token, _ = await open_session(ada, user_agent=None, ip=None)

    request = _request_with_header(f"{COOKIE_NAME}=stale-from-a-session-that-ended; {COOKIE_NAME}={token}")
    assert sessions.cookie_tokens(request) == ["stale-from-a-session-that-ended", token]
    resolved = await resolve_request(request)
    assert resolved is not None
    assert resolved.user.id == ada.id

    other = _request_with_header(f'{COOKIE_NAME}="{token}"; theme=dark; {COOKIE_NAME}=stale')
    assert (await resolve_request(other)).user.id == ada.id
    assert await resolve_request(_request_with_header("theme=dark")) is None
