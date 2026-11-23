"""What becomes of a claim mail, as the node reads it.

The route queues the mail (tests/test_node_claim_endpoint.py) and the outbox
sends and settles it (tests/test_mail_outbox.py). What is tested here is the
step between them: the handler services/claim_links.py registers for claim
mail, which the worker runs in the transaction that settles the row, and what
it leaves for `GET /v1/nodes/claim` and the heartbeat to say.

Every outcome is driven through the node API and a worker run by hand
(tests/claim_mail_helpers.py), with a fake transport answering as the provider
would. The last section goes through mail.deliver instead, to a mocked
Cloudflare, so that what its send endpoint answers is proved to reach the node.

A mail can only settle after the node has moved on when its settlement was held
up. A request that replaces a mail still queued, or still being sent, cancels
it, and a cancelled mail runs no handler. The tests about a late settlement
fail the first one, as a database locked past its busy timeout would, and let
the retry land after the node's next request.
"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from core.mail_outbox import EXPIRED, FAILED, REJECTED
from core.nodes import NodeClaim, NodeClaimChallenge
from core.rate_counters import RateCounter
from core.users import MagicLink
from services import claim_links, mail, mail_outbox
from services.claim_links import CHALLENGE_EXPIRY_S
from services.mail_outbox import KIND_CLAIM, Settled
from services.node_claim_store import clear_claim
from services.node_rate_limits import token_rate_limiter
from tests.claim_mail_helpers import Outbox, Transport, configure_mail, token_in

ADA = "ada@example.com"
GRACE = "grace@example.com"
OWNER = "11111111-1111-1111-1111-111111111111"

CLAIM = "/v1/nodes/claim"
RESEND = "/v1/nodes/claim/resend"
HEARTBEAT = "/v1/nodes/heartbeat"

PENDING = {"state": "pending", "email": ADA, "undeliverable": False}
BOUNCED = {"state": "unclaimed", "email": ADA, "undeliverable": True}
UNSENT = {"state": "unclaimed", "email": ADA, "undeliverable": False}

BOUNCE = mail.Delivery(mail.UNDELIVERABLE, "permanent bounce")


@pytest.fixture(autouse=True)
async def outbox(monkeypatch, tmp_path, node_session) -> Outbox:
    """Mail configured, one database for the claims, the links and the queue,
    and a fresh allowance, as tests/test_node_claim_endpoint.py has them."""
    configure_mail(monkeypatch, tmp_path)
    await node_session.execute(delete(RateCounter))
    await node_session.commit()
    return Outbox(monkeypatch, node_session)


class _Node:
    """The registered node, asking the API what a node asks it.

    node_client serves every request from the one test session, where
    production opens a session per request. A row an earlier request loaded
    stays in that session for as long as anything still refers to it, and a
    request finding it there would not read what the worker committed since.
    So each request here starts from an empty identity map and no open
    transaction, as a request of its own would, rather than resting on the
    earlier rows having been let go.
    """

    def __init__(self, client, session: AsyncSession, token: str, node_id: str) -> None:
        self.client = client
        self.session = session
        self.headers = {"Authorization": f"Bearer {token}"}
        self.id = node_id

    async def _fresh(self) -> None:
        await self.session.rollback()
        self.session.expire_all()

    async def put(self, email: str) -> dict:
        await self._fresh()
        response = self.client.put(CLAIM, json={"email": email}, headers=self.headers)
        assert response.status_code == 200
        return response.json()

    async def read(self) -> dict:
        await self._fresh()
        response = self.client.get(CLAIM, headers=self.headers)
        assert response.status_code == 200
        return response.json()

    async def resend(self) -> httpx.Response:
        await self._fresh()
        return self.client.post(RESEND, headers=self.headers)

    async def beat(self) -> dict:
        await self._fresh()
        beat = {"state": "streaming", "uptime_s": 9, "boot_id": "k3n8v2qp71ab", "config_version": 1}
        response = self.client.post(HEARTBEAT, json=beat, headers=self.headers)
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def node(registered_node, node_session, node_client) -> _Node:
    token, node_id = registered_node
    return _Node(node_client, node_session, token, node_id)


async def _statuses(outbox: Outbox) -> list[tuple[str, str]]:
    return [(row.recipient, row.status) for row in await outbox.rows()]


async def _claim(outbox: Outbox, node_id: str) -> NodeClaim | None:
    async with outbox.sessions() as session:
        return await session.get(NodeClaim, node_id)


async def _challenge(outbox: Outbox, node_id: str) -> NodeClaimChallenge | None:
    async with outbox.sessions() as session:
        return await session.get(NodeClaimChallenge, node_id)


async def _link_lives(outbox: Outbox, handle: str) -> bool:
    """Whether the link `handle` names can still be clicked. For a link whose
    mail never went, so no test ever saw its token."""
    async with outbox.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(MagicLink).where(MagicLink.token_hash == handle))
    return bool(count)


async def _links_to(outbox: Outbox, email: str) -> int:
    async with outbox.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(MagicLink).where(MagicLink.email == email))
    return count or 0


async def _locked(_session: AsyncSession, _settled: Settled) -> None:
    raise OperationalError("UPDATE node_claims", None, sqlite3.OperationalError("database is locked"))


async def _delivered_but_not_settled(outbox: Outbox, delivery: mail.Delivery) -> mail.Message:
    """Send the one queued mail and have its settlement fail, as it does when
    the database stays locked past its busy timeout.

    What delivery came to is kept and the claim is untouched: the row waits to
    be settled again, and a request the node makes meanwhile neither cancels it
    nor is cancelled by it. Due again at once rather than after a backoff, so
    the retry is ahead of whatever that request queues, as it is when a worker
    comes back to a backlog.
    """
    transport = Transport(delivery)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(mail_outbox._handlers, KIND_CLAIM, _locked)
        patch.setattr(mail_outbox, "BACKOFF_BASE_S", 0.0)
        assert await mail_outbox.run_once(sessions=outbox.sessions, owner="test-worker", transport=transport)
    [row] = await outbox.rows()
    assert (row.status, row.outcome) == ("settling", delivery.outcome)
    [message] = transport.sent
    outbox.mailed.append(message)
    return message


async def _assert_bounced(node: _Node, outbox: Outbox, message: mail.Message) -> None:
    """What a node is left with when its claim mail could not reach the address:
    unclaimed with the flag, no link, and a resend that answers without mailing."""
    assert await node.read() == BOUNCED
    assert await claim_links.peek(token_in(message)) is None
    assert await _challenge(outbox, node.id) is None

    response = await node.resend()

    assert (response.status_code, response.json()) == (200, BOUNCED)
    assert await _statuses(outbox) == [(ADA, "undeliverable")]
    assert await outbox.send() == []
    # The resend minted nothing either: no link to the address is left at all.
    assert await _links_to(outbox, ADA) == 0


# ── A mail that went ─────────────────────────────────────────────────────────


async def test_a_claim_mail_that_went_leaves_the_node_pending_and_its_link_live(node, outbox):
    """Sent changes nothing: the person has the link, and the node waits for the click."""
    await node.put(ADA)

    [message] = await outbox.send()

    assert await _statuses(outbox) == [(ADA, "sent")]
    assert await node.read() == PENDING
    resolved = await claim_links.peek(token_in(message))
    assert resolved is not None
    assert (resolved.email, resolved.node_id) == (ADA, node.id)
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert challenge.handle == claim_links.handle_for(token_in(message))
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.verified, claim.undeliverable) == (ADA, False, False)


# ── A mail that could not reach the address ──────────────────────────────────


async def test_a_bounced_claim_mail_reads_unclaimed_with_the_flag(node, outbox):
    await node.put(ADA)

    [message] = await outbox.send(BOUNCE)

    assert await _statuses(outbox) == [(ADA, "undeliverable")]
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.undeliverable) == (ADA, True)
    await _assert_bounced(node, outbox, message)


async def test_the_heartbeat_carries_what_a_bounced_claim_mail_left(node, outbox):
    """The node that stopped polling hears it here, so the beat must say what the GET says."""
    # The beat's allowance is in memory and shared by every suite's node.
    token_rate_limiter.reset()
    await node.put(ADA)
    await outbox.send(BOUNCE)

    beat = await node.beat()

    carried = {"state": beat["claim_state"], "email": beat["claim_email"], "undeliverable": beat["claim_undeliverable"]}
    assert carried == await node.read() == BOUNCED


async def test_a_resend_queued_while_the_first_mail_was_out_is_cancelled_when_that_mail_bounces(node, outbox):
    """The resend carries a fresh link to the same address, which has just
    proved it takes nothing. Sending it would earn a second bounce, and bounces
    are how a sending domain loses its reputation."""
    await node.put(ADA)
    first = await _delivered_but_not_settled(outbox, BOUNCE)
    # The outbox knows of the bounce and the claim does not yet.
    assert await node.read() == PENDING

    response = await node.resend()

    assert response.json() == PENDING
    [_first_row, resend_row] = await outbox.rows()
    assert (resend_row.status, resend_row.recipient) == ("queued", ADA)

    # The first mail's settlement lands ahead of the resend and cancels it.
    assert await outbox.send() == []

    assert await _statuses(outbox) == [(ADA, "undeliverable"), (ADA, "cancelled")]
    [_first_row, resend_row] = await outbox.rows()
    assert resend_row.body is None
    assert await node.read() == BOUNCED
    assert await claim_links.peek(token_in(first)) is None
    assert resend_row.handle is not None
    assert not await _link_lives(outbox, resend_row.handle)
    assert await _challenge(outbox, node.id) is None


async def _bound_beside_the_address(session: AsyncSession, node_id: str) -> None:
    """An owner, with the address still on file and never confirmed, so the
    owner alone is what keeps the address from being marked."""
    claim = await session.get(NodeClaim, node_id)
    assert claim is not None
    claim.user_id = OWNER


async def _confirmed(session: AsyncSession, node_id: str) -> None:
    """The address confirmed, and no owner, so the confirmation alone is what
    keeps it from being marked."""
    claim = await session.get(NodeClaim, node_id)
    assert claim is not None
    claim.verified = True


_OUTRANKED: dict[str, tuple[Callable[[AsyncSession, str], Awaitable[None]], dict, tuple]] = {
    "owned": (_bound_beside_the_address, {"state": "owned", "email": ADA, "undeliverable": False}, (OWNER, False)),
    "verified": (_confirmed, UNSENT, (None, True)),
}


@pytest.mark.parametrize("standing", _OUTRANKED)
async def test_a_bounce_on_an_owned_or_confirmed_claim_marks_nothing_but_kills_its_link(standing, node, outbox):
    """An owner settles the claim whatever its address does, and an address
    somebody confirmed by clicking has proved itself, so neither is flagged. The
    link still dies, since nobody at the address will ever click it, and with
    it the challenge, so the node does not read `pending` for a dead link."""
    outrank, reads, (user_id, verified) = _OUTRANKED[standing]
    await node.put(ADA)
    async with outbox.sessions() as session:
        await outrank(session, node.id)
        await session.commit()

    [message] = await outbox.send(BOUNCE)

    assert await _statuses(outbox) == [(ADA, "undeliverable")]
    assert await node.read() == reads
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.user_id, claim.verified, claim.undeliverable) == (ADA, user_id, verified, False)
    assert await claim_links.peek(token_in(message)) is None
    assert await _challenge(outbox, node.id) is None


async def test_a_bounce_for_a_claim_released_while_its_mail_was_out_kills_the_link_and_nothing_else(node, outbox):
    await node.put(ADA)
    async with outbox.sessions() as session:
        await clear_claim(session, node.id)
        await session.commit()

    [message] = await outbox.send(BOUNCE)

    assert await _statuses(outbox) == [(ADA, "undeliverable")]
    assert await node.read() == {"state": "unclaimed", "email": None, "undeliverable": False}
    assert await _claim(outbox, node.id) is None
    assert await claim_links.peek(token_in(message)) is None


# ── A mail that never left ───────────────────────────────────────────────────


async def _refused(outbox: Outbox, _patch: pytest.MonkeyPatch) -> None:
    await outbox.send(mail.Delivery(mail.REJECTED, "HTTP 422: 10001 email.sending.error.invalid_request_schema"))


async def _given_up(outbox: Outbox, patch: pytest.MonkeyPatch) -> None:
    # One attempt allowed, so the first answer worth retrying is the last.
    patch.setattr(mail_outbox, "MAX_ATTEMPTS", 1)
    await outbox.send(mail.Delivery(mail.RETRY, "HTTP 503: 10100 email.sending.error.unavailable"))


async def _held_past_its_link(outbox: Outbox, _patch: pytest.MonkeyPatch) -> None:
    """The worker got to it with under a minute of link left, so it never went."""
    [row] = await outbox.rows()
    assert await outbox.send(now=row.expires_at - mail_outbox.EXPIRY_MARGIN_S) == []


async def _throttled_past_its_link(outbox: Outbox, _patch: pytest.MonkeyPatch) -> None:
    """Asked to wait longer than the link has left."""
    await outbox.send(mail.Delivery(mail.RETRY, "HTTP 429: rate limited", retry_after_s=float(CHALLENGE_EXPIRY_S)))


_NEVER_LEFT: dict[str, tuple[Callable[[Outbox, pytest.MonkeyPatch], Awaitable[None]], str]] = {
    "rejected": (_refused, REJECTED),
    "failed": (_given_up, FAILED),
    "expired before it was sent": (_held_past_its_link, EXPIRED),
    "expired while throttled": (_throttled_past_its_link, EXPIRED),
}


@pytest.mark.parametrize("cause", _NEVER_LEFT)
async def test_a_claim_mail_that_never_left_reads_unclaimed_with_the_address_on_file(cause, node, outbox, monkeypatch):
    """Nobody was sent the link, so the node must not read `pending` for it.
    The address stays, unflagged, since nothing was learned about it, and a
    resend asks for a fresh link without retyping it."""
    settle, outcome = _NEVER_LEFT[cause]
    await node.put(ADA)
    [row] = await outbox.rows()
    assert row.handle is not None

    await settle(outbox, monkeypatch)

    assert await _statuses(outbox) == [(ADA, outcome)]
    assert await node.read() == UNSENT
    assert await _challenge(outbox, node.id) is None
    assert not await _link_lives(outbox, row.handle)
    for message in outbox.mailed:
        assert await claim_links.peek(token_in(message)) is None
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.verified, claim.undeliverable) == (ADA, False, False)

    response = await node.resend()
    sent = await outbox.send()

    assert response.json() == PENDING
    assert await _statuses(outbox) == [(ADA, outcome), (ADA, "sent")]
    assert [message.to for message in sent] == [ADA]
    assert await claim_links.peek(token_in(sent[0])) is not None
    assert await node.read() == PENDING


# ── A mail that settles after the node has moved on ──────────────────────────


@pytest.mark.parametrize("outcome", [mail.UNDELIVERABLE, mail.REJECTED])
async def test_an_old_mail_settling_after_a_new_address_leaves_the_new_claim_alone(outcome, node, outbox):
    """A bounce from the old address says nothing about the new one, and the
    new challenge has a mail of its own. Neither outcome may flag the new
    address, drop its challenge or kill its link."""
    await node.put(ADA)
    old = await _delivered_but_not_settled(outbox, mail.Delivery(outcome, "the provider's verdict"))

    assert await node.put(GRACE) == {"state": "pending", "email": GRACE, "undeliverable": False}
    [_old_row, new_row] = await outbox.rows()
    [sent] = await outbox.send()

    assert sent.to == GRACE
    assert await _statuses(outbox) == [(ADA, outcome), (GRACE, "sent")]
    assert await node.read() == {"state": "pending", "email": GRACE, "undeliverable": False}
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert (challenge.email, challenge.handle) == (GRACE, new_row.handle)
    assert claim_links.handle_for(token_in(sent)) == new_row.handle
    assert await claim_links.peek(token_in(sent)) is not None
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.undeliverable) == (GRACE, False)
    # The old link died when the new address displaced it.
    assert await claim_links.peek(token_in(old)) is None


async def test_a_refused_mail_settling_after_a_resend_leaves_the_resend_alone(node, outbox):
    """Unlike a bounce, a refusal is about that one message, not the address:
    the resend that replaced it carries a fresh link and goes."""
    await node.put(ADA)
    old = await _delivered_but_not_settled(outbox, mail.Delivery(mail.REJECTED, "HTTP 422: refused"))

    assert (await node.resend()).json() == PENDING
    [_old_row, resend_row] = await outbox.rows()
    [sent] = await outbox.send()

    assert await _statuses(outbox) == [(ADA, "rejected"), (ADA, "sent")]
    assert await node.read() == PENDING
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert challenge.handle == resend_row.handle == claim_links.handle_for(token_in(sent))
    assert await claim_links.peek(token_in(sent)) is not None
    assert await claim_links.peek(token_in(old)) is None


# ── Cloudflare's answers, through mail.deliver ───────────────────────────────

_SEND_URL = "https://api.cloudflare.com/client/v4/accounts/acct123/email/sending/send"
_SUPPRESSED = "email.sending.error.email.recipient_suppressed"
_NOT_READY = "email.sending.error.email.sending_not_ready"

# What the send endpoint answers, as a status and a body, given the recipients it was asked to mail.
_Answer = Callable[[list[str]], tuple[int, dict]]


@pytest.fixture
def cloudflare(monkeypatch) -> None:
    """Mail configured as a droplet has it. Set before the nomination, so the
    route's check and the send see the same configuration."""
    monkeypatch.setenv("MAIL_TRANSPORT", "cloudflare")
    monkeypatch.setenv("CLOUDFLARE_EMAIL_TOKEN", "token-value-not-in-logs")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")


def _accepted(listed_as: str) -> _Answer:
    """A 200 with `success: true`, listing the recipient under `listed_as`."""

    def answer(to: list[str]) -> tuple[int, dict]:
        result = {
            "message_id": "<m@retina.fm>",
            "delivered": [],
            "queued": [],
            "permanent_bounces": [],
            "suppressed_recipients": [],
        }
        result[listed_as] = list(to)
        return 200, {"success": True, "errors": [], "messages": [], "result": result}

    return answer


def _refused(status: int, message: str, code: int | None = None) -> _Answer:
    """A refusal as the endpoint words it, with the code left null unless given."""

    def answer(_to: list[str]) -> tuple[int, dict]:
        return status, {
            "success": False,
            "errors": [{"code": code, "message": message}],
            "messages": [],
            "result": None,
        }

    return answer


async def _send_through_cloudflare(outbox: Outbox, answer: _Answer, *, now: float | None = None) -> list[dict]:
    """Run the worker through mail.deliver until nothing is due, Cloudflare
    answering `answer`, and return the bodies it was posted."""
    posted: list[dict] = []

    def _post(url: str, *, json: dict, **_kwargs) -> httpx.Response:
        assert url == _SEND_URL
        posted.append(json)
        status, body = answer(json["to"])
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mail.httpx, "post", _post)
        while await mail_outbox.run_once(
            sessions=outbox.sessions, owner="test-worker", now=now, transport=mail.deliver
        ):
            pass
    return posted


def _message_in(body: dict) -> mail.Message:
    [to] = body["to"]
    return mail.Message(to=to, subject=body["subject"], text=body["text"], html=body["html"])


_UNDELIVERABLE_AT_THE_SEND: dict[str, tuple[_Answer, str]] = {
    "a permanent bounce": (_accepted("permanent_bounces"), "permanent bounce"),
    "a suppressed recipient, dropped": (_accepted("suppressed_recipients"), "suppressed recipient"),
    "a suppressed recipient, refused": (_refused(400, _SUPPRESSED), f"HTTP 400: {_SUPPRESSED}"),
}


@pytest.mark.parametrize("verdict", _UNDELIVERABLE_AT_THE_SEND)
async def test_a_bounce_cloudflare_reports_with_the_send_reads_the_same(verdict, node, outbox, cloudflare):
    """Cloudflare says an address cannot receive in three ways, and the node
    must read each as it reads a bounce. A permanent bounce comes back as a
    200 with `success: true`, so the outcome is not in the status code. A
    suppressed recipient comes back the same way when the sending domain drops
    suppressed recipients, and as a 400 when it does not, which is the default."""
    answer, detail = _UNDELIVERABLE_AT_THE_SEND[verdict]
    await node.put(ADA)

    [body] = await _send_through_cloudflare(outbox, answer)

    assert body["to"] == [ADA]
    [row] = await outbox.rows()
    assert (row.status, row.outcome, row.last_error) == ("undeliverable", "undeliverable", detail)
    await _assert_bounced(node, outbox, _message_in(body))


async def test_cloudflare_not_ready_to_send_leaves_the_node_pending_and_the_mail_queued_for_a_retry(
    node, outbox, cloudflare
):
    """A 409 says Email Sending is still being set up for the account. Nothing
    was sent and nothing was learned about the address, so the mail waits for
    its next attempt, and the link it carries stays good for that attempt."""
    await node.put(ADA)

    [body] = await _send_through_cloudflare(outbox, _refused(409, _NOT_READY))

    [row] = await outbox.rows()
    assert (row.status, row.outcome, row.settled_at, row.attempts) == ("queued", None, None, 1)
    assert row.last_error == f"HTTP 409: {_NOT_READY}"
    assert row.body is not None
    assert row.next_attempt_at > row.created_at
    assert await node.read() == PENDING
    token = token_in(_message_in(body))
    assert await claim_links.peek(token) is not None
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert challenge.handle == claim_links.handle_for(token)
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert claim.undeliverable is False

    [retried] = await _send_through_cloudflare(outbox, _accepted("queued"), now=row.next_attempt_at)

    # The same mail, link and all, once sending is ready.
    assert token_in(_message_in(retried)) == token
    assert await _statuses(outbox) == [(ADA, "sent")]
    assert await node.read() == PENDING
    assert await claim_links.peek(token) is not None


# What the send endpoint answers when the token, the account or the sending
# domain is at fault, as (status, code, message). Nothing in the mail earns one.
_ACCOUNT_FAULTS: dict[str, tuple[int, int | None, str]] = {
    "a token that is not valid": (401, 10101, "email.sending.error.authentication.unauthorized"),
    "a token that may not send": (403, 10102, "email.sending.error.authentication.forbidden"),
    "a sending domain not set up": (403, None, "email.sending.error.email.sender_not_configured"),
    "an account that is not found": (404, 10000, "email.sending.error.not_found"),
}
_FORBIDDEN = _refused(403, "email.sending.error.authentication.forbidden", code=10102)


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("fault", _ACCOUNT_FAULTS)
async def test_cloudflare_refusing_the_account_leaves_the_node_pending_and_the_mail_queued_for_a_retry(
    fault, node, outbox, cloudflare, caplog
):
    """A bad token or account setting is somebody's to fix, and the fix can
    land while the link is still good. So the mail waits for its next attempt
    as it does for a 409, the node keeps reading `pending`, and the attempt is
    logged as an error naming Cloudflare's reason and no recipient."""
    status, code, message = _ACCOUNT_FAULTS[fault]
    reason = f"{code} {message}" if code is not None else message
    await node.put(ADA)

    with caplog.at_level(logging.WARNING):
        [body] = await _send_through_cloudflare(outbox, _refused(status, message, code=code))

    [row] = await outbox.rows()
    assert (row.status, row.outcome, row.settled_at, row.attempts) == ("queued", None, None, 1)
    assert row.last_error == f"HTTP {status}: {reason}"
    assert row.body is not None
    assert row.next_attempt_at > row.created_at
    assert await node.read() == PENDING
    token = token_in(_message_in(body))
    assert await claim_links.peek(token) is not None
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert challenge.handle == claim_links.handle_for(token)
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert claim.undeliverable is False
    [error] = _errors(caplog)
    assert f"HTTP {status}: {reason}" in error
    assert ADA not in error
    assert "token-value-not-in-logs" not in caplog.text


_OUTLASTED: dict[str, tuple[int, set[str]]] = {
    # Few attempts allowed, so the cap comes long before the link runs out.
    "the attempt cap": (2, {FAILED}),
    # Attempts enough to outlast any link, so the link's life ends it.
    "the link's life": (1000, {EXPIRED}),
    # As shipped: whichever comes first, given the jitter on each wait.
    "as configured": (mail_outbox.MAX_ATTEMPTS, {FAILED, EXPIRED}),
}


@pytest.mark.parametrize("limit", _OUTLASTED)
async def test_a_refusal_of_the_account_that_outlasts_the_mail_reads_unclaimed_as_any_mail_that_never_left(
    limit, node, outbox, cloudflare, monkeypatch, caplog
):
    """The node reads `pending` while the mail is retried, and once the mail
    gives up, failed or expired, it is cleaned up as any mail that never left:
    the challenge and the link go, the address stays unflagged, and a resend
    after the fix asks for a fresh link without retyping it."""
    attempts, outcomes = _OUTLASTED[limit]
    monkeypatch.setattr(mail_outbox, "MAX_ATTEMPTS", attempts)
    await node.put(ADA)
    [row] = await outbox.rows()
    assert row.handle is not None

    with caplog.at_level(logging.WARNING):
        posted = await _send_through_cloudflare(outbox, _FORBIDDEN)
        for _ in range(100):
            [row] = await outbox.rows()
            if row.status != "queued":
                break
            assert await node.read() == PENDING
            posted += await _send_through_cloudflare(outbox, _FORBIDDEN, now=row.next_attempt_at)

    assert row.status in outcomes
    assert row.last_error is not None
    assert "HTTP 403: 10102 email.sending.error.authentication.forbidden" in row.last_error
    assert row.body is None
    # Retried before giving up, in every case.
    assert len(posted) == attempts if limit == "the attempt cap" else len(posted) >= 2
    # One error for each refusal, and none of them names the address.
    errors = _errors(caplog)
    assert len(errors) == len(posted)
    assert all(ADA not in error for error in errors)
    assert await node.read() == UNSENT
    assert await _challenge(outbox, node.id) is None
    assert not await _link_lives(outbox, row.handle)
    for body in posted:
        assert await claim_links.peek(token_in(_message_in(body))) is None
    claim = await _claim(outbox, node.id)
    assert claim is not None
    assert (claim.email, claim.verified, claim.undeliverable) == (ADA, False, False)

    response = await node.resend()
    [sent] = await _send_through_cloudflare(outbox, _accepted("queued"))

    assert response.json() == PENDING
    assert sent["to"] == [ADA]
    assert await claim_links.peek(token_in(_message_in(sent))) is not None
    assert await node.read() == PENDING


async def test_a_refusal_of_the_account_fixed_before_the_retry_delivers_the_mail_once(node, outbox, cloudflare):
    """The retry carries the same mail and link, and the fix lets it through
    once: nothing is left to send after it."""
    await node.put(ADA)

    [refused] = await _send_through_cloudflare(outbox, _FORBIDDEN)
    [row] = await outbox.rows()
    assert (row.status, row.attempts) == ("queued", 1)
    token = token_in(_message_in(refused))

    [retried] = await _send_through_cloudflare(outbox, _accepted("queued"), now=row.next_attempt_at)

    assert token_in(_message_in(retried)) == token
    [row] = await outbox.rows()
    assert (row.status, row.outcome, row.attempts) == ("sent", "sent", 2)
    assert row.body is None
    assert await node.read() == PENDING
    assert await claim_links.peek(token) is not None
    challenge = await _challenge(outbox, node.id)
    assert challenge is not None
    assert challenge.handle == claim_links.handle_for(token)
    # Nothing is due again, however late the worker looks.
    assert await _send_through_cloudflare(outbox, _accepted("queued"), now=row.expires_at) == []
    assert await _statuses(outbox) == [(ADA, "sent")]
