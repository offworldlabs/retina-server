"""Known-track HOLD (path H) — services/known_claiming._claim_holds,
state.known_track_holds, and known_lane's stale-fix seed.

Same conventions as test_known_claiming.py: a real associator geometry
registered per test, ADS-B injected through state.adsb_aircraft, claiming
driven frame by frame.

The requirement under test: once a node track is linked to an ADS-B hex the
link survives the transponder going quiet — subsequent detections in that
track stay claimed, and no other hex's dead-reckoned fix can peel them off.
"""

import random
import time

import pytest
from retina_analytics.association import predict_observation
from retina_simulation.world import NodeConfig as SimNodeConfig
from retina_simulation.world import SimulationWorld

from config.constants import FT_TO_M
from core import state
from services import known_claiming as kc
from services.tasks import known_lane

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
_NODE_ID = "test-known-hold"
_LAT, _LON = 34.88, -82.35
_ALT_BARO_FT = 7000.0 / FT_TO_M
_HEX = "abc123"


@pytest.fixture(autouse=True)
def _clean():
    state.known_claims.clear()
    state.known_track_holds.clear()
    state.adsb_aircraft.clear()
    state.multinode_tracks.clear()
    yield
    state.known_claims.clear()
    state.known_track_holds.clear()
    state.adsb_aircraft.clear()
    state.multinode_tracks.clear()


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


def _tag(lat=_LAT, lon=_LON, gs=0.0, track=0.0):
    return {"hex": _HEX, "lat": lat, "lon": lon, "alt_baro": _ALT_BARO_FT, "gs": gs, "track": track}


def _pred(geo, lat=_LAT, lon=_LON, ve=0.0, vn=0.0):
    return predict_observation(geo, lat, lon, _ALT_BARO_FT * FT_TO_M / 1000.0, ve, vn)


class TestHoldPath:
    def test_tagged_frame_then_silence_keeps_the_claim(self):
        """1. Path-1 claim on frame 1; frame 2 carries no tag and the cache is
        empty — the detection is claimed anyway, marked hold."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()])) == {0}
        assert state.known_track_holds[_NODE_ID][_HEX]["delay_us"] == pytest.approx(d)

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts + 1000, [d], [f]))
        assert claimed == {0}
        rec = state.known_claims[_HEX][-1]
        assert rec["hold"] is True
        assert rec["hold_gap_s"] == pytest.approx(1.0)
        # The STORED fix, with its original epoch, so the silence is visible.
        assert rec["adsb_fix"]["fix_ts_ms"] == ts

    def test_fresh_fix_that_agrees_refreshes_the_hold(self):
        """A live transponder that AGREES with the held track is carried by
        the hold claim and refreshes the store: on a node without tags path H
        outranks path 2 every frame, and without this the entry's fix would
        freeze at the first claim and read a live aircraft as silent."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()]))
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": _LAT,
            "lon": _LON,
            "alt_baro": _ALT_BARO_FT,
            "gs": 0,
            "track": 0,
            "timestamp_ms": ts + 1000,
            "last_seen_ms": ts + 1000,
        }
        before = state.known_hold_dropped_disagree
        assert kc.claim_known_targets(_NODE_ID, _frame(ts + 1000, [d], [f])) == {0}
        assert state.known_hold_dropped_disagree == before
        rec = state.known_claims[_HEX][-1]
        assert rec["hold"] is True
        assert rec["fix_refreshed"] is True
        assert rec["adsb_fix"]["fix_ts_ms"] == ts + 1000
        assert state.known_track_holds[_NODE_ID][_HEX]["fix"]["fix_ts_ms"] == ts + 1000

    def test_gap_beyond_the_window_expires_the_hold(self):
        """2. A gap longer than KNOWN_HOLD_MAX_GAP_S drops the entry."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()]))
        before = state.known_hold_expired

        gap_ms = int((kc.KNOWN_HOLD_MAX_GAP_S + 2.0) * 1000)
        assert kc.claim_known_targets(_NODE_ID, _frame(ts + gap_ms, [d], [f])) == set()
        assert _HEX not in state.known_track_holds.get(_NODE_ID, {})
        assert state.known_hold_expired == before + 1

    def test_fresh_fix_that_disagrees_drops_the_hold(self):
        """3. The ghost-lock guard: a live transponder that puts the aircraft
        somewhere else wins, and the hex falls through to path 2."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()]))
        before = state.known_hold_dropped_disagree

        # Fresh cached fix for the SAME hex, far from the held track.
        moved_lat, moved_lon = 34.94, -82.28
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": moved_lat,
            "lon": moved_lon,
            "alt_baro": _ALT_BARO_FT,
            "gs": 0,
            "track": 0,
            "last_seen_ms": ts + 1000,
        }
        d2, f2 = _pred(geo, lat=moved_lat, lon=moved_lon)
        assert abs(d2 - d) > kc.KNOWN_HOLD_DELAY_GATE_US + kc.KNOWN_HOLD_DELAY_RATE_US_PER_S

        # The detection is where the HOLD predicts, not where the fix does.
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts + 1000, [d], [f]))
        assert state.known_hold_dropped_disagree == before + 1
        assert claimed == set()  # path 2's gate rejects it too — correctly
        assert _HEX not in state.known_track_holds.get(_NODE_ID, {})

    def test_hold_precedes_dark_follow(self, monkeypatch):
        """4. A dark-follow target predicting the same detection does not get
        it: path H claims first, and path 3 only ever sees the leftovers."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()]))

        monkeypatch.setattr(kc.dark_follow, "mode", lambda: "binding")
        monkeypatch.setattr(
            kc.dark_follow,
            "follow_targets",
            lambda: [
                {
                    "key": "mn-dark-zzz",
                    "lat": _LAT,
                    "lon": _LON,
                    "alt_m": _ALT_BARO_FT * FT_TO_M,
                    "vel_east": 0.0,
                    "vel_north": 0.0,
                    "timestamp_ms": ts + 1000,
                    "world": None,
                }
            ],
        )
        followed: set[int] = set()
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts + 1000, [d], [f]), follow_claimed=followed)
        assert claimed == {0}
        assert followed == set()
        assert "mn-dark-zzz" not in state.known_claims

    def test_zero_gap_is_the_rollback_lever(self, monkeypatch):
        """5. KNOWN_HOLD_MAX_GAP_S = 0 reproduces pre-feature behaviour: no
        hold claim, and no store either."""
        geo = _register()
        ts = int(time.time() * 1000)
        d, f = _pred(geo)
        monkeypatch.setattr(kc, "KNOWN_HOLD_MAX_GAP_S", 0.0)
        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [d], [f], adsb=[_tag()])) == {0}
        assert state.known_track_holds == {}
        assert kc.claim_known_targets(_NODE_ID, _frame(ts + 1000, [d], [f])) == set()
        assert len(state.known_claims[_HEX]) == 1


class TestDelayRatePhysics:
    def test_delay_rate_sign_against_the_simulator(self):
        """The sign of d(delay)/dt = -doppler * 1e6 / fc, checked against the
        simulator's own generator rather than the derivation: a convention flip
        anywhere upstream would double the error instead of cancelling it."""
        # Seeded for the same reason the integration test is: the sample count
        # this asserts over is the simulator's spawn draw.
        random.seed(20260908)
        world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
        node = SimNodeConfig(
            node_id="sim-hold-1",
            rx_lat=33.939,
            rx_lon=-84.651,
            tx_lat=33.756,
            tx_lon=-84.331,
            beam_width_deg=90,
            max_range_km=120,
        )
        world.add_node(node)
        for _ in range(20):
            world.step(1.0, mode="adsb")

        # Noise-free consecutive observations of each aircraft, straight from
        # the generator's own physics helpers.
        from retina_simulation import world as wmod

        rx_alt_km = node.rx_alt_ft * 0.3048 / 1000.0
        tx_enu = wmod._lla_to_enu(
            node.tx_lat, node.tx_lon, node.tx_alt_ft * 0.3048 / 1000.0, node.rx_lat, node.rx_lon, rx_alt_km
        )

        def obs(ac):
            t_enu = wmod._lla_to_enu(ac.lat, ac.lon, ac.alt_km, node.rx_lat, node.rx_lon, rx_alt_km)
            return (
                wmod._bistatic_delay(t_enu, tx_enu, (0.0, 0.0, 0.0)),
                wmod._bistatic_doppler(
                    t_enu, (ac.vel_east, ac.vel_north, ac.vel_up), tx_enu, (0.0, 0.0, 0.0), node.fc_hz
                ),
            )

        before = {id(ac): obs(ac) for ac in world.aircraft}
        world.step(1.0, mode="adsb")
        checked = 0
        for ac in world.aircraft:
            if id(ac) not in before:
                continue
            d0, f0 = before[id(ac)]
            d1, _f1 = obs(ac)
            measured_rate = d1 - d0  # µs per 1 s step
            predicted_rate = -f0 * 1.0e6 / node.fc_hz
            if abs(measured_rate) < 0.05:
                continue  # too slow to have a sign worth asserting
            assert measured_rate * predicted_rate > 0, "delay-rate sign disagrees with Doppler"
            assert predicted_rate == pytest.approx(measured_rate, abs=0.15 + 0.2 * abs(measured_rate))
            checked += 1
        assert checked >= 3


class TestSimulationIntegration:
    def test_link_survives_the_transponder_going_quiet(self):  # noqa: C901
        """6. One node, the simulator's own aircraft and clutter: 30 s of tags,
        then 90 s of silence.  The hold keeps the aircraft's detections claimed
        and never claims clutter."""
        # Seeded: the simulator draws spawn poses, misses and noise from the
        # global RNG, which any earlier test in the run may have advanced.  A
        # rate assertion has to be measured against one fixed scenario or it is
        # measuring the run order.
        random.seed(20260908)
        world = SimulationWorld(center_lat=34.85, center_lon=-82.40)
        node_id = "sim-hold-node"
        world.add_node(
            SimNodeConfig(
                node_id=node_id,
                rx_lat=_NODE_CFG["rx_lat"],
                rx_lon=_NODE_CFG["rx_lon"],
                tx_lat=_NODE_CFG["tx_lat"],
                tx_lon=_NODE_CFG["tx_lon"],
                beam_width_deg=120,
                max_range_km=120,
            )
        )
        state.node_associator.register_node(
            node_id,
            {
                **_NODE_CFG,
                "beam_width_deg": 120,
                "max_range_km": 120,
                "fc_hz": world.nodes[node_id].fc_hz,
                "beam_azimuth_deg": world.nodes[node_id].beam_azimuth_deg,
            },
        )
        for _ in range(30):
            world.step(1.0, mode="adsb")
        # One transponder-carrying aircraft the node can see, and only it.
        target = None
        for ac in world.aircraft:
            if ac.has_adsb and world._aircraft_in_detection_cone(ac, world.nodes[node_id]):
                target = ac
                break
        if target is None:
            pytest.skip("no ADS-B aircraft in this node's cone")
        for ac in world.aircraft:
            if ac is not target:
                ac.has_adsb = False

        ts = int(time.time() * 1000)
        # 30 s with tags.
        for k in range(30):
            world.step(1.0, mode="adsb")
            frame = world.generate_detections_for_node(node_id, ts + k * 1000)
            kc.claim_known_targets(node_id, frame)
        assert state.known_track_holds.get(node_id, {}).get(target.adsb_hex) is not None

        # 90 s of silence: no tag, no cache updates.
        target.has_adsb = False
        state.adsb_aircraft.clear()
        detected = 0
        held = 0
        clutter_claims = 0
        for k in range(30, 120):
            world.step(1.0, mode="adsb")
            ts_ms = ts + k * 1000
            frame = world.generate_detections_for_node(node_id, ts_ms)
            # Which detection (if any) is the target's, from truth.
            truth_i = _truth_index(world, node_id, target, frame)
            before = len(state.known_claims.get(target.adsb_hex, ()))
            claimed = kc.claim_known_targets(node_id, frame)
            after = len(state.known_claims.get(target.adsb_hex, ()))
            if truth_i is not None:
                detected += 1
                if truth_i in claimed:
                    held += 1
            if after > before and truth_i not in claimed:
                clutter_claims += 1

        assert detected >= 20, f"target barely detected ({detected} frames)"
        assert held / detected >= 0.85, f"held {held}/{detected}"
        assert clutter_claims == 0


def _truth_index(world, node_id, target, frame):
    """Index of the target's own detection in this frame, from the simulator's
    noise-free geometry — the frame carries no identity once has_adsb is off."""
    from retina_simulation import world as wmod

    node = world.nodes[node_id]
    if not world._aircraft_in_detection_cone(target, node):
        return None
    rx_alt_km = node.rx_alt_ft * 0.3048 / 1000.0
    tx_enu = wmod._lla_to_enu(
        node.tx_lat, node.tx_lon, node.tx_alt_ft * 0.3048 / 1000.0, node.rx_lat, node.rx_lon, rx_alt_km
    )
    t_enu = wmod._lla_to_enu(target.lat, target.lon, target.alt_km, node.rx_lat, node.rx_lon, rx_alt_km)
    d = wmod._bistatic_delay(t_enu, tx_enu, (0.0, 0.0, 0.0))
    f = wmod._bistatic_doppler(
        t_enu, (target.vel_east, target.vel_north, target.vel_up), tx_enu, (0.0, 0.0, 0.0), node.fc_hz
    )
    best, best_i = None, None
    for i, (dd, ff) in enumerate(zip(frame["delay"], frame["doppler"])):
        score = abs(dd - d) / 1.0 + abs(ff - f) / 15.0
        if abs(dd - d) < 1.0 and abs(ff - f) < 15.0 and (best is None or score < best):
            best, best_i = score, i
    return best_i


class TestKnownLaneStaleSeed:
    def _claims(self, fix_age_s, ts_ms):
        fix = {
            "lat": _LAT,
            "lon": _LON,
            "alt_baro": _ALT_BARO_FT,
            "gs": 0.0,
            "track": 0.0,
            "fix_ts_ms": ts_ms - int(fix_age_s * 1000),
        }
        return {
            f"n{i}": {"node_id": f"n{i}", "delay_us": 100.0 + i, "doppler_hz": 5.0, "ts_ms": ts_ms, "adsb_fix": fix}
            for i in range(2)
        }

    def test_fresh_fix_seeds_from_the_fix(self):
        ts = int(time.time() * 1000)
        s_in = known_lane._build_solver_input(_HEX, self._claims(5.0, ts))
        assert s_in["seed_source"] == "fix"
        assert s_in["initial_guess"]["lat"] == pytest.approx(_LAT)

    def test_stale_fix_seeds_from_the_lanes_own_solve(self):
        ts = int(time.time() * 1000)
        state.multinode_tracks[f"mn-adsb-{_HEX}"] = {
            "lat": 34.90,
            "lon": -82.30,
            "vel_east": 100.0,
            "vel_north": 0.0,
            "timestamp_ms": ts - 2000,
        }
        s_in = known_lane._build_solver_input(_HEX, self._claims(600.0, ts))
        assert s_in["seed_source"] == "kf"
        assert s_in["initial_guess"]["lat"] == pytest.approx(34.90, abs=1e-4)
        # Coasted 2 s east at 100 m/s from the previous solve.
        assert s_in["initial_guess"]["lon"] > -82.30
        # Altitude still comes from the fix.
        assert s_in["initial_guess"]["alt_km"] == pytest.approx(_ALT_BARO_FT * FT_TO_M / 1000.0)
        assert s_in["initial_velocity"]["vel_east_ms"] == pytest.approx(100.0)

    def test_stale_fix_without_a_prior_solve_falls_back(self):
        ts = int(time.time() * 1000)
        s_in = known_lane._build_solver_input(_HEX, self._claims(600.0, ts))
        assert s_in["seed_source"] == "fix"
