"""Node data model, minimal.

This is ADR section 6 with the archive, rotation, reactivation, ownership and
mirror columns left out. They are in `docs/superpowers/plans/2026-08-06-node-api-v1.md`
with their reasons; adding one back later is a migration, which is what Alembic
is here for.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.users import Base

NODE_STATUSES = ("active", "retired", "blocked")


class Node(Base):
    """One radar node. `node_id` comes off the board and is what Mender knows it
    as; `node_ref` is the handle used anywhere public, so it can be rotated
    without the board being reflashed."""

    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_ref: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    board_model: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    active_config_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    licence_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    licence_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_management_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_management_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication: Mapped[str] = mapped_column(String(8), default="public", server_default="public")
    publication_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publication_chosen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeLocationPrivacy(Base):
    """An owner's or an admin's answer to "publish this node's location", set
    outside registration and outranking what registration recorded.

    `Node.publication` above is written once, by a board coming through the v1
    registration handshake, and is rewritten by a reflash. Neither is a place an
    owner can change their mind from, and most of what the server carries never
    registered at all: a legacy TCP node, the synthetic fleet, a node mirrored
    onto the test droplet. This table is the choice made from the dashboard, for
    any node id the system knows by string.

    So no foreign key to `nodes`, deliberately, and `String(255)` rather than
    the `String(32)` of `Node.node_id` — the same key space as
    `node_owners.node_id`, which is what the owner routes join against and which
    likewise accepts ids that never registered. A row here for an id nothing has
    ever heard of is inert rather than an error, which is the behaviour a
    pre-registration override needs.

    A row wins over the registration choice and deleting it hands the node back
    to that choice, so a reflash rewriting `Node.publication` cannot quietly
    republish a node its owner hid; `services/publication.effective_privacy`
    states the rule and is the only place it is stated.

    `set_by` and `set_at` are provenance, not an audit log: the dashboard shows
    the owner when and by whom the current state was set, and an admin change is
    additionally recorded through `log_event`. `set_by` is a user id for an
    owner and `admin:<email>` for an admin, the two being distinguishable
    without a second column because a uuid cannot contain a colon.
    """

    __tablename__ = "node_location_privacy"

    node_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False)
    set_by: Mapped[str] = mapped_column(String(255), default="", server_default="")
    set_at: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")


class NodeConfig(Base):
    """One row per configuration version, append-only.

    A detection frame arrives stamped with the version it was computed under, so
    superseded rows have to stay readable; updating in place would leave archived
    detections referring to geometry that no longer exists.
    """

    __tablename__ = "node_configs"
    __table_args__ = (UniqueConstraint("node_id", "version", name="uq_node_configs_node_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # Nullable since contract 1.1.3: an owner cannot always supply the geometry
    # at setup, and such a node is carried without being placed. Latitude and
    # longitude are validated as a pair; altitude stands alone.
    rx_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    rx_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    rx_alt_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_alt_ft: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_callsign: Mapped[str] = mapped_column(String(32))
    fc_hz: Mapped[float] = mapped_column(Float)
    fs_hz: Mapped[float] = mapped_column(Float)
    # Both nullable, and neither null may be filled in. A null width means the
    # antenna is not characterised, which is every node in the fleet: retina-gui
    # does not collect the geometry from owners, and an invented width would be
    # wrong data indistinguishable from a measurement. A null azimuth means
    # broadside or, equally, not characterised, and 0.0 is aimed due north.
    beam_width_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    beam_azimuth_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_range_km: Mapped[float] = mapped_column(Float)
    # Processing parameters, required on the wire since contract 1.1.0. Not null
    # like the other required columns: the validator rejects a config missing any
    # of them, so a null here would mean a bug upstream rather than a node that
    # genuinely has no CPI, and a made-up default would hide it.
    cpi_s: Mapped[float] = mapped_column(Float)
    delay_tolerance_us: Mapped[float] = mapped_column(Float)
    doppler_tolerance_hz: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeToken(Base):
    """A node's bearer credential, stored only as a SHA-256.

    There is no expiry: authentication is `revoked_at IS NULL` and nothing else.
    Revoked rows are kept rather than deleted so that when a board was reflashed,
    and when its previous credential died, stays answerable.
    """

    __tablename__ = "node_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
