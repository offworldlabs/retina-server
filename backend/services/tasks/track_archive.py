"""Background task: drain state.track_archive_buffer into Parquet.

The multi-node solver appends successful solves to ``state.track_archive_buffer``
(see services.tasks.solver._process_solver_item). This task wakes every
``TRACK_ARCHIVE_FLUSH_INTERVAL_S`` seconds, snapshots the buffer, and writes
it as a single Parquet file under ``coverage_data/tracks/``.

The same lifecycle that uploads detection archives to R2 (archive_lifecycle)
will also pick up these track files because the lifecycle iterator globs
``*.parquet`` regardless of the parent directory tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

try:
    from config.constants import TRACK_ARCHIVE_FLUSH_INTERVAL_S
except ImportError:  # pragma: no cover — stale volume without this constant
    TRACK_ARCHIVE_FLUSH_INTERVAL_S = 60
from core import state
from services.track_writer import write_tracks_parquet

logger = logging.getLogger(__name__)

_TRACKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "coverage_data",
    "tracks",
)

# At most one failed batch is held outside the producer's bounded deque. New
# records keep the deque's existing newest-records policy during a disk outage;
# retries cannot grow this batch or silently discard the batch that failed.
_pending_records: list[dict] = []
_flush_lock = threading.Lock()


def flush_track_archive_buffer() -> str | None:
    """Write one batch, retrying a failed batch before accepting new records.

    Raises on failure so task health does not report a successful flush. The
    batch stays pending until a write succeeds. Memory is bounded by one deque
    plus one batch of at most the deque's capacity (currently 10,000 each).
    """
    with _flush_lock:
        if not _pending_records:
            # Take only the records present at the start; producers can append
            # during the drain without extending this flush indefinitely.
            for _ in range(len(state.track_archive_buffer)):
                try:
                    _pending_records.append(state.track_archive_buffer.popleft())
                except IndexError:
                    break
        if not _pending_records:
            return None
        key = write_tracks_parquet(records=_pending_records, base_dir=_TRACKS_DIR)
        _pending_records.clear()
        return key


async def track_flush_task():
    """Periodic flush loop. Runs forever as a background asyncio task."""
    while True:
        await asyncio.sleep(TRACK_ARCHIVE_FLUSH_INTERVAL_S)
        try:
            key = flush_track_archive_buffer()
            if key:
                logger.debug("track archive flushed: %s", key)
            state.task_last_success["track_archive_flush"] = time.time()
        except Exception:
            state.bump_task_error("track_archive_flush")
            logger.exception("track_flush_task iteration failed")
