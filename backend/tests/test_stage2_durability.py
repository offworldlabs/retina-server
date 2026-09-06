"""Stage-2 regression: archive-flush exclusivity and snapshot atomicity.

- _flush_archive_node's snapshot → write → truncate cycle is not atomic under
  its lock (the disk write must happen outside it); two concurrent flushers
  for one node used to double-write the same frames and then truncate twice,
  discarding frames that arrived during the first write.
- The state snapshot's checksum used to live in a side file written
  non-atomically with the payload; a crash between the two writes made a
  VALID snapshot fail its integrity check on boot and the server started
  empty.  The checksum now travels inside the file.
"""

import json
import os
import threading
import time

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services import frame_processor as fp  # noqa: E402
from services import state_snapshot as snap  # noqa: E402


class TestArchiveFlushExclusivity:
    def setup_method(self):
        with fp._archive_buffer_lock:
            fp._archive_buffer.clear()
            fp._archive_inflight.clear()

    teardown_method = setup_method

    def test_concurrent_flushes_neither_duplicate_nor_lose_frames(self, monkeypatch):
        """Two flushers race over one node while a writer keeps appending.

        Every frame must be archived exactly once; frames still in the buffer
        at the end are fine (the next cycle takes them), but none may vanish
        and none may be written twice.
        """
        written: list[int] = []
        write_started = threading.Event()

        def slow_archive(node_id, frames):
            write_started.set()
            time.sleep(0.05)  # hold the race window open
            written.extend(f["seq"] for f in frames)

        monkeypatch.setattr(fp, "archive_detections", slow_archive)

        n_frames = 40
        stop_feeding = threading.Event()

        def feeder():
            for i in range(n_frames):
                with fp._archive_buffer_lock:
                    fp._archive_buffer["n1"].append({"seq": i})
                time.sleep(0.002)
            stop_feeding.set()

        t_feed = threading.Thread(target=feeder)
        t_feed.start()

        # Two competing flushers, hammering while the feeder runs.
        def flusher():
            while not stop_feeding.is_set():
                fp._flush_archive_node("n1")
                time.sleep(0.005)

        t1 = threading.Thread(target=flusher)
        t2 = threading.Thread(target=flusher)
        t1.start()
        t2.start()
        t_feed.join()
        t1.join()
        t2.join()
        # Final drain.
        fp._flush_archive_node("n1")

        with fp._archive_buffer_lock:
            leftover = [f["seq"] for f in fp._archive_buffer.get("n1", [])]
        assert sorted(written + leftover) == list(range(n_frames)), (
            f"duplicated={sorted(set(x for x in written if written.count(x) > 1))} "
            f"lost={sorted(set(range(n_frames)) - set(written) - set(leftover))}"
        )

    def test_inflight_marker_is_cleared_on_write_failure(self, monkeypatch):
        def boom(node_id, frames):
            raise OSError("disk full")

        monkeypatch.setattr(fp, "archive_detections", boom)
        with fp._archive_buffer_lock:
            fp._archive_buffer["n1"].append({"seq": 0})
        fp._flush_archive_node("n1")
        assert "n1" not in fp._archive_inflight  # next cycle can retry


class TestArchiveBufferReclamation:
    """A buffer key is popped only on a successful write that empties it.

    A node that departs while its buffer is non-empty and its writes are
    failing therefore pinned its frames for the process lifetime, and
    flush_all_archive_buffers retried it every cycle forever — bounded per
    node, unbounded in node count across a long uptime with node churn.
    """

    def setup_method(self):
        fp._reset_for_tests()
        state.connected_nodes.pop("gone", None)
        state.connected_nodes.pop("live", None)

    def teardown_method(self):
        self.setup_method()

    def _fail_once(self, monkeypatch, node_id):
        def boom(nid, frames):
            raise OSError("disk full")

        monkeypatch.setattr(fp, "archive_detections", boom)
        with fp._archive_buffer_lock:
            fp._archive_buffer[node_id].append({"seq": 0})
        fp._flush_archive_node(node_id)

    def test_departed_node_buffer_is_abandoned_after_the_ttl(self, monkeypatch):
        self._fail_once(monkeypatch, "gone")
        assert "gone" in fp._archive_fail_since
        # Age the failure past the TTL rather than waiting six hours.
        with fp._archive_buffer_lock:
            fp._archive_fail_since["gone"] -= fp.ARCHIVE_BUFFER_FAIL_TTL_S + 1

        fp.flush_all_archive_buffers()

        assert "gone" not in fp._archive_buffer
        assert "gone" not in fp._archive_fail_since

    def test_connected_node_keeps_its_frames_however_long_the_outage(self, monkeypatch):
        """Retain-on-failure is the point: a live node must not lose data."""
        state.connected_nodes["live"] = {"status": "active"}
        self._fail_once(monkeypatch, "live")
        with fp._archive_buffer_lock:
            fp._archive_fail_since["live"] -= fp.ARCHIVE_BUFFER_FAIL_TTL_S + 1

        fp.flush_all_archive_buffers()

        assert fp._archive_buffer["live"] == [{"seq": 0}]

    def test_a_successful_write_clears_the_failure_clock(self, monkeypatch):
        self._fail_once(monkeypatch, "gone")
        assert "gone" in fp._archive_fail_since
        monkeypatch.setattr(fp, "archive_detections", lambda nid, frames: None)
        fp._flush_archive_node("gone")
        assert "gone" not in fp._archive_fail_since
        assert "gone" not in fp._archive_buffer


class TestSnapshotAtomicity:
    def _fresh_paths(self, tmp_path, monkeypatch):
        p = str(tmp_path / "state_snapshot.json")
        monkeypatch.setattr(snap, "_SNAPSHOT_PATH", p)
        monkeypatch.setattr(snap, "_SNAPSHOT_DIR", str(tmp_path))
        return p

    def test_round_trip_restores(self, tmp_path, monkeypatch):
        p = self._fresh_paths(tmp_path, monkeypatch)
        snap.save_snapshot()
        assert os.path.exists(p)
        assert snap.restore_snapshot() is True

    def test_payload_and_checksum_are_one_file(self, tmp_path, monkeypatch):
        """The crash window between the two writes is gone by construction:
        deleting the side file entirely must not affect integrity."""
        p = self._fresh_paths(tmp_path, monkeypatch)
        snap.save_snapshot()
        os.remove(p + ".sha256")
        assert snap.restore_snapshot() is True

    def test_stale_side_file_is_ignored(self, tmp_path, monkeypatch):
        """Simulates the old crash: side checksum from a DIFFERENT save.
        Schema-2 restore must not consult it."""
        p = self._fresh_paths(tmp_path, monkeypatch)
        snap.save_snapshot()
        with open(p + ".sha256", "w") as f:
            f.write("0" * 64)
        assert snap.restore_snapshot() is True

    def test_corrupt_payload_is_rejected(self, tmp_path, monkeypatch):
        p = self._fresh_paths(tmp_path, monkeypatch)
        snap.save_snapshot()
        with open(p) as f:
            envelope = json.load(f)
        envelope["payload"] = envelope["payload"].replace('"saved_at"', '"saved_At"', 1)  # tamper one byte
        with open(p, "w") as f:
            json.dump(envelope, f)
        assert snap.restore_snapshot() is False

    def test_legacy_bare_payload_still_restores(self, tmp_path, monkeypatch):
        """Pre-envelope snapshots on disk (payload + matching side file)
        must keep working across the upgrade."""
        p = self._fresh_paths(tmp_path, monkeypatch)
        import hashlib

        payload = json.dumps(
            {
                "saved_at": time.time(),
                "trust_scores": {},
                "reputations": {},
                "accuracy_samples": [],
                "chain_entries": {},
                "node_identities": {},
                "iq_commitments": {},
                "anomaly_log": [],
            }
        )
        with open(p, "w") as f:
            f.write(payload)
        with open(p + ".sha256", "w") as f:
            f.write(hashlib.sha256(payload.encode()).hexdigest())
        assert snap.restore_snapshot() is True
