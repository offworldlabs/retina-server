"""Tests for archive lifecycle: offload to the object store + local disk cleanup.

Two suites:
  1. TestArchiveLifecycleMoto  — integration tests using moto to mock S3.
  2. Unit test classes         — error-path and edge-case tests using
                                 unittest.mock only (no real or mocked S3).
"""

import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import boto3
from moto import mock_aws

from config.constants import ARCHIVE_OFFLOAD_AGE_DAYS
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
        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0})

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
        """Create a Hive-partitioned tracks/ Parquet file with the given age."""
        mtime = time.time() - age_days * 86400
        part_dir = self._tracks_dir / "year=2026" / "month=05" / "day=01"
        part_dir.mkdir(parents=True, exist_ok=True)
        f = part_dir / "part-120000.parquet"
        f.write_bytes(b"PAR1")
        os.utime(f, (mtime, mtime))
        return f

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
        keys = list_keys("tracks/")
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("tracks/year=2026/"))

    def test_tracks_parquet_deleted_past_retention(self):
        """Track Parquet past retention is deleted locally once confirmed in the object store."""
        _make_bucket()
        f = self._make_tracks_parquet(age_days=_TEST_RETENTION_DAYS + 1)
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertGreater(stats["deleted"], 0)
        self.assertFalse(f.exists(), "track Parquet past retention must be deleted")

    def _make_adsb_parquet(self, age_days: float) -> Path:
        """Create a Hive-partitioned adsb/ Parquet file with the given age."""
        mtime = time.time() - age_days * 86400
        part_dir = self._adsb_dir / "year=2026" / "month=05" / "day=01"
        part_dir.mkdir(parents=True, exist_ok=True)
        f = part_dir / f"part-120000-{'ab12cd34' * 4}.parquet"
        f.write_bytes(b"PAR1")
        os.utime(f, (mtime, mtime))
        return f

    def test_adsb_parquet_uploaded_under_adsb_prefix(self):
        """ADS-B snapshot Parquet in adsb/ is offloaded under the 'adsb/' object store prefix."""
        _make_bucket()
        self._make_adsb_parquet(age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        stats = run_archive_lifecycle()
        self.assertGreater(stats["uploaded"], 0)
        keys = list_keys("adsb/")
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("adsb/year=2026/"))

    def test_adsb_parquet_deleted_past_retention(self):
        """ADS-B snapshot Parquet past retention is deleted locally once confirmed in the object store."""
        _make_bucket()
        f = self._make_adsb_parquet(age_days=_TEST_RETENTION_DAYS + 1)
        from services.object_store import list_keys
        from services.tasks.archive_lifecycle import run_archive_lifecycle

        with _patch_retention():
            stats = run_archive_lifecycle()
        self.assertGreater(stats["deleted"], 0)
        self.assertEqual(len(list_keys("adsb/")), 1)
        self.assertFalse(f.exists(), "ADS-B Parquet past retention must be deleted")


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
        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._TRACKS_DIR", new=self._tracks_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
        ):
            with unittest.mock.patch("services.object_store.is_enabled", return_value=store_enabled):
                with unittest.mock.patch("services.object_store.upload_file", return_value=upload_return):
                    from services.tasks.archive_lifecycle import run_archive_lifecycle

                    return run_archive_lifecycle()


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

        self.assertEqual(stats, {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0})


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

    def test_existing_sentinel_skips_re_upload(self):
        """A second cycle must not waste a PUT on a file already uploaded."""
        f = _make_archive_file(self._archive_dir, age_days=ARCHIVE_OFFLOAD_AGE_DAYS + 1)
        f.with_name(f.name + ".uploaded").touch()

        with (
            unittest.mock.patch("services.tasks.archive_lifecycle._ARCHIVE_DIR", new=self._archive_dir),
            unittest.mock.patch("services.tasks.archive_lifecycle._ADSB_DIR", new=self._adsb_dir),
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


if __name__ == "__main__":
    unittest.main()
