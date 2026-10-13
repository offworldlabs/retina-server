"""Finishing the step a background task is in before a shutdown stops it.

The lifespan stops its tasks by cancelling them, and a cancellation lands
wherever the task happens to be waiting. The mail outbox's worker and the
Cloudflare bounce feed each have steps that must not be left halfway, and run
them through `to_completion`.
"""

import asyncio
import logging
from collections.abc import Awaitable


async def to_completion[T](work: Awaitable[T], *, log: logging.Logger, label: str) -> T:
    """Await `work`, and if this task is cancelled, let it finish first.

    A step run through here is never stopped inside: a shutdown stops the task
    after it, where it would have gone on. Inside a send, the provider may have
    the mail already: stopping before its result is recorded would leave the
    row to be retaken when the lease runs out and sent a second time, so a
    shutdown waits out the send (bounded by the transport's own timeout) and
    its record. Inside a statement, the connection is dropped with its
    transaction still open (aiosqlite before 0.22.1 gives SQLAlchemy no way to
    stop one), and the write lock it holds keeps every other writer out until
    that connection happens to be garbage collected.

    A second cancel while it waits is waited out too: awaiting the step itself
    would hand that cancel on to the step, inside whatever statement it is in.
    A step that fails once the task is stopping is logged on the caller's `log`
    under its `label`, and the cancellation goes on. One that fails with nobody
    stopping it raises as it would have.
    """
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        if not task.cancelled() and (failure := task.exception()) is not None:
            log.error("%s: the step under way failed while stopping", label, exc_info=failure)
        raise
