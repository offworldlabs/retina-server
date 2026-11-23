"""What Cloudflare learns about a message after the send API accepted it, put
back onto the claims it concerns.

The send API answers before the recipient's server has said anything, so a
claim link to a mistyped address is accepted and then bounces. Email Sending
reports that afterwards through event subscriptions, which publish into a
Cloudflare Queue rather than calling a webhook. This drains the queue over its
HTTP pull consumer: a pull leases a batch, an ack removes what was handled, and
anything not acknowledged comes back once the lease runs out. Delivery is
therefore at least once, and applying an event a second time has to change
nothing.

Two verdicts mean nobody will read mail at the address: a hard bounce and a
spam complaint, the two Cloudflare's own example of keeping recipient records
in step acts on. A soft bounce, including the one a `bounced` event carries
when temporary retries ran out, says the mailbox may work tomorrow, and is left
alone.

So is a rejection, whatever its reason. One because the address is suppressed
is not proof that it is dead: the suppression list also holds soft bounces for
a day, hard bounces that lapse after a week, and entries an operator or
Cloudflare's own policy put there, and an address can work again once its
owner fixes the mailbox. Nor is it news. A send of ours to a suppressed address
hears so from the send API itself, and the outbox puts that on the claim the
mail was for. Cloudflare does not list a rejection's other reasons.

Only unverified claims with no owner are touched. An address somebody
confirmed by clicking has proved itself, and a later complaint from it says
nothing about whether the node belongs to them.

There is no signed webhook for these events: a subscription delivers only to a
queue (checked 2026-10-13). What this trusts is the pull itself, over HTTPS to
api.cloudflare.com for the configured account and queue, with a token holding
Queues read and write on that account. Anything that can publish to the queue,
that token included, can write an event that reads as Cloudflare's, so the
queue should carry this subscription and nothing else. The most a forged event
can do is flag an unverified address on a node nobody owns, kill its pending
link and stop the claim mail still queued for it, after which that address gets
no more claim mail until the node offers another. It changes no owner and sends
no mail.

Settings are read when the task starts rather than at import, for the reason
services/mail.py gives. Nothing here ever writes the token to a log. A token
holding anything a bearer token cannot, such as the line break a value quoted
across two lines in backend/.env keeps, leaves the feed off: the HTTP client
refuses a header like that by quoting it, token and all, into its error. A
failure whose text quotes the token anyway is logged by its type alone.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import traceback
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeClaimChallenge
from services import claim_links, mail_outbox
from services.cancel_shield import to_completion
from services.node_claim import ClaimInvalid, validate_claim
from services.node_claim_store import mark_open_undeliverable, open_claims

logger = logging.getLogger(__name__)

_QUEUE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/queues/{queue}/messages/{action}"

_TIMEOUT_S = 15.0

#: The most one pull may lease.
_BATCH_SIZE = 100

#: How long a pulled batch stays leased to this consumer. Far longer than a
#: batch takes to apply. A lease that runs out first only hands its messages
#: back for a second delivery, which changes nothing.
_LEASE_MS = 120_000

#: The wait after a pull comes back empty. Queues answers a pull at once rather
#: than holding it open until something arrives, so this is the polling rate.
#: A bounce that reaches the node half a minute late costs nothing.
_IDLE_S = 30.0

#: The wait after a failed round, doubling up to the cap. A pull that keeps
#: failing is a bad token or an outage, and asking every second fixes neither.
_BACKOFF_FIRST_S = 30.0
_BACKOFF_MAX_S = 600.0

#: Swapped for an httpx.MockTransport by the tests, as services/mender.py's is.
_transport: httpx.AsyncBaseTransport | None = None

_SOURCE = "email.sending"
_BOUNCED = "cf.email.sending.message.bounced"
_COMPLAINED = "cf.email.sending.message.complained"


@dataclass(frozen=True)
class _Queue:
    account: str
    queue: str
    token: str


class _QueueRefused(Exception):
    """The queue API answered, but not with success."""


class _Malformed(Exception):
    """An event this module cannot read, which no number of retries would fix."""


def _setting(name: str) -> str:
    return os.getenv(name, "").strip()


def _unsendable(token: str) -> str | None:
    """What in `token` no bearer token holds, named by kind and never quoted, or None.

    RFC 6750 draws a bearer token from letters, digits and `-._~+/=`, all of it
    visible ASCII, so refusing everything else never refuses a token that could
    work. What it does refuse is a quoting or pasting mistake, and httpx and h11
    turn some of that away by quoting the header whole into their error: h11 a
    line break, form feed, vertical tab or NUL, httpx anything outside ASCII.
    """
    for char in token:
        if "!" <= char <= "~":
            continue
        if char in "\r\n":
            return "a line break"
        if char in " \t":
            return "a space or a tab"
        if char.isascii():
            return "a control character"
        return "a character outside ASCII"
    return None


def _quotes_token(exc: BaseException, token: str) -> bool:
    """Whether logging `exc`, its message or its traceback, would write `token`,
    as typed or as Python quotes it.

    Read off the traceback as logging would render it, so an exception it was
    raised from, or a note on it, is read too. Nothing known quotes the token
    once `_unsendable` has kept out what h11 and httpx refuse, so this is for
    whatever does next, as services/mail.py's check of the same name is for the
    send token.
    """
    text = "".join(traceback.format_exception(exc))
    forms = (token, repr(token)[1:-1], repr(token.encode("utf-8", "backslashreplace"))[2:-1])
    return any(form in text for form in forms)


def _configured_queue() -> _Queue | None:
    """The queue to drain, or None when any of the three settings is missing or
    the token holds something no token does.

    Says which, once: the task runs once per process, so this is read once.
    """
    queue = _Queue(
        account=_setting("CLOUDFLARE_ACCOUNT_ID"),
        queue=_setting("CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID"),
        token=_setting("CLOUDFLARE_EMAIL_EVENTS_TOKEN"),
    )
    missing = [
        name
        for name, value in (
            ("CLOUDFLARE_ACCOUNT_ID", queue.account),
            ("CLOUDFLARE_EMAIL_EVENTS_QUEUE_ID", queue.queue),
            ("CLOUDFLARE_EMAIL_EVENTS_TOKEN", queue.token),
        )
        if not value
    ]
    if missing:
        logger.info(
            "Mail events: off, since %s %s not set. A bounce or complaint will not reach the claim it was for.",
            ", ".join(missing),
            "is" if len(missing) == 1 else "are",
        )
        return None
    unsendable = _unsendable(queue.token)
    if unsendable is not None:
        logger.warning(
            "Mail events: off, since CLOUDFLARE_EMAIL_EVENTS_TOKEN holds %s, which no API token does. "
            "A bounce or complaint will not reach the claim it was for.",
            unsendable,
        )
        return None
    return queue


async def _call(client: httpx.AsyncClient, queue: _Queue, action: str, body: dict) -> dict:
    """POST to one of the queue's message endpoints and return its `result`."""
    response = await client.post(
        _QUEUE_URL.format(account=queue.account, queue=queue.queue, action=action),
        headers={"Authorization": f"Bearer {queue.token}"},
        json=body,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if not response.is_success or payload.get("success") is not True:
        errors = payload.get("errors")
        reasons = "; ".join(
            f"{e.get('code')} {e.get('message')}"
            for e in (errors if isinstance(errors, list) else [])
            if isinstance(e, dict)
        )
        if not reasons:
            reasons = "the answer does not say success" if response.is_success else response.reason_phrase
        raise _QueueRefused(f"{action}: HTTP {response.status_code}: {reasons}")
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def _decode(body: object) -> object:
    """A pulled message's body as the event it carries. Raises ValueError.

    Queues hands a message published as `json` or `bytes` over base64 encoded,
    and one published as `text` as the string itself. Strict base64 never
    accepts the text of a JSON object, whose first character is `{`, so trying
    it first cannot misread the other.
    """
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        raise ValueError("the body is not a string")
    try:
        raw = base64.b64decode(body, validate=True)
    except binascii.Error:
        raw = body.encode()
    return json.loads(raw)


def _undeliverable(kind: str, payload: dict) -> bool:
    """Whether an event of this type and payload says the address is dead.

    A hard bounce or a complaint does. A rejection never does, whatever its
    reason, so the reason is not read and one that gives none is not
    malformed. Even `suppressed` only says the address is on Cloudflare's list,
    which holds addresses that still work, and the send API already told the
    outbox about any mail of ours turned away for it.
    """
    if kind == _BOUNCED:
        bounce = payload.get("bounce")
        bounce_type = bounce.get("type") if isinstance(bounce, dict) else None
        # Not read as soft when it is neither: a bounce that does not say which
        # it was is a schema this was not written for, and saying so is how
        # anybody finds out that bounces have stopped landing.
        if bounce_type not in ("hard", "soft"):
            raise _Malformed("a bounce that is neither hard nor soft")
        return bounce_type == "hard"
    return kind == _COMPLAINED


async def handle_event(session: AsyncSession, event: dict) -> bool:
    """Apply one Email Sending event, returning whether it marked any claim.

    False covers every event that changes nothing: a delivery, a deferral, a
    soft bounce, a rejection, a failure on Cloudflare's side, a verdict on an
    address no open claim carries, an event already applied, one from another
    source, and one that cannot be read. None of those is worth another delivery, so the caller
    acknowledges whatever this returns and retries only what it raises.

    Commits, since the links it kills have to die around a write that is
    durable; see _mark. A cancel that lands while it reads or writes waits for
    the event to be applied whole before it goes on, so stopping the app never
    leaves the database's write lock held (services/cancel_shield.py says how
    it would).
    """
    source = event.get("source")
    if not isinstance(source, dict) or source.get("type") != _SOURCE:
        # A queue shared with another subscription carries its events too. This
        # tells them apart; it proves nothing about who wrote the event.
        logger.debug("Mail events: ignoring an event from another source")
        return False

    kind = event.get("type")
    try:
        if not isinstance(kind, str) or not kind:
            raise _Malformed("no event type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise _Malformed("no payload")
        if not _undeliverable(kind, payload):
            logger.debug("Mail events: %s changes nothing here", kind)
            return False
        try:
            # Normalised exactly as the address was when it was stored, or the
            # comparison below silently matches nothing.
            address = validate_claim({"email": payload.get("recipient")})
        except ClaimInvalid as exc:
            raise _Malformed(f"a recipient that is {exc.reason}") from exc
    except _Malformed as exc:
        logger.warning("Mail events: dropping an event that cannot be read (%s): %s", kind or "no type", exc)
        return False

    # Here rather than around the session _drain opens, so that every caller
    # gets it, while the pull and the ack around it stay as quick to stop as
    # any other request. Stopped here, the event is applied but never
    # acknowledged, and comes back to find nothing left to change.
    return await to_completion(_mark(session, kind, address), log=logger, label="Mail events")


async def _mark(session: AsyncSession, kind: str, address: str) -> bool:
    """Mark every open claim on `address` undeliverable, stop the claim mail
    still queued for it, and kill its link.

    The links die before the claims are marked as well as after. Marking drops
    the challenge row, and with it the only record of the link's handle, so a
    link killed only afterwards and failing to die would be live and unfindable
    when the event came round again. Dying first, a failure leaves every row as
    it was for the redelivery to find. The handles dropped by the marking are
    killed after the commit as well, for a challenge a resend replaced in
    between.

    The claims are read before the links die and written after, with no write
    lock held in between, so the marking checks each claim again in the
    statement that flags it. One confirmed, bound or given another address
    meanwhile is left as it stands, with its challenge and its mail: a bounce
    for one address says nothing about the next. A node that is marked has its
    queued claim mail cancelled in the same transaction, as the outbox's own
    verdict does (services/claim_links.py): a resend still waiting to go would
    otherwise go to the address that just bounced, carrying a link this kills.

    The link store writes over a connection of its own, and SQLite takes one
    writer at a time, so nothing here holds a transaction open while it does.
    """
    # Already-flagged rows are left out, which is what makes a redelivery a read
    # and nothing else.
    node_ids = await open_claims(session, address)
    if not node_ids:
        logger.debug("Mail events: %s for an address no open claim carries", kind)
        return False

    outstanding = (
        await session.execute(
            select(NodeClaimChallenge.handle).where(
                NodeClaimChallenge.node_id.in_(node_ids), NodeClaimChallenge.email == address
            )
        )
    ).scalars()
    killed = set(outstanding)
    # Ends the read before another connection writes, so the write below does
    # not come from a transaction that began before that one committed.
    await session.commit()
    for handle in killed:
        await claim_links.invalidate(handle)

    # The marking is this transaction's first write, so it takes the write lock
    # before it decides anything, and the cancels below see what it saw.
    marked = await mark_open_undeliverable(session, address, node_ids)
    cancelled = {node_id: await mail_outbox.cancel(session, node_id=node_id) for node_id in marked}
    await session.commit()

    for handle in marked.values():
        if handle is not None and handle not in killed:
            await claim_links.invalidate(handle)
    for node_id in marked:
        logger.info(
            "Mail events: %s, so the claim address on node %s is marked undeliverable%s",
            kind,
            node_id,
            " and the claim mail still queued for it is cancelled" if cancelled[node_id] else "",
        )
    return bool(marked)


async def _drain(client: httpx.AsyncClient, queue: _Queue) -> tuple[int, int]:
    """Pull one batch, apply it, and settle it with the queue.

    Returns how many messages were acknowledged and how many could not be
    applied. Each is applied in a session of its own, so one failing does not
    undo the rest, and each failure goes back on the queue for another attempt.
    """
    # Resolved at call time, as blah2_poller resolves it.
    import core.users

    result = await _call(client, queue, "pull", {"batch_size": _BATCH_SIZE, "visibility_timeout_ms": _LEASE_MS})
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        return 0, 0

    acks: list[dict[str, str]] = []
    retries: list[dict[str, str]] = []
    first_failure: Exception | None = None
    for message in messages:
        lease = message.get("lease_id") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(lease, str) or not lease:
            logger.warning("Mail events: a pulled message has no lease to settle it by, and comes back when it lapses")
            continue
        try:
            event = _decode(message.get("body"))
        except ValueError as exc:
            # Acknowledged: the same bytes would fail the same way on every
            # delivery until the queue gave up on them.
            logger.warning("Mail events: dropping message %s, whose body cannot be read: %s", message.get("id"), exc)
            acks.append({"lease_id": lease})
            continue
        if not isinstance(event, dict):
            logger.warning("Mail events: dropping message %s, whose body is not an event", message.get("id"))
            acks.append({"lease_id": lease})
            continue
        try:
            async with core.users.async_session_maker() as session:
                await handle_event(session, event)
        except Exception as exc:
            first_failure = first_failure or exc
            retries.append({"lease_id": lease})
            continue
        acks.append({"lease_id": lease})

    if first_failure is not None:
        logger.warning(
            "Mail events: %d of %d events could not be applied and go back on the queue",
            len(retries),
            len(messages),
            exc_info=first_failure,
        )
    if acks or retries:
        answer = await _call(client, queue, "ack", {"acks": acks, "retries": retries})
        warnings = answer.get("warnings")
        if isinstance(warnings, dict) and warnings:
            logger.warning("Mail events: the queue settled %d leases with a warning", len(warnings))
    return len(acks), len(retries)


def _longer(backoff: float) -> float:
    return min(backoff * 2, _BACKOFF_MAX_S) if backoff else _BACKOFF_FIRST_S


async def events_task() -> None:
    """The lifespan's task: drain the queue for as long as the process runs.

    Returns at once, having said so, when the queue is not configured. Never
    raises otherwise: a failed round is logged and tried again after a wait
    that grows while the failures continue.
    """
    queue = _configured_queue()
    if queue is None:
        return
    logger.info("Mail events: on, applying bounces and complaints from Cloudflare queue %s", queue.queue)

    backoff = 0.0
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=_transport) as client:
        while True:
            try:
                settled, failed = await _drain(client, queue)
            except Exception as exc:
                backoff = _longer(backoff)
                if _quotes_token(exc, queue.token):
                    # Neither the message nor the traceback, which repeats it.
                    logger.warning(
                        "Mail events: %s, whose text quotes CLOUDFLARE_EMAIL_EVENTS_TOKEN and is withheld; "
                        "trying again in %.0fs",
                        type(exc).__name__,
                        backoff,
                    )
                else:
                    logger.warning(
                        "Mail events: %s: %s; trying again in %.0fs",
                        type(exc).__name__,
                        exc,
                        backoff,
                        # A refusal or a network fault explains itself; anything
                        # else is a fault here, and wants its traceback.
                        exc_info=None if isinstance(exc, (httpx.HTTPError, _QueueRefused)) else exc,
                    )
                await asyncio.sleep(backoff)
                continue
            if failed:
                # The events went back on the queue, and the next pull would
                # hand them straight back to whatever is failing.
                backoff = _longer(backoff)
                await asyncio.sleep(backoff)
                continue
            if backoff:
                logger.info("Mail events: the queue is being drained again")
                backoff = 0.0
            # Straight back for more only after a batch was settled. A batch
            # with nothing in it to settle would come back the same, and asking
            # for it in a loop would spend Cloudflare's API rate limit, which is
            # counted per user across every token that user made.
            if not settled:
                await asyncio.sleep(_IDLE_S)
