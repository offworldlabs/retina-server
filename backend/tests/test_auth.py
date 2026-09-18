"""Tests for auth system: fastapi-users JWT, node ownership, and FastAPI deps."""

import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.ownership_helpers import register_node_row
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


# ── JWT via fastapi-users JWTStrategy ─────────────────────────────────────────


class TestJWT:
    """Verify that fastapi-users' JWTStrategy correctly issues and validates tokens."""

    def _make_user(self, *, is_superuser: bool = False) -> MagicMock:
        user = MagicMock()
        user.id = uuid.uuid4()
        user.email = "test@retina.fm"
        user.is_active = True
        user.is_superuser = is_superuser
        return user

    def test_write_and_read_token_roundtrip(self):
        """A token written by JWTStrategy must be readable back to the same user id."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()

        token = asyncio.run(strategy.write_token(user))
        assert isinstance(token, str)
        assert len(token) > 0

        # read_token looks up the user by id — mock the manager to return our user
        mock_manager = MagicMock()
        mock_manager.get = AsyncMock(return_value=user)

        result = asyncio.run(strategy.read_token(token, mock_manager))
        assert result is not None
        assert result.id == user.id

    def test_token_contains_subject(self):
        """The JWT sub claim must equal the user's id."""
        import jwt as pyjwt

        from core.users import JWT_SECRET, get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        # fastapi-users sets aud=["fastapi-users:auth"] — pass it when decoding
        payload = pyjwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=["fastapi-users:auth"],
        )
        assert payload["sub"] == str(user.id)

    def test_token_has_expiry(self):
        """Token must include an exp claim set in the future."""
        import jwt as pyjwt

        from core.users import JWT_LIFETIME_SECONDS, JWT_SECRET, get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        payload = pyjwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=["fastapi-users:auth"],
        )
        assert "exp" in payload
        assert payload["exp"] > time.time()
        assert payload["exp"] <= time.time() + JWT_LIFETIME_SECONDS + 10

    def test_tampered_token_not_readable(self):
        """read_token on a tampered JWT must return None (not raise)."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        user = self._make_user()
        token = asyncio.run(strategy.write_token(user))

        parts = token.split(".")
        sig = parts[2]
        mid = len(sig) // 2
        tampered_c = "A" if sig[mid] != "A" else "B"
        tampered = f"{parts[0]}.{parts[1]}.{sig[:mid]}{tampered_c}{sig[mid + 1 :]}"

        # read_token with None user_manager returns None for bad tokens
        result = asyncio.run(strategy.read_token(tampered, None))
        assert result is None

    def test_expired_token_rejected(self):
        """A token with a past exp must be rejected by read_token."""
        import jwt as pyjwt

        from core.users import JWT_SECRET, get_jwt_strategy

        payload = {
            "sub": str(uuid.uuid4()),
            "aud": ["fastapi-users:auth"],
            "exp": int(time.time()) - 60,
        }
        expired_token = pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

        strategy = get_jwt_strategy()
        result = asyncio.run(strategy.read_token(expired_token, None))
        assert result is None

    def test_garbage_token_rejected(self):
        """Nonsense strings must return None, not raise."""
        from core.users import get_jwt_strategy

        strategy = get_jwt_strategy()
        for bad in ("", "not.a.jwt", "abc"):
            assert asyncio.run(strategy.read_token(bad, None)) is None


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

    def test_require_admin_non_admin_raises_403(self, tmp_path):
        """A valid token for a non-superuser must yield 403 from require_admin."""
        import jwt as pyjwt
        from fastapi import HTTPException

        from core.users import JWT_SECRET, require_admin

        # Build a valid JWT for a regular (non-superuser) user
        uid = uuid.uuid4()
        payload = {
            "sub": str(uid),
            "aud": ["fastapi-users:auth"],
            "exp": int(time.time()) + 3600,
        }
        token = pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

        request = MagicMock()
        request.cookies = {"auth_token": token}

        # Patch _read_user_from_request so we don't need a real DB
        non_admin_user = MagicMock()
        non_admin_user.is_active = True
        non_admin_user.is_superuser = False

        async def _fake_read(req):
            return non_admin_user

        with patch("core.users.AUTH_BYPASS", False), patch("core.users._read_user_from_request", _fake_read):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(require_admin(request))
        assert exc_info.value.status_code == 403


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
        from core.auth import get_node_owner, list_node_owners, set_node_owner

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
        from core.auth import get_node_owner, get_user_nodes, set_node_owner

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
        from core.auth import set_node_owner

        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", "user-B")

        claim, challenge = await _claim_and_challenge("claimed-node")
        assert (claim.user_id, claim.email, claim.verified, claim.undeliverable) == ("user-B", None, False, False)
        assert challenge is None

    async def test_assigning_the_owner_a_node_already_has_changes_nothing(self):
        from core.auth import set_node_owner

        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", "user-A")

        claim, challenge = await _claim_and_challenge("claimed-node")
        assert (claim.user_id, claim.email, claim.verified) == ("user-A", "ada@example.com", True)
        assert challenge is not None

    async def test_clearing_the_owner_leaves_the_node_unclaimed(self):
        from core.auth import set_node_owner

        await _claimed("claimed-node", "user-A")

        await set_node_owner("claimed-node", None)

        assert await _claim_and_challenge("claimed-node") == (None, None)

    async def test_a_node_that_never_registered_cannot_be_owned(self):
        """The foreign key is the rule; the administrator's route checks first
        so that nobody meets it as a 500."""
        from sqlalchemy.exc import IntegrityError

        from core.auth import get_node_owner, set_node_owner

        with pytest.raises(IntegrityError):
            await set_node_owner("never-registered", "user-A")
        assert await get_node_owner("never-registered") is None
