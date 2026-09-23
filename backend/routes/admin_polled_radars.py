"""Polled radars as administrators see them, and the trust decision on each.

Its own module rather than a routes/admin.py addition, as
routes/admin_infrastructure.py is. The decision is recorded in node_events
rather than the admin event log, which is neither durable nor complete.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.users import get_async_session, require_admin
from services import graduation, probation, publication

router = APIRouter(prefix="/api/admin", tags=["admin"])


class TrustDecision(BaseModel):
    trust_state: graduation.TrustState
    # The epoch the administrator was shown.
    epoch: int = Field(ge=1)


@router.get("/polled-radars")
async def admin_polled_radars(
    _admin=Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    return {"probation_enabled": probation.enabled(), "radars": await graduation.listing(session)}


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
