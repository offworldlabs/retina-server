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

Retiring a node still held in the fleet registry is refused: it would be undone
by the next registration anyway, and it would strip the custody chain of a live
data source mid-session.  Held is registry presence rather than a live stream,
so a receiver marked disconnected is still refused.
"""

import logging

from config import constants
from core import state

log = logging.getLogger(__name__)


class NodeStillConnected(Exception):
    """Raised when asked to retire a node still held in the fleet registry.

    Held rather than streaming: an entry marked disconnected still counts,
    because nothing has removed it and the next frame would restore it.
    """


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
    if the node is still held in the fleet registry and ``force`` is not set,
    or ForceRetireNotAllowed if it is held and ``force`` is set but ``node_id``
    falls outside the configured allowlist.  Held means present in
    ``state.connected_nodes`` whatever its status, so a receiver marked
    disconnected still qualifies; neither exception applies to a node already
    absent from the registry, which retires without ``force`` mattering.
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
        # Presence in the registry, not the entry's status field.  A receiver
        # dark for a week is still held here (check_node_health only marks it
        # disconnected, and prune_synthetic_nodes only removes test-prefixed
        # ids after seven days), so it still needs force and is still gated by
        # the allowlist.  Reading this as "currently streaming" would put both
        # the wrong nodes through the guard.
        in_registry = node_id in state.connected_nodes
        if in_registry:
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
    report["was_connected"] = in_registry
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
        log.exception("Node %s is half retired after this failure; what went: %s", node_id, report)
        raise

    # The snapshot is written from these stores, so the next save already omits
    # the node; nothing needs to rewrite the file here.
    log.info("Retired node %s: %s", node_id, report)
    return report


def retire_stale_nodes() -> dict:
    """Retire every node held in state but absent from the fleet.

    Every node is attempted and every outcome is reported, because the sweep is
    a batch: one node's problem is not a reason to abandon the rest, and an
    exception escaping the loop would discard the record of everything already
    retired in this pass, leaving the caller to re-derive it from
    ``stale_node_ids``.  A node can register between the target list being
    computed and its turn arriving, which is one node changing its mind rather
    than a failure; anything else is a genuine fault and is reported separately
    so the two are not confused.  Callers that need a failure to be loud should
    read ``failed``: the route turns it into a non-200, since a sweep where
    nothing worked must not read as success.

    All three lists carry ``node_id``-keyed entries so a caller can treat them
    the same way.
    """
    retired, skipped, failed = [], [], []
    for nid in stale_node_ids():
        try:
            retired.append(retire_node(nid))
        except NodeStillConnected:
            log.info("Skipped %s: registered again before the sweep reached it", nid)
            skipped.append({"node_id": nid})
        except Exception as exc:
            # No traceback here: retire_node has already logged one for
            # everything that fails inside its own try block, which is every
            # fallible thing it does, and a second stack per failed node
            # doubles what an operator reads for one error.  This line exists
            # to name the sweep as the context and to carry the id.
            log.error("Retiring %s failed during the sweep: %r", nid, exc)
            failed.append({"node_id": nid, "error": repr(exc)})
    if skipped or failed:
        log.warning(
            "Retire-stale finished with %d retired, %d skipped, %d failed",
            len(retired),
            len(skipped),
            len(failed),
        )
    return {
        "retired": retired,
        "count": len(retired),
        "skipped": skipped,
        "failed": failed,
    }
