"""A node's claim state as administrators read it: GET /api/admin/nodes/{node_id}/claim.

The owner, the address on file with its confirmation and bounce, and the
pending challenge, which no other route shows them, so a stuck or bounced
claim is diagnosed from here. Each state is built through the calls that make
it in production (services/node_claim_store.py, services/node_claiming.py, the
owner route), and `status` is checked against claim_status, which is what the
node itself is told.

Run as a deployment runs, as tests/test_admin_users.py is.
"""

import asyncio
import sqlite3
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import main
import routes.admin as admin
from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.nodes import NodeClaim
from core.users import (
    ACCESS_ASSERTION_HEADER,
    ANONYMOUS_USER,
    User,
    _set_sqlite_pragmas,
    async_session_maker,
    get_async_session,
)
from services import claim_links, node_refs, registry_mirror
from services.node_claim_store import claim_status, mark_undeliverable, put_challenge, set_claim_address
from services.node_claiming import complete_claim
from tests.migration_helpers import _alembic
from tests.ownership_helpers import register_node_row, set_node_owner

OPERATOR = "operator@example.com"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}

ADA = "ada@example.com"
NODE = "ret1a2b3c4d"


class _Access:
    """Stands in for core.users.access_identity, verifying ASSERTION as OPERATOR."""

    def is_configured(self) -> bool:
        return True

    async def identity(self, token: str | None) -> str | None:
        return OPERATOR if token == ASSERTION else None


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch, tmp_path):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    monkeypatch.setattr(users, "access_identity", _Access())
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


@pytest.fixture
def ref() -> str:
    """NODE, registered and nothing else, and its ref."""
    node_ref = asyncio.run(register_node_row(NODE))
    node_refs._reset_for_tests()
    return node_ref


async def _offer(email: str, *, expires_at: float | None = None) -> str:
    """Offer `email` for NODE with a challenge in the post, as the node route
    records one, and return the claim link's token."""
    challenge = await claim_links.issue(email=email, node_id=NODE, now=time.time())
    assert challenge is not None
    async with async_session_maker() as db:
        async with db.begin():
            await set_claim_address(db, NODE, email)
            await put_challenge(
                db, NODE, email, challenge.handle, challenge.expires_at if expires_at is None else expires_at
            )
    return challenge.token


def offer(email: str, *, expires_at: float | None = None) -> str:
    return asyncio.run(_offer(email, expires_at=expires_at))


def _claim(client, node_id: str = NODE) -> dict:
    r = client.get(f"/api/admin/nodes/{node_id}/claim", headers=AS_ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _told(node_id: str = NODE) -> str:
    """What the node itself is told its claim state is."""

    async def _read() -> str:
        async with async_session_maker() as db:
            return (await claim_status(db, node_id, time.time())).state

    return asyncio.run(_read())


def _between(value: float, before: float) -> bool:
    return before - 1 <= value <= time.time() + 1


class TestEachState:
    def test_an_owner_who_clicked_reads_owned_and_verified(self, client, ref):
        before = time.time()
        token = offer(ADA)
        _outcome, user, _ = asyncio.run(complete_claim(token))
        assert user is not None

        claim = _claim(client)

        assert {k: claim[k] for k in ("node_id", "node_ref", "status", "email", "verified", "undeliverable")} == {
            "node_id": NODE,
            "node_ref": ref,
            "status": "owned",
            "email": ADA,
            "verified": True,
            "undeliverable": False,
        }
        assert claim["owner"] == {"user_id": str(user.id), "email": ADA, "name": "ada"}
        # The click spent the challenge.
        assert claim["challenge"] is None
        assert _between(claim["updated_at"], before)
        assert _between(claim["owned_since"], before)
        assert claim["status"] == _told()

    def test_an_owner_an_administrator_assigned_reads_owned_and_unverified(self, client, ref):
        made, _ = asyncio.run(users.get_or_create_admin_account(ADA))
        before = time.time()
        r = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": str(made.id)}, headers=AS_ADMIN)
        assert r.status_code == 200

        claim = _claim(client)

        assert claim["status"] == "owned" == _told()
        assert claim["owner"] == {"user_id": str(made.id), "email": ADA, "name": "ada"}
        # No address was ever offered or confirmed for it.
        assert (claim["email"], claim["verified"], claim["undeliverable"]) == (None, False, False)
        assert _between(claim["owned_since"], before)
        assert claim["challenge"] is None

    def test_an_owner_a_pushed_registry_set_reads_owned_since_the_rows_last_write(self, client, ref):
        """A claim row as production's registry mirror writes it: the owner by
        address, updated_at as production stamped it, and no owned_since, which
        production's schema does not have. The owner's routes start their
        tenure at updated_at, and so does this."""
        stamped = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        row = {"node_id": NODE, "email": None, "verified": False, "undeliverable": False}
        row |= {"updated_at": stamped.isoformat(), "owner_email": ADA}
        asyncio.run(registry_mirror.apply({"upserts": {"node_claims": [row]}, "seq": 0, "last": True}))

        claim = _claim(client)

        assert (claim["status"], claim["owner"]["email"]) == ("owned", ADA)
        assert claim["owned_since"] == stamped.timestamp()

    def test_a_live_challenge_reads_pending_with_its_challenge(self, client, ref):
        before = time.time()
        offer(ADA)

        claim = _claim(client)

        assert claim["status"] == "pending" == _told()
        assert (claim["owner"], claim["email"], claim["verified"], claim["undeliverable"]) == (None, ADA, False, False)
        assert claim["owned_since"] is None
        challenge = claim["challenge"]
        assert (challenge["email"], challenge["expired"]) == (ADA, False)
        assert _between(challenge["created_at"], before)
        assert challenge["expires_at"] > time.time()

    def test_an_expired_challenge_reads_unclaimed_with_the_challenge_marked_expired(self, client, ref):
        expired_at = time.time() - 60
        offer(ADA, expires_at=expired_at)

        claim = _claim(client)

        assert claim["status"] == "unclaimed" == _told()
        # The address stays on file, so a fresh challenge needs no retyping.
        assert claim["email"] == ADA
        assert claim["challenge"]["email"] == ADA
        assert claim["challenge"]["expires_at"] == expired_at
        assert claim["challenge"]["expired"] is True

    def test_a_bounced_address_reads_unclaimed_and_undeliverable(self, client, ref):
        offer(ADA)
        asyncio.run(_bounce(ADA))

        claim = _claim(client)

        assert claim["status"] == "unclaimed" == _told()
        assert (claim["email"], claim["undeliverable"], claim["verified"]) == (ADA, True, False)
        # The bounce dropped the challenge, which nobody could click.
        assert claim["challenge"] is None

    def test_a_node_nobody_has_claimed_reads_unclaimed_with_nothing_on_file(self, client, ref):
        assert _claim(client) == {
            "node_id": NODE,
            "node_ref": ref,
            "status": "unclaimed",
            "owner": None,
            "email": None,
            "verified": False,
            "undeliverable": False,
            "updated_at": None,
            "owned_since": None,
            "challenge": None,
        }
        assert _told() == "unclaimed"

    def test_an_owner_with_no_account_row_is_named_by_id_alone(self, client, ref):
        """An owner can be an administrator's own identity, which has no row."""
        asyncio.run(set_node_owner(NODE, ANONYMOUS_USER["id"]))

        claim = _claim(client)

        assert claim["owner"] == {"user_id": ANONYMOUS_USER["id"], "email": None, "name": None}

    def test_a_node_the_ref_map_does_not_know_yet_has_no_ref(self, client):
        """The ref comes from the same cached map the admin node-refs route reads,
        and a node registered since it was loaded is not in it yet."""
        node_refs.owner_identity(NODE)
        asyncio.run(register_node_row(NODE))

        assert _claim(client)["node_ref"] is None

    @pytest.mark.parametrize("node_id", ["ret00000000", "never-registered"])
    def test_a_node_with_no_registration_is_404(self, client, ref, node_id):
        r = client.get(f"/api/admin/nodes/{node_id}/claim", headers=AS_ADMIN)

        assert r.status_code == 404
        assert r.json() == {"detail": "Node not found"}


async def _bounce(email: str) -> None:
    async with async_session_maker() as db:
        async with db.begin():
            await mark_undeliverable(db, NODE, email)


class TestOnlyAnAdministratorMay:
    def test_no_credentials_is_401(self, client, ref):
        r = client.get(f"/api/admin/nodes/{NODE}/claim")

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    def test_a_signed_in_account_is_403_even_for_its_own_node(self, client, ref):
        account: User = asyncio.run(users.get_or_create_magic_link_user(ADA))
        asyncio.run(set_node_owner(NODE, str(account.id)))
        token, _ = asyncio.run(sessions.open_session(account, user_agent="Mozilla/5.0 (X11; Linux x86_64)", ip=None))

        r = client.get(f"/api/admin/nodes/{NODE}/claim", headers={"Cookie": f"{sessions.COOKIE_NAME}={token}"})

        assert r.status_code == 403
        assert r.json() == {"detail": "Admin access required"}


class TestAnOwnerAssignedByAnySpellingOfTheId:
    """uuid.UUID reads one account id in several spellings, and an
    administrator's client sends whichever it holds. node_claims is matched
    against the account's own form exactly, so that is the form an assignment
    stores, or the node is owned by an id no read by account finds."""

    @pytest.mark.parametrize(
        "spell",
        [str.upper, lambda i: i.replace("-", ""), lambda i: f"{{{i}}}", lambda i: f"urn:uuid:{i}"],
        ids=["upper-case", "undashed", "braced", "urn"],
    )
    def test_the_owner_is_stored_as_the_accounts_own_id(self, client, ref, spell):
        made, _ = asyncio.run(users.get_or_create_admin_account(ADA))
        user_id = str(made.id)

        r = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": spell(user_id)}, headers=AS_ADMIN)

        assert r.status_code == 200
        assert r.json()["user_id"] == user_id
        assert _claim(client)["owner"] == {"user_id": user_id, "email": ADA, "name": "ada"}
        account = client.get(f"/api/admin/users/{user_id}", headers=AS_ADMIN).json()
        assert account["user"]["node_count"] == 1
        assert [node["node_id"] for node in account["nodes"]] == [NODE]
        events = client.get(f"/api/admin/events?user_id={user_id}", headers=AS_ADMIN).json()
        assert [event["meta"]["node_id"] for event in events] == [NODE]

    def test_the_owner_it_already_has_in_another_spelling_keeps_the_claim(self, client, ref):
        token = offer(ADA)
        _outcome, user, _ = asyncio.run(complete_claim(token))
        assert user is not None
        before = _claim(client)

        r = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": str(user.id).upper()}, headers=AS_ADMIN)

        assert r.status_code == 200
        after = _claim(client)
        assert after["owner"]["user_id"] == str(user.id)
        assert (after["email"], after["verified"], after["owned_since"]) == (ADA, True, before["owned_since"])


# What the owner route stored for Ada while it kept the spelling it was sent:
# her id upper case and undashed, in both owner columns.
OWNED_SINCE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _legacy(user_id: str) -> str:
    return user_id.replace("-", "").upper()


def _the_rest(claim: NodeClaim) -> tuple:
    """Everything on a claim but its owner ids."""
    return (claim.email, claim.verified, claim.undeliverable, claim.declined, claim.updated_at, claim.owned_since)


async def _read_row(node_id: str = NODE) -> NodeClaim:
    async with async_session_maker() as db:
        row = await db.get(NodeClaim, node_id)
        assert row is not None
        return row


@contextmanager
def _serving(db: Path) -> Iterator[TestClient]:
    """The app with every request's session on `db`, as conftest's node_client
    serves node_session's. A connection per session rather than a pool: each
    request runs on an event loop of its own, which a pooled connection would
    outlive."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}", poolclass=NullPool)
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with maker() as session:
            yield session

    saved = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_async_session] = _session
    client = TestClient(main.app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(saved)
        asyncio.run(engine.dispose())


def _at_0026_from_a_legacy_owner(db: Path, account_id: str) -> None:
    """A database at 0025 holding Ada's account and NODE, owned by her under
    the legacy spelling with the address she claimed it with, migrated to 0026."""
    up = _alembic("upgrade", "0025", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr
    con = sqlite3.connect(db)
    try:
        con.execute(
            'INSERT INTO "user" (id, email, name, hashed_password, is_active, is_superuser, is_verified) '
            "VALUES (?, ?, 'ada', 'x', 1, 0, 1)",
            (account_id, ADA),
        )
        con.execute("INSERT INTO nodes (node_id, node_ref) VALUES (?, 'nde1a2b3c4d00')", (NODE,))
        con.execute(
            "INSERT INTO node_claims (node_id, user_id, email, verified, undeliverable, declined, updated_at, "
            "owned_since, owned_since_user_id) VALUES (?, ?, ?, 1, 0, 0, ?, ?, ?)",
            (
                NODE,
                _legacy(account_id),
                ADA,
                UPDATED_AT.replace(tzinfo=None).isoformat(sep=" "),
                OWNED_SINCE.replace(tzinfo=None).isoformat(sep=" "),
                _legacy(account_id),
            ),
        )
        con.commit()
    finally:
        con.close()
    up = _alembic("upgrade", "0026", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr


class TestAnOwnerStoredInAnotherSpelling:
    """What the owner route left before it stored the account's own id: the
    node owned under a spelling that no read by account matches. Migration
    0026 rewrites those ids, and set_owner reads one as the account it names,
    so neither the reads nor a repeated assignment take it for somebody else."""

    def test_assigning_that_account_again_keeps_the_claim(self, client, ref):
        account = asyncio.run(users.get_or_create_magic_link_user(ADA))
        user_id = str(account.id)

        async def _legacy_claim() -> None:
            async with async_session_maker() as db:
                async with db.begin():
                    db.add(
                        NodeClaim(
                            node_id=NODE,
                            user_id=_legacy(user_id),
                            email=ADA,
                            verified=True,
                            undeliverable=False,
                            declined=False,
                            updated_at=UPDATED_AT,
                            owned_since=OWNED_SINCE,
                            owned_since_user_id=_legacy(user_id),
                        )
                    )

        asyncio.run(_legacy_claim())
        before = asyncio.run(_read_row())

        r = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": user_id}, headers=AS_ADMIN)

        assert r.status_code == 200
        after = asyncio.run(_read_row())
        assert (after.user_id, after.owned_since_user_id) == (user_id, user_id)
        assert _the_rest(after) == _the_rest(before) == (ADA, True, False, False, UPDATED_AT, OWNED_SINCE)
        assert _claim(client)["owner"] == {"user_id": user_id, "email": ADA, "name": "ada"}
        assert client.get(f"/api/admin/users/{user_id}", headers=AS_ADMIN).json()["user"]["node_count"] == 1

    def test_after_0026_every_read_by_account_finds_the_node_and_assigning_it_again_keeps_it(self, tmp_path):
        user_id = "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"
        db = tmp_path / "legacy.db"
        _at_0026_from_a_legacy_owner(db, user_id)

        with _serving(db) as client:
            listed = client.get("/api/admin/users", headers=AS_ADMIN)
            assert listed.status_code == 200, listed.text
            assert [(u["id"], u["node_count"]) for u in listed.json()["users"]] == [(user_id, 1)]

            account = client.get(f"/api/admin/users/{user_id}", headers=AS_ADMIN)
            assert account.status_code == 200, account.text
            assert account.json()["user"]["node_count"] == 1
            assert [n["node_id"] for n in account.json()["nodes"]] == [NODE]
            assert account.json()["claims"] == []

            claim = client.get(f"/api/admin/nodes/{NODE}/claim", headers=AS_ADMIN)
            assert claim.status_code == 200, claim.text
            assert claim.json()["owner"] == {"user_id": user_id, "email": ADA, "name": "ada"}
            assert claim.json()["status"] == "owned"

            assigned = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": user_id}, headers=AS_ADMIN)
            assert assigned.status_code == 200, assigned.text

        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT user_id, owned_since_user_id, email, verified, undeliverable, declined, updated_at, "
                "owned_since FROM node_claims WHERE node_id = ?",
                (NODE,),
            ).fetchone()
        finally:
            con.close()
        assert row == (
            user_id,
            user_id,
            ADA,
            1,
            0,
            0,
            UPDATED_AT.replace(tzinfo=None).isoformat(sep=" "),
            OWNED_SINCE.replace(tzinfo=None).isoformat(sep=" "),
        )
