"""An owner registering a stock blah2 radar: probe it, then register it.

Absent (404) wherever POLLED_RADAR_REGISTRATION_ENABLED is not exactly `1`, as
though unmounted, so a deployment that takes no registrations offers nothing to
probe with. services/polled_registration.py decides; this module speaks HTTP.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.users import get_async_session, get_current_user
from services import blah2_poller, polled_registration, publication
from services.polled_registration import RegistrationRefused


class PolledRadarProbeRequest(BaseModel):
    # parse_endpoint refuses anything longer with a message of its own.
    address: str = Field(max_length=2048)


class PolledRadarRegisterRequest(PolledRadarProbeRequest):
    # The declaration the owner confirmed, as the probe reported it.
    fingerprint: str = Field(min_length=64, max_length=64)
    publication: Literal["public", "private"]


def _refused(exc: RegistrationRefused) -> JSONResponse:
    body: dict = {"detail": exc.message, "code": exc.code}
    if exc.probe is not None:
        body["probe"] = exc.probe.public()
    headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s is not None else None
    return JSONResponse(body, status_code=exc.status, headers=headers)


def _open() -> None:
    if not polled_registration.enabled():
        raise HTTPException(404, "Not Found")


# A router dependency runs before the body is validated, so a closed switch
# answers 404 whatever was sent.
router = APIRouter(prefix="/api/auth/me/polled-radars", tags=["auth"], dependencies=[Depends(_open)])


@router.post("/probe")
async def probe_polled_radar(
    body: PolledRadarProbeRequest, request: Request, session: AsyncSession = Depends(get_async_session)
):
    """What the radar at this address declares, for the owner to confirm. Saves nothing."""
    user = await get_current_user(request)
    try:
        probed = await polled_registration.probe(session, body.address, user["id"])
    except RegistrationRefused as exc:
        return _refused(exc)
    return probed.public()


@router.post("", status_code=201)
async def register_polled_radar(
    body: PolledRadarRegisterRequest, request: Request, session: AsyncSession = Depends(get_async_session)
):
    """Register the radar, on probation, if it still declares what the owner confirmed."""
    user = await get_current_user(request)
    try:
        registration = await polled_registration.register(
            session, raw=body.address, fingerprint=body.fingerprint, publication=body.publication, user=user
        )
    except RegistrationRefused as exc:
        return _refused(exc)
    await session.commit()
    # After the commit, so a reader in between cannot cache the state before it.
    publication.invalidate()
    blah2_poller.refresh()
    return {"node_id": registration.node.node_id, "epoch": registration.radar.epoch, "trust_state": "probation"}
