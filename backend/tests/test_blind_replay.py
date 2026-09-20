"""The evaluation path cannot feed aircraft truth back into estimation."""

from copy import deepcopy

import pytest

from scripts.blind_replay import (
    DetectionLabels,
    TruthIndex,
    blind_detections,
    evaluate,
    select_exclusive_hypotheses,
    solve_candidate,
)


def candidate():
    return {
        "timestamp_ms": 20000,
        "n_nodes": 2,
        "initial_guess": {"lat": 34, "lon": -82, "alt_km": 5},
        "initial_velocity": {"vel_east_ms": 100, "vel_north_ms": 20},
        "measurements": [{"node_id": "one", "delay_us": 50, "doppler_hz": 20, "snr": 10}],
        "cv_epochs": [
            {"t_s": t, "measurements": [{"node_id": "one", "delay_us": 50, "doppler_hz": 20, "snr": 10}]}
            for t in (1, 5, 10, 20)
        ],
    }


def test_truth_and_identity_cannot_enter_tracker():
    frame = {"delay": [10], "doppler": [30], "snr": [12]}
    tagged = {**frame, "adsb_hex": ["abc123"], "adsb": [{"lat": 89, "lon": 0, "alt_baro": 90000}]}
    assert blind_detections(tagged, 4) == blind_detections(frame, 4) == [{"delay": 10, "doppler": 30, "snr": 12}]


def test_post_solve_identity_join_detects_crossed_aircraft():
    frames = [
        {"node_id": nid, "frame": {"timestamp": 20000, "delay": [50], "doppler": [20], "adsb_hex": [h]}}
        for nid, h in (("one", "abc123"), ("two", "def456"))
    ]
    raw = candidate()
    raw["measurements"].append({"node_id": "two", "delay_us": 50, "doppler_hz": 20})
    assert DetectionLabels(frames).for_candidate(raw) == (None, "identity_conflict")
    frames[1]["frame"]["adsb_hex"] = ["abc123"]
    assert DetectionLabels(frames).for_candidate(raw) == ("abc123", "node_identity_consensus")


def test_uncertainty_gate_does_not_depend_on_truth(monkeypatch):
    monkeypatch.setattr("scripts.blind_replay.reference_for", lambda *a: (None, "no_reference_match"))
    rec = {
        "candidate": candidate(),
        "result": {
            "lat": 34,
            "lon": -82,
            "alt_m": 7000,
            "timestamp_ms": 20000,
            "chi2_per_dof": 0.1,
            "horizontal_sigma_km": 12,
        },
        "outcome": "converged",
    }
    assert evaluate([rec], {}, TruthIndex([]))["counts"]["accepted"] == 1
    assert evaluate([rec], {}, TruthIndex([]), max_horizontal_sigma=2)["counts"]["accepted"] == 0


def test_one_track_cannot_publish_two_hypotheses_in_the_same_round():
    records = []
    for chi2, tid in ((0.2, "a"), (0.1, "a"), (0.3, "b")):
        c = candidate()
        c["track_ids_by_node"] = {"one": [tid]}
        records.append({"candidate": c, "result": {"chi2_per_dof": chi2, "alt_m": 7000}, "outcome": "converged"})
    select_exclusive_hypotheses(records)
    assert [r["selected"] for r in records] == [False, True, True]
    records[0]["candidate"]["timestamp_ms"] += 30000
    select_exclusive_hypotheses(records)
    assert all(r["selected"] for r in records)


def test_solver_boundary_allowlist_and_altitude_starts_are_truth_independent(monkeypatch):
    calls = []

    def solve(inp, configs):
        calls.append(deepcopy(inp))
        return {"success": True, "lat": 34, "lon": -82, "alt_m": 7000, "chi2_per_dof": 1}

    monkeypatch.setattr("scripts.blind_replay.solver.fit_constant_velocity", solve)
    raw = candidate()
    solve_candidate(raw, {})
    first = deepcopy(calls)
    calls.clear()
    raw["adsb_hex"] = "abc123"
    raw["adsb_fix"] = {"lat": 80, "lon": 80, "alt_m": 17000}
    raw["cv_epochs"][0]["measurements"][0]["adsb"] = raw["adsb_fix"]
    solve_candidate(raw, {})
    assert calls == first
    assert [c["initial_guess"]["alt_km"] for c in calls] == [3, 7, 11]


def test_fixed_layer_experiment_never_reads_adsb_altitude(monkeypatch):
    calls = []

    def solve(inp, configs, **kwargs):
        calls.append((inp["initial_guess"]["alt_km"], kwargs))
        return None

    monkeypatch.setattr("scripts.blind_replay.solver.fit_constant_velocity", solve)
    raw = candidate()
    raw["adsb_fix"] = {"alt_m": 13000}
    solve_candidate(raw, {}, altitude_model="layers")
    assert calls == [(alt, {"fix_altitude": True}) for alt in (3, 7, 11)]


def test_all_node_refit_cannot_inherit_uncertainty_from_a_different_pair_fit(monkeypatch):
    pair_result = {
        "success": True,
        "lat": 34,
        "lon": -82,
        "alt_m": 7000,
        "chi2_per_dof": 0.1,
        "vel_east": 100,
        "vel_north": 20,
        "horizontal_sigma_km": 0.01,
        "contributing_node_ids": ["one", "two"],
    }
    monkeypatch.setattr("scripts.blind_replay.solver.fit_constant_velocity", lambda *a: dict(pair_result))
    monkeypatch.setattr("scripts.blind_replay.solver.solve_multinode", lambda *a, **kw: dict(pair_result))
    raw = candidate()
    raw["n_nodes"] = 3
    result, outcome = solve_candidate(raw, {"one": {"fc_hz": 100e6}})
    assert outcome == "converged"
    assert result["horizontal_sigma_km"] is None


def test_truth_age_uses_capture_time_and_propagates_to_measurement_epoch():
    index = TruthIndex(
        [
            {
                "aircraft": {
                    "abc123": {
                        "lat": 34,
                        "lon": -82,
                        "alt_m": 7000,
                        "vel_east": 100,
                        "vel_north": 0,
                        "timestamp_ms": 100000,
                    }
                }
            }
        ]
    )
    assert index.at(105000)["abc123"]["lon"] > -82
    assert index.at(120000) == {}
    assert index.at(105000, identities={"abc123", "missing"}) == index.at(105000)
    assert index.at(105000, identities=set()) == {}
    assert index.at(120000, identities={"abc123"}) == {}


@pytest.mark.parametrize("fc", [None, 0, -1, True, "100000000", float("nan"), float("inf")])
def test_bad_multinode_frequency_rejects_candidate_without_aborting_replay(monkeypatch, fc):
    monkeypatch.setattr(
        "scripts.blind_replay.solver.fit_constant_velocity",
        lambda *a: {"success": True, "lat": 34, "lon": -82, "alt_m": 7000, "chi2_per_dof": 1},
    )
    raw = candidate()
    raw["n_nodes"] = 3
    assert solve_candidate(raw, {"one": {"fc_hz": fc}}) == (None, "invalid_geometry")
    assert solve_candidate(raw, {}) == (None, "invalid_geometry")


def test_failed_solves_remain_in_reference_denominator(monkeypatch):
    ref = {"lat": 34, "lon": -82, "alt_m": 7000, "vel_east": 0, "vel_north": 0}
    monkeypatch.setattr("scripts.blind_replay.reference_for", lambda *a: (ref, "abc123"))
    rec = {"candidate": candidate(), "result": None, "outcome": "no_convergence"}
    result = evaluate([rec], {}, TruthIndex([]))
    assert result["counts"]["reference_eligible"] == 1
    assert result["reference_solve_rate"] == 0
    assert result["accepted_error_km"]["median"] is None


def test_wrong_solve_is_scored_against_measurement_identity_not_nearest_aircraft(monkeypatch):
    ref = {"lat": 34, "lon": -82, "alt_m": 7000, "vel_east": 0, "vel_north": 0}
    monkeypatch.setattr("scripts.blind_replay.reference_for", lambda *a: (ref, "abc123"))
    rec = {
        "candidate": candidate(),
        "result": {"lat": 35, "lon": -82, "alt_m": 7000, "timestamp_ms": 20000, "chi2_per_dof": 1},
        "outcome": "converged",
    }
    result = evaluate([rec], {}, TruthIndex([]))
    assert result["accepted_error_km"]["median"] == pytest.approx(111.195, abs=0.01)
    assert result["counts"]["accepted_within_5km"] == 0
