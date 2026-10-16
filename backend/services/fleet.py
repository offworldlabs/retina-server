"""The admin fleet read: every node the registry or the Mender mirror knows,
one row each, with its claim and contact state.

A node the registry holds and the mirror lists is `both`; one only the registry
holds is `registry_only`; one only Mender knows is `mender_only`. The mirror
(core/mender_mirror.py) can hold several rows for a node_id, a decommissioned
device and its replacement, and the row shown is the present one, a flagged
one first so its issue is visible, else the one confirmed last.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.mender_mirror import (
    MenderDevice,
    MenderSyncState,
    load_sync_state,
    mender_source,
    mirror_max_age_s,
    snapshot_age_s,
    stale_reason,
)
from core.node_ids import FLEET
from core.nodes import Node, NodeClaim, NodeContact
from core.users import accounts_by_id

PRESENCES = ("both", "registry_only", "mender_only")


def _epoch(when: datetime | None) -> float | None:
    return when.timestamp() if when is not None else None


def _header(state: MenderSyncState | None, now: datetime) -> dict[str, Any]:
    reason = stale_reason(state, now)
    try:
        max_age_s = mirror_max_age_s()
    except ValueError:
        # A bad limit is shown as stale, not hidden: registration refuses on it too.
        max_age_s = None
    return {
        "last_snapshot_at": _epoch(state.last_snapshot_at) if state else None,
        "age_s": snapshot_age_s(state, now),
        "max_age_s": max_age_s,
        "stale": reason is not None,
        "stale_reason": reason,
        "generated_at": _epoch(state.generated_at) if state else None,
        "source": state.source if state else None,
        "device_count": state.device_count if state else None,
        "mender_source": mender_source(),
    }


def _mirror_rows(devices: list[MenderDevice]) -> tuple[dict[str, MenderDevice], list[MenderDevice]]:
    """The device each node_id is shown by, and the present devices with no node_id."""
    by_node: dict[str, MenderDevice] = {}
    unidentified = []
    for device in devices:
        if device.node_id is None:
            if device.present:
                unidentified.append(device)
            continue
        shown = by_node.get(device.node_id)
        if shown is None or _rank(device) > _rank(shown):
            by_node[device.node_id] = device
    return by_node, unidentified


def _rank(device: MenderDevice) -> tuple[bool, bool, datetime, str]:
    flagged = device.present and device.snapshot_issue is not None
    return device.present, flagged, device.last_confirmed_at, device.mender_device_id


def _row(
    node_id: str | None,
    node: Node | None,
    device: MenderDevice | None,
    claim: NodeClaim | None,
    claim_email: str | None,
    contact_email: str | None,
) -> dict[str, Any]:
    if node is not None:
        presence = "both" if device is not None else "registry_only"
    else:
        presence = "mender_only"
    if claim is not None and claim.verified:
        email_state = "claimed_verified"
    elif contact_email:
        email_state = "contact_only"
    else:
        email_state = "none"
    return {
        "node_id": node_id,
        "presence": presence,
        "node_ref": node.node_ref if node else None,
        "status": node.status if node else None,
        "first_seen_at": _epoch(node.first_seen_at) if node else None,
        "last_seen_at": _epoch(node.last_seen_at) if node else None,
        "claim_email": claim_email,
        "claim_verified": bool(claim.verified) if claim else None,
        "contact_email": contact_email,
        "email_state": email_state,
        "mender_device_id": device.mender_device_id if device else None,
        "auth_status": device.auth_status if device else None,
        "device_type": device.device_type if device else None,
        "artifact_name": device.artifact_name if device else None,
        "inventory_updated_at": _epoch(device.inventory_updated_at) if device else None,
        "present": bool(device.present) if device else None,
        "snapshot_issue": device.snapshot_issue if device else None,
        "never_registered": presence == "mender_only" and node_id is not None and node_id.startswith(FLEET),
    }


async def build_fleet(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """The fleet as the admin console reads it. `now` must be timezone-aware."""
    nodes = {n.node_id: n for n in await session.scalars(select(Node))}
    claims = {c.node_id: c for c in await session.scalars(select(NodeClaim))}
    contacts = {c.node_id: c for c in await session.scalars(select(NodeContact))}
    devices, unidentified = _mirror_rows(list(await session.scalars(select(MenderDevice))))
    accounts = await accounts_by_id(session, [c.user_id for c in claims.values() if c.user_id is not None])

    def claim_email(claim: NodeClaim | None) -> str | None:
        # An owned claim reads as the account's current address, as the admin
        # claim read does (routes/admin.py, _admin_claims): the address a claim
        # was made with stays on the row after the account changes it.
        if claim is None:
            return None
        account = accounts.get(claim.user_id) if claim.user_id is not None else None
        return account.email if account else claim.email

    rows = []
    for node_id in nodes.keys() | devices.keys():
        claim, contact = claims.get(node_id), contacts.get(node_id)
        rows.append(
            _row(
                node_id,
                nodes.get(node_id),
                devices.get(node_id),
                claim,
                claim_email(claim),
                contact.email if contact else None,
            )
        )
    rows.extend(_row(None, None, device, None, None, None) for device in unidentified)
    rows.sort(key=lambda r: (r["node_id"] is None, r["node_id"] or "", r["mender_device_id"] or ""))

    counts = dict.fromkeys(PRESENCES, 0)
    for row in rows:
        counts[row["presence"]] += 1
    return {
        "mirror": _header(await load_sync_state(session), now),
        "counts": counts,
        "rows": rows,
    }
