"""A step a stop lands inside is finished before the stop goes on.

What it guards, a send and a database write, is shown where those happen:
tests/test_mail_outbox.py stops the worker inside a send, a claim and a purge,
and tests/test_mail_events.py stops the feed inside the marking. These pin the
helper's own contract with plain coroutines.
"""

import asyncio
import logging

import pytest

from services.cancel_shield import to_completion

LABEL = "Caller"
CALLER = logging.getLogger("tests.cancel_shield.caller")


class Step:
    """A step that waits for `release` once it has started, then returns
    "done" or raises `fails`, recording the task it runs in and whether it got
    to the end."""

    def __init__(self, fails: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fails = fails
        self.task: asyncio.Task | None = None
        self.finished = False

    async def __call__(self) -> str:
        self.task = asyncio.current_task()
        self.started.set()
        await self.release.wait()
        self.finished = True
        if self.fails is not None:
            raise self.fails
        return "done"


async def _ticks() -> None:
    """Long enough for a stop to have gone through, had nothing held it."""
    for _ in range(10):
        await asyncio.sleep(0)


def _logged(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == CALLER.name]


async def test_a_step_nobody_stops_returns_or_raises_as_it_would(caplog):
    caplog.set_level(logging.DEBUG, logger=CALLER.name)
    answers, fails = Step(), Step(RuntimeError("the database went away"))
    answers.release.set()
    fails.release.set()

    assert await to_completion(answers(), log=CALLER, label=LABEL) == "done"
    with pytest.raises(RuntimeError, match="the database went away"):
        await to_completion(fails(), log=CALLER, label=LABEL)
    assert _logged(caplog) == []


async def test_a_stop_waits_for_the_step_then_goes_on(caplog):
    caplog.set_level(logging.DEBUG, logger=CALLER.name)
    step = Step()
    task = asyncio.create_task(to_completion(step(), log=CALLER, label=LABEL))
    await step.started.wait()

    task.cancel()
    await _ticks()
    assert not task.done()
    step.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert step.finished
    assert _logged(caplog) == []


async def test_a_second_stop_while_waiting_still_lets_the_step_finish():
    """Awaiting the step itself while stopping would hand the second stop on to
    it, which is the cancellation inside a statement this exists to prevent."""
    step = Step()
    task = asyncio.create_task(to_completion(step(), log=CALLER, label=LABEL))
    await step.started.wait()

    task.cancel()
    await _ticks()
    task.cancel()
    await _ticks()
    assert not task.done()
    step.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert step.finished


async def test_a_step_that_fails_while_stopping_is_logged_by_its_caller_and_the_stop_goes_on(caplog):
    caplog.set_level(logging.DEBUG, logger=CALLER.name)
    step = Step(RuntimeError("the database went away"))
    task = asyncio.create_task(to_completion(step(), log=CALLER, label=LABEL))
    await step.started.wait()

    task.cancel()
    await _ticks()
    step.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    [record] = _logged(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Caller: the step under way failed while stopping"
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)


async def test_a_step_cancelled_itself_stops_its_caller_and_logs_nothing(caplog):
    """As the event loop's own teardown cancels every task left, the step's
    among them: there is nothing left to wait for, and nothing failed."""
    caplog.set_level(logging.DEBUG, logger=CALLER.name)

    async def cancelled() -> str:
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await to_completion(cancelled(), log=CALLER, label=LABEL)

    step = Step()
    task = asyncio.create_task(to_completion(step(), log=CALLER, label=LABEL))
    await step.started.wait()
    task.cancel()
    await _ticks()
    assert step.task is not None and step.task is not task
    step.task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not step.finished
    assert _logged(caplog) == []
