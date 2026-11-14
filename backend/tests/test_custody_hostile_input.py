"""Hostile input on the custody routes and the radar TCP connection.

Malformed input is a 422 on HTTP and a rejected CHAIN_ENTRY_ACK or KEY_ACK on
TCP; it is never a 500 and never an exception out of the connection handler.
"""

import asyncio
import json

import pytest

from config.constants import CUSTODY_BATCH_MAX_CHARS
from core import state
from services.tcp_handler import handle_tcp_client
from tests.custody_helpers import ChainNode
from tests.tcp_helpers import FakeReader, FakeWriter

_HEADERS = {"X-API-Key": "test-key-abc123", "content-type": "application/json"}
_NODE = "test-hostile-01"
_ENTRY = '{"node_id":"test-hostile-01","window_end":1}'
_CHAIN = "/api/custody/chain-entry"
_REGISTER = "/api/custody/register"
_MAX_RESPONSE_BYTES = 4096

_HOSTILE_BODIES = [
    pytest.param(_CHAIN, '{"entry":' + _ENTRY + ',"batch":NaN}', id="chain-batch-nan"),
    pytest.param(_CHAIN, '{"entry":' + _ENTRY + ',"batch":"\\ud800"}', id="chain-batch-lone-surrogate"),
    pytest.param(
        _CHAIN,
        '{"entry":{"node_id":"test-\\ud800","window_end":1},"batch":""}',
        id="chain-node-id-lone-surrogate",
    ),
    pytest.param(_CHAIN, '{"entry":{"\\ud800":1},"batch":7}', id="chain-entry-key-lone-surrogate"),
    pytest.param(
        _CHAIN,
        '{"entry":' + _ENTRY + ',"batch":' + json.dumps("x" * (CUSTODY_BATCH_MAX_CHARS + 1)) + "}",
        id="chain-batch-over-long",
    ),
    pytest.param(
        _REGISTER,
        '{"node_id":"test-hostile-01","public_key_pem":NaN,"fingerprint":"f"}',
        id="register-key-nan",
    ),
    pytest.param(
        _REGISTER,
        '{"node_id":"test-hostile-01","public_key_pem":"k","fingerprint":"\\ud800"}',
        id="register-fingerprint-lone-surrogate",
    ),
]


@pytest.mark.parametrize(("path", "body"), _HOSTILE_BODIES)
def test_a_hostile_body_is_a_small_422(client, path, body):
    r = client.post(path, content=body.encode("ascii"), headers=_HEADERS)

    assert r.status_code == 422
    assert len(r.content) < _MAX_RESPONSE_BYTES
    detail = r.json()["detail"]
    assert detail
    assert all({"input", "ctx"}.isdisjoint(item) for item in detail)


def test_the_node_api_still_answers_in_its_taxonomy(node_client, registered_node):
    token, _node_id = registered_node

    r = node_client.post(
        "/v1/nodes/heartbeat",
        content=b'{"state":NaN}',
        headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
    )

    assert r.status_code == 400
    assert r.json()["error"] == "invalid_body"


def test_a_hostile_line_does_not_end_the_connection(tmp_path):
    node = ChainNode(tmp_path, _NODE)
    entry, batch = node.next_window()
    hostile = [b"[" * 100000, b"9" * 5000, b"[1, 2]", b'"text"', b"42"]
    valid = [
        {"type": "HELLO", "node_id": _NODE, "version": "1.0"},
        {"type": "REGISTER_KEY", **node.register_body},
        {"type": "CHAIN_ENTRY", "entry": entry, "batch": batch},
    ]
    lines = b"".join(line + b"\n" for line in hostile) + b"".join(json.dumps(m).encode() + b"\n" for m in valid)
    reader = FakeReader([lines[i : i + 4096] for i in range(0, len(lines), 4096)])
    writer = FakeWriter()

    asyncio.run(handle_tcp_client(reader, writer))

    acks = [m for m in writer.messages() if m["type"] == "CHAIN_ENTRY_ACK"]
    assert [(a["node_id"], a["verified"]) for a in acks] == [(_NODE, True)]


def _connection(*msgs: dict) -> list[dict]:
    lines = b"".join(json.dumps(m).encode() + b"\n" for m in msgs)
    reader = FakeReader([lines[i : i + 4096] for i in range(0, len(lines), 4096)])
    writer = FakeWriter()
    asyncio.run(handle_tcp_client(reader, writer))  # type: ignore[arg-type]
    return writer.messages()


_KEY = {"type": "REGISTER_KEY", "node_id": _NODE, "public_key_pem": "k"}


@pytest.mark.parametrize(
    "msg",
    [
        _KEY | {"fingerprint": "\ud800"},
        _KEY | {"serial_number": "\udcff"},
        _KEY | {"node_id": "test-\ud800"},
        _KEY | {"fingerprint": "f" * 257},
        _KEY | {"public_key_pem": "k" * 8193},
        _KEY | {"signing_mode": "SECRET" * 10},
        _KEY | {"signing_mode": 5},
        _KEY | {"fingerprint": ["SECRET"]},
        {k: v for k, v in _KEY.items() if k != "public_key_pem"},
        _KEY | {"public_key_pem": ""},
        _KEY | {"node_id": ""},
        _KEY | {"node_id": None},
    ],
    ids=[
        "fingerprint-lone-surrogate",
        "serial-lone-surrogate",
        "node-id-lone-surrogate",
        "fingerprint-over-long",
        "key-over-long",
        "signing-mode-over-long",
        "signing-mode-int",
        "fingerprint-list",
        "key-missing",
        "key-empty",
        "node-id-empty",
        "node-id-null",
    ],
)
def test_a_hostile_key_registration_is_rejected_and_nothing_stored(client, msg):
    msgs = _connection({"type": "HELLO", "node_id": _NODE, "version": "1.0"}, msg)

    assert msgs == [{"type": "KEY_ACK", "status": "rejected", "node_id": None, "reason": "invalid key registration"}]
    assert state.node_identities == {}
    assert client.get("/api/custody/status").status_code == 200


def test_a_key_registration_without_any_node_id_is_rejected():
    """Neither a HELLO nor the message names a node: refused for the HELLO."""
    msg = {k: v for k, v in _KEY.items() if k != "node_id"}

    msgs = _connection(msg)

    assert msgs == [{"type": "KEY_ACK", "status": "rejected", "node_id": None, "reason": "no HELLO on this connection"}]
    assert state.node_identities == {}
