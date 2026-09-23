"""Node data model, minimal.

This is ADR section 6 with the archive, rotation, reactivation, ownership and
mirror columns left out. They are in `docs/superpowers/plans/2026-08-06-node-api-v1.md`
with their reasons; adding one back later is a migration, which is what Alembic
is here for.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    the `String(32)` of `Node.node_id`. A row here for an id nothing has ever
    heard of is inert rather than an error, which is the behaviour a
    pre-registration override needs. Ownership (`node_claims`) is narrower: only
    a registered node can have an owner, so the owner's own route only ever
    reaches the registered subset of this key space.

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
    # Nullable since 1.2.2: an owner who cannot name the illuminator, the same
    # case the coordinates above carry.
    tx_callsign: Mapped[str | None] = mapped_column(String(32), nullable=True)
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


class NodeContact(Base):
    """Whom to contact about a node, as its owner reported it.

    Unverified by construction: it arrives over the node's bearer token, so
    whoever holds that token can set it. A support artefact, never an identity;
    when claiming binds a node to an account the account's verified email is the
    source of truth and this is the fallback for unclaimed nodes.

    Mutable, one row per node, unlike NodeConfig above. A frame references a
    configuration version for as long as the archive holds it, so personal data
    there could be neither corrected nor removed.
    """

    __tablename__ = "node_contacts"

    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), primary_key=True)
    first_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # ISO 3166-1 alpha-2, so the phone number above resolves: a national number
    # says nothing about where it is dialable from.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeClaim(Base):
    """Who owns a node, the address it was claimed with, and whether that
    address was ever confirmed.

    Separate from NodeContact above, which is the site contact: freely
    writable, replaced wholesale, granting nothing. This one is an
    authorisation boundary: `user_id` is what gates an owner's data. The address
    cannot be rewritten while the node has an owner, an omission does not clear
    it, and clearing it is release rather than a write from the node.

    Outside `node_contacts` for a second reason: routes/node_register.py calls
    delete_contact, so a re-register wipes that document, and neither the
    binding nor the confirmed address may go with it.

    `verified` is not `user_id IS NOT NULL` under another name. A click sets
    both, but an owner an administrator assigned has `user_id` set and
    `verified` false, and that difference is the whole of what the flag carries.

    Only a registered node can be owned: the foreign key makes an owner for an
    id that never came through v1 registration an integrity error, which the
    administrator's route answers as 404 before it writes.
    """

    __tablename__ = "node_claims"

    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), primary_key=True)
    # Indexed because "which nodes does this account own" is the hot lookup:
    # the owner websocket resolves it per connection and the analytics payload
    # per request.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # A hard bounce from the provider's feed. Not a state: a node whose address
    # bounced is unclaimed, with the field live and a different address accepted
    # normally. The flag only changes what its setup UI says about the address
    # still on file, and refuses a re-send to that one.
    undeliverable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NodeClaimChallenge(Base):
    """The one address a node has awaiting verification.

    `node_id` is the primary key, so "at most one pending challenge per node"
    is the schema's rule rather than a convention its writers keep. Nominating
    a new address replaces this row, and the handle it held is invalidated by
    the caller that displaced it, which is what stops a link mailed to a
    mistyped address working once the address is corrected.

    `handle` is what services/claim_links.py returns and what invalidation is
    called with. The token that goes in the mail is never stored.
    """

    __tablename__ = "node_claim_challenges"

    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    handle: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)


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


class NodeReport(Base):
    """What a node last said about itself on its heartbeat, one row per node.

    Diagnostic and untrusted: nothing decides whether a node is working from
    it, since `blah2: "up"` reads the same on a wedged node as a working one.
    It is here so an operator can read a node's own account without reaching
    the board. `errors` carries node-internal detail, so this is shown to
    administrators only.
    """

    __tablename__ = "node_reports"

    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    boot_id: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    # When `state` last changed, or the node last restarted: how long a node
    # has said `starting` is the signal, not that it says it.
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    uptime_s: Mapped[int] = mapped_column(Integer)
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    versions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The most recent errors across beats, oldest first, each {"at", "message"}.
    # A node clears its list once a beat is acknowledged, so the last beat's
    # list alone would lose an error a minute after it was reported.
    errors: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class NodeEvent(Base):
    """A human decision about a node, oldest first and never rewritten.

    Keyed to `nodes` without a cascade, so the record of who released a
    radar's data outlives its registration. `actor` follows
    NodeLocationPrivacy.set_by: `admin:<email>` for an administrator.
    """

    __tablename__ = "node_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    # The polled radar's epoch the decision applied to.
    epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"NodeEvent(id={self.id!r}, node_id={self.node_id!r}, kind={self.kind!r})"


class PolledRadar(Base):
    """What polling a stock blah2 radar adds to its node.

    The node itself is an ordinary `nodes` row with a configuration version and
    a claim, so the pipeline, ownership and publication code read it unchanged.
    This row holds only how to reach the radar and how the poller finds it.

    The address is the operator's home: `__repr__` names neither it nor the
    credential, and the secret is Fernet ciphertext (core/secrets.py).
    """

    __tablename__ = "polled_radars"

    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("nodes.node_id"), primary_key=True)
    # Moves on an endpoint edit or a config-fingerprint change: whenever what
    # the radar declares may have come from another box. Probation restarts
    # with it. Neither a credential change nor the address moving network does
    # this: an address is the operator's ISP or proxy moving them about, and
    # says nothing about which box answers.
    epoch: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # As typed, with any userinfo removed.
    endpoint_raw: Mapped[str] = mapped_column(String(2048))
    scheme: Mapped[str] = mapped_column(String(8))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    # Normalised `host:port`. Unique, so one radar cannot be registered twice
    # however differently its address was typed.
    endpoint_key: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    auth_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Evidence of where the name pointed, never used to reach the radar.
    last_resolved_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How often the name has moved to another network, and when it last did.
    # Evidence for the trust layer to weigh (123zgec4bxx), not a fence.
    network_moves: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_network_move_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config_fingerprint: Mapped[str] = mapped_column(String(128))
    probe_passed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    endpoint_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trust_state: Mapped[str] = mapped_column(String(16), default="probation", server_default="probation")
    unprotected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # pending, streaming, stalled or unreachable.
    liveness: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the radar last served the configuration its epoch holds.
    last_config_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"PolledRadar(node_id={self.node_id!r}, epoch={self.epoch!r}, liveness={self.liveness!r})"


class PolledRadarEndpointHistory(Base):
    """Each endpoint a polled radar has had, oldest first.

    Deleted with the registration by the database's cascade, which needs
    `PRAGMA foreign_keys=ON` (core/users.py). `changed_by` follows
    NodeLocationPrivacy.set_by: a user id for an owner, `admin:<email>` for an
    administrator.
    """

    __tablename__ = "polled_radar_endpoint_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("polled_radars.node_id", ondelete="CASCADE"), index=True
    )
    old_key: Mapped[str | None] = mapped_column(String(320), nullable=True)
    new_key: Mapped[str] = mapped_column(String(320))
    resolved_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(255))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"PolledRadarEndpointHistory(id={self.id!r}, node_id={self.node_id!r})"
