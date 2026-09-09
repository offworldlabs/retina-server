"""Which nodes sit at one receive site, so the fuzz can move them as one.

Two receivers can share an address: one roof, one operator, two illuminators —
``radar3-retnode`` and ``radar3a-retnode`` are exactly that, configured at the
same coordinates on purpose.  Fuzzing them independently publishes two points
drawn from the same true position, and two samples are worth far more to an
attacker than one.  Each published point says "the receiver is somewhere in
this annulus", and the annuli intersect:

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

**The rule is exact equality of the configured position.**  Sites are entered,
not measured — a second receiver at an existing site is registered with that
site's coordinates — so equality is what a shared site actually looks like in
the data, and it makes a node's identity a function of its own configuration
alone.  Nothing about which other nodes are online, or in what order they
connected, can move a node that is alone at its position: it keys on its own
node id exactly as it always has, so adopting this file re-fuzzes nobody except
the co-located nodes it is for.

**Near-but-not-equal is audited, not merged.**  Two receivers 30 m apart with
independently typed coordinates are the same site in every sense that matters
and this will not group them.  Grouping by proximity instead would make a
node's offset depend on its neighbours, and then a neighbour connecting,
disconnecting or being retired would move a node that did not move — trading a
leak we can see for one we cannot.  ``colocation_report()`` names those pairs
instead, so the case surfaces as a warning an operator can fix by aligning the
two configurations, rather than as silence.

**What sharing gives up.**  Nodes at one site are published at one point, so
the map cannot distinguish them there, and any true separation smaller than the
audit threshold is not represented.  That is the intended trade: their
separation was never the interesting fact, and publishing it independently was
what leaked the site.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time

from config.constants import node_fuzz_site_audit_km
from core import state
from core.runtime_config import runtime_path

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
# config files already use.
_SITE_DECIMALS = 6

# The runtime files that define nodes this deployment did not register: the
# blah2 bridge's node list and the synthetic fleet's config.
_NODE_FILES = ("blah2_nodes.json", "nodes_config.json")

_lock = threading.Lock()
# node_id -> (lat, lon), last known.  Positions are overwritten, never dropped:
# a node that goes offline must not dissolve the site it shares, or its
# site-mate would move on the map every time it disconnected.  Bounded by the
# number of nodes this process has ever seen.
_positions: dict[str, tuple[float, float]] = {}
# node_id -> the id its offset is keyed on.  Rebuilt whenever _positions is.
_identities: dict[str, str] = {}
_expires_at: float = 0.0


def _reset_for_tests() -> None:
    """Drop the snapshot.  Tests only."""
    global _expires_at
    with _lock:
        _positions.clear()
        _identities.clear()
        _expires_at = 0.0


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _site_of(lat, lon) -> tuple[float, float] | None:
    if not _is_num(lat) or not _is_num(lon):
        return None
    return (round(float(lat), _SITE_DECIMALS), round(float(lon), _SITE_DECIMALS))


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

    The blah2 bridge's nodes and the synthetic fleet's live here and never
    reach the database, so a site shared between two of them — which is the
    case this module exists for — is invisible without reading the files.
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


def _refresh_locked() -> None:
    """Fold every source into _positions and rebuild the identity map.

    Order matters only where sources disagree about the same node, and the
    live configuration wins: it is the one the pipeline is solving against and
    therefore the one whose coordinates are being published.
    """
    for source in (_positions_from_db, _positions_from_files, _positions_from_live):
        try:
            _positions.update(source())
        except Exception:
            # One unavailable source must not cost the others, and none of them
            # is worth failing a published payload over.  The stale answer is
            # the previous snapshot, which is a position map, not a wrong one.
            logger.exception("node_sites: position source %s failed", source.__name__)

    sites: dict[tuple[float, float], list[str]] = {}
    for node_id, site in _positions.items():
        sites.setdefault(site, []).append(node_id)

    _identities.clear()
    for node_ids in sites.values():
        # The lowest node id at the site, which for a site of one is the node
        # itself — so a node alone at its coordinates keys exactly as it did
        # before this module existed.
        anchor = min(node_ids)
        for node_id in node_ids:
            _identities[node_id] = anchor


def _snapshot() -> dict[str, str]:
    global _expires_at
    now = time.monotonic()
    if now < _expires_at:
        return _identities
    with _lock:
        if time.monotonic() < _expires_at:
            return _identities
        try:
            _refresh_locked()
            _expires_at = time.monotonic() + _TTL_S
        except Exception:
            logger.exception("node_sites: refresh failed, serving the previous snapshot")
            _expires_at = time.monotonic() + _ERROR_RETRY_S
    return _identities


def site_identity(node_id: str) -> str:
    """The id this node's public offset is keyed on.

    Its own, unless it shares its configured coordinates with another node, in
    which case the lowest id at that site — so every node there is displaced by
    one offset and published at one point.

    A node whose position this deployment does not know keys on itself, which
    is both the old behaviour and the safe one: the unknown case must not
    silently merge a node into somebody else's site.
    """
    if not node_id:
        return node_id
    return _snapshot().get(node_id, node_id)


def shared_sites() -> dict[str, list[str]]:
    """{anchor node id: every node id at that site}, sites of two or more only."""
    identities = _snapshot()
    grouped: dict[str, list[str]] = {}
    for node_id, anchor in identities.items():
        grouped.setdefault(anchor, []).append(node_id)
    return {anchor: sorted(ids) for anchor, ids in grouped.items() if len(ids) > 1}


def colocation_report() -> dict:
    """Sites being shared, and pairs that look like a site but are not grouped.

    ``near_misses`` is the operational half: two nodes closer than
    NODE_FUZZ_SITE_AUDIT_KM whose configured coordinates are not equal are
    almost certainly one site described twice, and they are being published as
    two independent samples of it.  Aligning the two configurations to the same
    coordinates is what fixes it, and this is how anyone finds out there is
    something to fix.
    """
    _snapshot()
    with _lock:
        positions = dict(_positions)
    shared = shared_sites()

    threshold_km = node_fuzz_site_audit_km()
    node_ids = sorted(positions)
    near: list[dict] = []
    for i, first in enumerate(node_ids):
        for second in node_ids[i + 1 :]:
            a, b = positions[first], positions[second]
            if a == b:
                continue
            gap_km = _km_between(a, b)
            if gap_km <= threshold_km:
                near.append({"nodes": [first, second], "km": round(gap_km, 4)})
    near.sort(key=lambda entry: entry["km"])
    return {"shared_sites": shared, "near_misses": near, "audit_threshold_km": threshold_km}


def _km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    from services.geo import haversine_km

    return haversine_km(a[0], a[1], b[0], b[1])


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
        tuple(tuple(entry["nodes"]) for entry in report["near_misses"]),
    )
    if fingerprint == _last_logged:
        return report
    _last_logged = fingerprint
    for node_ids in report["shared_sites"].values():
        logger.info("node_sites: %s share one receive site and one fuzz offset", ", ".join(node_ids))
    for entry in report["near_misses"]:
        logger.warning(
            "node_sites: %s and %s are %.0f m apart but configured at different coordinates, so each "
            "is fuzzed independently and the pair publishes two samples of one site. Align their "
            "configured rx_lat/rx_lon to group them.",
            entry["nodes"][0],
            entry["nodes"][1],
            entry["km"] * 1000.0,
        )
    return report
