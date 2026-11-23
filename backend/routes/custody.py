"""Chain of custody API endpoints."""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple

import orjson
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from retina_custody.hash_chain import HashChainEntry, HashChainVerifier
from retina_custody.models import NodeIdentity

from config.constants import CUSTODY_BATCH_MAX_CHARS, IQ_COMMITMENTS_MAX_PER_NODE
from core import state
from services.custody import (
    MalformedEntry,
    RegisterNodeRequest,
    ingest_chain_entry,
    is_verified_window,
    parse_entry,
    public_key_for,
)
from services.node_refs import id_for_identity, public_identity

router = APIRouter(tags=["custody"])

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")

_PUBLISHED_STR_MAX_CHARS = 64


def _published_str(value: object) -> str | None:
    """A stored value fit to publish as is, else None.

    Rows restored from a snapshot can hold whatever an older server stored:
    only a short string that encodes as UTF-8 is passed through.
    """
    if not isinstance(value, str) or len(value) > _PUBLISHED_STR_MAX_CHARS:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


# ── Request models ────────────────────────────────────────────────────────────


class ChainEntryRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    entry: dict[str, Any]
    batch: str | None = Field(default=None, max_length=CUSTODY_BATCH_MAX_CHARS)


class IqCommitmentRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    capture_id: str = Field(default="", max_length=256)
    model_config = {"extra": "allow"}


@router.post("/api/custody/register")
async def custody_register_node(
    body: RegisterNodeRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    identity = NodeIdentity(
        node_id=body.node_id,
        public_key_pem=body.public_key_pem,
        public_key_fingerprint=body.fingerprint,
        serial_number=body.serial_number,
        signing_mode=body.signing_mode,
        registered_at=datetime.now(timezone.utc).isoformat(),
    )
    state.node_identities[body.node_id] = identity

    return {
        "status": "registered",
        "node_id": body.node_id,
        "fingerprint": body.fingerprint,
        "signing_mode": identity.signing_mode,
    }


@router.post("/api/custody/chain-entry")
async def custody_submit_chain_entry(
    body: ChainEntryRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    try:
        return ingest_chain_entry(body.node_id, body.entry, body.batch)
    except MalformedEntry as exc:
        raise HTTPException(status_code=422, detail=f"malformed entry: {exc}") from None


@router.post("/api/custody/iq-commitment")
async def custody_iq_commitment(
    body: IqCommitmentRequest,
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    if RADAR_API_KEY and x_api_key != RADAR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    node_id = body.node_id
    if node_id not in state.iq_commitments:
        state.iq_commitments[node_id] = []

    body_dict = body.model_dump()
    body_dict["_received_at"] = datetime.now(timezone.utc).isoformat()
    commits = state.iq_commitments[node_id]
    commits.append(body_dict)
    if len(commits) > IQ_COMMITMENTS_MAX_PER_NODE:
        state.iq_commitments[node_id] = commits[-IQ_COMMITMENTS_MAX_PER_NODE:]

    return {"status": "committed", "node_id": node_id, "capture_id": body.capture_id}


@router.get("/api/custody/status")
async def custody_status():
    # Unauthenticated, and all three blocks are keyed on the node.  A map keyed
    # on node ids is the same disclosure as a field holding one, so the keys are
    # the published handle and a node with no handle is left out entirely;
    # registered_nodes counts the same filtered map so the total cannot
    # reinstate what the listing dropped.
    node_keys = {
        ref: {
            "fingerprint": ident.public_key_fingerprint,
            "signing_mode": ident.signing_mode,
            "serial_number": ident.serial_number,
            "registered_at": ident.registered_at,
        }
        for nid, ident in state.node_identities.items()
        if (ref := public_identity(nid))
    }
    body = orjson.dumps(
        {
            "registered_nodes": len(node_keys),
            "node_keys": node_keys,
            "chain_entries": {
                ref: {
                    "count": len(entries),
                    "latest_window_end": _published_str(entries[-1].get("window_end")) if entries else None,
                    "latest_verified": is_verified_window(entries[-1]) if entries else None,
                    "verified": sum(1 for e in entries if is_verified_window(e)),
                    "gaps": sum(1 for e in entries if e.get("_gap") is True),
                }
                for nid, entries in state.chain_entries.items()
                if (ref := public_identity(nid))
            },
            "iq_commitments": {
                ref: len(captures) for nid, captures in state.iq_commitments.items() if (ref := public_identity(nid))
            },
        }
    )
    return Response(content=body, media_type="application/json")


@router.get("/api/custody/chain/{node_ref}")
async def custody_node_chain(node_ref: str):
    # An unresolvable ref gets the same answer as a node with no entries, and
    # neither detail quotes what was asked for.
    node_id = id_for_identity(node_ref)
    entries = state.chain_entries.get(node_id, []) if node_id else []
    if not entries:
        raise HTTPException(status_code=404, detail="No chain entries for that node")

    identity = state.node_identities.get(node_id)
    # The registration record is the server's own, not part of any preimage, so
    # its node_id is renamed like every other published identity field.
    published_identity = None
    if identity:
        published_identity = {k: v for k, v in identity.to_dict().items() if k != "node_id"}
        published_identity["node_ref"] = node_ref
    # The signed entry bodies are withheld, not rewritten.  Each carries the
    # node_id it was signed over, inside the ECDSA preimage
    # (retina_custody/hash_chain.py), and the server holds only public keys: the
    # bodies cannot be published under the ref without publishing the mapping,
    # and cannot be rewritten without breaking the signature.  Refs are
    # enumerable from /api/radar/nodes, so one request each would otherwise
    # de-anonymise the whole fleet.  What is left is the metadata a caller can
    # act on without the bodies; the bodies themselves need an authenticated
    # surface.
    return {
        "node_ref": node_ref,
        "identity": published_identity,
        "chain_length": len(entries),
        "latest_window_end": _published_str(entries[-1].get("window_end")),
        "latest_verified": is_verified_window(entries[-1]),
        "verified_entries": sum(1 for e in entries if is_verified_window(e)),
        "gaps": sum(1 for e in entries if e.get("_gap") is True),
    }


# verify_chain's issue prefix, "Entry <index in the list it was given> (".
_ISSUE_INDEX = re.compile(r"Entry (\d+) \(")


class _Verdict(NamedTuple):
    entries: list  # the list it was computed over, held so its identity is not reused
    length: int
    keys: dict[str, str | None]  # every key the verifier asked for, as it was then
    issues: list[str]  # by stored index, before the node_id is renamed


# The last /verify verdict per node. A held chain is appended to in place and
# replaced by a new list otherwise (the rolling cap, a restore, retirement),
# and no stored row is edited, so the same list at the same length holds the
# same rows; with the same keys the verdict is the same.
_verdicts: dict[str, _Verdict] = {}


def _reset_for_tests() -> None:
    """Drop the cached verdicts. Tests only."""
    _verdicts.clear()


def _chain_verdict(node_id: str, entries: list) -> list[str]:
    """The issues verify_chain finds in a node's held entries, cached."""
    cached = _verdicts.get(node_id)
    if (
        cached is not None
        and cached.entries is entries
        and cached.length == len(entries)
        and all(public_key_for(nid) == pem for nid, pem in cached.keys.items())
    ):
        return cached.issues

    rows = list(entries)
    keys: dict[str, str | None] = {}

    def key_for(nid: str) -> str | None:
        keys[nid] = public_key_for(nid)
        return keys[nid]

    issues: list[tuple[int, str]] = []
    well_formed: list[tuple[int, HashChainEntry]] = []
    for i, stored in enumerate(rows):
        try:
            well_formed.append((i, parse_entry(stored)))
        except MalformedEntry as exc:
            window = (_published_str(stored.get("window_start")) if isinstance(stored, dict) else None) or "?"
            issues.append((i, f"Entry {i} ({window}): malformed entry: {exc}"))
    _, chain_issues = HashChainVerifier(key_for).verify_chain([e for _, e in well_formed])
    for issue in chain_issues:
        # Issues index the well-formed list; put the stored index back.
        m = _ISSUE_INDEX.match(issue)
        if m is None:
            issues.append((len(rows), issue))
            continue
        stored_i = well_formed[int(m.group(1))][0]
        issues.append((stored_i, f"Entry {stored_i} (" + issue[m.end() :]))
    issues.sort(key=lambda pair: pair[0])
    texts = [text for _, text in issues]

    # A verdict over a list no longer held goes at the next miss, so the
    # cache does not keep a retired node's entries alive.
    for nid in [nid for nid, v in _verdicts.items() if state.chain_entries.get(nid) is not v.entries]:
        del _verdicts[nid]
    _verdicts[node_id] = _Verdict(entries, len(rows), keys, texts)
    return texts


@router.get("/api/custody/verify/{node_ref}")
async def custody_verify_chain(node_ref: str):
    """Re-run on what is held the check done on receipt, minus the batch.

    The server keeps no batches, so this is a consistency check of the stored
    entries, not new evidence. Each entry is linked to the well-formed entry
    held before it, so one stored after a rejected entry reports a prev_hash
    issue here even if it was verified on receipt. Rows that do not parse as
    windowed entries, such as those of the old hourly chain, are reported as
    malformed at their stored index. The verdict is cached until the node's
    chain or key changes.
    """
    node_id = id_for_identity(node_ref)
    entries = state.chain_entries.get(node_id, []) if node_id else []
    if not entries:
        raise HTTPException(status_code=404, detail="No chain entries for that node")

    if node_id not in state.node_identities:
        raise HTTPException(status_code=400, detail="No public key registered for that node")

    try:
        issues = _chain_verdict(node_id, entries)
        # An issue is the verifier's own summary, or a parse error naming a
        # field, rather than a quoted entry body, and the only identifier
        # either interpolates is the entry's node_id, which ingest keys the
        # chain on, so the rename covers every id an issue can carry.
        # Everything else in one is a hash prefix or a window start.
        published = [text.replace(node_id, node_ref) for text in issues]
        return {"node_ref": node_ref, "chain_length": len(entries), "valid": not published, "issues": published}
    except Exception:
        logging.exception("Chain verification failed for %s", node_id)
        raise HTTPException(status_code=500, detail="Chain verification failed") from None
