"""What readers are shown of the node performance files.

Each view is a new dict built from an allow-list, so a key nobody pinned reaches no
reader.  The keyed route adds its block after the scrub that cleans the rest of its
body, so these lists are all that stands between a file and a key holder.  The raw
``node_id`` leaves on the admin row alone, ``pd_by_bearing`` on the owner's block and
the administrator's alone.

Asks ``services.publication``, which reads the app's database, and reads the app's
analytics snapshot, so the job's child never imports this module.
"""

import logging
from collections.abc import Collection
from datetime import UTC, datetime

import orjson

from config.constants import is_num
from core import state
from services import node_refs, performance_store, publication

_SHARED_KEYS = (
    "schema",
    "window",
    "opportunities",
    "detections",
    "detections_claims",
    "detections_tags",
    "denominator",
    "numerator",
    "system_constant_db",
    "system_constant_sigma_db",
    "system_constant_bound",
    "pd_ceiling",
    "threshold_db",
    "pd_by_class_range_alt",
    "evidence_by_class_range_alt",
    "tag_delay_residual_mad_us",
    "delay_residual_source",
    "range_50pct_km",
    "range_50pct_bound",
    "classes_with_evidence",
    "tower",
    "rank_status",
)
_OWNER_KEYS = (*_SHARED_KEYS, "pd_by_bearing")
_FLEET_KEYS = (
    "rank_status",
    "system_constant_db",
    "system_constant_sigma_db",
    "system_constant_bound",
    "pd_ceiling",
    "threshold_db",
    "tag_delay_residual_mad_us",
    "delay_residual_source",
    "opportunities",
    "detections",
    "window",
    "tower",
)
_TOWER_KEYS = ("callsign", "eirp_dbm", "status")


def snapshot_rows() -> list[tuple[str, str, dict]]:
    """The rows the analytics snapshot puts on the public board, as (node_ref, node_id, summary).

    The snapshot is built for publication, so it is already keyed on node_ref, which
    is what the board reports.  The node_id rides alongside because everything else
    read for a row is node_id-keyed; a ref nothing answers to stands in for it.  Empty
    until the first refresh.
    """
    raw = state.latest_analytics_bytes
    if not raw or raw == b"{}":
        return []
    try:
        summaries = orjson.loads(raw).get("nodes", {})
    except Exception:
        logging.debug("analytics snapshot bytes unparseable", exc_info=True)
        return []
    return [(ref, node_refs.id_for_identity(ref) or ref, s) for ref, s in summaries.items()]


def ranks(on_board: Collection[str] | None = None) -> dict[str, int]:
    """Each node's place on the public board, the one source of ``rank``.

    Empty until a file that can take a place reads claims, because a tag rate isn't a
    score: a low-tag node would rank on how seldom it tags.  Until then a rankable file
    reads a null ``rank`` beside a null ``rank_status`` everywhere but on the board,
    whose rows keep their places among the detections.

    From then on a place takes a public file with a null ``rank_status`` and a numeric
    ``system_constant_db``, whichever numerator it reads; neither a null sigma nor an
    "at least" bound costs one.  Places run from 1 by ``system_constant_db`` descending,
    then node id.  A private node and a file with no board row are left out before
    they're counted, so they stay contiguous and their claims open nothing.

    ``on_board`` is the ids the board lists, which is the snapshot's rows unless the
    caller holds them.  A board listing nobody leaves no file out: before the first
    refresh only the board's route has rows, as reading them here would hold the
    analytics lock in a handler, so every other reader counts each file until then.
    """
    if not performance_store.has_claims_window():
        return {}
    if on_board is None:
        on_board = {node_id for _, node_id, _ in snapshot_rows()}
    placed = {
        node_id: doc
        for node_id, doc in performance_store.all().items()
        if doc["rank_status"] is None
        and is_num(doc.get("system_constant_db"))
        and not publication.is_private(node_id)
        and (not on_board or node_id in on_board)
    }
    if not any(doc.get("numerator") == "claims" for doc in placed.values()):
        return {}
    ranked = sorted((-doc["system_constant_db"], node_id) for node_id, doc in placed.items())
    return {node_id: place for place, (_, node_id) in enumerate(ranked, start=1)}


def _projected(doc: dict, keys: tuple[str, ...]) -> dict:
    """``keys`` of a file in a new dict, null where the file has none.

    ``tower`` is cut to its own pinned keys: it is the one object here whose keys are
    fixed, and a tower record is where a position would ride in.
    """
    tower = doc.get("tower")
    return {
        **{key: doc.get(key) for key in keys},
        "tower": {key: tower.get(key) for key in _TOWER_KEYS} if isinstance(tower, dict) else None,
    }


def keyed(node_id: str) -> dict | None:
    """The block a key holder reads, or None with no file.  The route has already refused a private node."""
    doc = performance_store.get(node_id)
    return None if doc is None else {**_projected(doc, _SHARED_KEYS), "rank": ranks().get(node_id)}


def _inside_tenure(doc: dict, since: datetime) -> bool:
    """Whether a file's first day starts at or after ``since``.  A window that can't be read proves nothing."""
    try:
        return datetime.strptime(doc["window"][0], "%Y-%m-%d").replace(tzinfo=UTC) >= since
    except ValueError:
        return False


def owner(node_id: str, since: datetime) -> dict | None:
    """The owner's block, from the public file or else the owner file, whichever holds nothing from before ``since``.

    None when neither does: a handover since the last run reads as no figures yet,
    never as the last owner's.  ``rank`` is still the public board's place.
    """
    for doc in (performance_store.get(node_id), performance_store.get_owner(node_id)):
        if doc is not None and _inside_tenure(doc, since):
            return {**_projected(doc, _OWNER_KEYS), "rank": ranks().get(node_id)}
    return None


def fleet_node(node_id: str) -> dict | None:
    """The block an administrator opens from a table row, or None with no file.

    The owner's keys over the public file, so the whole window whoever holds the node.
    A private node reads the same, with a null ``rank``.
    """
    doc = performance_store.get(node_id)
    return None if doc is None else {**_projected(doc, _OWNER_KEYS), "rank": ranks().get(node_id)}


def _fleet_order(item: tuple[str, dict]) -> tuple:
    node_id, doc = item
    status = doc["rank_status"]
    group = 0 if status is None else 2 if status == "config_suspect" else 1
    figure = doc.get("system_constant_db" if status is None else "detections")
    return group, -figure if is_num(figure) else float("inf"), node_id


def fleet() -> dict:
    """The admin table: a row per public file.

    Rankable files come first by ``system_constant_db`` descending, a private node
    or one with no board row among them with a null ``rank``, as all of them have while
    ``ranks`` is empty.  The rest follow by ``detections`` descending, with
    ``config_suspect`` last.
    """
    places = ranks()
    docs = sorted(performance_store.all().items(), key=_fleet_order)
    return {
        "nodes": [
            {"node_id": node_id, "rank": places.get(node_id), **_projected(doc, _FLEET_KEYS)} for node_id, doc in docs
        ]
    }
