"""A node that registers with null coordinates is carried, not placed.

The sibling of test_unpositioned_registration, which covers coordinates that
are absent. Here they are explicitly null, which is what contract 1.1.3 added:
the node is counted and visible in the dashboard, and takes no part in the map
or the solver.
"""

import pytest

from core import state
from services.node_config import position_status

_CONFIG = {
    "rx_lat": None,
    "rx_lon": None,
    "rx_alt_ft": None,
    "tx_lat": None,
    "tx_lon": None,
    "tx_alt_ft": None,
    "tx_callsign": "WSPA",
    "fc_hz": 195e6,
    "fs_hz": 2.4e6,
    "beam_width_deg": None,
    "beam_azimuth_deg": None,
    "max_range_km": 150.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 10.0,
    "doppler_tolerance_hz": 5.0,
}


# Enumerated explicitly rather than scanned from connected_nodes: not every
# test below registers through it, so cleanup can't be derived from it.
_NODE_IDS = ("test-null-1", "test-null-2", "test-null-3")


@pytest.fixture(autouse=True)
def _clean():
    yield
    for node_id in _NODE_IDS:
        state.connected_nodes.pop(node_id, None)
        state.node_pipelines.pop(node_id, None)
        state.node_associator.unregister_node(node_id)
        state.node_analytics.retire_node(node_id)


def test_positionless_node_is_counted_but_not_placed():
    node_id = "test-null-1"
    state.node_analytics.register_node(node_id, dict(_CONFIG))
    state.node_associator.register_node(node_id, dict(_CONFIG))

    assert node_id in state.node_analytics.metrics
    assert "detection_area" not in state.node_analytics.get_node_summary(node_id)
    assert all(node_id not in (z["node_a"], z["node_b"]) for z in state.node_associator.get_overlap_summary())

    # A metrics entry alone doesn't show frames are counted, which is the
    # promise this feature makes to the owner of a node we cannot place.
    assert state.node_analytics.record_detection_frame(node_id, {"timestamp": 1.0, "detections": []}) is True
    assert state.node_analytics.metrics[node_id].total_frames == 1


def test_positionless_node_builds_no_solver_pipeline():
    """The never-solve half: a positionless node builds no solver pipeline.

    Holds today because get_or_create_node_pipeline's guard at
    frame_processor.py:171, `if cfg.get("rx_lat") and cfg.get("tx_lat")`,
    falls through to the shared default pipeline only because None is falsy:
    a known truthiness bug, 86cbavanm.

    Worth pinning because a correct repair to `is not None` preserves this
    fall-through, but a repair that tests key presence (`"rx_lat" in cfg`)
    instead of value nullity does not: the key is present, carrying None, so
    pipeline construction proceeds and raises a TypeError.
    """
    from services.frame_processor import get_or_create_node_pipeline

    node_id = "test-null-3"
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {
            "config": dict(_CONFIG),
            "config_hash": "",
            "status": "active",
            "last_heartbeat": None,
            "peer": "test",
            "is_synthetic": False,
            "capabilities": {},
        }

    # It falls back to the shared default rather than returning None, so assert
    # identity against a sentinel: a node-specific pipeline would be a new
    # object, and would also register itself in state.node_pipelines.
    sentinel = object()
    assert get_or_create_node_pipeline(node_id, sentinel) is sentinel
    assert node_id not in state.node_pipelines


def test_position_status_reaches_the_nodes_payload():
    import orjson

    from services.tasks.analytics_refresh import _refresh_analytics_and_nodes

    node_id = "test-null-2"
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {
            "config": dict(_CONFIG),
            "config_hash": "",
            "status": "active",
            "last_heartbeat": None,
            "peer": "test",
            "is_synthetic": False,
            "capabilities": {},
        }
    _refresh_analytics_and_nodes()
    payload = orjson.loads(state.latest_nodes_bytes)
    assert payload["nodes"][node_id]["position_status"] == "missing_both"
    assert payload["nodes"][node_id]["location"]["rx_lat"] is None
    assert position_status(_CONFIG) == "missing_both"
