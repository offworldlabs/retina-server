"""
Unit tests for the adsb.lol client and metro filtering logic.
"""

import gzip
import json
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from clients.adsb_lol import AdsbLolClient

# ── Fixtures ──────────────────────────────────────────────────────────────────

_AREAS = [
    {"name": "Atlanta", "lat": 33.749, "lon": -84.388, "radius_nm": 80},
    {"name": "Greenville", "lat": 34.852, "lon": -82.394, "radius_nm": 60},
]

_MOCK_RESPONSE = {
    "ac": [
        {
            "hex": "a1b2c3",
            "flight": "DAL123 ",
            "lat": 33.75,
            "lon": -84.39,
            "alt_baro": 35000,
            "gs": 450,
            "track": 90,
            "squawk": "1234",
            "category": "A3",
            "type": "adsb_icao",
            "r": "N12345",
            "t": "B738",
        },
        {"hex": "d4e5f6", "lat": 33.80, "lon": -84.30, "alt_baro": 28000, "gs": 380, "track": 180},
        # Aircraft with no lat/lon should be skipped
        {"hex": "no_pos", "alt_baro": 10000},
    ],
}


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAdsbLolClient:
    def _mock_urlopen(self, response_data):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_returns_aircraft(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        result = client.fetch_area(_AREAS[0])

        assert len(result) == 2  # no_pos aircraft skipped
        assert result[0]["hex"] == "a1b2c3"
        assert result[0]["flight"] == "DAL123"  # stripped
        assert result[0]["lat"] == 33.75
        assert result[0]["registration"] == "N12345"
        assert result[1]["hex"] == "d4e5f6"

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_caches_within_interval(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)

        first = client.fetch_area(_AREAS[0])
        assert len(first) == 2
        assert mock_urlopen.call_count == 1

        # Second call within interval should use cache
        second = client.fetch_area(_AREAS[0])
        assert len(second) == 2
        assert mock_urlopen.call_count == 1  # no new request

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_returns_cache_on_error(self, mock_urlopen):
        # First call succeeds
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])

        # Expire cache
        client._last_poll["Atlanta"] = 0

        # Second call fails
        mock_urlopen.side_effect = Exception("network error")
        result = client.fetch_area(_AREAS[0])
        assert len(result) == 2  # returns cached data

    @patch("clients.adsb_lol.time.sleep")
    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_all_deduplicates(self, mock_urlopen, mock_sleep):
        # Same aircraft in both areas
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        result = client.fetch_all()

        # Should deduplicate by hex
        hexes = [ac["hex"] for ac in result]
        assert len(hexes) == len(set(hexes))

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_area_url_format(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen({"ac": []})
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "33.749" in req.full_url
        assert "-84.388" in req.full_url
        assert "/80" in req.full_url

    def test_empty_areas(self):
        client = AdsbLolClient([])
        assert client.fetch_all() == []

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_request_carries_contact_ua_and_gzip(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        AdsbLolClient(_AREAS).fetch_area(_AREAS[0])

        req = mock_urlopen.call_args[0][0]
        # A generic or absent User-Agent gets 403 "include valid contact info".
        assert "offworldlabs" in req.get_header("User-agent")
        assert req.get_header("Accept-encoding") == "gzip"

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_gzip_response_is_decompressed(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = gzip.compress(json.dumps(_MOCK_RESPONSE).encode())
        mock_resp.headers = {"Content-Encoding": "gzip"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert len(AdsbLolClient(_AREAS).fetch_area(_AREAS[0])) == 2

    @patch("clients.adsb_lol.time.sleep")
    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_429_retries_once_after_a_backoff(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None),
            self._mock_urlopen(_MOCK_RESPONSE),
        ]
        client = AdsbLolClient(_AREAS)

        assert len(client.fetch_area(_AREAS[0])) == 2
        assert mock_urlopen.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 10.0

    @patch("clients.adsb_lol.time.sleep")
    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_areas_are_spaced_by_five_seconds(self, mock_urlopen, mock_sleep):
        # _MIN_POLL_INTERVAL is keyed per area name, so it throttles nothing
        # across a fan-out.  Without this spacing the 9th request 429s.
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        AdsbLolClient(_AREAS).fetch_all()

        assert [c[0][0] for c in mock_sleep.call_args_list] == [5.0]

    @patch("clients.adsb_lol.time.sleep")
    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_last_status_distinguishes_success_from_failure(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._mock_urlopen(_MOCK_RESPONSE), OSError("boom")]
        client = AdsbLolClient(_AREAS)
        client.fetch_all()

        assert client.last_status[_AREAS[0]["name"]] is True
        assert client.last_status[_AREAS[1]["name"]] is False

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_403_logs_and_propagates_without_retry(self, mock_urlopen, caplog):
        # Distinct from the 429 path: a 403 means the User-Agent gate rejected
        # us outright, so retrying with the same request would just 403 again.
        mock_urlopen.side_effect = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        client = AdsbLolClient(_AREAS)

        with caplog.at_level(logging.WARNING, logger="clients.adsb_lol"):
            with pytest.raises(urllib.error.HTTPError):
                client._get("https://api.adsb.lol/v2/point/0/0/1")

        assert mock_urlopen.call_count == 1  # no retry, unlike 429
        assert "User-Agent gate" in caplog.text

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_cache_hit_leaves_last_status_untouched(self, mock_urlopen):
        # The truth fetcher decides whether to overwrite its cache from exactly
        # this flag, so the early cache-hit return (before the try block) must
        # not touch it.
        mock_urlopen.side_effect = OSError("boom")
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])
        assert client.last_status["Atlanta"] is False

        # Force the next call inside _MIN_POLL_INTERVAL regardless of wall time.
        client._last_poll["Atlanta"] = float("inf")
        client.fetch_area(_AREAS[0])

        assert mock_urlopen.call_count == 1  # served from cache, no second attempt
        assert client.last_status["Atlanta"] is False

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_dropped_area_is_forgotten_from_all_three_dicts(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])
        client.fetch_area(_AREAS[1])

        client.areas = [_AREAS[0]]

        assert "Greenville" not in client._last_poll
        assert "Greenville" not in client._cache
        assert "Greenville" not in client.last_status

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_area_still_configured_keeps_cache_and_serves_it_on_failure(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])

        # Reassigning areas with Atlanta still present must not disturb its cache.
        client.areas = list(_AREAS)

        client._last_poll["Atlanta"] = 0
        mock_urlopen.side_effect = Exception("network error")
        result = client.fetch_area(_AREAS[0])

        assert len(result) == 2  # still served from its own last-good cache

    @patch("clients.adsb_lol.time.sleep")
    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_re_added_area_does_not_serve_its_previous_occupancy(self, mock_urlopen, mock_sleep):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        client = AdsbLolClient(_AREAS)
        client.fetch_area(_AREAS[0])  # "Atlanta" cache now holds real aircraft

        # The lattice cell name "Atlanta" drops out of the fleet's footprint...
        client.areas = [_AREAS[1]]
        # ...and later a different node occupies the same cell id.
        client.areas = [_AREAS[0]]

        client._last_poll["Atlanta"] = 0
        mock_urlopen.side_effect = Exception("network error")
        result = client.fetch_area(_AREAS[0])

        assert result == []  # no stale aircraft resurrected for the reused name

    def test_a_nameless_area_does_not_raise_at_construction(self, caplog):
        # Indexing area["name"] unconditionally used to raise KeyError straight
        # out of __init__/the setter; the caller wraps this assignment in a
        # try that treats any exception as the whole provider failing, so one
        # malformed area must not raise here at all.
        bad = {"lat": 1.0, "lon": 2.0}
        with caplog.at_level(logging.WARNING, logger="clients.adsb_lol"):
            client = AdsbLolClient([_AREAS[0], bad])

        assert client.areas == [_AREAS[0]]
        assert "malformed" in caplog.text

    def test_an_area_missing_lat_or_lon_is_also_skipped(self, caplog):
        bad = {"name": "NoCoords"}
        with caplog.at_level(logging.WARNING, logger="clients.adsb_lol"):
            client = AdsbLolClient([_AREAS[0], bad])

        assert client.areas == [_AREAS[0]]
        assert "malformed" in caplog.text

    @pytest.mark.parametrize("bad", ["not-a-dict", None, 42, ["name", "lat", "lon"]])
    def test_a_non_mapping_entry_does_not_raise_at_construction(self, caplog, bad):
        # retina-simulation builds this list from parsed configuration, not
        # always a dict, so `.get`/`in` must never run on an entry that
        # cannot support them.
        with caplog.at_level(logging.WARNING, logger="clients.adsb_lol"):
            client = AdsbLolClient([_AREAS[0], bad])

        assert client.areas == [_AREAS[0]]
        assert "malformed" in caplog.text

    @patch("clients.adsb_lol.urllib.request.urlopen")
    def test_fetch_all_still_serves_the_good_areas_alongside_a_malformed_one(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_urlopen(_MOCK_RESPONSE)
        bad = {"lat": 1.0, "lon": 2.0}
        client = AdsbLolClient([_AREAS[0], bad])

        result = client.fetch_all()

        assert len(result) == 2

    def test_malformed_area_does_not_leave_orphaned_per_area_state(self, caplog):
        # A malformed entry that replaces a previously-good one of the same
        # shape must still prune like any other dropped area -- it must not
        # short-circuit the pruning pass just because it fails validation.
        bad = {"lat": 1.0, "lon": 2.0}
        with caplog.at_level(logging.WARNING, logger="clients.adsb_lol"):
            client = AdsbLolClient([_AREAS[0], _AREAS[1]])
            client.areas = [_AREAS[0], bad]

        assert "Greenville" not in client._last_poll
        assert "Greenville" not in client._cache
        assert "Greenville" not in client.last_status
