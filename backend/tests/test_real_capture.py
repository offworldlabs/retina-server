"""Private capture persists quiet truth updates and rejects invalid frames."""

import json
import queue

import pytest

from services import real_capture


@pytest.fixture(autouse=True)
def isolated_capture(monkeypatch):
    monkeypatch.setattr(real_capture, "_queue", queue.Queue(maxsize=2))
    monkeypatch.setattr(real_capture, "_enabled", True)
    monkeypatch.setattr(real_capture, "_counters", {"frames": 0, "dropped": 0, "bytes": 0, "errors": 0})
    monkeypatch.setattr(real_capture.state, "node_world", lambda _: "real")


def test_truth_without_radar_frames_is_actually_persisted(tmp_path):
    truth = {"abc123": {"lat": 34, "lon": -82, "timestamp_ms": 1000000}}
    assert real_capture._write_batch(tmp_path, truth, 10000)
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    row = json.loads(files[0].read_text())
    assert row["kind"] == "truth"
    assert row["aircraft"] == truth
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert real_capture.status()["frames"] == 0
    assert not real_capture._write_batch(tmp_path, {}, 10000)


def test_budget_does_not_claim_an_unwritten_truth_snapshot(tmp_path):
    assert not real_capture._write_batch(tmp_path, {"abc123": {"timestamp_ms": 1000}}, 1)
    assert not list(tmp_path.glob("*.jsonl"))
    assert not real_capture.status()["enabled"]


def test_explicitly_invalid_signature_is_not_captured():
    real_capture.offer("hardware-test", {"timestamp": 1000, "_signature_valid": False})
    assert real_capture.status()["queue_depth"] == 0
    real_capture.offer("hardware-test", {"timestamp": 1000, "_signature_valid": True, "secret": "excluded"})
    row = json.loads(real_capture._queue.get_nowait())
    assert row["frame"] == {"timestamp": 1000}
