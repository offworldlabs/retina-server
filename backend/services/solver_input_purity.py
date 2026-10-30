"""One node kind per solve: real hardware never co-solves with the synthetic fleet.

On the test droplet the synthetic fleet (FLEET_METRO=gvl, 50 nodes) sits in
the same metro as the real Greenville nodes and seeds its world from live
ADS-B, so synthetic and real nodes report the same aircraft and association
clusters them together.  26 hours of solve records near Greenville showed
real-node-only solves topping out at 4 nodes while mixed solves reached 15,
and the real-only map (which passed any solve with one real contributor)
presented them as 6-node real evidence.

Every path that shapes a solver input runs through here before the solver
sees it: the bottom-up pairs, the anchored (claim-lane) and ADS-B-seeded
inputs in frame_processor, and the known lane's per-hex claims.  An input
whose nodes are all one kind passes through untouched — the same object.  A
mixed one keeps exactly ONE kind: real when it holds two or more real nodes,
else synthetic, and the rest is dropped from every per-node field (the
measurements, the constant-velocity epochs, the track provenance, the pool's
spare measurements).

One kind, not both.  Two solves of the same aircraft would share a key — the
known lane keys mn-adsb-<hex>, and the dark lane's proximity match adopts the
first solve's key — and alternate on it, so a contributing-node set that flips
every solve would blink the entry on and off the real-only feeds.  Real wins
because real-node evidence is what the real-only map exists to show; the
synthetic fleet still solves the aircraft on its own from any other candidate.

Node kind is the id prefix (tcp_handler.is_synthetic_node), the same source
state.connected_nodes' ``is_synthetic`` flag is derived from.  Production
registers no synthetic nodes, so every input there is single-kind and this
module changes nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from core import state
from services.tcp_handler import is_synthetic_node

# A solve needs this many nodes; a kind that cannot reach it is not worth
# keeping on its own.
_MIN_NODES = 2


def single_kind_survivors(node_ids: Iterable[str]) -> set[str]:
    """The node ids to keep so that one kind remains.

    Returns the whole set (equal to the input) when it is already one kind,
    the real subset when at least _MIN_NODES real nodes are in it, else the
    synthetic subset.  The caller tells "unchanged" by comparing sizes.
    """
    ids = set(node_ids)
    real = {nid for nid in ids if not is_synthetic_node(nid)}
    if not real or real == ids:
        return ids
    return real if len(real) >= _MIN_NODES else ids - real


def restrict_solver_input(s_in: dict) -> dict | None:
    """``s_in`` narrowed to one node kind, or None when that leaves fewer
    than two nodes.  Single-kind inputs come back as the same object.

    The rebuild mirrors solver._filter_s_in_to_nodes (measurements, n_nodes,
    track_ids_by_node/track_ids) and extends it to the fields that function
    never has to touch because it runs after the first solve: cv_epochs and
    track_pair_ids, which the solver reads before solving, and the pool's
    spare measurements, which _adopt_pool_nodes may widen a dark solve with
    afterwards.
    """
    measurements = s_in.get("measurements") or []
    ids = {m.get("node_id") for m in measurements}
    ids.discard(None)
    keep = single_kind_survivors(ids)
    if keep == ids:
        return s_in
    state.bump_counter("solver_input_kind_split")
    if len(keep) < _MIN_NODES:
        return None
    keep_synthetic = all(is_synthetic_node(nid) for nid in keep)

    s_next = dict(s_in)
    s_next["measurements"] = [m for m in measurements if m.get("node_id") in keep]
    s_next["n_nodes"] = len(keep)

    by_node = s_in.get("track_ids_by_node")
    surviving_tids: set[str] | None = None
    if by_node:
        surviving_by_node = {nid: ids for nid, ids in by_node.items() if nid in keep}
        s_next["track_ids_by_node"] = surviving_by_node
        surviving_tids = {tid for ids in surviving_by_node.values() for tid in ids}
        s_next["track_ids"] = sorted(surviving_tids)
    if s_in.get("track_pair_ids") and surviving_tids is not None:
        # The track-claim arbitration (solver._claim_tracks) claims every
        # track in these pairs; a pair naming a dropped node's track would
        # claim a track this solve no longer uses.
        s_next["track_pair_ids"] = [
            pair for pair in s_in["track_pair_ids"] if all(tid in surviving_tids for tid in pair)
        ]

    epochs = s_in.get("cv_epochs")
    if epochs:
        kept_epochs = []
        for epoch in epochs:
            if not isinstance(epoch, dict):
                continue
            ms = [m for m in (epoch.get("measurements") or []) if m.get("node_id") in keep]
            # An epoch is a simultaneous look from several nodes; one node's
            # worth constrains nothing the constant-velocity fit can use.
            if len({m.get("node_id") for m in ms}) >= _MIN_NODES:
                kept_epochs.append({**epoch, "measurements": ms})
        s_next["cv_epochs"] = kept_epochs
        if "n_epochs" in s_in:
            s_next["n_epochs"] = len(kept_epochs)

    spare = s_in.get("pool_measurements")
    if spare:
        spare_kept = [m for m in spare if is_synthetic_node(m.get("node_id") or "") == keep_synthetic]
        s_next["pool_measurements"] = spare_kept
        if s_in.get("pool_n_nodes") is not None:
            # The pool's node set is the measured nodes plus the spares
            # (association's pool_node_ids); this is that set, one kind.
            s_next["pool_n_nodes"] = len(keep) + len({m.get("node_id") for m in spare_kept})
    return s_next


def restrict_claims(claims: dict[str, dict]) -> dict[str, dict]:
    """The known lane's per-node claims for one hex, narrowed to one kind.

    Unchanged (same object) when already one kind.  May come back with a
    single node, which the caller treats as it treats any short selection.
    """
    keep = single_kind_survivors(claims)
    if len(keep) == len(claims):
        return claims
    state.bump_counter("known_claims_kind_split")
    return {nid: c for nid, c in claims.items() if nid in keep}
