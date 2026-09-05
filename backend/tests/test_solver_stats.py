"""Tests for GET /api/test/solver-stats — the Solver Report panel's data source.

Funnel/reject/error stats come from the two solve-history deques merged and
windowed; ghost detection and consensus/counters read current live state
directly.  The top-level funnel is the DARK lane only — see
routes.test._solver_window_stats for why, and for the ghost definition.

_rec's defaults (no solve_key, no adsb_hex, no known_lane) make a DARK record,
so every pre-lane-split funnel test below still describes the lane those keys
were always meant to describe.
"""

import threading
import time
from collections import deque

from fastapi.testclient import TestClient

from core import state
from routes.test import _ERR_GT_GATE_KM, _GHOST_GATE_KM, _record_lane, _solver_window_stats
from services.tasks import solver as solver_mod


def _client():
    from main import app

    return TestClient(app)


def _rec(
    outcome,
    n_nodes=2,
    gt_error_km=None,
    age_s=0.0,
    solve_key=None,
    anchor_key=None,
    adsb_hex=None,
    known_lane=False,
    displacement_km=None,
):
    return {
        "ts_ms": int((time.time() - age_s) * 1000),
        "outcome": outcome,
        "n_nodes": n_nodes,
        "gt_error_km": gt_error_km,
        "solve_key": solve_key,
        "anchor_key": anchor_key,
        "adsb_hex": adsb_hex,
        "known_lane": known_lane,
        "displacement_km": displacement_km,
    }


def _push(rec):
    """Route a record to the deque the solver would have written it to."""
    if rec.get("known_lane"):
        state.mlat_solve_history_known.append(rec)
    else:
        state.mlat_solve_history.append(rec)


class TestFunnelAndRejects:
    def setup_method(self):
        state._reset_for_tests()

    def test_funnel_splits_n2_vs_n3plus(self):
        state.mlat_solve_history.append(_rec("published", n_nodes=2))
        state.mlat_solve_history.append(_rec("published", n_nodes=2))
        state.mlat_solve_history.append(_rec("published", n_nodes=3))
        state.mlat_solve_history.append(_rec("published", n_nodes=4))
        out = _solver_window_stats(10.0)
        assert out["published"] == {"total": 4, "n2": 2, "n3plus": 2}
        assert out["attempts"] == 4

    def test_rejected_prefix_is_stripped(self):
        state.mlat_solve_history.append(_rec("rejected_beam"))
        state.mlat_solve_history.append(_rec("rejected_beam"))
        state.mlat_solve_history.append(_rec("rejected_displacement"))
        out = _solver_window_stats(10.0)
        assert out["rejects"] == {
            "total": 3,
            "by_reason": {"beam": 2, "displacement": 1},
        }

    def test_reject_reason_without_prefix_kept_as_is(self):
        # n2_unconfirmed / n2_outbid / unconverged never carried the
        # rejected_ prefix (services/tasks/solver.py); they must not be
        # mangled by the strip.
        state.mlat_solve_history.append(_rec("n2_unconfirmed"))
        state.mlat_solve_history.append(_rec("unconverged"))
        out = _solver_window_stats(10.0)
        assert out["rejects"]["by_reason"] == {"n2_unconfirmed": 1, "unconverged": 1}
        assert out["rejects"]["total"] == 2

    def test_window_excludes_old_records(self):
        state.mlat_solve_history.append(_rec("published", age_s=20 * 60))
        state.mlat_solve_history.append(_rec("published", age_s=1))
        out = _solver_window_stats(10.0)
        assert out["attempts"] == 1


class TestPositionErrorGate:
    def setup_method(self):
        state._reset_for_tests()

    def test_error_gate_inclusion_and_exclusion(self):
        # In-gate: 0.4 km. Out-of-gate: 40.0 km (way past _ERR_GT_GATE_KM).
        assert _ERR_GT_GATE_KM == 15.0
        state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=0.4))
        state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=40.0))
        state.mlat_solve_history.append(_rec("published", n_nodes=3, gt_error_km=None))
        out = _solver_window_stats(10.0)
        assert out["position_error_km"]["n"] == 1
        assert out["position_error_km"]["median"] == 0.4
        assert out["position_error_km"]["p90"] == 0.4

    def test_exact_median_and_p90(self):
        errs = [0.1, 0.2, 0.5, 1.0, 2.0]
        for e in errs:
            state.mlat_solve_history.append(_rec("published", n_nodes=2, gt_error_km=e))
        out = _solver_window_stats(10.0)
        # sorted = [0.1, 0.2, 0.5, 1.0, 2.0]; n=5
        # median idiom (matches validate_ground_truth's p50): sorted[n//2] == sorted[2] == 0.5
        # p90 per spec: sorted[int(0.9*(n-1))] == sorted[int(3.6)] == sorted[3] == 1.0
        assert out["position_error_km"]["median"] == 0.5
        assert out["position_error_km"]["p90"] == 1.0
        assert out["position_error_km"]["n"] == 5

    def test_empty_position_error_is_none(self):
        out = _solver_window_stats(10.0)
        assert out["position_error_km"] == {"median": None, "p90": None, "n": 0}


class TestGhosts:
    def setup_method(self):
        state._reset_for_tests()

    def test_adsb_associated_by_key_prefix(self):
        state.multinode_tracks["mn-adsb-abc123"] = {"lat": 35.0, "lon": -82.0}
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["live_tracks"] == 1
        assert out["ghosts"]["adsb_associated"] == 1
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_adsb_associated_by_result_field(self):
        # Same association signal, this time via the result's own adsb_hex
        # field rather than the key prefix (belt-and-suspenders per spec).
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0, "adsb_hex": "abc123"}
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["adsb_associated"] == 1

    def test_close_to_ground_truth_is_matched_not_ghost(self):
        assert _GHOST_GATE_KM == 5.0
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km north of the track.
        state.ground_truth_trails["gt1"] = deque([[35.009, -82.0, 9000.0, time.time()]])
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["gt_matched"] == 1
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_far_from_everything_is_a_ghost(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~50 km away — outside both gates.
        state.ground_truth_trails["gt1"] = deque([[35.45, -82.0, 9000.0, time.time()]])
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["gt_matched"] == 0
        assert out["ghosts"]["ghost_tracks"] == 1
        assert out["ghosts"]["precision_pct"] == 0.0

    def test_close_to_fresh_adsb_is_not_a_ghost(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km away, fresh (just seen).
        state.adsb_aircraft["dead1"] = {
            "lat": 35.009,
            "lon": -82.0,
            "last_seen_ms": int(time.time() * 1000),
        }
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["ghost_tracks"] == 0

    def test_stale_adsb_entry_does_not_rescue(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        # ~1 km away but last seen 5 minutes ago — past the 60 s freshness gate.
        state.adsb_aircraft["dead1"] = {
            "lat": 35.009,
            "lon": -82.0,
            "last_seen_ms": int((time.time() - 300) * 1000),
        }
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["ghost_tracks"] == 1

    def test_precision_pct_denominator_is_dark_only(self):
        # One tagged track and one dark ghost.  The old denominator was
        # live_tracks, which scored this 50% — half the "precision" being an
        # ADS-B track that the ghost scan never even looked at.  Every dark
        # track here is a ghost, so dark precision is 0%.
        state.multinode_tracks["mn-adsb-a"] = {"lat": 35.0, "lon": -82.0}
        state.multinode_tracks["mn-dark-1"] = {"lat": 10.0, "lon": 10.0}  # ghost, nothing nearby
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["live_tracks"] == 2
        assert out["ghosts"]["adsb_associated"] == 1
        assert out["ghosts"]["dark_tracks"] == 1
        assert out["ghosts"]["ghost_tracks"] == 1
        assert out["ghosts"]["precision_pct"] == 0.0

    def test_precision_is_none_with_no_dark_tracks(self):
        # The structural lie this replaces: ADS-B tracks only, so the ghost
        # scan has nothing to score, and the answer used to be a confident
        # 100% precision with gt_matched pinned at 0 — indistinguishable from
        # a perfectly healthy dark lane.
        state.multinode_tracks["mn-adsb-a"] = {"lat": 35.0, "lon": -82.0}
        state.multinode_tracks["mn-adsb-b"] = {"lat": 35.1, "lon": -82.0}
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["dark_tracks"] == 0
        assert out["ghosts"]["gt_matched"] == 0
        assert out["ghosts"]["precision_pct"] is None
        assert out["ghosts"]["scope"] == "dark"

    def test_dark_partition_adds_up(self):
        # gt_matched + adsb_near + ghost_tracks == dark_tracks, exactly.
        now = time.time()
        state.multinode_tracks["mn-adsb-a"] = {"lat": 35.0, "lon": -82.0}
        state.multinode_tracks["mn-dark-gt"] = {"lat": 35.0, "lon": -82.0}
        state.multinode_tracks["mn-dark-adsb"] = {"lat": 36.0, "lon": -82.0}
        state.multinode_tracks["mn-dark-ghost"] = {"lat": 10.0, "lon": 10.0}
        state.ground_truth_trails["gt1"] = deque([[35.009, -82.0, 9000.0, now]])
        state.adsb_aircraft["real1"] = {"lat": 36.009, "lon": -82.0, "last_seen_ms": int(now * 1000)}
        g = _solver_window_stats(10.0)["ghosts"]
        assert g["dark_tracks"] == 3
        assert (g["gt_matched"], g["adsb_near"], g["ghost_tracks"]) == (1, 1, 1)
        assert g["gt_matched"] + g["adsb_near"] + g["ghost_tracks"] == g["dark_tracks"]
        # 2 of 3 dark tracks corroborated.
        assert g["precision_pct"] == 66.7

    def test_positionless_dark_track_is_out_of_the_denominator(self):
        state.multinode_tracks["mn-dark-1"] = {"lat": None, "lon": None}
        g = _solver_window_stats(10.0)["ghosts"]
        assert g["live_tracks"] == 1
        assert g["dark_tracks"] == 0
        assert g["precision_pct"] is None


class TestConsensusAndCounters:
    def setup_method(self):
        state._reset_for_tests()

    def test_consensus_and_counters_reflect_state(self):
        state.solver_successes = 5
        state.solver_failures = 2
        state.n2_unconfirmed = 1
        state.solver_trimmed = 3
        state.solver_stale_drops = 4
        state.solver_resolve_skips = 12
        state.solver_queue_drops = 6
        state.solver_consensus_selected = 7
        state.solver_consensus_filtered = 8
        state.solver_consensus_fallback = 9
        state.solver_consensus_shadow = 10
        state.solver_vel_untrusted_published = 11
        out = _solver_window_stats(10.0)
        assert out["counters"] == {
            "successes": 5,
            "failures": 2,
            "n2_unconfirmed": 1,
            "solver_trimmed": 3,
            "stale_drops": 4,
            "resolve_skips": 12,
            "queue_drops": 6,
            "worker_errors": 0,
            "vel_untrusted_published": 11,
        }
        assert out["consensus"]["selected"] == 7
        assert out["consensus"]["filtered"] == 8
        assert out["consensus"]["fallback"] == 9
        assert out["consensus"]["shadow"] == 10
        assert "mode" in out["consensus"]


class TestClaimingPassthrough:
    """The "claiming" block is a straight passthrough of the library
    associator's counters plus the solver-side anchor-honoring counters —
    same shape as TestConsensusAndCounters above."""

    def setup_method(self):
        state._reset_for_tests()

    def test_claiming_reflects_the_associator_and_solver_counters(self):
        _a = state.node_associator
        _a.claim_rounds = 4
        _a.claims_matched = 3
        _a.claim_conflicts = 1
        _a.anchored_inputs_emitted = 2
        _a.tracklets_excluded = 5
        state.solver_anchor_hits = 6
        state.solver_anchor_fallbacks = 7
        state.solver_anchored_published = 13
        out = _solver_window_stats(10.0)
        assert out["claiming"] == {
            "mode": _a.claim_mode,
            "rounds": 4,
            "matched": 3,
            "conflicts": 1,
            "anchored_inputs": 2,
            "tracklets_excluded": 5,
            "anchor_hits": 6,
            "anchor_fallbacks": 7,
            "anchored_published": 13,
        }

    def test_claiming_mode_defaults_to_off_in_tests(self):
        out = _solver_window_stats(10.0)
        assert out["claiming"]["mode"] == "off"


class TestFovStatsPassthrough:
    """The "fov" block is a straight passthrough of the FOV_MODE beam-gate
    counters — same shape as TestClaimingPassthrough above."""

    def setup_method(self):
        state._reset_for_tests()

    def test_fov_reflects_state_counters(self):
        state.fov_shadow_agree = 11
        state.fov_shadow_would_pass = 4
        state.fov_shadow_would_reject = 2
        state.fov_neg_events = 9
        out = _solver_window_stats(10.0)
        assert out["fov"] == {
            "mode": state.FOV_MODE,
            "shadow_agree": 11,
            "would_pass": 4,
            "would_reject": 2,
            "neg_events": 9,
        }

    def test_fov_mode_defaults_to_off_in_tests(self):
        out = _solver_window_stats(10.0)
        assert out["fov"]["mode"] == "off"
        assert out["fov"] == {
            "mode": "off",
            "shadow_agree": 0,
            "would_pass": 0,
            "would_reject": 0,
            "neg_events": 0,
        }


class TestKnownLaneAndClaimsPassthrough:
    """The "known_lane" / "known_claims" blocks are straight passthroughs of
    the since-boot counters the lane and the claiming stage bump — the only
    way to read them short of attaching a debugger."""

    def setup_method(self):
        state._reset_for_tests()

    def test_known_lane_reflects_the_lane_counters(self):
        # Bumped the way the lane itself bumps them, under counters_lock; the
        # known_lane_* names are registered onto state by the lane's import.
        for name, n in (
            ("known_lane_attempts", 9),
            ("known_lane_truth_match", 5),
            ("known_lane_ghost", 3),
            ("known_lane_no_converge", 1),
            ("known_lane_published", 4),
            ("known_lane_publish_errors", 1),
        ):
            state.bump_counter(name, n)
        out = _solver_window_stats(10.0)
        # publish_errors accounts for the truth_match minus published gap: 5
        # classified truth_match, 4 on the map, 1 that threw on the way there
        # (see known_lane._attempt's publish catch).
        assert out["known_lane"] == {
            "mode": state.KNOWN_LANE_MODE,
            "attempts": 9,
            "truth_match": 5,
            "ghost": 3,
            "no_converge": 1,
            "published": 4,
            "publish_errors": 1,
            # Windowed, and empty here — these are since-boot counters bumped
            # directly, with no history records behind them.
            "position_error_km": {"median": None, "p90": None, "n": 0, "window_minutes": 10.0},
        }

    def test_known_claims_reflects_the_claiming_counters(self):
        for name, n in (
            ("known_claims_made", 20),
            ("known_claim_contentions", 2),
            ("known_claims_bound", 15),
            ("known_claims_visibility_rejects", 6),
            ("known_claims_world_rejects", 3),
            ("known_claims_errors", 1),
        ):
            state.bump_counter(name, n)
        out = _solver_window_stats(10.0)
        assert out["known_claims"] == {
            "made": 20,
            "contentions": 2,
            "bound": 15,
            "visibility_rejects": 6,
            "world_rejects": 3,
            "errors": 1,
        }

    def test_both_blocks_zero_on_a_fresh_process(self):
        out = _solver_window_stats(10.0)
        assert out["known_lane"]["attempts"] == 0
        assert out["known_lane"]["published"] == 0
        assert out["known_claims"] == {
            "made": 0,
            "contentions": 0,
            "bound": 0,
            "visibility_rejects": 0,
            "world_rejects": 0,
            "errors": 0,
        }

    def test_lane_counters_absent_from_state_read_as_zero(self, monkeypatch):
        # The lane registers its counters at import and solver.py imports it
        # lazily, so a process that never ran a worker has them missing —
        # the payload must still build.
        monkeypatch.delattr(state, "known_lane_attempts")
        out = _solver_window_stats(10.0)
        assert out["known_lane"]["attempts"] == 0


class TestFragmentation:
    """Windowed, from published DARK records — the acceptance metric top-down
    claiming exists to move: distinct published keys.  Keys are spelled
    mn-dark-* because that prefix is what puts a record in this lane."""

    def setup_method(self):
        state._reset_for_tests()

    def test_distinct_keys_and_solves_per_key(self):
        # 5 distinct keys, solved 1, 1, 1, 2 and 3 times respectively.
        for key, n in (("mn-dark-a", 1), ("mn-dark-b", 1), ("mn-dark-c", 1), ("mn-dark-d", 2), ("mn-dark-e", 3)):
            for _ in range(n):
                state.mlat_solve_history.append(_rec("published", solve_key=key))
        out = _solver_window_stats(10.0)
        frag = out["fragmentation"]
        assert frag["distinct_keys"] == 5
        assert frag["published"] == 8
        # counts sorted = [1, 1, 1, 2, 3]; n=5
        # median idiom sorted[n//2] == sorted[2] == 1
        # p90 idiom sorted[int(0.9*(n-1))] == sorted[int(3.6)] == sorted[3] == 2
        assert frag["solves_per_key"]["median"] == 1
        assert frag["solves_per_key"]["p90"] == 2

    def test_anchored_pct_reads_anchor_key_regardless_of_outcome_scope(self):
        state.mlat_solve_history.append(_rec("published", solve_key="mn-dark-a", anchor_key="mn-dark-1"))
        state.mlat_solve_history.append(_rec("published", solve_key="mn-dark-b"))
        state.mlat_solve_history.append(_rec("published", solve_key="mn-dark-c"))
        state.mlat_solve_history.append(_rec("published", solve_key="mn-dark-d"))
        out = _solver_window_stats(10.0)
        assert out["fragmentation"]["anchored_pct"] == 25.0

    def test_rejects_do_not_count_toward_fragmentation(self):
        state.mlat_solve_history.append(_rec("rejected_beam", solve_key="mn-dark-a"))
        state.mlat_solve_history.append(_rec("published", solve_key="mn-dark-b"))
        out = _solver_window_stats(10.0)
        assert out["fragmentation"]["published"] == 1
        assert out["fragmentation"]["distinct_keys"] == 1

    def test_empty_window_no_division_errors(self):
        out = _solver_window_stats(10.0)
        assert out["fragmentation"] == {
            "distinct_keys": 0,
            "published": 0,
            "solves_per_key": {"median": None, "p90": None},
            "anchored_pct": 0.0,
            "dark_keys_minted": 0,
            "dark_keys_proximity": 0,
        }

    def test_dark_key_decision_counters_are_surfaced(self):
        """Key births vs re-keys, from the state counters solver.py bumps in
        the publish path.  Since boot, not windowed — the four keys above
        count what SURVIVED the decisions inside the window, these count the
        decisions themselves, and the panel needs both to tell a fragmenting
        lane from a busy one."""
        state.solver_key_minted_dark = 4
        state.solver_key_proximity_dark = 11
        out = _solver_window_stats(10.0)
        assert out["fragmentation"]["dark_keys_minted"] == 4
        assert out["fragmentation"]["dark_keys_proximity"] == 11

    def test_dark_key_decision_counters_reset_with_state(self):
        state.solver_key_minted_dark = 4
        state.solver_key_proximity_dark = 11
        state._reset_for_tests()
        out = _solver_window_stats(10.0)
        assert out["fragmentation"]["dark_keys_minted"] == 0
        assert out["fragmentation"]["dark_keys_proximity"] == 0


class TestEmptyState:
    def setup_method(self):
        state._reset_for_tests()

    def test_empty_state_no_division_errors(self):
        out = _solver_window_stats(10.0)
        assert out["attempts"] == 0
        assert out["published"] == {"total": 0, "n2": 0, "n3plus": 0}
        assert out["rejects"] == {"total": 0, "by_reason": {}}
        assert out["position_error_km"] == {"median": None, "p90": None, "n": 0}
        assert out["ghosts"]["live_tracks"] == 0
        assert out["ghosts"]["precision_pct"] is None
        assert out["lane_split"] == {"dark": 0, "adsb": 0, "known": 0}
        assert out["window_effective_minutes"] == 0.0


class TestEndpoint:
    def setup_method(self):
        state._reset_for_tests()

    def test_default_window(self):
        resp = _client().get("/api/test/solver-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_minutes"] == 10.0

    def test_claiming_and_fragmentation_blocks_present(self):
        resp = _client().get("/api/test/solver-stats")
        data = resp.json()
        assert data["claiming"].keys() == {
            "mode",
            "rounds",
            "matched",
            "conflicts",
            "anchored_inputs",
            "tracklets_excluded",
            "anchor_hits",
            "anchor_fallbacks",
            "anchored_published",
        }
        assert data["fragmentation"].keys() == {
            "distinct_keys",
            "published",
            "solves_per_key",
            "anchored_pct",
            "dark_keys_minted",
            "dark_keys_proximity",
        }
        assert data["fov"].keys() == {
            "mode",
            "shadow_agree",
            "would_pass",
            "would_reject",
            "neg_events",
        }

    def test_known_lane_and_known_claims_blocks_present(self):
        resp = _client().get("/api/test/solver-stats")
        data = resp.json()
        assert data["known_lane"].keys() == {
            "mode",
            "attempts",
            "truth_match",
            "ghost",
            "no_converge",
            "published",
            "publish_errors",
            "position_error_km",
        }
        assert data["known_claims"].keys() == {
            "made",
            "contentions",
            "bound",
            "visibility_rejects",
            "world_rejects",
            "errors",
        }

    def test_minutes_clamp_low(self):
        resp = _client().get("/api/test/solver-stats?minutes=0")
        assert resp.json()["window_minutes"] == 1.0

    def test_minutes_clamp_high(self):
        resp = _client().get("/api/test/solver-stats?minutes=1000")
        assert resp.json()["window_minutes"] == 35.0


class TestLaneSplit:
    """The funnel is the DARK lane; every record is classified first.

    Live before this split (test droplet, 10 min): ``attempts 4475,
    published 80`` with ``rejects.by_reason {known_truth_match: 3296,
    known_ghost: 914, displacement: 109, ...}``.  94% of the "attempts" were
    known-lane records, the known lane's own SUCCESS label led the reject
    table, and the dark lane's real 265 -> 80 could not be recovered from the
    payload at all.
    """

    def setup_method(self):
        state._reset_for_tests()

    def test_known_records_leave_the_funnel_and_land_in_lane_split(self):
        _push(_rec("published", solve_key="mn-dark-1"))
        _push(_rec("rejected_displacement"))
        for _ in range(3):
            _push(_rec("known_truth_match", known_lane=True, displacement_km=0.5))
        _push(_rec("known_ghost", known_lane=True, displacement_km=8.0))
        out = _solver_window_stats(10.0)
        assert out["lane_split"] == {"dark": 2, "adsb": 0, "known": 4}
        assert out["attempts"] == 2
        assert out["published"]["total"] == 1
        assert out["rejects"] == {"total": 1, "by_reason": {"displacement": 1}}
        # The known lane's success label is no longer a reject reason.
        assert "known_truth_match" not in out["rejects"]["by_reason"]
        assert "known_ghost" not in out["rejects"]["by_reason"]

    def test_adsb_lane_records_are_counted_but_not_funnelled(self):
        _push(_rec("published", solve_key="mn-adsb-a1b2c3"))
        _push(_rec("rejected_displacement", adsb_hex="a1b2c3"))
        _push(_rec("published", solve_key="mn-dark-1"))
        out = _solver_window_stats(10.0)
        assert out["lane_split"] == {"dark": 1, "adsb": 2, "known": 0}
        assert out["attempts"] == 1
        assert out["rejects"]["total"] == 0

    def test_lane_split_sums_to_the_whole_window(self):
        _push(_rec("published", solve_key="mn-dark-1"))
        _push(_rec("published", solve_key="mn-adsb-a1b2c3"))
        _push(_rec("known_truth_match", known_lane=True))
        _push(_rec("published", age_s=60 * 60))  # outside the window
        out = _solver_window_stats(10.0)
        assert sum(out["lane_split"].values()) == 3

    def test_position_error_and_fragmentation_are_dark_only(self):
        _push(_rec("published", solve_key="mn-dark-1", gt_error_km=1.0))
        _push(_rec("published", solve_key="mn-adsb-a1b2c3", gt_error_km=9.0))
        _push(_rec("known_truth_match", known_lane=True, solve_key="mn-adsb-b", gt_error_km=0.1))
        out = _solver_window_stats(10.0)
        assert out["position_error_km"] == {"median": 1.0, "p90": 1.0, "n": 1}
        assert out["fragmentation"]["published"] == 1
        assert out["fragmentation"]["distinct_keys"] == 1

    def test_known_lane_window_position_error(self):
        for d in (0.2, 0.5, 1.0, 4.0, 9.0):
            _push(_rec("known_truth_match", known_lane=True, displacement_km=d))
        pe = _solver_window_stats(10.0)["known_lane"]["position_error_km"]
        # Same percentile idiom as the dark block: sorted[n//2], sorted[int(.9*(n-1))].
        assert pe == {"median": 1.0, "p90": 4.0, "n": 5, "window_minutes": 10.0}


class TestRecordLane:
    """The key is the authority when there is one; a reject has none yet, so
    it falls back to the predicate that chose its displacement cap."""

    def test_known_flag_wins_over_everything(self):
        assert _record_lane({"known_lane": True, "solve_key": "mn-dark-1"}) == "known"

    def test_key_prefix_decides_a_published_record(self):
        assert _record_lane({"solve_key": "mn-dark-1"}) == "dark"
        assert _record_lane({"solve_key": "mn-adsb-a1b2c3"}) == "adsb"

    def test_keyless_reject_falls_back_to_the_input_identity(self):
        assert _record_lane({"solve_key": None, "adsb_hex": "a1b2c3"}) == "adsb"
        assert _record_lane({"solve_key": None, "adsb_hex": None}) == "dark"

    def test_non_transponder_id_is_dark(self):
        # A simulator object id is not a transponder identity — the same rule
        # multinode_key_decision and the dark displacement cap use.
        assert _record_lane({"solve_key": None, "adsb_hex": "obj-01373"}) == "dark"


class TestWindowEffectiveMinutes:
    """Truncation has to be legible.  The shared deque held ~18 min at the
    live record rate while both endpoints advertise a 35 min window, so
    ``?minutes=35`` answered out of 18 min of records with nothing in the
    payload saying so."""

    def setup_method(self):
        state._reset_for_tests()

    def test_reports_the_age_of_the_oldest_record_held(self):
        _push(_rec("published", age_s=8 * 60))
        _push(_rec("published", age_s=1))
        out = _solver_window_stats(35.0)
        assert out["window_minutes"] == 35.0
        assert 7.9 <= out["window_effective_minutes"] <= 8.1

    def test_never_exceeds_the_requested_window(self):
        _push(_rec("published", age_s=30 * 60))
        out = _solver_window_stats(10.0)
        assert out["window_effective_minutes"] == 10.0

    def test_counts_the_known_deque_too(self):
        # Oldest record overall is the known one; the merged view is what
        # both endpoints answer from.
        _push(_rec("known_truth_match", known_lane=True, age_s=12 * 60))
        _push(_rec("published", age_s=1))
        assert _solver_window_stats(35.0)["window_effective_minutes"] >= 11.9


class _MutatingRec(dict):
    """A track record that inserts a new track the first time it is read.

    Stands in for a solver worker publishing mid-scan.  Only a snapshot taken
    before the loop survives this; iterating state.multinode_tracks live
    raises "dictionary changed size during iteration", which is exactly how
    the endpoint 500'd.
    """

    def __init__(self, target, *a, **kw):
        super().__init__(*a, **kw)
        self._target = target
        self._fired = False

    def get(self, *a, **kw):
        if not self._fired:
            self._fired = True
            self._target[f"mn-dark-inserted-{len(self._target)}"] = {"lat": 1.0, "lon": 1.0}
        return super().get(*a, **kw)


class TestLiveStateSnapshots:
    """Every dict _solver_window_stats scans is written by another thread
    while the request runs, so each is snapshotted before iteration."""

    def setup_method(self):
        state._reset_for_tests()

    def test_multinode_tracks_iterated_under_the_solver_lock(self, monkeypatch):
        """The snapshot is taken while solver._MN_TRACKS_LOCK is held — the
        same lock solver._process_solver_item and known_lane._publish write
        the dict under."""
        lock = threading.Lock()
        monkeypatch.setattr(solver_mod, "_MN_TRACKS_LOCK", lock)
        seen = []

        class _Tracks(dict):
            def items(self):
                seen.append(lock.locked())
                return super().items()

        monkeypatch.setattr(state, "multinode_tracks", _Tracks({"mn-dark-1": {"lat": 35.0, "lon": -82.0}}))
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["dark_tracks"] == 1
        assert seen and all(seen), "multinode_tracks was iterated without _MN_TRACKS_LOCK"

    def test_concurrent_track_insert_does_not_raise(self):
        tracks = state.multinode_tracks
        tracks["mn-dark-1"] = _MutatingRec(tracks, {"lat": 35.0, "lon": -82.0})
        tracks["mn-dark-2"] = {"lat": 35.5, "lon": -82.0}
        # Pre-fix this raised RuntimeError: dictionary changed size during
        # iteration, and the endpoint returned a 500.
        out = _solver_window_stats(10.0)
        assert out["ghosts"]["live_tracks"] == 2

    def test_concurrent_ground_truth_trail_insert_does_not_raise(self):
        now = time.time()

        class _MutatingTrail(deque):
            def __iter__(self):
                state.ground_truth_trails.setdefault("gt-late", deque())
                return super().__iter__()

        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        state.ground_truth_trails["gt1"] = _MutatingTrail([[35.009, -82.0, 9000.0, now]])
        assert _solver_window_stats(10.0)["ghosts"]["gt_matched"] == 1

    def test_concurrent_adsb_insert_does_not_raise(self):
        now_ms = int(time.time() * 1000)

        class _MutatingFix(dict):
            def get(self, *a, **kw):
                state.adsb_aircraft.setdefault("late1", {"lat": 0.0, "lon": 0.0, "last_seen_ms": now_ms})
                return super().get(*a, **kw)

        state.multinode_tracks["mn-dark-1"] = {"lat": 35.0, "lon": -82.0}
        state.adsb_aircraft["real1"] = _MutatingFix({"lat": 35.009, "lon": -82.0, "last_seen_ms": now_ms})
        assert _solver_window_stats(10.0)["ghosts"]["ghost_tracks"] == 0
