"""The frame-starvation check: a node that is heard from and files next to nothing.

Against compute_health_issues directly, since it is a pure read of state, plus
the two writers it depends on: the frame workers and the heartbeat's report.
"""

import time

from config.constants import FRAME_STARVATION_MIN_FRAMES
from core import state
from services.health import _DEFAULT_FRAME_STARVATION_S, compute_health_issues

NODE_ID = "ret1a2b3c4d"


def _long_ago() -> float:
    """Three windows back, taken when the test runs: a module-level value ages
    with the suite and turns an exact duration into a flaky one."""
    return time.time() - 3 * _DEFAULT_FRAME_STARVATION_S


def _filed(*ages_s: float) -> None:
    """Frames filed this many seconds ago, as the frame workers record them."""
    for age in sorted(ages_s, reverse=True):
        state.record_node_frame(NODE_ID, time.time() - age)


def _online_since(when: float) -> None:
    """A node the check first saw online at `when`."""
    state.node_heard_since[NODE_ID] = when


def _node(**overrides) -> dict:
    return {"status": "active", "is_synthetic": False, "config": {}, **overrides}


def _starved() -> dict[str, str]:
    return {i["type"]: i["message"] for i in compute_health_issues() if i["type"].startswith("frame_starvation:")}


def test_a_heard_node_filing_nothing_is_starved():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()

    starved = _starved()

    assert list(starved) == [f"frame_starvation:{NODE_ID}"]
    assert "filed 0 frame(s) in the last 15 min and none in 45 min online" in starved[f"frame_starvation:{NODE_ID}"]


def test_a_node_filing_frames_is_not():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    _filed(*[i * 0.5 for i in range(FRAME_STARVATION_MIN_FRAMES)])

    assert _starved() == {}


def test_a_node_that_trickles_is_starved():
    """Seen on prod: a board that cannot reach its own radar still slips out a
    frame every few minutes, against the one or two a second a working node
    files. Zero was the wrong line."""
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    _filed(700, 400, 100)

    message = _starved()[f"frame_starvation:{NODE_ID}"]

    assert "filed 3 frame(s) in the last 15 min, the latest 1 min ago" in message


def test_the_floor_is_the_boundary_it_claims_to_be():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    _filed(*[60 * i + 1 for i in range(FRAME_STARVATION_MIN_FRAMES - 1)])

    assert f"frame_starvation:{NODE_ID}" in _starved()

    _filed(0)

    assert _starved() == {}


def test_a_shortened_window_asks_for_proportionally_fewer_frames(monkeypatch):
    """One frame a minute of window: a two-minute window asks for two, which a
    working node clears easily, rather than the full fifteen."""
    monkeypatch.setenv("FRAME_STARVATION_S", "120")
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    _filed(90, 30)

    assert _starved() == {}


def test_a_node_that_stopped_filing_is_starved_once_its_frames_age_out():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    _filed(*[2 * _DEFAULT_FRAME_STARVATION_S + i for i in range(FRAME_STARVATION_MIN_FRAMES)])

    assert "filed 0 frame(s)" in _starved()[f"frame_starvation:{NODE_ID}"]


def test_a_node_first_seen_is_given_a_window():
    """After a restart, a registration or any other way onto the registry, a
    node has had no window to file in yet; its blah2 may still be starting."""
    state.connected_nodes[NODE_ID] = _node()

    assert _starved() == {}
    assert state.node_heard_since[NODE_ID] >= time.time() - 5


def test_a_node_back_from_offline_starts_a_fresh_window():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node(status="disconnected")
    _starved()

    state.connected_nodes[NODE_ID]["status"] = "active"

    assert _starved() == {}


def test_an_offline_node_is_left_to_the_offline_sweep():
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node(status="disconnected")

    assert _starved() == {}


def test_the_synthetic_fleet_is_not_watched():
    _online_since(_long_ago())
    state.connected_nodes["synth-0001"] = _node(is_synthetic=True)

    assert _starved() == {}


def test_the_node_s_own_report_is_quoted_with_how_long_it_has_held():
    """A node stuck in `starting` is caught because it files nothing; its report
    says why, and for how long."""
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()
    state.node_reported_state[NODE_ID] = ("starting", time.time() - 3 * 3600 - 600)

    message = _starved()[f"frame_starvation:{NODE_ID}"]

    assert "`starting` for 3 h 10 min" in message


def test_the_window_is_configurable(monkeypatch):
    monkeypatch.setenv("FRAME_STARVATION_S", "60")
    _online_since(time.time() - 120)
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

    assert state.node_recent_frames[NODE_ID][-1] >= before


def test_the_monitor_alerts_once_per_node_and_resolves_when_frames_resume(monkeypatch):
    from services.tasks import health_monitor

    sent = []
    monkeypatch.setattr(health_monitor, "send_alert", lambda type_, message, meta: sent.append(type_))
    _online_since(_long_ago())
    state.connected_nodes[NODE_ID] = _node()

    active = health_monitor.run_cycle(set())
    _filed(*[0] * FRAME_STARVATION_MIN_FRAMES)
    health_monitor.run_cycle(active)

    starvation = [t for t in sent if "frame_starvation" in t]
    assert starvation == [f"frame_starvation:{NODE_ID}", f"resolved:frame_starvation:{NODE_ID}"]
