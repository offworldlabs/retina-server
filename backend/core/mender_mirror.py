"""The Mender mirror: the fleet as the last snapshot from mender-auto-accept saw it.

mender-auto-accept is the only reader of the Mender management API. Each sweep
it posts the whole fleet to the snapshot route (routes/mender_sync.py), which
writes these two tables. Registration reads them in place of a live call when
MENDER_SOURCE says so (services/mender.py), and the admin fleet page joins them
to the registry. The contract is docs/mender-snapshot.md.

`node_id` carries no foreign key: Mender can know a device the registry has
never seen, and the registry outlives a device Mender forgot.
"""

import math
import os
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from core.timestamps import UTCDateTime
from core.users import Base

#: The one row mender_sync_state holds.
SYNC_STATE_ID = 1
#: Ten missed sweeps at mender-auto-accept's 30 s.
DEFAULT_MIRROR_MAX_AGE_S = 300.0
#: How far ahead of this server's clock the last snapshot may sit and still
#: count as fresh: clock jitter, not a step back.
MIRROR_FUTURE_TOLERANCE_S = 30.0
#: What MENDER_SOURCE may name: the live API, the API checked against the
#: mirror, or the mirror alone.
MENDER_SOURCES = ("api", "shadow", "mirror")


class MenderDevice(Base):
    """One Mender device, keyed on Mender's own id.

    A device missing from a snapshot is kept with `present` false rather than
    deleted. The unique index on `node_id` counts present rows with no
    `snapshot_issue` only, so neither a decommissioned device nor one whose
    latest entry is unusable stops another enrolling under the same node_id.

    `snapshot_issue`: why the device's entry in the latest snapshot cannot be
    used for registration (null when clean).

    Timestamps:
    - `accepted_at`: when the device last became accepted; null while it is not.
    - `auth_set_changed_at`: when the accepted auth set id last changed.
    - `last_confirmed_at`: the `generated_at` of the last snapshot that listed
      the device.
    - `inventory_updated_at`: Mender's inventory `updated_ts`, as sent.
    """

    __tablename__ = "mender_devices"
    __table_args__ = (
        Index(
            "ux_mender_devices_present_node_id",
            "node_id",
            unique=True,
            sqlite_where=text("present = 1 AND snapshot_issue IS NULL"),
        ),
    )

    mender_device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_status: Mapped[str] = mapped_column(String(32))
    auth_set_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_set_pubkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_set_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auth_set_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    device_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    artifact_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    inventory_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    snapshot_issue: Mapped[str | None] = mapped_column(String(128), nullable=True)


class MenderSyncState(Base):
    """The last snapshot taken, in a single row.

    Timestamps:
    - `last_snapshot_at`: when this server received it, by its own clock. What
      staleness is measured against, so a sender's clock cannot make the
      mirror look fresh.
    - `generated_at`: when the sender built it, by the sender's clock. A
      snapshot not newer than this one is refused.
    """

    __tablename__ = "mender_sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_mender_sync_state_single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SYNC_STATE_ID)
    last_snapshot_at: Mapped[datetime] = mapped_column(UTCDateTime())
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    device_count: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(128))


# Both read per call, not at import: backend/main.py calls load_dotenv() after
# its imports, so a module-level read would miss a value set in .env.


def mender_source() -> str:
    """MENDER_SOURCE, stripped and lower-cased, or "api" when unset or blank.

    A value outside MENDER_SOURCES comes back as it is, for the caller to
    refuse: guessing a mode would quietly change what gates registration.
    """
    return (os.getenv("MENDER_SOURCE") or "").strip().lower() or "api"


def mirror_max_age_s() -> float:
    """MENDER_MIRROR_MAX_AGE_S in seconds, or DEFAULT_MIRROR_MAX_AGE_S when
    unset or blank.

    Anything else that is not a positive finite number raises ValueError
    rather than falling back to the default: this is the limit that keeps
    registration closed on a stale mirror, and a typo should not quietly set
    one nobody chose, or none at all.
    """
    raw = (os.getenv("MENDER_MIRROR_MAX_AGE_S") or "").strip()
    if not raw:
        return DEFAULT_MIRROR_MAX_AGE_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"MENDER_MIRROR_MAX_AGE_S={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"MENDER_MIRROR_MAX_AGE_S={raw!r} is not a positive number of seconds")
    return value


async def load_sync_state(session: AsyncSession) -> MenderSyncState | None:
    """The sync state row, or None before the first snapshot.

    Read from the database even when the session already holds the row: a
    copy cached there could make a stale mirror look fresh.
    """
    return await session.get(MenderSyncState, SYNC_STATE_ID, populate_existing=True)


def snapshot_age_s(state: MenderSyncState | None, now: datetime) -> float | None:
    """Seconds since the last snapshot arrived, or None if none ever has.

    `now` must be timezone-aware, as `last_snapshot_at` is read back. The age
    is signed: negative when `last_snapshot_at` is ahead of `now`, as after
    this server's clock stepped back. stale_reason judges that case.
    """
    if state is None:
        return None
    return (now - state.last_snapshot_at).total_seconds()


def stale_reason(state: MenderSyncState | None, now: datetime) -> str | None:
    """Why the mirror cannot be trusted at `now`, or None when it is fresh.

    A last snapshot more than MIRROR_FUTURE_TOLERANCE_S ahead of `now` is
    stale too: the clock went back, and the time since the last push is no
    longer known, so a mirror nobody has pushed to could read as fresh.
    Ages are rounded up, so a stale mirror never reads as within its limit.
    """
    age = snapshot_age_s(state, now)
    if age is None:
        return "no snapshot yet"
    try:
        limit = mirror_max_age_s()
    except ValueError as exc:
        return str(exc)
    if age > limit:
        return f"last snapshot {math.ceil(age)} s ago, limit {limit:g} s"
    if age < -MIRROR_FUTURE_TOLERANCE_S:
        return f"last snapshot is {math.ceil(-age)} s in the future, the server clock went back"
    return None
