"""The /api/custody/verify verdict cache.

The route is public and runs one signature check per held entry, so its
verdict is computed once per change to what it depends on: the node's held
chain and the keys the entries were checked against. A cached answer is the
same body an uncached run gives.
"""

from unittest.mock import patch

import pytest

from core import state
from routes import custody as custody_routes
from services import custody
from tests.custody_helpers import ChainNode

_HEADERS = {"X-API-Key": "test-key-abc123"}
_NODE = "test-vcache-01"
_OTHER = "test-vcache-02"

# A row of the old hourly chain, as a snapshot written before windowed
# entries restores it.
_LEGACY_ROW = {
    "node_id": _NODE,
    "hour_utc": "2026-09-09T00:00:00Z",
    "prev_hash": "0" * 64,
    "entry_hash": "e" * 64,
    "_verified": True,
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    custody_routes._reset_for_tests()
    yield
    custody_routes._reset_for_tests()


@pytest.fixture()
def node(tmp_path):
    return ChainNode(tmp_path, _NODE)


@pytest.fixture()
def runs(monkeypatch):
    """The length of the list each verify_chain run by the route was given."""
    calls: list[int] = []

    class CountingVerifier(custody_routes.HashChainVerifier):
        def verify_chain(self, entries, batches=None):
            calls.append(len(entries))
            return super().verify_chain(entries, batches)

    monkeypatch.setattr(custody_routes, "HashChainVerifier", CountingVerifier)
    return calls


def _register(client, node: ChainNode) -> None:
    assert client.post("/api/custody/register", json=node.register_body, headers=_HEADERS).status_code == 200


def _ingest(client, entry: dict, batch: str | None = None, node_id: str = _NODE) -> dict:
    body = {"node_id": node_id, "entry": entry} | ({} if batch is None else {"batch": batch})
    r = client.post("/api/custody/chain-entry", json=body, headers=_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _verify(client, node_id: str = _NODE) -> dict:
    r = client.get(f"/api/custody/verify/{node_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _uncached(client) -> dict:
    custody_routes._reset_for_tests()
    return _verify(client)


def test_two_requests_with_no_change_verify_once(client, node, runs):
    _register(client, node)
    for _ in range(3):
        _ingest(client, *node.next_window())

    first = _verify(client)
    second = _verify(client)

    assert runs == [3]
    assert first == second == {"node_ref": _NODE, "chain_length": 3, "valid": True, "issues": []}


def test_an_append_recomputes(client, node, runs):
    _register(client, node)
    for _ in range(2):
        _ingest(client, *node.next_window())
    _verify(client)

    _ingest(client, *node.next_window())
    body = _verify(client)

    assert runs == [2, 3]
    assert body == {"node_ref": _NODE, "chain_length": 3, "valid": True, "issues": []}


def test_a_new_key_recomputes(client, node, runs, tmp_path):
    _register(client, node)
    windows = [node.next_window() for _ in range(2)]
    for entry, batch in windows:
        _ingest(client, entry, batch)
    assert _verify(client)["valid"] is True

    _register(client, ChainNode(tmp_path / "rekeyed", _NODE))
    body = _verify(client)

    assert runs == [2, 2]
    assert body["issues"] == [f"Entry {i} ({e['window_start']}): invalid signature" for i, (e, _) in enumerate(windows)]
    assert body == _uncached(client)


def test_at_the_cap_a_resent_tail_recomputes(client, node, runs, monkeypatch):
    """The count and the tail's entry_hash stay the same: the resent entry is
    stored with the hash of the one it repeats, and the cap drops the oldest."""
    monkeypatch.setattr(custody, "CHAIN_ENTRIES_MAX_PER_NODE", 3)
    _register(client, node)
    windows = [node.next_window() for _ in range(3)]
    for entry, batch in windows:
        _ingest(client, entry, batch)
    assert _verify(client)["valid"] is True
    tail_hash = state.chain_entries[_NODE][-1]["entry_hash"]

    assert _ingest(client, *windows[2])["reason"].startswith("prev_hash mismatch")
    assert (len(state.chain_entries[_NODE]), state.chain_entries[_NODE][-1]["entry_hash"]) == (3, tail_hash)
    body = _verify(client)

    assert runs == [3, 3]
    (issue,) = body["issues"]
    assert issue.startswith(f"Entry 2 ({windows[2][0]['window_start']}): prev_hash mismatch")
    assert body == _uncached(client)


def test_a_cached_body_with_issues_is_the_uncached_body(client, node, runs):
    _register(client, node)
    state.chain_entries[_NODE] = [dict(_LEGACY_ROW), dict(_LEGACY_ROW)]
    _ingest(client, *node.next_window())
    second, b2 = node.next_window()
    _ingest(client, second, b2)
    held = state.chain_entries[_NODE][3]
    held["signature"] = held["signature"][:-1] + ("0" if held["signature"][-1] != "0" else "1")

    first = _verify(client)
    cached = _verify(client)
    assert runs == [2]
    uncached = _uncached(client)

    assert runs == [2, 2]
    assert first == cached == uncached
    assert [i.split(" (")[0] for i in first["issues"]] == ["Entry 0", "Entry 1", "Entry 3"]
    assert first["issues"][2] == f"Entry 3 ({second['window_start']}): invalid signature"


def test_a_failure_is_not_cached(client, node):
    _register(client, node)
    _ingest(client, *node.next_window())

    with patch("routes.custody.HashChainVerifier", side_effect=Exception("corrupt chain")):
        assert client.get(f"/api/custody/verify/{_NODE}").status_code == 500

    assert _verify(client) == {"node_ref": _NODE, "chain_length": 1, "valid": True, "issues": []}


def test_a_chain_no_longer_held_leaves_the_cache_at_the_next_miss(client, node, tmp_path):
    other = ChainNode(tmp_path, _OTHER)
    for n in (node, other):
        _register(client, n)
        entry, batch = n.next_window()
        _ingest(client, entry, batch, node_id=n.node_id)
    _verify(client)
    assert _NODE in custody_routes._verdicts

    state.chain_entries.pop(_NODE)
    _verify(client, _OTHER)

    assert set(custody_routes._verdicts) == {_OTHER}
