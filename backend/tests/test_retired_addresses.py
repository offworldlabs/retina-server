"""A retired address stays its account's: it never makes a second account,
at sign-in or at a node's claim, and it is never mailed.

An address an account changed away from (services/account_email.py) is kept
in retired_addresses. Everything that turns an address into an account finds
the account through it (core/users.py, _account_at), and mail meant for it
goes to the account's current address instead (forwarding_address), since the
old mailbox may be dead or somebody else's by now.

The node's claim runs its real route against the suite's shared database, so
the claim, the link, the queue and the accounts are one file as on a droplet,
and the mail is read back by running the outbox's worker by hand.
"""

import asyncio
import re
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

import routes.auth as auth_routes
from core import sessions, state
from core.auth import create_magic_link
from core.mail_outbox import MailOutbox
from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.rate_counters import RateCounter
from core.users import (
    MagicLink,
    RetiredAddress,
    User,
    UserSession,
    _account_at,
    async_session_maker,
    engine,
    forwarding_address,
    get_or_create_admin_account,
    get_or_create_magic_link_user,
)
from main import app
from services import (
    account_email,
    blah2_poller,
    claim_links,
    mail,
    mail_outbox,
    mail_templates,
    node_auth,
    node_retirement,
    polled_registration,
)
from services.node_claim_store import put_challenge
from services.node_claiming import ClaimOutcome, complete_claim
from services.node_rate_limits import polled_probe_limiter
from tests.claim_mail_helpers import HOST_APP, Transport, configure_mail, token_in
from tests.test_polled_radar_registration import STOCK_FINGERPRINT, _canned

ADA = "ada@example.com"
NEW = "ada.new@example.com"
NODE = "ret0a1b2c3d"
OTHER_NODE = "ret3a4b5c6d"
RADAR = "radar.example.com:3000"

_SIGN_IN_LINK = re.compile(r"https://" + re.escape(HOST_APP) + r"/auth/link/([A-Za-z0-9_-]+)")
_CHANGE_LINK = re.compile(r"https://" + re.escape(HOST_APP) + r"/auth/email/([A-Za-z0-9_-]+)")


async def _empty_queue_and_counters() -> None:
    async with async_session_maker() as session:
        await session.execute(delete(MailOutbox))
        await session.execute(delete(RateCounter))
        await session.commit()


@pytest.fixture(autouse=True)
def _mail(monkeypatch, tmp_path):
    configure_mail(monkeypatch, tmp_path)
    # The per-source sign-in window is module state the whole session shares.
    auth_routes._magic_link_requests.clear()
    asyncio.run(_empty_queue_and_counters())
    yield
    auth_routes._magic_link_requests.clear()
    asyncio.run(_empty_queue_and_counters())


@pytest.fixture
def client():
    # Bare, with no lifespan, so no worker of the app's own races the drain.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def ada() -> User:
    """An account that signed in as ADA and has since moved to NEW, with ADA retired to it."""

    async def _make() -> User:
        async with async_session_maker() as session:
            async with session.begin():
                account = User(email=NEW, name="ada", is_verified=True)
                session.add(account)
                await session.flush()
                session.add(RetiredAddress(email=ADA, user_id=str(account.id), retired_at=datetime.now(UTC)))
            return account

    return asyncio.run(_make())


def _sent(*deliveries: mail.Delivery) -> list[mail.Message]:
    transport = Transport(*deliveries)

    async def _drain() -> None:
        while await mail_outbox.run_once(owner="test-worker", transport=transport):
            pass

    asyncio.run(_drain())
    return transport.sent


def _read(statement) -> list:
    async def _go():
        async with async_session_maker() as session:
            return list((await session.execute(statement)).scalars())

    return asyncio.run(_go())


def _accounts() -> list[User]:
    return _read(select(User))


def _claim(node_id: str) -> NodeClaim:
    (row,) = _read(select(NodeClaim).where(NodeClaim.node_id == node_id))
    return row


def _challenge(node_id: str) -> NodeClaimChallenge | None:
    (row,) = _read(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id)) or [None]
    return row


def _register(node_id: str) -> tuple[str, str]:
    """A registered node and its bearer token, in the shared database. Returns (token, node_ref)."""

    async def _go() -> tuple[str, str]:
        async with async_session_maker() as session:
            async with session.begin():
                node = Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), board_model="raspberrypi5-4gb")
                session.add(node)
                await session.flush()
                token = await node_auth.mint_token(session, node_id)
            return token, node.node_ref

    return asyncio.run(_go())


def _nominate(client: TestClient, bearer: str, email: str):
    return client.put("/v1/nodes/claim", json={"email": email}, headers={"Authorization": f"Bearer {bearer}"})


# ── A node's claim ───────────────────────────────────────────────────────────


def test_a_claim_typed_with_a_retired_address_binds_the_node_to_the_same_account(client, ada):
    bearer, node_ref = _register(NODE)

    nominated = _nominate(client, bearer, ADA)

    assert nominated.status_code == 200
    assert nominated.json()["state"] == "pending"
    # The mail went to the address the account has now, and the retired one
    # was never mailed.
    (message,) = _sent()
    token = token_in(message)
    rendered = mail_templates.claim(node_ref, f"https://{HOST_APP}/auth/claim/{token}", 15, former=ADA)
    assert message == mail.Message(to=NEW, subject=rendered.subject, text=rendered.text, html=rendered.html)
    assert [row.recipient for row in _read(select(MailOutbox))] == [NEW]

    clicked = client.post("/api/auth/claim/consume", json={"token": token})

    assert clicked.status_code == 200
    assert clicked.json()["user"]["id"] == str(ada.id)
    assert clicked.json()["user"]["email"] == NEW
    assert [account.id for account in _accounts()] == [ada.id]
    claim = _claim(NODE)
    assert claim.user_id == str(ada.id)
    # The node keeps the address it was claimed with.
    assert (claim.email, claim.verified) == (ADA, True)


def test_a_second_link_for_a_node_the_account_owns_reads_as_its_own(ada):
    """The node was claimed with ADA before the account moved. A link for it
    that is still about, such as one a resend minted while the first click was
    binding, finds the binding is the same account's rather than somebody else's."""
    _register(NODE)

    async def _owned_with_a_live_link() -> str:
        challenge = await claim_links.issue(email=ADA, node_id=NODE, now=datetime.now(UTC).timestamp())
        assert challenge is not None
        async with async_session_maker() as session:
            async with session.begin():
                session.add(NodeClaim(node_id=NODE, user_id=str(ada.id), email=ADA, verified=True, undeliverable=False))
                await session.flush()
                await put_challenge(session, NODE, ADA, challenge.handle, challenge.expires_at)
        return challenge.token

    token = asyncio.run(_owned_with_a_live_link())

    outcome, user, _ref = asyncio.run(complete_claim(token))

    assert outcome is ClaimOutcome.BOUND
    assert user is not None and user.id == ada.id
    assert [account.id for account in _accounts()] == [ada.id]


def test_a_bounce_of_a_forwarded_claim_mail_marks_no_claim(client, ada):
    """The current address bounced, not the node's. The node's address was never
    mailed, so it is not flagged: its link and challenge die as a refused mail's
    do, and it reads unclaimed with its address on file. Another node offering
    the current address is left alone, as a bounce for one node's mail says
    nothing about another's."""
    forwarded, _ = _register(NODE)
    direct, _ = _register(OTHER_NODE)
    _nominate(client, forwarded, ADA)
    _nominate(client, direct, NEW)

    sent = _sent(mail.Delivery(mail.UNDELIVERABLE, "no such mailbox"), mail.Delivery(mail.SENT, "accepted"))

    assert [message.to for message in sent] == [NEW, NEW]
    link = token_in(sent[0])
    assert [(row.node_id, row.status) for row in _read(select(MailOutbox).order_by(MailOutbox.id))] == [
        (NODE, "undeliverable"),
        (OTHER_NODE, "sent"),
    ]
    claim = _claim(NODE)
    assert (claim.email, claim.undeliverable, claim.user_id) == (ADA, False, None)
    assert _challenge(NODE) is None
    assert asyncio.run(claim_links.peek(link)) is None
    assert client.get("/v1/nodes/claim", headers={"Authorization": f"Bearer {forwarded}"}).json() == {
        "state": "unclaimed",
        "email": ADA,
        "undeliverable": False,
    }
    other = _claim(OTHER_NODE)
    assert (other.email, other.undeliverable) == (NEW, False)
    assert _challenge(OTHER_NODE) is not None


# ── Sign-in ──────────────────────────────────────────────────────────────────


def test_signing_in_with_a_retired_address_mails_the_current_one_and_signs_into_the_same_account(client, ada):
    asked = client.post("/api/auth/magic-link", json={"email": ADA})

    assert asked.status_code == 202
    (message,) = _sent()
    assert message.to == NEW
    assert [row.recipient for row in _read(select(MailOutbox))] == [NEW]
    tokens = set(_SIGN_IN_LINK.findall(message.text))
    assert len(tokens) == 1
    token = tokens.pop()
    assert client.get(f"/api/auth/magic-link/{token}").json() == {"email": NEW}

    signed_in = client.post("/api/auth/magic-link/consume", json={"token": token})

    assert signed_in.status_code == 200
    assert (signed_in.json()["user"]["id"], signed_in.json()["user"]["email"]) == (str(ada.id), NEW)
    assert sessions.COOKIE_NAME in signed_in.cookies
    assert [account.id for account in _accounts()] == [ada.id]


def test_a_deactivated_account_gets_no_sign_in_link_for_its_retired_address(ada):
    asyncio.run(sessions.deactivate_account(ada.id))

    assert asyncio.run(create_magic_link(ADA)) is None


def test_no_sign_in_link_is_minted_for_a_retired_address(ada):
    """A sign-in asked for with one is minted for the current address, and
    mailed there. A link for the retired one would be mailed to it."""
    assert asyncio.run(create_magic_link(ADA)) is None

    assert _read(select(MagicLink)) == []
    assert asyncio.run(create_magic_link(NEW)) is not None


def test_where_a_sign_in_goes_is_read_under_the_lock_its_link_is_minted_under(client, ada, monkeypatch):
    """A change of address committing between that read and the mint would
    leave a link for the address it had just retired, mailed there, which
    nothing kills. So the read holds SQLite's write lock, and a writer on
    another connection is refused outright while it runs."""
    refusals: list[str] = []
    read = auth_routes.forwarding_address

    async def read_while_another_connection_writes(session, email):
        other = sqlite3.connect(engine.url.database, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        except sqlite3.OperationalError as refused:
            refusals.append(str(refused))
        finally:
            other.close()
        return await read(session, email)

    monkeypatch.setattr(auth_routes, "forwarding_address", read_while_another_connection_writes)

    assert client.post("/api/auth/magic-link", json={"email": ADA}).status_code == 202

    assert refusals == ["database is locked"]
    (message,) = _sent()
    assert message.to == NEW


async def _moved_away_leaving_a_link() -> tuple[User, str]:
    """An account that signed in as ADA and moved to NEW, and a sign-in link
    for ADA that outlived the move, as an older release or a hand edit leaves
    one. Returns the account and the link's token."""
    account = await get_or_create_magic_link_user(ADA)
    token = await create_magic_link(ADA)
    assert token is not None
    async with async_session_maker() as session:
        async with session.begin():
            await session.execute(update(User).where(User.id == account.id).values(email=NEW))
            session.add(RetiredAddress(email=ADA, user_id=str(account.id), retired_at=datetime.now(UTC)))
    return account, token


def test_a_sign_in_link_carrying_a_retired_address_signs_nobody_in(client):
    """None is minted, and a change kills those still out for the address it
    retires. One that is there all the same, left by an older release or a
    hand edit, would sign whoever reads the old mailbox in to the account that
    left it: it is refused as a dead link is, and spent."""
    account, token = asyncio.run(_moved_away_leaving_a_link())

    refused = client.post("/api/auth/magic-link/consume", json={"token": token})

    assert (refused.status_code, refused.json()) == (400, {"detail": "That sign-in link is no longer valid"})
    assert "set-cookie" not in refused.headers
    assert _read(select(UserSession).where(UserSession.user_id == str(account.id))) == []
    assert client.get(f"/api/auth/magic-link/{token}").status_code == 404
    assert client.post("/api/auth/magic-link/consume", json={"token": token}).status_code == 400


def test_a_sign_in_link_carrying_a_retired_address_previews_as_a_dead_link(client):
    """The page names the address before it redeems, so it would offer a
    sign-in that fails at the press. It reads as a dead link does, and the
    preview leaves the link as it found it."""
    _account, token = asyncio.run(_moved_away_leaving_a_link())

    preview = client.get(f"/api/auth/magic-link/{token}")

    assert (preview.status_code, preview.json()) == (404, {"detail": "That sign-in link is no longer valid"})
    assert preview.json() == client.get("/api/auth/magic-link/never-issued").json()
    (link,) = _read(select(MagicLink))
    assert (link.email, link.used_at) == (ADA, None)


# ── An address one account has and another retired ──────────────────────────


@pytest.fixture(params=["ada@example.com", "Ada@Example.COM"], ids=["as-typed", "stored-in-capitals"])
def rolled_back(request) -> tuple[User, User]:
    """ADA retired to an account now at NEW, and the address of a second
    account as well, which a release from before migration 0025 makes when
    somebody signs in with a retired address. Returns (retired it, has it)."""

    async def _make() -> tuple[User, User]:
        async with async_session_maker() as session:
            async with session.begin():
                moved = User(email=NEW, name="moved", is_verified=True)
                holder = User(email=request.param, name="holder", is_verified=True)
                session.add_all([moved, holder])
                await session.flush()
                session.add(RetiredAddress(email=ADA, user_id=str(moved.id), retired_at=datetime.now(UTC)))
            return moved, holder

    return asyncio.run(_make())


def test_an_address_an_account_has_is_its_own_whoever_retired_it(rolled_back):
    """The account that has it as its address is the one it resolves to, and
    mail for it is not forwarded to the account that retired it."""
    _moved, holder = rolled_back

    async def _resolved() -> tuple[str | None, uuid.UUID | None]:
        async with async_session_maker() as session:
            account = await _account_at(session, ADA)
            return await forwarding_address(session, ADA), account.id if account else None

    assert asyncio.run(_resolved()) == (None, holder.id)
    found, created = asyncio.run(get_or_create_admin_account(ADA))
    assert (found.id, created) == (holder.id, False)


def test_a_claim_on_an_address_an_account_has_is_mailed_there_and_binds_it(client, rolled_back):
    _moved, holder = rolled_back
    bearer, node_ref = _register(NODE)
    _nominate(client, bearer, ADA)

    (message,) = _sent()

    token = token_in(message)
    rendered = mail_templates.claim(node_ref, f"https://{HOST_APP}/auth/claim/{token}", 15)
    assert message == mail.Message(to=ADA, subject=rendered.subject, text=rendered.text, html=rendered.html)
    clicked = client.post("/api/auth/claim/consume", json={"token": token})
    assert (clicked.status_code, clicked.json()["user"]["id"]) == (200, str(holder.id))
    assert _claim(NODE).user_id == str(holder.id)


def test_a_sign_in_with_an_address_an_account_has_is_mailed_there_and_signs_it_in(client, rolled_back):
    _moved, holder = rolled_back

    assert client.post("/api/auth/magic-link", json={"email": ADA}).status_code == 202

    (message,) = _sent()
    assert message.to == ADA
    (token,) = set(_SIGN_IN_LINK.findall(message.text))
    assert client.get(f"/api/auth/magic-link/{token}").json() == {"email": ADA}
    signed_in = client.post("/api/auth/magic-link/consume", json={"token": token})
    assert (signed_in.status_code, signed_in.json()["user"]["id"]) == (200, str(holder.id))


def test_the_account_that_has_it_can_move_away_from_it_and_retires_it_to_itself(client, rolled_back):
    """A change retires the address the account leaves, whose key the other
    account's retirement already holds. The account that has an address wins
    it, so that retirement goes and the address is retired to this account:
    mail for it follows this account to its new address, and signs it in."""
    _moved, holder = rolled_back
    moving_to = "holder.new@example.com"
    asyncio.run(account_email.request_change(holder.id, moving_to, by_account=True))
    (confirmation,) = _sent()
    (change,) = set(_CHANGE_LINK.findall(confirmation.text))

    confirmed = client.post("/api/auth/email-change/consume", json={"token": change})

    assert (confirmed.status_code, confirmed.json()) == (200, {"email": moving_to})
    assert {row.email: row.user_id for row in _read(select(RetiredAddress))} == {ADA: str(holder.id)}
    (notice,) = _sent()
    assert notice.to.lower() == ADA
    assert client.post("/api/auth/magic-link", json={"email": ADA}).status_code == 202
    (message,) = _sent()
    assert message.to == moving_to
    (token,) = set(_SIGN_IN_LINK.findall(message.text))
    signed_in = client.post("/api/auth/magic-link/consume", json={"token": token})
    assert (signed_in.status_code, signed_in.json()["user"]["id"]) == (200, str(holder.id))


def test_an_administrator_connecting_a_retired_address_finds_its_account(ada):
    account, created = asyncio.run(get_or_create_admin_account(ADA.upper()))

    assert (account.id, created) == (ada.id, False)
    assert [account.id for account in _accounts()] == [ada.id]


@pytest.fixture
def radar(monkeypatch):
    """A stock radar answering with no network, on a server that polls, and
    whatever connecting it leaves in memory cleared after."""
    monkeypatch.setenv(blah2_poller.ENABLED_ENV, "1")
    monkeypatch.setattr(polled_registration, "probe_blah2", _canned())
    monkeypatch.setattr(blah2_poller, "refresh", lambda: None)
    polled_probe_limiter.reset()
    before = set(state.connected_nodes)
    yield
    polled_probe_limiter.reset()
    for node_id in set(state.connected_nodes) - before:
        node_retirement.forget_node(node_id)


def test_an_administrators_check_names_the_account_a_retired_address_connects_to(client, ada, radar):
    """The check is what the administrator confirms by eye, so it names the
    account connecting will use, by the address it has now, rather than
    offering to make one for an address that is already an account's."""
    checked = client.post("/api/admin/polled-radars/probe", json={"address": RADAR, "email": "Ada@Example.com"})

    assert checked.status_code == 200
    assert checked.json()["account"] == {"email": NEW, "exists": True}

    connected = client.post(
        "/api/admin/polled-radars",
        json={"address": RADAR, "fingerprint": STOCK_FINGERPRINT, "publication": "private", "email": ADA},
    )

    assert connected.status_code == 201
    assert connected.json()["owner"] == {"email": NEW, "created": False}
    assert _claim(connected.json()["node_id"]).user_id == str(ada.id)
    assert [account.id for account in _accounts()] == [ada.id]
