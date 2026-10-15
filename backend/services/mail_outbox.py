"""Mail that has to go out, queued in the database and sent by a worker.

A caller queues a message in its own transaction with `enqueue`, commits, and
calls `wake`. The mail then goes if and only if what made it necessary was
committed, and it survives a restart: the worker finds it in the table, not in
this process's memory.

Sending is at least once, and in practice once. A worker takes a row with one
statement that also leases it, sends it with no transaction open, and records
what happened fenced on the lease it took, so a worker whose lease ran out
cannot overwrite the result of the worker that took the row from it. Two
workers can only both send a row when the first is still sending past the end
of its lease, which is why the lease is many times the transport's timeout.

When a row settles, the handler registered for its kind runs in the same
transaction as the settlement, and sees every outcome including "sent". A
handler that raises rolls its own writes and the settlement back together. The
mail is not sent again: the row keeps what delivery came to, its body is wiped,
and the settlement alone is retried with the usual backoff until the handler
lets it through, or until the link has died and the row has been taken
MAX_SETTLE_ATTEMPTS times. A handler still raising then is given up: the row
settles as what delivery came to, `last_error` says the handler never
completed, and an error names the row. A handler must not commit, or a failure
after its commit would leave a settlement on record without the rest of what
the handler meant to do.

The body carries a link that is the whole credential, so it is encrypted under
a key kept beside the database file rather than in it (services/claim_links.py
says why the token may exist in the mail and nowhere else), and it is wiped as
soon as the row settles. Nothing here writes a body or a link to a log or to
`last_error`. A log line names the row's id and kind; only a warning or an
error, about a mail that has not gone or a row that needs looking at, also
names its recipient. The error that gives a handler up names the row alone,
which keeps the address until it is purged.
"""

import asyncio
import json
import logging
import os
import random
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from core.database import DATABASE_URL
from core.mail_outbox import (
    CANCELLED,
    EXPIRED,
    FAILED,
    FINAL_STATUSES,
    QUEUED,
    REJECTED,
    SENT,
    SETTLING,
    UNDELIVERABLE,
    MailOutbox,
)
from core.users import async_session_maker
from services import mail
from services.cancel_shield import to_completion

logger = logging.getLogger(__name__)

KIND_SIGN_IN = "sign_in"
KIND_CLAIM = "claim"
# The link that confirms an account's new address, the notice its old address
# gets once it has, and the notice an address gets in place of that link when
# it is another account's already (services/account_email.py).
KIND_ADDRESS_CHANGE = "address_change"
KIND_ADDRESS_NOTICE = "address_notice"
KIND_ADDRESS_TAKEN = "address_taken"
_KINDS = (KIND_SIGN_IN, KIND_CLAIM, KIND_ADDRESS_CHANGE, KIND_ADDRESS_NOTICE, KIND_ADDRESS_TAKEN)

#: Sends of one message before it settles as failed.
MAX_ATTEMPTS = 8
#: How many times a row must have been taken, sends and settlement passes
#: alike, before a handler that keeps raising is given up, and then only once
#: the link the mail carried has died too. While the link lives, what a claim
#: mail's handler does is what the node reads; once it has died, all that is
#: left for it is to flag an address that bounced. A row is never sent after
#: its MAX_ATTEMPTS-th claim, so the handler gets at least MAX_ATTEMPTS passes
#: after its first run, which the backoff spreads over the better part of an hour.
MAX_SETTLE_ATTEMPTS = 2 * MAX_ATTEMPTS + 1
#: How long a claim holds a row. A send takes at most the transport's 15 s
#: timeout, but the thread it runs on can wait its turn in the default
#: executor, and recording the result may wait out a locked database. A lease
#: that ran out mid-send would let a second worker send the row again; one
#: that is too long only delays a row whose worker died, by two minutes on a
#: fifteen-minute link.
LEASE_S = 120.0
#: The first retry waits about this long, and each one after it twice as long,
#: up to the cap. Jitter spreads a batch that failed together, so it does not
#: all come back at once when the provider does.
BACKOFF_BASE_S = 10.0
BACKOFF_CAP_S = 300.0
BACKOFF_JITTER = 0.2
#: A link with less life left than this when it would be sent is not sent: it
#: would arrive dead, or die while the person reads the mail.
EXPIRY_MARGIN_S = 60.0
#: The longest the worker sleeps with nothing due. Another process's enqueue
#: cannot wake this one, so this is how late its mail can be found.
IDLE_WAIT_S = 5.0
#: Settled rows are kept this long for whoever is asking where a mail went.
KEEP_SETTLED_S = 7 * 86400.0
_PURGE_EVERY_S = 3600.0
#: Never spin: a row the due query sees but the claim does not take is someone
#: else's, and theirs to finish.
_MIN_WAIT_S = 0.05
_FAILED_PASS_WAIT_CAP_S = 60.0
#: Tries at recording a send against a locked database. Each waits out the
#: connection's busy_timeout first, and the whole run stays well inside LEASE_S.
_RECORD_TRIES = 4
_DETAIL_MAX = 500
#: The message_id column's width. The provider's id is passed through as it
#: came, with no bound of its own.
_MESSAGE_ID_MAX = 255

KEY_FILE_NAME = "mail-outbox.key"


@dataclass(frozen=True)
class Settled:
    """What a handler is told about a row that has settled."""

    kind: str
    recipient: str
    node_id: str | None
    handle: str | None
    outcome: str
    detail: str


Handler = Callable[[AsyncSession, Settled], Awaitable[None]]
_handlers: dict[str, Handler] = {}

# The running worker's loop and the event that wakes it, or None when this
# process runs no worker.
_worker: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None


class OutboxKeyError(RuntimeError):
    """The key file is there and unusable, or there is no database file to keep it beside."""


@dataclass(frozen=True)
class _Claim:
    """A row as a worker took it. `owner` and `attempts` are its fence."""

    owner: str
    id: int
    kind: str
    recipient: str
    body: str | None
    node_id: str | None
    handle: str | None
    status: str
    outcome: str | None
    attempts: int
    expires_at: float
    last_error: str | None
    message_id: str | None


class _Unreadable(Exception):
    """A queued body that cannot be turned back into the message it was."""


# ── The key ──────────────────────────────────────────────────────────────────

_fernets: dict[Path, Fernet] = {}


def _key_path() -> Path:
    """Beside the database file, so a copy of the database alone, such as the
    daily backup, holds ciphertext and nothing that opens it."""
    database = make_url(DATABASE_URL).database
    if not database or database == ":memory:":
        raise OutboxKeyError("the mail outbox keeps its key beside the database file, and this database has none")
    return Path(database).parent / KEY_FILE_NAME


def _fernet() -> Fernet:
    path = _key_path()
    fernet = _fernets.get(path)
    if fernet is None:
        fernet = _fernets[path] = _load_key(path)
    return fernet


def _load_key(path: Path) -> Fernet:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = _create_key(path)
    try:
        return Fernet(raw.strip())
    except ValueError as exc:
        raise OutboxKeyError(
            f"{path} does not hold a Fernet key, so queued mail can be neither written nor read"
        ) from exc


def _create_key(path: Path) -> bytes:
    """Write a new key unless another process gets there first, and return
    whichever key is in place.

    The key is written whole under a private temporary name and then linked to
    its real one. A process that finds the file never reads half a key, and of
    two processes starting together exactly one link succeeds while the other
    reads the winner's. A rename would not do: it replaces, so the second
    process would swap out a key the first had already encrypted with.
    """
    key = Fernet.generate_key()
    # mkstemp creates the file 0600, so the key is never readable by anyone
    # else, not even for the moment before a chmod.
    descriptor, temporary = tempfile.mkstemp(prefix=f".{KEY_FILE_NAME}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return path.read_bytes()
        # The key has to outlive a crash before any row is written under it,
        # or every one of those rows would settle as undecryptable.
        _fsync_directory(path.parent)
        return key
    finally:
        os.unlink(temporary)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal(message: mail.Message) -> str:
    """The whole message as ciphertext. The address and subject go in with the
    body, so what is sent is exactly what was queued, whatever the plaintext
    columns beside it say."""
    fields = {"to": message.to, "subject": message.subject, "text": message.text, "html": message.html}
    return _fernet().encrypt(json.dumps(fields).encode()).decode()


def _unseal(claim: _Claim) -> mail.Message:
    if claim.body is None:
        raise _Unreadable("the queued row has no body")
    try:
        plaintext = _fernet().decrypt(claim.body.encode())
    except InvalidToken:
        raise _Unreadable(
            "the body cannot be decrypted with the key beside the database: the key file was replaced, "
            "or the database was restored without it"
        ) from None
    try:
        fields = json.loads(plaintext)
        message = mail.Message(to=fields["to"], subject=fields["subject"], text=fields["text"], html=fields["html"])
    except (ValueError, KeyError, TypeError):
        raise _Unreadable("the body decrypts, but not to a message") from None
    if message.to != claim.recipient:
        raise _Unreadable("the body is addressed to someone other than the row's recipient")
    return message


# ── What callers use ─────────────────────────────────────────────────────────


def unavailable() -> str | None:
    """None when a mail queued now could go out, otherwise why not, for the log.

    For a route to ask before it writes anything, so a link is never minted for
    a mail that cannot leave. Cheap and never raises: configuration is read
    from the environment, and the key is read from disk once and then kept.
    The reason names what is missing and nothing more.
    """
    try:
        if not mail.is_configured():
            return "mail is not configured"
        # Every mail a caller asks this before is a link, so without a host
        # there is nothing to send. The one that carries none, the notice of a
        # changed address, only follows a link that went.
        if mail.link_to("/") is None:
            return "HOST_APP is not set"
    except Exception:
        logger.exception("mail_outbox: could not tell whether mail is configured")
        return "mail is not configured"
    try:
        _fernet()
    except Exception as exc:
        # OutboxKeyError, or the disk refusing to read or write the key file.
        logger.warning("mail_outbox: the outbox key cannot be used: %s", exc)
        return "the outbox key is unusable"
    return None


def _check_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"unknown mail kind {kind!r}; expected one of {', '.join(_KINDS)}")


async def enqueue(
    session: AsyncSession,
    message: mail.Message,
    *,
    kind: str,
    expires_at: float,
    node_id: str | None = None,
    handle: str | None = None,
    now: float | None = None,
) -> int:
    """Queue `message` in the caller's transaction and return the row's id.

    Flushed, never committed: the mail goes if and only if the caller commits.
    Call `wake` after the commit, not before it, or the worker can look before
    the row is there to find.

    `expires_at` is when the link the mail carries dies, or for a mail with no
    link, when it stops being worth sending. A mail that could not go out with
    at least a minute of that left is not sent at all, and settles as "expired".

    Raises OutboxKeyError when the key beside the database cannot be used: the
    mail is refused rather than stored where it could be read.
    """
    _check_kind(kind)
    now = time.time() if now is None else now
    row = MailOutbox(
        kind=kind,
        recipient=message.to,
        subject=message.subject,
        body=_seal(message),
        node_id=node_id,
        handle=handle,
        status=QUEUED,
        attempts=0,
        created_at=now,
        next_attempt_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row.id


def wake() -> None:
    """Have this process's worker look for due mail now rather than at its next poll.

    Callable from the event loop or from any thread, so from a sync route as
    well as an async one. Does nothing when this process runs no worker; one in
    another process finds the row within IDLE_WAIT_S.
    """
    worker = _worker
    if worker is None:
        return
    loop, event = worker
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        event.set()
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        pass  # the loop closed under us, so there is no worker left to wake


async def cancel(session: AsyncSession, *, node_id: str, kind: str = KIND_CLAIM) -> int:
    """Stop every queued mail of `kind` about `node_id` from going, and say how many there were.

    In the caller's transaction, which the caller commits. No handler runs: the
    caller is the one that decided the mail should not go. A mail that has
    already gone is not recalled, and a send in flight finishes, but its result
    is not recorded and no handler runs for it.

    Cancel before enqueueing a replacement in the same transaction, not after:
    every queued row for the node is cancelled, including one queued a moment ago.
    """
    _check_kind(kind)
    result = await session.execute(
        update(MailOutbox)
        .where(MailOutbox.node_id == node_id, MailOutbox.kind == kind, MailOutbox.status == QUEUED)
        .values(status=CANCELLED, body=None, settled_at=time.time(), lease_owner=None, lease_until=None)
        .returning(MailOutbox.id)
    )
    return len(result.all())


def on_settled(kind: str, handler: Handler) -> None:
    """Run `handler` whenever a mail of `kind` settles, whatever it came to.

    One handler per kind: registering again replaces the one before. It is
    awaited in the transaction that records the settlement, with that
    transaction's session, and must not commit it. One that raises is run
    again later, until the link has died and the row has been taken
    MAX_SETTLE_ATTEMPTS times; past that, the row settles without it.
    """
    _check_kind(kind)
    _handlers[kind] = handler


def backoff_s(attempts: int, retry_after_s: float | None = None, *, jitter: float | None = None) -> float:
    """How long to wait after the `attempts`-th send was told to retry.

    About 10 s after the first, doubling to about five minutes, and never
    sooner than the provider asked for. `jitter` is the factor applied to the
    nominal wait; drawn at random within BACKOFF_JITTER of 1 when not given.
    """
    nominal = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0 ** min(max(attempts, 1) - 1, 16))
    if jitter is None:
        jitter = random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
    wait = nominal * jitter
    if retry_after_s is not None and retry_after_s > wait:
        wait = retry_after_s
    return wait


# ── The worker ───────────────────────────────────────────────────────────────


def _claim_statement(owner: str, now: float):
    """Take the most overdue row nobody holds, in one statement.

    SQLite runs the whole statement under the database's single write lock, so
    the row the subquery picks cannot be picked by anybody else before this
    lease is on it. The statement writes before it reads, so it starts its
    transaction with that lock rather than upgrading a read to it, which is
    what would let it act on a snapshot another writer had moved past.
    """
    candidate = aliased(MailOutbox)
    due = (
        select(candidate.id)
        .where(
            candidate.status.in_((QUEUED, SETTLING)),
            candidate.next_attempt_at <= now,
            or_(candidate.lease_until.is_(None), candidate.lease_until < now),
        )
        .order_by(candidate.next_attempt_at, candidate.id)
        .limit(1)
        .scalar_subquery()
    )
    return (
        update(MailOutbox)
        .where(MailOutbox.id == due)
        .values(lease_owner=owner, lease_until=now + LEASE_S, attempts=MailOutbox.attempts + 1)
        .returning(
            MailOutbox.id,
            MailOutbox.kind,
            MailOutbox.recipient,
            MailOutbox.body,
            MailOutbox.node_id,
            MailOutbox.handle,
            MailOutbox.status,
            MailOutbox.outcome,
            MailOutbox.attempts,
            MailOutbox.expires_at,
            MailOutbox.last_error,
            MailOutbox.message_id,
        )
        .execution_options(synchronize_session=False)
    )


def _fenced(claim: _Claim):
    """An UPDATE that only lands while the row is still as this claim left it.

    A worker that takes the row over raises `attempts` and replaces the owner,
    and cancel replaces the status, so a worker whose claim has been
    superseded matches nothing. The owner alone would not do: the same worker
    can take the same row twice, once its first lease has run out.
    """
    return (
        update(MailOutbox)
        .where(
            MailOutbox.id == claim.id,
            MailOutbox.status == claim.status,
            MailOutbox.lease_owner == claim.owner,
            MailOutbox.attempts == claim.attempts,
        )
        .returning(MailOutbox.id)
        .execution_options(synchronize_session=False)
    )


async def _claim(sessions: async_sessionmaker[AsyncSession], owner: str, now: float) -> _Claim | None:
    async with sessions() as session:
        row = (await session.execute(_claim_statement(owner, now))).one_or_none()
        await session.commit()
    if row is None:
        return None
    return _Claim(
        owner=owner,
        id=row.id,
        kind=row.kind,
        recipient=row.recipient,
        body=row.body,
        node_id=row.node_id,
        handle=row.handle,
        status=row.status,
        outcome=row.outcome,
        attempts=row.attempts,
        expires_at=row.expires_at,
        last_error=row.last_error,
        message_id=row.message_id,
    )


def _superseded(claim: _Claim, what: str) -> None:
    logger.warning(
        "mail_outbox: row %d (%s to %s) was taken over or cancelled before its result (%s) was recorded; leaving it be",
        claim.id,
        claim.kind,
        claim.recipient,
        what,
    )


def _clip(detail: str) -> str:
    return detail[:_DETAIL_MAX]


def _clip_id(message_id: str | None) -> str | None:
    return message_id[:_MESSAGE_ID_MAX] if message_id else None


async def _reschedule(
    sessions: async_sessionmaker[AsyncSession], claim: _Claim, *, retry_at: float, detail: str, now: float
) -> None:
    async with sessions() as session:
        landed = (
            await session.execute(
                _fenced(claim).values(
                    next_attempt_at=retry_at, lease_owner=None, lease_until=None, last_error=_clip(detail) or None
                )
            )
        ).one_or_none()
        if landed is None:
            await session.rollback()
            _superseded(claim, "retry")
            return
        await session.commit()
    logger.warning(
        "mail_outbox: row %d (%s to %s) will be tried again in %.0f s: %s",
        claim.id,
        claim.kind,
        claim.recipient,
        retry_at - now,
        detail,
    )


async def _settle(
    sessions: async_sessionmaker[AsyncSession],
    claim: _Claim,
    *,
    outcome: str,
    detail: str,
    message_id: str | None,
    now: float,
) -> None:
    """Record what the mail came to, wipe its body, and run its kind's handler,
    all in one transaction.

    If the handler raises, both roll back and the row is left settling: the
    outcome is kept, the body is still wiped, and the settlement is tried again
    after a backoff. Once the link has died and the row has been taken
    MAX_SETTLE_ATTEMPTS times, a handler that raises again is given up instead,
    and the row settles without it. Sending it again is never on the table once
    it has gone.
    """
    settled = Settled(
        kind=claim.kind,
        recipient=claim.recipient,
        node_id=claim.node_id,
        handle=claim.handle,
        outcome=outcome,
        detail=detail,
    )
    async with sessions() as session:
        landed = (
            await session.execute(
                _fenced(claim).values(
                    status=outcome,
                    outcome=outcome,
                    body=None,
                    settled_at=now,
                    lease_owner=None,
                    lease_until=None,
                    last_error=_clip(detail) or None,
                    message_id=_clip_id(message_id),
                )
            )
        ).one_or_none()
        if landed is None:
            await session.rollback()
            _superseded(claim, outcome)
            return
        handler = _handlers.get(claim.kind)
        raised: Exception | None = None
        if handler is not None:
            try:
                await handler(session, settled)
            except Exception as exc:
                raised = exc
                await session.rollback()
        if raised is None:
            await session.commit()
    if raised is not None:
        if now >= claim.expires_at and claim.attempts >= MAX_SETTLE_ATTEMPTS:
            await _give_up_handler(
                sessions, claim, outcome=outcome, detail=detail, message_id=message_id, raised=raised, now=now
            )
            return
        logger.error(
            "mail_outbox: the %s handler raised settling row %d (to %s) as %s; settling it again later",
            claim.kind,
            claim.id,
            claim.recipient,
            outcome,
            exc_info=raised,
        )
        await _defer(sessions, claim, outcome=outcome, detail=detail, message_id=message_id, now=now)
        return
    if outcome == SENT:
        # Every mail that goes out writes this line, so it names the row and
        # not the address, which the row keeps until it is purged.
        logger.info("mail_outbox: row %d (%s) sent", claim.id, claim.kind)
    else:
        logger.warning(
            "mail_outbox: row %d (%s to %s) settled as %s: %s", claim.id, claim.kind, claim.recipient, outcome, detail
        )


async def _defer(
    sessions: async_sessionmaker[AsyncSession],
    claim: _Claim,
    *,
    outcome: str,
    detail: str,
    message_id: str | None,
    now: float,
) -> None:
    """Keep what delivery came to, and leave the handler to be run again later."""
    async with sessions() as session:
        landed = (
            await session.execute(
                _fenced(claim).values(
                    status=SETTLING,
                    outcome=outcome,
                    body=None,
                    next_attempt_at=now + backoff_s(claim.attempts),
                    lease_owner=None,
                    lease_until=None,
                    last_error=_clip(detail) or None,
                    message_id=_clip_id(message_id),
                )
            )
        ).one_or_none()
        if landed is None:
            await session.rollback()
            _superseded(claim, outcome)
            return
        await session.commit()


async def _give_up_handler(
    sessions: async_sessionmaker[AsyncSession],
    claim: _Claim,
    *,
    outcome: str,
    detail: str,
    message_id: str | None,
    raised: Exception,
    now: float,
) -> None:
    """Settle the row as what delivery came to, without the handler that never
    completed. What the handler was to do is left undone, and the error says
    which row, so someone can see to it."""
    reason = f"the {claim.kind} handler never completed ({type(raised).__name__}), so the row settled without it"
    async with sessions() as session:
        landed = (
            await session.execute(
                _fenced(claim).values(
                    status=outcome,
                    outcome=outcome,
                    body=None,
                    settled_at=now,
                    lease_owner=None,
                    lease_until=None,
                    last_error=_clip(f"{reason}: {detail}" if detail else reason),
                    message_id=_clip_id(message_id),
                )
            )
        ).one_or_none()
        if landed is None:
            await session.rollback()
            _superseded(claim, outcome)
            return
        await session.commit()
    # Names the row and not the address, which the row keeps until it is purged.
    logger.error(
        "mail_outbox: the %s handler never completed for row %d, so it is settled as %s without it",
        claim.kind,
        claim.id,
        outcome,
        exc_info=raised,
    )


async def _persist(write: Callable[[], Awaitable[None]], claim: _Claim) -> None:
    """Record a send, riding out a database that is briefly locked.

    By now the mail has gone or been refused. Giving up here leaves the row
    leased with nothing recorded, and once the lease runs out it is sent again.
    """
    for attempt in range(_RECORD_TRIES):
        try:
            await write()
            return
        except OperationalError:
            if attempt == _RECORD_TRIES - 1:
                raise
            logger.warning(
                "mail_outbox: the database refused the record of row %d (%s to %s); trying again",
                claim.id,
                claim.kind,
                claim.recipient,
            )
            await asyncio.sleep(2.0**attempt)


async def _process(
    sessions: async_sessionmaker[AsyncSession],
    claim: _Claim,
    clock: Callable[[], float],
    transport: Callable[[mail.Message], mail.Delivery],
) -> None:
    def settle(outcome: str, detail: str, message_id: str | None = None) -> Callable[[], Awaitable[None]]:
        return lambda: _settle(sessions, claim, outcome=outcome, detail=detail, message_id=message_id, now=clock())

    if claim.status == SETTLING:
        # Delivery already happened; only the handler is left. Neither the
        # link's life nor the attempt cap below ends it: _settle decides when
        # a handler that keeps raising is given up.
        await _persist(settle(claim.outcome or FAILED, claim.last_error or "", claim.message_id), claim)
        return
    if clock() >= claim.expires_at - EXPIRY_MARGIN_S:
        await _persist(settle(EXPIRED, "the link had under a minute to live by the time it could be sent"), claim)
        return
    if claim.attempts > MAX_ATTEMPTS:
        # Every attempt is counted when it is claimed, so this row has had its
        # sends and the last of them never reported back.
        await _persist(
            settle(FAILED, f"gave up after {MAX_ATTEMPTS} attempts, the last of which never reported back"), claim
        )
        return
    try:
        message = _unseal(claim)
    except (_Unreadable, OutboxKeyError) as exc:
        logger.error("mail_outbox: row %d (%s to %s) cannot be sent: %s", claim.id, claim.kind, claim.recipient, exc)
        await _persist(settle(FAILED, str(exc)), claim)
        return

    try:
        delivery = await asyncio.to_thread(transport, message)
    except Exception as exc:
        # mail.deliver never raises, so this is a bug in it. Only the type is
        # logged: what an exception says about a message can include the message.
        logger.error(
            "mail_outbox: the transport raised %s sending row %d (%s to %s)",
            type(exc).__name__,
            claim.id,
            claim.kind,
            claim.recipient,
        )
        delivery = mail.Delivery(mail.RETRY, f"the transport raised {type(exc).__name__}")

    detail = delivery.detail or ""
    if delivery.outcome == mail.SENT:
        await _persist(settle(SENT, detail, delivery.message_id), claim)
    elif delivery.outcome == mail.UNDELIVERABLE:
        await _persist(settle(UNDELIVERABLE, detail, delivery.message_id), claim)
    elif delivery.outcome == mail.REJECTED:
        await _persist(settle(REJECTED, detail, delivery.message_id), claim)
    elif delivery.outcome == mail.RETRY:
        if claim.attempts >= MAX_ATTEMPTS:
            await _persist(settle(FAILED, f"gave up after {claim.attempts} attempts: {detail}"), claim)
            return
        now = clock()
        retry_at = now + backoff_s(claim.attempts, delivery.retry_after_s)
        if retry_at >= claim.expires_at - EXPIRY_MARGIN_S:
            reason = f"the link would have under a minute to live by the next attempt: {detail}"
            await _persist(settle(EXPIRED, reason), claim)
            return
        await _persist(lambda: _reschedule(sessions, claim, retry_at=retry_at, detail=detail, now=now), claim)
    else:
        # Settled rather than retried: a result nobody can read might have
        # been a send, and sending twice is the one thing this must not do.
        await _persist(settle(FAILED, f"the transport answered {delivery.outcome!r}, which is not an outcome"), claim)


async def run_once(
    *,
    sessions: async_sessionmaker[AsyncSession] = async_session_maker,
    owner: str,
    now: float | None = None,
    transport: Callable[[mail.Message], mail.Delivery] = mail.deliver,
) -> bool:
    """Take at most one due row, send or settle it, and record what happened.

    Returns whether there was a row to take. `now` pins the clock for the
    whole pass; without it each decision reads the time as it is made. The
    send runs on a thread, so the event loop carries on while it waits.
    """
    fixed = now

    def clock() -> float:
        return time.time() if fixed is None else fixed

    async def take_and_send() -> bool:
        claim = await _claim(sessions, owner, clock())
        if claim is None:
            return False
        await _process(sessions, claim, clock, transport)
        return True

    # One step: a stop during the claim also waits for the row it takes, which
    # would otherwise sit leased and unsent until its lease ran out. Each step
    # of the worker runs this way, so a shutdown stops it between steps and
    # never inside one.
    return await to_completion(take_and_send(), log=logger, label="mail_outbox")


async def _purge(sessions: async_sessionmaker[AsyncSession], now: float) -> int:
    """Delete rows that settled more than KEEP_SETTLED_S ago. A row still
    queued or settling is kept however old it is."""
    async with sessions() as session:
        result = await session.execute(
            delete(MailOutbox)
            .where(MailOutbox.status.in_(FINAL_STATUSES), MailOutbox.settled_at < now - KEEP_SETTLED_S)
            .returning(MailOutbox.id)
        )
        purged = len(result.all())
        await session.commit()
    if purged:
        logger.info("mail_outbox: purged %d rows settled more than %d days ago", purged, KEEP_SETTLED_S // 86400)
    return purged


async def _until_due(sessions: async_sessionmaker[AsyncSession], now: float) -> float:
    """Seconds until a row could next be taken, within IDLE_WAIT_S. A leased
    row counts from the end of its lease, which is when it would be free."""
    async with sessions() as session:
        due = await session.scalar(
            select(
                func.min(
                    func.max(
                        MailOutbox.next_attempt_at,
                        func.coalesce(MailOutbox.lease_until, MailOutbox.next_attempt_at),
                    )
                )
            ).where(MailOutbox.status.in_((QUEUED, SETTLING)))
        )
    if due is None:
        return IDLE_WAIT_S
    return min(IDLE_WAIT_S, max(_MIN_WAIT_S, due - now))


async def _work(
    *,
    sessions: async_sessionmaker[AsyncSession],
    transport: Callable[[mail.Message], mail.Delivery],
) -> None:
    global _worker
    owner = f"{os.getpid()}-{secrets.token_hex(4)}"
    event = asyncio.Event()
    me = (asyncio.get_running_loop(), event)
    _worker = me
    purge_at = 0.0
    failed_passes = 0
    try:
        while True:
            # Cleared before the pass, not after it: a wake that lands while
            # the pass runs then cuts the wait below short instead of being lost.
            event.clear()
            try:
                if time.time() >= purge_at:
                    await to_completion(_purge(sessions, time.time()), log=logger, label="mail_outbox")
                    purge_at = time.time() + _PURGE_EVERY_S
                # A row past its link's life is due by construction, so this
                # also settles whatever has expired, without sending it.
                while await run_once(sessions=sessions, owner=owner, transport=transport):
                    pass
                wait = await to_completion(_until_due(sessions, time.time()), log=logger, label="mail_outbox")
                failed_passes = 0
            except Exception:
                failed_passes += 1
                wait = min(_FAILED_PASS_WAIT_CAP_S, 2.0**failed_passes)
                logger.exception("mail_outbox: a worker pass failed; trying again in %.1f s", wait)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait)
            except TimeoutError:
                pass
    finally:
        if _worker is me:
            _worker = None


async def outbox_worker_task() -> None:
    """Send queued mail for as long as the app runs.

    Safe to run in every process at once: rows are taken one at a time under a
    lease, so each is sent by one worker. Never returns, and no exception ends
    it: a failed pass is logged and the next one waits a little longer.

    mail.deliver is looked up for each send rather than once here, so whatever
    replaces it, a test above all, reaches a worker that is already running.
    """
    await _work(sessions=async_session_maker, transport=lambda message: mail.deliver(message))
