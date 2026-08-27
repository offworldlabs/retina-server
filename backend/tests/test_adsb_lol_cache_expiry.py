"""AdsbLolClient's last-good cache has to expire, or the one above it cannot.

The client serves its last good result for an area whenever a fetch fails.
Nothing ever removed those entries, so a multi-hour adsb.lol outage re-merged
hour-old aircraft into external truth on every cycle, indefinitely.  Giving
external_adsb_cache an expiry while leaving this one unbounded would not close
the hole: stale positions would keep arriving from underneath it.
ClickUp 86cb9br6k.
"""

import json
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from clients.adsb_lol import _CACHE_MAX_AGE_S, AdsbLolClient

_AREA = {"name": "Greenville", "lat": 34.852, "lon": -82.394, "radius_nm": 60}
_PAYLOAD = {"ac": [{"hex": "a1b2c3", "lat": 34.85, "lon": -82.39, "gs": 400, "seen_pos": 2.0}]}


@pytest.fixture
def client():
    return AdsbLolClient([dict(_AREA)])


@contextmanager
def _serve(payload):
    """urlopen returns `payload` for the duration of the block."""
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        yield urlopen


def _fail():
    """urlopen raises for the duration of the block."""
    return patch("urllib.request.urlopen", side_effect=OSError("adsb.lol unreachable"))


class TestLastGoodExpiry:
    def test_a_failed_fetch_serves_the_last_good_result(self, client):
        """The behaviour worth keeping: a blip does not blank the area."""
        with _serve(_PAYLOAD):
            assert len(client.fetch_area(client.areas[0])) == 1

        client._last_poll["Greenville"] = 0.0  # let the rate limiter through
        with _fail():
            assert len(client.fetch_area(client.areas[0])) == 1

    def test_a_prolonged_outage_stops_serving_it(self, client):
        """Past the budget the area goes empty rather than replaying old traffic."""
        with _serve(_PAYLOAD):
            client.fetch_area(client.areas[0])

        # Age the cached result past the budget, and clear the rate limiter.
        client._cache_ts["Greenville"] = time.monotonic() - _CACHE_MAX_AGE_S - 1
        client._last_poll["Greenville"] = 0.0

        with _fail():
            assert client.fetch_area(client.areas[0]) == []

    def test_a_successful_fetch_refreshes_the_budget(self, client):
        """Otherwise a long-lived area would expire while perfectly healthy."""
        with _serve(_PAYLOAD):
            client.fetch_area(client.areas[0])
            client._cache_ts["Greenville"] = time.monotonic() - _CACHE_MAX_AGE_S - 1
            client._last_poll["Greenville"] = 0.0
            client.fetch_area(client.areas[0])  # succeeds, so the stamp moves

        client._last_poll["Greenville"] = 0.0
        with _fail():
            assert len(client.fetch_area(client.areas[0])) == 1

    def test_the_rate_limiter_still_serves_a_fresh_cache(self, client):
        """Inside _MIN_POLL_INTERVAL there is no fetch at all — that path
        must read the cache, not be mistaken for an outage."""
        with _serve(_PAYLOAD):
            client.fetch_area(client.areas[0])
            assert len(client.fetch_area(client.areas[0])) == 1
