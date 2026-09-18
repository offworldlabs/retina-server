"""Domain-specific auth helpers: node ownership and emailed links.

All data is stored in the shared SQLite database (users.db) via SQLAlchemy
async sessions.

JWT, user storage, and session management are handled by fastapi-users
(see core/users.py). This module contains only the business logic that
has no equivalent in a general-purpose auth library.
"""

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import delete, select, update

from core.nodes import NodeClaim
from core.users import MagicLink, async_session_maker

logger = logging.getLogger(__name__)

# Short, because the link is a bearer credential sitting in a mailbox and the
# person asking for it is, by definition, in front of the page.
MAGIC_LINK_EXPIRY_S = 900  # 15 minutes

# Outstanding links one address may hold, per intent. Somebody clicking "send it
# again" while the first mail is slow is the normal case, so the cap is well
# above what that produces; it exists to stop a single address filling the table.
#
# Per intent rather than per address, because the two are not competing for the
# same thing. Somebody bringing up four nodes and signing in twice would
# otherwise exhaust one shared allowance, and create_magic_link answers None
# silently by design, so what they would see is nothing happening.
_MAX_OUTSTANDING_MAGIC_LINKS = 5

# The default intent. A link with no other purpose opens a session and does
# nothing else.
INTENT_SIGNIN = "signin"


# ── Node ownership ────────────────────────────────────────────────────────────
#
# The owner is `node_claims.user_id`, on the same row as the address the node
# was claimed with, so a binding and the claim that made it are written and
# cleared together.


async def get_node_owner(node_id: str) -> str | None:
    async with async_session_maker() as session:
        return (
            await session.execute(select(NodeClaim.user_id).where(NodeClaim.node_id == node_id))
        ).scalar_one_or_none()


async def list_node_owners() -> dict[str, str]:
    async with async_session_maker() as session:
        result = await session.execute(
            select(NodeClaim.node_id, NodeClaim.user_id).where(NodeClaim.user_id.is_not(None))
        )
        return dict(result.tuples().all())


async def set_node_owner(node_id: str, user_id: str | None) -> None:
    """Assign or clear the owner in a transaction of its own.

    services/node_claim_store.set_owner is the write, and says what a change of
    owner clears. The node must be registered, or this raises IntegrityError.
    """
    from services.node_claim_store import set_owner

    async with async_session_maker() as session:
        async with session.begin():
            await set_owner(session, node_id, user_id)


async def get_user_nodes(user_id: str) -> list[str]:
    async with async_session_maker() as session:
        result = await session.execute(select(NodeClaim.node_id).where(NodeClaim.user_id == user_id))
        return list(result.scalars().all())


# ── Magic links ───────────────────────────────────────────────────────────────


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def create_magic_link(
    email: str, *, intent: str = INTENT_SIGNIN, node_id: str | None = None, now: float | None = None
) -> str | None:
    """Issue a link for an address under one intent, returning the token to mail.

    Returns None when the address already holds the maximum number of
    outstanding links **of that intent**. Callers must answer their own request
    identically either way: whether a link went out is exactly what an attacker
    wants told.

    `node_id` is what a claim link is about, and is meaningless for a sign-in.
    `now` is for a caller that records the same expiry elsewhere, so the two
    come from one clock reading.
    """
    email = normalise_email(email)
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)

    async with async_session_maker() as session:
        async with session.begin():
            # Expired rows for this address go now rather than on a timer.
            # Nothing else sweeps this table, and the alternative is a row per
            # unredeemed request kept for ever. Every intent at once, since a
            # dead row is dead whatever it was for.
            await session.execute(delete(MagicLink).where(MagicLink.email == email, MagicLink.expires_at < now))
            outstanding = await session.execute(
                select(MagicLink).where(
                    MagicLink.email == email,
                    MagicLink.intent == intent,
                    MagicLink.used_at.is_(None),
                    MagicLink.expires_at >= now,
                )
            )
            if len(outstanding.scalars().all()) >= _MAX_OUTSTANDING_MAGIC_LINKS:
                logger.warning("Magic-link cap reached for an address under intent %s; not issuing another", intent)
                return None
            session.add(
                MagicLink(
                    token_hash=_hash_token(token),
                    email=email,
                    intent=intent,
                    node_id=node_id,
                    created_at=now,
                    expires_at=now + MAGIC_LINK_EXPIRY_S,
                    used_at=None,
                )
            )
    return token


@dataclass(frozen=True)
class RedeemedLink:
    """A link that has just been spent, and what it was for."""

    email: str
    intent: str
    node_id: str | None


async def redeem_magic_link(token: str | None, *, intent: str = INTENT_SIGNIN) -> RedeemedLink | None:
    """Redeem a link of this intent once, or return None.

    None covers unknown, expired, already redeemed, and issued under a different
    intent. The caller must not distinguish those: telling them apart says which
    guesses were once real, and the intent mismatch is the case that matters —
    a claim link forwarded to the sign-in route must read exactly like a token
    that never existed.

    The intent is part of the `WHERE`, not a check after the fact, so a link of
    the wrong kind is never marked used by the endpoint that refused it.
    """
    if not token or not token.strip():
        return None
    now = time.time()
    async with async_session_maker() as session:
        async with session.begin():
            # Claim it in the database before reading anything off it: a
            # read/check/write sequence lets two concurrent redemptions both
            # succeed.
            result = await session.execute(
                update(MagicLink)
                .where(
                    MagicLink.token_hash == _hash_token(token),
                    MagicLink.intent == intent,
                    MagicLink.used_at.is_(None),
                    MagicLink.expires_at >= now,
                )
                .values(used_at=now)
                .returning(MagicLink.email, MagicLink.node_id)
            )
            row = result.one_or_none()
            if row is None:
                return None
            email, node_id = row
            # Whoever else can read that mailbox must not be able to sign in on
            # a link its owner asked for and abandoned.
            #
            # Scoped to this intent and this node. Unscoped by intent, an
            # ordinary sign-in would wipe the claim challenge for a node its
            # owner is halfway through setting up; unscoped by node, claiming
            # one node would wipe the challenge for the next. Either way that
            # node would sit at `pending` until expiry with no link to click.
            same_node = MagicLink.node_id.is_(None) if node_id is None else MagicLink.node_id == node_id
            await session.execute(
                delete(MagicLink).where(
                    MagicLink.email == email,
                    MagicLink.intent == intent,
                    same_node,
                    MagicLink.used_at.is_(None),
                )
            )
        return RedeemedLink(email=email, intent=intent, node_id=node_id)


async def consume_magic_link(token: str | None) -> str | None:
    """Redeem a sign-in link once, returning the address it was issued to.

    The sign-in half of `redeem_magic_link`, kept because that is what the
    sign-in route wants and an address is the whole of what it needs.
    """
    redeemed = await redeem_magic_link(token, intent=INTENT_SIGNIN)
    return redeemed.email if redeemed is not None else None


async def peek_magic_link(token: str | None, *, intent: str) -> RedeemedLink | None:
    """Read a live link without spending it.

    For a landing page that has to name what it is about before anyone presses
    anything. It reveals only what the holder of the token could learn by
    redeeming it, and there is no page that should have to spend a credential to
    ask a question.
    """
    if not token or not token.strip():
        return None
    now = time.time()
    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(MagicLink.email, MagicLink.node_id).where(
                    MagicLink.token_hash == _hash_token(token),
                    MagicLink.intent == intent,
                    MagicLink.used_at.is_(None),
                    MagicLink.expires_at >= now,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return RedeemedLink(email=row[0], intent=intent, node_id=row[1])


async def invalidate_magic_link(handle: str) -> None:
    """Make an outstanding link unredeemable, by its stored handle.

    A handle that is unknown, spent or already gone is not an error: the caller
    is asking for a token to be dead, and a token that never existed is dead.

    Deleted rather than marked used, because "used" records a redemption that
    did not happen. Nothing reads this table for history.
    """
    async with async_session_maker() as session:
        async with session.begin():
            await session.execute(delete(MagicLink).where(MagicLink.token_hash == handle))
