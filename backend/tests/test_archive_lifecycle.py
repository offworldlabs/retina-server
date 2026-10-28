"""Tests for archive lifecycle: offload to the object store + local disk cleanup.

Two suites:
  1. TestArchiveLifecycleMoto  — integration tests using moto to mock S3.
  2. Unit test classes         — error-path and edge-case tests using
                                 unittest.mock only (no real or mocked S3).

Tracks and ADS-B parts are real Parquet in real day partitions, so the
compaction sweep that starts every cycle compacts a complete day as it would on
a host.
"""

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
import unittest.mock
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from moto import mock_aws

from config.constants import ARCHIVE_OFFLOAD_AGE_DAYS
from services import adsb_writer, compaction, object_store
from services.track_writer import SCHEMA
from tests.object_store_env import FAKE_AWS_ENV, store_env

# Tests that exercise the deletion phase patch this value into the lifecycle
# module — the production default is 0 (disabled), so we use a fixed positive
# value here whenever we need to verify that deletion happens.
_TEST_RETENTION_DAYS = 14


def _patch_retention(value: int = _TEST_RETENTION_DAYS):
    """Patch ARCHIVE_RETENTION_DAYS inside the lifecycle module for one test."""
    return unittest.mock.patch("services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", value)


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_bucket(bucket: str = "retina-data"):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)


def _create_archive_file(archive_dir: Path, age_days: float, content: bytes = b"{}") -> Path:
    """Create a year/month/day/node/frame_*.json file with the given mtime age."""
    mtime = time.time() - age_days * 86400
    node_dir = archive_dir / "2025" / "01" / "01" / "node_test"
    node_dir.mkdir(parents=True, exist_ok=True)
    fname = node_dir / f"frame_{int(mtime * 1000)}.json"
    fname.write_bytes(content)
    os.utime(fname, (mtime, mtime))
    return fname


def _make_archive_file(archive_dir: Path, age_days: float = 0.0, name: str = "data.json") -> Path:
    """Create a properly structured archive file used by the unit-test suite."""
    node_dir = archive_dir / "2024" / "01" / "15" / "node-A"
    node_dir.mkdir(parents=True, exist_ok=True)
    f = node_dir / name
    f.write_bytes(b"{}")
    mtime = time.time() - age_days * 86400
    os.utime(f, (mtime, mtime))
    return f


# A tracks day long complete: the first cycle's sweep compacts its partition.
_TRACKS_DAY = date(2026, 5, 1)
_DAY_KEY = "tracks/year=2026/month=05/day=01"


def _day_dir(tracks_dir: Path, day: date = _TRACKS_DAY) -> Path:
    return tracks_dir / f"year={day:%Y}" / f"month={day:%m}" / f"day={day:%d}"


def _utc_days_ago(days: int) -> date:
    return datetime.now(UTC).date() - timedelta(days=days)


def _track_part(tracks_dir: Path, name: str, age_days: float = 0.0, day: date = _TRACKS_DAY) -> Path:
    """A raw tracks part of real Parquet in ``day``'s partition: one solve at noon UTC, mtime ``age_days`` ago."""
    directory = _day_dir(tracks_dir, day)
    directory.mkdir(parents=True, exist_ok=True)
    noon_ms = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)
    solve = {"solve_ts_ms": noon_ms, "lat": 33.9, "lon": -82.39, "n_nodes": 2, "node_kind": "real"}
    path = directory / name
    pq.write_table(pa.table({f.name: [solve.get(f.name)] for f in SCHEMA}, schema=SCHEMA), path)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def _adsb_part(adsb_dir: Path, name: str, age_days: float = 0.0, day: date = _TRACKS_DAY) -> Path:
    """A raw ADS-B part of real Parquet in ``day``'s partition: one fix at noon UTC, mtime ``age_days`` ago."""
    directory = _day_dir(adsb_dir, day)
    directory.mkdir(parents=True, exist_ok=True)
    noon_ms = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)
    fix = {
        "ts_ms": noon_ms,
        "hex": "a1b2c3",
        "lat": 34.85,
        "lon": -82.39,
        "seen_ms": noon_ms - 500,
        "world": "real",
        "source": "node",
        "source_node_id": "ret1a2b3c4d",
    }
    path = directory / name
    pq.write_table(pa.table({f.name: [fix.get(f.name)] for f in adsb_writer.SCHEMA}, schema=adsb_writer.SCHEMA), path)
    mtime = time.time() - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


_ADSB_PART = f"part-120000-{'ab12cd34' * 4}.parquet"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sentinel(path: Path) -> Path:
    return path.with_name(path.name + ".uploaded")


def _raw_sentinels_refused():
    """Every raw file's sentinel touch fails, as on a full disk."""
    touch = Path.touch

    def refuse(path: Path, *args, **kwargs):
        if path.name.endswith(".uploaded"):
            raise OSError("No space left on device")
        return touch(path, *args, **kwargs)

    return unittest.mock.patch.object(Path, "touch", refuse)


def _compact_sentinels_refused():
    """Every compact's sentinel write fails, as on a full disk."""
    return unittest.mock.patch(
        "services.tasks.archive_lifecycle._write_sentinel", side_effect=OSError("No space left on device")
    )


def _compact_retention(days: int):
    """Patch TRACKS_COMPACT_RETENTION_DAYS, which the tracks store reads on every call, for one test."""
    return unittest.mock.patch("config.constants.TRACKS_COMPACT_RETENTION_DAYS", days)


def _adsb_compact_retention(days: int):
    """Patch ADSB_COMPACT_RETENTION_DAYS, which the ADS-B store reads on every call, for one test."""
    return unittest.mock.patch("config.constants.ADSB_COMPACT_RETENTION_DAYS", days)


# ══════════════════════════════════════════════════════════════════════════════
# Suite 1: moto-based integration tests
# ══════════════════════════════════════════════════════════════════════════════


class TestArchiveLifecycleMoto(unittest.TestCase):
    """Archive lifecycle integration tests using moto to mock S3."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._archive_dir = Path(self._tmpdir.name) / "archive"
        self._archive_dir.mkdir()
        self._tracks_dir = Path(self._tmpdir.name) / "tracks"
        self._tracks_dir.mkdir()
        self._adsb_dir = Path(self._tmpdir.name) / "adsb"
        self._adsb_dir.mkdir()

        import services.tasks.archive_lifecycle as alc

        self._orig_archive_dir = alc._ARCHIVE_DIR
        self._orig_tracks_dir = alc._TRACKS_DIR
        self._orig_adsb_dir = alc._ADSB_DIR
        alc._ARCHIVE_DIR = self._archive_dir
        # Redirect tracks/ too so the lifecycle doesn't sweep the real
        # coverage_data/tracks on the test host.
        alc._TRACKS_DIR = self._tracks_dir
        alc._ADSB_DIR = self._adsb_dir

        self.enterContext(store_env(FAKE_AWS_ENV))
        self.enterContext(mock_aws())

    def tearDown(self):
        import services.tasks.archive_lifecycle as alc

        alc._ARCHIVE_DIR = self._orig_archive_dir
        alc._TRACKS_DIR = self._orig_tracks_dir
        alc._ADSB_DIR = self._orig_adsb_dir

        self._tmpdir.cleanup()

    def test_old_file_is_uploaded_to_object_store(self):
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        self.assertEqual(stats["errors"], 0)

    def test_uploaded_file_content_in_object_store(self):
        _make_bucket()
        payload = b'{"node": "test", "detections": []}'
        _create_archive_file(
            self._archive_dir,
            age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1,
            content=payload,
        )
        from services.object_store import download_bytes, list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        run_archive_lifecycle()
        keys = list_keys("archive/")
        self.assertEqual(len(keys), 1)
        stored = download_bytes(keys[0])
        self.assertEqual(stored, payload)

    def test_fresh_file_not_uploaded(self):
        _make_bucket()
        _create_archive_file(self._archive_dir, age_days=0.5)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual(stats["uploaded"], 0)

    def test_very_old_file_deleted_locally(self):
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertFalse(f.exists())
        self.assertGreater(stats["deleted"], 0)

    def test_file_within_retention_not_deleted(self):
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            run_archive_lifecycle()
        self.assertTrue(f.exists())

    def test_old_file_uploaded_then_deleted(self):
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f.exists())
        self.assertEqual(len(list_keys("archive/")), 1)

    def test_empty_dirs_pruned_after_delete(self):
        _make_bucket()
        f = _create_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            run_archive_lifecycle()
        self.assertFalse(f.parent.exists())

    def test_no_upload_when_object_store_disabled(self):
        """When the object store is disabled, no upload happens AND no local deletion either.

        Local files are only deleted once they have an .uploaded sentinel,
        which is only written after a confirmed upload. Without the object store, the
        sentinel never appears, so the file must stay on disk indefinitely —
        otherwise we'd silently drop data with no backup at all.
        """
        self.enterContext(store_env({}))
        f_old = _create_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertEqual(stats["uploaded"], 0)
        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(
            f_old.exists(),
            "file must NOT be deleted when the object store never confirmed the upload",
        )

    def test_empty_archive_dir_returns_zero_stats(self):
        _make_bucket()
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0, "compacted": 0})

    def test_upload_cap_respected(self):
        _make_bucket()
        import services.tasks.archive_lifecycle as alc

        cap = alc._MAX_UPLOAD_PER_CYCLE
        for i in range(cap + 5):
            node_dir = self._archive_dir / "2025" / "01" / f"{i:02d}" / "node"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "frame.json"
            f.write_bytes(b"{}")
            mtime = time.time() - (ARCHIVE_OFFLOAD_AGE_DAYS + 1) * 86400
            os.utime(f, (mtime, mtime))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertLessEqual(stats["uploaded"], cap)

    def _make_tracks_parquet(self, age_days: float) -> Path:
        """Create a Hive-partitioned tracks/ Parquet file with the given age, in a complete day."""
        return _track_part(self._tracks_dir, "part-120000.parquet", age_days)

    def test_tracks_parquet_uploaded_under_tracks_prefix(self):
        """Track Parquet in tracks/ must be offloaded under the 'tracks/' object store prefix.

        The old iterator only walked archive/, so tracks/ Parquet accumulated
        on disk forever. It must now be swept like the detection archive.
        """
        _make_bucket()
        self._make_tracks_parquet(age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        # The day is complete, so its compact and sidecar go up beside the raw part.
        self.assertEqual(
            sorted(list_keys("tracks/")),
            [
                "tracks/year=2026/month=05/day=01/compact.json",
                "tracks/year=2026/month=05/day=01/compact.parquet",
                "tracks/year=2026/month=05/day=01/part-120000.parquet",
            ],
        )

    def test_tracks_parquet_deleted_past_retention(self):
        """Track Parquet past retention is deleted locally once confirmed in the object store."""
        _make_bucket()
        f = self._make_tracks_parquet(age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f.exists(), "track Parquet past retention must be deleted")

    @mock_aws
    def test_adsb_parquet_uploaded_under_adsb_prefix(self):
        """ADS-B snapshot Parquet in adsb/ is offloaded under the 'adsb/' R2 prefix, beside its day's compact."""
        _make_bucket()
        _adsb_part(self._adsb_dir, _ADSB_PART, ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertEqual((stats["compacted"], stats["uploaded"], stats["errors"]), (1, 2, 0))
        self.assertEqual(
            sorted(list_keys("adsb/")),
            [
                "adsb/year=2026/month=05/day=01/compact.json",
                "adsb/year=2026/month=05/day=01/compact.parquet",
                f"adsb/year=2026/month=05/day=01/{_ADSB_PART}",
            ],
        )

    @mock_aws
    def test_adsb_parquet_past_retention_is_deleted_only_once_superseded_by_a_safe_compact(self):
        """An uploaded ADS-B part past retention goes only once its day's compact holds its rows."""
        _make_bucket()
        superseded = _adsb_part(self._adsb_dir, _ADSB_PART, _TEST_RETENTION_DAYS + 1)
        # Not Parquet: the sweep keeps it raw, so no compact supersedes it.
        unreadable = _day_dir(self._adsb_dir) / f"part-130000-{'ef56ab78' * 4}.parquet"
        unreadable.write_bytes(b"PAR1")
        mtime = time.time() - (_TEST_RETENTION_DAYS + 1) * 86400
        os.utime(unreadable, (mtime, mtime))
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()

        self.assertEqual((stats["compacted"], stats["uploaded"], stats["deleted"]), (1, 3, 1))
        self.assertFalse(superseded.exists(), "an ADS-B part its uploaded compact holds must be deleted")
        self.assertTrue(unreadable.exists(), "an ADS-B part no compact holds must be kept")
        self.assertTrue(unreadable.with_name(unreadable.name + ".uploaded").exists())
        self.assertIn(f"adsb/year=2026/month=05/day=01/{_ADSB_PART}", list_keys("adsb/"))
        manifest = compaction.read_sidecar(str(_day_dir(self._adsb_dir)))
        assert manifest is not None
        self.assertEqual(
            (manifest.store, manifest.parts, manifest.unreadable), ("adsb", (_ADSB_PART,), (unreadable.name,))
        )

    @mock_aws
    def test_a_superseded_adsb_part_is_kept_while_its_compact_is_neither_on_disk_nor_uploaded(self):
        _make_bucket()
        part = _adsb_part(self._adsb_dir, _ADSB_PART, _TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        self._set_store_enabled(False)
        run_archive_lifecycle()
        (_day_dir(self._adsb_dir) / "compact.parquet").unlink()
        self._set_store_enabled(True)

        with _patch_retention():
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 0))
        self.assertTrue(part.exists())

    @mock_aws
    def test_an_adsb_compact_is_uploaded_as_parquet_in_the_cycle_that_makes_it_and_not_again(self):
        _make_bucket()
        _adsb_part(self._adsb_dir, _ADSB_PART)
        from services.object_store import download_bytes, list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()

        key = "adsb/year=2026/month=05/day=01"
        self.assertEqual((stats["compacted"], stats["uploaded"], stats["errors"]), (1, 1, 0))
        self.assertEqual(sorted(list_keys("adsb/")), [f"{key}/compact.json", f"{key}/compact.parquet"])
        s3 = boto3.client("s3", region_name="us-east-1")
        head = s3.head_object(Bucket="retina-data", Key=f"{key}/compact.parquet")
        self.assertEqual(head["ContentType"], "application/vnd.apache.parquet")
        head = s3.head_object(Bucket="retina-data", Key=f"{key}/compact.json")
        self.assertEqual(head["ContentType"], "application/json")
        compact = _day_dir(self._adsb_dir) / "compact.parquet"
        self.assertEqual(download_bytes(f"{key}/compact.parquet"), compact.read_bytes())
        self.assertEqual(compact.with_name("compact.parquet.uploaded").read_text(), _sha256(compact))

        with unittest.mock.patch("services.object_store.upload_file", wraps=object_store.upload_file) as upload:
            again = run_archive_lifecycle()

        upload.assert_not_called()
        self.assertEqual((again["compacted"], again["uploaded"]), (0, 0))

    @mock_aws
    def test_an_adsb_compact_is_kept_for_the_adsb_retention_whatever_the_tracks_one(self):
        _make_bucket()
        day = _utc_days_ago(60)
        adsb_part = _adsb_part(self._adsb_dir, _ADSB_PART, day=day)
        tracks_part = _track_part(self._tracks_dir, "part-110000.parquet", day=day)
        adsb_compact = _day_dir(self._adsb_dir, day) / "compact.parquet"
        tracks_compact = _day_dir(self._tracks_dir, day) / "compact.parquet"
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        # The lifecycle deleted the superseded parts in an earlier cycle.
        run_archive_lifecycle()
        adsb_part.unlink()
        tracks_part.unlink()

        with _adsb_compact_retention(0), _compact_retention(30):
            stats = run_archive_lifecycle()
        self.assertEqual(stats["deleted"], 1)
        self.assertTrue(adsb_compact.exists())
        self.assertFalse(tracks_compact.exists())

        with _adsb_compact_retention(30), _compact_retention(0):
            stats = run_archive_lifecycle()
        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(adsb_compact.exists())
        # Its sidecar and sentinel stay, for a part found later.
        self.assertEqual(
            sorted(p.name for p in adsb_compact.parent.iterdir()), ["compact.json", "compact.parquet.uploaded"]
        )

    # ── Compacted tracks days ──────────────────────────────────────────────────

    def _set_store_enabled(self, enabled: bool):
        self.enterContext(store_env(FAKE_AWS_ENV if enabled else {}))

    def _compacted_day(self, day: date) -> Path:
        """``day``'s partition, compacted from one raw part since deleted, as the lifecycle deletes superseded parts."""
        part = _track_part(self._tracks_dir, "part-110000.parquet", day=day)
        self.assertEqual(compaction.sweep(compaction.TRACKS, str(self._tracks_dir))["compacted"], 1)
        part.unlink()
        return _day_dir(self._tracks_dir, day)

    @mock_aws
    def test_a_compact_is_uploaded_in_the_cycle_that_makes_it_and_not_again(self):
        _make_bucket()
        _track_part(self._tracks_dir, "part-110000.parquet")
        _track_part(self._tracks_dir, "part-120000.parquet")
        from services.object_store import download_bytes, list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()

        self.assertEqual((stats["compacted"], stats["uploaded"], stats["errors"]), (1, 1, 0))
        self.assertEqual(sorted(list_keys("tracks/")), [f"{_DAY_KEY}/compact.json", f"{_DAY_KEY}/compact.parquet"])
        s3 = boto3.client("s3", region_name="us-east-1")
        head = s3.head_object(Bucket="retina-data", Key=f"{_DAY_KEY}/compact.parquet")
        self.assertEqual(head["ContentType"], "application/vnd.apache.parquet")
        head = s3.head_object(Bucket="retina-data", Key=f"{_DAY_KEY}/compact.json")
        self.assertEqual(head["ContentType"], "application/json")
        directory = _day_dir(self._tracks_dir)
        compact = directory / "compact.parquet"
        self.assertEqual(download_bytes(f"{_DAY_KEY}/compact.parquet"), compact.read_bytes())
        sha = _sha256(compact)
        self.assertEqual(json.loads((directory / "compact.json").read_text())["sha256"], sha)
        self.assertEqual((directory / "compact.parquet.uploaded").read_text(), sha)

        with unittest.mock.patch("services.object_store.upload_file", wraps=object_store.upload_file) as upload:
            again = run_archive_lifecycle()

        upload.assert_not_called()
        self.assertEqual((again["compacted"], again["uploaded"]), (0, 0))

    @mock_aws
    def test_a_compact_rewritten_after_a_late_part_is_uploaded_again(self):
        _make_bucket()
        _track_part(self._tracks_dir, "part-110000.parquet")
        from services.object_store import download_bytes
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        run_archive_lifecycle()
        compact = _day_dir(self._tracks_dir) / "compact.parquet"
        first = compact.read_bytes()
        _track_part(self._tracks_dir, "part-130000.parquet")

        stats = run_archive_lifecycle()

        self.assertEqual((stats["compacted"], stats["uploaded"]), (1, 1))
        rewritten = compact.read_bytes()
        self.assertNotEqual(rewritten, first)
        self.assertEqual(download_bytes(f"{_DAY_KEY}/compact.parquet"), rewritten)
        self.assertEqual(compact.with_name("compact.parquet.uploaded").read_text(), _sha256(compact))

    @mock_aws
    def test_a_sidecar_whose_size_disagrees_with_the_compact_blocks_its_upload(self):
        _make_bucket()
        _track_part(self._tracks_dir, "part-110000.parquet")
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        self._set_store_enabled(False)
        run_archive_lifecycle()
        self._set_store_enabled(True)
        directory = _day_dir(self._tracks_dir)
        manifest = compaction.read_sidecar(str(directory))
        assert manifest is not None and manifest.size is not None
        compaction.write_sidecar(str(directory), replace(manifest, size=manifest.size + 1))

        # A sweep that has not repaired the sidecar.
        with unittest.mock.patch("services.compaction.sweep", return_value={"compacted": 0}):
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["skipped"]), (0, 1))
        self.assertEqual(list_keys("tracks/"), [])
        self.assertFalse((directory / "compact.parquet.uploaded").exists())

        # The next sweep rewrites the sidecar from the compact's footer, and the compact goes up.
        self.assertEqual(run_archive_lifecycle()["uploaded"], 1)
        self.assertIn(f"{_DAY_KEY}/compact.parquet", list_keys("tracks/"))

    @mock_aws
    def test_a_compact_younger_than_its_retention_is_kept(self):
        _make_bucket()
        directory = self._compacted_day(_utc_days_ago(5))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30):
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 0))
        self.assertTrue((directory / "compact.parquet").exists())

    @mock_aws
    def test_an_uploaded_compact_past_its_retention_is_deleted_and_its_sidecar_and_sentinel_kept(self):
        _make_bucket()
        day = _utc_days_ago(60)
        directory = self._compacted_day(day)
        sha = _sha256(directory / "compact.parquet")
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30):
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 1))
        self.assertFalse((directory / "compact.parquet").exists())
        self.assertTrue((directory / "compact.json").exists())
        # It still records that the object store holds the compact the sidecar describes.
        self.assertEqual((directory / "compact.parquet.uploaded").read_text(), sha)
        self.assertIn(f"tracks/year={day:%Y}/month={day:%m}/day={day:%d}/compact.parquet", list_keys("tracks/"))

    @mock_aws
    def test_a_compact_json_torn_while_its_compact_goes_up_leaves_the_store_the_checked_manifest(self):
        _make_bucket()
        day = _utc_days_ago(60)
        directory = self._compacted_day(day)
        compact = directory / "compact.parquet"
        sidecar = directory / "compact.json"
        manifest = sidecar.read_bytes()
        upload_file = object_store.upload_file

        def tearing_the_sidecar(key: str, path: str, content_type: str) -> bool:
            sent = upload_file(key, path, content_type=content_type)
            if key.endswith("/compact.parquet"):
                # Outside corruption tears the local compact.json while the compact goes up.
                sidecar.write_bytes(manifest[: len(manifest) // 2])
            return sent

        from services.object_store import download_bytes
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30):
            with unittest.mock.patch("services.object_store.upload_file", tearing_the_sidecar):
                first = run_archive_lifecycle()

            self.assertEqual(download_bytes(f"tracks/year={day:%Y}/month={day:%m}/day={day:%d}/compact.json"), manifest)
            self.assertEqual((first["uploaded"], first["deleted"]), (1, 0))
            self.assertTrue(compact.exists())

            # The next sweep rewrites compact.json from the compact's footer, and the compact goes.
            second = run_archive_lifecycle()

        self.assertEqual(second["deleted"], 1)
        self.assertFalse(compact.exists())
        self.assertEqual(sidecar.read_bytes(), manifest)

        # The day reads exactly from the store's compact, checked against compact.json.
        import services.tasks.track_archive as track_archive
        from services import cold_store, tracks_store

        noon_ms = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)
        with (
            unittest.mock.patch.object(track_archive, "_TRACKS_DIR", str(self._tracks_dir)),
            unittest.mock.patch.object(cold_store, "_CACHE_DIR", str(Path(self._tmpdir.name) / "cold_cache")),
        ):
            cold_store._reset_for_tests()
            tracks_store._reset_for_tests()
            try:
                body = tracks_store.query_tracks(
                    noon_ms - 3_600_000, noon_ms + 3_600_000, fleet="all", private=frozenset()
                )
            finally:
                cold_store._reset_for_tests()
                tracks_store._reset_for_tests()
        self.assertEqual([row["lat"] for row in body["rows"]], [33.9])

    def test_a_compact_past_its_retention_is_kept_while_it_is_not_uploaded(self):
        directory = self._compacted_day(_utc_days_ago(60))
        # A sentinel of an earlier compact of the day; this one's upload fails.
        (directory / "compact.parquet.uploaded").write_text("0" * 64)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30), unittest.mock.patch("services.object_store.upload_file", return_value=False):
            stats = run_archive_lifecycle()

        self.assertEqual((stats["errors"], stats["deleted"]), (1, 0))
        self.assertTrue((directory / "compact.parquet").exists())

    @mock_aws
    def test_a_compact_is_kept_forever_when_its_retention_is_zero(self):
        _make_bucket()
        directory = self._compacted_day(date(2001, 1, 1))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(0):
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 0))
        self.assertTrue((directory / "compact.parquet").exists())

    @mock_aws
    def test_a_compact_past_its_retention_is_kept_until_the_raw_parts_of_its_day_are_gone(self):
        _make_bucket()
        day = _utc_days_ago(60)
        parts = [
            _track_part(self._tracks_dir, f"part-{hour}0000.parquet", _TEST_RETENTION_DAYS + 1, day)
            for hour in (10, 11, 12)
        ]
        compact = _day_dir(self._tracks_dir, day) / "compact.parquet"
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        # Two uploads a cycle: the compact and one raw part go up in the first, and the other parts wait.
        with (
            _compact_retention(30),
            _patch_retention(),
            unittest.mock.patch("services.tasks.archive_lifecycle._MAX_UPLOAD_PER_CYCLE", 2),
        ):
            first = run_archive_lifecycle()
            self.assertEqual((first["uploaded"], first["deleted"], first["skipped"]), (2, 1, 1))
            self.assertTrue(compact.exists())
            self.assertEqual([part.exists() for part in parts], [False, True, True])
            self.assertFalse(parts[2].with_name("part-120000.parquet.uploaded").exists())

            # The compact is visited before the parts of its partition, so it waits one more cycle.
            second = run_archive_lifecycle()
            self.assertEqual((second["uploaded"], second["deleted"], second["skipped"]), (2, 2, 1))
            self.assertTrue(compact.exists())
            self.assertFalse(any(part.exists() for part in parts))

            third = run_archive_lifecycle()

        self.assertEqual((third["uploaded"], third["deleted"], third["skipped"]), (0, 1, 0))
        self.assertFalse(compact.exists())

    @mock_aws
    def test_a_compact_is_kept_while_its_day_keeps_a_raw_part_that_cannot_be_read(self):
        _make_bucket()
        day = _utc_days_ago(60)
        _track_part(self._tracks_dir, "part-110000.parquet", _TEST_RETENTION_DAYS + 1, day)
        directory = _day_dir(self._tracks_dir, day)
        # Not Parquet: the sweep lists it unreadable and keeps it raw, so no compact supersedes it.
        unreadable = directory / "part-120000.parquet"
        unreadable.write_bytes(b"PAR1")
        mtime = time.time() - (_TEST_RETENTION_DAYS + 1) * 86400
        os.utime(unreadable, (mtime, mtime))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30), _patch_retention():
            cycles = [run_archive_lifecycle() for _ in range(3)]

        self.assertEqual(
            [(stats["uploaded"], stats["deleted"], stats["skipped"]) for stats in cycles],
            [(3, 1, 1), (0, 0, 1), (0, 0, 1)],
        )
        self.assertTrue((directory / "compact.parquet").exists())
        self.assertTrue(unreadable.exists())
        manifest = compaction.read_sidecar(str(directory))
        assert manifest is not None
        self.assertEqual(manifest.unreadable, ("part-120000.parquet",))

    @mock_aws
    def test_a_compact_retention_shorter_than_the_raw_one_deletes_the_raw_parts_first_and_the_compact_after(self):
        _make_bucket()
        day = _utc_days_ago(3)
        parts = [_track_part(self._tracks_dir, f"part-{hour}0000.parquet", 2.5, day) for hour in (11, 12)]
        directory = _day_dir(self._tracks_dir, day)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(1), _patch_retention():
            # Its day ended over a day ago; its raw parts are younger than _TEST_RETENTION_DAYS.
            first = run_archive_lifecycle()
            self.assertEqual((first["uploaded"], first["deleted"]), (3, 0))
            self.assertTrue((directory / "compact.parquet").exists())

            mtime = time.time() - (_TEST_RETENTION_DAYS + 1) * 86400
            for part in parts:
                os.utime(part, (mtime, mtime))
            second = run_archive_lifecycle()
            self.assertEqual(second["deleted"], 2)
            self.assertTrue((directory / "compact.parquet").exists())

            third = run_archive_lifecycle()

        self.assertEqual(third["deleted"], 1)
        # Nothing superseded is left behind; the sidecar and the compact's sentinel stay.
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()), ["compact.json", "compact.parquet.uploaded"]
        )

    @mock_aws
    def test_a_superseded_raw_part_found_after_its_compact_left_local_disk_is_deleted(self):
        _make_bucket()
        day = _utc_days_ago(60)
        directory = self._compacted_day(day)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _compact_retention(30):
            run_archive_lifecycle()
        self.assertFalse((directory / "compact.parquet").exists())
        # A raw part the compact holds, back on local disk (restored by hand, say).
        part = _track_part(self._tracks_dir, "part-110000.parquet", _TEST_RETENTION_DAYS + 1, day)

        with _compact_retention(30), _patch_retention():
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 1))
        self.assertFalse(part.exists())

    @mock_aws
    def test_an_uploaded_raw_part_past_retention_is_deleted_only_once_superseded(self):
        _make_bucket()
        superseded = _track_part(self._tracks_dir, "part-110000.parquet", age_days=_TEST_RETENTION_DAYS + 1)
        # Not Parquet: the sweep keeps it raw, so no compact supersedes it.
        unreadable = _day_dir(self._tracks_dir) / "part-120000.parquet"
        unreadable.write_bytes(b"PAR1")
        mtime = time.time() - (_TEST_RETENTION_DAYS + 1) * 86400
        os.utime(unreadable, (mtime, mtime))
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()

        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(superseded.exists())
        self.assertTrue(unreadable.exists())
        self.assertTrue(unreadable.with_name("part-120000.parquet.uploaded").exists())

    @mock_aws
    def test_a_superseded_raw_part_is_kept_while_its_compact_is_neither_on_disk_nor_uploaded(self):
        _make_bucket()
        part = _track_part(self._tracks_dir, "part-110000.parquet", age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        self._set_store_enabled(False)
        run_archive_lifecycle()
        (_day_dir(self._tracks_dir) / "compact.parquet").unlink()
        self._set_store_enabled(True)

        with _patch_retention():
            stats = run_archive_lifecycle()

        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 0))
        self.assertTrue(part.exists())
        self.assertTrue(part.with_name("part-110000.parquet.uploaded").exists())


# ══════════════════════════════════════════════════════════════════════════════
# Suite 2: unit tests for error paths and edge cases (no moto required)
# ══════════════════════════════════════════════════════════════════════════════


class _ArchiveUnitTestBase(unittest.TestCase):
    """Shared setUp/tearDown for unit-test classes that need a scratch archive dir."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._archive_dir = Path(self._tmpdir.name) / "archive"
        self._archive_dir.mkdir()
        # Isolate the tracks/ root too, so the lifecycle never touches the
        # real coverage_data/tracks on the test host.
        self._tracks_dir = Path(self._tmpdir.name) / "tracks"
        self._tracks_dir.mkdir()
        self._adsb_dir = Path(self._tmpdir.name) / "adsb"
        self._adsb_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, store_enabled: bool = True, upload_return: bool = True):
        """Run archive lifecycle with the object store fully mocked.

        Defaults reflect the happy path (object store reachable, uploads succeed) so a
        test exercising the deletion phase doesn't have to repeat that setup.
        Tests of the object-store-disabled or upload-failure paths pass explicit args.
        """
        return self._run_listing_uploads(store_enabled, upload_return)[0]

    def _run_listing_uploads(self, store_enabled: bool = True, upload_return: bool = True) -> tuple[dict, list[str]]:
        """As _run, with the object keys the cycle sent, files and bytes alike, in the order it sent them."""
        sent: list[str] = []

        def upload(key: str, *args, **kwargs) -> bool:
            sent.append(key)
            return upload_return

        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=self._tracks_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
        ):
            with unittest.mock.patch("services.object_store.is_enabled", return_value=store_enabled):
                with (
                    unittest.mock.patch("services.object_store.upload_file", upload),
                    unittest.mock.patch("services.object_store.upload_bytes", upload),
                ):
                    from services.tasks.archive_lifecycle import run_archive_lifecycle

                    stats = run_archive_lifecycle()
        return stats, sent


# ── TestRunArchiveLifecycleEmptyDir ───────────────────────────────────────────


class TestRunArchiveLifecycleEmptyDir(unittest.TestCase):
    """_ARCHIVE_DIR does not exist → all-zero stats, no crash."""

    def test_nonexistent_dir_returns_zero_stats(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "no_such_archive"
            missing_tracks = Path(td) / "no_such_tracks"
            missing_adsb = Path(td) / "no_such_adsb"
            # neither is created

            with (
                unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=missing),
                unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=missing_tracks),
                unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=missing_adsb),
            ):
                from services.tasks.archive_lifecycle import run_archive_lifecycle

                with unittest.mock.patch("services.object_store.is_enabled", return_value=False):
                    with unittest.mock.patch("services.object_store.upload_file", return_value=False):
                        stats = run_archive_lifecycle()

        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0, "compacted": 0})


# ── TestRunArchiveLifecycleOSError ────────────────────────────────────────────


class TestRunArchiveLifecycleOSError(_ArchiveUnitTestBase):
    """OSError paths: stat() skip, unlink() error."""

    def test_stat_oserror_skips_file_no_error_counted(self):
        """When stat() raises OSError, the file is silently skipped."""
        _make_archive_file(self._archive_dir, age_days=30.0)

        original_stat = Path.stat

        def _bad_stat(path, *args, **kwargs):
            if path.suffix == ".json":
                raise OSError("fake stat error")
            return original_stat(path, *args, **kwargs)

        with unittest.mock.patch.object(Path, "stat", _bad_stat):
            stats = self._run()

        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["uploaded"], 0)
        self.assertEqual(stats["deleted"], 0)

    def test_unlink_oserror_increments_errors(self):
        """When unlink() raises OSError on a file past retention, errors is incremented."""
        age = _TEST_RETENTION_DAYS + 1
        _make_archive_file(self._archive_dir, age_days=age)

        original_unlink = Path.unlink

        def _bad_unlink(path, missing_ok=False):
            if path.suffix == ".json":
                raise OSError("fake unlink error")
            return original_unlink(path, missing_ok=missing_ok)

        with unittest.mock.patch.object(Path, "unlink", _bad_unlink), _patch_retention():
            stats = self._run()

        self.assertGreater(stats["errors"], 0)
        self.assertEqual(stats["deleted"], 0)

    def test_upload_failure_increments_errors(self):
        """When store_upload() returns False (upload failed silently), errors is incremented."""
        _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(store_enabled=True, upload_return=False)

        self.assertGreater(stats["errors"], 0)
        self.assertEqual(stats["uploaded"], 0)


# ── TestRunArchiveLifecycleCap ────────────────────────────────────────────────


class TestRunArchiveLifecycleCap(_ArchiveUnitTestBase):
    """When uploaded+deleted >= _MAX_DELETE_PER_CYCLE, extras get skipped."""

    def test_files_beyond_cap_are_skipped(self):
        import services.tasks.archive_lifecycle as alc

        cap = alc._MAX_DELETE_PER_CYCLE

        age = _TEST_RETENTION_DAYS + 1
        for i in range(cap + 3):
            node_dir = self._archive_dir / "2024" / "01" / "15" / f"node-{i}"
            node_dir.mkdir(parents=True, exist_ok=True)
            f = node_dir / "data.json"
            f.write_bytes(b"{}")
            mtime = time.time() - age * 86400
            os.utime(f, (mtime, mtime))
            # Pre-mark as uploaded so the deletion phase has work to do all
            # the way to the cap (otherwise we'd hit the upload sub-cap of
            # _MAX_UPLOAD_PER_CYCLE first and stop short).
            f.with_name(f.name + ".uploaded").touch()

        with _patch_retention():
            stats = self._run()

        self.assertGreater(stats["skipped"], 0)
        self.assertLessEqual(stats["uploaded"] + stats["deleted"], cap)

    def _two_compacted_days(self):
        """The first day with three raw parts old enough to go up; the second with one too fresh to."""
        for hour in (10, 11, 12):
            _track_part(self._tracks_dir, f"part-{hour}0000.parquet", ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        _track_part(self._tracks_dir, "part-100000.parquet", day=_TRACKS_DAY + timedelta(days=1))

    def test_the_upload_cap_counts_compacts_and_raw_parts_sent_whose_sentinel_cannot_be_written(self):
        self._two_compacted_days()

        with (
            _compact_sentinels_refused(),
            _raw_sentinels_refused(),
            unittest.mock.patch("services.tasks.archive_lifecycle._MAX_UPLOAD_PER_CYCLE", 3),
        ):
            stats, uploads = self._run_listing_uploads()

        # The first day's compact and two of its parts are three uploads; the second day's compact waits.
        self.assertEqual(
            uploads,
            [
                f"{_DAY_KEY}/compact.parquet",
                f"{_DAY_KEY}/compact.json",
                f"{_DAY_KEY}/part-100000.parquet",
                f"{_DAY_KEY}/part-110000.parquet",
            ],
        )
        self.assertEqual((stats["compacted"], stats["uploaded"], stats["errors"]), (2, 0, 3))

    def test_the_work_cap_counts_uploads_sent_whose_sentinel_cannot_be_written(self):
        self._two_compacted_days()

        with (
            _compact_sentinels_refused(),
            _raw_sentinels_refused(),
            unittest.mock.patch("services.tasks.archive_lifecycle._MAX_DELETE_PER_CYCLE", 2),
        ):
            stats, uploads = self._run_listing_uploads()

        self.assertEqual(
            uploads, [f"{_DAY_KEY}/compact.parquet", f"{_DAY_KEY}/compact.json", f"{_DAY_KEY}/part-100000.parquet"]
        )
        self.assertEqual((stats["uploaded"], stats["errors"], stats["skipped"]), (0, 2, 4))


# ── TestPruneEmptyDirs ────────────────────────────────────────────────────────


class TestPruneEmptyDirs(unittest.TestCase):
    """_prune_empty_dirs behaviour: empty leaf removed, file-containing dir kept, base kept."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name) / "base"
        self._base.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_leaf_dir_is_removed(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        leaf = self._base / "2024" / "01" / "15"
        leaf.mkdir(parents=True)

        _prune_empty_dirs(self._base)

        self.assertFalse(leaf.exists(), "Empty leaf dir should be pruned")

    def test_dir_with_files_is_kept(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        leaf = self._base / "2024" / "01" / "15"
        leaf.mkdir(parents=True)
        (leaf / "data.json").write_bytes(b"{}")

        _prune_empty_dirs(self._base)

        self.assertTrue(leaf.exists(), "Dir containing files should NOT be pruned")
        self.assertTrue((leaf / "data.json").exists())

    def test_base_dir_is_never_removed(self):
        from services.tasks.archive_lifecycle import _prune_empty_dirs

        _prune_empty_dirs(self._base)

        self.assertTrue(self._base.exists(), "Base directory must never be removed")


# ── TestIterArchiveFiles ──────────────────────────────────────────────────────


class TestIterArchiveFiles(_ArchiveUnitTestBase):
    """_iter_archive_files: non-dirs skipped, OSError doesn't crash."""

    def _collect(self):
        """Return just the file paths yielded for the archive/ root.

        _iter_archive_files now yields (file, root_dir, store_prefix) tuples and
        sweeps both roots; these tests only assert on archive/ files, so we
        point tracks/ at an empty temp dir and unwrap the file from each tuple.
        """
        from services.tasks.archive_lifecycle import _iter_archive_files

        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=self._tracks_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
        ):
            return [f for f, _root, _prefix in _iter_archive_files()]

    def test_non_dir_entries_at_each_level_are_skipped(self):
        """Files placed at year/month/day/node level (not in a dir) are not yielded."""
        (self._archive_dir / "notadir.json").write_bytes(b"{}")
        _make_archive_file(self._archive_dir, name="valid.json")

        results = self._collect()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "valid.json")

    def test_parquet_and_json_files_are_yielded(self):
        """Both .parquet and .json files in the node dir are yielded; .txt is ignored."""
        node_dir = self._archive_dir / "2024" / "01" / "15" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "data.json").write_bytes(b"{}")
        (node_dir / "part-120000.parquet").write_bytes(b"")
        (node_dir / "data.txt").write_bytes(b"ignore me")

        results = self._collect()
        names = sorted(p.name for p in results)
        self.assertEqual(names, ["data.json", "part-120000.parquet"])

    def test_oserror_during_iteration_does_not_crash(self):
        """If iterdir() raises OSError, _iter_archive_files returns empty gracefully."""
        _make_archive_file(self._archive_dir)

        with unittest.mock.patch.object(
            Path,
            "iterdir",
            side_effect=OSError("fake iterdir failure"),
        ):
            results = self._collect()

        self.assertEqual(results, [])

    def test_hive_roots_come_from_archive_roots(self):
        """The roots walked with the recursive glob are the ones _archive_roots lists, the detection archive aside."""
        from services.tasks.archive_lifecycle import _iter_archive_files

        extra = Path(self._tmpdir.name) / "extra"
        listed = extra / "year=2024" / "month=01" / "day=15" / "part-120000.parquet"
        listed.parent.mkdir(parents=True)
        listed.write_bytes(b"")
        unlisted = _day_dir(self._adsb_dir) / "part-120000.parquet"
        unlisted.parent.mkdir(parents=True)
        unlisted.write_bytes(b"")

        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch(
                "services.tasks.archive_lifecycle._archive_roots",
                return_value=[(self._archive_dir, "archive"), (extra, "extra")],
            ),
        ):
            self.assertEqual(list(_iter_archive_files()), [(listed, extra, "extra")])


# ── TestWriteSentinel ─────────────────────────────────────────────────────────


class TestWriteSentinel(unittest.TestCase):
    def test_file_and_directory_are_fsynced_before_returning(self):
        from services.tasks.archive_lifecycle import _write_sentinel

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            compact = directory / "compact.parquet"
            fsynced = []
            real_fsync = os.fsync

            def record(fd):
                st = os.fstat(fd)
                kind = "dir" if stat.S_ISDIR(st.st_mode) else "file"
                fsynced.append((kind, st.st_ino, _sentinel(compact).exists()))
                real_fsync(fd)

            with unittest.mock.patch("os.fsync", side_effect=record):
                _write_sentinel(compact, "ab" * 32)

            sentinel = _sentinel(compact)
            self.assertEqual(sentinel.read_text(), "ab" * 32)
            self.assertEqual(
                fsynced,
                [("file", sentinel.stat().st_ino, False), ("dir", directory.stat().st_ino, True)],
            )

    def test_failed_write_removes_the_tmp_file_and_raises(self):
        from services.tasks.archive_lifecycle import _write_sentinel

        with tempfile.TemporaryDirectory() as tmp:
            compact = Path(tmp) / "compact.parquet"
            with (
                unittest.mock.patch("os.fsync", side_effect=OSError("No space left on device")),
                self.assertRaises(OSError),
            ):
                _write_sentinel(compact, "ab" * 32)

            self.assertEqual(list(Path(tmp).iterdir()), [])


# ── TestRetentionDisabled ─────────────────────────────────────────────────────


class TestRetentionDisabled(_ArchiveUnitTestBase):
    """When ARCHIVE_RETENTION_DAYS <= 0 the deletion phase is skipped entirely."""

    def test_old_file_kept_when_retention_zero(self):
        f = _make_archive_file(self._archive_dir, age_days=400.0)

        with unittest.mock.patch("services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", 0):
            stats = self._run()

        self.assertTrue(f.exists(), "file must NOT be deleted when retention is 0")
        self.assertEqual(stats["deleted"], 0)

    def test_old_file_kept_when_retention_negative(self):
        f = _make_archive_file(self._archive_dir, age_days=400.0)

        with unittest.mock.patch("services.tasks.archive_lifecycle.ARCHIVE_RETENTION_DAYS", -1):
            stats = self._run()

        self.assertTrue(f.exists(), "negative retention must also disable deletion")
        self.assertEqual(stats["deleted"], 0)

    def test_old_file_deleted_when_retention_positive(self):
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        with _patch_retention():
            stats = self._run()

        self.assertFalse(f.exists(), "file must be deleted when retention is positive")
        self.assertEqual(stats["deleted"], 1)


# ── TestUploadSentinel ────────────────────────────────────────────────────────


class TestUploadSentinel(_ArchiveUnitTestBase):
    """Sentinel file (.uploaded) gates local deletion on confirmed upload."""

    def test_sentinel_created_on_successful_upload(self):
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(store_enabled=True, upload_return=True)

        self.assertEqual(stats["uploaded"], 1)
        sentinel = f.with_name(f.name + ".uploaded")
        self.assertTrue(sentinel.exists(), "sentinel must be written after the upload")

    def test_sentinel_not_created_on_failed_upload(self):
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        stats = self._run(store_enabled=True, upload_return=False)

        self.assertEqual(stats["uploaded"], 0)
        self.assertGreater(stats["errors"], 0)
        sentinel = f.with_name(f.name + ".uploaded")
        self.assertFalse(sentinel.exists(), "sentinel must NOT exist on upload failure")

    def test_a_file_sent_whose_sentinel_cannot_be_written_is_an_error_not_an_upload(self):
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        with _raw_sentinels_refused():
            stats, uploads = self._run_listing_uploads()

        self.assertEqual(uploads, ["archive/2024/01/15/node-A/data.json"])
        self.assertEqual((stats["uploaded"], stats["errors"]), (0, 1))
        self.assertFalse(_sentinel(f).exists())

    def test_a_cycle_whose_only_event_is_an_error_logs_its_tally(self):
        _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        with _raw_sentinels_refused(), self.assertLogs("services.tasks.archive_lifecycle", level="INFO") as logs:
            self._run()

        self.assertIn(
            "Archive lifecycle: uploaded=0 deleted=0 errors=1 skipped=0 compacted=0",
            [record.getMessage() for record in logs.records],
        )

    def test_a_compact_sent_whose_sentinel_cannot_be_written_is_an_error_not_an_upload(self):
        # Too fresh to go up itself: only its day's compact does.
        _track_part(self._tracks_dir, "part-110000.parquet")

        with _compact_sentinels_refused():
            stats, uploads = self._run_listing_uploads()

        self.assertEqual(uploads, [f"{_DAY_KEY}/compact.parquet", f"{_DAY_KEY}/compact.json"])
        self.assertEqual((stats["compacted"], stats["uploaded"], stats["errors"]), (1, 0, 1))
        self.assertFalse(_sentinel(_day_dir(self._tracks_dir) / "compact.parquet").exists())

    def test_existing_sentinel_skips_re_upload(self):
        """A second cycle must not waste a PUT on a file already uploaded."""
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        f.with_name(f.name + ".uploaded").touch()

        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=self._tracks_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
            unittest.mock.patch("services.object_store.is_enabled", return_value=True),
            unittest.mock.patch("services.object_store.upload_file", return_value=True) as mock_upload,
            unittest.mock.patch("services.object_store.is_enabled", return_value=True),
            unittest.mock.patch("services.object_store.upload_file", return_value=True) as mock_upload,
        ):
            from services.tasks.archive_lifecycle import run_archive_lifecycle

            stats = run_archive_lifecycle()

        mock_upload.assert_not_called()
        self.assertEqual(stats["uploaded"], 0)

    def test_file_without_sentinel_not_deleted(self):
        """A file past retention but lacking a sentinel must NOT be deleted."""
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        # Store disabled — sentinel will never be created, simulating a deployment
        # where the object store is misconfigured but RETENTION_DAYS was set positive.
        with _patch_retention():
            stats = self._run(store_enabled=False, upload_return=False)

        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(f.exists(), "file without sentinel must survive lifecycle")

    def test_sentinel_removed_with_file(self):
        """When the file is deleted, its sentinel must go too (no orphan)."""
        f = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)

        with _patch_retention():
            stats = self._run(store_enabled=True, upload_return=True)

        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(f.exists())
        self.assertFalse(
            f.with_name(f.name + ".uploaded").exists(),
            "sentinel must be removed alongside its file",
        )


# ── TestTracksCompaction ──────────────────────────────────────────────────────


class TestTracksCompaction(_ArchiveUnitTestBase):
    """The compaction step that starts every cycle."""

    def test_a_host_without_an_object_store_compacts_and_uploads_or_deletes_nothing(self):
        parts = [
            _track_part(self._tracks_dir, name, _TEST_RETENTION_DAYS + 1)
            for name in ("part-110000.parquet", "part-120000.parquet")
        ]
        parts.append(_adsb_part(self._adsb_dir, _ADSB_PART, _TEST_RETENTION_DAYS + 1))

        with (
            _patch_retention(),
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=self._tracks_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
            unittest.mock.patch("services.object_store.is_enabled", return_value=False),
            unittest.mock.patch("services.object_store.upload_file", return_value=True) as upload,
        ):
            from services.tasks.archive_lifecycle import run_archive_lifecycle

            stats = run_archive_lifecycle()

        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0, "compacted": 2})
        upload.assert_not_called()
        for root, names in (
            (self._tracks_dir, ["part-110000.parquet", "part-120000.parquet"]),
            (self._adsb_dir, [_ADSB_PART]),
        ):
            directory = _day_dir(root)
            self.assertTrue((directory / "compact.parquet").exists())
            sidecar = json.loads((directory / "compact.json").read_text())
            self.assertEqual(sidecar["parts"], names)
            self.assertEqual(list(directory.glob("*.uploaded")), [])
        self.assertTrue(all(part.exists() for part in parts))

    def test_a_sweep_that_raises_does_not_stop_the_uploads_and_deletions_of_the_cycle(self):
        archived = _make_archive_file(self._archive_dir, age_days=_TEST_RETENTION_DAYS + 1)
        part = _track_part(self._tracks_dir, "part-110000.parquet", ARCHIVE_OFFLOAD_AGE_DAYS + 1)

        with (
            _patch_retention(),
            unittest.mock.patch("services.compaction.sweep", side_effect=RuntimeError("disk failure")),
            self.assertLogs("services.tasks.archive_lifecycle", level="ERROR"),
        ):
            stats = self._run()

        self.assertEqual((stats["uploaded"], stats["deleted"], stats["compacted"]), (2, 1, 0))
        self.assertFalse(archived.exists())
        self.assertTrue(part.with_name("part-110000.parquet.uploaded").exists())


# ── TestSupersededPartDeletion ────────────────────────────────────────────────


class TestSupersededPartDeletion(_ArchiveUnitTestBase):
    """A raw part of a compacted day goes only once the compact holding its rows is proven."""

    def _uploaded_parts(self, names: list[str], age_days: float, day: date = _TRACKS_DAY) -> list[Path]:
        """Raw parts uploaded in an earlier cycle, in a day the sweep has compacted; the compact is not uploaded."""
        parts = [_track_part(self._tracks_dir, name, age_days, day) for name in names]
        self.assertEqual(compaction.sweep(compaction.TRACKS, str(self._tracks_dir))["compacted"], 1)
        for part in parts:
            _sentinel(part).touch()
        return parts

    def test_due_parts_go_once_the_local_compact_matches_its_sidecar_hashing_it_once_a_cycle(self):
        names = ["part-100000.parquet", "part-110000.parquet", "part-120000.parquet"]
        due = self._uploaded_parts(names, _TEST_RETENTION_DAYS + 1)
        # Within retention: no part of this day is due, so its compact is not hashed.
        later = _TRACKS_DAY + timedelta(days=1)
        kept = self._uploaded_parts(names[:2], 0.0, later)
        import services.tasks.archive_lifecycle as alc

        # No object store this cycle, so the compacts stay not uploaded.
        with _patch_retention(), unittest.mock.patch.object(alc, "_sha256", wraps=alc._sha256) as sha256:
            stats = self._run(store_enabled=False)

        self.assertEqual(sha256.call_args_list, [unittest.mock.call(_day_dir(self._tracks_dir) / "compact.parquet")])
        self.assertEqual(stats["deleted"], 3)
        self.assertFalse(any(part.exists() for part in due))
        self.assertTrue(all(part.exists() for part in kept))

    def test_due_parts_stay_while_the_local_compact_has_its_sidecars_size_but_other_bytes(self):
        (part,) = self._uploaded_parts(["part-110000.parquet"], _TEST_RETENTION_DAYS + 1)
        compact = _day_dir(self._tracks_dir) / "compact.parquet"
        size = compact.stat().st_size
        data = bytearray(compact.read_bytes())
        # The byte after the leading magic belongs to the first column chunk; the footer is intact.
        data[4] ^= 0xFF
        compact.write_bytes(bytes(data))

        # A sweep that has not looked at the compact again.
        with _patch_retention(), unittest.mock.patch("services.compaction.sweep", return_value={"compacted": 0}):
            stats, uploads = self._run_listing_uploads()

        self.assertEqual(compact.stat().st_size, size)
        self.assertTrue(part.exists())
        self.assertEqual((stats["deleted"], uploads), (0, []))

    def test_a_part_is_judged_on_the_partition_a_writer_holding_the_lock_left(self):
        first, second = self._uploaded_parts(["part-110000.parquet", "part-120000.parquet"], _TEST_RETENTION_DAYS + 1)
        directory = _day_dir(self._tracks_dir)
        manifest = compaction.read_sidecar(str(directory))
        assert manifest is not None and manifest.sha256 is not None
        # The object store holds the compact, so the cycle's first read shows both parts safe to delete.
        _sentinel(directory / "compact.parquet").write_text(manifest.sha256)
        lock = compaction.lock
        rewrites = []

        @contextlib.contextmanager
        def lock_after_a_rewrite(root: str):
            # Another process holds the lock first, and leaves a sidecar that no longer lists the first part.
            if not rewrites:
                with lock(root):
                    compaction.write_sidecar(str(directory), replace(manifest, parts=(second.name,)))
                rewrites.append(root)
            with lock(root):
                yield

        with (
            _patch_retention(),
            unittest.mock.patch("services.compaction.sweep", return_value={"compacted": 0}),
            unittest.mock.patch("services.compaction.lock", lock_after_a_rewrite),
        ):
            stats = self._run()

        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(stats["deleted"], 1)
        # The rewrite came from the first deletion's lock, before any part was judged.
        self.assertEqual(rewrites, [str(self._tracks_dir)])

    def test_a_compact_whose_upload_fails_is_hashed_once_for_the_upload_and_the_due_parts(self):
        names = ["part-100000.parquet", "part-110000.parquet", "part-120000.parquet"]
        due = self._uploaded_parts(names, _TEST_RETENTION_DAYS + 1)
        compact = _day_dir(self._tracks_dir) / "compact.parquet"
        import services.tasks.archive_lifecycle as alc

        with _patch_retention(), unittest.mock.patch.object(alc, "_sha256", wraps=alc._sha256) as sha256:
            stats, uploads = self._run_listing_uploads(upload_return=False)

        self.assertEqual(uploads, [f"{_DAY_KEY}/compact.parquet"])
        self.assertEqual(sha256.call_args_list, [unittest.mock.call(compact)])
        self.assertEqual((stats["deleted"], stats["errors"]), (3, 1))
        self.assertFalse(any(part.exists() for part in due))

    def test_a_compact_renamed_over_mid_cycle_is_hashed_again_and_the_later_parts_are_kept(self):
        names = ["part-100000.parquet", "part-110000.parquet", "part-120000.parquet"]
        first, *later = self._uploaded_parts(names, _TEST_RETENTION_DAYS + 1)
        directory = _day_dir(self._tracks_dir)
        compact = directory / "compact.parquet"
        lock = compaction.lock
        entries = []

        @contextlib.contextmanager
        def lock_after_a_rename(root: str):
            # Before the second part is judged, a writer holding the lock renames a file over the
            # compact: same size and mtime, other bytes, so only its inode tells the two apart.
            entries.append(root)
            if len(entries) == 2:
                with lock(root):
                    st = compact.stat()
                    data = bytearray(compact.read_bytes())
                    data[4] ^= 0xFF
                    replacement = directory / ".replacement.tmp"
                    replacement.write_bytes(bytes(data))
                    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
                    os.replace(replacement, compact)
            with lock(root):
                yield

        import services.tasks.archive_lifecycle as alc

        with (
            _patch_retention(),
            unittest.mock.patch("services.compaction.sweep", return_value={"compacted": 0}),
            unittest.mock.patch("services.compaction.lock", lock_after_a_rename),
            unittest.mock.patch.object(alc, "_sha256", wraps=alc._sha256) as sha256,
            self.assertLogs("services.tasks.archive_lifecycle", level="WARNING") as logs,
        ):
            stats, uploads = self._run_listing_uploads(upload_return=False)

        self.assertEqual(uploads, [f"{_DAY_KEY}/compact.parquet"])
        self.assertFalse(first.exists())
        self.assertTrue(all(part.exists() for part in later))
        self.assertEqual(stats["deleted"], 1)
        # The upload's hash served the first part; the renamed file was hashed again, once for both later parts.
        self.assertEqual(sha256.call_count, 2)
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [f"Archive lifecycle: {compact} does not match its sidecar; keeping the raw parts it holds"],
        )


# ── TestCompactDeletion ───────────────────────────────────────────────────────


class TestCompactDeletion(_ArchiveUnitTestBase):
    """A compact past its retention goes once it is uploaded and its day has no raw part left."""

    def test_a_compact_uploaded_and_deleted_in_one_cycle_is_hashed_once(self):
        day = _utc_days_ago(60)
        part = _track_part(self._tracks_dir, "part-110000.parquet", day=day)
        self.assertEqual(compaction.sweep(compaction.TRACKS, str(self._tracks_dir))["compacted"], 1)
        part.unlink()
        compact = _day_dir(self._tracks_dir, day) / "compact.parquet"
        sha = _sha256(compact)
        key = f"tracks/year={day:%Y}/month={day:%m}/day={day:%d}"
        import services.tasks.archive_lifecycle as alc

        with _compact_retention(30), unittest.mock.patch.object(alc, "_sha256", wraps=alc._sha256) as sha256:
            stats, uploads = self._run_listing_uploads()

        self.assertEqual(uploads, [f"{key}/compact.parquet", f"{key}/compact.json"])
        self.assertEqual(sha256.call_args_list, [unittest.mock.call(compact)])
        self.assertEqual((stats["uploaded"], stats["deleted"]), (1, 1))
        self.assertFalse(compact.exists())
        self.assertEqual(_sentinel(compact).read_text(), sha)


if __name__ == "__main__":
    unittest.main()
