import os
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, select, text

import core.nodes  # noqa: F401  registers the node tables on Base.metadata
from core.timestamps import UTCDateTime
from core.users import Base

_STAMPS = Table("stamps", MetaData(), Column("id", Integer, primary_key=True), Column("at", UTCDateTime()))
NOON = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    _STAMPS.metadata.create_all(engine)
    with engine.begin() as connection:
        yield connection
    engine.dispose()


@pytest.fixture
def host_behind_utc():
    """A host zone five hours west of UTC, as a POSIX rule so no tz database is needed.

    Restating a zone and converting into one differ only off UTC, and CI runs on UTC.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("this interpreter cannot change its zone: it has no time.tzset")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "EST5"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _stored(db) -> str:
    return db.scalar(text("SELECT at FROM stamps"))


def _read(db) -> datetime | None:
    return db.scalar(select(_STAMPS.c.at))


def test_an_aware_value_is_stored_as_its_utc_wall_clock(db):
    db.execute(_STAMPS.insert().values(id=1, at=datetime(2026, 9, 28, 14, 0, tzinfo=timezone(timedelta(hours=2)))))

    assert _stored(db).startswith("2026-09-28 12:00:00")


@pytest.mark.usefixtures("host_behind_utc")
def test_a_stored_value_reads_back_aware_in_utc_whatever_the_host_zone(db):
    db.execute(text("INSERT INTO stamps (id, at) VALUES (1, '2026-09-28 12:00:00.000000')"))

    read = _read(db)
    # Aware values compare by instant, so the offset is checked on its own.
    assert (read, read.utcoffset()) == (NOON, timedelta(0))


@pytest.mark.usefixtures("host_behind_utc")
def test_a_naive_value_is_taken_to_be_utc(db):
    db.execute(_STAMPS.insert().values(id=1, at=NOON.replace(tzinfo=None)))

    assert _stored(db).startswith("2026-09-28 12:00:00")


def test_an_absent_value_stays_absent(db):
    db.execute(_STAMPS.insert().values(id=1, at=None))

    assert _read(db) is None


def test_every_stored_timestamp_uses_the_utc_type():
    """A plain DateTime column reads back naive, and nothing else would say so."""
    plain = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime)
    ]
    assert plain == []
