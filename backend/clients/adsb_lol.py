"""
adsb.lol API client — fetches real-time ADS-B aircraft positions.

API: https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}
Returns tar1090-compatible aircraft objects.
License: ODbL (same as OpenStreetMap).
"""

import json
import logging
import time
import urllib.request

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


class AdsbLolClient:
    """Polls adsb.lol for real aircraft in configured geographic areas."""

    def __init__(self, areas: list[dict]):
        """
        Args:
            areas: List of dicts with keys: name, lat, lon, radius_nm (default 80).
        """
        self.areas = areas
        self._last_poll: dict[str, float] = {}
        self._cache: dict[str, list[dict]] = {}
        self._cache_ts: dict[str, float] = {}  # area → time.monotonic() of its last good fetch

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
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
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
            return result
        except Exception as e:
            log.debug("adsb.lol fetch failed for %s: %s", name, e)
            return self._last_good(name)

    def fetch_all(self) -> list[dict]:
        """Fetch aircraft for all configured areas, deduplicated by hex."""
        seen = set()
        result = []
        for area in self.areas:
            for ac in self.fetch_area(area):
                h = ac.get("hex", "")
                if h and h not in seen:
                    seen.add(h)
                    result.append(ac)
        return result
