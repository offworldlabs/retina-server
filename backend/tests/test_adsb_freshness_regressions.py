"""Regressions found reviewing the capture-timestamp change (ClickUp 86cb9br6k).

Each test here pins one way the first cut of that change was wrong.  Several
are cases where making freshness honest broke something that had been relying,
silently, on every ADS-B stamp coming off the server clock.
"""

import asyncio
import time

import pytest
from retina_analytics.reputation import NodeReputation
from retina_analytics.trust import AdsReportEntry

from clients.adsb_lol import AdsbLolClient
from core import state
from services.adsb_regions import regions_for_nodes
from services.calibration import record_adsb_calibration
from services.feed_helpers import adsb_capture_ts_ms
from services.tasks.periodic import _cross_validate_adsb_reports, _fetch_adsb_lol
from services.tcp_handler import _apply_synthetic_adsb
from services.track_gates import fresh_adsb

_HEX = "cafe01"
_VIENNA = (48.0, 16.0)


def _stub_adsb_lol(monkeypatch, aircraft):
    """Serve `aircraft` from the adsb.lol client without touching the network."""
    import services.tasks.periodic as periodic

    def fetch_all(self):
        # The real client records a verdict per configured area, and the caller
        # reads exactly that to decide which regions it covered.
        for area in self.areas:
            self.last_status[area["name"]] = True
        return aircraft

    monkeypatch.setattr(periodic, "_adsb_lol_client", None, raising=False)
    monkeypatch.setattr(AdsbLolClient, "fetch_all", fetch_all)


def _lol_cache():
    """The cache half of one fetch over the region covering `_VIENNA`."""
    cache, _covered = asyncio.run(_fetch_adsb_lol(regions_for_nodes([_VIENNA])))
    return cache


@pytest.fixture(autouse=True)
def _clean():
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()
    yield
    state.adsb_aircraft.clear()
    state.external_adsb_cache.clear()


# ── The client owns capture time, because it is what knows when it fetched ───


class TestLastGoodRowsKeepTheirOriginalAge:
    def test_a_cached_row_is_not_restamped_as_fresh(self, monkeypatch):
        """The sharpest regression: an outage made stale data look current.

        `seen_pos` is an age relative to the fetch that produced the row, so
        resolving it against a later poll re-stamped the whole last-good
        snapshot as seconds old on every cycle — defeating the prune, the
        servable-age gate and the cross-validator's co-timing gate at once.
        """
        captured_at = time.time() - 540.0
        row = {"hex": _HEX, "lat": 48.0, "lon": 16.0, "gs": 400.0, "track": 90.0, "captured_at": captured_at}
        _stub_adsb_lol(monkeypatch, [row])

        cache = _lol_cache()

        age_s = time.time() - cache[_HEX]["last_seen_ms"] / 1000
        assert 539.0 < age_s < 542.0, f"expected the row's true age, got {age_s:.1f} s"

    def test_the_client_stamps_absolute_capture_time(self):
        """Not `seen_pos`: a relative age is only meaningful to the fetch that made it."""
        import json
        from unittest.mock import patch

        payload = {"ac": [{"hex": _HEX, "lat": 48.0, "lon": 16.0, "seen_pos": 8.0}]}
        client = AdsbLolClient([{"name": "a", "lat": 48.0, "lon": 16.0}])

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            aircraft = client.fetch_area(client.areas[0])

        assert time.time() - aircraft[0]["captured_at"] == pytest.approx(8.0, abs=2.0)


# ── Calibration compares two clocks, so both must be the same clock ──────────


class TestCalibrationSkewStaysOnOneClock:
    def test_a_node_whose_clock_is_off_still_calibrates(self):
        """CAL_FIX_DETECTION_SKEW_S bounds fix-vs-detection co-timing at 2 s.

        The detection stamp is server wall clock, so comparing it against a
        node-clock capture stamp silently turned that rule into an NTP test:
        a board 3 s out recorded no calibration points at all.
        """
        now = time.time()
        skew_s = 30.0
        fix = {
            "lat": 48.0,
            "lon": 16.0,
            "last_seen_ms": int((now - skew_s) * 1000),  # node clock, 30 s slow
            "recv_ms": int(now * 1000),  # server clock, when we took it
        }

        recorded = record_adsb_calibration(
            ["node-1"],
            fix["lat"],
            fix["lon"],
            age_s=0.5,
            fix_ts=fix["recv_ms"] / 1000,
            detection_ts=now,
        )

        assert recorded == 1

    def test_both_ingest_paths_record_server_receipt_alongside_capture(self):
        """The skew rule needs a server-clock stamp; the age gates need capture."""
        captured_ms = int((time.time() - 20.0) * 1000)

        _apply_synthetic_adsb(
            {"data": {"timestamp": captured_ms, "adsb": [{"hex": _HEX, "lat": 48.0, "lon": 16.0}]}},
            "sim-node-1",
        )

        rec = state.adsb_aircraft[_HEX]
        assert rec["last_seen_ms"] == captured_ms
        assert abs(rec["recv_ms"] / 1000 - time.time()) < 2.0


# ── A stamp that can go backwards must not evict a live aircraft ─────────────


class TestStampsNeverRegress:
    def test_an_older_frame_does_not_overwrite_a_newer_fix(self):
        """The TCP path stores every message, then stores the queued frame
        again later.  Once the stamp came from the frame rather than the
        clock, that replay walked last_seen_ms backwards by the queue
        latency, and feed_gc evicts anything over 60 s."""
        now = time.time()
        newer_ms = int(now * 1000)
        older_ms = int((now - 90.0) * 1000)
        adsb = [{"hex": _HEX, "lat": 48.0, "lon": 16.0}]

        _apply_synthetic_adsb({"data": {"timestamp": newer_ms, "adsb": adsb}}, "n1")
        _apply_synthetic_adsb({"data": {"timestamp": older_ms, "adsb": adsb}}, "n1")

        assert state.adsb_aircraft[_HEX]["last_seen_ms"] == newer_ms


# ── A node must not be able to dictate its own freshness ────────────────────


class TestForwardSkewIsNotTrusted:
    def test_a_stamp_seconds_into_the_future_is_refused(self):
        """Past skew is backlog and is reported honestly; future skew is a
        broken clock, and it is never stale to any gate."""
        now = time.time()
        stamp = int((now + 45.0) * 1000)

        assert adsb_capture_ts_ms({"timestamp": stamp}, now) == int(now * 1000)

    def test_ordinary_jitter_is_still_accepted(self):
        now = time.time()
        stamp = int((now + 0.4) * 1000)

        assert adsb_capture_ts_ms({"timestamp": stamp}, now) == stamp


# ── Cross-validation must not be switchable off by the node being checked ───


def _reputation(node_id):
    rep = NodeReputation(node_id=node_id)
    state.node_analytics.reputations[node_id] = rep
    return rep


def _report(node_id, lat, lon, ts_ms):
    state.node_analytics.record_adsb_correlation(
        node_id,
        AdsReportEntry(
            timestamp_ms=ts_ms,
            predicted_delay=100.0,
            predicted_doppler=0.0,
            measured_delay=100.5,
            measured_doppler=0.0,
            adsb_hex=_HEX,
            adsb_lat=lat,
            adsb_lon=lon,
        ),
    )


def _truth(age_s=1.0):
    state.external_adsb_cache[_HEX] = {
        "lat": 51.5,
        "lon": -0.1,
        "alt_m": 10000.0,
        "velocity": 200.0,
        "heading": 90.0,
        "last_seen_ms": int((time.time() - age_s) * 1000),
    }


class TestCrossValidationCannotBeBypassed:
    @pytest.fixture(autouse=True)
    def _clean_nodes(self):
        yield
        for nid in ("future-node", "dup-node"):
            state.node_analytics.trust_scores.pop(nid, None)
            state.node_analytics.reputations.pop(nid, None)

    def test_a_future_stamp_does_not_grant_immunity(self):
        """A one-sided age gate let a future stamp through, and the sample
        watermark it then set made every later sample look already judged."""
        rep = _reputation("future-node")
        _truth()
        now_ms = int(time.time() * 1000)
        _report("future-node", 51.5, 0.33, now_ms + 3_600_000)  # an hour ahead
        _cross_validate_adsb_reports()

        _report("future-node", 51.5, 0.33, now_ms)
        _cross_validate_adsb_reports()

        assert rep.reputation < 1.0, "an honest divergent sample was never judged"

    def test_every_sample_in_one_frame_is_judged(self):
        """A node correlating several aircraft against one frame stamps them
        all with that frame's timestamp; a scalar watermark judged one."""
        rep = _reputation("dup-node")
        _truth()
        ts_ms = int(time.time() * 1000)
        for _ in range(3):
            _report("dup-node", 51.5, 0.33, ts_ms)

        _cross_validate_adsb_reports()

        assert len(rep.penalties) == 3


# ── Feed-supplied stamps are input too ──────────────────────────────────────


class TestExternalStampsAreBounded:
    def test_a_future_external_stamp_cannot_outlive_the_prune(self):
        """An entry stamped ahead of now is never older than any budget, so
        it would sit in the cache until a fetch happened to replace it."""
        state.external_adsb_cache[_HEX] = {
            "lat": 48.0,
            "lon": 16.0,
            "alt_m": 3048.0,
            "velocity": 100.0,
            "heading": 45.0,
            "last_seen_ms": int((time.time() + 3600) * 1000),
        }

        assert fresh_adsb(_HEX, time.time()) is None


class TestNonFiniteFeedValuesDoNotBlankTheCache:
    def test_a_nan_capture_time_drops_one_row_not_the_poll(self, monkeypatch):
        """json.loads accepts a bare NaN, and int(nan) raises — which took the
        whole fetch down and left the previous cache frozen behind it."""
        good = {"hex": "aaaa01", "lat": 48.0, "lon": 16.0, "gs": 400.0, "captured_at": time.time()}
        bad = {"hex": "bbbb02", "lat": 48.1, "lon": 16.1, "gs": float("nan"), "captured_at": float("nan")}
        _stub_adsb_lol(monkeypatch, [good, bad])

        cache = _lol_cache()

        assert "aaaa01" in cache
        assert cache.get("bbbb02", {}).get("velocity") is None


# ── Consumers that read the cache directly must age it ──────────────────────


class TestDirectCacheConsumersGateOnAge:
    def test_verification_truth_pool_refuses_a_stale_external_entry(self):
        from services.tasks.analytics_refresh import _external_truth_entries

        state.external_adsb_cache["old01"] = {
            "lat": 48.0,
            "lon": 16.0,
            "alt_m": 3048.0,
            "velocity": 100.0,
            "heading": 45.0,
            "last_seen_ms": int((time.time() - 900.0) * 1000),
        }
        state.external_adsb_cache["new01"] = {
            "lat": 48.0,
            "lon": 16.0,
            "alt_m": 3048.0,
            "velocity": 100.0,
            "heading": 45.0,
            "last_seen_ms": int((time.time() - 30.0) * 1000),
        }

        entries = dict(_external_truth_entries(time.time()))

        assert "new01" in entries
        assert "old01" not in entries


# ── The sim push path writes the same store ─────────────────────────────────


class TestSimIngestIsBoundedToo:
    def test_a_future_push_stamp_is_refused(self):
        from routes.sim_ingest import _sim_push_ts_ms

        now = time.time()

        assert _sim_push_ts_ms({"ts_ms": int((now + 3600) * 1000)}, now) == int(now * 1000)

    def test_an_honest_push_stamp_is_kept(self):
        from routes.sim_ingest import _sim_push_ts_ms

        now = time.time()
        stamp = int((now - 5.0) * 1000)

        assert _sim_push_ts_ms({"ts_ms": stamp}, now) == stamp
