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

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeClaim, NodeClaimChallenge
from core.users import NodeOwner


@dataclass(frozen=True)
class ClaimStatus:
    """What a node is told about its own claim, derived rather than stored.

    Three states and a flag. `undeliverable` is not a fourth state: a node whose
    address bounced is unclaimed, its field is live and a different address is
    accepted normally, and the flag only changes what its setup UI should say
    about the address still on file.
    """

    state: str
    email: str | None
    undeliverable: bool

    def as_downlink(self) -> dict[str, object]:
        """The three fields a node's own responses carry, as one mapping.

        The models declaring them have no defaults, so a call site that dropped
        one would fail at construction. This is what stops a call site being
        able to drop one at all.
        """
        return {
            "claim_state": self.state,
            "claim_email": self.email,
            "claim_undeliverable": self.undeliverable,
        }


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


async def read_owner(session: AsyncSession, node_id: str) -> str | None:
    """The user this node belongs to, or None.

    Read through the injected session rather than through core.auth's own, which
    opens a second one against the application's database: a request handler
    that already holds a session should not reach past it, and the two would not
    be in the same transaction if it did.
    """
    return (await session.execute(select(NodeOwner.user_id).where(NodeOwner.node_id == node_id))).scalar_one_or_none()


async def claim_status(session: AsyncSession, node_id: str, now: float) -> ClaimStatus:
    """Derive what the node should be told, from the three rows that say it.

    The order is the order the facts outrank each other. An owner settles it
    whatever else is on file, since the challenge that produced the binding is
    spent and any later one cannot outrank it. A bounced address comes next,
    because the useful thing to say about an address nobody can receive at is
    that, not that the node is unclaimed like any other.

    An expired challenge reads as unclaimed with the address still on file,
    which is what lets a fresh one be asked for without retyping it.
    """
    owner = await read_owner(session, node_id)
    claim = await read_claim(session, node_id)
    if owner is not None:
        return ClaimStatus("owned", claim.email if claim is not None else None, False)
    if claim is not None and claim.undeliverable:
        return ClaimStatus("unclaimed", claim.email, True)

    if claim is None:
        # No address has ever been offered, so there is no challenge to find: a
        # challenge is only ever written alongside one, in the same transaction.
        # Worth the branch because this is the common case and this runs on
        # every heartbeat from every node.
        return ClaimStatus("unclaimed", None, False)

    challenge = await read_challenge(session, node_id)
    if challenge is not None and challenge.expires_at > now:
        return ClaimStatus("pending", challenge.email, False)
    return ClaimStatus("unclaimed", claim.email, False)


async def claim_addresses(node_ids: list[str]) -> dict[str, str | None]:
    """The address each of these nodes was claimed with, keyed by node.

    One query for the whole list, because the caller is rendering a table and a
    lookup per row is how a page that was fast at three nodes stops being fast
    at thirty. Nodes with no address are absent rather than null, so a caller
    reads the two the same way through `.get`.

    Only a verified address counts. An owner reached any other way, such as an
    administrator's assignment, can sit beside an address somebody offered and
    nobody confirmed, including one its recipient declined, and that is not what
    the node was claimed with.

    Opens its own session, unlike everything else here: the one caller is an
    owner-facing route in the `core/auth.py` style, which reaches the database
    through the shared maker rather than an injected session.
    """
    if not node_ids:
        return {}
    from core.users import async_session_maker

    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(NodeClaim.node_id, NodeClaim.email).where(
                    NodeClaim.node_id.in_(node_ids), NodeClaim.verified.is_(True)
                )
            )
        ).all()
    return {node_id: email for node_id, email in rows if email is not None}
