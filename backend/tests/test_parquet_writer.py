"""Tests for the Parquet detection archive writer."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from config.constants import node_fuzz_max_km, node_fuzz_min_km
from core.nodes import NodeConfig
from main import app
from services import node_sites, publication, snr_series
from services import parquet_writer as pw
from services import public_location as pl
from services.aircraft_db import build as build_aircraft_db
from services.geo import haversine_km
from tests.ownership_helpers import register_node_row
from tests.test_aircraft_db import write_sources
from tests.test_node_models import CONFIG_DEFAULTS

NODE = "ret1a2b3c4d"


def _frame(timestamp_ms: int, n_dets: int = 3, with_adsb: bool = False) -> dict:
    return {
        "timestamp": timestamp_ms,
        "delay": [10.0 + i for i in range(n_dets)],
        "doppler": [-50.0 + i for i in range(n_dets)],
        "snr": [12.0 + i for i in range(n_dets)],
        "adsb": (
            [{"hex": "abcdef", "lat": 40.0, "lon": -74.0, "alt_baro": 35000, "gs": 480, "track": 270, "flight": "UAL1"}]
            + [None] * (n_dets - 1)
            if with_adsb
            else [None] * n_dets
        ),
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


def test_writes_hive_partitioned_path(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )

    assert key.startswith("year=2025/month=01/day=15/node_id=node-A/part-143022-")
    assert key.endswith(".parquet")
    assert (tmp_path / key).exists()


def test_same_second_batches_do_not_replace_previous_detections(tmp_path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    first = pw.write_detections_parquet(
        node_id="node-A", frames=[_frame(1000, n_dets=1)], base_dir=tmp_path, write_ts=ts
    )
    second = pw.write_detections_parquet(
        node_id="node-A", frames=[_frame(2000, n_dets=1)], base_dir=tmp_path, write_ts=ts
    )
    assert first != second
    assert pq.read_table(tmp_path / first, partitioning=None).column("frame_ts_ms").to_pylist() == [1000]
    assert pq.read_table(tmp_path / second, partitioning=None).column("frame_ts_ms").to_pylist() == [2000]


@pytest.mark.parametrize("has_previous", [False, True])
def test_failed_write_leaves_no_partial_file_or_damaged_previous_batch(tmp_path, monkeypatch, has_previous):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    if has_previous:
        pw.write_detections_parquet(node_id="node-A", frames=[_frame(1000, n_dets=1)], base_dir=tmp_path, write_ts=ts)
    previous = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def partial_write(table, where, **kwargs):
        Path(where).write_bytes(b"incomplete parquet")
        raise OSError("disk full")

    monkeypatch.setattr(pq, "write_table", partial_write)
    with pytest.raises(OSError, match="disk full"):
        pw.write_detections_parquet(node_id="node-A", frames=[_frame(2000, n_dets=1)], base_dir=tmp_path, write_ts=ts)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == previous


def test_schema_is_per_detection_with_required_columns(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=4)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )

    table = pq.read_table(tmp_path / key, partitioning=None)
    assert table.num_rows == 4
    cols = set(table.column_names)
    expected = {
        "frame_ts_ms",
        "node_id",
        "detection_index",
        "delay_us",
        "doppler_hz",
        "snr_db",
        "adsb_hex",
        "adsb_lat",
        "adsb_lon",
        "adsb_alt_baro",
        "adsb_gs",
        "adsb_track",
        "adsb_flight",
        "signing_mode",
        "signature_valid",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_adsb_match_populated_when_present(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=3, with_adsb=True)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )

    table = pq.read_table(tmp_path / key, partitioning=None)
    rows = table.to_pylist()
    assert rows[0]["adsb_hex"] == "abcdef"
    assert rows[0]["adsb_lat"] == 40.0
    assert rows[1]["adsb_hex"] is None
    assert rows[2]["adsb_hex"] is None


def test_multiple_frames_concatenate(tmp_path: Path):
    frames = [
        _frame(timestamp_ms=1700000000000, n_dets=3),
        _frame(timestamp_ms=1700000001000, n_dets=2),
    ]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )
    table = pq.read_table(tmp_path / key, partitioning=None)
    assert table.num_rows == 5
    rows = table.to_pylist()
    assert rows[0]["frame_ts_ms"] == 1700000000000
    assert rows[3]["frame_ts_ms"] == 1700000001000
    assert rows[3]["detection_index"] == 0


def test_empty_frames_returns_none(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=[],
        base_dir=tmp_path,
        write_ts=ts,
    )
    assert key is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_uses_zstd_compression(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=10)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )
    meta = pq.read_metadata(tmp_path / key)
    rg = meta.row_group(0)
    codecs = {rg.column(i).compression for i in range(rg.num_columns)}
    assert "ZSTD" in codecs or "zstd" in {c.lower() for c in codecs}


def test_schema_includes_custody_and_ingest_columns(tmp_path: Path):
    """Schema must include payload_hash, signature, ingest_ts_ms and round-trip values."""
    frames = [
        {
            "timestamp": 1700000000000,
            "delay": [10.0, 11.0],
            "doppler": [-50.0, -49.0],
            "snr": [12.0, 13.0],
            "adsb": [None, None],
            "payload_hash": "deadbeef",
            "signature": "abcd1234",
            "_signing_mode": "ed25519",
            "_signature_valid": True,
        }
    ]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )
    table = pq.read_table(tmp_path / key, partitioning=None)
    cols = set(table.column_names)
    assert {"payload_hash", "signature", "ingest_ts_ms"} <= cols

    rows = table.to_pylist()
    assert rows[0]["payload_hash"] == "deadbeef"
    assert rows[0]["signature"] == "abcd1234"
    expected_ms = int(ts.timestamp() * 1000)
    assert rows[0]["ingest_ts_ms"] == expected_ms


def test_custody_columns_default_null_when_absent(tmp_path: Path):
    """Frames without payload_hash/signature get nulls; ingest_ts_ms is always set."""
    frames = [_frame(timestamp_ms=1700000000000, n_dets=2)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )
    rows = pq.read_table(tmp_path / key, partitioning=None).to_pylist()
    assert all(r["payload_hash"] is None for r in rows)
    assert all(r["signature"] is None for r in rows)
    assert all(isinstance(r["ingest_ts_ms"], int) for r in rows)


def test_a_frame_s_epoch_is_archived_and_null_where_it_has_none(tmp_path: Path):
    """A polled radar's frames say which epoch of the node they came from; a fleet node's say nothing."""
    polled = dict(_frame(timestamp_ms=1700000000000, n_dets=2), epoch=3)
    fleet = _frame(timestamp_ms=1700000001000, n_dets=1)

    key = pw.write_detections_parquet(node_id="node-A", frames=[polled, fleet], base_dir=tmp_path)

    rows = pq.read_table(tmp_path / key, partitioning=None).to_pylist()
    assert [r["epoch"] for r in rows] == [3, 3, None]


def test_round_trip_via_storage_module(tmp_path: Path, monkeypatch):
    """archive_detections + read_archived_file round-trips back to legacy JSON shape."""
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    from services.storage import archive_detections, read_archived_file

    frames = [_frame(timestamp_ms=1700000000000, n_dets=2, with_adsb=True)]
    key = archive_detections("node-X", frames)
    assert key is not None
    assert key.endswith(".parquet")

    decoded = read_archived_file(key)
    assert decoded is not None
    assert decoded["node_id"] == "node-X"
    assert decoded["count"] == 1
    fr0 = decoded["detections"][0]
    assert fr0["delay"] == [10.0, 11.0]
    assert fr0["adsb"][0]["hex"] == "abcdef"
    assert fr0["adsb"][1] is None


def test_an_archived_frame_s_epoch_is_read_back(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    from services.storage import archive_detections, read_archived_file

    polled = dict(_frame(timestamp_ms=1700000000000, n_dets=1), epoch=2)
    fleet = _frame(timestamp_ms=1700000001000, n_dets=1)
    decoded = read_archived_file(archive_detections("node-X", [polled, fleet]))
    assert [fr["epoch"] for fr in decoded["detections"]] == [2, None]


def test_schema_includes_geometry_and_rf_columns(tmp_path: Path):
    """rx/tx geometry and RF config are fanned out into every row from node_cfg.

    rx_lat/rx_lon are the PUBLISHED receiver coordinate, not the configured
    one: the archive is downloadable raw, and a Parquet file pins a receiver
    permanently.  See services/public_location.py and test_public_location.py.
    tx stays verbatim — transmitters are licensed broadcast towers.
    """
    frames = [_frame(timestamp_ms=1700000000000, n_dets=3)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    cfg = {
        "rx_lat": 33.9,
        "rx_lon": -84.62,
        "rx_alt_ft": 920,
        "tx_lat": 33.9,
        "tx_lon": -84.331,
        "tx_alt_ft": 1200,
        "fc_hz": 195_000_000,
        "fs_hz": 2_000_000,
    }

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
        node_cfg=cfg,
    )
    table = pq.read_table(tmp_path / key, partitioning=None)
    cols = set(table.column_names)
    assert {
        "rx_lat",
        "rx_lon",
        "rx_alt_ft",
        "tx_lat",
        "tx_lon",
        "tx_alt_ft",
        "fc_hz",
        "fs_hz",
        "adsb_squawk",
        "adsb_category",
    } <= cols

    rows = table.to_pylist()
    # One published coordinate for the whole file: exactly what public_latlon
    # publishes for this node, displaced from the true receiver by a distance
    # inside the fuzz donut.  Not merely "!= the true value": an offset that
    # points almost due east or west leaves the 4-decimal latitude unchanged
    # (and likewise longitude for near north/south), so an inequality on one
    # axis holds only for most salts, not all of them.
    published = {(r["rx_lat"], r["rx_lon"]) for r in rows}
    assert published == {pl.public_latlon(cfg["rx_lat"], cfg["rx_lon"], "node-A")}
    ((rx_lat, rx_lon),) = published
    displaced_km = haversine_km(cfg["rx_lat"], cfg["rx_lon"], rx_lat, rx_lon)
    # 4-decimal rounding moves the published point by up to ~8 m.
    assert node_fuzz_min_km() - 0.02 <= displaced_km <= node_fuzz_max_km() + 0.02
    assert all(r["tx_lat"] == 33.9 for r in rows)
    assert all(r["tx_lon"] == -84.331 for r in rows)
    assert all(r["fc_hz"] == 195_000_000 for r in rows)
    assert all(r["fs_hz"] == 2_000_000 for r in rows)


def test_geometry_columns_default_null_when_no_cfg(tmp_path: Path):
    frames = [_frame(timestamp_ms=1700000000000, n_dets=2)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
    )
    rows = pq.read_table(tmp_path / key, partitioning=None).to_pylist()
    for col in ("rx_lat", "rx_lon", "tx_lat", "tx_lon", "fc_hz", "fs_hz"):
        assert all(r[col] is None for r in rows), f"{col} should default to null"


def test_legacy_FC_Fs_keys_in_node_cfg(tmp_path: Path):
    """Old-style FC/Fs keys in node_cfg are accepted as fallbacks for fc_hz/fs_hz."""
    frames = [_frame(timestamp_ms=1700000000000, n_dets=1)]
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    cfg = {"FC": 100_000_000, "Fs": 5_000_000}

    key = pw.write_detections_parquet(
        node_id="node-A",
        frames=frames,
        base_dir=tmp_path,
        write_ts=ts,
        node_cfg=cfg,
    )
    rows = pq.read_table(tmp_path / key, partitioning=None).to_pylist()
    assert rows[0]["fc_hz"] == 100_000_000
    assert rows[0]["fs_hz"] == 5_000_000


def test_a_row_carries_claim_aircraft_illuminator_and_site_and_a_bare_flush_writes(tmp_path, monkeypatch, request):
    request.addfinalizer(node_sites._reset_for_tests)
    asyncio.run(register_node_row(NODE))
    with publication._sync_engine().begin() as conn:
        conn.execute(insert(NodeConfig).values(node_id=NODE, version=1, **CONFIG_DEFAULTS | {"tx_callsign": "KXYZ"}))
    node_sites._reset_for_tests()
    types = {"A00001": "A320", "A00002": "XX99", "A00003": "C172"}
    build_aircraft_db(*write_sources(tmp_path / "src", types, [("A00001", "1")]), tmp_path / "db" / "aircraft_db.npz")
    monkeypatch.setattr("services.aircraft_db.DB_DIR", tmp_path / "db")
    claim = dict(
        claim_hex="a00001", claim_path="tag", claim_contested=True, claim_delay_res_us=1.5, claim_doppler_res_hz=-2.5
    )
    held = claim | {"claim_path": "hold", "claim_fix_delay_res_us": 0.75}
    dark = claim | {"claim_hex": None, "claim_path": "follow", "claim_contested": False}
    # The claimed detection's own tag names another aircraft: the claim is the answer.
    frame = _frame(1700000000000, n_dets=4) | {"adsb": [{"hex": "a00003"}, {"hex": "a00002"}, None, None]}

    def written(node_id: str, **extra) -> list[tuple]:
        key = pw.write_detections_parquet(node_id=node_id, frames=[frame | extra], base_dir=tmp_path)
        table = pq.read_table(tmp_path / key, partitioning=None)
        # After epoch, in order: string, string, bool, three doubles, three strings, double, string.
        assert "".join(str(kind)[0] for kind in table.schema.types[29:40]) == "ssbdddsssds"
        return [row[29:40] for row in zip(*table.to_pydict().values(), strict=True)]

    assert written(NODE, _claims={0: held, 2: dark}) == [
        ("a00001", "hold", True, 1.5, -2.5, 0.75, "A320", "narrowbody", "KXYZ", 0.5, NODE),
        (None, None, None, None, None, None, "XX99", "unknown", "KXYZ", 0.5, NODE),
        (None, "follow", False, 1.5, -2.5, None, None, None, "KXYZ", 0.5, NODE),
        (None, None, None, None, None, None, None, None, "KXYZ", 0.5, NODE),
    ]
    monkeypatch.setattr("services.aircraft_db.DB_DIR", tmp_path / "missing")
    assert written("ret2b3c4d5e") == [(None,) * 10 + ("ret2b3c4d5e",)] * 4


def test_a_row_carries_the_hardware_and_the_tower_power_only_of_a_matched_tower(tmp_path, request):
    """A tower found on another frequency is not known to be the illuminator, so its power stays out."""
    request.addfinalizer(node_sites._reset_for_tests)
    hardware = dict(sdr_model="SDR-A1", gain_reduction_ref_db=20.0, gain_reduction_surv_db=25.5, lna_state=3)
    hardware |= dict(antenna_model="Yagi-8", antenna_gain_dbi=11.5, antenna_polarisation="vertical")
    hardware |= dict(feedline_loss_db=1.5, local_adsb_receiver=True, notch_dab=False, notch_rf=True)
    for node_id, status in (("ret5e6f7a8b", "matched"), ("retc0ffee00", "frequency_mismatch")):
        asyncio.run(register_node_row(node_id))
        tower = {"tower_callsign": "QBBB", "tower_eirp_dbm": 75.5, "tower_match_status": status}
        with publication._sync_engine().begin() as conn:
            conn.execute(insert(NodeConfig).values(node_id=node_id, version=1, **CONFIG_DEFAULTS, **hardware, **tower))
    node_sites._reset_for_tests()

    def written(node_id: str) -> tuple[str, list[tuple]]:
        key = pw.write_detections_parquet(node_id=node_id, frames=[_frame(1700000000000, n_dets=2)], base_dir=tmp_path)
        table = pq.ParquetFile(tmp_path / key).read()
        rows = [row[40:] for row in zip(*table.to_pydict().values(), strict=True)]
        return "".join(str(kind)[0] for kind in table.schema.types[40:]), rows

    descriptors = ("SDR-A1", 20.0, 25.5, 3, "Yagi-8", 11.5, "vertical", 1.5, True, False, True)
    assert written("ret5e6f7a8b") == ("dsddisdsdbbb", [(75.5, *descriptors)] * 2)
    assert written("retc0ffee00") == ("dsddisdsdbbb", [(None, *descriptors)] * 2)


def test_a_day_of_old_and_new_parts_reads_as_it_always_did_through_every_reader(tmp_path, monkeypatch):
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))
    ref = asyncio.run(register_node_row(NODE))
    flushed = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
    stamps = [1791797400000, 1791797400001, 1791797400002]
    current = pw.PUBLISHED_SCHEMA
    api = TestClient(app)
    # 28 columns is a part from before epoch, 29 one from before the claim columns.
    for stamp, columns in zip(stamps, (28, 29, len(current)), strict=True):
        monkeypatch.setattr(pw, "PUBLISHED_SCHEMA", pa.schema(list(current)[:columns], metadata=current.metadata))
        frame = _frame(stamp, n_dets=1) | {"tx_lat": 34.8, "tx_lon": -82.3}
        key = pw.write_detections_parquet(node_id=NODE, frames=[frame], base_dir=tmp_path, write_ts=flushed)
        body = api.get("/api/data/archive/" + key.replace(f"node_id={NODE}", f"node_ref={ref}")).json()
        assert json.dumps(body) == (
            f'{{"timestamp": "", "count": 1, "detections": [{{"timestamp": {stamp}, "delay": [10.0], "doppler": [-50.0], '
            '"snr": [12.0], "adsb": [null], "_signing_mode": "unknown", "_signature_valid": false, "epoch": null, '
            '"rx_lat": null, "rx_lon": null, "rx_alt_ft": null, "tx_lat": 34.8, "tx_lon": -82.3, "tx_alt_ft": null, '
            f'"fc_hz": null, "fs_hz": null}}], "node_ref": "{ref}"}}'
        )
    window = "from=2026-10-12T09:25:00Z&to=2026-10-12T09:35:00Z&fleet=all&bbox=34,-83,35.5,-81.5"
    assert [row["frame_ts_ms"] for row in api.get(f"/api/data/detections?{window}").json()["rows"]] == stamps
    series = snr_series.snr_series(NODE, 1, since=datetime(2026, 10, 1, tzinfo=timezone.utc), now_ms=stamps[2] + 1000)
    assert [point["count"] for point in series["points"]] == [3]
