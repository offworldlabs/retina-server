"""The frame-starvation check: a node that is heard from and files no frames.

Against compute_health_issues directly, since it is a pure read of state, plus
the two writers it depends on: the frame workers and the heartbeat's report.
"""

import time

from core import state
from services.health import _DEFAULT_FRAME_STARVATION_S, compute_health_issues

NODE_ID = "ret1a2b3c4d"


def _long_ago() -> float:
    """Three windows back, taken when the test runs: a module-level value ages
    with the suite and turns an exact duration into a flaky one."""
    return time.time() - 3 * _DEFAULT_FRAME_STARVATION_S


def _node(**overrides) -> dict:
    return {"status": "active", "is_synthetic": False, "config": {}, **overrides}


def _starved() -> dict[str, str]:
    return {i["type"]: i["message"] for i in compute_health_issues() if i["type"].startswith("frame_starvation:")}


def test_a_heard_node_filing_nothing_is_starved():
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node()

    starved = _starved()

    assert list(starved) == [f"frame_starvation:{NODE_ID}"]
    assert "45 min" in starved[f"frame_starvation:{NODE_ID}"]


def test_a_node_filing_frames_is_not():
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node()
    state.node_last_frame_at[NODE_ID] = time.time() - 30

    assert _starved() == {}


def test_a_node_that_stopped_filing_is_measured_from_its_last_frame():
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node()
    state.node_last_frame_at[NODE_ID] = time.time() - 2 * _DEFAULT_FRAME_STARVATION_S

    assert "30 min" in _starved()[f"frame_starvation:{NODE_ID}"]


def test_a_restart_is_not_read_as_starvation():
    """The frame record does not survive a restart, so every node starts with
    none; it is measured from the process starting, not from never."""
    state.frames_watched_since = time.time() - 60
    state.connected_nodes[NODE_ID] = _node()

    assert _starved() == {}


def test_an_offline_node_is_left_to_the_offline_sweep():
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node(status="disconnected")

    assert _starved() == {}


def test_the_synthetic_fleet_is_not_watched():
    state.frames_watched_since = _long_ago()
    state.connected_nodes["synth-0001"] = _node(is_synthetic=True)

    assert _starved() == {}


def test_the_node_s_own_report_is_quoted_with_how_long_it_has_held():
    """A node stuck in `starting` is caught because it files nothing; its report
    says why, and for how long."""
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node()
    state.node_reported_state[NODE_ID] = ("starting", time.time() - 3 * 3600 - 600)

    message = _starved()[f"frame_starvation:{NODE_ID}"]

    assert "`starting` for 3 h 10 min" in message


def test_the_window_is_configurable(monkeypatch):
    monkeypatch.setenv("FRAME_STARVATION_S", "60")
    state.frames_watched_since = time.time() - 120
    state.connected_nodes[NODE_ID] = _node()

    assert f"frame_starvation:{NODE_ID}" in _starved()


def test_the_frame_workers_stamp_each_node_s_last_frame():
    from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
    from services.frame_processor import process_one_frame

    before = time.time()
    process_one_frame(
        NODE_ID,
        {"timestamp": 1753900000123, "delay": [], "doppler": [], "snr": []},
        PassiveRadarPipeline(DEFAULT_NODE_CONFIG),
    )

    assert state.node_last_frame_at[NODE_ID] >= before


def test_the_monitor_alerts_once_per_node_and_resolves_when_frames_resume(monkeypatch):
    from services.tasks import health_monitor

    sent = []
    monkeypatch.setattr(health_monitor, "send_alert", lambda type_, message, meta: sent.append(type_))
    state.frames_watched_since = _long_ago()
    state.connected_nodes[NODE_ID] = _node()

    active = health_monitor.run_cycle(set())
    state.node_last_frame_at[NODE_ID] = time.time()
    health_monitor.run_cycle(active)

    starvation = [t for t in sent if "frame_starvation" in t]
    assert starvation == [f"frame_starvation:{NODE_ID}", f"resolved:frame_starvation:{NODE_ID}"]
