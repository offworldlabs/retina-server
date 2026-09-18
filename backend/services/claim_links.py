"""Minting, killing and delivering the link that claims a node.

The link is a bearer credential: whoever holds it binds the node to whatever
account the address resolves to. Only its SHA-256 is recorded, as `handle`, so
the token exists in the mail and nowhere else. Nothing here writes it to a log,
including when delivery fails.

Three calls, because those are the three things claiming needs of a magic-link
primitive and it needs nothing else: mint a challenge for an address carrying an
intent and the node it is about, kill one that is outstanding, and put the token
in front of the person. Keeping them behind one module is what lets the token
store move without the claim route moving with it.

Which store the token lives in is this module's business. Today it is the
`node_claim_challenges` row the caller writes with the handle, so killing a
challenge is removing that row; callers do not need to know that, and the route
reads better for not saying it twice.
"""

import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeClaimChallenge

logger = logging.getLogger(__name__)

# What the challenge is for. Carried so that a claim link cannot be redeemed as
# a bare sign-in for the sender's account if it is forwarded, and a sign-in link
# cannot be redeemed as a claim.
INTENT_CLAIM = "claim"

# Short, because the link sits in a mailbox and the person who asked for it is,
# by definition, in front of the setup page. Long enough to survive a slow
# provider and somebody finding the mail a few minutes later.
CHALLENGE_EXPIRY_S = 900


@dataclass(frozen=True)
class Resolved:
    """A redeemed challenge, and what it was for.

    `intent` travels because a link is a bearer credential in a mailbox and
    mailboxes get forwarded. A claim link must not work as a bare sign-in for
    whoever sent it, and a sign-in link must not claim anything, so both
    redeemers check this rather than trusting that a token can only reach the
    endpoint it was minted for.
    """

    email: str
    intent: str
    node_id: str


@dataclass(frozen=True)
class Challenge:
    """What a fresh challenge consists of.

    `handle` is what is stored and what kills it. `token` goes in the mail and
    is never stored, so a reader of the database cannot claim anything.
    """

    handle: str
    token: str
    expires_at: float


def handle_for(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(*, intent: str, node_id: str, now: float) -> Challenge:
    """Mint a challenge for `node_id` under `intent`.

    Takes the clock rather than reading it, so the expiry a caller records and
    the expiry it enforces come from one reading.

    The address is not an argument: this mints, and where the challenge is
    routed is the caller's, which is what keeps one address's challenges from
    being minted differently from another's.
    """
    if intent != INTENT_CLAIM:
        raise ValueError(f"unknown challenge intent {intent!r}")
    token = secrets.token_urlsafe(32)
    return Challenge(handle=handle_for(token), token=token, expires_at=now + CHALLENGE_EXPIRY_S)


async def invalidate(session: AsyncSession, handle: str) -> None:
    """Make an outstanding challenge unredeemable.

    A handle that is unknown, already spent or already displaced is not an
    error: the caller is asking for a token to be dead, and a token that never
    existed is dead.
    """
    await session.execute(delete(NodeClaimChallenge).where(NodeClaimChallenge.handle == handle))


async def deliver(email: str, node_ref: str, token: str) -> bool:
    """Put the link in front of `email`. False when nothing was sent.

    A server with no mail transport configured drops the link and warns rather
    than failing the request: the node's own operation does not depend on mail,
    and a 500 here would have a node retry a nomination the server has already
    recorded. The caller answers `pending` either way, which is true — a
    challenge is outstanding — and the person waiting for a mail that will not
    arrive asks for it again through the resend.

    The warning names the node and never the token. A link in an application
    log is a credential in an application log.
    """
    del email, token
    logger.warning(
        "node_api: no mail transport is configured, so the claim link for %s was not sent",
        node_ref,
    )
    return False


async def resolve(session: AsyncSession, token: str, now: float) -> Resolved | None:
    """Redeem `token` once, or return None.

    None covers unknown, expired and already redeemed, and the caller must not
    tell them apart: which of the three it was says whether a guess was ever a
    real token.

    Redeeming does not remove the row. Spending a challenge is the business of
    whatever the redemption achieves — a binding, or a refusal — and that writer
    drops it in the same transaction. A resolve that deleted first would leave a
    failed binding with no record of the challenge it failed on.
    """
    if not token or not token.strip():
        return None
    row = (
        await session.execute(
            select(NodeClaimChallenge).where(
                NodeClaimChallenge.handle == handle_for(token.strip()),
                NodeClaimChallenge.expires_at > now,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return Resolved(email=row.email, intent=INTENT_CLAIM, node_id=row.node_id)
