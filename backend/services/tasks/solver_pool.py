"""Solver process pool: ships the LM solves to spawn children, off the GIL.

services.tasks.solver's worker threads keep the gates, claims and counters;
only the pure compute crosses here, and every call runs inline when there is
no pool.
"""

import concurrent.futures
import logging
import multiprocessing
import os
import threading
from concurrent.futures.process import BrokenProcessPool

from core import state

_N_SOLVER_WORKERS = int(os.getenv("SOLVER_WORKERS", "2"))

# ── Solver process pool ──────────────────────────────────────────────────────
# The LM solves are pure CPU on picklable dicts, and running them on worker
# *threads* meant competing for the GIL with frame processing and the
# analytics refresh: py-spy on staging showed the two solver threads getting
# ~25% of interpreter time between them, the queue draining at ~0.8 items/s,
# and burst tails aging past the 45 s staleness drop.  The threads remain —
# they own the gates, the track claims and the counters, which live in this
# process — but the solve_multinode / fit_constant_velocity calls are shipped
# to child processes so the optimizer runs on its own core.
#
# spawn, not fork: this process is full of threads holding locks, and a forked
# child would inherit them mid-flight.  A spawn child imports only
# retina_geolocator (the submitted functions' module), none of the backend.
#
# The pool is created by start_solver_workers, never at import: tests and the
# offline bench reach the compute inline through the same helper (pool is
# None → direct call), so they pay no child startup and need no teardown.  If
# the pool breaks mid-flight (a child OOM-killed, say) the affected call runs
# inline and the first thread to notice rebuilds the pool for the rest.
_solver_pool: concurrent.futures.ProcessPoolExecutor | None = None
_solver_pool_lock = threading.Lock()

# Never under pytest: route tests boot the whole app via TestClient, so the
# lifespan's start_solver_workers would hang a real pool off the test process
# — and any later test that monkeypatches the compute functions would ship an
# unpicklable closure to a child.  Pool transport has its own tests, which
# build the pool explicitly.
#
# Gated on SOLVER_POOL (conftest.py sets 0), not RETINA_ENV: every deploy tier
# currently runs RETINA_ENV=test (see docker-compose.*.yml / ClickUp 86cb1emcx),
# and keying off it silently reverted deployments to inline GIL-bound solving.
_POOL_ENABLED = os.getenv("SOLVER_POOL", "1").strip().lower() not in ("0", "false", "off")

# Wall-clock ceiling on one pool round trip.  A solve runs well under a
# second; the queue itself discards items older than _SOLVER_MAX_QUEUE_AGE_S
# (45 s), so a call still outstanding at 30 s has already lost the work it was
# doing.  Without a timeout a child that is *alive but stuck* — a pathological
# LM, a spawn child wedged on import, a machine deep in swap — blocks one of
# only SOLVER_WORKERS (2) threads for the life of the process, silently: the
# enqueue side counts solver_queue_drops / solver_stale_drops, but nothing on
# this path bumps an error counter, so task health stays green while the lane
# is dead.
_POOL_CALL_TIMEOUT_S = float(os.getenv("SOLVER_POOL_CALL_TIMEOUT_S", "30"))


def _make_solver_pool() -> concurrent.futures.ProcessPoolExecutor:
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=_N_SOLVER_WORKERS,
        mp_context=multiprocessing.get_context("spawn"),
    )


def _replace_solver_pool(pool, reason: str) -> None:
    """Tear down `pool` and stand a fresh one up in its place.

    Idempotent across the worker threads: whoever gets the lock first swaps
    the executor, and a second thread that hit the same failure finds
    _solver_pool already moved on and leaves it alone.
    """
    global _solver_pool
    with _solver_pool_lock:
        if _solver_pool is not pool:
            return
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
        try:
            _solver_pool = _make_solver_pool()
            logging.warning("Solver process pool %s — recreated", reason)
        except Exception:
            _solver_pool = None
            logging.exception(
                "Solver process pool %s and could not be recreated — solving inline on worker threads from now on",
                reason,
            )


def _pool_call(fn, *args, **kwargs):
    """Run fn in the solver process pool; inline when there is no pool.

    Keyword arguments cross to the child like the positional ones do (both are
    pickled by submit), so a callee with an options keyword — fit_constant_
    velocity's fix_altitude — does not need a positional-only wrapper.
    """
    pool = _solver_pool
    if pool is None:
        return fn(*args, **kwargs)
    future = None
    try:
        future = pool.submit(fn, *args, **kwargs)
        return future.result(timeout=_POOL_CALL_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        if future is not None:
            future.cancel()
        state.bump_counter("solver_pool_timeouts")
        state.bump_task_error("solver_pool")
        # s_in is the first argument of every submitted solve, and carries the
        # node count — the one field that says whether the hang correlates
        # with problem size.
        n_nodes = args[0].get("n_nodes") if args and isinstance(args[0], dict) else None
        logging.warning(
            "Solver pool call %s timed out after %.0fs (n_nodes=%s) — retrying inline",
            getattr(fn, "__name__", fn),
            _POOL_CALL_TIMEOUT_S,
            n_nodes,
        )
        # Same recovery as the broken-pool branch below, and the same
        # fallback: retry inline rather than dropping the item.  A wedged
        # child never releases its worker slot, so the executor is replaced
        # either way.  The inline retry is deliberate — the overwhelming
        # cause here is a sick *child*, not a pathological input, and dropping
        # would cost a solve that the fresh interpreter completes in
        # milliseconds.  It is also the only branch that can still block this
        # thread; the counter and the task error make that case visible
        # instead of silent, which is what the timeout is for.
        _replace_solver_pool(pool, "call timed out")
        return fn(*args, **kwargs)
    except BrokenProcessPool:
        _replace_solver_pool(pool, "broke")
        return fn(*args, **kwargs)


def _pool_solve_multinode(s_in, node_cfgs):
    """solve_multinode via the process pool (inline when no pool exists)."""
    from retina_geolocator.multinode_solver import solve_multinode

    return _pool_call(solve_multinode, s_in, node_cfgs)


def _pool_solve_multistart(s_in, node_cfgs, alt_starts_km):
    """solve_multinode_multistart via the process pool (inline when none).

    What it submits is a module-level retina_geolocator function taking only
    picklable arguments, because the pool is a *spawn* pool: a child imports
    retina_geolocator and nothing of the backend, so what crosses is that
    function's qualified name plus the input dicts.
    """
    from retina_geolocator.multinode_solver import solve_multinode_multistart

    return _pool_call(solve_multinode_multistart, s_in, node_cfgs, alt_starts_km, True)


def _pool_select_consensus(s_in, node_cfgs):
    """select_consensus via the process pool (inline when no pool exists)."""
    from retina_geolocator.consensus import select_consensus

    return _pool_call(select_consensus, s_in, node_cfgs)


def start_pool() -> bool:
    """Create the pool if it is enabled and absent, then prewarm its children.

    Returns whether a pool is running.  Called by start_solver_workers.
    """
    global _solver_pool
    from retina_geolocator.multinode_solver import solve_multinode

    with _solver_pool_lock:
        if _solver_pool is None and _POOL_ENABLED:
            try:
                _solver_pool = _make_solver_pool()
            except Exception:
                _solver_pool = None
                logging.exception("Solver process pool unavailable — solving inline on worker threads")
    if _solver_pool is not None:
        # Fire-and-forget prewarm: spin the children up and have them import
        # scipy now, not under the first association burst.  (<2 measurements
        # returns None before any solving.)
        _noop = {"initial_guess": {"lat": 0.0, "lon": 0.0}, "measurements": []}
        for _ in range(_N_SOLVER_WORKERS):
            _solver_pool.submit(solve_multinode, _noop, {})
    return _solver_pool is not None


def stop_pool() -> None:
    """Detach the pool and shut it down, cancelling queued calls."""
    global _solver_pool
    # Detach under the same lock used by timeout recovery. A late failure
    # of this pool then cannot replace it after shutdown.
    with _solver_pool_lock:
        pool, _solver_pool = _solver_pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
