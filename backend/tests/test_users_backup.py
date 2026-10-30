"""Tests for users.db daily backup to the object store + retention pruning."""

import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from moto import mock_aws

from tests.object_store_env import FAKE_AWS_ENV, store_env


def _make_bucket(bucket: str = "retina-data"):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


class _UsersBackupBase(unittest.TestCase):
    """Sets up an isolated object store bucket and a fake users.db on disk."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "users.db"
        _seed_db(self._db_path)

        self.enterContext(store_env(FAKE_AWS_ENV))
        self.enterContext(mock_aws())

        # Point the backup task at our fake users.db.
        self._db_patcher = unittest.mock.patch(
            "services.tasks.users_backup._users_db_path",
            return_value=self._db_path,
        )
        self._db_patcher.start()

    def tearDown(self):
        self._db_patcher.stop()
        self._tmpdir.cleanup()


class TestUsersBackup(_UsersBackupBase):
    def test_uploads_today_backup(self):
        _make_bucket()
        from services.object_store import list_keys
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()
        today = datetime.now(timezone.utc).date().isoformat()
        expected_key = f"backups/users-db/{today}.db"

        self.assertEqual(stats["uploaded"], expected_key)
        self.assertEqual(stats["skipped"], None)
        self.assertIn(expected_key, list_keys("backups/users-db/"))

    def test_uploaded_blob_is_a_valid_sqlite_db(self):
        """VACUUM INTO must produce a self-consistent file we can re-open."""
        _make_bucket()
        from services.object_store import download_bytes
        from services.tasks.users_backup import run_users_db_backup

        run_users_db_backup()
        today = datetime.now(timezone.utc).date().isoformat()
        blob = download_bytes(f"backups/users-db/{today}.db")
        self.assertIsNotNone(blob)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            f.write(blob)
            restored_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(restored_path))
            try:
                rows = list(conn.execute("SELECT v FROM t"))
            finally:
                conn.close()
            self.assertEqual(rows, [("hello",)])
        finally:
            restored_path.unlink(missing_ok=True)

    def test_retention_drops_old_backups(self):
        """Anything older than RETENTION_DAYS (counted from the date in the
        key, not object store LastModified) must be deleted by the same cycle.
        """
        _make_bucket()
        from services.object_store import list_keys, upload_bytes
        from services.tasks.users_backup import run_users_db_backup

        # Plant one backup well outside the retention window and one inside.
        old_date = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).date().isoformat()
        upload_bytes(f"backups/users-db/{old_date}.db", b"old", content_type="application/x-sqlite3")
        upload_bytes(f"backups/users-db/{recent_date}.db", b"recent", content_type="application/x-sqlite3")

        stats = run_users_db_backup()

        keys = list_keys("backups/users-db/")
        self.assertNotIn(f"backups/users-db/{old_date}.db", keys, "old backup must be pruned")
        self.assertIn(f"backups/users-db/{recent_date}.db", keys, "recent backup must survive")
        self.assertGreaterEqual(stats["deleted"], 1)

    def test_skipped_when_object_store_disabled(self):
        self.enterContext(store_env({}))
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()

        self.assertEqual(stats["skipped"], "object-store-disabled")
        self.assertIsNone(stats["uploaded"])

    def test_skipped_when_db_missing(self):
        _make_bucket()
        self._db_path.unlink()  # delete the fake DB
        from services.tasks.users_backup import run_users_db_backup

        stats = run_users_db_backup()

        self.assertEqual(stats["skipped"], "db-missing")

    def test_idempotent_within_same_day(self):
        """Running the backup twice on the same day overwrites the same key
        and counts as one entry (no duplicates to prune)."""
        _make_bucket()
        from services.object_store import list_keys
        from services.tasks.users_backup import run_users_db_backup

        run_users_db_backup()
        run_users_db_backup()

        today = datetime.now(timezone.utc).date().isoformat()
        keys = [k for k in list_keys("backups/users-db/") if k.endswith(f"{today}.db")]
        self.assertEqual(len(keys), 1, "second run must overwrite, not duplicate")


if __name__ == "__main__":
    unittest.main()
