"""Polled radars as administrators see them, the trust decision on each, and
connecting one to an operator's email address.

Its own module rather than a routes/admin.py addition, as
routes/admin_infrastructure.py is. Decisions are recorded in node_events
rather than the admin event log, which is neither durable nor complete.

Connecting answers whether or not owners may register: it is how staff bring
radars on before registration opens. services/polled_registration.py decides.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.users import get_async_session, require_admin
from routes.polled_radar_registration import PolledRadarProbeRequest, PolledRadarRegisterRequest, refused_response
from services import blah2_poller, graduation, polled_registration, probation, publication
from services.polled_registration import RegistrationRefused

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ConnectionCheck(PolledRadarProbeRequest):
    # The operator's address, which may not have an account yet.
    email: EmailStr


class ConnectionRequest(PolledRadarRegisterRequest):
    email: EmailStr


class TrustDecision(BaseModel):
    trust_state: graduation.TrustState
    # The epoch the administrator was shown.
    epoch: int = Field(ge=1)


@router.get("/polled-radars")
async def admin_polled_radars(
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    return {
        "probation_enabled": probation.enabled(),
        # Where it is false, a radar connected here would never be heard.
        "polling_enabled": blah2_poller.enabled(),
        "radars": await graduation.listing(session),
    }


@router.put("/polled-radars/{node_id}/trust")
async def admin_set_polled_radar_trust(
    node_id: str,
    body: TrustDecision,
    admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Graduate a polled radar's current epoch, or return it to probation."""
    try:
        await graduation.set_trust(
            session, node_id, trust_state=body.trust_state, epoch=body.epoch, actor=f"admin:{admin['email']}"
        )
    except graduation.NotPolled:
        raise HTTPException(404, "Polled radar not found") from None
    except graduation.EpochMoved as exc:
        raise HTTPException(
            409,
            f"{node_id} is at epoch {exc.epoch} ({exc.trust_state}), not epoch {body.epoch}; reload and look again",
        ) from None
    await session.commit()
    # After the commit: expired before it, a reader in between would cache the old state again.
    probation.invalidate()
    publication.invalidate()
    return {"node_id": node_id, "epoch": body.epoch, "trust_state": body.trust_state}


@router.post("/polled-radars/probe")
async def admin_check_connection(
    body: ConnectionCheck,
    admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """What the radar declares, and whether the address has an account yet. Saves nothing."""
    try:
        probed, account = await polled_registration.check_connection(session, body.address, body.email, admin)
    except RegistrationRefused as exc:
        return refused_response(exc)
    return probed.public() | {"account": account}


@router.post("/polled-radars", status_code=201)
async def admin_connect_polled_radar(
    body: ConnectionRequest,
    admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Connect the radar to the account at the address, made for it if there is none."""
    try:
        connection = await polled_registration.connect(
            session,
            raw=body.address,
            fingerprint=body.fingerprint,
            publication=body.publication,
            email=body.email,
            admin=admin,
        )
    except RegistrationRefused as exc:
        return refused_response(exc)
    await session.commit()
    node = connection.registration.node
    await polled_registration.hand_over(session, node)
    return {
        "node_id": node.node_id,
        "epoch": connection.registration.radar.epoch,
        "trust_state": "probation",
        "owner": {"email": connection.email, "created": connection.created},
    }


@router.delete("/polled-radars/{node_id}")
async def admin_withdraw_polled_radar(
    node_id: str,
    admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Take back a connection whose owner has not signed in yet."""
    try:
        await polled_registration.withdraw(session, node_id, admin=admin)
    except RegistrationRefused as exc:
        return refused_response(exc)
    await session.commit()
    polled_registration.let_go(node_id)
    return {"ok": True}
