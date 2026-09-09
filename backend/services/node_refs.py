"""node_id → node_ref resolution for the publication boundary.

`node_id` is the private identifier and `node_ref` the public one (D16). This
module is the single place the first becomes the second on the way out.

Cached rather than queried per frame: the callers are the aircraft-flush and
analytics executor threads, which have no event loop to await on, and route
handlers, which have one that must not be handed a second. Same shape and same
reason as services/publication.py, which is why the engine is synchronous with
NullPool.

`node_ref` is never reassigned today, but routes/node_config.py:68 promises
rotation, so the map expires rather than being loaded once.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from core.nodes import Node
from core.users import DATABASE_URL
from services.tcp_handler import is_synthetic_node

log = logging.getLogger(__name__)

_TTL_S = 30.0
_ERROR_RETRY_S = 5.0

_lock = threading.Lock()
_forward: dict[str, str] = {}
_reverse: dict[str, str] = {}
_expires_at: float = 0.0
# node ids already logged as unresolvable since the last successful refresh.
# Only ever touched under _lock, matching how _forward/_reverse are rebound.
_logged_unresolved: set[str] = set()

_engine = None


def _reset_for_tests() -> None:
    """Drop the cached maps. Tests only."""
    global _forward, _reverse, _expires_at, _engine
    with _lock:
        _forward = {}
        _reverse = {}
        _expires_at = 0.0
        _logged_unresolved.clear()
        if _engine is not None:
            _engine.dispose()
            _engine = None


def _sync_engine():
    # Same synchronous engine over the same SQLite file as services/publication.py,
    # and for the same reason: the async driver cannot be driven from an executor
    # thread, and NullPool keeps a connection from outliving the call that opened it.
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{DATABASE_URL.split('///', 1)[1]}", poolclass=NullPool)
    return _engine


def _load() -> tuple[dict[str, str], dict[str, str]]:
    with _sync_engine().connect() as conn:
        rows = conn.execute(select(Node.node_id, Node.node_ref))
        pairs = [(nid, ref) for nid, ref in rows if nid and ref]
    return {nid: ref for nid, ref in pairs}, {ref: nid for nid, ref in pairs}


def _refresh() -> None:
    """Repopulate both maps once the TTL has passed.

    The check is unconditional on time, whether or not a load has ever
    succeeded: a failed load backs off for `_ERROR_RETRY_S` the same way a
    successful one waits out `_TTL_S`, so a database outage before the first
    refresh is throttled exactly like one after, instead of re-querying on
    every call. A successful load rebinds `_forward` and `_reverse` to freshly
    built dicts, one assignment each, rather than clearing and refilling the
    existing ones, so a reader that is not holding the lock always sees a
    complete map, the old one or the new one, never one caught mid-update.
    Before the first success there is no previous map to fall back on and both
    stay empty, the same fallback services/publication.py uses.
    """
    global _forward, _reverse, _expires_at
    now = time.monotonic()
    if now < _expires_at:
        return
    with _lock:
        if time.monotonic() < _expires_at:
            return
        try:
            forward, reverse = _load()
        except Exception:
            log.exception("node_ref map refresh failed; keeping the previous one")
            _expires_at = time.monotonic() + _ERROR_RETRY_S
            return
        _forward = forward
        _reverse = reverse
        _expires_at = time.monotonic() + _TTL_S
        # A rebind is a new generation of the map; a node unresolvable under
        # the old one may have gained a row, so it earns a fresh log line if
        # it is still unresolvable, and one that regresses is reported again.
        _logged_unresolved.clear()


def ref_for(node_id: str | None) -> str | None:
    """The public handle for a node id, or None if it has no registry row."""
    if not node_id:
        return None
    _refresh()
    return _forward.get(node_id)


def id_for_ref(node_ref: str | None) -> str | None:
    """The private id behind a public handle, or None if unknown."""
    if not node_ref:
        return None
    _refresh()
    return _reverse.get(node_ref)


def public_identity(node_id: str | None) -> str | None:
    """What a node id is published as, or None if it must not be published.

    Synthetic nodes pass through: they are not hardware at anyone's address,
    and the map identifies them by these ids. A real node with no registry row
    yields None and its entry is dropped, because publishing the private id as
    a fallback is the failure this boundary exists to prevent.

    Callers span a 1 Hz flush and unauthenticated routes that a caller can hit
    at whatever rate it chooses, so the same unresolved id is logged at most
    once per cache generation rather than once per lookup; `_refresh` clears
    `_logged_unresolved` each time it rebinds the maps.
    """
    if not node_id:
        return None
    if is_synthetic_node(node_id):
        return node_id
    ref = ref_for(node_id)
    if ref is None:
        with _lock:
            first_report = node_id not in _logged_unresolved
            _logged_unresolved.add(node_id)
        if first_report:
            log.error("no node_ref for %s; dropping its contribution from the public feed", node_id)
    return ref


def _renamed(entry: dict, old: str, new: str, value) -> dict:
    """`entry` with key `old` replaced by `new` in place, carrying `value`."""
    return {(new if k == old else k): (value if k == old else v) for k, v in entry.items()}


def substitute_identities(data: dict) -> dict:
    """A feed payload under its published field names and identities.

    `node_id` becomes `node_ref` and `contributing_node_ids` becomes
    `contributing_node_refs`. The old names do not survive alongside the new
    ones: a field called `node_id` holding a ref is the confusion this boundary
    exists to remove, and a consumer that still finds the old key will keep
    reading it. `detecting_nodes` keeps its name, which claims no identifier
    type; only its values changed.

    Runs last on each publication path, after the private-node redaction in
    services/publication.py and after every node_id-keyed filter in
    services/tasks/aircraft_flush.py. Those filters match ids against
    state.connected_nodes, so substituting before them empties the feed.
    """
    aircraft = []
    for ac in data.get("aircraft", []):
        if "contributing_node_ids" in ac:
            contributors = ac["contributing_node_ids"]
            kept = [r for r in (public_identity(n) for n in contributors) if r]
            if contributors and not kept:
                continue
            ac = _renamed(ac, "contributing_node_ids", "contributing_node_refs", kept)
        if "node_id" in ac:
            node_id = ac["node_id"]
            ref = public_identity(node_id)
            # A null node_id is a multinode entry with no single detector, not
            # an unresolvable node, so it is published as a null ref.
            if node_id is not None and ref is None:
                continue
            ac = _renamed(ac, "node_id", "node_ref", ref)
        aircraft.append(ac)

    out = {**data, "aircraft": aircraft}

    if "detection_arcs" in data:
        arcs = []
        for arc in data.get("detection_arcs", []):
            ref = public_identity(arc.get("node_id"))
            if ref is None:
                continue
            arcs.append(_renamed(arc, "node_id", "node_ref", ref))
        out["detection_arcs"] = arcs

    detecting = data.get("detecting_nodes")
    if isinstance(detecting, dict):
        out["detecting_nodes"] = {
            hex_code: kept
            for hex_code, nids in detecting.items()
            if (kept := [r for r in (public_identity(n) for n in nids) if r])
        }

    if "messages" in data:
        out["messages"] = len(aircraft)
    return out
