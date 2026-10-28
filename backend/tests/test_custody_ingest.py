"""Tests for services/custody.py below the transports: entry parsing, what
counts as a verified window, and the ingest lock."""

import hashlib
import json
import threading

import pytest
from retina_custody.canonical import canonicalize
from retina_custody.models import NodeIdentity

from core import state
from services import custody
from services.custody import MalformedEntry, ingest_chain_entry, is_verified_window, parse_entry
from tests.custody_helpers import ChainNode

_NODE = "test-cust-1"
_STAMPS = {"_verified", "_reason", "_batch_verified", "_gap", "_received_at"}
_STALE = "stale window: not after the last verified window"


@pytest.fixture()
def node(tmp_path):
    n = ChainNode(tmp_path, _NODE)
    body = n.register_body
    state.node_identities[_NODE] = NodeIdentity(
        node_id=_NODE,
        public_key_pem=body["public_key_pem"],
        public_key_fingerprint=body["fingerprint"],
        serial_number=body["serial_number"],
        signing_mode=body["signing_mode"],
    )
    return n


def _altered(batch: str) -> str:
    frames = json.loads(batch)
    frames[0]["snr"][0] += 1.0
    return json.dumps(frames, sort_keys=True, separators=(",", ":"))


def _resigned(node: ChainNode, entry: dict, **changes) -> dict:
    """The entry with `changes`, hashed and signed again with the node's key."""
    data = {k: v for k, v in (entry | changes).items() if k not in ("entry_hash", "signature", "signing_mode")}
    preimage = canonicalize(data)
    return data | {
        "entry_hash": hashlib.sha256(preimage).hexdigest(),
        "signature": node.crypto.sign_hex(preimage),
        "signing_mode": node.crypto.signing_mode,
    }


def _tampered_tail(node: ChainNode) -> list[tuple[dict, str]]:
    """Three windows: two verified, then the third with an altered batch, so
    the held tail is unverified and anchors nothing."""
    windows = [node.next_window() for _ in range(3)]
    assert ingest_chain_entry(_NODE, *windows[0])["verified"] is True
    assert ingest_chain_entry(_NODE, *windows[1])["verified"] is True
    third, b3 = windows[2]
    assert ingest_chain_entry(_NODE, third, _altered(b3))["verified"] is False
    return windows


class TestParseEntry:
    def test_a_built_entry_parses_unchanged(self, node):
        entry, _ = node.next_window()
        assert parse_entry(entry).to_dict() == entry

    @pytest.mark.parametrize(
        "field",
        [
            "node_id",
            "window_start",
            "window_end",
            "prev_hash",
            "detections_hash",
            "node_config_hash",
            "firmware_version",
            "timestamp_utc",
            "entry_hash",
            "signature",
            "signing_mode",
        ],
    )
    @pytest.mark.parametrize("value", [1, 1.5, None, True, ["SECRET-VALUE"], {"SECRET": "VALUE"}])
    def test_every_required_field_must_be_a_string(self, node, field, value):
        entry, _ = node.next_window()
        with pytest.raises(MalformedEntry) as exc:
            parse_entry(entry | {field: value})
        assert str(exc.value) == f"{field} must be a string"

    @pytest.mark.parametrize("value", ["3", 1.0, True, False, -1, None])
    def test_n_detections_must_be_a_non_negative_int(self, node, value):
        entry, _ = node.next_window()
        with pytest.raises(MalformedEntry, match="^n_detections must be a non-negative integer$"):
            parse_entry(entry | {"n_detections": value})

    def test_zero_detections_is_a_valid_count(self, node):
        entry, _ = node.next_window(n_frames=0)
        assert parse_entry(entry).n_detections == 0

    @pytest.mark.parametrize(("field", "limit"), [("node_id", 128), ("signature", 256), ("tsa_token", 65536)])
    def test_lengths_are_capped(self, node, field, limit):
        entry, _ = node.next_window()
        parse_entry(entry | {field: "x" * limit})
        with pytest.raises(MalformedEntry, match=f"^{field} is too long$"):
            parse_entry(entry | {field: "SECRET" + "x" * limit})

    def test_no_message_quotes_a_value(self, node):
        entry, _ = node.next_window()
        for bad in (entry | {"prev_hash": 42}, entry | {"firmware_version": "SECRET" * 100}, {"node_id": "SECRET"}):
            with pytest.raises(MalformedEntry) as exc:
                parse_entry(bad)
            assert "SECRET" not in str(exc.value)
            assert "42" not in str(exc.value)

    def test_unknown_keys_are_dropped(self, node):
        entry, _ = node.next_window()
        assert "_verified" not in parse_entry(entry | {"_verified": True}).to_dict()

    @pytest.mark.parametrize("field", ["window_start", "window_end"])
    @pytest.mark.parametrize(
        "value",
        ["2027-01-15T08:00:00", "2027-01-15", "", "SECRET", "2027-13-01T00:00:00Z", "1800000000"],
    )
    def test_a_window_time_must_be_iso_8601_with_an_offset(self, node, field, value):
        entry, _ = node.next_window()
        with pytest.raises(MalformedEntry) as exc:
            parse_entry(entry | {field: value})
        assert str(exc.value) == f"{field} must be an ISO 8601 time with a UTC offset"

    def test_a_window_time_may_carry_any_offset(self, node):
        entry, _ = node.next_window()
        assert parse_entry(entry | {"window_end": "2027-01-15T10:01:00+02:00"}).window_end.endswith("+02:00")


class TestIsVerifiedWindow:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            ({"window_end": "2026-10-19T14:15:00Z", "_verified": True}, True),
            ({"window_end": "2026-10-19T14:15:00Z", "_verified": False}, False),
            ({"window_end": "2026-10-19T14:15:00Z", "_verified": "true"}, False),
            ({"window_end": "2026-10-19T14:15:00Z", "_verified": 1}, False),
            ({"hour_utc": "2026-10-19T14:00:00Z", "_verified": True}, False),
            ({}, False),
        ],
    )
    def test_only_a_verified_windowed_row_counts(self, row, expected):
        assert is_verified_window(row) is expected


class TestIngest:
    def test_a_malformed_entry_stores_nothing(self, node):
        entry, batch = node.next_window()
        with pytest.raises(MalformedEntry):
            ingest_chain_entry(_NODE, entry | {"n_detections": True}, batch)
        assert _NODE not in state.chain_entries

    def test_a_bad_batch_is_refused_before_the_entry_is_read(self):
        with pytest.raises(MalformedEntry, match="^batch must be a string$"):
            ingest_chain_entry(_NODE, None, b"[]")

    def test_the_stored_row_is_the_entry_and_the_verdict(self, node):
        entry, batch = node.next_window()

        ingest_chain_entry(_NODE, entry, batch)

        (stored,) = state.chain_entries[_NODE]
        assert set(stored) == set(entry) | _STAMPS

    def test_unknown_keys_in_the_entry_are_not_stored(self, node):
        entry, batch = node.next_window()

        ingest_chain_entry(_NODE, entry | {"extra": "x", "hour_utc": "x", "_verified": True, "_gap": False}, batch)

        (stored,) = state.chain_entries[_NODE]
        assert set(stored) == set(entry) | _STAMPS
        assert stored["_verified"] is True  # the server's verdict, not the client's stamp
        assert stored["_gap"] is False

    def test_the_cap_replaces_the_list(self, node, monkeypatch):
        """A snapshot that took the list before the cap never sees it shrink."""
        monkeypatch.setattr(custody, "CHAIN_ENTRIES_MAX_PER_NODE", 2)
        for _ in range(2):
            ingest_chain_entry(_NODE, *node.next_window())
        before = state.chain_entries[_NODE]

        ingest_chain_entry(_NODE, *node.next_window())

        assert state.chain_entries[_NODE] is not before
        assert len(before) == 3
        assert len(state.chain_entries[_NODE]) == 2

    def test_two_ingests_of_one_window_cannot_both_be_verified(self, node, monkeypatch):
        """Both read the tail before either appends unless the read-to-append
        runs under the lock. The interleaving is forced: the first ingest is
        held inside verification until the second has either finished or is
        waiting for the lock."""
        first, b1 = node.next_window()
        assert ingest_chain_entry(_NODE, first, b1)["verified"] is True
        second, b2 = node.next_window()

        a_verifying = threading.Event()
        release_a = threading.Event()
        b_reached_lock_or_done = threading.Event()
        real_lock = custody._ingest_lock

        class TracingLock:
            def __enter__(self):
                if threading.current_thread().name == "ingest-b":
                    b_reached_lock_or_done.set()
                real_lock.acquire()

            def __exit__(self, *exc):
                real_lock.release()

        class HoldingVerifier(custody.HashChainVerifier):
            def verify_entry(self, entry, batch=None, expected_prev_hash=""):
                if threading.current_thread().name == "ingest-a":
                    a_verifying.set()
                    release_a.wait(10)
                return super().verify_entry(entry, batch=batch, expected_prev_hash=expected_prev_hash)

        monkeypatch.setattr(custody, "_ingest_lock", TracingLock())
        monkeypatch.setattr(custody, "HashChainVerifier", HoldingVerifier)
        results: dict[str, dict] = {}

        def run(name: str) -> None:
            results[name] = ingest_chain_entry(_NODE, dict(second), b2)
            b_reached_lock_or_done.set()

        a = threading.Thread(target=run, args=("a",), name="ingest-a")
        b = threading.Thread(target=run, args=("b",), name="ingest-b")
        a.start()
        assert a_verifying.wait(10)
        b.start()
        assert b_reached_lock_or_done.wait(10)
        release_a.set()
        a.join(10)
        b.join(10)

        assert results["a"]["verified"] is True
        assert results["b"]["verified"] is False
        assert results["b"]["reason"].startswith("prev_hash mismatch")
        assert [e["_verified"] for e in state.chain_entries[_NODE]] == [True, True, False]


class TestStaleWindow:
    """An entry the held tail does not anchor must end after the newest
    verified window, or a replay would pass behind one unverified entry."""

    @pytest.mark.parametrize("replayed", [0, 1], ids=["first-window", "last-verified-window"])
    def test_a_replay_behind_an_unverified_tail_is_stale(self, node, replayed):
        windows = _tampered_tail(node)
        entry, batch = windows[replayed]

        ack = ingest_chain_entry(_NODE, entry, batch)

        assert (ack["status"], ack["verified"], ack["reason"], ack["batch_verified"]) == (
            "stored",
            False,
            _STALE,
            False,
        )
        assert [(e["_verified"], e["_reason"]) for e in state.chain_entries[_NODE]][-1] == (False, _STALE)
        assert len(state.chain_entries[_NODE]) == 4

    def test_a_stale_entry_without_a_batch_leaves_the_batch_unchecked(self, node):
        windows = _tampered_tail(node)

        ack = ingest_chain_entry(_NODE, windows[0][0], None)

        assert (ack["verified"], ack["reason"], ack["batch_verified"]) == (False, _STALE, None)

    def test_a_stale_entry_never_reaches_the_verifier(self, node, monkeypatch):
        windows = _tampered_tail(node)
        calls = []

        class SpyVerifier(custody.HashChainVerifier):
            def verify_entry(self, entry, batch=None, expected_prev_hash=""):
                calls.append(entry.entry_hash)
                return super().verify_entry(entry, batch=batch, expected_prev_hash=expected_prev_hash)

        monkeypatch.setattr(custody, "HashChainVerifier", SpyVerifier)

        assert ingest_chain_entry(_NODE, *windows[0])["reason"] == _STALE
        assert calls == []

    def test_the_next_window_after_an_unverified_tail_is_verified(self, node):
        windows = _tampered_tail(node)
        fourth, b4 = node.next_window()

        resent = ingest_chain_entry(_NODE, *windows[2])
        after = ingest_chain_entry(_NODE, fourth, b4)

        assert (resent["verified"], resent["reason"]) == (True, "ok")
        assert (after["verified"], after["reason"]) == (True, "ok")
        assert [e["_verified"] for e in state.chain_entries[_NODE]] == [True, True, False, True, True]

    @pytest.mark.parametrize(
        ("window_end", "stale"),
        [
            # The last verified window ends at 08:02:00Z.
            ("2027-01-15T13:01:00+05:00", True),  # 08:01Z: sorts after as text
            ("2027-01-15T10:02:00+02:00", True),  # 08:02Z: the same instant
            ("2027-01-15T07:03:00-01:00", False),  # 08:03Z: sorts before as text
        ],
    )
    def test_window_ends_are_compared_as_instants(self, node, window_end, stale):
        windows = _tampered_tail(node)
        assert windows[1][0]["window_end"] == "2027-01-15T08:02:00Z"
        third, b3 = windows[2]
        # Linked to the first window rather than the newest verified one, so
        # the entry is not anchored and its window end decides.
        resigned = _resigned(node, third, window_end=window_end, prev_hash=windows[0][0]["entry_hash"])

        ack = ingest_chain_entry(_NODE, resigned, b3)

        assert (ack["verified"], ack["reason"]) == ((False, _STALE) if stale else (True, "ok"))

    def test_an_anchored_replay_keeps_the_verifiers_reason(self, node):
        first, b1 = node.next_window()
        second, b2 = node.next_window()
        ingest_chain_entry(_NODE, first, b1)
        ingest_chain_entry(_NODE, second, b2)

        ack = ingest_chain_entry(_NODE, first, b1)

        assert ack["verified"] is False
        assert ack["reason"].startswith("prev_hash mismatch")

    def test_with_no_verified_window_held_the_verifier_decides(self, node):
        first, b1 = node.next_window()
        ingest_chain_entry(_NODE, first, _altered(b1))

        assert ingest_chain_entry(_NODE, first, b1)["verified"] is True

    @pytest.mark.parametrize(
        "window_end",
        [5, None, "\ud800", "2027-01-15T08:02:00", "x" * 1_000_000],
        ids=["int", "none", "lone-surrogate", "naive", "1mb"],
    )
    def test_a_held_verified_window_whose_end_does_not_parse_counts_as_later(self, node, window_end):
        """Rows restored from a snapshot can hold anything; one that cannot be
        compared is not a reason to verify a window that may be a replay."""
        state.chain_entries[_NODE] = [
            {"window_end": window_end, "entry_hash": "e" * 64, "_verified": True},
            {"window_end": "2027-01-15T08:03:00Z", "entry_hash": "f" * 64, "_verified": False},
        ]

        ack = ingest_chain_entry(_NODE, *node.next_window())

        assert (ack["verified"], ack["reason"]) == (False, _STALE)


class TestAnchoredBehindUnverifiedRows:
    """Behind unverified rows, an entry whose prev_hash names the newest
    verified window is linked to it instead of being held to the stale rule,
    so one window dated wrong cannot lock out the node's genuine entries."""

    _BATCH = '[{"x":1}]'

    def test_a_future_window_does_not_lock_out_its_successors(self, node):
        real = node.clock.t
        first = node.sign(self._BATCH, n_detections=1)
        assert ingest_chain_entry(_NODE, first, self._BATCH)["verified"] is True
        node.clock.t = real + 10 * 365 * 86400  # one window under a clock ten years fast
        future = node.sign(self._BATCH, n_detections=1, prev_hash=first["entry_hash"])
        assert ingest_chain_entry(_NODE, future, self._BATCH)["verified"] is True
        resent = ingest_chain_entry(_NODE, future, self._BATCH)
        assert resent["verified"] is False
        assert resent["reason"].startswith("prev_hash mismatch")

        acks, prev = [], future["entry_hash"]
        for k in range(1, 4):  # the clock back to normal
            node.clock.t = real + 60 * k
            entry = node.sign(self._BATCH, n_detections=1, prev_hash=prev)
            acks.append(ingest_chain_entry(_NODE, entry, self._BATCH))
            prev = entry["entry_hash"]

        assert [(a["verified"], a["reason"]) for a in acks] == [(True, "ok")] * 3
        assert [e["_gap"] for e in state.chain_entries[_NODE][-3:]] == [True, False, False]

    def test_the_successor_of_the_newest_verified_window_is_linked_to_it(self, node):
        windows = [node.next_window() for _ in range(3)]
        for entry, batch in windows:
            assert ingest_chain_entry(_NODE, entry, batch)["verified"] is True
        newest, batch = windows[2]
        replays = [ingest_chain_entry(_NODE, *windows[0]) for _ in range(2)]
        assert [r["reason"].split(":")[0] for r in replays] == ["prev_hash mismatch", "stale window"]
        # Over the same window as the newest verified one: linked, not stale.
        successor = _resigned(node, newest, prev_hash=newest["entry_hash"])
        assert (successor["window_start"], successor["window_end"]) == (newest["window_start"], newest["window_end"])

        ack = ingest_chain_entry(_NODE, successor, batch)

        assert (ack["verified"], ack["reason"], ack["batch_verified"]) == (True, "ok", True)
        assert state.chain_entries[_NODE][-1]["_gap"] is True

    def test_an_anchored_entry_still_needs_the_nodes_signature(self, node, tmp_path):
        windows = _tampered_tail(node)
        impostor = ChainNode(tmp_path / "impostor", _NODE)
        third, b3 = windows[2]
        assert third["prev_hash"] == windows[1][0]["entry_hash"]

        ack = ingest_chain_entry(_NODE, _resigned(impostor, third), b3)

        assert (ack["verified"], ack["reason"]) == (False, "invalid signature")

    def test_an_empty_held_hash_anchors_nothing(self, node):
        state.chain_entries[_NODE] = [
            {"window_end": "2099-01-01T00:00:00Z", "entry_hash": "", "_verified": True},
            {"window_end": "2099-01-01T00:01:00Z", "entry_hash": "f" * 64, "_verified": False},
        ]
        entry, batch = node.next_window()

        ack = ingest_chain_entry(_NODE, _resigned(node, entry, prev_hash=""), batch)

        assert (ack["verified"], ack["reason"]) == (False, _STALE)
