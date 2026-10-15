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
import time
from collections import deque
from datetime import UTC, datetime

import pytest

import routes.admin as admin
from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.users import ACCESS_ASSERTION_HEADER, ANONYMOUS_USER, User, async_session_maker
from services import claim_links, node_refs, registry_mirror
from services.node_claim_store import claim_status, mark_undeliverable, put_challenge, set_claim_address
from services.node_claiming import complete_claim
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
