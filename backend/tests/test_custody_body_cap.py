"""POST /api/custody/chain-entry takes bodies up to its own cap, not the global one."""

import pytest
from fastapi.testclient import TestClient

import main
from config.constants import CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES
from tests.custody_helpers import ChainNode

# No lifespan: the middleware answers before any route runs.
client = TestClient(main.app, raise_server_exceptions=False)

_HEADERS = {"X-API-Key": "test-key-abc123"}
_PATH = "/api/custody/chain-entry"
_OVER = {"Content-Length": str(CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES + 1), "Content-Type": "application/json"}


@pytest.mark.parametrize("path", [_PATH, _PATH + "/"])
def test_a_body_over_the_cap_is_refused_in_the_detail_shape(path):
    r = client.post(path, content=b"{}", headers=_OVER | _HEADERS)
    assert r.status_code == 413
    assert r.json() == {"detail": f"Request body too large (max {CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES} bytes)"}


def test_a_body_above_the_global_cap_is_not_refused_on_this_path(tmp_path):
    """A batch of mostly quotes doubles when JSON escapes it, so a 4 MiB batch
    is an 8 MiB body: over the 5 MiB default, under this path's cap."""
    node = ChainNode(tmp_path, "test-cust-cap")
    batch = '"' * (4 * 1024 * 1024)
    entry = node.sign(batch, n_detections=1)

    r = client.post(_PATH, json={"node_id": node.node_id, "entry": entry, "batch": batch}, headers=_HEADERS)

    assert int(r.request.headers["content-length"]) > main._MAX_BODY_BYTES
    assert r.status_code != 413


def test_a_body_over_the_global_cap_on_another_path_is_still_refused():
    r = client.post(
        "/api/custody/register",
        content=b"{}",
        headers={"Content-Length": str(main._MAX_BODY_BYTES + 1), "Content-Type": "application/json"} | _HEADERS,
    )
    assert r.status_code == 413
    assert r.json() == {"detail": f"Request body too large (max {main._MAX_BODY_BYTES} bytes)"}
