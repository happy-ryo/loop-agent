"""Outer Reflexion loop observation: structured events + OTel GenAI span (Issue #30).

Observe the **inter-attempt lifecycle** of the outer
:func:`~loop_agent.reflexion.run_reflexion` using the same style as the inner
loop's :class:`~loop_agent.observe.LoopObserver`. The observation layer never
intervenes in ``run_reflexion`` decision logic; it leaves the existing safety
core (two-signal model / RQGM epoch gate) unchanged and only adds observation
hooks as a **side channel** (extending report.md S4.5 observability to the outer
loop).

Structured events emitted (reusing :class:`~loop_agent.events.LoopEvent`):

- ``reflexion_begin`` : run start (convergence condition names, declared axes,
  initial evaluator version, and epoch configuration).
- ``episode_begin``   : one episode start (episode/epoch/task/evaluator version).
- ``episode_end``     : one episode committed (primary aggregate / reward /
  success / lesson admission, etc.).
- ``lesson_decision`` : only for episodes that produced a lesson. Records
  admission (``admitted=True``) / rejection independently.
- ``epoch_boundary``  : epoch boundary (= new epoch start) plus evaluator
  promotion/rejection/unchanged decision.
- ``reflexion_end``   : run end (convergence reason, status, and aggregates,
  derived from ``state`` for consistency).

If OTel is installed, the same run also becomes one **GenAI span**
(:class:`~loop_agent.otel.ReflexionSpan`), with the transitions above recorded
as span events on the timeline (epoch number, evaluator version = grader id,
and lesson provenance become attributes). OTel is an **optional dependency** and
degrades to no-op when unavailable (same policy as MVP #13).

**best-effort**: sink delivery is guarded per sink through
:func:`~loop_agent.events.fan_out`, and the span is guarded inside
:class:`~loop_agent.otel.ReflexionSpan`. The observation hooks themselves are
also guarded, so observation failures (sink/tracer exceptions) never kill the
outer loop (``run_reflexion`` calls ``on_episode`` / ``on_epoch`` raw, so the
observation layer is responsible for self-guarding).
"""

from __future__ import annotations

import warnings
from typing import Any, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition
from .evaluator import (
    Evaluator,
    GroundTruthFn,
    HeldOut,
)
from .events import (
    EventSink,
    LoopEvent,
    SinkErrorHandler,
    _jsonable,
    fan_out,
)
from .memory import EpisodicMemory, LessonVerifier, default_admit
from .otel import ReflexionSpan
from .reflexion import (
    EpisodeFn,
    EpisodeRecord,
    EpochRecord,
    OuterConditions,
    ProposeEvaluatorFn,
    ReflectHook,
    ReflexionContext,
    ReflexionState,
    ReflexiveResult,
    run_reflexion,
)

# Event kinds (discriminators). Constants keep readers from scattering string
# literals when filtering.
REFLEXION_BEGIN = "reflexion_begin"
EPISODE_BEGIN = "episode_begin"
EPISODE_END = "episode_end"
LESSON_DECISION = "lesson_decision"
EPOCH_BOUNDARY = "epoch_boundary"
REFLEXION_END = "reflexion_end"

# Outer run status -> normal/error classification for spans/events.
# converged/stopped/paused are all normal endings (even a stop is a normal path
# once its reason is known). Only error is ERROR.
_OUTER_STATUSES = ("converged", "stopped", "paused")


def _outer_condition_names(conditions: OuterConditions) -> list[str]:
    """Extract names from outer stop conditions for reflexion_begin context."""
    if isinstance(conditions, AnyOf):
        conds: Sequence[StopCondition] = conditions.conditions
    else:
        conds = conditions  # Keep sequences as-is.
    return [getattr(c, "name", type(c).__name__) for c in conds]


class ReflexionObserver:
    """Observe one outer Reflexion run and emit structured events + an OTel span.

    Events are delivered to sinks on a best-effort basis (sink exceptions do
    not kill the outer loop), and the span automatically becomes a no-op when
    OTel is absent (:class:`~loop_agent.otel.ReflexionSpan`). The observation
    hooks themselves are also guarded, so observation failures do not propagate
    to ``run_reflexion``.

    For manual wiring, use this as a context manager and pass it to each
    ``run_reflexion`` observation point::

        obs = ReflexionObserver(sinks=[JsonlEventSink(path)], convergence=conds,
                                declared_keys=keys, evaluator_version=ev.version,
                                epoch_len=4, epsilon=0.02)
        with obs:
            result = run_reflexion(
                episode=lambda ctx: (obs.on_episode_begin(ctx), episode(ctx))[1],
                ..., on_episode=obs.on_episode, on_epoch=obs.on_epoch,
            )
            obs.record_result(result)

    The one-shot entry point is :func:`run_observed_reflexion` (recommended; it
    performs all wiring internally).
    """

    def __init__(
        self,
        sinks: Sequence[EventSink] = (),
        *,
        convergence: Optional[OuterConditions] = None,
        declared_keys: tuple[str, ...] = (),
        evaluator_version: str = "",
        epoch_len: Optional[int] = None,
        epsilon: Optional[float] = None,
        otel: bool = True,
        tracer: "Optional[Any]" = None,
        span_name: str = "loop_agent.reflexion",
        on_sink_error: Optional[SinkErrorHandler] = None,
        initial_state: Optional[ReflexionState] = None,
    ) -> None:
        self._sinks: tuple[EventSink, ...] = tuple(sinks)
        self._convergence = convergence
        self._declared_keys = tuple(declared_keys)
        self._evaluator_version = evaluator_version
        self._epoch_len = epoch_len
        self._epsilon = epsilon
        self._on_sink_error = on_sink_error
        self._span = ReflexionSpan(tracer=tracer, enabled=otel, span_name=span_name)
        self._begun = False
        self._ended = False
        # Last observed committed cumulative values. Even on ending paths that
        # cannot produce a result (exceptions), preserve already committed
        # episode/epoch counts in reflexion_end / the span (same policy as
        # LoopObserver).
        # During outer resume, a new process can raise from an episode/condition
        # before calling on_episode even once. Seed from the restored state's
        # cumulative values so error/incomplete reflexion_end does not collapse
        # episode/epoch counts that were committed before resume back to 0.
        self._last_episode = initial_state.episode if initial_state is not None else 0
        self._last_epoch = initial_state.epoch if initial_state is not None else 0
        self._last_best = (
            initial_state.best_gt_aggregate
            if initial_state is not None
            else float("-inf")
        )
        self._last_reflections = (
            initial_state.reflections if initial_state is not None else 0
        )
        self._last_evaluator_updates = (
            initial_state.evaluator_updates if initial_state is not None else 0
        )
        # Prefer the restored state's version (run_reflexion has already
        # verified it matches the supplied evaluator).
        self._last_evaluator_version = (
            initial_state.evaluator_version
            if initial_state is not None and initial_state.evaluator_version
            else evaluator_version
        )

    # -- Wiring hooks ------------------------------------------------------

    def begin(self) -> None:
        """Emit ``reflexion_begin`` and start the OTel span. Idempotent."""
        if self._begun:
            return
        self._begun = True
        self._span.start(
            declared_keys=self._declared_keys,
            evaluator_version=self._evaluator_version,
            epoch_len=self._epoch_len,
            epsilon=self._epsilon,
        )
        payload: dict[str, Any] = {}
        if self._convergence is not None:
            payload["conditions"] = _outer_condition_names(self._convergence)
        if self._declared_keys:
            payload["declared_keys"] = list(self._declared_keys)
        if self._evaluator_version:
            payload["evaluator_version"] = self._evaluator_version
        if self._epoch_len is not None:
            payload["epoch_len"] = self._epoch_len
        if self._epsilon is not None:
            payload["epsilon"] = self._epsilon
        self._emit(
            LoopEvent(kind=REFLEXION_BEGIN, iteration=0, elapsed=0.0, payload=payload)
        )

    def on_episode_begin(self, ctx: ReflexionContext) -> None:
        """Emit ``episode_begin``. Called just before ``run_reflexion`` episode.

        The hook body is guarded best-effort so observation failures do not kill
        the outer loop.
        """
        try:
            self._span.add_episode_begin(
                episode=ctx.episode,
                epoch=ctx.epoch,
                evaluator_version=ctx.evaluator.version,
            )
            self._emit(
                LoopEvent(
                    kind=EPISODE_BEGIN,
                    iteration=ctx.episode,
                    elapsed=0.0,
                    payload={
                        "epoch": ctx.epoch,
                        "evaluator_version": ctx.evaluator.version,
                        "task": _jsonable(ctx.task),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("on_episode_begin", exc)

    def on_episode(self, record: EpisodeRecord, state: ReflexionState) -> None:
        """Emit ``episode_end`` (+ ``lesson_decision`` when a lesson exists).

        Matches ``run_reflexion`` ``on_episode``. The hook body is guarded
        best-effort so observation failures do not kill the outer loop.
        """
        try:
            # Snapshot committed cumulative values (state is a mutable object
            # reused for each episode).
            self._last_episode = state.episode
            self._last_epoch = state.epoch
            self._last_best = state.best_gt_aggregate
            self._last_reflections = state.reflections
            self._last_evaluator_updates = state.evaluator_updates
            self._last_evaluator_version = state.evaluator_version

            lesson = record.lesson
            provenance = lesson.provenance if lesson is not None else ""
            self._span.add_episode(
                episode=record.episode,
                epoch=record.epoch,
                evaluator_version=record.evaluator_version,
                gt_aggregate=record.gt_aggregate,
                reward=record.reward,
                succeeded=record.succeeded,
                ground_truth_backed=record.signal.ground_truth_backed,
                best_gt_aggregate=state.best_gt_aggregate,
                lesson_admitted=record.admitted,
                lesson_provenance=provenance,
                detail=record.detail,
            )
            payload: dict[str, Any] = {
                "epoch": record.epoch,
                "evaluator_version": record.evaluator_version,
                "gt_aggregate": record.gt_aggregate,
                "reward": record.reward,
                "succeeded": record.succeeded,
                "ground_truth_backed": record.signal.ground_truth_backed,
                "reflections": state.reflections,
                "lesson_admitted": record.admitted,
                "lesson_provenance": provenance,
                "detail": record.detail,
            }
            # Omit best when it is -inf (no ground-truth-backed episode has
            # arrived). -Infinity is non-standard JSON; use the same convention
            # as run-end and omit it to avoid breaking downstream numeric
            # aggregation.
            if state.best_gt_aggregate != float("-inf"):
                payload["best_gt_aggregate"] = state.best_gt_aggregate
            self._emit(
                LoopEvent(
                    kind=EPISODE_END,
                    iteration=record.episode,
                    elapsed=0.0,
                    payload=payload,
                )
            )
            # For episodes that produced a lesson, record admission as an
            # independent event (easier admitted/rejected filtering).
            if lesson is not None:
                self._span.add_lesson(
                    episode=record.episode,
                    admitted=record.admitted,
                    provenance=lesson.provenance,
                    support=lesson.support,
                    reason="" if record.admitted else record.detail,
                )
                self._emit(
                    LoopEvent(
                        kind=LESSON_DECISION,
                        iteration=record.episode,
                        elapsed=0.0,
                        payload={
                            "epoch": record.epoch,
                            "admitted": record.admitted,
                            "text": lesson.text,
                            "provenance": lesson.provenance,
                            "support": lesson.support,
                            "reason": "" if record.admitted else record.detail,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("on_episode", exc)

    def on_epoch(self, record: EpochRecord) -> None:
        """Emit ``epoch_boundary``. Matches ``run_reflexion`` ``on_epoch``.

        Records evaluator promotion/rejection/unchanged (``record.decision``)
        and version transitions. The hook body is guarded best-effort so
        observation failures do not kill the outer loop.
        """
        try:
            self._last_epoch = record.epoch
            self._last_evaluator_version = record.evaluator_version
            # Synchronize the evaluator update counter at the boundary too.
            # run_reflexion increments state.evaluator_updates only at
            # boundaries where a candidate was proposed (= record.proposed),
            # regardless of promotion/rejection, so the observation snapshot
            # advances under the same condition. Without this, an
            # error/incomplete path where the next episode raises immediately
            # after the boundary would be short one evaluator_update and would
            # contradict the already emitted epoch_boundary (proposed=True).
            if record.proposed:
                self._last_evaluator_updates += 1
            admission = record.admission
            inc_agree = admission.incumbent_agreement if admission is not None else None
            cand_agree = (
                admission.candidate_agreement if admission is not None else None
            )
            self._span.add_epoch(
                epoch=record.epoch,
                boundary_episode=record.boundary_episode,
                decision=record.decision,
                previous_version=record.previous_version,
                evaluator_version=record.evaluator_version,
                incumbent_agreement=inc_agree,
                candidate_agreement=cand_agree,
            )
            payload: dict[str, Any] = {
                "epoch": record.epoch,
                "boundary_episode": record.boundary_episode,
                "evaluator_decision": record.decision,
                "proposed": record.proposed,
                "promoted": record.promoted,
                "previous_version": record.previous_version,
                "evaluator_version": record.evaluator_version,
            }
            if inc_agree is not None:
                payload["incumbent_agreement"] = inc_agree
            if cand_agree is not None:
                payload["candidate_agreement"] = cand_agree
            self._emit(
                LoopEvent(
                    kind=EPOCH_BOUNDARY,
                    iteration=record.boundary_episode,
                    elapsed=0.0,
                    payload=payload,
                )
            )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("on_epoch", exc)

    def record_result(self, result: ReflexiveResult) -> None:
        """Emit ``reflexion_end`` and close the span with reason + aggregates.

        Aggregates are derived from authoritative ``result.state``, so the
        number of emitted episode/epoch events and final aggregates always stay
        consistent (metric consistency).
        """
        stop_name = result.stop.name if result.stop is not None else None
        state = result.state
        self._emit_end(
            status=result.status,
            stop=stop_name,
            reason=result.reason,
            succeeded=result.succeeded,
            episodes=state.episode,
            epochs=state.epoch,
            best_gt_aggregate=state.best_gt_aggregate,
            reflections=state.reflections,
            evaluator_updates=state.evaluator_updates,
            evaluator_version=state.evaluator_version,
        )

    def record_error(self, error: BaseException) -> None:
        """Record ``status="error"`` reflexion_end when the outer loop raises.

        Aggregates use the **last observed committed cumulative values** so
        already committed episode/epoch counts are not lost.
        """
        reason = f"{type(error).__name__}: {error}"
        self._emit_end(
            status="error",
            stop=None,
            reason=reason,
            succeeded=False,
            episodes=self._last_episode,
            epochs=self._last_epoch,
            best_gt_aggregate=self._last_best,
            reflections=self._last_reflections,
            evaluator_updates=self._last_evaluator_updates,
            evaluator_version=self._last_evaluator_version,
            error=error,
        )

    def record_incomplete(self) -> None:
        """``status="incomplete"`` reflexion_end for a no-exception missing-result path."""
        self._emit_end(
            status="incomplete",
            stop=None,
            reason="observer closed without a result",
            succeeded=False,
            episodes=self._last_episode,
            epochs=self._last_epoch,
            best_gt_aggregate=self._last_best,
            reflections=self._last_reflections,
            evaluator_updates=self._last_evaluator_updates,
            evaluator_version=self._last_evaluator_version,
        )

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ReflexionObserver":
        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and isinstance(exc, BaseException):
            self.record_error(exc)
        elif not self._ended:
            self.record_incomplete()
        return False  # Propagate exceptions instead of swallowing them.

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        warnings.warn(
            f"reflexion observer {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    def _emit_end(
        self,
        *,
        status: str,
        stop: Optional[str],
        reason: str,
        succeeded: bool,
        episodes: int,
        epochs: int,
        best_gt_aggregate: float,
        reflections: int,
        evaluator_updates: int,
        evaluator_version: str,
        error: Optional[BaseException] = None,
    ) -> None:
        """Common end path: close the span and emit matching ``reflexion_end``.

        Span end and event emission are always paired, and duplicate end calls
        are ignored idempotently. This keeps end observation consistent between
        OTel and event sinks.
        """
        if self._ended:
            return
        self._ended = True
        self._span.end(
            status=status,
            reason=reason,
            episodes=episodes,
            epochs=epochs,
            best_gt_aggregate=best_gt_aggregate,
            reflections=reflections,
            evaluator_updates=evaluator_updates,
            evaluator_version=evaluator_version,
            stop=stop,
            error=error,
        )
        payload = {
            "status": status,
            "stop": stop,
            "reason": reason,
            "succeeded": succeeded,
            "episodes": episodes,
            "epochs": epochs,
            "reflections": reflections,
            "evaluator_updates": evaluator_updates,
            "evaluator_version": evaluator_version,
        }
        # Omit JSON-incompatible best when it is -inf (no ground-truth-backed
        # episodes).
        if best_gt_aggregate != float("-inf"):
            payload["best_gt_aggregate"] = best_gt_aggregate
        self._emit(
            LoopEvent(
                kind=REFLEXION_END,
                iteration=episodes,
                elapsed=0.0,
                payload=payload,
            )
        )

    def _emit(self, event: LoopEvent) -> None:
        fan_out(self._sinks, event, on_error=self._on_sink_error)


def run_observed_reflexion(
    *,
    episode: EpisodeFn,
    ground_truth: GroundTruthFn,
    reflect: ReflectHook,
    evaluator: Evaluator,
    convergence: OuterConditions,
    declared_keys: tuple[str, ...],
    production_tasks: Sequence[Any],
    held_out: HeldOut,
    epoch_len: int = 4,
    epsilon: float = 0.02,
    delta: float = 0.0,
    propose_evaluator: Optional[ProposeEvaluatorFn] = None,
    admit_lesson: LessonVerifier = default_admit,
    memory: Optional[EpisodicMemory] = None,
    task_id: Any = str,
    on_episode: Optional[Any] = None,
    persist: Optional[Any] = None,
    initial_state: Optional[ReflexionState] = None,
    sinks: Sequence[EventSink] = (),
    otel: bool = True,
    tracer: "Optional[Any]" = None,
    span_name: str = "loop_agent.reflexion",
    on_sink_error: Optional[SinkErrorHandler] = None,
) -> ReflexiveResult:
    """One-shot entry point that wires observation and runs ``run_reflexion``.

    Takes the same arguments as ``run_reflexion`` and adds ``sinks`` plus OTel
    settings for observation. ``episode`` is wrapped to emit ``episode_begin``,
    and observation hooks are wired to ``on_episode`` / ``on_epoch`` (if the
    caller supplied ``on_episode``, both hooks are composed and called). The
    return value is the :class:`~loop_agent.reflexion.ReflexiveResult` from
    ``run_reflexion`` unchanged (decision logic is unchanged).

    ``persist`` / ``initial_state`` are passed straight through to
    ``run_reflexion``, so outer Reflexion **persistence/resume** (Issue #29:
    :class:`~loop_agent.reflexion_store.DBReflexionLog`) and observation can
    coexist in one call. Even for runs resumed from a resume seed
    (``initial_state``), a suppressed tail boundary is recovered and emits
    ``on_epoch``, keeping observed epoch counts consistent with the DB's settled
    ``epoch`` (the SoT written by ``persist`` does not diverge from observation
    events). Observation is a side channel and never intervenes in
    ``persist`` persistence order or content.

    Events are always emitted in this order: ``reflexion_begin`` (before the
    first episode) -> ``episode_begin`` / ``episode_end`` /
    ``lesson_decision`` / ``epoch_boundary`` x N -> ``reflexion_end`` (after
    return). Exceptions from the outer loop body leave a ``status="error"``
    ``reflexion_end`` before being re-raised. If an inner episode pauses at a
    human gate, ``status="paused"`` ``reflexion_end`` is recorded (observing
    ``run_reflexion``'s pause propagation contract as-is).
    """
    observer = ReflexionObserver(
        sinks,
        convergence=convergence,
        declared_keys=declared_keys,
        evaluator_version=evaluator.version,
        epoch_len=epoch_len,
        epsilon=epsilon,
        otel=otel,
        tracer=tracer,
        span_name=span_name,
        on_sink_error=on_sink_error,
        initial_state=initial_state,
    )

    user_episode = episode

    def observed_episode(ctx: ReflexionContext):
        observer.on_episode_begin(ctx)
        return user_episode(ctx)

    if on_episode is None:
        episode_hook = observer.on_episode
    else:
        user_on_episode = on_episode

        def episode_hook(record: EpisodeRecord, state: ReflexionState) -> None:
            observer.on_episode(record, state)
            user_on_episode(record, state)

    with observer:
        result = run_reflexion(
            episode=observed_episode,
            ground_truth=ground_truth,
            reflect=reflect,
            evaluator=evaluator,
            convergence=convergence,
            declared_keys=declared_keys,
            production_tasks=production_tasks,
            held_out=held_out,
            epoch_len=epoch_len,
            epsilon=epsilon,
            delta=delta,
            propose_evaluator=propose_evaluator,
            admit_lesson=admit_lesson,
            memory=memory,
            task_id=task_id,
            on_episode=episode_hook,
            on_epoch=observer.on_epoch,
            persist=persist,
            initial_state=initial_state,
        )
        observer.record_result(result)
    return result


__all__ = [
    "ReflexionObserver",
    "run_observed_reflexion",
    "REFLEXION_BEGIN",
    "EPISODE_BEGIN",
    "EPISODE_END",
    "LESSON_DECISION",
    "EPOCH_BOUNDARY",
    "REFLEXION_END",
]
