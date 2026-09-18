"""Claiming a node by email: the click, the decline and the release.

Orchestration rather than storage. It reaches for the magic-link primitive in
core/auth.py, the account helpers in core/users.py, and the two claim tables
through services/node_claim_store.py, and owns the order they happen in.

It lives here and not in core/auth.py because services/claim_links.py depends on
that module for the primitive, and a core module that depended back on a
claiming service would close the loop. Ownership is an authorisation boundary
and its writers are worth keeping in one direction.
"""

import logging
from enum import StrEnum

from sqlalchemy import select

from core.nodes import Node
from core.users import MagicLinkRefused, User, async_session_maker, get_or_create_magic_link_user
from services import claim_links
from services.node_claim_store import (
    clear_claim,
    drop_challenge,
    mark_verified,
    read_challenge,
    read_claim,
    read_owner,
)

logger = logging.getLogger(__name__)


# ── Claiming a node by email ──────────────────────────────────────────────────


class ClaimOutcome(StrEnum):
    """What a redeemed link achieved, as far as the clicker needs to be told."""

    BOUND = "bound"
    # The link was unknown, expired or already spent. One value for all three:
    # which it was says whether a guess was ever a real token.
    INVALID = "invalid"
    # Somebody else claimed the node between the link being sent and clicked.
    TAKEN = "taken"


async def _user_id_for(session, email: str) -> str | None:
    """The account behind an address, without creating one. None if there is none."""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    return str(user.id) if user is not None else None


async def complete_claim(token: str):
    """Redeem a claim link: bind the node, confirm the address, open the door.

    Returns the outcome, the account and the node's public handle, the last two
    being None unless the binding happened. The account rather than its id,
    because the caller needs it to mint a session and this function already
    holds it.

    One transaction. The binding and the confirmation are set by one event and a
    partial write would leave an address confirmed for a node nobody owns, or a
    node owned with no record of what claimed it.

    The account is resolved before that transaction opens, because creating a
    user runs its own session and nesting one inside this would deadlock on
    SQLite.
    """
    # Read, not spent: a refusal knowable up front, an incumbent owner or an
    # address that may not open a session, must not cost the clicker the link.
    # The read matches on intent as the spend does, so a sign-in link reaching
    # this reads exactly like a token that never existed.
    resolved = await claim_links.peek(token)
    if resolved is None:
        return ClaimOutcome.INVALID, None, None
    node_id, email = resolved.node_id, resolved.email

    async with async_session_maker() as session:
        existing = await read_owner(session, node_id)
        if existing is not None and existing != await _user_id_for(session, email):
            # Valid link, but the node acquired an owner since it was sent, and
            # not the one this link would bind it to. The joint proof this
            # design rests on is that the person holding the hardware is the
            # authority on where an *unowned* node goes; once there is an
            # incumbent, they are the authority and this click is not enough.
            # Release is the way through, not a stronger link.
            #
            # Looked up rather than created: a link forwarded to a node that is
            # already somebody else's must not leave an account behind for the
            # address it names.
            return ClaimOutcome.TAKEN, None, None

    try:
        user = await get_or_create_magic_link_user(email)
    except MagicLinkRefused:
        # An administrator's address may not open a session from a mailbox, and
        # binding a node without one would leave the clicker with no feedback
        # and no way in. Answered as an invalid link, which is how every other
        # refusal on this path reads.
        return ClaimOutcome.INVALID, None, None

    # Spent atomically, so two clicks on one link cannot both bind. The spend
    # runs its own session and cannot share the write below, whose re-read
    # catches a claim or a release landing in between.
    if await claim_links.redeem(token) is None:
        return ClaimOutcome.INVALID, None, None

    async with async_session_maker() as session:
        async with session.begin():
            # Re-read inside the writing transaction. The account lookup above
            # released the first one, so the check that nobody else owns this
            # node has to be the one that holds while the row is written.
            claim = await read_claim(session, node_id)
            if claim is not None and claim.user_id is not None:
                if claim.user_id != str(user.id):
                    return ClaimOutcome.TAKEN, None, None
                # A second link for the same address, such as one a resend
                # minted while the first click was binding, finds the binding
                # the first made. It is the same person's, so this succeeded
                # rather than collided: telling them their own node belongs to
                # somebody else would be both wrong and alarming. The link is
                # spent already; the row naming it goes too, like any other
                # that binds.
                await mark_verified(session, node_id, email)
                node = await session.get(Node, node_id)
                return ClaimOutcome.BOUND, user, node.node_ref if node else None
            # The node must still be offering the address this link was sent
            # to. A nomination of another address, or a release, between the
            # link being read and this write has replaced it, and binding would
            # hand the node to an address its holder has already withdrawn.
            if claim is None or claim.email != email:
                return ClaimOutcome.INVALID, None, None
            claim.user_id = str(user.id)
            # Spends the challenge as well as confirming the address, so the
            # link cannot be walked into twice. The same row as the binding, so
            # the two are one write.
            await mark_verified(session, node_id, email)
        node = await session.get(Node, node_id)

    logger.info("Node %s claimed by a verified address", node_id)
    return ClaimOutcome.BOUND, user, node.node_ref if node else None


async def preview_claim(token: str) -> str | None:
    """The node a link is for, without spending it.

    The landing page names the node before the click confirms, so that somebody
    who was mailed this by mistake declines a thing they can see rather than a
    thing they cannot. Reading it needs the same secret the click needs, so this
    reveals nothing to anyone who does not already hold the link.
    """
    resolved = await claim_links.peek(token)
    if resolved is None:
        return None
    async with async_session_maker() as session:
        node = await session.get(Node, resolved.node_id)
        return node.node_ref if node else None


async def decline_claim(token: str) -> bool:
    """Refuse a claim: drop the challenge and leave the node unowned.

    The address stays on file. It is what the node was offered, it is still what
    its setup UI should show, and clearing it would leave whoever is standing at
    the node with no idea why nothing happened.

    True when there was a challenge to decline.
    """
    # Spending the link is the decline: whoever refused it must not find it
    # still works from another tab.
    resolved = await claim_links.redeem(token)
    if resolved is None:
        return False
    async with async_session_maker() as session:
        async with session.begin():
            # Only the row naming this link. A resend or a corrected address
            # landing since it was redeemed has put a live link of its own there.
            challenge = await read_challenge(session, resolved.node_id)
            if challenge is not None and challenge.handle == claim_links.handle_for(token):
                await drop_challenge(session, resolved.node_id)
    logger.info("A claim request for node %s was declined by its recipient", resolved.node_id)
    return True


async def release_node(node_id: str, user_id: str) -> bool:
    """Hand a node back to the unowned state. False when this user does not own it.

    The address goes with the binding rather than staying behind. It is a spent
    token recording what the node was claimed with, and a second-hand node that
    kept it would show its next owner the last one's address.
    """
    async with async_session_maker() as session:
        async with session.begin():
            if await read_owner(session, node_id) != user_id:
                return False
            await clear_claim(session, node_id)
    logger.info("Node %s was released by its owner", node_id)
    return True
