"""Chain of custody API endpoints."""

import logging
import os
from datetime import datetime, timezone

import orjson
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from retina_custody.hash_chain import HashChainEntry, HashChainVerifier
from retina_custody.models import NodeIdentity

from config.constants import CHAIN_ENTRIES_MAX_PER_NODE, IQ_COMMITMENTS_MAX_PER_NODE
from core import state
from services.node_refs import id_for_identity, public_identity

router = APIRouter()

RADAR_API_KEY = os.getenv("RADAR_API_KEY", "")


# ── Request models ────────────────────────────────────────────────────────────


class RegisterNodeRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    public_key_pem: str = Field(..., min_length=1, max_length=8192)
    fingerprint: str = Field(default="", max_length=256)
    serial_number: str = Field(default="", max_length=128)
    signing_mode: str = Field(default="software", max_length=32)


class ChainEntryRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    entry_hash: str = Field(default="", max_length=256)
    prev_hash: str = Field(default="", max_length=256)
    hour_utc: str = Field(default="", max_length=32)
    payload_hash: str = Field(default="", max_length=256)
    signature: str = Field(default="", max_length=4096)
    model_config = {"extra": "ignore"}  # Drop unknown fields — enumerated fields cover the chain schema


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
    state.sig_verifier.register_key(body.node_id, body.public_key_pem)

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

    node_id = body.node_id
    if node_id not in state.chain_entries:
        state.chain_entries[node_id] = []

    verified = False
    reason = "no key registered"
    body_dict = body.model_dump()
    if node_id in state.node_identities:
        try:
            entry_obj = HashChainEntry.from_dict(body_dict)
            verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
            verified, reason = verifier.verify_entry(entry_obj)
        except Exception as exc:
            reason = str(exc)

    body_dict["_verified"] = verified
    body_dict["_received_at"] = datetime.now(timezone.utc).isoformat()
    entries = state.chain_entries[node_id]
    entries.append(body_dict)
    # Rolling cap — drop oldest entries when limit exceeded
    if len(entries) > CHAIN_ENTRIES_MAX_PER_NODE:
        state.chain_entries[node_id] = entries[-CHAIN_ENTRIES_MAX_PER_NODE:]

    return {
        "status": "stored",
        "node_id": node_id,
        "entry_hash": body.entry_hash,
        "verified": verified,
        "reason": reason,
    }


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
                    "latest_hour": entries[-1].get("hour_utc") if entries else None,
                    "latest_verified": entries[-1].get("_verified") if entries else None,
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
        "latest_hour": entries[-1].get("hour_utc"),
        "latest_verified": entries[-1].get("_verified"),
        "verified_entries": sum(1 for e in entries if e.get("_verified")),
    }


@router.get("/api/custody/verify/{node_ref}")
async def custody_verify_chain(node_ref: str):
    node_id = id_for_identity(node_ref)
    entries = state.chain_entries.get(node_id, []) if node_id else []
    if not entries:
        raise HTTPException(status_code=404, detail="No chain entries for that node")

    if node_id not in state.node_identities:
        raise HTTPException(status_code=400, detail="No public key registered for that node")

    try:
        entry_objs = [HashChainEntry.from_dict(e) for e in entries]
        verifier = HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))
        valid, issues = verifier.verify_chain(entry_objs)
        # An issue is the verifier's own summary rather than a quoted entry
        # body, and the only identifier it interpolates is the entry's node_id,
        # which both ingest paths key the chain on, so the rename covers every
        # id an issue can carry.  Everything else in one is a hash prefix.
        issues = [i.replace(node_id, node_ref) for i in issues]
        return {"node_ref": node_ref, "chain_length": len(entries), "valid": valid, "issues": issues}
    except Exception:
        logging.exception("Chain verification failed for %s", node_id)
        raise HTTPException(status_code=500, detail="Chain verification failed") from None
