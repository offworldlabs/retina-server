"""Tests for the per-solve MLAT history buffer and GET /api/test/mlat-history.

Every solver outcome — published or gate-rejected — lands in
state.mlat_solve_history so an individual map marker's solves can be looked
up by its mn<sha256[:10]> hex and checked after the fact.
"""

import sys
import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

from config.constants import ASSOC_GRID_STEP_KM
from core import state
from services.id_utils import multinode_hex_from_key
from services.tasks import solver as solver_mod

# n=2 input whose track pairing already passed the CV fit (see
# test_solver_worker.py for why a bare {"n_nodes": 2} is withheld).
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}

LAT, LON = 35.0, -82.0


def _solve_fn(lat=LAT, lon=LON, **overrides):
    """A solve_fn returning a successful result at (lat, lon), now."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 10.0,
        "vel_north": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "contributing_node_ids": ["n1", "n2"],
    }
    result.update(overrides)

    def fn(s_in, cfgs):
        return dict(result)

    return fn


def _put_gt(hex_code="abc123", lat=LAT + 0.001, lon=LON, age_s=0.0):
    state.ground_truth_trails[hex_code] = deque([[lat, lon, 9000.0, time.time() - age_s]])


class TestRecording:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item((dict(s_in), cfgs or {}, time.time()), solve_fn)

    def _only_record(self):
        assert len(state.mlat_solve_history) == 1
        return state.mlat_solve_history[0]

    def test_published_solve_records_positions_and_gt(self):
        _put_gt()
        self._run(_CONFIRMED_N2, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "published"
        assert rec["solve_key"] is not None
        assert rec["solver_hex"] == multinode_hex_from_key(rec["solve_key"])
        # Raw position is the solver output; published lat/lon is the
        # (here identical — first solve) smoothed position.
        assert rec["raw_lat"] == pytest.approx(LAT)
        assert rec["lat"] == pytest.approx(LAT)
        # GT stamp frozen at solve time: 0.001° of latitude ≈ 0.111 km.
        assert rec["gt_hex"] == "abc123"
        assert rec["gt_error_km"] == pytest.approx(0.111, abs=0.01)

    def test_published_solver_hex_matches_feed_hex(self):
        """The recorded hex is the same one the feed shows on the map."""
        self._run(_CONFIRMED_N2, _solve_fn())
        rec = self._only_record()
        key = next(iter(state.multinode_tracks))
        assert rec["solver_hex"] == multinode_hex_from_key(key)

    def test_stale_gt_is_not_stamped(self):
        _put_gt(age_s=600.0)
        self._run(_CONFIRMED_N2, _solve_fn())
        assert self._only_record()["gt_hex"] is None

    def test_rms_delay_reject_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(rms_delay=10.0))
        rec = self._only_record()
        assert rec["outcome"] == "rejected_rms_delay"
        assert rec["solver_hex"] is None
        assert rec["raw_lat"] == pytest.approx(LAT)

    def test_rms_doppler_reject_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(rms_doppler=500.0))
        assert self._only_record()["outcome"] == "rejected_rms_doppler"

    def test_beam_reject_is_recorded(self):
        # Real geometry that fails the RANGE rule (applied at every n, so
        # it works for this n=2 _CONFIRMED_N2 input): rx placed ~556 km
        # from the solve position, well past a 50 km max_range_km — for
        # both contributing node ids the default _solve_fn() publishes
        # under (see contributing_node_ids in _solve_fn above).
        cfgs = {
            "n1": {"rx_lat": LAT + 5.0, "rx_lon": LON, "max_range_km": 50},
            "n2": {"rx_lat": LAT + 5.0, "rx_lon": LON, "max_range_km": 50},
        }
        self._run(_CONFIRMED_N2, _solve_fn(), cfgs=cfgs)
        rec = self._only_record()
        assert rec["outcome"] == "rejected_beam"
        assert rec["beam_failures"][0]["rule"] == "range"

    def test_displacement_reject_records_distance(self):
        s_in = dict(_CONFIRMED_N2)
        s_in["initial_guess"] = {"lat": LAT + 1.0, "lon": LON, "alt_km": 9.0}
        self._run(s_in, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "rejected_displacement"
        # 1° of latitude ≈ 111 km, way past the 2 km gate.
        assert rec["displacement_km"] == pytest.approx(111.2, abs=1.0)
        assert rec["guess_lat"] == pytest.approx(LAT + 1.0)

    def test_n2_unconfirmed_is_recorded(self, monkeypatch):
        monkeypatch.setattr(solver_mod, "_resolve_n2_chi2", lambda *a: None)
        self._run({"n_nodes": 2}, _solve_fn())
        rec = self._only_record()
        assert rec["outcome"] == "n2_unconfirmed"
        assert rec["chi2_per_dof"] is None

    def test_unconverged_is_recorded(self):
        self._run(_CONFIRMED_N2, _solve_fn(success=False))
        assert self._only_record()["outcome"] == "unconverged"

    def test_age_prune_drops_old_records(self):
        state.mlat_solve_history.append({"ts_ms": int(time.time() * 1000) - 40 * 60 * 1000, "outcome": "published"})
        self._run(_CONFIRMED_N2, _solve_fn())
        outcomes = [r["outcome"] for r in state.mlat_solve_history]
        assert len(state.mlat_solve_history) == 1
        assert outcomes == ["published"]

    def test_published_record_carries_the_calibrated_sigma(self):
        """sigma_m is the disc radius the map draws, stamped into history so
        the 2026-09-05 calibration can be re-run from /api/test/mlat-history
        alone (fraction(gt_error_km*1000 <= 2.448*sigma_m) ~ 0.95)."""
        self._run(_CONFIRMED_N2, _solve_fn(n_nodes=2))
        rec = self._only_record()
        assert rec["outcome"] == "published"
        # Dark lane (no adsb_hex on the input, so the key is minted
        # mn-dark-*): the n=2 floor of 650 m times the 1.5 dark gain.  No
        # pos_sigma_km on this fixture, so the formal term contributes 0.
        assert rec["solve_key"].startswith("mn-dark-")
        assert rec["sigma_m"] == pytest.approx(975.0)

    def test_known_lane_record_is_not_dark_inflated(self):
        s_in = dict(_CONFIRMED_N2)
        s_in["adsb_hex"] = "abc123"
        self._run(s_in, _solve_fn(n_nodes=2))
        rec = self._only_record()
        assert not rec["solve_key"].startswith("mn-dark-")
        assert rec["sigma_m"] == pytest.approx(650.0)

    def test_record_without_n_nodes_has_no_sigma(self):
        # A solve the model cannot floor is recorded with sigma_m None rather
        # than a formal-sigma-only guess — same rule the feed uses to omit the
        # field entirely.
        self._run(_CONFIRMED_N2, _solve_fn())
        assert self._only_record()["sigma_m"] is None

    def test_reset_for_tests_clears_history(self):
        self._run(_CONFIRMED_N2, _solve_fn())
        assert state.mlat_solve_history
        state._reset_for_tests()
        assert not state.mlat_solve_history


class TestDisplacementCapByLane:
    """The displacement gate's cap is chosen by LANE, not by anchor label.

    What the gate measures is |solve - anchor|, which only stands in for
    position error while the anchor is trustworthy.  An ADS-B-anchored input
    has its guess overridden onto a transponder fix (~100 m), so 2 km is a
    statement about the solve.  A dark input's guess is a quantised 3 km
    association-grid point averaged over a cluster up to 6 km wide, so at
    2 km the gate was measuring the anchor: live, displacement was 41% of
    dark-lane attempts and the median rejected dark solve sat 2.1 km from
    ground truth, against a 0.98 km median for the ones it published.

    Mirror-point and wrong-frame ghosts land 15-50 km out and are still
    rejected by the wider dark cap — see test_displacement_reject_records_
    distance above, which is a dark input 111 km from its guess.
    """

    # 1 km of latitude in degrees, so a displacement can be stated in km.
    KM_DEG = 1.0 / 111.32
    # Transponder-shaped, so is_transponder_hex accepts it and the input is
    # ADS-B-anchored.  "obj-01373" below is the simulator object id that must
    # NOT buy the tight cap.
    ADSB_HEX = "a1b2c3"
    OBJ_ID = "obj-01373"

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run_displaced(self, km, adsb_hex=None):
        """Solve at (LAT, LON) whose guess sits ``km`` south of it."""
        s_in = dict(_CONFIRMED_N2)
        s_in["initial_guess"] = {"lat": LAT - km * self.KM_DEG, "lon": LON, "alt_km": 9.0}
        if adsb_hex is not None:
            s_in["adsb_hex"] = adsb_hex
        result = solver_mod._process_solver_item((s_in, {}, time.time()), _solve_fn())
        assert len(state.mlat_solve_history) == 1
        return result, state.mlat_solve_history[0]

    def test_default_dark_cap_is_two_grid_steps(self):
        assert solver_mod._MAX_DISPLACEMENT_KM_DARK == 2.0 * ASSOC_GRID_STEP_KM
        assert solver_mod._MAX_DISPLACEMENT_KM == 2.0

    def test_dark_solve_inside_the_dark_cap_is_published(self):
        """4 km: past the ADS-B cap, inside the dark one.  This is the case
        the change exists for — 23 of 31 live dark rejects were <= 4 km."""
        result, rec = self._run_displaced(4.0)
        assert result is not None and result["success"]
        assert rec["outcome"] == "published"
        assert rec["displacement_km"] == pytest.approx(4.0, abs=0.05)

    def test_dark_solve_past_the_dark_cap_is_rejected(self):
        result, rec = self._run_displaced(7.0)
        assert result is None
        assert rec["outcome"] == "rejected_displacement"
        assert rec["displacement_km"] == pytest.approx(7.0, abs=0.05)

    def test_non_transponder_id_is_judged_dark(self):
        """A simulator object id is not an ADS-B anchor: nothing overrode the
        guess with a transponder fix, so it gets the dark cap — the same
        predicate multinode_key_decision keys it into mn-dark-* with."""
        result, rec = self._run_displaced(4.0, adsb_hex=self.OBJ_ID)
        assert result is not None
        assert rec["outcome"] == "published"
        assert rec["displacement_cap_km"] == solver_mod._MAX_DISPLACEMENT_KM_DARK

    def test_adsb_anchored_solve_keeps_the_tight_cap(self):
        """The same 4 km displacement that publishes dark still rejects here:
        the ADS-B override put this guess within ~100 m of truth, so 4 km of
        drift is the solve being wrong, not the anchor."""
        result, rec = self._run_displaced(4.0, adsb_hex=self.ADSB_HEX)
        assert result is None
        assert rec["outcome"] == "rejected_displacement"

    def test_history_record_carries_the_cap_that_judged_it(self):
        _, dark_rec = self._run_displaced(4.0)
        assert dark_rec["displacement_cap_km"] == solver_mod._MAX_DISPLACEMENT_KM_DARK
        state._reset_for_tests()
        solver_mod._reset_for_tests()
        _, adsb_rec = self._run_displaced(4.0, adsb_hex=self.ADSB_HEX)
        assert adsb_rec["displacement_cap_km"] == solver_mod._MAX_DISPLACEMENT_KM

    def test_dark_reject_bumps_the_dark_counter_as_well(self):
        """The dark counter is a SUBSET of the aggregate, never a substitute:
        anything already reading solver_fail_displacement keeps its meaning
        across the lane split."""
        self._run_displaced(7.0)
        assert state.solver_fail_displacement == 1
        assert state.solver_fail_displacement_dark == 1
        assert state.solver_failures == 1

    def test_adsb_reject_leaves_the_dark_counter_alone(self):
        self._run_displaced(4.0, adsb_hex=self.ADSB_HEX)
        assert state.solver_fail_displacement == 1
        assert state.solver_fail_displacement_dark == 0

    def test_env_var_overrides_the_dark_cap(self, monkeypatch):
        """The override is an absolute km value, not a grid multiple."""
        monkeypatch.setenv("SOLVER_MAX_DISPLACEMENT_KM_DARK", "3.5")
        assert solver_mod._dark_displacement_cap_km() == 3.5
        monkeypatch.delenv("SOLVER_MAX_DISPLACEMENT_KM_DARK")
        assert solver_mod._dark_displacement_cap_km() == 2.0 * ASSOC_GRID_STEP_KM

    def test_a_narrowed_dark_cap_rejects_what_the_default_publishes(self, monkeypatch):
        """The other half of the override: the resolved value is what the
        gate reads, so lowering it takes the 4 km publish above back out."""
        monkeypatch.setattr(solver_mod, "_MAX_DISPLACEMENT_KM_DARK", 3.0)
        result, rec = self._run_displaced(4.0)
        assert result is None
        assert rec["outcome"] == "rejected_displacement"
        assert rec["displacement_cap_km"] == 3.0


class TestKeyDecisionObservability:
    """key_how / key_dist_km on the history record, and the dark key-decision
    counters behind them.

    Fragmentation is decided in multinode_key_decision and nowhere else, but
    until these fields existed the record kept only the key that came OUT: a
    freshly minted key and a re-key onto a live entry were indistinguishable
    after the fact, so neither the flat 6 km gate nor the age-scaled one that
    replaced it could be measured against live traffic.  key_dist_km is the
    other half — a re-key at 1 km and one at 9 km are very different claims
    about the same aircraft.
    """

    # 1 km of latitude in degrees, so a separation can be stated in km.
    KM_DEG = 1.0 / 111.32
    ADSB_HEX = "a1b2c3"

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _run(self, s_in, solve_fn):
        return solver_mod._process_solver_item((dict(s_in), {}, time.time()), solve_fn)

    def test_first_dark_solve_is_recorded_as_a_mint(self):
        self._run(_CONFIRMED_N2, _solve_fn())
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["key_how"] == "minted"
        # Nothing was matched, so there is no distance to report.
        assert rec["key_dist_km"] is None
        assert state.solver_key_minted_dark == 1
        assert state.solver_key_proximity_dark == 0

    def test_a_second_solve_nearby_is_recorded_as_a_re_key(self):
        self._run(_CONFIRMED_N2, _solve_fn())
        self._run(_CONFIRMED_N2, _solve_fn(lat=LAT + 3.0 * self.KM_DEG))
        # One aircraft, one entry — the point of the whole rule.
        assert len(state.multinode_tracks) == 1
        rec = state.mlat_solve_history[-1]
        assert rec["key_how"] == "proximity"
        assert rec["key_dist_km"] == pytest.approx(3.0, abs=0.3)
        assert state.solver_key_minted_dark == 1
        assert state.solver_key_proximity_dark == 1

    def test_adsb_lane_records_its_branch_and_no_distance(self):
        """The ADS-B lane keys off the transponder hex unconditionally — a
        branch, but not a decision, and not this gate's business."""
        self._run(dict(_CONFIRMED_N2, adsb_hex=self.ADSB_HEX), _solve_fn())
        rec = state.mlat_solve_history[-1]
        assert rec["solve_key"] == f"mn-adsb-{self.ADSB_HEX}"
        assert rec["key_how"] == "adsb"
        assert rec["key_dist_km"] is None
        assert state.solver_key_minted_dark == 0
        assert state.solver_key_proximity_dark == 0

    def test_a_reject_carries_neither_field(self):
        """The key is minted after the gates, so a rejected solve never ran
        the keying rule — the same reason solver_hex is None there."""
        self._run(_CONFIRMED_N2, _solve_fn(rms_delay=10.0))
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "rejected_rms_delay"
        assert rec["key_how"] is None
        assert rec["key_dist_km"] is None
        assert state.solver_key_minted_dark == 0


class TestEndpoint:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _client(self):
        from main import app

        return TestClient(app)

    def _publish(self, lat=LAT, lon=LON):
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn(lat, lon))

    def test_requires_hex_or_all(self):
        assert self._client().get("/api/test/mlat-history").status_code == 400

    def test_lookup_by_hex(self):
        self._publish()
        rec = state.mlat_solve_history[0]
        data = self._client().get(f"/api/test/mlat-history?hex={rec['solver_hex']}").json()
        assert data["n_solves"] == 1
        assert data["solves"][0]["solve_key"] == rec["solve_key"]

    def test_unknown_hex_returns_empty(self):
        self._publish()
        data = self._client().get("/api/test/mlat-history?hex=mn0000000000").json()
        assert data["n_solves"] == 0
        assert data["rejects_nearby"]["n"] == 0

    def test_all_returns_window(self):
        self._publish()
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(rms_delay=10.0),
        )
        data = self._client().get("/api/test/mlat-history?all=1").json()
        assert data["n_records"] == 2
        assert {r["outcome"] for r in data["records"]} == {
            "published",
            "rejected_rms_delay",
        }

    def test_rejects_nearby_within_10km(self):
        self._publish()
        # A reject 0.01° (~1.1 km) away: inside the 10 km radius.
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(LAT + 0.01, LON, rms_delay=10.0),
        )
        # A reject ~1° (~111 km) away: outside.
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(LAT + 1.0, LON, rms_delay=10.0),
        )
        hex_ = next(r["solver_hex"] for r in state.mlat_solve_history if r["outcome"] == "published")
        data = self._client().get(f"/api/test/mlat-history?hex={hex_}").json()
        assert data["rejects_nearby"]["n"] == 1
        assert data["rejects_nearby"]["by_outcome"] == {"rejected_rms_delay": 1}


class TestTrailSnapshotRace:
    """_nearest_gt must survive concurrent trail appends (2026-08-08 outage).

    Both solver workers died with "deque mutated during iteration" — the
    Python-level min()/_trail_velocity loops over a live trail deque raced
    the sim-ingest append.  The fix snapshots each trail with tuple(), a
    single C call the appender cannot interleave.  This stress test raced
    reliably within a few hundred iterations before the fix.
    """

    def test_nearest_gt_survives_concurrent_appends(self):
        import threading

        now = time.time()
        trail = deque(
            [[LAT + i * 1e-4, LON, 9000.0, now - 60 + i] for i in range(80)],
            maxlen=4000,
        )
        state.ground_truth_trails["race"] = trail
        stop = threading.Event()

        def _appender():
            # Untraced deliberately. coverage serialises every traced line on a
            # single data_lock, and this loop is hot enough that tracing it
            # starved the whole process: CI stacks showed most threads parked in
            # coverage.collector.lock_data while this one held it. That made a
            # single _nearest_gt call unboundedly slow, which no between-
            # iterations deadline can bound. The loop is test scaffolding and
            # carries nothing worth measuring.
            sys.settrace(None)
            i = 0
            while not stop.is_set():
                trail.append([LAT + i * 1e-5, LON, 9000.0, now + i * 1e-3])
                i += 1

        t = threading.Thread(target=_appender, daemon=True)
        t.start()
        # Bounded on both axes, whichever is reached first. 2000 iterations is
        # what reproduced the original race, and an unloaded machine still runs
        # all of them well inside the deadline. The appender never yields,
        # though, so on a contended runner the same 2000 iterations cost
        # minutes rather than seconds: this test was measured at 19.66s locally
        # and 17m57s on CI, which alone exceeded the job's 20 minute cap and
        # silently withheld every deploy from main. The deadline caps that
        # without weakening the race, which is a function of appends landing
        # mid-iteration rather than of any particular iteration count.
        deadline = time.monotonic() + 30.0
        completed = 0
        try:
            for _ in range(2000):
                if time.monotonic() >= deadline:
                    break
                solver_mod._nearest_gt(LAT, LON, now)
                completed += 1
        finally:
            stop.set()
            t.join(timeout=5.0)

        # Guards a vacuous pass: a misconfigured deadline would otherwise let
        # the loop exit having raced nothing at all, and the test would still
        # look green. Deliberately not a "few hundred" floor, because the slow
        # runners this deadline exists for are exactly the ones that could not
        # meet it, and a bound that fails on the machines it protects is worse
        # than the stall it replaces.
        assert completed > 0


class TestGtIdentityBinding:
    """Identified (seeded) solves must never proximity-bind to another
    aircraft's synthetic trail — real hexes with no GT trail of their own
    were picking up ~200 km phantom errors from whatever trail happened to
    be nearest.  A record with adsb_hex set is scored against that identity
    only (its own trail, else its own live ADS-B fix, else abstain).  Dark
    records (no adsb_hex) keep the legacy _nearest_gt proximity scan.
    """

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _record(self, adsb_hex=None, lat=LAT, lon=LON, ts_ms=None):
        solver_mod._record_solve_history(
            "rejected_gate",
            {"adsb_hex": adsb_hex, "timestamp_ms": ts_ms or int(time.time() * 1000)},
            None,
            solve_key="mn-test",
            raw_lat=lat,
            raw_lon=lon,
        )
        return state.mlat_solve_history[-1]

    def test_dark_record_keeps_proximity_scan(self):
        _put_gt("abc123")
        rec = self._record(adsb_hex=None)
        assert rec["gt_hex"] == "abc123"
        assert rec["gt_source"] == "proximity"

    def test_seeded_binds_own_trail_not_nearest(self):
        _put_gt("aaa111", lat=LAT + 0.5)  # own trail, ~55 km away
        _put_gt("bbb222", lat=LAT + 0.001)  # someone else's, close by
        rec = self._record(adsb_hex="aaa111")
        assert rec["gt_hex"] == "aaa111"
        assert rec["gt_source"] == "trail"
        assert rec["gt_error_km"] > 50

    def test_seeded_uppercase_hex_normalized(self):
        _put_gt("aaa111")
        rec = self._record(adsb_hex="AAA111")
        assert rec["gt_hex"] == "aaa111"
        assert rec["gt_source"] == "trail"

    def test_seeded_no_trail_uses_live_adsb(self):
        state.adsb_aircraft["ac60c4"] = {
            "lat": LAT + 0.01,
            "lon": LON,
            "gs": 0,
            "track": 0,
            "last_seen_ms": int(time.time() * 1000),
        }
        rec = self._record(adsb_hex="ac60c4")
        assert rec["gt_hex"] == "ac60c4"
        assert rec["gt_source"] == "adsb"
        assert 0.5 < rec["gt_error_km"] < 2.0

    def test_adsb_fix_dead_reckoned(self):
        # gs=194.384 kt ≈ 100 m/s due north, fix 10 s stale: DR should carry
        # the fix ~1 km north to meet the solve (without DR, error ~1 km).
        now_ms = int(time.time() * 1000)
        state.adsb_aircraft["ac60c4"] = {
            "lat": LAT,
            "lon": LON,
            "gs": 194.384,
            "track": 0,
            "last_seen_ms": now_ms - 10_000,
        }
        rec = self._record(
            adsb_hex="ac60c4",
            lat=LAT + 1000 / 111320,
            lon=LON,
            ts_ms=now_ms,
        )
        assert rec["gt_source"] == "adsb"
        assert rec["gt_error_km"] < 0.2

    def test_seeded_unknown_hex_abstains(self):
        _put_gt("bbb222")  # near, but a different identity — must not bind
        rec = self._record(adsb_hex="ac60c4")
        assert rec["gt_hex"] is None
        assert rec["gt_error_km"] is None
        assert rec["gt_source"] is None

    def test_seeded_stale_adsb_abstains(self):
        state.adsb_aircraft["ac60c4"] = {
            "lat": LAT,
            "lon": LON,
            "gs": 0,
            "track": 0,
            "last_seen_ms": int(time.time() * 1000) - 120_000,
        }
        rec = self._record(adsb_hex="ac60c4")
        assert rec["gt_hex"] is None
        assert rec["gt_error_km"] is None
        assert rec["gt_source"] is None

    def test_seeded_stale_trail_falls_back_to_adsb(self):
        _put_gt("aaa111", age_s=300)
        state.adsb_aircraft["aaa111"] = {
            "lat": LAT,
            "lon": LON,
            "gs": 0,
            "track": 0,
            "last_seen_ms": int(time.time() * 1000),
        }
        rec = self._record(adsb_hex="aaa111")
        assert rec["gt_source"] == "adsb"


class TestPerLaneDeques:
    """The known lane writes to its own deque; every reader merges the two.

    One shared deque made the 8 000-record cap a race rather than a retention
    rule: the known lane attempts a solve per claimed hex per pass and on the
    test fleet wrote ~4 200 records per 10 min against the dark lane's ~265,
    so a dark record was evicted by known-lane volume in ~18 min even though
    both endpoints accept a 35 min window and the solver age-prunes at 35.
    """

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _client(self):
        from main import app

        return TestClient(app)

    def test_regular_records_go_to_the_regular_deque(self):
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn())
        assert len(state.mlat_solve_history) == 1
        assert not state.mlat_solve_history_known

    def test_known_lane_records_go_to_the_known_deque(self):
        solver_mod._record_solve_history(
            "known_truth_match",
            {"n_nodes": 2, "adsb_hex": "abc123", "initial_guess": {"lat": LAT, "lon": LON}},
            {"success": True, "lat": LAT, "lon": LON, "n_nodes": 2},
            extra={"known_lane": True, "label": "truth_match", "published": False},
        )
        assert len(state.mlat_solve_history_known) == 1
        assert not state.mlat_solve_history

    def test_known_lane_volume_cannot_evict_dark_records(self, monkeypatch):
        """The failure the split fixes, at 1/1000 scale: a flood of known-lane
        records past the cap leaves the dark record untouched."""
        monkeypatch.setattr(state, "mlat_solve_history", deque(maxlen=8))
        monkeypatch.setattr(state, "mlat_solve_history_known", deque(maxlen=8))
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn())
        for _ in range(40):
            solver_mod._record_solve_history(
                "known_truth_match",
                {"n_nodes": 2, "adsb_hex": "abc123", "initial_guess": {"lat": LAT, "lon": LON}},
                {"success": True, "lat": LAT, "lon": LON, "n_nodes": 2},
                extra={"known_lane": True, "label": "truth_match", "published": False},
            )
        assert len(state.mlat_solve_history) == 1
        assert state.mlat_solve_history[0]["outcome"] == "published"
        assert len(state.mlat_solve_history_known) == 8

    def test_all_query_merges_both_lanes_in_ts_order(self):
        solver_mod._record_solve_history(
            "known_truth_match",
            {"n_nodes": 2, "adsb_hex": "abc123", "initial_guess": {"lat": LAT, "lon": LON}},
            {"success": True, "lat": LAT, "lon": LON, "n_nodes": 2},
            extra={"known_lane": True, "label": "truth_match", "published": False},
        )
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn())
        data = self._client().get("/api/test/mlat-history?all=1").json()
        assert data["n_records"] == 2
        assert {r["outcome"] for r in data["records"]} == {"published", "known_truth_match"}
        # ?all=1 is newest-first; the known record was written first.
        assert [r["ts_ms"] for r in data["records"]] == sorted((r["ts_ms"] for r in data["records"]), reverse=True)

    def test_hex_lookup_finds_a_known_lane_publish(self):
        """The known lane publishes under mn-adsb-*, so its records are
        reachable by marker hex exactly as before the split."""
        key = "mn-adsb-abc123"
        solver_mod._record_solve_history(
            "known_truth_match",
            {"n_nodes": 2, "adsb_hex": "abc123", "initial_guess": {"lat": LAT, "lon": LON}},
            {"success": True, "lat": LAT, "lon": LON, "n_nodes": 2},
            solve_key=key,
            raw_lat=LAT,
            raw_lon=LON,
            extra={"known_lane": True, "label": "truth_match", "published": True},
        )
        data = self._client().get(f"/api/test/mlat-history?hex={multinode_hex_from_key(key)}").json()
        assert data["n_solves"] == 1
        assert data["solves"][0]["solve_key"] == key

    def test_window_effective_minutes_reports_the_oldest_record_held(self):
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn())
        state.mlat_solve_history[0]["ts_ms"] -= int(6 * 60 * 1000)
        data = self._client().get("/api/test/mlat-history?all=1&minutes=35").json()
        assert data["window_minutes"] == 35.0
        assert 5.9 <= data["window_effective_minutes"] <= 6.1

    def test_window_effective_minutes_is_zero_on_an_empty_store(self):
        data = self._client().get("/api/test/mlat-history?all=1").json()
        assert data["window_effective_minutes"] == 0.0


class TestDarkAccuracySamples:
    """A published dark solve with a ground-truth match feeds
    state.accuracy_samples — the store health.py's solver_accuracy_degraded
    is computed from, which until now had no dark writer at all: its only
    general one (track_gates._record_accuracy_sample) sits behind an ADS-B
    fix the dark lane by definition does not have.
    """

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _publish(self, lat=LAT, lon=LON):
        solver_mod._process_solver_item((dict(_CONFIRMED_N2), {}, time.time()), _solve_fn(lat, lon))

    def test_published_dark_solve_with_gt_is_sampled(self):
        _put_gt()  # ~0.11 km north of the solve
        self._publish()
        assert len(state.accuracy_samples) == 1
        sample = state.accuracy_samples[0]
        # multinode_solve, not a new source name: that is what aircraft_feed
        # stamps on these tracks, so this closes a sampling hole rather than
        # inventing a category health.py would have to be taught about.
        assert sample["position_source"] == "multinode_solve"
        assert sample["lane"] == "dark"
        assert sample["error_km"] == state.mlat_solve_history[0]["gt_error_km"]
        assert sample["n_nodes"] == 2

    def test_no_ground_truth_means_no_sample(self):
        # Production has no ground-truth trails, so the alert's inputs there
        # are byte-identical to before this feed existed.
        self._publish()
        assert state.mlat_solve_history[0]["gt_error_km"] is None
        assert not state.accuracy_samples

    def test_rejected_solve_is_not_sampled(self):
        _put_gt()
        solver_mod._process_solver_item(
            (dict(_CONFIRMED_N2), {}, time.time()),
            _solve_fn(rms_delay=10.0),
        )
        assert state.mlat_solve_history[0]["outcome"] == "rejected_rms_delay"
        assert not state.accuracy_samples

    def test_adsb_anchored_solve_is_not_double_sampled_here(self):
        """The tagged lane already has a sampler on the enrichment path; this
        one is dark-only so the two cannot both score the same solve."""
        _put_gt()
        s_in = dict(_CONFIRMED_N2)
        s_in["adsb_hex"] = "abc123"
        solver_mod._process_solver_item((s_in, {}, time.time()), _solve_fn())
        assert state.mlat_solve_history[0]["outcome"] == "published"
        assert not state.accuracy_samples

    def test_known_lane_records_are_not_sampled_by_this_path(self):
        """The known lane has its own throttled sampler with its own source
        names, which health.py deliberately excludes."""
        _put_gt()
        solver_mod._record_solve_history(
            "known_truth_match",
            {"n_nodes": 2, "adsb_hex": "abc123", "initial_guess": {"lat": LAT, "lon": LON}},
            {"success": True, "lat": LAT, "lon": LON, "n_nodes": 2},
            raw_lat=LAT,
            raw_lon=LON,
            extra={"known_lane": True, "label": "truth_match", "published": True},
        )
        assert not state.accuracy_samples
