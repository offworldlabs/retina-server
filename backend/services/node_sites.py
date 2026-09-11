"""Which nodes sit at one receive site, so the fuzz can move them as one.

Two receivers can share an address: one roof, one operator, two illuminators.
Fuzzing them independently publishes two points drawn from the same true
position, and two samples are worth far more to an attacker than one.  Each
published point says "the receiver is somewhere in this annulus", and the
annuli intersect:

===============  ======================================================
co-located       region consistent with EVERY published point, at the
receivers        shipped [0.5, 1.0] km donut
===============  ======================================================
1                2.36 km²  — the whole donut, which is the privacy on offer
2                0.69 km²
3                0.29 km²
===============  ======================================================

Three receivers at one house therefore give up about eight ninths of what the
fuzz buys, and the centroid of the three published points alone lands within
~380 m of the truth.  So co-located nodes share a single offset: they are
published at coincident coordinates, an attacker sees one sample, and the
annulus stays the annulus.

**Two rules decide a site, in this order.**

1. *Exact equality of the configured position.*  A second receiver registered
   with an existing site's coordinates carries the same number, at 6 decimals,
   and the group keys on its lowest node id.  These groups are formed first and
   their anchors are frozen: nothing in the second rule can re-anchor one, so a
   site that is already published at one point keeps that point.

2. *Proximity to an existing anchor, within NODE_FUZZ_SITE_KM.*  Sites are
   entered, not measured, and in practice two receivers on one roof are often
   two independently typed fixes a few tens of metres apart.  The fleet has
   produced exactly that: a fourth receiver 56 m from three at one address,
   and a receiver 16 m from a pair at another, each publishing a second sample
   of a site the first rule had already protected.  So a node that is alone at
   its coordinates joins the nearest existing anchor within the radius, and is
   published *at the anchor's position* plus the anchor's offset — not at its
   own position plus a shared offset, which would publish the true baseline
   between the two receivers as the gap between their markers.

**Why the second rule is safe to apply.**  The objection to proximity grouping
was that a node's offset would come to depend on its neighbours, so a
neighbour connecting or disconnecting could move a node that did not move.
Two properties close that off.  Positions are remembered, never dropped
(``_positions``), so a disconnect changes nothing.  And the join is a
deterministic greedy pass over node ids in sorted order, not a transitive
closure: every member is within the radius of its own anchor, a chain of
near neighbours cannot pull a whole street into one site, and the result does
not depend on connection order.  What remains is the case the exact rule
always had — a newcomer with a lower id at an existing site becomes its
anchor — and that is a one-off re-fuzz of a site that was going to publish a
second sample anyway.

**What a lone node keeps.**  A node with no anchor within the radius keys on
its own id and its own position, exactly as it did before this module existed.
Adopting either rule re-fuzzes nobody except the nodes it is for.

**What sharing gives up.**  Nodes at one site are published at one point, so
the map cannot distinguish them there, and a true separation up to the merge
radius is not represented.  The declared ``location_uncertainty_km`` for a
site with a joined member is widened by the radius so the disc stays honest.
Their separation was never the interesting fact; publishing it independently
was what leaked the site.

**What is still only audited.**  Two nodes closer than twice the radius that
did not merge — because each is nearer another anchor, or because they sit
just outside it — are reported by ``colocation_report()`` and logged, so the
residual case surfaces as a warning an operator can fix by aligning the two
configurations or widening NODE_FUZZ_SITE_KM.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time

from config.constants import node_fuzz_site_km
from core import state
from core.runtime_config import runtime_path
from services.geo import KM_PER_DEG_LAT, haversine_km

logger = logging.getLogger(__name__)

# How long a position snapshot is reused.  Node geometry changes at human speed
# — a registration, or an operator editing a config file — and the alternative
# is a database round trip and two file reads per published coordinate.
_TTL_S = 30.0

# After a failed refresh, retry sooner than the full TTL.  A failure means the
# snapshot is older than it should be, not that it is wrong, so the served
# answer stays the last good one either way.
_ERROR_RETRY_S = 5.0

# Coordinates are compared at 6 decimals, about 0.11 m.  This is canonical
# formatting rather than a tolerance: two configurations of the same site carry
# the same number, and 6 decimals is the precision the wire contract and the
# config files already use.  Only the comparison is rounded — the position a
# joined node is published from is the anchor's value as configured, so it
# goes through the same arithmetic as the anchor's own published point and
# lands on the identical coordinate.
_SITE_DECIMALS = 6

# The runtime files that define nodes this deployment did not register: a
# legacy geometry list and the synthetic fleet's config. Nothing writes
# blah2_nodes.json any more, but a deployment seeded before the poller was
# removed still has one, and its nodes still share a roof.
_NODE_FILES = ("blah2_nodes.json", "nodes_config.json")

_lock = threading.Lock()
# node_id -> (lat, lon) as configured, last known.  Positions are overwritten,
# never dropped: a node that goes offline must not dissolve the site it shares,
# or its site-mate would move on the map every time it disconnected.  Bounded
# by the number of nodes this process has ever seen.
_positions: dict[str, tuple[float, float]] = {}
# node_id -> the id its offset is keyed on.  Rebuilt whenever _positions is.
_identities: dict[str, str] = {}
# node_id -> the anchor's position, for nodes joined by proximity only.  A node
# absent here is published from its own coordinates.
_anchor_positions: dict[str, tuple[float, float]] = {}
# Anchor ids of sites with at least one proximity-joined member.
_snapped_sites: frozenset[str] = frozenset()
_expires_at: float = 0.0


def _reset_for_tests() -> None:
    """Drop the snapshot.  Tests only."""
    global _expires_at, _identities, _anchor_positions, _snapped_sites
    with _lock:
        _positions.clear()
        _identities = {}
        _anchor_positions = {}
        _snapped_sites = frozenset()
        _expires_at = 0.0


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _site_of(lat, lon) -> tuple[float, float] | None:
    """A usable configured position, or None."""
    if not _is_num(lat) or not _is_num(lon):
        return None
    return (float(lat), float(lon))


def _site_key(position: tuple[float, float]) -> tuple[float, float]:
    """The equality key for a position: the same site carries the same number."""
    return (round(position[0], _SITE_DECIMALS), round(position[1], _SITE_DECIMALS))


def _positions_from_live() -> dict[str, tuple[float, float]]:
    with state.connected_nodes_lock:
        snapshot = list(state.connected_nodes.items())
    out = {}
    for node_id, info in snapshot:
        cfg = (info or {}).get("config") or {}
        site = _site_of(cfg.get("rx_lat"), cfg.get("rx_lon"))
        if site is not None:
            out[node_id] = site
    return out


def _positions_from_files() -> dict[str, tuple[float, float]]:
    """Nodes defined by a runtime file rather than by registration.

    These nodes and the synthetic fleet's never reach the database, so a site
    shared between two of them — which is the case this module exists for — is
    invisible without reading the files.
    """
    out = {}
    for name in _NODE_FILES:
        path = runtime_path(name)
        try:
            if not path.exists():
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("node_sites: could not read %s", path)
            continue
        for entry in doc.get("nodes") or []:
            if not isinstance(entry, dict):
                continue
            node_id = entry.get("node_id")
            site = _site_of(entry.get("rx_lat"), entry.get("rx_lon"))
            if node_id and site is not None:
                out[node_id] = site
    return out


def _positions_from_db() -> dict[str, tuple[float, float]]:
    """The current configuration of every registered node.

    Imported inside the function and driven by the same synchronous-engine
    pattern services/publication.py documents: the callers are executor threads
    and route handlers, neither of which can await, and the module must import
    cleanly in a process with no database at all.
    """
    from sqlalchemy import select

    from core.nodes import NodeConfig
    from services.publication import _sync_engine

    with _sync_engine().connect() as conn:
        rows = conn.execute(
            select(NodeConfig.node_id, NodeConfig.rx_lat, NodeConfig.rx_lon).where(NodeConfig.superseded_at.is_(None))
        )
        out = {}
        for node_id, lat, lon in rows:
            site = _site_of(lat, lon)
            if node_id and site is not None:
                out[node_id] = site
        return out


def _km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    return haversine_km(a[0], a[1], b[0], b[1])


def _cluster(
    positions: dict[str, tuple[float, float]], radius_km: float
) -> tuple[dict[str, str], dict[str, tuple[float, float]]]:
    """Resolve every node to its anchor: (identities, anchor position of joined nodes).

    Pure, so the two rules in the module docstring can be read and tested as
    one function.  Pass one places every exact-equality group of two or more
    (equal at ``_SITE_DECIMALS``) as an anchor, lowest id first, and nothing
    later can move them.  Pass two walks the remaining nodes in id order: each
    joins the nearest anchor within ``radius_km`` — an earlier-placed anchor
    wins a tie — or becomes one.  A radius of zero (or less) is the exact rule
    alone.
    """
    by_site: dict[tuple[float, float], list[str]] = {}
    for node_id, position in positions.items():
        by_site.setdefault(_site_key(position), []).append(node_id)

    identities: dict[str, str] = {}
    joined: dict[str, tuple[float, float]] = {}
    # Anchors in placement order; a tie in distance goes to the earlier one.
    anchors: list[tuple[str, tuple[float, float]]] = []

    # An anchor's position is its own configured value, not the rounded key:
    # a joined node is published from it, and it must be the number the
    # anchor's own published point is computed from.
    groups = sorted(((min(ids), ids) for ids in by_site.values() if len(ids) > 1), key=lambda g: g[0])
    for anchor, ids in groups:
        anchors.append((anchor, positions[anchor]))
        for node_id in ids:
            identities[node_id] = anchor

    # A candidate anchor further than this in latitude alone is out of range,
    # which keeps the pass linear-ish for a fleet of hundreds without a spatial
    # index.  The 1.01 covers the haversine/flat-earth difference at this scale.
    lat_gate = (radius_km / KM_PER_DEG_LAT) * 1.01 if radius_km > 0 else -1.0
    singles = sorted(ids[0] for ids in by_site.values() if len(ids) == 1)
    for node_id in singles:
        position = positions[node_id]
        best: tuple[float, str, tuple[float, float]] | None = None
        if radius_km > 0:
            for anchor, anchor_position in anchors:
                if abs(anchor_position[0] - position[0]) > lat_gate:
                    continue
                gap = _km_between(position, anchor_position)
                if gap <= radius_km and (best is None or gap < best[0]):
                    best = (gap, anchor, anchor_position)
        if best is None:
            anchors.append((node_id, position))
            identities[node_id] = node_id
        else:
            identities[node_id] = best[1]
            joined[node_id] = best[2]
    return identities, joined


def _refresh_locked() -> None:
    """Fold every source into _positions and rebuild the identity map.

    Order matters only where sources disagree about the same node, and the
    live configuration wins: it is the one the pipeline is solving against and
    therefore the one whose coordinates are being published.
    """
    global _identities, _anchor_positions, _snapped_sites
    for source in (_positions_from_db, _positions_from_files, _positions_from_live):
        try:
            _positions.update(source())
        except Exception:
            # One unavailable source must not cost the others, and none of them
            # is worth failing a published payload over.  The stale answer is
            # the previous snapshot, which is a position map, not a wrong one.
            logger.exception("node_sites: position source %s failed", source.__name__)

    identities, joined = _cluster(_positions, node_fuzz_site_km())
    # Swapped in whole rather than mutated in place, so a reader between two
    # statements never sees a node keyed on a site it has not finished joining.
    _identities = identities
    _anchor_positions = joined
    _snapped_sites = frozenset(identities[node_id] for node_id in joined)


def _ensure_fresh() -> None:
    global _expires_at
    if time.monotonic() < _expires_at:
        return
    with _lock:
        if time.monotonic() < _expires_at:
            return
        try:
            _refresh_locked()
            _expires_at = time.monotonic() + _TTL_S
        except Exception:
            logger.exception("node_sites: refresh failed, serving the previous snapshot")
            _expires_at = time.monotonic() + _ERROR_RETRY_S


def site_identity(node_id: str) -> str:
    """The id this node's public offset is keyed on.

    Its own, unless it shares a site with another node — configured at the same
    coordinates, or within NODE_FUZZ_SITE_KM of that site's anchor — in which
    case the anchor's id, so every node there is displaced by one offset and
    published at one point.

    A node whose position this deployment does not know keys on itself, which
    is both the old behaviour and the safe one: the unknown case must not
    silently merge a node into somebody else's site.
    """
    if not node_id:
        return node_id
    _ensure_fresh()
    return _identities.get(node_id, node_id)


def site_position(node_id: str | None) -> tuple[float, float] | None:
    """The position this node is published FROM, when it is not its own.

    Only a node joined to a site by proximity answers: its published point is
    its anchor's configured position plus the site's offset, so the marker
    coincides with its site-mates' and the gap between the two receivers is
    not on the wire.  ``None`` for every other node — a lone node and a member
    of an exact-equality site are both published from their own coordinates,
    exactly as before.
    """
    if not node_id:
        return None
    _ensure_fresh()
    return _anchor_positions.get(node_id)


def site_shift_deg(node_id: str | None) -> tuple[float, float]:
    """The (dlat, dlon) from this node's own position to the one it is published from.

    The proximity join's half of the public displacement; the fuzz offset is
    the other half.  Anything derived from the node's true position — a
    coverage polygon, an arc, a trail — has to move by this as well as by the
    offset, or it would land around the node's own position while the marker
    sits at the anchor's.  (0.0, 0.0) for a node published from its own
    coordinates.
    """
    if not node_id:
        return (0.0, 0.0)
    _ensure_fresh()
    anchor = _anchor_positions.get(node_id)
    own = _positions.get(node_id)
    if anchor is None or own is None:
        return (0.0, 0.0)
    return (anchor[0] - own[0], anchor[1] - own[1])


def site_is_snapped(node_id: str | None) -> bool:
    """Whether this node's site has a member published from a position not its own.

    Such a site's true receivers are up to NODE_FUZZ_SITE_KM further from the
    published point than the fuzz alone allows for, so the uncertainty a
    client is told has to grow by that much — for every member, so the site's
    single published point carries a single honest radius.
    """
    if not node_id:
        return False
    _ensure_fresh()
    return _identities.get(node_id, node_id) in _snapped_sites


def shared_sites() -> dict[str, list[str]]:
    """{anchor node id: every node id at that site}, sites of two or more only."""
    _ensure_fresh()
    grouped: dict[str, list[str]] = {}
    for node_id, anchor in _identities.items():
        grouped.setdefault(anchor, []).append(node_id)
    return {anchor: sorted(ids) for anchor, ids in grouped.items() if len(ids) > 1}


def colocation_report() -> dict:
    """Sites being shared, how each member got there, and the pairs still apart.

    ``proximity_joins`` lists every node published from an anchor's position
    rather than its own, with the true gap: that is the set of nodes this
    module has moved, and therefore what to read before changing the radius.

    ``near_misses`` is the residual audit: two nodes at different sites closer
    than twice NODE_FUZZ_SITE_KM.  Each is fuzzed independently, so if they are
    one site described twice they are publishing two samples of it.  Aligning
    the two configurations, or widening the radius, is what fixes it, and this
    is how anyone finds out there is something to fix.
    """
    _ensure_fresh()
    with _lock:
        positions = dict(_positions)
        identities = dict(_identities)
        joined = dict(_anchor_positions)
    shared = shared_sites()

    radius_km = node_fuzz_site_km()
    audit_km = 2.0 * radius_km
    joins = sorted(
        (
            {"node": node_id, "anchor": identities[node_id], "km": round(_km_between(positions[node_id], anchor), 4)}
            for node_id, anchor in joined.items()
        ),
        key=lambda entry: (entry["km"], entry["node"]),
    )

    node_ids = sorted(positions)
    near: list[dict] = []
    for i, first in enumerate(node_ids):
        for second in node_ids[i + 1 :]:
            if identities.get(first, first) == identities.get(second, second):
                continue
            gap_km = _km_between(positions[first], positions[second])
            if gap_km <= audit_km:
                near.append({"nodes": [first, second], "km": round(gap_km, 4)})
    near.sort(key=lambda entry: entry["km"])
    return {
        "shared_sites": shared,
        "proximity_joins": joins,
        "near_misses": near,
        "merge_radius_km": radius_km,
        "audit_threshold_km": audit_km,
    }


_last_logged: tuple | None = None


def log_colocation_audit() -> dict:
    """Log the report when it changes, and return it.

    Only on change: this runs on a periodic task, and a line per cycle would be
    noise that nobody reads and therefore nobody notices a change in.
    """
    global _last_logged
    report = colocation_report()
    fingerprint = (
        tuple(sorted((k, tuple(v)) for k, v in report["shared_sites"].items())),
        tuple((entry["node"], entry["anchor"]) for entry in report["proximity_joins"]),
        tuple(tuple(entry["nodes"]) for entry in report["near_misses"]),
    )
    if fingerprint == _last_logged:
        return report
    _last_logged = fingerprint
    for node_ids in report["shared_sites"].values():
        logger.info("node_sites: %s share one receive site and one fuzz offset", ", ".join(node_ids))
    for entry in report["proximity_joins"]:
        logger.info(
            "node_sites: %s is configured %.0f m from %s and is published from that site's position, "
            "so the pair is one sample, not two (NODE_FUZZ_SITE_KM=%.3f)",
            entry["node"],
            entry["km"] * 1000.0,
            entry["anchor"],
            report["merge_radius_km"],
        )
    for entry in report["near_misses"]:
        logger.warning(
            "node_sites: %s and %s are %.0f m apart at different sites — outside the %.0f m merge radius "
            "but close enough to be one site described twice — so each is fuzzed independently and the "
            "pair publishes two samples. Align their configured rx_lat/rx_lon, or widen NODE_FUZZ_SITE_KM, "
            "to group them.",
            entry["nodes"][0],
            entry["nodes"][1],
            entry["km"] * 1000.0,
            report["merge_radius_km"] * 1000.0,
        )
    return report
