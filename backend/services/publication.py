"""Which nodes their owners asked us not to publish, and the cache that answers it.

Registration asks the owner one question — publish this node's detections, or
don't — and ``Node.publication`` records the answer (routes/node_register.py,
routes/node_schemas.py).  Nothing read it.  A node registered ``private`` was
solved against, drawn on the public map, summarised in analytics and archived
for anyone to download, exactly like a node registered ``public``.  This module
is the answer to "may this node's data be published", and the surfaces that
serve unauthenticated callers ask it.

**Not the same promise as fuzzing.**  ``public_location`` moves a receiver so
that publishing its data does not publish its address; this withholds the data
altogether, because the owner said so.  So ``fuzz_enabled()`` does not gate it:
turning the fuzz off is a deployment's choice about precision, and it is not
anyone's authority to overrule a registration.  The two compose — a private
node is absent, and a public node is displaced.

**Where the line falls.**  A single-node detection is that node's product and
goes.  A multinode solve is the network's — no one node's geometry produced it,
and dropping it would delete other operators' contributions along with the
private one — so the aircraft stays and the private id is struck from the
membership list it appears in.  An owner still sees their own private node in
full on the authenticated owner feed; enforcement lives on the public path
only, which is why services/tasks/aircraft_flush.py redacts a sibling payload
rather than the one the owner filter reads.

**Failure.**  The two wrong answers are not symmetric but they are both wrong:
answer "everything is private" on a dropped connection and the map goes blank
for a database hiccup; answer "nothing is private" and a hiccup publishes what
an owner declined.  So a failed query changes nothing — the last known set is
served and the failure is logged.  Before the first successful query there is
no last known set and the answer is the empty one, which is the only honest
thing an uninitialised cache can say and matches a fleet that has registered
nobody.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import create_engine, select
from sqlalchemy.pool import NullPool

from core.nodes import Node
from core.users import DATABASE_URL

logger = logging.getLogger(__name__)

# How long an answer is reused.  The choice is a registration, so it changes at
# human speed — a node that re-registers with a new choice is honoured within
# this window, and the alternative is a database round trip on every aircraft
# in every 1 Hz frame.
_TTL_S = 30.0

# After a failed query, retry sooner than the full TTL: the cache is serving
# values it can no longer vouch for, and the cost of asking again is one small
# indexed read.  Still a backoff rather than every call, so a database that is
# down does not get hammered by the feed loop.
_ERROR_RETRY_S = 5.0

_lock = threading.Lock()
_cached: frozenset[str] = frozenset()
_expires_at: float = 0.0
# Distinct from ``_cached`` being empty: an empty set from a healthy database
# means "no node is private", and the boot state means "nobody has asked yet".
# Only the log line depends on the difference, but conflating them is how a
# fail-open reads as a legitimate answer.
_have_data: bool = False

_engine = None


def _reset_for_tests() -> None:
    """Drop the cached answer and the engine.  Tests only."""
    global _cached, _expires_at, _have_data, _engine
    with _lock:
        _cached = frozenset()
        _expires_at = 0.0
        _have_data = False
        if _engine is not None:
            _engine.dispose()
        _engine = None


def _sync_engine():
    """A synchronous engine over the same SQLite file the app's async one uses.

    Everything else in this codebase reaches the database through
    ``core.users.async_session_maker``, and this deliberately does not.  The
    callers are the aircraft-flush and analytics executor threads, which have no
    event loop to await on, and route handlers, which have one that must not be
    handed a second.  Driving the async engine from a fresh loop per call would
    also put loop-bound pooled connections in the way, which is a race waiting
    to be found rather than a design.

    ``Node.__table__`` is still the source of truth for what is queried, so a
    renamed column moves with the model rather than silently returning nothing.
    ``NullPool`` keeps a connection from outliving the call that opened it, so
    the engine is safe from any thread; the cost is a connect per refresh, which
    on a local SQLite file is a fraction of a millisecond and happens at most
    once per _TTL_S for the whole process.

    Derived from DATABASE_URL rather than a second read of the path, the same
    way services/tasks/users_backup.py does, so a test pointing RETINA_DB_PATH
    at a scratch file cannot leave this one reading the real database.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(f"sqlite:///{DATABASE_URL.split('///', 1)[1]}", poolclass=NullPool)
    return _engine


def _query() -> frozenset[str]:
    with _sync_engine().connect() as conn:
        rows = conn.execute(select(Node.node_id).where(Node.publication == "private"))
        return frozenset(r[0] for r in rows if r[0])


def private_node_ids() -> frozenset[str]:
    """Node ids whose owner chose not to publish, as of at most _TTL_S ago.

    A node absent from the table is public: registration is what records the
    choice, and the synthetic fleet and any pre-registration node never made
    one.  The default is public because that is what the fleet was before the
    column was enforced, and silently retiring nodes from the map on a schema
    reading would be its own kind of wrong.
    """
    global _cached, _expires_at, _have_data
    now = time.monotonic()
    if now < _expires_at:
        return _cached
    with _lock:
        # Re-check under the lock: several feed threads can arrive together on
        # the same expiry and only one of them needs to do the read.
        if time.monotonic() < _expires_at:
            return _cached
        try:
            _cached = _query()
            _have_data = True
            _expires_at = time.monotonic() + _TTL_S
        except Exception:
            _expires_at = time.monotonic() + _ERROR_RETRY_S
            if _have_data:
                logger.exception(
                    "publication: could not refresh the private-node set, serving the last known %d id(s)",
                    len(_cached),
                )
            else:
                logger.exception(
                    "publication: could not read the private-node set and have never read one; "
                    "treating every node as public until a query succeeds"
                )
        return _cached


def is_private(node_id: str | None) -> bool:
    """Whether one node id is in the private set.  None/"" is public."""
    if not node_id:
        return False
    return node_id in private_node_ids()


def public_aircraft_payload(data: dict) -> dict:
    """An aircraft feed payload with private nodes' contributions removed.

    Four edits, and no others:

    * An entry whose ``node_id`` is private is dropped.  Every single-node
      position — an arc crossing, a solver estimate, a claimed ADS-B fix — is
      that node's detection and nothing else's.
    * A ``detection_arcs`` entry from a private node is dropped, for the same
      reason and more sharply: an ambiguity arc is an ellipse with the receiver
      at a focus, so it is the node's location written down.
    * A multinode entry keeps its position and loses the private ids from
      ``contributing_node_ids``.  The position is the network's product and
      deleting it would take other operators' contributions with it; the
      membership list is a statement about who was listening, which is the part
      the owner declined to publish.  An entry left with no contributors at all
      is dropped — every node behind it was private, so there is nothing left it
      is the product of.
    * ``detecting_nodes`` — hex → the node ids currently holding a track for it,
      the one place the full per-node fan-out is visible — loses private ids,
      and a hex left with none loses its entry.  The per-owner and real-only
      feeds blank this map wholesale; the full feed does not, so it is a
      membership list published by name.

    ``ground_truth``/``ground_truth_meta`` are simulator surfaces keyed by
    aircraft hex and carry no node identity at all; they are left alone here and
    stripped by the per-owner filter as before.

    Returns the input object itself when no node is private, so the caller can
    reuse the bytes it already serialised.
    """
    private = private_node_ids()
    if not private:
        return data

    aircraft = []
    for ac in data.get("aircraft", []):
        if ac.get("node_id") in private:
            continue
        contributors = ac.get("contributing_node_ids")
        if contributors:
            kept = [nid for nid in contributors if nid not in private]
            if not kept:
                continue
            if len(kept) != len(contributors):
                ac = {**ac, "contributing_node_ids": kept}
        aircraft.append(ac)

    arcs = [arc for arc in data.get("detection_arcs", []) if arc.get("node_id") not in private]

    out = {**data, "aircraft": aircraft}
    if "detection_arcs" in data:
        out["detection_arcs"] = arcs
    detecting = data.get("detecting_nodes")
    if isinstance(detecting, dict):
        out["detecting_nodes"] = {
            hex_code: kept
            for hex_code, nids in detecting.items()
            if (kept := [nid for nid in nids if nid not in private])
        }
    # `messages` is the served aircraft count everywhere else in this payload
    # (filter_payload_to_nodes recomputes it too); leaving the pre-redaction
    # count would tell a reader how many entries were removed.
    if "messages" in data:
        out["messages"] = len(aircraft)
    return out


def public_summaries(summaries: dict) -> dict:
    """A {node_id: summary} analytics map with private nodes absent.

    A per-node summary is the node's detection area, its empirical coverage
    polygon and its accuracy record — the most directly locating payload the
    server serves, even after public_location has moved the anchor.  There is
    no partial version of it worth keeping, so a private node simply is not in
    the map.
    """
    private = private_node_ids()
    if not private:
        return summaries
    return {nid: s for nid, s in summaries.items() if nid not in private}
