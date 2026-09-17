"""Persisting which address a node was claimed with, and its one pending challenge.

Two tables with different lifetimes. `node_claims` is durable and survives
re-registration; `node_claim_challenges` exists only between a nomination and
the click that spends it, and its primary key is the node, so the "one pending
challenge per node" rule is the schema's rather than a convention kept here.

Nothing commits. The routes own their transactions, as they do for
node_config_store and node_contact_store.

Addresses arrive normalised: trimmed and lower cased by whichever caller
validated them. The comparisons below are exact, so an address normalised by
one caller and not another silently matches nothing, and that no-op is
indistinguishable from the deliberate one for a superseded address.

Nothing here sends mail or talks to the magic-link primitive either. A writer
that displaces a challenge still live hands back its handle so the caller can
invalidate it in the order it chooses; an outbound call from a store module
would fix that order here and make a store a client. A writer that spends one
hands back nothing, a redeemed token being dead already.
"""

from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeClaim, NodeClaimChallenge


async def read_claim(session: AsyncSession, node_id: str) -> NodeClaim | None:
    """The node's claim row, or None if it has never been given an address."""
    return await session.get(NodeClaim, node_id)


async def set_claim_address(session: AsyncSession, node_id: str, email: str) -> NodeClaim:
    """Nominate `email` for this node and return the row.

    Writing the address already stored changes nothing, which is what lets a
    node resend its address on every configuration sync without unverifying
    itself. A different address is a fresh start: the confirmation and the
    bounce both belonged to the address being replaced, so neither survives it.
    """
    row = await session.get(NodeClaim, node_id)
    if row is None:
        row = NodeClaim(
            node_id=node_id,
            email=email,
            verified=False,
            undeliverable=False,
            # Written here rather than left to the column's server default,
            # which SQLAlchemy does not read back after the INSERT: the
            # returned row would carry None where its type says a datetime.
            updated_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        return row

    if row.email != email:
        row.email = email
        row.verified = False
        row.undeliverable = False
        row.updated_at = datetime.now(UTC)
        await session.flush()
    return row


async def mark_verified(session: AsyncSession, node_id: str, email: str) -> None:
    """Record that `email` was confirmed, if it is still the address on file.

    Drops the challenge, because a challenge redeemed is a challenge spent and
    a row that outlives its redemption is a link that can be walked into twice.
    Nothing downstream should have to notice that the node has since acquired an
    owner in order for the second click to fail.

    The address is checked rather than assumed because a click can arrive after
    the address it was mailed to has been corrected, and confirming the
    correction on the strength of a link sent to the typo is exactly the
    mis-binding the replacement rule exists to prevent.

    No handle comes back, unlike the two writers below. A redeemed token is
    already dead wherever it is recorded; a displaced one is not, which is the
    difference that makes them hand theirs over.
    """
    row = await session.get(NodeClaim, node_id)
    if row is None or row.email != email:
        return
    row.verified = True
    row.updated_at = datetime.now(UTC)
    await drop_challenge(session, node_id)
    await session.flush()


async def mark_undeliverable(session: AsyncSession, node_id: str, email: str) -> str | None:
    """Record that `email` bounced hard, if it is still the address on file.

    Returns the handle of the challenge this drops, as `put_challenge` does:
    nobody is going to click a link at an address that does not exist, and
    leaving the challenge standing would leave the node reading `pending` for
    ever. Dropping it is what puts the node back to unclaimed, where a
    different address is accepted normally; the flag only changes what its
    setup UI says about the address it is still carrying.

    The address is checked for the same reason as the confirmation above: a
    bounce for an address that has since been replaced says nothing about its
    replacement.
    """
    row = await session.get(NodeClaim, node_id)
    if row is None or row.email != email:
        return None
    row.undeliverable = True
    row.updated_at = datetime.now(UTC)
    handle = await drop_challenge(session, node_id)
    await session.flush()
    return handle


async def clear_claim(session: AsyncSession, node_id: str) -> None:
    """Return the node to the unclaimed state: no address, no challenge.

    The address goes with the binding. It is a spent token recording what the
    node was claimed with, and a second-hand node that kept it would show its
    next owner the last one's address.
    """
    await session.execute(delete(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id))
    await session.execute(delete(NodeClaim).where(NodeClaim.node_id == node_id))


async def read_challenge(session: AsyncSession, node_id: str) -> NodeClaimChallenge | None:
    """The node's outstanding challenge, or None."""
    return await session.get(NodeClaimChallenge, node_id)


async def put_challenge(
    session: AsyncSession,
    node_id: str,
    email: str,
    handle: str,
    expires_at: float,
) -> str | None:
    """Make this the node's pending challenge, returning the handle it displaced.

    A returned handle is one the caller must invalidate: the row is gone, so
    nothing else will ever find it, and the link it belongs to would otherwise
    stay live in a mailbox.
    """
    row = await session.get(NodeClaimChallenge, node_id)
    if row is None:
        session.add(
            NodeClaimChallenge(
                node_id=node_id,
                email=email,
                handle=handle,
                created_at=datetime.now(UTC).timestamp(),
                expires_at=expires_at,
            )
        )
        await session.flush()
        return None

    displaced = row.handle
    row.email = email
    row.handle = handle
    row.created_at = datetime.now(UTC).timestamp()
    row.expires_at = expires_at
    await session.flush()
    return displaced


async def drop_challenge(session: AsyncSession, node_id: str) -> str | None:
    """Remove the node's pending challenge, returning its handle if there was one."""
    row = await session.get(NodeClaimChallenge, node_id)
    if row is None:
        return None
    handle = row.handle
    await session.delete(row)
    await session.flush()
    return handle
