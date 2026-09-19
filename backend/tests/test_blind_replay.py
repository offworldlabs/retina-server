"""The evaluation path cannot feed aircraft truth back into estimation."""

from copy import deepcopy

import pytest

from scripts.blind_replay import TruthIndex, blind_detections, evaluate, solve_candidate


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
