"""Domain-specific auth helpers: invites, node ownership, claim codes.

All data is stored in the shared SQLite database (users.db) via SQLAlchemy
async sessions. On first startup, migrate_json_to_db() imports any existing
JSON files and renames them to *.json.migrated so they are not re-imported.

JWT, user storage, and session management are handled by fastapi-users
(see core/users.py). This module contains only the business logic that
has no equivalent in a general-purpose auth library.
"""

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select, update

from core.nodes import Node
from core.users import (
    ClaimCode,
    Invite,
    MagicLink,
    MagicLinkRefused,
    NodeOwner,
    User,
    async_session_maker,
    get_or_create_magic_link_user,
)
from services import claim_links
from services.node_claim_store import clear_claim, drop_challenge, mark_verified, read_claim

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INVITES_FILE = _DATA_DIR / "invites.json"
NODE_OWNERS_FILE = _DATA_DIR / "node_owners.json"
CLAIM_CODES_FILE = _DATA_DIR / "claim_codes.json"

INVITE_EXPIRY_S = 86400 * 14  # 14 days
CLAIM_CODE_EXPIRY_S = 86400 * 30  # 30 days

_MAX_ACTIVE_CLAIM_CODES_PER_USER = 10

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


# ── One-time JSON → SQLite migration ─────────────────────────────────────────


async def migrate_json_to_db() -> None:
    """Import existing JSON stores into SQLite on first startup (idempotent)."""
    imported = []
    async with async_session_maker() as session:
        async with session.begin():
            for migrate in (_migrate_invites, _migrate_node_owners, _migrate_claim_codes):
                source = await migrate(session)
                if source is not None:
                    imported.append(source)
    # Keep every source available until all records are committed. A crash or
    # rename failure afterwards is safe to retry: each importer skips known keys.
    for source in imported:
        source.rename(source.with_suffix(".json.migrated"))
        logger.info("Migrated auth records from %s", source)


async def _migrate_invites(session) -> Path | None:
    if not INVITES_FILE.exists():
        return
    try:
        data = json.loads(INVITES_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", INVITES_FILE)
        return
    for token, inv in data.items():
        if await session.get(Invite, token):
            continue
        session.add(
            Invite(
                token=token,
                email=inv.get("email", "").lower(),
                role=inv.get("role", "user"),
                created_by=inv.get("created_by", ""),
                created_at=float(inv.get("created_at", 0)),
                expires_at=float(inv.get("expires_at", 0)),
                used_at=inv.get("used_at"),
            )
        )
    return INVITES_FILE


async def _migrate_node_owners(session) -> Path | None:
    if not NODE_OWNERS_FILE.exists():
        return
    try:
        data = json.loads(NODE_OWNERS_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", NODE_OWNERS_FILE)
        return
    for node_id, user_id in data.items():
        if await session.get(NodeOwner, node_id):
            continue
        session.add(NodeOwner(node_id=node_id, user_id=user_id))
    return NODE_OWNERS_FILE


async def _migrate_claim_codes(session) -> Path | None:
    if not CLAIM_CODES_FILE.exists():
        return
    try:
        data = json.loads(CLAIM_CODES_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s for migration", CLAIM_CODES_FILE)
        return
    for code, rec in data.items():
        if await session.get(ClaimCode, code):
            continue
        session.add(
            ClaimCode(
                code=code,
                user_id=rec.get("user_id", ""),
                created_at=float(rec.get("created_at", 0)),
                expires_at=float(rec.get("expires_at", 0)),
                used_at=rec.get("used_at"),
                used_by_node_id=rec.get("used_by_node_id"),
            )
        )
    return CLAIM_CODES_FILE


# ── Invites ───────────────────────────────────────────────────────────────────


async def create_invite(email: str, role: str, created_by: str) -> dict:
    if role not in ("user", "admin"):
        raise ValueError("invalid role")
    email = email.lower().strip()
    if not email or "@" not in email:
        raise ValueError("invalid email")
    now = time.time()
    token = secrets.token_urlsafe(16)
    invite = Invite(
        token=token,
        email=email,
        role=role,
        created_by=created_by,
        created_at=now,
        expires_at=now + INVITE_EXPIRY_S,
        used_at=None,
    )
    async with async_session_maker() as session:
        session.add(invite)
        await session.commit()
    return _invite_to_dict(invite)


async def list_invites() -> list[dict]:
    async with async_session_maker() as session:
        result = await session.execute(select(Invite))
        return [_invite_to_dict(i) for i in result.scalars().all()]


async def revoke_invite(token: str) -> bool:
    async with async_session_maker() as session:
        invite = await session.get(Invite, token)
        if not invite:
            return False
        await session.delete(invite)
        await session.commit()
    return True


async def consume_invite_for_email(email: str) -> str | None:
    """Consume the oldest valid invite for this email. Returns role or None."""
    email = email.lower().strip()
    now = time.time()
    async with async_session_maker() as session:
        result = await session.execute(
            select(Invite)
            .where(
                Invite.email == email,
                Invite.used_at.is_(None),
                Invite.expires_at > now,
            )
            .order_by(Invite.created_at)
            .limit(1)
        )
        invite = result.scalar_one_or_none()
        if invite is None:
            return None
        invite.used_at = now
        role = invite.role
        await session.commit()
    return role


def _invite_to_dict(invite: Invite) -> dict:
    return {
        "token": invite.token,
        "email": invite.email,
        "role": invite.role,
        "created_by": invite.created_by,
        "created_at": invite.created_at,
        "expires_at": invite.expires_at,
        "used_at": invite.used_at,
    }


# ── Node ownership ────────────────────────────────────────────────────────────


async def get_node_owner(node_id: str) -> str | None:
    async with async_session_maker() as session:
        owner = await session.get(NodeOwner, node_id)
        return owner.user_id if owner else None


async def list_node_owners() -> dict[str, str]:
    async with async_session_maker() as session:
        result = await session.execute(select(NodeOwner))
        return {o.node_id: o.user_id for o in result.scalars().all()}


async def set_node_owner(node_id: str, user_id: str | None) -> None:
    async with async_session_maker() as session:
        if user_id is None:
            await session.execute(delete(NodeOwner).where(NodeOwner.node_id == node_id))
        else:
            owner = await session.get(NodeOwner, node_id)
            if owner:
                owner.user_id = user_id
            else:
                session.add(NodeOwner(node_id=node_id, user_id=user_id))
        await session.commit()


async def get_user_nodes(user_id: str) -> list[str]:
    async with async_session_maker() as session:
        result = await session.execute(select(NodeOwner.node_id).where(NodeOwner.user_id == user_id))
        return list(result.scalars().all())


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
    now = time.time()
    async with async_session_maker() as session:
        resolved = await claim_links.resolve(session, token, now)
        if resolved is None or resolved.intent != claim_links.INTENT_CLAIM:
            # The intent check is what stops a sign-in link claiming a node when
            # both live in one token store. It is cheap now and load-bearing
            # later, so it is not left for later.
            return ClaimOutcome.INVALID, None, None
        node_id, email = resolved.node_id, resolved.email

        existing = await session.get(NodeOwner, node_id)
        if existing is not None and existing.user_id != await _user_id_for(session, email):
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

    async with async_session_maker() as session:
        async with session.begin():
            # Re-read inside the writing transaction. The account lookup above
            # released the first one, so the check that nobody else owns this
            # node has to be the one that holds while the row is written.
            incumbent = await session.get(NodeOwner, node_id)
            if incumbent is not None:
                if incumbent.user_id != str(user.id):
                    return ClaimOutcome.TAKEN, None, None
                # Two clicks on one link, racing. The second finds the binding
                # the first made and it is the same person's, so this succeeded
                # rather than collided: telling them their own node belongs to
                # somebody else would be both wrong and alarming. The link is
                # still spent, like any other that binds.
                await mark_verified(session, node_id, email)
                node = await session.get(Node, node_id)
                return ClaimOutcome.BOUND, user, node.node_ref if node else None
            # The node must still be offering the address this link was sent
            # to. A nomination of another address, or a release, between the
            # link being read and this write has replaced it, and binding would
            # hand the node to an address its holder has already withdrawn.
            claim = await read_claim(session, node_id)
            if claim is None or claim.email != email:
                return ClaimOutcome.INVALID, None, None
            session.add(NodeOwner(node_id=node_id, user_id=str(user.id)))
            # Spends the challenge as well as confirming the address, so the
            # link cannot be walked into twice.
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
    async with async_session_maker() as session:
        resolved = await claim_links.resolve(session, token, time.time())
        if resolved is None or resolved.intent != claim_links.INTENT_CLAIM:
            return None
        node = await session.get(Node, resolved.node_id)
        return node.node_ref if node else None


async def decline_claim(token: str) -> bool:
    """Refuse a claim: drop the challenge and leave the node unowned.

    The address stays on file. It is what the node was offered, it is still what
    its setup UI should show, and clearing it would leave whoever is standing at
    the node with no idea why nothing happened.

    True when there was a challenge to decline.
    """
    async with async_session_maker() as session:
        async with session.begin():
            resolved = await claim_links.resolve(session, token, time.time())
            if resolved is None or resolved.intent != claim_links.INTENT_CLAIM:
                return False
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
            owner = await session.get(NodeOwner, node_id)
            if owner is None or owner.user_id != user_id:
                return False
            await session.delete(owner)
            await clear_claim(session, node_id)
    logger.info("Node %s was released by its owner", node_id)
    return True


# ── Claim codes ───────────────────────────────────────────────────────────────


async def create_claim_code(user_id: str) -> dict:
    """Create a one-time claim code for the user.

    Raises ValueError if the user already has _MAX_ACTIVE_CLAIM_CODES_PER_USER
    active (unused, non-expired) codes.
    """
    now = time.time()
    async with async_session_maker() as session:
        result = await session.execute(
            select(ClaimCode).where(
                ClaimCode.user_id == user_id,
                ClaimCode.used_at.is_(None),
                ClaimCode.expires_at >= now,
            )
        )
        if len(result.scalars().all()) >= _MAX_ACTIVE_CLAIM_CODES_PER_USER:
            raise ValueError(
                f"Maximum of {_MAX_ACTIVE_CLAIM_CODES_PER_USER} active claim codes "
                "allowed per user. Revoke an existing code first."
            )
        code = secrets.token_hex(6).upper()  # 12 hex chars = 48 bits of entropy
        record = ClaimCode(
            code=code,
            user_id=user_id,
            created_at=now,
            expires_at=now + CLAIM_CODE_EXPIRY_S,
            used_at=None,
            used_by_node_id=None,
        )
        session.add(record)
        await session.commit()
    return _claim_code_to_dict(record)


async def list_claim_codes(user_id: str | None = None) -> list[dict]:
    async with async_session_maker() as session:
        q = select(ClaimCode)
        if user_id is not None:
            q = q.where(ClaimCode.user_id == user_id)
        result = await session.execute(q)
        return [_claim_code_to_dict(c) for c in result.scalars().all()]


async def revoke_claim_code(code: str, user_id: str | None = None) -> bool:
    async with async_session_maker() as session:
        rec = await session.get(ClaimCode, code)
        if not rec:
            return False
        if user_id is not None and rec.user_id != user_id:
            return False
        if rec.used_at is not None:
            return False
        await session.delete(rec)
        await session.commit()
    return True


async def consume_claim_code(code: str, node_id: str) -> str | None:
    """Mark a claim code used and assign node ownership atomically.

    Returns the user_id that now owns the node, or None for an invalid/used code.
    Database failures propagate, with both changes rolled back.
    """
    if not code or not node_id:
        return None
    code = code.strip().upper()
    now = time.time()
    async with async_session_maker() as session:
        async with session.begin():
            # Claim the code in the database before reading ownership. A
            # read/check/write sequence lets concurrent requests both consume it.
            result = await session.execute(
                update(ClaimCode)
                .where(
                    ClaimCode.code == code,
                    ClaimCode.used_at.is_(None),
                    ClaimCode.expires_at >= now,
                )
                .values(used_at=now, used_by_node_id=node_id)
                .returning(ClaimCode.user_id)
            )
            user_id = result.scalar_one_or_none()
            if user_id is None:
                return None
            owner = await session.get(NodeOwner, node_id)
            if owner:
                owner.user_id = user_id
            else:
                session.add(NodeOwner(node_id=node_id, user_id=user_id))
        return user_id


# ── Magic links ───────────────────────────────────────────────────────────────


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def create_magic_link(email: str, *, intent: str = INTENT_SIGNIN, node_id: str | None = None) -> str | None:
    """Issue a link for an address under one intent, returning the token to mail.

    Returns None when the address already holds the maximum number of
    outstanding links **of that intent**. Callers must answer their own request
    identically either way: whether a link went out is exactly what an attacker
    wants told.

    `node_id` is what a claim link is about, and is meaningless for a sign-in.
    """
    email = normalise_email(email)
    now = time.time()
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
            # Claim it in the database before reading anything off it, the way
            # consume_claim_code does: a read/check/write sequence lets two
            # concurrent redemptions both succeed.
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


def _claim_code_to_dict(rec: ClaimCode) -> dict:
    return {
        "code": rec.code,
        "user_id": rec.user_id,
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
        "used_at": rec.used_at,
        "used_by_node_id": rec.used_by_node_id,
    }
