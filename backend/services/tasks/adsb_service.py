"""Bounded polling of the first-party readsb API for real receiver regions."""

import asyncio
import logging
import os
import time

import httpx

from core import state
from services.adsb_regions import regions_for_nodes
from services.adsb_truth import readsb_references

log = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


async def fetch_region(client, base_url, region) -> dict:
    url = f"{base_url}/v2/lat/{region.lat}/lon/{region.lon}/dist/{region.radius_nm}"
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("ADS-B service response exceeds size limit")
    import json

    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError("ADS-B service envelope must be an object")
    return readsb_references(payload, time.time())


async def adsb_service_task():
    """An unset URL disables network I/O; public source failures stay isolated."""
    base_url = os.getenv("ADSB_SERVICE_URL", "").rstrip("/")
    if not base_url:
        return
    if not base_url.startswith("https://"):
        log.error("ADSB_SERVICE_URL must use https")
        return
    async with httpx.AsyncClient(timeout=5, headers={"User-Agent": "RETINA-validation/1.0"}) as client:
        while True:
            positions = [
                (info["config"].get("rx_lat"), info["config"].get("rx_lon"))
                for info in list(state.connected_nodes.values())
                if not info.get("is_synthetic") and info.get("status") != "disconnected" and info.get("config")
            ]
            for region in regions_for_nodes(positions):
                try:
                    records = await fetch_region(client, base_url, region)
                    # Merge overlapping regions by capture time, never poll time.
                    for hexn, rec in records.items():
                        prev = state.service_adsb_cache.get(hexn)
                        if prev is None or rec["timestamp_ms"] >= prev["timestamp_ms"]:
                            state.service_adsb_cache[hexn] = rec
                    state.task_last_success["adsb_service"] = time.time()
                except (httpx.HTTPError, ValueError, TypeError):
                    state.bump_task_error("adsb_service")
                    log.warning("ADS-B service region %s unavailable", region.name)
                # The service admits 2 requests/s per client; sequential polling
                # also bounds concurrency and permits cancellation at every hop.
                await asyncio.sleep(0.55)
            cutoff = (time.time() - 60) * 1000
            for hexn, rec in list(state.service_adsb_cache.items()):
                if rec["timestamp_ms"] < cutoff:
                    state.service_adsb_cache.pop(hexn, None)
            await asyncio.sleep(3)
