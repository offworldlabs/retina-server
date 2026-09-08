"""Known-track FOLLOW and the two guards that keep a silent aircraft on its
own hex through a turn and a node handoff.

A. track_filter._adsb_velocity ignores a fix older than the claim cap, so a
   pre-silence heading cannot steer a live radar track.
B. known_lane re-anchors after two self-consistent kf-seeded ghosts.
C. known_claiming offers the lane's own published entry as a path-2 candidate
   on a node with no hold and no fresh fix.
"""

import time

import pytest
from retina_analytics.association import predict_observation

from config.constants import FT_TO_M
from core import state
from services import known_claiming as kc
from services import track_filter
from services.tasks import known_lane
from services.tasks import solver as solver_mod

_NODE_CFG = {
    "rx_lat": 34.85,
    "rx_lon": -82.40,
    "rx_alt_ft": 1000,
    "tx_lat": 34.9412,
    "tx_lon": -82.4103,
    "fc_hz": 183e6,
    "tx_alt_ft": 2000,
    "beam_width_deg": 90,
    "max_range_km": 60,
    "beam_azimuth_deg": 45.0,
}
_NODE_A = "test-known-follow-a"
_NODE_B = "test-known-follow-b"
_LAT, _LON = 34.88, -82.35
_ALT_M = 7000.0
_ALT_BARO_FT = _ALT_M / FT_TO_M
_HEX = "abc123"
_KEY = f"mn-adsb-{_HEX}"


@pytest.fixture(autouse=True)
def _clean():
    state.known_claims.clear()
    state.known_track_holds.clear()
    state.adsb_aircraft.clear()
    state.multinode_tracks.clear()
    track_filter.reset()
    known_lane._reset_for_tests()
    yield
    state.known_claims.clear()
    state.known_track_holds.clear()
    state.adsb_aircraft.clear()
    state.multinode_tracks.clear()
    track_filter.reset()
    known_lane._reset_for_tests()


def _register(node_id):
    state.node_associator.register_node(node_id, _NODE_CFG)
    return state.node_associator.node_geometries[node_id]


def _frame(ts_ms, delays, dopplers):
    return {
        "timestamp": ts_ms,
        "delay": list(delays),
        "doppler": list(dopplers),
        "snr": [20.0] * len(delays),
    }


def _pred(geo, lat=_LAT, lon=_LON, ve=0.0, vn=0.0):
    return predict_observation(geo, lat, lon, _ALT_M / 1000.0, ve, vn)


# ── A. stale ADS-B velocity never steers the filter ──────────────────────────


class TestStaleAdsbVelocity:
    def test_a_stale_eastbound_fix_does_not_hold_the_filter_east(self):
        """The aircraft went silent and then turned south.  The cache still
        holds its last eastbound report; the solves march south.  The filter
        must learn the southward velocity it can see, not the eastward one it
        was told about a minute ago."""
        t0 = int(time.time() * 1000)
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": _LAT,
            "lon": _LON,
            "gs": 400.0,
            "track": 90.0,  # due east
            "last_seen_ms": t0 - 60_000,
            "timestamp_ms": t0 - 60_000,
        }
        lat = _LAT
        for i in range(8):
            lat -= 0.02  # ~2.2 km south every 5 s ≈ 445 m/s southbound
            track_filter.smooth_solve(
                {
                    "lat": lat,
                    "lon": _LON,
                    "alt_km": _ALT_M / 1000.0,
                    "timestamp_ms": t0 + i * 5000,
                    "n_nodes": 4,
                },
                _KEY,
                _HEX,
            )

        lv = track_filter.learned_velocity(_KEY)
        assert lv is not None
        vel_east, vel_north = lv[0], lv[1]
        assert vel_north < -100.0, f"expected a southward velocity, got {vel_north}"
        assert abs(vel_east) < abs(vel_north)

    def test_a_fresh_fix_still_seeds_the_velocity(self):
        """The guard is about AGE, not about ADS-B: a fix from this epoch is
        still the tightest velocity the filter can get."""
        t0 = int(time.time() * 1000)
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": _LAT,
            "lon": _LON,
            "gs": 400.0,
            "track": 90.0,
            "last_seen_ms": t0,
            "timestamp_ms": t0,
        }
        has_vel, ve, vn = track_filter._adsb_velocity(_HEX, t0)
        assert has_vel
        assert ve > 100.0 and abs(vn) < 1.0
        assert track_filter._adsb_velocity(_HEX, t0 + 60_000) == (False, 0.0, 0.0)


# ── B. re-anchoring after a kf-seeded ghost ──────────────────────────────────


def _solver_input(seed_source, ts_ms):
    return {
        "initial_guess": {"lat": _LAT, "lon": _LON, "alt_km": _ALT_M / 1000.0},
        "seed_source": seed_source,
        "initial_velocity": {"vel_east_ms": 0.0, "vel_north_ms": 0.0},
        "measurements": [
            {"node_id": _NODE_A, "delay_us": 100.0, "doppler_hz": 5.0, "snr": 20.0, "t_s": ts_ms / 1000.0},
            {"node_id": _NODE_B, "delay_us": 101.0, "doppler_hz": 6.0, "snr": 20.0, "t_s": ts_ms / 1000.0},
        ],
        "n_nodes": 2,
        "timestamp_ms": ts_ms,
        "adsb_hex": _HEX,
        "known_lane": True,
    }


def _ghost_solve(lat, lon, ts_ms):
    def _fn(_s_in, _cfgs):
        return {
            "success": True,
            "lat": lat,
            "lon": lon,
            "alt_km": _ALT_M / 1000.0,
            "timestamp_ms": ts_ms,
            "n_nodes": 2,
            "vel_east": 0.0,
            "vel_north": 0.0,
        }

    return _fn


class TestReanchor:
    @pytest.fixture(autouse=True)
    def _no_epoch_align(self, monkeypatch):
        monkeypatch.setattr(state, "SOLVER_EPOCH_ALIGN", False)

    def test_b_two_consistent_kf_ghosts_reanchor_and_publish(self):
        """The prior drifted (the aircraft turned while silent); the solves
        agree with each other.  The second one is taken as truth."""
        t0 = int(time.time() * 1000)
        # Well beyond _MAX_DISPLACEMENT_KM from the prior, but the two solves
        # are one another's neighbours.
        far_lat = _LAT + 0.15
        before_ghost = state.known_lane_ghost
        before_reanchor = getattr(state, "known_lane_reanchored", 0)

        known_lane._attempt(_HEX, _solver_input("kf", t0), {}, _ghost_solve(far_lat, _LON, t0), "binding")
        assert state.known_lane_ghost == before_ghost + 1
        assert _KEY not in state.multinode_tracks

        known_lane._attempt(
            _HEX,
            _solver_input("kf", t0 + 3000),
            {},
            _ghost_solve(far_lat + 0.001, _LON, t0 + 3000),
            "binding",
        )
        assert state.known_lane_reanchored == before_reanchor + 1
        assert state.known_lane_ghost == before_ghost + 1
        assert _KEY in state.multinode_tracks

        rec = state.mlat_solve_history_known[-1]
        assert rec["outcome"] == "known_reanchored"
        assert rec["label"] == "reanchored"
        assert rec["seed_source"] == "kf"
        assert rec["published"] is True

    def test_b_a_fix_seeded_ghost_never_reanchors(self):
        """A live transponder is the lane's truth gate: with a fix-seeded
        prior a repeated disagreement is a wrong solve, not a moved prior."""
        t0 = int(time.time() * 1000)
        far_lat = _LAT + 0.15
        before_reanchor = getattr(state, "known_lane_reanchored", 0)

        for i in range(3):
            known_lane._attempt(
                _HEX,
                _solver_input("fix", t0 + i * 3000),
                {},
                _ghost_solve(far_lat, _LON, t0 + i * 3000),
                "binding",
            )
        assert state.known_lane_reanchored == before_reanchor
        assert _KEY not in state.multinode_tracks

    def test_b_inconsistent_kf_ghosts_do_not_reanchor(self):
        """Two ghosts that disagree with each other as well as with the prior
        are what a wrong solve looks like."""
        t0 = int(time.time() * 1000)
        before_reanchor = getattr(state, "known_lane_reanchored", 0)
        known_lane._attempt(_HEX, _solver_input("kf", t0), {}, _ghost_solve(_LAT + 0.15, _LON, t0), "binding")
        known_lane._attempt(
            _HEX,
            _solver_input("kf", t0 + 3000),
            {},
            _ghost_solve(_LAT - 0.15, _LON, t0 + 3000),
            "binding",
        )
        assert state.known_lane_reanchored == before_reanchor
        assert _KEY not in state.multinode_tracks


# ── C. follow claiming on a node with no hold and no fresh fix ───────────────


def _publish_entry(ts_ms, solve_count=3, lat=_LAT, lon=_LON):
    state.multinode_tracks[_KEY] = {
        "lat": lat,
        "lon": lon,
        "alt_m": _ALT_M,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": ts_ms,
        "solve_count": solve_count,
        "n_nodes": 4,
    }


class TestFollowClaim:
    def test_c_a_new_node_claims_from_the_lanes_own_position(self):
        geo = _register(_NODE_B)
        ts = int(time.time() * 1000)
        _publish_entry(ts - 5000)
        d, f = _pred(geo)
        before = state.known_follow_claims

        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == {0}
        assert state.known_follow_claims == before + 1
        rec = state.known_claims[_HEX][-1]
        assert rec["follow"] is True
        assert rec["adsb_fix"]["lat"] == pytest.approx(_LAT)
        # ...and the node now HOLDS the track, so the next frame needs no
        # candidate at all.
        assert _HEX in state.known_track_holds[_NODE_B]

    def test_c_the_original_stale_fix_is_carried_when_a_hold_has_one(self):
        """Node A held the hex from before the silence; node B's follow claim
        must carry A's original fix, so the lane still reads the aircraft as
        silent and seeds from its own solve rather than from the fix."""
        _register(_NODE_A)
        geo_b = _register(_NODE_B)
        ts = int(time.time() * 1000)
        state.known_track_holds[_NODE_A] = {
            _HEX: {
                "delay_us": 1.0,
                "doppler_hz": 1.0,
                "ts_ms": ts - 60_000,
                "prev_delay_us": None,
                "prev_doppler_hz": None,
                "prev_ts_ms": None,
                "fix": {
                    "lat": _LAT,
                    "lon": _LON,
                    "alt_baro": _ALT_BARO_FT,
                    "gs": 0.0,
                    "track": 0.0,
                    "fix_ts_ms": ts - 90_000,
                },
                "world": None,
                "n_claims": 1,
                "n_hold": 0,
            }
        }
        _publish_entry(ts - 5000)
        d, f = _pred(geo_b)
        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == {0}
        assert state.known_claims[_HEX][-1]["adsb_fix"]["fix_ts_ms"] == ts - 90_000

    def test_c_nothing_is_offered_for_a_stale_entry(self):
        geo = _register(_NODE_B)
        ts = int(time.time() * 1000)
        _publish_entry(ts - int((kc.KNOWN_FOLLOW_MAX_AGE_S + 5.0) * 1000))
        d, f = _pred(geo)
        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == set()

    def test_c_nothing_is_offered_below_the_solve_count(self):
        geo = _register(_NODE_B)
        ts = int(time.time() * 1000)
        _publish_entry(ts - 5000, solve_count=kc.KNOWN_FOLLOW_MIN_SOLVES - 1)
        d, f = _pred(geo)
        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == set()

    def test_c_a_fresh_fix_stays_with_path_2(self):
        """Path 2 already has the better candidate; the same aircraft must not
        enter the assignment twice, and the claim carries no follow mark."""
        geo = _register(_NODE_B)
        ts = int(time.time() * 1000)
        _publish_entry(ts - 5000)
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": _LAT,
            "lon": _LON,
            "alt_baro": _ALT_BARO_FT,
            "alt_m": _ALT_M,
            "gs": 0.0,
            "track": 0.0,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "timestamp_ms": ts,
            "last_seen_ms": ts,
        }
        d, f = _pred(geo)
        before = state.known_follow_claims
        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == {0}
        assert state.known_follow_claims == before
        assert "follow" not in state.known_claims[_HEX][-1]

    def test_c_the_path_is_off_when_the_max_age_is_zero(self, monkeypatch):
        geo = _register(_NODE_B)
        monkeypatch.setattr(kc, "KNOWN_FOLLOW_MAX_AGE_S", 0.0)
        ts = int(time.time() * 1000)
        _publish_entry(ts - 5000)
        d, f = _pred(geo)
        assert kc.claim_known_targets(_NODE_B, _frame(ts, [d], [f])) == set()


def test_solver_stats_export_the_new_counters():
    """Both counters are readable where the operator looks for them."""
    assert hasattr(state, "known_follow_claims")
    assert hasattr(state, "known_lane_reanchored")
    assert solver_mod is not None
