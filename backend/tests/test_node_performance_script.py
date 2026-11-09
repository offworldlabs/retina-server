"""scripts/node_performance.py: the detection tables of one node and day, from archive files the repo's writers wrote.

Ids and positions are invented: the receiver, transmitter and aircraft position of
test_performance_data.py (Rr 15.98 km, delay 32.65 us, 12,000 ft). Three aircraft
are in the aggregator's snapshot at the frame's time; two are tagged, one by a
claim that falls below the claim gate. 1000 more detections at 5.0 dB set the
node's threshold: all in bin 20, half 500, (20 - 0.5 + 500 / 1000) * 0.25 = 5.0.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine

from core.nodes import Node, NodeConfig
from core.users import Base
from scripts import node_performance
from services import adsb_writer, compaction, parquet_writer
from services import performance_fit as pfit
from tests.object_store_env import store_env

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


def test_a_window_says_what_bounds_the_constant_and_what_filled_the_residual(world, capsys, monkeypatch):
    figures = {
        "system_constant_db": 88.0,
        "system_constant_bound": "at_least",
        "tag_delay_residual_mad_us": 0.3,
        "delay_residual_source": "claims",
    }
    fit_node = pfit.fit_node
    monkeypatch.setattr(pfit, "fit_node", lambda merged, **options: fit_node(merged, **options) | figures)

    assert node_performance.main(world) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "system constant 88.0 dB (at_least)" in lines
    assert "delay residual MAD 0.30 us (claims)" in lines

    assert node_performance.main([*world, "--json", "--opportunities", "node"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["delay_residual_source"] == "claims"
    assert "system_constant_bound" not in report["performance"]


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
