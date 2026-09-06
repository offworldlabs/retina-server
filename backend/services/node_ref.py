"""The public handle for a node: `node_ref`, never the raw `node_id`.

A `node_id` comes off the board and is what Mender, the TCP handler and every
config file know the node as.  It is also, on this deployment, a name its owner
chose — `radar3-retnode` names a machine, and a run of them names a fleet's
naming convention — so printing it on a public map hands a stranger a
correlation key the node's owner never agreed to publish.  `core.nodes.Node`
already carries the answer: `node_ref`, minted at registration
(`services.node_auth.mint_node_ref`) as `"nde" + 12` base36 characters,
deliberately rotatable without reflashing the board.

The gap this module closes is that most live nodes have no row.  The `nodes`
table only holds nodes that registered through `/v1/nodes`; the blah2 bridge's
receivers and anything speaking the plain TCP protocol never do, and on the
test deployment (2026-09-06) that is all seven of them.  So a ref has to exist
for an unregistered node too, and it has to be indistinguishable from a minted
one — a map where some nodes show `ndeXXXXXXXXXXXX` and the rest show
`radar3-retnode` publishes exactly the ids it was trying not to.

For those, the ref is derived: `HMAC-SHA256(fuzz salt, "node_ref|" + node_id)`
rendered in the same base36 alphabet, truncated to the same 12 characters.

- The salt is `services.public_location._salt()` — the configured
  `NODE_FUZZ_SALT`, else the persisted runtime salt.  It is already this
  deployment's anonymity key, it is secret, and it survives restarts, so a
  derived ref is stable for the life of the deployment without a table to
  store it in.  A deployment that rotates the salt re-anonymises its nodes,
  which is the same thing rotating it already does to their positions.
- The `node_ref|` domain prefix separates this HMAC frame from the location
  one (`public_location._frame_message`, which hashes a bare identity or an
  identity plus a bounds label).  Two frames sharing a key must not be able to
  spell each other's messages, or a published ref would be a published sample
  of the offset key.
- There is no fallback to the raw id.  A node whose ref cannot be looked up is
  derived; derivation needs only the salt, and the salt always resolves (see
  `_persisted_salt`).  "Publish the id when something goes wrong" would make
  the leak conditional on a database being down, which is precisely when
  nobody is watching.

Registered nodes still win: their stored `node_ref` is the handle the rest of
the node API and the dashboard already use, and deriving a second one for them
would publish two names for one node.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time

logger = logging.getLogger(__name__)

# How long a snapshot of the nodes table is reused.  Refs change only when a
# node registers or is rotated, both human-speed events, and the alternative is
# a database round trip per published node per refresh cycle.  Mirrors
# services/node_sites.py, which caches the same kind of thing for the same
# reason.
_TTL_S = 30.0

# After a failed refresh, retry sooner than the full TTL — the snapshot is
# older than it should be, not wrong.
_ERROR_RETRY_S = 5.0

_PREFIX = "nde"
_REF_CHARS = 12  # same shape as mint_node_ref, so the two are not tellable apart

# Domain separator for the derivation HMAC.  "|" cannot start a node id, and
# the location frame's messages never begin with this literal, so no node id
# can spell a message in the other frame.
_DOMAIN = "node_ref|"

_lock = threading.Lock()
# node_id -> stored node_ref, from the last successful read of the nodes table.
_db_refs: dict[str, str] = {}
_expires_at: float = 0.0
# (salt, node_id) -> derived ref.  Keyed on the salt so a re-salted deployment
# (or a test that monkeypatches it) cannot read a stale ref back out — the same
# reason public_location keys its offset cache on the salt.  Bounded by the
# number of nodes this process has ever published.
_derived: dict[tuple[str, str], str] = {}
# Whether the "no node table" case has been logged.  It is the normal state of
# a deployment whose nodes all predate /v1/nodes, so it is worth saying once
# and worth never saying again.
_db_unavailable_logged = False


def _reset_for_tests() -> None:
    """Drop the snapshot and the derived refs.  Tests only."""
    global _expires_at, _db_unavailable_logged
    with _lock:
        _db_refs.clear()
        _derived.clear()
        _expires_at = 0.0
        _db_unavailable_logged = False


def _refs_from_db() -> dict[str, str]:
    """{node_id: node_ref} for every registered node.

    Imported inside the function and driven by the synchronous-engine pattern
    services/publication.py documents, for the reasons services/node_sites.py
    gives: the callers are executor threads and route handlers, neither of
    which can await, and this module must import cleanly in a process with no
    database at all.
    """
    from sqlalchemy import select

    from core.nodes import Node
    from services.publication import _sync_engine

    with _sync_engine().connect() as conn:
        rows = conn.execute(select(Node.node_id, Node.node_ref))
        return {node_id: ref for node_id, ref in rows if node_id and ref}


def _snapshot() -> dict[str, str]:
    global _expires_at, _db_unavailable_logged
    now = time.monotonic()
    if now < _expires_at:
        return _db_refs
    with _lock:
        if time.monotonic() < _expires_at:
            return _db_refs
        try:
            _db_refs.clear()
            _db_refs.update(_refs_from_db())
            _expires_at = time.monotonic() + _TTL_S
        except Exception:
            # No database, no table, or a transient failure.  Every node then
            # derives its ref, which is the answer for an unregistered node
            # anyway — so this degrades to "nothing is registered", never to
            # publishing an id.
            if not _db_unavailable_logged:
                _db_unavailable_logged = True
                logger.exception("node_ref: no node registry available, deriving every public ref")
            _expires_at = time.monotonic() + _ERROR_RETRY_S
    return _db_refs


def _derived_ref(node_id: str) -> str:
    """The HMAC-derived ref for a node with no registry row."""
    from services.node_auth import _ALPHABET
    from services.public_location import _salt

    salt = _salt()
    key = (salt, node_id)
    cached = _derived.get(key)
    if cached is not None:
        return cached

    digest = hmac.new(salt.encode("utf-8"), (_DOMAIN + node_id).encode("utf-8"), hashlib.sha256).digest()
    # Base36 over the digest as one big integer, least-significant digit first.
    # Twelve characters is ~62 bits of the 256 available, the same width — and
    # the same alphabet — mint_node_ref draws at random.
    value = int.from_bytes(digest, "big")
    base = len(_ALPHABET)
    chars = []
    for _ in range(_REF_CHARS):
        value, remainder = divmod(value, base)
        chars.append(_ALPHABET[remainder])
    ref = _PREFIX + "".join(chars)
    _derived[key] = ref
    return ref


def public_node_ref(node_id: str) -> str:
    """The public handle for a node: its registered ref, else a derived one.

    Never the node id.  A missing id derives from the empty string rather than
    passing through, for the reason public_offset_km hashes it: a missing id is
    a config fault, and the safe reading of a config fault is not to publish
    whatever was there.
    """
    return _snapshot().get(node_id) or _derived_ref(node_id or "")
