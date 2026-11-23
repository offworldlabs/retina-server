"""Background task: keep the aircraft type database fresh.

Once AIRCRAFT_DB_REFRESH_INTERVAL_S has passed since the file was written, or
when the file won't load, this runs ``python -m services.aircraft_db`` as a child
process.  Parsing the two sources in the app process would hold the GIL against
the frame threads, so the child does the download and the build, at a lower
priority, and the app only waits for its exit code.  A failed or killed run leaves
the last good file in service and is retried after AIRCRAFT_DB_RETRY_S.  It runs
only while AIRCRAFT_DB_ENABLED.
"""

import asyncio
import contextlib
import logging
import shutil
import sys
import time
from pathlib import Path

from config.constants import AIRCRAFT_DB_REFRESH_INTERVAL_S, AIRCRAFT_DB_RETRY_S, aircraft_db_settings
from core import state
from services import aircraft_db

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_TASK = "aircraft_db_refresh"


def _child_command() -> list[str]:
    settings = aircraft_db_settings()
    return [
        "nice",
        "-n",
        "10",
        sys.executable,
        "-m",
        "services.aircraft_db",
        "--mictronics-url",
        settings.mictronics_url,
        "--faa-url",
        settings.faa_url,
        "--faa-user-agent",
        settings.faa_user_agent,
        "--out",
        str(aircraft_db.DB_DIR / aircraft_db.FILE_NAME),
    ]


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and reap it.  It may already have exited."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


def _remove_download() -> None:
    """Delete what a killed child left behind: the FAA file holds owner names."""
    shutil.rmtree(aircraft_db.DB_DIR / aircraft_db.WORK_DIR_NAME, ignore_errors=True)


async def run_refresh(timeout_s: float | None = None) -> bool:
    """Run the child to its end.  True when it exited 0.

    ``timeout_s`` defaults to AIRCRAFT_DB_TIMEOUT_S.  The child's own log lines go
    to this process's output.  A timeout or a shutdown kills it, so no child
    outlives the app, and its download goes as soon as it has stopped.
    """
    timeout = aircraft_db_settings().timeout_s if timeout_s is None else timeout_s
    proc = await asyncio.create_subprocess_exec(*_child_command(), cwd=_BACKEND_DIR)
    try:
        code = await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        await _stop(proc)
        logger.warning("aircraft database refresh timed out after %ss and was stopped", timeout)
        return False
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    finally:
        _remove_download()
    if code != 0:
        logger.warning("aircraft database refresh exited with code %s, the last good file stands", code)
        return False
    return True


def _seconds_until_due() -> float:
    """Seconds until the file is old enough to refresh; 0 when there is none or it won't load."""
    path = aircraft_db.DB_DIR / aircraft_db.FILE_NAME
    try:
        age = time.time() - path.stat().st_mtime
        aircraft_db.load(path)
    except (OSError, MemoryError, aircraft_db.AircraftDbError):
        return 0.0
    return max(0.0, AIRCRAFT_DB_REFRESH_INTERVAL_S - age)


async def aircraft_db_refresh_task():
    """Refresh the file when it is due, retry after a failure.

    A download left by a server that died with its child goes first, even with
    the task switched off.  The first check waits AIRCRAFT_DB_START_DELAY_S, so a
    boot is not loaded by the child.  The file's age decides, so a restart with a
    fresh file that loads downloads nothing.
    """
    _remove_download()
    settings = aircraft_db_settings()
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
            logger.exception("aircraft database refresh could not run")
            ok = False
        # Every run ends on a good fresh file, which sleeps until due, or waits the retry.
        if not ok or _seconds_until_due() == 0:
            state.bump_task_error(_TASK)
            await asyncio.sleep(AIRCRAFT_DB_RETRY_S)
