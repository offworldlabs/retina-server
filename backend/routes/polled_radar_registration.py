"""An owner registering a stock blah2 radar: probe it, then register it. Moving
one to a new address is the same two steps.

Absent (404) wherever POLLED_RADAR_REGISTRATION_ENABLED is not exactly `1`, as
though unmounted, so a deployment that takes no registrations offers nothing to
probe with. services/polled_registration.py decides; this module speaks HTTP.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.nodes import Node
from core.users import get_async_session, get_current_user
from services import blah2_poller, node_pipeline, polled_registration, probation, publication
from services.polled_registration import RegistrationRefused

logger = logging.getLogger(__name__)


class PolledRadarProbeRequest(BaseModel):
    # parse_endpoint refuses anything longer with a message of its own.
    address: str = Field(max_length=2048)


class PolledRadarAddressRequest(PolledRadarProbeRequest):
    # The declaration the owner confirmed, as the probe reported it.
    fingerprint: str = Field(min_length=64, max_length=64)


class PolledRadarRegisterRequest(PolledRadarAddressRequest):
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
    await _join_pipeline(session, registration.node)
    blah2_poller.refresh()
    return {"node_id": registration.node.node_id, "epoch": registration.radar.epoch, "trust_state": "probation"}


async def _join_pipeline(session: AsyncSession, node: Node) -> None:
    """Put the radar where a restart's priming would, so its owner's list and map
    show it before its first frame, which a stalled or unreachable radar never sends.
    """
    try:
        await node_pipeline.register_with_pipeline(session, node)
    except Exception:
        # Registered all the same: the poller hands it over on its first frame.
        logger.exception("polled radar %s: could not hand it to the pipeline", node.node_id)


@router.post("/{node_id}/probe")
async def probe_polled_radar_address(
    node_id: str, body: PolledRadarProbeRequest, request: Request, session: AsyncSession = Depends(get_async_session)
):
    """What the owner's radar declares at a new address, for them to confirm. Saves nothing."""
    user = await get_current_user(request)
    try:
        probed = await polled_registration.probe_new_address(session, node_id, body.address, user["id"])
    except RegistrationRefused as exc:
        return _refused(exc)
    return probed.public()


@router.put("/{node_id}/address")
async def move_polled_radar(
    node_id: str, body: PolledRadarAddressRequest, request: Request, session: AsyncSession = Depends(get_async_session)
):
    """Move the owner's radar to a new address, if it still declares what they confirmed there."""
    user = await get_current_user(request)
    try:
        radar = await polled_registration.change_address(
            session, node_id, raw=body.address, fingerprint=body.fingerprint, user=user
        )
    except RegistrationRefused as exc:
        return _refused(exc)
    await session.commit()
    # After the commit, as above: a radar moved to another host is back on probation.
    probation.invalidate()
    publication.invalidate()
    blah2_poller.refresh()
    return {"node_id": radar.node_id, "epoch": radar.epoch, "trust_state": radar.trust_state}
