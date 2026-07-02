"""Observation orchestration: emit loop_begin/step/end and create an OTel span.

:class:`LoopObserver` follows the same pattern as
:class:`~loop_agent.progress.ProgressLog` (an ``on_step`` observation hook plus
``record_result``), while also adding ``loop_begin`` / ``loop_end`` loop boundary
events and wrapping the whole run in a single OTel GenAI span.

There are two ways to use it. Manual wiring, in the same shape as the existing
``ProgressLog``::

    obs = LoopObserver(sinks=[JsonlEventSink(path)])
    with obs:
        result = run_loop(act=..., verify=..., conditions=..., on_step=obs.on_step)
        obs.record_result(result)

All-in-one usage, the recommended entry point::

    result = run_observed_loop(
        act=..., verify=..., conditions=..., sinks=[JsonlEventSink(path)]
    )

This layer **depends only on the loop core**. ``loop_begin`` is emitted before
the first step and ``loop_end`` after the loop returns, so begin/end are always
recorded even for an immediate ``MaxIterations(0)`` stop. If the loop body exits
with an exception, :meth:`__exit__` emits a ``loop_end`` with ``status="error"``
and closes the span as ERROR, making every termination path observable.
"""

from __future__ import annotations

import inspect

from typing import Any, Callable, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition
from .events import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    EventSink,
    LoopEvent,
    SinkErrorHandler,
    _jsonable,
    fan_out,
)
from .loop import (
    ActHook,
    Conditions,
    GatherHook,
    LoopResult,
    StepHook,
    VerifyHook,
    _default_gather,
    run_loop,
)
from .otel import LoopSpan
from .state import LoopState, StepRecord


def _condition_names(conditions: Conditions) -> list[str]:
    """Extract the list of names from stop conditions for the loop_begin context."""
    if isinstance(conditions, AnyOf):
        conds: Sequence[StopCondition] = conditions.conditions
    else:
        conds = conditions
    return [getattr(c, "name", type(c).__name__) for c in conds]


class LoopObserver:
    """Observe one loop run and emit structured events plus an OTel span.

    Events are distributed to sinks on a best-effort basis; sink exceptions do
    not kill the loop. The span automatically becomes a no-op when OTel is
    unavailable (:class:`~loop_agent.otel.LoopSpan`).
    """

    def __init__(
        self,
        sinks: Sequence[EventSink] = (),
        *,
        conditions: Optional[Conditions] = None,
        otel: bool = True,
        tracer: "Optional[Any]" = None,
        span_name: str = "loop_agent.loop",
        on_sink_error: Optional[SinkErrorHandler] = None,
        initial_state: Optional[LoopState] = None,
    ) -> None:
        self._sinks: tuple[EventSink, ...] = tuple(sinks)
        self._conditions = conditions
        self._on_sink_error = on_sink_error
        self._span = LoopSpan(tracer=tracer, enabled=otel, span_name=span_name)
        self._begun = False
        self._ended = False
        # The last confirmed cumulative metrics seen by on_step. Even on
        # termination paths where no result is available (exception/missed
        # result), loop_end / span preserve the iterations already completed.
        # During resume, the new process can raise in gather/act/conditions
        # before calling on_step even once, so seed from the restored state's
        # cumulative values to avoid collapsing the iterations completed before
        # interruption to 0 in an error/incomplete loop_end.
        self._last_iterations = initial_state.iteration if initial_state is not None else 0
        self._last_tokens_used = (
            initial_state.tokens_used if initial_state is not None else 0
        )
        self._last_elapsed = initial_state.elapsed if initial_state is not None else 0.0

    # -- Wiring hooks, in the same pattern as ProgressLog ------------------

    def begin(self) -> None:
        """Emit ``loop_begin`` and start the OTel span. Idempotent."""
        if self._begun:
            return
        self._begun = True
        self._span.start()
        payload: dict[str, Any] = {}
        if self._conditions is not None:
            payload["conditions"] = _condition_names(self._conditions)
        self._emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0, payload=payload))

    def on_step(self, record: StepRecord, state: LoopState) -> None:
        """Emit ``loop_step``. Matches the driver's ``StepHook``."""
        # Snapshot confirmed cumulative metrics. Since state is a mutable
        # object reused for each iteration, explicitly retain scalar values.
        self._last_iterations = state.iteration
        self._last_tokens_used = state.tokens_used
        self._last_elapsed = state.elapsed
        self._span.add_step(
            iteration=record.iteration,
            tokens=record.tokens,
            tokens_used=state.tokens_used,
            elapsed=state.elapsed,
            goal_met=record.goal_met,
            detail=record.detail,
        )
        self._emit(
            LoopEvent(
                kind=LOOP_STEP,
                iteration=record.iteration,
                elapsed=state.elapsed,
                payload={
                    "tokens": record.tokens,
                    "tokens_used": state.tokens_used,
                    "goal_met": record.goal_met,
                    "detail": record.detail,
                    "observation": _jsonable(record.observation),
                },
            )
        )

    def record_result(self, result: LoopResult) -> None:
        """Emit ``loop_end`` and close the span with the reason and metrics."""
        stop_name = result.stop.name if result.stop is not None else None
        self._emit_end(
            status=result.status,
            stop=stop_name,
            reason=result.reason,
            goal_met=result.goal_met,
            iterations=result.iterations,
            tokens_used=result.tokens_used,
            elapsed=result.elapsed,
        )

    def record_error(self, error: BaseException) -> None:
        """Record a ``loop_end`` with ``status="error"`` when the loop raises.

        Iterations, tokens, and similar metrics use the **last confirmed
        cumulative values** retained by on_step, so the cost of completed
        iterations is not lost. If no iteration completed, they are 0. The
        exception details are included in the reason, and the span is closed as
        ERROR with the exception recorded.
        """
        reason = f"{type(error).__name__}: {error}"
        self._emit_end(
            status="error",
            stop=None,
            reason=reason,
            goal_met=False,
            iterations=self._last_iterations,
            tokens_used=self._last_tokens_used,
            elapsed=self._last_elapsed,
            error=error,
        )

    def record_incomplete(self) -> None:
        """Emit an ``incomplete`` loop_end when the result is missed without an exception.

        Like record_error, this uses the last confirmed cumulative metrics and
        keeps span and event sink termination observations aligned, avoiding a
        record with begin but no end.
        """
        self._emit_end(
            status="incomplete",
            stop=None,
            reason="observer closed without a result",
            goal_met=False,
            iterations=self._last_iterations,
            tokens_used=self._last_tokens_used,
            elapsed=self._last_elapsed,
        )

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "LoopObserver":
        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and isinstance(exc, BaseException):
            # The loop body raised: leave an error if record_result was not called.
            self.record_error(exc)
        elif not self._ended:
            # Safety path for forgetting record_result without an exception,
            # keeping span/sink termination aligned.
            self.record_incomplete()
        return False  # Propagate exceptions instead of swallowing them.

    # -- Internal ----------------------------------------------------------

    def _emit_end(
        self,
        *,
        status: str,
        stop: Optional[str],
        reason: str,
        goal_met: bool,
        iterations: int,
        tokens_used: int,
        elapsed: float,
        error: Optional[BaseException] = None,
    ) -> None:
        """Common path for all termination: close span and emit matching ``loop_end``.

        Span termination and event emission always happen as a pair, and a
        duplicate end is idempotently ignored. This keeps OTel-side and event
        sink-side termination observations consistent.
        """
        if self._ended:
            return
        self._ended = True
        self._span.end(
            status=status,
            reason=reason,
            iterations=iterations,
            tokens_used=tokens_used,
            elapsed=elapsed,
            stop=stop,
            error=error,
        )
        self._emit(
            LoopEvent(
                kind=LOOP_END,
                iteration=iterations,
                elapsed=elapsed,
                payload={
                    "status": status,
                    "stop": stop,
                    "reason": reason,
                    "goal_met": goal_met,
                    "iterations": iterations,
                    "tokens_used": tokens_used,
                },
            )
        )

    def _emit(self, event: LoopEvent) -> None:
        fan_out(self._sinks, event, on_error=self._on_sink_error)


def run_observed_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    sinks: Sequence[EventSink] = (),
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    otel: bool = True,
    tracer: "Optional[Any]" = None,
    span_name: str = "loop_agent.loop",
    on_sink_error: Optional[SinkErrorHandler] = None,
    time_fn: Optional[Callable[[], float]] = None,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """All-in-one entry point that wires observation and runs ``run_loop``.

    Takes the same ``act`` / ``verify`` / ``conditions`` / ``gather`` as
    ``run_loop`` and adds ``sinks`` plus OTel settings for observation. If the
    caller supplies ``on_step``, it is composed with the observation hook so
    both are called. The return value is ``run_loop``'s
    :class:`~loop_agent.loop.LoopResult`.

    Passing ``initial_state`` lets an interrupted loop **resume** while keeping
    observation intact. It is forwarded directly to ``run_loop``'s argument of
    the same name; see that docstring for details and limits. Observation emits
    begin/step/end as a new-process run, so loop_begin has iteration 0, while
    step/end iterations and cumulative metrics continue from the restored state.

    Events are always emitted in this order: loop_begin before the first step,
    loop_step N times, then loop_end after return. Exceptions from the loop body
    are re-raised after recording a loop_end with ``status="error"``.
    """
    observer = LoopObserver(
        sinks,
        conditions=conditions,
        otel=otel,
        tracer=tracer,
        span_name=span_name,
        on_sink_error=on_sink_error,
        initial_state=initial_state,
    )

    if on_step is None:
        step_hook: StepHook = observer.on_step
    else:
        user_on_step = on_step

        def step_hook(record: StepRecord, state: LoopState):
            # Let the caller's durable side effect (for the CLI, DBProgressLog)
            # succeed before emitting derived observations. If the user hook
            # raises or returns an invalid async seam on the sync path, the step
            # event is not emitted ahead of the source of truth.
            result = user_on_step(record, state)
            if inspect.isawaitable(result):
                return result
            observer.on_step(record, state)
            return result

    # Forward time_fn / initial_state to run_loop only when provided, preserving
    # the defaults (time.monotonic / fresh start).
    run_kwargs: dict[str, Any] = {}
    if time_fn is not None:
        run_kwargs["time_fn"] = time_fn
    if initial_state is not None:
        run_kwargs["initial_state"] = initial_state

    with observer:
        result = run_loop(
            act=act,
            verify=verify,
            conditions=conditions,
            gather=gather,
            on_step=step_hook,
            **run_kwargs,
        )
        observer.record_result(result)
    return result
