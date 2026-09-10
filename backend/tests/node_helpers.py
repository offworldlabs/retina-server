"""Put a node into the in-process registries the way an entry point does.

A test that hand-seeds only `state.connected_nodes`, or only the associator,
gets a node the frame path treats as half-registered: `get_or_create_node_pipeline`
returns None and every per-node branch downstream of it is skipped, so the test
passes without entering the code it names.
"""

from core import state
from services import node_registration
from services.node_config import canonical_config
from services.tcp_handler import is_synthetic_node


def register_test_node(node_id: str, config: dict, **overrides) -> dict:
    """Register `config` for `node_id`, and return the canonical form stored.

    The same two steps every writer of state.connected_nodes takes: store the
    canonical config, then register that same config with analytics and the
    associator.
    """
    canonical = canonical_config(config)
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {
            "config_hash": "",
            "config": canonical,
            "status": "active",
            "last_heartbeat": "",
            "peer": "test",
            "is_synthetic": is_synthetic_node(node_id),
            "capabilities": {},
            **overrides,
        }
    node_registration.register_node_blocking(node_id, canonical)
    return canonical
