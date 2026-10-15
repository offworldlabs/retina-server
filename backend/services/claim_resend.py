"""Sending a node's claim mail: when the node offers an address, and whenever
somebody asks for it again.

Three may ask again, under one set of rules (`resend`) and one allowance: the
node itself (POST /v1/nodes/claim/resend), whoever holds a link a claim mail
carried (POST /api/auth/claim/resend, from the claim page), and the account the
claim is addressed to (POST /api/auth/me/claims/{node_ref}/resend, from the
dashboard). The allowance is claim_rate_limiter's, per node and per address,
and all three spend from the same counters, so asking from three places sends
an address no more mail than asking from one.

The node is the authority on its own address and the other two are not. They
ask for the mail the claim already has, to the address already on file, and
are refused once the claim has moved on: a correction, a click, a decline, a
bounce or a release. That is asked again under the write lock the send takes,
so a page left open cannot put back an address the node has just replaced. A
decline holds until the node asks again, by its resend or with another
address: whoever declined cannot mail themselves a fresh link. Nor is a person
ever answered as if a mail went when none did: where the node is told only
where its claim stands, a person is told to wait, or that there is nothing to
send.

Distinct from services/claim_links.py, which mints the link and queues its
mail, and from services/node_claim_store.py, which keeps the claim. What each
outcome is said as is the routes' business, so this answers with data.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node, NodeClaim
from core.users import MagicLink, RetiredAddress, User, forwarding_address, normalise_email
from services import claim_links, mail_outbox
from services.node_claim_store import (
    ClaimStatus,
    OpenClaim,
    claim_status,
    clear_declined,
    drop_challenge,
    node_challenged_with,
    open_claims_on,
    put_challenge,
    read_challenge,
    read_claim,
    set_claim_address,
)
from services.node_rate_limits import Refusal, claim_rate_limiter
from services.node_refusals import RATE_LIMITED_BODY

logger = logging.getLogger(__name__)


class Result(StrEnum):
    """What came of asking for a claim mail."""

    # A mail was queued; the outbox's worker sends it once this has committed.
    SENT = "sent"
    # The node has an owner, so nobody is asked to claim it.
    OWNED = "owned"
    # No address on file, or one that bounced. For somebody other than the
    # node, also a claim that is not the one their link was mailed for, or not
    # addressed to their account, or not open, or declined.
    NOTHING_TO_SEND = "nothing_to_send"
    # This server cannot send mail at all, so nothing was written.
    MAIL_UNAVAILABLE = "mail_unavailable"
    # Past the allowance, with `refusal` saying how long to wait.
    RATE_LIMITED = "rate_limited"
    # The address holds as many outstanding claim links as it may. Only the
    # node is answered this way; anybody else is RATE_LIMITED.
    LINK_CAP = "link_cap"
    # The node's row has gone.
    NO_NODE = "no_node"
    # The link minted for the mail was killed before it could be recorded, by
    # an address change or an account's deletion withdrawing the claim links
    # on its address (services/account_email.py, core/sessions.py). Nothing
    # went. The node is answered where its claim stands, as at LINK_CAP;
    # anybody else as a claim with nothing to send.
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Outcome:
    result: Result
    # Where the claim stands once this is over, which is what a node is told.
    # None where the answer says nothing about the claim: no mail can leave, a
    # limit, a node that has gone, a link that names no claim, or a person's
    # link withdrawn before it was recorded.
    status: ClaimStatus | None = None
    # The wait, with RATE_LIMITED.
    refusal: Refusal | None = None


@dataclass(frozen=True)
class Asked:
    """Somebody other than the node asking, and what they hold: the handle of
    the link they were mailed, or the addresses of the account they are
    signed in to."""

    handle: str | None = None
    addresses: frozenset[str] | None = None


def _verdict(status: ClaimStatus, claim: NodeClaim | None, handle: str | None, asked: Asked | None) -> str | Result:
    """The address to send the mail again to, or why there is none.

    The node's own rules, in the order it has always been answered: an owner
    first, then an address on file that has not bounced. Somebody else is
    first asked about what they hold, so a claim that is not theirs answers as
    one that does not exist, owned or not, and is held to an open claim
    besides, as the dashboard lists them: unverified and not declined as well
    as unowned and deliverable, with the address the mail goes to the one on
    the claim's own row. `claim` is that row and `handle` the node's
    outstanding challenge's, both read only for somebody else.
    """
    if asked is not None:
        if asked.handle is not None and handle != asked.handle:
            return Result.NOTHING_TO_SEND
        if asked.addresses is not None and status.email not in asked.addresses:
            return Result.NOTHING_TO_SEND
    if status.state == "owned":
        return Result.OWNED
    if status.email is None or status.undeliverable:
        return Result.NOTHING_TO_SEND
    if asked is not None and (claim is None or claim.verified or claim.declined or claim.email != status.email):
        return Result.NOTHING_TO_SEND
    return status.email


async def _judge(
    session: AsyncSession, node_id: str, now: float, asked: Asked | None
) -> tuple[ClaimStatus, str | Result]:
    """Where the node's claim stands, and `_verdict` on it.

    The challenge and the claim's own row are read only for somebody other
    than the node: the node's rules need nothing claim_status has not read.
    """
    status = await claim_status(session, node_id, now)
    if asked is None:
        return status, _verdict(status, None, None, None)
    claim = await read_claim(session, node_id)
    challenge = await read_challenge(session, node_id)
    return status, _verdict(status, claim, challenge.handle if challenge is not None else None, asked)


async def resend(session: AsyncSession, node_id: str, now: float, *, asked: Asked | None = None) -> Outcome:
    """Mint and send a fresh challenge for the address already on file, if the
    rules allow one. The node asks with `asked` left out.

    Refused on a node that already has an owner, and nothing is sent to an
    address that is not on file or that bounced hard: a second copy to an
    address that does not exist earns nothing but a second bounce, and bounces
    are how a sending domain loses its reputation. Past those, `send` says what
    else stops it.

    Fresh rather than a second copy of the outstanding one: the link is the
    thing that expires, so re-sending the old token would mail somebody a link
    with less life left in it than the one they did not get.
    """
    status, verdict = await _judge(session, node_id, now, asked)
    if isinstance(verdict, Result):
        return Outcome(verdict, status)
    return await send(session, node_id, verdict, now, asked=asked)


async def send(session: AsyncSession, node_id: str, email: str, now: float, *, asked: Asked | None = None) -> Outcome:
    """Record a fresh challenge for `email`, queue its mail, and kill whatever it displaced.

    Nothing at all is written while this server cannot send mail: the caller
    is told so rather than `pending`, which would have somebody watch an inbox
    nothing is coming to. Asked first, so the refusal spends no allowance and
    mints no link.

    The challenge, the address, the mail and the death of the link they
    replace are one transaction. A mail goes if and only if the challenge it
    carries is on record, a displaced link dies with the row that named it,
    and a mail still queued for that link is cancelled with it. Nobody holds
    the new token until the mail goes, which is after the commit, so there is
    never a moment when two links for one node are in anybody's hands.

    The new link is minted before that transaction, because the link store
    commits on a connection of its own and SQLite would have it wait on this
    one's write lock. A failure between the two leaves a link nobody was sent,
    which is killed when the failure is one this expects and otherwise expires
    unused. Nor is the link sure to last until the write: an address change
    or an account's deletion committing in between withdraws the claim links
    on its address. So it is looked for again under the write lock, and one
    that has gone is neither recorded nor mailed (Result.WITHDRAWN).

    The node asking is a fresh nomination of its address, so once it is
    admitted it clears a decline recorded on the claim, with the mail or, at
    an address at its cap of links or for a link withdrawn, with none
    (_nominated_with_no_link). Turned away before that, while no mail can
    leave, past the allowance, or for a node that has gone, it leaves the
    decline as it stands, with everything else.

    Somebody other than the node (`asked`) is not the authority on its address,
    so two things differ for them. The claim is judged again once the write
    lock is held, and a claim that has moved on since is refused as it would
    have been up front, with nothing written. And an address at its cap of
    links, or a link withdrawn before it could be recorded, is refused with
    nothing written either: the claim and any link still live are left as
    they stand.
    """
    reason = mail_outbox.unavailable()
    if reason is not None:
        # The node, never the address or anything about a link: nothing was minted.
        logger.warning("node_api: no claim mail can be sent for %s (%s), so nothing was recorded", node_id, reason)
        return Outcome(Result.MAIL_UNAVAILABLE)

    node = await session.get(Node, node_id)
    if node is None:
        return Outcome(Result.NO_NODE)
    node_ref = node.node_ref

    refusal = await claim_rate_limiter.admit(session, node_id, email)
    if refusal is not None:
        return Outcome(Result.RATE_LIMITED, refusal=refusal)

    challenge = await claim_links.issue(email=email, node_id=node_id, now=now)
    # Everything read before this point is read again, under the write lock the
    # first write below takes: a request for this node that committed in the
    # meantime has replaced the challenge, and the handle to kill is the one it
    # left, not the one claim_status saw.
    session.expire_all()

    if challenge is None:
        if asked is not None:
            # A person is told to wait, never that a mail went. The links
            # holding the address up are claim links, which last no longer
            # than this.
            wait = Refusal(429, dict(RATE_LIMITED_BODY), claim_links.CHALLENGE_EXPIRY_S)
            return Outcome(Result.RATE_LIMITED, refusal=wait)
        await mail_outbox.cancel(session, node_id=node_id)
        return await _nominated_with_no_link(session, node_id, email, Result.LINK_CAP)

    try:
        # Cancelled before the new mail is queued, which it would otherwise
        # cancel too. Its UPDATE takes the write lock, so what is judged below
        # cannot change before the commit.
        await mail_outbox.cancel(session, node_id=node_id)
        if asked is not None:
            status, verdict = await _judge(session, node_id, now, asked)
            if verdict != email:
                # Rolled back whole, the cancelled mail included.
                await session.rollback()
                await claim_links.invalidate(challenge.handle)
                logger.info("node_api: the claim on %s moved on before its mail could go again, so none went", node_id)
                return Outcome(verdict if isinstance(verdict, Result) else Result.NOTHING_TO_SEND, status)
        if not await _unspent(session, challenge.handle):
            # Recorded, it would have the node read `pending` on a link nobody
            # can open, and its mail would carry that link.
            logger.info("node_api: the link minted for %s was withdrawn before it could be sent, so none went", node_id)
            if asked is not None:
                # Rolled back whole, the cancelled mail included.
                await session.rollback()
                return Outcome(Result.WITHDRAWN)
            return await _nominated_with_no_link(session, node_id, email, Result.WITHDRAWN)
        displaced = await put_challenge(session, node_id, email, challenge.handle, challenge.expires_at)
        if displaced is not None:
            await claim_links.invalidate_in(session, displaced)
        await set_claim_address(session, node_id, email)
        if asked is None:
            await clear_declined(session, node_id)
        await claim_links.queue(session, challenge, email=email, node_id=node_id, node_ref=node_ref)
    except claim_links.MailUnavailable as exc:
        # The configuration changed since it was asked about above. Rolled back
        # whole, so the claim stands as it did, the displaced link included.
        await session.rollback()
        await claim_links.invalidate(challenge.handle)
        logger.warning(
            "node_api: the claim mail for %s could not be queued (%s), so nothing was recorded", node_id, exc
        )
        return Outcome(Result.MAIL_UNAVAILABLE)
    await session.commit()
    mail_outbox.wake()

    logger.info("node_api: %s has a claim outstanding", node_id)
    return Outcome(Result.SENT, ClaimStatus("pending", email, False))


async def _unspent(session: AsyncSession, handle: str) -> bool:
    """Whether the link `handle` names is still in the link store, unspent.

    Read in the caller's transaction, so from its first write on the answer
    holds to its commit: whatever kills a link writes, and waits on the lock.
    """
    found = await session.execute(
        select(MagicLink.token_hash).where(MagicLink.token_hash == handle, MagicLink.used_at.is_(None))
    )
    return found.first() is not None


async def _nominated_with_no_link(session: AsyncSession, node_id: str, email: str, result: Result) -> Outcome:
    """Commit the node's nomination of `email` with no link out for it, the
    mail queued for the last one cancelled by the caller, and answer the node
    where that leaves its claim: unclaimed, with the address on file.

    The node is told where its claim stands and nothing else: whether a link
    went out is what an attacker wants told, and the node cannot act on the
    difference anyway. The row naming the displaced link goes with it, or the
    node would read `pending` for a link nobody can click.
    """
    displaced = await drop_challenge(session, node_id)
    if displaced is not None:
        await claim_links.invalidate_in(session, displaced)
    await set_claim_address(session, node_id, email)
    await clear_declined(session, node_id)
    await session.commit()
    return Outcome(result, ClaimStatus("unclaimed", email, False))


async def resend_by_link(session: AsyncSession, token: str, now: float) -> Outcome:
    """The claim mail again, for whoever holds a link one carried, while that
    link is still its claim's newest.

    Expired is fine, and is what the claim page offers this for. Spent,
    declined, replaced by a later link, or cleared with the claim by a release
    or a bounce is not, and answers as a token that never existed. The mail
    goes to the address the node gave, which is where the link went.
    """
    handle = claim_links.handle_for(token)
    node_id = await node_challenged_with(session, handle)
    if node_id is None:
        return Outcome(Result.NOTHING_TO_SEND)
    return await resend(session, node_id, now, asked=Asked(handle=handle))


async def addresses_of(session: AsyncSession, user: User) -> frozenset[str]:
    """Every address a claim can reach this account by: the one it has, and
    the ones it retired whose mail comes to it. A retired address another
    account has as its own since is that account's and is mailed itself, so it
    is not listed here or mailed on this account's say-so (core/users.py,
    forwarding_address).
    """
    retired = (
        await session.execute(select(RetiredAddress.email).where(RetiredAddress.user_id == str(user.id)))
    ).scalars()
    forwarded = [email for email in retired.all() if await forwarding_address(session, email) is not None]
    return frozenset({normalise_email(user.email), *forwarded})


async def open_claims_for(session: AsyncSession, user: User, now: float) -> list[OpenClaim]:
    """The open claims addressed to this account, by node_ref, each with its
    challenge's expiry while that is live, and None otherwise."""
    claims = await open_claims_on(session, await addresses_of(session, user))
    return [
        OpenClaim(c.node_ref, c.email, c.expires_at if c.expires_at is not None and c.expires_at > now else None)
        for c in claims
    ]


async def resend_for_account(session: AsyncSession, user: User, node_ref: str, now: float) -> Outcome:
    """The claim mail again, for the account an open claim on `node_ref` is
    addressed to.

    A node that is unknown, has no claim, or has one addressed to anybody else
    answers alike, so the answer says nothing about a node that is not this
    account's business. A claim to one of its retired addresses is mailed to
    the address it has now, as every claim mail for a retired one is
    (services/claim_links.py, queue).
    """
    node_id = (await session.execute(select(Node.node_id).where(Node.node_ref == node_ref))).scalar_one_or_none()
    if node_id is None:
        return Outcome(Result.NOTHING_TO_SEND)
    return await resend(session, node_id, now, asked=Asked(addresses=await addresses_of(session, user)))
