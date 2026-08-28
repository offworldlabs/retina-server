"""Shared task staleness registry.

Single source of truth for expected task intervals — and, since two routers had
grown byte-identical copies of the check, for the staleness rule itself.
"""

import time

# Task name → expected success interval in seconds.
# A task is considered stale if it hasn't reported success within 2× this value.
TASK_EXPECTED_INTERVAL_S: dict[str, int] = {
    "frame_processor": 10,
    "analytics_refresh": 60,
    "aircraft_flush": 5,
    "archive_flush": 120,
    "archive_lifecycle": 3600,
    "reputation_evaluator": 120,
    "prune_synthetic_nodes": 21600,  # Every 6 hours
    # A cycle is 120 s of sleep, plus 300 s more after an OpenSky 429 that cost
    # a region its coverage (86cb9m6wc), plus the fetch itself.  OpenSky is
    # asked once per box and a region straddling the antimeridian is two boxes,
    # so the 8-region cap is at worst 16 requests; four go out at a time, so
    # that phase is four 15 s timeouts deep = 60 s.  The fallback then costs
    # 7 x 5 s of request spacing = 35 s, plus per area whichever of its two
    # alternative paths is dearer: a 429 (returns promptly, then 10 s backoff +
    # 8 s retry timeout = 18 s) or a timeout (8 s, no retry).  Taking the
    # larger, 8 x 18 = 144 s.  So 60 + 35 + 144 = 239 s of fetch, 659 s all
    # told.  Alerting at 2x leaves room above that.
    "adsb_truth_fetcher": 400,
    "solver": 120,
    "storage_refresh": 720,  # expected every 300 s; alert if >2× late
    "track_archive_flush": 180,  # flush every 60 s; alert if >3× late
    "users_db_backup": 86400 * 2,  # daily; alert if it hasn't run in 2 days
    # The blah2 bridge registers one key per configured live node at startup
    # (see services/blah2_bridge.load_nodes) — its node list is config-driven,
    # so those keys cannot be enumerated here.
}


def register_task(name: str, expected_interval_s: int) -> None:
    """Add a dynamically-discovered task to the staleness registry.

    For tasks whose number is not known until config is read. Idempotent, so
    re-reading a config file does not disturb an already-registered task.
    """
    TASK_EXPECTED_INTERVAL_S.setdefault(name, expected_interval_s)


def get_stale_tasks() -> list[str]:
    """Tasks that have not reported success within 2x their expected interval.

    A task with no recorded success has not started yet and is not stale.
    """
    from core import state

    now = time.time()
    return [
        name
        for name, expected_s in TASK_EXPECTED_INTERVAL_S.items()
        if (last := state.task_last_success.get(name)) is not None and (now - last) > expected_s * 2
    ]
