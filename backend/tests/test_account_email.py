"""Changing the address an account signs in with: services/account_email.py,
behind POST /api/auth/me/email, the link's two routes, and the administrator's
POST /api/admin/users/{id}/email.

Every request here runs as a deployment does: the anonymous-admin bypass off,
the account its auth_token cookie, and the administrator a verified Access
identity. The mail takes its real path into the outbox, encrypted, and is read
back by running the outbox's own worker by hand over a recording transport, so
what a test reads is what would have been sent. The client is bare, with no
lifespan, so no worker of the app's own races it to the queue.
"""

import asyncio
import re
import time
import uuid
from collections import deque

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

import routes.admin as admin
from config.constants import EVENT_LOG_MAX
from core import sessions, users
from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS, _hash_token, create_magic_link
from core.mail_outbox import MailOutbox
from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.rate_counters import RateCounter
from core.users import (
    ACCESS_ASSERTION_HEADER,
    MagicLink,
    PendingAddressChange,
    RetiredAddress,
    User,
    UserSession,
    async_session_maker,
    get_or_create_admin_account,
)
from main import app
from services import account_email, claim_links, mail, mail_outbox, mail_templates, node_auth
from services.node_rate_limits import CLAIM_LIMITS
from tests.claim_mail_helpers import HOST_APP, Transport, configure_mail, token_in
from tests.owner_helpers import real_sessions, sign_in

ADA = "ada@example.com"
NEW = "ada.new@example.com"
GRACE = "grace@example.com"
OPERATOR = "operator@example.com"
NODE = "ret5e6f7a8b"
OTHER_NODE = "ret9c0d1e2f"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}

_LINK = re.compile(r"https://" + re.escape(HOST_APP) + r"/auth/email/([A-Za-z0-9_-]+)")


class _Access:
    """Stands in for core.users.access_identity, verifying ASSERTION as OPERATOR."""

    def is_configured(self) -> bool:
        return True

    async def identity(self, token: str | None) -> str | None:
        return OPERATOR if token == ASSERTION else None


async def _empty_queue_and_counters() -> None:
    """Neither table is emptied between tests by conftest.py, and the limits
    count in rate_counters, so a test left to inherit them would be refused for
    what an earlier one spent."""
    async with async_session_maker() as session:
        await session.execute(delete(MailOutbox))
        await session.execute(delete(RateCounter))
        await session.commit()


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch, tmp_path):
    configure_mail(monkeypatch, tmp_path)
    real_sessions(monkeypatch)
    monkeypatch.setattr(users, "access_identity", _Access())
    # This test's events alone, and saved under tmp_path rather than backend/data.
    monkeypatch.setattr(admin, "_events", deque(maxlen=EVENT_LOG_MAX))
    monkeypatch.setattr(admin, "_EVENTS_FILE", tmp_path / "events.json")
    asyncio.run(_empty_queue_and_counters())
    yield
    asyncio.run(_empty_queue_and_counters())


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _cookie(token: str) -> dict[str, str]:
    # By header: the cookie is Secure and the test client speaks plain HTTP.
    return {"Cookie": f"{sessions.COOKIE_NAME}={token}"}


def _signed_in(client: TestClient, email: str) -> dict[str, str]:
    return _cookie(sign_in(client, email))


def _sent(*deliveries: mail.Delivery) -> list[mail.Message]:
    """Run the outbox's worker until nothing is due, and return what it sent."""
    transport = Transport(*deliveries)

    async def _drain() -> None:
        while await mail_outbox.run_once(owner="test-worker", transport=transport):
            pass

    asyncio.run(_drain())
    return transport.sent


def _token(message: mail.Message) -> str:
    """The one address-change token the message carries, the same in both parts."""
    tokens = set(_LINK.findall(message.text)) | set(_LINK.findall(message.html))
    assert len(tokens) == 1, tokens
    return tokens.pop()


def _ask(client: TestClient, cookie: dict[str, str], email: str):
    return client.post("/api/auth/me/email", json={"email": email}, headers=cookie)


def _confirm(client: TestClient, token: str):
    return client.post("/api/auth/email-change/consume", json={"token": token})


def _change(client: TestClient, cookie: dict[str, str], email: str) -> None:
    """Change the signed-in account's address the whole way, mail and all."""
    assert _ask(client, cookie, email).status_code == 202
    (message,) = _sent()
    assert _confirm(client, _token(message)).status_code == 200
    _sent()  # the notice


def _read(statement):
    async def _go():
        async with async_session_maker() as session:
            return list((await session.execute(statement)).scalars())

    return asyncio.run(_go())


def _account(email: str) -> User | None:
    (found,) = _read(select(User).where(func.lower(User.email) == email)) or [None]
    return found


def _id_of(email: str) -> uuid.UUID:
    account = _account(email)
    assert account is not None, f"no account at {email}"
    return account.id


def _accounts() -> int:
    return len(_read(select(User)))


def _retired() -> dict[str, str]:
    return {row.email: row.user_id for row in _read(select(RetiredAddress))}


def _waiting() -> list[tuple[str, str]]:
    return [(row.user_id, row.email) for row in _read(select(PendingAddressChange))]


def _link_rows(token: str) -> list[MagicLink]:
    return _read(select(MagicLink).where(MagicLink.token_hash == _hash_token(token)))


def _change_links() -> list[MagicLink]:
    return _read(select(MagicLink).where(MagicLink.intent == account_email.INTENT_EMAIL_CHANGE))


def _queue() -> list[MailOutbox]:
    return _read(select(MailOutbox).order_by(MailOutbox.id))


def _counters() -> list[RateCounter]:
    return _read(select(RateCounter))


def _register(node_id: str) -> str:
    """A registered node in the suite's shared database, and its bearer token."""

    async def _go() -> str:
        async with async_session_maker() as session:
            async with session.begin():
                session.add(Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), board_model="raspberrypi5-4gb"))
                await session.flush()
                token = await node_auth.mint_token(session, node_id)
            return token

    return asyncio.run(_go())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _nominate(client: TestClient, bearer: str, email: str) -> None:
    """The node offers `email`, as its setup page does, and the claim mail is queued."""
    r = client.put("/v1/nodes/claim", json={"email": email}, headers=_bearer(bearer))
    assert (r.status_code, r.json()["state"]) == (200, "pending"), r.text


def _claim_state(client: TestClient, bearer: str) -> dict:
    """What the node reads about its own claim."""
    return client.get("/v1/nodes/claim", headers=_bearer(bearer)).json()


def _claim(node_id: str) -> NodeClaim:
    (row,) = _read(select(NodeClaim).where(NodeClaim.node_id == node_id))
    return row


def _challenge(node_id: str) -> NodeClaimChallenge | None:
    (row,) = _read(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id)) or [None]
    return row


def _challenge_handle(node_id: str) -> str | None:
    challenge = _challenge(node_id)
    return challenge.handle if challenge is not None else None


def _claim_mail() -> dict[str, str]:
    """What became of each node's claim mail in the queue, by node."""
    return {row.node_id: row.status for row in _queue() if row.kind == mail_outbox.KIND_CLAIM}


def _claim_links() -> list[MagicLink]:
    return _read(select(MagicLink).where(MagicLink.intent == claim_links.INTENT_CLAIM))


# ── The change, the whole way ────────────────────────────────────────────────


def test_the_new_address_confirms_the_change_through_the_outbox(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None

    asked = _ask(client, cookie, "  Ada.New@Example.com ")

    assert asked.status_code == 202
    assert asked.json() == {"status": "sent", "email": NEW, "expires_in": 900}
    # Nothing changes until the link is opened.
    assert _account(ADA) is not None and _account(NEW) is None
    assert _waiting() == [(str(ada.id), NEW)]

    (message,) = _sent()
    token = _token(message)
    rendered = mail_templates.address_change(f"https://{HOST_APP}/auth/email/{token}", 15)
    assert message == mail.Message(to=NEW, subject=rendered.subject, text=rendered.text, html=rendered.html)
    (row,) = _queue()
    assert (row.kind, row.recipient, row.status, row.body) == (mail_outbox.KIND_ADDRESS_CHANGE, NEW, "sent", None)

    assert client.get(f"/api/auth/email-change/{token}").json() == {"email": NEW}
    confirmed = _confirm(client, token)

    assert confirmed.status_code == 200
    assert confirmed.json() == {"email": NEW}
    moved = _account(NEW)
    assert moved is not None and moved.id == ada.id
    assert _account(ADA) is None
    assert _accounts() == 1
    assert _retired() == {ADA: str(ada.id)}
    assert _waiting() == []
    # The session that asked is still signed in, now as the new address.
    me = client.get("/api/auth/me", headers=cookie)
    assert (me.status_code, me.json()["email"], me.json()["id"]) == (200, NEW, str(ada.id))


def test_confirming_opens_no_session(client):
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    before = len(_read(select(UserSession)))

    confirmed = _confirm(client, _token(message))

    assert confirmed.status_code == 200
    assert "set-cookie" not in confirmed.headers
    assert len(_read(select(UserSession))) == before


def test_opening_the_link_marks_the_address_verified(client):
    """An account an administrator made is unverified until a link sent to it
    is opened. Opening the link to its new address is that proof."""
    made, created = asyncio.run(get_or_create_admin_account(GRACE))
    assert created and not made.is_verified

    asked = client.post(f"/api/admin/users/{made.id}/email", json={"email": NEW}, headers=AS_ADMIN)
    assert asked.status_code == 202
    (message,) = _sent()
    assert _confirm(client, _token(message)).status_code == 200

    moved = _account(NEW)
    assert moved is not None and moved.id == made.id and moved.is_verified


def test_the_old_address_is_told_without_being_told_the_new_one(client):
    """The old mailbox may be somebody else's by now, which may be why the
    address changed, so the notice names nothing that says where the account
    went, and carries no link."""
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    assert _confirm(client, _token(message)).status_code == 200

    (notice,) = _sent()

    rendered = mail_templates.address_changed()
    assert notice == mail.Message(to=ADA, subject=rendered.subject, text=rendered.text, html=rendered.html)
    for part in (notice.subject, notice.text, notice.html):
        assert NEW not in part
        assert "ada.new" not in part.lower()
        assert "http" not in part
    notices = [row for row in _queue() if row.kind == mail_outbox.KIND_ADDRESS_NOTICE]
    assert [(row.recipient, row.status) for row in notices] == [(ADA, "sent")]


def test_an_address_stored_in_another_case_is_retired_as_addresses_are_keyed(client):
    """An account from before addresses were normalised keeps the case it was
    typed in. Retired, it is keyed the way sign-in keys an address, so it finds
    the account whatever case it is typed in later, and the notice still goes
    to the address as the account held it."""

    async def _legacy() -> None:
        async with async_session_maker() as session:
            async with session.begin():
                session.add(User(email="Ada@Example.COM", name="ada", is_verified=True))

    asyncio.run(_legacy())
    cookie = _signed_in(client, ADA)
    legacy = _id_of(ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()

    assert _confirm(client, _token(message)).status_code == 200

    (notice,) = _sent()
    assert notice.to == "Ada@Example.COM"
    assert _retired() == {ADA: str(legacy)}
    found, created = asyncio.run(get_or_create_admin_account("ADA@example.com"))
    assert (found.id, created) == (legacy, False)


# ── What a request is refused for ────────────────────────────────────────────


def test_a_request_needs_a_session(client):
    r = client.post("/api/auth/me/email", json={"email": NEW})

    assert r.status_code == 401
    assert _change_links() == [] and _queue() == []


@pytest.mark.parametrize(
    "email",
    ["not-an-address", "ada@localhost", "a b@example.com", "", "Ada <ada.new@example.com>", "x" * 250 + "@example.com"],
)
def test_something_that_is_not_an_address_is_400(client, email):
    cookie = _signed_in(client, ADA)

    r = _ask(client, cookie, email)

    assert r.status_code == 400
    assert r.json() == {"detail": "That is not an email address"}
    assert _change_links() == [] and _queue() == [] and _waiting() == []


def test_the_accounts_own_address_is_400_whatever_its_case(client):
    cookie = _signed_in(client, ADA)

    r = _ask(client, cookie, "ADA@Example.com")

    assert r.status_code == 400
    assert r.json() == {"detail": "That is already this account's address"}
    assert _change_links() == [] and _queue() == []


# ── Another account's address ────────────────────────────────────────────────


def test_another_accounts_address_is_answered_as_a_free_one_and_told_instead(client):
    """No signed-in account is told whose an address is, as sign-in tells
    nobody. The address is sent a notice in place of the link, which names
    nothing of the account that asked and has nothing to open."""
    sign_in(client, GRACE)
    grace = _id_of(GRACE)
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)

    r = _ask(client, cookie, "Grace@Example.com")

    assert r.status_code == 202
    assert r.json() == {"status": "sent", "email": GRACE, "expires_in": 900}
    assert _change_links() == [] and _waiting() == []
    (notice,) = _sent()
    rendered = mail_templates.address_taken()
    assert notice == mail.Message(to=GRACE, subject=rendered.subject, text=rendered.text, html=rendered.html)
    for part in (notice.subject, notice.text, notice.html):
        assert ADA not in part.lower()
        assert str(ada) not in part
        assert "http" not in part
    assert [(row.kind, row.recipient, row.status) for row in _queue()] == [
        (mail_outbox.KIND_ADDRESS_TAKEN, GRACE, "sent")
    ]
    assert (_id_of(GRACE), _id_of(ADA)) == (grace, ada)


def test_an_address_another_account_retired_is_answered_as_a_free_one_and_sent_nothing(client):
    """A retired address is never mailed, so it is not told either."""
    _change(client, _signed_in(client, GRACE), "grace.new@example.com")
    cookie = _signed_in(client, ADA)
    queued = len(_queue())

    r = _ask(client, cookie, GRACE)

    assert r.status_code == 202
    assert r.json() == {"status": "sent", "email": GRACE, "expires_in": 900}
    assert [link for link in _change_links() if link.email == GRACE] == []
    assert _waiting() == []
    assert len(_queue()) == queued and _sent() == []


def test_the_answer_says_nothing_of_whose_the_address_is(client):
    """Another account's address and one it retired, a free address, and one
    the asking account retired itself all get the one answer."""
    sign_in(client, "bob@example.com")
    _change(client, _signed_in(client, GRACE), "grace.new@example.com")
    cookie = _signed_in(client, ADA)
    _change(client, cookie, NEW)

    for address in ("bob@example.com", GRACE, "nobody@example.com", ADA):
        r = _ask(client, cookie, address)
        assert (r.status_code, r.json()) == (202, {"status": "sent", "email": address, "expires_in": 900}), address


@pytest.mark.parametrize("taken", ["current", "retired"])
def test_asking_for_another_accounts_address_kills_the_link_waiting_before_it(client, taken):
    """As a request for a free address replaces the change waiting, so does
    one for a taken address, or whether the earlier link still opens would
    say whose the second address is."""
    if taken == "current":
        sign_in(client, GRACE)
    else:
        _change(client, _signed_in(client, GRACE), "grace.new@example.com")
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (first,) = _sent()
    stale = _token(first)

    assert _ask(client, cookie, GRACE).status_code == 202

    assert _waiting() == []
    assert _link_rows(stale) == []
    assert client.get(f"/api/auth/email-change/{stale}").status_code == 404
    assert _confirm(client, stale).status_code == 400
    assert _account(ADA) is not None and _account(NEW) is None


@pytest.mark.parametrize("taken", ["current", "retired"])
def test_the_administrator_is_told_the_address_is_another_accounts(client, taken):
    """The administrator's route keeps its 409, and the address is told nothing."""
    if taken == "current":
        sign_in(client, GRACE)
    else:
        _change(client, _signed_in(client, GRACE), "grace.new@example.com")
    sign_in(client, ADA)
    queued = len(_queue())

    r = client.post(f"/api/admin/users/{_id_of(ADA)}/email", json={"email": GRACE}, headers=AS_ADMIN)

    assert (r.status_code, r.json()) == (409, {"detail": "That address belongs to another account"})
    assert _waiting() == [] and len(_queue()) == queued


def test_an_address_the_account_retired_is_its_own_to_take_back(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None
    _change(client, cookie, NEW)
    assert _retired() == {ADA: str(ada.id)}

    asked = _ask(client, cookie, ADA)
    assert asked.status_code == 202
    (message,) = _sent()
    assert _confirm(client, _token(message)).status_code == 200

    back = _account(ADA)
    assert back is not None and back.id == ada.id
    # The address it has again is no longer a retired one; the one it left is.
    assert _retired() == {NEW: str(ada.id)}


def test_no_mail_means_503_and_nothing_written(client, monkeypatch):
    cookie = _signed_in(client, ADA)
    monkeypatch.delenv("HOST_APP")

    r = _ask(client, cookie, NEW)

    assert r.status_code == 503
    assert r.json() == {"detail": "Changing the address by email is unavailable"}
    assert _change_links() == [] and _queue() == [] and _waiting() == [] and _counters() == []


def test_an_account_is_held_to_the_claim_limits(client):
    """Five an hour, as a node's claim is held to, whichever addresses."""
    cookie = _signed_in(client, ADA)
    hourly, _window = CLAIM_LIMITS[0]
    for n in range(hourly):
        assert _ask(client, cookie, f"ada.{n}@example.com").status_code == 202

    refused = _ask(client, cookie, "ada.more@example.com")

    assert refused.status_code == 429
    assert 3000 < int(refused.headers["Retry-After"]) <= 3600
    assert [row.recipient for row in _queue()] == [f"ada.{n}@example.com" for n in range(hourly)]
    assert _waiting() == [(str(_id_of(ADA)), f"ada.{hourly - 1}@example.com")]


@pytest.mark.parametrize("taken", [False, True], ids=["free", "taken"])
def test_an_address_is_held_to_the_claim_limits_across_accounts(client, taken):
    """However many accounts ask for it, one mailbox is sent no more than a
    node's claim may send it, links and notices alike. The wait counts down
    the limit's hour, not the fifteen minutes the link store's own cap would."""
    if taken:
        sign_in(client, "shared@example.com")
    hourly, _window = CLAIM_LIMITS[0]
    for n in range(hourly):
        assert _ask(client, _signed_in(client, f"user{n}@example.com"), "shared@example.com").status_code == 202

    refused = _ask(client, _signed_in(client, "one-more@example.com"), "shared@example.com")

    assert refused.status_code == 429
    assert 3000 < int(refused.headers["Retry-After"]) <= 3600
    assert len(_queue()) == hourly


# ── One change waiting per account ───────────────────────────────────────────


def test_a_second_request_replaces_the_first_and_kills_its_link(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None
    _ask(client, cookie, "typo@example.com")
    (first,) = _sent()
    _ask(client, cookie, NEW)
    (second,) = _sent()
    stale = _token(first)

    assert _waiting() == [(str(ada.id), NEW)]
    # Dead in the store, not only refused at the door.
    assert _link_rows(stale) == []
    assert client.get(f"/api/auth/email-change/{stale}").status_code == 404
    assert _confirm(client, stale).status_code == 400
    assert _account(ADA) is not None

    assert _confirm(client, _token(second)).status_code == 200
    assert _account(NEW) is not None and _account("typo@example.com") is None


def test_a_live_link_that_is_not_the_waiting_change_is_400(client):
    """A link of this kind the account never asked for, or one whose change
    has gone, confirms nothing even while the store still holds it."""
    _signed_in(client, ADA)
    stray = asyncio.run(create_magic_link(NEW, intent=account_email.INTENT_EMAIL_CHANGE))
    assert stray is not None

    assert client.get(f"/api/auth/email-change/{stray}").status_code == 404
    r = _confirm(client, stray)

    assert r.status_code == 400
    assert r.json() == {"detail": "That link is no longer valid"}
    assert _account(ADA) is not None and _account(NEW) is None


# ── Read again under the write lock ──────────────────────────────────────────
#
# A request reads the account before it spends the limits, and whose the
# address is only after, under the lock its link is minted under. Anything
# that lands in between is what the second read is for.


def _meanwhile(monkeypatch, happens) -> None:
    """Run `happens(address)` once the limits are spent, before the lock is taken."""
    admit = account_email._admit

    async def admitted_then(session, user_key, address):
        wait_s = await admit(session, user_key, address)
        await happens(address)
        return wait_s

    monkeypatch.setattr(account_email, "_admit", admitted_then)


def test_an_address_the_account_moved_to_meanwhile_is_400(client, monkeypatch):
    """Asked again for the address a change is waiting on, and that change
    confirmed while the request is under way: it is the account's own address
    by the lock, which is 400 as it would be asked a moment later, not a
    second link to it."""
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    token = _token(message)

    async def confirmed(_address):
        await account_email.confirm(token)

    _meanwhile(monkeypatch, confirmed)

    r = _ask(client, cookie, NEW)

    assert (r.status_code, r.json()) == (400, {"detail": "That is already this account's address"})
    assert _id_of(NEW) == ada
    assert _waiting() == []
    assert [(row.kind, row.recipient) for row in _queue()][1:] == [(mail_outbox.KIND_ADDRESS_NOTICE, ADA)]


@pytest.mark.parametrize("by", ["account", "administrator"])
def test_an_address_taken_meanwhile_gets_no_link(client, monkeypatch, by):
    """Somebody signs in with the address while the request is under way, so
    it is theirs by the lock: the account is answered as before and the
    address told, the administrator is answered 409, and no link is minted."""
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)

    async def signed_in(address):
        await users.get_or_create_magic_link_user(address)

    _meanwhile(monkeypatch, signed_in)

    if by == "account":
        r = _ask(client, cookie, NEW)
        assert (r.status_code, r.json()) == (202, {"status": "sent", "email": NEW, "expires_in": 900})
        assert [(row.kind, row.recipient) for row in _queue()] == [(mail_outbox.KIND_ADDRESS_TAKEN, NEW)]
    else:
        r = client.post(f"/api/admin/users/{ada}/email", json={"email": NEW}, headers=AS_ADMIN)
        assert (r.status_code, r.json()) == (409, {"detail": "That address belongs to another account"})
        assert _queue() == []
    assert _change_links() == [] and _waiting() == []
    assert _id_of(NEW) != ada and _id_of(ADA) == ada


@pytest.mark.parametrize("by", ["account", "administrator"])
def test_an_account_deleted_meanwhile_is_404_and_gets_no_link(client, monkeypatch, by):
    """Deleted while the request is under way, the account is gone by the
    lock: 404, as an account that was never there is, with nothing minted,
    left waiting or mailed."""
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)

    async def deleted(_address):
        assert await sessions.remove_account(ada)

    _meanwhile(monkeypatch, deleted)

    if by == "account":
        r = _ask(client, cookie, NEW)
    else:
        r = client.post(f"/api/admin/users/{ada}/email", json={"email": NEW}, headers=AS_ADMIN)

    assert (r.status_code, r.json()) == (404, {"detail": "User not found"})
    assert _change_links() == [] and _waiting() == [] and _queue() == []
    assert _account(ADA) is None and _account(NEW) is None


# ── Opening the link ─────────────────────────────────────────────────────────


def test_a_link_works_once(client):
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    token = _token(message)
    assert _confirm(client, token).status_code == 200

    again = _confirm(client, token)

    assert again.status_code == 400
    assert again.json() == {"detail": "That link is no longer valid"}
    assert client.get(f"/api/auth/email-change/{token}").status_code == 404


def test_an_unknown_link_is_400_and_its_preview_404(client):
    assert _confirm(client, "not-a-token").status_code == 400
    assert client.get("/api/auth/email-change/not-a-token").status_code == 404


def test_an_address_taken_meanwhile_is_409_and_changes_nothing(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None
    _ask(client, cookie, NEW)
    (message,) = _sent()
    # Somebody else signs in with it before the link is opened.
    sign_in(client, NEW)

    r = _confirm(client, _token(message))

    assert r.status_code == 409
    assert r.json() == {"detail": "That address belongs to another account"}
    assert _id_of(ADA) == ada.id
    assert _id_of(NEW) != ada.id
    assert _retired() == {}
    # Nothing was written, the notice included.
    assert _sent() == []


def test_the_old_addresss_sign_in_links_die_with_the_change(client):
    """A sign-in link still in the old mailbox would sign whoever reads it in."""
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    waiting = asyncio.run(create_magic_link(ADA))
    assert waiting is not None

    assert _confirm(client, _token(message)).status_code == 200

    assert _link_rows(waiting) == []
    assert client.post("/api/auth/magic-link/consume", json={"token": waiting}).status_code == 400


def test_a_deactivated_account_cannot_confirm_until_it_is_reactivated(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None
    _ask(client, cookie, NEW)
    (message,) = _sent()
    token = _token(message)
    asyncio.run(sessions.deactivate_account(ada.id))

    refused = _confirm(client, token)

    assert refused.status_code == 400
    assert _account(ADA) is not None and _account(NEW) is None
    # Refused before it was spent, so it works once the account may sign in again.
    asyncio.run(sessions.reactivate_account(ada.id))
    assert _confirm(client, token).status_code == 200
    assert _id_of(NEW) == ada.id


def test_the_links_of_other_kinds_do_not_cross(client):
    """A change's link signs nobody in, and a sign-in link changes nothing."""
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (message,) = _sent()
    change = _token(message)
    signin = asyncio.run(create_magic_link(ADA))
    assert signin is not None

    assert client.post("/api/auth/magic-link/consume", json={"token": change}).status_code == 400
    assert _confirm(client, signin).status_code == 400
    # Neither was spent by the route that refused it.
    assert _confirm(client, change).status_code == 200


# ── Claims waiting in the old mailbox ────────────────────────────────────────


def test_a_claim_link_in_the_old_mailbox_dies_with_the_change(client):
    """A retired address still finds its account, so a claim link mailed there
    before the change would bind its node to the account and sign whoever
    opens it in, and the old mailbox may be somebody else's by now. It dies
    with the change, as the old address's sign-in links do, and so does the
    challenge it was for: the node reads unclaimed with its address on file,
    and asking again mails the address the account has now."""
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)
    bearer = _register(NODE)
    _nominate(client, bearer, ADA)
    (claim_mail,) = _sent()
    assert claim_mail.to == ADA
    stale = token_in(claim_mail)
    _ask(client, cookie, NEW)
    (change,) = _sent()
    signed_in = len(_read(select(UserSession)))

    assert _confirm(client, _token(change)).status_code == 200
    assert [message.to for message in _sent()] == [ADA]  # the notice alone

    clicked = client.post("/api/auth/claim/consume", json={"token": stale})
    assert (clicked.status_code, clicked.json()) == (400, {"detail": "That link is no longer valid"})
    assert "set-cookie" not in clicked.headers
    assert len(_read(select(UserSession))) == signed_in
    assert client.get(f"/api/auth/claim/{stale}").status_code == 404
    assert _claim_links() == []
    claim = _claim(NODE)
    assert (claim.user_id, claim.email, claim.verified) == (None, ADA, False)
    assert _challenge(NODE) is None
    assert _claim_state(client, bearer) == {"state": "unclaimed", "email": ADA, "undeliverable": False}

    resent = client.post("/v1/nodes/claim/resend", headers=_bearer(bearer))
    assert resent.json()["state"] == "pending"
    (forwarded,) = _sent()
    assert forwarded.to == NEW
    bound = client.post("/api/auth/claim/consume", json={"token": token_in(forwarded)})
    assert (bound.status_code, bound.json()["user"]["id"]) == (200, str(ada))
    assert (_claim(NODE).user_id, _claim(NODE).email) == (str(ada), ADA)


def test_a_claim_mail_still_queued_for_the_old_address_never_goes(client):
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, NEW)
    (change,) = _sent()
    bearer = _register(NODE)
    _nominate(client, bearer, ADA)
    assert _claim_mail() == {NODE: "queued"}

    assert _confirm(client, _token(change)).status_code == 200

    assert _claim_mail() == {NODE: "cancelled"}
    assert [row.body for row in _queue() if row.kind == mail_outbox.KIND_CLAIM] == [None]
    assert _claim_links() == []
    assert _challenge(NODE) is None
    assert _claim_state(client, bearer) == {"state": "unclaimed", "email": ADA, "undeliverable": False}
    assert [message.to for message in _sent()] == [ADA]  # the notice alone


@pytest.mark.parametrize("then", ["ada.third@example.com", ADA], ids=["moving-on", "taking-it-back"])
def test_a_claim_link_forwarded_to_the_old_address_dies_with_the_change(client, then):
    """A node given an address the account had retired is mailed at the one the
    account had then, so the link sits in that mailbox, and it is the one
    retired next. It dies as a link mailed there directly does, whether the
    account moves on or takes back the address the node was given."""
    cookie = _signed_in(client, ADA)
    _change(client, cookie, NEW)
    bearer = _register(NODE)
    _nominate(client, bearer, ADA)
    (claim_mail,) = _sent()
    assert claim_mail.to == NEW
    stale = token_in(claim_mail)
    _ask(client, cookie, then)
    (change,) = _sent()

    assert _confirm(client, _token(change)).status_code == 200

    assert _claim_links() == []
    assert client.get(f"/api/auth/claim/{stale}").status_code == 404
    clicked = client.post("/api/auth/claim/consume", json={"token": stale})
    assert clicked.status_code == 400
    assert "set-cookie" not in clicked.headers
    assert _claim(NODE).user_id is None
    assert _claim_state(client, bearer) == {"state": "unclaimed", "email": ADA, "undeliverable": False}


def test_claims_on_other_addresses_are_left_as_they_stand(client):
    """Another account's address, and the new address itself, which was nobody's
    when a node was given it: their links went to mailboxes the change leaves
    where they were, so the claims, their links and their mail all stand."""
    sign_in(client, GRACE)
    grace = _id_of(GRACE)
    cookie = _signed_in(client, ADA)
    ada = _id_of(ADA)
    _ask(client, cookie, NEW)
    (change,) = _sent()
    graces, news = _register(NODE), _register(OTHER_NODE)
    _nominate(client, graces, GRACE)
    _nominate(client, news, NEW)
    links = {row.token_hash for row in _claim_links()}
    challenges = {node_id: _challenge_handle(node_id) for node_id in (NODE, OTHER_NODE)}
    assert len(links) == 2 and None not in challenges.values()

    assert _confirm(client, _token(change)).status_code == 200

    assert {row.token_hash for row in _claim_links()} == links
    assert {node_id: _challenge_handle(node_id) for node_id in (NODE, OTHER_NODE)} == challenges
    assert _claim_mail() == {NODE: "queued", OTHER_NODE: "queued"}
    assert _claim_state(client, graces) == {"state": "pending", "email": GRACE, "undeliverable": False}
    assert _claim_state(client, news) == {"state": "pending", "email": NEW, "undeliverable": False}
    mailed = {message.to: message for message in _sent()}
    assert set(mailed) == {ADA, GRACE, NEW}
    for address, owner in ((GRACE, grace), (NEW, ada)):
        bound = client.post("/api/auth/claim/consume", json={"token": token_in(mailed[address])})
        assert (bound.status_code, bound.json()["user"]["id"]) == (200, str(owner))


def test_a_node_offered_another_address_since_keeps_that_claim(client):
    """An administrator clearing a node's owner drops its challenge and leaves
    the link to fail at the click, so a link in the old mailbox can outlive
    the challenge it was for. The node offered another address since has a
    challenge and a mail of its own, which the change leaves alone while the
    old link dies."""
    cookie = _signed_in(client, ADA)
    bearer = _register(NODE)
    _nominate(client, bearer, ADA)
    (claim_mail,) = _sent()
    stale = token_in(claim_mail)
    _ask(client, cookie, NEW)
    (change,) = _sent()
    cleared = client.put(f"/api/admin/nodes/{NODE}/owner", json={"user_id": None}, headers=AS_ADMIN)
    assert cleared.status_code == 200
    assert client.get(f"/api/auth/claim/{stale}").status_code == 200
    _nominate(client, bearer, GRACE)
    graces = _challenge_handle(NODE)

    assert _confirm(client, _token(change)).status_code == 200

    assert _link_rows(stale) == []
    assert client.get(f"/api/auth/claim/{stale}").status_code == 404
    assert graces is not None and _challenge_handle(NODE) == graces
    claim_rows = [(row.recipient, row.status) for row in _queue() if row.kind == mail_outbox.KIND_CLAIM]
    assert claim_rows == [(ADA, "sent"), (GRACE, "queued")]
    assert _claim_state(client, bearer) == {"state": "pending", "email": GRACE, "undeliverable": False}
    assert [message.to for message in _sent()] == [GRACE, ADA]  # GRACE's claim, then the notice


def test_a_claim_on_a_retired_address_another_account_has_since_is_left_alone(client):
    """An address Ada retired that somebody else signs in with later, as a
    release from before migration 0025 lets them, is theirs: claim mail for it
    goes to it rather than to Ada. So it is not in the mailbox Ada moves away
    from, and her next change leaves its link where it is."""
    cookie = _signed_in(client, ADA)
    _change(client, cookie, NEW)

    async def _signed_in_on_an_older_release() -> uuid.UUID:
        async with async_session_maker() as session:
            async with session.begin():
                holder = User(email=ADA, name="holder", is_verified=True)
                session.add(holder)
            return holder.id

    holder = asyncio.run(_signed_in_on_an_older_release())
    bearer = _register(NODE)
    _nominate(client, bearer, ADA)
    (claim_mail,) = _sent()
    assert claim_mail.to == ADA
    links = {row.token_hash for row in _claim_links()}

    _change(client, cookie, "ada.third@example.com")

    assert {row.token_hash for row in _claim_links()} == links
    assert _claim_state(client, bearer) == {"state": "pending", "email": ADA, "undeliverable": False}
    bound = client.post("/api/auth/claim/consume", json={"token": token_in(claim_mail)})
    assert (bound.status_code, bound.json()["user"]["id"]) == (200, str(holder))


# ── The administrator's route ────────────────────────────────────────────────


def test_an_administrator_starts_the_change_for_an_account(client):
    sign_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None

    asked = client.post(f"/api/admin/users/{ada.id}/email", json={"email": NEW}, headers=AS_ADMIN)

    assert asked.status_code == 202
    assert asked.json() == {"status": "sent", "email": NEW}
    # The same confirmation, to the new address, and nothing until it is opened.
    (message,) = _sent()
    assert message.to == NEW
    assert _account(ADA) is not None
    assert _confirm(client, _token(message)).status_code == 200
    assert _id_of(NEW) == ada.id
    (event,) = client.get("/api/admin/events", headers=AS_ADMIN).json()
    assert event["category"] == "user"
    assert str(ada.id) in event["message"]
    assert event["meta"] == {"user_id": str(ada.id), "by": OPERATOR}


def test_the_administrators_route_is_for_administrators(client):
    cookie = _signed_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None

    assert client.post(f"/api/admin/users/{ada.id}/email", json={"email": NEW}, headers=cookie).status_code == 403
    assert client.post(f"/api/admin/users/{ada.id}/email", json={"email": NEW}).status_code == 401
    assert _queue() == [] and _waiting() == []


@pytest.mark.parametrize("user_id", ["5b0c9e1d-7a2f-4c3e-8d6b-1f0e9a8b7c6d", "not-a-uuid"])
def test_the_administrators_route_404s_an_id_that_is_no_account(client, user_id):
    r = client.post(f"/api/admin/users/{user_id}/email", json={"email": NEW}, headers=AS_ADMIN)

    assert r.status_code == 404
    assert r.json() == {"detail": "User not found"}


def test_the_administrators_route_keeps_the_same_rules(client):
    sign_in(client, GRACE)
    sign_in(client, ADA)
    ada = _account(ADA)
    assert ada is not None
    path = f"/api/admin/users/{ada.id}/email"

    assert client.post(path, json={"email": GRACE}, headers=AS_ADMIN).status_code == 409
    assert client.post(path, json={"email": "nope"}, headers=AS_ADMIN).status_code == 400
    assert client.post(path, json={"email": ADA}, headers=AS_ADMIN).status_code == 400
    hourly, _window = CLAIM_LIMITS[0]
    # The 409 above spent one of the account's allowance already.
    for n in range(hourly - 1):
        assert client.post(path, json={"email": f"ada.{n}@example.com"}, headers=AS_ADMIN).status_code == 202
    limited = client.post(path, json={"email": "ada.more@example.com"}, headers=AS_ADMIN)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def _fill_link_store(address: str) -> list[str]:
    """As many live change links for `address` as the link store holds, which
    accounts asking for one address at once can leave inside the limits."""
    tokens = [
        asyncio.run(create_magic_link(address, intent=account_email.INTENT_EMAIL_CHANGE))
        for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS)
    ]
    assert None not in tokens
    assert asyncio.run(create_magic_link(address, intent=account_email.INTENT_EMAIL_CHANGE)) is None
    return [token for token in tokens if token is not None]


def test_a_full_link_store_is_answered_to_the_account_as_a_taken_address_is(client):
    """A taken address never fills the link store, as no link is minted for
    one, so a wait here would say the address is free. The account is answered
    as for a taken address: no new link, and the change it had waiting gone.
    The links already out for the address still work."""
    cookie = _signed_in(client, ADA)
    _ask(client, cookie, "typo@example.com")
    (first,) = _sent()
    stale = _token(first)
    held = _fill_link_store(NEW)

    r = _ask(client, cookie, NEW)

    assert (r.status_code, r.json()) == (202, {"status": "sent", "email": NEW, "expires_in": 900})
    assert "retry-after" not in r.headers
    assert _waiting() == [] and _link_rows(stale) == []
    assert {link.token_hash for link in _change_links()} == {_hash_token(token) for token in held}
    assert [row.recipient for row in _queue()] == ["typo@example.com"]
    assert _sent() == []


def test_a_full_link_store_is_refused_to_the_administrator_as_a_limit_is(client):
    """The administrator is told whose an address is in any case (a 409), so is
    told to wait here, the wait a link takes to expire, and the change the
    account had waiting stands."""
    sign_in(client, ADA)
    ada = _id_of(ADA)
    path = f"/api/admin/users/{ada}/email"
    assert client.post(path, json={"email": "typo@example.com"}, headers=AS_ADMIN).status_code == 202
    (first,) = _sent()
    _fill_link_store(NEW)

    r = client.post(path, json={"email": NEW}, headers=AS_ADMIN)

    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) == account_email.CHANGE_EXPIRY_S
    assert _waiting() == [(str(ada), "typo@example.com")]
    assert client.get(f"/api/auth/email-change/{_token(first)}").json() == {"email": "typo@example.com"}
    assert _sent() == []


def test_the_link_lives_as_long_as_a_sign_in_link(client):
    cookie = _signed_in(client, ADA)
    before = time.time()
    _ask(client, cookie, NEW)

    (link,) = _change_links()
    (row,) = _queue()

    assert before <= link.created_at <= time.time()
    assert link.expires_at == row.expires_at == link.created_at + 900
