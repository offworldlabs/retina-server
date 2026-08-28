"""ADS-B freshness must describe when a position was measured, not handled.

Every staleness gate downstream keys off ``last_seen_ms``.  Stamping it at
processing time makes those gates read a stale fix as current, which is worst
exactly when the data is worst: a queue backlog, or an external feed that has
stopped refreshing.  ClickUp 86cb9br6k.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from clients.adsb_lol import AdsbLolClient
from config.constants import ADSB_CAPTURE_MAX_SKEW_S, EXTERNAL_ADSB_MAX_AGE_S
from core import state
from services.calibration import record_adsb_calibration
from services.feed_helpers import adsb_capture_ts_ms
from services.frame_processor import process_one_frame
from services.tasks.periodic import (
    _adsb_truth_cycle,
    _fetch_adsb_lol,
    _opensky_entry,
    prune_external_adsb_cache,
)
from services.tcp_handler import _apply_synthetic_adsb
from services.track_gates import fresh_adsb

_HEX = "aa1234"


def _lol_ac(**over):
    """One aircraft as AdsbLolClient.fetch_all yields it."""
    ac = {"hex": "abc123", "lat": 48.0, "lon": 16.0, "alt_baro": 10000, "gs": 400.0, "track": 90.0}
    ac.update(over)
    return ac


def _stub_adsb_lol(monkeypatch, aircraft):
    """Serve `aircraft` from the adsb.lol client without touching the network."""
    import services.tasks.periodic as periodic

    monkeypatch.setattr(periodic, "_adsb_lol_client", None, raising=False)
    monkeypatch.setattr(AdsbLolClient, "fetch_all", lambda self: aircraft)


def _ext(captured_s: float | None, **over):
    """One external-cache entry, captured `captured_s` ago (None = unstamped)."""
    entry = {"lat": 48.0, "lon": 16.0, "alt_m": 3048.0, "velocity": 100.0, "heading": 45.0}
    if captured_s is not None:
        entry["last_seen_ms"] = int((time.time() - captured_s) * 1000)
    entry.update(over)
    return entry


@pytest.fixture(autouse=True)
def _clean_state():
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    yield
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()


class TestExternalCacheFreshness:
    def test_reports_the_entrys_own_capture_time(self):
        """The returned fix carries the poll's capture stamp, not `now`."""
        state.external_adsb_cache[_HEX] = _ext(captured_s=45.0)

        now = time.time()
        fix = fresh_adsb(_HEX, now)

        assert fix is not None
        age_s = now - fix["last_seen_ms"] / 1000
        assert 44.0 < age_s < 46.0, f"expected ~45 s of age, got {age_s:.1f} s"

    def test_a_stalled_feed_stops_being_served(self):
        """Past the admission budget the entry is withheld, not aged silently."""
        state.external_adsb_cache[_HEX] = _ext(captured_s=3600.0)

        assert fresh_adsb(_HEX, time.time()) is None

    def test_an_unstamped_entry_is_not_assumed_fresh(self):
        """Unknown freshness is not freshness — the fabrication this ticket removes."""
        state.external_adsb_cache[_HEX] = _ext(captured_s=None)

        assert fresh_adsb(_HEX, time.time()) is None


# ── What the pollers stamp entries with ──────────────────────────────────────
#
# Both feeds publish the age of each position, so the capture time is available
# rather than inferred.  Poll wall-time is the fallback, not the source: it is
# the same handling-time error this ticket exists to remove, only smaller.


class TestCalibrationCanNowRefuseAnOldFix:
    """The gate this ticket exists to re-arm, exercised end to end.

    record_adsb_calibration documents its input as "the ADS-B fix's own
    wall-clock timestamp (last_seen_ms / 1000)" and refuses anything older
    than CAL_MAX_ADSB_AGE_S.  While fresh_adsb reported `now`, that age was
    always zero and the refusal could not fire, so external fixes up to a poll
    interval old fed the learned FOV the module documents protecting.
    """

    @staticmethod
    def _record(fix, now):
        """Feed a fix through the gate the way track_entry does.

        The node's detection is co-timed with the fix, so the separate
        fix-vs-detection skew rule (CAL_FIX_DETECTION_SKEW_S, 2 s) is
        satisfied and the fix's own age is the only thing under test.
        """
        fix_ts = fix["last_seen_ms"] / 1000
        return record_adsb_calibration(
            ["node-1"],
            fix["lat"],
            fix["lon"],
            age_s=now - fix_ts,
            fix_ts=fix_ts,
            detection_ts=fix_ts,
        )

    def test_an_external_fix_of_a_typical_age_is_refused(self):
        """Typical is most of a poll interval: entries are 0-120 s old by design."""
        state.external_adsb_cache[_HEX] = _ext(captured_s=90.0)
        now = time.time()

        assert self._record(fresh_adsb(_HEX, now), now) == 0

    def test_a_live_fix_within_the_budget_is_still_recorded(self):
        """The capability being protected: this must not refuse everything."""
        now = time.time()
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": 48.0,
            "lon": 16.0,
            "last_seen_ms": int((now - 2.0) * 1000),
        }

        assert self._record(fresh_adsb(_HEX, now), now) == 1


class TestIngestCaptureStamp:
    """The node's own capture stamp, where it is usable.

    Both ingest paths had the capture time in hand and stamped receipt time
    anyway, so under queue backlog a position read as live to every 60 s gate:
    the staleness defences switched off exactly when the data was stalest.
    """

    def test_the_frames_own_capture_time_is_used(self):
        captured_ms = int((time.time() - 20.0) * 1000)

        assert adsb_capture_ts_ms({"timestamp": captured_ms}, time.time()) == captured_ms

    def test_a_frame_with_no_timestamp_falls_back_to_receipt(self):
        now = time.time()

        assert adsb_capture_ts_ms({}, now) == int(now * 1000)

    def test_the_fallback_is_counted_rather_than_silent(self):
        before = state.adsb_capture_ts_fallback

        adsb_capture_ts_ms({}, time.time())

        assert state.adsb_capture_ts_fallback == before + 1

    def test_a_node_clock_far_behind_ours_is_not_trusted(self):
        """Beyond the bound this is a broken clock, not a backlog."""
        now = time.time()
        stamp = int((now - ADSB_CAPTURE_MAX_SKEW_S - 60) * 1000)

        assert adsb_capture_ts_ms({"timestamp": stamp}, now) == int(now * 1000)

    def test_a_node_clock_ahead_of_ours_is_not_trusted(self):
        """The dangerous direction: a future stamp is never stale, to any gate."""
        now = time.time()
        stamp = int((now + ADSB_CAPTURE_MAX_SKEW_S + 60) * 1000)

        assert adsb_capture_ts_ms({"timestamp": stamp}, now) == int(now * 1000)

    def test_backlog_within_the_bound_is_reported_honestly(self):
        """The whole point — a genuinely old position must read as old."""
        now = time.time()
        stamp = int((now - 90.0) * 1000)

        assert adsb_capture_ts_ms({"timestamp": stamp}, now) == stamp


class TestBothIngestPathsUseIt:
    """The helper is only worth anything if the two store sites call it.

    They are separate paths: the TCP fast-path stores before queuing, the
    frame processor stores for sources that arrive unextracted (blah2_bridge),
    and each had its own `int(time.time() * 1000)`.
    """

    _ADSB = [{"hex": "cafe01", "lat": 48.0, "lon": 16.0, "alt_baro": 35000, "gs": 400, "track": 90}]

    def test_tcp_fast_path(self):
        captured_ms = int((time.time() - 45.0) * 1000)

        _apply_synthetic_adsb({"data": {"timestamp": captured_ms, "adsb": self._ADSB}}, "sim-node-1")

        assert state.adsb_aircraft["cafe01"]["last_seen_ms"] == captured_ms

    def test_frame_processor_path(self):
        from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline

        captured_ms = int((time.time() - 45.0) * 1000)
        frame = {
            "timestamp": captured_ms,
            "delay": [50.0, 52.0],
            "doppler": [10.0, 15.0],
            "snr": [20.0, 21.0],
            "adsb": self._ADSB,
        }

        process_one_frame("bridge-node-1", frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        assert state.adsb_aircraft["cafe01"]["last_seen_ms"] == captured_ms


class TestStalledFeedExpiry:
    """A refresh that fails must leave the cache emptying, not frozen.

    Both fetch paths only ever replace the dict wholesale, so before this an
    outage left the last good snapshot in place indefinitely behind a log line.
    """

    def test_entries_past_the_budget_are_dropped(self):
        state.external_adsb_cache["old"] = _ext(captured_s=EXTERNAL_ADSB_MAX_AGE_S + 60)
        state.external_adsb_cache["new"] = _ext(captured_s=30.0)

        dropped = prune_external_adsb_cache()

        assert dropped == 1
        assert set(state.external_adsb_cache) == {"new"}

    def test_unstamped_entries_are_dropped(self):
        """Nothing writes these now, so one present is a snapshot from before."""
        state.external_adsb_cache["legacy"] = _ext(captured_s=None)

        prune_external_adsb_cache()

        assert state.external_adsb_cache == {}

    def test_a_fetch_that_reaches_no_source_still_ages_the_cache(self):
        """The early returns leave the cache untouched; the loop must not."""
        state.external_adsb_cache["old"] = _ext(captured_s=EXTERNAL_ADSB_MAX_AGE_S + 60)
        state.connected_nodes.clear()  # no active nodes → _fetch_external_adsb returns early

        asyncio.run(_adsb_truth_cycle())

        assert state.external_adsb_cache == {}


class TestOpenSkyCaptureTime:
    @staticmethod
    def _state_vector(time_position, now):
        """One OpenSky /states/all row: 3 time_position, 5 lon, 6 lat, 9 velocity."""
        return ["abc123", "CALL    ", "UK", time_position, now, 16.0, 48.0, 3000.0, False, 250.0, 90.0]

    def test_position_time_is_the_capture_stamp(self):
        now = time.time()
        captured = now - 30.0

        entry = _opensky_entry(self._state_vector(captured, now), now)

        assert entry["last_seen_ms"] == int(captured * 1000)

    def test_poll_time_stands_in_when_the_row_carries_no_position_time(self):
        now = time.time()

        entry = _opensky_entry(self._state_vector(None, now), now)

        assert entry["last_seen_ms"] == int(now * 1000)


class TestAdsbLolCaptureTime:
    def test_the_rows_capture_time_is_carried_through(self, monkeypatch):
        """The client resolves seen_pos against its own fetch; this reads it."""
        captured_at = time.time() - 12.0
        _stub_adsb_lol(monkeypatch, [_lol_ac(captured_at=captured_at)])

        cache = asyncio.run(_fetch_adsb_lol(48.0, 16.0))

        assert cache["abc123"]["last_seen_ms"] == int(captured_at * 1000)

    def test_poll_time_stands_in_when_the_row_carries_no_capture_time(self, monkeypatch):
        _stub_adsb_lol(monkeypatch, [_lol_ac()])

        now = time.time()
        cache = asyncio.run(_fetch_adsb_lol(48.0, 16.0))

        assert abs(cache["abc123"]["last_seen_ms"] / 1000 - now) < 2.0

    def test_the_client_resolves_seen_pos_at_fetch_time(self):
        """An age is only meaningful against the fetch that produced the row."""
        payload = {"ac": [{"hex": "abc123", "lat": 48.0, "lon": 16.0, "seen_pos": 7.5}]}
        client = AdsbLolClient([{"name": "a", "lat": 48.0, "lon": 16.0}])

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            aircraft = client.fetch_area(client.areas[0])

        assert time.time() - aircraft[0]["captured_at"] == pytest.approx(7.5, abs=2.0)
