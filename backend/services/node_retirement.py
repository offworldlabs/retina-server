"""Forget a node that has left the fleet.

Every per-node store in the server is append-only in practice.  Retirement is
the only path that clears every one of them together.  A background sweep
(``services.tasks.periodic.prune_synthetic_nodes``) also deletes a node from
``state.connected_nodes``, but only that one store, only for the synth-,
e2e- and test- prefixes, and only once it has sat disconnected for days;
analytics, coverage polygon, custody chain, reputation and the cached
pipeline all survive it.  A receiver that is decommissioned, renamed, or
replaced by a different fleet layout therefore keeps every one of those
until something retires it explicitly, and every subsequent analytics pass,
snapshot write and coverage save keeps paying for it.  Staging showed this
directly after a layout change: 10 receivers that no longer existed were
still being iterated and re-saved to disk, and the node count reported 26
against 16 live.

This is an explicit operation, not a staleness sweep.  A real receiver offline
for a week is still a real receiver whose accumulated coverage we want back
when it returns; only a decision that the node is *gone* should discard it.
``retire_node`` is that decision, and it is irreversible — the coverage
polygon in particular represents observation time that cannot be recreated.

Retiring a node that is currently connected is refused: it would be undone by
the next registration anyway, and it would strip the custody chain of a live
data source mid-session.
"""

import logging

from config import constants
from core import state

log = logging.getLogger(__name__)


class NodeStillConnected(Exception):
    """Raised when asked to retire a node that is currently active."""


class ForceRetireNotAllowed(Exception):
    """Raised when force-retiring a node id outside the configured allowlist.

    Carries the prefixes so the caller can say what would have been accepted
    without reading the configuration a second time.
    """

    def __init__(self, node_id: str, prefixes: tuple[str, ...]):
        super().__init__(node_id)
        self.node_id = node_id
        self.prefixes = prefixes


def live_node_ids() -> set[str]:
    """Node IDs the server currently considers part of the fleet."""
    with state.connected_nodes_lock:
        return set(state.connected_nodes)


def stale_node_ids() -> list[str]:
    """Per-node state held for nodes that are not in the fleet.

    The union across every store, so a node that survives in only one of them
    (custody without analytics, say) is still reported.
    """
    live = live_node_ids()
    held: set[str] = set()
    analytics = getattr(state, "node_analytics", None)
    if analytics is not None:
        for store in (
            analytics.trust_scores,
            analytics.detection_areas,
            analytics.metrics,
            analytics.reputations,
            analytics.coverage_maps,
            analytics.empirical_coverages,
        ):
            held |= set(store)
    held |= set(state.node_identities)
    held |= set(state.chain_entries)
    held |= set(state.iq_commitments)
    return sorted(held - live)


def retire_node(node_id: str, *, force: bool = False) -> dict:
    """Drop every trace of ``node_id``: fleet registry, analytics, coverage
    files, custody, and the cached pipeline.

    Returns a report of what was actually removed.  Raises NodeStillConnected
    if the node is active and ``force`` is not set, or ForceRetireNotAllowed
    if the node is active and ``force`` is set but ``node_id`` falls outside
    the configured allowlist.  Neither applies to a node already absent from
    the registry, which retires without ``force`` mattering either way.
    """
    report: dict = {"node_id": node_id}

    # The registry is cleared before analytics and the associator, not after.
    # Retirement is not atomic across the stores, so a frame arriving mid-call
    # re-registers the node into whichever store has already been cleared.  We
    # take the concurrent-frame case over the failure case: in this order the
    # survivor is a node visible on /api/radar/nodes with no geometry, which an
    # operator can see and retire again, where the reverse order strands
    # geometry that no endpoint reports.  The cost is that a failure in the
    # fallible work below (analytics deletes files) leaves the node already
    # gone from the registry; that is rarer than a frame landing mid-call, and
    # the except clause says what it left.
    #
    # The liveness check and the pop happen under the same lock acquisition.
    # A node registering between a separate check and pop would be retired
    # without force, exactly what NodeStillConnected exists to prevent; the
    # registering write comes from a frame-worker thread, so the GIL does not
    # close that window on its own.
    with state.connected_nodes_lock:
        connected = node_id in state.connected_nodes
        if connected:
            if not force:
                raise NodeStillConnected(node_id)
            # The allowlist gates force only where force is actually doing
            # something, namely overriding the refusal above.  A node already
            # absent from the registry retires without force at all, so
            # refusing it here would be a dead end rather than a safeguard:
            # the operator would be told to drop a flag they need not have
            # passed.  Read at call time so a deployment's setting is what
            # applies; see constants.force_retire_prefixes.
            allowed = constants.force_retire_prefixes()
            if allowed and not node_id.startswith(allowed):
                raise ForceRetireNotAllowed(node_id, allowed)
        state.connected_nodes.pop(node_id, None)
    report["was_connected"] = connected
    # Otherwise this waits on the 2 h disconnect sweep in
    # services.tasks.analytics_refresh, which would leave a retired node holding a
    # cached pipeline built from config that no longer exists anywhere else.
    report["pipeline_evicted"] = state.node_pipelines.pop(node_id, None) is not None

    try:
        analytics = getattr(state, "node_analytics", None)
        if analytics is not None:
            report["analytics"] = analytics.retire_node(node_id)

        associator = getattr(state, "node_associator", None)
        if associator is not None and hasattr(associator, "unregister_node"):
            report["overlap_zones_removed"] = associator.unregister_node(node_id)

        report["custody"] = {
            "identity": state.node_identities.pop(node_id, None) is not None,
            "chain_entries": len(state.chain_entries.pop(node_id, []) or []),
            "iq_commitments": len(state.iq_commitments.pop(node_id, []) or []),
        }
    except Exception:
        # The registry and pipeline are already cleared above, so the node is
        # now half retired: gone from /api/radar/nodes, still holding whatever
        # of analytics, associator geometry or custody didn't finish. It will
        # show up in stale_node_ids() until a retire-stale pass clears the rest.
        log.exception("Node %s is half retired after this failure", node_id)
        raise

    # The snapshot is written from these stores, so the next save already omits
    # the node; nothing needs to rewrite the file here.
    log.info("Retired node %s: %s", node_id, report)
    return report


def retire_stale_nodes() -> dict:
    """Retire every node held in state but absent from the fleet.

    A node can register between the target list being computed and its turn
    arriving, at which point it is no longer stale and retiring it unforced is
    refused.  That is one node changing its mind, not a failed sweep, so it is
    reported as skipped and the rest still run; letting it propagate would
    abort partway and leave the caller unable to tell which nodes went.
    """
    retired, skipped = [], []
    for nid in stale_node_ids():
        try:
            retired.append(retire_node(nid))
        except NodeStillConnected:
            skipped.append(nid)
    return {
        "retired": retired,
        "count": len(retired),
        "skipped": skipped,
    }
