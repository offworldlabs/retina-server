"""The mirror's request-path half: arming, queueing and dropping.

No app and no database here. What matters is that `offer` cannot raise into
the ingest path and cannot block it, which is testable directly.

The sending half is exercised against httpx mocks. `stats()` carries the queue
counters at the top level and each target's own accounting under `targets`,
keyed by the target's host, which is why the single-target assertions below
read through `SINK`.
"""

import asyncio
import logging

import httpx
import pytest

from services import detection_mirror

ARMED = {"DETECTION_MIRROR_URL": "https://sink.invalid/", "DETECTION_MIRROR_KEY": "k"}
SINK = "sink.invalid"  # ARMED's one target, as stats() labels it
TWO_TARGETS = {
    "DETECTION_MIRROR_URL": "https://sink.invalid, https://staging.invalid/",
    "DETECTION_MIRROR_KEY": "k1, k2",
}


def _target_stats(label: str = SINK) -> dict:
    return detection_mirror.stats()["targets"][label]


def _healthy(label: str = SINK) -> bool:
    return _target_stats(label)["healthy"]


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
        "unregistered": 0,
        "queue_depth": 2,
        "targets": {SINK: {"sent": 0, "rejected": 0, "failed": 0, "healthy": True}},
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


def test_batch_carries_position_tags_in_the_pipeline_shape(_connected):
    """A node's own ADS-B correlation crosses the mirror with its position, in
    the record shape the receiving frame_processor files: this is the only fix
    the receiving environment will ever have for a mirrored node's detection."""
    from routes.node_schemas import DetectionFrame

    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer(
        "mirror-node-a",
        DetectionFrame(
            t=1753900000.123,
            seq=1,
            boot_id="k3n8v2qp71ab",
            config_version=1,
            delay=[12.4, 30.1],
            doppler=[-118.0, 44.5],
            snr=[14.2, 9.8],
            adsb_hex=["4ca1f2", None],
            adsb=[{"hex": "4ca1f2", "lat": 33.87, "lon": -84.68, "alt": 15375, "gs": 189, "track": 238.4}, None],
        ),
    )

    entries = {e["node_id"]: e for e in detection_mirror.build_batch(detection_mirror.drain())}

    assert entries["mirror-node-a"]["frames"][0]["adsb"] == [
        {"hex": "4ca1f2", "lat": 33.87, "lon": -84.68, "alt_baro": 15375, "gs": 189, "track": 238.4},
        None,
    ]
    # A hex-only frame stays byte-identical to what it was.
    detection_mirror.offer("mirror-node-b", _frame())
    (entry,) = detection_mirror.build_batch(detection_mirror.drain())
    assert "adsb" not in entry["frames"][0]


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
    assert _target_stats()["sent"] == 1


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
        "unregistered": 0,
        "queue_depth": 0,
        "targets": {SINK: {"sent": 0, "rejected": 0, "failed": 1, "healthy": False}},
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
    assert _target_stats()["sent"] == 0
    assert _target_stats()["rejected"] == 2
    assert _healthy() is False


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
    assert _target_stats()["sent"] == 1
    assert _target_stats()["rejected"] == 2


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
    assert _target_stats()["failed"] == 1
    assert _target_stats()["sent"] == 0


async def test_an_unreachable_receiver_is_counted_not_raised(_connected):
    detection_mirror.configure_from_env(ARMED)
    detection_mirror.offer("mirror-node-a", _frame())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain())) is False
        )

    assert _target_stats()["failed"] == 1


async def test_the_task_returns_at_once_when_unarmed():
    detection_mirror.configure_from_env({})
    await asyncio.wait_for(detection_mirror.mirror_task(), timeout=1.0)


async def test_a_failing_drain_or_build_does_not_kill_the_task(_connected, monkeypatch):
    """build_batch (or drain) raising must not exit the loop: offer() would then
    fill the queue and drop every frame forever, indistinguishable from nothing
    to send. The fault precedes any send, so every target lost the frames and
    every target is charged for them."""
    tried = asyncio.Event()

    def _boom(_items):
        tried.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(detection_mirror, "DETECTION_MIRROR_FLUSH_INTERVAL_S", 0.01)
    monkeypatch.setattr(detection_mirror, "build_batch", _boom)
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())

    task = asyncio.create_task(detection_mirror.mirror_task())
    try:
        await asyncio.wait_for(tried.wait(), timeout=5)  # bounds a hang, not the loop
        assert not task.done()  # the guard, stated directly
        assert _target_stats(SINK)["failed"] == 1
        assert _target_stats("staging.invalid")["failed"] == 1
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
            "unregistered": 1,
            "queue_depth": 0,
            "targets": {SINK: {"sent": 0, "rejected": 0, "failed": 0, "healthy": True}},
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
        detection_mirror._targets[0].sent += sum(len(e["frames"]) for e in entries)
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
            "unregistered": 1,
            "queue_depth": 0,
            "targets": {SINK: {"sent": 1, "rejected": 0, "failed": 0, "healthy": True}},
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

    assert _target_stats()["failed"] == 3


# ── more than one target ─────────────────────────────────────────────────────


def test_comma_separated_targets_are_trimmed():
    armed = detection_mirror.configure_from_env(
        {
            "DETECTION_MIRROR_URL": " https://sink.invalid/ , https://staging.invalid ",
            "DETECTION_MIRROR_KEY": " k1 , k2 ",
        }
    )

    assert armed is True
    assert [(t.label, t.url, t.key) for t in detection_mirror._targets] == [
        (SINK, "https://sink.invalid", "k1"),
        ("staging.invalid", "https://staging.invalid", "k2"),
    ]
    assert set(detection_mirror.stats()["targets"]) == {SINK, "staging.invalid"}


def test_a_single_target_still_arms_as_before():
    """The one-value form is the one-entry case of the list rule, and a lone
    URL with no key at all keeps arming: the receiver decides what an empty
    key is worth."""
    assert detection_mirror.configure_from_env(ARMED) is True
    ((label, target),) = ((t.label, t) for t in detection_mirror._targets)
    assert (label, target.url, target.key) == (SINK, "https://sink.invalid", "k")

    assert detection_mirror.configure_from_env({"DETECTION_MIRROR_URL": "https://sink.invalid"}) is True
    assert detection_mirror._targets[0].key == ""


def test_an_empty_key_entry_sends_that_receiver_no_key():
    """The lists are positional, so `k1,` is a stated choice: the second
    receiver gets an empty X-API-Key and decides for itself. A receiver whose
    RADAR_API_KEY is unset accepts it; one with a key set returns 401, which
    the mirror counts as rejected."""
    armed = detection_mirror.configure_from_env(
        {"DETECTION_MIRROR_URL": "https://sink.invalid,https://staging.invalid", "DETECTION_MIRROR_KEY": "k1,"}
    )

    assert armed is True
    assert [(t.label, t.key) for t in detection_mirror._targets] == [(SINK, "k1"), ("staging.invalid", "")]


def test_an_empty_url_entry_refuses_to_arm(caplog):
    """`a,,b` with keys `k1,k2,k3` would otherwise hand k3 to b, or with
    `k1,k2` arm b keyless by accident: neither is what a trailing or doubled
    comma meant, so the mirror stays off and says so."""
    for urls in ("https://sink.invalid,,https://staging.invalid", "https://sink.invalid,"):
        with caplog.at_level(logging.ERROR, logger="services.detection_mirror"):
            armed = detection_mirror.configure_from_env({"DETECTION_MIRROR_URL": urls, "DETECTION_MIRROR_KEY": "k1,k2"})
        assert armed is False
        assert detection_mirror._targets == []
    assert any("https://" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "env",
    [
        {"DETECTION_MIRROR_URL": "https://sink.invalid,https://staging.invalid", "DETECTION_MIRROR_KEY": "k1"},
        {"DETECTION_MIRROR_URL": "https://sink.invalid", "DETECTION_MIRROR_KEY": "k1,k2"},
        {"DETECTION_MIRROR_URL": "https://sink.invalid,https://staging.invalid", "DETECTION_MIRROR_KEY": ""},
    ],
    ids=["more-urls-than-keys", "more-keys-than-urls", "two-urls-no-key"],
)
def test_mismatched_url_and_key_counts_refuse_to_arm(env, caplog):
    """No half-arming: a target whose key is unknown is not a target, and
    arming the rest would hide the misconfiguration behind a working mirror."""
    with caplog.at_level(logging.ERROR, logger="services.detection_mirror"):
        armed = detection_mirror.configure_from_env(env)

    assert armed is False
    assert detection_mirror.stats()["targets"] == {}
    detection_mirror.offer("mirror-node", _frame())
    assert detection_mirror.stats()["accepted"] == 0
    assert any("DETECTION_MIRROR_KEY" in r.message for r in caplog.records)


def test_one_http_url_refuses_the_whole_configuration():
    """Not only the offending entry: the https target that was fine stays
    unarmed too, so the error line cannot be mistaken for a partial success."""
    armed = detection_mirror.configure_from_env(
        {"DETECTION_MIRROR_URL": "https://sink.invalid,http://staging.invalid", "DETECTION_MIRROR_KEY": "k1,k2"}
    )
    assert armed is False
    assert detection_mirror.stats()["targets"] == {}


def test_a_duplicate_host_refuses_to_arm():
    """Labels key the per-target accounting, so two targets with one host
    would fold into one row and post every batch twice to the same place."""
    armed = detection_mirror.configure_from_env(
        {"DETECTION_MIRROR_URL": "https://sink.invalid,https://sink.invalid/", "DETECTION_MIRROR_KEY": "k1,k2"}
    )
    assert armed is False
    assert detection_mirror.stats()["targets"] == {}


def _host_handler(responses: dict, seen: dict | None = None):
    """A MockTransport handler answering per host, recording each request."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if seen is not None:
            seen[host] = {"key": request.headers.get("X-API-Key"), "body": json.loads(request.content)}
        answer = responses[host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler


async def test_each_target_receives_the_same_batch_with_its_own_key(_connected):
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())
    seen = {}
    ok = httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 1})
    handler = _host_handler({SINK: ok, "staging.invalid": ok}, seen)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert seen[SINK]["key"] == "k1"
    assert seen["staging.invalid"]["key"] == "k2"
    assert seen[SINK]["body"] == seen["staging.invalid"]["body"]
    assert _target_stats(SINK)["sent"] == 1
    assert _target_stats("staging.invalid")["sent"] == 1


async def test_one_failing_target_leaves_the_other_healthy_and_credited(_connected):
    """The same batch, two outcomes: the frames are `sent` on one target and
    `failed` on the other, and only the failing one flips to unhealthy."""
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())
    detection_mirror.offer("mirror-node-a", _frame())
    handler = _host_handler(
        {
            SINK: httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 2}),
            "staging.invalid": httpx.Response(500, json={"detail": "nope"}),
        }
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    assert ok is False
    assert detection_mirror.stats() == {
        "accepted": 2,
        "dropped": 0,
        "unregistered": 0,
        "queue_depth": 0,
        "targets": {
            SINK: {"sent": 2, "rejected": 0, "failed": 0, "healthy": True},
            "staging.invalid": {"sent": 0, "rejected": 0, "failed": 2, "healthy": False},
        },
    }


async def test_a_failing_target_is_named_in_the_log_line_and_the_event(_connected, caplog, monkeypatch):
    """A failing staging receiver has to be distinguishable from a healthy
    test one, so the target's label is in the line and the event message."""
    events = []
    monkeypatch.setattr(detection_mirror, "_log_event", lambda *args: events.append(args))
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())
    handler = _host_handler(
        {
            SINK: httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 1}),
            "staging.invalid": httpx.Response(500, json={"detail": "nope"}),
        }
    )

    with caplog.at_level(logging.WARNING, logger="services.detection_mirror"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await detection_mirror.send_batch(client, detection_mirror.build_batch(detection_mirror.drain()))

    (record,) = caplog.records
    assert "staging.invalid" in record.message
    assert record.message.startswith("detection mirror to staging.invalid failing")
    ((category, message, severity, meta),) = events
    assert (category, severity) == ("detection_mirror", "warning")
    assert message.startswith("Detection mirror to staging.invalid failing")
    assert meta["targets"][SINK]["healthy"] is True


async def test_a_hung_target_does_not_hold_the_others_back(_connected):
    """Concurrent, not in turn: the first target's request only completes once
    the second target has been asked, which a sequential send never reaches.
    Bounded by wait_for rather than by the client timeout so a regression
    fails in seconds with this test's name on it."""
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())
    staging_asked = asyncio.Event()
    ok = {"status": "ok", "nodes_registered": 1, "frames_queued": 1}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == SINK:
            await staging_asked.wait()
        else:
            staging_asked.set()
        return httpx.Response(200, json=ok)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = detection_mirror.build_batch(detection_mirror.drain())
        assert await asyncio.wait_for(detection_mirror.send_batch(client, batch), timeout=5)

    assert _target_stats(SINK)["sent"] == 1
    assert _target_stats("staging.invalid")["sent"] == 1


async def test_the_task_fans_one_drain_out_to_every_target(_connected, monkeypatch):
    """End to end through mirror_task: one drain, one build, a POST per
    target. The client is the real one with its transport swapped, so the
    per-target pool sizing is exercised rather than stubbed around."""
    seen = {}
    both_seen = asyncio.Event()
    ok = httpx.Response(200, json={"status": "ok", "nodes_registered": 1, "frames_queued": 1})
    record = _host_handler({SINK: ok, "staging.invalid": ok}, seen)

    def handler(request: httpx.Request) -> httpx.Response:
        response = record(request)
        if len(seen) == 2:
            both_seen.set()
        return response

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr(detection_mirror, "DETECTION_MIRROR_FLUSH_INTERVAL_S", 0.01)
    detection_mirror.configure_from_env(TWO_TARGETS)
    detection_mirror.offer("mirror-node-a", _frame())

    task = asyncio.create_task(detection_mirror.mirror_task())
    try:
        await asyncio.wait_for(both_seen.wait(), timeout=5)  # bounds a hang, not the loop
        assert seen[SINK]["key"] == "k1"
        assert seen["staging.invalid"]["key"] == "k2"
        assert _target_stats(SINK)["sent"] == 1
        assert _target_stats("staging.invalid")["sent"] == 1
    finally:
        task.cancel()


def test_configure_from_env_resets_health_state():
    detection_mirror.configure_from_env(ARMED)
    detection_mirror._note(detection_mirror._targets[0], False, RuntimeError("boom"))
    assert _healthy() is False

    detection_mirror.configure_from_env(ARMED)

    assert _healthy() is True
    assert detection_mirror._targets[0].logged_at == 0.0


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


class TestBatchCarriesTheRef:
    """The receiving environment has no row for these nodes, so the ref has to
    travel with the detections or it cannot name them at all."""

    def _connected(self, node_id):
        from core import state

        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {"config": {"node_id": node_id}, "is_synthetic": False}

    def test_a_registered_node_sends_its_ref(self, monkeypatch):
        from services import detection_mirror, node_refs

        self._connected("ret1a2b3c4d")
        monkeypatch.setattr(node_refs, "ref_for", lambda nid: "nde1a2b3c4d00")
        monkeypatch.setattr(detection_mirror, "pipeline_frame", lambda f: f)
        (entry,) = detection_mirror.build_batch([("ret1a2b3c4d", {})])
        assert entry["node_ref"] == "nde1a2b3c4d00"

    def test_a_node_with_no_ref_sends_none_rather_than_a_null(self, monkeypatch):
        """An absent key, not node_ref: null, so the receiver's own validation
        never sees a value it would have to special-case."""
        from services import detection_mirror, node_refs

        self._connected("ret9f8e7d6c")
        monkeypatch.setattr(node_refs, "ref_for", lambda nid: None)
        monkeypatch.setattr(detection_mirror, "pipeline_frame", lambda f: f)
        (entry,) = detection_mirror.build_batch([("ret9f8e7d6c", {})])
        assert "node_ref" not in entry
