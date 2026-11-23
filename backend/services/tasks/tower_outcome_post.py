"""Background task: post the week's archive rows to tower-finder (services/tower_outcome.py).

A week is due from its TOWER_OUTCOME_DUE_WEEKDAY at TOWER_OUTCOME_DUE_TIME_UTC (Monday
04:00 UTC) until TOWER_OUTCOME_STATE_FILE names it, so a server that was down at the
slot posts when it comes back within that ISO week.  A week missed whole is not posted
late: the next week's files hold their own trailing window.  A failed run is tried
again after TOWER_OUTCOME_RETRY_S.  So is a run with no node to post, which records
nothing, as an empty store at boot must not use up the week.  With
TOWER_FINDER_FEEDBACK_TOKEN unset or blank the task returns at once, before any client,
request or read.

The state file also keeps, under ``windows``, each node's ``observed_at`` as last
posted, written only once a run's every batch was taken.  A node whose window doesn't
end after its own is not posted again, so a quiet node's last window counts once.  A
file without ``windows``, or an entry that doesn't read as a moment, holds none.

Each run is synchronous, in the task's one worker thread: the privacy policy, the
configurations and the published positions are sync database reads, and the post
uses a sync client, so none of it holds the event loop.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from config.constants import (
    TOWER_OUTCOME_DUE_TIME_UTC,
    TOWER_OUTCOME_DUE_WEEKDAY,
    TOWER_OUTCOME_RETRY_S,
    TOWER_OUTCOME_STATE_FILE,
    TowerOutcomeSettings,
    tower_outcome_settings,
)
from core import state
from core.runtime_config import write_runtime_file
from services import performance_store, tower_outcome
from services.tasks.executor import task_executor

logger = logging.getLogger(__name__)

_TASK = "tower_outcome_post"


def _now() -> datetime:
    return datetime.now(UTC)


def _state_path() -> Path:
    return performance_store.DATA_DIR / TOWER_OUTCOME_STATE_FILE


def _recorded() -> dict:
    """The state file; empty when there is none or it can't be read, which posts again."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _windows(recorded: dict) -> dict[str, datetime]:
    """Each node's last posted window end; an entry that isn't an aware ISO moment is left out."""
    windows = recorded.get("windows")
    out = {}
    for node_id, end in windows.items() if isinstance(windows, dict) else ():
        try:
            moment = datetime.fromisoformat(end)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is not None:
            out[node_id] = moment
    return out


def _record(week: str, at: datetime, windows: dict[str, str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_runtime_file(path, json.dumps({"week": week, "at": at.isoformat(), "windows": windows}) + "\n")


def _slot(moment: datetime) -> datetime:
    """This ISO week's due time."""
    day = moment.date() + timedelta(days=TOWER_OUTCOME_DUE_WEEKDAY - moment.weekday())
    return datetime.combine(day, TOWER_OUTCOME_DUE_TIME_UTC, tzinfo=UTC)


def run_once(settings: TowerOutcomeSettings, now: Callable[[], datetime], client: httpx.Client | None = None) -> float:
    """Post the week when it is due; the seconds to wait before the next check.

    Raises when the week was due and not posted.  The wait is never longer than the
    retry interval, so a clock that jumps or a state file that goes is noticed.
    """
    moment = now()
    week = tower_outcome.week_label(moment.date())
    slot = _slot(moment)
    recorded = _recorded()
    if moment < slot or recorded.get("week") == week:
        upcoming = slot if moment < slot else slot + timedelta(days=7)
        return min((upcoming - moment).total_seconds(), TOWER_OUTCOME_RETRY_S)
    windows = _windows(recorded)
    rows = tower_outcome.build_rows(moment.date(), windows)
    if not rows:
        logger.info("tower outcome: no node to post for %s yet", week)
        return TOWER_OUTCOME_RETRY_S
    tower_outcome.post(rows, url=settings.tower_finder_url, token=settings.token, client=client)
    posted = {node_id: end.isoformat() for node_id, end in windows.items()}
    _record(week, now(), posted | {row["node_id"]: row["observed_at"] for row in rows})
    logger.info("tower outcome: posted %d node(s) for %s", len(rows), week)
    return TOWER_OUTCOME_RETRY_S


async def tower_outcome_post_task(*, client: httpx.Client | None = None, now: Callable[[], datetime] = _now) -> None:
    """Check once per wait, post when due, retry after a failure; never raises out of the loop."""
    settings = tower_outcome_settings()
    if not settings.token:
        return
    async with task_executor("tower-outcome-post") as run:
        while True:
            try:
                wait_s = await run(run_once, settings, now, client)
                state.task_last_success[_TASK] = time.time()
            except Exception:
                logger.exception("tower outcome post failed")
                state.bump_task_error(_TASK)
                wait_s = TOWER_OUTCOME_RETRY_S
            await asyncio.sleep(wait_s)
