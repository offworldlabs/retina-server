"""A node's heartbeat self-report: stored for administrators, trusted by nothing.

Through the app, as test_node_streaming.py is, because the heartbeat handler is
what writes the report and the admin route is the only thing that reads it.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from core import state
from services.node_rate_limits import token_rate_limiter
from services.node_report_store import ERRORS_KEPT

HEARTBEAT = "/v1/nodes/heartbeat"
REPORTS = "/api/admin/node-reports"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """A fresh allowance per test; see the fixture of the same name in test_node_streaming.py."""
    token_rate_limiter.reset()
    yield
    token_rate_limiter.reset()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _beat(**overrides) -> dict:
    beat = {
        "state": "streaming",
        "uptime_s": 9,
        "boot_id": "k3n8v2qp71ab",
        "config_version": 1,
        "health": {"cpu_pct": 41.5, "disk_free_mb": 20480, "temp_c": 58.0, "blah2": "up", "adsb": "down"},
        "versions": {"owl_os": "1.4.0", "retina_node": "0.9.2", "blah2_image": "0.4.3"},
    }
    beat.update(overrides)
    return beat


def _report(node_client, node_id: str) -> dict:
    (report,) = [r for r in node_client.get(REPORTS).json() if r["node_id"] == node_id]
    return report


async def test_the_heartbeat_s_self_report_is_stored(registered_node, node_client):
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(errors=["blah2-api stopped answering"]))

    report = _report(node_client, node_id)
    assert report["state"] == "streaming"
    assert report["uptime_s"] == 9
    assert report["config_version"] == 1
    assert report["health"]["blah2"] == "up"
    assert report["health"]["adsb"] == "down"
    assert report["versions"]["blah2_image"] == "0.4.3"
    assert [e["message"] for e in report["errors"]] == ["blah2-api stopped answering"]
    assert report["node_ref"]


async def test_a_state_held_across_beats_keeps_the_time_it_began(registered_node, node_client):
    """How long a node has said `starting` is the signal, so a repeat must not reset it."""
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="starting"))
    first = _report(node_client, node_id)
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="starting", uptime_s=69))
    second = _report(node_client, node_id)

    assert second["state_since"] == first["state_since"]
    assert second["received_at"] > first["received_at"]


async def test_a_new_state_starts_its_own_clock(registered_node, node_client):
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="starting"))
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="streaming"))

    report = _report(node_client, node_id)
    assert report["state_since"] == report["received_at"]


async def test_a_restart_starts_the_clock_again_in_the_same_state(registered_node, node_client):
    """A node that reboots and says `starting` again has not been starting all along."""
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="starting"))
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="starting", boot_id="p2x7c4m9q0zz"))

    report = _report(node_client, node_id)
    assert report["state_since"] == report["received_at"]


async def test_errors_outlive_the_beat_that_reported_them(registered_node, node_client):
    """The node clears its list once a beat is acknowledged, so the next beat's
    empty list must not erase what the last one said."""
    token, node_id = registered_node

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(errors=["first"]))
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(errors=["second"]))

    errors = _report(node_client, node_id)["errors"]
    assert [e["message"] for e in errors] == ["first", "second"]
    assert all(datetime.fromisoformat(e["at"]).tzinfo is not None for e in errors)


async def test_the_kept_errors_are_bounded_and_the_newest_survive(registered_node, node_client):
    token, node_id = registered_node

    for beat in range(3):
        node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(errors=[f"{beat}-{i}" for i in range(32)]))

    errors = [e["message"] for e in _report(node_client, node_id)["errors"]]
    assert len(errors) == ERRORS_KEPT
    assert errors[-1] == "2-31"


async def test_a_node_reporting_a_fault_is_still_judged_by_the_server(registered_node, node_client):
    """The report is stored, not trusted: saying `error` does not take a node
    offline, any more than saying `streaming` would put a silent one online."""
    token, node_id = registered_node
    state.connected_nodes[node_id]["status"] = "disconnected"

    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(state="error"))

    assert state.connected_nodes[node_id]["status"] == "active"


async def test_the_reports_are_for_administrators_only(registered_node, node_client):
    token, _ = registered_node
    node_client.post(HEARTBEAT, headers=_auth(token), json=_beat(errors=["node-internal detail"]))

    with patch("core.users.AUTH_BYPASS", False):
        assert node_client.get(REPORTS).status_code == 401


async def test_a_report_that_cannot_be_stored_does_not_cost_the_beat(
    registered_node, node_client, node_session, monkeypatch
):
    from sqlalchemy import select

    from core.nodes import Node
    from routes import node_stream

    async def _broken(*_args):
        raise RuntimeError("no room for the report")

    monkeypatch.setattr(node_stream, "record_report", _broken)
    token, node_id = registered_node

    response = node_client.post(HEARTBEAT, headers=_auth(token), json=_beat())

    assert response.status_code == 200
    seen = await node_session.scalar(select(Node.last_seen_at).where(Node.node_id == node_id))
    # Naive, as SQLite returns it, and UTC.
    assert (datetime.now(UTC).replace(tzinfo=None) - seen).total_seconds() < 5
