"""Background task: keep each real node's tower match on its active config row.

Every TOWER_MATCH_INTERVAL_S a pass asks tower-finder's lookup by callsign
(services/tower_match.py) about each active row that names its tower in full and
has no match yet, or one older than TOWER_MATCH_MAX_AGE_S, and stores the answer on
the row.  With TOWER_MATCH_ENABLED off the task returns at once, before any client
or query.

A lookup is a blocking request that can take minutes, so it runs in the task's one
worker thread and the event loop never waits on it.  No SQLite transaction stays
open across a request either: the due rows are read in one short session that
closes before the first lookup, and each answer is written and committed in a
session of its own.

A failed lookup writes nothing, so the row keeps what it had and stays due, and the
next pass asks again.  Three failures in a row end the pass: tower-finder is then
taken to be down.  The rows go in a fresh random order each pass, so rows that keep
failing can't hold the rest back.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.constants import (
    TOWER_MATCH_INTERVAL_S,
    TOWER_MATCH_MAX_AGE_S,
    TOWER_MATCH_START_DELAY_S,
    tower_match_settings,
)
from core import state
from core.nodes import NodeConfig
from core.users import async_session_maker
from services import tower_match
from services.node_config_store import TOWER_MATCH_COLUMNS, set_tower_match
from services.node_kind import is_synthetic_node
from services.tasks.executor import task_executor

logger = logging.getLogger(__name__)

_TASK = "tower_match_refresh"
# What the lookup asks by: a row missing any of them names no tower to ask about.
_LOOKUP_INPUTS = ("tx_callsign", "fc_hz", "tx_lat", "tx_lon")
_MAX_CONSECUTIVE_FAILURES = 3
# tower-finder answers 200 without a state the FCC didn't answer, so a not_found
# for a row holding one of these may be a gap rather than a lost licence.
_KEPT_ON_NOT_FOUND = ("matched", "frequency_mismatch")


class _DueRow(NamedTuple):
    node_id: str
    version: int
    config: dict[str, Any]  # The lookup's inputs
    match: dict[str, Any]  # The stored match, keyed as a lookup's record


async def _due_rows(session_maker: async_sessionmaker[AsyncSession], now: datetime) -> list[_DueRow]:
    """The rows a pass asks about, read in a session that is closed when this returns."""
    refresh_before = now - timedelta(seconds=TOWER_MATCH_MAX_AGE_S)
    query = select(NodeConfig).where(
        NodeConfig.superseded_at.is_(None),
        *(getattr(NodeConfig, field).is_not(None) for field in _LOOKUP_INPUTS),
        or_(
            NodeConfig.tower_match_status.is_(None),
            NodeConfig.tower_matched_at.is_(None),
            NodeConfig.tower_matched_at < refresh_before,
        ),
    )
    async with session_maker() as session:
        return [
            _DueRow(
                row.node_id,
                row.version,
                {field: getattr(row, field) for field in _LOOKUP_INPUTS},
                {key: getattr(row, column) for key, column in TOWER_MATCH_COLUMNS.items()},
            )
            for row in await session.scalars(query)
            if not is_synthetic_node(row.node_id)
        ]


async def run_pass(
    run: Callable[..., Awaitable[Any]],
    session_maker: async_sessionmaker[AsyncSession],
    client: httpx.Client,
    base_url: str,
    now: datetime,
) -> bool:
    """Ask about each due row in turn and store its answer; False when failed lookups ended the pass early.

    ``run`` awaits a blocking call made off the event loop, as task_executor's
    runner does.  Every row written is stamped ``now``.  An answer that comes out as
    stored moves only ``tower_matched_at``.
    """
    due = await _due_rows(session_maker, now)
    # A fixed order would let rows that always fail, at its head, end every pass before the rest are asked.
    secrets.SystemRandom().shuffle(due)
    failures = 0
    for index, row in enumerate(due):
        try:
            record = await run(tower_match.resolve, client, base_url, row.config)
        except tower_match.LookupFailed as exc:
            failures += 1
            logger.warning("tower match refresh: lookup for %s failed: %s", row.node_id, exc)
            if failures == _MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "tower match refresh: %d lookups failed in a row, %d rows wait for the next pass",
                    failures,
                    len(due) - index - 1,
                )
                return False
            continue
        failures = 0
        if record["status"] == "not_found" and row.match["status"] in _KEPT_ON_NOT_FOUND:
            logger.warning("tower match refresh: %s's tower is missing from an answer, keeping its match", row.node_id)
            record = row.match
        async with session_maker() as session:
            changed = await set_tower_match(session, row.node_id, row.version, record, now)
            await session.commit()
        if changed:
            logger.info("tower match refresh: %s version %d is now %s", row.node_id, row.version, record["status"])
    return True


async def tower_match_refresh_task() -> None:
    """A pass TOWER_MATCH_START_DELAY_S after boot and every TOWER_MATCH_INTERVAL_S after it; never raises out of the loop."""
    settings = tower_match_settings()
    if not settings.enabled:
        return
    await asyncio.sleep(TOWER_MATCH_START_DELAY_S)
    async with task_executor("tower-match-refresh") as run:
        while True:
            try:
                # In the worker thread too: building a client loads the CA bundle from disk.
                client = await run(tower_match.http_client)
                try:
                    ok = await run_pass(run, async_session_maker, client, settings.tower_finder_url, datetime.now(UTC))
                finally:
                    client.close()
            except Exception:
                logger.exception("tower match refresh failed")
                ok = False
            if ok:
                state.task_last_success[_TASK] = time.time()
            else:
                state.bump_task_error(_TASK)
            await asyncio.sleep(TOWER_MATCH_INTERVAL_S)
