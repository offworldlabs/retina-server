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
from pathlib import Path

from sqlalchemy import delete, select, update

from core.users import ClaimCode, Invite, MagicLink, NodeOwner, async_session_maker

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

# Outstanding links one address may hold. Somebody clicking "send it again"
# while the first mail is slow is the normal case, so the cap is well above
# what that produces; it exists to stop a single address filling the table.
_MAX_OUTSTANDING_MAGIC_LINKS = 5


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


async def create_magic_link(email: str) -> str | None:
    """Issue a sign-in link for an address, returning the token to mail.

    Returns None when the address already holds the maximum number of
    outstanding links. Callers must answer their own request identically either
    way: whether a link went out is exactly what an attacker wants told.
    """
    email = normalise_email(email)
    now = time.time()
    token = secrets.token_urlsafe(32)

    async with async_session_maker() as session:
        async with session.begin():
            # Expired rows for this address go now rather than on a timer.
            # Nothing else sweeps this table, and the alternative is a row per
            # unredeemed request kept for ever.
            await session.execute(delete(MagicLink).where(MagicLink.email == email, MagicLink.expires_at < now))
            outstanding = await session.execute(
                select(MagicLink).where(
                    MagicLink.email == email,
                    MagicLink.used_at.is_(None),
                    MagicLink.expires_at >= now,
                )
            )
            if len(outstanding.scalars().all()) >= _MAX_OUTSTANDING_MAGIC_LINKS:
                logger.warning("Magic-link cap reached for an address; not issuing another")
                return None
            session.add(
                MagicLink(
                    token_hash=_hash_token(token),
                    email=email,
                    created_at=now,
                    expires_at=now + MAGIC_LINK_EXPIRY_S,
                    used_at=None,
                )
            )
    return token


async def consume_magic_link(token: str | None) -> str | None:
    """Redeem a link once, returning the address it was issued to.

    Returns None for a token that is unknown, expired or already redeemed. The
    caller must not distinguish those: telling them apart says which guesses
    were once real.
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
                    MagicLink.used_at.is_(None),
                    MagicLink.expires_at >= now,
                )
                .values(used_at=now)
                .returning(MagicLink.email)
            )
            email = result.scalar_one_or_none()
            if email is None:
                return None
            # Whoever else can read that mailbox must not be able to sign in on
            # a link its owner asked for and abandoned.
            await session.execute(delete(MagicLink).where(MagicLink.email == email, MagicLink.used_at.is_(None)))
        return email


def _claim_code_to_dict(rec: ClaimCode) -> dict:
    return {
        "code": rec.code,
        "user_id": rec.user_id,
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
        "used_at": rec.used_at,
        "used_by_node_id": rec.used_by_node_id,
    }
