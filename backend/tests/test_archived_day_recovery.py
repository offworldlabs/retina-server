"""Archived days left behind by an upload cut short, a compact.json or upload record that cannot be
read, or a compact whose download failed: every read answers exactly, or fails naming only the
day, and one hourly cycle brings the day's local files back by itself.

The object store is moto's in-process S3, configured as test_cold_store.py configures it. Days are
compacted from the store by scripts/compact_offloaded_days.py (process_day), swept by the archive
lifecycle's hourly cycle, and read through services/tracks_store.py and services/adsb_store.py as
the routes read them. Each row's lat (tracks) or hex (ADS-B) names the part it was written in, so
every expected answer is written out from the seeded parts.
"""

from __future__ import annotations

import hashlib
import os
import time
import unittest.mock
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import boto3
import pyarrow as pa
import pytest
from moto import mock_aws

from scripts import compact_offloaded_days as cli
from services import adsb_store, adsb_writer, cold_store, compaction, tracks_store
from services.cold_store import ColdStoreError
from services.compaction import ADSB, COMPACT_NAME, PARQUET_CONTENT_TYPE, PENDING_NAME, SIDECAR_NAME, TRACKS
from services.parquet_io import write_parquet_batch
from services.tasks import archive_lifecycle
from services.tasks import track_archive as ta
from services.track_writer import SCHEMA
from tests.test_cold_store import _FAKE_ENV

DAY = date(2026, 10, 18)
# 2026-10-19 00:30Z: the end of DAY plus the 30-minute settle margin.
READY_MS = 1_792_369_800_000
UNAVAILABLE = "The archived day 2026-10-18 could not be loaded from cold storage."


def ms(hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(2026, 10, 18, hour, minute, second, tzinfo=UTC).timestamp() * 1000)


def key_of(store: compaction.Store, name: str) -> str:
    return compaction.object_key(store, DAY, name)


def day_dir(root: Path) -> Path:
    return Path(compaction.day_dir(str(root), DAY))


class Crash(BaseException):
    """The process dying at one point of an upload; no handler of the code under test catches it."""


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
def root(tmp_path, monkeypatch, bucket) -> Iterator[Path]:
    """The tracks root the server, the lifecycle and the command share; the lifecycle's other roots empty."""
    path = tmp_path / "tracks"
    monkeypatch.setattr(ta, "_TRACKS_DIR", str(path))
    monkeypatch.setattr(archive_lifecycle, "_TRACKS_DIR", path)
    monkeypatch.setattr(archive_lifecycle, "_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(archive_lifecycle, "_ADSB_DIR", tmp_path / "adsb")
    tracks_store._reset_for_tests()
    yield path
    tracks_store._reset_for_tests()


@pytest.fixture
def work(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def hourly_cycle(monkeypatch, store: compaction.Store) -> dict:
    """One run of the archive lifecycle; the counts its sweep of ``store`` returned."""
    swept: dict = {}
    sweep = compaction.sweep

    def recording(of, *args, **kwargs):
        counts = sweep(of, *args, **kwargs)
        if of is store:
            swept.update(counts)
        return counts

    with monkeypatch.context() as patched:
        patched.setattr(compaction, "sweep", recording)
        archive_lifecycle.run_archive_lifecycle()
    return swept


def run_command(store: compaction.Store, root: Path, work: Path, *, dry_run: bool = False) -> cli.Outcome:
    """The command's work on DAY, as main runs it."""
    run = cli._Run(store, str(root), str(work), 2, dry_run, False, int(time.time() * 1000))
    return cli.process_day(run, DAY, cli.remote_days(store)[DAY])


def calls_to_the_store(monkeypatch) -> list[str]:
    """From now on, the keys the command downloads or uploads."""
    calls: list[str] = []
    for name in ("download_to", "download_bytes", "upload_file", "upload_bytes"):
        real = getattr(cold_store, name)

        def counting(key, *args, _real=real):
            calls.append(key)
            return _real(key, *args)

        monkeypatch.setattr(cold_store, name, counting)
    return calls


# ── Tracks ───────────────────────────────────────────────────────────────────


def track_part(directory: Path, minute: int, lats: list[float]) -> str:
    """A raw part flushed at 11:minute, one row per lat solved from 10:minute on, with no
    contributor, so every fleet publishes it; its name."""
    rows = [
        {
            "solve_ts_ms": ms(10, minute, i),
            "frame_ts_ms": ms(10, minute, i) - 700,
            "lat": lat,
            "lon": -82.39,
            "n_nodes": 2,
            "contributing_node_ids": "",
            "contributing_node_refs": "",
            "node_kind": "real",
            "track_key": f"mn-dark-{lat}",
        }
        for i, lat in enumerate(lats)
    ]
    table = pa.table({f.name: [r.get(f.name) for r in rows] for f in SCHEMA}, schema=SCHEMA)
    return write_parquet_batch(table, directory, datetime(2026, 10, 18, 11, minute, tzinfo=UTC))


def upload(store: compaction.Store, path: Path) -> None:
    content = PARQUET_CONTENT_TYPE if path.name == COMPACT_NAME else "application/json"
    assert cold_store.upload_file(key_of(store, path.name), str(path), content)


def stored_only(tmp_path: Path, minute: int, lats: list[float]) -> str:
    """A raw part the lifecycle uploaded and deleted locally: only in the store; its name."""
    elsewhere = tmp_path / "deleted-after-upload"
    elsewhere.mkdir(exist_ok=True)
    name = track_part(elsewhere, minute, lats)
    upload(TRACKS, elsewhere / name)
    return name


def read() -> list[float]:
    """Every row the day answers, 10:00 to 12:00, by lat."""
    body = tracks_store.query_tracks(ms(10), ms(12), fleet="all", private=frozenset())
    return sorted(row["lat"] for row in body["rows"])


@dataclass(frozen=True)
class Seeded:
    # What the day answered before the run, and what the run's compact holds.
    before: list[float]
    after: list[float]
    # The raw parts the run's compact lists, and the local compact.json before the run (None: none).
    parts: tuple[str, ...]
    sidecar: bytes | None


def seed_store_only(root: Path, tmp_path: Path) -> Seeded:
    """No local file of the day; the store holds two raw parts and no compact."""
    first = stored_only(tmp_path, 1, [3.0, 3.1])
    second = stored_only(tmp_path, 2, [4.0])
    assert read() == []
    return Seeded([], [3.0, 3.1, 4.0], tuple(sorted([first, second])), None)


def seed_compacted(root: Path, tmp_path: Path) -> Seeded:
    """Compacted earlier from two local parts, uploaded, then the compact and one part deleted
    locally as the lifecycle deletes them, the other part kept; the store also holds a raw part
    that compact lacks. The reader's cache is warm with that compact."""
    directory = day_dir(root)
    directory.mkdir(parents=True)
    kept = track_part(directory, 1, [1.0, 1.1])
    gone = track_part(directory, 2, [2.0])
    assert compaction.compact_day(TRACKS, str(root), DAY, now_ms=READY_MS) == "compacted"
    for name in (kept, gone, COMPACT_NAME, SIDECAR_NAME):
        upload(TRACKS, directory / name)
    (directory / COMPACT_NAME).unlink()
    (directory / gone).unlink()
    missing = stored_only(tmp_path, 3, [3.0])
    assert read() == [1.0, 1.1, 2.0]
    sidecar = (directory / SIDECAR_NAME).read_bytes()
    return Seeded([1.0, 1.1, 2.0], [1.0, 1.1, 2.0, 3.0], tuple(sorted([kept, gone, missing])), sidecar)


# Where the run dies: right after the real call to this function returns.
CRASH_POINTS = {
    "record written": (compaction, "write_pending"),
    "compact uploaded": (cold_store, "upload_file"),
    "compact.json uploaded": (cold_store, "upload_bytes"),
    "compact.json written": (compaction, "write_sidecar"),
}
# The points after which the store holds the run's compact.
LANDED = {"compact uploaded", "compact.json uploaded", "compact.json written"}


def cut_short(monkeypatch, point: str, run) -> None:
    """``run`` with the process dying at ``point``, or with the compact's upload refused."""
    with monkeypatch.context() as patched:
        if point == "compact upload refused":
            patched.setattr(cold_store, "upload_file", lambda key, path, content_type: False)
            with pytest.raises(cli._DayFailed, match=f"{PENDING_NAME} stays until the hourly sweep"):
                run()
            return
        module, name = CRASH_POINTS[point]
        real = getattr(module, name)

        def dying(*args, **kwargs):
            real(*args, **kwargs)
            raise Crash

        patched.setattr(module, name, dying)
        with pytest.raises(Crash):
            run()


@pytest.mark.parametrize("seeded_as", ["no compact.json", "compact.json of an earlier compact"])
@pytest.mark.parametrize("point", [*CRASH_POINTS, "compact upload refused"])
def test_an_upload_cut_short_reads_exactly_and_one_cycle_settles_it(
    root, tmp_path, work, monkeypatch, point, seeded_as
):
    seeded = (seed_store_only if seeded_as == "no compact.json" else seed_compacted)(root, tmp_path)
    directory = day_dir(root)

    cut_short(monkeypatch, point, lambda: run_command(TRACKS, root, work))
    late = track_part(directory, 50, [9.0])

    landed = point in LANDED
    expected = sorted((seeded.after if landed else seeded.before) + [9.0])
    # The first read after the event is exact: no row twice, none missing.
    assert read() == expected
    assert (directory / PENDING_NAME).exists()

    built: list = []
    build = compaction.build
    with monkeypatch.context() as patched:
        patched.setattr(compaction, "build", lambda *args, **kwargs: built.append(args) or build(*args, **kwargs))
        swept = hourly_cycle(monkeypatch, TRACKS)

    assert swept["settled" if landed else "dropped"] == 1
    assert not (directory / PENDING_NAME).exists()
    # Nothing was compacted from the late part while the record stood.
    assert built == [] and not (directory / COMPACT_NAME).exists()
    if landed:
        stored = cold_store.download_bytes(key_of(TRACKS, COMPACT_NAME))
        assert stored is not None
        sidecar = compaction.load_sidecar(TRACKS, str(directory), DAY)
        assert sidecar is not None
        assert (sidecar.parts, sidecar.size, sidecar.sha256) == (
            seeded.parts,
            len(stored),
            hashlib.sha256(stored).hexdigest(),
        )
    else:
        path = directory / SIDECAR_NAME
        assert (path.read_bytes() if path.exists() else None) == seeded.sidecar
    assert read() == expected

    if landed:
        # The late part waits for the sweep, which leaves it raw beside a compact kept only in the
        # store; set aside, the day is done, at no download or upload.
        os.replace(directory / late, tmp_path / late)
        calls = calls_to_the_store(monkeypatch)
        assert run_command(TRACKS, root, work).status == "done"
        assert calls == []


@pytest.mark.parametrize(("point", "status"), [("record written", "compacted"), ("compact.json uploaded", "done")])
def test_the_command_settles_an_upload_cut_short_before_anything_else_on_the_day(
    root, tmp_path, work, monkeypatch, point, status
):
    seeded = seed_compacted(root, tmp_path)
    cut_short(monkeypatch, point, lambda: run_command(TRACKS, root, work))
    files = sorted(os.listdir(day_dir(root)))

    # A dry run only reports it.
    assert run_command(TRACKS, root, work, dry_run=True).status == "would settle"
    assert sorted(os.listdir(day_dir(root))) == files

    assert run_command(TRACKS, root, work).status == status
    assert not (day_dir(root) / PENDING_NAME).exists()
    assert read() == seeded.after
    assert run_command(TRACKS, root, work).status == "done"


def test_while_the_store_cannot_say_whether_an_upload_landed_nothing_is_compacted_from_late_parts(
    root, tmp_path, work, monkeypatch
):
    seed_store_only(root, tmp_path)
    cut_short(monkeypatch, "compact uploaded", lambda: run_command(TRACKS, root, work))
    directory = day_dir(root)
    track_part(directory, 50, [9.0])

    with monkeypatch.context() as down:
        down.setattr(cold_store._client, "download_bytes", lambda key: None)
        down.setattr(cold_store._client, "list_keys", lambda prefix="": [])
        swept = hourly_cycle(monkeypatch, TRACKS)

    assert swept["unsettled"] == 1 and swept["compacted"] == 0
    assert sorted(os.listdir(directory)) == sorted([PENDING_NAME, *compaction.raw_part_names(str(directory))])
    assert hourly_cycle(monkeypatch, TRACKS)["settled"] == 1
    assert read() == [3.0, 3.1, 4.0, 9.0]


def stored_sidecar() -> bytes | None:
    """The store's compact.json of the tracks day."""
    return cold_store.download_bytes(key_of(TRACKS, SIDECAR_NAME))


def land_the_compact_alone(monkeypatch, root: Path, work: Path, how: str) -> None:
    """Run the command so that its compact lands in the store and its compact.json does not."""
    if how == "dies between the two uploads":
        cut_short(monkeypatch, "compact uploaded", lambda: run_command(TRACKS, root, work))
        return
    with monkeypatch.context() as refusing:
        refusing.setattr(cold_store, "upload_bytes", lambda key, data, content_type: False)
        with pytest.raises(cli._DayFailed, match=f"could not upload {key_of(TRACKS, SIDECAR_NAME)}"):
            run_command(TRACKS, root, work)


@pytest.mark.parametrize("how", ["dies between the two uploads", "compact.json refused"])
def test_a_settled_upload_leaves_the_stores_compact_json_describing_its_compact_so_a_restored_one_reads_whole(
    root, tmp_path, work, monkeypatch, how
):
    seeded = seed_compacted(root, tmp_path)
    directory = day_dir(root)
    land_the_compact_alone(monkeypatch, root, work, how)
    assert stored_sidecar() == seeded.sidecar

    assert hourly_cycle(monkeypatch, TRACKS)["settled"] == 1
    settled = (directory / SIDECAR_NAME).read_bytes()
    record = compaction.load_sidecar(TRACKS, str(directory), DAY)
    stored = cold_store.download_bytes(key_of(TRACKS, COMPACT_NAME))
    assert record is not None and stored is not None
    assert (record.size, record.sha256) == (len(stored), hashlib.sha256(stored).hexdigest())
    assert stored_sidecar() == settled
    assert read() == seeded.after

    # A torn compact.json is restored from the store's copy, and the day reads whole.
    (directory / SIDECAR_NAME).write_bytes(b"\x00torn")
    assert hourly_cycle(monkeypatch, TRACKS)["repaired"] == 1
    assert (directory / SIDECAR_NAME).read_bytes() == settled
    assert read() == seeded.after


def test_while_the_store_refuses_the_compact_json_of_a_landed_upload_its_record_stays_and_the_day_reads_exactly(
    root, tmp_path, work, monkeypatch
):
    seeded = seed_compacted(root, tmp_path)
    directory = day_dir(root)
    land_the_compact_alone(monkeypatch, root, work, "dies between the two uploads")

    with monkeypatch.context() as refusing:
        refusing.setattr(cold_store, "upload_bytes", lambda key, data, content_type: False)
        assert hourly_cycle(monkeypatch, TRACKS)["unsettled"] == 1
        with pytest.raises(cli._DayFailed, match=f"whose {SIDECAR_NAME} the store would not take"):
            run_command(TRACKS, root, work)

    assert (directory / PENDING_NAME).exists()
    assert (directory / SIDECAR_NAME).read_bytes() == seeded.sidecar
    assert stored_sidecar() == seeded.sidecar
    assert read() == seeded.after
    # Once the store takes it, the next cycle settles the record.
    assert hourly_cycle(monkeypatch, TRACKS)["settled"] == 1
    assert stored_sidecar() == (directory / SIDECAR_NAME).read_bytes()


# ── ADS-B ────────────────────────────────────────────────────────────────────


def adsb_part(base: Path, minute: int, hexes: list[str]) -> str:
    """An ADS-B part flushed at 11:minute, one simulator fix per hex from 10:minute on, which the
    synthetic fleet publishes; its key below ``base``."""
    fixes = [
        {"ts_ms": ms(10, minute, i), "hex": hex_, "lat": 34.85, "lon": -82.39, "seen_ms": ms(10, minute, i) - 500}
        | {"world": "sim", "source": "node", "source_node_id": None}
        for i, hex_ in enumerate(hexes)
    ]
    key = adsb_writer.write_adsb_parquet(
        rows=fixes, base_dir=base, write_ts=datetime(2026, 10, 18, 11, minute, tzinfo=UTC)
    )
    assert key is not None
    return key


def test_an_adsb_upload_cut_short_after_its_compact_landed_reads_exactly_and_one_cycle_settles_it(
    root, tmp_path, work, monkeypatch
):
    adsb_root = tmp_path / "adsb"
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(adsb_root))
    adsb_store._reset_for_tests()
    elsewhere = tmp_path / "adsb-deleted-after-upload"
    for minute, hexes in ((1, ["aaaaa1", "aaaaa2"]), (2, ["bbbbb1"])):
        key = adsb_part(elsewhere, minute, hexes)
        assert cold_store.upload_file(f"adsb/{key}", str(elsewhere / key), "application/json")

    def answered() -> list[str]:
        body = adsb_store.query_adsb(ms(10), ms(12), fleet="synthetic", private=frozenset())
        return sorted(row["hex"] for row in body["rows"])

    assert answered() == []
    cut_short(monkeypatch, "compact uploaded", lambda: run_command(ADSB, adsb_root, work))
    adsb_part(adsb_root, 50, ["fffff9"])

    expected = ["aaaaa1", "aaaaa2", "bbbbb1", "fffff9"]
    assert answered() == expected
    assert hourly_cycle(monkeypatch, ADSB)["settled"] == 1
    directory = Path(compaction.day_dir(str(adsb_root), DAY))
    sidecar = compaction.load_sidecar(ADSB, str(directory), DAY)
    assert not (directory / PENDING_NAME).exists()
    assert sidecar is not None and sidecar.rows == 3
    assert answered() == expected


# ── A compact.json or upload record that cannot be read ─────────────────────


def test_a_day_whose_compact_json_cannot_be_read_fails_naming_the_day_and_counts_nothing(root, tmp_path):
    seed_compacted(root, tmp_path)
    assert [h["count"] for h in tracks_store.hourly_availability(DAY, DAY, fleet="all")["hours"]] == [3]
    (day_dir(root) / SIDECAR_NAME).write_bytes(b'{"version": 1, "store": "tra')

    # The part still on disk would answer short; no rows would answer wrong.
    with pytest.raises(ColdStoreError) as raised:
        read()
    assert str(raised.value) == UNAVAILABLE
    assert tracks_store.hourly_availability(DAY, DAY, fleet="all")["hours"] == []


def test_the_hourly_cycle_restores_a_compact_json_that_cannot_be_read_from_the_stores_copy(root, tmp_path, monkeypatch):
    seeded = seed_compacted(root, tmp_path)
    (day_dir(root) / SIDECAR_NAME).write_bytes(b"\x00torn")
    with pytest.raises(ColdStoreError):
        read()

    assert hourly_cycle(monkeypatch, TRACKS)["repaired"] == 1
    assert (day_dir(root) / SIDECAR_NAME).read_bytes() == seeded.sidecar
    assert read() == [1.0, 1.1, 2.0]


def test_a_day_whose_upload_record_cannot_be_read_fails_and_waits_for_the_command(root, tmp_path, work, monkeypatch):
    seeded = seed_compacted(root, tmp_path)
    record = day_dir(root) / PENDING_NAME
    record.write_bytes(b"{")

    with pytest.raises(ColdStoreError) as raised:
        read()
    assert str(raised.value) == UNAVAILABLE
    assert hourly_cycle(monkeypatch, TRACKS)["errors"] == 1
    assert record.read_bytes() == b"{"
    with pytest.raises(cli._DayFailed, match="delete it and run this command again for the day"):
        run_command(TRACKS, root, work)

    record.unlink()
    assert run_command(TRACKS, root, work).status == "compacted"
    assert read() == seeded.after


# ── A download that fails ────────────────────────────────────────────────────


def test_a_compact_the_store_lists_but_fails_to_download_fails_for_30_seconds_then_reads_whole(
    root, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "compacted-elsewhere"
    directory = Path(compaction.day_dir(str(elsewhere), DAY))
    directory.mkdir(parents=True)
    track_part(directory, 1, [5.0, 5.1])
    assert compaction.compact_day(TRACKS, str(elsewhere), DAY, now_ms=READY_MS) == "compacted"
    upload(TRACKS, directory / COMPACT_NAME)
    clock = [1000.0]
    monkeypatch.setattr(cold_store, "_clock", lambda: clock[0])
    downloads: list[str] = []
    download = cold_store._client.download_bytes

    def failing_first(key: str) -> bytes | None:
        downloads.append(key)
        return None if len(downloads) == 1 else download(key)

    monkeypatch.setattr(cold_store._client, "download_bytes", failing_first)

    with pytest.raises(ColdStoreError) as raised:
        read()
    assert str(raised.value) == UNAVAILABLE
    assert cold_store._misses == {}
    clock[0] += cold_store._FAILURE_TTL_S - 1
    with pytest.raises(ColdStoreError):
        read()
    assert downloads == [key_of(TRACKS, COMPACT_NAME)]

    clock[0] += 1
    assert read() == [5.0, 5.1]
    assert downloads == [key_of(TRACKS, COMPACT_NAME)] * 2
