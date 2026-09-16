"""Cancellation must account for synchronous background work still running."""

import asyncio
import threading

import pytest

from services.tasks import executor as executor_module
from services.tasks.executor import task_executor, unfinished_task_executors


@pytest.mark.asyncio
async def test_cancellation_waits_for_running_work():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def work():
        entered.set()
        release.wait(2)
        finished.set()

    async def task():
        async with task_executor("test-owned", shutdown_timeout=1) as run:
            await run(work)

    active = asyncio.create_task(task())
    while not entered.is_set():
        await asyncio.sleep(0.001)
    active.cancel()
    await asyncio.sleep(0.01)
    assert not active.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert finished.is_set()


@pytest.mark.asyncio
async def test_shutdown_deadline_reports_work_that_cannot_be_interrupted(caplog):
    entered = threading.Event()
    release = threading.Event()

    def work():
        entered.set()
        release.wait(2)

    async def task():
        async with task_executor("test-stuck", shutdown_timeout=0.01) as run:
            await run(work)

    active = asyncio.create_task(task())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, timeout=1)
        assert "test-stuck" in caplog.text
        assert "still running" in caplog.text
        with pytest.raises(RuntimeError, match="still running"):
            async with task_executor("test-stuck"):
                pytest.fail("Started a second executor over unfinished work")
    finally:
        release.set()
        async with asyncio.timeout(2):
            while "test-stuck" in unfinished_task_executors():
                await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_each_context_has_a_fresh_executor_and_propagates_work_errors():
    threads = []
    for _ in range(2):
        async with task_executor("test-restart") as run:
            threads.append(await run(threading.current_thread))
            with pytest.raises(ValueError, match="bad work"):
                await run(lambda: int("bad work"))
    assert threads[0] is not threads[1]


@pytest.mark.asyncio
async def test_failed_executor_creation_does_not_prevent_retry(monkeypatch):
    constructor = executor_module.concurrent.futures.ThreadPoolExecutor

    def fail(**_kwargs):
        raise RuntimeError("cannot create executor")

    monkeypatch.setattr(executor_module.concurrent.futures, "ThreadPoolExecutor", fail)
    with pytest.raises(RuntimeError, match="cannot create executor"):
        async with task_executor("test-construction"):
            pass
    monkeypatch.setattr(executor_module.concurrent.futures, "ThreadPoolExecutor", constructor)
    async with task_executor("test-construction") as run:
        assert await run(lambda: 1) == 1
