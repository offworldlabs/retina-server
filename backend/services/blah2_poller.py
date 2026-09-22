"""Polling registered stock-blah2 radars into the pipeline.

Stock blah2 opens no connection of its own, so the server fetches from it: one
task per registered radar asks for /api/detection once a second and files each
new frame the way a v1 node's POST is filed (routes/node_stream.py). A radar
must be polled by exactly one server, so polling runs only where
POLLED_RADAR_POLLING_ENABLED is exactly `1`, which is production alone.

Every request goes through services/polled_endpoint.py. A radar is polled in
sessions: each one resolves and vets the address, pins a client to it, and polls
for SESSION_S, so a name is judged afresh at least once a session and on every
reconnect after a failure, and a resolution is never held longer than that.

Liveness is the server's own record of what the radar did, since stock blah2
reports nothing about itself: `streaming` while new frames arrive, `stalled`
while it answers without one, and `unreachable` once polls keep failing.
"""

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.constants import C_KM_US
from core import state
from core.nodes import Node, PolledRadar
from core.secrets import SecretKeyUnavailable
from routes.node_schemas import MAX_DETECTIONS, DetectionFrame
from services import detection_mirror
from services.blah2_probe import (
    DETECTION_MAX_BYTES,
    DETECTION_PATH,
    MAX_CLOCK_OFFSET_S,
    Blah2Refusal,
    parse_detection,
)
from services.blah2_probe import DetectionFrame as Blah2Frame
from services.node_pipeline import pipeline_frame, register_with_pipeline, submit_frame
from services.polled_endpoint import (
    AddressPolicy,
    EndpointRefused,
    PolledEndpoint,
    Resolver,
    pinned_client,
    resolve_host,
)
from services.polled_radars import poller_credentials

logger = logging.getLogger(__name__)

ENABLED_ENV = "POLLED_RADAR_POLLING_ENABLED"

POLL_INTERVAL_S = 1.0
SESSION_S = 60.0
# Consecutive failed polls before a radar is unreachable and backs off.
MAX_FAILURES = 5
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 60.0
# Without a new frame for this long, an answering radar is stalled.
STALLED_AFTER_S = 10.0
# Liveness is written on every change and otherwise at most this often.
PERSIST_INTERVAL_S = 60.0
# How often the registry is re-read when nothing has asked for it sooner.
RELOAD_INTERVAL_S = 60.0
# Requests in flight across every radar at once.
MAX_IN_FLIGHT = 32
# How often a radar missing from the pipeline registries tries to rejoin them.
REREGISTER_INTERVAL_S = 60.0

PENDING = "pending"
STREAMING = "streaming"
STALLED = "stalled"
UNREACHABLE = "unreachable"

# Why an answer carried no frame, beside the probe's own refusal codes.
TOO_MANY_DETECTIONS = "too_many_detections"


def _raise_if_cancelled() -> None:
    """Re-raise a cancellation aimed at this task that was absorbed on the way.

    The cancel stays counted either way: anyio's connect can swallow one that
    lands as a connection completes, and one that arrives while this task
    awaits another is delivered as that task's ending.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def enabled() -> bool:
    """Whether this deployment polls registered radars. Exactly `1`, nothing else."""
    return os.environ.get(ENABLED_ENV, "") == "1"


@dataclass(frozen=True)
class PollTarget:
    """What a radar's task polls. A change to any field restarts the task."""

    node_id: str
    epoch: int
    config_version: int
    endpoint: PolledEndpoint


def wire_frame(frame: Blah2Frame, *, seq: int, boot_id: str, config_version: int) -> DetectionFrame | None:
    """A blah2 frame as the v1 wire model, or None when the contract cannot carry it.

    blah2 gives delay in km of bistatic range; the wire wants microseconds.
    None for more detections than a v1 frame holds, since v1 files a frame
    whole or not at all.
    """
    if len(frame.delay_km) > MAX_DETECTIONS:
        return None
    try:
        return DetectionFrame(
            t=frame.timestamp_ms / 1000,
            seq=seq,
            boot_id=boot_id,
            config_version=config_version,
            delay=[d / C_KM_US for d in frame.delay_km],
            doppler=list(frame.doppler_hz),
            snr=list(frame.snr_db),
            adsb_hex=[None] * len(frame.delay_km),
        )
    except ValidationError:
        return None


async def load_targets(session: AsyncSession) -> dict[str, PollTarget]:
    """Every radar that should be polled now, by node id.

    A radar is polled while its node is active and its latest probe postdates
    its latest endpoint change. One whose credential cannot be decrypted is
    left out, since it could only be polled without it.
    """
    rows = await session.execute(
        select(PolledRadar, Node.active_config_version)
        .join(Node, Node.node_id == PolledRadar.node_id)
        .where(Node.status == "active", PolledRadar.probe_passed_at >= PolledRadar.endpoint_changed_at)
    )
    targets = {}
    for radar, config_version in rows:
        if config_version is None:
            logger.error("polled radar %s has no active configuration; not polling it", radar.node_id)
            continue
        try:
            credentials = poller_credentials(radar)
        except (SecretKeyUnavailable, InvalidToken):
            logger.error("polled radar %s: its credential cannot be decrypted; not polling it", radar.node_id)
            continue
        auth_user, auth_secret = credentials if credentials is not None else (radar.auth_user, None)
        targets[radar.node_id] = PollTarget(
            node_id=radar.node_id,
            epoch=radar.epoch,
            config_version=config_version,
            endpoint=PolledEndpoint(
                scheme=radar.scheme,
                host=radar.host,
                port=radar.port,
                endpoint_key=radar.endpoint_key,
                auth_user=auth_user,
                auth_secret=auth_secret,
                raw=radar.endpoint_raw,
            ),
        )
    return targets


class RadarPoller:
    """Polls one radar for as long as its task runs."""

    def __init__(
        self,
        target: PollTarget,
        *,
        session_maker: async_sessionmaker,
        semaphore: asyncio.Semaphore,
        resolver: Resolver = resolve_host,
        policy: AddressPolicy | None = None,
    ) -> None:
        self.target = target
        self._session_maker = session_maker
        self._semaphore = semaphore
        self._resolver = resolver
        self._policy = policy
        # A fresh pair per task, so the pipeline counts loss per run of the poller.
        self.boot_id = secrets.token_hex(8)
        self.seq = 0
        self.liveness = PENDING
        self.failures = 0
        self.last_timestamp_ms = 0
        # Radar clock minus server clock at the latest parsed frame.
        self.clock_offset_s: float | None = None
        # Why the latest poll produced no frame, as a refusal code.
        self.reason: str | None = None
        self._answered_at: datetime | None = None
        self._frame_at: datetime | None = None
        # The latest filed frame, or the task's start before there is one.
        self._since = time.monotonic()
        self._persist_due = 0.0
        self._register_due = 0.0

    async def run(self) -> None:
        while True:
            _raise_if_cancelled()
            try:
                await self._session()
                continue
            except EndpointRefused as exc:
                self.failures += 1
                self.reason = exc.code
            except Exception:
                # A fault in this code must not end the radar's polling for good.
                logger.exception("polled radar %s: poll failed unexpectedly", self.target.node_id)
                self.failures += 1
                self.reason = "error"
            await self._settle()
            await asyncio.sleep(self._retry_delay_s())

    def _retry_delay_s(self) -> float:
        if self.failures < MAX_FAILURES:
            return POLL_INTERVAL_S
        return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** min(self.failures - MAX_FAILURES, 16))

    async def _session(self) -> None:
        async with pinned_client(self.target.endpoint, resolver=self._resolver, policy=self._policy) as client:
            ends = time.monotonic() + SESSION_S
            while True:
                started = time.monotonic()
                async with self._semaphore:
                    body = await client.get(DETECTION_PATH, max_bytes=DETECTION_MAX_BYTES)
                _raise_if_cancelled()
                frame = self._answered(body)
                if frame is not None:
                    await self._file(frame)
                await self._settle()
                if time.monotonic() >= ends:
                    return
                await asyncio.sleep(max(0.0, POLL_INTERVAL_S - (time.monotonic() - started)))

    def _answered(self, body: bytes) -> Blah2Frame | None:
        """The frame in an answer, if it is new and on time."""
        self.failures = 0
        self._answered_at = datetime.now(UTC)
        with state.connected_nodes_lock:
            entry = state.connected_nodes.get(self.target.node_id)
            if entry is not None:
                entry["last_heartbeat"] = self._answered_at.isoformat()
        try:
            frame = parse_detection(body)
        except EndpointRefused as exc:
            self.reason = exc.code
            return None
        # An idle radar serves its last frame forever, and a repeat or a step
        # back would hand the tracker a non-positive dt.
        if frame.timestamp_ms <= self.last_timestamp_ms:
            self.reason = Blah2Refusal.STALLED
            return None
        offset_s = frame.timestamp_ms / 1000 - time.time()
        self.clock_offset_s = round(offset_s, 3)
        # Held back rather than filed: association pairs frames across nodes by
        # their capture times. Not recorded as the latest either, or a clock
        # that runs ahead and is then corrected would hold back every frame
        # until real time caught up with it.
        if abs(offset_s) > MAX_CLOCK_OFFSET_S:
            self.reason = Blah2Refusal.CLOCK_OFFSET
            return None
        self.last_timestamp_ms = frame.timestamp_ms
        return frame

    async def _file(self, frame: Blah2Frame) -> None:
        """Queue one frame and offer it to the mirror, as a v1 node's frame is.

        An empty frame is filed too: it is what ages out a coasting track.
        """
        node_id = self.target.node_id
        wire = wire_frame(frame, seq=self.seq, boot_id=self.boot_id, config_version=self.target.config_version)
        # Counted whether or not it is queued, so a dropped frame reads as a gap.
        self.seq += 1
        if wire is None:
            self.reason = TOO_MANY_DETECTIONS
            return
        if not await self._in_pipeline():
            state.bump_counter("frames_dropped")
            return
        if not submit_frame(node_id, pipeline_frame(wire)):
            return
        detection_mirror.offer(node_id, wire)
        self._since = time.monotonic()
        self._frame_at = datetime.now(UTC)
        self.reason = None

    async def _in_pipeline(self) -> bool:
        """Whether the node is in the pipeline registries, rejoining them if it can.

        Startup priming registers every active node, and a radar registered
        since is added here on its first frame.
        """
        node_id = self.target.node_id
        with state.connected_nodes_lock:
            if node_id in state.connected_nodes:
                return True
        now = time.monotonic()
        if now < self._register_due:
            return False
        self._register_due = now + REREGISTER_INTERVAL_S
        async with self._session_maker() as session:
            node = await session.get(Node, node_id)
            if node is None or node.status != "active":
                return False
            try:
                await register_with_pipeline(session, node)
            except ValueError:
                logger.warning("polled radar %s has no active configuration; its frames are dropped", node_id)
                return False
        return True

    def _current_liveness(self) -> str:
        if self.failures >= MAX_FAILURES:
            return UNREACHABLE
        quiet_s = time.monotonic() - self._since
        if self._frame_at is not None and quiet_s < STALLED_AFTER_S:
            return STREAMING
        if self._answered_at is not None and quiet_s >= STALLED_AFTER_S:
            return STALLED
        return self.liveness

    async def _settle(self) -> None:
        """Move liveness on, and record it when it changed or is due."""
        liveness = self._current_liveness()
        changed = liveness != self.liveness
        if changed:
            self.liveness = liveness
            self._log_transition()
        if changed or time.monotonic() >= self._persist_due:
            self._persist_due = time.monotonic() + PERSIST_INTERVAL_S
            await self._persist()

    def _log_transition(self) -> None:
        # WARNING, for the reason prime_pipeline gives: uvicorn's root logger
        # sits at WARNING in every deployed environment. Transitions are rare.
        node_id = self.target.node_id
        if self.liveness == STREAMING:
            logger.warning("polled radar %s: streaming", node_id)
        elif self.liveness == STALLED:
            logger.warning(
                "polled radar %s: stalled, answering with no new frame for %g s (%s, clock offset %s s)",
                node_id,
                STALLED_AFTER_S,
                self.reason,
                self.clock_offset_s,
            )
        elif self.liveness == UNREACHABLE:
            logger.warning(
                "polled radar %s: unreachable after %d failed polls (%s); backing off",
                node_id,
                self.failures,
                self.reason,
            )

    async def _persist(self) -> None:
        """Write liveness to the radar's row and the latest answer to its node.

        Keyed on the epoch as well as the id, so a task still running for an
        epoch that has since moved on writes nothing.
        """
        node_id = self.target.node_id
        values: dict = {"liveness": self.liveness, "consecutive_failures": self.failures}
        if self._frame_at is not None:
            values["last_frame_at"] = self._frame_at
        try:
            async with self._session_maker() as session:
                await session.execute(
                    update(PolledRadar)
                    .where(PolledRadar.node_id == node_id, PolledRadar.epoch == self.target.epoch)
                    .values(**values)
                )
                if self._answered_at is not None:
                    await session.execute(
                        update(Node).where(Node.node_id == node_id).values(last_seen_at=self._answered_at)
                    )
                await session.commit()
        except Exception:
            logger.exception("polled radar %s: could not record its liveness", node_id)


class Poller:
    """One task per radar in the registry, kept in step with it."""

    def __init__(
        self,
        session_maker: async_sessionmaker,
        *,
        resolver: Resolver = resolve_host,
        policy: AddressPolicy | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._resolver = resolver
        self._policy = policy
        self._semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._wake = asyncio.Event()
        self._running: dict[str, tuple[PollTarget, asyncio.Task]] = {}

    @property
    def targets(self) -> dict[str, PollTarget]:
        return {node_id: target for node_id, (target, _) in self._running.items()}

    def wake(self) -> None:
        self._wake.set()

    async def sync(self) -> None:
        """Start, stop and restart radars' tasks to match the registry."""
        async with self._session_maker() as session:
            targets = await load_targets(session)
        stopping = [
            node_id
            for node_id, (target, task) in self._running.items()
            if targets.get(node_id) != target or task.done()
        ]
        for node_id in stopping:
            _, task = self._running.pop(node_id)
            await _stop(task)
        for node_id, target in targets.items():
            if node_id not in self._running:
                poller = RadarPoller(
                    target,
                    session_maker=self._session_maker,
                    semaphore=self._semaphore,
                    resolver=self._resolver,
                    policy=self._policy,
                )
                self._running[node_id] = (target, asyncio.create_task(poller.run(), name=f"poll {node_id}"))

    async def run(self) -> None:
        """Keep in step with the registry until cancelled, then stop every radar."""
        try:
            while True:
                self._wake.clear()
                before = set(self._running)
                try:
                    await self.sync()
                except Exception:
                    logger.exception("polled radars: could not read the registry")
                if set(self._running) != before:
                    logger.warning("polled radars: polling %d radar(s)", len(self._running))
                try:
                    async with asyncio.timeout(RELOAD_INTERVAL_S):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        finally:
            running = [task for _, task in self._running.values()]
            self._running.clear()
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("polled radars: a radar's task had failed")
    # Either ending may have carried a cancel aimed at the caller.
    _raise_if_cancelled()


_poller: Poller | None = None


async def poller_task() -> None:
    """The lifespan's task: poll every registered radar, or return at once where polling is off."""
    global _poller
    if not enabled():
        logger.info("Registered radars are not polled here (%s is not 1)", ENABLED_ENV)
        return
    # Resolved at call time, so tests can substitute the session maker.
    import core.users

    _poller = Poller(core.users.async_session_maker)
    logger.warning("Polling registered stock-blah2 radars (%s=1)", ENABLED_ENV)
    try:
        await _poller.run()
    finally:
        _poller = None


def refresh() -> None:
    """Re-read the registry now, after a committed create, edit or block.

    A no-op where polling is off. Without it a change still takes effect
    within RELOAD_INTERVAL_S.
    """
    if _poller is not None:
        _poller.wake()
