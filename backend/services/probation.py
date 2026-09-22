"""Which polled radars are on probation, and the cache that answers it.

A stock blah2 radar is run by an operator nobody has vetted, so a polled node
that has not graduated feeds its own analytics and tracker and nothing else:
not the bistatic solve, not the archive, not a public payload. The fence is on
by default; POLLED_RADAR_PROBATION_ENABLED=0 switches it off. The frame workers ask
per frame from executor threads, so the answer is cached, on the same
synchronous-engine pattern as services/publication.py.

Fails closed: a `bla` id is on probation unless the cache positively knows it
as graduated, so a cold cache, a failed read and an id with no row all gate.
Ids of every other system are never on probation and are answered from their
prefix before the flag or the cache is read.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from core.node_ids import POLLED_BLAH2, system_of
from core.nodes import PolledRadar
from core.users import DATABASE_URL

logger = logging.getLogger(__name__)

GRADUATED = "graduated"

# A backstop for a writer that does not call invalidate(): a node sent back to
# probation is gated within this window regardless.
_TTL_S = 5.0
_ERROR_RETRY_S = 2.0

_lock = threading.Lock()
_graduated: frozenset[str] = frozenset()
_expires_at: float = 0.0
_engine = None


def enabled() -> bool:
    """Whether the fence is switched on. On unless set to exactly `0`.

    Read per call so switching it needs no restart.
    """
    return os.environ.get("POLLED_RADAR_PROBATION_ENABLED") != "0"


def _reset_for_tests() -> None:
    """Drop the cached answer and the engine. Tests only."""
    global _graduated, _expires_at, _engine
    with _lock:
        _graduated = frozenset()
        _expires_at = 0.0
        if _engine is not None:
            _engine.dispose()
        _engine = None


def _sync_engine():
    # Synchronous and NullPool for the reasons services/publication.py gives:
    # the callers are executor threads with no event loop to await on.
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{DATABASE_URL.split('///', 1)[1]}", poolclass=NullPool)
    return _engine


def _query() -> frozenset[str]:
    with _sync_engine().connect() as conn:
        rows = conn.execute(select(PolledRadar.node_id).where(PolledRadar.trust_state == GRADUATED))
        return frozenset(nid for (nid,) in rows)


def _graduated_ids() -> frozenset[str]:
    global _graduated, _expires_at
    if time.monotonic() < _expires_at:
        return _graduated
    with _lock:
        if time.monotonic() < _expires_at:
            return _graduated
        try:
            _graduated = _query()
            _expires_at = time.monotonic() + _TTL_S
        except Exception:
            # Not the last known set: that could hold open a node that has
            # since gone back to probation.
            _graduated = frozenset()
            _expires_at = time.monotonic() + _ERROR_RETRY_S
            logger.exception("probation: could not read graduated radars; gating every polled node")
        return _graduated


def invalidate() -> None:
    """Expire the cache after a committed trust_state change."""
    global _expires_at
    with _lock:
        _expires_at = 0.0


def in_probation(node_id: str | None) -> bool:
    """Whether a node's data is held back from everything but its own analytics and tracker."""
    # Prefix, then flag: fleet frames must not pay for either the environment or the cache.
    if not (isinstance(node_id, str) and node_id.startswith(POLLED_BLAH2)):
        return False
    if not enabled() or system_of(node_id) != POLLED_BLAH2:
        return False
    return node_id not in _graduated_ids()
