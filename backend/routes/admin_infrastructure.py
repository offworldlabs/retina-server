"""Admin Infrastructure page data.

Its own module rather than a routes/admin.py addition: that file is the whole
admin surface already, and this route depends on an outbound API none of it does.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from config.constants import INFRASTRUCTURE_BUILD_TIMEOUT_S
from core.users import require_admin
from services import infrastructure

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/infrastructure")
async def admin_infrastructure(_user=Depends(require_admin)):
    try:
        return await asyncio.wait_for(infrastructure.snapshot(), timeout=INFRASTRUCTURE_BUILD_TIMEOUT_S)
    except TimeoutError:
        # wait_for cancels the build, and with it the requests still in flight;
        # a slow DigitalOcean must not hold the admin past the dashboard's abort.
        last_good = infrastructure.cached_snapshot()
        if last_good is not None:
            return {**last_good, "stale": True}
        raise HTTPException(status_code=503, detail="infrastructure snapshot timed out") from None
