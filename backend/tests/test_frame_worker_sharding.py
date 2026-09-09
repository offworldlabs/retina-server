"""Frame workers must not process one node's frames concurrently.

Before the queue was sharded, FRAME_WORKERS copies of frame_processor_loop
drained one shared queue, so two consecutive frames from the same node ran in
process_one_frame at the same time and mutated the same RetinaTracker from two
threads: 108 `Frame processing failed` IndexErrors out of tracker._associate in
40 minutes on the live server, plus the interleavings that never raised
(out-of-order Kalman predict/update, missed M-of-N promotions).

These tests pin the fix: per-node serialisation, per-node arrival order,
cross-node parallelism, and a queue-depth metric that still sees every frame in
flight.  The first two fail against the old shared-queue loop — the recorder
below detects any overlap of a node with itself, which the old code produced on
every pair of frames it had workers for.
"""

import asyncio
import threading
import time
import zlib

import pytest

from core import state
from core.frame_queue import ShardedFrameQueue
from services.tasks import frame_loop

# frame_processor_loop passes this straight through to process_one_frame, which
# is stubbed out here, so it never has to be a real pipeline.
_PIPELINE = object()


class _Recorder:
    """Stands in for process_one_frame and records who ran when.

    Each call holds its slot for `hold_s`, which is what gives a second frame
    for the same node the chance to overlap it if anything still allows that.
    """

    def __init__(self, hold_s: float = 0.05) -> None:
        self.hold_s = hold_s
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}
        self.same_node_overlaps = 0
        self.peak_parallel = 0
        self.sequence: list[tuple[str, int]] = []
        self.completed = 0

    def __call__(self, node_id, frame, _default_pipeline):
        with self._lock:
            running = self._active.get(node_id, 0) + 1
            self._active[node_id] = running
            if running > 1:
                self.same_node_overlaps += 1
            self.peak_parallel = max(self.peak_parallel, sum(self._active.values()))
            self.sequence.append((node_id, frame["seq"]))
        time.sleep(self.hold_s)
        with self._lock:
            self._active[node_id] -= 1
            self.completed += 1

    def order_for(self, node_id: str) -> list[int]:
        return [seq for node, seq in self.sequence if node == node_id]


class _PairedRecorder:
    """Fails the barrier unless two frames really are in flight together."""

    def __init__(self, parties: int = 2, timeout: float = 5.0) -> None:
        self._barrier = threading.Barrier(parties, timeout=timeout)
        self.ran_in_parallel = False
        self.completed = 0
        self._lock = threading.Lock()

    def __call__(self, _node_id, _frame, _default_pipeline):
        try:
            self._barrier.wait()
            with self._lock:
                self.ran_in_parallel = True
        except threading.BrokenBarrierError:
            pass  # nobody joined in time — recorded as "not parallel"
        with self._lock:
            self.completed += 1


async def _run_workers(queue, recorder, monkeypatch, expected: int, timeout: float = 20.0) -> None:
    """Start one worker per shard, as main.py does, and drain `expected` frames."""
    monkeypatch.setattr(state, "frame_queue", queue)
    monkeypatch.setattr(frame_loop, "process_one_frame", recorder)

    tasks = [
        asyncio.create_task(frame_loop.frame_processor_loop(_PIPELINE, shard)) for shard in range(queue.shard_count)
    ]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    try:
        while recorder.completed < expected and loop.time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
    assert recorder.completed == expected, f"workers processed {recorder.completed} of {expected} frames"


def _nodes_on_distinct_shards(queue: ShardedFrameQueue, count: int) -> list[str]:
    """`count` node ids that each hash to a different shard of `queue`."""
    picked: dict[int, str] = {}
    for i in range(2000):
        node_id = f"shard-probe-{i}"
        picked.setdefault(queue.shard_for(node_id), node_id)
        if len(picked) == count:
            break
    assert len(picked) == count, f"only found {len(picked)} distinct shards"
    return list(picked.values())


async def test_same_node_frames_are_serial_and_keep_arrival_order(monkeypatch):
    """Six frames from one node: never two at once, and never out of order."""
    queue = ShardedFrameQueue(maxsize=100, shards=4)
    node_id = "serial-node"
    for seq in range(6):
        queue.put_nowait((node_id, {"seq": seq}))

    recorder = _Recorder()
    await _run_workers(queue, recorder, monkeypatch, expected=6)

    assert recorder.same_node_overlaps == 0, "two frames from one node were in process_one_frame at the same time"
    assert recorder.peak_parallel == 1, "one node's frames must not occupy two workers"
    assert recorder.order_for(node_id) == list(range(6)), "frames must be processed in the order they were enqueued"


async def test_frames_from_different_nodes_still_run_in_parallel(monkeypatch):
    """Nodes on different shards keep the throughput sharding is there to keep."""
    queue = ShardedFrameQueue(maxsize=100, shards=4)
    node_a, node_b = _nodes_on_distinct_shards(queue, 2)
    queue.put_nowait((node_a, {"seq": 0}))
    queue.put_nowait((node_b, {"seq": 0}))

    recorder = _PairedRecorder()
    await _run_workers(queue, recorder, monkeypatch, expected=2)

    assert recorder.ran_in_parallel, "frames from two nodes must be able to run concurrently"


async def test_one_worker_is_a_plain_fifo(monkeypatch):
    """FRAME_WORKERS=1 is the old single-queue behaviour, exactly."""
    queue = ShardedFrameQueue(maxsize=100, shards=1)
    enqueued = [("alpha", 0), ("beta", 1), ("alpha", 2), ("gamma", 3)]
    for node_id, seq in enqueued:
        queue.put_nowait((node_id, {"seq": seq}))

    recorder = _Recorder(hold_s=0.01)
    await _run_workers(queue, recorder, monkeypatch, expected=len(enqueued))

    assert recorder.sequence == enqueued, "a single worker must drain the queue in strict arrival order"
    assert recorder.peak_parallel == 1


def test_queue_depth_counts_frames_parked_in_shards():
    """qsize()/maxsize keep their whole-queue meaning, so the metrics still work."""
    queue = ShardedFrameQueue(maxsize=4, shards=4)
    nodes = _nodes_on_distinct_shards(queue, 3)
    for node_id in nodes:
        queue.put_nowait((node_id, {"seq": 0}))

    assert len({queue.shard_for(n) for n in nodes}) == 3, "test needs the frames spread over shards"
    assert queue.qsize() == 3, "depth must count frames waiting in every shard, not just one"
    assert not queue.empty()
    assert not queue.full()

    queue.put_nowait((nodes[0], {"seq": 1}))
    assert queue.full(), "maxsize is the budget for the queue as a whole"
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait((nodes[1], {"seq": 2}))


def test_health_check_sees_a_backlog_spread_across_shards(monkeypatch):
    """frame_queue_saturated still fires when the backlog sits in the shards."""
    from services.health import compute_health_issues

    queue = ShardedFrameQueue(maxsize=10, shards=4)
    for i in range(10):
        queue.put_nowait((f"saturate-{i}", {"seq": i}))
    monkeypatch.setattr(state, "frame_queue", queue)

    assert any(issue["type"] == "frame_queue_saturated" for issue in compute_health_issues())


def test_drain_helpers_walk_every_shard():
    """_reset_for_tests and the tests that assert on producers drain by hand."""
    queue = ShardedFrameQueue(maxsize=10, shards=4)
    nodes = _nodes_on_distinct_shards(queue, 3)
    for node_id in nodes:
        queue.put_nowait((node_id, {"seq": 0}))

    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
        queue.task_done()

    assert sorted(node for node, _ in drained) == sorted(nodes)
    assert queue.qsize() == 0
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


def test_shard_mapping_is_stable_across_processes():
    """crc32, not the builtin hash(): PYTHONHASHSEED must not move a node."""
    queue = ShardedFrameQueue(maxsize=10, shards=6)
    for node_id in ("node-a", "tcp-unknown", "blah2-42"):
        assert queue.shard_for(node_id) == zlib.crc32(node_id.encode()) % 6
