"""scripts/node_performance.py: the detection tables of one node and day, from archive files the repo's writers wrote.

Ids and positions are invented: the receiver, transmitter and aircraft position of
test_performance_data.py (Rr 15.98 km, delay 32.65 us, 12,000 ft). Three aircraft
are in the aggregator's snapshot at the frame's time; two are tagged, one by a
claim that falls below the claim gate. 1000 more detections at 5.0 dB set the
node's threshold: all in bin 20, half 500, (20 - 0.5 + 500 / 1000) * 0.25 = 5.0.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine

from core.nodes import Node, NodeConfig
from core.users import Base
from scripts import node_performance
from services import adsb_writer, compaction, parquet_writer, performance_predict
from services import performance_fit as pfit
from tests.object_store_env import store_env
from tests.test_performance_job import World, config_row

BACKEND_DIR = Path(__file__).resolve().parents[1]
NODE = "radar-alpha"
FLUSHED = datetime(2026, 3, 10, 18, 0, 0, tzinfo=UTC)
T = int(datetime(2026, 3, 10, 12, 0, 5, tzinfo=UTC).timestamp() * 1000)
CONFIG = {
    "node_id": NODE,
    "version": 1,
    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    "rx_lat": 10.0,
    "rx_lon": 20.0,
    "rx_alt_ft": 300.0,
    "tx_lat": 10.2,
    "tx_lon": 20.0,
    "tx_alt_ft": 1000.0,
    "fc_hz": 200e6,
    "fs_hz": 2e6,
    "cpi_s": 0.5,
    "max_range_km": 59.96,
    "delay_tolerance_us": 5.0,
    "doppler_tolerance_hz": 5.0,
}
WINDOW = ["--node", NODE, "--from", "2026-03-10", "--to", "2026-03-10"]
OBJECT_STORE = {
    "OBJECT_STORE_ENDPOINT_URL": "https://objects.example.com",
    "OBJECT_STORE_ACCESS_KEY_ID": "test-key-id",
    "OBJECT_STORE_SECRET_ACCESS_KEY": "test-secret",
    "OBJECT_STORE_BUCKET": "test-bucket",
}


def claim(hex_: str, path: str) -> dict:
    return {
        "claim_hex": hex_,
        "claim_path": path,
        "claim_contested": False,
        "claim_delay_res_us": 0.1,
        "claim_doppler_res_hz": 1.0,
    }


@pytest.fixture
def world(tmp_path) -> list[str]:
    """The options that point a run at the written archive, registry and aircraft file."""
    position = {"lat": 10.1, "lon": 20.1, "alt_baro": 12000}
    snr = [12.0, 5.5, 12.0] + [5.0] * 1000
    frame = {
        "timestamp": T,
        "delay": [30.0] * len(snr),
        "doppler": [-50.0] * len(snr),
        "snr": snr,
        "adsb": [{"hex": "a1b2c3", **position}, None, {"hex": "c0ffee", **position}] + [None] * 1000,
        "_signing_mode": "unknown",
        "_signature_valid": False,
        "_claims": {0: claim("a1b2c3", "tag"), 1: claim("d4e5f6", "dr")},
    }
    parquet_writer.write_detections_parquet(
        node_id=NODE, frames=[frame], base_dir=tmp_path / "archive", write_ts=FLUSHED
    )
    row = {"ts_ms": T, "lat": 10.1, "lon": 20.1, "alt_baro": 12000, "gs": 450.0, "track": 90.0, "flight": None}
    rows = [
        {**row, "hex": h, "seen_ms": T, "world": "real", "source": "aggregator", "source_node_id": NODE}
        for h in ("a1b2c3", "d4e5f6", "c0ffee")
    ]
    rows.append({**rows[0], "source": "node"})
    adsb_writer.write_adsb_parquet(rows=rows, base_dir=tmp_path / "adsb", write_ts=FLUSHED)

    aircraft = tmp_path / "aircraft.npz"
    hexes = np.array([0xA1B2C3, 0xC0FFEE], dtype=np.uint32)
    types = np.array(["B738", "C172"])
    np.savez(aircraft, hex=hexes, type_idx=np.array([0, 1], dtype=np.uint16), types=types, schema=np.array(1))

    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    Base.metadata.create_all(engine, tables=[Node.__table__, NodeConfig.__table__])
    with engine.begin() as connection:
        connection.execute(NodeConfig.__table__.insert(), CONFIG)
    engine.dispose()
    return [
        *WINDOW,
        *("--registry", str(tmp_path / "registry.db"), "--aircraft-db", str(aircraft)),
        *("--archive-root", str(tmp_path / "archive"), "--adsb-root", str(tmp_path / "adsb")),
        *("--data-dir", str(tmp_path)),
    ]


def test_a_window_prints_the_detection_tables(world, capsys):
    assert node_performance.main(world) == 0
    text = capsys.readouterr().out
    assert "threshold 5.00 dB" in text
    assert "opportunities 3, detections 1 (tags 2, claims 1)" in text
    for line in (
        "  all in-window opportunities   0.33         3",
        "  10-20                         0.33         3",
        "  25-50                         0.33         3",
        "  2-20                          0.33         3",
        "  narrowbody                    1.00         1",
        "  light_GA                      0.00         1",
        "  unknown                       0.00         1",
    ):
        assert line in text.splitlines()

    assert node_performance.main([*world, "--json", "--numerator", "tags"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["numerator"], report["detections"], report["threshold_db"]) == ("tags", 2, 5.0)
    assert [(row["label"], row["pd"]) for row in report["by_class"]] == [
        ("light_GA", 1.0),
        ("narrowbody", 1.0),
        ("unknown", 0.0),
    ]

    assert node_performance.main([*world, "--json", "--opportunities", "node"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["opportunities"], report["denominator"]) == (1, "node")
    assert "rank_status" not in report["performance"]
    assert "pd_ceiling" not in report["performance"]
    assert node_performance.main([*world, "--opportunities", "node"]) == 0
    assert "rank status" not in capsys.readouterr().out


def test_the_system_constant_takes_the_power_of_the_tower_stored_in_the_data_dir(tmp_path, capsys):
    # The daily job's test day: 300 typed aircraft, the near ones mostly tagged, enough for a curve.
    built = World(tmp_path)
    built.registry(config_row())
    built.day(date(2026, 3, 11), NODE)
    aircraft = tmp_path / "aircraft.npz"
    hexes = np.arange(0xA00000, 0xA00000 + 300, dtype=np.uint32)
    np.savez(aircraft, hex=hexes, type_idx=np.zeros(300, dtype=np.uint16), types=np.array(["B738"]), schema=np.array(1))
    roots = [f"--{name.replace('_', '-')}={path}" for name, path in built.roots().items()]
    args = ["--node", NODE, "--from", "2026-03-11", "--to", "2026-03-11", "--aircraft-db", str(aircraft), *roots]
    entry = {"callsign": "QAAA-TV", "frequency_mhz": 200.0, "eirp_dbm": 81.2, "status": "matched", "date": "2026-03-01"}
    stored = tmp_path / "tower_match.json"
    stored.write_text(json.dumps({"refreshed_at": None, "towers": {"QAAA|200.000|10.2000|20.0000": entry}}))

    assert node_performance.main([*args, "--data-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert {"system constant 63.6 dB (measured)", "delay residual MAD 0.00 us (tags)"} <= set(captured.out.splitlines())
    assert captured.err == ""

    stored.unlink()
    assert node_performance.main([*args, "--data-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "system constant none" in captured.out.splitlines()
    assert captured.err == f"node_performance: no {stored}, the tower is unknown\n"


def test_a_window_with_no_opportunities_exits_1(tmp_path, capsys):
    configs = tmp_path / "configs.json"
    row = {**CONFIG, "created_at": "2026-01-01 00:00:00"}
    rows = [row, {**row, "node_id": "radar-beta"}]
    configs.write_text(json.dumps(rows))
    roots = ("--archive-root", str(tmp_path / "archive"), "--adsb-root", str(tmp_path / "adsb"))

    assert node_performance.main([*WINDOW, "--configs", str(configs), *roots]) == 1
    assert capsys.readouterr().out == ""


def test_a_missing_registry_is_refused_and_not_created(tmp_path):
    registry = tmp_path / "absent.db"

    assert node_performance.main([*WINDOW, "--registry", str(registry)]) == 2
    assert not registry.exists()


def test_the_script_imports_neither_main_nor_core_state():
    code = "import sys, scripts.node_performance; print(sorted({'main', 'core.state'} & set(sys.modules)))"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_a_day_whose_detections_may_have_left_local_disk_is_refused(world, capsys):
    with store_env(OBJECT_STORE):
        assert node_performance.main(world) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "node_performance: 2026-03-10: its detections may have been deleted from local disk after their upload\n"
    )
    assert captured.out == ""


def test_a_day_whose_adsb_compact_left_local_disk_is_refused(world, tmp_path, capsys, monkeypatch):
    partition = tmp_path / "adsb" / "year=2026" / "month=03" / "day=10"
    compaction.sweep(compaction.ADSB, str(tmp_path / "adsb"))
    for path in partition.glob("*.parquet"):
        path.unlink()
    assert [path.name for path in partition.iterdir()] == ["compact.json"]
    monkeypatch.setattr("config.constants.ARCHIVE_RETENTION_DAYS", 0)

    with store_env(OBJECT_STORE):
        assert node_performance.main(world) == 2
    assert capsys.readouterr().err == (
        "node_performance: 2026-03-10: the ADS-B partition of 2026-03-10 may be in object storage only\n"
    )

    # As the command line runs, with no logging configured: a warning logged on the way is a second stderr line.
    (partition / "compact.json").write_text("{ not a manifest")
    child = "import sys, config.constants as c; c.ARCHIVE_RETENTION_DAYS = 0; from scripts import node_performance as n"
    run = [sys.executable, "-c", f"{child}; sys.exit(n.main(sys.argv[1:]))", *world]
    done = subprocess.run(run, cwd=BACKEND_DIR, env={**os.environ, **OBJECT_STORE}, capture_output=True, text=True)
    assert (done.returncode, done.stderr) == (
        2,
        "node_performance: 2026-03-10: the ADS-B partition of 2026-03-10 has a broken compact.json or upload record\n",
    )


def test_a_configs_row_without_node_id_is_refused(tmp_path, capsys):
    configs = tmp_path / "configs.json"
    configs.write_text(json.dumps([{"created_at": "2026-01-01 00:00:00"}]))

    assert node_performance.main([*WINDOW, "--configs", str(configs)]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"node_performance: {configs}: row 0 is not a config row with node_id and created_at\n"
    assert captured.out == ""

    configs.write_text(json.dumps([{"node_id": NODE, "created_at": None}]))
    assert node_performance.main([*WINDOW, "--configs", str(configs)]) == 2
    assert capsys.readouterr().err == f"node_performance: {configs}: row 0 has created_at null, not an ISO date\n"


def test_a_window_whose_days_saw_two_towers_is_refused(world, tmp_path, capsys):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}")
    with engine.begin() as connection:
        moved = {**CONFIG, "version": 2, "created_at": datetime(2026, 3, 11, tzinfo=UTC), "tx_lat": 10.3}
        connection.execute(NodeConfig.__table__.insert(), moved)
    engine.dispose()

    assert node_performance.main([*world, "--from", "2026-03-09", "--to", "2026-03-11"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "node_performance: 2026-03-10: the transmitter tower changed inside the window\n"
    assert captured.out == ""


# Band and power explain every node of this fleet to the decibel, so its tau sits on the 0.1 dB floor.
EXPLAINED = (
    ("tt-node-01", 79.1, 5.0, "VHF", 82.0),
    ("tt-node-04", 77.4, 5.2, "VHF", 79.5),
    ("tt-node-06", 80.3, 4.8, "VHF", 84.0),
    ("tt-node-02", 66.2, 5.1, "UHF", 80.0),
    ("tt-node-05", 64.8, 5.0, "UHF", 77.0),
    ("tt-node-03", 67.5, 4.9, "UHF", 83.5),
)
FLEET = (
    ("tt-node-01", 79.1, 5.0, "VHF", 82.0),
    ("tt-node-04", 74.4, 5.2, "VHF", 79.5),
    ("tt-node-06", 82.3, 4.8, "VHF", 84.0),
    ("tt-node-02", 63.2, 5.1, "UHF", 80.0),
    ("tt-node-05", 68.8, 5.0, "UHF", 77.0),
    ("tt-node-03", 66.5, 4.9, "UHF", 83.5),
)


def stored_model(data_dir, fleet):
    rows = [
        {
            "node_id": node_id,
            "system_constant_db": measured,
            "sigma_db": 1.0,
            "threshold_db": threshold,
            "processing_gain_db": 60.0,
            "window": ["2026-03-05", "2026-03-11"],
            "descriptors": {"band": band, "tower_eirp_dbm": eirp},
        }
        for node_id, measured, threshold, band, eirp in fleet
    ]
    model = performance_predict.model_file(rows, pfit.DEFAULT_CLASS_OFFSETS_DB, date(2026, 3, 12))
    stored = data_dir / performance_predict.MODEL_FILE
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps(model))
    return model


def test_a_node_that_does_not_exist_is_predicted_from_the_stored_model(tmp_path, capsys):
    model = stored_model(tmp_path, FLEET)
    assert model["status"] == "fitted"

    # The band is read as the job reads a config row's, and no fitted node carries a polarisation.
    terms = ["--band", "uhf", "--tower-eirp-dbm", "80", "--tower-polarisation", "H"]
    assert node_performance.main(["--predict", *terms, "--json", "--data-dir", str(tmp_path)]) == 0
    calibration = model["calibration"]
    mean, sigma = performance_predict.predict(calibration, {"band": "UHF", "tower_eirp_dbm": 80.0})
    threshold = calibration["threshold_db"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "system_constant_db": mean,
        "prediction_sigma_db": sigma,
        "tau_db": calibration["tau_db"],
        "threshold_db": threshold,
        "class_margin_db": {
            name: mean + offset - threshold for name, offset in calibration["class_offsets_db"].items()
        },
    }
    assert captured.err == (
        "node_performance: tower_polarisation is not in the model (no fitted node carries it): fleet average used\n"
    )

    assert node_performance.main(["--predict", "--band", "FM", "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().err == "node_performance: band FM is not seen in the fleet: prior added to the band\n"


def test_without_a_published_calibration_the_model_print_explains_and_the_other_modes_exit_1(tmp_path, capsys):
    assert node_performance.main(["--predict", "--band", "UHF", "--data-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    stored = tmp_path / performance_predict.MODEL_FILE
    assert (captured.out, captured.err) == ("", f"node_performance: no fitted system constant model in {stored}\n")

    stored_model(tmp_path, EXPLAINED)
    why = "tau sits at a bound of its range on 6 nodes, so none is published"
    assert node_performance.main(["--system-constant", "--data-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines()[:3] == [
        "status tau_at_bound, fitted on 2026-03-12, 6 nodes",
        f"no calibration in {stored}: {why}",
        "leave-one-node-out",
    ]
    assert "tt-node-03" in captured.out
    for mode in (["--system-constant", "--calibration"], ["--predict", "--band", "UHF"]):
        assert node_performance.main([*mode, "--data-dir", str(tmp_path)]) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", f"node_performance: no calibration in {stored}: {why}\n")


def test_a_model_file_that_does_not_parse_as_a_json_object_exits_1_naming_it(tmp_path, capsys):
    stored = tmp_path / performance_predict.MODEL_FILE
    stored.parent.mkdir(parents=True)
    for text, why in (
        ("not json", "it doesn't parse (Expecting value: line 1 column 1 (char 0))"),
        ("[]", "it is not a JSON object"),
    ):
        stored.write_text(text)
        assert node_performance.main(["--system-constant", "--data-dir", str(tmp_path)]) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", f"node_performance: no system constant model in {stored}: {why}\n")
