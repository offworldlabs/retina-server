"""The mail outbox: queueing, sending, settling, and what survives a crash.

Every test runs against its own migrated database file, copied from the suite's
`alembic upgrade head` template, with the outbox key beside it in the same
temporary directory. The tests that need a second process to be a real second
process, a restart, a crash, two workers and the key race, run Python
subprocesses against that file, and those find the key the way the app does:
beside RETINA_DB_PATH.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.mail_outbox import MailOutbox
from core.users import MagicLink, _set_sqlite_pragmas
from services import mail, mail_outbox
from services.mail_outbox import KIND_CLAIM, KIND_SIGN_IN, Settled
from tests.migration_helpers import BACKEND

T0 = 1_700_000_000.0
LINK_LIFE_S = 900.0
NODE = "ret1a2b3c4d"
OTHER_NODE = "ret9f8e7d6c"
HANDLE = hashlib.sha256(b"a claim challenge").hexdigest()
ADA = "ada@example.com"
BOB = "bob@example.com"

_derived_key_path = mail_outbox._key_path


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """The key beside this test's database, no handlers left over from another
    test, and no worker registered."""
    monkeypatch.setattr(mail_outbox, "_key_path", lambda: tmp_path / mail_outbox.KEY_FILE_NAME)
    monkeypatch.setattr(mail_outbox, "_fernets", {})
    monkeypatch.setattr(mail_outbox, "_handlers", {})
    monkeypatch.setattr(mail_outbox, "_worker", None)


@pytest.fixture
def outbox_db(tmp_path, _node_schema_template) -> Path:
    db = tmp_path / "users.db"
    shutil.copyfile(_node_schema_template, db)
    return db


@pytest.fixture
async def sessions(outbox_db):
    engine = create_async_engine(f"sqlite+aiosqlite:///{outbox_db}", connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settled() -> list[Settled]:
    """Every settlement, of either kind, in order."""
    calls: list[Settled] = []

    async def record(_session, outcome: Settled) -> None:
        calls.append(outcome)

    mail_outbox.on_settled(KIND_SIGN_IN, record)
    mail_outbox.on_settled(KIND_CLAIM, record)
    return calls


class Transport:
    """Answers with `deliveries` in turn, repeating the last, and keeps what it was handed."""

    def __init__(self, *deliveries: mail.Delivery) -> None:
        self.deliveries = list(deliveries)
        self.sent: list[mail.Message] = []

    def __call__(self, message: mail.Message) -> mail.Delivery:
        self.sent.append(message)
        return self.deliveries.pop(0) if len(self.deliveries) > 1 else self.deliveries[0]


def _message(token: str = "a-token", *, to: str = ADA, subject: str = "Connect your RETINA node") -> mail.Message:
    link = f"https://app.example.com/auth/claim/{token}"
    return mail.Message(to=to, subject=subject, text=f"Open {link}", html=f'<a href="{link}">Connect</a>')


async def _queue(
    sessions,
    message: mail.Message | None = None,
    *,
    kind: str = KIND_CLAIM,
    node_id: str | None = NODE,
    now: float = T0,
    expires_at: float | None = None,
) -> int:
    async with sessions() as session:
        row_id = await mail_outbox.enqueue(
            session,
            message or _message(),
            kind=kind,
            expires_at=now + LINK_LIFE_S if expires_at is None else expires_at,
            node_id=node_id,
            handle=HANDLE if kind == KIND_CLAIM else None,
            now=now,
        )
        await session.commit()
    return row_id


def _row(db: Path, row_id: int) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM mail_outbox WHERE id = ?", (row_id,)).fetchone())
    finally:
        con.close()


def _query(db: Path, sql: str, *params) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _execute(db: Path, sql: str, *params) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


async def _run(sessions, transport, *, now: float = T0, owner: str = "worker-a") -> bool:
    return await mail_outbox.run_once(sessions=sessions, owner=owner, now=now, transport=transport)


# ── Backoff ──────────────────────────────────────────────────────────────────


def test_backoff_doubles_from_ten_seconds_up_to_five_minutes():
    schedule = [mail_outbox.backoff_s(attempts, jitter=1.0) for attempts in range(1, 10)]
    assert schedule == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0, 300.0]


def test_backoff_jitter_stays_within_a_fifth_either_side_and_varies():
    for attempts in range(1, 9):
        nominal = mail_outbox.backoff_s(attempts, jitter=1.0)
        draws = [mail_outbox.backoff_s(attempts) for _ in range(200)]
        assert all(0.8 * nominal <= wait <= 1.2 * nominal for wait in draws)
        assert len(set(draws)) > 1


def test_retry_after_is_a_floor_and_never_shortens_the_backoff():
    assert mail_outbox.backoff_s(1, 45.0, jitter=1.0) == 45.0
    assert mail_outbox.backoff_s(3, 5.0, jitter=1.0) == 40.0
    assert mail_outbox.backoff_s(1, None, jitter=1.2) == pytest.approx(12.0)


async def test_a_retry_is_rescheduled_by_the_backoff_with_the_body_kept(sessions, outbox_db, settled):
    row_id = await _queue(sessions)
    transport = Transport(mail.Delivery(mail.RETRY, "HTTP 503: busy"))

    assert await _run(sessions, transport)

    row = _row(outbox_db, row_id)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert T0 + 8.0 <= row["next_attempt_at"] <= T0 + 12.0
    assert row["lease_owner"] is None and row["lease_until"] is None
    assert row["last_error"] == "HTTP 503: busy"
    assert row["body"] is not None
    assert settled == []
    # Not due again until the backoff is over.
    assert not await _run(sessions, transport, now=T0 + 1.0)
    assert len(transport.sent) == 1


async def test_retry_after_from_the_provider_sets_the_next_attempt(sessions, outbox_db):
    row_id = await _queue(sessions)

    assert await _run(sessions, Transport(mail.Delivery(mail.RETRY, "HTTP 429", retry_after_s=120.0)))

    assert _row(outbox_db, row_id)["next_attempt_at"] == T0 + 120.0


async def test_the_retries_follow_the_schedule_until_the_attempt_cap_fails_the_row(sessions, outbox_db, settled):
    row_id = await _queue(sessions, expires_at=T0 + 10 * 86400)
    transport = Transport(mail.Delivery(mail.RETRY, "HTTP 500: upstream"))

    now = T0
    waits = []
    for _ in range(mail_outbox.MAX_ATTEMPTS - 1):
        assert await _run(sessions, transport, now=now)
        due = _row(outbox_db, row_id)["next_attempt_at"]
        waits.append(due - now)
        now = due
    assert await _run(sessions, transport, now=now)

    nominal = [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    assert all(0.8 * n <= w <= 1.2 * n for n, w in zip(nominal, waits)), waits
    row = _row(outbox_db, row_id)
    assert (row["status"], row["outcome"], row["attempts"]) == ("failed", "failed", 8)
    assert row["body"] is None
    assert len(transport.sent) == 8
    [outcome] = settled
    assert outcome.outcome == "failed"
    assert outcome.detail == "gave up after 8 attempts: HTTP 500: upstream"
    assert not await _run(sessions, transport, now=now + 86400)


async def test_a_row_whose_sends_never_reported_back_fails_without_another_send(sessions, outbox_db, settled):
    """Each claim counts as an attempt whether or not the worker lived to
    record it, so a message that kills its worker every time still ends."""
    row_id = await _queue(sessions)
    _execute(
        outbox_db,
        "UPDATE mail_outbox SET attempts = ?, lease_owner = 'gone', lease_until = ? WHERE id = ?",
        8,
        T0,
        row_id,
    )
    transport = Transport(mail.Delivery(mail.SENT))

    assert await _run(sessions, transport, now=T0 + 1.0)

    assert transport.sent == []
    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["body"]) == ("failed", 9, None)
    assert [s.outcome for s in settled] == ["failed"]


# ── Expiry ───────────────────────────────────────────────────────────────────


async def test_a_retry_that_would_land_in_the_links_last_minute_expires_instead(sessions, outbox_db, settled):
    row_id = await _queue(sessions, expires_at=T0 + 65.0)
    transport = Transport(mail.Delivery(mail.RETRY, "HTTP 503"))

    assert await _run(sessions, transport)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["body"]) == ("expired", None)
    [outcome] = settled
    assert outcome.outcome == "expired"
    assert "HTTP 503" in outcome.detail
    assert len(transport.sent) == 1


async def test_a_retry_after_past_the_links_life_expires_the_row(sessions, outbox_db, settled):
    row_id = await _queue(sessions)

    assert await _run(sessions, Transport(mail.Delivery(mail.RETRY, "HTTP 429", retry_after_s=LINK_LIFE_S)))

    assert _row(outbox_db, row_id)["status"] == "expired"
    assert [s.outcome for s in settled] == ["expired"]


async def test_a_link_with_under_a_minute_left_is_never_sent(sessions, outbox_db, settled):
    row_id = await _queue(sessions, expires_at=T0 + 59.0)
    transport = Transport(mail.Delivery(mail.SENT))

    assert await _run(sessions, transport)

    assert transport.sent == []
    assert (_row(outbox_db, row_id)["status"], _row(outbox_db, row_id)["body"]) == ("expired", None)
    assert [s.outcome for s in settled] == ["expired"]


# ── Settlement and handlers ──────────────────────────────────────────────────


_WORD = "the provider's word"


@pytest.mark.parametrize(
    ("outcome", "delivery", "expires_at", "detail", "message_id"),
    [
        ("sent", mail.Delivery(mail.SENT, _WORD, message_id="msg-1"), T0 + LINK_LIFE_S, _WORD, "msg-1"),
        ("undeliverable", mail.Delivery(mail.UNDELIVERABLE, _WORD), T0 + LINK_LIFE_S, _WORD, None),
        ("rejected", mail.Delivery(mail.REJECTED, _WORD), T0 + LINK_LIFE_S, _WORD, None),
        (
            "failed",
            mail.Delivery("bogus"),
            T0 + LINK_LIFE_S,
            "the transport answered 'bogus', which is not an outcome",
            None,
        ),
        (
            "expired",
            mail.Delivery(mail.SENT),
            T0 + 30.0,
            "the link had under a minute to live by the time it could be sent",
            None,
        ),
    ],
)
async def test_each_outcome_settles_with_its_handler_in_the_same_transaction(
    sessions, outbox_db, outcome, delivery, expires_at, detail, message_id
):
    row_id = await _queue(sessions, expires_at=expires_at)
    seen = []

    async def handler(session, settled: Settled) -> None:
        # This session already sees the row settled and its body gone...
        inside = (
            await session.execute(select(MailOutbox.status, MailOutbox.body).where(MailOutbox.id == row_id))
        ).one()
        # ...while nobody else does yet: it is one transaction, still open.
        outside = _row(outbox_db, row_id)
        seen.append((settled, tuple(inside), outside["status"], outside["body"] is not None))
        session.add(
            MagicLink(
                token_hash="0" * 64,
                email=settled.recipient,
                intent="claim",
                node_id=settled.node_id,
                created_at=T0,
                expires_at=T0 + 1,
            )
        )

    mail_outbox.on_settled(KIND_CLAIM, handler)

    assert await _run(sessions, Transport(delivery))

    [(settled, inside, outside_status, outside_has_body)] = seen
    assert settled == Settled(
        kind=KIND_CLAIM, recipient=ADA, node_id=NODE, handle=HANDLE, outcome=outcome, detail=detail
    )
    assert (settled.kind, settled.recipient, settled.node_id, settled.handle) == (KIND_CLAIM, ADA, NODE, HANDLE)
    assert inside == (outcome, None)
    assert (outside_status, outside_has_body) == ("queued", True)
    row = _row(outbox_db, row_id)
    assert (row["status"], row["outcome"], row["body"], row["settled_at"]) == (outcome, outcome, None, T0)
    assert (row["message_id"], row["lease_owner"], row["lease_until"]) == (message_id, None, None)
    # The handler's write committed with the settlement.
    assert _query(outbox_db, "SELECT email, node_id FROM magic_links") == [(ADA, NODE)]


async def test_a_kind_with_no_handler_still_settles(sessions, outbox_db):
    row_id = await _queue(sessions, kind=KIND_SIGN_IN, node_id=None)

    assert await _run(sessions, Transport(mail.Delivery(mail.SENT, message_id="m")))

    assert _row(outbox_db, row_id)["status"] == "sent"


async def test_a_sent_mail_is_logged_by_its_row_without_its_recipient(sessions, outbox_db, caplog):
    """Every mail that goes out writes this line, at INFO, which production logs."""
    caplog.set_level(logging.INFO, logger="services.mail_outbox")
    row_id = await _queue(sessions)

    assert await _run(sessions, Transport(mail.Delivery(mail.SENT, message_id="msg-1")))

    lines = [r for r in caplog.records if r.name == "services.mail_outbox"]
    assert [(r.levelno, r.getMessage()) for r in lines] == [(logging.INFO, f"mail_outbox: row {row_id} (claim) sent")]
    assert ADA not in caplog.text


async def test_a_mail_that_did_not_go_is_logged_with_its_recipient(sessions, outbox_db, caplog):
    """A warning is rare and asks for someone to look, and the address says whose mail it was."""
    caplog.set_level(logging.INFO, logger="services.mail_outbox")
    row_id = await _queue(sessions)

    assert await _run(sessions, Transport(mail.Delivery(mail.UNDELIVERABLE, "no such mailbox")))

    [line] = [r for r in caplog.records if r.name == "services.mail_outbox"]
    assert line.levelno == logging.WARNING
    assert line.getMessage() == f"mail_outbox: row {row_id} (claim to {ADA}) settled as undeliverable: no such mailbox"


async def test_a_raising_handler_rolls_back_and_the_settlement_is_retried_without_a_resend(sessions, outbox_db, caplog):
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    row_id = await _queue(sessions)

    async def broken(session, settled: Settled) -> None:
        session.add(
            MagicLink(token_hash="1" * 64, email=settled.recipient, intent="claim", created_at=T0, expires_at=T0 + 1)
        )
        await session.flush()
        raise RuntimeError("the handler's own bug")

    mail_outbox.on_settled(KIND_CLAIM, broken)
    transport = Transport(mail.Delivery(mail.SENT, "accepted", message_id="msg-1"))

    assert await _run(sessions, transport)

    row = _row(outbox_db, row_id)
    # Rolled back together: neither the settlement nor the handler's write landed.
    assert _query(outbox_db, "SELECT COUNT(*) FROM magic_links") == [(0,)]
    # What delivery came to is kept, the body is gone, and the row waits out a backoff.
    assert (row["status"], row["outcome"], row["message_id"], row["body"]) == ("settling", "sent", "msg-1", None)
    assert T0 + 8.0 <= row["next_attempt_at"] <= T0 + 12.0
    assert row["lease_owner"] is None
    assert "the claim handler raised" in caplog.text

    fixed: list[Settled] = []

    async def mended(session, settled: Settled) -> None:
        fixed.append(settled)
        session.add(
            MagicLink(token_hash="2" * 64, email=settled.recipient, intent="claim", created_at=T0, expires_at=T0 + 1)
        )

    mail_outbox.on_settled(KIND_CLAIM, mended)
    assert not await _run(sessions, transport, now=T0 + 1.0)
    assert await _run(sessions, transport, now=row["next_attempt_at"])

    assert len(transport.sent) == 1
    assert [(s.outcome, s.detail) for s in fixed] == [("sent", "accepted")]
    row = _row(outbox_db, row_id)
    assert (row["status"], row["outcome"], row["message_id"], row["body"]) == ("sent", "sent", "msg-1", None)
    assert _query(outbox_db, "SELECT token_hash FROM magic_links") == [("2" * 64,)]


class _Raises:
    """A handler that raises `error` on its first `failures` runs, or on every
    run when that is None, and counts its runs. A run that gets through adds a
    link, so its write can be looked for."""

    def __init__(self, error: Exception, failures: int | None = None) -> None:
        self.error = error
        self.failures = failures
        self.runs = 0

    async def __call__(self, session, settled: Settled) -> None:
        self.runs += 1
        if self.failures is None or self.runs <= self.failures:
            raise self.error
        session.add(
            MagicLink(token_hash="3" * 64, email=settled.recipient, intent="claim", created_at=T0, expires_at=T0 + 1)
        )


async def _settle_until_final(sessions, outbox_db, row_id: int, transport, *, now: float, caplog=None) -> list[dict]:
    """Run the row's passes, each when it falls due, until it leaves settling.
    Returns each pass's time, the row after it, and what it logged."""
    passes = []
    for _ in range(4 * mail_outbox.MAX_ATTEMPTS):
        if caplog is not None:
            caplog.clear()
        assert await _run(sessions, transport, now=now)
        row = _row(outbox_db, row_id)
        lines = [] if caplog is None else [r for r in caplog.records if r.name == "services.mail_outbox"]
        passes.append({"at": now, "row": row, "lines": lines})
        if row["status"] != "settling":
            break
        now = row["next_attempt_at"]
    return passes


_GAVE_UP = "the claim handler never completed (RuntimeError), so the row settled without it"
_EXPIRED_DETAIL = "the link had under a minute to live by the time it could be sent"


@pytest.mark.parametrize(
    ("outcome", "delivery", "expires_at", "message_id", "sends"),
    [
        ("sent", mail.Delivery(mail.SENT, "accepted", message_id="msg-1"), T0 + LINK_LIFE_S, "msg-1", 1),
        ("undeliverable", mail.Delivery(mail.UNDELIVERABLE, "no such mailbox"), T0 + LINK_LIFE_S, None, 1),
        # Its link was dead before the handler first ran, and it still gets its passes.
        ("expired", mail.Delivery(mail.SENT), T0 + 30.0, None, 0),
    ],
)
async def test_a_handler_that_keeps_raising_is_given_up_with_the_outcome_its_mail_had(
    sessions, outbox_db, caplog, outcome, delivery, expires_at, message_id, sends
):
    """Retried with the usual backoff, past the link's death, until the row has
    been taken MAX_SETTLE_ATTEMPTS times; then settled once, as what delivery
    came to and never as failed, without the handler, and kept and purged like
    any settled row."""
    caplog.set_level(logging.INFO, logger="services.mail_outbox")
    row_id = await _queue(sessions, expires_at=expires_at)
    handler = _Raises(RuntimeError("still broken"))
    mail_outbox.on_settled(KIND_CLAIM, handler)
    transport = Transport(delivery)

    passes = await _settle_until_final(sessions, outbox_db, row_id, transport, now=T0, caplog=caplog)

    final = passes[-1]
    row, final_at = final["row"], final["at"]
    # Given up rather than left settling, as what the mail came to.
    assert row["status"] == outcome
    assert (row["outcome"], row["message_id"], row["body"]) == (outcome, message_id, None)
    assert (row["settled_at"], row["lease_owner"], row["lease_until"]) == (final_at, None, None)
    detail = delivery.detail if outcome != "expired" else _EXPIRED_DETAIL
    assert row["last_error"] == f"{_GAVE_UP}: {detail}"
    # Every pass short of the bound settled again later, those after the link died included.
    assert row["attempts"] == mail_outbox.MAX_SETTLE_ATTEMPTS == 17
    assert [p["row"]["status"] for p in passes[:-1]] == ["settling"] * 16
    assert any(p["at"] >= expires_at for p in passes[:-1])
    assert final_at >= expires_at
    assert (handler.runs, len(transport.sent)) == (17, sends)
    for attempts, (before, after) in enumerate(zip(passes, passes[1:]), start=1):
        nominal = mail_outbox.backoff_s(attempts, jitter=1.0)
        assert 0.8 * nominal <= after["at"] - before["at"] <= 1.2 * nominal
    # One error says so, by row and not by address; the passes before it said
    # they would settle again later.
    again = f"the claim handler raised settling row {row_id} (to {ADA}) as {outcome}; settling it again later"
    expected = [(logging.ERROR, f"mail_outbox: {again}")]
    assert all([(r.levelno, r.getMessage()) for r in p["lines"]] == expected for p in passes[:-1])
    [line] = final["lines"]
    assert (line.levelno, line.getMessage()) == (
        logging.ERROR,
        f"mail_outbox: the claim handler never completed for row {row_id}, so it is settled as {outcome} without it",
    )
    logged = logging.Formatter().format(line)
    assert "RuntimeError: still broken" in logged
    assert ADA not in logged

    # Never sent or settled again, nothing left to wait for, and purged a week on.
    assert not await _run(sessions, transport, now=final_at + 86400)
    assert (handler.runs, len(transport.sent)) == (17, sends)
    assert await mail_outbox._until_due(sessions, final_at) == mail_outbox.IDLE_WAIT_S
    assert await mail_outbox._purge(sessions, final_at + mail_outbox.KEEP_SETTLED_S - 1) == 0
    assert await mail_outbox._purge(sessions, final_at + mail_outbox.KEEP_SETTLED_S + 1) == 1
    assert _query(outbox_db, "SELECT COUNT(*) FROM mail_outbox") == [(0,)]


async def test_a_raising_handler_is_never_given_up_while_the_link_lives(sessions, outbox_db):
    """However many passes it has had: until the link dies, what the handler
    does is still what the node reads."""
    expires_at = T0 + 30 * 86400
    row_id = await _queue(sessions, expires_at=expires_at)
    handler = _Raises(RuntimeError("still broken"))
    mail_outbox.on_settled(KIND_CLAIM, handler)
    transport = Transport(mail.Delivery(mail.UNDELIVERABLE, "no such mailbox"))

    now = T0
    for _ in range(3 * mail_outbox.MAX_ATTEMPTS):
        assert await _run(sessions, transport, now=now)
        now = _row(outbox_db, row_id)["next_attempt_at"]
    assert await _run(sessions, transport, now=expires_at - 1.0)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["body"]) == ("settling", 25, None)

    # The first pass after the link has died is the last.
    last_at = row["next_attempt_at"]
    assert last_at > expires_at
    assert await _run(sessions, transport, now=last_at)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["settled_at"]) == ("undeliverable", 26, last_at)
    assert row["last_error"] == f"{_GAVE_UP}: no such mailbox"
    assert (handler.runs, len(transport.sent)) == (26, 1)


async def test_a_raising_handler_on_a_mail_that_used_every_attempt_still_gets_its_passes(sessions, outbox_db):
    """The latest a handler first runs is on the claim after the last send, so
    the bound leaves it MAX_ATTEMPTS more passes even then."""
    row_id = await _queue(sessions)
    _execute(
        outbox_db,
        "UPDATE mail_outbox SET attempts = ?, lease_owner = 'gone', lease_until = ? WHERE id = ?",
        mail_outbox.MAX_ATTEMPTS,
        T0,
        row_id,
    )
    handler = _Raises(RuntimeError("still broken"))
    mail_outbox.on_settled(KIND_CLAIM, handler)
    transport = Transport(mail.Delivery(mail.SENT))

    passes = await _settle_until_final(sessions, outbox_db, row_id, transport, now=T0 + 1.0)

    row = passes[-1]["row"]
    assert row["status"] == "failed"
    assert row["last_error"] == f"{_GAVE_UP}: gave up after 8 attempts, the last of which never reported back"
    assert (row["attempts"], row["body"], transport.sent) == (17, None, [])
    assert handler.runs == 1 + mail_outbox.MAX_ATTEMPTS


async def test_giving_a_handler_up_is_fenced_like_any_settlement(sessions, outbox_db, monkeypatch, caplog):
    """A worker whose lease ran out while it was giving the handler up records
    nothing, and the one that took the row over gives it up, once."""
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    row_id = await _queue(sessions)
    after_the_link = T0 + LINK_LIFE_S
    _execute(
        outbox_db,
        "UPDATE mail_outbox SET status = 'settling', outcome = 'sent', body = NULL, attempts = ?, next_attempt_at = ?"
        " WHERE id = ?",
        mail_outbox.MAX_SETTLE_ATTEMPTS - 1,
        after_the_link,
        row_id,
    )
    handler = _Raises(RuntimeError("still broken"))
    mail_outbox.on_settled(KIND_CLAIM, handler)
    later = after_the_link + mail_outbox.LEASE_S + 1.0
    give_up = mail_outbox._give_up_handler
    taken: list = []

    async def taken_over_first(sessions, claim, **kwargs) -> None:
        if not taken:
            taken.append(await mail_outbox._claim(sessions, "worker-b", later))
        await give_up(sessions, claim, **kwargs)

    monkeypatch.setattr(mail_outbox, "_give_up_handler", taken_over_first)
    transport = Transport(mail.Delivery(mail.SENT))

    assert await _run(sessions, transport, now=after_the_link, owner="worker-a")

    row = _row(outbox_db, row_id)
    assert (row["status"], row["lease_owner"], row["settled_at"]) == ("settling", "worker-b", None)
    assert "taken over or cancelled" in caplog.text
    assert "never completed" not in caplog.text

    [claim] = taken
    await mail_outbox._process(sessions, claim, lambda: later, transport)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["settled_at"]) == ("sent", mail_outbox.MAX_SETTLE_ATTEMPTS + 1, later)
    # No word from the provider to keep, so the reason is all there is.
    assert row["last_error"] == _GAVE_UP
    assert caplog.text.count("never completed") == 1
    assert (handler.runs, transport.sent) == (2, [])


async def test_a_handler_that_recovers_after_the_link_has_died_settles_with_its_effect(sessions, outbox_db, caplog):
    """A lock that outlasts the link: the passes left after its death are what
    let the handler's work land at all, and the row settles as it always has."""
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    row_id = await _queue(sessions)
    locked = OperationalError("UPDATE node_claims", None, sqlite3.OperationalError("database is locked"))
    handler = _Raises(locked, failures=10)
    mail_outbox.on_settled(KIND_CLAIM, handler)
    transport = Transport(mail.Delivery(mail.SENT, "accepted", message_id="msg-1"))

    passes = await _settle_until_final(sessions, outbox_db, row_id, transport, now=T0)

    row, final_at = passes[-1]["row"], passes[-1]["at"]
    assert final_at > T0 + LINK_LIFE_S
    assert (row["status"], row["outcome"], row["message_id"], row["body"]) == ("sent", "sent", "msg-1", None)
    assert (row["attempts"], row["settled_at"], row["last_error"]) == (11, final_at, "accepted")
    assert (handler.runs, len(transport.sent)) == (11, 1)
    # The handler's write landed with the settlement.
    assert _query(outbox_db, "SELECT token_hash FROM magic_links") == [("3" * 64,)]
    assert "never completed" not in caplog.text


async def test_an_outcome_the_outbox_does_not_know_fails_rather_than_risk_a_second_send(sessions, outbox_db, settled):
    row_id = await _queue(sessions)

    assert await _run(sessions, Transport(mail.Delivery("maybe")))

    assert _row(outbox_db, row_id)["status"] == "failed"
    assert settled[0].detail == "the transport answered 'maybe', which is not an outcome"


async def test_a_transport_that_raises_is_retried_and_its_message_kept_out_of_the_logs(sessions, outbox_db, caplog):
    caplog.set_level(logging.DEBUG, logger="services.mail_outbox")
    token = secrets.token_urlsafe(32)
    row_id = await _queue(sessions, _message(token))

    def raises(message: mail.Message) -> mail.Delivery:
        raise ValueError(f"could not send {message.text}")

    assert await _run(sessions, raises)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["last_error"]) == ("queued", "the transport raised ValueError")
    assert "ValueError" in caplog.text
    assert token not in caplog.text


# ── Cancel ───────────────────────────────────────────────────────────────────


async def test_cancel_stops_the_nodes_queued_claim_mail_and_nothing_else(sessions, outbox_db, settled):
    already_sent = await _queue(sessions, _message("sent-one"))
    assert await _run(sessions, Transport(mail.Delivery(mail.SENT)))
    settled.clear()
    first = await _queue(sessions, _message("one"))
    second = await _queue(sessions, _message("two"))
    other_node = await _queue(sessions, _message("three", to=BOB), node_id=OTHER_NODE)
    sign_in = await _queue(sessions, _message("four"), kind=KIND_SIGN_IN, node_id=NODE)

    async with sessions() as session:
        assert await mail_outbox.cancel(session, node_id=NODE) == 2
        await session.commit()

    for row_id in (first, second):
        row = _row(outbox_db, row_id)
        assert (row["status"], row["body"]) == ("cancelled", None)
        assert row["settled_at"] is not None
    assert _row(outbox_db, already_sent)["status"] == "sent"
    transport = Transport(mail.Delivery(mail.SENT))
    while await _run(sessions, transport):
        pass
    assert sorted(m.text for m in transport.sent) == sorted([_message("three").text, _message("four").text])
    assert {_row(outbox_db, other_node)["status"], _row(outbox_db, sign_in)["status"]} == {"sent"}
    # No handler for a cancellation; only the two that went.
    assert sorted(s.outcome for s in settled) == ["sent", "sent"]


async def test_a_renomination_cancels_the_old_mail_and_queues_the_new_in_one_transaction(sessions, outbox_db):
    """How a node that changes its address is handled: the old link's mail
    stops and the new one's is queued, or neither, never one without the other."""
    old = await _queue(sessions, _message("old-link"))

    async with sessions() as session:
        assert await mail_outbox.cancel(session, node_id=NODE) == 1
        new = await mail_outbox.enqueue(
            session, _message("new-link", to=BOB), kind=KIND_CLAIM, expires_at=T0 + LINK_LIFE_S, node_id=NODE, now=T0
        )
        await session.commit()

    assert (_row(outbox_db, old)["status"], _row(outbox_db, new)["status"]) == ("cancelled", "queued")
    transport = Transport(mail.Delivery(mail.SENT))
    while await _run(sessions, transport):
        pass
    assert [m.to for m in transport.sent] == [BOB]


async def test_cancel_is_the_callers_to_commit(sessions, outbox_db):
    row_id = await _queue(sessions)

    async with sessions() as session:
        assert await mail_outbox.cancel(session, node_id=NODE) == 1
        await session.rollback()

    assert _row(outbox_db, row_id)["status"] == "queued"


async def test_a_cancel_during_the_send_lets_it_finish_and_records_nothing(sessions, outbox_db, settled, caplog):
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    row_id = await _queue(sessions)
    loop = asyncio.get_running_loop()

    async def cancel() -> int:
        async with sessions() as session:
            cancelled = await mail_outbox.cancel(session, node_id=NODE)
            await session.commit()
        return cancelled

    def transport(_message: mail.Message) -> mail.Delivery:
        assert asyncio.run_coroutine_threadsafe(cancel(), loop).result(timeout=10) == 1
        return mail.Delivery(mail.SENT, message_id="msg-1")

    assert await _run(sessions, transport)

    row = _row(outbox_db, row_id)
    assert (row["status"], row["message_id"], row["body"]) == ("cancelled", None, None)
    assert settled == []
    assert "taken over or cancelled" in caplog.text


async def test_enqueue_is_part_of_the_callers_transaction(sessions, outbox_db):
    async with sessions() as session:
        await mail_outbox.enqueue(session, _message(), kind=KIND_CLAIM, expires_at=T0 + LINK_LIFE_S, now=T0)
        await session.rollback()

    assert _query(outbox_db, "SELECT COUNT(*) FROM mail_outbox") == [(0,)]


async def test_an_unknown_kind_is_refused(sessions):
    async with sessions() as session:
        with pytest.raises(ValueError, match="unknown mail kind"):
            await mail_outbox.enqueue(session, _message(), kind="signin", expires_at=T0 + LINK_LIFE_S)
        with pytest.raises(ValueError, match="unknown mail kind"):
            await mail_outbox.cancel(session, node_id=NODE, kind="claims")

    async def handler(_session, _settled) -> None:
        pass

    with pytest.raises(ValueError, match="unknown mail kind"):
        mail_outbox.on_settled("sign-in", handler)


# ── At rest ──────────────────────────────────────────────────────────────────


def _database_bytes(db: Path) -> bytes:
    return b"".join(path.read_bytes() for path in (db, Path(f"{db}-wal")) if path.exists())


async def test_the_link_is_never_in_the_database_and_the_ciphertext_is_wiped_on_settling(
    sessions, outbox_db, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG, logger="services.mail_outbox")
    token = secrets.token_urlsafe(32)
    row_id = await _queue(sessions, _message(token))

    assert token.encode() not in _database_bytes(outbox_db)
    row = _row(outbox_db, row_id)
    assert row["body"] is not None and token not in row["body"]
    assert all(token not in str(value) for value in row.values())
    # What is there is the message itself, under the key beside the database.
    key = (tmp_path / mail_outbox.KEY_FILE_NAME).read_bytes()
    assert token in json.loads(Fernet(key).decrypt(row["body"].encode()))["text"]

    transport = Transport(mail.Delivery(mail.RETRY, "HTTP 503"), mail.Delivery(mail.SENT, message_id="m"))
    assert await _run(sessions, transport)
    assert await _run(sessions, transport, now=_row(outbox_db, row_id)["next_attempt_at"])

    assert [m.text for m in transport.sent] == [_message(token).text] * 2
    assert transport.sent[0] == _message(token)
    assert _row(outbox_db, row_id)["body"] is None
    assert token.encode() not in _database_bytes(outbox_db)
    assert token not in caplog.text


async def test_any_text_comes_back_exactly_as_it_was_queued(sessions):
    message = mail.Message(
        to=ADA, subject="Café — ünïcode ✓", text='Zażółć gęślą jaźń\n\t"quoted"', html="<p>日本語 🚀</p>"
    )
    await _queue(sessions, message)
    transport = Transport(mail.Delivery(mail.SENT))

    assert await _run(sessions, transport)

    assert transport.sent == [message]


async def test_a_body_that_cannot_be_decrypted_fails_the_row_without_stopping_the_worker(
    sessions, outbox_db, tmp_path, monkeypatch, settled
):
    lost = await _queue(sessions, _message("under-the-old-key"))
    # The database restored somewhere the key did not follow.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(mail_outbox, "_key_path", lambda: elsewhere / mail_outbox.KEY_FILE_NAME)
    fresh = await _queue(sessions, _message("under-the-new-key"), node_id=OTHER_NODE)
    transport = Transport(mail.Delivery(mail.SENT))

    assert await _run(sessions, transport)
    assert await _run(sessions, transport)

    assert [m.text for m in transport.sent] == [_message("under-the-new-key").text]
    row = _row(outbox_db, lost)
    assert (row["status"], row["body"]) == ("failed", None)
    assert "cannot be decrypted" in row["last_error"]
    assert _row(outbox_db, fresh)["status"] == "sent"
    assert [(s.node_id, s.outcome) for s in settled] == [(NODE, "failed"), (OTHER_NODE, "sent")]
    assert "cannot be decrypted" in settled[0].detail


async def test_a_body_that_is_not_ciphertext_or_is_for_someone_else_fails_the_row(sessions, outbox_db, settled):
    garbage = await _queue(sessions)
    _execute(outbox_db, "UPDATE mail_outbox SET body = 'not a token' WHERE id = ?", garbage)
    for_ada = await _queue(sessions)
    for_bob = await _queue(sessions, _message(to=BOB))
    # A body moved onto another person's row.
    _execute(
        outbox_db,
        "UPDATE mail_outbox SET body = (SELECT body FROM mail_outbox WHERE id = ?) WHERE id = ?",
        for_ada,
        for_bob,
    )
    transport = Transport(mail.Delivery(mail.SENT))

    while await _run(sessions, transport):
        pass

    assert _row(outbox_db, garbage)["status"] == "failed"
    assert "cannot be decrypted" in _row(outbox_db, garbage)["last_error"]
    assert _row(outbox_db, for_bob)["status"] == "failed"
    assert "someone other than" in _row(outbox_db, for_bob)["last_error"]
    assert [m.to for m in transport.sent] == [ADA]


async def test_a_key_file_that_is_not_a_key_refuses_new_mail_and_fails_queued_mail(
    sessions, outbox_db, tmp_path, monkeypatch, settled
):
    row_id = await _queue(sessions)
    broken = tmp_path / "broken" / mail_outbox.KEY_FILE_NAME
    broken.parent.mkdir()
    broken.write_text("not a key")
    monkeypatch.setattr(mail_outbox, "_key_path", lambda: broken)

    async with sessions() as session:
        with pytest.raises(mail_outbox.OutboxKeyError):
            await mail_outbox.enqueue(session, _message(), kind=KIND_CLAIM, expires_at=T0 + LINK_LIFE_S, now=T0)
    assert await _run(sessions, Transport(mail.Delivery(mail.SENT)))

    assert _row(outbox_db, row_id)["status"] == "failed"
    assert "does not hold a Fernet key" in settled[0].detail
    assert broken.read_text() == "not a key"


# ── The key file ─────────────────────────────────────────────────────────────


def test_the_key_path_is_beside_the_database_file(monkeypatch):
    monkeypatch.setattr(mail_outbox, "DATABASE_URL", "sqlite+aiosqlite:////srv/backend/data/users.db")
    assert _derived_key_path() == Path("/srv/backend/data/mail-outbox.key")

    monkeypatch.setattr(mail_outbox, "DATABASE_URL", "sqlite+aiosqlite://")
    with pytest.raises(mail_outbox.OutboxKeyError):
        _derived_key_path()


async def test_the_key_file_is_created_once_private_and_kept(sessions, tmp_path):
    key_file = tmp_path / mail_outbox.KEY_FILE_NAME
    assert not key_file.exists()

    await _queue(sessions)
    key = key_file.read_bytes()
    made = key_file.stat()
    assert stat.S_IMODE(made.st_mode) == 0o600
    Fernet(key)

    mail_outbox._fernets.clear()
    await _queue(sessions, _message("again"))

    assert key_file.read_bytes() == key
    assert key_file.stat().st_ino == made.st_ino
    assert not list(tmp_path.glob(f".{mail_outbox.KEY_FILE_NAME}.*"))


def test_a_process_that_loses_the_race_for_the_key_uses_the_winners(tmp_path, monkeypatch):
    key_file = tmp_path / mail_outbox.KEY_FILE_NAME
    winner = Fernet.generate_key()
    real_link = os.link

    def raced(source, destination):
        # Another process links its key in between this one finding no file
        # and linking its own.
        Path(destination).write_bytes(winner)
        real_link(source, destination)

    monkeypatch.setattr(mail_outbox.os, "link", raced)

    fernet = mail_outbox._load_key(key_file)

    assert key_file.read_bytes() == winner
    assert Fernet(winner).decrypt(fernet.encrypt(b"x")) == b"x"
    assert not list(tmp_path.glob(f".{mail_outbox.KEY_FILE_NAME}.*"))


_KEY_RACE = r"""
import sys, time
from pathlib import Path

from services import mail_outbox

ready, go, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
ready.touch()
deadline = time.monotonic() + 60
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
fernet = mail_outbox._fernet()
print(mail_outbox._key_path())
print(fernet.encrypt(name.encode()).decode())
"""


def _subprocess_env(db: Path) -> dict[str, str]:
    return os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db)}


def _start(script: str, *args: str, db: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=BACKEND,
        env=_subprocess_env(db),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process: subprocess.Popen, timeout: float = 90) -> tuple[int, str, str]:
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        pytest.fail(f"subprocess did not finish:\n{out}\n{err}")
    return process.returncode, out, err


def _await_files(paths: list[Path], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, "the subprocesses never got ready"
        time.sleep(0.01)


def test_processes_starting_together_agree_on_one_key(tmp_path):
    db = tmp_path / "users.db"
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{i}" for i in range(6)]
    processes = [_start(_KEY_RACE, str(path), str(go), f"process {i}", db=db) for i, path in enumerate(ready)]
    _await_files(ready)
    go.touch()
    results = [_finish(process) for process in processes]

    key_file = tmp_path / mail_outbox.KEY_FILE_NAME
    fernet = Fernet(key_file.read_bytes())
    for i, (code, out, err) in enumerate(results):
        assert code == 0, err
        path, ciphertext = out.strip().splitlines()
        assert Path(path) == key_file
        assert fernet.decrypt(ciphertext.encode()) == f"process {i}".encode()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir() if "key" in p.name) == [mail_outbox.KEY_FILE_NAME]


# ── Leases, fencing, and stopping ────────────────────────────────────────────

_CRASH = r"""
import asyncio, os

from services import mail_outbox


def crash(_message):
    os._exit(3)


asyncio.run(mail_outbox.run_once(owner="crashed-worker", transport=crash))
"""


async def test_a_row_held_by_a_worker_that_died_is_taken_again_when_its_lease_runs_out(sessions, outbox_db, settled):
    now = time.time()
    row_id = await _queue(sessions, now=now)

    code, _out, err = _finish(_start(_CRASH, db=outbox_db))
    assert code == 3, err

    row = _row(outbox_db, row_id)
    assert (row["status"], row["lease_owner"], row["attempts"]) == ("queued", "crashed-worker", 1)
    assert row["body"] is not None
    lease_until = row["lease_until"]
    assert now + mail_outbox.LEASE_S <= lease_until <= time.time() + mail_outbox.LEASE_S
    transport = Transport(mail.Delivery(mail.SENT, message_id="m"))

    assert not await _run(sessions, transport, now=lease_until - 1.0, owner="survivor")
    assert await _run(sessions, transport, now=lease_until + 0.001, owner="survivor")

    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["lease_owner"]) == ("sent", 2, None)
    assert len(transport.sent) == 1
    assert [s.outcome for s in settled] == ["sent"]


@pytest.mark.parametrize("stale_outcome", [mail.SENT, mail.RETRY])
async def test_a_worker_whose_lease_ran_out_cannot_overwrite_the_one_that_took_over(
    sessions, outbox_db, settled, caplog, stale_outcome
):
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    row_id = await _queue(sessions)
    loop = asyncio.get_running_loop()
    later = T0 + mail_outbox.LEASE_S + 1.0
    second = Transport(mail.Delivery(mail.SENT, message_id="second"))

    def stalls_past_its_lease(_message: mail.Message) -> mail.Delivery:
        took = mail_outbox.run_once(sessions=sessions, owner="worker-b", now=later, transport=second)
        assert asyncio.run_coroutine_threadsafe(took, loop).result(timeout=10)
        return mail.Delivery(stale_outcome, message_id="first")

    assert await _run(sessions, stalls_past_its_lease, owner="worker-a")

    row = _row(outbox_db, row_id)
    assert (row["status"], row["message_id"], row["attempts"], row["settled_at"]) == ("sent", "second", 2, later)
    assert [s.outcome for s in settled] == ["sent"]
    assert "taken over or cancelled" in caplog.text


async def test_the_same_worker_taking_a_row_twice_is_still_fenced(sessions, outbox_db, settled):
    """The owner alone does not fence: a worker can take its own row again once
    the first lease has run out, and the first claim must not then record over
    the second while that one is still sending."""
    row_id = await _queue(sessions)
    loop = asyncio.get_running_loop()
    later = T0 + mail_outbox.LEASE_S + 1.0

    def stalls(_message: mail.Message) -> mail.Delivery:
        again = mail_outbox._claim(sessions, "worker-a", later)
        assert asyncio.run_coroutine_threadsafe(again, loop).result(timeout=10) is not None
        return mail.Delivery(mail.SENT, message_id="first")

    assert await _run(sessions, stalls, owner="worker-a")

    row = _row(outbox_db, row_id)
    assert (row["status"], row["lease_owner"], row["attempts"]) == ("queued", "worker-a", 2)
    assert (row["message_id"], row["settled_at"]) == (None, None)
    assert row["body"] is not None
    assert settled == []


async def test_stopping_mid_send_waits_for_the_result_and_records_it(sessions, outbox_db, settled):
    row_id = await _queue(sessions)
    started, release = threading.Event(), threading.Event()

    def slow(_message: mail.Message) -> mail.Delivery:
        started.set()
        release.wait(10)
        return mail.Delivery(mail.SENT, message_id="m")

    task = asyncio.create_task(_run(sessions, slow))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _row(outbox_db, row_id)["status"] == "sent"
    assert [s.outcome for s in settled] == ["sent"]


_HOLD_THE_CLAIM = (
    "CREATE TEMP TRIGGER hold BEFORE UPDATE OF lease_owner ON mail_outbox"
    " WHEN NEW.lease_owner IS NOT NULL BEGIN SELECT hold(); END"
)
_HOLD_THE_PURGE = "CREATE TEMP TRIGGER hold BEFORE DELETE ON mail_outbox BEGIN SELECT hold(); END"


def _held_inside(db: Path, trigger: str):
    """An engine whose statements stop where `trigger` fires, until `release` is
    set, with `started` set once one has: a stop can then land mid-statement."""
    started, release = threading.Event(), threading.Event()

    def hold() -> int:
        started.set()
        release.wait(10)
        return 0

    def install(dbapi_connection, _record) -> None:
        dbapi_connection.create_function("hold", 0, hold)
        cursor = dbapi_connection.cursor()
        cursor.execute(trigger)
        cursor.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db}", connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    event.listen(engine.sync_engine, "connect", install)
    return engine, started, release


def _writable_at_once(db: Path) -> bool:
    con = sqlite3.connect(db, timeout=0.1)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


async def test_stopping_mid_claim_finishes_the_claim_and_its_send_and_holds_no_lock(outbox_db, settled):
    """A stop that lands inside the claim's statement waits for it, and for the
    row it took. Cancelled there instead, the statement's connection is dropped
    with its transaction open, and its write lock holds every other writer off
    until that connection happens to be garbage collected."""
    engine, started, release = _held_inside(outbox_db, _HOLD_THE_CLAIM)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    row_id = await _queue(sessions)
    transport = Transport(mail.Delivery(mail.SENT, message_id="m"))

    task = asyncio.create_task(_run(sessions, transport))
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

    assert _writable_at_once(outbox_db)
    assert not stopped_inside
    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["lease_owner"]) == ("sent", 1, None)
    assert len(transport.sent) == 1
    assert [s.outcome for s in settled] == ["sent"]
    await engine.dispose()


async def test_stopping_mid_purge_finishes_it_and_holds_no_lock(outbox_db):
    """The worker purges as it starts, so an app stopped as soon as it started
    stops inside the purge's statement."""
    engine, started, release = _held_inside(outbox_db, _HOLD_THE_PURGE)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    row_id = await _queue(sessions)
    long_ago = time.time() - mail_outbox.KEEP_SETTLED_S - 60
    _execute(outbox_db, "UPDATE mail_outbox SET status = 'sent', settled_at = ? WHERE id = ?", long_ago, row_id)

    task = asyncio.create_task(mail_outbox._work(sessions=sessions, transport=Transport(mail.Delivery(mail.SENT))))
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

    assert _writable_at_once(outbox_db)
    assert not stopped_inside
    assert _query(outbox_db, "SELECT COUNT(*) FROM mail_outbox") == [(0,)]
    await engine.dispose()


async def test_nothing_due_means_nothing_done(sessions, outbox_db):
    transport = Transport(mail.Delivery(mail.SENT))
    assert not await _run(sessions, transport)
    await _queue(sessions, now=T0 + 10.0)
    assert not await _run(sessions, transport)
    assert transport.sent == []


# ── The worker loop ──────────────────────────────────────────────────────────


def test_wake_without_a_worker_does_nothing():
    assert mail_outbox._worker is None
    mail_outbox.wake()


@pytest.fixture
def worker_idle(monkeypatch) -> asyncio.Event:
    """Set each time the worker finishes a pass and is about to wait."""
    idle = asyncio.Event()
    until_due = mail_outbox._until_due

    async def reporting(*args):
        wait = await until_due(*args)
        idle.set()
        return wait

    monkeypatch.setattr(mail_outbox, "_until_due", reporting)
    return idle


def _signalling_transport(delivered: asyncio.Event):
    loop = asyncio.get_running_loop()

    def transport(_message: mail.Message) -> mail.Delivery:
        loop.call_soon_threadsafe(delivered.set)
        return mail.Delivery(mail.SENT)

    return transport


async def test_wake_cuts_the_idle_wait_short_from_a_thread_or_the_loop(sessions, monkeypatch, worker_idle):
    """The app's own entry point, on the app's session maker, sending through
    whatever mail.deliver is at the time, even when it was replaced after the
    worker started."""
    monkeypatch.setattr(mail_outbox, "IDLE_WAIT_S", 120.0)
    monkeypatch.setattr(mail_outbox, "async_session_maker", sessions)
    delivered = asyncio.Event()

    async def from_a_thread() -> None:
        await asyncio.to_thread(mail_outbox.wake)

    async def from_the_loop() -> None:
        mail_outbox.wake()

    worker = asyncio.create_task(mail_outbox.outbox_worker_task())
    try:
        await asyncio.wait_for(worker_idle.wait(), 10)
        monkeypatch.setattr(mail, "deliver", _signalling_transport(delivered))
        for wake in (from_a_thread, from_the_loop):
            await asyncio.wait_for(worker_idle.wait(), 10)
            worker_idle.clear()
            delivered.clear()
            # Idle, with two minutes to wait: only a wake gets this sent in time.
            await _queue(sessions, now=time.time())
            await wake()
            await asyncio.wait_for(delivered.wait(), 10)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert mail_outbox._worker is None


async def test_a_failed_pass_is_logged_and_the_worker_carries_on(sessions, outbox_db, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="services.mail_outbox")
    monkeypatch.setattr(mail_outbox, "_FAILED_PASS_WAIT_CAP_S", 0.05)
    row_id = await _queue(sessions, now=time.time())
    run_once = mail_outbox.run_once
    calls = 0

    async def fails_first(**kwargs) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the database went away")
        return await run_once(**kwargs)

    monkeypatch.setattr(mail_outbox, "run_once", fails_first)
    delivered = asyncio.Event()
    worker = asyncio.create_task(mail_outbox._work(sessions=sessions, transport=_signalling_transport(delivered)))
    try:
        await asyncio.wait_for(delivered.wait(), 10)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert "a worker pass failed" in caplog.text
    assert _row(outbox_db, row_id)["status"] == "sent"


async def test_settled_rows_are_purged_after_a_week_and_unsettled_ones_never(sessions, outbox_db):
    week = mail_outbox.KEEP_SETTLED_S
    ids = {
        name: await _queue(sessions, _message(name)) for name in ("old", "recent", "cancelled", "queued", "settling")
    }
    _execute(
        outbox_db, "UPDATE mail_outbox SET status = 'sent', settled_at = ? WHERE id = ?", T0 - week - 1, ids["old"]
    )
    _execute(
        outbox_db, "UPDATE mail_outbox SET status = 'sent', settled_at = ? WHERE id = ?", T0 - week + 60, ids["recent"]
    )
    _execute(
        outbox_db,
        "UPDATE mail_outbox SET status = 'cancelled', settled_at = ? WHERE id = ?",
        T0 - week - 1,
        ids["cancelled"],
    )
    _execute(outbox_db, "UPDATE mail_outbox SET created_at = ? WHERE id = ?", T0 - 30 * 86400, ids["queued"])
    _execute(outbox_db, "UPDATE mail_outbox SET status = 'settling', outcome = 'sent' WHERE id = ?", ids["settling"])

    assert await mail_outbox._purge(sessions, T0) == 2

    left = {row_id for (row_id,) in _query(outbox_db, "SELECT id FROM mail_outbox")}
    assert left == {ids["recent"], ids["queued"], ids["settling"]}


# ── Whether mail can go ──────────────────────────────────────────────────────


@pytest.fixture
def mail_ready(monkeypatch):
    monkeypatch.setattr(mail, "is_configured", lambda: True)
    monkeypatch.setattr(mail, "link_to", lambda path: f"https://app.example.com{path}")


def test_unavailable_is_none_when_mail_can_go_and_reads_the_key_once(mail_ready, tmp_path, monkeypatch):
    key_file = tmp_path / mail_outbox.KEY_FILE_NAME

    assert mail_outbox.unavailable() is None
    assert key_file.exists()

    def no_disk(_path):
        raise AssertionError("the key was read from disk again")

    monkeypatch.setattr(mail_outbox, "_load_key", no_disk)
    assert mail_outbox.unavailable() is None


def test_unavailable_says_mail_is_not_configured_and_leaves_the_key_alone(mail_ready, tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "is_configured", lambda: False)

    assert mail_outbox.unavailable() == "mail is not configured"
    assert not (tmp_path / mail_outbox.KEY_FILE_NAME).exists()


def test_unavailable_says_host_app_is_not_set(mail_ready, monkeypatch):
    monkeypatch.setattr(mail, "link_to", lambda _path: None)

    assert mail_outbox.unavailable() == "HOST_APP is not set"


def _broken_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "broken" / mail_outbox.KEY_FILE_NAME
    path.parent.mkdir()
    path.write_text("not a key")
    return path


def _no_database_file() -> Path:
    raise mail_outbox.OutboxKeyError("this database has no file")


@pytest.mark.parametrize(
    "key_path",
    [
        _broken_key_file,
        lambda tmp_path: tmp_path / "no-such-directory" / mail_outbox.KEY_FILE_NAME,
        None,
    ],
    ids=["not-a-key", "cannot-be-created", "no-database-file"],
)
def test_unavailable_says_the_key_is_unusable_rather_than_raising(mail_ready, tmp_path, monkeypatch, key_path):
    if key_path is None:
        monkeypatch.setattr(mail_outbox, "_key_path", _no_database_file)
    else:
        path = key_path(tmp_path)
        monkeypatch.setattr(mail_outbox, "_key_path", lambda: path)

    assert mail_outbox.unavailable() == "the outbox key is unusable"


def test_unavailable_never_raises_when_the_mail_checks_do(monkeypatch):
    def broken() -> bool:
        raise RuntimeError("a bug in the mail module")

    monkeypatch.setattr(mail, "is_configured", broken)

    assert mail_outbox.unavailable() == "mail is not configured"


def test_unavailable_follows_the_mail_settings(monkeypatch):
    """Against the mail module itself rather than stand-ins for it."""
    for name in ("MAIL_TRANSPORT", "MAIL_FROM", "HOST_APP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RETINA_ENV", "test")
    assert mail_outbox.unavailable() == "mail is not configured"

    monkeypatch.setenv("MAIL_TRANSPORT", "log")
    monkeypatch.setenv("MAIL_FROM", "RETINA <no-reply@example.com>")
    assert mail_outbox.unavailable() == "HOST_APP is not set"

    monkeypatch.setenv("HOST_APP", "app.example.com")
    assert mail_outbox.unavailable() is None


@pytest.mark.parametrize("handler_raises", [False, True])
async def test_a_provider_message_id_longer_than_its_column_is_clipped(sessions, outbox_db, handler_raises):
    row_id = await _queue(sessions)
    if handler_raises:

        async def broken(_session, _settled) -> None:
            raise RuntimeError("deferred, so the id is written by the settling path")

        mail_outbox.on_settled(KIND_CLAIM, broken)

    assert await _run(sessions, Transport(mail.Delivery(mail.SENT, message_id="m" * 1000)))

    row = _row(outbox_db, row_id)
    assert row["status"] == ("settling" if handler_raises else "sent")
    assert row["message_id"] == "m" * 255


# ── More than one process ────────────────────────────────────────────────────

_WORKER = r"""
import asyncio, json, os, sqlite3, sys, time
from pathlib import Path

from core.users import engine
from services import mail, mail_outbox

db, sends, ready, go = sys.argv[1], sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
# How many worker processes are sending, this one included.
peers = int(sys.argv[5]) if len(sys.argv) > 5 else 1
first_send = True


def senders():
    pids = set()
    for line in Path(sends).read_text().splitlines():
        try:
            pids.add(json.loads(line)["pid"])
        except ValueError:
            pass  # another process's line, still being written
    return pids


def deliver(message):
    global first_send
    line = json.dumps({"subject": message.subject, "text": message.text, "pid": os.getpid()})
    # One short write to a file opened for appending lands whole, whoever
    # else is appending at the same moment.
    with open(sends, "a") as out:
        out.write(line + "\n")
    if first_send:
        first_send = False
        # The first row is held, leased and mid-send, until every other worker
        # has sent a row of its own. Each then takes a row while another holds
        # one, however unevenly a busy machine runs them: without this, one
        # worker given the CPU first could send every row before the other
        # took any. The wait stays far inside the lease, so the held row is
        # never free to be taken again.
        wait_until = time.monotonic() + 30
        while len(senders()) < peers:
            if time.monotonic() > wait_until:
                print("no other worker sent while this one held its first row", file=sys.stderr)
                break
            time.sleep(0.005)
    time.sleep(0.02)
    return mail.Delivery(mail.SENT, message_id=f"{os.getpid()}:{message.subject}")


def outstanding():
    con = sqlite3.connect(db, timeout=30)
    try:
        return con.execute("SELECT COUNT(*) FROM mail_outbox WHERE status IN ('queued', 'settling')").fetchone()[0]
    finally:
        con.close()


async def main():
    mail.deliver = deliver
    ready.touch()
    deadline = time.monotonic() + 60
    while not go.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    worker = asyncio.create_task(mail_outbox.outbox_worker_task())
    while outstanding() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await engine.dispose()


asyncio.run(main())
"""

_ENQUEUE = r"""
import asyncio, sys, time

from core.users import async_session_maker, engine
from services import mail, mail_outbox


async def main():
    link = "https://app.example.com/auth/claim/" + sys.argv[1]
    message = mail.Message(to="ada@example.com", subject="Connect", text=link, html=link)
    async with async_session_maker() as session:
        await mail_outbox.enqueue(session, message, kind=mail_outbox.KIND_CLAIM, expires_at=time.time() + 900)
        await session.commit()
    mail_outbox.wake()
    await engine.dispose()


asyncio.run(main())
"""


def _sends(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_mail_queued_by_one_process_is_sent_by_the_next_after_a_restart(outbox_db, tmp_path):
    token = secrets.token_urlsafe(32)
    code, _out, err = _finish(_start(_ENQUEUE, token, db=outbox_db))
    assert code == 0, err
    # The first process is gone, and with it anything it held in memory.
    [(row_id, status)] = _query(outbox_db, "SELECT id, status FROM mail_outbox")
    assert status == "queued"
    assert token.encode() not in _database_bytes(outbox_db)

    sends = tmp_path / "sends.jsonl"
    go = tmp_path / "go"
    go.touch()
    code, _out, err = _finish(
        _start(_WORKER, str(outbox_db), str(sends), str(tmp_path / "ready"), str(go), db=outbox_db)
    )
    assert code == 0, err

    [sent] = _sends(sends)
    assert sent["text"] == f"https://app.example.com/auth/claim/{token}"
    row = _row(outbox_db, row_id)
    assert (row["status"], row["attempts"], row["body"]) == ("sent", 1, None)


async def test_two_worker_processes_send_every_row_exactly_once(sessions, outbox_db, tmp_path):
    count = 60
    now = time.time()
    async with sessions() as session:
        for i in range(count):
            message = _message(f"token-{i}", subject=f"mail {i}")
            await mail_outbox.enqueue(session, message, kind=KIND_SIGN_IN, expires_at=now + LINK_LIFE_S, now=now)
        await session.commit()

    sends = tmp_path / "sends.jsonl"
    go = tmp_path / "go"
    ready = [tmp_path / "ready-a", tmp_path / "ready-b"]
    workers = [
        _start(_WORKER, str(outbox_db), str(sends), str(path), str(go), str(len(ready)), db=outbox_db) for path in ready
    ]
    await asyncio.to_thread(_await_files, ready)
    go.touch()
    results = [await asyncio.to_thread(_finish, worker) for worker in workers]

    for code, _out, err in results:
        assert code == 0, err
        assert "taken over" not in err
        assert "worker pass failed" not in err
        assert "no other worker sent" not in err
    lines = _sends(sends)
    assert Counter(line["subject"] for line in lines) == Counter(f"mail {i}" for i in range(count))
    # Both processes did some of the sending, each taking a row while the
    # other held one, so the two really did contend.
    assert len({line["pid"] for line in lines}) == 2
    assert _query(outbox_db, "SELECT status, attempts, COUNT(*) FROM mail_outbox GROUP BY status, attempts") == [
        ("sent", 1, count)
    ]
    assert _query(outbox_db, "SELECT COUNT(*) FROM mail_outbox WHERE body IS NOT NULL") == [(0,)]
