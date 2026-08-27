"""The mirror's request-path half: arming, queueing and dropping.

No app and no database here. What matters is that `offer` cannot raise into
the ingest path and cannot block it, which is testable directly.
"""

import asyncio
import logging

import httpx
import pytest

from services import detection_mirror

ARMED = {"DETECTION_MIRROR_URL": "https://sink.invalid/", "DETECTION_MIRROR_KEY": "k"}


def _frame():
    from routes.node_schemas import DetectionFrame

    return DetectionFrame(
        t=1753900000.123,
        seq=1,
        boot_id="k3n8v2qp71ab",
        config_version=1,
        delay=[12.4],
        doppler=[-118.0],
        snr=[14.2],
        adsb_hex=["4ca1f2"],
    )


@pytest.fixture(autouse=True)
def _disarmed():
    """Leave the module unarmed, whatever a test did to it."""
    yield
    detection_mirror.configure_from_env({})


def test_unset_url_leaves_the_mirror_unarmed():
    assert detection_mirror.configure_from_env({}) is False
    assert detection_mirror.stats()["accepted"] == 0


def test_a_non_https_url_refuses_to_arm():
    """DETECTION_MIRROR_KEY would otherwise cross the wire in clear."""
    armed = detection_mirror.configure_from_env(
        {"DETECTION_MIRROR_URL": "http://sink.invalid/", "DETECTION_MIRROR_KEY": "k"}
    )
    assert armed is False
    detection_mirror.offer("mirror-node", _frame())
    assert detection_mirror.stats()["accepted"] == 0
    assert detection_mirror.drain() == []


def test_offer_is_a_no_op_when_unarmed():
    detection_mirror.configure_from_env({})
    detection_mirror.offer("mirror-node", _frame())
    assert detection_mirror.stats()["accepted"] == 0


def test_armed_offer_queues_the_frame():
    assert detection_mirror.configure_from_env(ARMED) is True
    detection_mirror.offer("mirror-node", _frame())
    assert detection_mirror.stats()["accepted"] == 1
    assert [node_id for node_id, _ in detection_mirror.drain()] == ["mirror-node"]


def test_a_full_queue_drops_rather_than_raising(monkeypatch):
    monkeypatch.setattr(detection_mirror, "QUEUE_MAX", 2)
    detection_mirror.configure_from_env(ARMED)

    for _ in range(5):
        detection_mirror.offer("mirror-node", _frame())

    # queue_depth is live, not cumulative: 2 sit in the queue, unlike accepted
    # and dropped which keep counting even once drain() empties it.
    assert detection_mirror.stats() == {
        "accepted": 2,
        "dropped": 3,
        "sent": 0,
        "rejected": 0,
        "failed": 0,
        "unregistered": 0,
        "queue_depth": 2,
    }
    assert len(detection_mirror.drain()) == 2
    assert detection_mirror.stats()["queue_depth"] == 0


def test_a_saturated_queue_logs_its_own_line_throttled(monkeypatch, caplog):
    """A healthy receiver must not make a saturated queue invisible: dropped
    frames get their own line rather than surfacing only bundled inside a
    failure line that a healthy receiver never triggers."""
    monkeypatch.setattr(detection_mirror, "QUEUE_MAX", 1)
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node", _frame())
    detection_mirror.offer("mirror-node", _frame())  # queue full, dropped

    with caplog.at_level(logging.WARNING, logger="services.detection_mirror"):
        detection_mirror._note_dropped()
        first_call_records = list(caplog.records)
        caplog.clear()
        detection_mirror._note_dropped()  # same drop count, still inside the throttle window
        second_call_records = list(caplog.records)

    assert len(first_call_records) == 1
    assert "drop" in first_call_records[0].message.lower()
    assert second_call_records == []


def test_offer_holds_the_wire_model_not_a_converted_dict():
    """The reason offer takes the model: the dict submit_frame queued is stamped
    with `_node_id` and mutated further by the frame workers."""
    detection_mirror.configure_from_env(ARMED)
    frame = _frame()

    detection_mirror.offer("mirror-node", frame)

    ((_, queued),) = detection_mirror.drain()
    assert queued is frame


def test_arming_clears_the_counters():
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node", _frame())
    detection_mirror.configure_from_env(ARMED)
    assert detection_mirror.stats()["accepted"] == 0


from core import state


def _connect(node_id: str, config: dict) -> None:
    with state.connected_nodes_lock:
        state.connected_nodes[node_id] = {
            "config_hash": "",
            "config": config,
            "status": "active",
            "last_heartbeat": "",
            "peer": "v1",
            "is_synthetic": False,
            "capabilities": {},
        }


@pytest.fixture()
def _connected():
    _connect("mirror-node-a", {"rx_lat": 33.9, "rx_lon": -84.6, "tx_lat": 34.0, "tx_lon": -84.7})
    _connect("mirror-node-b", {"rx_lat": 34.8, "rx_lon": -82.3, "tx_lat": 35.1, "tx_lon": -82.2})
    yield
    with state.connected_nodes_lock:
        state.connected_nodes.pop("mirror-node-a", None)
        state.connected_nodes.pop("mirror-node-b", None)


def test_batch_groups_by_node_and_carries_config(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-b", _frame())
    detection_mirror.offer("mirror-node-a", _frame())

    entries = {e["node_id"]: e for e in detection_mirror.build_batch(detection_mirror.drain())}

    assert len(entries["mirror-node-a"]["frames"]) == 2
    assert len(entries["mirror-node-b"]["frames"]) == 1
    assert entries["mirror-node-a"]["config"]["rx_lat"] == 33.9
    # Converted, not the wire model: the receiving endpoint skips a frame with
    # no `timestamp`.
    assert entries["mirror-node-a"]["frames"][0]["timestamp"] == 1753900000123


def test_batch_skips_a_node_whose_config_left_the_registry(_connected):
    """A `config: None` entry would replace the node's last-known geometry on
    the receiver, not read as an unconfigured node: the receiving endpoint
    hashes whatever it is given and re-registers on a mismatch (commit
    42170ac). Skipping the node outright is the only safe option once frames
    for a since-retired node are already in the mirror queue."""
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-gone", _frame())
    detection_mirror.offer("mirror-node-a", _frame())

    entries = detection_mirror.build_batch(detection_mirror.drain())

    assert [e["node_id"] for e in entries] == ["mirror-node-a"]


async def test_send_posts_the_bulk_shape_with_the_key(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-API-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert seen["url"] == "https://sink.invalid/api/radar/detections/bulk"
    assert seen["key"] == "k"
    assert seen["body"]["nodes"][0]["node_id"] == "mirror-node-a"
    assert detection_mirror.stats()["sent"] == 1


async def test_a_refusing_receiver_is_counted_not_raised(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "nope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain())) is False
        )

    assert detection_mirror.stats() == {
        "accepted": 1,
        "dropped": 0,
        "sent": 0,
        "rejected": 0,
        "failed": 1,
        "unregistered": 0,
        "queue_depth": 0,
    }


async def test_a_receiver_that_queues_nothing_is_a_failure_not_a_success(_connected):
    """The receiving endpoint answers 200 with `frames_queued: 0` for a full
    state.frame_queue on its side, which it treats as unremarkable. Taking the
    status code at face value would make that indistinguishable from a healthy
    receiver: crediting `sent` from the response is what makes it visible."""
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert ok is False
    assert detection_mirror.stats()["sent"] == 0
    assert detection_mirror.stats()["rejected"] == 2
    assert detection_mirror._healthy is False


async def test_a_partial_landing_credits_only_what_arrived(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert ok is False
    assert detection_mirror.stats()["sent"] == 1
    assert detection_mirror.stats()["rejected"] == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"status": "ok"}),  # no frames_queued key
    ],
    ids=["non-json-body", "missing-frames-queued"],
)
async def test_an_unusable_response_is_a_failure_not_a_silent_success(_connected, response):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert ok is False
    assert detection_mirror.stats()["failed"] == 1
    assert detection_mirror.stats()["sent"] == 0


async def test_an_unreachable_receiver_is_counted_not_raised(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain())) is False
        )

    assert detection_mirror.stats()["failed"] == 1


async def test_the_task_returns_at_once_when_unarmed():
    detection_mirror.configure_from_env({})
    await asyncio.wait_for(detection_mirror.mirror_task(), timeout=1.0)


async def test_a_failing_drain_or_build_does_not_kill_the_task(_connected, monkeypatch):
    """build_batch (or drain) raising must not exit the loop: offer() would then
    fill the queue and drop every frame forever, indistinguishable from nothing
    to send."""
    tried = asyncio.Event()

    def _boom(_items):
        tried.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(detection_mirror, "DETECTION_MIRROR_FLUSH_INTERVAL_S", 0.01)
    monkeypatch.setattr(detection_mirror, "build_batch", _boom)
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())

    task = asyncio.create_task(detection_mirror.mirror_task())
    try:
        await asyncio.wait_for(tried.wait(), timeout=5)  # bounds a hang, not the loop
        assert not task.done()  # the guard, stated directly
        assert detection_mirror.stats()["failed"] == 1
    finally:
        task.cancel()


async def test_a_batch_that_builds_empty_is_not_posted_and_is_counted(monkeypatch):
    """Regression: build_batch returns [] when every node in a drain has left
    state.connected_nodes before the batch is built (by design, see its
    docstring), but the raw drained items are not empty. Gating the POST on
    `items` rather than on build_batch's result sent an empty {"nodes": []}
    batch and let send_batch's success path mark the mirror healthy, so those
    frames landed in none of sent/rejected/failed and the counters stopped
    reconciling.

    build_batch runs for real here (only wrapped to signal when it has run);
    only send_batch is stubbed, so a POST attempt is caught at the one call
    site mirror_task has for it, whatever the transport would have done.
    """
    processed = asyncio.Event()
    real_build_batch = detection_mirror.build_batch

    def _build_batch_and_signal(items):
        result = real_build_batch(items)
        processed.set()
        return result

    posted = False

    async def _unexpected_post(_client, _entries):
        nonlocal posted
        posted = True
        return True

    monkeypatch.setattr(detection_mirror, "DETECTION_MIRROR_FLUSH_INTERVAL_S", 0.01)
    monkeypatch.setattr(detection_mirror, "build_batch", _build_batch_and_signal)
    monkeypatch.setattr(detection_mirror, "send_batch", _unexpected_post)
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-gone", _frame())  # never in state.connected_nodes

    task = asyncio.create_task(detection_mirror.mirror_task())
    try:
        await asyncio.wait_for(processed.wait(), timeout=5)  # bounds a hang, not the loop
        assert posted is False
        assert detection_mirror.stats() == {
            "accepted": 1,
            "dropped": 0,
            "sent": 0,
            "rejected": 0,
            "failed": 0,
            "unregistered": 1,
            "queue_depth": 0,
        }
    finally:
        task.cancel()


async def test_a_mixed_drain_counts_the_departed_node_while_sending_the_survivor(_connected, monkeypatch):
    """Regression: build_batch omits only the departed node's frames when a
    drain mixes a surviving node with one that has left state.connected_nodes,
    so entries is non-empty and mirror_task took the send path without ever
    crediting the departed node's frames anywhere (they were counted on
    offer() and landed in none of sent/rejected/failed/unregistered).
    Deriving unregistered from the shortfall between what was drained and
    what build_batch returned, rather than only from entries being empty, is
    what makes this case reconcile too.

    build_batch runs for real here, as in the all-departed regression test
    above; only send_batch is stubbed, so waiting on it also proves it was
    called with just the survivor.
    """
    sent_entries = []
    processed = asyncio.Event()

    async def _fake_send(_client, entries):
        sent_entries.append(entries)
        detection_mirror._counters["sent"] += sum(len(e["frames"]) for e in entries)
        processed.set()
        return True

    monkeypatch.setattr(detection_mirror, "DETECTION_MIRROR_FLUSH_INTERVAL_S", 0.01)
    monkeypatch.setattr(detection_mirror, "send_batch", _fake_send)
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-gone", _frame())  # never in state.connected_nodes
    detection_mirror.offer("mirror-node-a", _frame())  # in _connected

    task = asyncio.create_task(detection_mirror.mirror_task())
    try:
        await asyncio.wait_for(processed.wait(), timeout=5)  # bounds a hang, not the loop
        assert [e["node_id"] for e in sent_entries[0]] == ["mirror-node-a"]
        assert detection_mirror.stats() == {
            "accepted": 2,
            "dropped": 0,
            "sent": 1,
            "rejected": 0,
            "failed": 0,
            "unregistered": 1,
            "queue_depth": 0,
        }
    finally:
        task.cancel()


async def test_a_failed_batch_counts_frames_not_batches(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-b", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "nope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain())) is False
        )

    assert detection_mirror.stats()["failed"] == 3


def test_configure_from_env_resets_health_state():
    detection_mirror.configure_from_env(ARMED)
    detection_mirror._note(False, RuntimeError("boom"))
    assert detection_mirror._healthy is False

    detection_mirror.configure_from_env(ARMED)

    assert detection_mirror._healthy is True
    assert detection_mirror._logged_at == 0.0


def test_the_lifespan_arms_the_mirror_and_starts_its_task(monkeypatch):
    """The wiring, exercised rather than grepped.

    `configure_from_env` is stubbed before the boot so the lifespan cannot arm a
    real mirror from whatever is in the ambient environment. The `mirror_task`
    stub records at call time rather than in the coroutine body: the task may be
    cancelled at shutdown before the loop ever schedules it.
    """
    from fastapi.testclient import TestClient

    import main

    called = {"configured": 0, "task": 0}

    async def _noop():
        return None

    def _configure(env=None):
        called["configured"] += 1
        return False

    def _task():
        called["task"] += 1
        return _noop()

    monkeypatch.setattr(detection_mirror, "configure_from_env", _configure)
    monkeypatch.setattr(detection_mirror, "mirror_task", _task)

    with TestClient(main.app):
        pass

    assert called == {"configured": 1, "task": 1}
