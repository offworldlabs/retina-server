"""Frame ingress, sharded by node so one node's frames never run concurrently.

A frame worker hands each frame to `PassiveRadarPipeline.process_frame`, which
mutates a `RetinaTracker` that has no locking of its own.  While every worker
drained one shared queue, two consecutive frames from the same node could be in
`process_one_frame` on two threads at once, both mutating the same tracker: the
track list grew or shrank under `_associate`, which raised
`IndexError: index N is out of bounds` from the cost matrix (or from indexing
`self.tracks`) and dropped the frame.  The interleavings that did not raise are
worse — out-of-order Kalman predict/update pairs and missed M-of-N promotions,
silently.

The fix is structural rather than a lock: a frame is placed in shard
`crc32(node_id) % N` and each of the N frame workers drains exactly one shard.
One node therefore always maps to one shard, one shard is served by one worker,
and that worker awaits each frame's executor call before taking the next — so a
node's frames are processed one at a time, in the order they were enqueued,
while frames from nodes on other shards still run in parallel across the pool.

Sharding uses `zlib.crc32`, not the builtin `hash()`: PYTHONHASHSEED
randomisation would give every process — and every test run — a different
node-to-shard mapping, which makes a shard-related report impossible to
reproduce.

The class keeps the whole-queue `asyncio.Queue` surface (`put_nowait`,
`qsize`, `maxsize`, `full`, `empty`, `get_nowait`, `task_done`) so producers,
the queue-depth metrics and the `frame_queue_saturated` health check are
unchanged and keep counting every frame in flight, wherever it is parked.
"""

import asyncio
import zlib


class ShardedFrameQueue:
    """N FIFO shards behind one queue-shaped façade, keyed on node_id.

    `maxsize` is the budget for the queue as a whole, not per shard, so the
    backpressure producers see (`asyncio.QueueFull` → frame dropped) is the
    same as it was with a single queue of that size.
    """

    def __init__(self, maxsize: int = 0, shards: int = 1) -> None:
        self.maxsize = maxsize
        self._shards: list[asyncio.Queue] = [asyncio.Queue() for _ in range(max(1, shards))]
        self._last_get_shard: int | None = None

    # ── shard routing ────────────────────────────────────────────────────────

    @property
    def shard_count(self) -> int:
        """Number of shards, which is also the number of frame workers to run."""
        return len(self._shards)

    def shard_for(self, node_id: str) -> int:
        """Index of the shard that owns `node_id`. Stable across processes."""
        return zlib.crc32(str(node_id).encode()) % len(self._shards)

    def shard(self, index: int) -> asyncio.Queue:
        """The queue one frame worker owns; workers await get() on it directly."""
        return self._shards[index]

    # ── whole-queue view (metrics, health, tests) ────────────────────────────

    def qsize(self) -> int:
        """Frames waiting in every shard, i.e. the real backlog."""
        return sum(q.qsize() for q in self._shards)

    def empty(self) -> bool:
        return self.qsize() == 0

    def full(self) -> bool:
        return self.maxsize > 0 and self.qsize() >= self.maxsize

    def put_nowait(self, item: tuple[str, dict]) -> None:
        """Queue one (node_id, frame). Raises asyncio.QueueFull at maxsize."""
        if self.full():
            raise asyncio.QueueFull
        self._shards[self.shard_for(item[0])].put_nowait(item)

    def get_nowait(self) -> tuple[str, dict]:
        """Take one frame from the first non-empty shard.

        Only the drain paths use this — `_reset_for_tests` and tests asserting
        on what a producer queued.  Frame workers read their own shard, which
        is what preserves per-node order.
        """
        for index, q in enumerate(self._shards):
            if not q.empty():
                self._last_get_shard = index
                return q.get_nowait()
        raise asyncio.QueueEmpty

    def task_done(self) -> None:
        """Mark the frame from the last get_nowait() done, for callers that pair
        the two.  Workers call task_done() on their own shard instead."""
        if self._last_get_shard is not None:
            self._shards[self._last_get_shard].task_done()
            self._last_get_shard = None
