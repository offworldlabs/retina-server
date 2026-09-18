"""Minting, killing, reading and delivering the link that claims a node.

The link is a bearer credential: whoever holds it binds the node to whatever
account the address resolves to. Only its SHA-256 is recorded, as `handle`, so
the token exists in the mail and nowhere else. Nothing here writes it to a log,
including when delivery fails.

Four calls, because those are the things claiming needs of the magic-link
primitive and it needs nothing else: mint a challenge for an address carrying an
intent and the node it is about, kill one that is outstanding, read one without
spending it, and put the token in front of the person.

Which store the token lives in is this module's business. It is `magic_links`,
shared with sign-in, which is why `intent` exists there: a claim link must not
open a session for whoever it was forwarded to. The per-node
`node_claim_challenges` row is a different thing and stays where it is — it
carries the "one pending challenge per node" rule, which is this design's and
not the primitive's.
"""

import logging
from dataclasses import dataclass

from core.auth import (
    MAGIC_LINK_EXPIRY_S,
    _hash_token,
    create_magic_link,
    invalidate_magic_link,
    peek_magic_link,
    redeem_magic_link,
)
from services import mail

logger = logging.getLogger(__name__)

# What the challenge is for, matching the value stored on `magic_links.intent`.
INTENT_CLAIM = "claim"

# Named here because the callers that record an expiry beside a challenge read
# it from here rather than reaching into the primitive.
CHALLENGE_EXPIRY_S = MAGIC_LINK_EXPIRY_S

# `/dash/` because HOST_APP serves the map bundle at `/` and mounts the
# dashboard beneath, and the dashboard's router carries a matching basename: a
# link to `/auth/claim/...` would render the map and never redeem the token.
# This, routes/auth.py's `_SIGN_IN_PATH` and dashboard/src/utils/basePath.ts all
# have to agree.
_CLAIM_PATH = "/dash/auth/claim/"


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


def _body(node_ref: str, link: str) -> str:
    """A request naming the node, with a decline, rather than a notification
    that assumes consent.

    Whoever receives this may have nothing to do with the node: anyone who can
    get one registered can make this server write to an address of their
    choosing, which is a surface claim codes never had. So it reads as somebody
    asking, it says what happens if they ignore it, and the page it opens offers
    declining as plainly as accepting.
    """
    return (
        f"Someone entered this address while setting up a RETINA node, {node_ref}.\n\n"
        f"{link}\n\n"
        "Opening that link connects the node to this address and signs you in. The link works "
        f"once and expires in {CHALLENGE_EXPIRY_S // 60} minutes.\n\n"
        "If this wasn't you, somebody has mistyped their own address. The page will let you say "
        "so, and nothing about the node changes in the meantime."
    )


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
    if not mail.is_configured():
        logger.warning("node_api: no mail transport is configured, so the claim link for %s was not sent", node_ref)
        return False
    link = mail.link_to(_CLAIM_PATH + token)
    if link is None:
        logger.warning("node_api: HOST_APP is not set, so the claim link for %s cannot be addressed", node_ref)
        return False
    # Handed to a background thread so the response time does not vary with the
    # provider's, which would otherwise time how long a send took and so say
    # whether one happened.
    mail.send_in_background(email, f"Connect your RETINA node {node_ref}", _body(node_ref, link))
    return True
