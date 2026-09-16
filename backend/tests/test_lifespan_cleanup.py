"""The lifespan owns its tasks and workers even when the application raises."""

import asyncio
import queue
import threading
from unittest.mock import AsyncMock, Mock

import pytest

import main
from services.tasks import solver, track_archive


@pytest.mark.parametrize("exceptional", [False, True])
@pytest.mark.parametrize("unfinished", [None, "solver", "executor"])
@pytest.mark.asyncio
async def test_cleanup_awaits_tasks_before_final_writes(monkeypatch, exceptional, unfinished):
    events = []
    active = set()

    async def parked(*_args):
        active.add(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    for name in (
        "reputation_evaluator",
        "prune_synthetic_nodes",
        "adsb_truth_fetcher",
        "aircraft_flush_task",
        "feed_gc_task",
        "archive_flush_task",
        "track_flush_task",
        "archive_lifecycle_task",
        "users_backup_task",
        "analytics_refresh_task",
        "coverage_constraints_task",
        "storage_refresh_task",
        "health_monitor_task",
        "heartbeat_task",
        "frame_processor_loop",
    ):
        monkeypatch.setattr(main, name, parked)
    monkeypatch.setattr(main.detection_mirror, "mirror_task", parked)
    monkeypatch.setattr(main, "restore_snapshot", lambda: {})
    monkeypatch.setattr(main, "save_snapshot", lambda: events.append("snapshot"))
    monkeypatch.setattr(main.state.node_analytics, "save_coverage_maps", lambda: None)
    if unfinished == "solver":
        monkeypatch.setattr(main, "stop_solver_workers", lambda: False)
    if unfinished == "executor":
        monkeypatch.setattr(main, "unfinished_task_executors", lambda: ["stuck-frame"] if active else [])
    writes = iter(["pending.parquet", "current.parquet", None])

    def flush():
        events.append("track_flush")
        return next(writes)

    monkeypatch.setattr(track_archive, "flush_track_archive_buffer", flush)
    connection = None
    ports = []
    callbacks = []
    start_server = asyncio.start_server

    async def capture_server(*args, **kwargs):
        callbacks.append(args[0])
        server = await start_server(*args, **kwargs)
        ports.append(server.sockets[0].getsockname()[1])
        return server

    monkeypatch.setattr(asyncio, "start_server", capture_server)

    async def tcp_client(reader, writer):
        try:
            await parked()
        finally:
            writer.close()

    monkeypatch.setattr(main, "handle_tcp_client", tcp_client)
    try:
        try:
            async with asyncio.timeout(2):
                async with main.lifespan(main.app):
                    _, connection = await asyncio.open_connection("127.0.0.1", ports[0])
                    await asyncio.sleep(0)
                    if exceptional:
                        raise ValueError("application failure")
        except ValueError:
            if not exceptional:
                raise
        assert active
        assert all(t.done() for t in active)
        assert events.count("cancelled") == len(active)
        if unfinished:
            assert "snapshot" not in events
            assert "track_flush" not in events
        else:
            assert events.index("snapshot") >= len(active)
            assert events.count("track_flush") == 3
            assert not [t for t in threading.enumerate() if t.name.startswith("solver-")]
            # A connection callback queued before listener closure may run
            # after shutdown has already taken its connection snapshot.
            late_writer = Mock()
            callbacks[0](None, late_writer)
            await asyncio.sleep(0)
            assert late_writer.close.called
    finally:
        if connection is not None:
            connection.close()
            await connection.wait_closed()
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        solver.stop_solver_workers(timeout=2)


@pytest.mark.asyncio
async def test_partial_startup_still_stops_workers(monkeypatch):
    def fail_after_start():
        solver.start_solver_workers()
        raise RuntimeError("startup failure")

    monkeypatch.setattr(main, "start_solver_workers", fail_after_start)
    monkeypatch.setattr(main, "restore_snapshot", lambda: {})
    monkeypatch.setattr(main, "save_snapshot", lambda: None)
    monkeypatch.setattr(main.state.node_analytics, "save_coverage_maps", lambda: None)
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            async with main.lifespan(main.app):
                pytest.fail("Startup should fail before yielding")
        assert not [t for t in threading.enumerate() if t.name.startswith("solver-")]
    finally:
        solver.stop_solver_workers(timeout=2)


@pytest.mark.asyncio
async def test_restart_refuses_unfinished_background_work_before_restoring_state(monkeypatch):
    monkeypatch.setattr(main, "unfinished_task_executors", lambda: ["stuck-frame"])
    restored = []
    monkeypatch.setattr(main, "restore_snapshot", lambda: restored.append(True))
    with pytest.raises(RuntimeError, match="still running"):
        async with main.lifespan(main.app):
            pass
    assert restored == []


@pytest.mark.asyncio
async def test_restart_refuses_stopping_solver_before_restoring_or_priming_state(monkeypatch):
    from services import node_pipeline

    entered = threading.Event()
    release = threading.Event()
    restored = []
    prime = AsyncMock()
    monkeypatch.setattr(main, "restore_snapshot", lambda: restored.append(True))
    monkeypatch.setattr(node_pipeline, "prime_pipeline_at_startup", prime)
    monkeypatch.setattr(solver, "_N_SOLVER_WORKERS", 1)
    monkeypatch.setattr(main.state, "solver_queue", queue.Queue())

    def work(*_args):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(solver, "_process_solver_item", work)
    main.state.solver_queue.put(({}, {}, 0))
    solver.start_solver_workers()
    try:
        assert entered.wait(2)
        assert not solver.stop_solver_workers(timeout=0.01)
        with pytest.raises(RuntimeError, match="still stopping"):
            async with main.lifespan(main.app):
                pytest.fail("Must not restart over a live solver generation")
        assert restored == []
        prime.assert_not_awaited()
    finally:
        release.set()
        solver.stop_solver_workers(timeout=2)
