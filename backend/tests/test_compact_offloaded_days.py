"""scripts/compact_offloaded_days.py: archive days compacted from raw parts that live only in object storage.

The object store is moto's in-process S3, configured as test_cold_store.py
configures it. Raw parts are written by the archive's batch publisher in the
writer's schema, or in the 14 columns legacy parts had, and uploaded under the
keys the lifecycle gives them: the store's prefix and the path below its root.
ADS-B parts are written by their own writer (services/adsb_writer.py).
"""

from __future__ import annotations

import io
import os
import unittest.mock
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from scripts import compact_offloaded_days as cli
from services import adsb_writer, cold_store, compaction
from services.compaction import ADSB, COMPACT_NAME, PENDING_NAME, SIDECAR_NAME, TRACKS, Manifest
from services.parquet_io import write_parquet_batch
from services.track_writer import SCHEMA

DAY = date(2026, 10, 18)
OTHER_DAY = date(2026, 10, 17)
LEGACY_SCHEMA = pa.schema(list(SCHEMA)[:14])
# 2026-10-19 00:30Z: the end of DAY plus the 30-minute settle margin.
READY_MS = 1_792_369_800_000
COMPACT_KEY = "tracks/year=2026/month=10/day=18/compact.parquet"
SIDECAR_KEY = "tracks/year=2026/month=10/day=18/compact.json"

_FAKE_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "AKIATEST",
    "R2_SECRET_ACCESS_KEY": "testsecret",
    "R2_BUCKET": "retina-data",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "AKIATEST",
    "AWS_SECRET_ACCESS_KEY": "testsecret",
}


@pytest.fixture
def bucket(tmp_path, monkeypatch) -> Iterator[None]:
    """A configured object store with one empty bucket, and the read-through cache under tmp_path."""
    monkeypatch.setattr(cold_store, "_CACHE_DIR", str(tmp_path / "cold_cache"))
    cold_store._reset_for_tests()
    client = cold_store._client
    for name, value in (
        ("_ENABLED", True),
        ("_BUCKET", "retina-data"),
        ("_ACCOUNT_ID", "testaccount"),
        ("_ACCESS_KEY", "AKIATEST"),
        ("_SECRET_KEY", "testsecret"),
        ("_ENDPOINT_URL", None),
    ):
        monkeypatch.setattr(client, name, value)
    with unittest.mock.patch.dict(os.environ, _FAKE_ENV), mock_aws():
        client._clear_cache()
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        yield
        client._clear_cache()
    cold_store._reset_for_tests()


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "tracks"


@pytest.fixture
def work(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def rows(first: int, count: int, kind: str = "real") -> list[dict]:
    """``count`` track rows on DAY from 10:00:``first``Z, one a second; lat marks each."""
    out = []
    for second in range(first, first + count):
        ts = int(datetime(2026, 10, 18, 10, 0, second, tzinfo=UTC).timestamp() * 1000)
        out.append(
            {
                "solve_ts_ms": ts,
                "frame_ts_ms": ts - 700,
                "lat": 34.0 + second / 100,
                "lon": -82.39,
                "n_nodes": 2,
                "contributing_node_ids": "synth-alpha" if kind == "synthetic" else "ret1a2b3c4d",
                "node_kind": kind,
                "track_key": f"mn-dark-{second}",
            }
        )
    return out


def write_part(root: Path, day: date, part_rows: list[dict], *, legacy: bool = False) -> Path:
    """A raw part in ``day``'s partition under ``root``, in the writer's schema or the legacy one."""
    schema = LEGACY_SCHEMA if legacy else SCHEMA
    table = pa.table({f.name: [r.get(f.name) for r in part_rows] for f in schema}, schema=schema)
    directory = Path(compaction.day_dir(str(root), day))
    name = write_parquet_batch(table, directory, datetime(day.year, day.month, day.day, 12, tzinfo=UTC))
    return directory / name


def offload(root: Path, part: Path) -> str:
    """Upload a raw part as the lifecycle does, under the prefix and its path below the root; its name."""
    assert cold_store.upload_file(f"tracks/{part.relative_to(root)}", str(part), "application/json")
    return part.name


def offloaded(tmp_path: Path, day: date, part_rows: list[dict], *, legacy: bool = False) -> str:
    """A raw part that lives only in the store, its local copy deleted after the upload; its name."""
    elsewhere = tmp_path / "deleted-after-upload"
    return offload(elsewhere, write_part(elsewhere, day, part_rows, legacy=legacy))


def stored(r: dict, *, legacy: bool = False) -> tuple:
    """A row as a compact holds it: a column its part lacked is null."""
    return tuple(r.get(name) if not legacy or name in LEGACY_SCHEMA.names else None for name in SCHEMA.names)


def row_counter(table: pa.Table) -> Counter:
    return Counter(tuple(d[name] for name in SCHEMA.names) for d in table.to_pylist())


def stored_compact() -> pa.Table:
    data = cold_store.download_bytes(COMPACT_KEY)
    assert data is not None
    return pq.read_table(io.BytesIO(data))


def keys() -> list[str]:
    return sorted(cold_store.list_keys("tracks/"))


def tree(path: Path) -> list[tuple[str, int, int]]:
    """Every file below ``path`` with its size and mtime."""
    return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file())


def count_calls(monkeypatch) -> dict[str, list[str]]:
    """From now on, the keys cold_store's object-store calls are made with."""
    calls: dict[str, list[str]] = {"download_to": [], "download_bytes": [], "upload_file": [], "upload_bytes": []}
    for name, seen in calls.items():
        real = getattr(cold_store, name)

        def counting(key, *args, _real=real, _seen=seen):
            _seen.append(key)
            return _real(key, *args)

        monkeypatch.setattr(cold_store, name, counting)
    return calls


def run(root: Path, work: Path, capsys, *args: str) -> tuple[int, dict[str, str]]:
    """The exit status, and each day's printed outcome line by ISO day."""
    status = cli.main(["--root", str(root), "--work-dir", str(work), *args])
    lines = capsys.readouterr().out.splitlines()
    return status, {line.split()[0]: line for line in lines if line[:4].isdigit()}


def day_dir(root: Path, day: date = DAY) -> Path:
    return Path(compaction.day_dir(str(root), day))


# ── Compacting from the store ────────────────────────────────────────────────


def test_a_day_only_in_the_store_is_compacted_uploaded_and_served(bucket, tmp_path, root, work, capsys):
    new_a, new_b, old = rows(0, 3), rows(10, 2, "synthetic"), rows(20, 2)
    names = [
        offloaded(tmp_path, DAY, new_a),
        offloaded(tmp_path, DAY, new_b),
        offloaded(tmp_path, DAY, old, legacy=True),
    ]

    status, lines = run(root, work, capsys)

    assert status == 0
    assert " compacted " in lines["2026-10-18"]
    s3 = boto3.client("s3", region_name="us-east-1")
    assert s3.head_object(Bucket="retina-data", Key=COMPACT_KEY)["ContentType"] == "application/vnd.apache.parquet"
    assert s3.head_object(Bucket="retina-data", Key=SIDECAR_KEY)["ContentType"] == "application/json"
    expected = Counter([stored(r) for r in new_a + new_b] + [stored(r, legacy=True) for r in old])
    assert row_counter(stored_compact()) == expected
    assert sorted(os.listdir(day_dir(root))) == [SIDECAR_NAME]
    sidecar = compaction.read_sidecar(str(day_dir(root)))
    assert sidecar is not None and sidecar.parts == tuple(sorted(names)) and sidecar.rows == 7
    assert Manifest.from_json(cold_store.download_bytes(SIDECAR_KEY) or b"") == sidecar
    assert os.listdir(work) == []
    with cold_store.open_day(TRACKS, DAY, expected=sidecar, now_ms=READY_MS) as path:
        assert path is not None and os.path.getsize(path) == sidecar.size


def test_a_second_run_downloads_and_uploads_nothing(bucket, tmp_path, root, work, capsys, monkeypatch):
    offloaded(tmp_path, DAY, rows(0, 3))
    offloaded(tmp_path, DAY, rows(10, 2), legacy=True)
    assert run(root, work, capsys)[0] == 0
    before = tree(root)
    calls = count_calls(monkeypatch)

    status, lines = run(root, work, capsys)

    assert status == 0
    assert lines["2026-10-18"].split()[1] == "done"
    assert calls == {"download_to": [], "download_bytes": [], "upload_file": [], "upload_bytes": []}
    assert tree(root) == before


def test_sidecars_only_restores_a_missing_sidecar_without_compacting(bucket, tmp_path, root, work, capsys, monkeypatch):
    offloaded(tmp_path, DAY, rows(0, 3))
    offloaded(tmp_path, OTHER_DAY, rows(10, 2))
    assert run(root, work, capsys, "--from", "2026-10-18", "--to", "2026-10-18")[0] == 0
    sidecar = compaction.read_sidecar(str(day_dir(root)))
    (day_dir(root) / SIDECAR_NAME).unlink()
    calls = count_calls(monkeypatch)

    status, lines = run(root, work, capsys, "--sidecars-only")

    assert status == 0
    assert lines["2026-10-18"].split()[1] == "restored"
    assert lines["2026-10-17"].split()[1] == "skipped:"
    assert compaction.read_sidecar(str(day_dir(root))) == sidecar
    assert calls == {"download_to": [], "download_bytes": [SIDECAR_KEY], "upload_file": [], "upload_bytes": []}
    assert not day_dir(root, OTHER_DAY).exists()


def test_a_partly_local_day_merges_the_stored_parts_into_its_local_compact(
    bucket, tmp_path, root, work, capsys, monkeypatch
):
    local_rows = [rows(0, 2), rows(10, 2, "synthetic")]
    local = [offload(root, write_part(root, DAY, r)) for r in local_rows]
    assert compaction.compact_day(TRACKS, str(root), DAY, now_ms=READY_MS) == "compacted"
    gone_rows = [rows(20, 3), rows(30, 2)]
    gone = [offloaded(tmp_path, DAY, gone_rows[0]), offloaded(tmp_path, DAY, gone_rows[1], legacy=True)]
    calls = count_calls(monkeypatch)

    status, lines = run(root, work, capsys)

    assert status == 0
    assert " compacted " in lines["2026-10-18"]
    with pq.ParquetFile(day_dir(root) / COMPACT_NAME) as parquet:
        table = parquet.read()
    expected = [stored(r) for part in local_rows + gone_rows[:1] for r in part]
    assert row_counter(table) == Counter(expected + [stored(r, legacy=True) for r in gone_rows[1]])
    sidecar = compaction.read_sidecar(str(day_dir(root)))
    assert sidecar is not None and sidecar.parts == tuple(sorted(local + gone)) and sidecar.rows == 9
    footer = compaction.read_footer_manifest(str(day_dir(root) / COMPACT_NAME))
    assert footer is not None and footer.parts == sidecar.parts
    assert calls["upload_file"] == calls["upload_bytes"] == []
    assert COMPACT_KEY not in keys()
    assert sorted(compaction.raw_part_names(str(day_dir(root)))) == sorted(local)


def test_a_day_with_local_parts_the_sweep_has_not_compacted_is_left_alone(
    bucket, tmp_path, root, work, capsys, monkeypatch
):
    offload(root, write_part(root, DAY, rows(0, 2)))
    offloaded(tmp_path, DAY, rows(10, 2))
    before = tree(root)
    calls = count_calls(monkeypatch)

    status, lines = run(root, work, capsys)

    assert status == 0
    assert "skipped: pending the hourly compaction" in lines["2026-10-18"]
    assert tree(root) == before
    assert calls == {"download_to": [], "download_bytes": [], "upload_file": [], "upload_bytes": []}


def test_a_corrupt_stored_part_is_listed_unreadable_and_the_day_still_compacts(bucket, tmp_path, root, work, capsys):
    good_rows = rows(0, 3)
    good = offloaded(tmp_path, DAY, good_rows)
    bad = "part-120000-0123456789abcdef0123456789abcdef.parquet"
    assert cold_store.upload_bytes(f"tracks/year=2026/month=10/day=18/{bad}", b"not parquet", "application/json")

    status, lines = run(root, work, capsys)

    assert status == 0
    assert lines["2026-10-18"].endswith(f"unreadable={bad}")
    sidecar = compaction.read_sidecar(str(day_dir(root)))
    assert sidecar is not None and sidecar.parts == (good,) and sidecar.unreadable == (bad,)
    assert row_counter(stored_compact()) == Counter(stored(r) for r in good_rows)


def test_a_failed_upload_fails_the_day_and_leaves_only_its_upload_record(
    bucket, tmp_path, root, work, capsys, monkeypatch
):
    offloaded(tmp_path, DAY, rows(0, 3))
    monkeypatch.setattr(cold_store, "upload_file", lambda key, path, content_type: False)

    status, lines = run(root, work, capsys)

    assert status == 1
    assert lines["2026-10-18"].split()[1] == "failed:"
    assert "compact.pending.json stays until the hourly sweep or the next run settles it" in lines["2026-10-18"]
    assert sorted(os.listdir(day_dir(root))) == [PENDING_NAME]
    assert SIDECAR_KEY not in keys()
    assert os.listdir(work) == []


def test_a_dry_run_changes_nothing_local_or_remote(bucket, tmp_path, root, work, capsys, monkeypatch):
    offloaded(tmp_path, OTHER_DAY, rows(0, 2))
    assert run(root, work, capsys, "--to", "2026-10-17")[0] == 0
    (day_dir(root, OTHER_DAY) / SIDECAR_NAME).unlink()
    offloaded(tmp_path, DAY, rows(10, 3))
    before_local, before_remote = tree(root), keys()
    calls = count_calls(monkeypatch)

    status, lines = run(root, work, capsys, "--dry-run")

    assert status == 0
    assert "would compact: raw parts to download: 1" in lines["2026-10-18"]
    assert "would restore" in lines["2026-10-17"]
    assert (tree(root), keys()) == (before_local, before_remote)
    assert calls["download_to"] == calls["upload_file"] == calls["upload_bytes"] == []
    assert os.listdir(work) == []


def test_without_an_object_store_it_exits_2(root, work, capsys, monkeypatch):
    monkeypatch.setattr(cold_store._client, "_ENABLED", False)

    assert run(root, work, capsys)[0] == 2
    assert not root.exists()


def test_a_store_that_lists_no_days_exits_1_and_says_so(bucket, root, work, capsys, monkeypatch):
    monkeypatch.setattr(cold_store, "list_keys", lambda prefix: [])

    status = cli.main(["--root", str(root), "--work-dir", str(work)])

    assert status == 1
    assert capsys.readouterr().out.splitlines() == [
        "the store listed no tracks days under tracks/ (an empty store or a failed listing; see the log)"
    ]


def test_a_work_dir_inside_the_store_root_is_refused(bucket, root, capsys):
    (root / "work").mkdir(parents=True)

    with pytest.raises(SystemExit) as exited:
        cli.main(["--root", str(root), "--work-dir", str(root / "work")])

    assert exited.value.code == 2
    assert "inside the store root" in capsys.readouterr().err


# ── The ADS-B store ──────────────────────────────────────────────────────────


def adsb_offloaded(tmp_path: Path, hms: tuple[int, int, int], hexes: list[str]) -> tuple[str, Counter]:
    """An ADS-B part of DAY flushed at hms, one fix per hex, that lives only in the store; its name and rows."""
    elsewhere = tmp_path / "adsb-deleted-after-upload"
    flushed = datetime(2026, 10, 18, *hms, tzinfo=UTC)
    ts = int(flushed.timestamp() * 1000) - 9_000
    fixes = [
        {"ts_ms": ts, "hex": hex_, "lat": 34.85, "lon": -82.39, "seen_ms": ts - 500, "world": "real"}
        | {"source": "node", "source_node_id": "ret1a2b3c4d", "flight": f"DAL{i}"}
        for i, hex_ in enumerate(hexes)
    ]
    key = adsb_writer.write_adsb_parquet(rows=fixes, base_dir=elsewhere, write_ts=flushed)
    assert key is not None
    assert cold_store.upload_file(f"adsb/{key}", str(elsewhere / key), "application/json")
    with pq.ParquetFile(elsewhere / key) as parquet:
        table = parquet.read()
    return os.path.basename(key), Counter(tuple(d.values()) for d in table.to_pylist())


def test_store_adsb_compacts_the_adsb_days_and_leaves_the_tracks_ones(bucket, tmp_path, work, capsys):
    tracks_part = offloaded(tmp_path, DAY, rows(0, 2))
    first, first_rows = adsb_offloaded(tmp_path, (10, 0, 59), ["a1b2c3", "c3d4e5"])
    second, second_rows = adsb_offloaded(tmp_path, (11, 0, 59), ["a1b2c3"])
    adsb_root = tmp_path / "adsb"

    status = cli.main(["--store", "adsb", "--root", str(adsb_root), "--work-dir", str(work)])

    lines = capsys.readouterr().out.splitlines()
    assert status == 0
    assert lines[0].startswith("2026-10-18  compacted  parts=2 rows=3 ")
    assert lines[-1] == "adsb: 1 days: 1 compacted, 0 failed"
    s3 = boto3.client("s3", region_name="us-east-1")
    adsb_key = "adsb/year=2026/month=10/day=18"
    assert s3.head_object(Bucket="retina-data", Key=f"{adsb_key}/compact.parquet")["ContentType"] == (
        "application/vnd.apache.parquet"
    )
    data = cold_store.download_bytes(f"{adsb_key}/compact.parquet")
    assert data is not None
    assert Counter(tuple(d.values()) for d in pq.read_table(io.BytesIO(data)).to_pylist()) == first_rows + second_rows
    sidecar = compaction.read_sidecar(compaction.day_dir(str(adsb_root), DAY))
    assert sidecar is not None and (sidecar.store, sidecar.parts) == ("adsb", tuple(sorted([first, second])))
    with cold_store.open_day(ADSB, DAY, expected=sidecar, now_ms=READY_MS) as path:
        assert path is not None and os.path.getsize(path) == sidecar.size
    # The tracks day in the same store is not touched.
    assert keys() == [f"tracks/year=2026/month=10/day=18/{tracks_part}"]
