"""Tests for the multi-node solver track Parquet writer."""

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

from services import parquet_io
from services import track_writer as tw
from services.solve_uncertainty import solve_sigma_m

# The schema before track_key and the columns after it; files written with it
# are still in the archive.
OLD_SCHEMA = pa.schema(
    [
        ("solve_ts_ms", pa.int64()),
        ("frame_ts_ms", pa.int64()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("alt_m", pa.float64()),
        ("vel_east_ms", pa.float64()),
        ("vel_north_ms", pa.float64()),
        ("vel_up_ms", pa.float64()),
        ("n_nodes", pa.int32()),
        ("contributing_node_ids", pa.string()),
        ("adsb_hex", pa.string()),
        ("rms_delay_us", pa.float64()),
        ("rms_doppler_hz", pa.float64()),
        ("target_class", pa.string()),
    ]
)
NEW_COLUMNS = [
    ("track_key", pa.string()),
    ("lane", pa.string()),
    ("pos_sigma_m", pa.float64()),
    ("is_anomalous", pa.bool_()),
    ("anomaly_types", pa.string()),
    ("node_kind", pa.string()),
    ("solve_count", pa.int32()),
    ("contributing_node_refs", pa.string()),
]
TS = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)


def _record(ts_ms: int = 1700000000000, **overrides) -> dict:
    base = {
        "solve_ts_ms": ts_ms + 50,
        "timestamp_ms": ts_ms,
        "lat": 40.7,
        "lon": -74.0,
        "alt_m": 10500.0,
        "vel_east": 220.5,
        "vel_north": -33.0,
        "vel_up": 0.0,
        "n_nodes": 3,
        "contributing_node_ids": ["node-A", "node-B", "node-C"],
        "adsb_hex": "abcd12",
        "rms_delay": 1.2,
        "rms_doppler": 4.5,
        "target_class": "aircraft",
    }
    base.update(overrides)
    return base


def test_writes_hive_partitioned_path(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    assert key.startswith("year=2025/month=01/day=15/part-143022-")
    assert key.endswith(".parquet")
    assert (tmp_path / key).exists()


def test_schema_columns_present(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    schema = pq.read_schema(tmp_path / key)
    assert schema.names == [
        "solve_ts_ms",
        "frame_ts_ms",
        "lat",
        "lon",
        "alt_m",
        "vel_east_ms",
        "vel_north_ms",
        "vel_up_ms",
        "n_nodes",
        "contributing_node_ids",
        "adsb_hex",
        "rms_delay_us",
        "rms_doppler_hz",
        "target_class",
        "track_key",
        "lane",
        "pos_sigma_m",
        "is_anomalous",
        "anomaly_types",
        "node_kind",
        "solve_count",
        "contributing_node_refs",
    ]
    # Appended only: the old columns keep their names, types and places, and
    # every new one is nullable.
    assert list(tw.SCHEMA)[: len(OLD_SCHEMA)] == list(OLD_SCHEMA)
    assert [(f.name, f.type, f.nullable) for f in list(tw.SCHEMA)[len(OLD_SCHEMA) :]] == [
        (name, typ, True) for name, typ in NEW_COLUMNS
    ]


def test_contributing_nodes_serialized_as_csv(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[_record()], base_dir=tmp_path, write_ts=ts)
    rows = pq.read_table(tmp_path / key).to_pylist()
    assert rows[0]["contributing_node_ids"] == "node-A,node-B,node-C"


def test_nullable_fields_round_trip(tmp_path: Path):
    rec = _record()
    rec.pop("alt_m")
    rec.pop("rms_delay")
    rec["adsb_hex"] = None
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[rec], base_dir=tmp_path, write_ts=ts)
    row = pq.read_table(tmp_path / key).to_pylist()[0]
    assert row["alt_m"] is None
    assert row["rms_delay_us"] is None
    assert row["adsb_hex"] is None
    assert row["lat"] == 40.7  # required field still populated


def test_empty_records_returns_none(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    key = tw.write_tracks_parquet(records=[], base_dir=tmp_path, write_ts=ts)
    assert key is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_batches_written_in_same_second_are_both_retained(tmp_path: Path):
    ts = datetime(2025, 1, 15, 14, 30, 22, tzinfo=timezone.utc)
    first = tw.write_tracks_parquet(records=[_record(ts_ms=1000)], base_dir=tmp_path, write_ts=ts)
    second = tw.write_tracks_parquet(records=[_record(ts_ms=2000)], base_dir=tmp_path, write_ts=ts)
    assert first != second
    assert pq.read_table(tmp_path / first).column("frame_ts_ms").to_pylist() == [1000]
    assert pq.read_table(tmp_path / second).column("frame_ts_ms").to_pylist() == [2000]


def test_failed_write_never_publishes_partial_parquet(tmp_path: Path, monkeypatch):
    # Fails after the first row group is on disk, so the temp file is a real
    # partial Parquet file when the error lands.
    write_group = pq.ParquetWriter.write_table
    groups = []

    def fail_on_second_group(self, table, row_group_size=None):
        groups.append(table.num_rows)
        if len(groups) > 1:
            raise OSError("disk full")
        write_group(self, table, row_group_size=row_group_size)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", fail_on_second_group)
    records = [_record(), _record(contributing_node_ids=["synth-a", "synth-b"])]
    with pytest.raises(OSError, match="disk full"):
        tw.write_tracks_parquet(records=records, base_dir=tmp_path)
    assert groups == [1, 1]
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def _row(rec: dict, tmp_path: Path) -> dict:
    key = tw.write_tracks_parquet(records=[rec], base_dir=tmp_path, write_ts=TS)
    (row,) = pq.read_table(tmp_path / key).to_pylist()
    return row


@pytest.mark.parametrize(
    ("track_key", "follow_lane", "lane"),
    [
        ("mn-adsb-abcd12", None, "known"),
        ("mn-adsb-abcd12", True, "known"),
        ("mn-dark-1700000000000-40.700--74.000", None, "dark"),
        ("mn-dark-1700000000000-40.700--74.000", False, "dark"),
        ("mn-dark-1700000000000-40.700--74.000", True, "follow"),
        ("mn-other", True, None),
        ("", True, None),
        (None, None, None),
        (None, True, None),
    ],
)
def test_track_key_and_lane(tmp_path: Path, track_key, follow_lane, lane):
    rec = _record()
    if track_key is not None:
        rec["track_key"] = track_key
    if follow_lane is not None:
        rec["follow_lane"] = follow_lane
    row = _row(rec, tmp_path)
    assert row["track_key"] == (track_key or None)
    assert row["lane"] == lane


@pytest.mark.parametrize(
    ("ids", "kind"),
    [
        (["node-A", "node-B"], "real"),
        (["synth-a", "e2e-b", "test-c", "realnode-d"], "synthetic"),
        (["node-A", "test-b"], "mixed"),
        ("synth-a,synth-b", "synthetic"),
        ([], None),
        (None, None),
    ],
)
def test_node_kind_column(tmp_path: Path, ids, kind):
    assert _row(_record(contributing_node_ids=ids), tmp_path)["node_kind"] == kind


def test_node_kind_is_of_the_stored_string(tmp_path: Path):
    """An id holding a comma is classified by the parts a reader splits the stored string into."""
    row = _row(_record(contributing_node_ids=["ret1a2b3c4d,synth-x", "ret2b3c4d5e"]), tmp_path)
    assert (row["contributing_node_ids"], row["node_kind"]) == ("ret1a2b3c4d,synth-x,ret2b3c4d5e", "mixed")


def test_pos_sigma_m_uses_the_lane_floors(tmp_path: Path):
    rec = _record(n_nodes=3, pos_sigma_km=0.05)
    known = solve_sigma_m(rec, dark=False)
    dark = solve_sigma_m(rec, dark=True)
    assert known != dark
    assert _row({**rec, "track_key": "mn-adsb-abcd12"}, tmp_path)["pos_sigma_m"] == known
    assert _row({**rec, "track_key": "mn-dark-1"}, tmp_path)["pos_sigma_m"] == dark
    # No key: the dark floors, the wider of the two.
    assert _row(rec, tmp_path)["pos_sigma_m"] == dark


def test_pos_sigma_m_null_without_n_nodes(tmp_path: Path):
    rec = _record(track_key="mn-adsb-abcd12")
    rec.pop("n_nodes")
    assert solve_sigma_m(rec, dark=False) is None
    assert _row(rec, tmp_path)["pos_sigma_m"] is None


def test_pos_sigma_m_null_when_not_finite(tmp_path: Path, monkeypatch):
    import services.solve_uncertainty as su

    monkeypatch.setattr(su, "solve_sigma_m", lambda r, *, dark: float("nan"))
    assert _row(_record(track_key="mn-dark-1"), tmp_path)["pos_sigma_m"] is None


@pytest.mark.parametrize(
    ("value", "stored"),
    [(None, None), (False, False), (True, True), (0, False), (1, True)],
)
def test_is_anomalous(tmp_path: Path, value, stored):
    assert _row(_record(is_anomalous=value), tmp_path)["is_anomalous"] is stored


def test_is_anomalous_null_when_absent(tmp_path: Path):
    assert _row(_record(), tmp_path)["is_anomalous"] is None


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (["speed", "altitude"], "speed,altitude"),
        (("altitude", "speed"), "altitude,speed"),
        (["speed"], "speed"),
        ([], ""),
        ("speed,altitude", "speed,altitude"),
        (None, None),
    ],
)
def test_anomaly_types(tmp_path: Path, value, stored):
    assert _row(_record(anomaly_types=value), tmp_path)["anomaly_types"] == stored


def test_anomaly_types_null_when_absent(tmp_path: Path):
    assert _row(_record(), tmp_path)["anomaly_types"] is None


def test_solve_count(tmp_path: Path):
    assert _row(_record(solve_count=7), tmp_path)["solve_count"] == 7
    assert _row(_record(), tmp_path)["solve_count"] is None


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (["nde0123456789ab", "synth-a", "nde23456789abcd"], "nde0123456789ab,synth-a,nde23456789abcd"),
        (["nde0123456789ab", "", "nde23456789abcd"], "nde0123456789ab,,nde23456789abcd"),
        (["nde0123456789ab", None, "nde23456789abcd"], "nde0123456789ab,,nde23456789abcd"),
        (("nde0123456789ab", "", ""), "nde0123456789ab,,"),
        ([], ""),
        ("nde0123456789ab,synth-a", "nde0123456789ab,synth-a"),
        (None, None),
    ],
)
def test_contributing_node_refs(tmp_path: Path, value, stored):
    """The stamped refs comma-joined in contributor order; an empty slot for a contributor with none."""
    assert _row(_record(contributing_node_refs=value), tmp_path)["contributing_node_refs"] == stored


def test_contributing_node_refs_null_when_absent(tmp_path: Path):
    assert _row(_record(), tmp_path)["contributing_node_refs"] is None


def test_importing_the_writer_stays_light():
    """Tools write the archive outside the app, so the writer loads neither the
    app state nor the TCP handler; the refs are stamped before it sees them."""
    heavy = ("core.state", "services.tcp_handler", "services.node_refs")
    code = f"import sys, services.track_writer; print([m for m in {heavy!r} if m in sys.modules])"
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"


def _node_kind_groups(path: Path) -> list[tuple[str | None, int, int]]:
    """(kind, rows, null_count) per row group, from the footer alone."""
    meta = pq.ParquetFile(path).metadata
    col = meta.schema.names.index("node_kind")
    groups = []
    for i in range(meta.num_row_groups):
        rg = meta.row_group(i)
        stats = rg.column(col).statistics
        if stats.has_min_max:
            assert stats.min == stats.max
            kind = stats.min
        else:
            kind = None
        groups.append((kind, rg.num_rows, stats.null_count))
    return groups


def test_one_row_group_per_node_kind_in_fixed_order(tmp_path: Path):
    kinds = {
        "real": ["node-A", "node-B"],
        "synthetic": ["synth-a", "synth-b"],
        "mixed": ["node-A", "synth-b"],
        None: [],
    }
    order = ["synthetic", None, "real", "mixed", "synthetic", "real", None, "real"]
    records = [_record(ts_ms=1000 + i, contributing_node_ids=kinds[k]) for i, k in enumerate(order)]
    key = tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)
    path = tmp_path / key

    assert _node_kind_groups(path) == [("real", 3, 0), ("synthetic", 2, 0), ("mixed", 1, 0), (None, 2, 2)]
    # Rows keep their batch order within a group.
    rows = pq.read_table(path).to_pylist()
    assert [(r["node_kind"], r["frame_ts_ms"]) for r in rows] == [
        ("real", 1002),
        ("real", 1005),
        ("real", 1007),
        ("synthetic", 1000),
        ("synthetic", 1004),
        ("mixed", 1003),
        (None, 1001),
        (None, 1006),
    ]
    meta = pq.ParquetFile(path).metadata
    assert {
        meta.row_group(i).column(j).compression for i in range(meta.num_row_groups) for j in range(meta.num_columns)
    } == {"ZSTD"}


def test_only_the_kinds_present_get_a_group(tmp_path: Path):
    records = [_record(contributing_node_ids=[]), _record(contributing_node_ids=["synth-a"])]
    key = tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)
    assert _node_kind_groups(tmp_path / key) == [("synthetic", 1, 0), (None, 1, 1)]


def test_large_single_kind_batch_is_one_row_group(tmp_path: Path):
    records = [_record(ts_ms=i, contributing_node_ids=["synth-a", "synth-b"]) for i in range(12_000)]
    key = tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)
    assert _node_kind_groups(tmp_path / key) == [("synthetic", 12_000, 0)]


def test_old_files_read_with_the_new_columns_null(tmp_path: Path):
    old = tmp_path / "old.parquet"
    old_cols = tw._flatten([_record(ts_ms=1000)])
    pq.write_table(pa.table({f.name: old_cols[f.name] for f in OLD_SCHEMA}, schema=OLD_SCHEMA), old)
    assert pq.read_schema(old).names == OLD_SCHEMA.names
    new_rec = _record(
        ts_ms=2000,
        track_key="mn-adsb-abcd12",
        is_anomalous=False,
        anomaly_types=[],
        solve_count=2,
        contributing_node_refs=["nde0123456789ab", "", "nde23456789abcd"],
    )
    new = tmp_path / tw.write_tracks_parquet(records=[new_rec], base_dir=tmp_path, write_ts=TS)

    table = ds.dataset([str(old), str(new)], schema=tw.SCHEMA, format="parquet").to_table()
    rows = sorted(table.to_pylist(), key=lambda r: r["frame_ts_ms"])
    assert [r["frame_ts_ms"] for r in rows] == [1000, 2000]
    old_row, new_row = rows
    assert old_row["lat"] == 40.7
    assert old_row["contributing_node_ids"] == "node-A,node-B,node-C"
    assert {name: old_row[name] for name, _ in NEW_COLUMNS} == dict.fromkeys(name for name, _ in NEW_COLUMNS)
    assert new_row["track_key"] == "mn-adsb-abcd12"
    assert new_row["lane"] == "known"
    assert new_row["node_kind"] == "real"
    assert new_row["is_anomalous"] is False
    assert new_row["anomaly_types"] == ""
    assert new_row["solve_count"] == 2
    assert new_row["contributing_node_refs"] == "nde0123456789ab,,nde23456789abcd"
    assert new_row["pos_sigma_m"] is not None


def _detections_table(n: int) -> pa.Table:
    return pa.table({"frame_ts_ms": list(range(n)), "node_id": ["node-A"] * n})


def test_default_batch_is_one_write_table_call(tmp_path: Path, monkeypatch):
    """The detections writer passes no row_group_sizes and must get the
    write it always had: one pq.write_table, zstd level 3, one row group,
    the same file name and no temp file left behind."""
    calls = []
    write_table = pq.write_table

    def spy(table, where, **kwargs):
        calls.append(kwargs)
        return write_table(table, where, **kwargs)

    monkeypatch.setattr(pq, "write_table", spy)
    directory = tmp_path / "year=2025/month=01/day=15/node_id=node-A"
    name = parquet_io.write_parquet_batch(_detections_table(12_000), directory, TS)

    assert calls == [{"compression": "zstd", "compression_level": 3}]
    assert re.fullmatch(r"part-143022-[0-9a-f]{32}\.parquet", name)
    assert [p.name for p in directory.iterdir()] == [name]
    meta = pq.ParquetFile(directory / name).metadata
    assert meta.num_rows == 12_000
    assert meta.num_row_groups == 1
    assert {meta.row_group(0).column(i).compression for i in range(meta.num_columns)} == {"ZSTD"}


@pytest.mark.parametrize("sizes", [[2, 2], [3, 0, 2], [6], []])
def test_row_group_sizes_must_partition_the_table(tmp_path: Path, sizes):
    directory = tmp_path / "out"
    with pytest.raises(ValueError, match="row_group_sizes"):
        parquet_io.write_parquet_batch(_detections_table(5), directory, TS, row_group_sizes=sizes)
    assert not directory.exists()


def test_flush_track_archive_buffer_drains_state(tmp_path: Path, monkeypatch):
    """flush_track_archive_buffer drains state.track_archive_buffer into a Parquet file."""
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    monkeypatch.setattr(ta, "_pending_records", [])
    state.track_archive_buffer.clear()
    state.track_archive_buffer.append(_record(ts_ms=1700000000000))
    state.track_archive_buffer.append(_record(ts_ms=1700000001000))

    key = ta.flush_track_archive_buffer()
    assert key is not None
    assert key.endswith(".parquet")
    assert len(state.track_archive_buffer) == 0

    table = pq.read_table(tmp_path / key)
    assert table.num_rows == 2


def test_flush_track_archive_buffer_no_op_when_empty(tmp_path: Path, monkeypatch):
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    monkeypatch.setattr(ta, "_pending_records", [])
    state.track_archive_buffer.clear()
    assert ta.flush_track_archive_buffer() is None
    assert not list(tmp_path.rglob("*.parquet"))


def test_solver_hook_appends_to_track_archive_buffer():
    """A successful solve should append a record to state.track_archive_buffer."""
    from core import state
    from services.tasks import solver as sv

    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()

    fake_result = {
        "success": True,
        "lat": 40.0,
        "lon": -74.0,
        "alt_m": 11000.0,
        "vel_east": 200.0,
        "vel_north": -10.0,
        "n_nodes": 3,
        "contributing_node_ids": ["a", "b", "c"],
        "timestamp_ms": 1700000000000,
        "rms_delay": 1.0,
        "rms_doppler": 2.0,
    }

    def fake_solve(s_in, node_cfgs):
        return fake_result

    import time as _t

    item = ({"n_nodes": 3, "candidates": []}, {}, _t.time())
    sv._process_solver_item(item, fake_solve)

    assert len(state.track_archive_buffer) == 1
    rec = state.track_archive_buffer[0]
    assert rec["lat"] == 40.0
    assert "solve_ts_ms" in rec
    (key,) = state.multinode_tracks
    assert key.startswith("mn-dark-")
    assert rec["track_key"] == key
    assert "follow_lane" not in rec
    # Stamped on the archive copy only; the live entry is unchanged.
    assert "track_key" not in state.multinode_tracks[key]
    assert "contributing_node_refs" not in state.multinode_tracks[key]


def test_solver_hook_stamps_the_refs_the_live_feed_publishes(monkeypatch):
    """Each contributor is archived under what the live feed publishes it as
    now: a registered node's ref, a mirrored node's ref from connected_nodes,
    a synthetic id as itself, and an empty slot for a node with no identity."""
    import time as _t

    from core import state
    from services import node_auth, node_refs
    from services.tasks import solver as sv
    from tests.test_tracks_archive_route import MIRRORED, NO_REF, PUB_A, SYN_A, _seed

    refs = _seed()
    mirrored_ref = node_auth.mint_node_ref()
    monkeypatch.setitem(state.connected_nodes, MIRRORED, {"node_ref": mirrored_ref})
    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()
    ids = [PUB_A, MIRRORED, SYN_A, NO_REF]

    def fake_solve(s_in, node_cfgs):
        return {
            "success": True,
            "lat": 40.0,
            "lon": -74.0,
            "alt_m": 11000.0,
            "vel_east": 200.0,
            "vel_north": -10.0,
            "n_nodes": len(ids),
            "contributing_node_ids": list(ids),
            "timestamp_ms": 1700000000000,
            "rms_delay": 1.0,
            "rms_doppler": 2.0,
        }

    sv._process_solver_item(({"n_nodes": len(ids), "candidates": []}, {}, _t.time()), fake_solve)

    (rec,) = state.track_archive_buffer
    (key,) = state.multinode_tracks
    assert rec["contributing_node_refs"] == [refs[PUB_A], mirrored_ref, SYN_A, ""]
    # The live feed publishes the same refs, in the same order, and drops the
    # contributor with none.
    live = node_refs.substitute_identities({"aircraft": [{"contributing_node_ids": ids}]})
    assert live["aircraft"][0]["contributing_node_refs"] == [refs[PUB_A], mirrored_ref, SYN_A]
    assert "contributing_node_refs" not in state.multinode_tracks[key]
    assert state.multinode_tracks[key]["contributing_node_ids"] == ids
    assert tw._flatten([rec])["contributing_node_refs"] == [f"{refs[PUB_A]},{mirrored_ref},{SYN_A},"]


@pytest.mark.parametrize(
    "follow",
    [
        {"follow_key": "mn-dark-followed", "lane": "dark_follow"},
        {"follow_key": "mn-dark-followed"},
        {"lane": "dark_follow"},
    ],
)
def test_solver_hook_marks_follow_lane_solves(follow):
    """A solve from the dark-follow lane's input is archived with follow_lane."""
    import time as _t

    from core import state
    from services.tasks import solver as sv

    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()

    def fake_solve(s_in, node_cfgs):
        return {
            "success": True,
            "lat": 40.0,
            "lon": -74.0,
            "alt_m": 11000.0,
            "vel_east": 200.0,
            "vel_north": -10.0,
            "n_nodes": 3,
            "contributing_node_ids": ["a", "b", "c"],
            "timestamp_ms": 1700000000000,
            "rms_delay": 1.0,
            "rms_doppler": 2.0,
        }

    sv._process_solver_item(({"n_nodes": 3, "candidates": [], **follow}, {}, _t.time()), fake_solve)

    (rec,) = state.track_archive_buffer
    (key,) = state.multinode_tracks
    assert rec["track_key"] == key
    assert rec["follow_lane"] is True
    assert "follow_lane" not in state.multinode_tracks[key]
