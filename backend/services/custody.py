"""Chain-of-custody ingest, shared by the HTTP route and the TCP handler.

A node closes a time window, signs a HashChainEntry over it and sends the entry
with the window's canonical frame batch. `ingest_chain_entry` verifies it
against the node's registered key and the chain already held for the node,
stores the entry with the verdict, and returns the acknowledgement body. The
batch is hashed and dropped; only the entry and the verdict are kept.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import fields
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from retina_custody.hash_chain import HashChainEntry, HashChainVerifier

from config.constants import CHAIN_ENTRIES_MAX_PER_NODE, CUSTODY_BATCH_MAX_CHARS
from core import state

log = logging.getLogger(__name__)

_NODE_ID_MAX_CHARS = 128
_FIELD_MAX_CHARS = 256
_PROOF_MAX_CHARS = 65536
_REQUIRED_STR_FIELDS = tuple(
    f.name for f in fields(HashChainEntry) if f.name not in ("n_detections", "tsa_token", "ots_proof")
)
_OPTIONAL_STR_FIELDS = ("tsa_token", "ots_proof")
_WINDOW_FIELDS = ("window_start", "window_end")

# Held from reading a chain's tail to appending to it, so two entries for the
# same window cannot both be checked against the same tail.
_ingest_lock = threading.Lock()


class MalformedEntry(ValueError):
    """The entry or batch cannot be parsed. The message never quotes a value."""


# A node's key registration, over HTTP or TCP REGISTER_KEY. No docstring: it
# would become the schema's description in the published OpenAPI.
class RegisterNodeRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    public_key_pem: str = Field(..., min_length=1, max_length=8192)
    fingerprint: str = Field(default="", max_length=256)
    serial_number: str = Field(default="", max_length=128)
    signing_mode: str = Field(default="software", max_length=32)


def public_key_for(node_id: str) -> str | None:
    ident = state.node_identities.get(node_id)
    return ident.public_key_pem if ident else None


def is_verified_window(stored: dict) -> bool:
    """Whether a stored row is a windowed entry that was verified on receipt.

    Rows from the hourly chain have no window_end and may carry a `_verified`
    stamp they were never checked for, so they never count and never anchor.
    """
    return stored.get("_verified") is True and "window_end" in stored


def parse_entry(entry_dict: object) -> HashChainEntry:
    """A HashChainEntry from untrusted input, with every field type-checked.

    `HashChainEntry.from_dict` checks only that the required keys are present,
    and the verifier slices and serialises the fields outside its own error
    handling, so a wrong type would raise there instead of failing the entry.
    """
    if not isinstance(entry_dict, dict):
        raise MalformedEntry("entry must be an object")
    try:
        entry = HashChainEntry.from_dict(entry_dict)
    except TypeError as exc:
        raise MalformedEntry(str(exc)) from None
    for name in _REQUIRED_STR_FIELDS:
        value = getattr(entry, name)
        if not isinstance(value, str):
            raise MalformedEntry(f"{name} must be a string")
        limit = _NODE_ID_MAX_CHARS if name == "node_id" else _FIELD_MAX_CHARS
        if len(value) > limit:
            raise MalformedEntry(f"{name} is too long")
        _require_unicode(name, value)
    for name in _WINDOW_FIELDS:
        if _instant(getattr(entry, name)) is None:
            raise MalformedEntry(f"{name} must be an ISO 8601 time with a UTC offset")
    if not entry.node_id:
        raise MalformedEntry("node_id must not be empty")
    n = entry.n_detections
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise MalformedEntry("n_detections must be a non-negative integer")
    for name in _OPTIONAL_STR_FIELDS:
        value = getattr(entry, name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise MalformedEntry(f"{name} must be a string")
        if len(value) > _PROOF_MAX_CHARS:
            raise MalformedEntry(f"{name} is too long")
        _require_unicode(name, value)
    return entry


def _require_unicode(name: str, value: str) -> None:
    # JSON can carry a lone surrogate, which no UTF-8 encoder accepts: stored,
    # it would fail every response and snapshot that serialises the row.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise MalformedEntry(f"{name} must be valid Unicode") from None


def _instant(value: object) -> datetime | None:
    """An ISO 8601 time with a UTC offset as an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _link(entry: HashChainEntry, held: list[dict] | None) -> tuple[str, bool]:
    """The prev_hash the entry must carry ("" for no linkage check), and whether it is stale.

    A verified tail anchors the entry. Behind unverified rows, an entry whose
    prev_hash names the newest verified window held is linked to that window
    and its window is not compared with that one's. Any other entry is not
    linked, and is stale unless it ends after the newest verified window.
    """
    tail = held[-1] if held else None
    if tail is not None and is_verified_window(tail) and tail["entry_hash"]:
        return tail["entry_hash"], False
    last = next((row for row in reversed(held or []) if is_verified_window(row)), None)
    if last is None:
        return "", False
    anchor = last.get("entry_hash")
    if isinstance(anchor, str) and anchor and entry.prev_hash == anchor:
        return anchor, False
    return "", _is_stale(entry, last)


def _is_stale(entry: HashChainEntry, last: dict) -> bool:
    """Whether the entry's window ends no later than `last`, a verified window held.

    A held window whose end does not parse cannot be shown to be earlier, so
    it counts as later.
    """
    last_end = _instant(last["window_end"])
    end = _instant(entry.window_end)
    return last_end is None or end is None or end <= last_end


def ingest_chain_entry(node_id: str, entry_dict: object, batch: object) -> dict:
    """Verify one window's entry, store it with the verdict, return the ack body.

    `node_id` is the node the message was sent under. The entry is linked to
    the last entry held for its node when that one was itself verified, so
    one bad entry does not fail every entry after it. Behind unverified rows,
    an entry whose prev_hash names the newest verified window held is linked
    to that window instead, so a window dated wrong cannot lock out its
    genuine successors. Any other entry must end after the newest verified
    window held, or it is stored unverified as stale, so a replay cannot pass
    behind an unverified tail. A window that does not start where the held
    tail ended is recorded as a gap, not rejected.

    Raises MalformedEntry, storing nothing, when the entry or batch cannot be
    parsed. Synchronous on purpose: everything from reading the chain's tail
    to appending runs under one module lock, which blocks whichever thread
    calls this, including the event loop's.
    """
    batch_bytes = None
    if batch is not None:
        if not isinstance(batch, str):
            raise MalformedEntry("batch must be a string")
        if len(batch) > CUSTODY_BATCH_MAX_CHARS:
            raise MalformedEntry("batch is too long")
        try:
            batch_bytes = batch.encode("utf-8")
        except UnicodeEncodeError:
            raise MalformedEntry("batch must be valid Unicode") from None
    entry = parse_entry(entry_dict)
    chain_id = entry.node_id

    with _ingest_lock:
        held = state.chain_entries.get(chain_id)
        tail = held[-1] if held else None

        if entry.node_id != node_id:
            verified, reason = False, "node_id mismatch"
        else:
            expected_prev, stale = _link(entry, held)
            if stale:
                verified, reason = False, "stale window: not after the last verified window"
            else:
                try:
                    verified, reason = HashChainVerifier(public_key_for).verify_entry(
                        entry, batch=batch_bytes, expected_prev_hash=expected_prev
                    )
                except Exception as exc:
                    verified, reason = False, f"verification error: {exc}"
        batch_verified = verified if batch is not None else None
        gap = tail is not None and entry.window_start != tail.get("window_end")

        stored = entry.to_dict() | {
            "_verified": verified,
            "_reason": reason,
            "_batch_verified": batch_verified,
            "_gap": gap,
            "_received_at": datetime.now(timezone.utc).isoformat(),
        }
        entries = state.chain_entries.setdefault(chain_id, [])
        entries.append(stored)
        # Replaced rather than trimmed in place, so a snapshot holding the old
        # list never sees it shrink under it.
        if len(entries) > CHAIN_ENTRIES_MAX_PER_NODE:
            state.chain_entries[chain_id] = entries[-CHAIN_ENTRIES_MAX_PER_NODE:]

    log.info(
        "Chain entry from %s (window_end=%s, verified=%s, reason=%s)", chain_id, entry.window_end, verified, reason
    )
    return {
        "status": "stored",
        "node_id": chain_id,
        "entry_hash": entry.entry_hash,
        "verified": verified,
        "reason": reason,
        "batch_verified": batch_verified,
    }
