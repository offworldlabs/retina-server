"""Claim candidates for real nodes from adsb-service, where no node sends its own ADS-B.

Claiming binds a detection only against a transponder fix, and a hardware node
that reports no ADS-B of its own leaves it nothing to bind against, so its
coverage polygon never learns.  This task polls adsb-service (ADSBHub traffic at
adsb.retina.fm) over the regions the real nodes occupy and keeps each region's
answer in state.adsb_fallback, under its lattice cell, tagged world "real".
Runs only where ADSB_FALLBACK_ENABLED is exactly `1`.

Each record is stamped with its own capture time (the answer's ``now`` less
``seen_pos``), never the poll's: calibration takes a fix no older than
CAL_MAX_ADSB_AGE_S at the frame instant, and a poll-time stamp would pass fixes
a minute old as current.

The service allows 2 requests a second per IP.  Requests are spaced to that
rate, so a cycle asks at most INTERVAL_S / REQUEST_SPACING_S regions; one that
cannot finish inside its interval, or is refused with a 429, stops there and
logs the regions it left unasked, and the next cycle starts with them.
"""

import asyncio
import logging
import os
import time
from typing import NamedTuple

from clients.adsb_service import AdsbServiceClient, RateLimited
from config.constants import ADSB_FALLBACK_INTERVAL_S
from core import state
from services.adsb_regions import Region, regions_for_nodes
from services.known_claiming import KNOWN_CLAIM_MAX_FIX_AGE_S

log = logging.getLogger(__name__)

ENABLED_ENV = "ADSB_FALLBACK_ENABLED"
INTERVAL_S = ADSB_FALLBACK_INTERVAL_S
# The service's sustained limit is 2 requests a second per IP.
REQUEST_SPACING_S = 0.5
# An older fix can never be claimed against.
MAX_FIX_AGE_S = KNOWN_CLAIM_MAX_FIX_AGE_S
# The longest a 429's Retry-After holds the poller: every candidate has aged
# past MAX_FIX_AGE_S by then, so a longer wait only stops claiming for longer.
MAX_RETRY_AFTER_S = 60.0

_TASK = "adsb_fallback"


def enabled() -> bool:
    return os.getenv(ENABLED_ENV) == "1"


_regions_key: tuple = ()
_regions: list[Region] = []


def real_node_regions() -> list[Region]:
    """Query regions over every connected, positioned hardware node.

    Synthetic nodes are left out: their echoes are of simulated aircraft, which
    claiming's world gate keeps apart from these.  Recomputed only when the
    positions change, so regions_for_nodes's cap warning is not repeated every
    cycle.
    """
    global _regions_key, _regions
    positions = []
    for info in list(state.connected_nodes.values()):
        if info.get("status") == "disconnected" or info.get("is_synthetic", False):
            continue
        cfg = info.get("config") or {}
        lat, lon = cfg.get("rx_lat"), cfg.get("rx_lon")
        if lat is not None and lon is not None:
            positions.append((lat, lon))
    key = tuple(sorted(positions))
    if key != _regions_key:
        _regions_key, _regions = key, regions_for_nodes(positions)
    return _regions


def _as_candidate(row: dict) -> dict:
    """A parsed answer row as a seeding-provider record, derived fields included."""
    rec = {
        "hex": row["hex"],
        "flight": row["flight"],
        "lat": row["lat"],
        "lon": row["lon"],
        "alt_baro": row["alt_baro"],
        "gs": row["gs"],
        "track": row["track"],
        "last_seen_ms": row["captured_ms"],
        "world": "real",
        "source": "adsb_service",
    }
    rec.update(state.adsb_derived_fields(rec))
    return rec


class Cycle(NamedTuple):
    # The extra wait a 429 asked for, up to MAX_RETRY_AFTER_S; 0 when none did.
    retry_after_s: float
    # Where in the regions the next cycle should start: the first one this
    # cycle left unasked, or 0 once every region was asked.
    resume_at: int


async def run_cycle(
    client, regions: list[Region], *, start: int = 0, clock=time.monotonic, sleep=asyncio.sleep
) -> Cycle:
    """Ask every region once, beginning at ``regions[start]``, and publish the result."""
    started = clock()
    order = regions[start:] + regions[:start]
    answers: dict[tuple[int, int], list[dict]] = {}
    answered = 0
    unasked: list[Region] = []
    reason = ""
    retry_after_s = 0.0
    for i, region in enumerate(order):
        if i:
            await sleep(REQUEST_SPACING_S)
        if clock() - started >= INTERVAL_S:
            unasked, reason = order[i:], f"the {INTERVAL_S:.0f}s interval ran out"
            break
        try:
            rows = await client.fetch_point(region.lat, region.lon, region.radius_nm)
        except RateLimited as exc:
            unasked, reason = order[i:], f"429, retry after {exc.retry_after_s:.0f}s"
            retry_after_s = min(exc.retry_after_s, MAX_RETRY_AFTER_S)
            break
        except Exception as exc:
            state.bump_counter("adsb_fallback_region_errors")
            log.warning("adsb-service: region %s failed: %s", region.name, exc)
            continue
        answered += 1
        answers[(region.row, region.col)] = rows
    if unasked:
        state.bump_counter("adsb_fallback_truncated_cycles")
        log.warning(
            "adsb-service: cycle cut short (%s); %d of %d region(s) not asked: %s",
            reason,
            len(unasked),
            len(regions),
            ", ".join(r.name for r in unasked),
        )
    elif clock() - started > INTERVAL_S:
        log.warning("adsb-service: cycle overran its %.0fs interval (%.1fs)", INTERVAL_S, clock() - started)

    # Records this cycle did not refresh are kept until too old to claim: their
    # region may simply have gone unasked, and each carries its own true age.  A
    # cell no region occupies any more is dropped whole.
    now_s = time.time()
    oldest_ms = (now_s - MAX_FIX_AGE_S) * 1000.0
    store = {}
    for region in regions:
        cell = (region.row, region.col)
        kept = {h: rec for h, rec in state.adsb_fallback.get(cell, {}).items() if rec["last_seen_ms"] >= oldest_ms}
        for row in answers.get(cell, ()):
            if row["captured_ms"] >= max(oldest_ms, kept.get(row["hex"], {}).get("last_seen_ms", 0)):
                kept[row["hex"]] = _as_candidate(row)
        if kept:
            store[cell] = kept
    state.adsb_fallback = store
    if answered:
        state.task_last_success[_TASK] = now_s
    return Cycle(retry_after_s, regions.index(unasked[0]) if unasked else 0)


async def adsb_fallback_task() -> None:
    """The lifespan's task: poll every INTERVAL_S, or return at once where it is off."""
    if not enabled():
        log.info("adsb-service is not polled for claim candidates (%s is not 1)", ENABLED_ENV)
        return
    log.info("Polling adsb-service for claim candidates every %.0fs (%s=1)", INTERVAL_S, ENABLED_ENV)
    client = AdsbServiceClient()
    resume_at = 0
    try:
        while True:
            started = time.monotonic()
            retry_after_s = 0.0
            try:
                regions = real_node_regions()
                if regions:
                    cycle = await run_cycle(client, regions, start=resume_at % len(regions))
                    retry_after_s, resume_at = cycle
                else:
                    state.adsb_fallback = {}
                    state.task_last_success[_TASK] = time.time()
            except Exception:
                state.bump_task_error(_TASK)
                log.exception("adsb-service poll failed")
            await asyncio.sleep(max(0.0, INTERVAL_S - (time.monotonic() - started)) + retry_after_s)
    finally:
        await client.aclose()
