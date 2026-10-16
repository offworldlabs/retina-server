"""core/mender_mirror.py: the two settings read per call, the sync state read,
the mirror's age and staleness, and the tables as the models build them."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.mender_mirror import (
    DEFAULT_MIRROR_MAX_AGE_S,
    MENDER_SOURCES,
    MIRROR_FUTURE_TOLERANCE_S,
    SYNC_STATE_ID,
    MenderDevice,
    MenderSyncState,
    load_sync_state,
    mender_source,
    mirror_max_age_s,
    snapshot_age_s,
    stale_reason,
)
from core.users import Base, _set_sqlite_pragmas

NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
async def maker(tmp_path):
    """Sessions on a database holding the two mirror tables as create_all builds them."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mirror.db'}")
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[MenderDevice.__table__, MenderSyncState.__table__])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _state(last_snapshot_at: datetime = NOON) -> MenderSyncState:
    return MenderSyncState(
        id=SYNC_STATE_ID,
        last_snapshot_at=last_snapshot_at,
        generated_at=last_snapshot_at - timedelta(seconds=1),
        device_count=21,
        source="mender-auto-accept@central",
    )


def _device(
    device_id: str, node_id: str | None, *, present: bool = True, snapshot_issue: str | None = None
) -> MenderDevice:
    return MenderDevice(
        mender_device_id=device_id,
        node_id=node_id,
        auth_status="accepted",
        last_confirmed_at=NOON,
        present=present,
        snapshot_issue=snapshot_issue,
    )


def test_the_names_the_next_readers_build_on():
    assert SYNC_STATE_ID == 1
    assert DEFAULT_MIRROR_MAX_AGE_S == 300.0
    assert MIRROR_FUTURE_TOLERANCE_S == 30.0
    assert MENDER_SOURCES == ("api", "shadow", "mirror")


# ── MENDER_SOURCE ────────────────────────────────────────────────────────────


def test_mender_source_is_the_api_when_unset(monkeypatch):
    monkeypatch.delenv("MENDER_SOURCE", raising=False)
    assert mender_source() == "api"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_mender_source_is_the_api_when_blank(monkeypatch, raw):
    monkeypatch.setenv("MENDER_SOURCE", raw)
    assert mender_source() == "api"


@pytest.mark.parametrize(
    ("raw", "mode"),
    [("api", "api"), ("shadow", "shadow"), ("mirror", "mirror"), (" Mirror\n", "mirror"), ("SHADOW", "shadow")],
)
def test_mender_source_names_a_mode_whatever_its_case_and_spacing(monkeypatch, raw, mode):
    monkeypatch.setenv("MENDER_SOURCE", raw)
    assert mender_source() == mode


def test_an_unknown_mender_source_comes_back_for_the_caller_to_refuse(monkeypatch):
    monkeypatch.setenv("MENDER_SOURCE", " Mirrors ")
    assert mender_source() == "mirrors"
    assert mender_source() not in MENDER_SOURCES


def test_mender_source_is_read_per_call(monkeypatch):
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    assert mender_source() == "shadow"
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    assert mender_source() == "mirror"


# ── MENDER_MIRROR_MAX_AGE_S ──────────────────────────────────────────────────


def test_mirror_max_age_is_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    assert mirror_max_age_s() == 300.0


@pytest.mark.parametrize("raw", ["", "   "], ids=["empty", "spaces"])
def test_mirror_max_age_is_the_default_when_blank(monkeypatch, raw):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    assert mirror_max_age_s() == 300.0


@pytest.mark.parametrize(("raw", "seconds"), [("120", 120.0), (" 45.5 ", 45.5), ("1e3", 1000.0), ("0.25", 0.25)])
def test_mirror_max_age_takes_a_positive_number_of_seconds(monkeypatch, raw, seconds):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    assert mirror_max_age_s() == seconds


@pytest.mark.parametrize(
    "raw",
    ["five", "300s", "0", "0.0", "-0", "-1", "-0.5", "inf", "-inf", "Infinity", "nan"],
)
def test_mirror_max_age_refuses_anything_but_a_positive_finite_number(monkeypatch, raw):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    with pytest.raises(ValueError, match="MENDER_MIRROR_MAX_AGE_S"):
        mirror_max_age_s()


def test_mirror_max_age_is_read_per_call(monkeypatch):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "60")
    assert mirror_max_age_s() == 60.0
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "600")
    assert mirror_max_age_s() == 600.0


# ── snapshot_age_s ───────────────────────────────────────────────────────────


def test_there_is_no_age_without_a_snapshot():
    assert snapshot_age_s(None, NOON) is None


def test_the_age_is_the_seconds_since_the_snapshot_arrived():
    assert snapshot_age_s(_state(), NOON + timedelta(seconds=90, milliseconds=250)) == 90.25
    assert snapshot_age_s(_state(), NOON) == 0.0


def test_the_age_does_not_depend_on_the_zone_now_is_given_in():
    two_hours_east = timezone(timedelta(hours=2))
    assert snapshot_age_s(_state(), (NOON + timedelta(seconds=30)).astimezone(two_hours_east)) == 30.0


def test_the_age_is_negative_when_the_snapshot_is_ahead_of_now():
    """As after this server's clock stepped back: left signed for
    stale_reason to judge, not clamped to a fresh-looking 0."""
    assert snapshot_age_s(_state(), NOON - timedelta(seconds=30)) == -30.0
    assert snapshot_age_s(_state(), NOON - timedelta(hours=1, milliseconds=500)) == -3600.5


# ── stale_reason ─────────────────────────────────────────────────────────────


def _reason_at(age_s: float) -> str | None:
    """stale_reason for a snapshot that arrived age_s ago; negative is ahead."""
    return stale_reason(_state(), NOON + timedelta(seconds=age_s))


def test_with_no_snapshot_the_mirror_is_stale(monkeypatch):
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    assert stale_reason(None, NOON) == "no snapshot yet"


def test_no_snapshot_is_reported_before_a_malformed_limit(monkeypatch):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "five")
    assert stale_reason(None, NOON) == "no snapshot yet"


@pytest.mark.parametrize("age_s", [0.0, 1.0, 299.9, 300.0, -1.0, -29.9, -30.0], ids=str)
def test_within_the_limit_and_the_tolerance_the_mirror_is_fresh(monkeypatch, age_s):
    """The limit and the tolerance are both inclusive."""
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    assert _reason_at(age_s) is None


@pytest.mark.parametrize(
    ("age_s", "reason"),
    [
        (300.4, "last snapshot 301 s ago, limit 300 s"),
        (300.000001, "last snapshot 301 s ago, limit 300 s"),
        (301.0, "last snapshot 301 s ago, limit 300 s"),
        (3600.0, "last snapshot 3600 s ago, limit 300 s"),
    ],
    ids=["300.4", "a microsecond over", "301", "an hour"],
)
def test_an_old_snapshot_is_stale_with_its_age_rounded_up(monkeypatch, age_s, reason):
    """Rounded up, so a stale mirror never reads as an age within its limit."""
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    assert _reason_at(age_s) == reason


@pytest.mark.parametrize(
    ("raw", "age_s", "reason"),
    [
        ("60", 61.0, "last snapshot 61 s ago, limit 60 s"),
        ("45.5", 46.0, "last snapshot 46 s ago, limit 45.5 s"),
        ("1e3", 1000.5, "last snapshot 1001 s ago, limit 1000 s"),
        ("0.25", 0.5, "last snapshot 1 s ago, limit 0.25 s"),
    ],
)
def test_the_limit_is_the_configured_one_written_short(monkeypatch, raw, age_s, reason):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    assert _reason_at(age_s) == reason


@pytest.mark.parametrize(
    ("age_s", "reason"),
    [
        (-31.0, "last snapshot is 31 s in the future, the server clock went back"),
        (-30.000001, "last snapshot is 31 s in the future, the server clock went back"),
        (-30.5, "last snapshot is 31 s in the future, the server clock went back"),
        (-3600.0, "last snapshot is 3600 s in the future, the server clock went back"),
    ],
    ids=["31", "a microsecond past the tolerance", "30.5", "an hour"],
)
def test_a_snapshot_further_ahead_than_the_tolerance_is_stale(monkeypatch, age_s, reason):
    """The clock went back, so the time since the last push is unknown: a
    mirror nobody has pushed to since would otherwise read as fresh for as
    long as the step."""
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    assert _reason_at(age_s) == reason


def test_the_tolerance_does_not_grow_with_the_limit(monkeypatch):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "3600")
    assert _reason_at(-31.0) == "last snapshot is 31 s in the future, the server clock went back"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("five", "MENDER_MIRROR_MAX_AGE_S='five' is not a number"),
        ("-1", "MENDER_MIRROR_MAX_AGE_S='-1' is not a positive number of seconds"),
        ("nan", "MENDER_MIRROR_MAX_AGE_S='nan' is not a positive number of seconds"),
    ],
)
@pytest.mark.parametrize("age_s", [0.0, 3600.0, -3600.0], ids=["fresh", "old", "ahead"])
def test_a_malformed_limit_is_the_reason_whatever_the_age(monkeypatch, raw, reason, age_s):
    """mirror_max_age_s's own text: the mirror is never judged fresh against
    a limit nobody chose."""
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    with pytest.raises(ValueError) as raised:
        mirror_max_age_s()
    assert str(raised.value) == reason
    assert _reason_at(age_s) == reason


def test_stale_reason_reads_the_limit_per_call(monkeypatch):
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "600")
    assert _reason_at(400.0) is None
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "300")
    assert _reason_at(400.0) == "last snapshot 400 s ago, limit 300 s"


def test_stale_reason_does_not_depend_on_the_zone_now_is_given_in(monkeypatch):
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)
    two_hours_east = timezone(timedelta(hours=2))
    assert stale_reason(_state(), (NOON + timedelta(seconds=299)).astimezone(two_hours_east)) is None
    assert (
        stale_reason(_state(), (NOON + timedelta(seconds=301)).astimezone(two_hours_east))
        == "last snapshot 301 s ago, limit 300 s"
    )


# ── load_sync_state and the tables ───────────────────────────────────────────


async def test_there_is_no_sync_state_before_the_first_snapshot(maker):
    async with maker() as session:
        assert await load_sync_state(session) is None


async def test_the_sync_state_row_is_read_back_aware(maker):
    async with maker() as session:
        session.add(_state())
        await session.commit()

    async with maker() as session:
        state = await load_sync_state(session)

    assert state is not None
    assert (state.id, state.device_count, state.source) == (SYNC_STATE_ID, 21, "mender-auto-accept@central")
    assert state.last_snapshot_at == NOON
    assert state.last_snapshot_at.tzinfo is not None
    assert state.generated_at == NOON - timedelta(seconds=1)
    assert snapshot_age_s(state, NOON + timedelta(seconds=10)) == 10.0


async def test_the_sync_state_is_read_from_the_database_not_the_session(maker):
    """A session that read the row before a newer snapshot landed must see the
    newer one, or a mirror gone stale since would read as fresh."""
    async with maker() as writer:
        writer.add(_state())
        await writer.commit()

    async with maker() as reader:
        first = await load_sync_state(reader)
        assert first is not None
        assert first.last_snapshot_at == NOON

        async with maker() as writer:
            later = NOON + timedelta(minutes=5)
            await writer.execute(update(MenderSyncState).values(last_snapshot_at=later, device_count=22))
            await writer.commit()

        second = await load_sync_state(reader)
        assert second is not None
        assert (second.last_snapshot_at, second.device_count) == (later, 22)


async def test_a_second_sync_state_row_is_refused(maker):
    async with maker() as session:
        session.add(_state())
        await session.commit()
        second = _state()
        second.id = 2
        session.add(second)
        with pytest.raises(IntegrityError, match="ck_mender_sync_state_single_row"):
            await session.commit()


async def test_a_device_round_trips_every_column(maker):
    accepted = NOON - timedelta(days=3)
    async with maker() as session:
        session.add(
            MenderDevice(
                mender_device_id="6703f1",
                node_id="ret2b3c4d5e",
                auth_status="accepted",
                auth_set_id="a1b2",
                auth_set_pubkey="-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
                auth_set_fingerprint="f" * 64,
                auth_set_changed_at=accepted,
                accepted_at=accepted,
                last_confirmed_at=NOON,
                device_type="pi5-v3-arm64",
                artifact_name="owl-os-1.4.2",
                inventory_updated_at=NOON - timedelta(seconds=31),
                snapshot_issue="node_id also sent by 6703f2",
            )
        )
        await session.commit()

    async with maker() as session:
        device = (await session.execute(select(MenderDevice))).scalar_one()

    assert (device.mender_device_id, device.node_id, device.auth_status) == ("6703f1", "ret2b3c4d5e", "accepted")
    assert (device.auth_set_id, device.auth_set_fingerprint) == ("a1b2", "f" * 64)
    assert device.auth_set_pubkey == "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
    assert (device.auth_set_changed_at, device.accepted_at) == (accepted, accepted)
    assert device.last_confirmed_at == NOON
    assert device.inventory_updated_at == NOON - timedelta(seconds=31)
    assert all(
        stamp.tzinfo is not None for stamp in (device.auth_set_changed_at, device.accepted_at, device.last_confirmed_at)
    )
    assert (device.device_type, device.artifact_name) == ("pi5-v3-arm64", "owl-os-1.4.2")
    assert device.snapshot_issue == "node_id also sent by 6703f2"
    # Present unless a snapshot left it out.
    assert device.present is True


async def test_a_device_is_clean_unless_given_an_issue(maker):
    async with maker() as session:
        session.add(MenderDevice(mender_device_id="6703f1", auth_status="pending", last_confirmed_at=NOON))
        await session.commit()

    async with maker() as session:
        device = (await session.execute(select(MenderDevice))).scalar_one()

    assert device.snapshot_issue is None


async def test_one_present_device_per_node_id_however_many_are_kept_absent(maker):
    async with maker() as session:
        session.add_all(
            [
                _device("dev-a", "ret1a2b3c4d"),
                _device("dev-b", "ret1a2b3c4d", present=False),
                _device("dev-c", "ret1a2b3c4d", present=False),
                _device("dev-d", None),
                _device("dev-e", None),
            ]
        )
        await session.commit()
        session.add(_device("dev-f", "ret1a2b3c4d"))
        with pytest.raises(IntegrityError, match="mender_devices.node_id"):
            await session.commit()


@pytest.mark.parametrize(
    ("first_issue", "second_issue"),
    [(None, "node_id also sent by dev-a"), ("node_id also sent by dev-b", None), ("one", "two")],
    ids=["clean then flagged", "flagged then clean", "both flagged"],
)
async def test_a_device_with_a_snapshot_issue_shares_its_node_id(maker, first_issue, second_issue):
    async with maker() as session:
        session.add(_device("dev-a", "ret1a2b3c4d", snapshot_issue=first_issue))
        await session.commit()
        session.add(_device("dev-b", "ret1a2b3c4d", snapshot_issue=second_issue))
        await session.commit()

    async with maker() as session:
        rows = (
            await session.execute(
                select(MenderDevice.mender_device_id, MenderDevice.snapshot_issue).order_by(
                    MenderDevice.mender_device_id
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [("dev-a", first_issue), ("dev-b", second_issue)]


async def test_two_clean_present_devices_still_collide_beside_a_flagged_one(maker):
    async with maker() as session:
        session.add_all(
            [
                _device("dev-a", "ret1a2b3c4d", snapshot_issue="node_id also sent by dev-b"),
                _device("dev-b", "ret1a2b3c4d"),
            ]
        )
        await session.commit()
        session.add(_device("dev-c", "ret1a2b3c4d"))
        with pytest.raises(IntegrityError, match="mender_devices.node_id"):
            await session.commit()
