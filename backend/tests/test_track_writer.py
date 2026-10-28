"""Tests for the multi-node solver track Parquet writer."""

import logging
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest

from services import parquet_io
from services import track_writer as tw
from services.solve_uncertainty import solve_sigma_m
from tests.test_tracks_archive_route import OLD_SCHEMA

# The columns appended after OLD_SCHEMA, the schema of the files written
# before track_key, which are still in the archive.
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


def _row(rec: dict, tmp_path: Path) -> dict:
    key = tw.write_tracks_parquet(records=[rec], base_dir=tmp_path, write_ts=TS)
    (row,) = pq.read_table(tmp_path / key).to_pylist()
    return row


_ABSENT = object()


@pytest.fixture
def skip_warnings(caplog):
    """The writer's warnings so far, as messages."""
    caplog.set_level(logging.WARNING, logger=tw.__name__)
    return lambda: [r.getMessage() for r in caplog.records if r.name == tw.__name__ and r.levelno == logging.WARNING]


def _invalid(field: str, value, label: str):
    return pytest.param(field, value, id=f"{field}={label}")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        _invalid("lat", _ABSENT, "absent"),
        _invalid("lat", None, "None"),
        _invalid("lat", float("nan"), "nan"),
        _invalid("lat", float("inf"), "inf"),
        _invalid("lat", 91.0, "91"),
        _invalid("lat", -90.5, "-90.5"),
        _invalid("lat", "north", "text"),
        _invalid("lat", 10**400, "too-big-for-a-float"),
        _invalid("lon", _ABSENT, "absent"),
        _invalid("lon", None, "None"),
        _invalid("lon", 181.0, "181"),
        _invalid("lon", float("-inf"), "-inf"),
        _invalid("timestamp_ms", _ABSENT, "absent"),
        _invalid("timestamp_ms", None, "None"),
        _invalid("timestamp_ms", 0, "0"),
        _invalid("timestamp_ms", -1, "-1"),
        _invalid("timestamp_ms", float("inf"), "inf"),
        _invalid("timestamp_ms", float("nan"), "nan"),
        _invalid("timestamp_ms", 2**63, "past-int64"),
        _invalid("solve_ts_ms", _ABSENT, "absent"),
        _invalid("solve_ts_ms", 0, "0"),
        _invalid("n_nodes", 0, "0"),
        _invalid("n_nodes", None, "None"),
        _invalid("n_nodes", _ABSENT, "absent"),
        _invalid("n_nodes", 2**31, "past-int32"),
    ],
)
def test_a_record_without_a_real_position_time_or_node_count_is_skipped(tmp_path: Path, skip_warnings, field, value):
    """No stand-in value reaches a row: the record is skipped, its neighbours are kept."""
    bad = _record(ts_ms=2000)
    if value is _ABSENT:
        del bad[field]
    else:
        bad[field] = value
    records = [_record(ts_ms=1000), bad, _record(ts_ms=3000)]
    key = tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)

    rows = pq.read_table(tmp_path / key).to_pylist()
    assert [(r["frame_ts_ms"], r["solve_ts_ms"], r["lat"], r["lon"], r["n_nodes"]) for r in rows] == [
        (1000, 1050, 40.7, -74.0, 3),
        (3000, 3050, 40.7, -74.0, 3),
    ]
    assert skip_warnings() == [
        f"track archive: skipped 1 of 3 records without a valid position, time or node count ({field}=1)"
    ]


@pytest.mark.parametrize(("lat", "lon"), [(0.0, 0.0), (-0.0, 0.0), (90.0, 180.0), (-90.0, -180.0), (0, 0)])
def test_a_position_on_the_bounds_or_at_zero_is_kept(tmp_path: Path, skip_warnings, lat, lon):
    row = _row(_record(lat=lat, lon=lon), tmp_path)
    assert (row["lat"], row["lon"]) == (lat, lon)
    assert skip_warnings() == []


def test_one_warning_per_call_names_the_count_and_the_fields(tmp_path: Path, skip_warnings):
    first = [
        _record(ts_ms=1000),
        _record(ts_ms=2000, lat=None),
        _record(ts_ms=0, lat=float("nan")),
        _record(ts_ms=4000, n_nodes=0),
        _record(ts_ms=5000),
    ]
    key = tw.write_tracks_parquet(records=first, base_dir=tmp_path, write_ts=TS)
    assert pq.read_table(tmp_path / key).column("frame_ts_ms").to_pylist() == [1000, 5000]
    tw.write_tracks_parquet(records=[_record(ts_ms=6000)], base_dir=tmp_path, write_ts=TS)
    tw.write_tracks_parquet(records=[_record(ts_ms=7000, solve_ts_ms=None, lon=200.0)], base_dir=tmp_path, write_ts=TS)

    assert skip_warnings() == [
        "track archive: skipped 3 of 5 records without a valid position, time or node count "
        "(lat=2, timestamp_ms=1, n_nodes=1)",
        "track archive: skipped 1 of 1 records without a valid position, time or node count (lon=1, solve_ts_ms=1)",
    ]


def test_flatten_fills_in_nothing():
    """What the record lacks is null, never 0 or Null Island, wherever _flatten is used."""
    cols = tw._flatten([{"contributing_node_ids": ["node-A"]}])
    assert {name: cols[name] for name in ("solve_ts_ms", "frame_ts_ms", "lat", "lon", "n_nodes")} == {
        "solve_ts_ms": [None],
        "frame_ts_ms": [None],
        "lat": [None],
        "lon": [None],
        "n_nodes": [None],
    }


def test_a_batch_with_no_valid_record_writes_no_file(tmp_path: Path, skip_warnings):
    records = [_record(lat=None), _record(timestamp_ms=float("inf")), _record(n_nodes=0)]
    assert tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS) is None
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    assert skip_warnings() == [
        "track archive: skipped 3 of 3 records without a valid position, time or node count "
        "(lat=1, timestamp_ms=1, n_nodes=1)"
    ]


def test_flush_skips_an_unreadable_record_instead_of_retrying_the_batch(tmp_path: Path, monkeypatch):
    """A record that cannot be read is dropped, not a batch the flush keeps failing on."""
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    monkeypatch.setattr(ta, "_pending_records", [])
    state.track_archive_buffer.clear()
    state.track_archive_buffer.extend(
        [_record(ts_ms=1000), _record(ts_ms=2000, timestamp_ms=float("inf")), _record(ts_ms=3000)]
    )
    key = ta.flush_track_archive_buffer()
    assert pq.read_table(tmp_path / key).column("frame_ts_ms").to_pylist() == [1000, 3000]
    assert ta._pending_records == []

    # A batch with nothing to archive counts as written.
    state.track_archive_buffer.extend([_record(lat=float("nan")), _record(solve_ts_ms=float("inf"))])
    assert ta.flush_track_archive_buffer() is None
    assert ta._pending_records == []
    assert len(state.track_archive_buffer) == 0
    assert ta.flush_track_archive_buffer() is None
    assert [path.name for path in tmp_path.rglob("*.parquet")] == [Path(key).name]


@pytest.mark.parametrize(
    ("field", "column", "value"),
    [
        ("solve_count", "solve_count", float("inf")),
        ("solve_count", "solve_count", 2**31),
        ("alt_m", "alt_m", 10**400),
        ("vel_east", "vel_east_ms", 10**400),
    ],
    ids=["solve_count=inf", "solve_count=past-int32", "alt_m=too-big-for-a-float", "vel_east=too-big-for-a-float"],
)
def test_an_optional_value_a_column_cannot_hold_is_null(tmp_path: Path, skip_warnings, field, column, value):
    row = _row(_record(**{field: value}), tmp_path)
    assert row[column] is None
    assert (row["frame_ts_ms"], row["lat"]) == (1700000000000, 40.7)
    assert skip_warnings() == []


def _full_record(ts_ms: int = 1700000000000, **overrides) -> dict:
    """A record with every field the writer reads."""
    full = {
        "track_key": "mn-dark-1700000000000-40.700--74.000",
        "follow_lane": True,
        "pos_sigma_km": 0.05,
        "is_anomalous": True,
        "anomaly_types": ["speed", "altitude"],
        "node_kind": "mixed",
        "solve_count": 4,
        "contributing_node_refs": ["nde0123456789ab", "", "nde23456789abcd"],
    }
    return _record(ts_ms, **{**full, **overrides})


def _rows(records: list[dict], directory: Path) -> list[dict]:
    """The rows write_tracks_parquet writes for ``records``, in frame_ts_ms order."""
    key = tw.write_tracks_parquet(records=records, base_dir=directory, write_ts=TS)
    return sorted(pq.read_table(directory / key).to_pylist(), key=lambda r: r["frame_ts_ms"])


# Values no string column takes: not a str, or a str Parquet cannot hold.
_NOT_TEXT = [
    pytest.param(7, id="int"),
    pytest.param(7.5, id="float"),
    pytest.param({"speed": "altitude"}, id="dict"),
    pytest.param(["speed", 7], id="list"),
    pytest.param(b"speed", id="bytes"),
    pytest.param(object(), id="object"),
    pytest.param("speed\ud800", id="lone-surrogate"),
]


@pytest.mark.parametrize(
    "field", ["adsb_hex", "target_class", "track_key", "anomaly_types", "node_kind", "contributing_node_refs"]
)
@pytest.mark.parametrize("value", _NOT_TEXT)
def test_a_text_value_its_column_cannot_hold_reads_as_absent(tmp_path: Path, skip_warnings, field, value):
    """The record is written as if it lacked the field, and its neighbours as they are:
    the column is null (lane with track_key), or for node_kind the id prefixes decide."""
    poisoned = _full_record(ts_ms=2000, **{field: value})
    absent = _full_record(ts_ms=2000)
    del absent[field]
    rows = _rows([_full_record(ts_ms=1000), poisoned, _full_record(ts_ms=3000)], tmp_path / "got")

    assert rows == _rows([_full_record(ts_ms=1000), absent, _full_record(ts_ms=3000)], tmp_path / "want")
    assert rows[1][field] == ("real" if field == "node_kind" else None)
    assert rows[0][field] not in (None, "real")
    if field == "track_key":
        assert [r["lane"] for r in rows] == ["follow", None, "follow"]
    assert skip_warnings() == []


@pytest.mark.parametrize(("field", "column"), [("follow_lane", "lane"), ("is_anomalous", "is_anomalous")])
def test_a_flag_without_a_truth_value_is_null(tmp_path: Path, skip_warnings, field, column):
    """A numpy array of several items has no truth value: the column is null, not an error."""
    poisoned = _full_record(ts_ms=2000, **{field: np.array([True, False])})
    rows = _rows([_full_record(ts_ms=1000), poisoned], tmp_path)
    assert [r[column] for r in rows] == [{"lane": "follow", "is_anomalous": True}[column], None]
    assert skip_warnings() == []


@pytest.mark.parametrize(
    "ids",
    [
        pytest.param(7, id="int"),
        pytest.param(0, id="zero"),
        pytest.param(7.5, id="float"),
        pytest.param({"node-A": "node-B"}, id="dict"),
        pytest.param({}, id="empty-dict"),
        pytest.param({"node-A", "node-B"}, id="set"),
        pytest.param(b"node-A", id="bytes"),
        pytest.param(object(), id="object"),
        pytest.param(["node-A", 7], id="list-holding-an-int"),
        pytest.param(["node-A", None], id="list-holding-None"),
        pytest.param([b"node-A"], id="list-holding-bytes"),
        pytest.param([["node-A"]], id="list-holding-a-list"),
        pytest.param(("node-A", 7.5), id="tuple-holding-a-float"),
        pytest.param("node-A,node-\ud800", id="lone-surrogate"),
        pytest.param(["node-A", "node-\ud800"], id="list-holding-a-lone-surrogate"),
        pytest.param(np.array(["node-A", "node-B"]), id="numpy-array"),
    ],
)
def test_a_record_whose_contributor_ids_cannot_be_read_is_skipped(tmp_path: Path, skip_warnings, ids):
    """The reader drops a row with a private contributor by its ids, so a row is never
    written with null or partial ones: the record is skipped, its neighbours kept."""
    poisoned = _full_record(ts_ms=2000, contributing_node_ids=ids)
    rows = _rows([_full_record(ts_ms=1000), poisoned, _full_record(ts_ms=3000)], tmp_path / "got")

    assert rows == _rows([_full_record(ts_ms=1000), _full_record(ts_ms=3000)], tmp_path / "want")
    assert skip_warnings() == [
        "track archive: skipped 1 of 3 records without a valid position, time or node count (contributing_node_ids=1)"
    ]


@pytest.mark.parametrize(
    ("ids", "stored", "kind"),
    [
        (("node-A", "synth-b"), "node-A,synth-b", "mixed"),
        (("node-A",), "node-A", "real"),
        ((), "", None),
        (["node-A", ""], "node-A,", "real"),
        ("node-A,synth-b", "node-A,synth-b", "mixed"),
        ("", "", None),
        (_ABSENT, "", None),
    ],
)
def test_contributor_ids_that_can_be_read_are_archived(tmp_path: Path, skip_warnings, ids, stored, kind):
    """A list or tuple of str, a str, or none at all: a tuple is joined like a list."""
    rec = _record()
    if ids is _ABSENT:
        del rec["contributing_node_ids"]
    else:
        rec["contributing_node_ids"] = ids
    row = _row(rec, tmp_path)
    assert (row["contributing_node_ids"], row["node_kind"]) == (stored, kind)
    assert skip_warnings() == []


def test_unreadable_contributor_ids_are_counted_after_the_required_fields(tmp_path: Path, skip_warnings):
    records = [
        _record(ts_ms=1000, contributing_node_ids=7),
        _record(ts_ms=2000, lat=None, contributing_node_ids=[None]),
        _record(ts_ms=3000),
        _record(ts_ms=4000, n_nodes=0),
    ]
    key = tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)
    assert pq.read_table(tmp_path / key).column("frame_ts_ms").to_pylist() == [3000]
    assert skip_warnings() == [
        "track archive: skipped 3 of 4 records without a valid position, time or node count "
        "(lat=1, n_nodes=1, contributing_node_ids=2)"
    ]


@pytest.mark.parametrize(
    ("field", "value", "written"),
    [
        pytest.param("adsb_hex", 7, [1000, 2000, 3000], id="adsb_hex=int"),
        pytest.param("track_key", 7, [1000, 2000, 3000], id="track_key=int"),
        pytest.param("anomaly_types", "speed\ud800", [1000, 2000, 3000], id="anomaly_types=lone-surrogate"),
        pytest.param(
            "contributing_node_refs",
            ["nde0123456789ab", "\ud800", ""],
            [1000, 2000, 3000],
            id="contributing_node_refs=lone-surrogate",
        ),
        pytest.param("is_anomalous", np.array([True, False]), [1000, 2000, 3000], id="is_anomalous=numpy-array"),
        pytest.param("pos_sigma_km", 10**400, [1000, 2000, 3000], id="pos_sigma_km=too-big-for-a-float"),
        pytest.param("contributing_node_ids", 7, [1000, 3000], id="contributing_node_ids=int"),
        pytest.param("contributing_node_ids", ["node-A", None], [1000, 3000], id="contributing_node_ids=list-of-None"),
    ],
)
def test_flush_writes_the_rest_of_a_batch_around_a_poisoned_record(tmp_path: Path, monkeypatch, field, value, written):
    """One record's value never stalls the archive: the flush writes the batch, the
    record nulled where it cannot be read or skipped, and holds nothing to retry."""
    from core import state
    from services.tasks import track_archive as ta

    monkeypatch.setattr(ta, "_TRACKS_DIR", str(tmp_path))
    monkeypatch.setattr(ta, "_pending_records", [])
    state.track_archive_buffer.clear()
    state.track_archive_buffer.extend(
        [_full_record(ts_ms=1000), _full_record(ts_ms=2000, **{field: value}), _full_record(ts_ms=3000)]
    )
    key = ta.flush_track_archive_buffer()
    assert sorted(pq.read_table(tmp_path / key).column("frame_ts_ms").to_pylist()) == written
    assert ta._pending_records == []
    assert len(state.track_archive_buffer) == 0
    assert ta.flush_track_archive_buffer() is None
    assert [path.name for path in tmp_path.rglob("*.parquet")] == [Path(key).name]


class _Reads(dict):
    """A record that notes the fields read from it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)


# Values a producer bug, a tool or a decoded payload could leave in any field.
_ANY_VALUE = [
    None,
    True,
    0,
    -1,
    7,
    2**70,
    10**400,
    7.5,
    float("nan"),
    float("-inf"),
    complex(1, 2),
    Decimal("sNaN"),
    "",
    "x",
    "x\ud800",
    b"x",
    [],
    ["x", 7],
    [None],
    ("x",),
    {"x"},
    {},
    {"x": 7},
    object(),
    np.array([1.0, 2.0]),
    np.float64("nan"),
]


def test_no_value_in_any_field_fails_the_batch(tmp_path: Path):
    """Whatever a field holds, alone or in every field at once, _flatten reads it as
    its column's type or null and the batch is written around the record."""
    probe = _Reads(_full_record())
    tw.write_tracks_parquet(records=[probe], base_dir=tmp_path / "probe", write_ts=TS)
    assert probe.read == set(_full_record()), "_full_record must set every field the writer reads"

    cases = [
        (f"{field}={value!r}", {**_full_record(ts_ms=2000), field: value})
        for field in sorted(probe.read)
        for value in _ANY_VALUE
    ]
    cases += [(f"every field={value!r}", dict.fromkeys(probe.read, value)) for value in _ANY_VALUE]
    failures = []
    for label, rec in cases:
        try:
            pa.table(tw._flatten([rec]), schema=tw.SCHEMA)
            key = tw.write_tracks_parquet(
                records=[_full_record(ts_ms=1000), rec, _full_record(ts_ms=3000)], base_dir=tmp_path, write_ts=TS
            )
        except Exception as e:
            failures.append(f"{label}: {type(e).__name__}: {e}")
            continue
        frames = pq.read_table(tmp_path / key, columns=["frame_ts_ms"]).column(0).to_pylist()
        if not {1000, 3000} <= set(frames):
            failures.append(f"{label}: wrote frames {frames}")
    assert failures == []


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


@pytest.mark.parametrize(
    ("ids", "stamped", "kind"),
    [
        (["synth-a", "synth-b"], "real", "real"),
        (["node-A", "node-B"], "synthetic", "synthetic"),
        (["node-A", "node-B"], "mixed", "mixed"),
        ([], "real", "real"),
        # Anything else is not a verdict: the id prefixes decide.
        (["synth-a", "synth-b"], None, "synthetic"),
        (["synth-a", "synth-b"], "bogus", "synthetic"),
        (["node-A", "synth-b"], "Real", "mixed"),
        (["node-A"], "", "real"),
        (["node-A"], 1, "real"),
        ([], "unknown", None),
    ],
)
def test_node_kind_is_the_stamped_verdict_else_the_id_prefixes(tmp_path: Path, ids, stamped, kind):
    assert _row(_record(contributing_node_ids=ids, node_kind=stamped), tmp_path)["node_kind"] == kind


def test_a_stamped_node_kind_picks_the_row_group(tmp_path: Path):
    records = [
        _record(ts_ms=1000, contributing_node_ids=["synth-a", "synth-b"], node_kind="real"),
        _record(ts_ms=2000, contributing_node_ids=["synth-a", "synth-b"]),
        _record(ts_ms=3000, contributing_node_ids=["node-A", "node-B"], node_kind="synthetic"),
        _record(ts_ms=4000, contributing_node_ids=["node-A", "node-B"], node_kind="bogus"),
    ]
    path = tmp_path / tw.write_tracks_parquet(records=records, base_dir=tmp_path, write_ts=TS)
    assert _node_kind_groups(path) == [("real", 2, 0), ("synthetic", 2, 0)]
    rows = pq.read_table(path).to_pylist()
    assert [(r["frame_ts_ms"], r["node_kind"]) for r in rows] == [
        (1000, "real"),
        (4000, "real"),
        (2000, "synthetic"),
        (3000, "synthetic"),
    ]


def test_pos_sigma_m_uses_the_lane_floors(tmp_path: Path):
    rec = _record(n_nodes=3, pos_sigma_km=0.05)
    known = solve_sigma_m(rec, dark=False)
    dark = solve_sigma_m(rec, dark=True)
    assert known != dark
    assert _row({**rec, "track_key": "mn-adsb-abcd12"}, tmp_path)["pos_sigma_m"] == known
    assert _row({**rec, "track_key": "mn-dark-1"}, tmp_path)["pos_sigma_m"] == dark
    # No key: the dark floors, the wider of the two.
    assert _row(rec, tmp_path)["pos_sigma_m"] == dark


def test_pos_sigma_m_null_without_n_nodes():
    # Such a record is not archived (n_nodes is required); _flatten itself
    # still writes no sigma for it.
    rec = _record(track_key="mn-adsb-abcd12")
    rec.pop("n_nodes")
    assert solve_sigma_m(rec, dark=False) is None
    assert tw._flatten([rec])["pos_sigma_m"] == [None]


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
    records = [_record(ts_ms=1 + i, contributing_node_ids=["synth-a", "synth-b"]) for i in range(12_000)]
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


def _fail_plain_write(monkeypatch, calls: list[int]) -> None:
    """pq.write_table leaves a partial file where it was told to write, then fails."""

    def partial_write(table, where, **kwargs):
        calls.append(table.num_rows)
        Path(where).write_bytes(b"PAR1 incomplete")
        raise OSError("disk full")

    monkeypatch.setattr(pq, "write_table", partial_write)


def _fail_second_row_group(monkeypatch, calls: list[int]) -> None:
    """The first row group reaches the file and the second fails, so the
    file is a real partial Parquet file when the error lands."""
    write_group = pq.ParquetWriter.write_table

    def fail_on_second_group(self, table, row_group_size=None):
        calls.append(table.num_rows)
        if len(calls) > 1:
            raise OSError("disk full")
        write_group(self, table, row_group_size=row_group_size)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", fail_on_second_group)


@pytest.mark.parametrize(
    ("row_group_sizes", "fail", "calls_made"),
    [
        pytest.param(None, _fail_plain_write, [2], id="plain"),
        pytest.param([1, 1], _fail_second_row_group, [1, 1], id="row-grouped"),
    ],
)
def test_failed_write_never_publishes_partial_parquet(tmp_path: Path, monkeypatch, row_group_sizes, fail, calls_made):
    """Both ways write_parquet_batch writes (the detections writer's plain
    one, the tracks writer's row groups): a write that fails part way
    publishes nothing and leaves no temp file."""
    calls: list[int] = []
    fail(monkeypatch, calls)
    with pytest.raises(OSError, match="disk full"):
        parquet_io.write_parquet_batch(_detections_table(2), tmp_path / "out", TS, row_group_sizes=row_group_sizes)
    assert calls == calls_made
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


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


def _solve_of(ids):
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

    return fake_solve


def _solve(ids):
    import time as _t

    from services.tasks import solver as sv

    sv._process_solver_item(({"n_nodes": len(ids), "candidates": []}, {}, _t.time()), _solve_of(ids))


def test_solver_hook_stamps_the_refs_the_live_feed_publishes(monkeypatch):
    """Once the maps are loaded, as the 1 Hz live flush keeps them, each
    contributor is archived under what the live feed publishes it as: a
    registered node's ref, a mirrored node's ref from connected_nodes, a
    synthetic id as itself, and an empty slot for a node with no identity."""
    from core import state
    from services import node_auth, node_refs
    from tests.test_tracks_archive_route import MIRRORED, NO_REF, PUB_A, SYN_A, _seed

    refs = _seed()
    mirrored_ref = node_auth.mint_node_ref()
    monkeypatch.setitem(state.connected_nodes, MIRRORED, {"node_ref": mirrored_ref})
    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()
    ids = [PUB_A, MIRRORED, SYN_A, NO_REF]
    assert node_refs.ref_for(PUB_A) == refs[PUB_A]

    _solve(ids)

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


def test_solver_hook_stamps_an_unloaded_registered_node_as_an_empty_slot(monkeypatch):
    """Before the maps hold a registered node, its slot is empty, to be resolved
    when the row is read; the mirrored and synthetic ones are stamped anyway,
    and the solve never loads the registry."""
    from core import state
    from services import node_auth, node_refs
    from tests.test_tracks_archive_route import MIRRORED, NO_REF, PUB_A, SYN_A, _seed

    _seed()
    mirrored_ref = node_auth.mint_node_ref()
    monkeypatch.setitem(state.connected_nodes, MIRRORED, {"node_ref": mirrored_ref})
    loads = []
    monkeypatch.setattr(node_refs, "_load", lambda: loads.append(1) or ({}, {}))
    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()

    _solve([PUB_A, MIRRORED, SYN_A, NO_REF])

    (rec,) = state.track_archive_buffer
    assert rec["contributing_node_refs"] == ["", mirrored_ref, SYN_A, ""]
    assert loads == []


@pytest.mark.parametrize(
    ("ids", "verdicts", "kind"),
    [
        (("node-a", "node-b", "node-c"), (True, True, True), "synthetic"),
        (("synth-a", "synth-b", "synth-c"), (False, False, False), "real"),
        (("node-a", "node-b", "node-c"), (True, False, True), "mixed"),
    ],
)
def test_solver_hook_stamps_the_handshake_fleet_on_the_archive_copy_only(monkeypatch, ids, verdicts, kind):
    """The fleet is each contributor's handshake verdict (state.node_world), as
    the live feed splits by, and not its id prefix: every case here is one the
    prefix would file differently."""
    from core import state

    for node, verdict in zip(ids, verdicts):
        monkeypatch.setitem(state.connected_nodes, node, {"is_synthetic": verdict})
    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()

    _solve(list(ids))

    (rec,) = state.track_archive_buffer
    (key,) = state.multinode_tracks
    assert rec["node_kind"] == kind
    assert "node_kind" not in state.multinode_tracks[key]


def test_solver_hook_does_not_wait_on_a_registry_refresh(monkeypatch):
    """The refs are stamped from node_refs' maps as they stand: a refresh
    holding the lock across a slow registry read must not hold up a solve."""
    from core import state
    from services import node_refs
    from tests.test_tracks_archive_route import PUB_A, PUB_B, SYN_A, _seed

    _seed()
    entered, release = threading.Event(), threading.Event()

    def _slow_load():
        entered.set()
        release.wait(timeout=30)
        return {}, {}

    monkeypatch.setattr(node_refs, "_load", _slow_load)
    monkeypatch.setattr(node_refs, "_expires_at", 0.0)
    state.track_archive_buffer.clear()
    state.multinode_tracks.clear()
    refresher = threading.Thread(target=node_refs.ref_for, args=(PUB_B,))
    solver = threading.Thread(target=_solve, args=([PUB_A, PUB_B, SYN_A],))
    refresher.start()
    try:
        assert entered.wait(timeout=10)
        solver.start()
        solver.join(timeout=5)
        assert not solver.is_alive(), "the solve waited on the registry refresh"
    finally:
        release.set()
        refresher.join(timeout=10)
        if solver.ident is not None:
            solver.join(timeout=10)

    (rec,) = state.track_archive_buffer
    assert rec["contributing_node_refs"] == ["", "", SYN_A]


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
