"""Which fleet a node id, or a solve's contributors, belong to.

Stdlib only, so the tracks archive writer and reader can classify ids without
importing the TCP handler and the app state behind it.
"""

from collections.abc import Callable, Iterable

# Must stay disjoint from routes/node_schemas.NodeId: services/node_pipeline.py
# registers every v1 node as real on the strength of that.
SYNTHETIC_NODE_PREFIXES = ("synth-", "e2e-", "realnode-", "test-")


def is_synthetic_node(node_id: str) -> bool:
    """Detect synthetic/test nodes by their ID prefix.

    Marks as synthetic any node with a test/simulation prefix:
    - synth-* — simulated fleet orchestrator nodes
    - e2e-* — frontend E2E test nodes (including bulk registration)
    - realnode-* — legacy E2E test nodes from prior CI runs
    - test-* — backend test suite nodes
    """
    return any(node_id.startswith(p) for p in SYNTHETIC_NODE_PREFIXES)


def node_kind(ids: str | Iterable[str] | None, is_synthetic: Callable[[str], bool] = is_synthetic_node) -> str | None:
    """Classify a solve's contributing node ids as "real", "synthetic" or "mixed".

    ``ids`` is a list of ids or the archive's comma-joined string. Blank
    items are ignored, and no ids at all gives None: an unknown fleet, not a
    real one. "synthetic" when every id is synthetic, "real" when none is,
    "mixed" otherwise.

    ``is_synthetic`` decides each id and defaults to the id prefix
    (is_synthetic_node). The solve's append sites pass the handshake verdict
    (core.state.node_world); the archive reader falls back on the prefix for
    rows written before the column existed, when that verdict is gone.
    """
    if ids is None:
        return None
    items = ids.split(",") if isinstance(ids, str) else ids
    present = [s for s in (str(x).strip() for x in items if x is not None) if s]
    if not present:
        return None
    synthetic = sum(1 for s in present if is_synthetic(s))
    if synthetic == len(present):
        return "synthetic"
    return "real" if synthetic == 0 else "mixed"
