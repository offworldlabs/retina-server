"""What an owner is shown of the nodes they own, and the two things they may change.

Reached only through routes/owner.py, behind its ownership check, so nothing
here is a public surface: the receiver is where it is, a private node is listed
like any other, and no fuzz is applied. services/public_location.py is what
moves a receiver for everyone else.

A node's configuration is its live connected-node entry, what the pipeline runs
on, laid over its active node_configs version. The stored version is what keeps
a node that is not connected, after a restart say, or a retired one, showing
its configuration, and it fills what the live entry does not carry: a v1 node is
primed with its geometry and radio alone (services/node_pipeline.py,
_WIRE_FIELDS), so its callsign, CPI and tolerances are only ever stored.

An owner may name the node for themselves (NodeLabel, core/nodes.py) and choose
whether it is published (Node.publication). Each change is a NodeEvent under the
owner's own id, and a request that changes nothing records nothing.
"""

import math
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
from sqlalchemy import and_, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from core import state
from core.node_ids import POLLED_BLAH2, system_of
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, NodeLabel
from core.timestamps import UTCDateTime
from core.users import async_session_maker
from services import blah2_poller, publication
from services.node_claim_store import claim_addresses
from services.node_config import canonical_config, carrier_hz, position_status
from services.node_config_store import config_fields
from services.node_refs import owner_identity, public_name
from services.polled_radars import owner_views

#: The longest label, in characters, once trimmed.
LABEL_MAX_CHARS = 64

# Controls, and what breaks a one-line label as a control would. A lone
# surrogate is here too: it decodes from JSON and cannot be stored.
_UNPRINTABLE = frozenset({"Cc", "Cs", "Zl", "Zp"})

# The `location` block of GET /api/radar/nodes, less its uncertainty radius:
# nothing here is displaced, so there is no radius to declare.
_LOCATION_FIELDS = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft")

_RF_NUMBERS = (
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
)

# fs_hz is the v1 wire's and a polled radar's, Fs the TCP fleet's.
_SAMPLE_RATE_KEYS = ("fs_hz", "Fs")


class LabelRefused(ValueError):
    """A name an owner may not give their node, carrying the reason to show them."""


async def label_from(name: str | None) -> str | None:
    """The label `name` asks for, trimmed, or None for a name that clears it.

    Raises LabelRefused for one that is too long, carries a control character,
    or spells a node id.
    """
    if name is None:
        return None
    label = name.strip()
    if not label:
        return None
    if len(label) > LABEL_MAX_CHARS:
        raise LabelRefused(f"A name is at most {LABEL_MAX_CHARS} characters.")
    if any(unicodedata.category(c) in _UNPRINTABLE for c in label):
        raise LabelRefused("A name cannot contain control characters.")
    # A label is never published, but it is shown, and a node id on a screen is
    # one more place to copy it from.
    if await _spells_a_node_id(label):
        raise LabelRefused("A name cannot be a node id.")
    return label


async def _spells_a_node_id(label: str) -> bool:
    """Whether `label` reads as the id of a node this server knows.

    Wider than the rule public_name holds a node-supplied name to. A synthetic
    node counts: it publishes under its own id, but a label that is another
    node's id misnames the node it is on. Case and compatibility forms are
    folded, since an id spelt in capitals or full-width letters is still that
    id to a reader. The table is read rather than the registry cache, so a node
    registered seconds ago counts, and the connected fleet is read too, for the
    node that is connected and carries no row.
    """
    folded = unicodedata.normalize("NFKC", label).casefold()
    with state.connected_nodes_lock:
        fleet = list(state.connected_nodes)
    if any(node_id.casefold() == folded for node_id in fleet):
        return True
    # Node ids are ASCII, which is all SQLite's lower() folds.
    async with async_session_maker() as session:
        return bool(await session.scalar(select(exists().where(func.lower(Node.node_id) == folded))))


async def current_labels(session: AsyncSession, node_ids: Iterable[str]) -> dict[str, str]:
    """The label each of these nodes carries for its owner now, keyed by node.

    A label set by anyone else, an owner the node has since left, is absent:
    the join on node_claims is what makes a change of hands clear it.
    """
    ids = list(node_ids)
    if not ids:
        return {}
    rows = await session.execute(
        select(NodeLabel.node_id, NodeLabel.label)
        .join(NodeClaim, and_(NodeClaim.node_id == NodeLabel.node_id, NodeClaim.user_id == NodeLabel.user_id))
        .where(NodeLabel.node_id.in_(ids))
    )
    return dict(rows.tuples().all())


async def _stored_configs(session: AsyncSession, node_ids: list[str]) -> dict[str, dict]:
    """Each node's active configuration version, in the shape priming gives it."""
    if not node_ids:
        return {}
    rows = await session.scalars(
        select(NodeConfig)
        .where(NodeConfig.node_id.in_(node_ids), NodeConfig.superseded_at.is_(None))
        .order_by(NodeConfig.version)
    )
    # Ascending, so the newest wins should a node ever hold two, as
    # node_config_store.active_config would choose.
    return {row.node_id: canonical_config(config_fields(row)) for row in rows}


def _number(value: Any) -> int | float | None:
    """A configuration value as a JSON number, or None when it is not one.

    A config that arrived over TCP keeps every key but its coordinates as the
    node sent it, so a numeric string or a numpy scalar can stand where a
    number belongs. The framework's encoder refuses a numpy integer, and a
    non-finite value is unknown rather than a 500.
    """
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _first(config: dict, keys: tuple[str, ...]) -> Any:
    """The first of `keys` the config sets, keyed on None as carrier_hz is."""
    for key in keys:
        if config.get(key) is not None:
            return config[key]
    return None


async def views(node_ids: list[str], owned: list[str]) -> list[dict]:
    """What the owner of `node_ids` is shown of each, in order.

    `owned` is every node the caller owns: a node-supplied name that spells
    one of them is shown as the ref instead, as the listing always has.

    Authenticated and scoped to the owner, so the node_id is theirs to see and
    stays. The ref rides along because every public surface is keyed on it, and
    a consumer merging this list with one of those needs one key space rather
    than the same node twice under two. It is null for a node with no published
    handle, which is a node that appears on no public surface either.

    `polled` is null but for a polled radar, whose address it carries: the
    owner's to see, as claimed_with is.
    """
    with state.connected_nodes_lock:
        live = {nid: dict(state.connected_nodes.get(nid, {})) for nid in node_ids}
        live_configs = {nid: dict(info.get("config") or {}) for nid, info in live.items()}
    private = await publication.registered_private(node_ids)
    # One query for the list rather than a lookup per node. The address is what
    # the owner was mailed to claim the node with, so it is theirs to see; it is
    # not published anywhere else.
    claimed_with = await claim_addresses(node_ids)
    async with async_session_maker() as session:
        polled = await owner_views(session, [nid for nid in node_ids if system_of(nid) == POLLED_BLAH2])
        stored = await _stored_configs(session, node_ids)
        labels = await current_labels(session, node_ids)
    out = []
    for nid in node_ids:
        info = live[nid]
        cfg = {**stored.get(nid, {}), **live_configs[nid]}
        ref = owner_identity(nid)
        radar = polled.get(nid)
        label = labels.get(nid)
        status = info.get("status", "never_connected")
        # The poller knows within a few polls, where the offline sweep waits out
        # its threshold. Here rather than in connected_nodes, since the sweep
        # logs a node going offline only when it is the one to mark it so.
        # Liveness never says online on its own: it is the last the poller
        # wrote, and stands still where nothing polls.
        if radar is not None and radar["liveness"] == blah2_poller.UNREACHABLE:
            status = "disconnected"
        callsign = cfg.get("tx_callsign")
        out.append(
            {
                "node_id": nid,
                "node_ref": ref,
                # The owner's own label first. Without one the console names a
                # node by its ref, so an unnamed node, or one whose name is a
                # node id, is called by that instead.
                "name": label or public_name(cfg.get("name"), ref, owned),
                "label": label,
                "status": status,
                "last_heartbeat": info.get("last_heartbeat"),
                # Here as well as on /api/radar/nodes, which leaves private nodes out.
                "config_hash": info.get("config_hash"),
                "is_synthetic": info.get("is_synthetic", False),
                "rx_lat": _number(cfg.get("rx_lat")),
                "rx_lon": _number(cfg.get("rx_lon")),
                "position_status": position_status(cfg),
                "frequency": _number(carrier_hz(cfg)),
                "sample_rate": _number(_first(cfg, _SAMPLE_RATE_KEYS)),
                "location": {field: _number(cfg.get(field)) for field in _LOCATION_FIELDS},
                "rf": {
                    "tx_callsign": callsign if isinstance(callsign, str) and callsign else None,
                    **{field: _number(cfg.get(field)) for field in _RF_NUMBERS},
                },
                "location_private": nid in private,
                "claimed_with": claimed_with.get(nid),
                "polled": radar,
            }
        )
    return out


async def edit(
    node_id: str,
    user_id: str,
    *,
    rename: bool = False,
    label: str | None = None,
    private: bool | None = None,
) -> None:
    """Apply an owner's changes to their node in one transaction, recording each
    that changed something.

    `rename` sets the label to `label`, None clearing it; `private` chooses the
    publication, None leaving it as it is. Every write carries the ownership
    check in its own condition, as services/graduation.py carries the epoch, so
    a node that changed hands after the route checked is left as its new owner
    has it, with nothing recorded.
    """
    now = datetime.now(UTC)
    holds = exists().where(NodeClaim.node_id == node_id, NodeClaim.user_id == user_id)
    async with async_session_maker() as session:
        async with session.begin():
            renamed = rename and await _write_label(session, node_id, user_id, label, holds, now)
            republished = private is not None and await _write_publication(session, node_id, private, holds, now)
            if renamed:
                session.add(NodeEvent(node_id=node_id, kind="renamed", actor=user_id))
            if republished:
                kind = "made_private" if private else "made_public"
                session.add(NodeEvent(node_id=node_id, kind=kind, actor=user_id))
    if republished:
        # After the commit, as registration does, so a reader in between cannot
        # cache the choice this one replaced.
        publication.invalidate()


async def _write_label(
    session: AsyncSession,
    node_id: str,
    user_id: str,
    label: str | None,
    holds: ColumnElement[bool],
    now: datetime,
) -> bool:
    """Set or clear the caller's label, and say whether what they see changed.

    A row an earlier owner left is theirs and already unseen, so clearing
    leaves it, and setting overwrites it.
    """
    if label is None:
        cleared = await session.execute(
            delete(NodeLabel).where(NodeLabel.node_id == node_id, NodeLabel.user_id == user_id, holds)
        )
        return _changed(cleared)
    # One statement, so the ownership check and the write cannot be split by
    # another writer. The update is conditional too: the label they already
    # have is no change.
    written = await session.execute(
        sqlite_insert(NodeLabel)
        .from_select(
            ["node_id", "user_id", "label", "updated_at"],
            select(literal(node_id), literal(user_id), literal(label), literal(now, UTCDateTime())).where(holds),
        )
        .on_conflict_do_update(
            index_elements=[NodeLabel.node_id],
            set_={"user_id": user_id, "label": label, "updated_at": now},
            where=or_(NodeLabel.user_id != user_id, NodeLabel.label != label),
        )
    )
    return _changed(written)


async def _write_publication(
    session: AsyncSession, node_id: str, private: bool, holds: ColumnElement[bool], now: datetime
) -> bool:
    """Choose whether the node is published, and say whether that changed it.

    Recorded as a console choice is at registration (services/polled_radars.py,
    create_polled_radar): the choice and when it was made, and no version, since
    no versioned text was put to the owner here. It stands until the node
    registers again, whose own record replaces it as it replaces any choice
    (routes/node_register.py, _apply_agreements).
    """
    choice = "private" if private else "public"
    changed = await session.execute(
        update(Node)
        .where(Node.node_id == node_id, Node.publication != choice, holds)
        .values(publication=choice, publication_version=None, publication_chosen_at=now)
    )
    return _changed(changed)


def _changed(result: Result[Any]) -> bool:
    """Whether a single-row write wrote its row."""
    return cast(CursorResult, result).rowcount == 1
