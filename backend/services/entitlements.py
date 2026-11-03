"""Who may use the keyed data API: an account's entitlement, computed from its rows.

An account is entitled while any of its `entitlements` rows is active: started
(`starts_at` <= now), not lapsed (`lapsed_at` null) and not past its end
(`ends_at` null or later than now). A contributor row comes from the account's
nodes' activity, by evaluate_contributors below; a customer row from an
administrator's grant. Both kinds active, the customer row's tier applies.

Administrators have no account row: an administrator is a Cloudflare Access
identity or the anonymous-admin bypass (core/users.py). The caller tells one
apart and uses admin_access() for them, which is always entitled with no limits.

The contributor rule, from the design note: the account owns
(`node_claims.user_id`, as core/auth.py get_user_nodes reads it) at least one
node with activity rows on at least ceil(CONTRIBUTOR_MIN_ACTIVE_FRACTION x
CONTRIBUTOR_WINDOW_DAYS) of the last CONTRIBUTOR_WINDOW_DAYS complete UTC days.
Today is never counted, so a day just begun cannot lower the count. Days are
the node's, not summed across nodes, and how long the owner has held the node
does not matter: the note counts the node's days. Below the threshold, access
holds for CONTRIBUTOR_GRACE_DAYS (fractional days allowed) and then lapses.
These three are read at every call: 0.5, 30 and 7 by default, and a value that
is not usable is the default, with a warning.

Public:

- REASON_NOT_ENTITLED, REASON_LAPSED, REASON_GRANT_ENDED: why an account is not
  entitled. `not_entitled` when no row of the account has ended; otherwise the
  reason of the row that ended last: `grant_ended` for a customer row that
  ended by its date, `lapsed` for any other (a revoked grant, a contributor
  past its grace).
- TierLimits: a tier's limits. None is unlimited, which only administrators get.
- Access: whether a caller is entitled, as what kind ("contributor",
  "customer" or "admin"), under which limits, and if not, why.
- AccessStatus: Access plus the contributor rule's numbers and the ends of the
  grace and of the grant, for the account page and the admin view; to_dict()
  is the JSON body, times as ISO 8601 UTC with a `Z`.
- admin_access(): the Access of an administrator.
- effective_access(user_id, now=): the account's Access. Reads only.
- access_status(user_id, now=): the account's AccessStatus. Reads only.
- list_entitlements(user_id): every row of the account, history included,
  newest first.
- grant_customer(user_id, ends_at=, now=): grant customer access from now,
  until `ends_at` or with no end. ValueError for an `ends_at` not later than
  now, LookupError for an id that names no account.
- revoke_grant(entitlement_id, now=): end a customer grant now. None when no
  customer row has that id; a grant already ended is returned unchanged.
- evaluate_contributors(now=): apply the contributor rule to every account
  that owns a node or holds an open contributor row, and stamp every customer
  row past its end; returns the accounts whose rows changed.
  services/tasks/entitlement_refresh.py runs it every
  ENTITLEMENT_REFRESH_INTERVAL_S, refresh_interval_s() here.
- contributor_window(now): the rule's first counted day and today, as of `now`.
- add_access_listener(listener): call `listener` with the set of accounts
  after every committed change to their rows; returns the remover.

`user_id` is an account id in any spelling uuid.UUID reads. A row is never
deleted, nor any key: a key of an account that is not entitled stays and is
refused.

Answers are not cached here. A per-principal cache belongs to its caller, which
drops an account's entry when a listener names it. An entitlement also ends at
its `ends_at` with nothing written, so a cached answer must expire on its own
as well.
"""

import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from typing import Any

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.api_access import (
    KIND_CONTRIBUTOR,
    KIND_CUSTOMER,
    SOURCE_MANUAL,
    SOURCE_UPTIME,
    Entitlement,
    NodeActivityDay,
    Tier,
)
from core.nodes import NodeClaim
from core.users import User, async_session_maker, parse_account_id

logger = logging.getLogger(__name__)

REASON_NOT_ENTITLED = "not_entitled"
REASON_LAPSED = "lapsed"
REASON_GRANT_ENDED = "grant_ended"

# Far past any real window or grace, and short of what a date can hold either
# side of now: a value beyond it would fail every evaluation and status read.
_MAX_DAYS = 100_000


@dataclass(frozen=True)
class TierLimits:
    tier: str
    requests_per_minute: int | None
    max_concurrent_streams: int | None
    max_history_span_s: int | None


@dataclass(frozen=True)
class Access:
    entitled: bool
    kind: str | None
    limits: TierLimits | None
    reason: str | None


@dataclass(frozen=True)
class AccessStatus:
    access: Access
    active_days: int
    window_days: int
    threshold_days: int
    # The open contributor row's end, set while its grace runs.
    grace_ends_at: datetime | None
    # The latest end of the active customer grants; None when one has no end.
    grant_ends_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        limits = self.access.limits
        return {
            "entitled": self.access.entitled,
            "kind": self.access.kind,
            "reason": self.access.reason,
            "limits": None if limits is None else asdict(limits),
            "contributor": {
                "active_days": self.active_days,
                "window_days": self.window_days,
                "threshold_days": self.threshold_days,
                "grace_ends_at": _iso(self.grace_ends_at),
            },
            "grant_ends_at": _iso(self.grant_ends_at),
        }


_ADMIN_ACCESS = Access(entitled=True, kind="admin", limits=TierLimits("admin", None, None, None), reason=None)


def admin_access() -> Access:
    return _ADMIN_ACCESS


# ── Configuration ─────────────────────────────────────────────────────────────


def _setting[T](name: str, default: str, parse: Callable[[str], T], usable: Callable[[T], bool]) -> T:
    """The environment variable `name` parsed, or `default` parsed when it is
    unset or blank, and with a warning when it does not parse or is not usable.

    Read at every call rather than at import: main.py calls load_dotenv() after
    the imports, so an import-time read would miss backend/.env.
    """
    raw = os.getenv(name)
    if raw is not None and raw.strip():
        try:
            value = parse(raw.strip())
        except (ValueError, ZeroDivisionError):
            pass
        else:
            if usable(value):
                return value
        logger.warning("%s=%r is not a usable value, using %s", name, raw, default)
    return parse(default)


@dataclass(frozen=True)
class _Rule:
    window_days: int
    threshold_days: int
    grace: timedelta


def _contributor_rule() -> _Rule:
    # A Fraction parsed from the text, so 0.28 x 50 is 14 days, not the 15 that
    # ceil(0.28 * 50) gives in floating point.
    fraction = _setting("CONTRIBUTOR_MIN_ACTIVE_FRACTION", "0.5", Fraction, lambda v: 0 < v <= 1)
    window = _setting("CONTRIBUTOR_WINDOW_DAYS", "30", int, lambda v: 1 <= v <= _MAX_DAYS)
    grace_days = _setting("CONTRIBUTOR_GRACE_DAYS", "7", float, lambda v: 0 <= v <= _MAX_DAYS)
    return _Rule(window, math.ceil(fraction * window), timedelta(days=grace_days))


def refresh_interval_s() -> float:
    """Seconds between two runs of evaluate_contributors, read at every call."""
    return _setting("ENTITLEMENT_REFRESH_INTERVAL_S", "3600", float, lambda v: 0 < v < math.inf)


# ── Listeners ─────────────────────────────────────────────────────────────────

_listeners: list[Callable[[frozenset[str]], None]] = []


def add_access_listener(listener: Callable[[frozenset[str]], None]) -> Callable[[], None]:
    """Call `listener` with the accounts whose rows changed, after each commit
    that changed any. Returns what removes it again.

    Called on the event loop, so it must not block. One that raises is logged
    and does not stop the others or the write that called it.
    """
    _listeners.append(listener)

    def remove() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return remove


def _notify(user_ids: frozenset[str]) -> None:
    if not user_ids:
        return
    for listener in list(_listeners):
        try:
            listener(user_ids)
        except Exception:
            logger.exception("An access listener failed for %d accounts", len(user_ids))


# ── Reading ───────────────────────────────────────────────────────────────────


def _instant(when: datetime | None) -> datetime:
    """`when` in UTC, a naive one taken to be UTC as every stored one is; now when None."""
    if when is None:
        return datetime.now(UTC)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _iso(when: datetime | None) -> str | None:
    """UTC with a `Z`, as routes/node_schemas.py renders a server time."""
    return None if when is None else when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _account_key(user_id: str) -> str | None:
    """The account id as "user".id holds it, or None for a value that is not one."""
    parsed = parse_account_id(user_id)
    return None if parsed is None else str(parsed)


def _window(now: datetime, window_days: int) -> tuple[date, date]:
    """The first day the rule counts, and today, which it does not."""
    today = now.date()
    return today - timedelta(days=window_days), today


def contributor_window(now: datetime) -> tuple[date, date]:
    """The contributor rule's first counted day as of `now`, and today, which it does not count."""
    return _window(_instant(now), _contributor_rule().window_days)


async def _best_days(db: AsyncSession, first: date, today: date, account: str | None = None) -> dict[str, int]:
    """Each account's most active node's days from `first` to the day before
    `today`, for the accounts with any.

    Joined to "user": an owner bound by the registry mirror or an older row can
    name no account, and the job must not open a row for one.
    """
    per_node = (
        select(NodeClaim.user_id.label("user_id"), func.count().label("days"))
        .join(NodeActivityDay, NodeActivityDay.node_id == NodeClaim.node_id)
        .join(User, User.id == NodeClaim.user_id)
        .where(NodeActivityDay.day >= first, NodeActivityDay.day < today)
        .group_by(NodeClaim.user_id, NodeClaim.node_id)
    )
    if account is not None:
        per_node = per_node.where(NodeClaim.user_id == account)
    nodes = per_node.subquery()
    rows = await db.execute(select(nodes.c.user_id, func.max(nodes.c.days)).group_by(nodes.c.user_id))
    return {user_id: days for user_id, days in rows.tuples()}


async def _rows(db: AsyncSession, account: str) -> list[tuple[Entitlement, Tier]]:
    rows = await db.execute(
        select(Entitlement, Tier).join(Tier, Tier.id == Entitlement.tier_id).where(Entitlement.user_id == account)
    )
    return list(rows.tuples())


def _active(row: Entitlement, now: datetime) -> bool:
    return row.starts_at <= now and row.lapsed_at is None and (row.ends_at is None or row.ends_at > now)


def _ending(row: Entitlement, now: datetime) -> tuple[datetime, str] | None:
    """When the row ended and why, or None if it has not ended.

    It ended by its date when `ends_at` came at or before its lapse, or before
    now if it has not lapsed; otherwise when it lapsed, as a revoked grant does.
    """
    if row.ends_at is not None and row.ends_at <= (row.lapsed_at or now):
        return row.ends_at, REASON_GRANT_ENDED if row.kind == KIND_CUSTOMER else REASON_LAPSED
    if row.lapsed_at is not None:
        return row.lapsed_at, REASON_LAPSED
    return None


def _access(rows: Sequence[tuple[Entitlement, Tier]], now: datetime) -> Access:
    active = [(row, tier) for row, tier in rows if _active(row, now)]
    if active:
        # A customer row before a contributor row, then the latest grant.
        row, tier = max(active, key=lambda pair: (pair[0].kind == KIND_CUSTOMER, pair[0].starts_at))
        limits = TierLimits(tier.name, tier.requests_per_minute, tier.max_concurrent_streams, tier.max_history_span_s)
        return Access(entitled=True, kind=row.kind, limits=limits, reason=None)
    endings = [ending for row, _ in rows if (ending := _ending(row, now)) is not None]
    reason = max(endings)[1] if endings else REASON_NOT_ENTITLED
    return Access(entitled=False, kind=None, limits=None, reason=reason)


async def effective_access(user_id: str, *, now: datetime | None = None) -> Access:
    now = _instant(now)
    account = _account_key(user_id)
    if account is None:
        return _access([], now)
    async with async_session_maker() as db:
        return _access(await _rows(db, account), now)


async def access_status(user_id: str, *, now: datetime | None = None) -> AccessStatus:
    now = _instant(now)
    rule = _contributor_rule()
    account = _account_key(user_id)
    rows: list[tuple[Entitlement, Tier]] = []
    active_days = 0
    if account is not None:
        async with async_session_maker() as db:
            rows = await _rows(db, account)
            best = await _best_days(db, *_window(now, rule.window_days), account=account)
            active_days = best.get(account, 0)
    grace_ends_at = next(
        (row.ends_at for row, _ in rows if row.kind == KIND_CONTRIBUTOR and row.lapsed_at is None), None
    )
    grant_ends = [row.ends_at for row, _ in rows if row.kind == KIND_CUSTOMER and _active(row, now)]
    dated = [end for end in grant_ends if end is not None]
    return AccessStatus(
        access=_access(rows, now),
        active_days=active_days,
        window_days=rule.window_days,
        threshold_days=rule.threshold_days,
        grace_ends_at=grace_ends_at,
        grant_ends_at=max(dated) if dated and len(dated) == len(grant_ends) else None,
    )


async def list_entitlements(user_id: str) -> list[Entitlement]:
    """The account's rows, the latest started first and, of two started at
    once, the one written later."""
    account = _account_key(user_id)
    if account is None:
        return []
    async with async_session_maker() as db:
        rows = await db.scalars(
            select(Entitlement)
            .where(Entitlement.user_id == account)
            .order_by(Entitlement.starts_at.desc(), literal_column("entitlements.rowid").desc())
        )
        return list(rows)


# ── Writing ───────────────────────────────────────────────────────────────────


async def _tier_id(db: AsyncSession, kind: str) -> int:
    # A kind's tier is the one named after it (core/api_access.py).
    return (await db.execute(select(Tier.id).where(Tier.name == kind))).scalar_one()


async def grant_customer(user_id: str, *, ends_at: datetime | None = None, now: datetime | None = None) -> Entitlement:
    now = _instant(now)
    ends_at = None if ends_at is None else _instant(ends_at)
    if ends_at is not None and ends_at <= now:
        raise ValueError("A grant must end after it starts")
    account = _account_key(user_id)
    async with async_session_maker() as db:
        async with db.begin():
            if account is None or await db.get(User, account) is None:
                raise LookupError(f"No account {user_id!r}")
            row = Entitlement(
                user_id=account,
                kind=KIND_CUSTOMER,
                tier_id=await _tier_id(db, KIND_CUSTOMER),
                source=SOURCE_MANUAL,
                starts_at=now,
                ends_at=ends_at,
            )
            db.add(row)
    _notify(frozenset({account}))
    return row


async def revoke_grant(entitlement_id: str, *, now: datetime | None = None) -> Entitlement | None:
    now = _instant(now)
    grant = (Entitlement.id == entitlement_id, Entitlement.kind == KIND_CUSTOMER)
    async with async_session_maker() as db:
        async with db.begin():
            # Conditional on lapsed_at, so of two revokes at once one stamps and
            # notifies, and the other finds the grant already ended.
            stamped = (
                await db.execute(
                    update(Entitlement)
                    .where(*grant, Entitlement.lapsed_at.is_(None))
                    .values(lapsed_at=now)
                    .returning(Entitlement.user_id)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            row = await db.scalar(select(Entitlement).where(*grant))
    if stamped is not None:
        _notify(frozenset({stamped}))
    return row


async def evaluate_contributors(now: datetime | None = None) -> frozenset[str]:
    """Apply the contributor rule as of `now`, in one transaction, and stamp
    every customer row past its end.

    For an account that owns a node or holds an open contributor row:
    qualifying with no open row opens one; qualifying in grace ends the grace
    on the same row. Not qualifying, an open row with no end gets one, now plus
    the grace, set once. An open row past its end lapses at that end, qualifying
    or not, and a qualifying account then opens a new row, so history stays.
    """
    now = _instant(now)
    rule = _contributor_rule()
    changed: set[str] = set()
    opening: list[str] = []
    async with async_session_maker() as db:
        async with db.begin():
            # Written first: the UPDATE takes SQLite's write lock, even matching
            # nothing, so no other writer changes what is read below before the
            # commit.
            ended_grants = await db.execute(
                update(Entitlement)
                .where(
                    Entitlement.kind == KIND_CUSTOMER,
                    Entitlement.lapsed_at.is_(None),
                    Entitlement.ends_at.is_not(None),
                    Entitlement.ends_at <= now,
                )
                .values(lapsed_at=Entitlement.ends_at)
                .returning(Entitlement.user_id)
                .execution_options(synchronize_session=False)
            )
            changed.update(ended_grants.scalars())
            best = await _best_days(db, *_window(now, rule.window_days))
            open_rows = {
                row.user_id: row
                for row in await db.scalars(
                    select(Entitlement).where(Entitlement.kind == KIND_CONTRIBUTOR, Entitlement.lapsed_at.is_(None))
                )
            }
            for account in best.keys() | open_rows.keys():
                qualifies = best.get(account, 0) >= rule.threshold_days
                row = open_rows.get(account)
                if row is not None and row.ends_at is not None and row.ends_at <= now:
                    row.lapsed_at = row.ends_at
                    changed.add(account)
                    row = None
                if qualifies and row is None:
                    opening.append(account)
                elif qualifies and row is not None and row.ends_at is not None:
                    row.ends_at = None
                    changed.add(account)
                elif not qualifies and row is not None and row.ends_at is None:
                    row.ends_at = now + rule.grace
                    changed.add(account)
            if opening:
                # The lapses first: an account holds one open contributor row
                # at most (ux_entitlements_open_contributor).
                await db.flush()
                tier_id = await _tier_id(db, KIND_CONTRIBUTOR)
                db.add_all(
                    Entitlement(
                        user_id=account, kind=KIND_CONTRIBUTOR, tier_id=tier_id, source=SOURCE_UPTIME, starts_at=now
                    )
                    for account in opening
                )
                changed.update(opening)
    result = frozenset(changed)
    _notify(result)
    return result
