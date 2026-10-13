"""Minting, killing, reading and mailing the link that claims a node.

The link is a bearer credential: whoever holds it binds the node to whatever
account the address resolves to. Only its SHA-256 is recorded, as `handle`, so
the token exists in the mail and nowhere else. Nothing here writes it to a log,
including when the mail cannot go.

Four things, because those are what claiming needs of the magic-link primitive
and it needs nothing else: mint a challenge for an address carrying an intent
and the node it is about, kill one that is outstanding, read one without
spending it, and put the token in front of the person.

Which store the token lives in is this module's business. It is `magic_links`,
shared with sign-in, which is why `intent` exists there: a claim link must not
open a session for whoever it was forwarded to. The per-node
`node_claim_challenges` row is a different thing and stays where it is — it
carries the "one pending challenge per node" rule, which is this design's and
not the primitive's.

The mail goes through the outbox (services/mail_outbox.py), queued in the
transaction that records the challenge, so a challenge on record is a mail that
will go and one rolled back is a mail that never does. What became of it comes
back through `_settled`, registered with the outbox when this module is
imported, which main.py's routes do before any worker starts: a link whose mail
could not be delivered is killed, and the node stops reading `pending` for it.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import (
    MAGIC_LINK_EXPIRY_S,
    _hash_token,
    create_magic_link,
    invalidate_magic_link,
    peek_magic_link,
    redeem_magic_link,
)
from core.mail_outbox import EXPIRED, FAILED, REJECTED, UNDELIVERABLE
from core.users import MagicLink
from services import mail, mail_outbox, mail_templates
from services.mail_outbox import KIND_CLAIM, Settled
from services.node_claim_store import drop_challenge, mark_undeliverable, read_challenge, read_claim

logger = logging.getLogger(__name__)

# What the challenge is for, matching the value stored on `magic_links.intent`.
INTENT_CLAIM = "claim"

# Named here because the callers that record an expiry beside a challenge read
# it from here rather than reaching into the primitive.
CHALLENGE_EXPIRY_S = MAGIC_LINK_EXPIRY_S

# The console's own route (ClaimPage), at the root of HOST_APP, beside
# routes/auth.py's `_SIGN_IN_PATH`.
_CLAIM_PATH = "/auth/claim/"


@dataclass(frozen=True)
class Resolved:
    """A challenge that has been read or spent, and what it was for."""

    email: str
    intent: str
    node_id: str


@dataclass(frozen=True)
class Challenge:
    """What a fresh challenge consists of.

    `handle` is what is stored beside the node and what kills it. `token` goes
    in the mail and is never stored, so a reader of the database cannot claim
    anything.
    """

    handle: str
    token: str
    expires_at: float


class MailUnavailable(Exception):
    """The claim mail cannot be queued, so nothing was.

    The message says which of the two causes it was, no host to address the
    link to or no usable outbox key, and never carries the link.
    """


def handle_for(token: str) -> str:
    return _hash_token(token)


async def issue(*, email: str, node_id: str, now: float) -> Challenge | None:
    """Mint a claim challenge for `node_id`, routed to `email`.

    None when the address is already holding as many outstanding claim links as
    it may. The caller answers its request the same way either way: whether a
    link went out is what an attacker wants told.

    Takes the clock rather than reading it, so the expiry a caller records beside
    the node and the expiry the primitive enforces come from one reading.
    """
    token = await create_magic_link(email, intent=INTENT_CLAIM, node_id=node_id, now=now)
    if token is None:
        return None
    return Challenge(handle=handle_for(token), token=token, expires_at=now + CHALLENGE_EXPIRY_S)


async def invalidate(handle: str) -> None:
    """Make an outstanding challenge unredeemable.

    A handle that is unknown, spent or already displaced is not an error: the
    caller is asking for a token to be dead, and a token that never existed is
    dead.
    """
    await invalidate_magic_link(handle)


async def invalidate_in(session: AsyncSession, handle: str) -> None:
    """`invalidate`, in the caller's transaction rather than one of its own.

    For a caller that is writing already: a route between its first write and
    its commit, or a settlement handler. SQLite takes one writer at a time, so
    `invalidate`'s own connection would wait on the caller's until it gave up.
    The link dies if and only if the caller commits, which is what lets it die
    with the row that named it.
    """
    await session.execute(delete(MagicLink).where(MagicLink.token_hash == handle))


async def peek(token: str) -> Resolved | None:
    """Read a live challenge without spending it, for a page that has to name
    the node before anybody presses anything."""
    link = await peek_magic_link(token, intent=INTENT_CLAIM)
    if link is None or link.node_id is None:
        return None
    return Resolved(email=link.email, intent=INTENT_CLAIM, node_id=link.node_id)


async def redeem(token: str) -> Resolved | None:
    """Spend a challenge once.

    None for unknown, expired, already redeemed, and issued for something other
    than claiming. The caller must not tell those apart: which of them it was
    says whether a guess was ever a real token.
    """
    link = await redeem_magic_link(token, intent=INTENT_CLAIM)
    if link is None or link.node_id is None:
        # A claim link with no node is not one this server minted; refusing it
        # keeps the node id a guarantee for everything downstream rather than
        # something each caller has to re-check.
        return None
    return Resolved(email=link.email, intent=INTENT_CLAIM, node_id=link.node_id)


async def queue(session: AsyncSession, challenge: Challenge, *, email: str, node_id: str, node_ref: str) -> None:
    """Queue the mail that puts `challenge`'s link in front of `email`.

    In the caller's transaction, flushed and never committed: the mail goes if
    and only if the challenge it carries is committed beside it. Call
    `mail_outbox.wake` after the commit.

    The caller asks `mail_outbox.unavailable` before minting anything, so this
    raising MailUnavailable means the configuration changed since: HOST_APP
    gone, or the key beside the database no longer usable. The caller rolls
    back and kills the link.

    The mail names the node by the handle used anywhere public, never by the
    identifier off the board.
    """
    link = mail.link_to(_CLAIM_PATH + challenge.token)
    if link is None:
        raise MailUnavailable("HOST_APP is not set")
    rendered = mail_templates.claim(node_ref, link, CHALLENGE_EXPIRY_S // 60)
    message = mail.Message(to=email, subject=rendered.subject, text=rendered.text, html=rendered.html)
    try:
        await mail_outbox.enqueue(
            session,
            message,
            kind=KIND_CLAIM,
            # The link's own expiry, so a mail held up past it is never sent.
            expires_at=challenge.expires_at,
            node_id=node_id,
            handle=challenge.handle,
        )
    except mail_outbox.OutboxKeyError as exc:
        raise MailUnavailable("the outbox key is unusable") from exc


async def _settled(session: AsyncSession, settled: Settled) -> None:
    """Put what became of a claim mail onto the claim it was for.

    Runs in the outbox's settlement transaction, so it never commits and never
    writes through a connection of its own: the settlement holds SQLite's one
    write lock until this returns.

    A mail that went changes nothing. One the provider says cannot reach the
    address marks the address undeliverable, as a bounce from the event feed
    does (services/mail_events.py). One that was refused, gave up, or ran out
    of link before it could go leaves the node with a challenge nobody was
    sent, so the challenge goes if it is still this mail's, and the node reads
    `unclaimed` with its address on file rather than `pending` for a mail that
    never left. A challenge that has replaced it since has a mail of its own,
    and is left alone.
    """
    node_id, handle = settled.node_id, settled.handle
    if node_id is None or handle is None:
        # Not a mail `queue` wrote: every claim mail names its node and link.
        return
    if settled.outcome == UNDELIVERABLE:
        await _undeliverable(session, node_id, settled.recipient, handle)
    elif settled.outcome in (REJECTED, FAILED, EXPIRED):
        if await _drop_if_current(session, node_id, handle):
            await invalidate_in(session, handle)
            logger.info(
                "node_api: the claim mail for %s came to %s, so its challenge is dropped", node_id, settled.outcome
            )


async def _drop_if_current(session: AsyncSession, node_id: str, handle: str) -> bool:
    """Drop the node's challenge if it is still the one `handle` names, and say whether it was."""
    challenge = await read_challenge(session, node_id)
    if challenge is None or challenge.handle != handle:
        return False
    await drop_challenge(session, node_id)
    return True


async def _undeliverable(session: AsyncSession, node_id: str, address: str, handle: str) -> None:
    """Mark `address` undeliverable on `node_id` and kill the links that went to it.

    Only an unverified claim with no owner, still offering this address, is
    marked, as the event feed marks only those: an address somebody confirmed by
    clicking has proved itself, an owned node reads `owned` whatever its
    address does, and a bounce from an address since replaced says nothing
    about its replacement. This mail's own link dies either way, since nobody
    at the address will ever click it, and so does its challenge if it is still
    the node's, as a refused mail's does, so the node stops reading `pending`
    for it. A challenge that has replaced it since has a mail of its own.
    """
    claim = await read_claim(session, node_id)
    await invalidate_in(session, handle)
    # Decided here rather than read back off the row after the marking, so it
    # holds whether `claim` is the session's own row or a copy of it.
    if claim is None or claim.user_id is not None or claim.verified or claim.email != address:
        await _drop_if_current(session, node_id, handle)
        return
    dropped = await mark_undeliverable(session, node_id, address)
    if dropped is not None and dropped != handle:
        await invalidate_in(session, dropped)
    # A resend to the same address, queued while this mail was out, carries the
    # link the marking just dropped, to an address that will not take it.
    await mail_outbox.cancel(session, node_id=node_id)
    logger.info("node_api: the claim mail for %s cannot be delivered, so its address is marked undeliverable", node_id)


mail_outbox.on_settled(KIND_CLAIM, _settled)
