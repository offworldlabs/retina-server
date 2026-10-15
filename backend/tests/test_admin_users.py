"""Managing accounts from the admin API: the paged list, one account's page, a
sign-in link sent on its behalf, deleting it, and the event log narrowed to it.

The routes are in routes/admin.py. Every test here runs as a deployment does,
as tests/test_admin_user_state.py does: the anonymous-admin bypass off, the
administrator a verified Access identity, and an account its auth_token cookie.
Claim state on its own is tests/test_admin_claims.py.
"""

import asyncio
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

import routes.admin as admin
import routes.streaming as streaming
from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.auth import INTENT_SIGNIN, MAGIC_LINK_EXPIRY_S, create_magic_link
from core.mail_outbox import MailOutbox
from core.nodes import Node, NodeClaim, NodeEvent
from core.users import (
    ACCESS_ASSERTION_HEADER,
    JWT_SECRET,
    MagicLink,
    RetiredAddress,
    RetiredLegacyToken,
    User,
    UserSession,
    async_session_maker,
    engine,
)
from services import accounts, blah2_poller, claim_links, mail, mail_outbox, mail_templates, node_refs
from services.node_claim_store import mark_undeliverable, put_challenge, set_claim_address
from tests.claim_mail_helpers import HOST_APP, configure_mail
from tests.ownership_helpers import own, own_registered_node, register_node_row
from tests.test_owner_socket_sessions import _assert_closed_promptly, _Checks, _eventually, _owner_feed

OPERATOR = "operator@example.com"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}

ADA = "ada@example.com"
GRACE = "grace@example.com"
FLEET_NODE = "ret1a2b3c4d"
POLLED_NODE = "bla1a2b3c4d"
# Invented ids from tests/test_no_real_identities.py's allow-list.
SECOND_NODE = "ret9f8e7d6c"
PENDING_NODE = "retdeadbeef"
EXPIRED_NODE = "retffffffff"
BOUNCED_NODE = "ret2b3c4d5e"
GRACES_OFFER = "retc0ffee00"
GRACES_NODE = "ret0badcafe"


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
    # This test's events alone, and saved under tmp_path rather than backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")


@pytest.fixture(autouse=True)
def _mail(monkeypatch, tmp_path):
    """Mail that could go, logged rather than sent, and an outbox this test's own.

    Nothing else empties the outbox between tests.
    """
    configure_mail(monkeypatch, tmp_path)
    monkeypatch.delenv("CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_EMAIL_EVENTS_TOKEN", raising=False)
    asyncio.run(_empty_outbox())
    yield
    asyncio.run(_empty_outbox())


async def _empty_outbox() -> None:
    async with async_session_maker() as db:
        async with db.begin():
            await db.execute(delete(MailOutbox))


async def _make_account(email: str, *, verified: bool = True, created_at: datetime | None = None) -> User:
    """An account as either kind of maker leaves it, made `created_at` if given."""
    async with async_session_maker() as db:
        account = User(email=email, name=email.split("@")[0], is_verified=verified)
        if created_at is not None:
            account.created_at = created_at
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return account


def make(email: str, *, verified: bool = True, created_at: datetime | None = None) -> User:
    return asyncio.run(_make_account(email, verified=verified, created_at=created_at))


@pytest.fixture
def account() -> User:
    return asyncio.run(users.get_or_create_magic_link_user(ADA))


def _rows(model, *where) -> list:
    async def _read():
        async with async_session_maker() as db:
            return list((await db.scalars(select(model).where(*where))).all())

    return asyncio.run(_read())


def _cookie(token: str) -> dict[str, str]:
    # By header: the cookie is Secure and the test client speaks plain HTTP.
    return {"Cookie": f"{sessions.COOKIE_NAME}={token}"}


def _sign_in(user: User) -> dict[str, str]:
    """Headers carrying the cookie of a session opened for the account now."""
    token, _ = asyncio.run(sessions.open_session(user, user_agent="Mozilla/5.0 (X11; Linux x86_64)", ip=None))
    return _cookie(token)


def _legacy_cookie(user: User) -> dict[str, str]:
    """Headers carrying a fastapi-users JWT, as minted before sessions, a minute old."""
    exp = datetime.now(UTC) - timedelta(minutes=1) + sessions.LEGACY_TOKEN_LIFETIME
    return _cookie(jwt.encode({"sub": str(user.id), "aud": ["fastapi-users:auth"], "exp": exp}, JWT_SECRET, "HS256"))


def _users(client: TestClient, query: str = "") -> dict:
    r = client.get(f"/api/admin/users{query}", headers=AS_ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _emails(client: TestClient, query: str = "") -> list[str]:
    return [u["email"] for u in _users(client, query)["users"]]


def _events(client: TestClient, query: str = "") -> list[dict]:
    r = client.get(f"/api/admin/events{query}", headers=AS_ADMIN)
    assert r.status_code == 200
    return r.json()


def _sign_in_link(client: TestClient, user_id: object, headers: dict[str, str] | None = None):
    return client.post(f"/api/admin/users/{user_id}/sign-in-link", headers=AS_ADMIN if headers is None else headers)


def _delete(client: TestClient, user_id: object, headers: dict[str, str] | None = None):
    return client.delete(f"/api/admin/users/{user_id}", headers=AS_ADMIN if headers is None else headers)


async def _offer_address(node_id: str, email: str, *, expires_at: float | None = None) -> str:
    """Register `node_id` with `email` offered for it and a challenge in the post,
    as the node route records one. Returns the claim link's token."""
    await register_node_row(node_id)
    now = time.time()
    challenge = await claim_links.issue(email=email, node_id=node_id, now=now)
    assert challenge is not None
    async with async_session_maker() as db:
        async with db.begin():
            await set_claim_address(db, node_id, email)
            await put_challenge(
                db, node_id, email, challenge.handle, challenge.expires_at if expires_at is None else expires_at
            )
    return challenge.token


def offer_address(node_id: str, email: str, *, expires_at: float | None = None) -> str:
    return asyncio.run(_offer_address(node_id, email, expires_at=expires_at))


# ── a. the list ──────────────────────────────────────────────────────────────


class TestTheList:
    def test_each_account_is_listed_with_what_the_console_shows(self, client, account):
        listed = _users(client)

        assert listed == {
            "users": [
                {
                    "id": str(account.id),
                    "email": ADA,
                    "name": "ada",
                    "role": "user",
                    "is_superuser": False,
                    "is_active": True,
                    "created_at": account.created_at.timestamp(),
                    "last_seen_at": None,
                    "is_verified": True,
                    "node_count": 0,
                }
            ],
            "total": 1,
        }

    def test_an_account_an_administrator_made_reads_unverified(self, client):
        made, _ = asyncio.run(users.get_or_create_admin_account(GRACE))

        (row,) = _users(client)["users"]

        assert (row["id"], row["is_verified"]) == (str(made.id), False)

    def test_a_deactivated_account_reads_inactive(self, client, account):
        asyncio.run(sessions.deactivate_account(account.id))

        (row,) = _users(client)["users"]

        assert row["is_active"] is False

    def test_node_count_is_the_nodes_each_account_owns(self, client, account):
        make(GRACE)
        own(FLEET_NODE, str(account.id))
        own(SECOND_NODE, str(account.id))
        # Carrying the address is not owning: a claim in progress counts for nothing.
        offer_address(PENDING_NODE, ADA)

        counts = {u["email"]: u["node_count"] for u in _users(client)["users"]}

        assert counts == {ADA: 2, GRACE: 0}

    def test_node_count_is_one_grouped_query_for_the_whole_page(self, client):
        for n, node_id in enumerate(("ret0a1b2c3d", "ret5e6f7a8b", "ret9c0d1e2f")):
            account = make(f"owner{n}@example.com")
            own(node_id, str(account.id))
        counting: list[str] = []

        def _note(_conn, _cursor, statement, *_args):
            if "node_claims" in statement and "count(" in statement.lower():
                counting.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _note)
        try:
            listed = _users(client)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _note)

        assert [u["node_count"] for u in listed["users"]] == [1, 1, 1]
        assert len(counting) == 1
        assert "GROUP BY" in counting[0]


class TestSearch:
    @pytest.fixture(autouse=True)
    def _accounts(self):
        for email in ("ada.lovelace@example.com", "grace@example.org", "Mixed.Case@Example.com"):
            make(email)

    def test_q_is_a_substring_of_the_address(self, client):
        assert _emails(client, "?q=lovelace") == ["ada.lovelace@example.com"]
        assert _emails(client, "?q=example.org") == ["grace@example.org"]

    def test_q_ignores_case_on_either_side(self, client):
        assert _emails(client, "?q=LOVELACE") == ["ada.lovelace@example.com"]
        # An address stored in mixed case, as rows from before normalising are.
        assert _emails(client, "?q=mixed.case") == ["Mixed.Case@Example.com"]

    def test_q_is_trimmed(self, client):
        assert _emails(client, "?q=%20%20lovelace%20") == ["ada.lovelace@example.com"]

    @pytest.mark.parametrize("q", ["", "%20%20"], ids=["empty", "blank"])
    def test_an_empty_q_lists_everyone(self, client, q):
        assert _users(client, f"?q={q}")["total"] == 3

    def test_nothing_matching_is_an_empty_page(self, client):
        assert _users(client, "?q=nobody") == {"users": [], "total": 0}

    def test_total_counts_the_matches_not_the_table(self, client):
        assert _users(client, "?q=example.com")["total"] == 2


class TestWildcardsAreLiteral:
    @pytest.fixture(autouse=True)
    def _accounts(self):
        for email in ("a_b@example.com", "axb@example.com", "100%@example.com", "a/b@example.com", "ab@example.com"):
            make(email)

    @pytest.mark.parametrize(
        ("q", "matches"),
        [
            ("_", ["a_b@example.com"]),
            ("a_b", ["a_b@example.com"]),
            ("%25", ["100%@example.com"]),
            ("100%25", ["100%@example.com"]),
            # The escape character itself, which must not escape anything either.
            ("/", ["a/b@example.com"]),
            ("a/b", ["a/b@example.com"]),
        ],
    )
    def test_like_wildcards_in_q_match_themselves(self, client, q, matches):
        assert _emails(client, f"?q={q}") == matches


class TestOrderAndPaging:
    @pytest.fixture
    def accounts(self) -> list[User]:
        """Five accounts, newest first as the list orders them. The two made in
        the same second come in id order."""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        made = [make(f"user{n}@example.com", created_at=base + timedelta(seconds=n)) for n in range(3)]
        tied = [make(f"tied{n}@example.com", created_at=base + timedelta(seconds=10)) for n in range(2)]
        return sorted(tied, key=lambda u: str(u.id)) + made[::-1]

    def test_newest_first_then_by_id(self, client, accounts):
        assert [u["id"] for u in _users(client)["users"]] == [str(u.id) for u in accounts]

    def test_pages_cover_the_list_once_each_with_the_total_on_every_page(self, client, accounts):
        pages = [_users(client, f"?limit=2&offset={offset}") for offset in (0, 2, 4)]

        assert [len(p["users"]) for p in pages] == [2, 2, 1]
        assert {p["total"] for p in pages} == {5}
        assert [u["id"] for p in pages for u in p["users"]] == [str(u.id) for u in accounts]

    def test_an_offset_past_the_end_is_an_empty_page_with_the_total(self, client, accounts):
        assert _users(client, "?offset=50") == {"users": [], "total": 5}

    def test_the_default_page_is_25(self, client):
        for n in range(26):
            make(f"many{n:02d}@example.com")

        listed = _users(client)

        assert (len(listed["users"]), listed["total"]) == (25, 26)

    @pytest.mark.parametrize("limit", [1, 200])
    def test_limits_at_the_bounds_are_taken(self, client, accounts, limit):
        assert len(_users(client, f"?limit={limit}")["users"]) == min(limit, 5)

    def test_the_largest_offset_taken_is_an_empty_page_with_the_total(self, client, accounts):
        assert _users(client, "?offset=1000000000") == {"users": [], "total": 5}

    @pytest.mark.parametrize(
        "query",
        [
            "?limit=0",
            "?limit=201",
            "?limit=-1",
            "?limit=x",
            "?offset=-1",
            "?offset=x",
            "?offset=1000000001",
            "?offset=10000000000000000000",
        ],
    )
    def test_out_of_range_paging_is_422(self, client, accounts, query):
        r = client.get(f"/api/admin/users{query}", headers=AS_ADMIN)

        assert r.status_code == 422


# ── b. one account ───────────────────────────────────────────────────────────


class TestOneAccount:
    def test_an_account_with_nothing_has_empty_nodes_and_claims(self, client, account):
        r = client.get(f"/api/admin/users/{account.id}", headers=AS_ADMIN)

        assert r.status_code == 200
        body = r.json()
        assert body["user"] == _users(client)["users"][0]
        assert (body["nodes"], body["claims"]) == ([], [])

    def test_nodes_are_what_it_owns_and_claims_what_carries_its_address(self, client, account):
        grace = make(GRACE)
        own(FLEET_NODE, str(account.id))
        own(SECOND_NODE, str(account.id))
        offer_address(PENDING_NODE, ADA)
        offer_address(EXPIRED_NODE, ADA, expires_at=time.time() - 60)
        offer_address(BOUNCED_NODE, ADA)
        asyncio.run(_bounce(BOUNCED_NODE, ADA))
        offer_address(GRACES_OFFER, GRACE)
        own(GRACES_NODE, str(grace.id))
        node_refs._reset_for_tests()

        body = client.get(f"/api/admin/users/{account.id}", headers=AS_ADMIN).json()

        assert body["user"]["node_count"] == 2
        # Each list in node_id order.
        assert [(n["node_id"], n["status"], n["owner"]["user_id"]) for n in body["nodes"]] == [
            (FLEET_NODE, "owned", str(account.id)),
            (SECOND_NODE, "owned", str(account.id)),
        ]
        assert [(c["node_id"], c["status"], c["email"]) for c in body["claims"]] == [
            (BOUNCED_NODE, "unclaimed", ADA),
            (PENDING_NODE, "pending", ADA),
            (EXPIRED_NODE, "unclaimed", ADA),
        ]
        claims = {c["node_id"]: c for c in body["claims"]}
        expired, bounced = claims[EXPIRED_NODE], claims[BOUNCED_NODE]
        assert expired["challenge"]["expired"] is True
        assert (bounced["undeliverable"], bounced["challenge"]) == (True, None)

    def test_an_address_stored_in_mixed_case_still_finds_its_claims(self, client):
        mixed = make("Ada@Example.com")
        offer_address(PENDING_NODE, ADA)

        body = client.get(f"/api/admin/users/{mixed.id}", headers=AS_ADMIN).json()

        assert [c["node_id"] for c in body["claims"]] == [PENDING_NODE]

    def test_a_claim_it_won_is_a_node_and_not_a_claim(self, client):
        token = offer_address(FLEET_NODE, ADA)
        r = client.post("/api/auth/claim/consume", json={"token": token})
        assert r.status_code == 200

        body = client.get(f"/api/admin/users/{r.json()['user']['id']}", headers=AS_ADMIN).json()

        assert [n["node_id"] for n in body["nodes"]] == [FLEET_NODE]
        assert body["claims"] == []

    @pytest.mark.parametrize("user_id", [uuid.uuid4(), "not-a-uuid", "0"], ids=["unknown", "malformed", "short"])
    def test_an_id_that_is_no_account_is_404(self, client, account, user_id):
        r = client.get(f"/api/admin/users/{user_id}", headers=AS_ADMIN)

        assert r.status_code == 404
        assert r.json() == {"detail": "User not found"}


async def _bounce(node_id: str, email: str) -> None:
    async with async_session_maker() as db:
        async with db.begin():
            await mark_undeliverable(db, node_id, email)


# ── c. a sign-in link on the account's behalf ────────────────────────────────


@pytest.fixture
def enqueued(monkeypatch) -> list:
    """Every message the outbox is handed, which it is handed sealed."""
    handed = []
    real = mail_outbox.enqueue

    async def recording(session, message, **kwargs):
        handed.append(message)
        return await real(session, message, **kwargs)

    monkeypatch.setattr(mail_outbox, "enqueue", recording)
    return handed


class TestSignInLink:
    def test_the_link_is_queued_in_the_outbox_to_the_address(self, client, account):
        before = time.time()

        r = _sign_in_link(client, account.id)

        assert r.status_code == 202
        assert r.json() == {"status": "queued", "email": ADA}
        (row,) = _rows(MailOutbox)
        assert (row.kind, row.recipient) == (mail_outbox.KIND_SIGN_IN, ADA)
        assert before + MAGIC_LINK_EXPIRY_S <= row.expires_at <= time.time() + MAGIC_LINK_EXPIRY_S
        (link,) = _rows(MagicLink)
        assert (link.email, link.intent, link.used_at) == (ADA, INTENT_SIGNIN, None)

    def test_the_mail_is_the_self_service_one_and_its_link_signs_the_account_in(self, client, account, enqueued):
        _sign_in_link(client, account.id)

        (message,) = enqueued
        token = message.text.split("/auth/link/")[1].split()[0]
        rendered = mail_templates.sign_in(f"https://{HOST_APP}/auth/link/{token}")
        assert message == mail.Message(to=ADA, subject=rendered.subject, text=rendered.text, html=rendered.html)
        signed_in = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert signed_in.status_code == 200
        assert signed_in.json()["user"]["id"] == str(account.id)

    def test_it_is_recorded_naming_the_administrator(self, client, account):
        _sign_in_link(client, account.id)

        (recorded,) = _events(client)
        assert (recorded["category"], recorded["severity"]) == ("user", "info")
        assert str(account.id) in recorded["message"]
        assert recorded["meta"] == {"user_id": str(account.id), "by": OPERATOR}

    def test_an_account_an_administrator_made_gets_one_too(self, client):
        made, _ = asyncio.run(users.get_or_create_admin_account(GRACE))

        assert _sign_in_link(client, made.id).status_code == 202
        assert [row.recipient for row in _rows(MailOutbox)] == [GRACE]

    @pytest.mark.parametrize("user_id", [uuid.uuid4(), "not-a-uuid"], ids=["unknown", "malformed"])
    def test_an_id_that_is_no_account_is_404(self, client, account, user_id):
        r = _sign_in_link(client, user_id)

        assert r.status_code == 404
        assert r.json() == {"detail": "User not found"}
        self._nothing_minted_or_queued(client)

    def test_a_deactivated_account_is_409(self, client, account):
        asyncio.run(sessions.deactivate_account(account.id))

        r = _sign_in_link(client, account.id)

        assert r.status_code == 409
        assert r.json() == {"detail": "Account is deactivated"}
        self._nothing_minted_or_queued(client)

    def test_an_address_holding_the_most_links_it_may_is_429(self, client, account):
        held = [asyncio.run(create_magic_link(ADA)) for _ in range(5)]
        assert all(held)

        r = _sign_in_link(client, account.id)

        assert r.status_code == 429
        assert "maximum" in r.json()["detail"]
        assert len(_rows(MagicLink)) == 5
        assert _rows(MailOutbox) == []
        assert _events(client) == []

    def test_an_address_another_account_retired_is_409_and_says_so(self, client):
        """An address stored as typed, before addresses were normalised, that
        SQLite's lower(), which folds ASCII only, cannot match to its own
        normalised form, resolves to the account that retired that form
        instead. A link for it would sign that account in, so none is minted,
        and the refusal says why rather than reading as the cap's 429."""
        stored = make("ÉVE@example.com")
        retiring = make("eve.new@example.com")

        async def retire() -> None:
            async with async_session_maker() as db:
                async with db.begin():
                    db.add(
                        RetiredAddress(email="éve@example.com", user_id=str(retiring.id), retired_at=datetime.now(UTC))
                    )

        asyncio.run(retire())

        r = _sign_in_link(client, stored.id)

        assert r.status_code == 409
        assert r.json() == {"detail": "This address is another account's retired address"}
        self._nothing_minted_or_queued(client)

    @pytest.mark.parametrize("unset", ["HOST_APP", "MAIL_TRANSPORT"])
    def test_mail_that_cannot_leave_is_503_before_a_link_is_minted(self, client, account, monkeypatch, unset):
        monkeypatch.delenv(unset)

        r = _sign_in_link(client, account.id)

        assert r.status_code == 503
        assert r.json() == {"detail": "Sign-in by email is unavailable"}
        self._nothing_minted_or_queued(client)

    def test_a_mail_that_cannot_be_queued_is_500_and_its_link_withdrawn(self, client, account, monkeypatch):
        async def refusing(*_args, **_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(mail_outbox, "enqueue", refusing)

        r = _sign_in_link(client, account.id)

        assert r.status_code == 500
        assert r.json() == {"detail": "Could not queue the sign-in mail"}
        self._nothing_minted_or_queued(client)

    def test_an_address_changed_meanwhile_is_never_mailed(self, client, account, monkeypatch, enqueued):
        """The account is read again under the write lock the link is minted
        under, so a change of address confirmed after the route first read it
        sends the link to the new address, minted for it, and nothing to the
        retired one, as the self-service route forwards a retired address."""
        _after_the_first_read(monkeypatch, _move_to(MOVED))

        r = _sign_in_link(client, account.id)

        assert r.status_code == 202
        assert r.json() == {"status": "queued", "email": MOVED}
        assert [message.to for message in enqueued] == [MOVED]
        (link,) = _rows(MagicLink)
        assert (link.email, link.intent) == (MOVED, INTENT_SIGNIN)
        token = enqueued[0].text.split("/auth/link/")[1].split()[0]
        signed_in = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert signed_in.json()["user"]["id"] == str(account.id)

    def test_an_account_deactivated_meanwhile_is_409_and_nothing_is_minted(self, client, account, monkeypatch):
        _after_the_first_read(monkeypatch, sessions.deactivate_account)

        r = _sign_in_link(client, account.id)

        assert r.status_code == 409
        assert r.json() == {"detail": "Account is deactivated"}
        self._nothing_minted_or_queued(client)

    def test_an_account_deleted_meanwhile_is_404_and_nothing_is_minted(self, client, account, monkeypatch):
        """A link minted for the address would make whoever opens it a new
        account there, which nobody asked for."""

        async def deleted(user_id):
            assert await accounts.delete_account(user_id, by=OPERATOR) == 0
            admin._events.clear()

        _after_the_first_read(monkeypatch, deleted)

        r = _sign_in_link(client, account.id)

        assert r.status_code == 404
        assert r.json() == {"detail": "User not found"}
        self._nothing_minted_or_queued(client)

    @staticmethod
    def _nothing_minted_or_queued(client: TestClient) -> None:
        assert _rows(MagicLink) == []
        assert _rows(MailOutbox) == []
        assert _events(client) == []


MOVED = "ada.moved@example.com"


def _move_to(address: str):
    """What confirming a change of address leaves in the rows: the account at
    `address`, and the one it had retired to it (services/account_email.py)."""

    async def move(user_id) -> None:
        async with async_session_maker() as db:
            async with db.begin():
                account = await db.get(User, user_id)
                db.add(RetiredAddress(email=account.email, user_id=str(account.id), retired_at=datetime.now(UTC)))
                account.email = address

    return move


def _after_the_first_read(monkeypatch, meanwhile) -> None:
    """Run `meanwhile(account id)` once the route has read the account, before
    the link is minted: a change that lands between the two."""
    read = admin._account

    async def then_meanwhile(session, user_id):
        account = await read(session, user_id)
        await meanwhile(account.id)
        return account

    monkeypatch.setattr(admin, "_account", then_meanwhile)


# ── d. deleting an account ───────────────────────────────────────────────────


@pytest.fixture
def poller_wakes(monkeypatch) -> list:
    wakes = []
    monkeypatch.setattr(blah2_poller, "refresh", lambda: wakes.append(True))
    return wakes


@pytest.fixture
def owner_of_two(account, poller_wakes) -> User:
    """The account, owning a fleet node and a polled radar."""
    own(FLEET_NODE, str(account.id))
    own(POLLED_NODE, str(account.id))
    return account


def _node(node_id: str) -> Node:
    (node,) = _rows(Node, Node.node_id == node_id)
    return node


class TestDelete:
    def test_it_answers_with_how_many_nodes_it_released(self, client, owner_of_two):
        r = _delete(client, owner_of_two.id)

        assert r.status_code == 200
        assert r.json() == {"ok": True, "released": 2}

    def test_it_is_the_account_pages_deletion_done_by_the_administrator(self, client, account, monkeypatch):
        """One deletion for both doors (services/accounts.py): the owner's own
        from the account page, and this one, which names the administrator."""
        calls = []
        delete_account = accounts.delete_account

        async def watched(user_id, *, by):
            calls.append((user_id, by))
            return await delete_account(user_id, by=by)

        monkeypatch.setattr(accounts, "delete_account", watched)

        assert _delete(client, account.id).json() == {"ok": True, "released": 0}
        assert calls == [(str(account.id), OPERATOR)]
        assert not hasattr(admin, "_delete_account")

    def test_each_node_goes_the_way_its_owners_release_takes_it(self, client, owner_of_two, poller_wakes):
        """routes/owner.py: a fleet node handed back for its next owner, a polled
        radar removed, since nothing can claim one afterwards."""
        _delete(client, owner_of_two.id)

        assert _rows(NodeClaim) == []
        assert _node(FLEET_NODE).status == "active"
        assert _node(POLLED_NODE).status == "retired"
        (removed,) = _rows(NodeEvent, NodeEvent.node_id == POLLED_NODE)
        assert (removed.kind, removed.actor) == ("removed", str(owner_of_two.id))
        assert poller_wakes == [True]

    def test_its_cookies_stop_signing_it_in(self, client, account):
        session_cookie = _sign_in(account)
        legacy_cookie = _legacy_cookie(account)
        assert client.get("/api/auth/me", headers=session_cookie).json()["email"] == ADA
        assert client.get("/api/auth/me", headers=legacy_cookie).status_code == 200

        _delete(client, account.id)

        assert client.get("/api/auth/me", headers=session_cookie).status_code == 401
        assert client.get("/api/auth/me", headers=legacy_cookie).status_code == 401
        assert client.get("/api/auth/me/nodes", headers=session_cookie).status_code == 401

    def test_a_sign_in_link_mailed_before_redeems_nothing(self, client, account):
        token = asyncio.run(create_magic_link(ADA))
        assert client.get(f"/api/auth/magic-link/{token}").json() == {"email": ADA}

        _delete(client, account.id)

        assert client.get(f"/api/auth/magic-link/{token}").status_code == 404
        r = client.post("/api/auth/magic-link/consume", json={"token": token})
        assert r.status_code == 400
        assert "set-cookie" not in r.headers
        # Redeeming would have made the address a fresh account.
        assert _rows(User) == []

    def test_a_claim_link_to_the_address_is_the_nodes_and_stays_live(self, client, account):
        """A claim is the node's, not the account's: the person at the address
        can still take or decline it, as anyone with no account can."""
        token = offer_address(FLEET_NODE, ADA)

        assert _delete(client, account.id).status_code == 200

        assert _rows(User) == []
        assert client.get(f"/api/auth/claim/{token}").status_code == 200

    def test_the_row_is_gone_and_its_session_rows_with_it(self, client, account):
        """The row takes its sessions and any retirement of its JWT cookies
        with it, on their foreign keys (core/sessions.py, remove_account)."""
        _sign_in(account)
        # Exchanging a JWT cookie for a session writes the row tying the two.
        assert client.get("/api/auth/me", headers=_legacy_cookie(account)).status_code == 200
        assert len(_rows(RetiredLegacyToken)) == 1

        _delete(client, account.id)

        assert _rows(User) == []
        assert _rows(UserSession) == []
        assert _rows(RetiredLegacyToken) == []
        assert client.get(f"/api/admin/users/{account.id}", headers=AS_ADMIN).status_code == 404
        assert _users(client) == {"users": [], "total": 0}

    def test_it_is_recorded_once_naming_the_administrator_and_not_the_address(self, client, owner_of_two):
        """The core records the deletion, so the route adds no second entry. The
        log is kept on disk, and the address is what the deletion removes."""
        _delete(client, owner_of_two.id)

        (recorded,) = _events(client)
        assert (recorded["category"], recorded["severity"]) == ("user", "warning")
        assert recorded["message"] == f"Account {owner_of_two.id} deleted"
        assert sorted(recorded["meta"].pop("nodes")) == [POLLED_NODE, FLEET_NODE]
        assert recorded["meta"] == {"user_id": str(owner_of_two.id), "by": OPERATOR}
        assert ADA not in str(recorded)

    def test_other_accounts_are_left_alone(self, client, account):
        grace = make(GRACE)
        own(FLEET_NODE, str(grace.id))
        cookie = _sign_in(grace)

        _delete(client, account.id)

        assert _emails(client) == [GRACE]
        assert client.get("/api/auth/me", headers=cookie).status_code == 200
        assert [n["node_id"] for n in client.get(f"/api/admin/users/{grace.id}", headers=AS_ADMIN).json()["nodes"]] == [
            FLEET_NODE
        ]

    @pytest.mark.parametrize("user_id", [uuid.uuid4(), "not-a-uuid", "0"], ids=["unknown", "malformed", "short"])
    def test_an_id_that_is_no_account_is_404(self, client, account, user_id):
        r = _delete(client, user_id)

        assert r.status_code == 404
        assert r.json() == {"detail": "User not found"}
        assert [u.id for u in _rows(User)] == [account.id]
        assert _events(client) == []

    def test_a_node_claimed_meanwhile_is_handed_back_and_the_deletion_finishes(self, client, account, monkeypatch):
        """A claim that binds a node between the hand-back and the removal stops
        the removal, and the next round hands that node back too."""
        remove = sessions.remove_account
        landed = []

        async def claim_lands_first(user_id, **kwargs):
            if not landed:
                await own_registered_node(SECOND_NODE, str(account.id))
                landed.append(SECOND_NODE)
            return await remove(user_id, **kwargs)

        monkeypatch.setattr(sessions, "remove_account", claim_lands_first)
        own(FLEET_NODE, str(account.id))

        r = _delete(client, account.id)

        assert r.json() == {"ok": True, "released": 2}
        assert _rows(User) == []
        assert _rows(NodeClaim, NodeClaim.user_id.is_not(None)) == []

    def test_claims_that_keep_landing_are_409_with_the_account_whole(self, client, account, monkeypatch):
        """Each round a new claim binds a node before the removal, until the
        core gives up: the account is still there, active and signed in, owns
        only the last node to land, and nothing records a deletion. Deleting it
        again finishes the job."""
        cookie = _sign_in(account)
        remove = sessions.remove_account
        arriving = iter([FLEET_NODE, SECOND_NODE, PENDING_NODE])

        async def another_lands(user_id, **kwargs):
            await own_registered_node(next(arriving), str(account.id))
            return await remove(user_id, **kwargs)

        monkeypatch.setattr(sessions, "remove_account", another_lands)

        r = _delete(client, account.id)

        assert r.status_code == 409
        assert r.json() == {"detail": "A node was claimed for this account while it was being deleted. Try again."}
        (row,) = _rows(User)
        assert (row.id, row.is_active) == (account.id, True)
        assert client.get("/api/auth/me", headers=cookie).status_code == 200
        assert [c.node_id for c in _rows(NodeClaim, NodeClaim.user_id == str(account.id))] == [PENDING_NODE]
        assert _events(client) == []

        monkeypatch.setattr(sessions, "remove_account", remove)
        assert _delete(client, account.id).json() == {"ok": True, "released": 1}
        assert _rows(User) == []


class TestDeleteClosesOwnerFeeds:
    """An owner feed open on the account's cookie, which /ws/aircraft/owner
    closes with 1008 once the cookie no longer signs the account in
    (tests/test_owner_socket_sessions.py)."""

    @pytest.fixture(autouse=True)
    def _feeds_as_deployed(self, monkeypatch):
        # Imported by name there, so the patch in _as_deployed does not reach them.
        monkeypatch.setattr(streaming, "AUTH_BYPASS", False)
        monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
        # An hour off, so a socket closed within the test was closed by the wake and not the backstop.
        monkeypatch.setattr(streaming, "SESSION_RECHECK_S", 3600.0)
        yield
        _eventually(lambda: not sessions._watches, "every watch released")

    @pytest.fixture
    def checks(self, monkeypatch) -> _Checks:
        """_owner_feed waits on the feed's first read-only check of its cookie."""
        seen = _Checks()
        resolve = sessions.resolve

        async def recorded(token, **kwargs):
            resolved = await resolve(token, **kwargs)
            if not kwargs.get("slide", True):
                seen.results.append(resolved)
            return resolved

        monkeypatch.setattr(sessions, "resolve", recorded)
        return seen

    @staticmethod
    def _token(headers: dict[str, str]) -> str:
        return headers["Cookie"].removeprefix(f"{sessions.COOKIE_NAME}=")

    def _delete_with_feed_open(self, client, account, headers, checks) -> None:
        with _owner_feed(client, self._token(headers), checks) as (ws, server_side):
            started = time.monotonic()
            assert _delete(client, account.id).status_code == 200
            _assert_closed_promptly(ws, server_side, account.id, started)

    def test_a_feed_on_a_session_cookie_is_closed(self, client, account, checks):
        self._delete_with_feed_open(client, account, _sign_in(account), checks)

    def test_a_feed_on_a_legacy_cookie_is_closed(self, client, account, checks):
        self._delete_with_feed_open(client, account, _legacy_cookie(account), checks)


# ── e. the event log, narrowed to one account ────────────────────────────────


class TestEventsForOneAccount:
    @pytest.fixture
    def history(self, client, account) -> tuple[User, User]:
        """Events naming Ada, Grace, both through a reassignment, and neither."""
        grace = make(GRACE)
        asyncio.run(register_node_row(FLEET_NODE))

        def put_owner(user_id):
            r = client.put(f"/api/admin/nodes/{FLEET_NODE}/owner", json={"user_id": user_id}, headers=AS_ADMIN)
            assert r.status_code == 200

        assert client.post(f"/api/admin/users/{account.id}/deactivate", headers=AS_ADMIN).status_code == 200
        put_owner(str(account.id))
        put_owner(str(grace.id))
        admin.log_event("node", "Somebody else's node went quiet", "warning", {"node_id": GRACES_NODE})
        admin.log_event("system", "No meta at all")
        put_owner(None)
        return account, grace

    def test_the_owner_route_records_the_owner_it_replaced(self, client, history):
        ada, grace = history

        owner_events = [e["meta"] for e in _events(client) if "node_id" in e["meta"] and "by" in e["meta"]]

        assert owner_events == [
            {"node_id": FLEET_NODE, "user_id": None, "previous_user_id": str(grace.id), "by": OPERATOR},
            {"node_id": FLEET_NODE, "user_id": str(grace.id), "previous_user_id": str(ada.id), "by": OPERATOR},
            {"node_id": FLEET_NODE, "user_id": str(ada.id), "previous_user_id": None, "by": OPERATOR},
        ]

    def test_user_id_keeps_the_events_naming_the_account_newest_first(self, client, history):
        ada, grace = history

        mine = _events(client, f"?user_id={ada.id}")
        theirs = _events(client, f"?user_id={grace.id}")

        assert [e["message"] for e in mine] == [
            f"Node {FLEET_NODE} owner set to {grace.id}",
            f"Node {FLEET_NODE} owner set to {ada.id}",
            f"Account {ada.id} deactivated",
        ]
        assert [e["message"] for e in theirs] == [
            f"Node {FLEET_NODE} owner set to (unassigned)",
            f"Node {FLEET_NODE} owner set to {grace.id}",
        ]

    @pytest.mark.parametrize(
        "spell",
        [str.upper, lambda uid: uid.replace("-", ""), lambda uid: f"{{{uid}}}", lambda uid: f"urn:uuid:{uid}"],
        ids=["upper-case", "undashed", "braced", "urn"],
    )
    def test_user_id_takes_any_spelling_the_account_route_does(self, client, history, spell):
        ada, _ = history
        spelled = spell(str(ada.id))
        assert client.get(f"/api/admin/users/{spelled}", headers=AS_ADMIN).status_code == 200

        assert _events(client, f"?user_id={spelled}") == _events(client, f"?user_id={ada.id}") != []

    def test_user_id_that_is_no_uuid_is_matched_as_written(self, client, history):
        admin.log_event("system", "An id from before accounts were uuids", "info", {"user_id": "legacy-id"})

        assert [e["message"] for e in _events(client, "?user_id=legacy-id")] == [
            "An id from before accounts were uuids"
        ]
        assert _events(client, "?user_id=LEGACY-ID") == []

    def test_without_user_id_the_whole_log_is_listed(self, client, history):
        assert len(_events(client)) == 6

    def test_an_id_nothing_names_lists_nothing(self, client, history):
        assert _events(client, f"?user_id={uuid.uuid4()}") == []

    def test_a_deleted_account_keeps_its_history(self, client, history):
        ada, grace = history

        _delete(client, ada.id)

        assert [e["message"] for e in _events(client, f"?user_id={ada.id}")] == [
            f"Account {ada.id} deleted",
            f"Node {FLEET_NODE} owner set to {grace.id}",
            f"Node {FLEET_NODE} owner set to {ada.id}",
            f"Account {ada.id} deactivated",
        ]

    def test_the_filter_comes_before_the_limit(self, client, account):
        for n in range(3):
            admin.log_event("user", f"mine {n}", meta={"user_id": str(account.id)})
        for n in range(10):
            admin.log_event("node", f"other {n}", meta={"node_id": f"node-{n}"})

        narrowed = _events(client, f"?user_id={account.id}&limit=2")

        assert [e["message"] for e in narrowed] == ["mine 2", "mine 1"]


# ── f. who may call them ─────────────────────────────────────────────────────

ROUTES = [
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/users?q=ada&limit=5&offset=1"),
    # A refused value is still the gate's answer, not a 422 for whoever asks.
    ("GET", "/api/admin/users?limit=0&offset=-1"),
    ("GET", "/api/admin/users/{user_id}"),
    ("POST", "/api/admin/users/{user_id}/sign-in-link"),
    ("DELETE", "/api/admin/users/{user_id}"),
    ("GET", "/api/admin/events?user_id={user_id}"),
    ("GET", "/api/admin/events?user_id={user_id}&limit=5"),
]


class TestOnlyAnAdministratorMay:
    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_no_credentials_is_401(self, client, account, method, path):
        r = client.request(method, path.format(user_id=account.id))

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}
        self._nothing_changed(client, account)

    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_a_signed_in_account_is_403_even_about_itself(self, client, account, method, path):
        cookie = _sign_in(account)

        r = client.request(method, path.format(user_id=account.id), headers=cookie)

        assert r.status_code == 403
        assert r.json() == {"detail": "Admin access required"}
        self._nothing_changed(client, account)
        assert client.get("/api/auth/me", headers=cookie).status_code == 200

    @staticmethod
    def _nothing_changed(client: TestClient, account: User) -> None:
        (row,) = _rows(User)
        assert (row.id, row.is_active) == (account.id, True)
        assert _rows(MagicLink) == []
        assert _rows(MailOutbox) == []
        assert _events(client) == []
