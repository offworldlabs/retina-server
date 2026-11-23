"""Bounces and complaints from Cloudflare's event queue, applied to the claims
they concern, and everything else on it, suppressions included, left alone.

Against the shared database core.users opens, as tests/test_claim_routes.py is,
because that is where production keeps the claims and the links together, and
the order services/mail_events.py writes the two in only means something where
they share a file. The queue is an httpx.MockTransport answering in the shapes
Cloudflare's pull-consumer documentation gives; nothing here leaves the
machine. One test sends to a socket of its own on 127.0.0.1 instead, to meet
the HTTP client's own refusal of a header.

The settings are driven through the environment rather than patched in, for the
reason tests/test_mail.py gives.
"""

import asyncio
import base64
import contextlib
import json
import logging
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.users
from core.database import DATABASE_URL
from core.mail_outbox import MailOutbox
from core.nodes import NodeClaim
from core.users import _set_sqlite_pragmas, async_session_maker, engine
from services import claim_links, mail_events, mail_outbox
from services.node_claim_store import (
    ClaimStatus,
    claim_status,
    mark_verified,
    put_challenge,
    read_challenge,
    read_claim,
    set_claim_address,
)
from tests.claim_mail_helpers import Transport, configure_mail
from tests.ownership_helpers import register_node_row

ADA = "ada@example.com"
GRACE = "grace@example.com"
NODE = "ret1a2b3c4d"
OTHER_NODE = "ret9f8e7d6c"
THIRD_NODE = "ret2b3c4d5e"
FOURTH_NODE = "ret0123abcd"

ACCOUNT = "test-account"
QUEUE = "test-queue"
TOKEN = "events-token-never-logged"

SETTINGS = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID", "CLOUDFLARE_EMAIL_EVENTS_TOKEN")

BOUNCED = "cf.email.sending.message.bounced"
REJECTED = "cf.email.sending.message.rejected"
COMPLAINED = "cf.email.sending.message.complained"
DELIVERED = "cf.email.sending.message.delivered"
DEFERRED = "cf.email.sending.message.deferred"
FAILED = "cf.email.sending.message.failed"

HARD_BOUNCE = {"bounce": {"type": "hard", "classification": "permanent_failure", "reason": "550 5.1.1 User unknown"}}
SOFT_BOUNCE = {"bounce": {"type": "soft", "classification": "temporary_failure", "reason": "451 4.2.0 Try later"}}
SUPPRESSED = {"rejection": {"reason": "suppressed", "party": "recipient", "detail": "Recipient is suppressed"}}
COMPLAINT = {"complaint": {"type": "abuse"}}


@pytest.fixture(autouse=True)
def _clean_events_env(monkeypatch):
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID", QUEUE)
    monkeypatch.setenv("CLOUDFLARE_EMAIL_EVENTS_TOKEN", TOKEN)


@pytest.fixture
def sleeps(monkeypatch):
    """Every wait the task asks for, in order, each returning at once.

    Only the module's own reference is replaced, so the event loop and the test
    keep the real one.
    """
    real_sleep = asyncio.sleep
    asked: list[float] = []

    async def _sleep(delay: float) -> None:
        asked.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(mail_events, "asyncio", SimpleNamespace(sleep=_sleep))
    return asked


@pytest.fixture
def events_log(caplog):
    caplog.set_level(logging.DEBUG, logger="services.mail_events")
    return caplog


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "services.mail_events" and r.levelno == level]


# ── Building events and messages ─────────────────────────────────────────────


def _event(kind: str, status: dict | None = None, recipient: object = ADA, source: str = "email.sending") -> dict:
    """An event in the envelope the Email Sending documentation publishes."""
    payload = {
        "eventId": "0190d0c4-7ea1-7af2-8b88-c1d2e3f4a5b6",
        "messageId": "0101018f7d0c4d9a-msg",
        "sender": "no-reply@example.com",
        "recipient": recipient,
        "subject": "Connect your RETINA node",
        "terminal": True,
        "delivery": {"status": kind.rsplit(".", 1)[-1]},
        **(status or {}),
    }
    return {
        "type": kind,
        "source": {"type": source, "zoneId": "zone", "domain": "example.com"},
        "payload": payload,
        "metadata": {
            "accountId": ACCOUNT,
            "eventSubscriptionId": "subscription",
            "eventSchemaVersion": 1,
            "eventTimestamp": "2026-10-12T02:48:57.132Z",
        },
    }


def _message(lease: str, event: object = None, *, body: object = None, content_type: str = "json") -> dict:
    """One pulled message. A `json` body is base64 encoded, as Queues sends it
    to a pull consumer; a `text` one is the string itself."""
    if body is None:
        text = json.dumps(event)
        body = base64.b64encode(text.encode()).decode() if content_type == "json" else text
    return {
        "body": body,
        "id": f"id-{lease}",
        "timestamp_ms": 1689615013586,
        "attempts": 1,
        "metadata": {"CF-Content-Type": content_type},
        "lease_id": lease,
    }


class FakeQueue:
    """The pull and ack endpoints, answering in the documented shapes.

    `pull_answers` are served in turn before any batch: an httpx.Response to
    answer with, or an exception to raise as the transport would.
    """

    def __init__(self, batches=(), pull_answers=(), ack_answers=()):
        self.batches = [list(batch) for batch in batches]
        self.pull_answers = list(pull_answers)
        self.ack_answers = list(ack_answers)
        self.requests: list[httpx.Request] = []
        self.acks: list[dict] = []
        self.pulls = 0
        self.acked = asyncio.Event()
        self.pulled_enough = asyncio.Event()
        self.pulls_wanted = 1
        self.on_pull = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/messages/pull"):
            self.pulls += 1
            if self.on_pull is not None:
                self.on_pull()
            if self.pulls >= self.pulls_wanted:
                self.pulled_enough.set()
            if self.pull_answers:
                answer = self.pull_answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            messages = self.batches.pop(0) if self.batches else []
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "errors": [],
                    "messages": [],
                    "result": {"message_backlog_count": len(messages), "messages": messages},
                },
            )
        assert request.url.path.endswith("/messages/ack"), request.url
        body = json.loads(request.content)
        if self.ack_answers:
            return self.ack_answers.pop(0)
        self.acks.append(body)
        self.acked.set()
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "messages": [],
                "result": {"ackCount": len(body["acks"]), "retryCount": len(body["retries"]), "warnings": {}},
            },
        )


@pytest.fixture
def queue(monkeypatch):
    fake = FakeQueue()
    monkeypatch.setattr(mail_events, "_transport", httpx.MockTransport(fake.handler))
    return fake


async def _run_until(waited: asyncio.Event) -> None:
    """Run the lifespan task until `waited` is set, then stop it.

    A task that is done by then returned or raised, neither of which a
    configured task may do, and awaiting it surfaces whichever it was.
    """
    task = asyncio.create_task(mail_events.events_task())
    try:
        await asyncio.wait_for(waited.wait(), timeout=5)  # bounds a hang, not the loop
        await asyncio.sleep(0)
        assert not task.done(), "the task stopped"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ── Building claims ──────────────────────────────────────────────────────────


async def _offer(node_id: str, email: str) -> str:
    """A registered node offering `email`, with a live link in the post; returns
    the token mailed. Built through the calls the node route makes."""
    await register_node_row(node_id)
    challenge = await claim_links.issue(email=email, node_id=node_id, now=time.time())
    assert challenge is not None
    async with async_session_maker() as session:
        async with session.begin():
            await set_claim_address(session, node_id, email)
            await put_challenge(session, node_id, email, challenge.handle, challenge.expires_at)
    return challenge.token


async def _status(node_id: str) -> ClaimStatus:
    async with async_session_maker() as session:
        return await claim_status(session, node_id, time.time())


async def _row(node_id: str) -> NodeClaim | None:
    async with async_session_maker() as session:
        return await read_claim(session, node_id)


async def _link_live(token: str) -> bool:
    return await claim_links.peek(token) is not None


async def _handle(event: dict) -> bool:
    async with async_session_maker() as session:
        return await mail_events.handle_event(session, event)


# ── The task's configuration ─────────────────────────────────────────────────


@pytest.mark.parametrize("missing", [SETTINGS, *((name,) for name in SETTINGS)])
async def test_the_task_is_off_without_all_three_settings(missing, configured, queue, monkeypatch, events_log):
    for name in missing:
        monkeypatch.delenv(name)

    await asyncio.wait_for(mail_events.events_task(), timeout=1.0)

    assert queue.requests == []
    [said] = _records(events_log, logging.INFO)
    assert said.getMessage().startswith("Mail events: off")
    assert all(name in said.getMessage() for name in missing)


async def test_the_task_says_once_that_it_is_on(configured, queue, sleeps, events_log):
    await _run_until(queue.pulled_enough)

    on = [r for r in _records(events_log, logging.INFO) if "on," in r.getMessage()]
    assert len(on) == 1
    assert QUEUE in on[0].getMessage()


# ── Keeping the token out of the log ─────────────────────────────────────────

# The two halves of a token with something between them. Every form the token
# could be written in carries both.
HEAD = "cfq-head-7Hq2"
TAIL = "cfq-tail-Zp9x"


class _Stopped(Exception):
    """Raised from the task's first backoff, ending it after one failed round."""


def _stop_at_first_backoff(monkeypatch) -> None:
    async def _sleep(_delay: float) -> None:
        raise _Stopped

    monkeypatch.setattr(mail_events, "asyncio", SimpleNamespace(sleep=_sleep))


def _token_logged(caplog) -> bool:
    return HEAD in caplog.text or TAIL in caplog.text


@pytest.fixture
def silent_server():
    """A local socket that takes connections and reads what is sent, and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.2)
    stop = threading.Event()

    def _accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                with contextlib.suppress(OSError):
                    connection.recv(65536)

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    yield server.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    server.close()


@pytest.mark.parametrize(
    ("between", "named"),
    [
        ("\n", "a line break"),
        ("\r\n", "a line break"),
        (" ", "a space or a tab"),
        ("\t", "a space or a tab"),
        ("\x01", "a control character"),
        ("\x7f", "a control character"),
        ("é", "a character outside ASCII"),
    ],
    ids=["newline", "crlf", "space", "tab", "control", "delete", "non-ascii"],
)
async def test_a_token_no_bearer_token_could_be_keeps_the_task_off(
    between, named, configured, queue, monkeypatch, events_log
):
    """A value quoted across two lines in backend/.env keeps its line break,
    since only the ends are trimmed, and the HTTP client would refuse the
    header by quoting it whole. Said by the setting's name, never its value."""
    monkeypatch.setenv("CLOUDFLARE_EMAIL_EVENTS_TOKEN", f"{HEAD}{between}{TAIL}")

    await asyncio.wait_for(mail_events.events_task(), timeout=1.0)

    assert queue.requests == []
    [said] = _records(events_log, logging.WARNING)
    assert said.getMessage().startswith(f"Mail events: off, since CLOUDFLARE_EMAIL_EVENTS_TOKEN holds {named},")
    assert not _token_logged(events_log)


def test_every_character_a_bearer_token_is_made_of_is_accepted():
    """RFC 6750's b64token, so the check never turns away a token that could work."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~+/"

    assert mail_events._unsendable(alphabet + "==") is None


async def test_an_error_quoting_the_token_is_logged_without_it(silent_server, configured, monkeypatch, events_log):
    """What the check keeps out, met by the real client against a real socket:
    h11 refuses the header by quoting it whole, and the warning names the
    failure without the text. The check is switched off to get there, as if a
    character had slipped past it."""
    monkeypatch.setenv("CLOUDFLARE_EMAIL_EVENTS_TOKEN", f"{HEAD}\n{TAIL}")
    monkeypatch.setattr(mail_events, "_unsendable", lambda _token: None)
    monkeypatch.setattr(
        mail_events,
        "_QUEUE_URL",
        f"http://127.0.0.1:{silent_server}/accounts/{{account}}/queues/{{queue}}/messages/{{action}}",
    )
    monkeypatch.setattr(mail_events, "_transport", None)
    judged: list[BaseException] = []
    real_quotes_token = mail_events._quotes_token

    def _quotes_token(exc: BaseException, token: str) -> bool:
        judged.append(exc)
        return real_quotes_token(exc, token)

    monkeypatch.setattr(mail_events, "_quotes_token", _quotes_token)
    _stop_at_first_backoff(monkeypatch)

    with pytest.raises(_Stopped):
        await asyncio.wait_for(mail_events.events_task(), timeout=5)

    # The library's own refusal, which would have put the token in the log.
    [refusal] = judged
    assert isinstance(refusal, httpx.LocalProtocolError)
    assert TAIL in str(refusal)
    [warning] = _records(events_log, logging.WARNING)
    assert warning.getMessage() == (
        "Mail events: LocalProtocolError, whose text quotes CLOUDFLARE_EMAIL_EVENTS_TOKEN and is withheld; "
        "trying again in 30s"
    )
    assert warning.exc_info is None
    assert not _token_logged(events_log)


async def test_a_fault_raised_from_one_quoting_the_token_is_logged_without_its_traceback(
    configured, queue, monkeypatch, events_log
):
    """Anything but a refusal or a network fault is logged with its traceback,
    which prints what it was raised from as well."""

    def _refuse(request: httpx.Request) -> httpx.Response:
        try:
            raise ValueError(f"cannot send {request.headers['Authorization']!r}")
        except ValueError as exc:
            raise RuntimeError("the request could not be sent") from exc

    monkeypatch.setattr(mail_events, "_transport", httpx.MockTransport(_refuse))
    _stop_at_first_backoff(monkeypatch)

    with pytest.raises(_Stopped):
        await asyncio.wait_for(mail_events.events_task(), timeout=5)

    [warning] = _records(events_log, logging.WARNING)
    assert warning.getMessage().startswith("Mail events: RuntimeError, whose text quotes")
    assert warning.exc_info is None
    assert TOKEN not in events_log.text


# ── The verdicts ─────────────────────────────────────────────────────────────


async def test_a_hard_bounce_marks_the_claim_and_kills_its_link():
    token = await _offer(NODE, ADA)

    assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is True

    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    async with async_session_maker() as session:
        assert await read_challenge(session, NODE) is None
    assert not await _link_live(token)


async def test_a_complaint_marks_the_claim_and_kills_its_link():
    token = await _offer(NODE, ADA)

    assert await _handle(_event(COMPLAINED, COMPLAINT)) is True

    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(token)


async def test_a_suppressed_rejection_changes_nothing_and_is_acknowledged(configured, queue, sleeps, events_log):
    """The suppression list also holds addresses that lapse off it or get
    fixed, so being on it is no proof the mailbox is dead; and a send of ours
    to one already heard so from the send API."""
    token = await _offer(NODE, ADA)
    claim_columns = ("email", "verified", "undeliverable", "user_id", "updated_at")
    challenge_columns = ("email", "handle", "created_at", "expires_at")

    async def _snapshot() -> tuple[list, list]:
        async with async_session_maker() as session:
            claim, challenge = await read_claim(session, NODE), await read_challenge(session, NODE)
            assert claim is not None and challenge is not None
            return [getattr(claim, c) for c in claim_columns], [getattr(challenge, c) for c in challenge_columns]

    before = await _snapshot()
    queue.batches = [[_message("lease-suppressed", _event(REJECTED, SUPPRESSED))]]

    await _run_until(queue.acked)

    assert queue.acks == [{"acks": [{"lease_id": "lease-suppressed"}], "retries": []}]
    assert await _snapshot() == before
    assert await _status(NODE) == ClaimStatus("pending", ADA, False)
    assert await _link_live(token)
    assert any(f"{REJECTED} changes nothing" in r.getMessage() for r in _records(events_log, logging.DEBUG))
    assert [r for r in events_log.records if r.name == "services.mail_events" and r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (DELIVERED, None),
        (DEFERRED, SOFT_BOUNCE),
        # What a bounced event carries once temporary retries ran out.
        (BOUNCED, SOFT_BOUNCE),
        (FAILED, {"failure": {"reason": "delivery_failed"}}),
        # A rejection, whatever it says or leaves out.
        (REJECTED, SUPPRESSED),
        (REJECTED, {"rejection": {"reason": "spam", "party": "sender", "detail": "Rejected"}}),
        (REJECTED, {"rejection": {"reason": "a_reason_nobody_listed", "party": "recipient"}}),
        (REJECTED, {"rejection": {"party": "recipient"}}),
        (REJECTED, {"rejection": "suppressed"}),
        (REJECTED, None),
        ("cf.email.sending.message.opened", None),
    ],
)
async def test_everything_else_changes_nothing(kind, status, events_log):
    token = await _offer(NODE, ADA)

    assert await _handle(_event(kind, status)) is False

    assert await _status(NODE) == ClaimStatus("pending", ADA, False)
    assert await _link_live(token)
    # Acknowledged quietly: nothing above debug.
    assert [r for r in events_log.records if r.name == "services.mail_events" and r.levelno > logging.DEBUG] == []


async def test_only_open_claims_on_that_address_are_marked():
    """A confirmed address has proved itself, an owner outranks the address,
    and a different address is a different person."""
    await _offer(NODE, ADA)
    grace_token = await _offer(OTHER_NODE, GRACE)

    await register_node_row(THIRD_NODE)
    await register_node_row(FOURTH_NODE)
    async with async_session_maker() as session:
        async with session.begin():
            await set_claim_address(session, THIRD_NODE, ADA)
            await mark_verified(session, THIRD_NODE, ADA)
            # An owner beside an unconfirmed address, as a radar an
            # administrator connected is recorded.
            session.add(NodeClaim(node_id=FOURTH_NODE, user_id="some-account", email=ADA, verified=False))

    assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is True

    assert (await _status(NODE)).undeliverable is True
    verified, owned = await _row(THIRD_NODE), await _row(FOURTH_NODE)
    assert (verified.verified, verified.undeliverable) == (True, False)
    assert (owned.user_id, owned.undeliverable) == ("some-account", False)
    assert await _status(OTHER_NODE) == ClaimStatus("pending", GRACE, False)
    assert await _link_live(grace_token)


async def test_every_open_claim_on_the_address_is_marked():
    first = await _offer(NODE, ADA)
    second = await _offer(OTHER_NODE, ADA)

    assert await _handle(_event(COMPLAINED, COMPLAINT)) is True

    for node_id in (NODE, OTHER_NODE):
        assert await _status(node_id) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(first)
    assert not await _link_live(second)


async def test_the_recipient_is_normalised_as_the_address_was():
    await _offer(NODE, ADA)

    assert await _handle(_event(BOUNCED, HARD_BOUNCE, recipient="  Ada@Example.COM ")) is True

    assert (await _status(NODE)).undeliverable is True


async def test_a_redelivered_event_changes_nothing_more(events_log):
    await _offer(NODE, ADA)
    event = _event(BOUNCED, HARD_BOUNCE)
    assert await _handle(event) is True
    before = await _row(NODE)

    assert await _handle(event) is False

    after = await _row(NODE)
    assert (after.email, after.undeliverable, after.updated_at) == (before.email, True, before.updated_at)
    marked = [r for r in _records(events_log, logging.INFO) if "marked undeliverable" in r.getMessage()]
    assert len(marked) == 1
    assert NODE in marked[0].getMessage()
    assert BOUNCED in marked[0].getMessage()


async def test_an_address_corrected_since_is_left_alone():
    """The bounce was for the address the node no longer offers."""
    await _offer(NODE, ADA)
    grace_token = await _offer(NODE, GRACE)

    assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is False

    assert await _status(NODE) == ClaimStatus("pending", GRACE, False)
    assert await _link_live(grace_token)


async def test_events_from_another_source_are_ignored():
    token = await _offer(NODE, ADA)

    assert await _handle(_event(BOUNCED, HARD_BOUNCE, source="kv")) is False

    assert await _status(NODE) == ClaimStatus("pending", ADA, False)
    assert await _link_live(token)


@pytest.mark.parametrize(
    "event",
    [
        {**_event(BOUNCED, HARD_BOUNCE), "type": None},
        {**_event(BOUNCED, HARD_BOUNCE), "payload": "not an object"},
        _event(BOUNCED, {"bounce": {"classification": "permanent_failure"}}),
        _event(BOUNCED, {"bounce": "hard"}),
        _event(COMPLAINED, COMPLAINT, recipient=None),
        _event(COMPLAINED, COMPLAINT, recipient="not an address"),
        _event(COMPLAINED, COMPLAINT, recipient=["ada@example.com"]),
    ],
    ids=[
        "no-type",
        "payload",
        "bounce-type",
        "bounce-shape",
        "no-recipient",
        "bad-recipient",
        "list",
    ],
)
async def test_a_malformed_event_is_dropped_and_said_once(event, events_log):
    token = await _offer(NODE, ADA)

    assert await _handle(event) is False

    assert len(_records(events_log, logging.WARNING)) == 1
    assert await _status(NODE) == ClaimStatus("pending", ADA, False)
    assert await _link_live(token)


# ── Surviving a failure halfway ──────────────────────────────────────────────


async def test_a_link_that_will_not_die_leaves_the_claim_for_the_redelivery(monkeypatch):
    """Killed first, so a failure leaves the challenge, and the handle with it,
    for the next delivery of the same event to find."""
    token = await _offer(NODE, ADA)
    real_invalidate = claim_links.invalidate

    async def _refuse(handle: str) -> None:
        raise RuntimeError("the link store is unavailable")

    monkeypatch.setattr(claim_links, "invalidate", _refuse)
    with pytest.raises(RuntimeError):
        await _handle(_event(BOUNCED, HARD_BOUNCE))

    assert await _status(NODE) == ClaimStatus("pending", ADA, False)
    assert await _link_live(token)

    monkeypatch.setattr(claim_links, "invalidate", real_invalidate)
    assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is True

    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(token)


async def test_a_resend_landing_mid_way_has_its_link_killed_too(monkeypatch):
    """A resend between the links dying and the claim being marked replaces the
    challenge; the marking drops the replacement, and its link goes too."""
    first = await _offer(NODE, ADA)
    resent: list[str] = []
    real_invalidate = claim_links.invalidate

    async def _invalidate_then_resend(handle: str) -> None:
        await real_invalidate(handle)
        if not resent:
            challenge = await claim_links.issue(email=ADA, node_id=NODE, now=time.time())
            async with async_session_maker() as session:
                async with session.begin():
                    await put_challenge(session, NODE, ADA, challenge.handle, challenge.expires_at)
            resent.append(challenge.token)

    monkeypatch.setattr(claim_links, "invalidate", _invalidate_then_resend)

    assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is True

    assert resent
    assert not await _link_live(first)
    assert not await _link_live(resent[0])
    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)


async def test_no_transaction_is_open_while_the_link_store_writes():
    """The same event against a session whose reads open a transaction, as
    SQLAlchemy's recipe for real SQLite transactions makes them do.

    A transaction that read before the link store's connection wrote cannot
    write after it: SQLite refuses to upgrade a read whose snapshot another
    writer has moved past. The default driver opens no transaction for a read,
    so only a session like this one shows that the read is closed first.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from core.database import DATABASE_URL
    from core.users import _set_sqlite_pragmas

    token = await _offer(NODE, ADA)
    engine = create_async_engine(DATABASE_URL, connect_args={"timeout": 1})
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    @event.listens_for(engine.sync_engine, "connect")
    def _no_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            assert await mail_events.handle_event(session, _event(BOUNCED, HARD_BOUNCE)) is True
    finally:
        await engine.dispose()

    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(token)


# ── Mail still queued, and claims that moved on ──────────────────────────────


async def _clear_outbox() -> None:
    async with async_session_maker() as session:
        await session.execute(delete(MailOutbox))
        await session.commit()


@pytest.fixture
async def claim_mail(monkeypatch, tmp_path):
    """Claim mail that can be queued, keyed beside this test's own files, in a
    queue empty before the test and after it: the suite's shared database is
    not cleared of mail between tests."""
    configure_mail(monkeypatch, tmp_path)
    await _clear_outbox()
    yield
    await _clear_outbox()


async def _queue_claim_mail(node_id: str, email: str) -> claim_links.Challenge:
    """A fresh challenge for `email` and the mail carrying it, written as
    PUT /v1/nodes/claim writes them."""
    node_ref = await register_node_row(node_id)
    challenge = await claim_links.issue(email=email, node_id=node_id, now=time.time())
    assert challenge is not None
    async with async_session_maker() as session:
        async with session.begin():
            await mail_outbox.cancel(session, node_id=node_id)
            displaced = await put_challenge(session, node_id, email, challenge.handle, challenge.expires_at)
            if displaced is not None:
                await claim_links.invalidate_in(session, displaced)
            await set_claim_address(session, node_id, email)
            await claim_links.queue(session, challenge, email=email, node_id=node_id, node_ref=node_ref)
    return challenge


async def _mail_rows() -> list[tuple[str | None, str, str]]:
    async with async_session_maker() as session:
        rows = await session.execute(
            select(MailOutbox.node_id, MailOutbox.recipient, MailOutbox.status).order_by(MailOutbox.id)
        )
        return [(node_id, recipient, status) for node_id, recipient, status in rows]


@pytest.mark.parametrize(
    ("kind", "status"), [(BOUNCED, HARD_BOUNCE), (COMPLAINED, COMPLAINT)], ids=["bounce", "complaint"]
)
async def test_the_claim_mail_still_queued_for_a_marked_address_never_goes(kind, status, claim_mail, events_log):
    """The first mail went and was accepted. The resend the node asked for is
    still queued, behind a provider asking us to slow down, when the verdict on
    the first arrives: it would go to the address that just failed, carrying a
    link the marking kills."""
    await _offer(NODE, ADA)
    resend = await _queue_claim_mail(NODE, ADA)
    assert await _mail_rows() == [(NODE, ADA, "queued")]

    assert await _handle(_event(kind, status)) is True

    assert await _mail_rows() == [(NODE, ADA, "cancelled")]
    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(resend.token)
    transport = Transport()
    assert await mail_outbox.run_once(owner="test-worker", transport=transport) is False
    assert transport.sent == []
    [marked] = [r.getMessage() for r in _records(events_log, logging.INFO) if "marked undeliverable" in r.getMessage()]
    assert marked.endswith(f"node {NODE} is marked undeliverable and the claim mail still queued for it is cancelled")

    # Delivered again, it finds nothing open and touches nothing.
    assert await _handle(_event(kind, status)) is False
    assert await _mail_rows() == [(NODE, ADA, "cancelled")]


def _renominate(node_id: str, email: str, challenge: claim_links.Challenge) -> None:
    """What PUT /v1/nodes/claim with another address commits for `node_id`, on
    a connection of its own as another request's would be: the address, its
    challenge in place of the old one, and the old one's link killed."""
    database = make_url(DATABASE_URL).database
    assert database is not None
    connection = sqlite3.connect(database, timeout=5)
    try:
        displaced = connection.execute(
            "SELECT handle FROM node_claim_challenges WHERE node_id = ?", (node_id,)
        ).fetchone()
        connection.execute(
            "UPDATE node_claims SET email = ?, verified = 0, undeliverable = 0 WHERE node_id = ?", (email, node_id)
        )
        connection.execute(
            "UPDATE node_claim_challenges SET email = ?, handle = ?, created_at = ?, expires_at = ? WHERE node_id = ?",
            (email, challenge.handle, time.time(), challenge.expires_at, node_id),
        )
        if displaced is not None:
            connection.execute("DELETE FROM magic_links WHERE token_hash = ?", displaced)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("seam", ["while-the-links-die", "as-the-marking-writes"])
async def test_an_address_replaced_while_the_feed_works_is_left_alone(seam, claim_mail, monkeypatch, events_log):
    """The node offered another address after the feed read its claim and
    before it wrote: a verdict on one address says nothing about the next. The
    replacement commits while the links die, and at the last moment there is,
    as the statement that flags the claim is about to run. Another claim on the
    same address that nothing raced is marked all the same."""
    await _offer(NODE, ADA)
    other = await _offer(OTHER_NODE, ADA)
    node_ref = await register_node_row(NODE)
    grace = await claim_links.issue(email=GRACE, node_id=NODE, now=time.time())
    assert grace is not None
    async with async_session_maker() as session:
        async with session.begin():
            # The route queues it with the new address. Queued ahead of it here,
            # it would be cancelled by a feed that marked the node anyway.
            await claim_links.queue(session, grace, email=GRACE, node_id=NODE, node_ref=node_ref)

    replaced: list[str] = []

    def _replace_once() -> None:
        if not replaced:
            replaced.append(seam)
            _renominate(NODE, GRACE, grace)

    def _before_the_flag(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("UPDATE NODE_CLAIMS"):
            _replace_once()

    if seam == "while-the-links-die":
        real_invalidate = claim_links.invalidate

        async def _invalidate_then_replace(handle: str) -> None:
            await real_invalidate(handle)
            _replace_once()

        monkeypatch.setattr(claim_links, "invalidate", _invalidate_then_replace)
    else:
        event.listen(engine.sync_engine, "before_cursor_execute", _before_the_flag)
    try:
        assert await _handle(_event(BOUNCED, HARD_BOUNCE)) is True
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", _before_the_flag):
            event.remove(engine.sync_engine, "before_cursor_execute", _before_the_flag)

    assert replaced == [seam]
    assert await _status(NODE) == ClaimStatus("pending", GRACE, False)
    row = await _row(NODE)
    assert row is not None and row.undeliverable is False
    async with async_session_maker() as session:
        challenge = await read_challenge(session, NODE)
    assert challenge is not None and (challenge.email, challenge.handle) == (GRACE, grace.handle)
    assert await _link_live(grace.token)
    assert await _mail_rows() == [(NODE, GRACE, "queued")]

    assert await _status(OTHER_NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(other)
    marked = [r.getMessage() for r in _records(events_log, logging.INFO) if "marked undeliverable" in r.getMessage()]
    assert len(marked) == 1
    assert f"node {OTHER_NODE} " in marked[0]


# ── The pull consumer ────────────────────────────────────────────────────────


async def test_a_pulled_batch_is_applied_and_acknowledged(configured, queue, sleeps, events_log):
    ada_token = await _offer(NODE, ADA)
    grace_token = await _offer(OTHER_NODE, GRACE)
    queue.batches = [
        [
            _message("lease-bounce", _event(BOUNCED, HARD_BOUNCE)),
            _message("lease-delivered", _event(DELIVERED, recipient=GRACE), content_type="text"),
            _message("lease-garbage", body="%%% neither base64 nor JSON"),
            _message("lease-list", body=base64.b64encode(b"[1, 2]").decode()),
            _message("lease-number", body=7),
        ]
    ]

    await _run_until(queue.acked)

    pull = queue.requests[0]
    assert pull.method == "POST"
    assert str(pull.url) == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/queues/{QUEUE}/messages/pull"
    assert pull.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(pull.content) == {"batch_size": 100, "visibility_timeout_ms": 120_000}
    ack = next(r for r in queue.requests if r.url.path.endswith("/ack"))
    assert str(ack.url) == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/queues/{QUEUE}/messages/ack"
    assert ack.headers["Authorization"] == f"Bearer {TOKEN}"

    leases = ["lease-bounce", "lease-delivered", "lease-garbage", "lease-list", "lease-number"]
    assert queue.acks == [{"acks": [{"lease_id": lease} for lease in leases], "retries": []}]
    assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
    assert not await _link_live(ada_token)
    assert await _status(OTHER_NODE) == ClaimStatus("pending", GRACE, False)
    assert await _link_live(grace_token)
    # One line for each of the three unreadable bodies.
    assert len(_records(events_log, logging.WARNING)) == 3
    assert TOKEN not in events_log.text
    assert ada_token not in events_log.text


async def test_an_empty_queue_is_asked_again_after_half_a_minute(configured, queue, sleeps):
    queue.pulls_wanted = 3

    await _run_until(queue.pulled_enough)

    assert sleeps[:2] == [30.0, 30.0]
    assert all(r.url.path.endswith("/pull") for r in queue.requests)


async def test_a_settled_batch_is_followed_by_another_pull_at_once(configured, queue, sleeps):
    queue.batches = [[_message("lease-one", _event(DELIVERED))], [_message("lease-two", _event(DELIVERED))]]
    waits_before_each_pull: list[int] = []
    queue.on_pull = lambda: waits_before_each_pull.append(len(sleeps))
    queue.pulls_wanted = 4

    await _run_until(queue.pulled_enough)

    assert [a["acks"] for a in queue.acks] == [[{"lease_id": "lease-one"}], [{"lease_id": "lease-two"}]]
    # No wait until the third pull came back empty, then the idle one.
    assert waits_before_each_pull[:4] == [0, 0, 0, 1]
    assert sleeps[0] == 30.0


async def test_an_event_that_fails_goes_back_and_the_task_backs_off(configured, queue, sleeps, monkeypatch):
    await _offer(NODE, ADA)
    real_handle = mail_events.handle_event

    async def _fail_on_grace(session, event):
        if event["payload"]["recipient"] == GRACE:
            raise RuntimeError("the database is locked")
        return await real_handle(session, event)

    monkeypatch.setattr(mail_events, "handle_event", _fail_on_grace)
    queue.batches = [
        [
            _message("lease-grace", _event(COMPLAINED, COMPLAINT, recipient=GRACE)),
            _message("lease-ada", _event(BOUNCED, HARD_BOUNCE)),
        ]
    ]
    queue.pulls_wanted = 2

    await _run_until(queue.pulled_enough)

    assert queue.acks[0] == {"acks": [{"lease_id": "lease-ada"}], "retries": [{"lease_id": "lease-grace"}]}
    assert (await _status(NODE)).undeliverable is True
    # Backed off before pulling again, rather than straight back into it.
    assert sleeps[0] == 30.0


async def test_an_unsettled_batch_is_applied_again_without_changing_anything(configured, queue, sleeps, events_log):
    """An ack that fails leaves the batch to come back, and coming back is harmless."""
    await _offer(NODE, ADA)
    batch = [_message("lease-bounce", _event(BOUNCED, HARD_BOUNCE))]
    queue.batches = [batch, batch]
    queue.ack_answers = [httpx.Response(503, json={"success": False, "errors": [{"code": 10000, "message": "busy"}]})]

    await _run_until(queue.acked)

    assert queue.acks == [{"acks": [{"lease_id": "lease-bounce"}], "retries": []}]
    marked = [r for r in _records(events_log, logging.INFO) if "marked undeliverable" in r.getMessage()]
    assert len(marked) == 1
    assert (await _status(NODE)).undeliverable is True


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.Response(500, json={"success": False, "errors": [{"code": 10001, "message": "internal"}]}),
        lambda: httpx.Response(
            403, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
        ),
        lambda: httpx.Response(200, json={"success": False, "errors": [], "result": None}),
        lambda: httpx.Response(502, text="<html>bad gateway</html>"),
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.ReadTimeout("timed out"),
    ],
    ids=["500", "403", "unsuccessful-200", "not-json", "connect", "timeout"],
)
async def test_a_failing_pull_backs_off_without_raising(failure, configured, queue, sleeps, events_log):
    queue.pull_answers = [failure() for _ in range(7)]
    queue.pulls_wanted = 9

    await _run_until(queue.pulled_enough)

    # Doubling to the cap, then the idle wait once the queue answers again.
    assert sleeps[:8] == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0, 30.0]
    warnings = _records(events_log, logging.WARNING)
    assert len(warnings) == 7
    assert all("trying again in" in r.getMessage() for r in warnings)
    assert all(r.exc_info is None for r in warnings)
    assert any("being drained again" in r.getMessage() for r in _records(events_log, logging.INFO))
    assert TOKEN not in events_log.text


async def test_a_message_with_no_lease_is_said_and_left_to_lapse(configured, queue, sleeps, events_log):
    """Nothing settles it, so nothing comes of pulling again at once either."""
    no_lease = _message("unused", _event(DELIVERED))
    del no_lease["lease_id"]
    queue.batches = [[None, no_lease]]
    queue.pulls_wanted = 2

    await _run_until(queue.pulled_enough)

    assert len([r for r in _records(events_log, logging.WARNING) if "no lease" in r.getMessage()]) == 2
    assert not any(r.url.path.endswith("/ack") for r in queue.requests)
    assert sleeps[0] == 30.0


async def test_an_unexpected_fault_is_logged_with_its_traceback_and_survived(
    configured, queue, sleeps, events_log, monkeypatch
):
    real_drain = mail_events._drain
    calls = 0

    async def _fault_once(client, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("a fault in this module")
        return await real_drain(client, settings)

    monkeypatch.setattr(mail_events, "_drain", _fault_once)

    await _run_until(queue.pulled_enough)

    [warning] = _records(events_log, logging.WARNING)
    assert "RuntimeError: a fault in this module" in warning.getMessage()
    assert warning.exc_info is not None
    assert sleeps[0] == 30.0


# ── Stopping ─────────────────────────────────────────────────────────────────

_HOLD_THE_MARKING = "CREATE TEMP TRIGGER hold BEFORE UPDATE OF undeliverable ON node_claims BEGIN SELECT hold(); END"


def _held_at_the_marking():
    """An engine on the suite's database whose statement flagging a claim stops
    until `release` is set, with `started` set once one has: a stop can then
    land mid-statement, with the write lock the statement took still held."""
    started, release = threading.Event(), threading.Event()

    def hold() -> int:
        started.set()
        release.wait(10)
        return 0

    def install(dbapi_connection, _record) -> None:
        dbapi_connection.create_function("hold", 0, hold)
        cursor = dbapi_connection.cursor()
        cursor.execute(_HOLD_THE_MARKING)
        cursor.close()

    held = create_async_engine(DATABASE_URL, connect_args={"timeout": 30})
    event.listen(held.sync_engine, "connect", _set_sqlite_pragmas)
    event.listen(held.sync_engine, "connect", install)
    return held, started, release


def _writable_at_once() -> bool:
    database = make_url(DATABASE_URL).database
    assert database is not None
    connection = sqlite3.connect(database, timeout=0.1)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


@pytest.mark.parametrize("entry", ["the-feed", "handle-event"])
async def test_a_stop_inside_the_marking_finishes_the_event_and_holds_no_lock(entry, configured, queue, monkeypatch):
    """A stop that lands inside the statement flagging the claim waits for it
    and for the rest of the event's writes, then goes on. Cancelled there
    instead, the statement's connection is dropped with its transaction open,
    and its write lock holds every other writer off, the outbox and the routes
    included, until that connection happens to be garbage collected.

    Stopped as the lifespan stops the feed, and through handle_event alone, as
    any other caller of it would be."""
    token = await _offer(NODE, ADA)
    held, started, release = _held_at_the_marking()
    sessions = async_sessionmaker(held, expire_on_commit=False)
    bounce = _event(BOUNCED, HARD_BOUNCE)

    async def _apply() -> None:
        async with sessions() as session:
            await mail_events.handle_event(session, bounce)

    if entry == "the-feed":
        monkeypatch.setattr(core.users, "async_session_maker", sessions)
        queue.batches = [[_message("lease-bounce", bounce)]]
        task = asyncio.create_task(mail_events.events_task())
    else:
        task = asyncio.create_task(_apply())
    try:
        try:
            assert await asyncio.to_thread(started.wait, 10)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            stopped_inside = task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _writable_at_once()
        assert not stopped_inside
        assert task.cancelled()
        assert await _status(NODE) == ClaimStatus("unclaimed", ADA, True)
        assert not await _link_live(token)
        # Applied and never acknowledged: it comes back once its lease runs
        # out, and finds nothing left to change.
        assert [r.url.path.rsplit("/", 1)[-1] for r in queue.requests] == (["pull"] if entry == "the-feed" else [])
    finally:
        await held.dispose()


@pytest.mark.parametrize("call", ["pull", "ack"])
async def test_a_stop_during_a_pull_or_an_ack_ends_the_task_at_once(call, configured, monkeypatch):
    """Only the database work is waited for. A request to the queue stops
    where it stands, and leaves nothing behind but a lease that runs out."""
    await _offer(NODE, ADA)
    fake = FakeQueue(batches=[[_message("lease-bounce", _event(BOUNCED, HARD_BOUNCE))]])
    asked, never = asyncio.Event(), asyncio.Event()

    async def _hang_at_the_call(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/messages/{call}"):
            asked.set()
            await never.wait()
        return fake.handler(request)

    monkeypatch.setattr(mail_events, "_transport", httpx.MockTransport(_hang_at_the_call))
    task = asyncio.create_task(mail_events.events_task())
    try:
        await asyncio.wait_for(asked.wait(), timeout=5)
        stopped_at = time.monotonic()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        took = time.monotonic() - stopped_at
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert done == {task}
    assert task.cancelled()
    assert took < 1
    # The event under way before the ack was finished whole.
    assert (await _status(NODE)).undeliverable is (call == "ack")


# ── Reading a pulled body ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (base64.b64encode(b'{"type": "x"}').decode(), {"type": "x"}),
        ('{"type": "x"}', {"type": "x"}),
        ('  {"type": "x"}', {"type": "x"}),
        ({"type": "x"}, {"type": "x"}),
    ],
    ids=["json-as-base64", "text", "text-with-space", "already-an-object"],
)
def test_a_body_is_read_however_it_was_published(body, expected):
    assert mail_events._decode(body) == expected


@pytest.mark.parametrize("body", ["", "%%%", base64.b64encode(b"\xff\xfe not json").decode(), None, 12])
def test_an_unreadable_body_raises_value_error(body):
    with pytest.raises(ValueError):
        mail_events._decode(body)
