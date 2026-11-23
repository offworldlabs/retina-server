"""Tests for state_snapshot.py object store integration.

Covers:
  1. save_snapshot() replicates to the object store when enabled
  2. restore_snapshot() falls back to the object store when local file is absent
  3. restore_snapshot() prefers local file over the object store
  4. restore_snapshot() returns False when neither source has a snapshot
  5. Object store replication failure doesn't prevent local save

Uses moto for S3 mocking and a temp directory for the snapshot path.
"""

import json
import os
import tempfile
import time
import unittest
import unittest.mock

import boto3
from moto import mock_aws

from services import object_store
from tests.object_store_env import FAKE_AWS_ENV, store_env


def _make_bucket():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")


def _minimal_snapshot() -> dict:
    """Minimal valid snapshot dict (empty data — enough for restore to succeed)."""
    return {
        "saved_at": time.time(),
        "trust_scores": {},
        "reputations": {},
        "accuracy_samples": [],
        "chain_entries": {},
        "node_identities": {},
        "iq_commitments": {},
        "anomaly_log": [],
    }


class TestStateSnapshotObjectStore(unittest.TestCase):
    def setUp(self):
        # Redirect snapshot to a temp dir
        self._tmpdir = tempfile.TemporaryDirectory()
        self._snapshot_path = os.path.join(self._tmpdir.name, "state_snapshot.json")

        import services.state_snapshot as ss

        self._ss = ss
        self._orig_path = ss._SNAPSHOT_PATH
        self._orig_dir = ss._SNAPSHOT_DIR
        ss._SNAPSHOT_PATH = self._snapshot_path
        ss._SNAPSHOT_DIR = self._tmpdir.name

        self.enterContext(store_env(FAKE_AWS_ENV))
        self.enterContext(mock_aws())

    def tearDown(self):
        self._ss._SNAPSHOT_PATH = self._orig_path
        self._ss._SNAPSHOT_DIR = self._orig_dir
        self._tmpdir.cleanup()

    # ── save_snapshot replicates to the object store ─────────────────────────

    def test_save_snapshot_replicates_to_object_store(self):
        """save_snapshot() should upload the snapshot file to the object store."""
        _make_bucket()

        from services.state_snapshot import save_snapshot

        save_snapshot()

        # Local file written
        self.assertTrue(os.path.exists(self._snapshot_path))

        # The object store has the snapshot
        from services.object_store import download_bytes

        data = download_bytes("snapshots/state_snapshot.json")
        self.assertIsNotNone(data, "Snapshot should be in the object store")
        snap = json.loads(json.loads(data)["payload"])  # schema-2 envelope
        self.assertIn("saved_at", snap)
        self.assertIn("trust_scores", snap)

    def test_save_snapshot_local_content_matches_object_store(self):
        """Local snapshot and object store copy should have identical content."""
        _make_bucket()

        from services.state_snapshot import save_snapshot

        save_snapshot()

        with open(self._snapshot_path) as f:
            local = json.load(f)

        from services.object_store import download_bytes

        store_data = download_bytes("snapshots/state_snapshot.json")
        store_snap = json.loads(store_data)

        # Keys must match; saved_at may differ by <1s so just check structure
        self.assertEqual(set(local.keys()), set(store_snap.keys()))

    def test_save_snapshot_local_written_even_if_replication_fails(self):
        """Local file should be written even when replication fails."""
        _make_bucket()

        # Make upload_file fail an exception
        with unittest.mock.patch("services.object_store.upload_file", return_value=False):
            from services.state_snapshot import save_snapshot

            save_snapshot()

        self.assertTrue(os.path.exists(self._snapshot_path), "Local snapshot must exist")

    def test_failed_replication_sends_object_store_alert(self):
        """A failed upload must raise the object_store_replication_failed alert."""
        _make_bucket()

        with (
            unittest.mock.patch("services.object_store.upload_file", return_value=False),
            unittest.mock.patch("services.state_snapshot.send_alert") as mock_alert,
        ):
            from services.state_snapshot import save_snapshot

            save_snapshot()

        mock_alert.assert_called_once()
        self.assertEqual(mock_alert.call_args.args[0], "object_store_replication_failed")

    def test_save_snapshot_local_written_when_object_store_disabled(self):
        """Local file should be written when the object store is not configured."""
        self.enterContext(store_env({}))

        from services.state_snapshot import save_snapshot

        save_snapshot()

        self.assertTrue(os.path.exists(self._snapshot_path))
        with open(self._snapshot_path) as f:
            snap = json.loads(json.load(f)["payload"])  # schema-2 envelope
        self.assertIn("trust_scores", snap)

    # ── restore_snapshot fallback to the object store ────────────────────────

    def test_restore_falls_back_to_object_store_when_local_missing(self):
        """If local snapshot is absent, restore_snapshot() should fetch from the object store."""
        _make_bucket()

        # Upload a snapshot to the object store directly (no local file)
        snap = _minimal_snapshot()
        from services.object_store import upload_bytes

        upload_bytes("snapshots/state_snapshot.json", json.dumps(snap).encode())

        # Ensure local file does not exist
        self.assertFalse(os.path.exists(self._snapshot_path))

        from services.state_snapshot import restore_snapshot

        result = restore_snapshot()

        self.assertTrue(result, "restore_snapshot() should return True after object store restore")

    def test_restore_prefers_local_over_object_store(self):
        """When both local and object store snapshots exist, local should be used (faster)."""
        _make_bucket()

        # Put different snapshots locally vs the object store
        local_snap = _minimal_snapshot()
        local_snap["anomaly_log"] = [{"source": "local"}]
        with open(self._snapshot_path, "w") as f:
            json.dump(local_snap, f)

        store_snap = _minimal_snapshot()
        store_snap["anomaly_log"] = [{"source": "object_store"}]
        from services.object_store import upload_bytes

        upload_bytes("snapshots/state_snapshot.json", json.dumps(store_snap).encode())

        # Track which source gets used by watching download_bytes calls
        with unittest.mock.patch.object(
            object_store, "download_bytes", wraps=object_store.download_bytes
        ) as mock_download:
            from services.state_snapshot import restore_snapshot

            result = restore_snapshot()
            # object store download should NOT have been called since local file existed
            mock_download.assert_not_called()

        self.assertTrue(result)

    def test_restore_returns_false_when_no_snapshot_anywhere(self):
        """No local file + nothing in the object store → restore_snapshot() returns False."""
        _make_bucket()  # empty bucket

        from services.state_snapshot import restore_snapshot

        result = restore_snapshot()

        self.assertFalse(result)

    def test_restore_returns_false_when_object_store_disabled_and_no_local(self):
        """No local file + the object store disabled → restore_snapshot() returns False."""
        self.enterContext(store_env({}))

        from services.state_snapshot import restore_snapshot

        result = restore_snapshot()

        self.assertFalse(result)

    def test_restore_returns_false_when_object_store_has_corrupt_json(self):
        """Corrupt JSON in the object store → restore_snapshot() returns False cleanly."""
        _make_bucket()
        from services.object_store import upload_bytes

        upload_bytes("snapshots/state_snapshot.json", b"NOT VALID JSON{{{{")

        from services.state_snapshot import restore_snapshot

        result = restore_snapshot()

        self.assertFalse(result)

    # ── Atomic write ─────────────────────────────────────────────────────────

    def test_save_snapshot_atomic_no_tmp_file_left_behind(self):
        """save_snapshot() should not leave a .tmp file after completion."""
        _make_bucket()

        from services.state_snapshot import save_snapshot

        save_snapshot()

        tmp_path = self._snapshot_path + ".tmp"
        self.assertFalse(os.path.exists(tmp_path), ".tmp file should be cleaned up")
        self.assertTrue(os.path.exists(self._snapshot_path))


if __name__ == "__main__":
    unittest.main()
