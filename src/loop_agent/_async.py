"""Small utility shared by sync and async seams (Issue #40).

:func:`maybe_await` is a thin adapter that only says: if the value is
awaitable, await it; otherwise, return it as-is. This lets ``gather`` /
``act`` / ``verify`` / ``conditions`` / ``gate`` hooks be **accepted as
synchronous callables** while **asynchronous callables can also be awaited** at
the same call site.

Usage convention: pass the **result of calling** the hook (not the hook itself).

    outcome = await maybe_await(act(context))   # act may be sync or async

Synchronous hooks return ordinary values, so :func:`inspect.isawaitable` is
``False`` and the value is returned immediately with almost no extra cost (no
coroutine is created). Asynchronous hooks return a coroutine / future, so they
are awaited in place.

This is the foundation that lets :func:`loop_agent.loop.async_run_loop` drive
both sync and async paths with a single control flow. ``review``,
``conditions`` (each ``check``), and ``gate.review`` follow the same convention
and accept either sync or async implementations.

**strict-sync mode (for run_loop)**: the synchronous API
:func:`loop_agent.run_loop` drives the shared coroutine *all at once in the
caller's context* -- without creating an event loop, manually stepping it with
``coro.send(None)``. run_loop calls :func:`loop_agent.async_run_loop` with
``_strict_sync=True``, and async_run_loop sets :data:`reject_awaitables` to
``True`` only for its own execution scope. If :func:`maybe_await` receives an
awaitable under strict-sync, it raises :exc:`AsyncSeamInSyncLoop` without
awaiting it. This consistently and reliably rejects "an async seam was passed
to run_loop" early for **any async seam**, without depending on whether the
async hook actually suspends internally (preventing the inconsistent case where
only suspending hooks are rejected while non-suspending hooks run silently).
Use :func:`loop_agent.async_run_loop` for async seams.

async_run_loop **explicitly sets the flag to its own mode at entry** (overriding
the inherited value), so behavior does not depend on nesting or the presence of
an ambient event loop: even if a synchronous ``run_loop`` hook internally runs
``asyncio.run(async_run_loop(...))``, the inner call enters with
``_strict_sync=False``, so it is not treated as strict and its valid inner async
seams are awaited. Conversely, if ``run_loop`` is called from inside a running
loop, ``_strict_sync=True`` still takes effect and async seams are correctly
rejected.
"""

from __future__ import annotations

import contextvars
import inspect
from typing import Awaitable, TypeVar, Union

# The canonical definition of AsyncSeamInSyncLoop lives in loop_agent.errors
# (moved into the unified hierarchy in Issue #43).
# Re-export it here for backward compatibility: do not break existing
# `from loop_agent._async import AsyncSeamInSyncLoop` /
# `loop_agent._async.AsyncSeamInSyncLoop` references.
from .errors import AsyncSeamInSyncLoop

T = TypeVar("T")

__all__ = ["AsyncSeamInSyncLoop", "reject_awaitables", "driven_synchronously", "maybe_await"]

# async_run_loop sets this to True only for its own execution scope
# (_strict_sync=True when driven by run_loop).
# While True, maybe_await / afirst_triggered reject awaitables with
# AsyncSeamInSyncLoop.
# It is explicitly set at entry, so it overrides inherited values
# (copy_context) and does not leak across nesting or ambient loops.
reject_awaitables: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loop_agent_reject_awaitables", default=False
)


def driven_synchronously() -> bool:
    """Return ``True`` in strict-sync mode (= while driven by ``run_loop``).

    Reads :data:`reject_awaitables`, which :func:`loop_agent.async_run_loop`
    sets at entry according to ``_strict_sync``. The decision is based on the
    value explicitly provided by the driver, not on whether an ambient event
    loop exists, so calls to ``run_loop`` from inside a running loop and nested
    ``asyncio.run(async_run_loop(...))`` calls are distinguished correctly.
    """
    return reject_awaitables.get()


async def maybe_await(value: Union[T, Awaitable[T]]) -> T:
    """Await ``value`` if it is awaitable; otherwise return ``value``.

    Pass the return value from calling a hook (not the hook function itself).
    Since synchronous hooks return non-awaitable values, they are returned
    immediately without waiting.

    If an awaitable is received in strict-sync mode (=
    :func:`driven_synchronously` is ``True`` while synchronous ``run_loop`` is
    manually driving), raise :exc:`AsyncSeamInSyncLoop` without awaiting it (and
    ``close`` the awaitable to avoid unawaited warnings).
    """
    if inspect.isawaitable(value):
        if driven_synchronously():
            # Close before rejecting to avoid unawaited coroutine warnings
            # (future-like objects without close are skipped by the getattr
            # guard).
            close = getattr(value, "close", None)
            if close is not None:
                close()
            raise AsyncSeamInSyncLoop(
                "run_loop() received an async (awaitable) seam "
                "(act/review/verify/gather/condition/gate/on_step/on_complete); "
                "use `await async_run_loop(...)` for async seams"
            )
        return await value  # type: ignore[no-any-return]
    return value  # type: ignore[return-value]
