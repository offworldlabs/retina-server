"""Tests for the chain-of-custody HTTP routes, over real windowed chains."""

import json
from unittest.mock import patch

import pytest

from config.constants import CUSTODY_BATCH_MAX_CHARS
from core import state
from tests.custody_helpers import ChainNode

_HEADERS = {"X-API-Key": "test-key-abc123"}
_NODE = "test-cust-1"
_OTHER = "test-cust-2"
_STALE = "stale window: not after the last verified window"
_ISO = "must be an ISO 8601 time with a UTC offset"

# A row of the old hourly chain, as a snapshot written before windowed entries
# restores it: no window_end, and a `_verified` stamp nothing ever checked.
_LEGACY_ROW = {
    "node_id": _NODE,
    "hour_utc": "2026-09-09T00:00:00Z",
    "prev_hash": "0" * 64,
    "content_hash": "c" * 64,
    "entry_hash": "e" * 64,
    "_verified": True,
}


@pytest.fixture()
def node(tmp_path):
    return ChainNode(tmp_path, _NODE)


def _register(client, node):
    r = client.post("/api/custody/register", json=node.register_body, headers=_HEADERS)
    assert r.status_code == 200


def _post(client, node_id, entry, batch=None):
    body = {"node_id": node_id, "entry": entry}
    if batch is not None:
        body["batch"] = batch
    return client.post("/api/custody/chain-entry", json=body, headers=_HEADERS)


def _ingest(client, node_id, entry, batch=None) -> dict:
    r = _post(client, node_id, entry, batch)
    assert r.status_code == 200, r.text
    return r.json()


def _status(client, node_id=_NODE) -> dict:
    r = client.get("/api/custody/status")
    assert r.status_code == 200
    return r.json()["chain_entries"][node_id]


def _chain(client, node_id=_NODE) -> dict:
    r = client.get(f"/api/custody/chain/{node_id}")
    assert r.status_code == 200
    return r.json()


def _altered(batch: str) -> str:
    """The same batch with one value of its first frame changed."""
    frames = json.loads(batch)
    frames[0]["snr"][0] += 1.0
    return json.dumps(frames, sort_keys=True, separators=(",", ":"))


# ── Register ─────────────────────────────────────────────────────────────────


class TestCustodyRegister:
    def test_register_success(self, client, node):
        r = client.post("/api/custody/register", json=node.register_body, headers=_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["node_id"] == _NODE
        assert body["fingerprint"] == node.register_body["fingerprint"]
        assert body["signing_mode"] == "software"
        assert state.node_identities[_NODE].public_key_pem == node.register_body["public_key_pem"]

    def test_register_missing_node_id(self, client):
        r = client.post("/api/custody/register", json={"public_key_pem": "key"}, headers=_HEADERS)
        assert r.status_code == 422

    def test_register_empty_node_id(self, client):
        r = client.post("/api/custody/register", json={"node_id": "", "public_key_pem": "key"}, headers=_HEADERS)
        assert r.status_code == 422

    def test_register_no_api_key(self, client):
        r = client.post("/api/custody/register", json={"node_id": _NODE, "public_key_pem": "key"})
        assert r.status_code == 401

    def test_register_wrong_api_key(self, client):
        r = client.post(
            "/api/custody/register",
            json={"node_id": _NODE, "public_key_pem": "key"},
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_register_then_ingest_needs_no_signature_registry(self, client, node):
        """The registered identity is the key store; there is no second one."""
        _register(client, node)
        entry, batch = node.next_window()
        assert _ingest(client, _NODE, entry, batch)["verified"] is True
        assert not hasattr(state, "sig_verifier")


# ── Chain entry: verification on receipt ─────────────────────────────────────


class TestChainEntryVerification:
    def test_valid_entry_and_batch_are_verified(self, client, node):
        _register(client, node)
        entry, batch = node.next_window()

        body = _ingest(client, _NODE, entry, batch)

        assert body == {
            "status": "stored",
            "node_id": _NODE,
            "entry_hash": entry["entry_hash"],
            "verified": True,
            "reason": "ok",
            "batch_verified": True,
        }
        (stored,) = state.chain_entries[_NODE]
        assert stored["_verified"] is True
        assert stored["_batch_verified"] is True
        assert stored["_gap"] is False
        assert stored["_reason"] == "ok"
        assert "_received_at" in stored
        assert {k: v for k, v in stored.items() if not k.startswith("_")} == entry
        assert _status(client) == {
            "count": 1,
            "latest_window_end": entry["window_end"],
            "latest_verified": True,
            "verified": 1,
            "gaps": 0,
        }
        chain = _chain(client)
        assert chain["chain_length"] == 1
        assert chain["latest_window_end"] == entry["window_end"]
        assert chain["latest_verified"] is True
        assert chain["verified_entries"] == 1
        assert chain["gaps"] == 0

    def test_second_window_is_linked_and_verified(self, client, node):
        _register(client, node)
        first, b1 = node.next_window()
        second, b2 = node.next_window()
        assert second["prev_hash"] == first["entry_hash"]

        _ingest(client, _NODE, first, b1)
        body = _ingest(client, _NODE, second, b2)

        assert body["verified"] is True
        assert _status(client)["verified"] == 2
        assert state.chain_entries[_NODE][-1]["_gap"] is False

    def test_a_replayed_window_is_unverified_and_does_not_anchor(self, client, node):
        """A stale prev_hash fails linkage; the entry after it is checked on its
        own, so one bad entry does not fail the rest of the chain."""
        _register(client, node)
        first, b1 = node.next_window()
        second, b2 = node.next_window()
        third, b3 = node.next_window()
        _ingest(client, _NODE, first, b1)
        _ingest(client, _NODE, second, b2)

        replay = _ingest(client, _NODE, first, b1)
        assert replay["status"] == "stored"
        assert replay["verified"] is False
        assert replay["reason"].startswith("prev_hash mismatch")

        after = _ingest(client, _NODE, third, b3)
        assert after["verified"] is True
        status = _status(client)
        assert (status["count"], status["verified"], status["latest_verified"]) == (4, 3, True)

    def test_a_replay_behind_a_tampered_window_is_stale(self, client, node):
        """The tamper run: two windows, a third with an altered batch, so the
        tail anchors nothing. A replay of the first window is not verified
        again and does not count."""
        _register(client, node)
        windows = [node.next_window() for _ in range(3)]
        _ingest(client, _NODE, *windows[0])
        _ingest(client, _NODE, *windows[1])
        third, b3 = windows[2]
        assert _ingest(client, _NODE, third, _altered(b3))["verified"] is False

        replay = _ingest(client, _NODE, *windows[0])

        assert replay == {
            "status": "stored",
            "node_id": _NODE,
            "entry_hash": windows[0][0]["entry_hash"],
            "verified": False,
            "reason": _STALE,
            "batch_verified": False,
        }
        stored = state.chain_entries[_NODE][-1]
        assert (stored["_verified"], stored["_reason"], stored["_batch_verified"]) == (False, _STALE, False)
        # latest_window_end is the last row received, the stored replay's.
        assert _status(client) == {
            "count": 4,
            "latest_window_end": windows[0][0]["window_end"],
            "latest_verified": False,
            "verified": 2,
            "gaps": 1,
        }
        assert _chain(client)["verified_entries"] == 2

    def test_the_next_window_after_a_tampered_window_is_verified(self, client, node):
        _register(client, node)
        windows = [node.next_window() for _ in range(4)]
        _ingest(client, _NODE, *windows[0])
        _ingest(client, _NODE, *windows[1])
        third, b3 = windows[2]
        _ingest(client, _NODE, third, _altered(b3))

        after = _ingest(client, _NODE, *windows[3])

        assert (after["verified"], after["reason"], after["batch_verified"]) == (True, "ok", True)
        status = _status(client)
        assert (status["count"], status["verified"], status["latest_verified"]) == (4, 3, True)

    def test_an_altered_frame_fails_the_batch(self, client, node):
        _register(client, node)
        entry, batch = node.next_window()

        body = _ingest(client, _NODE, entry, _altered(batch))

        assert body["status"] == "stored"
        assert body["verified"] is False
        assert body["batch_verified"] is False
        assert body["reason"].startswith("detections_hash mismatch")
        assert _status(client)["verified"] == 0

    def test_no_batch_is_verified_with_the_batch_unchecked(self, client, node):
        _register(client, node)
        entry, _ = node.next_window()

        body = _ingest(client, _NODE, entry)

        assert body["verified"] is True
        assert body["batch_verified"] is None
        assert state.chain_entries[_NODE][0]["_batch_verified"] is None

    def test_no_key_is_stored_unverified(self, client, node):
        entry, batch = node.next_window()

        body = _ingest(client, _NODE, entry, batch)

        assert body["verified"] is False
        assert body["reason"] == f"no public key for node {_NODE}"
        assert len(state.chain_entries[_NODE]) == 1

    def test_missing_window_end_is_422_and_nothing_stored(self, client, node):
        _register(client, node)
        entry, batch = node.next_window()
        del entry["window_end"]

        r = _post(client, _NODE, entry, batch)

        assert r.status_code == 422
        assert r.json()["detail"].startswith("malformed entry: ")
        assert "window_end" in r.json()["detail"]
        assert _NODE not in state.chain_entries

    def test_a_skipped_window_is_a_gap_and_still_verified(self, client, node):
        _register(client, node)
        first, b1 = node.next_window()
        later, b2 = node.next_window(skip=1)
        assert later["window_start"] != first["window_end"]

        _ingest(client, _NODE, first, b1)
        body = _ingest(client, _NODE, later, b2)

        assert body["verified"] is True
        assert state.chain_entries[_NODE][-1]["_gap"] is True
        assert _status(client)["gaps"] == 1
        assert _chain(client)["gaps"] == 1

    def test_the_rolling_cap_keeps_the_newest(self, client, node, monkeypatch):
        import services.custody

        monkeypatch.setattr(services.custody, "CHAIN_ENTRIES_MAX_PER_NODE", 3)
        _register(client, node)
        windows = [node.next_window() for _ in range(4)]
        for entry, batch in windows:
            assert _ingest(client, _NODE, entry, batch)["verified"] is True

        held = state.chain_entries[_NODE]
        assert [e["entry_hash"] for e in held] == [e["entry_hash"] for e, _ in windows[1:]]

    def test_node_id_mismatch_is_stored_under_the_entrys_node(self, client, node, tmp_path):
        other = ChainNode(tmp_path, _OTHER)
        _register(client, other)
        entry, batch = other.next_window()

        body = _ingest(client, _NODE, entry, batch)

        assert body["node_id"] == _OTHER
        assert body["verified"] is False
        assert body["reason"] == "node_id mismatch"
        assert body["batch_verified"] is False
        assert len(state.chain_entries[_OTHER]) == 1
        assert _NODE not in state.chain_entries

    def test_a_legacy_tail_neither_anchors_nor_counts(self, client, node):
        _register(client, node)
        state.chain_entries[_NODE] = [dict(_LEGACY_ROW)]
        assert _status(client)["latest_verified"] is False
        assert _status(client)["verified"] == 0
        assert _status(client)["latest_window_end"] is None
        entry, batch = node.next_window()

        body = _ingest(client, _NODE, entry, batch)

        assert body["verified"] is True
        assert state.chain_entries[_NODE][-1]["_gap"] is True
        status = _status(client)
        assert (status["count"], status["verified"]) == (2, 1)
        assert _chain(client)["verified_entries"] == 1

    def test_submit_entry_no_auth(self, client, node):
        entry, batch = node.next_window()
        r = client.post("/api/custody/chain-entry", json={"node_id": _NODE, "entry": entry, "batch": batch})
        assert r.status_code == 401
        assert _NODE not in state.chain_entries


# ── Chain entry: hostile input ───────────────────────────────────────────────


def _with(entry: dict, **fields) -> dict:
    return entry | fields


class TestChainEntryHostileInput:
    @pytest.mark.parametrize("entry", [["not", "an", "object"], "entry", None, 7])
    def test_entry_not_an_object_is_422(self, client, entry):
        r = client.post("/api/custody/chain-entry", json={"node_id": _NODE, "entry": entry}, headers=_HEADERS)
        assert r.status_code == 422
        assert _NODE not in state.chain_entries

    @pytest.mark.parametrize(
        ("fields", "detail"),
        [
            ({"n_detections": "3"}, "n_detections must be a non-negative integer"),
            ({"n_detections": True}, "n_detections must be a non-negative integer"),
            ({"n_detections": -1}, "n_detections must be a non-negative integer"),
            ({"n_detections": 1.5}, "n_detections must be a non-negative integer"),
            ({"prev_hash": 123}, "prev_hash must be a string"),
            ({"signature": None}, "signature must be a string"),
            ({"window_end": ["2026"]}, "window_end must be a string"),
            ({"window_start": "2027-01-15T08:00:00"}, f"window_start {_ISO}"),
            ({"window_start": "SECRET"}, f"window_start {_ISO}"),
            ({"window_end": "2027-01-15"}, f"window_end {_ISO}"),
            ({"window_end": "1800000060"}, f"window_end {_ISO}"),
            ({"node_id": ""}, "node_id must not be empty"),
            ({"node_id": "test-" + "x" * 124}, "node_id is too long"),
            ({"firmware_version": "x" * 257}, "firmware_version is too long"),
            ({"tsa_token": 5}, "tsa_token must be a string"),
            ({"ots_proof": "x" * 65537}, "ots_proof is too long"),
        ],
    )
    def test_a_wrong_typed_or_oversized_field_is_422(self, client, node, fields, detail):
        _register(client, node)
        entry, batch = node.next_window()

        r = _post(client, _NODE, _with(entry, **fields), batch)

        assert r.status_code == 422
        assert r.json()["detail"] == f"malformed entry: {detail}"
        assert _NODE not in state.chain_entries

    def test_a_lone_surrogate_in_a_field_is_422(self, client, node):
        """Stored, it would break every response that serialises the row."""
        _register(client, node)
        entry, batch = node.next_window()
        body = json.dumps({"node_id": _NODE, "entry": _with(entry, firmware_version="\ud800"), "batch": batch})

        r = client.post(
            "/api/custody/chain-entry",
            content=body.encode("ascii"),
            headers=_HEADERS | {"content-type": "application/json"},
        )

        assert r.status_code == 422
        assert r.json()["detail"] == "malformed entry: firmware_version must be valid Unicode"
        assert _NODE not in state.chain_entries
        assert client.get("/api/custody/status").status_code == 200

    @pytest.mark.parametrize("batch", [123, ["[]"], {"a": 1}, True])
    def test_a_non_string_batch_is_422(self, client, node, batch):
        _register(client, node)
        entry, _ = node.next_window()

        r = client.post(
            "/api/custody/chain-entry", json={"node_id": _NODE, "entry": entry, "batch": batch}, headers=_HEADERS
        )

        assert r.status_code == 422
        assert _NODE not in state.chain_entries

    def test_an_over_long_batch_is_422(self, client, node):
        _register(client, node)
        entry, _ = node.next_window()

        r = _post(client, _NODE, entry, "x" * (CUSTODY_BATCH_MAX_CHARS + 1))

        assert r.status_code == 422
        assert _NODE not in state.chain_entries

    def test_a_batch_at_the_cap_is_checked(self, client, node):
        _register(client, node)
        entry, _ = node.next_window()

        body = _ingest(client, _NODE, entry, "x" * CUSTODY_BATCH_MAX_CHARS)

        assert body["batch_verified"] is False
        assert body["reason"].startswith("detections_hash mismatch")

    def test_a_deeply_nested_batch_that_matches_its_hash_is_unverified(self, client, node):
        """The verifier hashes the batch, finds it matches, and only then parses
        it, which raises RecursionError rather than ValueError."""
        _register(client, node)
        batch = "[" * 100_000 + "]" * 100_000
        entry = node.sign(batch, n_detections=1)

        body = _ingest(client, _NODE, entry, batch)

        assert body["verified"] is False
        assert body["batch_verified"] is False
        assert body["reason"].startswith("verification error: ")
        assert state.chain_entries[_NODE][0]["_verified"] is False

    def test_a_deeply_nested_body_is_refused(self, client):
        body = '{"node_id":"' + _NODE + '","entry":{"node_id":' + "[" * 100_000 + "]" * 100_000 + "}}"
        r = client.post(
            "/api/custody/chain-entry",
            content=body.encode("ascii"),
            headers=_HEADERS | {"content-type": "application/json"},
        )
        assert r.status_code in (400, 422)
        assert _NODE not in state.chain_entries

    def test_client_stamps_in_the_entry_are_ignored(self, client, node):
        entry, batch = node.next_window()

        body = _ingest(client, _NODE, _with(entry, _verified=True, _gap=False), batch)

        assert body["verified"] is False  # no key registered
        assert state.chain_entries[_NODE][0]["_verified"] is False
        assert _status(client)["verified"] == 0


# ── IQ Commitment ────────────────────────────────────────────────────────────


class TestCustodyIqCommitment:
    def test_submit_commitment(self, client):
        r = client.post(
            "/api/custody/iq-commitment",
            json={"node_id": _NODE, "capture_id": "cap-001"},
            headers=_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "committed"
        assert body["capture_id"] == "cap-001"

    def test_submit_no_auth(self, client):
        r = client.post("/api/custody/iq-commitment", json={"node_id": _NODE})
        assert r.status_code == 401

    def test_rolling_cap(self, client):
        from config.constants import IQ_COMMITMENTS_MAX_PER_NODE

        state.iq_commitments[_NODE] = [{"i": i} for i in range(IQ_COMMITMENTS_MAX_PER_NODE)]
        r = client.post(
            "/api/custody/iq-commitment",
            json={"node_id": _NODE, "capture_id": "overflow"},
            headers=_HEADERS,
        )
        assert r.status_code == 200
        assert len(state.iq_commitments[_NODE]) == IQ_COMMITMENTS_MAX_PER_NODE


# ── Read routes ──────────────────────────────────────────────────────────────


class TestCustodyRead:
    def test_status_empty(self, client):
        r = client.get("/api/custody/status")
        assert r.status_code == 200
        assert "registered_nodes" in r.json()

    def test_status_of_an_empty_chain(self, client):
        state.chain_entries[_NODE] = []
        assert _status(client) == {
            "count": 0,
            "latest_window_end": None,
            "latest_verified": None,
            "verified": 0,
            "gaps": 0,
        }

    def test_chain_not_found(self, client):
        assert client.get("/api/custody/chain/nonexistent").status_code == 404

    def test_verify_not_found(self, client):
        assert client.get("/api/custody/verify/nonexistent").status_code == 404

    def test_chain_carries_the_identity_and_no_bodies(self, client, node):
        _register(client, node)
        entry, batch = node.next_window()
        _ingest(client, _NODE, entry, batch)

        body = _chain(client)

        assert body["identity"]["node_ref"] == _NODE
        assert body["identity"]["public_key_fingerprint"] == node.register_body["fingerprint"]
        assert "entries" not in body
        assert entry["signature"] not in json.dumps(body)


_HOSTILE_STORED = pytest.mark.parametrize(
    "value",
    ["\ud800", "x" * 1_000_000, "x" * 65, 5, ["2027"], None],
    ids=["lone-surrogate", "1mb", "65-chars", "int", "list", "none"],
)


def _legacy_row(value) -> dict:
    """A row as the old TCP path stored it: whatever the client sent."""
    return {
        "node_id": _NODE,
        "hour_utc": "x",
        "window_start": value,
        "window_end": value,
        "entry_hash": "e" * 64,
        "_verified": False,
    }


class TestLegacyRowsOnTheReadRoutes:
    @_HOSTILE_STORED
    def test_status_and_chain_leave_a_hostile_window_end_out(self, client, value):
        state.chain_entries[_NODE] = [_legacy_row(value)]

        assert _status(client)["latest_window_end"] is None
        assert _chain(client)["latest_window_end"] is None

    @_HOSTILE_STORED
    def test_verify_labels_a_hostile_window_start_with_a_question_mark(self, client, node, value):
        _register(client, node)
        state.chain_entries[_NODE] = [_legacy_row(value)]

        r = client.get(f"/api/custody/verify/{_NODE}")

        assert r.status_code == 200
        (issue,) = r.json()["issues"]
        assert issue.startswith("Entry 0 (?): malformed entry: ")

    def test_a_short_stored_string_is_published_as_is(self, client, node):
        _register(client, node)
        state.chain_entries[_NODE] = [_legacy_row("x" * 64)]

        assert _status(client)["latest_window_end"] == "x" * 64
        assert _chain(client)["latest_window_end"] == "x" * 64
        (issue,) = client.get(f"/api/custody/verify/{_NODE}").json()["issues"]
        assert issue.startswith(f"Entry 0 ({'x' * 64}): malformed entry: ")


class TestCustodyVerifyChain:
    def test_a_received_chain_verifies(self, client, node):
        _register(client, node)
        for _ in range(3):
            _ingest(client, _NODE, *node.next_window())

        r = client.get(f"/api/custody/verify/{_NODE}")

        assert r.status_code == 200
        assert r.json() == {"node_ref": _NODE, "chain_length": 3, "valid": True, "issues": []}

    def test_legacy_rows_are_malformed_at_their_stored_index(self, client, node):
        _register(client, node)
        state.chain_entries[_NODE] = [dict(_LEGACY_ROW), dict(_LEGACY_ROW)]
        _ingest(client, _NODE, *node.next_window())
        _ingest(client, _NODE, *node.next_window())

        r = client.get(f"/api/custody/verify/{_NODE}")

        assert r.status_code == 200
        body = r.json()
        assert body["chain_length"] == 4
        assert body["valid"] is False
        assert len(body["issues"]) == 2
        for i, issue in enumerate(body["issues"]):
            assert issue.startswith(f"Entry {i} (?): malformed entry: ")

    def test_an_issue_after_legacy_rows_carries_its_stored_index(self, client, node):
        _register(client, node)
        state.chain_entries[_NODE] = [dict(_LEGACY_ROW), dict(_LEGACY_ROW)]
        _ingest(client, _NODE, *node.next_window())
        second, b2 = node.next_window()
        _ingest(client, _NODE, second, b2)
        held = state.chain_entries[_NODE][3]
        held["signature"] = held["signature"][:-1] + ("0" if held["signature"][-1] != "0" else "1")

        body = client.get(f"/api/custody/verify/{_NODE}").json()

        assert [i.split(" (")[0] for i in body["issues"]] == ["Entry 0", "Entry 1", "Entry 3"]
        assert body["issues"][2] == f"Entry 3 ({second['window_start']}): invalid signature"

    def test_verify_chain_no_public_key_returns_400(self, client, node):
        entry, _ = node.next_window()
        state.chain_entries[_NODE] = [entry | {"_verified": True}]

        r = client.get(f"/api/custody/verify/{_NODE}")

        assert r.status_code == 400
        assert "No public key registered" in r.json()["detail"]

    def test_verify_chain_exception_returns_500(self, client, node):
        _register(client, node)
        _ingest(client, _NODE, *node.next_window())

        with patch("routes.custody.HashChainVerifier", side_effect=Exception("corrupt chain")):
            r = client.get(f"/api/custody/verify/{_NODE}")

        assert r.status_code == 500
        assert "Chain verification failed" in r.json()["detail"]
