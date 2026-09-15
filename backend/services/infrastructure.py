"""The admin Infrastructure page's data: DigitalOcean uptime checks and droplet metrics.

A view over what claude-shared docs/runbooks/uptime-monitoring.md configures,
never the source of an alert: DigitalOcean and Cloudflare email whether or not
this loads. One item failing costs that item and an entry in `errors`, not the
page.
"""

import asyncio
import logging
import math
import time

import httpx

from clients import digitalocean as do
from config.constants import INFRASTRUCTURE_CACHE_TTL_S

logger = logging.getLogger(__name__)

WINDOW_S = 24 * 3600
ROOT_MOUNT = "/"

_cache: tuple[float, dict] | None = None
# Single-flight: concurrent cold requests share one build rather than each
# opening its own two dozen connections.
_build_lock = asyncio.Lock()


def _describe(exc: BaseException) -> str:
    """What failed, in a form that separates a 401 from a 429 from a 500. Never a URL, never the token."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return exc.__class__.__name__


def _reset_for_tests() -> None:
    global _cache, _build_lock
    _cache = None
    # A fresh lock per test: a Lock binds to the loop it is first awaited on.
    _build_lock = asyncio.Lock()


def cpu_utilisation_series(result: list[dict]) -> list[list]:
    """Busy percent per interval from DigitalOcean's cumulative per-mode CPU counters."""
    by_mode = {s.get("metric", {}).get("mode"): {int(t): float(v) for t, v in (s.get("values") or [])} for s in result}
    idle = by_mode.get("idle")
    if not idle:
        return []
    out = []
    stamps = sorted(idle)
    for opened, closed in zip(stamps, stamps[1:]):
        # A mode missing either endpoint makes the interval's total a sum over a
        # different span per mode, so skip the interval rather than mis-scale it.
        if any(opened not in mode or closed not in mode for mode in by_mode.values()):
            continue
        total = sum(mode[closed] - mode[opened] for mode in by_mode.values())
        if total <= 0:
            continue
        busy = 1.0 - (idle[closed] - idle[opened]) / total
        if not math.isfinite(busy):  # a NaN sample would otherwise clamp to 0%
            continue
        out.append([closed, round(min(1.0, max(0.0, busy)) * 100, 1)])
    return out


def used_fraction_series(part: list[list], total: list[list]) -> list[list]:
    """(1 - part/total) as percent, aligned by timestamp; a timestamp missing either side is skipped."""
    totals = {int(t): float(v) for t, v in total}
    out = []
    for t, v in part:
        whole = totals.get(int(t))
        if not whole:
            continue
        used = (1.0 - float(v) / whole) * 100
        if not math.isfinite(used):  # a NaN sample is a gap, not a reading
            continue
        out.append([int(t), round(used, 1)])
    return out


def _root_filesystem(series: dict) -> bool:
    return series.get("metric", {}).get("mountpoint") == ROOT_MOUNT


def _first_values(result: list[dict], keep=None) -> list[list]:
    for series in result:
        if keep is None or keep(series):
            return series.get("values") or []
    return []


def _last_value(series: list[list]):
    return series[-1][1] if series else None


def summarise_check(check: dict, state: dict) -> dict:
    regions = {}
    for name, rs in (state.get("regions") or {}).items():
        regions[name] = {
            "status": rs.get("status"),
            "since": rs.get("status_changed_at"),
            "uptime_30d": rs.get("thirty_day_uptime_percentage"),
        }
    statuses = {r["status"] for r in regions.values()}
    if not statuses:
        status = "UNKNOWN"
    elif statuses == {"UP"}:
        status = "UP"
    elif statuses == {"DOWN"}:
        status = "DOWN"
    else:
        status = "MIXED"
    uptimes = [r["uptime_30d"] for r in regions.values() if r["uptime_30d"] is not None]
    return {
        "id": check.get("id"),
        "name": check.get("name"),
        "target": check.get("target"),
        "enabled": check.get("enabled", True),
        "status": status,
        "uptime_30d": min(uptimes) if uptimes else None,
        "regions": regions,
        "last_outage": state.get("previous_outage") or None,
    }


async def _metrics(names: tuple[str, ...], host_id: int, start: int, end: int) -> list[list[dict]]:
    """The raw series a derived metric needs, concurrently. Either half failing fails the pair."""
    results = await asyncio.gather(
        *(do.droplet_metric(name, host_id, start, end) for name in names), return_exceptions=True
    )
    for result in results:  # raise here rather than leave a sibling request running
        if isinstance(result, BaseException):
            raise result
    return results


async def summarise_droplet(droplet: dict, now: int) -> tuple[dict, list[str]]:
    host_id = droplet["id"]
    start = now - WINDOW_S
    name = droplet.get("name")
    errors: list[str] = []
    failed: set[str] = set()

    async def cpu_series() -> list[list]:
        (cpu,) = await _metrics(("cpu",), host_id, start, now)
        return cpu_utilisation_series(cpu)

    async def memory_series() -> list[list]:
        free, total = await _metrics(("memory_available", "memory_total"), host_id, start, now)
        return used_fraction_series(_first_values(free), _first_values(total))

    async def disk_series() -> list[list]:
        free, total = await _metrics(("filesystem_free", "filesystem_size"), host_id, start, now)
        return used_fraction_series(_first_values(free, _root_filesystem), _first_values(total, _root_filesystem))

    async def derive(metric: str, build) -> list[list]:
        # Fetch and derivation share one guard: a malformed sample costs the same
        # one metric that a failed GET does, not the droplet.
        try:
            return await build()
        except Exception as exc:
            errors.append(f"{name}: {metric} unavailable ({_describe(exc)})")
            failed.add(metric)
            return []

    cpu, memory, disk = await asyncio.gather(
        derive("cpu", cpu_series), derive("memory", memory_series), derive("disk", disk_series)
    )

    # An off droplet, a missing metrics agent and a metrics API returning nothing
    # are all a blank chart otherwise. A metric that failed outright already said so.
    for metric, series in (("cpu", cpu), ("memory", memory), ("disk", disk)):
        if not series and metric not in failed:
            errors.append(f"{name}: {metric} empty")

    summary = {
        "id": host_id,
        "name": name,
        "vcpus": droplet.get("vcpus"),
        "memory_mb": droplet.get("memory"),
        "disk_gb": droplet.get("disk"),
        "status": droplet.get("status"),
        "cpu_pct": _last_value(cpu),
        "memory_pct": _last_value(memory),
        "disk_pct": _last_value(disk),
        "series": {"cpu": cpu, "memory": memory, "disk": disk},
    }
    return summary, errors


async def _check_summary(check: dict) -> dict:
    return summarise_check(check, await do.check_state(check["id"]))


async def _collect_checks() -> tuple[list[dict], list[str]]:
    """Every uptime check with its state. The listing failing costs them all; one state costs one check."""
    try:
        raw = await do.list_checks()
    except Exception as exc:
        return [], [f"uptime checks unavailable ({_describe(exc)})"]
    checks: list[dict] = []
    errors: list[str] = []
    states = await asyncio.gather(*(_check_summary(check) for check in raw), return_exceptions=True)
    for check, summary in zip(raw, states):
        if isinstance(summary, BaseException):
            errors.append(f"check {check.get('name')}: state unavailable ({_describe(summary)})")
        else:
            checks.append(summary)
    return checks, errors


async def _collect_droplets(tag: str, now: int) -> tuple[list[dict], list[str]]:
    """Every tagged droplet with its metrics. The listing failing costs them all; one droplet costs one."""
    try:
        raw = await do.list_droplets(tag)
    except Exception as exc:
        return [], [f"droplets unavailable ({_describe(exc)})"]
    droplets: list[dict] = []
    errors: list[str] = []
    summaries = await asyncio.gather(*(summarise_droplet(droplet, now) for droplet in raw), return_exceptions=True)
    for droplet, summary in zip(raw, summaries):
        if isinstance(summary, BaseException):
            errors.append(f"droplet {droplet.get('name')}: unavailable ({_describe(summary)})")
            continue
        droplets.append(summary[0])
        errors.extend(summary[1])
    return droplets, errors


async def build_snapshot(now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    tag = do.droplet_tag()
    if not do.token():
        return {
            "configured": False,
            "reason": f"{do.TOKEN_ENV} is not set",
            "fetched_at": now,
            "tag": tag,
            "stale": False,
            "checks": [],
            "droplets": [],
            "errors": [],
        }
    # The two pipelines share nothing, so a cold build costs the slower of them
    # rather than both against the route's deadline.
    (checks, check_errors), (droplets, droplet_errors) = await asyncio.gather(
        _collect_checks(), _collect_droplets(tag, now)
    )
    errors = check_errors + droplet_errors
    if errors:
        logger.warning("infrastructure snapshot degraded: %s", "; ".join(errors))
    return {
        "configured": True,
        "reason": None,
        "fetched_at": now,
        "tag": tag,
        "stale": False,
        "checks": checks,
        "droplets": droplets,
        "errors": errors,
    }


def _fresh() -> dict | None:
    if _cache is not None and time.time() - _cache[0] < INFRASTRUCTURE_CACHE_TTL_S:
        return _cache[1]
    return None


def cached_snapshot() -> dict | None:
    """The last build at any age, for a caller that would otherwise answer nothing."""
    return _cache[1] if _cache is not None else None


async def snapshot() -> dict:
    """The cached snapshot. An unconfigured answer is not cached, so setting the token shows at once."""
    global _cache
    fresh = _fresh()
    if fresh is not None:
        return fresh
    async with _build_lock:
        # Whoever held the lock has just filled the cache; take their build.
        fresh = _fresh()
        if fresh is not None:
            return fresh
        now = time.time()
        snap = await build_snapshot(int(now))
        if snap["configured"]:
            _cache = (now, snap)
        return snap
