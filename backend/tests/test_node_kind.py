"""The fleet classifier shared by the tracks archive writer and reader."""

import pytest

from services import node_kind as nk
from services import tcp_handler


# Spelled out rather than read from the module, so a dropped prefix fails here.
@pytest.mark.parametrize("prefix", ["synth-", "e2e-", "realnode-", "test-"])
def test_every_synthetic_prefix_counts_as_synthetic(prefix):
    assert nk.node_kind([f"{prefix}a", f"{prefix}b"]) == "synthetic"
    assert nk.node_kind([f"{prefix}a", "node-a"]) == "mixed"


@pytest.mark.parametrize(
    ("ids", "kind"),
    [
        (["node-a", "node-b"], "real"),
        (["synth-a", "e2e-b", "test-c", "realnode-d"], "synthetic"),
        (["node-a", "synth-b"], "mixed"),
        (("node-a",), "real"),
        ("node-a,node-b", "real"),
        ("synth-a,test-b", "synthetic"),
        ("node-a,e2e-b", "mixed"),
        (["node-a", "", "  "], "real"),
        ("synth-a,,", "synthetic"),
        ([], None),
        ("", None),
        (",,", None),
        (None, None),
    ],
)
def test_node_kind(ids, kind):
    assert nk.node_kind(ids) == kind


def test_tcp_handler_still_exports_the_same_test():
    assert tcp_handler.is_synthetic_node is nk.is_synthetic_node
    assert tcp_handler.SYNTHETIC_NODE_PREFIXES is nk.SYNTHETIC_NODE_PREFIXES
