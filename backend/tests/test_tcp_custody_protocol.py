"""Tests for the chain-of-custody TCP message handlers, over real windowed chains.

Covers REGISTER_KEY, CHAIN_ENTRY and IQ_COMMITMENT: the sub-handlers in
services/tcp_handler.py that handle_tcp_client dispatches to. The sub-handlers
are driven directly, with the HELLO node passed as handle_tcp_client would;
the tests of what a connection must send first run the whole connection.
"""

import asyncio
import json

import pytest

from config.constants import CUSTODY_BATCH_MAX_CHARS, IQ_COMMITMENTS_MAX_PER_NODE
from core import state
from services import tcp_handler
from services.tcp_handler import (
    _enqueue_detection,
    _handle_chain_entry,
    _handle_iq_commitment,
    _handle_register_key,
    handle_tcp_client,
)
from tests.custody_helpers import NODE_CONFIG, ChainNode, frame
from tests.tcp_helpers import FakeReader, FakeWriter

_NODE = "test-cust-1"
_OTHER = "test-cust-2"
_TOKEN = "test-node-token"
_STALE = "stale window: not after the last verified window"

_LEGACY_ROW = {
    "node_id": _NODE,
    "hour_utc": "2026-09-09T00:00:00Z",
    "prev_hash": "0" * 64,
    "entry_hash": "e" * 64,
    "_verified": True,
}

_IQ_COMMITMENT_MSG = {
    "type": "IQ_COMMITMENT",
    "capture": {
        "node_id": _NODE,
        "capture_id": "cap001",
        "iq_hash": "aabbccdd1234",
        "sha256": "deadbeef",
    },
}


@pytest.fixture()
def node(tmp_path):
    return ChainNode(tmp_path, _NODE)


def _register(node: ChainNode, msg: dict | None = None, hello_node: str | None = None) -> dict:
    """Send one REGISTER_KEY on a connection whose HELLO named `hello_node`,
    the node's own id by default; return its one ack."""
    writer = FakeWriter()
    msg = {"type": "REGISTER_KEY", **node.register_body} if msg is None else msg
    asyncio.run(_handle_register_key(msg, node.node_id if hello_node is None else hello_node, writer))
    (ack,) = writer.messages()
    return ack


def _key_rejected(reason: str) -> dict:
    return {"type": "KEY_ACK", "status": "rejected", "node_id": None, "reason": reason}


def _chain_msg(entry, batch=None, **extra) -> dict:
    msg = {"type": "CHAIN_ENTRY", "entry": entry}
    if batch is not None:
        msg["batch"] = batch
    return msg | extra


def _send(msg: dict, hello_node: str | None = _NODE) -> dict:
    """Send one CHAIN_ENTRY on a connection whose HELLO named `hello_node`;
    return its one ack. An exception out of the handler fails the test."""
    writer = FakeWriter()
    asyncio.run(_handle_chain_entry(msg, hello_node, writer))
    (ack,) = writer.messages()
    assert ack["type"] == "CHAIN_ENTRY_ACK"
    return ack


def _rejected(reason: str, node_id: str | None = _NODE) -> dict:
    return {
        "type": "CHAIN_ENTRY_ACK",
        "status": "rejected",
        "node_id": node_id,
        "entry_hash": None,
        "verified": False,
        "reason": reason,
        "batch_verified": None,
    }


def _altered(batch: str) -> str:
    frames = json.loads(batch)
    frames[0]["snr"][0] += 1.0
    return json.dumps(frames, sort_keys=True, separators=(",", ":"))


def _connect(*msgs: dict) -> list[dict]:
    """Run one whole connection that sends `msgs`; return what the server sent."""
    lines = b"".join(json.dumps(m).encode() + b"\n" for m in msgs)
    reader = FakeReader([lines[i : i + 4096] for i in range(0, len(lines), 4096)])
    writer = FakeWriter()
    asyncio.run(handle_tcp_client(reader, writer))  # type: ignore[arg-type]
    return writer.messages()


def _chain_acks(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m["type"] == "CHAIN_ENTRY_ACK"]


# ── REGISTER_KEY ──────────────────────────────────────────────────────────────


class TestRegisterKey:
    def test_register_key_stores_identity(self, node):
        _register(node)

        identity = state.node_identities[_NODE]
        assert identity.public_key_pem == node.register_body["public_key_pem"]
        assert identity.public_key_fingerprint == node.register_body["fingerprint"]
        assert identity.serial_number == node.register_body["serial_number"]

    def test_register_key_sends_key_ack(self, node):
        ack = _register(node)

        assert ack == {
            "type": "KEY_ACK",
            "node_id": _NODE,
            "fingerprint": node.register_body["fingerprint"],
            "status": "registered",
        }

    def test_register_key_with_custom_signing_mode(self, node):
        _register(node, {"type": "REGISTER_KEY", **node.register_body, "signing_mode": "hardware"})

        assert state.node_identities[_NODE].signing_mode == "hardware"

    def test_register_then_ingest_needs_no_signature_registry(self, node):
        _register(node)
        entry, batch = node.next_window()

        assert _send(_chain_msg(entry, batch))["verified"] is True
        assert not hasattr(state, "sig_verifier")

    def test_without_a_node_id_the_key_is_the_hello_nodes(self, node):
        body = {k: v for k, v in node.register_body.items() if k != "node_id"}

        ack = _register(node, {"type": "REGISTER_KEY", **body})

        assert (ack["status"], ack["node_id"]) == ("registered", _NODE)
        assert state.node_identities[_NODE].public_key_pem == node.register_body["public_key_pem"]

    def test_without_hello_it_is_rejected_and_nothing_stored(self, node):
        writer = FakeWriter()

        asyncio.run(_handle_register_key({"type": "REGISTER_KEY", **node.register_body}, None, writer))

        assert writer.messages() == [_key_rejected("no HELLO on this connection")]
        assert state.node_identities == {}

    def test_another_nodes_id_is_a_node_id_mismatch_and_nothing_stored(self, node, tmp_path):
        other = ChainNode(tmp_path, _OTHER)
        _register(other)
        held = state.node_identities[_OTHER]

        ack = _register(node, {"type": "REGISTER_KEY", **node.register_body, "node_id": _OTHER}, hello_node=_NODE)

        assert ack == _key_rejected("node_id mismatch")
        assert state.node_identities[_OTHER] is held
        assert _NODE not in state.node_identities


# ── CHAIN_ENTRY ───────────────────────────────────────────────────────────────


class TestChainEntry:
    def test_valid_entry_and_batch_are_verified(self, node, client):
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch))

        assert ack == {
            "type": "CHAIN_ENTRY_ACK",
            "status": "stored",
            "node_id": _NODE,
            "entry_hash": entry["entry_hash"],
            "verified": True,
            "reason": "ok",
            "batch_verified": True,
        }
        (stored,) = state.chain_entries[_NODE]
        assert (stored["_verified"], stored["_batch_verified"], stored["_gap"]) == (True, True, False)
        status = client.get("/api/custody/status").json()["chain_entries"][_NODE]
        assert (status["count"], status["verified"], status["latest_verified"]) == (1, 1, True)
        chain = client.get(f"/api/custody/chain/{_NODE}").json()
        assert (chain["chain_length"], chain["verified_entries"], chain["latest_verified"]) == (1, 1, True)

    def test_second_window_is_linked_and_verified(self, node):
        _register(node)
        first, b1 = node.next_window()
        second, b2 = node.next_window()

        _send(_chain_msg(first, b1))
        ack = _send(_chain_msg(second, b2))

        assert ack["verified"] is True
        assert [e["_verified"] for e in state.chain_entries[_NODE]] == [True, True]

    def test_a_replayed_window_is_unverified_and_does_not_anchor(self, node):
        _register(node)
        first, b1 = node.next_window()
        second, b2 = node.next_window()
        third, b3 = node.next_window()
        _send(_chain_msg(first, b1))
        _send(_chain_msg(second, b2))

        replay = _send(_chain_msg(first, b1))
        assert replay["status"] == "stored"
        assert replay["verified"] is False
        assert replay["reason"].startswith("prev_hash mismatch")

        assert _send(_chain_msg(third, b3))["verified"] is True

    def test_a_replay_behind_a_tampered_window_is_stale(self, node, client):
        _register(node)
        windows = [node.next_window() for _ in range(3)]
        _send(_chain_msg(*windows[0]))
        _send(_chain_msg(*windows[1]))
        third, b3 = windows[2]
        assert _send(_chain_msg(third, _altered(b3)))["verified"] is False

        replay = _send(_chain_msg(*windows[0]))

        assert (replay["status"], replay["verified"], replay["reason"], replay["batch_verified"]) == (
            "stored",
            False,
            _STALE,
            False,
        )
        status = client.get("/api/custody/status").json()["chain_entries"][_NODE]
        assert (status["count"], status["verified"], status["latest_verified"]) == (4, 2, False)
        assert status["latest_window_end"] == windows[0][0]["window_end"]

    def test_the_tampered_window_resent_intact_is_verified(self, node):
        _register(node)
        windows = [node.next_window() for _ in range(3)]
        _send(_chain_msg(*windows[0]))
        _send(_chain_msg(*windows[1]))
        third, b3 = windows[2]
        _send(_chain_msg(third, _altered(b3)))

        ack = _send(_chain_msg(third, b3))

        assert (ack["verified"], ack["reason"], ack["batch_verified"]) == (True, "ok", True)
        assert [e["_verified"] for e in state.chain_entries[_NODE]] == [True, True, False, True]

    def test_an_altered_frame_fails_the_batch(self, node):
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, _altered(batch)))

        assert ack["status"] == "stored"
        assert ack["verified"] is False
        assert ack["batch_verified"] is False
        assert ack["reason"].startswith("detections_hash mismatch")

    def test_no_batch_is_verified_with_the_batch_unchecked(self, node):
        _register(node)
        entry, _ = node.next_window()

        ack = _send(_chain_msg(entry))

        assert ack["verified"] is True
        assert ack["batch_verified"] is None

    def test_no_key_is_stored_unverified(self, node):
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch))

        assert ack["verified"] is False
        assert ack["reason"] == f"no public key for node {_NODE}"
        assert len(state.chain_entries[_NODE]) == 1

    def test_missing_window_end_is_rejected_and_nothing_stored(self, node):
        _register(node)
        entry, batch = node.next_window()
        del entry["window_end"]

        ack = _send(_chain_msg(entry, batch))

        assert ack["status"] == "rejected"
        assert ack["reason"].startswith("malformed entry: ")
        assert "window_end" in ack["reason"]
        assert ack == _rejected(ack["reason"])
        assert _NODE not in state.chain_entries

    def test_a_skipped_window_is_a_gap_and_still_verified(self, node, client):
        _register(node)
        first, b1 = node.next_window()
        later, b2 = node.next_window(skip=1)

        _send(_chain_msg(first, b1))
        ack = _send(_chain_msg(later, b2))

        assert ack["verified"] is True
        assert state.chain_entries[_NODE][-1]["_gap"] is True
        assert client.get("/api/custody/status").json()["chain_entries"][_NODE]["gaps"] == 1
        assert client.get(f"/api/custody/chain/{_NODE}").json()["gaps"] == 1

    def test_the_rolling_cap_keeps_the_newest(self, node, monkeypatch):
        import services.custody

        monkeypatch.setattr(services.custody, "CHAIN_ENTRIES_MAX_PER_NODE", 2)
        _register(node)
        windows = [node.next_window() for _ in range(3)]
        for entry, batch in windows:
            assert _send(_chain_msg(entry, batch))["verified"] is True

        held = state.chain_entries[_NODE]
        assert [e["entry_hash"] for e in held] == [e["entry_hash"] for e, _ in windows[1:]]

    def test_an_entry_for_another_node_is_a_node_id_mismatch(self, node, tmp_path):
        other = ChainNode(tmp_path, _OTHER)
        _register(other)
        entry, batch = other.next_window()

        ack = _send(_chain_msg(entry, batch), hello_node=_NODE)

        assert ack["node_id"] == _OTHER
        assert ack["verified"] is False
        assert ack["reason"] == "node_id mismatch"
        assert ack["batch_verified"] is False
        assert len(state.chain_entries[_OTHER]) == 1
        assert _NODE not in state.chain_entries

    def test_the_messages_own_node_id_is_ignored(self, node):
        """Only the HELLO node counts; a top-level node_id cannot vouch."""
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch, node_id=_OTHER))

        assert ack["node_id"] == _NODE
        assert ack["verified"] is True

    def test_without_hello_it_is_rejected_and_nothing_stored(self, node):
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch, node_id=_NODE), hello_node=None)

        assert ack == _rejected("no HELLO on this connection", node_id=None)
        assert _NODE not in state.chain_entries

    def test_a_legacy_tail_neither_anchors_nor_counts(self, node, client):
        _register(node)
        state.chain_entries[_NODE] = [dict(_LEGACY_ROW)]
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch))

        assert ack["verified"] is True
        status = client.get("/api/custody/status").json()["chain_entries"][_NODE]
        assert (status["count"], status["verified"], status["gaps"]) == (2, 1, 1)

    def test_over_a_connection(self, node):
        """HELLO, REGISTER_KEY and CHAIN_ENTRY on one connection: the entry is
        checked against the HELLO node."""
        entry, batch = node.next_window()
        lines = b"".join(
            json.dumps(m).encode() + b"\n"
            for m in (
                {"type": "HELLO", "node_id": _NODE, "version": "1.0"},
                {"type": "REGISTER_KEY", **node.register_body},
                _chain_msg(entry, batch),
            )
        )
        reader = FakeReader([lines[i : i + 4096] for i in range(0, len(lines), 4096)])
        writer = FakeWriter()

        asyncio.run(handle_tcp_client(reader, writer))

        acks = [m for m in writer.messages() if m["type"] == "CHAIN_ENTRY_ACK"]
        assert [(a["node_id"], a["verified"], a["batch_verified"]) for a in acks] == [(_NODE, True, True)]


# ── CHAIN_ENTRY: the connection's HELLO ───────────────────────────────────────


class TestChainEntryNeedsAHello:
    """CONFIG and REGISTER_KEY name a node too, but only an accepted HELLO
    lets a connection send entries."""

    @pytest.mark.parametrize("config", [NODE_CONFIG, {}], ids=["config-accepted", "config-refused"])
    def test_config_without_hello_is_rejected_and_nothing_stored(self, node, config):
        _register(node)
        entry, batch = node.next_window()

        msgs = _connect({"type": "CONFIG", "node_id": _NODE, "config": config}, _chain_msg(entry, batch))

        assert _chain_acks(msgs) == [_rejected("no HELLO on this connection", node_id=None)]
        assert _NODE not in state.chain_entries

    def test_with_a_token_set_no_hello_is_rejected_and_nothing_stored(self, node, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)
        entry, batch = node.next_window()

        msgs = _connect(
            {"type": "REGISTER_KEY", **node.register_body},
            {"type": "CONFIG", "node_id": _NODE, "config": NODE_CONFIG},
            _chain_msg(entry, batch, node_id=_NODE),
        )

        assert _chain_acks(msgs) == [_rejected("no HELLO on this connection", node_id=None)]
        assert not any(m["type"] == "AUTH_FAIL" for m in msgs)
        assert _NODE not in state.chain_entries
        assert _NODE not in state.node_identities

    def test_a_hello_with_the_token_still_verifies(self, node, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)
        entry, batch = node.next_window()

        msgs = _connect(
            {"type": "HELLO", "node_id": _NODE, "version": "1.0", "token": _TOKEN},
            {"type": "REGISTER_KEY", **node.register_body},
            _chain_msg(entry, batch),
        )

        assert [(a["node_id"], a["verified"], a["batch_verified"]) for a in _chain_acks(msgs)] == [(_NODE, True, True)]

    def test_a_hello_with_a_wrong_token_ends_the_connection_first(self, node, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)
        _register(node)
        entry, batch = node.next_window()

        msgs = _connect(
            {"type": "HELLO", "node_id": _NODE, "version": "1.0", "token": "wrong"}, _chain_msg(entry, batch)
        )

        assert [m["type"] for m in msgs] == ["AUTH_FAIL"]
        assert _NODE not in state.chain_entries


# ── REGISTER_KEY: the connection's HELLO ──────────────────────────────────────


def _key_acks(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m["type"] == "KEY_ACK"]


class TestRegisterKeyNeedsAHello:
    """A key decides which entries verify, so only the node a connection's
    accepted HELLO named can register one, and only its own."""

    def test_a_key_sent_without_hello_cannot_replace_a_nodes_key(self, node, tmp_path, client, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)
        headers = {"X-API-Key": "test-key-abc123"}
        attacker = ChainNode(tmp_path / "attacker", _NODE)
        assert client.post("/api/custody/register", json=node.register_body, headers=headers).status_code == 200
        first, b1 = node.next_window()
        body = {"node_id": _NODE, "entry": first, "batch": b1}
        assert client.post("/api/custody/chain-entry", json=body, headers=headers).json()["verified"] is True

        msgs = _connect({"type": "REGISTER_KEY", **attacker.register_body})

        assert msgs == [_key_rejected("no HELLO on this connection")]
        assert state.node_identities[_NODE].public_key_pem == node.register_body["public_key_pem"]
        second, b2 = node.next_window()
        body = {"node_id": _NODE, "entry": second, "batch": b2}
        ack = client.post("/api/custody/chain-entry", json=body, headers=headers).json()
        assert (ack["verified"], ack["reason"]) == (True, "ok")

    @pytest.mark.parametrize("config", [NODE_CONFIG, {}], ids=["config-accepted", "config-refused"])
    def test_config_without_hello_is_rejected_and_nothing_stored(self, node, config):
        msgs = _connect(
            {"type": "CONFIG", "node_id": _NODE, "config": config}, {"type": "REGISTER_KEY", **node.register_body}
        )

        assert _key_acks(msgs) == [_key_rejected("no HELLO on this connection")]
        assert state.node_identities == {}

    def test_after_hello_another_nodes_key_is_a_node_id_mismatch(self, node, tmp_path, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)
        other = ChainNode(tmp_path, _OTHER)
        _register(other)
        held = state.node_identities[_OTHER]

        msgs = _connect(
            {"type": "HELLO", "node_id": _NODE, "version": "1.0", "token": _TOKEN},
            {"type": "REGISTER_KEY", **node.register_body, "node_id": _OTHER},
        )

        assert msgs == [_key_rejected("node_id mismatch")]
        assert state.node_identities[_OTHER] is held
        assert _NODE not in state.node_identities

    def test_after_hello_the_nodes_own_key_is_registered(self, node, monkeypatch):
        monkeypatch.setattr(tcp_handler, "_RADAR_NODE_TOKEN", _TOKEN)

        msgs = _connect(
            {"type": "HELLO", "node_id": _NODE, "version": "1.0", "token": _TOKEN},
            {"type": "REGISTER_KEY", **node.register_body},
        )

        assert [(a["status"], a["node_id"]) for a in _key_acks(msgs)] == [("registered", _NODE)]
        assert state.node_identities[_NODE].public_key_pem == node.register_body["public_key_pem"]


# ── CHAIN_ENTRY: hostile input ────────────────────────────────────────────────


class TestChainEntryHostileInput:
    @pytest.mark.parametrize("entry", [["not", "an", "object"], "entry", None, 7, True])
    def test_entry_not_an_object_is_rejected(self, entry):
        ack = _send(_chain_msg(entry))

        assert ack == _rejected("malformed entry: entry must be an object")
        assert _NODE not in state.chain_entries

    def test_a_message_without_an_entry_is_rejected(self):
        ack = _send({"type": "CHAIN_ENTRY"})

        assert ack == _rejected("malformed entry: entry must be an object")

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            ({"n_detections": "3"}, "n_detections must be a non-negative integer"),
            ({"n_detections": True}, "n_detections must be a non-negative integer"),
            ({"n_detections": -1}, "n_detections must be a non-negative integer"),
            ({"n_detections": None}, "n_detections must be a non-negative integer"),
            ({"prev_hash": 123}, "prev_hash must be a string"),
            ({"entry_hash": {"a": 1}}, "entry_hash must be a string"),
            ({"node_id": None}, "node_id must be a string"),
            ({"node_id": "test-" + "x" * 124}, "node_id is too long"),
            ({"signing_mode": "x" * 257}, "signing_mode is too long"),
            ({"ots_proof": ["x"]}, "ots_proof must be a string"),
            ({"tsa_token": "x" * 65537}, "tsa_token is too long"),
            ({"timestamp_utc": "\ud800"}, "timestamp_utc must be valid Unicode"),
            ({"window_start": "2027-01-15T08:00:00"}, "window_start must be an ISO 8601 time with a UTC offset"),
            ({"window_end": "SECRET"}, "window_end must be an ISO 8601 time with a UTC offset"),
        ],
    )
    def test_a_wrong_typed_or_oversized_field_is_rejected(self, node, fields, reason):
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry | fields, batch))

        assert ack == _rejected(f"malformed entry: {reason}")
        assert _NODE not in state.chain_entries

    @pytest.mark.parametrize(
        ("batch", "reason"),
        [
            (123, "batch must be a string"),
            (["[]"], "batch must be a string"),
            ({"a": 1}, "batch must be a string"),
            (False, "batch must be a string"),
            ("x" * (CUSTODY_BATCH_MAX_CHARS + 1), "batch is too long"),
            ("[\ud800]", "batch must be valid Unicode"),
        ],
    )
    def test_a_bad_batch_is_rejected(self, node, batch, reason):
        _register(node)
        entry, _ = node.next_window()

        ack = _send(_chain_msg(entry, batch))

        assert ack == _rejected(f"malformed entry: {reason}")
        assert _NODE not in state.chain_entries

    def test_a_deeply_nested_batch_that_matches_its_hash_is_unverified(self, node):
        _register(node)
        batch = "[" * 100_000 + "]" * 100_000
        entry = node.sign(batch, n_detections=1)

        ack = _send(_chain_msg(entry, batch))

        assert ack["status"] == "stored"
        assert ack["verified"] is False
        assert ack["batch_verified"] is False
        assert ack["reason"].startswith("verification error: ")

    def test_a_hostile_hello_node_id_is_a_mismatch(self, node):
        _register(node)
        entry, batch = node.next_window()

        ack = _send(_chain_msg(entry, batch), hello_node=["not", "a", "string"])  # type: ignore[arg-type]

        assert ack["reason"] == "node_id mismatch"
        assert ack["verified"] is False


# ── DETECTION ─────────────────────────────────────────────────────────────────


def test_a_signed_detection_is_enqueued_without_a_verify_stamp():
    """Per-frame signatures are no longer checked, so nothing marks one."""
    data = frame(0) | {"payload_hash": "ab" * 32, "signature": "cd" * 32, "signing_mode": "software"}

    _enqueue_detection({"type": "DETECTION", "data": data}, _NODE)

    assert "_needs_sig_verify" not in data
    queued_node, queued = state.frame_queue.get_nowait()
    assert queued_node == _NODE
    assert queued is data


# ── IQ_COMMITMENT ─────────────────────────────────────────────────────────────


class TestIQCommitment:
    def test_iq_commitment_appended_to_state(self):
        writer = FakeWriter()
        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE, writer))

        commitments = state.iq_commitments[_NODE]
        assert len(commitments) == 1
        assert "_received_at" in commitments[0]
        assert commitments[0]["capture_id"] == "cap001"

    def test_iq_commitment_sends_ack(self):
        writer = FakeWriter()
        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE, writer))

        acks = [m for m in writer.messages() if m.get("type") == "IQ_COMMITMENT_ACK"]
        assert len(acks) == 1
        assert acks[0]["capture_id"] == "cap001"
        assert acks[0]["status"] == "committed"
        assert acks[0]["node_id"] == _NODE

    def test_iq_commitment_cap_enforced(self):
        writer = FakeWriter()
        state.iq_commitments[_NODE] = [{"_synthetic": True, "i": i} for i in range(IQ_COMMITMENTS_MAX_PER_NODE)]

        asyncio.run(_handle_iq_commitment(_IQ_COMMITMENT_MSG, _NODE, writer))

        commitments = state.iq_commitments[_NODE]
        assert len(commitments) == IQ_COMMITMENTS_MAX_PER_NODE
        assert "_received_at" in commitments[-1]
        assert commitments[0].get("i") == 1
