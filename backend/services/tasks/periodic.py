"""Periodic background tasks: archive flush, archive lifecycle, reputation, ADS-B truth fetch."""

import asyncio
import logging
import time

import httpx

from config.constants import (
    ADSB_BACKOFF_S,
    ADSB_TRUTH_INTERVAL_S,
    ARCHIVE_FLUSH_INTERVAL_S,
    ARCHIVE_LIFECYCLE_INTERVAL_S,
    EXTERNAL_ADSB_MAX_AGE_S,
    OPENSKY_BUFFER_DEG,
    REPUTATION_INTERVAL_S,
    XVAL_MAX_AGE_S,
)
from core import state
from services.frame_processor import flush_all_archive_buffers
from services.geo import haversine_km

_opensky_client: httpx.AsyncClient | None = None
_adsb_lol_client: object | None = None
# node_id → newest sample timestamp already cross-validated, so a sample is
# charged at most once.  In memory only: losing it on restart re-judges at
# most the last ten samples, and the age gate discards them anyway.
_xval_judged_through: dict[str, int] = {}


def _reset_for_tests() -> None:
    """Restore this module's private state to boot values.  Tests only."""
    _xval_judged_through.clear()


async def close_http_clients() -> None:
    """Shutdown hook: close the pooled clients (they had no close path)."""
    global _opensky_client, _adsb_lol_client
    if _opensky_client is not None:
        try:
            await _opensky_client.aclose()
        except Exception:
            pass
        _opensky_client = None
    if _adsb_lol_client is not None:
        closer = getattr(_adsb_lol_client, "aclose", None) or getattr(_adsb_lol_client, "close", None)
        if closer is not None:
            try:
                res = closer()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        _adsb_lol_client = None


async def archive_flush_task():
    """Periodically flush batched detection archives to disk/B2."""
    while True:
        await asyncio.sleep(ARCHIVE_FLUSH_INTERVAL_S)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, flush_all_archive_buffers)
            state.task_last_success["archive_flush"] = time.time()
        except Exception:
            state.task_error_counts["archive_flush"] += 1
            logging.exception("Archive batch flush failed")


async def archive_lifecycle_task():
    """Periodically offload old archives to R2 and delete expired local files."""
    from services.tasks.archive_lifecycle import run_archive_lifecycle

    while True:
        await asyncio.sleep(ARCHIVE_LIFECYCLE_INTERVAL_S)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_archive_lifecycle)
            state.task_last_success["archive_lifecycle"] = time.time()
        except Exception:
            state.task_error_counts["archive_lifecycle"] += 1
            logging.exception("Archive lifecycle failed")


async def reputation_evaluator():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(REPUTATION_INTERVAL_S)
        try:
            await loop.run_in_executor(
                None,
                state.node_analytics.evaluate_reputations,
            )
            state.task_last_success["reputation_evaluator"] = time.time()
        except Exception:
            state.task_error_counts["reputation_evaluator"] += 1
            logging.exception("Reputation evaluation failed")


async def prune_synthetic_nodes():
    """Periodically remove old synthetic/test nodes that have been disconnected."""
    # Test nodes (e2e-, synth-, test- prefixes) disconnected >7 days old get pruned
    # to avoid accumulating stale state in memory across CI/CD cycles.
    PRUNE_INTERVAL_S = 6 * 3600  # Every 6 hours
    MAX_AGE_DISCONNECTED_S = 7 * 86400  # 7 days

    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        try:
            now = time.time()
            pruned = []
            with state.connected_nodes_lock:
                to_remove = []
                for node_id, info in state.connected_nodes.items():
                    # Only prune synthetic/test nodes
                    if not any(node_id.startswith(p) for p in ("synth-", "e2e-", "test-")):
                        continue
                    # Only prune if disconnected
                    if info.get("status") != "disconnected":
                        continue
                    # Age from the *disconnect*, not from first_seen — an
                    # 8-day-connected node used to be pruned one second after
                    # a blip.  Entries marked disconnected before the
                    # disconnect timestamp existed (or restored from an old
                    # snapshot) have no disconnected_ts; fall back to
                    # first_seen_ts so they still age out eventually.
                    disconnected_at = info.get("disconnected_ts") or info.get("first_seen_ts", now)
                    if now - disconnected_at > MAX_AGE_DISCONNECTED_S:
                        to_remove.append(node_id)
                        pruned.append(node_id)
                for node_id in to_remove:
                    del state.connected_nodes[node_id]

            if pruned:
                logging.info("Pruned %d old synthetic nodes: %s", len(pruned), pruned[:5])
            state.task_last_success["prune_synthetic_nodes"] = time.time()
        except Exception:
            state.task_error_counts["prune_synthetic_nodes"] += 1
            logging.exception("Node pruning failed")


def prune_external_adsb_cache() -> int:
    """Drop external ADS-B entries past EXTERNAL_ADSB_MAX_AGE_S.  Returns the count.

    Both fetch paths replace state.external_adsb_cache wholesale, so nothing
    else ever removes an entry: without this a stalled feed keeps serving its
    last good snapshot for as long as the outage lasts.  Unstamped entries go
    too — only a pre-upgrade snapshot has them, and an entry that cannot be
    aged cannot be trusted.
    """
    cutoff_ms = (time.time() - EXTERNAL_ADSB_MAX_AGE_S) * 1000
    stale = [
        h for h, e in state.external_adsb_cache.items() if not e.get("last_seen_ms") or e["last_seen_ms"] < cutoff_ms
    ]
    for h in stale:
        state.external_adsb_cache.pop(h, None)
    return len(stale)


async def _adsb_truth_cycle() -> bool:
    """One fetch-then-age cycle.  Returns True if OpenSky rate-limited us.

    The prune is outside the fetch deliberately: _fetch_external_adsb returns
    early when no real node is connected, and those are exactly the runs after
    which a stale cache would otherwise sit untouched.
    """
    rate_limited = await _fetch_external_adsb()
    dropped = prune_external_adsb_cache()
    if dropped:
        logging.debug("external ADS-B cache: dropped %d stale entries", dropped)
    return rate_limited


async def adsb_truth_fetcher():
    backoff = 0
    while True:
        await asyncio.sleep(ADSB_TRUTH_INTERVAL_S + backoff)
        backoff = 0
        try:
            if await _adsb_truth_cycle():
                backoff = ADSB_BACKOFF_S
            state.task_last_success["adsb_truth_fetcher"] = time.time()
        except Exception:
            state.task_error_counts["adsb_truth_fetcher"] += 1
            logging.exception("External ADS-B fetch failed")


def _opensky_entry(s: list, poll_ts: float) -> dict | None:
    """One OpenSky state vector as an external_adsb_cache entry, or None.

    Index map, from /states/all: 3 time_position, 5 longitude, 6 latitude,
    7 baro_altitude (m), 9 velocity (m/s), 10 true_track.

    time_position is when the position was measured, and is null for a row
    whose last contact carried no position.  Only then does the poll stand in,
    which is a bounded ADSB_TRUTH_INTERVAL_S of error rather than the
    unbounded kind an unstamped entry used to carry downstream.
    """
    lon_val, lat_val, alt_val = s[5], s[6], s[7]
    if lat_val is None or lon_val is None:
        return None
    ts_pos = s[3] if len(s) > 3 and s[3] else None
    return {
        "lat": lat_val,
        "lon": lon_val,
        "alt_m": alt_val or 0,
        "velocity": s[9] if len(s) > 9 else None,
        "heading": s[10] if len(s) > 10 else None,
        "last_seen_ms": int((ts_pos if ts_pos is not None else poll_ts) * 1000),
    }


async def _fetch_external_adsb() -> bool:
    """Fetch aircraft positions for cross-validation.

    Primary source: OpenSky Network.
    Fallback: adsb.lol (free, no auth required) when OpenSky rate-limits or fails.
    Returns True if OpenSky was rate-limited (HTTP 429), False otherwise.
    """
    active_nodes = [
        info
        for info in list(state.connected_nodes.values())
        if info.get("status") != "disconnected" and info.get("config")
    ]
    if not active_nodes:
        return False

    if all(info.get("is_synthetic", False) for info in active_nodes):
        logging.debug("All nodes synthetic — skipping external ADS-B fetch")
        return False

    real_nodes = [n for n in active_nodes if not n.get("is_synthetic", False)]
    source_nodes = real_nodes if real_nodes else active_nodes
    lats = [n["config"].get("rx_lat", 0) for n in source_nodes]
    lons = [n["config"].get("rx_lon", 0) for n in source_nodes]
    if not lats or all(la == 0 for la in lats):
        return False

    lamin, lamax = min(lats) - OPENSKY_BUFFER_DEG, max(lats) + OPENSKY_BUFFER_DEG
    lomin, lomax = min(lons) - OPENSKY_BUFFER_DEG, max(lons) + OPENSKY_BUFFER_DEG
    lat_center = (lamin + lamax) / 2
    lon_center = (lomin + lomax) / 2

    # ── Try OpenSky first ─────────────────────────────────────────────────────
    opensky_failed = False
    rate_limited = False
    url = "https://opensky-network.org/api/states/all"
    global _opensky_client
    if _opensky_client is None or _opensky_client.is_closed:
        _opensky_client = httpx.AsyncClient(timeout=15.0)
    try:
        resp = await _opensky_client.get(
            url,
            params={
                "lamin": lamin,
                "lamax": lamax,
                "lomin": lomin,
                "lomax": lomax,
            },
        )
        if resp.status_code == 429:
            logging.debug("OpenSky rate-limited (429) — trying adsb.lol fallback")
            rate_limited = True
            opensky_failed = True
        elif resp.status_code != 200:
            opensky_failed = True
        else:
            data = resp.json()
            states = data.get("states", [])
            if states:
                now_cache = {}
                poll_ts = time.time()
                for s in states:
                    icao = s[0] if s[0] else None
                    if not icao:
                        continue
                    entry = _opensky_entry(s, poll_ts)
                    if entry is not None:
                        now_cache[icao] = entry
                state.external_adsb_cache = now_cache
                logging.debug("OpenSky: cached %d aircraft positions", len(now_cache))
                _cross_validate_adsb_reports()
                return False
            opensky_failed = True
    except Exception:
        # Close before dropping the reference: discarding the client leaked
        # its connection pool once per poll for the whole of an outage.
        _dead_client, _opensky_client = _opensky_client, None
        if _dead_client is not None:
            try:
                await _dead_client.aclose()
            except Exception:
                pass
        opensky_failed = True

    # ── Fallback: adsb.lol ────────────────────────────────────────────────────
    if opensky_failed:
        try:
            fallback_cache = await _fetch_adsb_lol(lat_center, lon_center)
            if fallback_cache:
                state.external_adsb_cache = fallback_cache
                logging.debug("adsb.lol fallback: cached %d aircraft positions", len(fallback_cache))
                _cross_validate_adsb_reports()
        except Exception:
            logging.warning("adsb.lol fallback also failed — external ADS-B cache may be stale")

    return rate_limited


async def _fetch_adsb_lol(lat: float, lon: float) -> dict:
    """Fetch aircraft positions from adsb.lol centered on lat/lon.

    Returns {hex: {lat, lon, alt_m, velocity, heading}} matching external_adsb_cache format.
    """
    from clients.adsb_lol import AdsbLolClient

    global _adsb_lol_client
    loop = asyncio.get_running_loop()
    area = {"name": "auto", "lat": lat, "lon": lon, "radius_nm": 200}
    if _adsb_lol_client is None:
        _adsb_lol_client = AdsbLolClient([area])
    else:
        # Update area center so the rate-limit cache key stays consistent
        _adsb_lol_client.areas = [area]
    aircraft = await loop.run_in_executor(None, _adsb_lol_client.fetch_all)
    poll_ts = time.time()
    result = {}
    for ac in aircraft:
        h = (ac.get("hex") or "").lower()
        if not h:
            continue
        alt_baro = ac.get("alt_baro", 0)
        alt_m = alt_baro * 0.3048 if isinstance(alt_baro, (int, float)) else 0.0
        # seen_pos is an age in seconds, so it resolves against the poll, not
        # against the epoch.  The poll alone is the fallback — see _opensky_entry.
        seen_pos = ac.get("seen_pos")
        captured = poll_ts - seen_pos if isinstance(seen_pos, (int, float)) else poll_ts
        result[h] = {
            "lat": ac.get("lat", 0.0),
            "lon": ac.get("lon", 0.0),
            "alt_m": alt_m,
            "velocity": ac.get("gs"),
            "heading": ac.get("track"),
            "last_seen_ms": int(captured * 1000),
        }
    return result


def _cross_validate_adsb_reports():
    """Penalise nodes whose ADS-B reports diverge from external truth.

    The penalty here is severe and sticky: 0.1 a time against a block
    threshold of 0.2 from a start of 1.0, persisted by state_snapshot, and
    apply_reward is a no-op while blocked.  So every gate below exists to keep
    a truthful node out of it, and each is load bearing:

    - Both sides must be recent (XVAL_MAX_AGE_S).  The 10 km bar measures
      disagreement only while the two fixes describe nearly the same instant;
      across a poll interval an airliner outruns it honestly.
    - A sample is judged once (_xval_judged_through).  This rescans the last
      ten samples every cycle, so without it one bad fix is re-charged until
      the node blocks.
    - (0, 0) is routes/analytics.py's default for an omitted position, not a
      claim to have seen an aircraft off West Africa.

    Anything a gate rejects is left for a later cycle rather than judged on
    worse evidence.
    """
    if not state.external_adsb_cache:
        return
    now = time.time()
    for node_id, ts_state in state.node_analytics.trust_scores.items():
        if not ts_state.samples:
            continue
        judged_through = _xval_judged_through.get(node_id, 0)
        newest_judged = judged_through
        for sample in ts_state.samples[-10:]:
            if not sample.adsb_hex:
                continue
            # Only samples carrying the node's own position claim can be
            # cross-validated: backend-computed claim residuals store the hex
            # for retraction but no fix (adsb_lat/lon are 0.0 placeholders),
            # so haversine against external truth would read as a >10 km
            # "mismatch" and penalize the node for a position it never
            # reported.
            if sample.provenance != "self_report":
                continue
            if sample.adsb_lat == 0.0 and sample.adsb_lon == 0.0:
                continue
            sample_ts_ms = getattr(sample, "timestamp_ms", 0) or 0
            if sample_ts_ms <= judged_through:
                continue
            if now - sample_ts_ms / 1000 > XVAL_MAX_AGE_S:
                continue
            ext = state.external_adsb_cache.get(sample.adsb_hex.lower())
            if ext is None:
                continue
            ext_ts_ms = ext.get("last_seen_ms")
            if not ext_ts_ms or now - ext_ts_ms / 1000 > XVAL_MAX_AGE_S:
                continue
            newest_judged = max(newest_judged, sample_ts_ms)
            dist_km = haversine_km(sample.adsb_lat, sample.adsb_lon, ext["lat"], ext["lon"])
            if dist_km > 10.0:
                rep = state.node_analytics.reputations.get(node_id)
                if rep:
                    rep.apply_penalty(
                        0.1,
                        f"ADS-B position mismatch: {sample.adsb_hex} reported {dist_km:.1f}km from external truth",
                    )
                    logging.warning(
                        "Node %s ADS-B mismatch for %s: %.1f km off",
                        node_id,
                        sample.adsb_hex,
                        dist_km,
                    )
        if newest_judged > judged_through:
            _xval_judged_through[node_id] = newest_judged
