"""Persisting a node's configuration, one append-only row per version.

Separate from `services/node_config.py`, which is a leaf on purpose: it takes a
dict and returns a dict, knowing nothing of identity or the database, and both
its callers depend on it staying that way. Versioning is the other half of the
job and needs a session, so it lives here.

Nothing commits. `mint_token`, `revoke_tokens` and this all flush and leave the
transaction to the route, so registration can write a node, an agreement set, a
config version, a revocation and a token as one unit.

Registration and PUT /v1/nodes/config both answer from here: a node that resends
an unchanged configuration must be told the version the server already holds
rather than a fresh one, and the two endpoints cannot be allowed to answer that
differently.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import NodeConfig

# The fifteen fields the contract requires: the geometry, the radio and the
# association tolerances.
REQUIRED_FIELDS = (
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "tx_callsign",
    "fc_hz",
    "fs_hz",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
)

# The node's hardware, optional on the wire since contract 1.7.0. Each is None
# unless the node reported it, since absent means unknown.
HARDWARE_FIELDS = (
    "sdr_model",
    "gain_reduction_db",
    "lna_state",
    "antenna_model",
    "antenna_gain_dbi",
    "antenna_polarisation",
    "feedline_loss_db",
    "local_adsb_receiver",
    "notch_filters",
)

# What the tower match derives for a version, by the key its record carries
# each under, then when it was made. Server-derived, so never compared: the node
# does not send them, and a match coming out differently is no new configuration.
TOWER_MATCH_COLUMNS = {
    "callsign": "tower_callsign",
    "eirp_dbm": "tower_eirp_dbm",
    "antenna_height_m": "tower_antenna_height_m",
    "band": "tower_band",
    "polarisation": "tower_polarisation",
    "status": "tower_match_status",
}
TOWER_MATCH_FIELDS = (*TOWER_MATCH_COLUMNS.values(), "tower_matched_at")

# What the tower match is looked up by. A new version that keeps all four keeps
# its predecessor's match.
_TOWER_MATCH_INPUTS = ("tx_callsign", "fc_hz", "tx_lat", "tx_lon")

# What a configuration version consists of: exactly validate_config's output
# keys, the fifteen it has always required and then the hardware. Twenty-four
# fields over twenty-six columns: the model stores gain_reduction_db and
# notch_filters as two columns each and reads and writes each pair through a
# property. Every other column on node_configs is server-derived
# (TOWER_MATCH_FIELDS) or bookkeeping: the surrogate key, the node it belongs
# to, the version number and the two timestamps. test_node_config_store.py pins
# these against validate_config and the columns, so a column added later cannot
# be silently left out of the comparison below.
_CONFIG_FIELDS = (*REQUIRED_FIELDS, *HARDWARE_FIELDS)


async def active_config(session: AsyncSession, node_id: str) -> NodeConfig | None:
    """The node's version not yet superseded, if it has one."""
    rows = await session.execute(
        select(NodeConfig)
        .where(NodeConfig.node_id == node_id, NodeConfig.superseded_at.is_(None))
        .order_by(NodeConfig.version.desc())
    )
    return rows.scalars().first()


def config_fields(row: NodeConfig, fields: Sequence[str] = _CONFIG_FIELDS) -> dict[str, Any]:
    """A version's configuration in validate_config's shape, as upsert_config
    takes it, or just the `fields` of it a caller names."""
    return {field: getattr(row, field) for field in fields}


async def upsert_config(session: AsyncSession, node_id: str, config: dict[str, Any]) -> int:
    """Return the node's active configuration version, minting one if it moved.

    `config` is `validate_config`'s output: exactly the wire fields, normalised,
    every one of them a column on `node_configs` or, for the two pairs, a
    property over two. Passing a raw request body would write whatever keys it
    happened to carry, and a dict missing a field raises KeyError rather than
    reading as unchanged.

    The comparison is field by field in Python rather than in SQL, because
    `NULL = NULL` is never true and most of the fields are nullable:
    `beam_azimuth_deg` on every node that is not aimed, `beam_width_deg` on every
    node in the fleet, since retina-gui does not collect the geometry, and every
    hardware field a node does not report, which before contract 1.7.0 is all of
    them. Comparing in the database would mint a version per resend for the
    whole fleet, tell each node `config_stale` in perpetuity, and have each one
    resend in response. A pair reads as None when both its columns are null and
    as a two-element list otherwise, so it compares equal exactly when both
    columns do.

    A new version carries the active one's tower match, `tower_matched_at`
    included so the weekly refresh keeps its schedule, when the illuminator's
    callsign, frequency and position are unchanged, and starts unmatched
    otherwise.
    """
    active = await active_config(session, node_id)

    if active is not None and all(getattr(active, field) == config[field] for field in _CONFIG_FIELDS):
        return active.version

    tower: dict[str, Any] = {}
    if active is not None and all(getattr(active, field) == config[field] for field in _TOWER_MATCH_INPUTS):
        tower = {field: getattr(active, field) for field in TOWER_MATCH_FIELDS}

    now = datetime.now(UTC)
    # Counted from the highest version the node has ever held rather than from
    # the active one. They are the same today, since nothing else supersedes a
    # row, but anything that ever superseded one without inserting a replacement
    # would restart this at 1 and collide with the unique constraint.
    highest = (
        await session.execute(select(func.max(NodeConfig.version)).where(NodeConfig.node_id == node_id))
    ).scalar()
    version = 1 if highest is None else highest + 1
    if active is not None:
        # Superseded rather than updated: a detection frame arrives stamped with
        # the version it was computed under, so the geometry behind an archived
        # detection has to stay readable.
        active.superseded_at = now
    # The instant that superseded the last one, rather than the column's server
    # default, which keeps whole seconds: an owner's history starts when the
    # node became theirs (services/owner_views.py), and a version from the
    # second they took it has to fall on their side of that.
    session.add(NodeConfig(node_id=node_id, version=version, created_at=now, **config, **tower))
    await session.flush()
    return version


async def set_tower_match(
    session: AsyncSession, node_id: str, version: int, record: Mapping[str, Any], now: datetime
) -> bool:
    """Store a tower match on one version, and say whether what it found moved.

    `record` is the match's record, keyed as TOWER_MATCH_COLUMNS. A record that
    comes out as stored moves only `tower_matched_at`, so a re-confirmed match
    reads as unchanged. Compared in Python, as upsert_config compares, so a null
    that stays null is unchanged. A version the node does not have is False, and
    nothing is written.
    """
    row = await session.scalar(select(NodeConfig).where(NodeConfig.node_id == node_id, NodeConfig.version == version))
    if row is None:
        return False
    changed = any(getattr(row, column) != record[key] for key, column in TOWER_MATCH_COLUMNS.items())
    if changed:
        for key, column in TOWER_MATCH_COLUMNS.items():
            setattr(row, column, record[key])
    row.tower_matched_at = now
    await session.flush()
    return changed
