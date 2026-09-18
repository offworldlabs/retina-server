"""Worker ownership across application starts and stops."""

import queue
import threading
import time

import pytest

from core import state
from services.tasks import solver, solver_pool


def test_start_is_idempotent_and_stop_joins_workers(monkeypatch):
    monkeypatch.setattr(solver_pool, "_POOL_ENABLED", False)
    monkeypatch.setattr(state, "solver_queue", queue.Queue())
    solver.start_solver_workers()
    first = [t for t in threading.enumerate() if t.name.startswith("solver-")]
    try:
        solver.start_solver_workers()
        assert [t for t in threading.enumerate() if t.name.startswith("solver-")] == first
    finally:
        solver.stop_solver_workers(timeout=2)
    assert first
    assert all(not t.is_alive() for t in first)
    solver.start_solver_workers()
    try:
        assert all(t not in first for t in solver._solver_workers)
    finally:
        solver.stop_solver_workers(timeout=2)


def test_stop_is_bounded_and_refuses_overlap_with_unfinished_generation(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(solver_pool, "_POOL_ENABLED", False)
    monkeypatch.setattr(solver_pool, "_N_SOLVER_WORKERS", 1)
    monkeypatch.setattr(state, "solver_queue", queue.Queue())

    def process(*_args):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(solver, "_process_solver_item", process)
    state.solver_queue.put(({}, {}, 0))
    solver.start_solver_workers()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        assert solver.stop_solver_workers(timeout=0.01) is False
        assert time.monotonic() - started < 1
        with pytest.raises(RuntimeError, match="still stopping"):
            solver.start_solver_workers()
    finally:
        release.set()
        solver.stop_solver_workers(timeout=2)


def test_stop_closes_pool_and_does_not_recreate_it(monkeypatch):
    class Pool:
        closed = False

        def shutdown(self, *, wait, cancel_futures):
            assert not wait
            assert cancel_futures
            self.closed = True

    pool = Pool()
    monkeypatch.setattr(solver_pool, "_solver_pool", pool)
    assert solver.stop_solver_workers(timeout=0)
    assert pool.closed
    assert solver_pool._solver_pool is None
    solver_pool._replace_solver_pool(pool, "late failure")
    assert solver_pool._solver_pool is None


def test_start_builds_and_prewarms_the_pool_and_stop_shuts_it(monkeypatch):
    calls = []

    class Pool:
        def submit(self, fn, *args):
            calls.append(("submit", fn.__name__))

        def shutdown(self, *, wait, cancel_futures):
            calls.append(("shutdown", wait, cancel_futures))

    monkeypatch.setattr(solver_pool, "_POOL_ENABLED", True)
    monkeypatch.setattr(solver_pool, "_N_SOLVER_WORKERS", 2)
    monkeypatch.setattr(solver_pool, "_make_solver_pool", Pool)
    monkeypatch.setattr(state, "solver_queue", queue.Queue())
    solver.start_solver_workers()
    try:
        assert isinstance(solver_pool._solver_pool, Pool)
        assert len([t for t in solver._solver_workers if t.is_alive()]) == 2
    finally:
        solver.stop_solver_workers(timeout=2)
    assert calls == [("submit", "solve_multinode")] * 2 + [("shutdown", False, True)]
    assert solver_pool._solver_pool is None
