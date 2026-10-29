"""Where production's registry mirror lands (services/registry_mirror.py).

A write path into the node registry, so it is guarded twice over. It is mounted
only where RETINA_ENV names a receiving environment (test, staging, dev): an
unset RETINA_ENV is production's, and production is the source, whose registry
nothing writes from outside. And it answers only the caller holding
this environment's RADAR_API_KEY, the key production's detection mirror already
sends here; with no key set it answers no one, unlike the bulk detection
endpoint, which an unset key leaves open.
"""

import hmac
import os
from collections.abc import Mapping

from fastapi import APIRouter, Body, Header, HTTPException

from core.users import async_session_maker
from services import registry_mirror

router = APIRouter()


def receiving_enabled(env: Mapping[str, str]) -> bool:
    """Whether this environment takes a pushed registry: named receivers only."""
    return (env.get("RETINA_ENV") or "").strip().lower() in ("test", "staging", "dev")


@router.post(registry_mirror.PATH, include_in_schema=False)
async def registry_sync(
    chunk: dict = Body(...),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    key = os.getenv("RADAR_API_KEY", "")
    if not key or not hmac.compare_digest(x_api_key.encode(), key.encode()):
        # As an unknown route would: a caller without the key learns nothing.
        raise HTTPException(status_code=404, detail="Not Found")
    async with async_session_maker() as session:
        return await registry_mirror.apply(session, chunk)
