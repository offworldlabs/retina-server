"""Access to the keyed data API: tiers, entitlements, keys, and per-day node activity.

- `tiers`: the limits a caller works under. Every database starts with two,
  one per kind of entitlement and named after it, and a value changes by an
  UPDATE.
- `entitlements`: that an account may use the API, of which kind, and on what
  grounds. A contributor row comes from the account's nodes' activity, a
  customer row from an administrator. A row ends by `lapsed_at`, never by
  deletion, so qualifying again opens a new row and the history stays.
- `api_keys`: an account's keys, each kept as the prefix it is shown and found
  by and a SHA-256 of its secret, never the secret.
- `node_activity_days`: one row per node per UTC day on which it delivered
  accepted detections, which the contributor rule counts.

An account's entitlements and keys go with it (ON DELETE CASCADE).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Connection,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.timestamps import UTCDateTime
from core.users import Base

KIND_CONTRIBUTOR = "contributor"
KIND_CUSTOMER = "customer"
SOURCE_UPTIME = "uptime"
SOURCE_MANUAL = "manual"

#: The tiers every database starts with. A kind's tier is the one named after
#: it. Migration 0029 seeds a migrated database with its own copy of these, and
#: _seed_tiers below a create_all build; tests/test_migrations.py holds both
#: to the same rows.
DEFAULT_TIERS = (
    {
        "id": 1,
        "name": KIND_CONTRIBUTOR,
        "requests_per_minute": 60,
        "max_concurrent_streams": 2,
        "max_history_span_s": 86_400,
    },
    {
        "id": 2,
        "name": KIND_CUSTOMER,
        "requests_per_minute": 600,
        "max_concurrent_streams": 10,
        "max_history_span_s": 604_800,
    },
)


class Tier(Base):
    """The limits a caller works under: requests a minute, open streams at
    once, and the longest history span one request may ask for."""

    __tablename__ = "tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer)
    max_concurrent_streams: Mapped[int] = mapped_column(Integer)
    max_history_span_s: Mapped[int] = mapped_column(Integer)


def _seed_tiers(table: Table, connection: Connection, **_kw: object) -> None:
    connection.execute(table.insert(), [dict(tier) for tier in DEFAULT_TIERS])


event.listen(Tier.__table__, "after_create", _seed_tiers)


class Entitlement(Base):
    """An account's entitlement to the API, open from `starts_at`.

    `source` says on what grounds: the account's nodes' uptime, an
    administrator's grant, or later a Stripe subscription, which writes the
    same fields. `ends_at` is when it stops, if that is known; `lapsed_at` is
    when it stopped, after which the row is history.

    An account holds at most one open contributor row.
    """

    __tablename__ = "entitlements"
    __table_args__ = (
        CheckConstraint("kind IN ('contributor', 'customer')", name="ck_entitlements_kind"),
        CheckConstraint("source IN ('uptime', 'manual', 'stripe')", name="ck_entitlements_source"),
        Index(
            "ux_entitlements_open_contributor",
            "user_id",
            unique=True,
            sqlite_where=text("kind = 'contributor' AND lapsed_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # CHAR(36), as "user".id is stored on SQLite.
    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    tier_id: Mapped[int] = mapped_column(Integer, ForeignKey("tiers.id"))
    source: Mapped[str] = mapped_column(String(16))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lapsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ApiKey(Base):
    """One of an account's keys.

    Found by `prefix`, the part of the key shown in lists, and checked against
    `secret_hash`, a SHA-256 of the rest: a read of this table must not be
    enough to call the API as anybody. A revoked key keeps its row.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # CHAR(36), as "user".id is stored on SQLite.
    user_id: Mapped[str] = mapped_column(CHAR(36), ForeignKey("user.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class NodeActivityDay(Base):
    """A UTC day on which a node delivered accepted detections."""

    __tablename__ = "node_activity_days"

    # No foreign key to nodes: a node reporting over TCP alone has no registry
    # row, and SQLite's INSERT OR IGNORE does not ignore a foreign key failure.
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
