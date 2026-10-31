"""The per-address limit on the map's archive reads.

The archive routes answer anyone, and every answer is file work: Parquet
footers and row groups for the map's tracks, availability and detections.
main.py puts those routers, and whatever later ones it adds to that tuple,
behind `limit_archive_read`, so all of them spend one budget per client
address. The address is the one services/client_source.py derives, the
transport peer after uvicorn's trusted-proxy handling, never a forwarding
header, so a caller cannot widen its budget by naming somebody else. The Data
Explorer's listing and downloads are not counted here: its scans and download
scripts read in bursts, so they stay behind nginx's per-IP `api` zone alone.
That zone bounds requests per second at the edge; this limit bounds how much
archive reading one address can ask of this process for the map.

Two fixed windows per address, both of which must have room: ARCHIVE_RATE_BURST
reads per 10 s and ARCHIVE_RATE_PER_MINUTE per 60 s (config/constants.py, 30
and 120 unless set). A read refused by one window spends neither. 0 turns a
window off, and with both off nothing is counted or stored. The map's archive
mode spends two or three reads per window change, about four a minute while it
plays at its fastest, one to three on a drag of the scrubber across the loaded
window and at most six in a minute of dragging it back and forth, paused or
playing, so the defaults leave a person room to jump around and stop a script
walking the map's archive at full speed; backend/.env.example gives the counts.

A refusal is a 429 whose Retry-After is the whole seconds until every full
window has room again, at least 1, and whose body names that wait and nothing
about the caller.

The counters are services/node_rate_limits.FixedWindowCounters, bounded the
way registration's are, because the key is something a caller can manufacture
(a /64 costs next to nothing): a full map refuses only the sources it does not
already hold, and never evicts a live counter, since evicting one would clear
that source's limit. The bound is MAX_ARCHIVE_SOURCES sources, a counter per
window each; reclaiming expired counters runs at most once a second, and only
while the map is full, so a request costs a dict lookup or two.
"""

import math
import threading
import time
from typing import Any

from fastapi import HTTPException, Request

from config.constants import archive_rate_limits
from services.client_source import client_source
from services.node_rate_limits import Clock, FixedWindowCounters

BURST_WINDOW_S = 10
MINUTE_WINDOW_S = 60

# Sources, not counters: the map holds up to this many addresses (an IPv6 /64
# counting as one), each with a counter per window turned on, so 100,000
# counters at the defaults. Full of IPv6 /64 sources, the longest keys, that
# measured 22 MB under tracemalloc on CPython 3.12. The bound is a security parameter
# rather than a traffic estimate, as MAX_REGISTRATION_KEYS's is: it is what a
# caller with many addresses has to get admitted inside one window (a minute
# at the defaults) before addresses the map does not hold are refused until
# counters expire, and real readers are far fewer. A source already held is always admitted to its own
# budget, which can carry the map a little past the bound (see
# FixedWindowCounters.admit).
MAX_ARCHIVE_SOURCES = 50_000

_MAP_LABEL = "archive rate limit map"


class ArchiveRateLimiter:
    """One budget per client source, over the map's archive reads."""

    def __init__(
        self,
        burst: int,
        per_minute: int,
        *,
        clock: Clock = time.monotonic,
        max_sources: int = MAX_ARCHIVE_SOURCES,
    ) -> None:
        self._windows = tuple(
            (limit, window_s)
            for limit, window_s in ((burst, BURST_WINDOW_S), (per_minute, MINUTE_WINDOW_S))
            if limit > 0
        )
        self._counters = (
            FixedWindowCounters(clock, max_sources * len(self._windows), refuse_when_full=True, label=_MAP_LABEL)
            if self._windows
            else None
        )

    @classmethod
    def from_environment(cls, clock: Clock = time.monotonic) -> "ArchiveRateLimiter":
        """A limiter with the limits the environment sets now."""
        burst, per_minute = archive_rate_limits()
        return cls(burst, per_minute, clock=clock)

    @property
    def tracked_counters(self) -> int:
        return 0 if self._counters is None else self._counters.tracked_counters

    def admit(self, source: str) -> int | None:
        """Spend one read for this source, or return the Retry-After in whole
        seconds, at least 1.

        None means the read is admitted and spent; call this once per request,
        not as a predicate to poll.
        """
        if self._counters is None:
            return None
        wait_s = self._counters.admit([((source, window_s), limit, window_s) for limit, window_s in self._windows])
        if wait_s is None:
            return None
        return max(1, math.ceil(wait_s))


# A process wants exactly one, since the counters are the budget and a second
# instance is a second allowance. Built at first use rather than at import, so
# the limits are read after main.py has loaded backend/.env.
_limiter: ArchiveRateLimiter | None = None
_limiter_lock = threading.Lock()


def archive_rate_limiter() -> ArchiveRateLimiter:
    """The process's limiter, built from the environment on the first call."""
    global _limiter
    limiter = _limiter
    if limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = ArchiveRateLimiter.from_environment()
            limiter = _limiter
    return limiter


def _reset_for_tests() -> None:
    """Drop the limiter, so the next read builds it from the environment again."""
    global _limiter
    _limiter = None


async def limit_archive_read(request: Request) -> None:
    """Spend one read from the caller's budget, or refuse with a 429.

    A coroutine, so FastAPI runs it on the event loop: the counters are a dict
    behind a lock held for microseconds, not worth a hop to the threadpool.
    """
    retry_after_s = archive_rate_limiter().admit(client_source(request))
    if retry_after_s is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many archive requests from this address; try again in {retry_after_s} s.",
            headers={"Retry-After": str(retry_after_s)},
        )


# For include_router(responses=...), beside the dependency, so the API
# reference documents the refusal on every route that can give it.
RATE_LIMITED_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: {
        "description": "Too many archive requests from this address. The map's archive routes spend "
        "from one budget per client address, counted over 10 seconds and over a minute. Wait "
        "`Retry-After` seconds, then ask again.",
        "headers": {
            "Retry-After": {
                "description": "Whole seconds until every full window has room again.",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            }
        },
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string", "description": "The refusal, naming the wait."}},
                    "required": ["detail"],
                }
            }
        },
    }
}
