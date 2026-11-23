"""Sign-in sessions held on the server, and the JWT cookies they replace.

A session is a random token in the auth_token cookie and a row in
user_sessions keyed on its SHA-256, so signing out ends it here rather than
only in the browser that asked. It slides: a use pushes the idle end out, at
most once an hour so a page that polls does not write on every request, and
never past an absolute end thirty days after the sign-in.

Cookies minted before this are fastapi-users JWTs, good until their own `exp`.
One is accepted read-only (a `Resolved` with no session) until a caller that
can set a cookie exchanges it for a session. Nothing mints them any more, so
seven days after the release that stopped minting them every one has expired,
and the legacy half of this module can go.

A websocket outlives the handshake that resolved its cookie, so whatever ends
a session or a cookie here also wakes the account's watches (watch_account)
once it has committed, and the socket checks its own cookie again.

The functions that read the clock take `now`, so the tests can move it, bar
resolve_request, which always reads the real one. Callers leave it out.
"""

import asyncio
import logging
import os
import re
import secrets
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address, ip_network

import jwt
from fastapi import Response
from fastapi.requests import HTTPConnection
from jwt.utils import base64url_decode, base64url_encode
from sqlalchemy import ColumnElement, and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from core.auth import INTENT_SIGNIN, _hash_token
from core.nodes import NodeClaim
from core.users import (
    JWT_SECRET,
    MagicLink,
    PendingAddressChange,
    RetiredAddress,
    RetiredLegacyToken,
    User,
    UserSession,
    async_session_maker,
    forwarding_address,
    normalise_email,
)
from services import account_email, claim_links, mail_outbox
from services.node_claim_store import drop_challenge, read_challenge

logger = logging.getLogger(__name__)

COOKIE_NAME = "auth_token"

# A session nobody uses for this long ends.
IDLE_LIFETIME = timedelta(days=7)
# One in constant use still ends this long after the sign-in, so a stolen
# cookie cannot be kept alive for ever just by using it.
ABSOLUTE_LIFETIME = timedelta(days=30)
# How stale last_used_at may grow before a use writes it again.
REFRESH_INTERVAL = timedelta(hours=1)
# What fastapi-users gave a JWT cookie, so its issue time is `exp` less this.
LEGACY_TOKEN_LIFETIME = timedelta(days=7)

# How fastapi-users' JWTStrategy signed and addressed its cookies.
_LEGACY_AUDIENCE = ["fastapi-users:auth"]
_LEGACY_ALGORITHM = "HS256"

_USER_AGENT_MAX = 120
# An HTTP token (RFC 9110), as a product name in a User-Agent is.
_PRODUCT = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,40}")

# Where resolve_request keeps its answer on request.state.
_STATE_KEY = "resolved_session"
_UNRESOLVED = object()


class InactiveAccount(Exception):
    """The account is deactivated, or gone, so it may not hold a session."""


class AccountHoldsNodes(Exception):
    """A node still names the account as its owner, so the account may not be removed."""


@dataclass(frozen=True)
class Resolved:
    """Who a cookie signs in, and through what.

    `session` is None only for a JWT cookie nobody has exchanged yet. The
    legacy fields are set for any JWT cookie, exchanged or not: the hash is the
    cookie's identity in retired_legacy_tokens, and the expiry is its `exp`.
    """

    user: User
    session: UserSession | None
    legacy_hash: str | None
    legacy_expires_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _expires_at(created_at: datetime, last_used_at: datetime) -> datetime:
    return min(last_used_at + IDLE_LIFETIME, created_at + ABSOLUTE_LIFETIME)


def _user_uuid(user_id: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


def _user_key(user_id: str | uuid.UUID) -> str | None:
    """An account id as the session tables hold it: the dashed form "user".id is stored in."""
    parsed = _user_uuid(user_id)
    return None if parsed is None else str(parsed)


async def _prune(db: AsyncSession, now: datetime) -> None:
    """Delete every session and retirement that has expired.

    Runs first in each transaction that checks something before writing. A
    DELETE takes SQLite's write lock even when it matches nothing, so from here
    to the commit no other writer can change what the checks read.
    """
    await db.execute(delete(UserSession).where(UserSession.expires_at <= now))
    await db.execute(delete(RetiredLegacyToken).where(RetiredLegacyToken.expires_at <= now))


async def _add_session(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    created_at: datetime,
    user_agent: str | None,
    ip: str | None,
    now: datetime,
) -> tuple[str, UserSession]:
    # The account's row, not the caller's copy of it, which may predate a
    # deactivation.
    account = await db.get(User, user_id)
    if account is None or not account.is_active:
        raise InactiveAccount
    token = secrets.token_urlsafe(32)
    row = UserSession(
        id=str(uuid.uuid4()),
        user_id=str(user_id),
        token_hash=_hash_token(token),
        created_at=created_at,
        last_used_at=now,
        expires_at=_expires_at(created_at, now),
        user_agent=describe_user_agent(user_agent),
        ip=ip,
    )
    db.add(row)
    return token, row


async def open_session(
    user: User, *, user_agent: str | None, ip: str | None, now: datetime | None = None
) -> tuple[str, UserSession]:
    """Open a session for an account that has just proved its address.

    Returns the token for the cookie, which is stored nowhere, and the row.
    Raises InactiveAccount for a deactivated or deleted account. `ip` is stored
    as given, so pass it through coarse_ip.
    """
    now = _utcnow() if now is None else now
    async with async_session_maker() as db:
        async with db.begin():
            await _prune(db, now)
            return await _add_session(db, user.id, created_at=now, user_agent=user_agent, ip=ip, now=now)


async def _slide(db: AsyncSession, row: UserSession, now: datetime) -> None:
    """Move a session's idle end out from this use, at most once per REFRESH_INTERVAL.

    Conditional on last_used_at, so the requests of one polling page write once
    between them. A failed write is logged and dropped: the session is still
    good until the end it already has, and the request must not fail for it.
    """
    if now - row.last_used_at < REFRESH_INTERVAL:
        return
    expires_at = _expires_at(row.created_at, now)
    try:
        slid = (
            await db.execute(
                update(UserSession)
                .where(UserSession.id == row.id, UserSession.last_used_at <= now - REFRESH_INTERVAL)
                .values(last_used_at=now, expires_at=expires_at)
                .returning(UserSession.id)
                .execution_options(synchronize_session=False)
            )
        ).first()
        await db.commit()
    except Exception:
        logger.warning("Could not extend a session", exc_info=True)
        return
    if slid is not None:
        set_committed_value(row, "last_used_at", now)
        set_committed_value(row, "expires_at", expires_at)


async def resolve(token: str | None, *, now: datetime | None = None, slide: bool = True) -> Resolved | None:
    """The account and session an auth_token cookie signs in, or None.

    None covers a token that is unknown, expired, forged or retired, and one
    whose account is gone or deactivated. A session token never contains a
    dot, so a value that does is taken for a JWT cookie from before sessions.

    A use of a session slides it. With `slide` False nothing is written: a
    check that a cookie still signs in, such as an open socket's, is no use of
    it. A failure to read the database is raised rather than answered as
    signed out.
    """
    if not token:
        return None
    now = _utcnow() if now is None else now
    if "." in token:
        return await _resolve_legacy(token, now, slide=slide)
    async with async_session_maker() as db:
        found = (
            await db.execute(
                select(UserSession, User)
                .join(User, UserSession.user_id == User.id)
                .where(UserSession.token_hash == _hash_token(token))
            )
        ).one_or_none()
        if found is None:
            return None
        session, user = found
        if session.expires_at <= now or not user.is_active:
            return None
        if slide:
            await _slide(db, session, now)
    return Resolved(user=user, session=session, legacy_hash=None, legacy_expires_at=None)


def _legacy_claims(token: str) -> tuple[uuid.UUID, datetime] | None:
    """The account and expiry a JWT cookie names, if fastapi-users signed it."""
    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[_LEGACY_ALGORITHM],
            audience=_LEGACY_AUDIENCE,
            # Expiry is checked by the caller against `now`, the clock every
            # other expiry here is read on, rather than by PyJWT's own.
            options={"verify_exp": False, "require": ["exp", "sub", "aud"]},
        )
    except jwt.PyJWTError:
        return None
    exp, sub = claims["exp"], claims["sub"]
    if isinstance(exp, bool) or not isinstance(exp, int | float) or not isinstance(sub, str):
        return None
    try:
        return uuid.UUID(sub), datetime.fromtimestamp(exp, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _legacy_hash(token: str) -> str:
    """The SHA-256 of a verified JWT, spelled the way it was minted.

    A signature decodes the same from several spellings (the spare bits of its
    last character, trailing padding) and PyJWT accepts each of them, so a
    retired cookie re-spelled would otherwise hash as one never seen. Only the
    signature can vary: the two segments before it are what it signs.
    """
    signed, signature = token.rsplit(".", 1)
    return _hash_token(f"{signed}.{base64url_encode(base64url_decode(signature)).decode()}")


def _retired_wholesale(user_key: str, issued_at: datetime) -> ColumnElement[bool]:
    """The account's retirement of every cookie issued at or before `issued_at`."""
    return and_(
        RetiredLegacyToken.user_id == user_key,
        RetiredLegacyToken.token_hash.is_(None),
        RetiredLegacyToken.retired_at >= issued_at,
    )


async def _resolve_legacy(token: str, now: datetime, *, slide: bool) -> Resolved | None:
    claims = _legacy_claims(token)
    if claims is None:
        return None
    user_id, expires_at = claims
    if expires_at <= now:
        return None
    legacy_hash = _legacy_hash(token)
    user_key = str(user_id)
    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active:
            return None
        retired = (
            await db.scalars(
                select(RetiredLegacyToken).where(
                    or_(
                        and_(RetiredLegacyToken.user_id == user_key, RetiredLegacyToken.token_hash == legacy_hash),
                        _retired_wholesale(user_key, expires_at - LEGACY_TOKEN_LIFETIME),
                    )
                )
            )
        ).all()
        if not retired:
            return Resolved(user=user, session=None, legacy_hash=legacy_hash, legacy_expires_at=expires_at)
        # Signing out everywhere ends an exchanged cookie too: whoever else
        # holds a copy of it would otherwise keep the session it aliases.
        if any(row.token_hash is None for row in retired):
            return None
        (exchanged,) = retired
        if exchanged.session_id is None:
            return None
        session = await db.get(UserSession, exchanged.session_id)
        if session is None or session.user_id != user_key or session.expires_at <= now:
            return None
        if slide:
            await _slide(db, session, now)
    return Resolved(user=user, session=session, legacy_hash=legacy_hash, legacy_expires_at=expires_at)


async def resolve_request(request: HTTPConnection) -> Resolved | None:
    """resolve() for the auth_token cookie of a request or websocket, once per request.

    Kept on request.state, so the dependencies of one route share a lookup and
    a slide.
    """
    cached = getattr(request.state, _STATE_KEY, _UNRESOLVED)
    if cached is None or isinstance(cached, Resolved):
        return cached
    resolved = await resolve_any(cookie_tokens(request))
    setattr(request.state, _STATE_KEY, resolved)
    return resolved


def cookie_tokens(conn: HTTPConnection) -> list[str]:
    """Every auth_token value a request or websocket carries, in the browser's order.

    A browser sends one pair per cookie it holds under the name, so a host-only
    auth_token this environment set and one another environment set on the
    parent domain (SESSION_COOKIE_DOMAIN) arrive together, and `conn.cookies`
    keeps only the last of them. Read from the header instead, so whichever
    one signs in here can.
    """
    values: list[str] = []
    headers = conn.headers
    # A stub request in a test may hold its headers as a plain dict.
    raw = headers.getlist("cookie") if hasattr(headers, "getlist") else [headers.get("cookie") or ""]
    for header in raw:
        for pair in header.split(";"):
            name, sep, value = pair.partition("=")
            if sep and name.strip() == COOKIE_NAME:
                values.append(value.strip().strip('"'))
    return values


async def resolve_any(tokens: list[str], *, now: datetime | None = None, slide: bool = True) -> Resolved | None:
    """resolve() for the first of `tokens` that signs in; resolve(None) for none at all."""
    for token in tokens or [None]:
        resolved = await resolve(token, now=now, slide=slide)
        if resolved is not None:
            return resolved
    return None


async def exchange_legacy(
    resolved: Resolved, *, user_agent: str | None, ip: str | None, now: datetime | None = None
) -> tuple[str, UserSession] | None:
    """Swap a JWT cookie nobody has exchanged yet for a session.

    The session and the row tying the JWT to it are written together, so from
    the commit on the JWT is only an alias of that session and ends with it.
    That also covers the browser's other requests still carrying the JWT.

    The session counts its absolute lifetime from the JWT's issue, which is
    when the person last proved their address.

    Returns None, opening nothing, when there is nothing to exchange any more:
    another request exchanged or signed out the same JWT first, the account
    signed out everywhere or was deactivated since, or this is not an
    unexchanged JWT at all.
    """
    if resolved.session is not None or resolved.legacy_hash is None or resolved.legacy_expires_at is None:
        return None
    now = _utcnow() if now is None else now
    if resolved.legacy_expires_at <= now:
        return None
    issued_at = resolved.legacy_expires_at - LEGACY_TOKEN_LIFETIME
    user_id = resolved.user.id
    try:
        async with async_session_maker() as db:
            async with db.begin():
                await _prune(db, now)
                wholesale = select(RetiredLegacyToken.id).where(_retired_wholesale(str(user_id), issued_at))
                if await db.scalar(wholesale) is not None:
                    return None
                token, row = await _add_session(
                    db, user_id, created_at=min(issued_at, now), user_agent=user_agent, ip=ip, now=now
                )
                # Unique on token_hash: an exchange or a sign-out of the same
                # JWT that committed first fails this insert, and the session
                # above goes with it.
                db.add(
                    RetiredLegacyToken(
                        user_id=str(user_id),
                        token_hash=resolved.legacy_hash,
                        session_id=row.id,
                        retired_at=now,
                        expires_at=resolved.legacy_expires_at,
                    )
                )
            return token, row
    except (InactiveAccount, IntegrityError):
        return None


class AccountWatch:
    """Word that something an account signs in with may have ended.

    From watch_account, for a holder that outlives the request that resolved
    its cookie, such as an open socket. Each write here that ends a session or
    retires a cookie wakes the account's watches after it commits, so a woken
    holder reads the state after it. A wake does not say whose cookie ended:
    the holder checks its own. Release it when done, or the registry keeps it.
    """

    def __init__(self, user_key: str, loop: asyncio.AbstractEventLoop) -> None:
        self.user_key = user_key
        self._loop = loop
        self._woken = asyncio.Event()

    async def wait(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a wake, and say whether one came.

        A wake while the holder is busy is kept for its next wait, so one that
        lands during a check is not lost.
        """
        try:
            await asyncio.wait_for(self._woken.wait(), timeout)
        except TimeoutError:
            return False
        self._woken.clear()
        return True

    def release(self) -> None:
        """Stop watching. Releasing again does nothing."""
        with _watches_lock:
            watches = _watches.get(self.user_key)
            if watches is None:
                return
            watches.discard(self)
            if not watches:
                del _watches[self.user_key]

    def _wake(self) -> None:
        # An asyncio.Event belongs to the loop that waits on it, so a write on
        # another loop or thread, as a TestClient portal is, hands the set over.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._woken.set()
            return
        try:
            self._loop.call_soon_threadsafe(self._woken.set)
        except RuntimeError:
            # Its loop has closed, and the holder with it.
            self.release()


# Every live watch, by account as the session tables key it. Locked because a
# wake may come from another thread than the one that registered the watch.
_watches: dict[str, set[AccountWatch]] = {}
_watches_lock = threading.Lock()


def watch_account(user_id: uuid.UUID) -> AccountWatch:
    """Watch an account for its sessions and cookies ending, from inside a running loop."""
    watch = AccountWatch(str(user_id), asyncio.get_running_loop())
    with _watches_lock:
        _watches.setdefault(watch.user_key, set()).add(watch)
    return watch


def _wake_watches(user_id: str | uuid.UUID) -> None:
    """Wake the account's watches. Only after the write that ended something has committed."""
    user_key = _user_key(user_id)
    if user_key is None:
        return
    with _watches_lock:
        watches = list(_watches.get(user_key, ()))
    for watch in watches:
        watch._wake()


async def end(resolved: Resolved, *, now: datetime | None = None) -> None:
    """Sign a cookie out: delete its session, or retire a JWT that has none.

    Ending a session a JWT was exchanged for ends the JWT with it. Ending a
    JWT that has been exchanged since it was resolved ends that session too.
    Ending what has already ended does nothing.
    """
    now = _utcnow() if now is None else now
    async with async_session_maker() as db:
        async with db.begin():
            await _end_in(db, resolved, now)
    _wake_watches(resolved.user.id)


async def _end_in(db: AsyncSession, resolved: Resolved, now: datetime) -> None:
    if resolved.session is not None:
        await db.execute(delete(UserSession).where(UserSession.id == resolved.session.id))
        return
    if resolved.legacy_hash is None or resolved.legacy_expires_at is None:
        return
    user_key = str(resolved.user.id)
    this_cookie = RetiredLegacyToken.token_hash == resolved.legacy_hash
    exchanged_for = select(RetiredLegacyToken.session_id).where(this_cookie)
    await db.execute(delete(UserSession).where(UserSession.user_id == user_key, UserSession.id.in_(exchanged_for)))
    if await db.scalar(select(RetiredLegacyToken.id).where(this_cookie)) is not None:
        return
    db.add(
        RetiredLegacyToken(
            user_id=user_key,
            token_hash=resolved.legacy_hash,
            session_id=None,
            retired_at=now,
            expires_at=resolved.legacy_expires_at,
        )
    )


async def list_sessions(user_id: str | uuid.UUID, *, now: datetime | None = None) -> list[UserSession]:
    """An account's live sessions, the most recently used first."""
    user_key = _user_key(user_id)
    if user_key is None:
        return []
    now = _utcnow() if now is None else now
    async with async_session_maker() as db:
        rows = await db.scalars(
            select(UserSession)
            .where(UserSession.user_id == user_key, UserSession.expires_at > now)
            .order_by(UserSession.last_used_at.desc(), UserSession.created_at.desc())
        )
        return list(rows)


async def revoke_session(user_id: str | uuid.UUID, session_id: str) -> bool:
    """End one of an account's sessions by its id.

    False when there is no such session or it is somebody else's, which the
    caller answers alike: a session id is no secret, but it grants nothing to
    anyone but its owner.
    """
    user_key = _user_key(user_id)
    if user_key is None:
        return False
    async with async_session_maker() as db:
        async with db.begin():
            ended = (
                await db.execute(
                    delete(UserSession)
                    .where(UserSession.id == session_id, UserSession.user_id == user_key)
                    .returning(UserSession.id)
                )
            ).first()
    if ended is None:
        return False
    _wake_watches(user_key)
    return True


async def _retire_every_legacy_token(db: AsyncSession, user_key: str, now: datetime) -> None:
    """Refuse every JWT cookie of the account issued until now.

    One such row per account is enough, as the newest covers whatever an older
    one did.
    """
    await db.execute(
        delete(RetiredLegacyToken).where(
            RetiredLegacyToken.user_id == user_key,
            RetiredLegacyToken.token_hash.is_(None),
            RetiredLegacyToken.retired_at <= now,
        )
    )
    db.add(
        RetiredLegacyToken(
            user_id=user_key,
            token_hash=None,
            session_id=None,
            retired_at=now,
            expires_at=now + LEGACY_TOKEN_LIFETIME,
        )
    )


async def revoke_other_sessions(
    user_id: str | uuid.UUID, keep_session_id: str | None, *, now: datetime | None = None
) -> int:
    """Sign an account out everywhere but `keep_session_id`, returning how many sessions ended.

    Every JWT cookie of the account goes as well, including one exchanged for
    the session kept: the session's own token carries on, but a copy of the
    JWT elsewhere must not. With `keep_session_id` None, nothing is kept.
    """
    user_key = _user_key(user_id)
    if user_key is None:
        return 0
    now = _utcnow() if now is None else now
    async with async_session_maker() as db:
        async with db.begin():
            await _prune(db, now)
            others = delete(UserSession).where(UserSession.user_id == user_key)
            if keep_session_id is not None:
                others = others.where(UserSession.id != keep_session_id)
            ended = len((await db.execute(others.returning(UserSession.id))).all())
            await _retire_every_legacy_token(db, user_key, now)
    # Whatever the count: a JWT cookie may be all the account had open.
    _wake_watches(user_key)
    return ended


async def deactivate_account(user_id: str | uuid.UUID, *, now: datetime | None = None) -> User | None:
    """Deactivate an account and sign it out everywhere, in one transaction.

    Returns the account, or None when there is no such account.
    """
    account_id = _user_uuid(user_id)
    if account_id is None:
        return None
    now = _utcnow() if now is None else now
    async with async_session_maker() as db:
        async with db.begin():
            await _prune(db, now)
            user = await db.get(User, account_id)
            if user is None:
                return None
            user.is_active = False
            await db.execute(delete(UserSession).where(UserSession.user_id == str(account_id)))
            await _retire_every_legacy_token(db, str(account_id), now)
    _wake_watches(account_id)
    return user


async def reactivate_account(user_id: str | uuid.UUID) -> User | None:
    """Let a deactivated account sign in again.

    Nothing it held before comes back: its sessions were deleted and its JWT
    cookies stay retired, so the person signs in afresh.
    """
    account_id = _user_uuid(user_id)
    if account_id is None:
        return None
    async with async_session_maker() as db:
        async with db.begin():
            user = await db.get(User, account_id)
            if user is None:
                return None
            user.is_active = True
    return user


async def remove_account(user_id: str | uuid.UUID, *, now: datetime | None = None) -> bool:
    """Delete an account that owns no node, with everything it signs in by, in one transaction.

    The row and the sign-in links outstanding for its address go together, and
    so does the link of a change of address it had waiting, which is keyed on
    the new address rather than on the account. The row takes its sessions,
    the retirements of its JWT cookies and that waiting change with it (ON
    DELETE CASCADE): a cookie naming an account that is gone resolves to
    nobody either way. The address is nobody's afterwards. Claim links to it
    stay, as they are about a node rather than the account, and one clicked
    later makes a new account. Those to an address it retired go, with their
    claims' challenges and mail (_withdraw_forwarded_claims).

    Another account may have retired the address before this one had it,
    which a rollback across migration 0025 leaves, and that account's claim to
    it goes too: this account won the address while it stood (core/users.py,
    _account_at), and the stale row would hand it back, with the claim links in
    this account's mailbox. Then the claim links to the address go as well. One
    minted before this account had it was mailed to the other account's
    address (services/claim_links.py, queue), so whoever holds it proved that
    address and not this one, and with neither account left at it a click
    would make an account there, verified and signed in. A link does not say
    which mailbox it went to, so all of them go, and their nodes ask again.

    Whether a node names it is read under the write lock, so the answer holds
    to the commit. A claim that bound a node before the lock is seen, and
    raises AccountHoldsNodes having changed nothing. One that binds after the
    commit finds no account, as the claim checks the account again in its own
    write (services/node_claiming.py, complete_claim).

    False when there is no such account.
    """
    account_id = _user_uuid(user_id)
    if account_id is None:
        return False
    now = _utcnow() if now is None else now
    user_key = str(account_id)
    async with async_session_maker() as db:
        async with db.begin():
            await _prune(db, now)
            user = await db.get(User, account_id)
            if user is None:
                return False
            if await db.scalar(select(NodeClaim.node_id).where(NodeClaim.user_id == user_key).limit(1)) is not None:
                raise AccountHoldsNodes
            # Before the row, which takes the retired addresses with it.
            await _withdraw_forwarded_claims(db, user_key)
            # Links and retired addresses are keyed on the normalised address
            # (core/auth.py, create_magic_link), where an account's own row
            # may not be.
            address = normalise_email(user.email)
            # Another account's retirement of it, which ON DELETE CASCADE
            # does not reach. Left, it would find that account again for the
            # address the moment this one is gone.
            retired_by = (
                await db.execute(
                    delete(RetiredAddress).where(RetiredAddress.email == address).returning(RetiredAddress.user_id)
                )
            ).scalar_one_or_none()
            if retired_by is not None and retired_by != user_key:
                # And the claim links on it, some of which that account's
                # mailbox holds.
                await _withdraw_claim_links(db, [address])
            await db.execute(delete(MagicLink).where(MagicLink.email == address, MagicLink.intent == INTENT_SIGNIN))
            # Before the row, whose cascade takes the change that names it.
            waiting = select(PendingAddressChange.handle).where(PendingAddressChange.user_id == user_key)
            await db.execute(
                delete(MagicLink).where(
                    MagicLink.token_hash.in_(waiting), MagicLink.intent == account_email.INTENT_EMAIL_CHANGE
                )
            )
            await db.execute(delete(User).where(User.id == account_id))
    _wake_watches(account_id)
    return True


async def _withdraw_forwarded_claims(db: AsyncSession, user_key: str) -> None:
    """Kill every claim link still out for an address the account retired and
    is mailed for, in remove_account's transaction (_withdraw_claim_links).

    Their mail went to the address the account has now (services/claim_links.py,
    queue), so whoever holds one proved that address and not the one the link
    names. While the account stands, a click binds the node to it. Once it is
    gone, nothing ties the two addresses together, and a click would make an
    account at the retired one, verified, owning the node and signed in. The
    links to the account's own address are left: whoever clicks one proved it.
    So are those to a retired address another account has as its own, which
    is mailed itself (core/users.py, forwarding_address).
    """
    retired = (await db.execute(select(RetiredAddress.email).where(RetiredAddress.user_id == user_key))).scalars()
    forwarded = [email for email in retired.all() if await forwarding_address(db, email) is not None]
    await _withdraw_claim_links(db, forwarded)


async def _withdraw_claim_links(db: AsyncSession, addresses: list[str]) -> None:
    """Kill every claim link still out for `addresses`, in remove_account's
    transaction, and with it the challenge it was for and the claim mail
    queued to carry it.

    The steps an address change takes for the links in the old mailbox
    (services/account_email.py, _withdraw_claims). A node waiting on one reads
    unclaimed with its address on file, so whoever is at it can ask again, and
    that mail goes to the address itself, which is nobody's once the account
    is gone. A node whose challenge is another link by now keeps it, and the
    mail queued for it.
    """
    if not addresses:
        return
    links = (
        await db.execute(
            select(MagicLink.token_hash, MagicLink.node_id).where(
                MagicLink.email.in_(addresses),
                MagicLink.intent == claim_links.INTENT_CLAIM,
                MagicLink.used_at.is_(None),
            )
        )
    ).all()
    for handle, node_id in links:
        await claim_links.invalidate_in(db, handle)
        if node_id is None:
            continue
        challenge = await read_challenge(db, node_id)
        if challenge is not None and challenge.handle == handle:
            # Every claim mail queued for the node carries this challenge's
            # link, since a new challenge cancels the mail queued for the one
            # it replaces.
            await mail_outbox.cancel(db, node_id=node_id)
            await drop_challenge(db, node_id)


def set_session_cookie(response: Response, token: str, session: UserSession, *, now: datetime | None = None) -> None:
    """Put a session's token in the auth_token cookie.

    The cookie lasts until the session's absolute end. The idle end moves with
    every use and is the server's to enforce: keeping the cookie in step would
    mean setting it again on every response.
    """
    now = _utcnow() if now is None else now
    remaining = session.created_at + ABSOLUTE_LIFETIME - now
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(0, int(remaining.total_seconds())),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        domain=cookie_domain(),
    )


def clear_session_cookie(response: Response) -> None:
    """Delete the auth_token cookie, with the attributes it was set with."""
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax", domain=cookie_domain())


def cookie_domain(env: Mapping[str, str] | None = None) -> str | None:
    """The Domain the auth_token cookie is set with, or None for a host-only cookie.

    SESSION_COOKIE_DOMAIN names a parent domain, such as `retina.fm`, so a
    sign-in here is sent to every host under it: the other environments, whose
    receivers hold a mirror of this environment's sessions
    (services/registry_mirror.py), then sign the same person in without a
    sign-in of their own. Unset (the default, and every environment but
    production) keeps the cookie to this host. Read on every call, so a test
    can set it.
    """
    env = os.environ if env is None else env
    return (env.get("SESSION_COOKIE_DOMAIN") or "").strip().lstrip(".") or None


# Tried in order. A browser carries the tokens of the ones it is built on (Edge
# and Opera say Chrome, Chrome says Safari), so the more specific goes first.
# Every browser on iOS is WebKit, which is why Chrome and Firefox there have
# tokens of their own.
_BROWSERS = (
    ("Edge", ("Edg/", "EdgA/", "EdgiOS/", "Edge/")),
    ("Opera", ("OPR/",)),
    ("Samsung Internet", ("SamsungBrowser/",)),
    ("Firefox", ("Firefox/", "FxiOS/")),
    ("Chrome", ("Chrome/", "CriOS/")),
)
# iOS says "like Mac OS X", Android and ChromeOS both say Linux.
_SYSTEMS = (
    ("iOS", ("iPhone", "iPad", "iPod")),
    ("Android", ("Android",)),
    ("ChromeOS", ("CrOS",)),
    ("Windows", ("Windows",)),
    ("macOS", ("Macintosh", "Mac OS X")),
    ("Linux", ("Linux",)),
)


def describe_user_agent(ua: str | None) -> str:
    """A short label for a User-Agent, such as "Firefox on Windows".

    What a session list shows so a person can tell their devices apart. A
    client that is no browser known here is named by its product token, "curl"
    for curl, or "Unknown".
    """
    ua = (ua or "").strip()
    if not ua:
        return "Unknown"
    browser = next((name for name, tokens in _BROWSERS if any(token in ua for token in tokens)), None)
    if browser is None and "Version/" in ua and "Safari/" in ua:
        browser = "Safari"
    if browser is None:
        product = ua.partition("/")[0]
        browser = product if "/" in ua and _PRODUCT.fullmatch(product) else "Unknown"
    system = next((name for name, tokens in _SYSTEMS if any(token in ua for token in tokens)), None)
    label = f"{browser} on {system}" if system else browser
    return label[:_USER_AGENT_MAX]


def coarse_ip(host: str | None) -> str | None:
    """The network a client address is in, never the address itself.

    A /24 for IPv4 and a /48 for IPv6, with an IPv4-mapped address taken as the
    IPv4 one. None for anything that is not an address.

    Coarser than the magic-link quota's source (services/client_source.py, client_source)
    on purpose, so keep the two apart: that one keys an admission limit, which
    wants one subscriber (an address or a /64), where this is kept on file.
    """
    if not host:
        return None
    try:
        address = ip_address(host)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    prefix = 24 if address.version == 4 else 48
    return str(ip_network((address, prefix), strict=False))
