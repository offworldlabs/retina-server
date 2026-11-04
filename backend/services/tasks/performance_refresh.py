"""Background task: start the daily node performance job.

At PERFORMANCE_JOB_TIME_UTC this runs ``python -m scripts.node_performance --daily``
as a child process, because reading a day of archive in the app process would hold
the GIL against the frame threads.  The child does the work, at a lower priority,
and writes files; the app only waits for its exit code and then reads those files
into services/performance_store, whatever became of the run, since a killed child
may still have finished some nodes.  A failed run is tried again after
PERFORMANCE_RETRY_S.  It runs only while PERFORMANCE_JOB_ENABLED.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config.constants import PERFORMANCE_LAST_RUN_FILE, PERFORMANCE_RETRY_S, performance_settings
from core import state
from services import performance_store

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_TASK = "performance_refresh"


def _now() -> datetime:
    return datetime.now(UTC)


def _child_command() -> list[str]:
    return ["nice", "-n", "10", sys.executable, "-m", "scripts.node_performance", "--daily"]


def _child_env() -> dict[str, str]:
    # Arrow's own pool keeps what it frees, a third of a day's peak; it reads this as pyarrow loads.
    return {**os.environ, "ARROW_DEFAULT_MEMORY_POOL": "system"}


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and reap it.  It may already have exited."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def run_refresh(timeout_s: float | None = None) -> bool:
    """Run the child to its end.  True when it exited 0.

    ``timeout_s`` defaults to PERFORMANCE_JOB_TIMEOUT_S.  The child's own log lines
    go to this process's output.  A timeout or a shutdown kills it, so no child
    outlives the app, and what it had not swapped in yet is simply not there.
    """
    timeout = performance_settings().timeout_s if timeout_s is None else timeout_s
    proc = await asyncio.create_subprocess_exec(*_child_command(), cwd=_BACKEND_DIR, env=_child_env())
    try:
        code = await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        await _stop(proc)
        logger.warning("node performance run timed out after %ss and was stopped", timeout)
        return False
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    if code != 0:
        logger.warning("node performance run exited with code %s, the last good files stand", code)
        return False
    return True


def _last_finished() -> datetime | None:
    """When the last run that reached its marker ended; None when there is none, or it can't be told."""
    try:
        with open(performance_store.DATA_DIR / PERFORMANCE_LAST_RUN_FILE, encoding="utf-8") as f:
            finished = datetime.fromisoformat(json.load(f)["finished_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return finished if finished.tzinfo is not None else None


def _seconds_until_due() -> float:
    """0 when no run has finished since the last slot, else the seconds to the next one."""
    now = _now()
    slot = datetime.combine(now.date(), performance_settings().time_utc, tzinfo=UTC)
    if slot > now:
        slot -= timedelta(days=1)
    finished = _last_finished()
    if finished is None or finished < slot:
        return 0.0
    return (slot + timedelta(days=1) - now).total_seconds()


async def performance_refresh_task():
    """Run the job when a slot has gone by without one, retry after a failure.

    The first check waits PERFORMANCE_JOB_START_DELAY_S, so a boot is not loaded by
    the child and a server that was down at its slot catches up after it.  The
    marker decides, so a restart after a good run starts nothing.
    """
    settings = performance_settings()
    if not settings.enabled:
        return
    await asyncio.sleep(settings.start_delay_s)
    while True:
        due = _seconds_until_due()
        if due > 0:
            state.task_last_success[_TASK] = time.time()
            await asyncio.sleep(due)
            continue
        try:
            ok = await run_refresh()
        except Exception:
            logger.exception("node performance run could not start")
            ok = False
        await asyncio.to_thread(performance_store.reload)
        # A run that ended well left a marker past the slot and sleeps to the next one; anything else waits the retry.
        if not ok or _seconds_until_due() == 0:
            state.bump_task_error(_TASK)
            await asyncio.sleep(PERFORMANCE_RETRY_S)
