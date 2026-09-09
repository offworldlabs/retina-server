"""
adsb.lol API client — fetches real-time ADS-B aircraft positions.

API: https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}
Returns tar1090-compatible aircraft objects.
License: ODbL (same as OpenStreetMap).
"""

import gzip
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

from config.constants import is_num

log = logging.getLogger(__name__)

_BASE = "https://api.adsb.lol/v2"
_TIMEOUT = 8  # seconds
_MIN_POLL_INTERVAL = 5.0  # seconds between requests per area
# How long an area's last good result may stand in for a failed fetch.  Serving
# it across a blip is the point; serving it across an outage republishes hours-old
# aircraft as current truth, and the caller's own expiry cannot see far enough
# down to stop that.
_CACHE_MAX_AGE_S = 600.0

# adsb.lol returns 403 "User-Agent too generic; include valid contact info" for
# a generic or absent UA, so every request must carry real contact info here.
_USER_AGENT = "retina-server/1.0 (+https://github.com/offworldlabs/retina-server)"
_REQUEST_SPACING_S = 5.0  # Between network calls, the cadence blessed in adsblol/api#62
_RATE_LIMIT_BACKOFF_S = 10.0  # Single retry after a 429, same source


class AdsbLolClient:
    """Polls adsb.lol for real aircraft in configured geographic areas."""

    def __init__(self, areas: list[dict]):
        """
        Args:
            areas: List of dicts with keys: name, lat, lon, radius_nm (default 80).
        """
        self._last_poll: dict[str, float] = {}
        self._cache: dict[str, list[dict]] = {}
        self.last_status: dict[str, bool] = {}
        self._cache_ts: dict[str, float] = {}  # area → time.monotonic() of its last good fetch
        self._areas: list[dict] = []
        self.areas = areas

    @property
    def areas(self) -> list[dict]:
        return self._areas

    @areas.setter
    def areas(self, value: list[dict]) -> None:
        """Replace the configured areas, forgetting per-area state for any dropped.

        The caller reassigns this on every fetch cycle to the fleet's current
        lattice cells, so a name that drops out here must stop being served: a
        stale entry left behind would let a later re-occupation of that cell
        serve aircraft cached from its previous occupancy.

        An entry missing "name", "lat" or "lon" cannot be queried or cached by
        name, so it is dropped here rather than left for fetch_area to index
        unconditionally: one malformed area must cost only itself, not the
        whole batch the caller treats this assignment as part of.  The mapping
        check must come first: a non-mapping entry (retina-simulation builds
        this list from parsed configuration, not always a dict) would raise
        out of `.get` before the key checks ever ran.
        """
        good = []
        for area in value:
            if not isinstance(area, Mapping) or not area.get("name") or "lat" not in area or "lon" not in area:
                log.warning("adsb.lol: skipping malformed area (needs name, lat, lon): %r", area)
                continue
            good.append(area)
        self._areas = good
        kept = {area["name"] for area in good}
        for stale in set(self._last_poll) - kept:
            del self._last_poll[stale]
        for stale in set(self._cache) - kept:
            del self._cache[stale]
        for stale in set(self._cache_ts) - kept:
            del self._cache_ts[stale]
        for stale in set(self.last_status) - kept:
            del self.last_status[stale]

    def _last_good(self, name: str) -> list[dict]:
        """This area's last good result, while it is still young enough to serve."""
        ts = self._cache_ts.get(name)
        if ts is None or time.monotonic() - ts > _CACHE_MAX_AGE_S:
            self._cache.pop(name, None)
            self._cache_ts.pop(name, None)
            return []
        return self._cache.get(name, [])

    def fetch_area(self, area: dict) -> list[dict]:
        """Fetch aircraft for a single area. Returns list of aircraft dicts."""
        name = area["name"]
        now = time.monotonic()
        if now - self._last_poll.get(name, 0) < _MIN_POLL_INTERVAL:
            return self._last_good(name)

        lat = area["lat"]
        lon = area["lon"]
        radius_nm = area.get("radius_nm", 80)
        url = f"{_BASE}/point/{lat}/{lon}/{radius_nm}"

        try:
            data = self._get(url)
            # Wall clock, not the monotonic `now` above: this leaves the rows as
            # an absolute capture time, which is the only form that survives
            # being served again from _last_good on a later, failed fetch.
            fetched_at = time.time()
            aircraft = data.get("ac", [])
            result = []
            for ac in aircraft:
                lat_v = ac.get("lat")
                lon_v = ac.get("lon")
                if lat_v is None or lon_v is None:
                    continue
                # tar1090's seen_pos is an age, meaningful only against the
                # fetch that produced the row.  Resolve it here, where that
                # fetch time is known: _last_good serves these rows again on a
                # later failed fetch, and a relative age would read as fresh.
                seen_pos = ac.get("seen_pos")
                captured_at = fetched_at - seen_pos if is_num(seen_pos) else fetched_at
                result.append(
                    {
                        "hex": ac.get("hex", ""),
                        "flight": (ac.get("flight") or "").strip(),
                        "lat": lat_v,
                        "lon": lon_v,
                        "alt_baro": ac.get("alt_baro") or 0,
                        "gs": ac.get("gs") or 0,
                        "track": ac.get("track") or 0,
                        "captured_at": captured_at,  # epoch seconds
                        "squawk": ac.get("squawk", ""),
                        "category": ac.get("category", ""),
                        "type": ac.get("type", "adsb_icao"),
                        "registration": ac.get("r", ""),
                        "aircraft_type": ac.get("t", ""),
                    }
                )
            self._cache[name] = result
            self._cache_ts[name] = now
            self._last_poll[name] = now
            self.last_status[name] = True
            return result
        except Exception as e:
            log.debug("adsb.lol fetch failed for %s: %s", name, e)
            self.last_status[name] = False
            return self._last_good(name)

    def _get(self, url: str) -> dict:
        """One GET, retrying once after a 429.

        429 gets its own retry here rather than falling into fetch_area's
        blanket except, so a rate limit gets one more chance before the
        caller falls back to stale cache.
        """
        try:
            return self._get_once(url)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                log.warning("adsb.lol 403 (User-Agent gate): %s", e.reason)
                raise
            if e.code != 429:
                raise
            log.warning("adsb.lol rate-limited (429), retrying in %.0fs", _RATE_LIMIT_BACKOFF_S)
            time.sleep(_RATE_LIMIT_BACKOFF_S)
            return self._get_once(url)

    def _get_once(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": _USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            # urllib does not decompress for us, and 305 KB becomes 75 KB.
            if (resp.headers or {}).get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw)

    def fetch_all(self) -> list[dict]:
        """Fetch aircraft for all configured areas, deduplicated by hex."""
        seen = set()
        result = []
        spaced = False
        for area in self.areas:
            now = time.monotonic()
            hits_network = now - self._last_poll.get(area["name"], 0) >= _MIN_POLL_INTERVAL
            if hits_network and spaced:
                time.sleep(_REQUEST_SPACING_S)
            for ac in self.fetch_area(area):
                h = ac.get("hex", "")
                if h and h not in seen:
                    seen.add(h)
                    result.append(ac)
            if hits_network:
                spaced = True
        return result
