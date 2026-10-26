"""What the public archive routes share: the query-parameter rules (the fleet
filter, the time instants and the bounding box, each answered with a 422 when
malformed), and the thread pool the tracks and ADS-B routes read their stores on."""

import asyncio
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException

from services import tracks_store

Fleet = Literal["real", "synthetic", "all"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_MS = re.compile(r"-?[0-9]{1,15}")
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def invalid_parameter(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def parse_instant_ms(value: str, name: str) -> int:
    """Epoch ms from integer ms or ISO-8601 (Z or an offset; none means UTC; a date is its midnight)."""
    text = value.strip()
    try:
        if _EPOCH_MS.fullmatch(text):
            moment = _EPOCH + timedelta(milliseconds=int(text))
        else:
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
        # Rounded up: the first whole millisecond at or after the instant.
        return -((_EPOCH - moment) // timedelta(milliseconds=1))
    except (ValueError, OverflowError):
        raise invalid_parameter(f"{name} must be epoch milliseconds or an ISO-8601 time") from None


def parse_bbox(value: str) -> tracks_store.BBox:
    parts = value.split(",")
    message = "bbox must be south,west,north,east in degrees, with south <= north"
    if len(parts) != 4 or not all(_NUMBER.fullmatch(part.strip()) for part in parts):
        raise invalid_parameter(message)
    south, west, north, east = (float(part) for part in parts)
    # NaN and infinities fail these comparisons too.
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        raise invalid_parameter(message)
    return tracks_store.BBox(south, west, north, east)


# A cold day downloads from the object store while its readers wait, which can
# take seconds; on the loop's default executor, shared with every other
# asyncio.to_thread caller, a few visitors to one cold day would stall them all.
# /api/data/tracks, its availability and /api/data/adsb share these threads, so
# a cold day of either archive holds one of them while it loads.
STORE_WORKERS = 4
_store_pool: ThreadPoolExecutor | None = None
_store_pool_lock = threading.Lock()


def _store_executor() -> ThreadPoolExecutor:
    global _store_pool
    with _store_pool_lock:
        if _store_pool is None:
            _store_pool = ThreadPoolExecutor(max_workers=STORE_WORKERS, thread_name_prefix="tracks-archive")
        return _store_pool


async def in_store_pool(work: Callable[[], bytes]) -> bytes:
    """Runs a route's store work on the archives' own threads, created on first use."""
    return await asyncio.get_running_loop().run_in_executor(_store_executor(), work)
