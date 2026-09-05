"""Feed-side handling of multinode solver tracks.

Covers the dead-reckoning of mn-* entries in build_combined_aircraft_json:
position is advanced with the solved velocity, but only up to a 30 s horizon —
past that a velocity error dominates any solve accuracy, so an old solve holds
its last dead-reckoned point until the 60 s entry expiry.
"""

import math
import os
import time
import types

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services import track_filter  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402

LAT, LON = 35.0, -82.0


def _mn_entry(age_s: float, vel_north: float) -> dict:
    return {
        "success": True,
        "lat": LAT,
        "lon": LON,
        "alt_m": 7000.0,
        "vel_east": 0.0,
        "vel_north": vel_north,
        "rms_delay": 0.1,
        "rms_doppler": 1.0,
        "n_nodes": 2,
        "n_measurements": 2,
        "contributing_node_ids": ["n1", "n2"],
        "timestamp_ms": int((time.time() - age_s) * 1000),
        # Confirmed (solve_count >= 2) so the MN_N2_MIN_SOLVES display gate
        # does not hide these fixtures — this file tests dead-reckoning, not
        # the gate itself; see test_mn_lifetime.py for the gate tests.
        "solve_count": 2,
    }


class TestMultinodeDeadReckonCap:
    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def test_fresh_entry_is_dead_reckoned_fully(self):
        state.multinode_tracks["mn-dark-x"] = _mn_entry(age_s=10.0, vel_north=100.0)
        ac = self._build_mn()
        exp_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 10.0)
        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)

    def test_dr_horizon_is_capped_at_30s(self):
        # 45 s old (younger than the 60 s expiry): advanced 30 s worth of
        # motion, not 45.
        state.multinode_tracks["mn-dark-x"] = _mn_entry(age_s=45.0, vel_north=100.0)
        ac = self._build_mn()
        capped_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 30.0)
        uncapped_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * 45.0)
        assert ac["lat"] == pytest.approx(capped_lat, abs=2e-4)
        assert abs(ac["lat"] - uncapped_lat) > 5e-3


class TestMultinodeDeadReckonSource:
    """TRACK_DR_SOURCE selects what velocity the DR block above uses: the KF
    display filter's LEARNED velocity (default, "kf") or the solved
    vel_east/vel_north the block used exclusively before this existed
    ("solve", rollback only).  This class's keys always seed a real KF entry
    first -- TestMultinodeDeadReckonCap's keys never touch track_filter at
    all, which is exactly what proves the "no KF entry" fallback path (the
    default env, unknown key) is unaffected by this feature.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def _seed_kf_entry(self, key: str, v_north_true: float) -> None:
        """Two solves, 10 s apart, moving purely north at v_north_true --
        vel_east/vel_north left at 0.0 on the fed results so the filter's
        learned velocity comes from the POSITION sequence, not an echoed
        prior (same discipline as test_track_filter.TestLearnedVelocity)."""
        lat2, lon2 = offset_latlon_m(LAT, LON, east_m=0.0, north_m=v_north_true * 10.0)
        track_filter.smooth_solve(
            {"lat": LAT, "lon": LON, "timestamp_ms": 1_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )
        track_filter.smooth_solve(
            {"lat": lat2, "lon": lon2, "timestamp_ms": 21_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )

    def test_kf_entry_dr_uses_learned_velocity_by_default(self, monkeypatch):
        monkeypatch.delenv("TRACK_DR_SOURCE", raising=False)
        key = "mn-kf-learn"
        self._seed_kf_entry(key, v_north_true=50.0)
        lv = track_filter.learned_velocity(key)
        assert lv is not None
        learned_ve, learned_vn = lv[0], lv[1]

        # The solve's OWN vel_north (100.0) differs from the learned one
        # (~50.0) -- the point of the test: prove the DR block reads the KF,
        # not the solve, whenever a KF entry actually exists.
        age_s = 10.0
        state.multinode_tracks[key] = _mn_entry(age_s=age_s, vel_north=100.0)
        ac = self._build_mn()

        exp_lat, exp_lon = offset_latlon_m(LAT, LON, east_m=learned_ve * age_s, north_m=learned_vn * age_s)
        solve_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * age_s)

        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)
        assert ac["lon"] == pytest.approx(exp_lon, abs=2e-4)
        assert abs(ac["lat"] - solve_lat) > 1e-4

    def test_track_dr_source_solve_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setenv("TRACK_DR_SOURCE", "solve")
        key = "mn-kf-rollback"
        self._seed_kf_entry(key, v_north_true=50.0)
        assert track_filter.learned_velocity(key) is not None  # KF entry genuinely exists

        age_s = 10.0
        state.multinode_tracks[key] = _mn_entry(age_s=age_s, vel_north=100.0)
        ac = self._build_mn()

        # "solve" must ignore the (present, different) KF entry entirely and
        # extrapolate with the solve's own vel_north, exactly as before this
        # feature existed.
        exp_lat, _ = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * age_s)
        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)


class TestGsSource:
    """TRACK_GS_SOURCE selects what velocity multinode_to_aircraft displays as
    gs/track: the KF display filter's LEARNED velocity (default, "kf") or the
    solved vel_east/vel_north multinode_to_aircraft used exclusively before
    this existed ("solve", rollback only).  This class's keys always seed a
    real KF entry first -- mirrors TestMultinodeDeadReckonSource's fixture
    and _seed_kf_entry discipline exactly, since gs/track and the DR
    projection now read the same learned vector by default.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def _seed_kf_entry(self, key: str, v_north_true: float) -> None:
        """Two solves, 10 s apart, moving purely north at v_north_true --
        vel_east/vel_north left at 0.0 on the fed results so the filter's
        learned velocity comes from the POSITION sequence, not an echoed
        prior (same discipline as test_track_filter.TestLearnedVelocity)."""
        lat2, lon2 = offset_latlon_m(LAT, LON, east_m=0.0, north_m=v_north_true * 10.0)
        track_filter.smooth_solve(
            {"lat": LAT, "lon": LON, "timestamp_ms": 1_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )
        track_filter.smooth_solve(
            {"lat": lat2, "lon": lon2, "timestamp_ms": 21_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )

    def test_kf_entry_gs_uses_learned_velocity_by_default(self, monkeypatch):
        monkeypatch.delenv("TRACK_GS_SOURCE", raising=False)
        key = "mn-gs-learn"
        self._seed_kf_entry(key, v_north_true=50.0)
        lv = track_filter.learned_velocity(key)
        assert lv is not None
        learned_ve, learned_vn = lv[0], lv[1]
        exp_speed_ms = math.sqrt(learned_ve**2 + learned_vn**2)
        exp_heading = math.degrees(math.atan2(learned_ve, learned_vn)) % 360

        # The solve's OWN vel_north (100.0) differs from the learned one
        # (~50.0) -- the point of the test: prove gs/track read the KF, not
        # the solve, whenever a KF entry actually exists.
        state.multinode_tracks[key] = _mn_entry(age_s=10.0, vel_north=100.0)
        ac = self._build_mn()

        assert ac["gs"] == pytest.approx(exp_speed_ms * 1.94384, abs=0.1)
        assert ac["track"] == pytest.approx(exp_heading, abs=0.1)
        assert ac["gs_source"] == "kf"
        solve_gs = round(100.0 * 1.94384, 1)
        assert abs(ac["gs"] - solve_gs) > 1.0
        # max_velocity_ms still latches the SOLVE speed, not the display one.
        assert ac["max_velocity_ms"] == pytest.approx(100.0, abs=0.1)

    def test_track_gs_source_solve_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setenv("TRACK_GS_SOURCE", "solve")
        key = "mn-gs-rollback"
        self._seed_kf_entry(key, v_north_true=50.0)
        assert track_filter.learned_velocity(key) is not None  # KF entry genuinely exists

        state.multinode_tracks[key] = _mn_entry(age_s=10.0, vel_north=100.0)
        ac = self._build_mn()

        assert ac["gs"] == pytest.approx(194.4, abs=0.1)
        assert "gs_source" not in ac

    def test_no_kf_entry_uses_solve_velocity(self, monkeypatch):
        monkeypatch.delenv("TRACK_GS_SOURCE", raising=False)
        key = "mn-gs-unsmoothed"
        assert track_filter.learned_velocity(key) is None  # never smoothed

        state.multinode_tracks[key] = _mn_entry(age_s=10.0, vel_north=100.0)
        ac = self._build_mn()

        assert ac["gs"] == pytest.approx(194.4, abs=0.1)
        assert "gs_source" not in ac

    def test_vel_untrusted_active_mode_keeps_kf_sourced_gs_track(self, monkeypatch):
        monkeypatch.setenv("VEL_TRUST_MODE", "active")
        monkeypatch.delenv("TRACK_GS_SOURCE", raising=False)
        key = "mn-gs-untrusted-kf"
        self._seed_kf_entry(key, v_north_true=50.0)

        entry = _mn_entry(age_s=10.0, vel_north=100.0)
        entry["vel_untrusted"] = True
        state.multinode_tracks[key] = entry
        ac = self._build_mn()

        assert "gs" in ac
        assert "track" in ac
        assert ac["gs_source"] == "kf"
        assert ac["vel_untrusted"] is True

    def test_vel_untrusted_active_mode_drops_solve_sourced_gs_track(self, monkeypatch):
        monkeypatch.setenv("VEL_TRUST_MODE", "active")
        monkeypatch.delenv("TRACK_GS_SOURCE", raising=False)
        key = "mn-gs-untrusted-nokf"
        assert track_filter.learned_velocity(key) is None  # never smoothed

        entry = _mn_entry(age_s=10.0, vel_north=100.0)
        entry["vel_untrusted"] = True
        state.multinode_tracks[key] = entry
        ac = self._build_mn()

        assert "gs" not in ac
        assert "track" not in ac
        assert "gs_source" not in ac
        assert ac["vel_untrusted"] is True


class TestVelTrustDisplay:
    """VEL_TRUST_MODE selects whether an untrusted-velocity multinode entry
    still carries gs/track for display (multinode_to_aircraft): "off"
    (default) changes nothing; "active" additionally drops gs/track from an
    entry whose vel_untrusted flag is set, since a solve-velocity vector
    this unreliable is worse than showing no heading/speed at all.  A
    trusted entry (no vel_untrusted / False) is never affected by the mode.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def test_untrusted_default_env_keeps_gs_track(self, monkeypatch):
        monkeypatch.delenv("VEL_TRUST_MODE", raising=False)
        entry = _mn_entry(age_s=10.0, vel_north=100.0)
        entry["vel_untrusted"] = True
        state.multinode_tracks["mn-dark-x"] = entry
        ac = self._build_mn()
        assert "gs" in ac
        assert "track" in ac
        assert ac["vel_untrusted"] is True

    def test_untrusted_active_mode_drops_gs_track(self, monkeypatch):
        monkeypatch.setenv("VEL_TRUST_MODE", "active")
        age_s = 10.0
        entry = _mn_entry(age_s=age_s, vel_north=100.0)
        entry["vel_untrusted"] = True
        state.multinode_tracks["mn-dark-x"] = entry
        ac = self._build_mn()
        assert "gs" not in ac
        assert "track" not in ac
        assert ac["vel_untrusted"] is True
        # lat/lon/seen are computed independently of gs/track — untouched.
        exp_lat, exp_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=100.0 * age_s)
        assert ac["lat"] == pytest.approx(exp_lat, abs=2e-4)
        assert ac["lon"] == pytest.approx(exp_lon, abs=2e-4)
        assert ac["seen"] == pytest.approx(age_s, abs=1.0)

    def test_trusted_active_mode_keeps_gs_track_no_flag(self, monkeypatch):
        monkeypatch.setenv("VEL_TRUST_MODE", "active")
        entry = _mn_entry(age_s=10.0, vel_north=100.0)
        # vel_untrusted absent -> trusted; active mode must leave it alone.
        state.multinode_tracks["mn-dark-x"] = entry
        ac = self._build_mn()
        assert "gs" in ac
        assert "track" in ac
        assert "vel_untrusted" not in ac


class TestAdsbAssistedLane:
    """The lane a multinode entry came from, exposed to the frontend.

    The emitted hex is multinode_hex_from_key's sha, so mn-adsb-* and
    mn-dark-* entries are indistinguishable on the wire without these fields
    -- and the frontend cannot pair an mn entry with the adsb_single_node
    entry for the same transponder.  The KEY is the source of truth: the
    smoother's result dict is not guaranteed to carry adsb_hex.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def test_adsb_keyed_entry_is_assisted_and_carries_the_hex(self):
        state.multinode_tracks["mn-adsb-abc123"] = _mn_entry(age_s=1.0, vel_north=100.0)
        ac = self._build_mn()
        assert ac["adsb_assisted"] is True
        assert ac["adsb_hex"] == "abc123"
        # The displayed hex stays the synthetic one -- purely additive.
        assert ac["hex"].startswith("mn")

    def test_dark_keyed_entry_is_not_assisted_and_has_no_hex(self):
        state.multinode_tracks["mn-dark-1"] = _mn_entry(age_s=1.0, vel_north=100.0)
        ac = self._build_mn()
        assert ac["adsb_assisted"] is False
        assert "adsb_hex" not in ac

    def test_result_adsb_hex_does_not_override_a_dark_key(self):
        # A dark key whose result dict happens to carry adsb_hex (verification
        # tagging) is still the dark lane -- the key decides, not the result.
        entry = _mn_entry(age_s=1.0, vel_north=100.0)
        entry["adsb_hex"] = "abc123"
        state.multinode_tracks["mn-dark-1"] = entry
        ac = self._build_mn()
        assert ac["adsb_assisted"] is False
        assert "adsb_hex" not in ac


class TestSolveUncertaintyFields:
    """pos_sigma_m / pos_sigma_vel_ms on every mn entry — the calibrated disc
    the map draws (services/solve_uncertainty.py).

    Both are optional on the wire, and the lane decides the inflation: a dark
    solve had no ADS-B fix seeding its initial guess and no pinned altitude,
    so it carries DARK_GAIN.  The lane comes from the KEY prefix, the same
    authority the adsb_assisted field uses.  The KF is reset per test so the
    velocity sigma exercises the no-KF-state fallback unless a test seeds one.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()

    def _build_mn(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        result = build_combined_aircraft_json(pipeline)
        mn = [a for a in result["aircraft"] if a.get("multinode")]
        assert len(mn) == 1
        return mn[0]

    def test_known_lane_n2_entry_carries_the_bare_floor(self):
        # No pos_sigma_km on the fixture: sigma_solve is the n=2 floor alone.
        state.multinode_tracks["mn-adsb-abc123"] = _mn_entry(age_s=1.0, vel_north=100.0)
        ac = self._build_mn()
        assert ac["pos_sigma_m"] == pytest.approx(650.0)

    def test_no_kf_state_uses_the_default_velocity_sigma(self):
        key = "mn-adsb-abc123"
        assert track_filter.learned_velocity(key) is None  # never smoothed
        state.multinode_tracks[key] = _mn_entry(age_s=1.0, vel_north=100.0)
        ac = self._build_mn()
        assert ac["pos_sigma_vel_ms"] == pytest.approx(25.0)

    def test_dark_lane_gets_the_gain_and_the_known_lane_does_not(self):
        state.multinode_tracks["mn-dark-1"] = _mn_entry(age_s=1.0, vel_north=100.0)
        dark = self._build_mn()
        state.multinode_tracks.clear()
        state.multinode_tracks["mn-adsb-abc123"] = _mn_entry(age_s=1.0, vel_north=100.0)
        known = self._build_mn()

        assert known["adsb_assisted"] is True
        assert dark["adsb_assisted"] is False
        assert known["pos_sigma_m"] == pytest.approx(650.0)
        assert dark["pos_sigma_m"] == pytest.approx(650.0 * 1.5)

    def test_result_adsb_hex_does_not_soften_a_dark_key(self):
        # Same key-is-authoritative rule as adsb_assisted: a dark key whose
        # result dict happens to carry adsb_hex is still the dark lane, so it
        # still gets the gain.
        entry = _mn_entry(age_s=1.0, vel_north=100.0)
        entry["adsb_hex"] = "abc123"
        state.multinode_tracks["mn-dark-1"] = entry
        ac = self._build_mn()
        assert ac["pos_sigma_m"] == pytest.approx(650.0 * 1.5)

    def test_formal_sigma_widens_the_disc(self):
        entry = _mn_entry(age_s=1.0, vel_north=100.0)
        entry["pos_sigma_km"] = 1.0
        state.multinode_tracks["mn-adsb-abc123"] = entry
        ac = self._build_mn()
        assert ac["pos_sigma_m"] == pytest.approx(math.sqrt(1000.0**2 + 650.0**2), abs=0.1)

    def test_kf_state_supplies_the_velocity_sigma(self):
        key = "mn-adsb-abc123"
        lat2, lon2 = offset_latlon_m(LAT, LON, east_m=0.0, north_m=50.0 * 20.0)
        track_filter.smooth_solve(
            {"lat": LAT, "lon": LON, "timestamp_ms": 1_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )
        track_filter.smooth_solve(
            {"lat": lat2, "lon": lon2, "timestamp_ms": 21_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )
        lv = track_filter.learned_velocity(key)
        assert lv is not None

        state.multinode_tracks[key] = _mn_entry(age_s=1.0, vel_north=100.0)
        ac = self._build_mn()
        assert ac["pos_sigma_vel_ms"] == pytest.approx(round(lv[2], 1), abs=0.05)
        assert ac["pos_sigma_vel_ms"] != pytest.approx(25.0)


class TestMultinodeEntryFailureIsolation:
    """One bad multinode key must cost one aircraft, not the whole feed.

    The 2026-09-05 droplet failure went through here: a track whose filter
    covariance had gone non-PSD made learned_velocity raise, and because the
    per-entry work sat inline in build_combined_aircraft_json's loop the
    exception propagated out of the flush task ("Aircraft flush failed") and
    dropped the ENTIRE tick's payload -- every other aircraft with it, 91
    times in 40 minutes.  track_filter now stops that covariance ever
    forming; this is the second line of defence, which has to hold for any
    future per-entry bug, not just that one.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from services import aircraft_feed

        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        aircraft_feed._reset_for_tests()
        yield
        state.multinode_tracks.clear()
        state.track_histories.clear()
        track_filter.reset()
        aircraft_feed._reset_for_tests()

    def _build(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        return build_combined_aircraft_json(pipeline)

    def test_one_raising_entry_does_not_abort_the_build(self, monkeypatch, caplog):
        from services import aircraft_feed

        good_a, bad, good_b = "mn-dark-good-a", "mn-dark-bad", "mn-dark-good-b"
        for i, key in enumerate((good_a, bad, good_b)):
            # Spread them out: co-located entries are collapsed by
            # dedup_aircraft, which would hide the very thing under test.
            entry = _mn_entry(age_s=5.0, vel_north=100.0)
            entry["lat"] = LAT + 0.1 * i
            state.multinode_tracks[key] = entry

        real_learned_velocity = track_filter.learned_velocity

        def _boom(track_key):
            # Exactly the failure the droplet saw, raised from exactly the
            # function it was raised from.
            if track_key == bad:
                raise ValueError("math domain error")
            return real_learned_velocity(track_key)

        monkeypatch.setattr(aircraft_feed.track_filter, "learned_velocity", _boom)

        with caplog.at_level("ERROR"):
            result = self._build()

        mn_hexes = {a["hex"] for a in result["aircraft"] if a.get("multinode")}
        from services.id_utils import multinode_hex_from_key

        # The two healthy aircraft are still served ...
        assert multinode_hex_from_key(good_a) in mn_hexes
        assert multinode_hex_from_key(good_b) in mn_hexes
        # ... and only the sick one is missing.
        assert multinode_hex_from_key(bad) not in mn_hexes
        # Logged once, naming the key, so this is diagnosable rather than silent.
        assert sum("Multinode feed entry failed" in r.message for r in caplog.records) == 1
        assert bad in caplog.text

    def test_repeated_failures_are_rate_limited_to_one_log_line(self, monkeypatch, caplog):
        from services import aircraft_feed

        bad = "mn-dark-bad"
        state.multinode_tracks[bad] = _mn_entry(age_s=5.0, vel_north=100.0)

        def _boom(track_key):
            raise ValueError("math domain error")

        monkeypatch.setattr(aircraft_feed.track_filter, "learned_velocity", _boom)

        with caplog.at_level("ERROR"):
            for _ in range(20):
                # Re-stamp: the entry would otherwise age past the 60 s expiry
                # only after many more ticks, but keeping it fresh makes the
                # 20 failures unambiguous.
                state.multinode_tracks[bad] = _mn_entry(age_s=5.0, vel_north=100.0)
                self._build()

        # 20 failures inside one _MN_ENTRY_FAIL_LOG_INTERVAL_S window ->
        # exactly one line, not 20.
        assert sum("Multinode feed entry failed" in r.message for r in caplog.records) == 1
        assert aircraft_feed._mn_entry_fail_count == 20
