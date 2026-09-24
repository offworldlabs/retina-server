"""The server clock behind node availability: what it marks, and from when."""

import asyncio
import time

from retina_analytics.availability import MinuteRing, minute_of

from core import state
from services.tasks import periodic


async def _marked_after_running_briefly(monkeypatch, grace_s):
    monkeypatch.setattr(periodic, "SERVER_CLOCK_GRACE_S", grace_s)
    monkeypatch.setattr(state.node_analytics, "server_minutes", MinuteRing())
    task = asyncio.create_task(periodic.server_clock_task())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    now = minute_of(time.time())
    return state.node_analytics.server_minutes.bits(now - 1, now)


async def test_the_server_clock_marks_the_minute_it_is_up_in(monkeypatch):
    assert await _marked_after_running_briefly(monkeypatch, grace_s=0)


async def test_the_server_clock_marks_nothing_while_nodes_reconnect(monkeypatch):
    assert not await _marked_after_running_briefly(monkeypatch, grace_s=60)
