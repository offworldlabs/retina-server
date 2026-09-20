"""Hourly archive work must remain healthy between scheduled flushes."""

import pytest

from config.constants import ARCHIVE_FLUSH_INTERVAL_S
from core import state, task_registry


@pytest.mark.parametrize(
    "age_s, stale", [(ARCHIVE_FLUSH_INTERVAL_S / 2, False), (2 * ARCHIVE_FLUSH_INTERVAL_S + 1, True)]
)
def test_archive_health_tracks_actual_flush_cadence(monkeypatch, age_s, stale):
    now = 100000.0
    monkeypatch.setattr(task_registry.time, "time", lambda: now)
    monkeypatch.setattr(state, "task_last_success", {"archive_flush": now - age_s})
    assert ("archive_flush" in task_registry.get_stale_tasks()) is stale
