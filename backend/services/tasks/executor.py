"""Ownership and bounded shutdown for a task's synchronous background work."""

import asyncio
import concurrent.futures
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Generation:
    jobs: set[concurrent.futures.Future] = field(default_factory=set)
    closing: bool = False


_lock = threading.Lock()
_generations: dict[str, _Generation] = {}


def unfinished_task_executors() -> list[str]:
    """Task names whose synchronous work has not completed."""
    with _lock:
        return [name for name, generation in _generations.items() if any(not job.done() for job in generation.jobs)]


def _finished(name: str, generation: _Generation, job: concurrent.futures.Future) -> None:
    with _lock:
        generation.jobs.discard(job)
        if generation.closing and not generation.jobs and _generations.get(name) is generation:
            del _generations[name]


def _consume_result(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()


@asynccontextmanager
async def task_executor(name: str, shutdown_timeout: float = 5.0):
    """Yield an async runner and close its executor when the task exits.

    Shield work from coroutine cancellation: cancelling the await cannot stop
    its thread, and must not erase our ability to wait for completion. Python
    cannot interrupt a running native call; a missed deadline is logged rather
    than turning application shutdown into an indefinite wait.
    """
    with _lock:
        if name in _generations:
            raise RuntimeError(f"{name}: previous executor is still running")
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        generation = _Generation()
        _generations[name] = generation

    async def run(fn, *args):
        job = executor.submit(fn, *args)
        with _lock:
            generation.jobs.add(job)
        job.add_done_callback(lambda completed: _finished(name, generation, completed))
        return await asyncio.shield(asyncio.wrap_future(job))

    try:
        yield run
    finally:
        with _lock:
            generation.closing = True
            pending = list(generation.jobs)
            if not pending:
                _generations.pop(name, None)
        executor.shutdown(wait=False, cancel_futures=True)
        if pending:
            done, running = await asyncio.wait([asyncio.wrap_future(job) for job in pending], timeout=shutdown_timeout)
            for future in done:
                _consume_result(future)
            for future in running:
                future.add_done_callback(_consume_result)
            if running:
                logging.warning("%s: %d job(s) still running after shutdown deadline", name, len(running))
