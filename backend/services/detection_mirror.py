"""Forward accepted v1 detection frames to other environments' bulk ingest.

Inert unless DETECTION_MIRROR_URL is set, which is why every environment but
production behaves exactly as it did before this existed.

The frame is handed over as the wire model, not as the dict `submit_frame`
queued: that dict is stamped with `_node_id` and mutated further by the frame
workers, so sharing it would let the mirror send something a worker had since
altered. Conversion happens in the drain task instead, off the path that runs
at frame rate.

One queue feeds every target: the batch is built once per flush and the same
batch is posted to each target concurrently, so a slow or dead receiver costs
its own frames and never delays another's.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from config.constants import (
    DETECTION_MIRROR_FLUSH_INTERVAL_S,
    DETECTION_MIRROR_LOG_INTERVAL_S,
    DETECTION_MIRROR_QUEUE_MAX,
    DETECTION_MIRROR_TIMEOUT_S,
)
from core import state
from services import node_refs, probation
from services.node_pipeline import pipeline_frame

if TYPE_CHECKING:
    from routes.node_schemas import DetectionFrame

logger = logging.getLogger(__name__)

# Module-level rather than passed around: there is one mirror per process, and
# the request path reaches it through `offer` alone.
QUEUE_MAX = DETECTION_MIRROR_QUEUE_MAX
LOG_INTERVAL_S = DETECTION_MIRROR_LOG_INTERVAL_S


@dataclass
class Target:
    """One receiving environment, with the accounting and health that are its own.

    "sent" is frames the receiver's response admitted it queued, "rejected" is
    frames a 200 response admitted it did not, and "failed" is frames that
    never got a usable answer. Per target rather than global because the same
    batch goes to every target and each can lose it independently: for each
    target, sent + rejected + failed + the global unregistered reconciles
    against the global accepted.
    """

    label: str
    url: str
    key: str
    sent: int = 0
    rejected: int = 0
    failed: int = 0
    healthy: bool = True
    logged_at: float = 0.0

    def stats(self) -> dict:
        return {"sent": self.sent, "rejected": self.rejected, "failed": self.failed, "healthy": self.healthy}


_targets: list[Target] = []
_queue: "asyncio.Queue[tuple[str, DetectionFrame]] | None" = None
# "accepted" is cumulative, not a queue depth: stats() adds the live depth
# alongside it so an operator cannot mistake one for the other. "unregistered"
# is frames whose node left state.connected_nodes between offer() and the drain
# that would have sent them, so build_batch had nothing to carry them in —
# distinct from "dropped", which means the local queue was full. All three are
# properties of the one queue, which is why they are not on Target.
_counters = {"accepted": 0, "dropped": 0, "unregistered": 0}


def _parse_targets(env) -> list[Target]:
    """The configured targets, or an empty list when there are none or the
    configuration is unusable.

    DETECTION_MIRROR_URL and DETECTION_MIRROR_KEY are parallel comma-separated
    lists, whitespace trimmed, so the single-value form is the one-entry case
    of the same rule. The lists are positional: a key entry may be left empty
    (`k1,` sends the second receiver no key, which is that receiver's business
    to accept or refuse), whereas an empty URL entry is an error, since it
    would otherwise silently shift every key after it onto the wrong receiver.
    A mismatch in length, a non-https URL or a duplicate host refuses the whole
    configuration rather than arming the targets that were fine: half a mirror
    with an error line nobody reads is harder to notice than no mirror at all.
    Only https, because DETECTION_MIRROR_KEY would otherwise cross the wire in
    clear on every batch.

    The one allowance is a single URL with DETECTION_MIRROR_KEY unset, which
    armed before the lists existed and still does. With more than one URL the
    variable must be present so that a keyless receiver is a stated choice
    rather than a forgotten line.
    """
    raw_urls = (env.get("DETECTION_MIRROR_URL") or "").strip()
    raw_keys = (env.get("DETECTION_MIRROR_KEY") or "").strip()
    if not raw_urls:
        return []
    urls = [u.strip().rstrip("/") for u in raw_urls.split(",")]
    keys = [k.strip() for k in raw_keys.split(",")] if raw_keys else []
    if not keys and len(urls) == 1:
        keys = [""]
    if len(keys) != len(urls):
        logger.error(
            "DETECTION_MIRROR_URL lists %d targets but DETECTION_MIRROR_KEY lists %d keys, detection mirror stays unarmed",
            len(urls),
            len(keys),
        )
        return []
    targets: list[Target] = []
    for url, key in zip(urls, keys, strict=True):
        label = urlsplit(url).netloc
        if not url.startswith("https://") or not label:
            logger.error("DETECTION_MIRROR_URL entries must be https:// with a host, detection mirror stays unarmed")
            return []
        if any(t.label == label for t in targets):
            logger.error("DETECTION_MIRROR_URL names %s twice, detection mirror stays unarmed", label)
            return []
        targets.append(Target(label=label, url=url, key=key))
    return targets


def configure_from_env(env=None) -> bool:
    """Arm the mirror if at least one target is configured, and report whether it is armed.

    Called once at startup, and by tests. Replaces the queue and the targets
    and clears the counters, so an unarmed call is also the reset.
    """
    global _targets, _queue, _dropped_logged_at, _dropped_at_last_log
    env = os.environ if env is None else env
    _targets = _parse_targets(env)
    _queue = asyncio.Queue(maxsize=QUEUE_MAX) if _targets else None
    for name in _counters:
        _counters[name] = 0
    _dropped_logged_at = -LOG_INTERVAL_S  # not 0.0, for the same reason as the module-level default
    _dropped_at_last_log = 0
    if _queue is not None:
        logger.info("detection mirror armed, forwarding accepted v1 frames to %s", ", ".join(t.label for t in _targets))
    return _queue is not None


def offer(node_id: str, frame: "DetectionFrame") -> None:
    """Hand one accepted frame to the mirror.

    Called from the ingest path, so it does no I/O, holds no lock of its own and
    cannot raise: a full queue is a dropped frame, which is the whole of the
    backpressure policy. Retrying or buffering here would put a backlog on
    production, which is the thing this design exists to avoid.
    """
    # A receiver would solve and archive what it is sent.
    if _queue is None or probation.in_probation(node_id):
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
    """Queue counters plus the queue's live depth, and each target's own accounting by label.

    `_counters` alone reads as a snapshot of the backlog; `queue_depth` is the
    one number here that actually is one.
    """
    depth = _queue.qsize() if _queue is not None else 0
    return {**_counters, "queue_depth": depth, "targets": {t.label: t.stats() for t in _targets}}


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
        # The receiving environment has no registry row for this node, so the
        # ref goes with the detections or it cannot name the node at all.
        entry = {"node_id": node_id, "config": config, "frames": frames}
        ref = node_refs.ref_for(node_id)
        if ref:
            entry["node_ref"] = ref
        entries.append(entry)
    return entries


async def send_to_target(client, target: Target, entries) -> bool:
    """POST one batch to one target. Reports success; never raises, and never retries.

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
            f"{target.url}/api/radar/detections/bulk",
            json={"nodes": entries},
            headers={"X-API-Key": target.key},
        )
        response.raise_for_status()
        landed = int(response.json()["frames_queued"])
    except Exception as exc:
        target.failed += frame_count
        _note(target, False, exc)
        return False
    shortfall = frame_count - landed
    if shortfall > 0:
        target.sent += landed
        target.rejected += shortfall
        _note(target, False, RuntimeError(f"receiver only queued {landed}/{frame_count} frames"))
        return False
    target.sent += frame_count
    _note(target, True, None)
    return True


async def send_batch(client, entries) -> bool:
    """POST one batch to every target at once. True only when every target took all of it.

    Concurrent rather than in turn so a target that hangs until the client
    timeout cannot hold the others' frames back for that long. Each send does
    its own accounting and cannot raise; `return_exceptions` is the guard
    against that guarantee ever slipping, so one target's fault stays one
    target's fault rather than surfacing through the gather as everyone's.
    """
    results = await asyncio.gather(
        *(send_to_target(client, target, entries) for target in _targets), return_exceptions=True
    )
    return all(result is True for result in results)


async def mirror_task() -> None:
    """Drain and send once a second. Returns immediately when unarmed.

    The drain-and-convert step is guarded the same as the network call: left
    unguarded, a raise here would exit the loop for good, and the queue would
    fill and drop every frame afterward with no sign anything had stopped. A
    fault there happens before any target is tried, so every target lost the
    frames and every target is charged for them.
    """
    if _queue is None:
        return
    # Two per target: the batches are concurrent, so a pool sized for one
    # target would serialise the rest behind whichever is slowest.
    pool = 2 * len(_targets)
    limits = httpx.Limits(max_connections=pool, max_keepalive_connections=pool)
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
                    # is what keeps each target's sent + rejected + failed +
                    # unregistered reconciling against accepted.
                    if len(items) > built:
                        _counters["unregistered"] += len(items) - built
                    if entries:
                        await send_batch(client, entries)
            except Exception as exc:
                for target in _targets:
                    target.failed += len(items)
                    _note(target, False, exc)


# Throttle for the dropped-frame line, separate from each target's health: the
# local queue can saturate while every receiver stays perfectly healthy, so a
# drop is not a healthy/failing transition.
# -LOG_INTERVAL_S, not 0.0: time.monotonic() counts from boot, so 0.0 would
# suppress the first drop line on a host that has been up less than LOG_INTERVAL_S.
_dropped_logged_at = -LOG_INTERVAL_S
_dropped_at_last_log = 0


def _note(target: Target, ok: bool, exc: Exception | None) -> None:
    """Log a target's transition immediately, and its continuing fault once a minute.

    The target is named in the line and the event so a failing receiver is
    distinguishable from a healthy one beside it.
    """
    if ok:
        if not target.healthy:
            target.healthy = True
            logger.warning("detection mirror to %s recovered (%s)", target.label, stats())
            _log_event("detection_mirror", f"Detection mirror to {target.label} recovered", "info", stats())
        return
    now = time.monotonic()
    if target.healthy:
        target.healthy = False
        target.logged_at = now
        logger.warning("detection mirror to %s failing: %s (%s)", target.label, exc, stats())
        _log_event("detection_mirror", f"Detection mirror to {target.label} failing: {exc}", "warning", stats())
        return
    if now - target.logged_at >= LOG_INTERVAL_S:
        target.logged_at = now
        logger.warning("detection mirror to %s still failing: %s (%s)", target.label, exc, stats())


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
