"""The poller that files registered stock-blah2 radars' frames into the pipeline.

Radars are stub HTTP servers on loopback, reached through the real pinned
client with a resolver and address policy that admit only the stub.
"""

import asyncio
import base64
import copy
import http.server
import ipaddress
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from config.constants import C_KM_US
from core import state
from core.frame_queue import ShardedFrameQueue
from core.nodes import Node, NodeConfig, PolledRadar
from services import blah2_poller, blah2_probe, detection_mirror, polled_endpoint, probation, publication
from services.blah2_poller import (
    INVALID_CONFIG,
    NO_CONFIGURATION,
    PENDING,
    STALLED,
    STREAMING,
    UNREACHABLE,
    Poller,
    PollTarget,
    RadarPoller,
    load_targets,
    moved_network,
    wire_frame,
)
from services.blah2_probe import CONFIG_PATH, DETECTION_PATH, Blah2Refusal, config_fingerprint
from services.blah2_probe import DetectionFrame as Blah2Frame
from services.parquet_writer import write_detections_parquet
from services.polled_endpoint import EndpointRefused, PinnedClient, PolledEndpoint, Refusal
from services.polled_radars import RadarGeometry, create_polled_radar
from tests.radar_stub import STOCK_CONFIG, StubServer, only_loopback, resolve_to_loopback

HOST = "radar.example.com"
# Where the stub answers from, and so where every registered radar last resolved.
STUB_ADDRESS = "127.0.0.1"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Timings short enough for a test, in the same proportions as deployed."""
    monkeypatch.setattr(blah2_poller, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(blah2_poller, "SESSION_S", 0.25)
    monkeypatch.setattr(blah2_poller, "BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(blah2_poller, "BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(blah2_poller, "STALLED_AFTER_S", 0.2)
    monkeypatch.setattr(blah2_poller, "PERSIST_INTERVAL_S", 0.0)
    monkeypatch.setattr(blah2_poller, "RELOAD_INTERVAL_S", 0.1)
    monkeypatch.setattr(blah2_probe, "freshness_gap_s", lambda cpi_s: 0.05)


@pytest.fixture
def queue(monkeypatch):
    q = asyncio.Queue(maxsize=1000)
    monkeypatch.setattr(state, "frame_queue", q)
    return q


@pytest.fixture
def maker(node_session):
    return async_sessionmaker(node_session.bind, expire_on_commit=False)


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _body(timestamp_ms: int, delays_km=(12.5, 30.0)) -> bytes:
    n = len(delays_km)
    return json.dumps(
        {"timestamp": timestamp_ms, "delay": list(delays_km), "doppler": [-40.0] * n, "snr": [14.2] * n}
    ).encode()


def _live(delays_km=(12.5, 30.0), offset_s: float = 0.0):
    """A route serving a new frame, stamped now, on every request."""

    async def route():
        return _body(_now_ms() + int(offset_s * 1000), delays_km)

    return route


def _fixed(body: bytes, status: int = 200):
    async def route():
        return status, body

    return route


def _serve(payload):
    """A route serving `payload` as JSON, as it stands when asked."""

    async def route():
        return json.dumps(payload).encode()

    return route


def _radar(detection=None, config=STOCK_CONFIG) -> dict:
    """A stub radar's routes: its configuration, and a new frame per request unless given."""
    return {CONFIG_PATH: _serve(config), DETECTION_PATH: detection or _live()}


def _config(**changes) -> dict:
    """STOCK_CONFIG with each `a__b__c` path set to its value, or removed for None."""
    config = copy.deepcopy(STOCK_CONFIG)
    for path, value in changes.items():
        *parents, key = path.split("__")
        node = config
        for parent in parents:
            node = node[parent]
        if value is None:
            del node[key]
        else:
            node[key] = value
    return config


async def _register(session, port: int, **overrides) -> str:
    args = {
        "owner_user_id": "user-a",
        "owner_email": "ada@example.com",
        "endpoint_raw": f"{HOST}:{port}",
        "scheme": "http",
        "host": HOST,
        "port": port,
        "endpoint_key": f"{HOST}:{port}",
        "auth_user": None,
        "auth_secret": None,
        "geometry": RadarGeometry(rx_lat=10.5, rx_lon=-30.25, rx_alt_m=12, tx_lat=10.75, tx_lon=-30.5, tx_alt_m=300),
        "fc_hz": 204.64e6,
        "fs_hz": 2e6,
        "max_range_km": 150.0,
        "cpi_s": 0.75,
        "delay_tolerance_us": 2.0,
        "doppler_tolerance_hz": 5.0,
        "config_fingerprint": config_fingerprint(STOCK_CONFIG),
        "resolved_ip": STUB_ADDRESS,
        "publication": "public",
        "licence_version": "eula-1",
        "unprotected": True,
    }
    registration = await create_polled_radar(session, **(args | overrides))
    await session.commit()
    return registration.node.node_id


def _target(port: int, node_id: str = "bla0000abcd", epoch: int = 1, **endpoint) -> PollTarget:
    return PollTarget(
        node_id=node_id,
        epoch=epoch,
        config_version=1,
        endpoint=PolledEndpoint(
            scheme="http",
            host=HOST,
            port=port,
            endpoint_key=f"{HOST}:{port}",
            auth_user=endpoint.get("auth_user"),
            auth_secret=endpoint.get("auth_secret"),
            raw=f"{HOST}:{port}",
        ),
    )


def _poller(target: PollTarget, maker, **kwargs) -> RadarPoller:
    kwargs.setdefault("resolver", resolve_to_loopback)
    kwargs.setdefault("policy", only_loopback)
    return RadarPoller(target, session_maker=maker, semaphore=asyncio.Semaphore(4), **kwargs)


async def _eventually(condition, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not await _holds(condition):
        if time.monotonic() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


async def _holds(condition) -> bool:
    result = condition()
    return await result if asyncio.iscoroutine(result) else bool(result)


class _Running:
    """Runs a coroutine as a task for the length of a with block."""

    def __init__(self, coro):
        self._coro = coro

    async def __aenter__(self):
        self.task = asyncio.create_task(self._coro)
        return self.task

    async def __aexit__(self, *exc):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def _row(maker, node_id: str) -> PolledRadar:
    async with maker() as session:
        return await session.get(PolledRadar, node_id)


async def _node(maker, node_id: str) -> Node:
    async with maker() as session:
        return await session.get(Node, node_id)


async def _versions(maker, node_id: str) -> list[NodeConfig]:
    async with maker() as session:
        rows = await session.execute(
            select(NodeConfig).where(NodeConfig.node_id == node_id).order_by(NodeConfig.version)
        )
        return list(rows.scalars())


async def _graduate(session, node_id: str) -> None:
    await session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(trust_state="graduated"))
    await session.commit()


def _drain(queue) -> list[tuple[str, dict]]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# ── The wire frame ────────────────────────────────────────────────────────────


def test_a_blah2_frame_becomes_a_v1_wire_frame():
    frame = Blah2Frame(timestamp_ms=1790000000250, delay_km=(C_KM_US * 40,), doppler_hz=(-12.5,), snr_db=(9.0,))

    wire = wire_frame(frame, seq=7, boot_id="0123456789abcdef", config_version=3)

    assert wire.t == 1790000000.25
    assert wire.delay == [pytest.approx(40.0)]
    assert (wire.doppler, wire.snr, wire.adsb_hex) == ([-12.5], [9.0], [None])
    assert (wire.seq, wire.boot_id, wire.config_version) == (7, "0123456789abcdef", 3)


def test_an_empty_frame_is_a_valid_wire_frame():
    wire = wire_frame(Blah2Frame(1790000000000, (), (), ()), seq=0, boot_id="0123456789abcdef", config_version=1)
    assert (wire.delay, wire.doppler, wire.snr, wire.adsb_hex) == ([], [], [], [])


def test_more_detections_than_a_v1_frame_holds_is_no_frame():
    n = 513
    frame = Blah2Frame(1790000000000, (1.0,) * n, (0.0,) * n, (10.0,) * n)
    assert wire_frame(frame, seq=0, boot_id="0123456789abcdef", config_version=1) is None


# ── Filing frames ─────────────────────────────────────────────────────────────


async def test_new_frames_are_queued_under_the_node_and_mirrored(node_session, maker, queue, monkeypatch):
    offered = []
    monkeypatch.setattr(detection_mirror, "offer", lambda node_id, frame: offered.append((node_id, frame)))
    async with StubServer(_radar(_live(delays_km=(C_KM_US * 40,)))) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: queue.qsize() >= 3)

    items = _drain(queue)
    assert {nid for nid, _ in items} == {node_id}
    frame = items[0][1]
    assert frame["_node_id"] == node_id
    assert frame["delay"] == [pytest.approx(40.0)]
    assert abs(frame["timestamp"] - _now_ms()) < 5000
    seqs = [f["seq"] for _, f in items]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert offered and offered[0][0] == node_id
    assert set(stub.paths) == {CONFIG_PATH, DETECTION_PATH}


async def test_a_filed_frame_is_archived_as_polled_under_its_epoch(node_session, maker, queue, tmp_path):
    """Nothing signs a stock blah2 frame, so the archive says it was polled and which box the id stood for."""
    async with StubServer(_radar(_live())) as stub:
        node_id = await _register(node_session, stub.port)
        await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=2))
        await node_session.commit()
        async with _Running(_poller(_target(stub.port, node_id, epoch=2), maker).run()):
            await _eventually(lambda: queue.qsize() >= 1)

    _, frame = _drain(queue)[0]
    key = write_detections_parquet(node_id=node_id, frames=[frame], base_dir=tmp_path)
    rows = pq.read_table(tmp_path / key, partitioning=None).to_pylist()
    assert {(r["signing_mode"], r["epoch"]) for r in rows} == {("polled", 2)}
    assert {(r["payload_hash"], r["signature"], r["signature_valid"]) for r in rows} == {(None, None, None)}


async def test_empty_frames_are_filed(node_session, maker, queue):
    async with StubServer(_radar(_live(delays_km=()))) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: queue.qsize() >= 2)

    _, frame = queue.get_nowait()
    assert (frame["delay"], frame["doppler"], frame["snr"]) == ([], [], [])


async def test_a_repeated_frame_is_filed_once(node_session, maker, queue):
    body = _body(_now_ms())
    async with StubServer(_radar(_fixed(body))) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: len(stub.requests) >= 5)

    assert queue.qsize() == 1


async def test_the_undefined_prefix_is_stripped(node_session, maker, queue):
    async def route():
        return b"undefined" + _body(_now_ms())

    async with StubServer(_radar(route)) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: queue.qsize() >= 1)


async def test_a_node_missing_from_the_registries_is_registered_before_its_frame(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        state.connected_nodes.pop(node_id, None)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: queue.qsize() >= 1)

    assert state.connected_nodes[node_id]["config"]["fc_hz"] == 204.64e6


async def test_a_node_the_pipeline_will_not_take_has_its_frames_dropped(node_session, maker, queue, monkeypatch):
    async def refuse(session, node):
        raise ValueError(f"{node.node_id} has no active configuration")

    monkeypatch.setattr(blah2_poller, "register_with_pipeline", refuse)
    before = state.frames_dropped
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        state.connected_nodes.pop(node_id, None)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: state.frames_dropped > before)

    assert queue.empty()


async def test_a_radar_without_an_active_configuration_files_nothing(node_session, maker, queue, caplog):
    caplog.set_level(logging.WARNING, logger=blah2_poller.__name__)
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        await node_session.execute(delete(NodeConfig).where(NodeConfig.node_id == node_id))
        await node_session.commit()
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 3)

    assert queue.empty()
    assert poller_.reason == NO_CONFIGURATION
    assert DETECTION_PATH not in stub.paths
    # Once for the state, not once a session.
    assert sum("no active configuration" in r.getMessage() for r in caplog.records) == 1


async def test_an_answer_refreshes_the_nodes_heartbeat(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        state.connected_nodes[node_id] = {"last_heartbeat": "", "config": {}}
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: state.connected_nodes[node_id]["last_heartbeat"])

    stamped = datetime.fromisoformat(state.connected_nodes[node_id]["last_heartbeat"])
    assert datetime.now(UTC) - stamped < timedelta(seconds=5)


# ── Liveness ──────────────────────────────────────────────────────────────────


async def test_a_live_radar_is_streaming(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: _liveness(maker, node_id, STREAMING))

    radar = await _row(maker, node_id)
    assert radar.last_frame_at is not None
    assert radar.consecutive_failures == 0
    async with maker() as session:
        assert (await session.get(Node, node_id)).last_seen_at is not None


async def _liveness(maker, node_id: str, expected: str) -> bool:
    return (await _row(maker, node_id)).liveness == expected


async def test_an_idle_radar_serving_its_last_frame_is_stalled(node_session, maker, queue):
    poller_ = None
    async with StubServer(_radar(_fixed(_body(_now_ms())))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))

    assert poller_.reason == Blah2Refusal.STALLED


async def test_a_radar_with_no_detection_yet_is_stalled_not_unreachable(node_session, maker, queue):
    async with StubServer(_radar(_fixed(b""))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))

    assert poller_.reason == Blah2Refusal.NO_DETECTION_YET
    assert queue.empty()


async def test_a_radar_clock_beyond_the_limit_files_nothing_and_says_so(node_session, maker, queue, caplog):
    caplog.set_level(logging.WARNING, logger=blah2_poller.__name__)
    async with StubServer(_radar(_live(offset_s=-30))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))

    assert queue.empty()
    assert poller_.reason == Blah2Refusal.CLOCK_OFFSET
    assert poller_.clock_offset_s == pytest.approx(-30, abs=2)
    logged = [re.search(r"clock offset (-?[\d.]+) s", r.getMessage()) for r in caplog.records]
    assert any(m and float(m.group(1)) == pytest.approx(-30, abs=2) for m in logged)


async def test_a_radar_that_keeps_failing_is_unreachable(node_session, maker, queue):
    async with StubServer(_radar(_fixed(b"oops", status=500))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, UNREACHABLE))

    assert poller_.reason == Refusal.BAD_RESPONSE
    assert (await _row(maker, node_id)).consecutive_failures >= blah2_poller.MAX_FAILURES


async def test_an_unreachable_radar_backs_off(maker):
    poller_ = _poller(_target(1), maker)
    poller_.failures = blah2_poller.MAX_FAILURES - 1
    assert poller_._retry_delay_s() == blah2_poller.POLL_INTERVAL_S
    poller_.failures = blah2_poller.MAX_FAILURES
    assert poller_._retry_delay_s() == blah2_poller.BACKOFF_BASE_S
    poller_.failures = blah2_poller.MAX_FAILURES + 1
    assert poller_._retry_delay_s() == 2 * blah2_poller.BACKOFF_BASE_S
    poller_.failures = 10_000
    assert poller_._retry_delay_s() == blah2_poller.BACKOFF_MAX_S


async def test_a_radar_that_recovers_streams_again(node_session, maker, queue):
    failing = {"on": True}

    async def route():
        return (500, b"") if failing["on"] else _body(_now_ms())

    async with StubServer(_radar(route)) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: _liveness(maker, node_id, UNREACHABLE))
            failing["on"] = False
            await _eventually(lambda: _liveness(maker, node_id, STREAMING))

    assert (await _row(maker, node_id)).consecutive_failures == 0


async def test_a_write_for_a_superseded_epoch_changes_nothing(node_session, maker):
    node_id = await _register(node_session, 3000)
    await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=2))
    await node_session.commit()
    poller_ = _poller(_target(3000, node_id, epoch=1), maker)
    poller_.liveness = STREAMING

    await poller_._persist()

    assert (await _row(maker, node_id)).liveness == PENDING


async def test_a_task_whose_epoch_has_moved_on_ends_without_polling(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=2))
        await node_session.commit()
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id, epoch=1), maker).run()

    assert stub.paths == [CONFIG_PATH]
    assert queue.empty()


# ── The address, every session ────────────────────────────────────────────────


async def test_the_address_is_vetted_again_every_session(node_session, maker, queue):
    resolutions = []

    async def resolver(host, port):
        resolutions.append(host)
        return await resolve_to_loopback(host, port)

    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker, resolver=resolver).run()):
            await _eventually(lambda: len(resolutions) >= 3)

    assert set(resolutions) == {HOST}


async def test_an_address_the_policy_refuses_is_never_contacted(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker, policy=lambda address: False)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, UNREACHABLE))

    assert stub.requests == []
    assert poller_.reason == Refusal.ADDRESS_NOT_PUBLIC


async def test_credentials_are_presented(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        target = _target(stub.port, node_id, auth_user="ada", auth_secret="s3cret")
        async with _Running(_poller(target, maker).run()):
            await _eventually(lambda: stub.requests)

    expected = "Basic " + base64.b64encode(b"ada:s3cret").decode()
    assert stub.requests[0].headers["authorization"] == expected


async def test_a_cancel_that_httpx_absorbs_still_stops_the_radar(node_session, maker, queue, monkeypatch):
    """anyio's connect can swallow a cancel and return normally; the loop must still stop."""
    real_get = PinnedClient.get

    async def absorbing_get(self, path, *, max_bytes):
        try:
            return await real_get(self, path, max_bytes=max_bytes)
        except asyncio.CancelledError:
            return _body(_now_ms())

    async def slow():
        await asyncio.sleep(0.5)
        return _body(_now_ms())

    monkeypatch.setattr(PinnedClient, "get", absorbing_get)
    async with StubServer(_radar(slow)) as stub:
        node_id = await _register(node_session, stub.port)
        task = asyncio.create_task(_poller(_target(stub.port, node_id), maker).run())
        await _eventually(lambda: stub.requests)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)

    assert done == {task} and task.cancelled()


# ── The configuration, every session ──────────────────────────────────────────


async def test_the_configuration_is_read_every_session(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()) as task:
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 3)
            assert not task.done()

    radar = await _row(maker, node_id)
    assert radar.last_config_at is not None
    assert radar.epoch == 1
    assert [v.version for v in await _versions(maker, node_id)] == [1]
    assert not queue.empty()


async def test_a_moved_site_starts_a_new_epoch_on_probation_with_the_radar_s_geometry(node_session, maker, queue):
    moved = _config(location__rx__latitude=10.6, location__rx__altitude=40)
    async with StubServer(_radar(config=moved)) as stub:
        node_id = await _register(node_session, stub.port)
        await _graduate(node_session, node_id)
        registered = await _row(maker, node_id)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker).run()

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.trust_state) == (2, "probation")
    assert radar.config_fingerprint == config_fingerprint(moved)
    assert radar.probe_passed_at > registered.probe_passed_at
    assert radar.last_config_at is not None
    old, new = await _versions(maker, node_id)
    assert old.superseded_at is not None and new.superseded_at is None
    assert (new.rx_lat, new.rx_alt_ft) == (10.6, pytest.approx(40 / 0.3048))
    # What the radar does not declare carries over.
    assert (new.max_range_km, new.delay_tolerance_us, new.doppler_tolerance_hz) == (150.0, 2.0, 5.0)
    assert (await _node(maker, node_id)).active_config_version == 2
    # Nothing is filed against geometry the radar no longer declares.
    assert queue.empty()


async def test_a_renamed_site_starts_a_new_epoch_without_a_new_version(node_session, maker, queue):
    async with StubServer(_radar(config=_config(location__tx__name="Mast"))) as stub:
        node_id = await _register(node_session, stub.port)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker).run()

    assert (await _row(maker, node_id)).epoch == 2
    assert [v.version for v in await _versions(maker, node_id)] == [1]
    assert (await _node(maker, node_id)).active_config_version == 1


async def test_a_new_epoch_is_committed_and_the_caches_expired_before_the_poller_is_woken(
    node_session, maker, queue, monkeypatch
):
    calls = []
    monkeypatch.setattr(probation, "invalidate", lambda: calls.append("probation"))
    monkeypatch.setattr(publication, "invalidate", lambda: calls.append("publication"))
    database = node_session.bind.url.database

    def wake():
        with closing(sqlite3.connect(database)) as conn:
            calls.append(conn.execute("SELECT epoch, trust_state FROM polled_radars").fetchone())

    async with StubServer(_radar(config=_config(location__tx__name="Mast"))) as stub:
        node_id = await _register(node_session, stub.port)
        await _graduate(node_session, node_id)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker, wake=wake).run()

    assert calls == ["probation", "publication", (2, "probation")]


async def test_a_configuration_gone_by_the_re_probe_is_logged_once(node_session, maker, queue, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=blah2_poller.__name__)
    real_probe = blah2_poller.probe_blah2

    async def probe(endpoint, **kwargs):
        result = await real_probe(endpoint, **kwargs)
        await node_session.execute(delete(NodeConfig).where(NodeConfig.node_id == node_id))
        await node_session.commit()
        return result

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar(config=_config(location__rx__latitude=10.6))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 4)

    assert poller_.reason == NO_CONFIGURATION
    assert (await _row(maker, node_id)).epoch == 1
    assert sum("no active configuration" in r.getMessage() for r in caplog.records) == 1


async def test_a_re_probe_counts_against_the_requests_in_flight(node_session, maker, queue, monkeypatch):
    semaphore = asyncio.Semaphore(1)
    held = []

    async def probe(endpoint, **kwargs):
        held.append(semaphore.locked())
        raise EndpointRefused(Refusal.TIMED_OUT, f"{endpoint.host} did not finish answering in time.")

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar(config=_config(location__rx__latitude=10.6))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = RadarPoller(
            _target(stub.port, node_id),
            session_maker=maker,
            semaphore=semaphore,
            resolver=resolve_to_loopback,
            policy=only_loopback,
        )
        async with _Running(poller_.run()):
            await _eventually(lambda: held)

    assert held[0] is True


async def test_an_epoch_moved_on_before_a_new_version_keeps_the_other_writer_s(node_session, maker, queue, monkeypatch):
    real_upsert = blah2_poller.upsert_config

    async def upsert(session, node_id, config):
        await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=5))
        await node_session.commit()
        return await real_upsert(session, node_id, config)

    monkeypatch.setattr(blah2_poller, "upsert_config", upsert)
    async with StubServer(_radar(config=_config(capture__fs=2_400_000))) as stub:
        node_id = await _register(node_session, stub.port)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker).run()

    assert (await _row(maker, node_id)).epoch == 5
    assert [v.version for v in await _versions(maker, node_id)] == [1]
    assert (await _node(maker, node_id)).active_config_version == 1


async def test_an_epoch_moved_on_during_the_re_probe_keeps_the_other_writer_s(node_session, maker, queue, monkeypatch):
    real_probe = blah2_poller.probe_blah2

    async def probe(endpoint, **kwargs):
        result = await real_probe(endpoint, **kwargs)
        await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=5))
        await node_session.commit()
        return result

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar(config=_config(location__rx__latitude=10.6))) as stub:
        node_id = await _register(node_session, stub.port)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker).run()

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.config_fingerprint) == (5, config_fingerprint(STOCK_CONFIG))
    assert [v.version for v in await _versions(maker, node_id)] == [1]
    assert (await _node(maker, node_id)).active_config_version == 1


@pytest.mark.parametrize(
    ("change", "field", "value"),
    [({"capture__fs": 2_400_000}, "fs_hz", 2.4e6), ({"process__data__cpi": 0.5}, "cpi_s", 0.5)],
)
async def test_fs_or_cpi_alone_writes_a_version_without_a_new_epoch(
    node_session, maker, queue, monkeypatch, change, field, value
):
    probes = []

    async def probe(endpoint, **kwargs):
        probes.append(endpoint)
        raise AssertionError("not re-probed")

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    woken = []
    async with StubServer(_radar(config=_config(**change))) as stub:
        node_id = await _register(node_session, stub.port)
        await _graduate(node_session, node_id)
        async with asyncio.timeout(5):
            await _poller(_target(stub.port, node_id), maker, wake=lambda: woken.append(True)).run()

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.trust_state) == (1, "graduated")
    old, new = await _versions(maker, node_id)
    assert getattr(new, field) == value
    assert (new.rx_lat, new.fc_hz) == (old.rx_lat, old.fc_hz)
    assert (await _node(maker, node_id)).active_config_version == 2
    assert probes == [] and woken == [True]


async def test_a_configuration_without_fs_or_cpi_keeps_the_values_held(node_session, maker, queue):
    async with StubServer(_radar(config=_config(capture__fs=None, process__data__cpi=None))) as stub:
        node_id = await _register(node_session, stub.port)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()) as task:
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 2 and not queue.empty())
            assert not task.done()

    assert [v.version for v in await _versions(maker, node_id)] == [1]


async def test_an_address_in_another_network_is_re_probed_and_keeps_its_trust(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port, resolved_ip="198.51.100.7")
        await _graduate(node_session, node_id)
        registered = await _row(maker, node_id)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()) as task:
            await _eventually(lambda: _moves(maker, node_id, 1))
            await _eventually(lambda: not queue.empty())
            assert not task.done()

    radar = await _row(maker, node_id)
    # The address says nothing about which box answers, so the epoch stands.
    assert (radar.epoch, radar.trust_state) == (1, "graduated")
    assert (radar.last_resolved_ip, radar.last_network_move_at is not None) == (STUB_ADDRESS, True)
    assert radar.probe_passed_at > registered.probe_passed_at
    assert [v.version for v in await _versions(maker, node_id)] == [1]


async def _moves(maker, node_id: str, expected: int) -> bool:
    return (await _row(maker, node_id)).network_moves == expected


async def test_a_recorded_move_is_not_probed_again(node_session, maker, queue, monkeypatch):
    real_probe = blah2_poller.probe_blah2
    probes = []

    async def probe(endpoint, **kwargs):
        probes.append(endpoint)
        return await real_probe(endpoint, **kwargs)

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port, resolved_ip="198.51.100.7")
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: _moves(maker, node_id, 1))
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 5)

    assert len(probes) == 1
    assert await _moves(maker, node_id, 1)


async def test_a_failed_re_probe_after_a_move_keeps_the_radar_streaming(node_session, maker, queue, monkeypatch):
    async def probe(endpoint, **kwargs):
        raise EndpointRefused(Refusal.TIMED_OUT, f"{endpoint.host} did not finish answering in time.")

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port, resolved_ip="198.51.100.7")
        registered = await _row(maker, node_id)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()):
            await _eventually(lambda: _moves(maker, node_id, 1))
            await _eventually(lambda: not queue.empty())

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.last_resolved_ip) == (1, STUB_ADDRESS)
    # Only a probe that passed says the radar was judged whole at this address.
    assert radar.probe_passed_at == registered.probe_passed_at


@pytest.mark.parametrize("resolved_ip", ["127.0.200.9", "2001:db8::7", None])
async def test_an_address_in_the_same_network_or_family_is_the_same_epoch(node_session, maker, queue, resolved_ip):
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port, resolved_ip=resolved_ip)
        async with _Running(_poller(_target(stub.port, node_id), maker).run()) as task:
            await _eventually(lambda: stub.paths.count(CONFIG_PATH) >= 2 and not queue.empty())
            assert not task.done()

    assert (await _row(maker, node_id)).epoch == 1


@pytest.mark.parametrize(
    ("previous", "current", "moved"),
    [
        ("198.51.100.7", ["203.0.113.9"], True),
        ("198.51.100.7", ["198.51.7.1"], False),
        # One address still in the old network is no move, so a name with
        # several records does not start an epoch per session.
        ("198.51.100.7", ["203.0.113.9", "198.51.3.3"], False),
        ("2001:db8:1::7", ["2001:db8:ffff::1"], False),
        ("2001:db8::7", ["2001:db9::7"], True),
        # Another family is not compared, so a dual-stack radar does not flap.
        ("2001:db8::7", ["198.51.100.7"], False),
        ("198.51.100.7", ["2001:db8::7"], False),
        (None, ["198.51.100.7"], False),
        ("not an address", ["198.51.100.7"], False),
    ],
)
def test_a_network_move(previous, current, moved):
    assert moved_network(previous, [ipaddress.ip_address(a) for a in current]) is moved


async def test_a_failed_re_probe_files_nothing_changes_nothing_and_is_retried(node_session, maker, queue, monkeypatch):
    probes = []

    async def probe(endpoint, **kwargs):
        probes.append(endpoint)
        raise EndpointRefused(Refusal.TIMED_OUT, f"{endpoint.host} did not finish answering in time.")

    monkeypatch.setattr(blah2_poller, "probe_blah2", probe)
    async with StubServer(_radar(config=_config(location__rx__latitude=10.6))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()) as task:
            await _eventually(lambda: len(probes) >= 2)
            await _eventually(lambda: _liveness(maker, node_id, STALLED))
            assert not task.done()

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.config_fingerprint) == (1, config_fingerprint(STOCK_CONFIG))
    assert radar.consecutive_failures == 0
    assert poller_.reason == Refusal.TIMED_OUT
    assert DETECTION_PATH not in stub.paths
    assert queue.empty()


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"truth": {"adsb": {"enabled": True}}}, Blah2Refusal.BLAH2_ARM),
        ({"hello": "world"}, Blah2Refusal.NOT_BLAH2),
        (
            _config(
                location__rx__latitude=-34.9286,
                location__rx__longitude=138.5999,
                location__rx__altitude=50,
                location__tx__latitude=-34.981,
                location__tx__longitude=138.7081,
                location__tx__altitude=750,
            ),
            Blah2Refusal.EXAMPLE_CONFIG,
        ),
    ],
)
async def test_a_configuration_that_is_no_longer_stock_blah2_files_nothing(node_session, maker, queue, config, code):
    async with StubServer(_radar(config=config)) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))

    radar = await _row(maker, node_id)
    assert (radar.epoch, radar.consecutive_failures, radar.last_config_at) == (1, 0, None)
    assert poller_.reason == code
    assert DETECTION_PATH not in stub.paths
    assert queue.empty()


@pytest.mark.parametrize(
    "change",
    [
        # Moves the fingerprint, so it is re-probed first.
        {"capture__fc": 500_000},
        # Leaves it, so it is not.
        {"capture__fs": 50_000},
    ],
)
async def test_a_declaration_the_server_cannot_hold_files_nothing(node_session, maker, queue, change):
    async with StubServer(_radar(config=_config(**change))) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, STALLED))

    assert poller_.reason == INVALID_CONFIG
    assert (await _row(maker, node_id)).epoch == 1
    assert [v.version for v in await _versions(maker, node_id)] == [1]
    assert queue.empty()


async def test_a_configuration_that_cannot_be_fetched_is_a_failed_poll(node_session, maker, queue):
    async with StubServer({CONFIG_PATH: _fixed(b"oops", status=500), DETECTION_PATH: _live()}) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = _poller(_target(stub.port, node_id), maker)
        async with _Running(poller_.run()):
            await _eventually(lambda: _liveness(maker, node_id, UNREACHABLE))

    assert poller_.reason == Refusal.BAD_RESPONSE
    assert DETECTION_PATH not in stub.paths


async def test_the_poller_hands_a_new_epoch_s_geometry_to_the_pipeline(node_session, maker, queue):
    config = copy.deepcopy(STOCK_CONFIG)
    async with StubServer(_radar(config=config)) as stub:
        node_id = await _register(node_session, stub.port)
        poller_ = Poller(maker, resolver=resolve_to_loopback, policy=only_loopback)
        async with _Running(poller_.run()):
            await _eventually(lambda: not queue.empty())
            config["location"]["rx"]["latitude"] = 10.6
            await _eventually(lambda: poller_.targets.get(node_id) and poller_.targets[node_id].epoch == 2)
            assert state.connected_nodes[node_id]["config"]["rx_lat"] == 10.6
            _drain(queue)
            await _eventually(lambda: not queue.empty())

    assert {frame["config_version"] for _, frame in _drain(queue)} == {2}


# ── The registry ──────────────────────────────────────────────────────────────


async def test_an_active_probed_radar_is_a_target(node_session, secret_key):
    node_id = await _register(node_session, 3000, auth_user="ada", auth_secret="s3cret", endpoint_raw=f"{HOST}:3000")

    targets = await load_targets(node_session)

    target = targets[node_id]
    assert (target.epoch, target.config_version) == (1, 1)
    assert (target.endpoint.host, target.endpoint.port, target.endpoint.scheme) == (HOST, 3000, "http")
    assert (target.endpoint.auth_user, target.endpoint.auth_secret) == ("ada", "s3cret")


async def test_a_blocked_node_is_not_a_target(node_session):
    node_id = await _register(node_session, 3000)
    await node_session.execute(update(Node).where(Node.node_id == node_id).values(status="blocked"))
    await node_session.commit()

    assert node_id not in await load_targets(node_session)


async def test_an_endpoint_changed_since_its_probe_is_not_a_target(node_session):
    node_id = await _register(node_session, 3000)
    radar = await node_session.get(PolledRadar, node_id)
    radar.endpoint_changed_at = radar.probe_passed_at + timedelta(seconds=1)
    await node_session.commit()

    assert node_id not in await load_targets(node_session)


async def test_a_credential_that_cannot_be_decrypted_is_not_polled_without_it(node_session, secret_key, monkeypatch):
    node_id = await _register(node_session, 3000, auth_user="ada", auth_secret="s3cret")
    monkeypatch.setenv("POLLED_RADAR_SECRET_KEY", Fernet.generate_key().decode())

    assert node_id not in await load_targets(node_session)


async def test_the_poller_follows_the_registry(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        poller_ = Poller(maker, resolver=resolve_to_loopback, policy=only_loopback)
        async with _Running(poller_.run()):
            node_id = await _register(node_session, stub.port)
            poller_.wake()
            await _eventually(lambda: node_id in poller_.targets)
            await _eventually(lambda: queue.qsize() >= 1)

            await node_session.execute(update(PolledRadar).where(PolledRadar.node_id == node_id).values(epoch=2))
            await node_session.commit()
            poller_.wake()
            await _eventually(lambda: poller_.targets.get(node_id) and poller_.targets[node_id].epoch == 2)

            await node_session.execute(update(Node).where(Node.node_id == node_id).values(status="blocked"))
            await node_session.commit()
            poller_.wake()
            await _eventually(lambda: node_id not in poller_.targets)
            _drain(queue)
            await asyncio.sleep(0.1)
            assert queue.empty()


async def test_a_radar_stopped_mid_query_leaves_the_registry_writable(node_session, maker, monkeypatch):
    """A cancel that lands while aiosqlite steps a SELECT leaves its cursor open until a collection frees it."""
    holding, release = threading.Event(), threading.Event()

    def hold() -> int:
        holding.set()
        release.wait(5)
        return 1

    real_active_config = blah2_poller.active_config

    async def held(session, node_id):
        connection = await (await session.connection()).get_raw_connection()
        await connection.driver_connection.create_function("hold", 0, hold)
        # Reading a table, as the poller's own queries do, is what takes the lock on the file.
        await session.execute(select(Node.node_id, func.hold()).where(Node.node_id == node_id))
        return await real_active_config(session, node_id)

    monkeypatch.setattr(blah2_poller, "active_config", held)
    async with StubServer(_radar()) as stub:
        node_id = await _register(node_session, stub.port)
        # Held across the commit. The cursor lives as long as the task does, which in the poller is until
        # the collector next runs.
        task = asyncio.create_task(_poller(_target(stub.port, node_id), maker).run())
        try:
            assert await asyncio.to_thread(holding.wait, 5)
        finally:
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)

        await node_session.execute(update(Node).where(Node.node_id == node_id).values(status="blocked"))
        await node_session.commit()

    assert task.cancelled()


async def test_the_registry_is_re_read_without_being_asked(node_session, maker, queue):
    poller_ = Poller(maker, resolver=resolve_to_loopback, policy=only_loopback)
    async with _Running(poller_.run()):
        await asyncio.sleep(0.05)
        node_id = await _register(node_session, 3000)
        await _eventually(lambda: node_id in poller_.targets)


async def test_stopping_the_poller_stops_every_radar(node_session, maker, queue):
    async with StubServer(_radar()) as stub:
        await _register(node_session, stub.port)
        poller_ = Poller(maker, resolver=resolve_to_loopback, policy=only_loopback)
        task = asyncio.create_task(poller_.run())
        await _eventually(lambda: queue.qsize() >= 1)
        radar_tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("poll ")]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert radar_tasks and all(t.done() for t in radar_tasks)
    assert poller_.targets == {}


@pytest.mark.parametrize("ending", [asyncio.CancelledError, RuntimeError])
async def test_stopping_the_poller_while_it_stops_a_radar(node_session, maker, monkeypatch, ending):
    """The poller's own cancel reaches it through the radar task it awaits, however that task ends."""
    stopping = asyncio.Event()

    async def slow_to_stop(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise ending from None

    monkeypatch.setattr(RadarPoller, "run", slow_to_stop)
    node_id = await _register(node_session, 3000)
    poller_ = Poller(maker, resolver=resolve_to_loopback, policy=only_loopback)
    async with _Running(poller_.run()) as task:
        await _eventually(lambda: node_id in poller_.targets)
        await node_session.execute(update(Node).where(Node.node_id == node_id).values(status="blocked"))
        await node_session.commit()
        poller_.wake()
        await asyncio.wait_for(stopping.wait(), 5)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)

    assert done == {task} and task.cancelled()


# ── Where it runs ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False), ("true", False), ("", False)])
def test_polling_is_on_only_for_exactly_1(monkeypatch, value, expected):
    monkeypatch.setenv(blah2_poller.ENABLED_ENV, value)
    assert blah2_poller.enabled() is expected


async def test_the_task_returns_at_once_where_polling_is_off(monkeypatch):
    monkeypatch.delenv(blah2_poller.ENABLED_ENV, raising=False)
    async with asyncio.timeout(1):
        await blah2_poller.poller_task()
    blah2_poller.refresh()


async def test_the_task_polls_where_polling_is_on(node_session, maker, queue, monkeypatch):
    import core.users

    monkeypatch.setenv(blah2_poller.ENABLED_ENV, "1")
    monkeypatch.setattr(core.users, "async_session_maker", maker)
    async with _Running(blah2_poller.poller_task()):
        await _eventually(lambda: blah2_poller._poller is not None)
        node_id = await _register(node_session, 3000)
        blah2_poller.refresh()
        await _eventually(lambda: node_id in blah2_poller._poller.targets)
    assert blah2_poller._poller is None


def test_health_reports_whether_this_deployment_polls(client, monkeypatch):
    monkeypatch.setenv(blah2_poller.ENABLED_ENV, "1")
    assert client.get("/api/health").json()["polled_radar_polling"] is True
    monkeypatch.delenv(blah2_poller.ENABLED_ENV)
    assert client.get("/api/health").json()["polled_radar_polling"] is False


class _LiveRadar(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (the stdlib's name)
        if self.path == DETECTION_PATH:
            body = _body(_now_ms())
        elif self.path == CONFIG_PATH:
            body = json.dumps(STOCK_CONFIG).encode()
        else:
            body = b""
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def test_the_running_app_polls_a_radar_into_its_frame_workers(node_session, monkeypatch):
    """The whole path in the app's own lifespan: priming, the poller, the frame workers."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    import core.users
    from main import app

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LiveRadar)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    node_id = await _register(node_session, port, host="127.0.0.1", endpoint_key=f"127.0.0.1:{port}")
    # The app runs on TestClient's own loop, so it gets an engine of its own.
    app_engine = create_async_engine(node_session.bind.url, poolclass=NullPool)
    monkeypatch.setattr(core.users, "async_session_maker", async_sessionmaker(app_engine, expire_on_commit=False))
    # A shard queue binds to the loop of the first lifespan that waits on it,
    # and any earlier TestClient in this process ran on a loop of its own.
    frames = state.frame_queue
    monkeypatch.setattr(state, "frame_queue", ShardedFrameQueue(frames.maxsize, frames.shard_count))
    monkeypatch.setenv(blah2_poller.ENABLED_ENV, "1")
    monkeypatch.setattr(polled_endpoint, "is_public_address", lambda address, deny=(): only_loopback(address))
    try:
        with TestClient(app, raise_server_exceptions=False):
            deadline = time.monotonic() + 10
            while node_id not in state.node_pipelines and time.monotonic() < deadline:
                time.sleep(0.05)
            assert node_id in state.node_pipelines
    finally:
        server.shutdown()
        await app_engine.dispose()
