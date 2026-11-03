"""Who may use the keyed data API, against the real database.

Ada owns a node whose activity days are seeded as the frame loop writes them.
The contributor job and every read run with `now` given, at noon UTC on
2 November 2026, so the rule's window is 3 October to 1 November.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from core.api_access import ApiKey, NodeActivityDay
from core.users import User, async_session_maker
from services import entitlements
from services.entitlements import Access, TierLimits
from tests.ownership_helpers import own_registered_node

ADA = "ada@example.com"
NODE = "ret0a1b2c3d"
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
YESTERDAY = date(2026, 11, 1)
HOUR, DAY = timedelta(hours=1), timedelta(days=1)
CONTRIBUTOR = {
    "tier": "contributor",
    "requests_per_minute": 60,
    "max_concurrent_streams": 2,
    "max_history_span_s": 86400,
}


@pytest.fixture(autouse=True)
def _rule_defaults_and_no_activity(monkeypatch):
    """tests/conftest.py's _clean_db leaves node_activity_days as it finds it."""
    for name in ("CONTRIBUTOR_MIN_ACTIVE_FRACTION", "CONTRIBUTOR_WINDOW_DAYS", "CONTRIBUTOR_GRACE_DAYS"):
        monkeypatch.delenv(name, raising=False)

    async def _clear():
        async with async_session_maker() as db, db.begin():
            await db.execute(delete(NodeActivityDay))

    asyncio.run(_clear())


@pytest.fixture
def heard():
    calls: list[frozenset[str]] = []
    remove = entitlements.add_access_listener(calls.append)
    yield calls
    remove()


def _days(last: date, count: int) -> list[date]:
    return [last - k * DAY for k in range(count)]


async def _active_on(*days: date) -> None:
    async with async_session_maker() as db, db.begin():
        db.add_all(NodeActivityDay(node_id=NODE, day=day) for day in days)


async def _quiet_on(*days: date) -> None:
    async with async_session_maker() as db, db.begin():
        await db.execute(delete(NodeActivityDay).where(NodeActivityDay.day.in_(days)))


async def _owner() -> str:
    """Ada, who took the node yesterday, with a key."""
    async with async_session_maker() as db, db.begin():
        db.add(user := User(email=ADA))
    ada = str(user.id)
    await own_registered_node(NODE, ada, since=NOW - DAY)
    async with async_session_maker() as db, db.begin():
        db.add(ApiKey(user_id=ada, name="script", prefix="adakey01", secret_hash="0" * 64, created_at=NOW))
    return ada


async def _contributor() -> str:
    """Ada with 15 of the 30 days, 18 October to 1 November, entitled at NOW."""
    ada = await _owner()
    await _active_on(*_days(YESTERDAY, 15))
    assert await entitlements.evaluate_contributors(NOW) == {ada}
    return ada


async def _keys() -> list[tuple[str, datetime | None]]:
    async with async_session_maker() as db:
        return [(key.prefix, key.revoked_at) for key in await db.scalars(select(ApiKey))]


async def _history(ada: str) -> list[tuple[str, datetime, datetime | None, datetime | None]]:
    return [(e.id, e.starts_at, e.ends_at, e.lapsed_at) for e in await entitlements.list_entitlements(ada)]


def _status(entitled, kind, reason, limits, active_days, grace_ends_at=None, grant_ends_at=None) -> dict:
    contributor = {"active_days": active_days, "window_days": 30, "threshold_days": 15, "grace_ends_at": grace_ends_at}
    body = {"entitled": entitled, "kind": kind, "reason": reason, "limits": limits, "contributor": contributor}
    return body | {"grant_ends_at": grant_ends_at}


async def test_an_owner_gains_contributor_access_at_15_of_the_last_30_complete_days(heard):
    ada = await _owner()
    # 14 days in the window; today and the day before the window do not count.
    await _active_on(*_days(YESTERDAY, 14), date(2026, 11, 2), date(2026, 10, 2))

    assert await entitlements.evaluate_contributors(NOW) == frozenset()
    assert (await entitlements.access_status(ada, now=NOW)).to_dict() == _status(False, None, "not_entitled", None, 14)

    await _active_on(date(2026, 10, 3))

    assert await entitlements.evaluate_contributors(NOW) == {ada}
    assert heard == [{ada}]
    assert (await entitlements.access_status(ada, now=NOW)).to_dict() == _status(
        True, "contributor", None, CONTRIBUTOR, 15
    )


async def test_a_contributor_below_the_threshold_keeps_access_through_the_grace_then_lapses(monkeypatch):
    monkeypatch.setenv("CONTRIBUTOR_GRACE_DAYS", "0.5")
    ada = await _contributor()
    await _quiet_on(date(2026, 10, 18))
    grace_end = datetime(2026, 11, 3, tzinfo=UTC)

    assert await entitlements.evaluate_contributors(NOW) == {ada}
    assert await entitlements.evaluate_contributors(NOW + 6 * HOUR) == frozenset()
    assert (await entitlements.access_status(ada, now=NOW + 6 * HOUR)).to_dict() == _status(
        True, "contributor", None, CONTRIBUTOR, 14, grace_ends_at="2026-11-03T00:00:00Z"
    )

    assert await entitlements.evaluate_contributors(grace_end) == {ada}
    assert [row[2:] for row in await _history(ada)] == [(grace_end, grace_end)]
    assert (await entitlements.access_status(ada, now=grace_end)).to_dict() == _status(False, None, "lapsed", None, 14)
    assert await _keys() == [("adakey01", None)]


async def test_a_contributor_regains_in_grace_on_its_row_and_after_the_grace_on_a_new_one():
    ada = await _contributor()
    ((first, *_),) = await _history(ada)
    await _quiet_on(date(2026, 10, 18))
    await entitlements.evaluate_contributors(NOW + HOUR)
    await _active_on(date(2026, 10, 18))

    assert await entitlements.evaluate_contributors(NOW + 2 * HOUR) == {ada}
    assert await _history(ada) == [(first, NOW, None, None)]

    # Below again, then back above only after the seven days' grace, with no run between.
    await _quiet_on(date(2026, 10, 18))
    await entitlements.evaluate_contributors(NOW + 3 * HOUR)
    await _active_on(*_days(date(2026, 11, 9), 8))
    later, grace_end = datetime(2026, 11, 10, 12, tzinfo=UTC), datetime(2026, 11, 9, 15, tzinfo=UTC)

    assert await entitlements.evaluate_contributors(later) == {ada}
    history = await _history(ada)
    assert history == [(history[0][0], later, None, None), (first, NOW, grace_end, grace_end)]
    assert history[0][0] != first


async def test_a_customer_grant_outranks_contributor_access_and_ends_at_its_date(monkeypatch):
    monkeypatch.setenv("CONTRIBUTOR_GRACE_DAYS", "0")
    ada = await _contributor()
    grant_end = NOW + 10 * DAY
    await entitlements.grant_customer(ada, ends_at=grant_end, now=NOW - HOUR)
    customer = {
        "tier": "customer",
        "requests_per_minute": 600,
        "max_concurrent_streams": 10,
        "max_history_span_s": 604800,
    }

    assert (await entitlements.access_status(ada, now=NOW)).to_dict() == _status(
        True, "customer", None, customer, 15, grant_ends_at="2026-11-12T12:00:00Z"
    )

    await _quiet_on(*_days(YESTERDAY, 15))
    await entitlements.evaluate_contributors(NOW + HOUR)
    after = NOW + 11 * DAY

    assert await entitlements.effective_access(ada, now=after) == Access(False, None, None, "grant_ended")
    assert await entitlements.evaluate_contributors(after) == {ada}
    assert [row[3] for row in await _history(ada)] == [NOW + HOUR, grant_end]
    assert (await entitlements.effective_access(ada, now=after)).reason == "grant_ended"


async def test_a_revoked_grant_ends_access_at_once_and_keeps_the_keys(heard):
    ada = await _owner()
    with pytest.raises(ValueError):
        await entitlements.grant_customer(ada, ends_at=NOW, now=NOW)
    grant = await entitlements.grant_customer(ada, now=NOW)
    assert (await entitlements.effective_access(ada, now=NOW)).kind == "customer"

    revoked = await entitlements.revoke_grant(grant.id, now=NOW + HOUR)

    assert (revoked.id, revoked.lapsed_at) == (grant.id, NOW + HOUR)
    assert await entitlements.effective_access(ada, now=NOW + 2 * HOUR) == Access(False, None, None, "lapsed")
    assert (await entitlements.revoke_grant(grant.id, now=NOW + 3 * HOUR)).lapsed_at == NOW + HOUR
    assert await entitlements.revoke_grant("no-such-grant") is None
    assert heard == [{ada}, {ada}]
    assert await _keys() == [("adakey01", None)]


def test_an_administrator_is_always_entitled_without_limits():
    assert entitlements.admin_access() == Access(True, "admin", TierLimits("admin", None, None, None), None)
