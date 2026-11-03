"""An account's keys to the data API: made, listed, revoked, and checked.

A key reads `rtk_<prefix>_<secret>`. The prefix is 8 lower-case hex characters
(secrets.token_hex(4)), stored, shown in lists, and what finds the key. The
secret is 43 characters of the URL-safe alphabet (secrets.token_urlsafe(32),
256 random bits) and is kept only as its SHA-256, the scheme user_sessions
uses for its tokens: a random secret that long needs no slow hash. The secret
may itself hold `_` and `-`, so a key is read by its shape, never split.

The full key exists once, in what create_key returns. It is never stored,
logged, or put in an exception's message, and neither is its hash.

A key is never deleted here. A revoked key keeps its row, and so does a key
whose owner is not entitled: core/data_access.py refuses it.

Public:

- KEY_NAME_MAX: the longest name a key may have.
- KeyRecord: what checking a key needs of its row, the hash left out of its repr.
- create_key(user_id, name, now=): a new key for the account; returns the row
  and the full key. ValueError for a name that is blank or longer than
  KEY_NAME_MAX, LookupError for an id that names no account. Whether the
  account may hold a key is the caller's check.
- list_keys(user_id): the account's keys, newest first, revoked ones included.
- revoke_key(key_id, user_id=, now=): stamp the key revoked. With `user_id`,
  only a key of that account: another account's key is None, as an unknown id
  is. A key already revoked is returned unchanged. Of two revokes at once one
  stamps, and only that one tells the key listeners.
- add_key_listener(listener): call `listener` with the KeyRecord of each key
  revoked, after the commit, so a cache can drop it at once. Returns the
  remover.
- key_to_dict(row): the key as the API shows it, never its hash.
- fetch_key(prefix): the KeyRecord of the key with that prefix, or None. Reads only.
- verify_key(presented, fetch=): the KeyRecord of the live key `presented`
  is, or None for one that is malformed, unknown, revoked, or whose secret
  does not match. `fetch` reads a record by prefix, fetch_key by default; the
  gate passes its cache.
- touch_key(key_id, now=): record a use of the key. last_used_at is written at
  most once per TOUCH_INTERVAL per key: the throttle is a map in this module,
  so a key in constant use costs one write a minute.
"""

import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import literal_column, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.api_access import ApiKey
from core.users import User, async_session_maker, parse_account_id

logger = logging.getLogger(__name__)

KEY_NAME_MAX = 100
TOUCH_INTERVAL = timedelta(minutes=1)

_KEY_SHAPE = re.compile(r"rtk_([0-9a-f]{8})_([A-Za-z0-9_-]{43})")
# Two keys drawing the same 8-hex prefix is about one in four billion a pair;
# a few draws make a refusal for that reason all but impossible.
_PREFIX_DRAWS = 5


@dataclass(frozen=True)
class KeyRecord:
    id: str
    user_id: str
    prefix: str
    secret_hash: str = field(repr=False)
    revoked: bool


def _record(row: ApiKey) -> KeyRecord:
    return KeyRecord(row.id, row.user_id, row.prefix, row.secret_hash, row.revoked_at is not None)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _instant(when: datetime | None) -> datetime:
    """`when` in UTC, a naive one taken to be UTC; now when None."""
    if when is None:
        return datetime.now(UTC)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _iso(when: datetime | None) -> str | None:
    """UTC with a `Z`, as services/entitlements.py renders a time."""
    return None if when is None else when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Listeners ─────────────────────────────────────────────────────────────────

_listeners: list[Callable[[KeyRecord], None]] = []


def add_key_listener(listener: Callable[[KeyRecord], None]) -> Callable[[], None]:
    """Call `listener` with each key revoked, after its commit. Returns what removes it.

    Called on the event loop, so it must not block. One that raises is logged
    and does not stop the others or the revoke.
    """
    _listeners.append(listener)

    def remove() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return remove


def _notify(record: KeyRecord) -> None:
    for listener in list(_listeners):
        try:
            listener(record)
        except Exception:
            logger.exception("A key listener failed for key %s", record.id)


# ── Writing ───────────────────────────────────────────────────────────────────


async def create_key(user_id: str, name: str, *, now: datetime | None = None) -> tuple[ApiKey, str]:
    now = _instant(now)
    name = name.strip()
    if not name or len(name) > KEY_NAME_MAX:
        raise ValueError(f"A key's name is 1 to {KEY_NAME_MAX} characters")
    account = parse_account_id(user_id)
    if account is None:
        raise LookupError(f"No account {user_id!r}")
    for _ in range(_PREFIX_DRAWS):
        prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
        row = ApiKey(user_id=str(account), name=name, prefix=prefix, secret_hash=_digest(secret), created_at=now)
        try:
            async with async_session_maker() as db:
                async with db.begin():
                    if await db.get(User, account) is None:
                        raise LookupError(f"No account {user_id!r}")
                    db.add(row)
        except IntegrityError:
            # The prefix is taken, or the account went since the read above,
            # which the next draw's read reports.
            continue
        except SQLAlchemyError as error:
            # Its message would carry the INSERT's parameters, the hash among
            # them; the driver's own message does not.
            cause = getattr(error, "orig", None) or type(error).__name__
            raise RuntimeError(f"Could not store a new key: {cause}") from None
        return row, f"rtk_{prefix}_{secret}"
    raise RuntimeError("Could not draw a free key prefix")


async def revoke_key(key_id: str, *, user_id: str | None = None, now: datetime | None = None) -> ApiKey | None:
    now = _instant(now)
    where = [ApiKey.id == key_id]
    if user_id is not None:
        account = parse_account_id(user_id)
        if account is None:
            return None
        where.append(ApiKey.user_id == str(account))
    async with async_session_maker() as db:
        async with db.begin():
            # Conditional on revoked_at, so of two revokes at once one stamps
            # and notifies, and the other finds the key already revoked.
            stamped = (
                await db.execute(
                    update(ApiKey)
                    .where(*where, ApiKey.revoked_at.is_(None))
                    .values(revoked_at=now)
                    .returning(ApiKey.id)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            row = await db.scalar(select(ApiKey).where(*where))
    if stamped is not None and row is not None:
        _notify(_record(row))
    return row


class _UseThrottle:
    """When each key's use was last written, on the monotonic clock, kept no
    longer than TOUCH_INTERVAL, so the map holds only the keys used lately."""

    def __init__(self) -> None:
        self._written: dict[str, float] = {}
        self._next_sweep = 0.0

    def due(self, key_id: str) -> bool:
        """Whether a use of the key is to be written now; if so, it counts as written."""
        now, interval = time.monotonic(), TOUCH_INTERVAL.total_seconds()
        if now >= self._next_sweep:
            self._next_sweep = now + interval
            for stale in [key for key, at in self._written.items() if now - at >= interval]:
                del self._written[stale]
        last = self._written.get(key_id)
        if last is not None and now - last < interval:
            return False
        self._written[key_id] = now
        return True


_uses = _UseThrottle()


async def touch_key(key_id: str, *, now: datetime | None = None) -> None:
    if not _uses.due(key_id):
        return
    try:
        async with async_session_maker() as db:
            async with db.begin():
                await db.execute(
                    update(ApiKey)
                    .where(ApiKey.id == key_id)
                    .values(last_used_at=_instant(now))
                    .execution_options(synchronize_session=False)
                )
    except Exception:
        # A use not recorded costs a stale "last used"; the call itself stands.
        logger.warning("Could not record a use of key %s", key_id, exc_info=True)


# ── Reading ───────────────────────────────────────────────────────────────────


async def list_keys(user_id: str) -> list[ApiKey]:
    """The account's keys, the latest made first and, of two made at once, the one written later."""
    account = parse_account_id(user_id)
    if account is None:
        return []
    async with async_session_maker() as db:
        rows = await db.scalars(
            select(ApiKey)
            .where(ApiKey.user_id == str(account))
            .order_by(ApiKey.created_at.desc(), literal_column("api_keys.rowid").desc())
        )
        return list(rows)


def key_to_dict(row: ApiKey) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": _iso(row.created_at),
        "last_used_at": _iso(row.last_used_at),
        "revoked_at": _iso(row.revoked_at),
    }


async def fetch_key(prefix: str) -> KeyRecord | None:
    async with async_session_maker() as db:
        row = await db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    return None if row is None else _record(row)


async def verify_key(
    presented: str, *, fetch: Callable[[str], Awaitable[KeyRecord | None]] = fetch_key
) -> KeyRecord | None:
    shape = _KEY_SHAPE.fullmatch(presented)
    if shape is None:
        return None
    prefix, secret = shape.groups()
    record = await fetch(prefix)
    if record is None or record.revoked:
        return None
    # Constant time on the hash. The prefix is no secret: lists show it.
    if not hmac.compare_digest(_digest(secret), record.secret_hash):
        return None
    return record
