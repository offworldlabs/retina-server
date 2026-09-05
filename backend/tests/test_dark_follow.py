"""Dark track following (DARK_FOLLOW_MODE) — services/dark_follow.py,
known_claiming's path 3, known_lane's follow pass, and the solver's anchor
dead-reckoning.

The lane is the known lane's shape applied to aircraft with no transponder:
an established mn-dark-* track's Kalman state stands in for a dead-reckoned
ADS-B fix, and the solve it produces carries the followed key as its anchor so
it lands back on the same track.  Pinned here:

- pseudo-state eligibility (age, solve count, node count, filter state, and
  the velocity-sigma ceiling that also drops the key);
- claiming: a matching detection is claimed and a non-matching one is not, an
  ADS-B state always beats a dark pseudo-state for the same detection, and
  ``off`` claims nothing;
- the follow pass: one queue item per key with the anchor, the prediction as
  initial guess and no track provenance; the per-key rate limit; shadow
  records without publishing and binding reaches the queue;
- binding mode removing the claimed detections from the frame the dark lane
  sees (frame_processor);
- the ghost guard: two rejected follow-solves drop the key for the cooldown,
  including when the verdicts arrive through _record_solve_history;
- key ownership in binding mode: a bottom-up solve may not join a key the lane
  just published on, is refused outright inside DARK_FOLLOW_SHADOW_KM, and
  those refusals are invisible to the guard.

Style follows test_known_claiming.py (registered associator geometry, frames
built around a real predicted observation) and test_solver_anchor.py.
"""

import time

import pytest
from retina_analytics.association import predict_observation

from config.constants import FT_TO_M
from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services import dark_follow, track_filter
from services import known_claiming as kc
from services.frame_processor import process_one_frame
from services.geo import offset_latlon_m
from services.tasks import known_lane
from services.tasks import solver as solver_mod

_NODE_CFG = {
    "rx_lat": 34.85,
    "rx_lon": -82.40,
    "rx_alt_ft": 1000,
    "tx_lat": 34.9412,
    "tx_lon": -82.4103,
    "tx_alt_ft": 2000,
    "fc_hz": 183e6,
    "beam_width_deg": 90,
    "max_range_km": 60,
    "beam_azimuth_deg": 45.0,
}

_NODE_ID = "test-dark-follow"
_KEY = "mn-dark-followed"

# Well inside _NODE_CFG's beam — the same corner of Greenville the claiming
# tests use, so the visibility gate is never the thing under test.
_LAT, _LON, _ALT_M = 34.88, -82.35, 7000.0


@pytest.fixture(autouse=True)
def _lane_off_unless_armed(monkeypatch):
    """Leave the live flag off between tests.

    state.DARK_FOLLOW_MODE defaults to "shadow", and every solver worker daemon
    a TestClient lifespan has leaked into this process polls it — arming it
    globally would let one race these tests for the per-key rate limit, the
    same hazard test_known_lane.py documents for KNOWN_LANE_MODE.  Tests that
    need the lane arm it themselves (``_install``) or pass ``mode`` explicitly.
    """
    monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "off")


def _register(node_id=_NODE_ID):
    state.node_associator.register_node(node_id, _NODE_CFG)
    return state.node_associator.node_geometries[node_id]


def _frame(ts_ms, delays, dopplers, adsb=None):
    f = {
        "timestamp": ts_ms,
        "delay": list(delays),
        "doppler": list(dopplers),
        "snr": [20.0] * len(delays),
    }
    if adsb is not None:
        f["adsb"] = adsb
    return f


def _track(ts_ms, **overrides) -> dict:
    """A live, followable mn-dark-* entry in state.multinode_tracks."""
    rec = {
        "lat": _LAT,
        "lon": _LON,
        "alt_m": _ALT_M,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": ts_ms,
        "n_nodes": 3,
        "solve_count": 5,
        "contributing_node_ids": [_NODE_ID],
    }
    rec.update(overrides)
    return rec


def _kf(monkeypatch, vel_east=0.0, vel_north=0.0, vel_sigma=5.0, keys=(_KEY,)):
    """Fake filter state for ``keys`` — the real KF is fed by the publish path,
    which none of these tests go through."""
    lookup = {k: (vel_east, vel_north, vel_sigma, 0.0) for k in keys}
    monkeypatch.setattr(track_filter, "learned_velocity", lookup.get)


def _install(monkeypatch, ts_ms, mode="shadow", key=_KEY, kf=True, **overrides):
    """Register the node, install one followable track, arm the lane."""
    geo = _register()
    monkeypatch.setattr(state, "DARK_FOLLOW_MODE", mode)
    state.multinode_tracks[key] = _track(ts_ms, **overrides)
    if kf:
        _kf(monkeypatch, keys=(key,))
    else:
        monkeypatch.setattr(track_filter, "learned_velocity", lambda _k: None)
    # The target list is TTL-cached; a test that rewrites multinode_tracks has
    # to invalidate it or it reads the previous assertion's world.
    dark_follow._reset_for_tests()
    return geo


def _pred(geo, lat=_LAT, lon=_LON, alt_m=_ALT_M, ve=0.0, vn=0.0):
    return predict_observation(geo, lat, lon, alt_m / 1000.0, ve, vn)


def _follow_claim(node_id, ts_ms, delay_us, doppler_hz, lat=_LAT, lon=_LON):
    return {
        "node_id": node_id,
        "delay_us": delay_us,
        "doppler_hz": doppler_hz,
        "pred_delay_us": delay_us,
        "pred_doppler_hz": doppler_hz,
        "ts_ms": ts_ms,
        "dark_follow": True,
        "follow_fix": {
            "lat": lat,
            "lon": lon,
            "alt_km": _ALT_M / 1000.0,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "fix_ts_ms": ts_ms,
        },
        "contested": False,
    }


def _install_follow_claims(node_ids, ts_ms, key=_KEY):
    from collections import deque

    dq = state.known_claims.setdefault(key, deque(maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX))
    for i, nid in enumerate(node_ids):
        dq.append(_follow_claim(nid, ts_ms, 100.0 + i, 10.0 + i))
    return dq


def _reject_result():
    """A minimal converged solver result, enough for a history record."""
    return {
        "success": True,
        "lat": _LAT,
        "lon": _LON,
        "alt_m": _ALT_M,
        "n_nodes": 3,
        "timestamp_ms": int(time.time() * 1000),
        "contributing_node_ids": ["n1", "n2", "n3"],
    }


def _drain_queue():
    items = []
    while True:
        try:
            items.append(state.solver_queue.get_nowait())
        except Exception:
            return items


class TestPseudoStates:
    """Which dark tracks may be followed at all."""

    def test_an_established_track_is_a_target(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts)

        (t,) = dark_follow.follow_targets()

        assert t["key"] == _KEY
        assert t["lat"] == _LAT
        assert t["pos_sigma_m"] == dark_follow._DEFAULT_POS_SIGMA_M
        assert state.dark_follow_targets == 1

    def test_a_stale_track_is_not(self, monkeypatch):
        ts = int((time.time() - dark_follow.DARK_FOLLOW_MAX_AGE_S - 5) * 1000)
        _install(monkeypatch, ts)

        assert dark_follow.follow_targets() == []

    def test_too_few_solves_is_not(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts, solve_count=dark_follow.DARK_FOLLOW_MIN_SOLVES - 1)

        assert dark_follow.follow_targets() == []

    def test_too_few_nodes_is_not(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts, n_nodes=2)

        assert dark_follow.follow_targets() == []

    def test_no_filter_state_is_not(self, monkeypatch):
        """The velocity and its sigma ARE the prediction; without them there is
        nothing to dead-reckon with and no honest way to widen a gate."""
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts, kf=False)

        assert dark_follow.follow_targets() == []

    def test_an_adsb_key_is_never_followed(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts, key="mn-adsb-abc123")

        assert dark_follow.follow_targets() == []

    def test_a_noisy_velocity_drops_the_key(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts)
        _kf(monkeypatch, vel_sigma=dark_follow.DARK_FOLLOW_MAX_VEL_SIGMA_MS + 1.0)
        dark_follow._reset_for_tests()

        assert dark_follow.follow_targets() == []
        assert state.dark_follow_dropped == 1

    def test_off_mode_has_no_targets(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts, mode="off")

        assert dark_follow.follow_targets() == []


class TestClaiming:
    """Path 3 of the claiming stage: leftover detections vs pseudo-states."""

    def test_a_matching_detection_is_claimed_and_a_stray_is_not(self, monkeypatch):
        ts = int(time.time() * 1000)
        geo = _install(monkeypatch, ts - 2000)
        pd, pf = _pred(geo)
        followed: set[int] = set()

        claimed = kc.claim_known_targets(
            _NODE_ID,
            _frame(ts, [pd, pd + 500.0], [pf, pf + 500.0]),
            follow_claimed=followed,
        )

        assert claimed == set()
        assert followed == {0}
        assert state.dark_follow_claims == 1
        (c,) = list(state.known_claims[_KEY])
        assert c["dark_follow"] is True
        assert c["node_id"] == _NODE_ID
        assert c["delay_us"] == pytest.approx(pd)
        # follow_fix, never adsb_fix: the feed's single-node ADS-B section and
        # the node-trust residuals both key on adsb_fix, and a follow claim has
        # no transponder fix to offer them.
        assert "adsb_fix" not in c
        assert c["follow_fix"]["lat"] == pytest.approx(_LAT)

    def test_adsb_wins_a_contested_detection(self, monkeypatch):
        """One detection both a cached transponder fix and a followed track
        explain.  The ADS-B paths run first and path 3 only ever sees what they
        left, so the aircraft with an identity keeps it."""
        ts = int(time.time() * 1000)
        geo = _install(monkeypatch, ts - 2000)
        state.adsb_aircraft["abc123"] = {
            "hex": "abc123",
            "lat": _LAT,
            "lon": _LON,
            "alt_baro": _ALT_M / FT_TO_M,
            "gs": 0,
            "track": 0,
            "last_seen_ms": ts,
        }
        pd, pf = _pred(geo)
        followed: set[int] = set()

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]), follow_claimed=followed)

        assert claimed == {0}
        assert followed == set()
        assert _KEY not in state.known_claims
        assert state.dark_follow_claims == 0

    def test_off_mode_claims_nothing(self, monkeypatch):
        ts = int(time.time() * 1000)
        geo = _install(monkeypatch, ts - 2000, mode="off")
        pd, pf = _pred(geo)
        followed: set[int] = set()

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]), follow_claimed=followed)

        assert followed == set()
        assert state.known_claims == {}

    def test_a_caller_that_cannot_take_the_split_gets_no_path_3(self, monkeypatch):
        """Omitting follow_claimed disables path 3: the two lanes have
        independent binding modes, so a caller that cannot separate them must
        not be handed a set it would strip wholesale."""
        ts = int(time.time() * 1000)
        geo = _install(monkeypatch, ts - 2000)
        pd, pf = _pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == set()
        assert state.known_claims == {}

    def test_a_stale_track_claims_nothing(self, monkeypatch):
        ts = int(time.time() * 1000)
        geo = _register()
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "shadow")
        state.multinode_tracks[_KEY] = _track(int((time.time() - 120) * 1000))
        _kf(monkeypatch)
        dark_follow._reset_for_tests()
        pd, pf = _pred(geo)
        followed: set[int] = set()

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]), follow_claimed=followed)

        assert followed == set()


class TestFollowPass:
    """known_lane's second pass: claims → solver input."""

    _CFGS = {"n1": _NODE_CFG, "n2": _NODE_CFG, "n3": _NODE_CFG}

    @pytest.fixture(autouse=True)
    def _private_queue(self, monkeypatch):
        """A queue of this test's own: a leaked solver worker daemon drains
        state.solver_queue, and would take the item under assertion."""
        import queue

        monkeypatch.setattr(state, "solver_queue", queue.Queue(maxsize=200))

    def test_three_nodes_produce_one_anchored_queue_item(self):
        ts = int(time.time() * 1000)
        _install_follow_claims(["n1", "n2", "n3"], ts)

        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 1

        (item,) = _drain_queue()
        s_in, cfgs, _enqueued = item
        assert s_in["anchor_key"] == _KEY
        assert s_in["follow_key"] == _KEY
        assert s_in["lane"] == "dark_follow"
        assert s_in["guess_source"] == "prediction"
        assert s_in["track_ids"] == []
        assert s_in["n_nodes"] == 3
        assert s_in["initial_guess"]["lat"] == pytest.approx(_LAT)
        assert s_in["initial_guess"]["lon"] == pytest.approx(_LON)
        assert s_in["initial_guess"]["alt_km"] == pytest.approx(_ALT_M / 1000.0)
        assert set(cfgs) == {"n1", "n2", "n3"}
        assert state.dark_follow_inputs == 1

    def test_a_single_node_is_not_enough(self):
        _install_follow_claims(["n1"], int(time.time() * 1000))

        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 0
        assert _drain_queue() == []

    def test_the_rate_limit_holds_between_passes(self):
        ts = int(time.time() * 1000)
        _install_follow_claims(["n1", "n2"], ts)
        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 1

        # A newer claim set, but inside DARK_FOLLOW_INTERVAL_S.
        _install_follow_claims(["n1", "n2"], ts + 500)
        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 0
        assert len(_drain_queue()) == 1

    def test_no_newer_claim_is_no_new_solve(self, monkeypatch):
        monkeypatch.setattr(dark_follow, "DARK_FOLLOW_INTERVAL_S", 0.0)
        _install_follow_claims(["n1", "n2"], int(time.time() * 1000))
        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 1

        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 0

    def test_off_mode_does_nothing(self):
        _install_follow_claims(["n1", "n2"], int(time.time() * 1000))

        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="off") == 0
        assert _drain_queue() == []

    def test_shadow_records_and_never_publishes(self):
        ts = int(time.time() * 1000)
        _install_follow_claims(["n1", "n2"], ts)

        def solve(s_in, cfgs):
            ig = s_in["initial_guess"]
            return {
                "success": True,
                "lat": ig["lat"],
                "lon": ig["lon"],
                "alt_m": ig["alt_km"] * 1000.0,
                "timestamp_ms": s_in["timestamp_ms"],
                "n_nodes": s_in["n_nodes"],
                "contributing_node_ids": ["n1", "n2"],
            }

        assert known_lane.run_dark_follow_pass(solve, self._CFGS, mode="shadow") == 1

        assert _drain_queue() == []
        assert state.multinode_tracks == {}
        assert state.dark_follow_published == 0
        (rec,) = [r for r in state.mlat_solve_history if r.get("follow_key")]
        assert rec["outcome"] == "dark_follow_shadow"
        assert rec["lane"] == "dark_follow"
        assert rec["guess_source"] == "prediction"
        assert rec["published"] is False
        assert rec["displacement_km"] == pytest.approx(0.0, abs=1e-3)

    def test_shadow_classifies_a_displaced_solve_as_not_ok(self):
        """The shadow verdict is the dark displacement cap — the same number
        the binding path's gate would judge the solve by — so the guard is not
        inert for the whole soak."""
        _install_follow_claims(["n1", "n2"], int(time.time() * 1000))

        def solve(s_in, cfgs):
            ig = s_in["initial_guess"]
            return {
                "success": True,
                "lat": ig["lat"] + 0.5,  # ~55 km
                "lon": ig["lon"],
                "timestamp_ms": s_in["timestamp_ms"],
                "n_nodes": s_in["n_nodes"],
                "contributing_node_ids": ["n1", "n2"],
            }

        known_lane.run_dark_follow_pass(solve, self._CFGS, mode="shadow")

        (rec,) = [r for r in state.mlat_solve_history if r.get("follow_key")]
        assert rec["follow_ok"] is False

    def test_a_node_with_no_config_is_dropped_not_the_key(self):
        ts = int(time.time() * 1000)
        _install_follow_claims(["n1", "n2", "gone"], ts)

        assert known_lane.run_dark_follow_pass(None, self._CFGS, mode="binding") == 1

        (item,) = _drain_queue()
        assert set(item[1]) == {"n1", "n2"}
        assert item[0]["n_nodes"] == 2

    def test_the_adsb_pass_ignores_follow_claims(self):
        """Both passes read state.known_claims; neither may solve the other's
        entries."""
        _install_follow_claims(["n1", "n2"], int(time.time() * 1000))

        assert known_lane.run_known_lane_pass(lambda s, c: None, self._CFGS, mode="shadow") == 0


class TestModesInProcessOneFrame:
    """Binding removes the followed detections from the frame the dark lane
    processes; shadow leaves the frame whole."""

    def _run(self, monkeypatch, mode):
        ts = int(time.time() * 1000)
        geo = _install(monkeypatch, ts - 2000, mode=mode)
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")
        pd, pf = _pred(geo)
        frame = _frame(ts, [pd, pd + 500.0], [pf, pf + 500.0])

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        seen = []
        monkeypatch.setattr(default, "process_frame", lambda f: seen.append(f))
        process_one_frame(_NODE_ID, frame, default)
        assert len(seen) == 1
        return frame, seen[0]

    def test_binding_strips_the_followed_detection(self, monkeypatch):
        frame, processed = self._run(monkeypatch, "binding")

        assert len(processed["delay"]) == 1
        assert processed["delay"][0] == frame["delay"][1]
        # The original frame is untouched — the archive and the ADS-B cache
        # extraction still see everything the node sent.
        assert len(frame["delay"]) == 2

    def test_shadow_leaves_the_frame_whole(self, monkeypatch):
        _frame_in, processed = self._run(monkeypatch, "shadow")

        assert len(processed["delay"]) == 2
        assert state.dark_follow_claims == 1


class TestGhostGuard:
    """Following is a feedback loop; the guard is what makes it droppable."""

    def _armed(self, monkeypatch):
        ts = int(time.time() * 1000) - 2000
        _install(monkeypatch, ts)
        assert len(dark_follow.follow_targets()) == 1

    def test_two_rejected_solves_drop_the_key(self, monkeypatch):
        self._armed(monkeypatch)

        dark_follow.record_outcome(_KEY, False)
        dark_follow._expire_targets_for_tests()
        assert len(dark_follow.follow_targets()) == 1

        dark_follow.record_outcome(_KEY, False)
        dark_follow._expire_targets_for_tests()
        assert dark_follow.follow_targets() == []
        assert state.dark_follow_dropped == 1

    def test_a_good_solve_clears_the_streak(self, monkeypatch):
        self._armed(monkeypatch)

        dark_follow.record_outcome(_KEY, False)
        dark_follow.record_outcome(_KEY, True)
        dark_follow.record_outcome(_KEY, False)
        dark_follow._expire_targets_for_tests()

        assert len(dark_follow.follow_targets()) == 1
        assert state.dark_follow_dropped == 0

    def test_the_cooldown_expires(self, monkeypatch):
        self._armed(monkeypatch)
        monkeypatch.setattr(dark_follow, "DARK_FOLLOW_COOLDOWN_S", 0.0)

        dark_follow.drop_target(_KEY, "test")
        dark_follow._expire_targets_for_tests()

        assert len(dark_follow.follow_targets()) == 1

    def test_rejected_history_records_feed_the_guard(self, monkeypatch):
        """The verdicts arrive through _record_solve_history in binding mode —
        the one place every outcome of a follow-solve passes through."""
        self._armed(monkeypatch)
        s_in = {"follow_key": _KEY, "lane": "dark_follow", "n_nodes": 3}

        for _ in range(2):
            solver_mod._record_solve_history("rejected_rms_delay", s_in, _reject_result())

        dark_follow._expire_targets_for_tests()
        assert dark_follow.follow_targets() == []
        assert state.dark_follow_dropped == 1

    def test_a_published_record_counts_and_clears(self, monkeypatch):
        self._armed(monkeypatch)
        s_in = {"follow_key": _KEY, "lane": "dark_follow", "n_nodes": 3}
        solver_mod._record_solve_history("rejected_rms_delay", s_in, _reject_result())
        solver_mod._record_solve_history("published", s_in, _reject_result(), solve_key=_KEY)

        assert state.dark_follow_published == 1
        dark_follow._expire_targets_for_tests()
        assert len(dark_follow.follow_targets()) == 1


class TestAnchorDeadReckoning:
    """A follow input's anchor is stale by construction — its guess IS the
    prediction of where the anchor drifted to — so the anchor distance check
    has to dead-reckon before it measures."""

    _TS_MS = 1_000_000

    def _tracks(self, dt_s, speed_ms):
        return {
            _KEY: {
                "lat": _LAT,
                "lon": _LON,
                "vel_east": 0.0,
                "vel_north": speed_ms,
                "timestamp_ms": self._TS_MS - int(dt_s * 1000),
                "n_nodes": 3,
                "solve_count": 5,
            }
        }

    def _result(self, north_km):
        lat, lon = offset_latlon_m(_LAT, _LON, east_m=0.0, north_m=north_km * 1000.0)
        return {"lat": lat, "lon": lon, "timestamp_ms": self._TS_MS}

    def test_the_flat_gate_refuses_a_fast_anchor(self):
        """15 s of coasting at 270 m/s is 4.05 km of travel; a solve 2 km past
        that is 6.05 km from where the entry was last STORED — outside the flat
        6 km gate, purely because the aircraft moved."""
        key, how, _d = solver_mod.multinode_key_decision(
            self._tracks(15.0, 270.0),
            self._result(6.05),
            None,
            _KEY,
            learned_vel_fn=lambda _k: None,
        )
        assert how != "anchor"

    def test_dead_reckoning_honours_it(self):
        key, how, dist = solver_mod.multinode_key_decision(
            self._tracks(15.0, 270.0),
            self._result(6.05),
            None,
            _KEY,
            learned_vel_fn=lambda _k: None,
            anchor_dr=True,
        )
        assert (key, how) == (_KEY, "anchor")
        assert dist == pytest.approx(2.0, abs=0.1)

    def test_dead_reckoning_still_refuses_a_far_solve(self):
        """The check's job is unchanged: an anchor whose solve converged
        somewhere else entirely is not honoured just because it was named."""
        key, how, _d = solver_mod.multinode_key_decision(
            self._tracks(15.0, 270.0),
            self._result(30.0),
            None,
            _KEY,
            learned_vel_fn=lambda _k: None,
            anchor_dr=True,
        )
        assert how != "anchor"


class TestKeyOwnership:
    """A key the follow lane just published on is not the bottom-up lane's to
    join.

    Binding mode only, and the reason is measured rather than aesthetic: 21% of
    bottom-up proximity joins on test landed on a key belonging to a DIFFERENT
    aircraft, and same-aircraft re-key distances (p50 1.5 km) overlap the
    wrong-aircraft ones entirely, so no tighter spatial gate separates them.
    See dark_follow.DARK_FOLLOW_OWN_S.
    """

    _TS_MS = 2_000_000
    _TS_S = _TS_MS / 1000.0

    def setup_method(self):
        dark_follow._reset_for_tests()

    def teardown_method(self):
        dark_follow._reset_for_tests()

    def _tracks(self, dt_s=1.0):
        """One live dark entry at the reference position, last solved dt_s ago
        and not moving — so the distance below is exactly the offset."""
        return {
            _KEY: {
                "lat": _LAT,
                "lon": _LON,
                "vel_east": 0.0,
                "vel_north": 0.0,
                "timestamp_ms": self._TS_MS - int(dt_s * 1000),
                "n_nodes": 3,
                "solve_count": 5,
            }
        }

    def _result(self, north_km):
        lat, lon = offset_latlon_m(_LAT, _LON, east_m=0.0, north_m=north_km * 1000.0)
        return {"lat": lat, "lon": lon, "timestamp_ms": self._TS_MS}

    def _decide(self, north_km, anchor_key=None, dt_s=1.0):
        return solver_mod.multinode_key_decision(
            self._tracks(dt_s),
            self._result(north_km),
            None,
            anchor_key,
            learned_vel_fn=lambda _k: None,
        )

    def test_a_solve_next_to_a_freshly_followed_key_is_shadowed(self, monkeypatch):
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, self._TS_S - 2.0)
        key, how, dist = self._decide(1.0)
        assert (key, how) == (_KEY, "shadowed")
        assert dist == pytest.approx(1.0, abs=0.05)

    def test_a_solve_further_out_mints_rather_than_joining(self, monkeypatch):
        """4 km is well inside the 6 km proximity gate — without ownership
        this solve joins the followed key, which is the bug.  It gets its own
        key instead: too far to be the same aircraft, and never the followed
        one's."""
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, self._TS_S - 2.0)
        key, how, _dist = self._decide(4.0)
        assert how == "minted"
        assert key != _KEY

    def test_ownership_expires(self, monkeypatch):
        """Past DARK_FOLLOW_OWN_S the lane has stopped answering for the key
        (three missed follow-solve intervals), so the bottom-up lane may have
        it back."""
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, self._TS_S - 20.0)
        key, how, _dist = self._decide(1.0)
        assert (key, how) == (_KEY, "proximity")

    @pytest.mark.parametrize("mode", ["shadow", "off"])
    def test_the_inert_modes_key_exactly_as_before(self, monkeypatch, mode):
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", mode)
        dark_follow.note_follow_publish(_KEY, self._TS_S - 2.0)
        key, how, _dist = self._decide(1.0)
        assert (key, how) == (_KEY, "proximity")

    def test_the_follow_lanes_own_solve_still_lands_on_its_key(self, monkeypatch):
        """Ownership is a rule about BOTTOM-UP solves.  The follow lane's own
        solves are anchored and return from the anchor branch, which never
        reaches the proximity scan — otherwise the lane would shadow itself
        off the map."""
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, self._TS_S - 2.0)
        key, how, dist = self._decide(1.0, anchor_key=_KEY)
        assert (key, how) == (_KEY, "anchor")
        assert dist == pytest.approx(1.0, abs=0.05)

    def test_an_n2_solve_cannot_join_a_followed_n3_key(self, monkeypatch):
        """The population the ownership rule is aimed at: an n=2 bottom-up
        solve, whose own position error is ~2.4 km median, arriving at a key
        the follow lane is refreshing from n>=3 measurements."""
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, self._TS_S - 2.0)
        result = self._result(1.5)
        result["n_nodes"] = 2
        key, how, _dist = solver_mod.multinode_key_decision(
            self._tracks(),
            result,
            None,
            None,
            learned_vel_fn=lambda _k: None,
        )
        assert (key, how) == (_KEY, "shadowed")


class TestShadowedSolveIsARejection:
    """What the solver worker does with a "shadowed" verdict: record it, count
    it, and touch nothing else."""

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()
        dark_follow._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()
        dark_follow._reset_for_tests()

    def _entry(self, ts_ms):
        return {
            "success": True,
            "lat": _LAT,
            "lon": _LON,
            "alt_m": _ALT_M,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "n_nodes": 3,
            "solve_count": 5,
            "timestamp_ms": ts_ms - 1000,
            "contributing_node_ids": [_NODE_ID],
        }

    def _solve_fn(self, ts_ms, north_km):
        lat, lon = offset_latlon_m(_LAT, _LON, east_m=0.0, north_m=north_km * 1000.0)

        def fn(_s_in, _cfgs):
            return {
                "success": True,
                "lat": lat,
                "lon": lon,
                "alt_m": _ALT_M,
                "vel_east": 0.0,
                "vel_north": 0.0,
                "rms_delay": 1.0,
                "rms_doppler": 5.0,
                "n_nodes": 3,
                "n_measurements": 3,
                "timestamp_ms": ts_ms,
                "contributing_node_ids": ["n1", "n2", "n3"],
            }

        return fn

    def _run(self, monkeypatch, north_km=1.0, follow_age_s=2.0):
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        ts_ms = int(time.time() * 1000)
        state.multinode_tracks[_KEY] = self._entry(ts_ms)
        dark_follow.note_follow_publish(_KEY, ts_ms / 1000.0 - follow_age_s)
        solver_mod._process_solver_item(
            ({"n_nodes": 3}, {}, time.time()),
            self._solve_fn(ts_ms, north_km),
        )
        return ts_ms

    def test_the_solve_is_recorded_counted_and_not_published(self, monkeypatch):
        ts_ms = self._run(monkeypatch)
        assert state.dark_bottomup_shadowed == 1
        # The followed entry is byte-for-byte what it was: no new position, no
        # solve_count bump, no smoothing, and no second key minted beside it.
        assert list(state.multinode_tracks) == [_KEY]
        assert state.multinode_tracks[_KEY] == self._entry(ts_ms)
        assert len(state.mlat_solve_history) == 1
        rec = state.mlat_solve_history[0]
        assert rec["outcome"] == "shadowed_by_follow"
        assert rec["follow_key"] == _KEY
        assert rec["key_how"] == "shadowed"
        assert rec["key_dist_km"] == pytest.approx(1.0, abs=0.05)
        # A reject has no key of its own, shadowed or otherwise.
        assert rec["solve_key"] is None
        assert rec["lat"] is None

    def test_a_solve_the_lane_does_not_own_still_publishes(self, monkeypatch):
        """The same solve with the ownership window expired — the control that
        says the assertions above are about ownership and not about the
        harness."""
        self._run(monkeypatch, follow_age_s=30.0)
        assert state.dark_bottomup_shadowed == 0
        assert state.mlat_solve_history[0]["outcome"] == "published"

    def test_refusals_never_reach_the_follow_ghost_guard(self, monkeypatch):
        """A shadowed record names a followed key but was not produced BY the
        follow lane, so the guard must not hear about it — otherwise the
        bottom-up lane's refusals would drop the very track that refused
        them, twice in a row being enough."""
        _kf(monkeypatch)
        ts_ms = self._run(monkeypatch)
        state.multinode_tracks[_KEY] = self._entry(ts_ms)
        dark_follow.note_follow_publish(_KEY, ts_ms / 1000.0 - 2.0)
        solver_mod._process_solver_item(
            ({"n_nodes": 3}, {}, time.time()),
            self._solve_fn(ts_ms, 1.0),
        )
        assert state.dark_bottomup_shadowed == 2
        # Two rejects in a row is exactly what drops a followed key.  It is
        # still a target, so the guard never saw them.
        assert [t["key"] for t in dark_follow.follow_targets()] == [_KEY]
