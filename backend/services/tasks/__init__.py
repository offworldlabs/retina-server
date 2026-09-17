"""Background async tasks, one sub-module per task.

The task entry points are re-exported here so schedulers can name them
without knowing which sub-module each lives in::

    from services.tasks import frame_processor_loop, start_solver_workers

Anything else a sub-module exposes is imported from that sub-module directly.
"""

from services.tasks.aircraft_flush import aircraft_flush_task
from services.tasks.analytics_refresh import analytics_refresh_task, coverage_constraints_task
from services.tasks.feed_gc import feed_gc_task
from services.tasks.frame_loop import frame_processor_loop
from services.tasks.health_monitor import health_monitor_task
from services.tasks.heartbeat import heartbeat_task
from services.tasks.periodic import (
    adsb_truth_fetcher,
    archive_flush_task,
    archive_lifecycle_task,
    prune_synthetic_nodes,
    reputation_evaluator,
)
from services.tasks.solver import start_solver_workers, stop_solver_workers
from services.tasks.storage_refresh import storage_refresh_task
from services.tasks.track_archive import track_flush_task
from services.tasks.users_backup import users_backup_task

__all__ = [
    "analytics_refresh_task",
    "coverage_constraints_task",
    "start_solver_workers",
    "stop_solver_workers",
    "frame_processor_loop",
    "health_monitor_task",
    "heartbeat_task",
    "aircraft_flush_task",
    "feed_gc_task",
    "storage_refresh_task",
    "archive_flush_task",
    "archive_lifecycle_task",
    "reputation_evaluator",
    "prune_synthetic_nodes",
    "adsb_truth_fetcher",
    "track_flush_task",
    "users_backup_task",
]
