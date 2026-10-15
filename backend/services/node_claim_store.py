"""Persisting who owns a node, the address it was claimed with, and its one
pending challenge.

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

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node, NodeClaim, NodeClaimChallenge


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
    """The node's claim row, or None if it has neither an owner nor an address."""
    return await session.get(NodeClaim, node_id)


async def set_claim_address(session: AsyncSession, node_id: str, email: str) -> NodeClaim:
    """Nominate `email` for this node and return the row.

    Writing the address already stored changes nothing, which is what lets a
    node resend its address on every configuration sync without unverifying
    itself, or taking back a decline. A different address is a fresh start:
    the confirmation, the bounce and the decline all belonged to the address
    being replaced, so none of them survives it.
    """
    row = await session.get(NodeClaim, node_id)
    if row is None:
        row = NodeClaim(
            node_id=node_id,
            email=email,
            verified=False,
            undeliverable=False,
            declined=False,
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
        row.declined = False
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
    replacement. It is checked by the statement that writes the flag, so it
    holds whether or not the caller took SQLite's write lock before calling.
    """
    marked = await _mark_undeliverable(session, email, [node_id])
    return marked.get(node_id)


# What makes a claim open: nobody has confirmed the address, nobody owns the
# node, and nothing has flagged the address already.
_OPEN = (NodeClaim.verified.is_(False), NodeClaim.user_id.is_(None), NodeClaim.undeliverable.is_(False))


async def open_claims(session: AsyncSession, email: str) -> list[str]:
    """The nodes whose claim is open on `email`, as `mark_open_undeliverable` means it."""
    return list((await session.execute(select(NodeClaim.node_id).where(NodeClaim.email == email, *_OPEN))).scalars())


@dataclass(frozen=True)
class OpenClaim:
    """An open claim as the person it is addressed to is shown it: the node by
    its public handle, the address it was given, and when its challenge lapses,
    or None when it has none."""

    node_ref: str
    email: str
    expires_at: float | None


async def open_claims_on(session: AsyncSession, emails: Collection[str]) -> list[OpenClaim]:
    """The claims open on any of `emails`, as `open_claims` means it, that the
    address has not declined, by node_ref.

    For the person at the address, which is why a decline counts here and not
    in `_OPEN`: it closes the claim to them, and leaves the facts a bounce is
    recorded against as they are.

    The challenge's expiry is given whether or not it has passed: whether it is
    still live is the caller's to say, against its own clock.
    """
    if not emails:
        return []
    rows = await session.execute(
        select(Node.node_ref, NodeClaim.email, NodeClaimChallenge.expires_at)
        .join(Node, Node.node_id == NodeClaim.node_id)
        .outerjoin(NodeClaimChallenge, NodeClaimChallenge.node_id == NodeClaim.node_id)
        .where(NodeClaim.email.in_(emails), *_OPEN, NodeClaim.declined.is_(False))
        .order_by(Node.node_ref)
    )
    return [OpenClaim(ref, email, expires_at) for ref, email, expires_at in rows.tuples() if email is not None]


async def mark_open_undeliverable(session: AsyncSession, email: str, node_ids: list[str]) -> dict[str, str | None]:
    """Record that `email` bounced hard on whichever of `node_ids` still hold it
    as an open claim, and return those nodes, each with the handle of the
    challenge dropped, or None where there was none.

    Open is unverified, with no owner and not flagged already. An address
    somebody confirmed by clicking has proved itself, an owned node reads
    `owned` whatever its address does, and leaving flagged rows alone is what
    lets the same verdict arrive twice and change nothing the second time.

    For a caller that looked at the claims before it could write them, and
    could not hold the write lock in between. Every condition is part of the
    statement that writes the flag, so a claim confirmed, bound, flagged or
    given another address after the caller looked is left as it stands, its
    challenge included.
    """
    return await _mark_undeliverable(session, email, node_ids, *_OPEN)


async def _mark_undeliverable(
    session: AsyncSession, email: str, node_ids: list[str], *conditions: ColumnElement[bool]
) -> dict[str, str | None]:
    """Flag `email` on each of `node_ids` whose claim still carries it and meets
    `conditions`, then drop those nodes' challenges, returning the dropped
    handle for each node flagged.

    The update picks the rows it changes as it writes them, under SQLite's
    write lock, which a transaction holds from its first write on: nothing can
    move a claim between the check and the flag, and the challenges dropped
    next are the ones those nodes held then. "fetch" rather than the default,
    which may match rows already loaded into the session against their loaded
    values: only the rows the database changed are changed in the session too.
    """
    if not node_ids:
        return {}
    marked = list(
        (
            await session.execute(
                update(NodeClaim)
                .where(NodeClaim.node_id.in_(node_ids), NodeClaim.email == email, *conditions)
                .values(undeliverable=True, updated_at=datetime.now(UTC))
                .returning(NodeClaim.node_id)
                .execution_options(synchronize_session="fetch")
            )
        ).scalars()
    )
    if not marked:
        return {}
    dropped = await session.execute(
        delete(NodeClaimChallenge)
        .where(NodeClaimChallenge.node_id.in_(marked))
        .returning(NodeClaimChallenge.node_id, NodeClaimChallenge.handle)
        .execution_options(synchronize_session="fetch")
    )
    handles = {node_id: handle for node_id, handle in dropped.tuples()}
    return {node_id: handles.get(node_id) for node_id in marked}


async def decline(session: AsyncSession, node_id: str, email: str, handle: str) -> bool:
    """Refuse the claim the link `handle` was mailed for, as the person at
    `email`: drop the node's challenge if it is still that link's, and record
    the decline on the claim if `email` is still its address and nobody owns
    the node. True when the challenge was dropped, and only then is anything
    recorded.

    A challenge that is another link's by now was put there since the link was
    spent, by a resend or a corrected address, and its mail is on its way. The
    refusal was of the link before it, so neither the challenge nor the claim
    is touched.

    Each condition is part of the statement that writes, as in
    `_mark_undeliverable`: the first takes SQLite's write lock, so nothing can
    replace the challenge between the check and the drop, nor move the claim
    between the drop and the flag. No handle comes back, as from
    `mark_verified`: the link was spent by the caller.
    """
    dropped = await session.execute(
        delete(NodeClaimChallenge)
        .where(NodeClaimChallenge.node_id == node_id, NodeClaimChallenge.handle == handle)
        .returning(NodeClaimChallenge.node_id)
        .execution_options(synchronize_session="fetch")
    )
    if dropped.first() is None:
        return False
    await session.execute(
        update(NodeClaim)
        .where(NodeClaim.node_id == node_id, NodeClaim.email == email, NodeClaim.user_id.is_(None))
        .values(declined=True, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session="fetch")
    )
    return True


async def clear_declined(session: AsyncSession, node_id: str) -> None:
    """Forget a decline recorded on the node's claim, for the node asking for
    its mail again: whoever is at the node is the authority on its address,
    and asking again is a fresh nomination of it."""
    await session.execute(
        update(NodeClaim)
        .where(NodeClaim.node_id == node_id, NodeClaim.declined.is_(True))
        .values(declined=False, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session="fetch")
    )


async def clear_claim(session: AsyncSession, node_id: str) -> None:
    """Return the node to the unclaimed state: no owner, no address, no challenge.

    The address goes with the binding. It is a spent token recording what the
    node was claimed with, and a second-hand node that kept it would show its
    next owner the last one's address.
    """
    await session.execute(delete(NodeClaimChallenge).where(NodeClaimChallenge.node_id == node_id))
    await session.execute(delete(NodeClaim).where(NodeClaim.node_id == node_id))


async def set_owner(session: AsyncSession, node_id: str, user_id: str | None) -> None:
    """Assign or clear the node's owner, on somebody's behalf.

    A change of owner clears what a release clears: the address, its
    confirmation, its bounce, its decline and any pending challenge. Leaving
    the address would show the new owner the last one's, or a stranger's that
    was declined, and leaving the challenge would let a link already in a
    mailbox rebind a node an administrator has just freed. Assigning the owner
    a node already has changes nothing, so it keeps the address that claimed
    it, and when they took it.

    The node must be registered: an id that is not fails on the foreign key.
    """
    row = await session.get(NodeClaim, node_id)
    if row is not None and user_id is not None and row.user_id == user_id:
        return
    if user_id is None:
        await clear_claim(session, node_id)
        return
    # The link the challenge named needs no invalidating here: the bind re-reads
    # the address it was sent to, and finds it gone.
    await drop_challenge(session, node_id)
    now = datetime.now(UTC)
    if row is None:
        session.add(
            NodeClaim(
                node_id=node_id,
                user_id=user_id,
                verified=False,
                undeliverable=False,
                declined=False,
                updated_at=now,
                owned_since=now,
                owned_since_user_id=user_id,
            )
        )
    else:
        row.user_id = user_id
        row.email = None
        row.verified = False
        row.undeliverable = False
        row.declined = False
        row.updated_at = now
        row.owned_since = now
        row.owned_since_user_id = user_id
    await session.flush()


async def read_challenge(session: AsyncSession, node_id: str) -> NodeClaimChallenge | None:
    """The node's outstanding challenge, or None."""
    return await session.get(NodeClaimChallenge, node_id)


async def node_challenged_with(session: AsyncSession, handle: str) -> str | None:
    """The node whose outstanding challenge is `handle`, or None.

    Read off the challenge rather than the link: the row stays after its link
    expires, until it is replaced, dropped or cleared, so it still says which
    claim a dead link was mailed for and whether it is that claim's newest.
    Unindexed, as only a person asking for the mail again reads by it, and the
    table holds one row per node with a claim waiting.
    """
    return (
        await session.execute(select(NodeClaimChallenge.node_id).where(NodeClaimChallenge.handle == handle))
    ).scalar_one_or_none()


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
    claim = await read_claim(session, node_id)
    return claim.user_id if claim is not None else None


async def claim_status(session: AsyncSession, node_id: str, now: float) -> ClaimStatus:
    """Derive what the node should be told, from the two rows that say it.

    The order is the order the facts outrank each other. An owner settles it
    whatever else is on file, since the challenge that produced the binding is
    spent and any later one cannot outrank it. A bounced address comes next,
    because the useful thing to say about an address nobody can receive at is
    that, not that the node is unclaimed like any other.

    An expired challenge reads as unclaimed with the address still on file,
    which is what lets a fresh one be asked for without retyping it.
    """
    claim = await read_claim(session, node_id)
    if claim is None:
        # No owner and no address has ever been offered, so there is no
        # challenge to find: a challenge is only ever written alongside an
        # address, in the same transaction. Worth the branch because this is
        # the common case and this runs on every heartbeat from every node.
        return ClaimStatus("unclaimed", None, False)
    if claim.user_id is not None:
        return ClaimStatus("owned", claim.email, False)
    if claim.undeliverable:
        return ClaimStatus("unclaimed", claim.email, True)

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

    Only a verified address counts. A nomination racing the click that binds
    the node can leave an address nobody confirmed beside the owner, and that is
    not what the node was claimed with.

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
