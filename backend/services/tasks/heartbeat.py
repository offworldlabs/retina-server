"""Optional heartbeat ping.

If HEARTBEAT_URL is set, pings it on a fixed interval. No environment sets
it: outside-in probing is done by DigitalOcean Uptime (see
claude-shared/docs/runbooks/uptime-monitoring.md), so this stays dormant.
Disabled (no-op) when HEARTBEAT_URL is unset.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

HEARTBEAT_URL = os.getenv("HEARTBEAT_URL", "")
HEARTBEAT_INTERVAL_S = float(os.getenv("HEARTBEAT_INTERVAL_S", "60"))


async def heartbeat_task() -> None:
    if not HEARTBEAT_URL:
        logger.info("Heartbeat disabled (HEARTBEAT_URL unset)")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await client.get(HEARTBEAT_URL)
            except Exception:
                logger.warning("Heartbeat ping failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
