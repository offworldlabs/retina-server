"""fastapi-users: battle-tested JWT + cookie auth backed by SQLite.

User model, auth backend, and FastAPI dependency helpers live here.
All JWT issuance/verification is delegated to fastapi-users' JWTStrategy.
"""

import hashlib
import logging
import os
import secrets
import uuid
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users.exceptions import UserAlreadyExists, UserNotExists
from sqlalchemy import DateTime, Float, String, event, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.access_identity import AccessIdentity

# ── Config ────────────────────────────────────────────────────────────────────

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()
_jwt_from_env = os.getenv("JWT_SECRET", "")
if not _jwt_from_env and _RETINA_ENV not in ("dev", "test"):
    raise RuntimeError(
        "JWT_SECRET environment variable is required in production "
        f"(RETINA_ENV={_RETINA_ENV!r}). Set it to a random ≥32-byte string."
    )

JWT_SECRET = _jwt_from_env or "retina-dev-secret-change-me-in-prod-32b!"
JWT_LIFETIME_SECONDS = 86400 * 7  # 7 days


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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
# RETINA_DB_PATH exists so tests and one-off migrations can point at a scratch
# file. It is unset in every deployed environment, where the path derives from
# this module's own location and lands inside the backend-data volume.
# A relative override is resolved against the current working directory, which
# is not the same for every caller: alembic runs with cwd backend/, while the
# application may not. Resolving here makes the same override mean the same
# file regardless of caller.
_DB_PATH = Path(os.getenv("RETINA_DB_PATH") or _DATA_DIR / "users.db").resolve()
DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"

# ── SQLAlchemy setup ─────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """Extends fastapi-users base with radar-specific profile fields."""

    name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    avatar: Mapped[str] = mapped_column(String(512), default="", server_default="")
    provider: Mapped[str] = mapped_column(String(50), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class MagicLink(Base):
    """One outstanding emailed link: a sign-in, or a node claim, told apart by
    `intent`.

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


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


# ── fastapi-users schemas ─────────────────────────────────────────────────────


class UserRead(schemas.BaseUser[uuid.UUID]):
    name: str
    avatar: str
    provider: str
    created_at: datetime


class UserCreate(schemas.BaseUserCreate):
    name: str = ""
    avatar: str = ""
    provider: str = ""


class UserUpdate(schemas.BaseUserUpdate):
    name: str | None = None
    avatar: str | None = None
    provider: str | None = None


# ── UserManager ──────────────────────────────────────────────────────────────

# Derive distinct secrets so reset and verify tokens can't be cross-used
_RESET_SECRET = hashlib.sha256(b"reset:" + JWT_SECRET.encode()).hexdigest()
_VERIFY_SECRET = hashlib.sha256(b"verify:" + JWT_SECRET.encode()).hexdigest()


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = _RESET_SECRET
    verification_token_secret = _VERIFY_SECRET


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


# ── Auth backend: Cookie transport + JWT strategy ─────────────────────────────

cookie_transport = CookieTransport(
    cookie_name="auth_token",
    cookie_max_age=JWT_LIFETIME_SECONDS,
    cookie_httponly=True,
    cookie_secure=True,
    cookie_samesite="lax",
)


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=JWT_SECRET, lifetime_seconds=JWT_LIFETIME_SECONDS)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

# ── Helper: anonymous user (auth disabled in dev) ─────────────────────────────

ANONYMOUS_USER: dict = {
    "id": "00000000-0000-0000-0000-000000000000",
    "email": "admin@retina.fm",
    "name": "Admin (no auth)",
    "avatar": "",
    "provider": "none",
    "role": "admin",
    "is_superuser": True,
    "created_at": 0,
}


def user_to_dict(user: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "avatar": user.avatar,
        "provider": user.provider,
        "role": "admin" if user.is_superuser else "user",
        "is_superuser": user.is_superuser,
        "created_at": user.created_at.timestamp() if user.created_at else 0,
    }


# ── FastAPI dependency helpers ────────────────────────────────────────────────
# These wrap fastapi-users' JWT strategy so the rest of the codebase can call
# them with just a Request — no change to route signatures needed.

_SENTINEL = object()


async def read_user_from_token(token: str | None) -> User | None:
    """Validate a raw auth_token JWT and return the User, or None if invalid.

    Usable outside the request/response cycle (e.g. WebSocket handshakes, where
    the cookie is read off ws.cookies rather than a Request).
    """
    if not token:
        return None
    strategy = get_jwt_strategy()
    async with async_session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)
        try:
            return await strategy.read_token(token, user_manager)
        except Exception:
            return None


async def _read_user_from_request(request: Request) -> User | None:
    """Validate the auth_token cookie using fastapi-users' JWTStrategy.

    Result is cached on request.state to avoid repeated DB lookups per request.
    """
    cached = getattr(request.state, "_auth_user", _SENTINEL)
    if cached is not _SENTINEL:
        return cached
    user = await read_user_from_token(request.cookies.get("auth_token"))
    request.state._auth_user = user
    return user


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
        "avatar": "",
        "provider": "cloudflare-access",
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
    """Like get_current_user but also enforces superuser/admin role.

    Over the same resolution, rather than its own copy of it: a fourth source
    of identity added to get_optional_user must not have to be remembered here
    as well. An Access assertion and the bypass both yield is_superuser, which
    is why they pass the check below rather than skipping it — membership of
    the Access group is what grants the console, and the bypass hands out an
    administrator by definition.
    """
    user = await get_optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.get("is_superuser"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Magic-link user creation helper ───────────────────────────────────────────


class MagicLinkRefused(Exception):
    """This address may not hold a session opened by a sign-in link.

    Carries no detail on purpose: the route answers it exactly as it answers a
    token that never existed.
    """


async def get_or_create_magic_link_user(email: str) -> User:
    """Find or create the account a redeemed link belongs to, sign-in or claim.

    No address list is consulted, so an account reached this way is never a
    superuser. Administrator identity is Cloudflare Access and only Cloudflare
    Access; an address that can receive mail is not a claim to the console. The
    account grants nothing by itself either way — node ownership comes from the
    node claiming it by email (services/node_claiming.py).

    Raises MagicLinkRefused for an account that is already a superuser. The
    invariant has to hold for a row that exists, not only for one created here,
    or whoever can read an administrator's mailbox holds an admin session
    without ever meeting Access. Nothing creates such a row today, which is
    exactly why the guard is worth having: it is the thing that keeps being
    true if something later does.
    """
    email = email.lower().strip()

    def _guard(user: User) -> User:
        if user.is_superuser:
            logging.warning("Refusing a magic-link session for a superuser account")
            raise MagicLinkRefused
        return user

    async with async_session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user_manager = UserManager(user_db)

        try:
            return _guard(await user_manager.get_by_email(email))
        except UserNotExists:
            user_create = UserCreate(
                email=email,
                # Never used: there is no password login. fastapi-users requires
                # the field, and a random value is safer than a known one.
                password=secrets.token_urlsafe(32),
                name=email.split("@")[0],
                avatar="",
                provider="magic-link",
                # Redeeming the link is the proof of the address.
                is_verified=True,
                is_superuser=False,
            )
            try:
                return await user_manager.create(user_create)
            except UserAlreadyExists:
                # Race: another redemption created this user between our get
                # and create. Through the same guard, since what that other
                # request created is not this one's to assume.
                return _guard(await user_manager.get_by_email(email))
