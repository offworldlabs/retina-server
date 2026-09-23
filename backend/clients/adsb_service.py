"""adsb-service client: ADSBHub traffic from adsb.retina.fm, in adsb.lol's point shape.

API: https://adsb.retina.fm/v2/point/{lat}/{lon}/{radius_nm} (radius capped at
250 nm), answering tar1090 aircraft objects under ``ac`` and the answer's own
build time, in epoch ms, under ``now``.  The service allows 2 requests a second
per IP, burst 10, and answers 429 with Retry-After beyond that; pacing is the
caller's, and this client only reports the limit.
"""

import math
import time

import httpx

from config.constants import ADSB_CAPTURE_MAX_SKEW_S, is_num
from services.adsb_regions import is_position_absent, is_usable
from services.id_utils import is_transponder_hex, normalize_hex_key

BASE_URL = "https://adsb.retina.fm"
_TIMEOUT_S = 4.0
_USER_AGENT = "retina-server/1.0 (+https://github.com/offworldlabs/retina-server)"
# Used when a 429 names no wait in seconds.
DEFAULT_RETRY_AFTER_S = 5.0


class RateLimited(Exception):
    """The service answered 429."""

    def __init__(self, retry_after_s: float):
        super().__init__(f"adsb-service rate limit, retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


def parse_point_response(body: dict, recv_s: float) -> list[dict]:
    """One point answer as rows carrying an absolute ``captured_ms``.

    ``seen_pos`` is an age against the answer's ``now``, so the capture time is
    ``now`` less it, bounded by our own receipt: a server clock running ahead
    must not date a fix in our future, where every age gate reads it as fresh.
    A row without ``seen_pos`` has no capture time at all and is dropped rather
    than dated from the poll.  A ``now`` behind receipt is believed, however
    far, up to ADSB_CAPTURE_MAX_SKEW_S: an answer served from an old build
    really is that old, and dating it from receipt would pass its rows as
    fresh.  Past that bound it is garbage (a ``now`` in seconds, say), and
    receipt stands in for it.

    An aircraft on the ground is dropped: adsb.lol reports ``alt_baro`` as
    "ground" and adsb.retina.fm as 0, and a parked aircraft is a decoy for
    zero-Doppler clutter, not something a node can see in the air.
    """
    now_ms = body.get("now")
    recv_ms = recv_s * 1000.0
    base_ms = recv_ms
    if is_num(now_ms) and not isinstance(now_ms, bool) and recv_ms - now_ms <= ADSB_CAPTURE_MAX_SKEW_S * 1000:
        base_ms = min(now_ms, recv_ms)
    rows = []
    for ac in body.get("ac") or []:
        if not isinstance(ac, dict):
            continue
        hexn = normalize_hex_key(ac.get("hex"))
        lat, lon, seen_pos = ac.get("lat"), ac.get("lon"), ac.get("seen_pos")
        if not is_transponder_hex(hexn) or not is_num(seen_pos):
            continue
        if not is_usable(lat, lon) or is_position_absent(lat, lon):
            continue
        alt_baro = ac.get("alt_baro")
        if not is_num(alt_baro) or alt_baro <= 0:
            continue
        rows.append(
            {
                "hex": hexn,
                "flight": (ac.get("flight") or "").strip(),
                "lat": lat,
                "lon": lon,
                "alt_baro": alt_baro,
                "gs": ac.get("gs", 0),
                "track": ac.get("track", 0),
                "captured_ms": int(base_ms - seen_pos * 1000.0),
            }
        )
    return rows


class AdsbServiceClient:
    """Point queries against adsb-service over one pooled connection."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, base_url: str = BASE_URL):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=_TIMEOUT_S,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            transport=transport,
        )

    async def fetch_point(self, lat: float, lon: float, radius_nm: int) -> list[dict]:
        """Aircraft within ``radius_nm`` of a point.  Raises RateLimited on 429."""
        resp = await self._http.get(f"/v2/point/{lat}/{lon}/{radius_nm}")
        recv_s = time.time()
        if resp.status_code == 429:
            raise RateLimited(_retry_after_s(resp.headers.get("Retry-After")))
        resp.raise_for_status()
        return parse_point_response(resp.json(), recv_s)

    async def aclose(self) -> None:
        await self._http.aclose()


def _retry_after_s(value: str | None) -> float:
    """Retry-After in seconds; the HTTP-date form and anything unreadable wait the default."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
    return seconds if math.isfinite(seconds) and seconds >= 0 else DEFAULT_RETRY_AFTER_S
