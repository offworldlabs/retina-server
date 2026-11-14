"""The test dashboard's chain-of-custody block counts verified windows."""

import json

from core import state
from routes.test import _build_dashboard_data


def _windowed(node_id: str, n: int, verified: bool) -> dict:
    return {
        "node_id": node_id,
        "window_start": f"2026-10-19T00:0{n}:00Z",
        "window_end": f"2026-10-19T00:0{n + 1}:00Z",
        "entry_hash": f"{n:02d}" * 32,
        "_verified": verified,
    }


def test_verified_entries_total_counts_only_verified_windows():
    state.chain_entries["test-dash-1"] = [
        _windowed("test-dash-1", 1, True),
        _windowed("test-dash-1", 2, False),
        {"node_id": "test-dash-1", "entry_hash": "e" * 64, "_verified": True},
    ]
    state.chain_entries["test-dash-2"] = [_windowed("test-dash-2", 1, True)]
    try:
        custody = json.loads(_build_dashboard_data())["chain_of_custody"]
    finally:
        state.chain_entries.pop("test-dash-1", None)
        state.chain_entries.pop("test-dash-2", None)

    assert custody["verified_entries_total"] == 2
    assert custody["chain_entries_total"] == 4
    assert custody["nodes_with_chains"] == 2
