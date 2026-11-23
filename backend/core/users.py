"""Accounts, and the FastAPI dependencies that say who a request is.

An account is a row in "user", opened by redeeming an emailed link or made by
an administrator connecting a radar to an address. Who a request is comes from
one of three places, tried in order: a verified Cloudflare Access assertion, the
anonymous admin where AUTH_ALLOW_ANONYMOUS_ADMIN opts into it, and the
auth_token cookie, which core/sessions.py resolves to an account. The first two
are administrators. An account never is.
"""

import logging
import os
import uuid
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import CHAR, Boolean, Float, ForeignKey, Integer, String, event, func, select, update
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from core.access_identity import AccessIdentity
from core.database import DATABASE_URL
from core.timestamps import UTCDateTime

# ── Config ────────────────────────────────────────────────────────────────────

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()
_jwt_from_env = os.getenv("JWT_SECRET", "")
# deploy/start.sh applies the same rule before the migration step: change both.
if not _jwt_from_env and _RETINA_ENV not in ("dev", "test"):
    raise RuntimeError(
        "JWT_SECRET environment variable is required in production "
        f"(RETINA_ENV={_RETINA_ENV!r}). Set it to a random ≥32-byte string."
    )

# Signs nothing any more. core/sessions.py verifies the JWT cookies issued
# before sessions moved into the database with it, until the last of them has
# expired, after which the key and the guard above can go.
JWT_SECRET = _jwt_from_env or "retina-dev-secret-change-me-in-prod-32b!"


def _derive_auth_flags(env: Mapping[str, str]) -> tuple[bool, bool]:
    """Return (auth_enabled, auth_bypass) for an environment.

    The anonymous-admin bypass is an explicit opt-in, AUTH_ALLOW_ANONYMOUS_ADMIN=1,
    rather than a consequence of the environment's name. RETINA_ENV gated six
    unrelated behaviours at once, so a deployment that wanted only this one had to
    call itself `test` and silently gave up the other five guards to get it. Naming
    the behaviour directly lets production be RETINA_ENV=production and still choose;
    the flag's own name grants a named permission rather than announcing a mode, so
    what is being handed out is legible at the point it is switched on.

    A configured identity provider still wins: a deployment with one must never
    serve an anonymous admin, whatever the flag says. That provider is Cloudflare
    Access, which is what admits an administrator. All three droplets set
    CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD, so the bypass is inert on every one
    of them, and a laptop with neither can still opt in.

    AUTH_ENABLED is unconditional. There is no provider left to configure: a
    sign-in link opens the same cookie session on any deployment that can send
    mail, and whether mail is configured is services/mail.py's answer to give,
    per request, rather than a boot-time constant here.

    Parameterised on `env` rather than reading os.environ so the derivation is
    testable on its own. The two module-level flags below are computed once at
    import and read as values elsewhere (routes/auth.py, routes/streaming.py), so
    reloading this module to vary the environment would leave those copies stale.
    """
    access_configured = bool(env.get("CF_ACCESS_TEAM_DOMAIN") and env.get("CF_ACCESS_AUD"))
    return True, not access_configured and env.get("AUTH_ALLOW_ANONYMOUS_ADMIN", "") == "1"


AUTH_ENABLED, AUTH_BYPASS = _derive_auth_flags(os.environ)

#: The header Cloudflare sets on every request its Access applications admit.
ACCESS_ASSERTION_HEADER = "Cf-Access-Jwt-Assertion"

#: Cloudflare's logout endpoint, relative to whatever origin serves the page.
#: The team-domain form ends the same session, but this one also deletes the
#: per-application cookie, so the next request is challenged immediately rather
#: than once revocation propagates ~30s later, and it keeps
#: CF_ACCESS_TEAM_DOMAIN out of the front-end bundle. Either form signs the
#: person out of every Access application: there is no per-application logout.
ACCESS_LOGOUT_PATH = "/cdn-cgi/access/logout"

#: One instance, so the JWKS cache and its lock are shared across requests.
#: The audience differs per environment and comes from the compose overlays; the
#: team domain is the same everywhere and comes from the base compose file.
#: Either unset means unconfigured, and an unconfigured verifier is never
#: consulted rather than refusing assertions nobody sent.
access_identity = AccessIdentity(
    team_domain=os.getenv("CF_ACCESS_TEAM_DOMAIN", ""),
    audience=os.getenv("CF_ACCESS_AUD", ""),
)

# ── SQLAlchemy setup ─────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class AccountId(TypeDecorator):
    """An account's id: a uuid.UUID here, its dashed form in the CHAR(36) that
    0001 made "user".id, which is how every existing row holds it."""

    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value: uuid.UUID | str | None, _dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(value))

    def process_result_value(self, value: str | None, _dialect: Dialect) -> uuid.UUID | None:
        return None if value is None else uuid.UUID(value)


class User(Base):
    """An account: an address that has redeemed a link, or one an administrator
    connected a radar to.

    It grants nothing by itself. A node is its owner's through node_claims, and
    an administrator is a Cloudflare Access identity, never a row.
    """

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(AccountId(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # False once deactivated: no sign-in link is issued to the address, no link
    # of either kind is redeemed for it, and no session resolves to it.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Read by nothing, so it grants nothing whatever it holds.
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether a link sent to the address has been redeemed. An account an
    # administrator made reads False until then.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Legacy column, unused. Mapped so a rollback to the previous release, which
    # selects it, still works on the migrated schema; the follow-up that drops
    # the legacy cookie path drops it. The default fills its NOT NULL, which has
    # no server default.
    hashed_password: Mapped[str] = mapped_column(String(1024), default="")
    # Legacy column, unused; kept and dropped with hashed_password.
    avatar: Mapped[str] = mapped_column(String(512), server_default="")
    # Legacy column, unused; kept and dropped with hashed_password.
    provider: Mapped[str] = mapped_column(String(50), server_default="")


class MagicLink(Base):
    """One outstanding emailed link: a sign-in, a node claim, or the
    confirmation of a new address for an account, told apart by `intent`.

    The primary key is a SHA-256 of the token, never the token itself: the link
    in the mailbox is the whole credential, so a database read must not be
    enough to mint a session.
    """

    __tablename__ = "magic_links"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    # What the link is for. A link is a bearer credential in a mailbox and
    # mailboxes get forwarded, so what a token may do has to travel with it:
    # a claim link must not open a session for whoever forwarded it, and a
    # sign-in link must not claim a node. Both redeemers check this rather than
    # trusting that a token only reaches the endpoint it was minted for.
    #
    # It also scopes the per-address cap and the sweep below. Without that, an
    # ordinary sign-in would wipe the claim challenge for a node its owner is
    # halfway through setting up.
    intent: Mapped[str] = mapped_column(String(16), default="signin", server_default="signin", index=True)
    # The node a claim link is about. Null for a sign-in link, which is about
    # nobody's node.
    node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)


class UserSession(Base):
    """One signed-in browser: what its auth_token cookie names (core/sessions.py).

    Kept as a SHA-256 of the token, never the token, for the reason a magic
    link is: a read of this table must not be enough to act as anybody. `id`
    is the handle a session list shows and a revocation names, so the hash
    never has to leave the server.

    Ending a session deletes its row. Nothing reads them for history, and a row
    that is gone cannot be resolved by mistake.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # CHAR(36), as "user".id is stored on SQLite.
    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # When the sign-in behind the session happened, which the absolute
    # lifetime counts from.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # The earlier of the idle and the absolute end, kept on the row so that a
    # lookup and the sweep are each one indexed comparison.
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    # A label such as "Firefox on Windows" and a /24 or /48: enough for a
    # person to tell their own devices apart, without a full user agent or an
    # address on file.
    user_agent: Mapped[str] = mapped_column(String(120), default="", server_default="")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RetiredAddress(Base):
    """An address an account signed in with before it changed to the one it has now.

    It stays the account's rather than being freed for reuse: somebody resetting
    a node later and typing an old address from habit must reach the same
    account, not create a second one holding that node. So everything that
    turns an address into an account (_account_at) finds the account through
    this table too, and no other account can take the address.

    It is never mailed, and never carries a sign-in link (core/auth.py). Mail
    meant for it goes to the account's current address (forwarding_address),
    since an address somebody moved away from may be dead or somebody else's by
    now.

    Stored normalised, so the primary key is case-insensitive uniqueness.
    """

    __tablename__ = "retired_addresses"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    # CHAR(36), as "user".id is stored on SQLite.
    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    retired_at: Mapped[datetime] = mapped_column(UTCDateTime())


class PendingAddressChange(Base):
    """The one change of address an account has waiting for its link to be opened.

    `user_id` is the primary key, so "one pending change per account" is the
    schema's rule. A new request replaces the row, and the link the old row
    named is killed in the same transaction (services/account_email.py), so a
    link mailed to a mistyped address stops working once it is corrected.

    `handle` is the SHA-256 of the mailed token, as magic_links keys it; the
    token itself is never stored. Confirming checks the link against this row
    rather than only checking that it is live.
    """

    __tablename__ = "pending_address_changes"

    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


class RetiredLegacyToken(Base):
    """A fastapi-users JWT cookie that must no longer sign anybody in.

    Cookies from before server-side sessions are signed JWTs, valid until their
    own `exp` seven days after issue, and nothing about one can be changed once
    it is out. This table is the only way to end one early, so core/sessions.py
    reads it before accepting any.

    With `token_hash` set, the row is one cookie: exchanged for the session in
    `session_id`, whose alias it stays until that session ends, or signed out
    with no session behind it. With `token_hash` null, it is every cookie of
    the account issued at or before `retired_at`, which is what signing out
    everywhere and deactivation write: nobody has a list of the cookies out
    there. Either way it expires when the last cookie it covers does, and is
    swept then.
    """

    __tablename__ = "retired_legacy_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    # No foreign key: a session ends by deletion, and this row has to stay
    # behind it so the cookie stops resolving instead of reading as one never
    # exchanged.
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    retired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _conn_rec):
    """Apply WAL mode + safety pragmas on every new SQLite connection.

    WAL mode is the single biggest correctness improvement we can make: it
    survives `kill -9` mid-write without corrupting the database (rollback
    journal mode can leave the file in a half-written state). Combined with
    `synchronous=NORMAL` it also lets readers proceed concurrently with a
    single writer instead of serialising everything behind a global lock.

    `busy_timeout` lets writers wait briefly for a contending lock instead
    of returning SQLITE_BUSY immediately — a much better default for a
    web app where the alternative is a 500 to the user.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def create_db_and_tables() -> None:
    """Create tables directly. Migrations own the schema everywhere except tests.

    `create_all` never alters an existing table, so leaving it as the deploy path
    would silently skip every column added after a table first appeared. The test
    suite still uses it: it is faster than a migration run per session, and the
    equivalence between the two is asserted in tests/test_migrations.py.
    """
    if os.getenv("RETINA_SCHEMA_SOURCE", "alembic") != "create_all":
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


# ── Helper: anonymous user (auth disabled in dev) ─────────────────────────────

ANONYMOUS_USER: dict = {
    "id": "00000000-0000-0000-0000-000000000000",
    "email": "admin@retina.fm",
    "name": "Admin (no auth)",
    "role": "admin",
    "is_superuser": True,
    "created_at": 0,
}


def user_to_dict(user: User) -> dict:
    """An account as the API reports it: never an administrator.

    Whatever the row's is_superuser holds. An administrator is an Access
    identity (_access_user_dict) or the anonymous admin, and require_admin
    admits those two alone.
    """
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": "user",
        "is_superuser": False,
        "is_active": user.is_active,
        "created_at": user.created_at.timestamp() if user.created_at else 0,
        "last_seen_at": user.last_seen_at.timestamp() if user.last_seen_at else None,
    }


# ── FastAPI dependency helpers ────────────────────────────────────────────────
# The cookie is resolved through core/sessions.py, imported where it is used:
# that module imports this one for the models.

# How stale last_seen_at may be. A page that polls uses its session on every
# poll, so the stamp is written once per window rather than on each request.
_VISIT_RESOLUTION = timedelta(minutes=5)


async def record_visit(user: User) -> None:
    """Stamp an active account's `last_seen_at` with now, on the row and on the instance in hand.

    A failed write is logged and dropped: the request it rides on must not fail
    for a bookkeeping column.
    """
    now = datetime.now(UTC)
    if user.last_seen_at is not None and now - user.last_seen_at < _VISIT_RESOLUTION:
        return
    try:
        async with async_session_maker() as session:
            # Active is checked in the write, not on the instance: that is the
            # caller's snapshot, and the row may have been deactivated since.
            stamped = await session.execute(
                update(User).where(User.id == user.id, User.is_active.is_(True)).values(last_seen_at=now)
            )
            await session.commit()
    except Exception:
        logging.warning("Could not record a visit", exc_info=True)
        return
    if stamped.rowcount:
        user.last_seen_at = now


async def read_user_from_token(token: str | None) -> User | None:
    """The active account an auth_token cookie value signs in, or None.

    For a caller outside the request/response cycle, such as a websocket
    handshake reading ws.cookies, and for a route that serves an unidentified
    caller rather than refusing one. Both treat any failure to identify a
    caller as nobody, so a failure to read the database is logged and answered
    as None too. Every use of a session resolves here or in
    _read_user_from_request, so these are where a visit is recorded.
    """
    from core import sessions

    try:
        resolved = await sessions.resolve(token)
    except Exception:
        logging.warning("Could not resolve a session", exc_info=True)
        return None
    if resolved is None:
        return None
    await record_visit(resolved.user)
    return resolved.user


async def _read_user_from_request(request: Request) -> User | None:
    """The active account the request's auth_token cookie signs in, or None.

    Resolved once per request, on request.state (core/sessions.py). A failure to
    read the database is raised: a 401 for it would tell the console it had
    been signed out.
    """
    from core import sessions

    resolved = await sessions.resolve_request(request)
    if resolved is None:
        return None
    await record_visit(resolved.user)
    return resolved.user


def _access_user_dict(email: str) -> dict:
    """A user dict for a verified Access identity, with no database row.

    Membership of the Access group is what grants the console, so anyone whose
    assertion verifies for this environment's audience is an administrator; a
    second list of addresses would only be one more thing to drift.

    The id is derived from the email rather than allocated, so the same person
    is the same id across requests and restarts and the destructive endpoints
    stay attributable in /api/admin/events.
    """
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailto:{email}")),
        "email": email,
        "name": email.split("@")[0],
        "role": "admin",
        "is_superuser": True,
        "created_at": 0,
    }


async def _access_user_from_request(request: Request) -> dict | None:
    """The verified Access identity for this request, or None."""
    if not access_identity.is_configured():
        return None
    email = await access_identity.identity(request.headers.get(ACCESS_ASSERTION_HEADER))
    return _access_user_dict(email) if email else None


async def has_access_session(request: Request) -> bool:
    """Whether this request was admitted by Access rather than by our own cookie.

    A service token is admitted but carries no email, so it has no identity here
    and answers False, which keeps non-interactive callers away from an
    interactive logout page.
    """
    return await _access_user_from_request(request) is not None


async def get_optional_user(request: Request) -> dict | None:
    """Return user dict or None, for a route that answers everyone.

    The same resolution get_current_user does, stopping short of the 401, so a
    route open to all can still tell a signed-in caller apart and answer them
    more fully. Withholding by omitting a field rather than by refusing the
    request is what lets one route serve both.
    """
    access = await _access_user_from_request(request)
    if access is not None:
        return access
    if AUTH_BYPASS:
        return dict(ANONYMOUS_USER)
    user = await _read_user_from_request(request)
    if user is None or not user.is_active:
        return None
    return user_to_dict(user)


async def get_current_user(request: Request) -> dict:
    """Return user dict or raise 401. Returns anonymous admin where AUTH_BYPASS is opted into."""
    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def require_admin(request: Request) -> dict:
    """Like get_current_user, but 403 for anyone who is not an administrator.

    An administrator is a verified Access identity, or the anonymous admin where
    AUTH_BYPASS is opted into, and nothing else: the two user dicts that say
    "admin". A signed-in account never is, whatever its row's is_superuser
    holds, so its cookie gets 403. Membership of the Access group is what grants
    the console, and an address that can receive a sign-in link is no claim to
    it.

    Over the same resolution, rather than its own copy of it: a fourth source
    of identity added to get_optional_user must not have to be remembered here
    as well, and is not an administrator unless it says so.
    """
    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Magic-link user creation helper ───────────────────────────────────────────


class MagicLinkRefused(Exception):
    """The account at this address is deactivated, so no link may sign it in.

    Carries no detail on purpose: the route answers it exactly as it answers a
    token that never existed.
    """


def normalise_email(email: str) -> str:
    """An address as accounts and links are keyed on it."""
    return email.strip().lower()


async def _account_at(session: AsyncSession, email: str) -> User | None:
    """The account an address belongs to: the one it is the address of, in
    whatever case it was stored, or the one that retired it.

    The first wins when both exist, which a rollback can leave: a release from
    before migration 0025 makes an account at a retired address when somebody
    signs in with it. The address is that account's from then on, and
    forwarding_address agrees.
    """
    account = (
        await session.execute(select(User).where(func.lower(User.email) == func.lower(email)))
    ).scalar_one_or_none()
    if account is not None:
        return account
    return await _retired_from(session, email)


async def _retired_from(session: AsyncSession, email: str) -> User | None:
    """The account that retired `email`, or None when it is not a retired address."""
    return (
        await session.execute(
            select(User)
            .join(RetiredAddress, RetiredAddress.user_id == User.id)
            .where(RetiredAddress.email == normalise_email(email))
        )
    ).scalar_one_or_none()


async def forwarding_address(session: AsyncSession, email: str) -> str | None:
    """Where mail meant for a retired address goes: its account's current
    address. None when `email` is not a retired address, and is mailed itself.

    Read off _account_at, so mail goes wherever the address resolves: an
    address that is one account's own and another's retired is mailed itself,
    to the account it belongs to.
    """
    account = await _account_at(session, email)
    if account is None or normalise_email(account.email) == normalise_email(email):
        return None
    return account.email


async def _make_account(session: AsyncSession, email: str, *, is_verified: bool) -> tuple[User, bool]:
    """Make the account at an address, or find the one another request made
    first, and say whether this call made it.

    The caller has just looked and found none. The unique index on the address
    is what closes the window between that read and this insert.
    """
    account = User(email=email, name=email.split("@")[0], is_verified=is_verified)
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        found = await _account_at(session, email)
        if found is None:
            raise
        return found, False
    # For created_at, which the database fills in.
    await session.refresh(account)
    return account, True


async def get_or_create_magic_link_user(email: str) -> User:
    """Find or create the account a redeemed link belongs to, sign-in or claim.

    Nothing here makes an administrator. Administrator identity is Cloudflare
    Access and only Cloudflare Access; an address that can receive mail is not a
    claim to the console. The account grants nothing by itself either way —
    node ownership comes from the node claiming it by email
    (services/node_claiming.py), or from an administrator connecting a polled
    radar to the address.

    An account an administrator made for its address (get_or_create_admin_account)
    is found like any other, and marked verified here: the redeemed link is the
    first proof of the address.

    A retired address finds the account that retired it, so a claim typed with
    one binds the node there rather than making a second account. Its link was
    mailed to the account's current address (services/claim_links.py), so
    redeeming it proves that address.

    Raises MagicLinkRefused for a deactivated account, before anything about it
    changes. A claim checks this before it spends its link or binds the node, so
    a deactivated account can neither sign in nor gain a node. An account made
    by a request racing this one goes through the same check: what that request
    made is not this one's to assume.
    """
    email = normalise_email(email)
    async with async_session_maker() as session:
        user = await _account_at(session, email)
        if user is None:
            user, made = await _make_account(session, email, is_verified=True)
            if made:
                return user
        if not user.is_active:
            logging.info("Refusing a magic-link session for a deactivated account")
            raise MagicLinkRefused
        if not user.is_verified:
            user.is_verified = True
            await session.commit()
        return user


async def get_or_create_admin_account(email: str) -> tuple[User, bool]:
    """The account at `email` for an administrator to connect a radar to, and
    whether it was made now.

    A made account is unverified: nobody has proved the address yet, so it
    grants nothing until a sign-in link sent there is redeemed. A retired
    address is an account's already, and finds it.
    """
    email = normalise_email(email)
    async with async_session_maker() as session:
        account = await _account_at(session, email)
        if account is not None:
            return account, False
        return await _make_account(session, email, is_verified=False)


async def accounts_by_id(session: AsyncSession, user_ids: list[str]) -> dict[str, User]:
    """The accounts behind these owner ids, keyed as node_claims holds them.

    An id that names no account is left out.
    """
    ids = set()
    for user_id in user_ids:
        try:
            ids.add(uuid.UUID(user_id))
        except ValueError:
            continue
    if not ids:
        return {}
    users = await session.scalars(select(User).where(User.id.in_(ids)))
    return {str(user.id): user for user in users}
