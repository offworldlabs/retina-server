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

log = logging.getLogger(__name__)

_TTL_S = 30.0
_ERROR_RETRY_S = 5.0

_lock = threading.Lock()
_forward: dict[str, str] = {}
_reverse: dict[str, str] = {}
_expires_at: float = 0.0

_engine = None


def _reset_for_tests() -> None:
    """Drop the cached maps. Tests only."""
    global _forward, _reverse, _expires_at, _engine
    with _lock:
        _forward = {}
        _reverse = {}
        _expires_at = 0.0
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
