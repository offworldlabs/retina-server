"""The limits are a security control, so every rate here is pinned by test.

Nothing sleeps: the limiters take their clock as an argument and the tests move
it by hand. The claim limiter counts in the database, so its tests run against
a real SQLite file, and two of them against that file reopened after a restart
and shared by two processes.
"""

import hashlib
import inspect
import logging
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from core.rate_counters import RateCounter
from core.users import _set_sqlite_pragmas
from services import node_rate_limits
from services.node_rate_limits import (
    CLAIM_LIMITS,
    ENDPOINT_LIMITS,
    OVERFLOW_LOG_INTERVAL_S,
    POLLED_PROBE_LIMITS,
    REGISTRATION_LIMITS,
    ClaimRateLimiter,
    PolledProbeRateLimiter,
    RegistrationRateLimiter,
    TokenRateLimiter,
)
from services.node_refusals import RATE_LIMITED_BODY, REFUSAL_BODY, RETRY_AFTER_BASE_S, RETRY_AFTER_JITTER_S


class FakeClock:
    """A clock the test drives."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter(clock: FakeClock, max_tracked: int = 64) -> TokenRateLimiter:
    return TokenRateLimiter(clock=clock, max_tracked=max_tracked)


def test_the_documented_rates_are_the_ones_configured():
    assert ENDPOINT_LIMITS == {"detection": (8, 1), "heartbeat": (30, 60), "config": (30, 60)}


def test_eight_detection_frames_a_second_are_admitted_and_the_ninth_is_not():
    clock = FakeClock()
    limiter = _limiter(clock)
    assert [limiter.admit("ret1a2b3c4d", "detection") for _ in range(8)] == [None] * 8
    refusal = limiter.admit("ret1a2b3c4d", "detection")
    assert refusal is not None
    assert refusal.status_code == 429
    assert refusal.body == {"error": "rate_limited"}
    assert refusal.retry_after_s >= 1


def test_the_detection_window_resets_after_a_second():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    assert limiter.admit("ret1a2b3c4d", "detection") is not None
    clock.advance(1.0)
    assert limiter.admit("ret1a2b3c4d", "detection") is None


def test_a_refused_request_is_not_counted():
    """Hammering a closed window must not push its reset further out."""
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    for _ in range(20):
        assert limiter.admit("ret1a2b3c4d", "detection") is not None
    clock.advance(1.0)
    assert [limiter.admit("ret1a2b3c4d", "detection") for _ in range(8)] == [None] * 8


@pytest.mark.parametrize("endpoint", ["heartbeat", "config"])
def test_thirty_a_minute_on_the_slow_endpoints(endpoint):
    clock = FakeClock()
    limiter = _limiter(clock)
    assert [limiter.admit("ret1a2b3c4d", endpoint) for _ in range(30)] == [None] * 30
    assert limiter.admit("ret1a2b3c4d", endpoint) is not None
    clock.advance(60.0)
    assert limiter.admit("ret1a2b3c4d", endpoint) is None


def test_the_endpoints_do_not_share_a_counter():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(30):
        limiter.admit("ret1a2b3c4d", "heartbeat")
    assert limiter.admit("ret1a2b3c4d", "heartbeat") is not None
    assert limiter.admit("ret1a2b3c4d", "config") is None
    assert limiter.admit("ret1a2b3c4d", "detection") is None


def test_two_nodes_do_not_share_a_counter():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    assert limiter.admit("ret1a2b3c4d", "detection") is not None
    assert limiter.admit("ret000000001", "detection") is None


def test_the_retry_after_counts_down_the_window_and_is_never_zero():
    # Windows are laid out against the clock rather than against first use, so
    # this one starts on a boundary to get the full sixty seconds to count down.
    clock = FakeClock(960.0)
    limiter = _limiter(clock)
    for _ in range(30):
        limiter.admit("ret1a2b3c4d", "heartbeat")
    early = limiter.admit("ret1a2b3c4d", "heartbeat")
    clock.advance(59.9)
    late = limiter.admit("ret1a2b3c4d", "heartbeat")
    assert early.retry_after_s == 60
    assert late.retry_after_s == 1


def test_an_endpoint_with_no_configured_limit_is_a_programming_error():
    limiter = _limiter(FakeClock())
    with pytest.raises(ValueError, match="towers"):
        limiter.admit("ret1a2b3c4d", "towers")


def test_a_crowded_map_reclaims_only_counters_whose_window_has_passed():
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    for n in range(5):
        limiter.admit(f"ret00000000{n}", "detection")
    assert limiter.tracked_counters == 5
    clock.advance(1.0)
    limiter.admit("ret000000009", "detection")
    assert limiter.tracked_counters == 1


def test_a_live_counter_survives_a_crowded_map():
    """Evicting a live counter is the same as clearing the victim's limit."""
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    for n in range(20):
        limiter.admit(f"ret00000000{n}", "detection")
    assert limiter.admit("ret1a2b3c4d", "detection") is not None


def test_a_map_above_the_expected_fleet_size_warns_rather_than_evicting(caplog):
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    with caplog.at_level(logging.WARNING, logger="services.node_rate_limits"):
        for n in range(10):
            limiter.admit(f"ret00000000{n}", "detection")
    assert len(caplog.records) == 1
    assert "nothing has been evicted" in caplog.records[0].message
    assert limiter.tracked_counters == 10


def test_the_overflow_warning_is_throttled_by_its_own_interval(caplog):
    """A crowded map kept crowded (fresh keys replacing expired ones) warns
    again once OVERFLOW_LOG_INTERVAL_S has passed, and not before."""
    clock = FakeClock(1000.0)
    limiter = _limiter(clock, max_tracked=2)
    with caplog.at_level(logging.WARNING, logger="services.node_rate_limits"):
        for n in range(4):
            limiter.admit(f"reta{n}", "heartbeat")
        assert len(caplog.records) == 1

        clock.advance(20.0)
        for n in range(4):
            limiter.admit(f"retb{n}", "heartbeat")
        assert len(caplog.records) == 1

        clock.advance(OVERFLOW_LOG_INTERVAL_S - 20.0)
        limiter.admit("retc0", "heartbeat")
        assert len(caplog.records) == 2


def test_the_token_limiter_keeps_growing_past_its_bound_rather_than_refusing():
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=2)
    for n in range(10):
        assert limiter.admit(f"ret00000000{n}", "detection") is None
    assert limiter.tracked_counters == 10


def test_a_refusal_does_not_hand_out_the_modules_own_body_dict():
    clock = FakeClock()
    limiter = _limiter(clock, max_tracked=64)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    first = limiter.admit("ret1a2b3c4d", "detection")
    second = limiter.admit("ret1a2b3c4d", "detection")
    first.body["detail"] = "leaked"
    assert second.body == {"error": "rate_limited"}
    assert first.body is not second.body
    assert RATE_LIMITED_BODY == {"error": "rate_limited"}


def test_a_registration_refusal_does_not_hand_out_the_modules_own_body_dict():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=64)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    first = limiter.admit("ret1a2b3c4d")
    second = limiter.admit("ret1a2b3c4d")
    first.body["detail"] = "leaked"
    assert second.body == {"error": "forbidden"}
    assert first.body is not second.body
    assert REFUSAL_BODY == {"error": "forbidden"}


def test_resetting_the_token_limiter_clears_an_exhausted_counter():
    clock = FakeClock()
    limiter = _limiter(clock)
    for _ in range(8):
        limiter.admit("ret1a2b3c4d", "detection")
    assert limiter.admit("ret1a2b3c4d", "detection") is not None
    limiter.reset()
    assert limiter.admit("ret1a2b3c4d", "detection") is None
    assert limiter.tracked_counters == 1


def _registration(clock: FakeClock, max_tracked: int = 64) -> RegistrationRateLimiter:
    return RegistrationRateLimiter(clock=clock, max_tracked=max_tracked)


def test_resetting_the_registration_limiter_clears_an_exhausted_counter():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    assert limiter.admit("ret1a2b3c4d") is not None
    limiter.reset()
    assert limiter.admit("ret1a2b3c4d") is None
    assert limiter.tracked_counters == 2


def test_the_documented_registration_rates_are_the_ones_configured():
    assert REGISTRATION_LIMITS == ((5, 3600), (20, 86400))


def test_five_registrations_an_hour_are_admitted_and_the_sixth_is_not():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    assert [limiter.admit("ret1a2b3c4d") for _ in range(5)] == [None] * 5
    refusal = limiter.admit("ret1a2b3c4d")
    assert refusal is not None
    assert refusal.status_code == 403
    assert refusal.body == {"error": "forbidden"}


def test_the_registration_refusal_carries_the_shared_jittered_retry_after():
    """A countdown of this node's window would time the refusal, and timing it
    would say what the shared 403 body deliberately does not."""
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    values = {limiter.admit("ret1a2b3c4d").retry_after_s for _ in range(100)}
    assert all(
        RETRY_AFTER_BASE_S - RETRY_AFTER_JITTER_S <= v < RETRY_AFTER_BASE_S + RETRY_AFTER_JITTER_S for v in values
    )
    assert len(values) > 1


def test_the_hourly_window_resets():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    assert limiter.admit("ret1a2b3c4d") is not None
    clock.advance(3600)
    assert limiter.admit("ret1a2b3c4d") is None


def test_the_daily_cap_holds_though_the_hourly_window_is_fresh():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            assert limiter.admit("ret1a2b3c4d") is None
        clock.advance(3600)
    refusal = limiter.admit("ret1a2b3c4d")
    assert refusal is not None
    assert refusal.status_code == 403


def test_the_daily_window_resets():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            limiter.admit("ret1a2b3c4d")
        clock.advance(3600)
    assert limiter.admit("ret1a2b3c4d") is not None
    clock.advance(86400)
    assert limiter.admit("ret1a2b3c4d") is None


def test_a_refusal_by_the_daily_cap_does_not_spend_the_hourly_allowance():
    """Both limits have to have room before either is charged."""
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(4):
        for _ in range(5):
            limiter.admit("ret1a2b3c4d")
        clock.advance(3600)
    for _ in range(10):
        assert limiter.admit("ret1a2b3c4d") is not None
    clock.advance(86400 - 4 * 3600)
    assert [limiter.admit("ret1a2b3c4d") for _ in range(5)] == [None] * 5


def test_two_node_ids_do_not_share_a_registration_counter():
    clock = FakeClock(0.0)
    limiter = _registration(clock)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    assert limiter.admit("ret1a2b3c4d") is not None
    assert limiter.admit("ret000000001") is None


def test_a_live_registration_counter_survives_a_crowded_map():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=2)
    for _ in range(5):
        limiter.admit("ret1a2b3c4d")
    for n in range(20):
        limiter.admit(f"ret00000000{n}")
    assert limiter.admit("ret1a2b3c4d") is not None


def test_a_full_registration_map_refuses_a_node_id_it_has_never_seen():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=4)
    limiter.admit("ret000000000")
    limiter.admit("ret000000001")
    assert limiter.tracked_counters == 4
    refusal = limiter.admit("ret000000002")
    assert refusal is not None
    assert refusal.status_code == 403
    assert refusal.body == {"error": "forbidden"}
    assert limiter.tracked_counters == 4
    limiter.admit("ret000000003")
    assert limiter.tracked_counters == 4


def test_a_full_registration_map_still_serves_a_node_id_it_already_tracks():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=4)
    limiter.admit("ret000000000")
    limiter.admit("ret000000001")
    assert limiter.tracked_counters == 4
    assert limiter.admit("ret000000000") is None
    assert limiter.admit("ret000000001") is None


def test_a_full_map_admits_a_tracked_node_whose_hourly_key_expired_but_daily_key_is_live():
    """A tracked node must not be refused just because a reclaim swept one of its
    two keys: the hourly key expires every hour by design, and its daily sibling
    being alive with allowance left is what makes this node a known identity
    rather than one the map has never seen."""
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=4)
    limiter.admit("ret000000000")  # hourly and daily keys open at t=0
    clock.advance(3600)  # ret000000000's hourly key is now expired
    limiter.admit("ret000000001")  # fills the map; does not yet trigger reclaim
    limiter.admit("ret000000002")  # crowds the map: this call's reclaim sweeps the
    # expired hourly key of ret000000000, then admits ret000000002 past the bound
    assert limiter.tracked_counters >= 4
    assert limiter.admit("ret000000000") is None


def test_a_previously_refused_node_id_is_admitted_once_its_window_is_reclaimed():
    clock = FakeClock(0.0)
    limiter = _registration(clock, max_tracked=4)
    limiter.admit("ret000000000")
    limiter.admit("ret000000001")
    assert limiter.admit("ret000000002") is not None
    clock.advance(86400)
    assert limiter.admit("ret000000002") is None
    assert limiter.tracked_counters == 2


# ── Claiming ─────────────────────────────────────────────────────────────────
#
# The claim limiter counts in the database, so these run against a real SQLite
# file opened with production's pragmas. The table is built from the model
# rather than by a migration: what is under test here is the limiter, and
# tests/test_migrations.py is what holds the two schemas together.

BACKEND = Path(__file__).resolve().parents[1]

ADA = "ada@example.com"
GRACE = "grace@example.com"


def _engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    return engine


@pytest.fixture
def counter_db(tmp_path) -> Path:
    """A database file holding an empty rate_counters table and nothing else.

    Built with production's pragmas, so the file is in WAL mode before anything
    else opens it, as production's has been since its first migration. Switching
    a file into WAL needs a lock SQLite does not wait for, so two processes
    connecting to a fresh file at once can refuse each other on connect, which
    is a fact about the fixture and not about the limiter.
    """
    path = tmp_path / "rate_counters.db"
    engine = create_engine(f"sqlite:///{path}")
    event.listen(engine, "connect", _set_sqlite_pragmas)
    try:
        RateCounter.__table__.create(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture
async def claim_session(counter_db):
    engine = _engine(counter_db)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _key(scope: str, identity: str) -> str:
    return hashlib.sha256(f"{scope}\x00{identity}".encode()).hexdigest()


def _committed(path: Path) -> dict[tuple[str, int], int]:
    """Every counter's count as committed, read on a connection of its own, so
    nothing the limiter's session holds can show through."""
    with closing(sqlite3.connect(path)) as con:
        return {
            (key, window_s): count
            for key, window_s, count in con.execute("SELECT key, window_s, count FROM rate_counters")
        }


def test_the_documented_claim_rates_are_the_ones_configured():
    assert CLAIM_LIMITS == ((5, 3600), (20, 86400))


def test_the_claim_limiter_keeps_wall_clock_time():
    """Its counters outlive the process, and a monotonic reading means nothing to
    another process or to this one after a restart."""
    assert inspect.signature(ClaimRateLimiter).parameters["clock"].default is time.time


async def test_five_nominations_an_hour_are_admitted_and_the_sixth_is_not(claim_session):
    limiter = ClaimRateLimiter(clock=FakeClock())

    for _ in range(5):
        assert await limiter.admit(claim_session, "ret1a2b3c4d", ADA) is None

    refusal = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    assert refusal is not None
    assert (refusal.status_code, refusal.body) == (429, RATE_LIMITED_BODY)


async def test_two_nodes_nominating_one_address_share_its_allowance(claim_session):
    """The address limit is what bounds the mailbox, and a node is cheap to
    register, so it cannot be per node."""
    limiter = ClaimRateLimiter(clock=FakeClock())

    for i in range(5):
        assert await limiter.admit(claim_session, f"ret0000000{i}", ADA) is None

    assert await limiter.admit(claim_session, "retdeadbeef", ADA) is not None


async def test_one_node_nominating_a_second_address_still_spends_its_own_allowance(claim_session):
    limiter = ClaimRateLimiter(clock=FakeClock())

    for i in range(5):
        assert await limiter.admit(claim_session, "ret1a2b3c4d", f"ada{i}@example.com") is None

    assert await limiter.admit(claim_session, "ret1a2b3c4d", GRACE) is not None


async def test_a_refusal_by_the_address_limit_does_not_spend_the_node_limit(claim_session):
    """One popular address would otherwise exhaust every node that tried it."""
    limiter = ClaimRateLimiter(clock=FakeClock())
    for i in range(5):
        await limiter.admit(claim_session, f"ret0000000{i}", ADA)

    for _ in range(10):
        assert await limiter.admit(claim_session, "retdeadbeef", ADA) is not None

    for i in range(5):
        assert await limiter.admit(claim_session, "retdeadbeef", f"grace{i}@example.com") is None


async def test_a_refused_nomination_writes_nothing(counter_db, claim_session):
    """The node's counters are spent before the address's is found full, and
    the rollback has to take them back."""
    limiter = ClaimRateLimiter(clock=FakeClock())
    for i in range(5):
        await limiter.admit(claim_session, f"ret0000000{i}", ADA)
    before = _committed(counter_db)

    assert await limiter.admit(claim_session, "retdeadbeef", ADA) is not None

    assert _committed(counter_db) == before
    assert not claim_session.in_transaction()


async def test_an_admission_commits_though_the_caller_read_first(counter_db, claim_session):
    """The route reads where the claim stands before it nominates, so the
    session arrives with a transaction already begun."""
    await claim_session.execute(select(RateCounter.key))
    assert claim_session.in_transaction()

    assert await ClaimRateLimiter(clock=FakeClock()).admit(claim_session, "ret1a2b3c4d", ADA) is None

    assert not claim_session.in_transaction()
    assert set(_committed(counter_db).values()) == {1}
    assert len(_committed(counter_db)) == 4


async def test_the_table_holds_no_address_in_clear(counter_db, claim_session):
    await ClaimRateLimiter(clock=FakeClock()).admit(claim_session, "ret1a2b3c4d", ADA)

    with closing(sqlite3.connect(counter_db)) as con:
        dump = "\n".join(con.iterdump())
    # Not "ada" alone: those are three hex digits, which a digest may contain.
    assert ADA not in dump
    assert "example" not in dump
    assert set(_committed(counter_db)) == {
        (_key(scope, identity), window_s)
        for scope, identity in (("claim", "ret1a2b3c4d"), ("claim_address", ADA))
        for _limit, window_s in CLAIM_LIMITS
    }


async def test_the_claim_daily_limit_outlives_the_hourly_one(claim_session):
    clock = FakeClock()
    limiter = ClaimRateLimiter(clock=clock)

    for _ in range(4):
        for _ in range(5):
            assert await limiter.admit(claim_session, "ret1a2b3c4d", ADA) is None
        clock.advance(3600)

    refusal = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    assert refusal is not None
    # The hourly window is fresh, so what is counted down is the day's.
    assert refusal.retry_after_s == 86400 - 4 * 3600


async def test_the_claim_hourly_window_resets(claim_session):
    clock = FakeClock()
    limiter = ClaimRateLimiter(clock=clock)
    for _ in range(5):
        await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    assert await limiter.admit(claim_session, "ret1a2b3c4d", ADA) is not None

    clock.advance(3600)

    assert await limiter.admit(claim_session, "ret1a2b3c4d", ADA) is None


async def test_the_claim_retry_after_counts_down_the_window_and_is_never_zero(claim_session):
    clock = FakeClock()
    limiter = ClaimRateLimiter(clock=clock)
    for _ in range(5):
        await limiter.admit(claim_session, "ret1a2b3c4d", ADA)

    early = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    clock.advance(3599.9)
    late = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)

    assert (early.retry_after_s, late.retry_after_s) == (3600, 1)


async def test_a_nomination_retried_when_the_retry_after_says_is_admitted(claim_session):
    """Rounded up, never down, so a node that waits as long as it was told is
    not refused a second time."""
    clock = FakeClock(1000.25)
    limiter = ClaimRateLimiter(clock=clock)
    for _ in range(5):
        await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    clock.advance(1234.5)

    refusal = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)
    clock.advance(refusal.retry_after_s)

    assert await limiter.admit(claim_session, "ret1a2b3c4d", ADA) is None


async def test_with_node_and_address_both_full_the_retry_after_counts_down_the_nodes_window(claim_session):
    """The node already knows its own window; the address's would tell it when
    somebody else nominated that address."""
    clock = FakeClock(1000.0)
    limiter = ClaimRateLimiter(clock=clock)
    for i in range(5):
        await limiter.admit(claim_session, "ret1a2b3c4d", f"grace{i}@example.com")
    clock.advance(1000)
    for i in range(5):
        await limiter.admit(claim_session, f"ret0000000{i}", ADA)

    refusal = await limiter.admit(claim_session, "ret1a2b3c4d", ADA)

    assert refusal.retry_after_s == 3600 - 1000


async def test_an_error_midway_takes_back_what_was_spent_and_releases_the_lock(counter_db, claim_session, monkeypatch):
    """Left alone, the node's counters would sit uncommitted in the caller's
    session, holding SQLite's write lock against every other worker until the
    session happened to close."""
    real_spend = node_rate_limits._spend
    spent = []

    def failing_spend(key, window_s, now):
        spent.append(key)
        if len(spent) == 3:
            raise RuntimeError("the address's counter could not be spent")
        return real_spend(key, window_s, now)

    monkeypatch.setattr(node_rate_limits, "_spend", failing_spend)

    with pytest.raises(RuntimeError):
        await ClaimRateLimiter(clock=FakeClock()).admit(claim_session, "ret1a2b3c4d", ADA)

    assert not claim_session.in_transaction()
    assert _committed(counter_db) == {}
    with closing(sqlite3.connect(counter_db, timeout=0)) as con:
        con.execute("BEGIN IMMEDIATE")
        con.rollback()


async def test_counters_whose_window_has_passed_are_swept_by_the_next_admission(counter_db, claim_session):
    clock = FakeClock()
    limiter = ClaimRateLimiter(clock=clock)
    await limiter.admit(claim_session, "ret1a2b3c4d", ADA)

    clock.advance(3600 + 1)
    await limiter.admit(claim_session, "retdeadbeef", GRACE)
    assert {key for key, _window_s in _committed(counter_db)} == {
        _key("claim", "ret1a2b3c4d"),
        _key("claim_address", ADA),
        _key("claim", "retdeadbeef"),
        _key("claim_address", GRACE),
    }
    assert (_key("claim", "ret1a2b3c4d"), 3600) not in _committed(counter_db)
    assert (_key("claim_address", ADA), 3600) not in _committed(counter_db)

    clock.advance(86400 + 1)
    await limiter.admit(claim_session, "ret000000001", "linus@example.com")
    assert {key for key, _window_s in _committed(counter_db)} == {
        _key("claim", "ret000000001"),
        _key("claim_address", "linus@example.com"),
    }


async def test_a_restart_does_not_refill_a_spent_allowance(counter_db):
    """A new engine and a new limiter on the same file, which is all a restarted
    process brings with it."""
    clock = FakeClock()
    engine = _engine(counter_db)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        limiter = ClaimRateLimiter(clock=clock)
        for _ in range(5):
            assert await limiter.admit(session, "ret1a2b3c4d", ADA) is None
    await engine.dispose()

    engine = _engine(counter_db)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            limiter = ClaimRateLimiter(clock=clock)
            by_node = await limiter.admit(session, "ret1a2b3c4d", GRACE)
            by_address = await limiter.admit(session, "retdeadbeef", ADA)
    finally:
        await engine.dispose()

    assert by_node is not None
    assert by_node.status_code == 429
    assert by_address is not None
    assert by_address.status_code == 429


# One process of the contention test below: the module's own limiter, with the
# wall clock, against the file it is handed. It reads before each nomination as
# the route does, so a read that left a transaction open would show here as
# SQLite refusing the upgrade to a write.
_CONTENDER = """
import asyncio
import sys

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.rate_counters import RateCounter
from core.users import _set_sqlite_pragmas
from services.node_rate_limits import claim_rate_limiter


async def main(path, email, attempts):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    admitted = 0
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(select(RateCounter.key))
        print("ready", flush=True)
        sys.stdin.readline()
        for _ in range(attempts):
            await session.execute(select(RateCounter.key))
            if await claim_rate_limiter.admit(session, "ret1a2b3c4d", email) is None:
                admitted += 1
    await engine.dispose()
    print(admitted, flush=True)


asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
"""


def test_two_processes_nominating_at_once_share_one_allowance(counter_db):
    """Two workers, or a worker and its replacement mid-deploy, spend one node's
    allowance between them. Each nominates an address of its own, so the node's
    counter is the only one they share: counted per process, as the limiter once
    was, this admits ten.

    Both start connected and are released together, so their attempts overlap
    rather than one process finishing before the other has imported anything.
    """
    contenders = [
        subprocess.Popen(
            [sys.executable, "-c", _CONTENDER, str(counter_db), f"owner{n}@example.com", "10"],
            cwd=BACKEND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(2)
    ]
    try:
        for contender in contenders:
            assert contender.stdout.readline() == "ready\n", contender.communicate(timeout=60)[1]
        for contender in contenders:
            contender.stdin.write("go\n")
            contender.stdin.flush()
        results = [contender.communicate(timeout=60) for contender in contenders]
    finally:
        for contender in contenders:
            if contender.poll() is None:
                contender.kill()
                contender.wait()

    assert [contender.returncode for contender in contenders] == [0, 0], [err for _out, err in results]
    assert sum(int(out) for out, _err in results) == 5
    assert _committed(counter_db)[(_key("claim", "ret1a2b3c4d"), 3600)] == 5


# ── Polled radar probes ──────────────────────────────────────────────────────


def _probe_limiter(clock: FakeClock, max_tracked: int = 64) -> PolledProbeRateLimiter:
    return PolledProbeRateLimiter(clock=clock, max_tracked=max_tracked)


def test_ten_probes_an_hour_per_account():
    assert POLLED_PROBE_LIMITS == ((10, 3600),)


def test_the_eleventh_probe_in_an_hour_is_refused_until_the_hour_turns():
    clock = FakeClock(3600.0)
    limiter = _probe_limiter(clock)
    for _ in range(10):
        assert limiter.admit("user-a") is None

    clock.advance(600)
    refusal = limiter.admit("user-a")
    assert (refusal.status_code, refusal.retry_after_s) == (429, 3000)

    clock.advance(3000)
    assert limiter.admit("user-a") is None


def test_one_account_probing_leaves_another_its_own_allowance():
    clock = FakeClock()
    limiter = _probe_limiter(clock)
    for _ in range(10):
        limiter.admit("user-a")

    assert limiter.admit("user-a") is not None
    assert limiter.admit("user-b") is None


def test_a_full_probe_map_refuses_an_account_it_does_not_already_track():
    """Accounts cost a mailbox each, so the bound is enforced as registration's is."""
    clock = FakeClock()
    limiter = _probe_limiter(clock, max_tracked=1)
    assert limiter.admit("user-a") is None

    assert limiter.admit("user-b") is not None
    assert limiter.admit("user-a") is None
