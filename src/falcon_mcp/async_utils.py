"""Cancellation-safe joining for resources that must finish closing before unlock."""

import asyncio


async def join_cleanup(task: asyncio.Task):
    """Finish cleanup despite repeated outer cancellation, then propagate cancellation.

    The task must own all cleanup state, including subprocesses still being created.
    Shielding once is insufficient: a second cancellation interrupts that shield too.
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
