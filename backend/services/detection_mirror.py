"""Forward accepted v1 detection frames to another environment's bulk ingest.

Inert unless DETECTION_MIRROR_URL is set, which is why every environment but
production behaves exactly as it did before this existed.

The frame is handed over as the wire model, not as the dict `submit_frame`
queued: that dict is stamped with `_node_id` and mutated further by the frame
workers, so sharing it would let the mirror send something a worker had since
altered. Conversion happens in the drain task instead, off the path that runs
at frame rate.
"""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx

from config.constants import (
    DETECTION_MIRROR_FLUSH_INTERVAL_S,
    DETECTION_MIRROR_LOG_INTERVAL_S,
    DETECTION_MIRROR_QUEUE_MAX,
    DETECTION_MIRROR_TIMEOUT_S,
)
from core import state
from services.node_pipeline import pipeline_frame

if TYPE_CHECKING:
    from routes.node_schemas import DetectionFrame

logger = logging.getLogger(__name__)

# Module-level rather than passed around: there is one mirror per process, and
# the request path reaches it through `offer` alone.
QUEUE_MAX = DETECTION_MIRROR_QUEUE_MAX
LOG_INTERVAL_S = DETECTION_MIRROR_LOG_INTERVAL_S

_url = ""
_key = ""
_queue: "asyncio.Queue[tuple[str, DetectionFrame]] | None" = None
# "accepted" is cumulative, not a queue depth: stats() adds the live depth
# alongside it so an operator cannot mistake one for the other. "rejected" is
# frames a 200 response admitted the receiver did not queue. "unregistered" is
# frames whose node left state.connected_nodes between offer() and the drain
# that would have sent them, so build_batch had nothing to carry them in —
# distinct from "dropped", which means the local queue was full.
_counters = {"accepted": 0, "dropped": 0, "sent": 0, "rejected": 0, "failed": 0, "unregistered": 0}


def configure_from_env(env=None) -> bool:
    """Arm the mirror if a target is configured, and report whether it is armed.

    Called once at startup, and by tests. Replaces the queue, clears the
    counters and resets the health state, so an unarmed call is also the reset.

    Refuses a non-https URL rather than arming against it: DETECTION_MIRROR_KEY
    would otherwise cross the wire in clear on every batch.
    """
    global _url, _key, _queue, _healthy, _logged_at, _dropped_logged_at, _dropped_at_last_log
    env = os.environ if env is None else env
    url = (env.get("DETECTION_MIRROR_URL") or "").rstrip("/")
    if url and not url.startswith("https://"):
        logger.error("DETECTION_MIRROR_URL must be https://, detection mirror stays unarmed")
        url = ""
    _url = url
    _key = env.get("DETECTION_MIRROR_KEY", "")
    _queue = asyncio.Queue(maxsize=QUEUE_MAX) if _url else None
    for name in _counters:
        _counters[name] = 0
    _healthy = True
    _logged_at = 0.0
    _dropped_logged_at = -LOG_INTERVAL_S  # not 0.0, for the same reason as the module-level default
    _dropped_at_last_log = 0
    if _queue is not None:
        logger.info("detection mirror armed, forwarding accepted v1 frames onward")
    return _queue is not None


def offer(node_id: str, frame: "DetectionFrame") -> None:
    """Hand one accepted frame to the mirror.

    Called from the ingest path, so it does no I/O, holds no lock of its own and
    cannot raise: a full queue is a dropped frame, which is the whole of the
    backpressure policy. Retrying or buffering here would put a backlog on
    production, which is the thing this design exists to avoid.
    """
    if _queue is None:
        return
    try:
        _queue.put_nowait((node_id, frame))
    except asyncio.QueueFull:
        _counters["dropped"] += 1
        return
    _counters["accepted"] += 1


def drain() -> list:
    """Everything queued, taken at once. Empty when unarmed."""
    items = []
    if _queue is None:
        return items
    while True:
        try:
            items.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


def stats() -> dict:
    """Cumulative counters plus the queue's live depth.

    `_counters` alone reads as a snapshot of the backlog; `queue_depth` is the
    one number here that actually is one.
    """
    depth = _queue.qsize() if _queue is not None else 0
    return {**_counters, "queue_depth": depth}


def build_batch(items) -> list:
    """One entry per node, in the shape routes/radar.py's BulkNodeEntry takes.

    The config is read from the registry rather than the database: production
    already holds it, and the receiving endpoint needs it to give the node any
    geometry at all. A node that has since left the registry is skipped
    outright rather than sent with `config: None`: the receiving endpoint now
    hashes whatever config it is given and re-registers on a mismatch, so
    `None` would replace that node's last-known geometry with none and evict
    its cached pipeline, with nothing left to put it back since no further
    batch would carry that node's id.
    """
    by_node: dict[str, list] = {}
    for node_id, frame in items:
        by_node.setdefault(node_id, []).append(pipeline_frame(frame))
    entries = []
    for node_id, frames in by_node.items():
        with state.connected_nodes_lock:
            known = state.connected_nodes.get(node_id)
            config = dict(known["config"]) if known and known.get("config") else None
        if config is None:
            continue
        entries.append({"node_id": node_id, "config": config, "frames": frames})
    return entries


async def send_batch(client, entries) -> bool:
    """POST one batch. Reports success; never raises, and never retries.

    Success is judged on the response body's `frames_queued`, not on the status
    code alone: the receiving endpoint answers 200 with a shortfall for a frame
    missing `timestamp` or a full `state.frame_queue` on its side, both of which
    it treats as unremarkable. Taking the status code at face value would make a
    receiver silently discarding every frame indistinguishable from a healthy
    one. A response that is not JSON, or that carries no `frames_queued`, is
    folded into the same failure path as a network error.
    """
    frame_count = sum(len(entry["frames"]) for entry in entries)
    try:
        response = await client.post(
            f"{_url}/api/radar/detections/bulk",
            json={"nodes": entries},
            headers={"X-API-Key": _key},
        )
        response.raise_for_status()
        landed = int(response.json()["frames_queued"])
    except Exception as exc:
        _counters["failed"] += frame_count
        _note(False, exc)
        return False
    shortfall = frame_count - landed
    if shortfall > 0:
        _counters["sent"] += landed
        _counters["rejected"] += shortfall
        _note(False, RuntimeError(f"receiver only queued {landed}/{frame_count} frames"))
        return False
    _counters["sent"] += frame_count
    _note(True, None)
    return True


async def mirror_task() -> None:
    """Drain and send once a second. Returns immediately when unarmed.

    The drain-and-convert step is guarded the same as the network call: left
    unguarded, a raise here would exit the loop for good, and the queue would
    fill and drop every frame afterward with no sign anything had stopped.
    """
    if _queue is None:
        return
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=2)
    async with httpx.AsyncClient(timeout=DETECTION_MIRROR_TIMEOUT_S, limits=limits) as client:
        while True:
            await asyncio.sleep(DETECTION_MIRROR_FLUSH_INTERVAL_S)
            _note_dropped()
            items = []
            try:
                items = drain()
                if items:
                    entries = build_batch(items)
                    built = sum(len(entry["frames"]) for entry in entries)
                    # A node that left state.connected_nodes before the batch
                    # was built is missing from entries (build_batch skips it,
                    # see its docstring), whether it is the whole drain or
                    # shares the drain with a survivor. Crediting the
                    # shortfall here, rather than only when entries is empty,
                    # is what keeps sent + rejected + failed + unregistered
                    # reconciling against accepted.
                    if len(items) > built:
                        _counters["unregistered"] += len(items) - built
                    if entries:
                        await send_batch(client, entries)
            except Exception as exc:
                _counters["failed"] += len(items)
                _note(False, exc)


_healthy = True
_logged_at = 0.0
# Separate throttle from the pair above: the local queue can saturate while the
# receiver stays perfectly healthy, so a drop is not a healthy/failing transition.
# -LOG_INTERVAL_S, not 0.0: time.monotonic() counts from boot, so 0.0 would
# suppress the first drop line on a host that has been up less than LOG_INTERVAL_S.
_dropped_logged_at = -LOG_INTERVAL_S
_dropped_at_last_log = 0


def _note(ok: bool, exc: Exception | None) -> None:
    """Log a transition immediately, and a continuing fault once a minute."""
    global _healthy, _logged_at
    if ok:
        if not _healthy:
            _healthy = True
            logger.warning("detection mirror recovered (%s)", stats())
            _log_event("detection_mirror", "Detection mirror recovered", "info", stats())
        return
    now = time.monotonic()
    if _healthy:
        _healthy = False
        _logged_at = now
        logger.warning("detection mirror failing: %s (%s)", exc, stats())
        _log_event("detection_mirror", f"Detection mirror failing: {exc}", "warning", stats())
        return
    if now - _logged_at >= LOG_INTERVAL_S:
        _logged_at = now
        logger.warning("detection mirror still failing: %s (%s)", exc, stats())


def _note_dropped() -> None:
    """Give a saturated local queue its own line, once per LOG_INTERVAL_S.

    Without this, `dropped` only ever surfaced bundled inside a failure line
    logged by `_note`, so a healthy receiver made a saturated queue invisible.
    """
    global _dropped_logged_at, _dropped_at_last_log
    dropped = _counters["dropped"]
    if dropped == _dropped_at_last_log:
        return
    now = time.monotonic()
    if now - _dropped_logged_at < LOG_INTERVAL_S:
        return
    _dropped_logged_at = now
    _dropped_at_last_log = dropped
    logger.warning("detection mirror dropping frames, local queue is full (%s)", stats())


# Lazy import for the reason services/tcp_handler.py gives: routes.admin must be
# importable first.
def _log_event(category: str, message: str, severity: str, meta: dict) -> None:
    try:
        from routes.admin import log_event

        log_event(category, message, severity, meta)
    except Exception:
        logger.debug("event log write failed", exc_info=True)
