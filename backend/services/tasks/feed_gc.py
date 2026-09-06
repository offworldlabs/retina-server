"""Feed-store garbage collection on its own timer.

Every store the aircraft feed accumulates used to be pruned inside
``build_combined_aircraft_json``, i.e. once per successful flush iteration.
That tied GC to two things it has nothing to do with:

* the websocket broadcast the flush task awaits before looping — one wedged
  client used to serialise the whole flush behind it, and
* ``state.aircraft_dirty``, which short-circuits the build entirely when no
  frame has arrived since the last flush.

Neither stops the writers.  Frame workers keep filling ``adsb_aircraft``,
``track_histories`` and friends, and solver threads keep filling
``multinode_tracks`` at full rate, so a stalled or skipped flush turned a
client-side problem into server-side unbounded growth.  This task runs the same
two pruners on a fixed 5 s cadence instead, and reports to the task-health
registry so a GC that stops is visible in /api/admin/metrics stale_tasks.
"""

import asyncio
import logging
import time

from core import state
from services.feed_gc import prune_multinode_tracks, prune_stale_stores

# Matches the old effective cadence: the flush task ran at
# AIRCRAFT_FLUSH_INTERVAL_S (1 s) and TASK_EXPECTED_INTERVAL_S already allowed
# aircraft_flush 5 s.  Nothing pruned here has a deadline tighter than the 30 s
# dark multinode expiry, so 5 s is frequent enough to bound the stores and rare
# enough that the pass is invisible against the 1 Hz feed build.
FEED_GC_INTERVAL_S = 5.0


def run_feed_gc(now: float | None = None) -> None:
    """One GC pass.  Separate from the loop so tests can run the body."""
    if now is None:
        now = time.time()
    prune_stale_stores(now)
    prune_multinode_tracks(now)


async def feed_gc_task():
    """Prune the feed's stale stores every FEED_GC_INTERVAL_S."""
    while True:
        await asyncio.sleep(FEED_GC_INTERVAL_S)
        try:
            run_feed_gc()
            state.task_last_success["feed_gc"] = time.time()
        except Exception:
            state.bump_task_error("feed_gc")
            logging.exception("Feed GC failed")
