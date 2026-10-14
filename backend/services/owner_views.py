"""What an owner is shown of one of their own nodes, beyond the node itself.

Read models only: every function here assumes the caller has already proved the
node is theirs, and answers for that one node. The routes in routes/owner.py do
the proving and the 404, so nothing here can tell an owned node from anyone
else's and nothing here should try.

What the routes prove includes when the node became the caller's
(core/auth.py, get_user_tenures), and the records views take it as `since`:
what the node recorded before then belongs to whoever held it, so none of it is
shown.

Four views, each the owner-scoped twin of something the server already holds:

* the analytics summary, as GET /api/radar/analytics/{ref} builds it but with
  nothing services/public_location.py would move: the true receiver and
  coverage polygon, the furthest detections the public copy drops, and no
  uncertainty radius, since the owner configured the position it qualifies;
* the node's last heartbeat report, which until now only administrators read;
* its configuration history from node_configs, newest version first;
* the decisions recorded about it in node_events, with who made each one
  reduced to whose side they were on.

Everything returned is plain JSON: no numpy scalars, no datetimes. Timestamps
are UTC strings ending in `Z`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import or_, select

from core import state
from core.nodes import NodeConfig, NodeEvent, NodeReport
from core.users import async_session_maker
from services import node_bias
from services.node_config_store import config_fields
from services.node_refs import owner_identity, public_records, withheld_from_public

# The analytics blocks a summary carries, in the order the public per-node
# route serves them. The manager emits each only once it holds that store for
# the node, so any of them can be missing; here a missing block is a null
# rather than an absent key, so the shape an owner's page reads never changes.
_BLOCKS = ("trust", "detection_area", "metrics", "reputation", "coverage_map", "empirical_coverage")

# How services/polled_registration.py and routes/admin_polled_radars.py record
# an administrator: `admin:<email>`. The email is the reason a stored actor is
# never sent.
_ADMIN_ACTOR_PREFIX = "admin:"


def owner_summary(node_id: str) -> dict | None:
    """The node's analytics with nothing moved, or None if it has none yet."""
    return _summary(node_id, _fleet_ids())


def owner_summaries(node_ids: list[str]) -> dict[str, dict]:
    """owner_summary for each of `node_ids` that has analytics, keyed by node_ref.

    Private nodes are included: they are the caller's own. A node with no
    published handle is left out rather than keyed on its id, the same call
    GET /api/radar/analytics makes for an owner's node: the map is keyed on
    refs, and an id-keyed entry beside them is the mapping D16 withholds. The
    per-node view still answers for it, with a null node_ref.
    """
    fleet = _fleet_ids()
    out: dict[str, dict] = {}
    for node_id in node_ids:
        summary = _summary(node_id, fleet)
        if summary is not None and summary["node_ref"] is not None:
            out[summary["node_ref"]] = summary
    return out


async def latest_report(node_id: str, *, since: datetime) -> dict | None:
    """The node's last heartbeat report, or None if it has sent none since `since`.

    One row per node, overwritten on every beat (services/node_report_store.py),
    so the row is the latest. Only v1 nodes beat; anything else answers None.
    A row received before `since` is a beat from the last owner's time: its
    health and versions describe the node as they ran it, so it reads as no
    beat at all. The errors are kept across beats, and only those reported
    since `since` are given.
    """
    async with async_session_maker() as session:
        row = await session.get(NodeReport, node_id)
    if row is None or row.received_at < since:
        return None
    return {
        "received_at": _utc_z(row.received_at),
        "state": row.state,
        "state_since": _utc_z(row.state_since),
        "uptime_s": row.uptime_s,
        "config_version": row.config_version,
        # Null when the beat carried no block, which the contract reads as an
        # empty record: there is nothing in it either way.
        "health": row.health or {},
        "versions": row.versions or {},
        # Stamped by record_report with isoformat(), so `+00:00` until restated.
        "errors": [
            {"at": _restamped(e.get("at")), "message": e.get("message")}
            for e in row.errors or []
            if _stamped_since(e.get("at"), since)
        ],
    }


async def config_history(node_id: str, limit: int, *, since: datetime) -> list[dict]:
    """The node's configuration versions since `since`, newest first, at most
    `limit` of them.

    A version is the caller's if the node reported it since `since`, or if it
    is the one the node still runs. The version in force when the node changed
    hands is the last owner's settings, their site among them, so it shows
    only while it is still the active one: the node's own page shows it then
    anyway, and so does the board in the new owner's hands. Once the node
    reports a version of its own, the old one is history from before `since`.
    """
    if limit < 1:
        # SQLite reads a negative LIMIT as none at all.
        return []
    async with async_session_maker() as session:
        rows = await session.scalars(
            select(NodeConfig)
            .where(
                NodeConfig.node_id == node_id,
                or_(NodeConfig.superseded_at.is_(None), NodeConfig.created_at >= since),
            )
            .order_by(NodeConfig.version.desc())
            .limit(limit)
        )
        return [
            {
                "version": row.version,
                "created_at": _utc_z(row.created_at),
                "superseded_at": _utc_z(row.superseded_at),
                **config_fields(row),
            }
            for row in rows
        ]


async def owner_events(node_id: str, limit: int, caller_id: str, *, since: datetime) -> list[dict]:
    """The decisions recorded about the node since `since`, newest first, at
    most `limit`.

    Ordered by id rather than by time: a row written before occurred_at was
    stamped to the microsecond holds whole seconds, so two decisions in one
    second would otherwise come back in either order.
    """
    if limit < 1:
        return []
    async with async_session_maker() as session:
        rows = await session.scalars(
            select(NodeEvent)
            .where(NodeEvent.node_id == node_id, NodeEvent.occurred_at >= since)
            .order_by(NodeEvent.id.desc())
            .limit(limit)
        )
        return [
            {
                "kind": row.kind,
                "at": _utc_z(row.occurred_at),
                "actor": _actor_role(row.actor, caller_id),
                "epoch": row.epoch,
            }
            for row in rows
        ]


def _fleet_ids() -> set[str]:
    """Every node the analytics manager holds, which a summary can name.

    The same 60 s-cached map GET /api/radar/analytics/{ref} hands the identity
    scrub, for the same reason: a reputation penalty quotes the neighbour it
    disagreed with, and that neighbour may have no registry row to be found in.
    """
    return set(state.node_analytics.get_all_summaries())


def _summary(node_id: str, fleet: set[str]) -> dict | None:
    summary = state.node_analytics.get_node_summary(node_id)
    # A node the manager has never seen answers with its id and nothing else,
    # which is "no analytics yet", as the public route reads it too.
    if summary.keys() == {"node_id"}:
        return None
    blocks = {key: summary.get(key) for key in _BLOCKS}
    blocks["node_bias"] = node_bias.node_summary(node_id)

    # Nothing moved, but the identities are scrubbed as the public route
    # scrubs them: the owner is entitled to this node's id, not to its
    # neighbours'. This node's own id is taken out of the blocks first, as the
    # public route drops it, and left out of the vocabulary, so a node whose
    # ref has not reached the registry cache yet is not logged as dropped. A
    # private neighbour is not named by its ref either: it is somebody else's
    # to see.
    published = public_records([_without_own_id(blocks, node_id)], fleet - {node_id}, withheld=_OthersWithheld(node_id))
    if not published:
        # The scrub drops a record that names an unpublishable node as a whole
        # value. No block does today; if one ever does, this is the public
        # route's 404 for the same reason.
        return None
    # A plain-JSON copy. The blocks hold whatever numbers their recorders were
    # handed, which is why every other payload built from these stores is
    # serialised with OPT_SERIALIZE_NUMPY (routes/analytics.py,
    # services/tasks/analytics_refresh.py); converting once here means the
    # caller gets plain JSON either way.
    plain = orjson.loads(orjson.dumps(published[0], option=orjson.OPT_SERIALIZE_NUMPY))
    return {"node_id": node_id, "node_ref": owner_identity(node_id), **plain}


class _OthersWithheld:
    """Every node a public payload withholds, bar the one being summarised."""

    def __init__(self, node_id: str) -> None:
        self._node_id = node_id
        self._withheld = withheld_from_public()

    def __contains__(self, other: object) -> bool:
        return isinstance(other, str) and other != self._node_id and other in self._withheld


def _without_own_id(value: Any, node_id: str) -> Any:
    """`value` without the `node_id` fields naming this node, however deep.

    Every block names the node it belongs to; the public route drops those
    fields and the summary already carries the id once at the top.
    """
    if isinstance(value, dict):
        return {k: _without_own_id(v, node_id) for k, v in value.items() if not (k == "node_id" and v == node_id)}
    if isinstance(value, list | tuple):
        return [_without_own_id(v, node_id) for v in value]
    return value


def _actor_role(actor: str | None, caller_id: str) -> str:
    """Whose side a recorded decision came from, as the caller should read it.

    The writers store three shapes (services/graduation.py,
    services/polled_registration.py, services/polled_radars.py and the owner
    edits): `admin:<email>` for an administrator, a user's id for an owner
    acting on their own node, and whatever a background job chooses. A user's
    id is the caller's own or somebody else's, and somebody else's can only be
    an owner from before the node changed hands. Anything that is not one of
    those is the system's.
    """
    if not actor:
        return "system"
    if caller_id and actor == caller_id:
        return "you"
    if actor.startswith(_ADMIN_ACTOR_PREFIX):
        return "admin"
    if _is_user_id(actor):
        return "owner"
    return "system"


def _is_user_id(value: str) -> bool:
    # users.id is a UUID, stored and compared in its canonical spelling
    # (core/users.py user_to_dict, _access_user_dict).
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _utc_z(when: datetime | None) -> str | None:
    """UTC with a `Z`, as routes/node_schemas.py renders a server time."""
    if when is None:
        return None
    # UTCDateTime reads every column back aware; a naive value is UTC, as
    # core/timestamps.py takes it to be.
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamped_since(at: Any, since: datetime) -> bool:
    """Whether a stored error stamp is at or after `since`. One that does not
    read as a time cannot be shown to be, and is left out."""
    if not isinstance(at, str):
        return False
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when >= since


def _restamped(at: Any) -> Any:
    """A stored ISO stamp in the `Z` form, or the value as stored if it is not one."""
    if not isinstance(at, str):
        return at
    try:
        return _utc_z(datetime.fromisoformat(at))
    except ValueError:
        return at
