"""Frame processor loop — one worker drains one shard of state.frame_queue."""

import asyncio
import logging
import time

from core import state
from services.frame_processor import process_one_frame


async def frame_processor_loop(default_pipeline, shard: int = 0):
    """Process queued detection frames for one shard, one frame at a time.

    Every node maps to exactly one shard (core.frame_queue) and every shard is
    drained by exactly one worker, which awaits the executor call before taking
    the next frame.  So one node's frames are processed serially, in arrival
    order, on whatever thread the pool gives them — the tracker they mutate is
    never touched by two of them at once — while other shards keep running in
    parallel, which is where the throughput comes from.

    state.frame_queue is re-read each iteration so a test that swaps the queue
    mid-flight still works.
    """
    loop = asyncio.get_event_loop()
    while True:
        queue = state.frame_queue.shard(shard)
        node_id, frame = await queue.get()
        try:
            await loop.run_in_executor(
                None,
                process_one_frame,
                node_id,
                frame,
                default_pipeline,
            )
            state.aircraft_dirty = True
            state.bump_counter("frames_processed")
            state.task_last_success["frame_processor"] = time.time()
        except Exception:
            state.task_error_counts["frame_processor"] += 1
            logging.exception("Frame processing failed")
        finally:
            queue.task_done()
        await asyncio.sleep(0)
