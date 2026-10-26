"""services/compaction.py: a day's raw tracks parts rewritten into one verified compact.

Parts are written into a per-test root through the archive's own batch
publisher (services/parquet_io.py): new-schema parts in track_writer's SCHEMA
with one row group per node_kind, as the writer lays them out, and legacy parts
in the 14 columns files had before the writer's newer columns. The ADS-B
store's parts are written by its own writer (services/adsb_writer.py).
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import groupby
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from config import constants
from services import adsb_writer, compaction
from services.compaction import ADSB, TRACKS, CompactionError, Group, Manifest
from services.parquet_io import write_parquet_batch
from services.track_writer import SCHEMA

DAY = date(2026, 10, 18)
LEGACY_SCHEMA = pa.schema(list(SCHEMA)[:14])
NAMES = SCHEMA.names
# 2026-10-19 00:30Z: the end of DAY plus the 30-minute settle margin.
READY_MS = 1_792_369_800_000
H10, H11 = 1_792_317_600_000, 1_792_321_200_000  # 2026-10-18 10:00Z and 11:00Z
IDS = {"real": "ret1a2b3c4d", "synthetic": "synth-alpha", "mixed": "ret1a2b3c4d,synth-alpha", None: ""}


def t(hour: int, minute: int = 0, second: int = 0, ms: int = 0) -> int:
    """Epoch ms of a moment on DAY."""
    return int(datetime(2026, 10, 18, hour, minute, second, ms * 1000, tzinfo=UTC).timestamp() * 1000)


def row(ts: int | None, kind: str | None = "real", lat: float = 0.0) -> dict:
    """A track row; lat marks it."""
    return {
        "solve_ts_ms": ts,
        "frame_ts_ms": None if ts is None else ts - 700,
        "lat": lat,
        "lon": -82.39,
        "n_nodes": 2,
        "contributing_node_ids": IDS.get(kind, "ret9f8e7d6c"),
        "node_kind": kind,
        "track_key": f"mn-dark-{lat}",
    }


def _kind_rank(r: dict) -> tuple:
    kind = r["node_kind"]
    known = ("real", "synthetic", "mixed")
    return (0, known.index(kind), "") if kind in known else (1, 0, kind) if kind is not None else (2, 0, "")


def new_part(directory: Path, hms: tuple[int, int, int], rows: list[dict], schema: pa.Schema = SCHEMA) -> str:
    """A new-schema part flushed at hms on DAY: one row group per node_kind, rows keeping their order within it."""
    ordered = sorted(rows, key=_kind_rank)
    table = pa.table({f.name: [r.get(f.name) for r in ordered] for f in schema}, schema=schema)
    sizes = [len(list(run)) for _, run in groupby(ordered, key=lambda r: r["node_kind"])]
    return write_parquet_batch(table, directory, datetime(2026, 10, 18, *hms, tzinfo=UTC), row_group_sizes=sizes)


def legacy_part(directory: Path, hms: tuple[int, int, int], rows: list[dict]) -> str:
    """A part as written before node_kind and the other newer columns existed: one row group."""
    table = pa.table({f.name: [r.get(f.name) for r in rows] for f in LEGACY_SCHEMA}, schema=LEGACY_SCHEMA)
    return write_parquet_batch(table, directory, datetime(2026, 10, 18, *hms, tzinfo=UTC))


def stored(r: dict, *, legacy: bool = False) -> tuple:
    """A row as the compact must hold it: a column its part lacked is null."""
    return tuple(r.get(name) if not legacy or name in LEGACY_SCHEMA.names else None for name in NAMES)


def compact_rows(directory: Path) -> Counter:
    with pq.ParquetFile(directory / "compact.parquet") as parquet:
        return Counter(tuple(d[name] for name in NAMES) for d in parquet.read().to_pylist())


def group_lats(directory: Path) -> list[list[float]]:
    with pq.ParquetFile(directory / "compact.parquet") as parquet:
        return [parquet.read_row_group(i).column("lat").to_pylist() for i in range(parquet.num_row_groups)]


def tmp_files(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name.startswith(".compact-"))


def mtimes(*paths: Path) -> list[int]:
    return [p.stat().st_mtime_ns for p in paths]


@pytest.fixture
def root(tmp_path) -> str:
    return str(tmp_path / "tracks")


@pytest.fixture
def day_dir(root) -> Path:
    path = Path(compaction.day_dir(root, DAY))
    path.mkdir(parents=True)
    return path


def compact(root: str, now_ms: int = READY_MS) -> str:
    return compaction.compact_day(TRACKS, root, DAY, now_ms=now_ms)


# ── What a compact holds ─────────────────────────────────────────────────────


def test_the_compact_holds_every_row_of_legacy_and_new_parts(root, day_dir):
    legacy_rows = [row(t(9, 0, 10), "real", 1.0), row(t(9, 0, 20), "synthetic", 2.0)]
    new_rows = [
        row(t(10, 0, 30), "real", 3.0),
        row(t(10, 0, 10), "synthetic", 4.0),
        row(t(10, 0, 20), "mixed", 5.0),
        row(t(10, 0, 5), None, 6.0),
        row(None, "real", 7.0),
    ]
    old = legacy_part(day_dir, (9, 0, 59), legacy_rows)
    new = new_part(day_dir, (10, 0, 59), new_rows)

    assert compact(root) == "compacted"

    expected = Counter([stored(r, legacy=True) for r in legacy_rows] + [stored(r) for r in new_rows])
    assert compact_rows(day_dir) == expected
    manifest = compaction.read_sidecar(str(day_dir))
    assert manifest is not None
    assert (manifest.parts, manifest.unreadable, manifest.rows) == (tuple(sorted([old, new])), (), 7)
    assert [(g.hour_ms, g.kind, g.legacy, g.rows) for g in manifest.groups] == [
        (H10 - 3_600_000, None, True, 2),
        (H10, "real", False, 1),
        (H10, "synthetic", False, 1),
        (H10, "mixed", False, 1),
        (H10, None, False, 1),
        (None, "real", False, 1),
    ]
    assert compaction.superseded(str(day_dir)) == {old, new}


def test_row_groups_are_hour_then_kind_then_legacy_sorted_by_time_with_valid_statistics(root, day_dir):
    # The legacy part and part a straddle 11:00; each row goes to its own hour.
    legacy_part(day_dir, (11, 0, 30), [row(t(11, 0, 0, 100), lat=11.0), row(t(10, 59, 59, 900), lat=10.0)])
    new_part(
        day_dir,
        (11, 0, 31),
        [
            row(t(11, 0, 5), "real", 20.0),
            row(t(10, 59, 30), "real", 21.0),
            row(t(11, 0, 1), "synthetic", 22.0),
            row(t(10, 59, 58), "synthetic", 23.0),
            row(t(11, 0, 2), None, 24.0),
            row(t(11, 0, 3), "zz-other", 25.0),
            row(t(11, 0, 4), "aa-other", 26.0),
            row(t(11, 0, 6), "mixed", 27.0),
            row(t(11, 0, 5), "real", 28.0),
        ],
    )
    new_part(day_dir, (11, 0, 40), [row(t(11, 0, 5), "real", 29.0), row(None, "synthetic", 30.0)])

    assert compact(root) == "compacted"

    manifest = compaction.read_footer_manifest(str(day_dir / "compact.parquet"))
    assert manifest is not None
    assert [(g.hour_ms, g.kind, g.legacy) for g in manifest.groups] == [
        (H10, "real", False),
        (H10, "synthetic", False),
        (H10, None, True),
        (H11, "real", False),
        (H11, "synthetic", False),
        (H11, "mixed", False),
        (H11, "aa-other", False),
        (H11, "zz-other", False),
        (H11, None, False),
        (H11, None, True),
        (None, "synthetic", False),
    ]
    # Sorted by time; the three rows solved at 11:00:05 keep their part's order, then the next part's.
    assert group_lats(day_dir) == [
        [21.0],
        [23.0],
        [10.0],
        [20.0, 28.0, 29.0],
        [22.0],
        [27.0],
        [26.0],
        [25.0],
        [24.0],
        [11.0],
        [30.0],
    ]
    with pq.ParquetFile(day_dir / "compact.parquet") as parquet:
        meta = parquet.metadata
    time_at, kind_at = NAMES.index("solve_ts_ms"), NAMES.index("node_kind")
    for i, group in enumerate(manifest.groups):
        times = meta.row_group(i).column(time_at).statistics
        kinds = meta.row_group(i).column(kind_at).statistics
        assert meta.row_group(i).num_rows == group.rows
        if group.hour_ms is None:
            assert (times.null_count, times.has_min_max) == (group.rows, False)
        else:
            assert (times.min, times.max, times.null_count) == (group.ts_min, group.ts_max, 0)
            assert group.hour_ms <= times.min <= times.max < group.hour_ms + 3_600_000
        if group.kind is None:
            assert kinds.null_count == group.rows
        else:
            assert (kinds.min, kinds.max, kinds.null_count) == (group.kind, group.kind, 0)
    assert manifest.groups[3] == Group(H11, "real", False, 3, t(11, 0, 5), t(11, 0, 5))


# ── Atomicity, verification and crashes ──────────────────────────────────────


def test_a_failed_write_leaves_the_installed_compact_and_sidecar_byte_identical(root, day_dir, monkeypatch):
    first = new_part(day_dir, (10, 0, 59), [row(t(10), "real", 1.0), row(t(10, 0, 1), "synthetic", 2.0)])
    assert compact(root) == "compacted"
    before = [(day_dir / name).read_bytes() for name in ("compact.parquet", "compact.json")]
    new_part(day_dir, (11, 0, 59), [row(t(11), "real", 3.0), row(t(11, 0, 1), "synthetic", 4.0)])

    written = []
    real = compaction._write_group

    def fail_on_the_second_group(writer, table):
        written.append(table.num_rows)
        if len(written) == 2:
            raise OSError("No space left on device")
        real(writer, table)

    monkeypatch.setattr(compaction, "_write_group", fail_on_the_second_group)
    assert compact(root) == "error"

    assert len(written) == 2
    assert [(day_dir / name).read_bytes() for name in ("compact.parquet", "compact.json")] == before
    assert tmp_files(day_dir) == []
    assert compaction.superseded(str(day_dir)) == {first}


def test_a_short_write_fails_verification_and_nothing_is_marked(root, day_dir, tmp_path, monkeypatch):
    new_part(
        day_dir,
        (10, 0, 59),
        [row(t(10), "real", 1.0), row(t(10, 0, 1), "real", 2.0), row(t(10, 0, 2), "synthetic", 3.0)],
    )
    real = compaction._write_group
    groups = []

    def drop_a_row_of_the_first_group(writer, table):
        groups.append(table.num_rows)
        real(writer, table.slice(1) if len(groups) == 1 else table)

    monkeypatch.setattr(compaction, "_write_group", drop_a_row_of_the_first_group)
    sources = [str(day_dir / name) for name in compaction.raw_part_names(str(day_dir))]
    with pytest.raises(CompactionError, match="failed verification"):
        compaction.build(TRACKS, DAY, sources, str(tmp_path / "out.tmp"))

    groups.clear()
    assert compact(root) == "error"
    assert not (day_dir / "compact.parquet").exists()
    assert not (day_dir / "compact.json").exists()
    assert tmp_files(day_dir) == []
    assert compaction.superseded(str(day_dir)) == frozenset()


def test_a_crash_between_the_compact_and_its_sidecar_is_repaired_from_the_footer(root, day_dir, monkeypatch):
    a = new_part(day_dir, (10, 0, 59), [row(t(10), "real", 1.0)])
    b = new_part(day_dir, (10, 1, 59), [row(t(10, 1), "synthetic", 2.0)])
    assert compact(root) == "compacted"
    late = new_part(day_dir, (10, 2, 59), [row(t(10, 2), "real", 3.0)])

    def crash(directory, manifest):
        raise OSError("power lost")

    with monkeypatch.context() as patched:
        patched.setattr(compaction, "write_sidecar", crash)
        assert compact(root) == "error"

    footer = compaction.read_footer_manifest(str(day_dir / "compact.parquet"))
    assert footer is not None
    assert footer.parts == tuple(sorted([a, b, late]))
    assert compaction.superseded(str(day_dir)) == {a, b}
    assert tmp_files(day_dir) == []

    assert compact(root) == "repaired"
    data = (day_dir / "compact.parquet").read_bytes()
    assert compaction.read_sidecar(str(day_dir)) == replace(
        footer, size=len(data), sha256=hashlib.sha256(data).hexdigest()
    )
    assert compaction.superseded(str(day_dir)) == {a, b, late}
    assert compact(root) == "unchanged"


def test_a_compaction_tmp_file_older_than_an_hour_is_removed_and_a_younger_one_kept(root, day_dir):
    new_part(day_dir, (10, 0, 59), [row(t(10))])
    stale = day_dir / f".compact-{uuid.uuid4().hex}.tmp"
    fresh = day_dir / f".compact-{uuid.uuid4().hex}.tmp"
    writers = day_dir / ".parquet-abc.tmp"
    for path in (stale, fresh, writers):
        path.write_bytes(b"partial")
    hours_ago = time.time() - 2 * 3600
    os.utime(stale, (hours_ago, hours_ago))
    os.utime(writers, (hours_ago, hours_ago))

    assert compact(root, now_ms=t(12)) == "skipped"

    assert not stale.exists()
    assert fresh.exists()
    assert writers.exists()


# ── Late, unreadable and foreign parts ───────────────────────────────────────


def test_a_late_part_is_compacted_in_and_old_legacy_rows_keep_their_flag(root, day_dir):
    legacy_rows = [row(t(10, 0, 10), lat=1.0), row(t(10, 0, 20), lat=2.0)]
    first_rows = [row(t(10, 0, 30), "real", 3.0)]
    late_rows = [row(t(10, 0, 15), "real", 4.0), row(t(10, 0, 25), None, 5.0)]
    old = legacy_part(day_dir, (10, 0, 59), legacy_rows)
    first = new_part(day_dir, (10, 1, 59), first_rows)
    assert compact(root) == "compacted"

    late = new_part(day_dir, (23, 59, 59), late_rows)
    assert compact(root) == "compacted"

    expected = [stored(r, legacy=True) for r in legacy_rows] + [stored(r) for r in first_rows + late_rows]
    assert compact_rows(day_dir) == Counter(expected)
    manifest = compaction.read_sidecar(str(day_dir))
    assert manifest is not None
    assert (manifest.parts, manifest.rows) == (tuple(sorted([old, first, late])), 5)
    assert [(g.kind, g.legacy, g.rows) for g in manifest.groups] == [
        ("real", False, 2),
        (None, False, 1),
        (None, True, 2),
    ]
    assert group_lats(day_dir) == [[4.0, 3.0], [5.0], [1.0, 2.0]]


def test_an_unreadable_part_stays_raw_and_alone_does_not_rewrite_the_compact(root, day_dir, caplog):
    good = new_part(day_dir, (10, 0, 59), [row(t(10), "real", 1.0)])
    broken = f"part-100100-{uuid.uuid4().hex}.parquet"
    (day_dir / broken).write_bytes(b"not a parquet file")

    with caplog.at_level(logging.WARNING, logger="services.compaction"):
        assert compact(root) == "compacted"
    assert [r for r in caplog.records if broken in r.getMessage()] != []
    assert len(caplog.records) == 1
    manifest = compaction.read_sidecar(str(day_dir))
    assert manifest is not None
    assert (manifest.parts, manifest.unreadable, manifest.rows) == ((good,), (broken,), 1)
    assert compaction.superseded(str(day_dir)) == {good}
    files = (day_dir / "compact.parquet", day_dir / "compact.json")
    before = mtimes(*files)

    assert compaction.sweep(TRACKS, root, now_ms=READY_MS)["unchanged"] == 1
    assert mtimes(*files) == before

    late = new_part(day_dir, (10, 2, 59), [row(t(10, 2), "real", 2.0)])
    assert compact(root) == "compacted"
    manifest = compaction.read_sidecar(str(day_dir))
    assert manifest is not None
    assert (manifest.parts, manifest.unreadable, manifest.rows) == (tuple(sorted([good, late])), (broken,), 2)
    assert (day_dir / broken).exists()


def test_a_part_with_a_column_the_schema_lacks_is_unreadable_and_nothing_is_dropped(root, day_dir):
    good_rows = [row(t(10), "real", 1.0)]
    good = new_part(day_dir, (10, 0, 59), good_rows)
    wider = pa.schema([*SCHEMA, ("surprise", pa.string())])
    foreign = new_part(day_dir, (10, 1, 59), [{**row(t(10, 1), "real", 2.0), "surprise": "x"}], schema=wider)

    assert compact(root) == "compacted"

    manifest = compaction.read_sidecar(str(day_dir))
    assert manifest is not None
    assert (manifest.parts, manifest.unreadable) == ((good,), (foreign,))
    assert compact_rows(day_dir) == Counter([stored(r) for r in good_rows])
    assert compaction.superseded(str(day_dir)) == {good}
    assert pq.ParquetFile(day_dir / foreign).metadata.num_rows == 1


# ── Sweeps and when a day is compacted ───────────────────────────────────────


def test_a_sweep_compacts_newest_first_within_its_cap_and_a_second_one_rewrites_nothing(root, day_dir):
    new_part(day_dir, (10, 0, 59), [row(t(10))])
    older = Path(compaction.day_dir(root, date(2026, 10, 17)))
    older.mkdir(parents=True)
    write_parquet_batch(
        pa.table({f.name: [row(t(10) - 86_400_000).get(f.name)] for f in SCHEMA}, schema=SCHEMA),
        older,
        datetime(2026, 10, 17, 10, 0, 59, tzinfo=UTC),
    )

    first = compaction.sweep(TRACKS, root, now_ms=READY_MS, max_days=1)
    assert first == {"compacted": 1, "repaired": 0, "skipped": 1, "unchanged": 0, "errors": 0}
    assert (day_dir / "compact.parquet").exists() and not (older / "compact.parquet").exists()
    assert compaction.sweep(TRACKS, root, now_ms=READY_MS)["compacted"] == 1
    files = [d / name for d in (day_dir, older) for name in ("compact.parquet", "compact.json")]
    before = mtimes(*files)

    again = compaction.sweep(TRACKS, root, now_ms=READY_MS)

    assert again == {"compacted": 0, "repaired": 0, "skipped": 0, "unchanged": 2, "errors": 0}
    assert mtimes(*files) == before


def test_only_a_day_past_its_end_and_settle_margin_is_compacted(root, day_dir):
    new_part(day_dir, (10, 0, 59), [row(t(10))])

    assert compact(root, now_ms=t(12)) == "skipped"
    assert compact(root, now_ms=READY_MS - 1) == "skipped"
    assert sorted(os.listdir(day_dir)) == compaction.raw_part_names(str(day_dir))
    assert compact(root, now_ms=READY_MS) == "compacted"
    assert not compaction.is_complete(TRACKS, date(2026, 10, 18), READY_MS - 1)
    assert compaction.is_complete(TRACKS, date(2026, 10, 18), READY_MS)


def test_a_day_whose_compact_is_only_in_object_storage_leaves_late_parts_raw(root, day_dir, caplog):
    part = new_part(day_dir, (10, 0, 59), [row(t(10))])
    assert compact(root) == "compacted"
    # The lifecycle deleted the uploaded compact and the superseded part.
    (day_dir / "compact.parquet").unlink()
    (day_dir / part).unlink()
    sidecar = (day_dir / "compact.json").read_bytes()
    late = new_part(day_dir, (10, 2, 59), [row(t(10, 2))])

    with caplog.at_level(logging.WARNING, logger="services.compaction"):
        assert compact(root) == "skipped"

    assert len(caplog.records) == 1
    assert sorted(os.listdir(day_dir)) == ["compact.json", late]
    assert (day_dir / "compact.json").read_bytes() == sidecar


def test_a_compact_without_a_manifest_or_a_broken_sidecar_without_a_compact_is_left_alone(root, day_dir):
    new_part(day_dir, (10, 0, 59), [row(t(10))])
    (day_dir / "compact.json").write_bytes(b"{")
    assert compact(root) == "error"
    assert (day_dir / "compact.json").read_bytes() == b"{"
    assert not (day_dir / "compact.parquet").exists()

    (day_dir / "compact.json").unlink()
    pq.write_table(pa.table({"solve_ts_ms": [1]}), day_dir / "compact.parquet")
    before = (day_dir / "compact.parquet").read_bytes()
    assert compact(root) == "error"
    assert (day_dir / "compact.parquet").read_bytes() == before
    assert not (day_dir / "compact.json").exists()


def test_the_lock_is_exclusive(root):
    with compaction.lock(root):
        fd = os.open(os.path.join(root, ".compaction.lock"), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


# ── The manifest and the partition helpers ───────────────────────────────────

GOOD = Manifest(
    store="tracks",
    day="2026-10-18",
    parts=("part-100059-a.parquet", "part-100159-b.parquet"),
    unreadable=("part-100259-c.parquet",),
    rows=3,
    groups=(Group(H10, "real", False, 2, H10 + 5, H10 + 9), Group(None, None, True, 1, None, None)),
    size=1234,
    sha256="0" * 64,
)


def test_a_manifest_round_trips_through_json():
    assert Manifest.from_json(GOOD.to_json()) == GOOD
    assert Manifest.from_json(GOOD.to_json().decode()) == GOOD


@pytest.mark.parametrize(
    "broken",
    [
        b"{not json",
        GOOD.to_json().replace(b'"rows":3', b'"rows":4'),
        GOOD.to_json().replace(b"part-100259-c", b"part-100059-a"),
        GOOD.to_json().replace(b'"kind":null,"legacy":true', b'"kind":"real","legacy":true'),
        GOOD.to_json().replace(str(H10 + 9).encode(), str(H11).encode()),
        GOOD.to_json().replace(b'"sha256":"' + b"0" * 64 + b'"', b'"sha256":null'),
        GOOD.to_json().replace(b"part-100059-a.parquet", b"../part-100059-a.parquet"),
        replace(
            GOOD,
            groups=(
                Group(H10, "synthetic", False, 1, H10 + 5, H10 + 5),
                Group(H10, "real", False, 1, H10 + 9, H10 + 9),
                GOOD.groups[1],
            ),
        ).to_json(),
        GOOD.to_json().replace(b'"store":"tracks"', b'"store":"archive"'),
    ],
    ids=[
        "not-json",
        "rows-not-the-sum",
        "part-also-unreadable",
        "legacy-with-kind",
        "time-outside-hour",
        "size-without-sha",
        "path-in-name",
        "kinds-out-of-order-within-an-hour",
        "store-not-known",
    ],
)
def test_a_malformed_manifest_is_a_value_error(broken):
    with pytest.raises(ValueError):
        Manifest.from_json(broken)


def test_the_stored_kind_order_of_each_store_only_grows_at_the_end():
    """Stored compacts hold their groups in this order and Manifest._check rejects any other order."""
    assert compaction.TRACKS.kinds == ("real", "synthetic", "mixed")
    assert compaction.ADSB.kinds == ("real", "sim")


def test_partition_paths_keys_and_part_names(root, day_dir):
    assert compaction.day_dir("/r", date(2026, 3, 7)) == "/r/year=2026/month=03/day=07"
    assert compaction.day_of_dir("/r/year=2026/month=03/day=07/") == date(2026, 3, 7)
    assert compaction.day_of_dir("/r/year=2026/month=02/day=30") is None
    assert compaction.day_of_dir("/r/year=2026/month=3/day=07") is None
    assert (
        compaction.object_key(TRACKS, date(2026, 3, 7), "compact.parquet")
        == "tracks/year=2026/month=03/day=07/compact.parquet"
    )
    assert compaction.store_for_prefix("tracks") is TRACKS and compaction.store_named("tracks") is TRACKS
    assert compaction.store_for_prefix("adsb") is ADSB and compaction.store_named("adsb") is ADSB
    assert compaction.store_for_prefix("archive") is None and compaction.store_named("archive") is None
    assert (
        compaction.object_key(ADSB, date(2026, 3, 7), "compact.parquet")
        == "adsb/year=2026/month=03/day=07/compact.parquet"
    )
    part = new_part(day_dir, (10, 0, 59), [row(t(10))])
    (day_dir / ".parquet-x.tmp").write_bytes(b"")
    (day_dir / "part-x.parquet.uploaded").write_bytes(b"")
    (day_dir / "part-dir.parquet").mkdir()
    assert compaction.raw_part_names(str(day_dir)) == [part]
    assert compaction.raw_part_names(str(day_dir / "missing")) == []


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 365), ("", 365), ("0", 0), ("30", 30), ("-1", 365), ("many", 365)]
)
def test_a_bad_count_in_the_environment_falls_back_to_the_default(monkeypatch, caplog, raw, expected):
    if raw is None:
        monkeypatch.delenv("TRACKS_COMPACT_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("TRACKS_COMPACT_RETENTION_DAYS", raw)
    with caplog.at_level(logging.WARNING):
        assert constants._parse_env_count("TRACKS_COMPACT_RETENTION_DAYS", 365) == expected
    warned = raw in ("-1", "many")
    assert [r.getMessage().startswith("TRACKS_COMPACT_RETENTION_DAYS=") for r in caplog.records] == [True] * warned


# ── The ADS-B snapshot store ─────────────────────────────────────────────────

ADSB_NAMES = adsb_writer.SCHEMA.names
# Per snapshot: one aircraft seen by its node and by ADSBHub, one by its node alone, one simulated.
_ADSB_MIX = (
    ("a1b2c3", "real", "node"),
    ("a1b2c3", "real", "aggregator"),
    ("c3d4e5", "real", "node"),
    ("f00001", "sim", "node"),
)


def snap(ts: int, hex_: str, world: str = "real", source: str = "node", lat: float = 34.85) -> dict:
    """An ADS-B fix as the snapshot task hands it to the writer; lat marks it."""
    node = {"node": "ret1a2b3c4d" if world == "real" else None, "aggregator": "ret1a2b3c4d,ret2b3c4d5e"}[source]
    return {
        "ts_ms": ts,
        "hex": hex_,
        "lat": lat,
        "lon": -82.39,
        "alt_baro": 35000,
        "gs": 450.0,
        "track": 270.0,
        "flight": "DAL1234",
        "seen_ms": ts - 500,
        "world": world,
        "source": source,
        "source_node_id": node,
    }


def adsb_part(root: str, hms: tuple[int, int, int], rows: list[dict]) -> str:
    """A raw part flushed at hms on DAY by the store's own writer; its name."""
    key = adsb_writer.write_adsb_parquet(rows=rows, base_dir=root, write_ts=datetime(2026, 10, 18, *hms, tzinfo=UTC))
    assert key is not None
    return os.path.basename(key)


def adsb_rows(directory: Path, name: str) -> Counter:
    with pq.ParquetFile(directory / name) as parquet:
        return Counter(tuple(d[column] for column in ADSB_NAMES) for d in parquet.read().to_pylist())


@pytest.fixture
def adsb_root(tmp_path) -> str:
    return str(tmp_path / "adsb")


def test_an_adsb_day_becomes_one_compact_of_hour_and_world_groups_holding_every_row(adsb_root):
    names = [
        adsb_part(
            adsb_root,
            (10, 30, 59),
            [snap(t(10, 30, s), h, w, src, lat=s) for s in (0, 10) for h, w, src in _ADSB_MIX],
        ),
        # Flushed after 11:00 with the 10:59:50 snapshot in it: each row goes to its own hour.
        adsb_part(
            adsb_root,
            (11, 0, 59),
            [snap(t(10, 59, 50), h, w, src, lat=59.0) for h, w, src in _ADSB_MIX]
            + [snap(t(11, 0, s), h, w, src, lat=60.0 + s) for s in (0, 10) for h, w, src in _ADSB_MIX],
        ),
    ]
    directory = Path(compaction.day_dir(adsb_root, DAY))
    raw = sum((adsb_rows(directory, name) for name in names), Counter())

    assert compaction.compact_day(ADSB, adsb_root, DAY, now_ms=READY_MS) == "compacted"

    assert adsb_rows(directory, "compact.parquet") == raw
    manifest = compaction.read_sidecar(str(directory))
    assert manifest is not None
    assert (manifest.store, manifest.parts, manifest.unreadable, manifest.rows) == (
        "adsb",
        tuple(sorted(names)),
        (),
        20,
    )
    assert [(g.hour_ms, g.kind, g.legacy, g.rows) for g in manifest.groups] == [
        (H10, "real", False, 9),
        (H10, "sim", False, 3),
        (H11, "real", False, 6),
        (H11, "sim", False, 2),
    ]
    with pq.ParquetFile(directory / "compact.parquet") as parquet:
        meta = parquet.metadata
        assert parquet.schema_arrow.equals(adsb_writer.SCHEMA, check_metadata=False)
        groups = [parquet.read_row_group(i) for i in range(meta.num_row_groups)]
    time_at, world_at = ADSB_NAMES.index("ts_ms"), ADSB_NAMES.index("world")
    for i, (group, table) in enumerate(zip(manifest.groups, groups, strict=True)):
        times = table.column("ts_ms").to_pylist()
        assert times == sorted(times) and set(table.column("world").to_pylist()) == {group.kind}
        ts_stats, world_stats = (
            meta.row_group(i).column(time_at).statistics,
            meta.row_group(i).column(world_at).statistics,
        )
        assert (ts_stats.min, ts_stats.max, ts_stats.null_count) == (group.ts_min, group.ts_max, 0)
        assert (world_stats.min, world_stats.max, world_stats.null_count) == (group.kind, group.kind, 0)
    assert groups[0].column("ts_ms").to_pylist() == [t(10, 30)] * 3 + [t(10, 30, 10)] * 3 + [t(10, 59, 50)] * 3
    # Ties on ts_ms keep the order the writer sorted them in: hex, then source.
    first = groups[0].slice(0, 3)
    assert list(zip(first.column("hex").to_pylist(), first.column("source").to_pylist())) == [
        ("a1b2c3", "aggregator"),
        ("a1b2c3", "node"),
        ("c3d4e5", "node"),
    ]


def test_a_late_adsb_part_is_compacted_in_beside_the_rows_already_compacted(adsb_root):
    first = adsb_part(adsb_root, (10, 0, 59), [snap(t(10, 0, 50), "a1b2c3"), snap(t(10, 0, 50), "f00001", "sim")])
    assert compaction.compact_day(ADSB, adsb_root, DAY, now_ms=READY_MS) == "compacted"
    directory = Path(compaction.day_dir(adsb_root, DAY))
    late = adsb_part(adsb_root, (10, 1, 59), [snap(t(10, 0, 40), "c3d4e5"), snap(t(23, 59, 50), "a1b2c3")])
    raw = adsb_rows(directory, first) + adsb_rows(directory, late)

    assert compaction.compact_day(ADSB, adsb_root, DAY, now_ms=READY_MS) == "compacted"

    assert adsb_rows(directory, "compact.parquet") == raw
    manifest = compaction.read_sidecar(str(directory))
    assert manifest is not None and manifest.parts == tuple(sorted([first, late]))
    assert [(g.hour_ms, g.kind, g.rows) for g in manifest.groups] == [
        (H10, "real", 2),
        (H10, "sim", 1),
        (t(23), "real", 1),
    ]
    with pq.ParquetFile(directory / "compact.parquet") as parquet:
        assert parquet.read_row_group(0).column("ts_ms").to_pylist() == [t(10, 0, 40), t(10, 0, 50)]


def test_an_adsb_part_lacking_a_column_the_schema_requires_stays_raw_and_the_day_still_compacts(adsb_root):
    good = adsb_part(adsb_root, (10, 0, 59), [snap(t(10, 0, 50), "a1b2c3")])
    directory = Path(compaction.day_dir(adsb_root, DAY))
    short = pa.schema([field for field in adsb_writer.SCHEMA if field.name != "seen_ms"])
    row = snap(t(10, 1, 50), "c3d4e5")
    table = pa.table({f.name: [row[f.name]] for f in short}, schema=short)
    broken = write_parquet_batch(table, directory, datetime(2026, 10, 18, 10, 1, 59, tzinfo=UTC))

    assert compaction.compact_day(ADSB, adsb_root, DAY, now_ms=READY_MS) == "compacted"

    manifest = compaction.read_sidecar(str(directory))
    assert manifest is not None
    assert (manifest.parts, manifest.unreadable, manifest.rows) == ((good,), (broken,), 1)
    assert adsb_rows(directory, "compact.parquet") == adsb_rows(directory, good)
    assert (directory / broken).exists()


def test_a_partition_without_a_local_compact_is_read_as_its_sidecar_and_raw_parts_say(adsb_root):
    a = adsb_part(adsb_root, (10, 0, 59), [snap(t(10, 0, 50), "a1b2c3")])
    b = adsb_part(adsb_root, (11, 0, 59), [snap(t(11, 0, 50), "a1b2c3")])
    directory = compaction.day_dir(adsb_root, DAY)

    def plan(names: list[str], store=ADSB, fetchable: bool = True) -> tuple:
        return tuple(compaction.read_plan(store, directory, DAY, names, fetchable=fetchable))

    # No sidecar: the raw parts, or the compact in object storage when there are none.
    assert plan([a, b]) == (False, None, [a, b])
    assert plan([]) == (True, None, [])
    assert compaction.compact_day(ADSB, adsb_root, DAY, now_ms=READY_MS) == "compacted"
    sidecar = compaction.read_sidecar(directory)
    os.unlink(os.path.join(directory, "compact.parquet"))
    late = adsb_part(adsb_root, (12, 0, 59), [snap(t(12, 0, 50), "a1b2c3")])

    # Every part the sidecar lists still on disk: they hold its rows, and nothing is fetched.
    assert plan([a, b, late]) == (False, None, [a, b, late])
    # One of them gone: the compact in object storage, checked against the sidecar, and the parts it does not list.
    assert plan([a, late]) == (True, sidecar, [late])
    # A day that cannot be fetched, or a sidecar of another store, leaves the raw parts.
    assert plan([a, late], fetchable=False) == (False, None, [a, late])
    assert plan([a, late], store=TRACKS) == (False, None, [a, late])
