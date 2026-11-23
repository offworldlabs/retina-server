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


def test_the_classifier_given_decides_each_id():
    """The append sites pass the handshake verdict, which can disagree with
    the prefix either way."""
    assert nk.node_kind(["node-a", "node-b"], is_synthetic=lambda n: True) == "synthetic"
    assert nk.node_kind("synth-a,test-b", is_synthetic=lambda n: False) == "real"
    assert nk.node_kind(["node-a", "synth-b"], is_synthetic=lambda n: n == "node-a") == "mixed"
    assert nk.node_kind([], is_synthetic=lambda n: True) is None


def test_the_classifier_is_asked_about_each_present_id_as_stripped():
    asked = []
    nk.node_kind([" node-a ", "", "synth-b"], is_synthetic=lambda n: asked.append(n) or False)
    assert asked == ["node-a", "synth-b"]


def test_tcp_handler_still_exports_the_same_test():
    assert tcp_handler.is_synthetic_node is nk.is_synthetic_node
    assert tcp_handler.SYNTHETIC_NODE_PREFIXES is nk.SYNTHETIC_NODE_PREFIXES
