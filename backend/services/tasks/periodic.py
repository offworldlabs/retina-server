"""Periodic background tasks: archive flush, archive lifecycle, reputation, ADS-B truth fetch."""

import asyncio
import logging
import time
from typing import NamedTuple

import httpx

from config.constants import (
    ADSB_BACKOFF_S,
    ADSB_NODE_RANGE_MARGIN_KM,
    ADSB_TRUTH_INTERVAL_S,
    ARCHIVE_FLUSH_INTERVAL_S,
    ARCHIVE_LIFECYCLE_INTERVAL_S,
    REPUTATION_INTERVAL_S,
)
from core import state
from services.adsb_regions import Box, Region, is_position_absent, is_usable, regions_for_nodes
from services.frame_processor import flush_all_archive_buffers
from services.geo import haversine_km

_OPENSKY_URL = "https://opensky-network.org/api/states/all"

# Simultaneous OpenSky requests.  A task apiece would make the burst size track
# ADSB_MAX_REGIONS_PER_CYCLE (sized for a different provider) and the boxes each
# region splits into, which is what per-second limiters and connection-rate
# defences react to; a fixed four holds the burst shape whatever those become.
# It bounds requests rather than regions, so a split region takes two of the
# four slots and the phase deepens by a wave instead of doubling the burst.
_OPENSKY_MAX_CONCURRENT = 4

_opensky_client: httpx.AsyncClient | None = None
_adsb_lol_client: object | None = None


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


async def adsb_truth_fetcher():
    backoff = 0
    while True:
        await asyncio.sleep(ADSB_TRUTH_INTERVAL_S + backoff)
        backoff = 0
        try:
            rate_limited = await _fetch_external_adsb()
            if rate_limited:
                backoff = ADSB_BACKOFF_S
            state.task_last_success["adsb_truth_fetcher"] = time.time()
        except Exception:
            state.task_error_counts["adsb_truth_fetcher"] += 1
            logging.exception("External ADS-B fetch failed")


async def _fetch_external_adsb() -> bool:
    """Fetch aircraft positions for cross-validation, one query region per node cluster.

    OpenSky is asked for every region; adsb.lol (free, no auth) is asked for
    whichever regions OpenSky did not cover, so a bad region costs only itself.
    The cache is replaced whenever at least one query genuinely answered — an
    empty answer from a working query included — and left stale only when none
    did.

    Returns True only if OpenSky reported credit exhaustion (HTTP 429) *and* a
    region went uncovered because of it.  The caller turns True into a 300 s
    backoff on top of the cycle, which a cycle whose truth arrived complete has
    not earned.
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
    positions = []
    half_positioned = 0
    over_margin: list[float] = []
    for n in source_nodes:
        lat, lon = n["config"].get("rx_lat"), n["config"].get("rx_lon")
        # Half a fix is no fix.  Substituting 0.0 for the missing half puts the
        # node on the prime meridian off West Africa, which is_usable cannot
        # tell from a real position, so it would claim a query region over empty
        # ocean every cycle.  Whatever the config does hold goes through
        # unaltered for regions_for_nodes to judge.
        if lat is None or lon is None:
            if lat is not None or lon is not None:
                half_positioned += 1
            continue
        positions.append((lat, lon))
        # The margin every region is padded by is an assumption about the fleet's
        # configuration; node_config accepts max_range_km up to 1000 km. A node
        # beyond the margin detects aircraft its region never asks for, so the
        # shortfall is named here rather than left as silently thin truth.
        max_range_km = n["config"].get("max_range_km")
        if isinstance(max_range_km, (int, float)) and not isinstance(max_range_km, bool):
            if max_range_km > ADSB_NODE_RANGE_MARGIN_KM:
                over_margin.append(float(max_range_km))
    if half_positioned:
        logging.warning(
            "ADS-B truth: %d node(s) carry only one of rx_lat/rx_lon — treated as unpositioned",
            half_positioned,
        )
    if over_margin:
        logging.warning(
            "ADS-B truth: %d node(s) configure a detection range past the %.0f km query margin "
            "(largest %.0f km) — the outer part of their coverage gets no truth",
            len(over_margin),
            ADSB_NODE_RANGE_MARGIN_KM,
            max(over_margin),
        )

    regions = regions_for_nodes(positions)
    if not regions:
        # HTTP-ingest nodes are registered with `{"node_id": ...}` as their
        # whole config, which passes the truthiness filter above and carries
        # neither coordinate, so an all-HTTP fleet lands on this branch.
        logging.warning(
            "ADS-B truth: %d active node(s) but none carries a position — nothing to query",
            len(source_nodes),
        )
        return False
    logging.debug(
        "ADS-B truth: %d region(s) %s, %d OpenSky credit(s)",
        len(regions),
        [r.name for r in regions],
        sum(r.opensky_credits() for r in regions),
    )

    cache, covered, credit_refused, opensky_answered = await _fetch_opensky(regions)
    # `opensky_answered` holds for a box that answered with no aircraft too, so
    # quiet airspace replaces the cache below rather than leaving it stale.  It
    # must come from OpenSky's own boxes and never from the merged cache:
    # adsb.lol serves its own stale per-area cache for an area that failed, and
    # that is what must never pass as a fresh answer.
    uncovered = [r for r in regions if r.name not in covered]
    lol_covered: set[str] = set()
    if uncovered:
        try:
            lol_cache, lol_covered = await _fetch_adsb_lol(uncovered)
        except Exception:
            logging.warning("adsb.lol fetch failed outright — %d region(s) uncovered", len(uncovered))
        else:
            # First writer wins, so an aircraft both providers saw keeps the
            # entry whose "source" names who actually supplied those figures.
            for adsb_hex, entry in lol_cache.items():
                cache.setdefault(adsb_hex, entry)

    if opensky_answered or lol_covered:
        # An empty result from a fetch that worked means the sky is empty
        # there, so the cache must be replaced even so: keeping the old one
        # would let a stale answer masquerade as a fresh one.
        state.external_adsb_cache = cache
        logging.info(
            "External ADS-B: cached %d aircraft from %d region(s); OpenSky %s, adsb.lol %s",
            len(cache),
            len(regions),
            sorted(covered) or "none",
            sorted(lol_covered) or "none",
        )
        _cross_validate_adsb_reports()
    else:
        logging.warning("External ADS-B: every region failed on both providers — cache left stale")
    # Both halves must speak of the same region, or a gap the credit budget had
    # nothing to do with (a transport error the fallback also missed) charges
    # the cycle a 300 s backoff.  A refused region is never in `covered`, so the
    # only question left is whether the fallback then covered it.
    return any(name not in lol_covered for name in credit_refused)


async def _fetch_opensky(regions: list[Region]) -> tuple[dict, set[str], set[str], bool]:
    """Fetch every region's boxes. Returns (cache, covered, credit-refused, anything answered).

    The last of those is not derivable from the others, and the caller's cache
    replacement turns on it: a region with one box answered and its sibling
    failed is deliberately kept out of `covered` so it still reaches the
    fallback, and a box that answered with no aircraft adds nothing to the
    cache.  Both are answers, and a fresh "nothing here" must replace the cache
    rather than leave the last cycle's aircraft standing.

    Small boxes around each cluster rather than one spanning the fleet: OpenSky
    charges credits by box area, so the fleet-wide box costs 4 a request
    against 1 for a box around a single cluster.  Credits are charged per
    request, not per second, so the requests go out concurrently (bounded by
    _OPENSKY_MAX_CONCURRENT): asked in turn, a black-holing OpenSky costs one
    timeout per request of wall clock before the fallback is even reached.

    The refused set names regions, not a bare count, because the caller's
    backoff is only earned where the same region that was refused credit also
    went uncovered by the fallback.

    Every failure mode is per-region: a region is either in the covered set or
    it is not, and the caller sends the rest to adsb.lol.  No region may cost
    another its answer, since `regions_for_nodes` orders deterministically and
    one persistently bad region would otherwise starve the same part of the
    fleet on every cycle.  The cache holds whatever came back, an uncovered
    region's part included; the covered set alone decides who goes to the
    fallback.
    """
    global _opensky_client
    # Built once for the whole batch, which the requests below then share: a
    # region rebuilding it mid-flight would pull it from under its siblings.
    if _opensky_client is None or _opensky_client.is_closed:
        _opensky_client = httpx.AsyncClient(timeout=15.0)
    client = _opensky_client

    limit = asyncio.Semaphore(_OPENSKY_MAX_CONCURRENT)
    results = await asyncio.gather(*(_fetch_opensky_region(client, region, limit) for region in regions))

    cache: dict = {}
    covered: set[str] = set()
    credit_refused: set[str] = set()
    transport_errors = 0
    answered = 0
    for region, result in zip(regions, results, strict=True):
        if result.limited:
            credit_refused.add(region.name)
        transport_errors += result.transport_errors
        answered += result.answered
        cache.update(result.cache)
        if result.covered:
            covered.add(region.name)

    requests = sum(len(region.boxes) for region in regions)
    if requests and transport_errors == requests and _opensky_client is client:
        # Only a client every one of whose requests failed at the transport is
        # suspect; a minority failure is the network's, and rebuilding over it
        # costs a fresh handshake per request next cycle while the rest were
        # being served.  It must be closed before the reference is dropped, or
        # its connection pool leaks once per poll for the whole of an outage.
        _opensky_client = None
        try:
            await client.aclose()
        except Exception:
            pass

    return cache, covered, credit_refused, answered > 0


class _RegionFetch(NamedTuple):
    """One region's OpenSky result. `answered` is how many of its boxes returned a payload."""

    cache: dict
    covered: bool
    limited: bool
    transport_errors: int
    answered: int


async def _fetch_opensky_region(client, region: Region, limit: asyncio.Semaphore) -> _RegionFetch:
    """One region's aircraft, a request per box.

    A region is covered only where every one of its boxes was, so a part-answered
    region still goes to the fallback whole.  What the boxes that did answer
    returned is kept regardless: the truth cache carries no completeness
    contract, its consumers gating on a delay window and a position match, so
    sparse truth is thinner rather than wrong, and it is worth more than nothing
    where the fallback fails as well.

    The semaphore is taken per request, not per region, so a split region
    deepens the phase rather than widening the burst.
    """

    async def _one(label: str, box: Box):
        async with limit:
            return await _fetch_opensky_box(client, label, box)

    n = len(region.boxes)
    # A split region's two failures must not read as one region failing twice.
    if n == 1:
        labels = [region.name]
    else:
        labels = [f"{region.name} box {i}/{n}" for i in range(1, n + 1)]
    results = await asyncio.gather(*(_one(label, box) for label, box in zip(labels, region.boxes, strict=True)))

    cache: dict = {}
    limited = False
    transport_errors = 0
    answered = 0
    for box_cache, box_limited, box_transport_error in results:
        limited = limited or box_limited
        transport_errors += int(box_transport_error)
        if box_cache is None:
            continue
        answered += 1
        cache.update(box_cache)
    # _region_from_members always yields at least one box, so the first clause
    # guards the type rather than that path: a Region assembled any other way
    # with no boxes has been asked nothing, and must reach the fallback rather
    # than pass as covered on an empty cache.
    covered = answered > 0 and answered == len(results)
    return _RegionFetch(cache, covered, limited, transport_errors, answered)


async def _fetch_opensky_box(client, label: str, box: Box) -> tuple[dict | None, bool, bool]:
    """One box's aircraft. Returns (cache, or None if unanswered; rate-limited; transport error).

    Every failure is caught here, which is what confines it to the box that
    caused it: the caller merges whatever came back and hands the regions that
    are short of a box to the fallback.
    """
    try:
        resp = await client.get(
            _OPENSKY_URL,
            params={
                "lamin": box.lamin,
                "lamax": box.lamax,
                "lomin": box.lomin,
                "lomax": box.lomax,
            },
        )
    except Exception:
        logging.warning("OpenSky unreachable for %s — leaving it to the fallback", label)
        return None, False, True

    if resp.status_code == 429:
        # Credit exhaustion, not a policy block.  The budget is per account and
        # IP rather than per box, so the sibling requests already in flight meet
        # it too; what keeps a spent budget from being hammered is the caller's
        # backoff between cycles, not anything one request can do about the
        # others.  The response says when the budget refills, which is worth
        # more than that fixed guess (86cb9m6wc).
        logging.info(
            "OpenSky out of credits for %s (retry after %ss)",
            label,
            resp.headers.get("X-Rate-Limit-Retry-After-Seconds"),
        )
        return None, True, False
    if resp.status_code != 200:
        logging.warning("OpenSky %d for %s", resp.status_code, label)
        return None, False, False

    # A 200 answers the box only where it carries the documented object: an HTML
    # interstitial, a bare list, an absent `states` or one of any type but list
    # leaves the box unanswered rather than crashing the fetch.  `states` is
    # judged on its type and never on truthiness, because a string is truthy and
    # iterates into characters that the vector guard below skips one by one,
    # which would pass an undocumented payload off as quiet airspace and rob the
    # box of the fallback.
    try:
        payload = resp.json()
        if not isinstance(payload, dict) or "states" not in payload:
            raise TypeError("not an OpenSky state-vector object")
        states = payload["states"]
        if states is None:
            states = []  # nullable in the schema: quiet airspace, not a fault
        if not isinstance(states, list):
            raise TypeError("states is not a list")
        box_cache = {}
        for s in states:
            # One rule for both halves of the schema: a vector must be long
            # enough to hold the fields read below and name its icao24 as a
            # string.  Either failing costs only that vector, so a single
            # malformed entry never sends an otherwise good region to the
            # fallback and discards the aircraft around it.
            if not isinstance(s, (list, tuple)) or len(s) < 8:
                continue
            icao = s[0]
            lon_val, lat_val, alt_val = s[5], s[6], s[7]
            if not isinstance(icao, str) or not icao or lat_val is None or lon_val is None:
                continue
            # Lowercased to match adsb.lol's keys: the cross-provider dedup and
            # every cache lookup assume one case for both.
            box_cache[icao.lower()] = {
                "lat": lat_val,
                "lon": lon_val,
                "alt_m": alt_val or 0,
                "velocity": s[9] if len(s) > 9 else None,
                "heading": s[10] if len(s) > 10 else None,
                "source": "opensky",
            }
    except Exception:
        logging.warning("OpenSky sent an unreadable 200 for %s — leaving it to the fallback", label)
        return None, False, False

    return box_cache, False, False


async def _fetch_adsb_lol(regions: list[Region]) -> tuple[dict, set[str]]:
    """Fetch aircraft for every region. Returns (cache, covered names).

    Coverage is named per region rather than reported as a bare success flag,
    so the caller can log each provider's real reach: partial coverage here is
    the normal case, and the summary log is read as what the cache holds.
    """
    from clients.adsb_lol import AdsbLolClient

    global _adsb_lol_client
    loop = asyncio.get_running_loop()
    areas = [r.as_area() for r in regions]
    if _adsb_lol_client is None:
        _adsb_lol_client = AdsbLolClient(areas)
    else:
        _adsb_lol_client.areas = areas
    aircraft = await loop.run_in_executor(None, _adsb_lol_client.fetch_all)

    status = _adsb_lol_client.last_status
    covered = {r.name for r in regions if status.get(r.name, False)}
    failed = [r.name for r in regions if r.name not in covered]
    if failed:
        logging.warning("adsb.lol: %d of %d regions failed: %s", len(failed), len(regions), ", ".join(failed))

    result = {}
    for ac in aircraft:
        h = (ac.get("hex") or "").lower()
        if not h:
            continue
        alt_baro = ac.get("alt_baro", 0)
        alt_m = alt_baro * 0.3048 if isinstance(alt_baro, (int, float)) else 0.0
        result[h] = {
            "lat": ac.get("lat", 0.0),
            "lon": ac.get("lon", 0.0),
            "alt_m": alt_m,
            "velocity": ac.get("gs"),
            "heading": ac.get("track"),
            "source": "adsb_lol",
        }
    return result, covered


def _cross_validate_adsb_reports():
    """Log nodes whose ADS-B reports diverge from external truth.

    Observation only.  A penalty needs the capture timestamp 86cb9br6k adds to
    each cache entry: without it the comparison is against a fix of unknown age
    (up to a whole fetch interval), and an aircraft at 200 m/s outruns the 10 km
    threshold inside that window, so a truthful node reads as a mismatch.
    """
    if not state.external_adsb_cache:
        return
    for node_id, ts_state in state.node_analytics.trust_scores.items():
        if not ts_state.samples:
            continue
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
            # A self-report may name a hex without echoing a position: the
            # analytics route requires neither adsb_lat nor adsb_lon and
            # defaults both to 0.
            if is_position_absent(sample.adsb_lat, sample.adsb_lon):
                continue
            # The route applies no type validation, so adsb_lat/adsb_lon can
            # carry a bool or any other non-coordinate JSON value -- not the
            # absent sentinel, but not something haversine_km can measure
            # against either.
            if not is_usable(sample.adsb_lat, sample.adsb_lon):
                continue
            ext = state.external_adsb_cache.get(sample.adsb_hex.lower())
            if ext is None:
                continue
            dist_km = haversine_km(sample.adsb_lat, sample.adsb_lon, ext["lat"], ext["lon"])
            if dist_km > 10.0:
                logging.warning(
                    "Node %s ADS-B mismatch for %s: %.1f km off",
                    node_id,
                    sample.adsb_hex,
                    dist_km,
                )
