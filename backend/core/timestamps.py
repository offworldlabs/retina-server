"""The column type every stored timestamp uses."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def _utc(when: datetime | None) -> datetime | None:
    # A naive value is taken to be UTC, as every stored one is.
    if when is None:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


class UTCDateTime(TypeDecorator):
    """A timestamp stored as UTC and read back aware.

    SQLite keeps no offset: it writes an aware value as its own wall clock and
    hands every value back naive. Converting on the way in and restating the zone
    on the way out keeps what is stored and what is read the same instant.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        return _utc(value)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        return _utc(value)
