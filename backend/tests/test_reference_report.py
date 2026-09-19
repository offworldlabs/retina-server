"""Coverage evidence must remain distinct from an airspace recall estimate."""

import json

import pytest

from scripts.blind_replay import TruthIndex, load_captures, main
from scripts.reference_report import node_report


def test_coverage_requires_identity_and_physics_agreement(monkeypatch):
    monkeypatch.setattr("scripts.reference_report.predict_observation", lambda *a: (50, 20))
    frames = [
        {
            "node_id": "test",
            "received_s": 102,
            "config": {"rx_lat": 34, "rx_lon": -82, "tx_lat": 34.1, "tx_lon": -82.1, "fc_hz": 100e6},
            "frame": {
                "timestamp": 100000,
                "delay": [50, 100, 50],
                "doppler": [20, 20, 20],
                "snr": [15, 15, 15],
                "adsb_hex": ["abc123", "abc123", None],
            },
        }
    ]
    truth = TruthIndex(
        [
            {
                "aircraft": {
                    "abc123": {
                        "timestamp_ms": 100000,
                        "lat": 34.05,
                        "lon": -82.05,
                        "alt_m": 7000,
                        "vel_east": 0,
                        "vel_north": 0,
                    }
                }
            }
        ]
    )
    result = node_report(frames, truth)
    node = result["nodes"]["test"]
    assert node["detections"] == 3
    assert node["fresh_reference"] == 2
    assert node["physics_agreed"] == 1
    assert sum(c["samples"] for c in node["observed_cells"]) == 1
    assert node["delay_residual_us"]["median"] == 25  # outliers remain in diagnostics
    assert result["suggested_calibration"] == {}  # insufficient independent aircraft


def test_active_capture_tolerates_only_an_incomplete_final_line(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text('{"kind":"truth","aircraft":{}}\n{"kind":')
    assert len(load_captures([path])[1]) == 1
    path.write_text('{"kind":\n{"kind":"truth","aircraft":{}}\n')
    with pytest.raises(json.JSONDecodeError):
        load_captures([path])


def test_calibration_cannot_train_on_evaluation_capture(tmp_path, monkeypatch):
    capture, calibration = tmp_path / "capture.jsonl", tmp_path / "calibration.json"
    capture.write_text(json.dumps({"kind": "frame", "frame": {"timestamp": 1000}}) + "\n")
    calibration.write_text(json.dumps({"trained_until_ms": 1000, "suggested_calibration": {}}))
    monkeypatch.setattr(
        "sys.argv",
        ["blind_replay", str(capture), "--calibration", str(calibration), "--output", str(tmp_path / "out.json")],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
