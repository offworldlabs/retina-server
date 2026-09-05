"""Tests for one-shot ghost suppression in the multinode solver track store.

Two problems in how state.multinode_tracks renders as mn-* aircraft:

- a track solved exactly once dead-reckons for the full 60 s entry lifetime
  (30 s DR cap) even though nothing has confirmed it is a real aircraft and
  not a mirror-point/wrong-frame ghost.  solver.py now stamps a solve_count
  onto every published entry, and aircraft_feed.py withholds rendering until
  a 2-node track has MN_N2_MIN_SOLVES solves, and caps an n>=3 one-shot's
  display window to MN_ONESHOT_TTL_S seconds.

- a later solve of the same aircraft that misses the 6 km track-key match
  (multinode_key_decision) mints a new entry instead of updating the old one,
  so the old ghost keeps rendering beside the new, correct entry.  solver.py
  supersedes the earlier entry — but a shared source single-node track id is
  only the cheap prefilter for that, never the rule: tracker track ids are
  reused across the association candidates of DIFFERENT aircraft, so
  solver.py's _supersession_match has to agree the two are the same target
  before anything is popped: dead-reckoned inside the age-scaled gate grown
  from the tighter _MN_SUPERSEDE_BASE_KM, or identical inputs close by, and
  in both cases within _MN_SUPERSEDE_MAX_ALT_DIFF_M vertically.  Refusals
  land on state.mn_superseded_blocked, and the altitude-only ones also on
  state.mn_superseded_blocked_alt.
"""

import time
import types

import pytest

from core import state
from services import aircraft_feed as aircraft_feed_mod
from services.geo import offset_latlon_m
from services.id_utils import multinode_hex_from_key
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0


def _solve_fn(lat=LAT, lon=LON, **overrides):
    """A solve_fn returning a successful n>=2 result at (lat, lon), now."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "n_nodes": 3,
        "n_measurements": 3,
        "contributing_node_ids": ["n1", "n2", "n3"],
    }
    result.update(overrides)

    def fn(s_in, cfgs):
        return dict(result)

    return fn


def _mn_entry(n_nodes: int, age_s: float, solve_count: int, lat: float = LAT, lon: float = LON) -> dict:
    """A state.multinode_tracks-shaped entry for feed-gate tests.

    Built directly (not via solver.py) so the feed gates are exercised in
    isolation from the solver's key/supersession machinery.
    """
    return {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 7000.0,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": 0.1,
        "rms_doppler": 1.0,
        "n_nodes": n_nodes,
        "n_measurements": n_nodes,
        "contributing_node_ids": [f"n{i}" for i in range(n_nodes)],
        "timestamp_ms": int((time.time() - age_s) * 1000),
        "solve_count": solve_count,
    }


class TestSolveCount:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item((dict(s_in), cfgs or {}, time.time()), solve_fn)

    def test_first_publish_solve_count_is_1(self):
        self._run({"n_nodes": 3}, _solve_fn())
        (entry,) = state.multinode_tracks.values()
        assert entry["solve_count"] == 1

    def test_second_publish_same_position_solve_count_is_2(self):
        # Same position -> same key via the 6 km dark-track match, so the
        # second solve updates the existing entry instead of minting a new
        # one.
        self._run({"n_nodes": 3}, _solve_fn())
        self._run({"n_nodes": 3}, _solve_fn())
        assert len(state.multinode_tracks) == 1
        (entry,) = state.multinode_tracks.values()
        assert entry["solve_count"] == 2


class TestSupersession:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item((dict(s_in), cfgs or {}, time.time()), solve_fn)

    def test_shared_track_id_within_the_gate_supersedes_old_entry(self):
        """The surviving supersession case: an entry sharing a source track
        id that ALSO dead-reckons inside the association gate is the same
        aircraft under a stale key, and is popped.

        It takes two seeded entries to show this, not one solve followed by a
        second: _supersession_match's spatial branch uses the same raw
        position, the same age-scaled gate and the same dead reckoning as
        multinode_key_decision's proximity scan, so a lone in-gate entry is
        simply the key this solve is written to (old_key == key, nothing to
        supersede).  The branch earns its keep when the solve keys onto a
        NEARER entry and a second one is still inside its own gate — two
        fragments of one aircraft collapsing into the closer of them.
        """
        now_ms = int(time.time() * 1000)
        # The entry this solve will key onto: same position, no shared ids.
        state.multinode_tracks["mn-dark-near"] = _mn_entry(3, age_s=5.0, solve_count=1)
        state.multinode_tracks["mn-dark-near"]["source_track_ids"] = ["t7"]
        # The victim: ~2.2 km north, inside its own 4.65 km supersession gate
        # at dt=5 s, at the same altitude as the solve, and sharing track t2
        # with the solve below.
        v_lat, v_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=2200.0)
        state.multinode_tracks["mn-dark-victim"] = _mn_entry(3, age_s=5.0, solve_count=1, lat=v_lat, lon=v_lon)
        state.multinode_tracks["mn-dark-victim"]["source_track_ids"] = ["t2", "t9"]
        # Pre-seed the anomaly hex the victim would carry if it had ever been
        # through the feed, to prove supersession discards it.
        with state.anomaly_lock:
            state.anomaly_hexes.add(multinode_hex_from_key("mn-dark-victim"))

        self._run(
            {"n_nodes": 3, "track_ids": ["t2", "t3"]},
            _solve_fn(LAT, LON, timestamp_ms=now_ms, alt_m=7000.0),
        )

        assert set(state.multinode_tracks) == {"mn-dark-near"}
        entry = state.multinode_tracks["mn-dark-near"]
        # The victim's solve_count (1) carries forward so the re-solved
        # aircraft is not hidden by the n=2 gate.
        assert entry["solve_count"] == 2
        assert multinode_hex_from_key("mn-dark-victim") not in state.anomaly_hexes
        assert state.mn_superseded == 1
        assert state.mn_superseded_blocked == 0
        assert state.mn_superseded_blocked_alt == 0

    def test_a_neighbour_five_km_off_is_no_longer_popped(self):
        """The publish path with the tightened base.  Same shape as the test
        above — the solve keys onto the nearer entry and a second shared-id
        entry is judged on its own — but the victim sits 5 km away, inside
        the old 6 km supersession base and outside the 4 km one.  This is the
        measured failure: a neighbour 5-6 km away in the same nodes' cones,
        popped by a solve whose own position error was several km.
        """
        now_ms = int(time.time() * 1000)
        state.multinode_tracks["mn-dark-near"] = _mn_entry(3, age_s=5.0, solve_count=1)
        state.multinode_tracks["mn-dark-near"]["source_track_ids"] = ["t7"]
        v_lat, v_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=5000.0)
        state.multinode_tracks["mn-dark-neighbour"] = _mn_entry(3, age_s=5.0, solve_count=4, lat=v_lat, lon=v_lon)
        state.multinode_tracks["mn-dark-neighbour"]["source_track_ids"] = ["t2", "t9"]

        self._run(
            {"n_nodes": 3, "track_ids": ["t2", "t3"]},
            _solve_fn(LAT, LON, timestamp_ms=now_ms, alt_m=7000.0),
        )

        # The neighbour keeps its key, its solve_count and its KF history.
        assert "mn-dark-neighbour" in state.multinode_tracks
        assert state.multinode_tracks["mn-dark-neighbour"]["solve_count"] == 4
        assert state.mn_superseded == 0
        assert state.mn_superseded_blocked == 1
        # Refused on distance, not altitude — the two counters are distinct.
        assert state.mn_superseded_blocked_alt == 0

    def test_an_in_gate_neighbour_at_a_different_altitude_is_counted_separately(self):
        """The altitude refusal, at the call site.  2.2 km away is well
        inside the spatial gate, so on distance alone this entry would be
        popped; 2.5 km of altitude difference is what saves it, and that is
        the case mn_superseded_blocked_alt exists to make visible.
        """
        now_ms = int(time.time() * 1000)
        state.multinode_tracks["mn-dark-near"] = _mn_entry(3, age_s=5.0, solve_count=1)
        state.multinode_tracks["mn-dark-near"]["source_track_ids"] = ["t7"]
        v_lat, v_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=2200.0)
        state.multinode_tracks["mn-dark-above"] = _mn_entry(3, age_s=5.0, solve_count=6, lat=v_lat, lon=v_lon)
        state.multinode_tracks["mn-dark-above"]["source_track_ids"] = ["t2", "t9"]

        self._run(
            {"n_nodes": 3, "track_ids": ["t2", "t3"]},
            # _mn_entry sits at 7000 m; solve 2.5 km above it.
            _solve_fn(LAT, LON, timestamp_ms=now_ms, alt_m=9500.0),
        )

        assert "mn-dark-above" in state.multinode_tracks
        assert state.multinode_tracks["mn-dark-above"]["solve_count"] == 6
        assert state.mn_superseded == 0
        assert state.mn_superseded_blocked == 1
        assert state.mn_superseded_blocked_alt == 1

    def test_shared_track_id_far_away_does_not_supersede(self):
        """The regression this guard exists for.  Two aircraft 55 km apart
        whose association candidates happened to be built from a common
        tracker track: on the old shared-id-only rule the second solve
        deleted the first one's key outright, and the first aircraft's next
        solve minted a fresh one.  Both entries must now survive, and the
        refusal must be counted.
        """
        self._run(
            {"n_nodes": 3, "track_ids": ["t1", "t2"]},
            _solve_fn(35.0, -82.0),
        )
        old_key = next(iter(state.multinode_tracks))
        with state.anomaly_lock:
            state.anomaly_hexes.add(multinode_hex_from_key(old_key))

        # ~55 km away: past the key matcher's gate, so a new key is minted,
        # and past the supersession gate too — the shared t2 is contamination,
        # not identity.  Nor is ["t1","t2"] a subset of ["t2","t3"].
        self._run(
            {"n_nodes": 3, "track_ids": ["t2", "t3"]},
            _solve_fn(35.5, -82.0),
        )

        assert len(state.multinode_tracks) == 2
        assert old_key in state.multinode_tracks
        assert state.multinode_tracks[old_key]["solve_count"] == 1
        # The victim keeps its identity, anomaly latch included.
        assert multinode_hex_from_key(old_key) in state.anomaly_hexes
        assert state.mn_superseded == 0
        assert state.mn_superseded_blocked == 1

    def test_disjoint_track_ids_do_not_supersede(self):
        self._run(
            {"n_nodes": 3, "track_ids": ["t1"]},
            _solve_fn(35.0, -82.0),
        )
        self._run(
            {"n_nodes": 3, "track_ids": ["t9"]},
            _solve_fn(45.0, -90.0),
        )
        assert len(state.multinode_tracks) == 2
        assert all(e["solve_count"] == 1 for e in state.multinode_tracks.values())
        assert state.mn_superseded == 0


class TestSupersessionMatch:
    """The predicate itself, units — clock-free and with an injected
    learned_vel_fn, the same way TestMultinodeKeyDecision in
    test_solver_anchor.py exercises the keying rule.
    """

    NO_VEL = staticmethod(lambda key: None)  # no KF state -> entry velocity

    def _entry(self, ts_ms, lat=LAT, lon=LON, ids=("t1",), **overrides):
        rec = {
            "lat": lat,
            "lon": lon,
            "vel_east": 0.0,
            "vel_north": 0.0,
            "timestamp_ms": ts_ms,
            "source_track_ids": list(ids),
        }
        rec.update(overrides)
        return rec

    def test_negative_dt_is_never_dead_reckoned_backwards(self):
        """The entry is stamped AFTER this solve (out-of-order arrival).  It
        is not run backwards to manufacture a match, and with only a partial
        id overlap there is nothing else to match on."""
        entry = self._entry(20_000, *offset_latlon_m(LAT, LON, east_m=0.0, north_m=3000.0), ids=("t1", "t9"))
        matched, dist = solver_mod._supersession_match(
            "mn-dark-future", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is False
        assert dist is None

    def test_entry_older_than_max_age_does_not_match(self):
        """Past _MN_ASSOC_MAX_AGE_S the map has already dropped it — the same
        window multinode_key_decision's scan uses."""
        entry = self._entry(0, ids=("t1", "t9"))
        ts_ms = int((solver_mod._MN_ASSOC_MAX_AGE_S + 1.0) * 1000)
        matched, dist = solver_mod._supersession_match(
            "mn-dark-stale", entry, {"t1", "t2"}, LAT, LON, ts_ms, learned_vel_fn=self.NO_VEL
        )
        assert matched is False
        assert dist is None
        # One second younger, same position: the window is what refused it.
        entry_fresh = self._entry(1000, ids=("t1", "t9"))
        matched_fresh, dist_fresh = solver_mod._supersession_match(
            "mn-dark-fresh", entry_fresh, {"t1", "t2"}, LAT, LON, ts_ms, learned_vel_fn=self.NO_VEL
        )
        assert matched_fresh is True
        assert dist_fresh == 0.0

    def test_inside_the_age_scaled_gate_matches_and_reports_the_distance(self):
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=4000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"))
        matched, dist = solver_mod._supersession_match(
            "mn-dark-near", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is True
        assert dist == pytest.approx(4.0, abs=0.1)

    def test_just_outside_the_age_scaled_gate_does_not_match(self):
        # dt = 10 s -> gate 4.0 + 1.3 = 5.3 km; sit at 8 km.
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=8000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"))
        matched, dist = solver_mod._supersession_match(
            "mn-dark-far", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is False
        assert dist == pytest.approx(8.0, abs=0.1)

    def test_dead_reckoning_carries_a_fast_target_into_the_gate(self):
        """The entry is stored standing still; the KF's learned velocity is
        what the feed draws it with, so that is what the predicate must
        dead-reckon by (_entry_dr_velocity).  14 km away is outside the 7.9 km
        gate at dt=30 s until 250 m/s of eastward motion is applied."""
        s_lat, s_lon = offset_latlon_m(LAT, LON, east_m=14_000.0, north_m=0.0)
        entry = self._entry(0, ids=("t1", "t9"))

        still, still_dist = solver_mod._supersession_match(
            "mn-dark-fast", entry, {"t1", "t2"}, s_lat, s_lon, 30_000, learned_vel_fn=self.NO_VEL
        )
        assert still is False
        assert still_dist == pytest.approx(14.0, abs=0.2)

        moving, moving_dist = solver_mod._supersession_match(
            "mn-dark-fast", entry, {"t1", "t2"}, s_lat, s_lon, 30_000, learned_vel_fn=lambda key: (250.0, 0.0)
        )
        assert moving is True
        assert moving_dist == pytest.approx(6.5, abs=0.2)

    def test_identical_inputs_match_beyond_the_spatial_gate_but_not_unboundedly(self):
        """Rule (b): built from a subset of the same measurements, so the same
        aircraft by construction — the anchor-merge case, where a fragment
        minted from exactly these tracks converged a few km from the anchor.

        Bounded at twice the branch (a) gate, though.  The real merge case is
        always close (the one legitimate identical-inputs merge in the
        2026-09-05 captures was 1.8 km and 31 m apart); identical inputs
        converging 16 km apart is a multi-modal solve, and popping a key on
        the strength of the other mode costs that aircraft its identity.
        """
        # dt = 10 s -> branch (a) gate 5.3 km, branch (b) bound 10.6 km.
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=8000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t2"))
        matched, dist = solver_mod._supersession_match(
            "mn-dark-fragment", entry, {"t1", "t2", "t3"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is True
        # The distance is still reported even though (b) is what matched.
        assert dist == pytest.approx(8.0, abs=0.2)

        far = self._entry(0, *offset_latlon_m(LAT, LON, east_m=0.0, north_m=16_000.0), ids=("t1", "t2"))
        matched_far, dist_far = solver_mod._supersession_match(
            "mn-dark-multimodal", far, {"t1", "t2", "t3"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched_far is False
        assert dist_far == pytest.approx(16.0, abs=0.2)

    def test_identical_inputs_close_by_at_a_different_altitude_do_not_match(self):
        """The altitude half of the gate applies to branch (b) too.  Same
        measurements 1 km apart horizontally but 7.4 km apart vertically is
        one solve landing on the wrong mode, not one aircraft twice — three of
        the seven branch (b) firings in the captures looked exactly like
        this."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=1000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t2"), alt_m=2000.0)
        matched, dist = solver_mod._supersession_match(
            "mn-dark-lowmode",
            entry,
            {"t1", "t2", "t3"},
            LAT,
            LON,
            10_000,
            learned_vel_fn=self.NO_VEL,
            alt_m=9400.0,
        )
        assert matched is False
        assert dist == pytest.approx(1.0, abs=0.1)

        # 31 m apart in altitude at 1.8 km: the legitimate merge, and it still
        # goes through — via (a) here, since the spatial gate covers 1.8 km.
        near = self._entry(0, *offset_latlon_m(LAT, LON, east_m=0.0, north_m=1800.0), ids=("t1", "t2"), alt_m=9369.0)
        merged, _dist = solver_mod._supersession_match(
            "mn-dark-merge", near, {"t1", "t2", "t3"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL, alt_m=9400.0
        )
        assert merged is True

    def test_the_supersession_base_is_tighter_than_the_keying_base(self):
        """_MN_SUPERSEDE_BASE_KM, the neighbour-pop fix.  5 km at dt=0 keys
        (the 6 km _MN_ASSOC_MAX_DIST_KM base) but must not POP: on the
        2026-09-05 captures a 4.5-10 km solve error landing inside a
        neighbour's 6 km gate was what deleted 63 of 129 keys.  The gate still
        grows with entry age, so the same 5 km at dt = 20 s (4.0 + 2.6 =
        6.6 km) is a match again — an old entry is judged by how far it could
        have drifted, not by the dt=0 number."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=5000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"), alt_m=9000.0)

        fresh, fresh_dist = solver_mod._supersession_match(
            "mn-dark-neighbour", entry, {"t1", "t2"}, LAT, LON, 0, learned_vel_fn=self.NO_VEL, alt_m=9000.0
        )
        assert fresh is False
        assert fresh_dist == pytest.approx(5.0, abs=0.1)

        aged, _dist = solver_mod._supersession_match(
            "mn-dark-neighbour", entry, {"t1", "t2"}, LAT, LON, 20_000, learned_vel_fn=self.NO_VEL, alt_m=9000.0
        )
        assert aged is True

    def test_close_but_far_apart_in_altitude_does_not_match(self):
        """Two aircraft in the same sector are separated by altitude long
        before they are separated on the map.  2 km apart horizontally and
        2.5 km apart vertically is the cross-aircraft pop this gate was
        measured against (median |dalt| 2.7 km, against 0.4 km for genuine
        same-aircraft pops)."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=2000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"), alt_m=6000.0)
        matched, dist = solver_mod._supersession_match(
            "mn-dark-below", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL, alt_m=8500.0
        )
        assert matched is False
        assert dist == pytest.approx(2.0, abs=0.1)

        # Within 1000 m of each other, same geometry: matched.
        same_alt, _dist = solver_mod._supersession_match(
            "mn-dark-below", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL, alt_m=6900.0
        )
        assert same_alt is True

    def test_a_missing_altitude_on_either_side_fails_open(self):
        """Optional fields are fail-open throughout this pipeline, and an
        entry that never recorded an altitude must stay poppable on distance
        alone rather than becoming immortal."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=2000.0)
        no_entry_alt = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"))
        matched, _dist = solver_mod._supersession_match(
            "mn-dark-altless", no_entry_alt, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL, alt_m=8500.0
        )
        assert matched is True

        with_entry_alt = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"), alt_m=6000.0)
        no_solve_alt, _dist = solver_mod._supersession_match(
            "mn-dark-altless", with_entry_alt, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert no_solve_alt is True

    def test_partial_overlap_far_away_is_refused(self):
        """Overlap is not identity — this is the cross-aircraft contamination
        the guard exists for."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=111_000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=("t1", "t9"))
        matched, _dist = solver_mod._supersession_match(
            "mn-dark-other", entry, {"t1", "t2", "t3"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is False

    def test_an_entry_with_no_source_ids_of_its_own_cannot_match_by_subset(self):
        """The empty set is a subset of everything; an entry that never
        recorded its inputs must not be swept up by that."""
        e_lat, e_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=111_000.0)
        entry = self._entry(0, lat=e_lat, lon=e_lon, ids=())
        matched, _dist = solver_mod._supersession_match(
            "mn-dark-idless", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is False

    def test_an_entry_with_no_position_cannot_match_spatially(self):
        entry = self._entry(0, lat=None, lon=None, ids=("t1", "t9"))
        matched, dist = solver_mod._supersession_match(
            "mn-dark-posless", entry, {"t1", "t2"}, LAT, LON, 10_000, learned_vel_fn=self.NO_VEL
        )
        assert matched is False
        assert dist is None


class TestHistoryRecord:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def test_published_record_carries_solve_count_and_source_track_ids(self):
        solver_mod._process_solver_item(
            (
                {"n_nodes": 3, "track_ids": ["t2", "t1"]},
                {},
                time.time(),
            ),
            _solve_fn(),
        )
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["solve_count"] == 1
        assert rec["source_track_ids"] == ["t1", "t2"]
        # Nothing to supersede on the first solve of a clean store.
        assert rec["superseded_keys"] == []
        assert rec["superseded_blocked"] == 0

    def test_published_record_carries_supersession_verdicts(self):
        """Which entries this publish popped, and how many shared-id entries
        the guard refused — the only way to attribute a vanished key after
        the fact.  One of each here: a live neighbour 55 km away that keeps
        its key, and an old entry 7 km off built from a subset of these
        inputs — past the spatial gate, inside the identical-inputs bound —
        that merges."""
        state.multinode_tracks["mn-dark-blocked"] = _mn_entry(3, age_s=5.0, solve_count=1, lat=35.5, lon=-82.0)
        state.multinode_tracks["mn-dark-blocked"]["source_track_ids"] = ["t1", "t9"]
        m_lat, m_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=7000.0)
        state.multinode_tracks["mn-dark-merged"] = _mn_entry(3, age_s=5.0, solve_count=1, lat=m_lat, lon=m_lon)
        state.multinode_tracks["mn-dark-merged"]["source_track_ids"] = ["t1", "t2"]

        solver_mod._process_solver_item(
            ({"n_nodes": 3, "track_ids": ["t1", "t2"]}, {}, time.time()),
            _solve_fn(LAT, LON, alt_m=7000.0),
        )

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["superseded_keys"] == ["mn-dark-merged"]
        assert rec["superseded_blocked"] == 1

    def test_reject_record_carries_empty_supersession_fields(self):
        """Supersession runs only on the publish path, so a reject has no
        verdict to report — the same reason key_how is None there."""
        solver_mod._process_solver_item(
            ({"n_nodes": 3, "track_ids": ["t1"]}, {}, time.time()),
            lambda s_in, cfgs: {"success": False},
        )
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "unconverged"
        assert rec["superseded_keys"] == []
        assert rec["superseded_blocked"] == 0


class TestFeedGates:
    def setup_method(self):
        state._reset_for_tests()

    def _build(self):
        from services.frame_processor import build_combined_aircraft_json

        pipeline = types.SimpleNamespace(geolocated_tracks={}, config={})
        return build_combined_aircraft_json(pipeline)

    def _mn_aircraft(self):
        return [a for a in self._build()["aircraft"] if a.get("multinode")]

    def test_n2_one_shot_is_not_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=1)
        assert self._mn_aircraft() == []
        # Gated, not expired: stays in the store for the next solve to
        # confirm or supersede.
        assert "k" in state.multinode_tracks

    def test_n2_confirmed_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=2)
        assert len(self._mn_aircraft()) == 1

    def test_n3_one_shot_within_ttl_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=3.0, solve_count=1)
        assert len(self._mn_aircraft()) == 1

    def test_n3_one_shot_past_ttl_is_not_rendered_but_not_expired(self):
        # Past MN_ONESHOT_TTL_S (15 s) but well inside the 60 s entry expiry,
        # so this exercises the display gate and not staleness.
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=20.0, solve_count=1)
        assert self._mn_aircraft() == []
        assert "k" in state.multinode_tracks

    def test_n3_confirmed_old_entry_is_rendered(self):
        state.multinode_tracks["k"] = _mn_entry(n_nodes=3, age_s=40.0, solve_count=2)
        assert len(self._mn_aircraft()) == 1

    def test_n2_gate_disabled_via_min_solves_1(self, monkeypatch):
        monkeypatch.setattr(aircraft_feed_mod, "MN_N2_MIN_SOLVES", 1)
        state.multinode_tracks["k"] = _mn_entry(n_nodes=2, age_s=1.0, solve_count=1)
        assert len(self._mn_aircraft()) == 1
