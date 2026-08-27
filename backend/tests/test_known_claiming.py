"""Known-target claiming (KNOWN_LANE_MODE) — services/known_claiming.py,
state.known_claims, and process_one_frame's binding-mode exclusion.

Follows test_adsb_seed_backend.py's conventions: real associator geometry
registered per test, ADS-B states injected through state.adsb_aircraft or a
monkeypatched provider, process_one_frame exercised with a captured pipeline.
"""

import math
import random
import sys
import time
import types

import pytest
from retina_analytics.association import (
    _V_MAX_MS,
    CLAIM_MAX_GLOBAL_TRACKS,
    NodeGeometry,
    _point_in_beam,
    predict_observation,
)
from retina_analytics.constants import KM_PER_DEG_LAT, km_per_deg_lon, offset_latlon_m

from config.constants import FT_TO_M
from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services import known_claiming as kc
from services.frame_processor import process_one_frame

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

_NODE_ID = "test-known-claiming"

# A position/velocity well inside _NODE_CFG's beam, reused across tests.
_LAT, _LON, _ALT_KM = 34.88, -82.35, 7.0
_ALT_BARO_FT = _ALT_KM * 1000.0 / FT_TO_M


def _register(node_id=_NODE_ID):
    state.node_associator.register_node(node_id, _NODE_CFG)
    return state.node_associator.node_geometries[node_id]


def _cache_state(hexn, ts_ms, lat=_LAT, lon=_LON, alt_baro=_ALT_BARO_FT, gs=0, track=0, world=None):
    state.adsb_aircraft[hexn] = {
        "hex": hexn,
        "lat": lat,
        "lon": lon,
        "alt_baro": alt_baro,
        "gs": gs,
        "track": track,
        "flight": "TST1",
        "last_seen_ms": ts_ms,
        # None models an untagged entry (prior state, an old pusher) — the
        # world gate must let those through, so most tests leave it unset.
        **({"world": world} if world is not None else {}),
    }


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


def _stationary_pred(geo):
    """Predicted observation for the shared test aircraft at rest."""
    return predict_observation(geo, _LAT, _LON, _ALT_BARO_FT * FT_TO_M / 1000.0, 0.0, 0.0)


class TestGlobalAssignment:
    def test_one_to_one_beats_greedy_on_a_crossing_pair(self, monkeypatch):
        """Two aircraft one gate-width apart, two detections: det0 scores best
        against A but det1 is feasible ONLY against A.  Greedy (the
        associate_detections_to_adsb shape) awards A to det0 on its better
        single score and leaves det1 unclaimable; the global assignment takes
        the only complete matching (det0→B, det1→A) instead."""
        _register()
        ts = int(time.time() * 1000)
        # Keyed by lat so the fake survives the (no-op, vel=0) dead-reckoning;
        # both positions in beam, so the visibility gate keeps them candidates.
        _cache_state("aaa111", ts, lat=34.88)
        _cache_state("bbb222", ts, lat=34.90)
        monkeypatch.setattr(
            kc,
            "predict_observation",
            lambda geo, lat, lon, alt_km, ve=0.0, vn=0.0, vu=0.0: (100.0, 0.0) if lat < 34.89 else (104.0, 20.0),
        )
        # det0: A (0.5 µs, 2 Hz) score 0.13 / B (3.5, 18) score 1.07 — both feasible
        # det1: A (1.5, 6) score 0.39 / B (2.5, 26) — 26 Hz > 25 gate, infeasible
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [100.5, 101.5], [2.0, -6.0]))

        assert claimed == {0, 1}
        assert state.known_claims["aaa111"][-1]["delay_us"] == 101.5  # det1 → A
        assert state.known_claims["bbb222"][-1]["delay_us"] == 100.5  # det0 → B

    def test_ungated_detection_stays_dark(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("aaa111", ts)
        pd, pf = _stationary_pred(geo)
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 500.0], [pf + 500.0]))
        assert claimed == set()
        assert state.known_claims == {}

    def test_no_geometry_claims_nothing(self):
        ts = int(time.time() * 1000)
        _cache_state("aaa111", ts)
        claimed = kc.claim_known_targets("test-unregistered-node", _frame(ts, [50.0], [10.0]))
        assert claimed == set()
        assert state.known_claims == {}


class TestGateVsFixAge:
    """The allowance doubles linearly toward the 45 s age cap: a residual the
    base gate rejects on a fresh fix passes once dead-reckoning drift has had
    time to accumulate — and past the cap the state stops being a candidate
    at all.  vel=0 states so aging moves the gate, not the prediction."""

    def _claim_at_age(self, geo, age_s):
        ts = int(time.time() * 1000)
        _cache_state("aged01", ts - int(age_s * 1000))
        pd, pf = _stationary_pred(geo)
        # 12 µs: outside the 10 µs base gate, inside 10 × (1 + 40/45) ≈ 18.9.
        return kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 12.0], [pf]))

    def test_fresh_fix_keeps_the_base_gate(self):
        geo = _register()
        assert self._claim_at_age(geo, 0.0) == set()

    def test_aged_fix_widens_the_gate(self):
        geo = _register()
        assert self._claim_at_age(geo, 40.0) == {0}

    def test_past_the_age_cap_is_no_candidate_at_all(self):
        geo = _register()
        assert self._claim_at_age(geo, 50.0) == set()


class TestVisibilityGate:
    """Path 2 claims only what the node could have seen.  Detections sit
    exactly on the prediction throughout, so the residual gates cannot be
    what rejects them — visibility is."""

    # Behind the node: 5.7 km out (well inside the 60 km footprint) on
    # bearing 234°, against a 45° ± 45° wedge.
    _BEHIND = (34.82, -82.45)
    # On the beam axis (bearing 44°) but 199 km out — range, not bearing.
    _BEYOND = (36.12, -80.85)

    def _pred_at(self, geo, lat, lon):
        return predict_observation(geo, lat, lon, _ALT_BARO_FT * FT_TO_M / 1000.0, 0.0, 0.0)

    def test_out_of_beam_bearing_claims_nothing(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("blind1", ts, lat=self._BEHIND[0], lon=self._BEHIND[1])
        pd, pf = self._pred_at(geo, *self._BEHIND)

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert claimed == set()
        assert state.known_claims == {}
        assert state.known_claims_visibility_rejects == 1

    def test_beyond_the_footprint_claims_nothing(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("blind2", ts, lat=self._BEYOND[0], lon=self._BEYOND[1])
        pd, pf = self._pred_at(geo, *self._BEYOND)

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert claimed == set()
        assert state.known_claims == {}
        assert state.known_claims_visibility_rejects == 1

    def test_in_beam_candidate_still_claims(self):
        """The gate rejects; it does not over-reject — and the counter starts
        at zero after the tests above bumped it."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("seen01", ts)
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}
        assert state.known_claims_visibility_rejects == 0

    def test_prescreen_rejects_are_counted_as_visibility_rejects(self, monkeypatch):
        """_BEYOND is far enough out that the range prescreen rejects it
        before _point_in_beam is ever called.  The tally must not notice: a
        prescreen reject and a gate reject are the same event, and the
        published rate would otherwise change meaning without changing name."""
        _register()
        ts = int(time.time() * 1000)
        _cache_state("blind3", ts, lat=self._BEYOND[0], lon=self._BEYOND[1])
        calls = []
        monkeypatch.setattr(kc, "_point_in_beam", lambda lat, lon, geo: calls.append(1) or True)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0])) == set()

        assert calls == []  # the expensive half was not paid
        assert state.known_claims_visibility_rejects == 1

    def test_node_tag_is_not_visibility_gated(self):
        """A node tag is the node's own evidence that it saw this aircraft.
        A fix the backend's geometry calls invisible means a stale footprint
        or a wrong beam azimuth, not a phantom claim."""
        _register()
        ts = int(time.time() * 1000)
        tag = {
            "hex": "tagblind",
            "lat": self._BEHIND[0],
            "lon": self._BEHIND[1],
            "alt_baro": _ALT_BARO_FT,
            "gs": 0,
            "track": 0,
        }

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))

        assert claimed == {0}
        assert "tagblind" in state.known_claims
        assert state.known_claims_visibility_rejects == 0


class TestNodeTagPrecedence:
    def test_node_supplied_tag_wins_over_a_better_cached_match(self):
        """The node's own correlation is authoritative: even a cached hex whose
        prediction matches the detection exactly must not displace the tag."""
        geo = _register()
        ts = int(time.time() * 1000)
        pd, pf = _stationary_pred(geo)
        _cache_state("cached1", ts)  # predicts (pd, pf) — a perfect score-0 match
        tag = {"hex": "TAGGED1", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        frame = _frame(ts, [pd], [pf], adsb=[tag])

        claimed = kc.claim_known_targets(_NODE_ID, frame)

        assert claimed == {0}
        assert "tagged1" in state.known_claims  # normalize_hex_key'd
        assert "cached1" not in state.known_claims
        # And the invariant that predates this stage: the node's list is
        # never rewritten by the backend.
        assert frame["adsb"] == [tag]

    def test_tag_is_not_regated(self):
        """A tag whose residual would fail the assignment gates still claims:
        trust covers which-echo, the gates exist for the backend's own
        guesses."""
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "far001", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [9999.0], [999.0], adsb=[tag]))
        assert claimed == {0}
        rec = state.known_claims["far001"][-1]
        assert rec["delay_us"] == 9999.0
        assert rec["pred_delay_us"] != 9999.0  # prediction recorded for the residual path


class TestGroundSentinelInTags:
    """A node tag is raw feed data, so alt_baro can be the string "ground"."""

    def test_grounded_tag_still_claims(self):
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "gnd001", "lat": _LAT, "lon": _LON, "alt_baro": "ground", "gs": 0, "track": 0}

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))

        assert claimed == {0}
        # Sea level for the prediction; the fix keeps the sentinel verbatim.
        assert state.known_claims["gnd001"][-1]["adsb_fix"]["alt_baro"] == "ground"

    def test_grounded_tag_does_not_silently_disable_the_lane(self, monkeypatch):
        """The stage fails open, so a raise here is invisible except as a
        counter tick and a claim that never happened."""
        _register()
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "shadow")
        ts = int(time.time() * 1000)
        tag = {"hex": "gnd002", "lat": _LAT, "lon": _LON, "alt_baro": "ground", "gs": 0, "track": 0}
        errors_before = state.known_claims_errors

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        monkeypatch.setattr(default, "process_frame", lambda f: None)
        process_one_frame(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]), default)

        assert state.known_claims_errors == errors_before
        assert "gnd002" in state.known_claims


class _FakeFov:
    """Duck-typed stand-in for an EmpiricalCoverageState, in the two methods
    _point_in_beam calls.  Reaches FURTHER than the theoretical footprint —
    the case the prescreen radius has to widen for, and the one a radius
    taken from footprint_radius_km alone would silently truncate."""

    def __init__(self, limit_km: float, lo_deg: float, hi_deg: float):
        self._limit_km = limit_km
        self._lo, self._hi = lo_deg, hi_deg

    def max_limit_km(self) -> float:
        return self._limit_km

    def contains(self, bearing: float, dist_km: float) -> bool:
        return dist_km <= self._limit_km and self._lo <= bearing % 360.0 <= self._hi


def _geo(**over):
    base = {
        "node_id": "screen-node",
        "rx_lat": 34.85,
        "rx_lon": -82.40,
        "rx_alt_km": 0.3048,
        "tx_lat": 34.85,
        "tx_lon": -82.40,
        "tx_alt_km": 0.6096,
        "fc_hz": 183e6,
        "beam_azimuth_deg": 45.0,
        "beam_width_deg": 90.0,
        "max_range_km": 60.0,
    }
    base.update(over)
    return NodeGeometry(**base)


class TestRangePrescreen:
    """The equirectangular range prescreen standing in front of the
    visibility gate must be provably WEAKER than the gate — same verdict for
    every candidate, same reject tally, just reached without paying
    offset_latlon_m and _point_in_beam's haversine + bearing.

    A prescreen that is even fractionally tighter than the gate silently
    drops real claims into the dark lane, which nothing downstream can tell
    from an aircraft the node genuinely could not see.  So this is a
    differential test over a candidate cloud, not a handful of examples.
    """

    # Monostatic, bistatic (a long baseline moves the footprint's centre off
    # the RX), a populated coverage_limit (shrink-only, so it can only make
    # the gate tighter than the prescreen), and a learned FOV reaching past
    # the theoretical footprint (the one case that widens it).
    #
    # high_latitude is deliberately beyond anything the fleet flies: a big
    # footprint near the pole is where the prescreen's flat-earth projection
    # is worst, and it is what pins the longitude scale to the POLEWARD edge
    # of the screen rather than to rx_lat.  Taking it at rx_lat passes every
    # other geometry here and over-rejects on this one.
    #
    # antimeridian pins the longitude-delta wrap: a node at 179.95E has close
    # neighbours whose stored longitude is -179.x, so the raw difference reads
    # ~359 degrees while the gate's haversine measures the short way round.
    # Without the wrap the prescreen rejects most of the footprint's western
    # half.
    _GEOMETRIES = {
        "monostatic": _geo(),
        "bistatic": _geo(tx_lat=35.35, tx_lon=-81.90, max_bistatic_range_km=90.0),
        "coverage_limit": _geo(coverage_limit=lambda bearing: 25.0 if 30.0 <= bearing <= 60.0 else None),
        "fov": _geo(fov=_FakeFov(140.0, 10.0, 150.0)),
        "high_latitude": _geo(rx_lat=80.0, rx_lon=18.0, tx_lat=80.0, tx_lon=18.0, max_range_km=400.0),
        "antimeridian": _geo(rx_lat=52.0, rx_lon=179.95, tx_lat=52.0, tx_lon=179.95, max_range_km=120.0),
    }

    # Enough to cover the boundary densely: samples are drawn out to 1.5x the
    # screen radius, so a systematic error of even a fraction of a percent in
    # the prescreen puts candidates on the wrong side of it here.
    _N = 10000

    def _cloud(self, rng, geo, frame_ts_s):
        """A live-like ADS-B cache: random bearing/range around the node, random
        ground speed and track, random fix age within the claiming cap."""
        reach_km = geo.effective_radius_km + _V_MAX_MS * kc.KNOWN_CLAIM_MAX_FIX_AGE_S / 1000.0
        for i in range(self._N):
            rng_km = rng.uniform(0.0, 1.5 * reach_km)
            brg = math.radians(rng.uniform(0.0, 360.0))
            lat = geo.rx_lat + (rng_km * math.cos(brg)) / KM_PER_DEG_LAT
            lon = geo.rx_lon + (rng_km * math.sin(brg)) / km_per_deg_lon(geo.rx_lat)
            # Stored the way a feed reports it, in [-180, 180): a cloud around
            # a node near the antimeridian must actually straddle the seam, or
            # the geometry above tests nothing.
            lon = ((lon + 180.0) % 360.0) - 180.0
            age_s = rng.uniform(-kc.KNOWN_CLAIM_MAX_FIX_AGE_S, kc.KNOWN_CLAIM_MAX_FIX_AGE_S)
            rec = {
                "hex": f"scr{i:05d}",
                "flight": "SCR1",
                "lat": lat,
                "lon": lon,
                # Up to _V_MAX_MS (340 m/s = 661 kt), the fastest the prescreen
                # assumes a fix can have moved since it was reported.
                "alt_baro": rng.uniform(0.0, 42000.0),
                "gs": rng.uniform(0.0, 660.0),
                "track": rng.uniform(0.0, 360.0),
                "last_seen_ms": int((frame_ts_s - age_s) * 1000),
            }
            rec.update(state.adsb_derived_fields(rec))
            state.adsb_aircraft[rec["hex"]] = rec

    @pytest.mark.parametrize("name", sorted(_GEOMETRIES))
    def test_prescreen_never_disagrees_with_the_gate(self, name, monkeypatch):
        geo = self._GEOMETRIES[name]
        state.node_associator.node_geometries[_NODE_ID] = geo
        ts = int(time.time() * 1000)
        frame_ts_s = ts / 1000.0
        # String seed, not hash(name): str hashing is salted per interpreter,
        # so a failure here has to be reproducible from the test id alone.
        self._cloud(random.Random(f"prescreen-{name}"), geo, frame_ts_s)

        # Reference: the gate on its own, applied exactly where the shipped
        # loop applies it — to the dead-reckoned position, off the same
        # snapshot the shipped loop reads.
        expect_pass, expect_rejects = set(), 0
        for hexn, st in state._adsb_for_seeding().items():
            age_s = frame_ts_s - st["timestamp_ms"] / 1000.0
            if abs(age_s) > kc.KNOWN_CLAIM_MAX_FIX_AGE_S:
                continue
            dr = offset_latlon_m(st["lat"], st["lon"], east_m=st["vel_east"] * age_s, north_m=st["vel_north"] * age_s)
            if _point_in_beam(dr[0], dr[1], geo):
                expect_pass.add(dr)
            else:
                expect_rejects += 1
        # A cloud that is all-pass or all-reject would prove nothing.
        assert expect_pass and expect_rejects

        # Shipped path.  predict_observation is called once per candidate that
        # survived BOTH the prescreen and the gate, so recording its argument
        # is how the surviving set is read back out.  The detection is placed
        # far outside every residual gate so nothing is claimed and the
        # assignment stays trivial — visibility is the only thing under test.
        survived = set()

        def _record(_geo, lat, lon, *args, **kwargs):
            survived.add((lat, lon))
            return (1.0e9, 1.0e9)

        monkeypatch.setattr(kc, "predict_observation", _record)
        before = state.known_claims_visibility_rejects

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [0.0], [0.0])) == set()

        assert state.known_claims_visibility_rejects - before == expect_rejects
        # The surviving candidates, not just how many: a prescreen that traded
        # one wrong reject for one wrong pass would balance the tally.
        assert survived == expect_pass


class TestContention:
    def _global(self, key, ts_ms, n_nodes, solve_count):
        state.multinode_tracks[key] = {
            "lat": _LAT,
            "lon": _LON,
            "alt_m": _ALT_BARO_FT * FT_TO_M,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "vel_up": 0.0,
            "timestamp_ms": ts_ms,
            "n_nodes": n_nodes,
            "solve_count": solve_count,
        }

    def test_claim_over_an_established_dark_track_is_contested(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont01", ts)
        self._global("mn-dark-cont1", ts - 5000, n_nodes=3, solve_count=5)
        pd, pf = _stationary_pred(geo)

        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert claimed == {0}
        assert state.known_claims["cont01"][-1]["contested"] is True
        assert state.known_claim_contentions == 1
        # The dark track is flagged against, never suppressed.
        assert "mn-dark-cont1" in state.multinode_tracks

    def test_ineligible_dark_track_does_not_contest(self):
        """A one-shot unconfirmed solve fails claim_eligible, so it is not an
        "established" track and must not mark real claims contested."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont02", ts)
        self._global("mn-dark-cont2", ts - 5000, n_nodes=2, solve_count=1)
        pd, pf = _stationary_pred(geo)

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        assert state.known_claims["cont02"][-1]["contested"] is False
        assert state.known_claim_contentions == 0

    def _fill_dark_globals(self, ts, n, lat, lon):
        """n eligible dark globals, one per millisecond of fix age, oldest
        first — so `key` order and timestamp order are the same and a test can
        say which end of the cap a track lands on."""
        for i in range(n):
            self._global(f"mn-dark-bulk{i:04d}", ts - 20000 - (n - i), n_nodes=3, solve_count=5)
            rec = state.multinode_tracks[f"mn-dark-bulk{i:04d}"]
            rec["lat"], rec["lon"] = lat, lon

    def test_projection_set_is_capped(self):
        """_dark_global_projections calls the track provider directly rather
        than through the associator, so it has to apply the library's own
        CLAIM_MAX_GLOBAL_TRACKS truncation itself.  Uncapped, this is a
        predict_observation per dark global per claiming frame."""
        geo = _register()
        ts = int(time.time() * 1000)
        over = CLAIM_MAX_GLOBAL_TRACKS + 50
        # Well away from the claim, so none of these contest by position.
        self._fill_dark_globals(ts, over, lat=_LAT + 2.0, lon=_LON + 2.0)

        projections = kc._dark_global_projections(geo, ts / 1000.0)

        assert len(state.multinode_tracks) == over
        assert len(projections) == CLAIM_MAX_GLOBAL_TRACKS

    def test_contested_still_set_for_a_track_inside_the_cap(self):
        """The cap keeps the newest fixes, matching _claim_round's ordering,
        so a track fresh enough to contest is never truncated away."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont03", ts)
        self._fill_dark_globals(ts, CLAIM_MAX_GLOBAL_TRACKS + 50, lat=_LAT + 2.0, lon=_LON + 2.0)
        # Newest of all, and co-located with the claim: inside the cap, and
        # gating against it.
        self._global("mn-dark-fresh", ts - 1000, n_nodes=3, solve_count=5)
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}

        assert state.known_claims["cont03"][-1]["contested"] is True
        assert state.known_claim_contentions == 1

    def test_a_track_truncated_away_does_not_contest(self):
        """The other half of the cap, and the one behaviour change in it: past
        CLAIM_MAX_GLOBAL_TRACKS eligible dark globals, the oldest stop being
        part of the contention reference set — the same trade _claim_round
        already makes against the same constant."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("cont04", ts)
        # Oldest of all, co-located with the claim: it WOULD contest, but the
        # newer bulk fills the cap ahead of it.
        self._global("mn-dark-oldest", ts - 25000, n_nodes=3, solve_count=5)
        self._fill_dark_globals(ts, CLAIM_MAX_GLOBAL_TRACKS, lat=_LAT + 2.0, lon=_LON + 2.0)
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}

        assert state.known_claims["cont04"][-1]["contested"] is False
        assert state.known_claim_contentions == 0


class TestModesInProcessOneFrame:
    """Shadow records but the dark lane sees the full frame; binding strips
    the claimed indices from what the pipeline processes — and only from
    that: the original frame (archive, ADS-B extraction) stays whole."""

    def _run(self, monkeypatch, mode):
        _register()
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", mode)
        ts = int(time.time() * 1000)
        tag = {"hex": "bind01", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}
        frame = _frame(ts, [50.0, 52.0], [10.0, 15.0], adsb=[tag, None])

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        seen = []
        monkeypatch.setattr(default, "process_frame", lambda f: seen.append(f))
        process_one_frame(_NODE_ID, frame, default)
        return frame, seen[0]

    def test_binding_strips_claimed_indices_from_the_pipeline_frame(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "binding")
        assert pframe["delay"] == [52.0]
        assert pframe["doppler"] == [15.0]
        assert pframe["snr"] == [20.0]
        assert pframe["adsb"] == [None]
        # Original untouched — it still feeds archive + ADS-B extraction.
        assert len(frame["delay"]) == 2 and frame["adsb"][0]["hex"] == "bind01"
        assert state.known_claims_bound == 1
        assert state.known_claims_made == 1

    def test_shadow_records_the_claim_but_changes_nothing_downstream(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "shadow")
        assert pframe is frame  # same object, not even a copy
        assert "bind01" in state.known_claims
        assert state.known_claims_made == 1
        assert state.known_claims_bound == 0

    def test_off_computes_nothing(self, monkeypatch):
        frame, pframe = self._run(monkeypatch, "off")
        assert pframe is frame
        assert state.known_claims == {}
        assert state.known_claims_made == 0

    def test_known_lane_gets_its_own_perf_bucket(self, monkeypatch):
        """The known lane used to be timed inside `pipeline=`, which reported
        a per-frame per-node stage as part of the tracker's cost.  The buckets
        are disjoint, so `pipeline` must not also contain it."""
        from services import frame_processor as fp

        self._run(monkeypatch, "binding")
        assert fp._prof_known > 0.0
        assert fp._prof_pipeline >= 0.0
        assert fp._prof_n == 1

        fp._reset_for_tests()
        self._run(monkeypatch, "off")
        # Off mode never enters the block, so the bucket stays empty rather
        # than absorbing the branch test.
        assert fp._prof_known < 1e-4

    def test_perf_line_carries_the_known_bucket(self, monkeypatch):
        """The line is emitted once per 1000 frames, so it is otherwise only
        seen in production logs — and a bucket added without a field in it is
        exactly as invisible as no bucket at all."""
        import logging

        from services import frame_processor as fp

        logged = []
        monkeypatch.setattr(logging, "warning", lambda fmt, *args: logged.append((fmt, args)))
        fp._prof_n = 999
        self._run(monkeypatch, "binding")

        assert len(logged) == 1
        fmt, args = logged[0]
        assert "known=%.1f" in fmt
        # Every %-placeholder is fed: a mismatch here logs a traceback in
        # production instead of a PERF line.
        assert fmt.replace("%%", "") % args


class TestRegistryContract:
    def test_claim_record_shape(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("shape1", ts, gs=100, track=90)
        pd, pf = predict_observation(geo, _LAT, _LON, _ALT_BARO_FT * FT_TO_M / 1000.0, 100 * 0.514444, 0.0)
        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf]))

        rec = state.known_claims["shape1"][-1]
        assert set(rec.keys()) == {
            "node_id",
            "delay_us",
            "doppler_hz",
            "pred_delay_us",
            "pred_doppler_hz",
            "ts_ms",
            "adsb_fix",
            "contested",
        }
        assert set(rec["adsb_fix"].keys()) == {"lat", "lon", "alt_baro", "gs", "track", "fix_ts_ms"}
        assert rec["node_id"] == _NODE_ID
        assert rec["ts_ms"] == ts
        # Assignment-path claims carry the REPORTED fix and its own timestamp.
        assert rec["adsb_fix"]["lat"] == _LAT
        assert rec["adsb_fix"]["fix_ts_ms"] == ts
        assert state.known_claims["shape1"].maxlen == state.KNOWN_CLAIMS_PER_HEX_MAX

    def test_reset_for_tests_clears_registry_and_counters(self):
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "reset1", "lat": _LAT, "lon": _LON, "alt_baro": 0, "gs": 0, "track": 0}
        kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))
        assert state.known_claims and state.known_claims_made == 1

        state._reset_for_tests()

        assert state.known_claims == {}
        assert state.known_claims_made == 0
        assert state.known_claim_contentions == 0
        assert state.known_claims_bound == 0
        assert state.known_claims_visibility_rejects == 0


class TestNodeBiasHook:
    def test_missing_module_degrades_silently(self, monkeypatch):
        """services.node_bias is optional at import time — a tree without it
        (or a rollback that drops it) must claim normally, and the ImportError
        verdict must be cached once.  Its presence in THIS tree means absence
        has to be simulated: None in sys.modules makes the import raise, and
        the package attribute (bound by conftest's reset imports) must go too
        or `from services import node_bias` never reaches the import system."""
        import services

        monkeypatch.delattr(services, "node_bias")
        monkeypatch.setitem(sys.modules, "services.node_bias", None)
        kc._reset_for_tests()
        _register()
        ts = int(time.time() * 1000)
        tag = {"hex": "nobias", "lat": _LAT, "lon": _LON, "alt_baro": 0, "gs": 0, "track": 0}
        claimed = kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag]))
        assert claimed == {0}
        assert kc._node_bias() is None
        assert kc._node_bias_unavailable is True

    def _install_fake(self, monkeypatch, recorder=None):
        import services

        fake = types.SimpleNamespace(
            record_claim_residual=recorder or (lambda *a: None),
        )
        monkeypatch.setitem(sys.modules, "services.node_bias", fake)
        monkeypatch.setattr(services, "node_bias", fake, raising=False)
        kc._reset_for_tests()  # forget any cached ImportError from earlier tests

    def test_residuals_are_recorded_signed(self, monkeypatch):
        recorded = []
        self._install_fake(monkeypatch, recorder=lambda *a: recorded.append(a))
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("resid1", ts)
        pd, pf = _stationary_pred(geo)

        kc.claim_known_targets(_NODE_ID, _frame(ts, [pd + 2.0], [pf - 5.0]))

        assert len(recorded) == 1
        node_id, hexn, d_res, f_res, ts_out = recorded[0]
        assert (node_id, hexn, ts_out) == (_NODE_ID, "resid1", ts)
        # Signed, measured minus predicted — bias direction matters downstream.
        assert d_res == pytest.approx(2.0)
        assert f_res == pytest.approx(-5.0)


class TestStripAndGc:
    def test_strip_all_claimed_leaves_empty_lists(self):
        f = _frame(1000, [1.0, 2.0], [3.0, 4.0])
        out = kc.strip_claimed_detections(f, {0, 1})
        assert out["delay"] == [] and out["doppler"] == [] and out["snr"] == []
        assert f["delay"] == [1.0, 2.0]

    def test_non_list_keys_pass_through(self):
        f = _frame(1000, [1.0], [3.0])
        f["adsb"] = None
        out = kc.strip_claimed_detections(f, {0})
        assert out["adsb"] is None
        assert out["timestamp"] == 1000

    def test_feed_gc_prunes_stale_hexes_only(self):
        from collections import deque

        from services.feed_gc import prune_stale_stores

        now = time.time()
        state.known_claims["fresh1"] = deque([{"ts_ms": int((now - 10) * 1000)}], maxlen=4)
        state.known_claims["stale1"] = deque([{"ts_ms": int((now - 500) * 1000)}], maxlen=4)
        prune_stale_stores(now)
        assert "fresh1" in state.known_claims
        assert "stale1" not in state.known_claims


class TestWorldGate:
    """Path-2 candidates must come from the claiming node's own world.

    The cache mixes simulated transponders with real traffic (hardware
    receivers, the simulator's adsb.lol relay) over one footprint, so the
    visibility gate cannot tell a decoy from a candidate — measured live
    2026-08-27: 35% of synthetic-node adsb_single_node icons were real
    aircraft no simulated radar ever detected."""

    def test_sim_node_skips_a_real_world_candidate(self):
        geo = _register()  # test-* prefix → sim world
        ts = int(time.time() * 1000)
        _cache_state("decoy1", ts, world="real")
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == set()
        assert state.known_claims == {}
        assert state.known_claims_world_rejects == 1

    def test_sim_node_claims_a_sim_world_candidate(self):
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("simown", ts, world="sim")
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}
        assert state.known_claims_world_rejects == 0

    def test_real_node_skips_a_sim_world_candidate(self):
        """The reverse direction: a hardware node's echoes are of real
        aircraft, so a simulated transponder is no candidate for them."""
        node_id = "hw-atl-1"  # no synthetic prefix → real world
        geo = _register(node_id)
        ts = int(time.time() * 1000)
        _cache_state("simdec", ts, world="sim")
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(node_id, _frame(ts, [pd], [pf])) == set()
        assert state.known_claims_world_rejects == 1

    def test_untagged_candidate_is_not_gated(self):
        """No writer in this tree leaves world unset, so an untagged entry is
        prior state or an old pusher — rejecting it would silently disable
        the lane rather than fail toward dark."""
        geo = _register()
        ts = int(time.time() * 1000)
        _cache_state("legacy", ts)  # no world key
        pd, pf = _stationary_pred(geo)

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [pd], [pf])) == {0}
        assert state.known_claims_world_rejects == 0

    def test_node_tag_is_not_world_gated(self):
        """Path 1 stays ungated for the same reason it skips the visibility
        gate: the tag is the node's own evidence, whatever the cache says."""
        _register()
        ts = int(time.time() * 1000)
        _cache_state("tagged", ts, world="real")
        tag = {"hex": "tagged", "lat": _LAT, "lon": _LON, "alt_baro": _ALT_BARO_FT, "gs": 0, "track": 0}

        assert kc.claim_known_targets(_NODE_ID, _frame(ts, [50.0], [10.0], adsb=[tag])) == {0}
        assert state.known_claims_world_rejects == 0

    def test_handshake_verdict_beats_the_prefix_rule(self):
        """A node whose CONFIG declared is_synthetic=True is a sim node even
        without a synthetic id prefix — the handshake honours the node's own
        claim, and state.node_world must agree with it."""
        node_id = "oddly-named-sim-node"
        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {"is_synthetic": True}
        try:
            assert state.node_world(node_id) == "sim"
        finally:
            with state.connected_nodes_lock:
                state.connected_nodes.pop(node_id, None)

    def test_unregistered_node_falls_back_to_the_prefix_rule(self):
        assert state.node_world("synth-GVL-0001") == "sim"
        assert state.node_world("radar3-retnode") == "real"
