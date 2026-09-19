"""backend/scripts/unblock_nodes.py — the only way out of a persisted block.

REPUTATION_PENALTY_SCALE=0 stops new penalties; it deliberately leaves blocks
a snapshot already carries alone.  So this script has to produce a file the
server will actually accept on boot: schema-2 envelope, self-consistent
checksum, and entries that still construct as NodeReputation(**entry) the way
services/state_snapshot.restore_snapshot does.
"""

import hashlib
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from retina_analytics.reputation import NodeReputation

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "unblock_nodes.py"


@pytest.fixture(scope="module")
def unblock():
    """Load the script as a module.

    By path rather than by import: it is a stdlib-only operator tool that has
    to run in a bare python:3.12-slim against a docker volume, so it is not on
    the backend's import path and must not become dependent on being there.
    """
    spec = importlib.util.spec_from_file_location("unblock_nodes", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocked_entry(node_id: str) -> dict:
    """A reputation as a real snapshot carries it — built through the dataclass
    and asdict() so the shape cannot drift from what the server writes."""
    rep = NodeReputation(
        node_id=node_id,
        reputation=0.05,
        blocked=True,
        block_reason="Reputation 0.05 below threshold",
    )
    rep.penalties = [
        {"time": 1.0, "amount": 0.15, "reason": "Trust score critically low: 0.000", "reputation_after": 0.85},
        {"time": 2.0, "amount": 0.15, "reason": "Trust score critically low: 0.000", "reputation_after": 0.70},
    ]
    # What live entries look like: the evaluator records the state of each
    # named condition every pass, keyed per neighbour.
    rep._condition_active = {
        "heartbeat_stale": True,
        "neighbour_inconsistent:synth-GVL-0002": False,
        "neighbour_inconsistent:synth-GVL-0003": False,
    }
    return asdict(rep)


def _healthy_entry(node_id: str) -> dict:
    rep = NodeReputation(node_id=node_id, reputation=0.9)
    rep._condition_active = {"heartbeat_stale": False}
    return asdict(rep)


def _write_snapshot(path: Path, reputations: dict) -> None:
    """A schema-2 envelope, written the way services/state_snapshot does."""
    payload = json.dumps({"saved_at": 1.0, "reputations": reputations, "trust_scores": {}})
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    path.write_text(json.dumps({"schema": 2, "sha256": checksum, "payload": payload}))
    Path(str(path) + ".sha256").write_text(checksum)


def _read_payload(path: Path) -> dict:
    """Read the file back the way the server does, verifying as it goes."""
    envelope = json.loads(path.read_text())
    assert envelope["schema"] == 2
    actual = hashlib.sha256(envelope["payload"].encode()).hexdigest()
    assert actual == envelope["sha256"], "the file no longer verifies against its own checksum"
    assert Path(str(path) + ".sha256").read_text().strip() == actual, "the side file is stale"
    return json.loads(envelope["payload"])


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "state_snapshot.json"
    _write_snapshot(
        path,
        {
            # node_id keys, not node_refs: the analytics API renames entries to
            # node_ref only at publication.
            "blocked-real-1": _blocked_entry("blocked-real-1"),
            "synth-GVL-0004": _blocked_entry("synth-GVL-0004"),
            "synth-GVL-0005": _healthy_entry("synth-GVL-0005"),
        },
    )
    return path


def test_a_named_node_is_reset_and_the_rest_are_untouched(unblock, snapshot):
    before = _read_payload(snapshot)

    assert unblock.main(["--path", str(snapshot), "--node", "blocked-real-1"]) == 0

    after = _read_payload(snapshot)
    reset = after["reputations"]["blocked-real-1"]
    assert reset["reputation"] == 1.0
    assert reset["blocked"] is False
    assert reset["block_reason"] == ""
    assert reset["penalties"] == []
    assert reset["_condition_active"] == {}, "a stale active flag would hide the next onset"

    for other in ("synth-GVL-0004", "synth-GVL-0005"):
        assert after["reputations"][other] == before["reputations"][other]


def test_all_blocked_resets_every_block_and_nothing_else(unblock, snapshot):
    assert unblock.main(["--path", str(snapshot), "--all-blocked"]) == 0

    after = _read_payload(snapshot)["reputations"]
    assert after["blocked-real-1"]["blocked"] is False
    assert after["synth-GVL-0004"]["blocked"] is False
    # The healthy node was never blocked, so --all-blocked must not have
    # touched its reputation either.
    assert after["synth-GVL-0005"]["reputation"] == 0.9


def test_dry_run_writes_nothing(unblock, snapshot):
    original = snapshot.read_text()
    original_sha = Path(str(snapshot) + ".sha256").read_text()

    assert unblock.main(["--path", str(snapshot), "--all-blocked", "--dry-run"]) == 0

    assert snapshot.read_text() == original
    assert Path(str(snapshot) + ".sha256").read_text() == original_sha


def test_the_result_still_restores_as_a_NodeReputation(unblock, snapshot):
    """restore_snapshot does NodeReputation(**entry) — an edit that added or
    dropped a key would fail there, at boot, with the block still in place."""
    assert unblock.main(["--path", str(snapshot), "--all-blocked"]) == 0

    for node_id, entry in _read_payload(snapshot)["reputations"].items():
        rep = NodeReputation(**entry)
        assert rep.node_id == node_id
    assert rep.blocked is False


def test_an_unknown_node_exits_non_zero(unblock, snapshot, capsys):
    rc = unblock.main(["--path", str(snapshot), "--node", "blocked-real-1", "--node", "no-such-node"])
    assert rc != 0
    assert "no-such-node" in capsys.readouterr().err
    # The one that did exist is still reset — a typo in a second --node must
    # not silently roll back the unblock the operator came for.
    assert _read_payload(snapshot)["reputations"]["blocked-real-1"]["blocked"] is False


def test_a_corrupt_snapshot_is_refused_unless_forced(unblock, snapshot):
    envelope = json.loads(snapshot.read_text())
    envelope["sha256"] = "0" * 64
    snapshot.write_text(json.dumps(envelope))

    with pytest.raises(SystemExit):
        unblock.main(["--path", str(snapshot), "--all-blocked"])
    assert json.loads(snapshot.read_text())["sha256"] == "0" * 64, "nothing should have been written"

    assert unblock.main(["--path", str(snapshot), "--all-blocked", "--force"]) == 0
    # Forcing rewrites it with a checksum that matches, so the server will
    # accept the repaired file on boot.
    assert _read_payload(snapshot)["reputations"]["blocked-real-1"]["blocked"] is False


def test_a_legacy_schema_1_snapshot_is_upgraded(unblock, tmp_path):
    path = tmp_path / "legacy.json"
    payload = json.dumps({"saved_at": 1.0, "reputations": {"blocked-real-1": _blocked_entry("blocked-real-1")}})
    path.write_text(payload)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(payload.encode()).hexdigest())

    assert unblock.main(["--path", str(path), "--all-blocked"]) == 0

    assert _read_payload(path)["reputations"]["blocked-real-1"]["blocked"] is False


def test_selecting_nothing_is_an_error(unblock, snapshot):
    with pytest.raises(SystemExit):
        unblock.main(["--path", str(snapshot)])


def test_a_missing_snapshot_exits_non_zero(unblock, tmp_path):
    assert unblock.main(["--path", str(tmp_path / "nope.json"), "--all-blocked"]) == 2
