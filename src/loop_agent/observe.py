"""Observation orchestration: emit loop_begin/step/end and span OTel.

:class:`LoopObserver` follows the same pattern as :class:`~loop_agent.progress.ProgressLog`
(``on_step`` observation hook + ``record_result``), while also adding loop boundaries
``loop_begin`` / ``loop_end``, and wraps 1 OTel GenAI span over the entire run.

There are 2 ways to use it. Manual wiring (same form as existing ``ProgressLog``)::

    obs = LoopObserver(sinks=[JsonlEventSink(path)])
    with obs:
        result = run_loop(act=..., verify=..., conditions=..., on_step=obs.on_step)
        obs.record_result(result)

Bulk case (recommended entry point)::

    result = run_observed_loop(
        act=..., verify=..., conditions=..., sinks=[JsonlEventSink(path)]
    )

This layer depends **only on the loop core**. loop_begin is emitted before the first step,
and loop_end is emitted after the loop returns, so begin/end are always emitted even with
``MaxIterations(0)`` immediate stop. If the loop body exits with an exception, :meth:`__exit__`
also emits a loop_end with ``status="error"`` and closes the span as ERROR, making all exit
paths observable.
"""

from __future__ import annotations

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
    """Extract a list of names from the stop conditions (for loop_begin context)."""
    if isinstance(conditions, AnyOf):
        conds: Sequence[StopCondition] = conditions.conditions
    else:
        conds = conditions
    return [getattr(c, "name", type(c).__name__) for c in conds]


class LoopObserver:
    """Observe a single loop run and emit structured events + OTel span.

    Distribute to sinks on a best-effort basis (sink exceptions do not kill the loop).
    The span automatically becomes a no-op if OTel is absent (:class:`~loop_agent.otel.LoopSpan`).
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
        # Last confirmed cumulative metrics seen by on_step. Even in exit paths where result is not
        # obtained (exception/dropouts), we keep the metrics for already-completed iterations in
        # loop_end / span. In resume, before the new process calls on_step even once, gather/act/condition
        # may throw an exception, so we seed with cumulative values from the restored state to prevent
        # error/incomplete loop_end from zeroing out "iterations completed before suspension".
        self._last_iterations = initial_state.iteration if initial_state is not None else 0
        self._last_tokens_used = (
            initial_state.tokens_used if initial_state is not None else 0
        )
        self._last_elapsed = initial_state.elapsed if initial_state is not None else 0.0

    # -- Wiring hooks (same pattern as ProgressLog) -------------------------

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
        # Snapshot confirmed cumulative metrics (state is a mutable object reused across
        # iterations, so we explicitly save scalar values).
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
        """Emit ``loop_end`` and close the span with stop reason + metrics."""
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
        """Leave a loop_end with ``status="error"`` when the loop exits with an exception.

        Iterations/tokens, etc. are populated with **last confirmed cumulative values** saved by on_step
        (preserving the cost of completed iterations; 0 if no iteration completed). Exception details
        go in the stop reason, the span is closed as ERROR, and the exception is recorded.
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
        """Insurance fallback ``status="incomplete"`` loop_end for when result is not obtained without exception.

        Like record_error, populate with the last confirmed cumulative metrics, and align span and event
        sink termination observation (do not leave records with begin but no end).
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
            # Loop body exited with exception: leave error if record_result was not called.
            self.record_error(exc)
        elif not self._ended:
            # Insurance for case where record_result was forgotten without exception (align span/sink termination).
            self.record_incomplete()
        return False  # Do not suppress exceptions; propagate them.

    # -- Internal -----------------------------------------------------------

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
        """Common to all exit paths: close the span and emit the paired ``loop_end`` event.

        Always perform span end and event emit as a pair, and idempotently ignore double ends.
        This ensures termination observation on both OTel and event sink sides always matches.
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
    """Bulk entry point that wires observation and runs :func:`~loop_agent.loop.run_loop`.

    Takes the same ``act`` / ``verify`` / ``conditions`` / ``gather`` as ``run_loop``, and adds
    ``sinks`` and OTel configuration for observation. If the user provides ``on_step``, it is
    composed with the observation hook and both are called. The return value is :class:`~loop_agent.loop.LoopResult`
    from ``run_loop``.

    When ``initial_state`` is passed, a suspended loop can be **resumed** while preserving observation
    (passed through to the same-named argument in ``run_loop``; see its docstring for details and limits).
    Observation is emitted as a run of the new process with begin/step/end, so loop_begin iteration
    starts at 0, but step/end iterations and cumulative metrics continue from the restored state.

    Guarantees to emit in order: loop_begin (before the first step) → loop_step×N → loop_end (after return).
    Exceptions in the loop body leave a loop_end with ``status="error"`` before re-raising.
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

        def step_hook(record: StepRecord, state: LoopState) -> None:
            observer.on_step(record, state)
            user_on_step(record, state)

    # time_fn / initial_state are forwarded to run_loop only when provided, respecting defaults
    # (time.monotonic / fresh start).
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
