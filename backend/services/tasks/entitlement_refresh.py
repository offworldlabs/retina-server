"""Re-evaluate contributor entitlements every ENTITLEMENT_REFRESH_INTERVAL_S.

The rule counts whole UTC days, so its answer moves once a day; an hourly run,
the default, opens, ends and lapses rows within the hour of the change.
services/entitlements.py, evaluate_contributors, is the rule, and reads the
interval at every call.

The first run comes FIRST_RUN_DELAY_S after the task starts, or one interval
when that is shorter, rather than a whole interval: a server restarted more
often than the interval still evaluates, a boot is not slowed by it, and a test
client that lives under a minute never runs it. Before that first evaluation,
services/activity_backfill.py fills the activity record from the detection
archive once, if it holds no day before today, so the launch day's evaluation
already counts the contributors' earlier days. It is judged as of the task's
start: every mark this process writes is dated that day or later, so none of
them can make the record look filled.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

from core import state
from services import activity_backfill, entitlements

FIRST_RUN_DELAY_S = 60.0


async def entitlement_refresh_task() -> None:
    started = datetime.now(UTC)
    await asyncio.sleep(min(FIRST_RUN_DELAY_S, entitlements.refresh_interval_s()))
    await activity_backfill.backfill_if_empty(started)
    while True:
        try:
            await entitlements.evaluate_contributors()
            state.task_last_success["entitlement_refresh"] = time.time()
        except Exception:
            state.bump_task_error("entitlement_refresh")
            logging.exception("Entitlement refresh failed")
        await asyncio.sleep(entitlements.refresh_interval_s())
