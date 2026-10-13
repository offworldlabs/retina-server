"""Tests for the auth system: the users.db engine, the anonymous-admin flag,
the FastAPI dependencies, node ownership, and who a sign-in link is issued to."""

import asyncio
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event, select
from starlette.requests import Request

from core import sessions
from core.auth import INTENT_SIGNIN, create_magic_link
from core.users import MagicLink, User, async_session_maker
from services.claim_links import INTENT_CLAIM
from tests.ownership_helpers import get_node_owner, register_node_row, set_node_owner
from tests.probe_helpers import run_probe

# ── SQLite durability pragmas ─────────────────────────────────────────────────


class TestSqlitePragmas:
    """The users.db engine MUST run in WAL mode with safety pragmas.

    Without WAL, a crash mid-commit can leave the database file in a state
    that the next process can't read — and we'd lose every user and
    node-ownership record. This test exists so that accidentally removing the
    `_set_sqlite_pragmas` event listener fails loudly in CI rather than
    silently shipping to prod.
    """

    @pytest.mark.asyncio
    async def test_engine_uses_wal_and_safety_pragmas(self):
        from core.users import engine

        async with engine.connect() as conn:
            jm = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            sync = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
            fk = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
            busy = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()

        assert str(jm).lower() == "wal", f"journal_mode must be WAL, got {jm!r}"
        # synchronous=NORMAL is integer 1 in SQLite's PRAGMA reply
        assert int(sync) == 1, f"synchronous must be NORMAL (1), got {sync!r}"
        assert int(fk) == 1, f"foreign_keys must be ON (1), got {fk!r}"
        assert int(busy) >= 1000, f"busy_timeout must be ≥1000ms, got {busy!r}"


# ── AUTH_ENABLED / AUTH_BYPASS derivation ─────────────────────────────────────


class TestAuthFlagDerivation:
    """The anonymous-admin bypass must be opt-in, never implied by RETINA_ENV.

    Production ran RETINA_ENV=test purely to keep this bypass, losing five
    unrelated guards with it. These tests pin the flag as the only way in.

    `_derive_auth_flags` is exercised directly rather than by reloading
    core.users: routes/auth.py and routes/streaming.py import AUTH_BYPASS by
    value, so a reload would leave those copies pointing at the old bool and
    poison every later test in the session.
    """

    def test_test_env_alone_does_not_enable_bypass(self):
        """The regression guard: no flag, no bypass, whatever the env is called."""
        from core.users import _derive_auth_flags

        auth_enabled, bypass = _derive_auth_flags({"RETINA_ENV": "test"})
        assert auth_enabled is True
        assert bypass is False

    @pytest.mark.parametrize("env_name", ["dev", "test", "staging", "production"])
    def test_no_environment_name_enables_bypass(self, env_name):
        from core.users import _derive_auth_flags

        assert _derive_auth_flags({"RETINA_ENV": env_name})[1] is False

    def test_flag_enables_bypass_in_production(self):
        """The point of the change: prod can name itself honestly and still opt in."""
        from core.users import _derive_auth_flags

        auth_enabled, bypass = _derive_auth_flags({"RETINA_ENV": "production", "AUTH_ALLOW_ANONYMOUS_ADMIN": "1"})
        assert auth_enabled is True
        assert bypass is True

    def test_flag_alone_enables_bypass_with_no_environment_set(self):
        from core.users import _derive_auth_flags

        assert _derive_auth_flags({"AUTH_ALLOW_ANONYMOUS_ADMIN": "1"})[1] is True

    def test_configured_access_beats_the_flag(self):
        """A real identity provider must never be shadowed by an anonymous
        admin. Access is what admits an administrator, so it is what the bypass
        defers to."""
        from core.users import _derive_auth_flags

        auth_enabled, bypass = _derive_auth_flags(
            {
                "CF_ACCESS_TEAM_DOMAIN": "offworldlab.cloudflareaccess.com",
                "CF_ACCESS_AUD": "a" * 64,
                "AUTH_ALLOW_ANONYMOUS_ADMIN": "1",
            }
        )
        assert auth_enabled is True
        assert bypass is False

    @pytest.mark.parametrize("configured_key", ["CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD"])
    def test_half_configured_access_does_not_beat_the_flag(self, configured_key):
        """core.users only consults the verifier when both are set, so half a
        configuration admits nobody and must not shadow the bypass either."""
        from core.users import _derive_auth_flags

        assert _derive_auth_flags({configured_key: "x", "AUTH_ALLOW_ANONYMOUS_ADMIN": "1"})[1] is True

    def test_auth_is_enabled_without_any_provider_configured(self):
        """AUTH_ENABLED must not key off a provider's keys, or removing a
        provider turns authentication off and reopens the path the bypass used
        to take. Sign-in by mailed link needs no boot-time configuration here."""
        from core.users import _derive_auth_flags

        assert _derive_auth_flags({})[0] is True

    @pytest.mark.parametrize("value", ["", "0", "true", "True", "yes", "on", " 1", "1 "])
    def test_only_the_literal_one_enables_bypass(self, value):
        """Matches SYNTHETIC_FLEET_ENABLED / COVERAGE_ENABLED: exactly "1", nothing else."""
        from core.users import _derive_auth_flags

        assert _derive_auth_flags({"AUTH_ALLOW_ANONYMOUS_ADMIN": value})[1] is False

    def test_module_flags_come_from_the_derivation(self):
        """Guards the wiring: the helper is what the module actually uses."""
        import os

        import core.users as _users

        assert _users._derive_auth_flags(os.environ) == (_users.AUTH_ENABLED, _users.AUTH_BYPASS)


# ── The admin boundary, at the route layer ────────────────────────────────────

# Run in a child interpreter (see tests/probe_helpers.py for why), printing one
# machine-readable line for the parent to assert on. The TestClient is
# deliberately not entered as a context manager: __enter__ runs main's lifespan,
# which binds the radar TCP port and starts every background task.
# /api/admin/events reads an in-memory deque, so the request path under test
# needs neither, and nothing here can collide with the suites other worktrees
# run concurrently.
_ROUTE_PROBE = """
import json

from fastapi.testclient import TestClient

import main
from core.users import AUTH_BYPASS

client = TestClient(main.app, raise_server_exceptions=False)
response = client.get("/api/admin/events")
print("PROBE:" + json.dumps({"bypass": AUTH_BYPASS, "status": response.status_code}))
"""


def _probe_admin_route(*, with_flag: bool, db_path) -> dict:
    """Boot the app in a subprocess and report what an anonymous caller gets."""
    env = os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db_path)}
    # The flag must be the only difference between the two runs, and absent has
    # to mean absent rather than empty. A configured Cloudflare Access would
    # suppress the bypass on its own (see _derive_auth_flags), which would make a
    # 401 prove nothing about the flag, so it is cleared from both.
    for key in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "AUTH_ALLOW_ANONYMOUS_ADMIN"):
        env.pop(key, None)
    if with_flag:
        env["AUTH_ALLOW_ANONYMOUS_ADMIN"] = "1"

    return run_probe(_ROUTE_PROBE, env)


def test_admin_route_refuses_anonymous_callers_without_the_flag(tmp_path):
    """A deployment that never sets AUTH_ALLOW_ANONYMOUS_ADMIN must return 401.

    TestAuthFlagDerivation above pins the derivation; this pins what the routes
    then do with it, which is the part the deployment actually depends on.

    It has to be a subprocess. routes/auth.py and routes/streaming.py import
    AUTH_BYPASS by value at import time, and conftest.py sets the flag before
    anything imports core.users, so in this interpreter the bypass is on and
    cannot be turned off: patching core.users.AUTH_BYPASS leaves those two
    copies untouched, and reloading core.users leaves them stale. A child
    interpreter with its own environment is the only way to see the flag off.

    RETINA_ENV=test in both runs is the regression guard: the env name used to
    grant the bypass by itself, and now buys nothing.
    """
    denied = _probe_admin_route(with_flag=False, db_path=tmp_path / "no_flag.db")
    assert denied == {"bypass": False, "status": 401}

    # Positive control: same harness, same environment bar the flag. Without it
    # a 401 could just as well mean the probe never reached the route.
    allowed = _probe_admin_route(with_flag=True, db_path=tmp_path / "with_flag.db")
    assert allowed == {"bypass": True, "status": 200}


# ── get_current_user / require_admin dependencies ─────────────────────────────


async def _account(email: str = "ada@example.com", *, is_superuser: bool = False) -> User:
    """An account row, as a redeemed link or an administrator leaves one."""
    account = User(id=uuid.uuid4(), email=email, name=email.split("@")[0], is_superuser=is_superuser)
    async with async_session_maker() as session:
        async with session.begin():
            session.add(account)
    return account


def _request(cookie: str | None = None) -> Request:
    """A request carrying `cookie` as the auth_token cookie, as a browser sends it."""
    headers = [(b"cookie", f"auth_token={cookie}".encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


async def _signed_in(account: User) -> Request:
    """A request from a browser that has just signed `account` in."""
    token, _ = await sessions.open_session(account, user_agent=None, ip=None)
    return _request(token)


class TestAuthDependencies:
    def test_get_current_user_auth_disabled_returns_anonymous(self):
        from core.users import get_current_user

        request = MagicMock()
        with patch("core.users.AUTH_BYPASS", True):
            user = asyncio.run(get_current_user(request))
        assert user["role"] == "admin"
        assert user["id"] == "00000000-0000-0000-0000-000000000000"

    def test_require_admin_auth_disabled_returns_anonymous(self):
        from core.users import require_admin

        request = MagicMock()
        with patch("core.users.AUTH_BYPASS", True):
            user = asyncio.run(require_admin(request))
        assert user["role"] == "admin"

    def test_get_current_user_missing_cookie_raises_401(self):
        from fastapi import HTTPException
        from starlette.datastructures import State

        from core.users import get_current_user

        request = MagicMock()
        request.cookies = {}
        request.state = State()
        with patch("core.users.AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    def test_get_current_user_invalid_token_raises_401(self):
        from fastapi import HTTPException
        from starlette.datastructures import State

        from core.users import get_current_user

        request = MagicMock()
        request.cookies = {"auth_token": "invalid.jwt.token"}
        request.state = State()
        with patch("core.users.AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_current_user(request))
        assert exc_info.value.status_code == 401

    async def test_get_current_user_session_cookie_returns_its_account(self):
        from core.users import get_current_user

        ada = await _account()
        with patch("core.users.AUTH_BYPASS", False):
            user = await get_current_user(await _signed_in(ada))
        assert (user["id"], user["email"], user["role"], user["is_superuser"]) == (
            str(ada.id),
            "ada@example.com",
            "user",
            False,
        )

    async def test_get_optional_user_without_a_cookie_is_nobody(self):
        from core.users import get_optional_user

        with patch("core.users.AUTH_BYPASS", False):
            assert await get_optional_user(_request()) is None

    async def test_get_optional_user_session_cookie_returns_its_account(self):
        from core.users import get_optional_user

        ada = await _account()
        with patch("core.users.AUTH_BYPASS", False):
            user = await get_optional_user(await _signed_in(ada))
        assert user is not None
        assert (user["id"], user["role"]) == (str(ada.id), "user")

    async def test_get_current_user_deactivated_account_raises_401(self):
        """Deactivating ends the session the cookie names, so the next request
        it rides on is nobody's."""
        from fastapi import HTTPException

        from core.users import get_current_user

        ada = await _account()
        request = await _signed_in(ada)
        await sessions.deactivate_account(ada.id)
        with patch("core.users.AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(request)
        assert exc_info.value.status_code == 401

    async def test_require_admin_non_admin_raises_403(self):
        """A live session for an ordinary account is signed in, and still 403."""
        from fastapi import HTTPException

        from core.users import require_admin

        ada = await _account()
        with patch("core.users.AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin(await _signed_in(ada))
        assert exc_info.value.status_code == 403

    async def test_require_admin_superuser_account_raises_403(self):
        """is_superuser grants nothing: an administrator is a Cloudflare Access
        identity or the anonymous admin, never a row, so an account flagged as
        one is refused like any other and reported as an ordinary user."""
        from fastapi import HTTPException

        from core.users import get_current_user, require_admin

        root = await _account("root@example.com", is_superuser=True)
        request = await _signed_in(root)
        with patch("core.users.AUTH_BYPASS", False):
            with pytest.raises(HTTPException) as exc_info:
                await require_admin(request)
            user = await get_current_user(request)
        assert exc_info.value.status_code == 403
        assert (user["email"], user["role"], user["is_superuser"]) == ("root@example.com", "user", False)


# ── Shared DB fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
def clean_auth_tables():
    """Wipe all auth-related tables before a test that requests this fixture."""
    from sqlalchemy import delete

    from core.nodes import NodeClaim, NodeClaimChallenge
    from core.users import async_session_maker, create_db_and_tables

    async def _setup():
        await create_db_and_tables()
        async with async_session_maker() as session:
            await session.execute(delete(NodeClaimChallenge))
            await session.execute(delete(NodeClaim))
            await session.commit()

    asyncio.run(_setup())
    yield


# ── Node ownership ────────────────────────────────────────────────────────────


async def _claimed(node_id: str, owner: str) -> None:
    """A registered node its owner claimed by email, with a challenge still on
    file and every flag set, so that clearing any of them shows."""
    from core.nodes import NodeClaim, NodeClaimChallenge
    from core.users import async_session_maker

    await register_node_row(node_id)
    async with async_session_maker() as session:
        async with session.begin():
            session.add(
                NodeClaim(node_id=node_id, user_id=owner, email="ada@example.com", verified=True, undeliverable=True)
            )
            session.add(
                NodeClaimChallenge(
                    node_id=node_id,
                    email="ada@example.com",
                    handle="0" * 64,
                    created_at=time.time(),
                    expires_at=time.time() + 900,
                )
            )


async def _claim_and_challenge(node_id: str):
    from core.nodes import NodeClaim, NodeClaimChallenge
    from core.users import async_session_maker

    async with async_session_maker() as session:
        return await session.get(NodeClaim, node_id), await session.get(NodeClaimChallenge, node_id)


class TestNodeOwnership:
    @pytest.fixture(autouse=True)
    def _clean_tables(self, clean_auth_tables):
        pass

    async def test_set_and_clear_node_owner(self):
        from core.auth import list_node_owners

        await register_node_row("node-1")
        await register_node_row("node-2")
        await set_node_owner("node-1", "user-A")
        await set_node_owner("node-2", "user-B")
        assert await get_node_owner("node-1") == "user-A"
        assert await list_node_owners() == {"node-1": "user-A", "node-2": "user-B"}
        await set_node_owner("node-1", None)
        assert await get_node_owner("node-1") is None
        assert "node-1" not in await list_node_owners()

    async def test_set_node_owner_updates_existing_owner(self):
        from core.auth import get_user_nodes

        await register_node_row("reassigned-node")
        await set_node_owner("reassigned-node", "user-1")
        await set_node_owner("reassigned-node", "user-2")
        assert await get_node_owner("reassigned-node") == "user-2"
        assert await get_user_nodes("user-1") == []
        assert await get_user_nodes("user-2") == ["reassigned-node"]

    async def test_a_new_owner_takes_the_node_without_the_last_ones_claim(self):
        """Owner and address are one row, so a reassignment clears the address,
        its confirmation, its bounce and the challenge in the write that sets
        the owner, rather than leaving a caller to remember a second one."""
        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", "user-B")

        claim, challenge = await _claim_and_challenge("claimed-node")
        assert claim is not None
        assert (claim.user_id, claim.email, claim.verified, claim.undeliverable) == ("user-B", None, False, False)
        assert challenge is None

    async def test_assigning_the_owner_a_node_already_has_changes_nothing(self):
        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", "user-A")

        claim, challenge = await _claim_and_challenge("claimed-node")
        assert claim is not None
        assert (claim.user_id, claim.email, claim.verified) == ("user-A", "ada@example.com", True)
        assert challenge is not None

    async def test_clearing_the_owner_leaves_the_node_unclaimed(self):
        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", None)

        assert await _claim_and_challenge("claimed-node") == (None, None)

    async def test_a_node_that_never_registered_cannot_be_owned(self):
        """The foreign key is the rule; the administrator's route checks first
        so that nobody meets it as a 500."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await set_node_owner("never-registered", "user-A")
        assert await get_node_owner("never-registered") is None


# ── Who a sign-in link is issued to ───────────────────────────────────────────


@pytest.fixture
def writes():
    """Every INSERT, UPDATE and DELETE the app's engine sends, in order."""
    from core.users import engine

    seen: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _links() -> list[MagicLink]:
    async with async_session_maker() as session:
        return list(await session.scalars(select(MagicLink)))


@pytest.mark.parametrize(
    ("stored", "asked"),
    [
        pytest.param("ada@example.com", "ada@example.com", id="same-address"),
        pytest.param("ada@example.com", "  Ada@Example.COM  ", id="asked-unnormalised"),
        pytest.param("Ada@Example.com", "ada@example.com", id="stored-in-capitals"),
    ],
)
async def test_a_deactivated_account_is_issued_no_sign_in_link(stored, asked, writes):
    """Refused before anything is written, so the request leaves no trace."""
    ada = await _account(stored)
    await sessions.deactivate_account(ada.id)
    writes.clear()

    assert await create_magic_link(asked) is None

    assert writes == []
    assert await _links() == []


async def test_a_deactivated_account_is_still_issued_a_claim_link():
    """Declining a node that names the address takes no account, so the link
    offering that must still go out. The click that would bind the node is what
    refuses a deactivated account."""
    ada = await _account()
    await sessions.deactivate_account(ada.id)

    token = await create_magic_link("ada@example.com", intent=INTENT_CLAIM, node_id="ret1a2b3c4d")

    assert token is not None
    (link,) = await _links()
    assert (link.email, link.intent, link.node_id) == ("ada@example.com", INTENT_CLAIM, "ret1a2b3c4d")


@pytest.mark.parametrize("has_account", [pytest.param(True, id="active-account"), pytest.param(False, id="no-account")])
async def test_an_active_account_or_an_unknown_address_is_issued_a_sign_in_link(has_account):
    if has_account:
        await _account()

    token = await create_magic_link("ada@example.com")

    assert token is not None
    (link,) = await _links()
    assert (link.email, link.intent) == ("ada@example.com", INTENT_SIGNIN)


async def test_a_reactivated_account_is_issued_a_sign_in_link_again():
    """Read at each request, not remembered from the last one."""
    ada = await _account()
    await sessions.deactivate_account(ada.id)
    assert await create_magic_link("ada@example.com") is None

    await sessions.reactivate_account(ada.id)

    assert await create_magic_link("ada@example.com") is not None
