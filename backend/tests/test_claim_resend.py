"""Asking for a claim mail again from the claim page and from the dashboard:
POST /api/auth/claim/resend, GET /api/auth/me/claims and POST
/api/auth/me/claims/{node_ref}/resend, under services/claim_resend.py's rules,
which the node's own POST /v1/nodes/claim/resend asks under too.

Every request runs as a deployment does: the anonymous-admin bypass off and
the account its auth_token cookie. The node's routes run against the suite's
shared database, so the claim, the link, the queue, the rate counters and the
accounts are one file, as on a droplet, and the mail is read back by running
the outbox's worker by hand over a recording transport. The client is bare,
with no lifespan, so no worker of the app's own races it to the queue.
"""

import asyncio
import sqlite3
import sys
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update

from core.auth import _MAX_OUTSTANDING_MAGIC_LINKS, create_magic_link
from core.mail_outbox import MailOutbox
from core.nodes import Node, NodeClaim, NodeClaimChallenge
from core.rate_counters import RateCounter
from core.sessions import COOKIE_NAME
from core.users import MagicLink, RetiredAddress, User, async_session_maker, engine
from main import app
from services import claim_links, claim_resend, mail, mail_outbox, mail_templates, node_auth, node_rate_limits
from services.node_claim_store import drop_challenge, mark_undeliverable, put_challenge, set_claim_address
from services.node_claiming import complete_claim, decline_claim, release_node
from services.node_rate_limits import CLAIM_LIMITS
from tests.claim_mail_helpers import HOST_APP, Transport, configure_mail, token_in
from tests.owner_helpers import real_sessions, sign_in, user_id_of

ADA = "ada@example.com"
NEW = "ada.new@example.com"
GRACE = "grace@example.com"
# Invented ids, from tests/test_no_real_identities.py's allow-list.
NODE = "ret1a2b3c4d"
NODES = ("ret1a2b3c4d", "ret2b3c4d5e", "ret0a1b2c3d", "ret5e6f7a8b", "ret9c0d1e2f", "ret3a4b5c6d")
CANNOT_RENEW = {"detail": "That link cannot be renewed"}
NOT_FOUND = {"detail": "Claim not found"}
SENT = {"status": "sent"}
LIMITED = {"detail": "Too many claim mails have been asked for; try again later"}
UNAVAILABLE = {"detail": "Claiming by email is unavailable"}

# Everything asking for a claim mail can write, table by table.
_WRITTEN = ("node_claims", "node_claim_challenges", "magic_links", "mail_outbox", "rate_counters")


async def _empty_queue_and_counters() -> None:
    """conftest.py empties neither, and the limits count in rate_counters."""
    async with async_session_maker() as session:
        await session.execute(delete(MailOutbox))
        await session.execute(delete(RateCounter))
        await session.commit()


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch, tmp_path):
    configure_mail(monkeypatch, tmp_path)
    real_sessions(monkeypatch)
    asyncio.run(_empty_queue_and_counters())
    yield
    asyncio.run(_empty_queue_and_counters())


@pytest.fixture
def client():
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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read(statement) -> list:
    async def _go():
        async with async_session_maker() as session:
            return list((await session.execute(statement)).scalars())

    return asyncio.run(_go())


def _write(change) -> None:
    """Run `change(session)` in a transaction of its own, as another request would."""

    async def _go() -> None:
        async with async_session_maker() as session:
            async with session.begin():
                await change(session)

    asyncio.run(_go())


async def _add(session, row) -> None:
    session.add(row)


def _edit_claim(node_id: str, **values) -> None:
    _write(lambda session: session.execute(update(NodeClaim).where(NodeClaim.node_id == node_id).values(**values)))


def _expire(node_id: str) -> None:
    """Let the node's outstanding link run out, as fifteen minutes would."""
    past = time.time() - 1
    (challenge,) = _read(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id))

    async def _both(session) -> None:
        await session.execute(
            update(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id).values(expires_at=past)
        )
        await session.execute(update(MagicLink).where(MagicLink.token_hash == challenge.handle).values(expires_at=past))

    _write(_both)


def _claim(node_id: str) -> NodeClaim | None:
    (row,) = _read(select(NodeClaim).where(NodeClaim.node_id == node_id)) or [None]
    return row


def _challenge(node_id: str) -> NodeClaimChallenge | None:
    (row,) = _read(select(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id)) or [None]
    return row


def _queue() -> list[MailOutbox]:
    return _read(select(MailOutbox).order_by(MailOutbox.id))


def _written() -> dict[str, list[tuple]]:
    async def _go():
        async with async_session_maker() as session:
            return {
                table: [tuple(row) for row in (await session.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all()]
                for table in _WRITTEN
            }

    return asyncio.run(_go())


def _without_counters(written: dict[str, list[tuple]]) -> dict[str, list[tuple]]:
    return {table: rows for table, rows in written.items() if table != "rate_counters"}


def _node_counts(node_id: str) -> dict[int, int]:
    """The node's claim counters, by window."""
    key = node_rate_limits._counter_key("claim", node_id)
    return {row.window_s: row.count for row in _read(select(RateCounter).where(RateCounter.key == key))}


def _register(node_id: str) -> tuple[str, str]:
    """A registered node and its bearer token. Returns (bearer, node_ref)."""

    async def _go() -> tuple[str, str]:
        async with async_session_maker() as session:
            async with session.begin():
                node = Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), board_model="raspberrypi5-4gb")
                session.add(node)
                await session.flush()
                bearer = await node_auth.mint_token(session, node_id)
            return bearer, node.node_ref

    return asyncio.run(_go())


def _sent(*deliveries: mail.Delivery) -> list[mail.Message]:
    """Run the outbox's worker until nothing is due, and return what it sent."""
    transport = Transport(*deliveries)

    async def _drain() -> None:
        while await mail_outbox.run_once(owner="test-worker", transport=transport):
            pass

    asyncio.run(_drain())
    return transport.sent


def _auth(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


def _nominate(client: TestClient, bearer: str, email: str):
    return client.put("/v1/nodes/claim", json={"email": email}, headers=_auth(bearer))


def _node_resend(client: TestClient, bearer: str):
    return client.post("/v1/nodes/claim/resend", headers=_auth(bearer))


def _node_reads(client: TestClient, bearer: str) -> dict:
    return client.get("/v1/nodes/claim", headers=_auth(bearer)).json()


def _renew(client: TestClient, token: str):
    return client.post("/api/auth/claim/resend", json={"token": token})


def _signed_in(client: TestClient, email: str) -> dict[str, str]:
    # By header: the cookie is Secure and the test client speaks plain HTTP.
    return {"Cookie": f"{COOKIE_NAME}={sign_in(client, email)}"}


def _mine(client: TestClient, cookie: dict[str, str]):
    return client.get("/api/auth/me/claims", headers=cookie)


def _ask(client: TestClient, cookie: dict[str, str], node_ref: str):
    return client.post(f"/api/auth/me/claims/{node_ref}/resend", headers=cookie)


def _claimed(client: TestClient, node_id: str, email: str) -> tuple[str, str, str]:
    """A node that has offered `email`, with its mail gone. Returns (bearer, node_ref, the mailed token)."""
    bearer, node_ref = _register(node_id)
    assert _nominate(client, bearer, email).json()["state"] == "pending"
    (message,) = _sent()
    return bearer, node_ref, token_in(message)


def _claim_mail(node_ref: str, token: str, to: str, former: str | None = None) -> mail.Message:
    rendered = mail_templates.claim(node_ref, f"https://{HOST_APP}/auth/claim/{token}", 15, former=former)
    return mail.Message(to=to, subject=rendered.subject, text=rendered.text, html=rendered.html)


# ── The claim page: renewing a link ──────────────────────────────────────────


def test_a_link_asks_for_a_fresh_mail_to_the_address_the_node_gave(client):
    bearer, node_ref, first = _claimed(client, NODE, ADA)

    renewed = _renew(client, first)

    assert (renewed.status_code, renewed.json()) == (202, SENT)
    (message,) = _sent()
    fresh = token_in(message)
    assert message == _claim_mail(node_ref, fresh, ADA)
    assert fresh != first
    challenge = _challenge(NODE)
    assert challenge is not None
    assert (challenge.email, challenge.handle) == (ADA, claim_links.handle_for(fresh))
    # Only the newest link works, as after the node's own resend.
    assert asyncio.run(claim_links.peek(first)) is None
    assert asyncio.run(claim_links.peek(fresh)) is not None
    assert _node_reads(client, bearer) == {"state": "pending", "email": ADA, "undeliverable": False}


def test_an_expired_link_is_what_the_claim_page_renews(client):
    bearer, _ref, first = _claimed(client, NODE, ADA)
    _expire(NODE)
    assert client.get(f"/api/auth/claim/{first}").status_code == 404
    assert _node_reads(client, bearer)["state"] == "unclaimed"

    renewed = _renew(client, first)

    assert (renewed.status_code, renewed.json()) == (202, SENT)
    (message,) = _sent()
    assert message.to == ADA
    assert client.get(f"/api/auth/claim/{token_in(message)}").status_code == 200
    assert _node_reads(client, bearer)["state"] == "pending"


def _used(client, _bearer, token) -> None:
    assert client.post("/api/auth/claim/consume", json={"token": token}).status_code == 200


def _declined(client, _bearer, token) -> None:
    assert client.post("/api/auth/claim/decline", json={"token": token}).status_code == 200


def _resent_by_the_node(client, bearer, _token) -> None:
    assert _node_resend(client, bearer).json()["state"] == "pending"


def _renewed_already(client, _bearer, token) -> None:
    assert _renew(client, token).status_code == 202


def _corrected(client, bearer, _token) -> None:
    assert _nominate(client, bearer, GRACE).json() == {"state": "pending", "email": GRACE, "undeliverable": False}


def _released(client, _bearer, token) -> None:
    _used(client, _bearer, token)
    assert asyncio.run(release_node(NODE, user_id_of(ADA))) is True


def _bounced(_client, _bearer, _token) -> None:
    _write(lambda session: mark_undeliverable(session, NODE, ADA))


_NOT_NEWEST = {
    "used": _used,
    "declined": _declined,
    "replaced by the node's resend": _resent_by_the_node,
    "replaced by a renewal from the page": _renewed_already,
    "replaced by a corrected address": _corrected,
    "released": _released,
    "bounced": _bounced,
}


@pytest.mark.parametrize("cause", _NOT_NEWEST)
def test_a_link_that_is_not_its_claims_newest_cannot_be_renewed(client, cause):
    """However it came to be dead, one answer, and nothing written: no mail, no
    link, and none of the node's allowance spent."""
    bearer, _ref, token = _claimed(client, NODE, ADA)
    _NOT_NEWEST[cause](client, bearer, token)
    _sent()
    before = _written()

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (404, CANNOT_RENEW)
    assert _written() == before


@pytest.mark.parametrize("token", ["not-a-token", ""])
def test_a_token_that_never_was_cannot_be_renewed(client, token):
    _claimed(client, NODE, ADA)
    before = _written()

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (404, CANNOT_RENEW)
    assert _written() == before


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"undeliverable": True}, id="the address bounced"),
        pytest.param({"email": None}, id="no address on file"),
        pytest.param({"verified": True}, id="the address confirmed"),
    ],
)
def test_a_link_to_a_claim_that_is_not_open_cannot_be_renewed(client, state):
    """Each rule alone: the link is still the claim's newest, and the claim is
    not one a mail goes to."""
    _bearer, _ref, token = _claimed(client, NODE, ADA)
    _edit_claim(NODE, **state)
    before = _written()

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (404, CANNOT_RENEW)
    assert _written() == before


def test_a_link_for_a_node_that_has_an_owner_is_409(client):
    _bearer, _ref, token = _claimed(client, NODE, ADA)
    _edit_claim(NODE, user_id="11111111-1111-1111-1111-111111111111")
    before = _written()

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (409, {"detail": "That node already belongs to someone else"})
    assert _written() == before


def test_a_renewal_past_the_allowance_is_429(client):
    bearer, _ref, _token = _claimed(client, NODE, ADA)
    hourly, _window = CLAIM_LIMITS[0]
    for _ in range(hourly - 1):
        assert _node_resend(client, bearer).json()["state"] == "pending"
    (*_, latest) = _sent()
    before = _without_counters(_written())

    refused = _renew(client, token_in(latest))

    assert (refused.status_code, refused.json()) == (429, LIMITED)
    assert 3000 < int(refused.headers["Retry-After"]) <= 3600
    assert _without_counters(_written()) == before
    assert asyncio.run(claim_links.peek(token_in(latest))) is not None


def _fill_the_link_store(address: str) -> None:
    """Outstanding claim links for `address` up to the store's cap, from another node."""
    for _ in range(_MAX_OUTSTANDING_MAGIC_LINKS):
        asyncio.run(create_magic_link(address, intent=claim_links.INTENT_CLAIM, node_id="retdeadbeef"))


def test_a_renewal_with_the_address_at_its_link_cap_is_429_and_changes_nothing(client):
    """Told to wait as a limit tells them, never that a mail went: the link
    still live stays live, and the mail already sent is the claim's."""
    bearer, _ref, token = _claimed(client, NODE, ADA)
    _fill_the_link_store(ADA)
    before = _without_counters(_written())

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (429, LIMITED)
    assert refused.headers["Retry-After"] == str(claim_links.CHALLENGE_EXPIRY_S)
    assert _without_counters(_written()) == before
    assert asyncio.run(claim_links.peek(token)) is not None
    assert _node_reads(client, bearer)["state"] == "pending"


def test_the_nodes_own_resend_at_the_link_cap_still_answers_where_its_claim_stands(client):
    """The node's answer is as it always was: unclaimed, with the link that
    could not be replaced dropped, rather than the person's 429."""
    bearer, _ref, token = _claimed(client, NODE, ADA)
    _fill_the_link_store(ADA)

    answered = _node_resend(client, bearer)

    assert (answered.status_code, answered.json()) == (
        200,
        {"state": "unclaimed", "email": ADA, "undeliverable": False},
    )
    assert _challenge(NODE) is None
    assert asyncio.run(claim_links.peek(token)) is None


def _no_transport(monkeypatch, _tmp_path) -> None:
    monkeypatch.delenv("MAIL_TRANSPORT")


def _no_host(monkeypatch, _tmp_path) -> None:
    monkeypatch.delenv("HOST_APP")


def _unusable_key(monkeypatch, tmp_path) -> None:
    (tmp_path / mail_outbox.KEY_FILE_NAME).write_bytes(b"not a fernet key")
    monkeypatch.setattr(mail_outbox, "_fernets", {})


_NO_MAIL = {"no transport": _no_transport, "no HOST_APP": _no_host, "an unusable outbox key": _unusable_key}


@pytest.mark.parametrize("cause", _NO_MAIL)
def test_a_renewal_no_mail_can_leave_for_is_503_and_writes_nothing(client, monkeypatch, tmp_path, cause):
    _bearer, _ref, token = _claimed(client, NODE, ADA)
    before = _written()
    _NO_MAIL[cause](monkeypatch, tmp_path)

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (503, UNAVAILABLE)
    assert _written() == before
    assert asyncio.run(claim_links.peek(token)) is not None


def test_a_link_to_a_retired_address_renews_to_the_accounts_current_address(client, ada):
    """The node was given an address its account has since moved away from. The
    renewal goes where the first mail went, never to the retired address."""
    _bearer, node_ref, token = _claimed(client, NODE, ADA)

    renewed = _renew(client, token)

    assert (renewed.status_code, renewed.json()) == (202, SENT)
    (message,) = _sent()
    assert message == _claim_mail(node_ref, token_in(message), NEW, former=ADA)
    assert [row.recipient for row in _queue()] == [NEW, NEW]
    assert _claim(NODE).email == ADA


# ── The claim moving on while the mail is minted ─────────────────────────────


def test_an_address_corrected_while_the_renewal_was_minted_is_not_put_back(client, monkeypatch):
    """The node offers another address after the page's request was judged and
    before its write. The page is not the authority on the node's address, so
    nothing is written, the correction and its link stand, and the link minted
    for the page dies unsent."""
    _bearer, _ref, token = _claimed(client, NODE, ADA)
    issue = claim_links.issue
    minted: list[claim_links.Challenge] = []
    corrections: list[claim_links.Challenge] = []

    async def corrected_while_minting(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        assert challenge is not None
        minted.append(challenge)
        correction = await issue(email=GRACE, node_id=NODE, now=now)
        assert correction is not None
        corrections.append(correction)
        async with async_session_maker() as session:
            async with session.begin():
                await put_challenge(session, NODE, GRACE, correction.handle, correction.expires_at)
                await set_claim_address(session, NODE, GRACE)
        return challenge

    monkeypatch.setattr(claim_links, "issue", corrected_while_minting)
    queued = len(_queue())

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (404, CANNOT_RENEW)
    claim = _claim(NODE)
    assert claim is not None and claim.email == GRACE
    challenge = _challenge(NODE)
    assert challenge is not None and challenge.handle == corrections[0].handle
    assert asyncio.run(claim_links.peek(minted[0].token)) is None
    assert asyncio.run(claim_links.peek(corrections[0].token)) is not None
    assert len(_queue()) == queued


def test_a_link_the_node_replaced_while_the_renewal_was_minted_renews_nothing(client, monkeypatch):
    """The node's own resend lands in between, to the same address. The page's
    link is no longer its claim's newest, so the page is refused as it would
    have been a moment later, and the node's mail still goes."""
    _bearer, node_ref, token = _claimed(client, NODE, ADA)
    issue = claim_links.issue
    minted: list[claim_links.Challenge] = []
    resent: list[claim_links.Challenge] = []

    async def resent_while_minting(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        assert challenge is not None
        minted.append(challenge)
        fresh = await issue(email=ADA, node_id=NODE, now=now)
        assert fresh is not None
        resent.append(fresh)
        async with async_session_maker() as session:
            async with session.begin():
                displaced = await put_challenge(session, NODE, ADA, fresh.handle, fresh.expires_at)
                assert displaced is not None
                await claim_links.invalidate_in(session, displaced)
                await claim_links.queue(session, fresh, email=ADA, node_id=NODE, node_ref=node_ref)
        return challenge

    monkeypatch.setattr(claim_links, "issue", resent_while_minting)

    refused = _renew(client, token)

    assert (refused.status_code, refused.json()) == (404, CANNOT_RENEW)
    challenge = _challenge(NODE)
    assert challenge is not None and challenge.handle == resent[0].handle
    assert asyncio.run(claim_links.peek(minted[0].token)) is None
    assert [(row.handle, row.status) for row in _queue()][-1] == (resent[0].handle, "queued")
    (message,) = _sent()
    assert token_in(message) == resent[0].token


def test_the_claim_is_judged_again_under_the_write_lock(client, monkeypatch):
    """The second look at the claim holds SQLite's write lock: a write from
    another connection in the middle of it is refused rather than landing
    between the look and the send."""
    _bearer, _ref, token = _claimed(client, NODE, ADA)
    read_claim = claim_resend.read_claim
    looks: list[str] = []

    async def read_while_another_writes(session, node_id):
        if looks:
            other = sqlite3.connect(engine.url.database, timeout=0, isolation_level=None)
            try:
                other.execute("UPDATE node_claims SET updated_at = updated_at WHERE node_id = ?", (node_id,))
                looks.append("written")
            except sqlite3.OperationalError as refused:
                looks.append(str(refused))
            finally:
                other.close()
        else:
            looks.append("first look")
        return await read_claim(session, node_id)

    monkeypatch.setattr(claim_resend, "read_claim", read_while_another_writes)

    renewed = _renew(client, token)

    assert renewed.status_code == 202
    assert looks == ["first look", "database is locked"]


def test_a_node_bound_while_the_dashboards_mail_was_minted_is_409_and_nothing_goes(client, monkeypatch, ada):
    """The person clicked the link already in their inbox while the dashboard's
    request was on its way. The binding stands and no mail follows it."""
    _bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, NEW)
    issue = claim_links.issue

    async def bound_while_minting(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        await complete_claim(token)
        return challenge

    monkeypatch.setattr(claim_links, "issue", bound_while_minting)
    queued = len(_queue())

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (409, {"detail": "That node already has an owner"})
    claim = _claim(NODE)
    assert claim is not None and (claim.user_id, claim.verified) == (str(ada.id), True)
    assert _challenge(NODE) is None
    assert len(_queue()) == queued


# ── The dashboard: the claims waiting on the account ─────────────────────────


def test_the_claim_routes_need_a_signed_in_session(client):
    for response in (_mine(client, {}), _ask(client, {}, "nde000000000000")):
        assert (response.status_code, response.json()) == (401, {"detail": "Not authenticated"})


def test_the_anonymous_admin_has_no_claims(client, monkeypatch):
    """With the bypass on, as a laptop runs, a request with no cookie is the
    anonymous admin everywhere get_current_user decides. It has no address,
    so these answer it as signed out."""
    for name, module in list(sys.modules.items()):
        if name.startswith(("core.", "routes.", "services.")) and "AUTH_BYPASS" in vars(module):
            monkeypatch.setattr(module, "AUTH_BYPASS", True)
    assert client.get("/api/auth/me").json()["auth_enabled"] is False

    for response in (_mine(client, {}), _ask(client, {}, "nde000000000000")):
        assert (response.status_code, response.json()) == (401, {"detail": "Not authenticated"})


def test_the_open_claims_addressed_to_the_account_are_listed(client, ada):
    """Open claims on the address the account has and on the one it retired,
    and nothing else: not another address's, not an owned node's, not a
    confirmed or a bounced address's."""
    refs = {}
    for node_id, email in zip(NODES, (ADA, NEW, GRACE, ADA, ADA, NEW), strict=True):
        _bearer, refs[node_id], _token = _claimed(client, node_id, email)
    live, expired, elsewhere, owned, confirmed, bounced = NODES
    _expire(expired)
    _edit_claim(owned, user_id=str(ada.id), verified=True)
    _edit_claim(confirmed, verified=True)
    _write(lambda session: mark_undeliverable(session, bounced, NEW))
    live_challenge = _challenge(live)
    assert live_challenge is not None
    cookie = _signed_in(client, NEW)

    listed = _mine(client, cookie)

    assert listed.status_code == 200
    expected = [
        {"node_ref": refs[live], "email": ADA, "pending": True, "expires_at": live_challenge.expires_at},
        {"node_ref": refs[expired], "email": NEW, "pending": False, "expires_at": None},
    ]
    assert listed.json() == sorted(expected, key=lambda claim: claim["node_ref"])
    # The node's private identifier is nobody's business here.
    assert not any(node_id in listed.text for node_id in NODES)
    assert refs[elsewhere] not in listed.text


def test_an_account_with_no_open_claims_lists_none(client):
    _claimed(client, NODE, ADA)

    listed = _mine(client, _signed_in(client, GRACE))

    assert (listed.status_code, listed.json()) == (200, [])


def test_the_account_asks_for_a_claims_mail_again(client, ada):
    """A claim on the retired address is mailed to the address the account has
    now, naming the one the node was given, as every mail for it is. One on
    the current address goes to it plainly. A link that ran out is renewed as
    one that is live is."""
    _bearer, retired_ref, retired_first = _claimed(client, NODES[0], ADA)
    _bearer, current_ref, _token = _claimed(client, NODES[1], NEW)
    _expire(NODES[1])
    cookie = _signed_in(client, NEW)

    asked = _ask(client, cookie, retired_ref)
    assert (asked.status_code, asked.json()) == (202, SENT)
    (to_retired,) = _sent()
    asked = _ask(client, cookie, current_ref)
    assert (asked.status_code, asked.json()) == (202, SENT)
    (to_current,) = _sent()

    assert to_retired == _claim_mail(retired_ref, token_in(to_retired), NEW, former=ADA)
    assert to_current == _claim_mail(current_ref, token_in(to_current), NEW)
    assert asyncio.run(claim_links.peek(retired_first)) is None
    assert [claim["pending"] for claim in _mine(client, cookie).json()] == [True, True]
    assert {row.recipient for row in _queue()} == {NEW}


def test_another_accounts_claim_reads_as_a_node_that_does_not_exist(client):
    """Somebody else's claim, a node with no claim at all, and a node_ref that
    names nothing are one answer, with nothing written."""
    _bearer, node_ref, _token = _claimed(client, NODE, ADA)
    _bearer, unclaimed_ref = _register(NODES[1])
    cookie = _signed_in(client, GRACE)
    before = _written()

    answers = [_ask(client, cookie, ref) for ref in (node_ref, unclaimed_ref, "nde000000000000")]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(404, NOT_FOUND)] * 3
    assert _written() == before


def test_a_retired_address_another_account_has_since_is_not_the_retiring_accounts_claim(client, ada):
    """After a rollback, ADA is retired to the account at NEW and is also the
    address of a second account. It is that account's: a claim on it is not
    listed for the first, and the first cannot have its mail asked for again,
    while the second sees it."""
    _bearer, node_ref, _token = _claimed(client, NODE, ADA)
    holder = User(email=ADA, name="holder", is_verified=True)
    _write(lambda session: _add(session, holder))
    retiring = _signed_in(client, NEW)
    holding = _signed_in(client, ADA)
    before = _written()

    listed = _mine(client, retiring)
    asked = _ask(client, retiring, node_ref)

    assert (listed.status_code, listed.json()) == (200, [])
    assert (asked.status_code, asked.json()) == (404, NOT_FOUND)
    assert _written() == before
    assert [claim["node_ref"] for claim in _mine(client, holding).json()] == [node_ref]


def test_another_accounts_owned_node_is_404_not_409(client):
    """An owned node whose address is not the caller's says nothing about who owns it."""
    _bearer, node_ref, token = _claimed(client, NODE, ADA)
    _used(client, None, token)
    cookie = _signed_in(client, GRACE)

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"undeliverable": True}, id="the address bounced"),
        pytest.param({"verified": True}, id="the address confirmed"),
    ],
)
def test_a_claim_that_is_not_open_is_404(client, state):
    _bearer, node_ref, _token = _claimed(client, NODE, ADA)
    _edit_claim(NODE, **state)
    cookie = _signed_in(client, ADA)
    before = _written()

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert _written() == before


def test_a_claim_on_the_accounts_address_whose_node_has_an_owner_is_409(client):
    _bearer, node_ref, token = _claimed(client, NODE, ADA)
    _used(client, None, token)
    cookie = _signed_in(client, ADA)
    before = _written()

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (409, {"detail": "That node already has an owner"})
    assert _written() == before


def test_the_dashboards_ask_past_the_allowance_is_429(client):
    bearer, node_ref, _token = _claimed(client, NODE, ADA)
    hourly, _window = CLAIM_LIMITS[0]
    for _ in range(hourly - 1):
        _node_resend(client, bearer)
    _sent()
    cookie = _signed_in(client, ADA)
    before = _without_counters(_written())

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (429, LIMITED)
    assert 3000 < int(refused.headers["Retry-After"]) <= 3600
    assert _without_counters(_written()) == before


def test_the_dashboards_ask_with_the_address_at_its_link_cap_is_429_and_changes_nothing(client):
    _bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)
    _fill_the_link_store(ADA)
    before = _without_counters(_written())

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (429, LIMITED)
    assert refused.headers["Retry-After"] == str(claim_links.CHALLENGE_EXPIRY_S)
    assert _without_counters(_written()) == before
    assert asyncio.run(claim_links.peek(token)) is not None


@pytest.mark.parametrize("cause", _NO_MAIL)
def test_the_dashboards_ask_no_mail_can_leave_for_is_503_and_writes_nothing(client, monkeypatch, tmp_path, cause):
    _bearer, node_ref, _token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)
    before = _written()
    _NO_MAIL[cause](monkeypatch, tmp_path)

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (503, UNAVAILABLE)
    assert _written() == before


# ── A decline is final from the address's side ──────────────────────────────
#
# Whoever was mailed a stranger's node by mistake and declined it is not
# offered it again: not on the dashboard, and not by a mail they ask for, which
# would carry a link binding the node to them with nobody at the node acting.
# Only the node asking again offers it again: its resend, or another address.


@pytest.mark.parametrize("retired", [False, True], ids=["the accounts address", "an address it retired"])
def test_a_declined_claim_is_not_listed_for_the_address_that_declined_it(client, request, retired):
    signed_in_as = NEW if retired else ADA
    if retired:
        request.getfixturevalue("ada")
    bearer, _ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)
    cookie = _signed_in(client, signed_in_as)

    listed = _mine(client, cookie)

    assert (listed.status_code, listed.json()) == (200, [])
    # The node is told what it was before: unclaimed, its address on file.
    assert _node_reads(client, bearer) == {"state": "unclaimed", "email": ADA, "undeliverable": False}


def test_the_dashboard_cannot_mail_a_declined_claim_again(client):
    """One answer with a claim that is not there, and nothing written: no
    mail, no link, none of the node's allowance spent."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)
    cookie = _signed_in(client, ADA)
    before = _written()

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert _written() == before
    assert _sent() == []


def test_a_claim_whose_link_was_withdrawn_is_still_listed(client):
    """The state an address change leaves on the claims mailed to the old
    mailbox (services/account_email.py, _withdraw_claims): the link dead and
    the challenge gone, as after a decline, but nobody refused anything. It
    stays the account's to ask for again."""
    _bearer, node_ref, token = _claimed(client, NODE, ADA)

    async def _withdrawn(session) -> None:
        await claim_links.invalidate_in(session, claim_links.handle_for(token))
        await drop_challenge(session, NODE)

    _write(_withdrawn)
    cookie = _signed_in(client, ADA)

    listed = _mine(client, cookie)

    assert listed.json() == [{"node_ref": node_ref, "email": ADA, "pending": False, "expires_at": None}]
    assert _ask(client, cookie, node_ref).status_code == 202


def test_the_nodes_resend_offers_a_declined_claim_again(client):
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)
    cookie = _signed_in(client, ADA)

    assert _node_resend(client, bearer).json() == {"state": "pending", "email": ADA, "undeliverable": False}

    (resent,) = _sent()
    listed = _mine(client, cookie).json()
    assert [(claim["node_ref"], claim["pending"]) for claim in listed] == [(node_ref, True)]
    assert _ask(client, cookie, node_ref).status_code == 202
    (asked,) = _sent()
    assert token_in(asked) != token_in(resent)


def test_the_node_offering_another_address_clears_the_decline_and_coming_back_does_too(client):
    """The decline belonged to the address the node replaced. Offered again
    after that, the address is a fresh nomination, and its own mail went."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)

    assert _nominate(client, bearer, GRACE).json()["state"] == "pending"
    assert [message.to for message in _sent()] == [GRACE]
    assert [claim["node_ref"] for claim in _mine(client, _signed_in(client, GRACE)).json()] == [node_ref]
    assert _nominate(client, bearer, ADA).json() == {"state": "pending", "email": ADA, "undeliverable": False}

    assert [message.to for message in _sent()] == [ADA]
    assert [claim["node_ref"] for claim in _mine(client, _signed_in(client, ADA)).json()] == [node_ref]


def test_the_node_offering_the_declined_address_again_keeps_the_decline(client):
    """A node sends its address on every configuration sync. Saying it again
    neither mails nor changes anything, so it does not undo a decline either."""
    bearer, _ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)

    again = _nominate(client, bearer, ADA)

    assert (again.status_code, again.json()) == (200, {"state": "unclaimed", "email": ADA, "undeliverable": False})
    assert _mine(client, _signed_in(client, ADA)).json() == []
    assert _sent() == []


def test_the_nodes_resend_clears_a_decline_even_when_no_mail_can_follow(client):
    """At the address's cap of links the node's resend sends nothing, and its
    asking still stands: the claim is offered again, to ask for once the cap
    has room."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    _declined(client, bearer, token)
    _fill_the_link_store(ADA)

    assert _node_resend(client, bearer).json() == {"state": "unclaimed", "email": ADA, "undeliverable": False}

    listed = _mine(client, _signed_in(client, ADA)).json()
    assert listed == [{"node_ref": node_ref, "email": ADA, "pending": False, "expires_at": None}]


def test_a_decline_landing_while_the_dashboards_mail_was_minted_stops_it(client, monkeypatch):
    """The decline spends its link, and with it every other link out for that
    address and node (core/auth.py, redeem_magic_link_in), the one just minted
    for the dashboard included. So no mail follows the refusal, and the
    dashboard is not told one went."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)
    issue = claim_links.issue
    minted: list[claim_links.Challenge] = []

    async def declined_while_minting(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        assert challenge is not None
        minted.append(challenge)
        assert await decline_claim(token) is True
        return challenge

    monkeypatch.setattr(claim_links, "issue", declined_while_minting)
    queued = len(_queue())

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert asyncio.run(claim_links.peek(minted[0].token)) is None
    assert _challenge(NODE) is None
    assert len(_queue()) == queued
    assert _node_reads(client, bearer)["state"] == "unclaimed"


def test_a_decline_recorded_while_the_dashboards_mail_was_minted_is_judged_under_the_lock(client, monkeypatch):
    """The claim is judged again once the write lock is held, the decline
    included, so the refusal holds whatever else the decline did or did not
    do. The link minted for the dashboard dies unsent, and the claim and the
    link already out stand as they were."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)
    issue = claim_links.issue
    minted: list[claim_links.Challenge] = []

    async def declined_while_minting(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        assert challenge is not None
        minted.append(challenge)
        async with async_session_maker() as session:
            async with session.begin():
                await session.execute(update(NodeClaim).where(NodeClaim.node_id == NODE).values(declined=True))
        return challenge

    monkeypatch.setattr(claim_links, "issue", declined_while_minting)
    queued = len(_queue())

    refused = _ask(client, cookie, node_ref)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert asyncio.run(claim_links.peek(minted[0].token)) is None
    assert asyncio.run(claim_links.peek(token)) is not None
    challenge = _challenge(NODE)
    assert challenge is not None and challenge.handle == claim_links.handle_for(token)
    assert len(_queue()) == queued
    assert _node_reads(client, bearer)["state"] == "pending"


# ── A link withdrawn between its minting and its mail ────────────────────────
#
# The link is minted on a connection of its own before the send's transaction.
# An address change confirmed (services/account_email.py), or an account
# deleted (core/sessions.py, remove_account), in between withdraws the claim
# links on its address, this one included. Recorded, it would have the node
# read `pending` on a link nobody can open, and its mail would carry it.


def _withdrawn_once_minted(monkeypatch) -> list[claim_links.Challenge]:
    """Kill each link as soon as it is minted, as a withdrawal committing then
    would, and keep what was minted."""
    issue = claim_links.issue
    minted: list[claim_links.Challenge] = []

    async def withdrawn(*, email, node_id, now):
        challenge = await issue(email=email, node_id=node_id, now=now)
        assert challenge is not None
        minted.append(challenge)
        await claim_links.invalidate(challenge.handle)
        return challenge

    monkeypatch.setattr(claim_links, "issue", withdrawn)
    return minted


def test_a_nomination_whose_link_was_withdrawn_reads_unclaimed_and_mails_nothing(client, monkeypatch):
    """The node's address is recorded, as it asked, and it is told where that
    leaves the claim: unclaimed, with the address on file, so whoever is at it
    asks again, and that link works."""
    bearer, _ref = _register(NODE)
    issue = claim_links.issue
    minted = _withdrawn_once_minted(monkeypatch)

    answered = _nominate(client, bearer, ADA)

    assert (answered.status_code, answered.json()) == (
        200,
        {"state": "unclaimed", "email": ADA, "undeliverable": False},
    )
    assert len(minted) == 1
    assert _challenge(NODE) is None
    claim = _claim(NODE)
    assert claim is not None and claim.email == ADA
    assert _queue() == []
    assert _node_reads(client, bearer) == {"state": "unclaimed", "email": ADA, "undeliverable": False}

    monkeypatch.setattr(claim_links, "issue", issue)
    assert _node_resend(client, bearer).json()["state"] == "pending"
    (message,) = _sent()
    assert asyncio.run(claim_links.peek(token_in(message))) is not None


def test_a_resend_whose_link_was_withdrawn_drops_the_link_it_was_to_replace(client, monkeypatch):
    """As at the address's cap of links: the node asked for a new link, so the
    one it was replacing goes with its challenge, and the node reads unclaimed
    rather than pending on either."""
    bearer, _ref, first = _claimed(client, NODE, ADA)
    _withdrawn_once_minted(monkeypatch)

    answered = _node_resend(client, bearer)

    assert (answered.status_code, answered.json()) == (
        200,
        {"state": "unclaimed", "email": ADA, "undeliverable": False},
    )
    assert _challenge(NODE) is None
    assert asyncio.run(claim_links.peek(first)) is None
    assert [row.status for row in _queue()] == ["sent"]


@pytest.mark.parametrize("asker", ["the claim page", "the dashboard"])
def test_a_persons_ask_whose_link_was_withdrawn_is_not_sent_and_changes_nothing(client, monkeypatch, asker):
    """Never "sent" for a mail that did not go. The claim, its link still live
    and the mail already sent are left as they stand, so asking again works."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)
    before = _without_counters(_written())
    minted = _withdrawn_once_minted(monkeypatch)

    if asker == "the claim page":
        refused, detail = _renew(client, token), CANNOT_RENEW
    else:
        refused, detail = _ask(client, cookie, node_ref), NOT_FOUND

    assert (refused.status_code, refused.json()) == (404, detail)
    assert len(minted) == 1
    assert _without_counters(_written()) == before
    assert asyncio.run(claim_links.peek(token)) is not None
    assert _node_reads(client, bearer)["state"] == "pending"
    assert _sent() == []


# ── One allowance for every asker ────────────────────────────────────────────


def test_the_node_the_link_and_the_account_spend_one_allowance_per_node(client):
    """The node's hourly five, spent from three places, is still five."""
    bearer, node_ref, token = _claimed(client, NODE, ADA)
    cookie = _signed_in(client, ADA)

    assert _node_resend(client, bearer).status_code == 200
    (message,) = _sent()
    assert _renew(client, token_in(message)).status_code == 202
    assert _ask(client, cookie, node_ref).status_code == 202
    assert _node_resend(client, bearer).status_code == 200
    (*_, latest) = _sent()
    hourly, _window = CLAIM_LIMITS[0]
    assert _node_counts(NODE) == {window_s: hourly for _limit, window_s in CLAIM_LIMITS}

    from_the_page = _renew(client, token_in(latest))
    from_the_dashboard = _ask(client, cookie, node_ref)
    from_the_node = _node_resend(client, bearer)

    assert (from_the_page.status_code, from_the_page.json()) == (429, LIMITED)
    assert (from_the_dashboard.status_code, from_the_dashboard.json()) == (429, LIMITED)
    assert (from_the_node.status_code, from_the_node.json()) == (429, {"error": "rate_limited"})
    assert _node_counts(NODE) == {window_s: hourly for _limit, window_s in CLAIM_LIMITS}
    assert len(_queue()) == hourly


def test_one_address_is_held_to_one_allowance_across_nodes_and_askers(client):
    """The address's hourly five, spent by three nodes, the page and the
    dashboard, is still five, whichever of them asks next."""
    claims = [_claimed(client, node_id, ADA) for node_id in NODES[:3]]
    (_, _, first_token), (_, second_ref, _), (third_bearer, third_ref, _) = claims
    cookie = _signed_in(client, ADA)
    assert _renew(client, first_token).status_code == 202
    (renewed,) = _sent()
    assert _ask(client, cookie, second_ref).status_code == 202
    _sent()

    answers = [_node_resend(client, third_bearer), _renew(client, token_in(renewed)), _ask(client, cookie, third_ref)]

    assert [answer.status_code for answer in answers] == [429, 429, 429]
    assert len(_queue()) == CLAIM_LIMITS[0][0]
