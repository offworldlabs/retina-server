"""Feed-store GC runs on its own timer, not off the back of a feed build.

Before services.tasks.feed_gc existed, every store below was pruned only
inside build_combined_aircraft_json — so a websocket client slow enough to
stall the flush task, or an idle state.aircraft_dirty, stopped server-wide GC
while frame workers and solver threads kept writing.  These tests assert the
pruning happens with no feed build in sight.
"""

import os

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

import asyncio  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from config.constants import MN_DARK_EXPIRY_S, TRAIL_STALE_S  # noqa: E402
from core import state  # noqa: E402
from services.feed_gc import prune_multinode_tracks  # noqa: E402
from services.id_utils import multinode_hex_from_key  # noqa: E402
from services.tasks import feed_gc as feed_gc_task_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_stores():
    state.track_arc_motion.clear()
    state.multinode_tracks.clear()
    with state.anomaly_lock:
        state.anomaly_hexes.clear()
    state.task_last_success.pop("feed_gc", None)
    yield
    state.track_arc_motion.clear()
    state.multinode_tracks.clear()
    with state.anomaly_lock:
        state.anomaly_hexes.clear()
    state.task_last_success.pop("feed_gc", None)


def _mn(age_s: float) -> dict:
    return {"timestamp_ms": (time.time() - age_s) * 1000}


class TestGCWithoutAFeedBuild:
    def test_stale_arc_motion_is_pruned_by_the_task_body(self):
        now = time.time()
        state.track_arc_motion["pr0001"] = [(1.0, 2.0, now - TRAIL_STALE_S - 10)]
        state.track_arc_motion["pr0002"] = [(1.0, 2.0, now)]

        feed_gc_task_mod.run_feed_gc(now)

        assert "pr0001" not in state.track_arc_motion
        assert "pr0002" in state.track_arc_motion

    def test_the_loop_reports_success_to_the_task_registry(self, monkeypatch):
        monkeypatch.setattr(feed_gc_task_mod, "FEED_GC_INTERVAL_S", 0.0)

        async def _until_first_pass():
            task = asyncio.create_task(feed_gc_task_mod.feed_gc_task())
            for _ in range(200):
                await asyncio.sleep(0)
                if "feed_gc" in state.task_last_success:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_until_first_pass())

        assert "feed_gc" in state.task_last_success

    def test_feed_gc_is_in_the_staleness_registry(self):
        from core.task_registry import TASK_EXPECTED_INTERVAL_S

        assert TASK_EXPECTED_INTERVAL_S["feed_gc"] == 5


class TestMultinodeExpiryOffTheTimer:
    """The store the solver writes at full rate, aged with no feed build."""

    def test_stale_dark_entry_is_popped_and_its_anomaly_hex_discarded(self):
        key = "mn-dark-stale"
        state.multinode_tracks[key] = _mn(age_s=MN_DARK_EXPIRY_S + 5.0)
        with state.anomaly_lock:
            state.anomaly_hexes.add(multinode_hex_from_key(key))

        prune_multinode_tracks(time.time())

        assert key not in state.multinode_tracks
        with state.anomaly_lock:
            assert multinode_hex_from_key(key) not in state.anomaly_hexes

    def test_fresh_dark_entry_is_kept(self):
        state.multinode_tracks["mn-dark-fresh"] = _mn(age_s=MN_DARK_EXPIRY_S - 5.0)
        prune_multinode_tracks(time.time())
        assert "mn-dark-fresh" in state.multinode_tracks

    def test_assisted_entry_keeps_its_longer_lane_expiry(self):
        # Past the dark budget, inside the 60 s an mn-adsb-* entry gets.
        state.multinode_tracks["mn-adsb-abc123"] = _mn(age_s=MN_DARK_EXPIRY_S + 5.0)
        state.multinode_tracks["mn-adsb-def456"] = _mn(age_s=61.0)

        prune_multinode_tracks(time.time())

        assert "mn-adsb-abc123" in state.multinode_tracks
        assert "mn-adsb-def456" not in state.multinode_tracks

    def test_the_task_body_runs_the_multinode_expiry_too(self):
        state.multinode_tracks["mn-dark-stale"] = _mn(age_s=MN_DARK_EXPIRY_S + 5.0)
        feed_gc_task_mod.run_feed_gc()
        assert "mn-dark-stale" not in state.multinode_tracks

    def test_pruning_twice_is_idempotent(self):
        # The feed build still calls it before reading its snapshot.
        state.multinode_tracks["mn-dark-stale"] = _mn(age_s=MN_DARK_EXPIRY_S + 5.0)
        state.multinode_tracks["mn-dark-fresh"] = _mn(age_s=1.0)

        now = time.time()
        prune_multinode_tracks(now)
        prune_multinode_tracks(now)

        assert list(state.multinode_tracks) == ["mn-dark-fresh"]
