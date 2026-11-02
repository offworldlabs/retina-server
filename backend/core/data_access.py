"""The one gate in front of the keyed data API: who is calling, and whether they may.

Every keyed route and stream asks here, and nothing re-implements any part of
it elsewhere. check_data_access takes a caller for the first of these it
presents:

1. An account key: on a websocket `?apikey=` first, then the `x-apikey`
   header, then `Authorization: Bearer <key>` (another scheme is no key). A
   key presented decides alone: one that is malformed, unknown or revoked is
   refused even beside a session cookie or an administrator's header, so a
   script holding a dead key learns so rather than riding a browser's session.
   A good key passes while its owner is active and entitled.
2. The radar key, RADAR_API_KEY, in `X-API-Key`. It decides alone too, and
   passes only while RADAR_API_KEY is set and not empty, compared in constant
   time. Read at every call, so backend/.env, loaded after the imports, counts.
3. An administrator: a verified Cloudflare Access identity in the connection's
   own headers (Access puts its assertion on websocket upgrades as well), or
   the anonymous-admin bypass. Asked before the session, so an administrator
   who also holds an account's cookie keeps internal access.
4. A session, through sessions.resolve_request, whose account is entitled. A
   deactivated account's session resolves to nobody, so it is the next case.
5. Nothing, which is refused.

Administrators and the radar key are internal access: no limits, and the only
callers that may be shown the synthetic fleet. Keys and sessions work under
their tier's limits and see real nodes only.

A refusal is DataAccessDenied, an HTTPException whose detail is
{"reason": <code>, "message": <a sentence>}. The message never repeats what
was presented.

- 401 `missing_credentials`: nothing was presented.
- 401 `invalid_key`: an account key malformed, unknown or revoked, or a wrong
  radar key. One reason for all, so a revoked key reads as one never issued.
- 403 `not_entitled`, `lapsed`, `grant_ended`: the owner of the key or
  session is not entitled, for services/entitlements.py's reason;
  `not_entitled` also for a deactivated owner, whom that module counts.
- 429 `rate_limited`: over the tier's requests a minute, with Retry-After,
  X-RateLimit-Limit, X-RateLimit-Remaining (0) and X-RateLimit-Reset (the
  seconds until the window has room, as Retry-After).
- 429 `stream_limit`: over the tier's streams open at once, with Retry-After
  (STREAM_RETRY_AFTER_S).
- 403 `forbidden_origin`: a websocket that sent no key, from a page that is
  not the console's (core/origins.py), refused before its credentials are read.

A websocket refused before it is accepted is answered as a REST call is, with
the status, headers and body above, and no socket opens (refuse_upgrade). Once
a socket is open only a close reaches it: a stream's re-check that refuses
closes it 1008 with the reason.

Public:

- Principal: who the gate took the caller for. `kind` is "key", "session",
  "admin" or "radar"; `account_id` the key's owner or the session's account
  (None for admin and radar); `key_id` set for a key alone; `limits` the
  tier's, or for admin and radar services/entitlements.admin_access()'s, where
  None is unlimited; `internal` True for admin and radar alone. Ids only.
- DataAccessDenied: a refusal; `.reason` is its code.
- REASON_MISSING_CREDENTIALS, REASON_INVALID_KEY, REASON_RATE_LIMITED,
  REASON_STREAM_LIMIT, REASON_FORBIDDEN_ORIGIN: the codes above that are not
  services/entitlements.py's.
- CACHE_TTL_S: how long a key's or an account's answer is kept (30 s).
- RECHECK_INTERVAL_S: how often an open stream calls check_data_access again
  (30 s), as the owner feed re-checks its cookie.
- RATE_WINDOW_S: the requests-per-minute window (60 s).
- STREAM_RETRY_AFTER_S: the Retry-After of a `stream_limit` (30 s).
- check_data_access(conn): the caller's Principal, or DataAccessDenied. No
  rate limit and no Origin check, so an open stream re-checks through it.
  After a connection's first call, the session that call admitted is read
  again, and nothing else: without sliding it, since an open stream's check
  is no use of its session (core/sessions.py), and without choosing among
  the connection's cookies again (_session).
- require_data_access(request): the FastAPI dependency for a route:
  check_data_access, then one request from the principal's minute, then the
  key's use recorded.
- ws_data_access(websocket): the same for a websocket route, asked before the
  route runs, with the Origin check first for a socket that sent no key. A
  refusal goes through refuse_upgrade; the upgrade counts as one request.
- refuse_upgrade(refusal): ends a websocket route before its handshake with an
  HTTPException, answered as a REST route answers it.
- RefusedUpgradeLogFilter: keeps refuse_upgrade's refusals out of uvicorn's
  error log; main.py installs it.
- stream_slot(principal): an async context manager holding one of the
  principal's streams open at once; see its docstring.

Limits are counted in this process's memory, which is exact: the server is one
process on one event loop, and a restart drops every stream anyway. A limit
counts by the key for a key call and by the account for a session call, so
each key and the account's own dashboard have a budget apiece.

Answers are cached CACHE_TTL_S: a key's row by its prefix, and an account's
entitlement and whether it is active. An unknown prefix is never cached, so
made-up keys cannot fill the memory. A key revoked (services/api_keys.py) and
an entitlement changed (services/entitlements.py) drop their entries through
listeners, at once. A grant that ends by its date writes nothing, nor does a
deactivation tell this module, so for those the expiry is what takes effect,
within CACHE_TTL_S.
"""

import hmac
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn

from fastapi import HTTPException, Request, WebSocket, status
from fastapi.requests import HTTPConnection
from sqlalchemy import select

from core import sessions, users
from core.origins import foreign_origin
from services import api_keys, entitlements
from services.api_keys import KeyRecord
from services.entitlements import REASON_GRANT_ENDED, REASON_LAPSED, REASON_NOT_ENTITLED, TierLimits
from services.node_rate_limits import Clock, FixedWindowCounters

REASON_MISSING_CREDENTIALS = "missing_credentials"
REASON_INVALID_KEY = "invalid_key"
REASON_RATE_LIMITED = "rate_limited"
REASON_STREAM_LIMIT = "stream_limit"
REASON_FORBIDDEN_ORIGIN = "forbidden_origin"

CACHE_TTL_S = 30.0
RECHECK_INTERVAL_S = 30.0
RATE_WINDOW_S = 60
# A slot comes free when one of the caller's own streams ends, which the
# server cannot foresee: this is a pause before asking again, not a promise.
STREAM_RETRY_AFTER_S = 30

_NOT_ENTITLED_MESSAGES = {
    REASON_NOT_ENTITLED: "This account has no access to the data API.",
    REASON_LAPSED: "This account's access to the data API has lapsed.",
    REASON_GRANT_ENDED: "This account's grant of access to the data API has ended.",
}

# Where check_data_access keeps the session a connection's first call admitted.
_ADMITTED_SESSION = "data_access_admitted_session"


@dataclass(frozen=True)
class Principal:
    kind: Literal["key", "session", "admin", "radar"]
    account_id: str | None
    key_id: str | None
    limits: TierLimits
    internal: bool


class DataAccessDenied(HTTPException):
    def __init__(self, status_code: int, reason: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code, detail={"reason": reason, "message": message}, headers=headers)
        self.reason = reason


def _unauthorized(reason: str, message: str) -> DataAccessDenied:
    # A 401 names the scheme it wants (RFC 9110), and Bearer is the one a key
    # is sent by.
    return DataAccessDenied(status.HTTP_401_UNAUTHORIZED, reason, message, {"WWW-Authenticate": "Bearer"})


def _invalid_key() -> DataAccessDenied:
    return _unauthorized(REASON_INVALID_KEY, "The key sent is not valid.")


def _unlimited() -> TierLimits:
    limits = entitlements.admin_access().limits
    if limits is None:
        raise RuntimeError("An administrator's access carries no limits")
    return limits


_ADMIN = Principal(kind="admin", account_id=None, key_id=None, limits=_unlimited(), internal=True)
_RADAR = Principal(kind="radar", account_id=None, key_id=None, limits=_unlimited(), internal=True)


# ── The cache ─────────────────────────────────────────────────────────────────

# Read through the module at every call, so a test can hold the clock still.
_clock: Clock = time.monotonic


class _Cache[K, V]:
    """Answers kept CACHE_TTL_S, and dropped at once by drop().

    One generation for the whole cache: drop() moves it on, and a fill that
    began before does not store what it read. So a drop that lands while a
    read of the old state is in flight wins, rather than the old state coming
    back for CACHE_TTL_S. Drops are rare (a revoke, a grant, the hourly job),
    and a fill one overtakes is only read again on the next call.
    """

    def __init__(self) -> None:
        self._entries: dict[K, tuple[float, V]] = {}
        self._generation = 0
        self._next_sweep = 0.0

    async def read(self, key: K, fill: Callable[[], Awaitable[V | None]]) -> V | None:
        """The answer for `key`: the cached one while fresh, else `fill`'s,
        kept unless it is None or a drop came while it was read."""
        now = _clock()
        held = self._entries.get(key)
        if held is not None and held[0] > now:
            return held[1]
        generation = self._generation
        value = await fill()
        if value is not None and generation == self._generation:
            self._entries[key] = (now + CACHE_TTL_S, value)
            if now >= self._next_sweep:
                self._next_sweep = now + CACHE_TTL_S
                for stale in [entry for entry, (expires, _) in self._entries.items() if expires <= now]:
                    del self._entries[stale]
        return value

    def drop(self, keys: Iterable[K]) -> None:
        self._generation += 1
        for key in keys:
            self._entries.pop(key, None)


_keys: _Cache[str, KeyRecord] = _Cache()
_accounts: _Cache[str, tuple[entitlements.Access, bool]] = _Cache()

api_keys.add_key_listener(lambda record: _keys.drop((record.prefix,)))
entitlements.add_access_listener(_accounts.drop)


async def _cached_key(prefix: str) -> KeyRecord | None:
    return await _keys.read(prefix, lambda: api_keys.fetch_key(prefix))


async def _read_account(account_id: str) -> tuple[entitlements.Access, bool]:
    access = await entitlements.effective_access(account_id)
    async with users.async_session_maker() as db:
        active = await db.scalar(select(users.User.is_active).where(users.User.id == account_id))
    return access, bool(active)


async def _entitled_limits(account_id: str) -> TierLimits:
    """The account's tier limits, or a 403 with why it may not call."""
    held = await _accounts.read(account_id, lambda: _read_account(account_id))
    access, active = held if held is not None else (None, False)
    if access is None or not active:
        reason = REASON_NOT_ENTITLED
    elif access.entitled and access.limits is not None:
        return access.limits
    else:
        reason = access.reason or REASON_NOT_ENTITLED
    message = _NOT_ENTITLED_MESSAGES.get(reason, _NOT_ENTITLED_MESSAGES[REASON_NOT_ENTITLED])
    raise DataAccessDenied(status.HTTP_403_FORBIDDEN, reason, message)


# ── Who is calling ────────────────────────────────────────────────────────────


def _presented_key(conn: HTTPConnection) -> str | None:
    """The account key the caller presents, by the first means it uses, or None."""
    if conn.scope["type"] == "websocket" and (key := conn.query_params.get("apikey")):
        return key
    if key := conn.headers.get("x-apikey"):
        return key
    scheme, _, credentials = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    return None


def _presented_radar_key(conn: HTTPConnection) -> str | None:
    return conn.headers.get("x-api-key") or None


def _radar_key_matches(presented: str) -> bool:
    expected = os.getenv("RADAR_API_KEY", "")
    if not expected:
        return False
    # Byte for byte as they arrived: a header is read as latin-1, the
    # environment with surrogateescape.
    return hmac.compare_digest(presented.encode("latin-1"), os.fsencode(expected))


async def _administrator(conn: HTTPConnection) -> bool:
    """A verified Access identity in the connection's headers, or the bypass.

    Through the module's attributes at call time, which the tests replace.
    """
    if users.AUTH_BYPASS:
        return True
    identity = users.access_identity
    return identity.is_configured() and bool(await identity.identity(conn.headers.get(users.ACCESS_ASSERTION_HEADER)))


async def _still_signed_in(admitted: sessions.Resolved) -> bool:
    """Whether the session a connection admitted is live and its account active, read without a write.

    Its row is gone once the session is signed out or revoked, or its account
    signed out everywhere, deactivated or removed (core/sessions.py). A JWT
    cookie from before sessions that nobody had exchanged has no row to read,
    and fails: the stream closes at its first re-check, and the client's next
    handshake carries the session every page load exchanges such a cookie for
    (routes/auth.py, me).
    """
    if admitted.session is None:
        return False
    async with users.async_session_maker() as db:
        live = await db.scalar(
            select(users.UserSession.id)
            .join(users.User, users.UserSession.user_id == users.User.id)
            .where(
                users.UserSession.id == admitted.session.id,
                users.UserSession.expires_at > datetime.now(UTC),
                users.User.is_active.is_(True),
            )
        )
    return live is not None


async def _session(conn: HTTPConnection) -> sessions.Resolved | None:
    """The connection's session: resolve_request's answer the first time, then that same session while it lasts.

    resolve_request keeps its answer on the connection for as long as the
    connection lives, which for a stream re-checking every RECHECK_INTERVAL_S
    would be its handshake's answer for hours. A re-check reads the session
    that answer named again instead, and leaves it where it was, as the owner
    feed's checks do (routes/streaming.py): an open stream is no use of its
    session. It never picks among the connection's cookies again, so it cannot
    disagree with the handshake about which session the stream rides on.
    """
    if not hasattr(conn.state, _ADMITTED_SESSION):
        resolved = await sessions.resolve_request(conn)
        setattr(conn.state, _ADMITTED_SESSION, resolved)
        return resolved
    admitted: sessions.Resolved | None = getattr(conn.state, _ADMITTED_SESSION)
    return admitted if admitted is not None and await _still_signed_in(admitted) else None


async def check_data_access(conn: HTTPConnection) -> Principal:
    presented = _presented_key(conn)
    if presented is not None:
        record = await api_keys.verify_key(presented, fetch=_cached_key)
        if record is None:
            raise _invalid_key()
        limits = await _entitled_limits(record.user_id)
        return Principal(kind="key", account_id=record.user_id, key_id=record.id, limits=limits, internal=False)
    radar = _presented_radar_key(conn)
    if radar is not None:
        if _radar_key_matches(radar):
            return _RADAR
        raise _invalid_key()
    if await _administrator(conn):
        return _ADMIN
    resolved = await _session(conn)
    if resolved is not None:
        account_id = str(resolved.user.id)
        limits = await _entitled_limits(account_id)
        return Principal(kind="session", account_id=account_id, key_id=None, limits=limits, internal=False)
    raise _unauthorized(REASON_MISSING_CREDENTIALS, "Send an API key, or sign in.")


# ── Limits ────────────────────────────────────────────────────────────────────


def _holder(principal: Principal) -> tuple[str, str] | None:
    """What a limit counts the principal by: its key, or its account; None for internal access."""
    if principal.kind == "key" and principal.key_id is not None:
        return ("key", principal.key_id)
    if principal.kind == "session" and principal.account_id is not None:
        return ("account", principal.account_id)
    return None


# Never refusing when full: a principal is a key or an account that has passed
# the checks above, which a caller cannot make up without an entitled account,
# and a busy map must not refuse an entitled caller its own budget. The bound
# only sets when expired counters are reclaimed and a warning logged; one
# counter per principal per minute, it sits far above the keys and accounts
# expected to call within one minute.
_MAX_TRACKED_PRINCIPALS = 10_000
_requests = FixedWindowCounters(lambda: _clock(), _MAX_TRACKED_PRINCIPALS, label="data access rate limit map")


def _spend_request(principal: Principal) -> None:
    """Count one request against the principal's minute, or raise the 429."""
    holder, limit = _holder(principal), principal.limits.requests_per_minute
    if holder is None or limit is None:
        return
    wait_s = _requests.admit([(holder, limit, RATE_WINDOW_S)])
    if wait_s is None:
        return
    retry_after = str(max(1, math.ceil(wait_s)))
    raise DataAccessDenied(
        status.HTTP_429_TOO_MANY_REQUESTS,
        REASON_RATE_LIMITED,
        f"Over this tier's {limit} requests a minute; try again in {retry_after} s.",
        {
            "Retry-After": retry_after,
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": retry_after,
        },
    )


# Streams open now, per holder; an entry goes when its count reaches zero.
_streams: dict[tuple[str, str], int] = {}


@asynccontextmanager
async def stream_slot(principal: Principal) -> AsyncIterator[None]:
    """Hold one of the principal's `limits.max_concurrent_streams` while inside.

    Counted by the key for a key and the account for a session; internal
    access is not counted. One past the limit raises DataAccessDenied, 429
    `stream_limit`, on entry. An SSE route enters before its response starts
    and lets the 429 propagate, since one raised inside a streaming body never
    reaches the caller; a websocket route enters before accept and refuses
    with it (refuse_upgrade). The slot is given back however the block ends:
    normally, by an exception, or by the task's cancellation.
    """
    holder, limit = _holder(principal), principal.limits.max_concurrent_streams
    if holder is None or limit is None:
        yield
        return
    held = _streams.get(holder, 0)
    if held >= limit:
        raise DataAccessDenied(
            status.HTTP_429_TOO_MANY_REQUESTS,
            REASON_STREAM_LIMIT,
            f"This tier allows {limit} streams open at once; close one first.",
            {"Retry-After": str(STREAM_RETRY_AFTER_S)},
        )
    _streams[holder] = held + 1
    try:
        yield
    finally:
        left = _streams.get(holder, 0) - 1
        if left > 0:
            _streams[holder] = left
        else:
            _streams.pop(holder, None)


# ── Dependencies ──────────────────────────────────────────────────────────────


async def require_data_access(request: Request) -> Principal:
    principal = await check_data_access(request)
    _spend_request(principal)
    if principal.key_id is not None:
        await api_keys.touch_key(principal.key_id)
    return principal


# Set in a connection's context by refuse_upgrade, for RefusedUpgradeLogFilter.
_upgrade_refused: ContextVar[bool] = ContextVar("data_access_upgrade_refused", default=False)
_UNFINISHED_HANDSHAKE = "ASGI callable returned without completing handshake."


def refuse_upgrade(refusal: HTTPException) -> NoReturn:
    """End a websocket route before its handshake with `refusal`, answered as a REST route's.

    Raised, so the route stops here whether it asked through Depends or by a
    call, and leaves whatever it had entered. The app's HTTPException handler,
    the one that answers every REST route's refusals, answers it too, as the
    websocket denial response uvicorn offers: the status, the headers and
    `{"detail": ...}`, and no socket opens. So a client, and a reconnect loop,
    sees a 401, 403, 422 or 429 rather than a socket that opens and closes,
    which would reset its backoff. Only before accept: once a socket is open,
    a close is all that reaches it.
    """
    _upgrade_refused.set(True)
    raise refusal


class RefusedUpgradeLogFilter(logging.Filter):
    """Drops uvicorn's handshake error for a socket refuse_upgrade refused; keeps it for any other.

    uvicorn 0.54's default websocket protocol (websockets-sansio) sends a
    denial response whole, but never marks the handshake complete for it, so
    once the route returns it logs "ASGI callable returned without completing
    handshake." at ERROR on uvicorn.error, for every refusal. That record is
    dropped for a connection refused here, read from the contextvar the
    refusal set in the connection's own task; for a route that returns
    without accepting or refusing, the bug it reports, it is kept.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_upgrade_refused.get() and record.getMessage() == _UNFINISHED_HANDSHAKE)


async def ws_data_access(websocket: WebSocket) -> Principal:
    # A browser sends its cookie, and Access its assertion, with a handshake
    # whatever page opened it; only a key is sent on purpose. So a socket
    # without one must come from the console's own pages, judged before the
    # credentials are read, as the owner feed is (routes/streaming.py).
    sent_key = _presented_key(websocket) is not None or _presented_radar_key(websocket) is not None
    if not sent_key and foreign_origin(websocket.headers.get("origin")):
        refuse_upgrade(
            DataAccessDenied(
                status.HTTP_403_FORBIDDEN,
                REASON_FORBIDDEN_ORIGIN,
                "Open the socket from the console's own pages, or send an API key.",
            )
        )
    try:
        principal = await check_data_access(websocket)
        _spend_request(principal)
    except DataAccessDenied as denied:
        refuse_upgrade(denied)
    if principal.key_id is not None:
        await api_keys.touch_key(principal.key_id)
    return principal
