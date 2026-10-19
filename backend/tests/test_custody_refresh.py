"""The analytics refresh must leave connected nodes' custody chains alone."""

from retina_custody.models import NodeIdentity

from core import state
from services import custody
from services.tasks import analytics_refresh
from tests.custody_helpers import ChainNode

_KEYED = "test-cust-01"
_KEYLESS = "test-cust-02"


class _Analytics:
    def get_all_summaries(self):
        return {}

    def get_cross_node_analysis(self):
        return {}


class _Assoc:
    node_geometries = {}

    def get_overlap_summary(self):
        return []


def _run_refresh(monkeypatch):
    for name in (
        "_refresh_coverage_constraints",
        "_refresh_accuracy_stats",
        "_refresh_missed_detections",
        "_refresh_mlat_verification",
        "_evict_stale_pipelines",
        "_refresh_node_verification",
    ):
        monkeypatch.setattr(analytics_refresh, name, lambda *a, **k: None)
    monkeypatch.setattr(state, "node_analytics", _Analytics())
    monkeypatch.setattr(state, "node_associator", _Assoc())
    analytics_refresh._refresh_analytics_and_nodes()


def test_refresh_leaves_a_connected_nodes_chain_intact(tmp_path, monkeypatch):
    node = ChainNode(tmp_path, _KEYED)
    body = node.register_body
    state.node_identities[_KEYED] = NodeIdentity(
        node_id=_KEYED,
        public_key_pem=body["public_key_pem"],
        public_key_fingerprint=body["fingerprint"],
        serial_number=body["serial_number"],
        signing_mode=body["signing_mode"],
    )
    monkeypatch.setattr(
        state,
        "connected_nodes",
        {_KEYED: {"status": "connected"}, _KEYLESS: {"status": "connected"}},
    )

    first, first_batch = node.next_window()
    assert custody.ingest_chain_entry(_KEYED, first, first_batch)["verified"] is True

    _run_refresh(monkeypatch)

    second, second_batch = node.next_window()
    assert custody.ingest_chain_entry(_KEYED, second, second_batch)["verified"] is True

    rows = state.chain_entries[_KEYED]
    assert [r["entry_hash"] for r in rows] == [first["entry_hash"], second["entry_hash"]]
    assert all("window_end" in r for r in rows)
    assert custody.is_verified_window(rows[1])
    assert rows[1]["_gap"] is False


def test_refresh_does_not_register_or_seed_a_node_without_a_key(monkeypatch):
    monkeypatch.setattr(state, "connected_nodes", {_KEYLESS: {"status": "connected"}})

    _run_refresh(monkeypatch)

    assert _KEYLESS not in state.node_identities
    assert _KEYLESS not in state.chain_entries
