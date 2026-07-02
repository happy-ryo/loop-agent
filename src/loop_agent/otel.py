"""OTel GenAI span integration (report.md S4.5 "Observability"). **Optional dependency**.

Represents the lifetime of a loop as a single OpenTelemetry span and attaches
GenAI semantic convention ``gen_ai.*`` attributes, the iteration number, and
the termination reason (task requirement).

``opentelemetry`` is an **optional dependency**, so environments without it do
not break. If the import fails, :class:`LoopSpan` degrades to a no-op (a dummy
object that records nothing), while the observation JSONL/event sink side keeps
working as-is. ``enabled=False`` also produces the same no-op behavior.

Semantic convention mapping (following the experimental GenAI conventions while
placing loop-specific information in the ``loop_agent.*`` namespace):

- ``gen_ai.operation.name`` = ``"loop"``      (the operation this span represents)
- ``gen_ai.system``         = ``"loop_agent"``
- ``gen_ai.usage.output_tokens`` = cumulative tokens (mapped to GenAI usage for dashboard compatibility)
- ``loop_agent.iterations``       = total iterations (= iteration number)
- ``loop_agent.status``           = ``"goal_met" | "stopped" | "error" | "incomplete"``
- ``loop_agent.stop``             = triggered stop condition name (unset if none)
- ``loop_agent.termination_reason`` = human-readable termination reason
- ``loop_agent.tokens_used`` / ``loop_agent.elapsed`` = metrics

Each iteration is recorded on the timeline as a span add_event (``loop_step``).
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

try:  # Optional dependency: degrade cleanly when it is not installed.
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent (only when OTel is not installed)
    _otel_trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False

# GenAI semantic-convention attribute keys (kept centralized).
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# Loop-specific attributes live in the loop_agent.* namespace (without polluting the GenAI convention).
ATTR_ITERATIONS = "loop_agent.iterations"
ATTR_STATUS = "loop_agent.status"
ATTR_STOP = "loop_agent.stop"
ATTR_TERMINATION_REASON = "loop_agent.termination_reason"
ATTR_TOKENS_USED = "loop_agent.tokens_used"
ATTR_ELAPSED = "loop_agent.elapsed"

DEFAULT_SPAN_NAME = "loop_agent.loop"
OPERATION_NAME = "loop"
SYSTEM_NAME = "loop_agent"

# Span convention for observing the outer Reflexion loop (run_reflexion) (Issue #30).
# It follows the same GenAI conventions as the inner loop (gen_ai.operation.name /
# gen_ai.system), while placing outer-loop-specific information in the
# loop_agent.reflexion.* namespace. Span events (episode/epoch_boundary/lesson_decision)
# record the epoch number, evaluator version (= grader id), and lesson provenance
# on the timeline.
REFLEXION_SPAN_NAME = "loop_agent.reflexion"
REFLEXION_OPERATION_NAME = "reflexion"

ATTR_REFLEXION_STATUS = "loop_agent.reflexion.status"
ATTR_REFLEXION_STOP = "loop_agent.reflexion.stop"
ATTR_REFLEXION_REASON = "loop_agent.reflexion.termination_reason"
ATTR_REFLEXION_EPISODES = "loop_agent.reflexion.episodes"
ATTR_REFLEXION_EPOCHS = "loop_agent.reflexion.epochs"
ATTR_REFLEXION_BEST = "loop_agent.reflexion.best_gt_aggregate"
ATTR_REFLEXION_REFLECTIONS = "loop_agent.reflexion.reflections"
ATTR_REFLEXION_EVALUATOR_UPDATES = "loop_agent.reflexion.evaluator_updates"
ATTR_REFLEXION_EVALUATOR_VERSION = "loop_agent.reflexion.evaluator_version"
ATTR_REFLEXION_DECLARED_KEYS = "loop_agent.reflexion.declared_keys"
ATTR_REFLEXION_EPOCH_LEN = "loop_agent.reflexion.epoch_len"
ATTR_REFLEXION_EPSILON = "loop_agent.reflexion.epsilon"


def otel_available() -> bool:
    """Return whether ``opentelemetry`` can be imported in this environment."""
    return _OTEL_AVAILABLE


class LoopSpan:
    """Thin wrapper around the OTel span for one loop run. No-op when OTel is absent.

    The span lifecycle is managed by :class:`~loop_agent.observe.LoopObserver`:
    :meth:`start` starts the span, :meth:`add_step` records iterations on the
    timeline, and :meth:`end` attaches gen_ai.* attributes plus the termination
    reason before ending it.

    If OTel is not installed or ``enabled=False``, all methods safely do nothing
    (:attr:`recording` returns ``False``).
    """

    def __init__(
        self,
        *,
        tracer: "Optional[Any]" = None,
        enabled: bool = True,
        span_name: str = DEFAULT_SPAN_NAME,
    ) -> None:
        self._span_name = span_name
        self._span: "Optional[Any]" = None
        self._ended = False
        # Fall back to no-op when OTel is absent or explicitly disabled.
        self._enabled = bool(enabled) and _OTEL_AVAILABLE
        if self._enabled and tracer is None:
            tracer = _otel_trace.get_tracer(__name__)
        self._tracer = tracer

    @property
    def recording(self) -> bool:
        """Return whether this span is actually recording (``False`` for no-op)."""
        return self._span is not None and not self._ended

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        """Expose span operation failures while swallowing them (observation must not kill the loop)."""
        warnings.warn(
            f"OTel span {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )

    def start(self) -> "LoopSpan":
        """Start the span and attach immutable GenAI attributes. Do nothing for no-op.

        Best-effort so tracer exceptions do not kill the loop. If start fails,
        subsequent operations become no-op.
        """
        if not self._enabled or self._span is not None:
            return self
        try:
            self._span = self._tracer.start_span(self._span_name)
            self._span.set_attribute(GEN_AI_OPERATION_NAME, OPERATION_NAME)
            self._span.set_attribute(GEN_AI_SYSTEM, SYSTEM_NAME)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._span = None  # Drop the partial span and fall back to no-op.
            self._warn("start", exc)
        return self

    def add_step(
        self,
        *,
        iteration: int,
        tokens: int,
        tokens_used: int,
        elapsed: float,
        goal_met: bool,
        detail: str = "",
    ) -> None:
        """Record one iteration on the timeline as a span add_event (``loop_step``).

        ``add_step`` is called on the driver's hot on_step path, so tracer
        exceptions are swallowed best-effort instead of propagating to the loop.
        """
        if not self.recording:
            return
        try:
            self._span.add_event(
                "loop_step",
                attributes={
                    "iteration": iteration,
                    "tokens": tokens,
                    "tokens_used": tokens_used,
                    "elapsed": elapsed,
                    "goal_met": goal_met,
                    "detail": detail,
                },
            )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("add_step", exc)

    def end(
        self,
        *,
        status: str,
        reason: str,
        iterations: int,
        tokens_used: int,
        elapsed: float,
        stop: Optional[str] = None,
        error: "Optional[BaseException]" = None,
    ) -> None:
        """Attach the termination reason and metrics to gen_ai.* / loop_agent.* and close the span.

        If ``status="error"`` or ``error`` is passed, set the span status to
        ERROR and record the exception. goal_met / stopped are treated as normal
        completion and marked OK. Duplicate end calls are ignored. This is
        best-effort so tracer exceptions do not kill the loop; even on failure it
        still attempts to reach span.end() (to avoid span leaks) and reliably
        moves to the ended state.
        """
        if not self.recording:
            self._ended = True
            return
        span = self._span
        try:
            span.set_attribute(ATTR_STATUS, status)
            span.set_attribute(ATTR_ITERATIONS, iterations)
            span.set_attribute(ATTR_TERMINATION_REASON, reason)
            span.set_attribute(ATTR_TOKENS_USED, tokens_used)
            span.set_attribute(ATTR_ELAPSED, elapsed)
            # Map cumulative tokens to GenAI usage as well for dashboard compatibility.
            span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, tokens_used)
            if stop is not None:
                span.set_attribute(ATTR_STOP, stop)
            if error is not None:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            elif status == "error":
                span.set_status(Status(StatusCode.ERROR, reason))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("end", exc)
        finally:
            # Always close the span even if setting attributes fails (leak prevention).
            try:
                span.end()
            except Exception as exc:  # noqa: BLE001 - observation is best-effort
                self._warn("end", exc)
            self._ended = True


class ReflexionSpan:
    """Thin wrapper around the OTel span for one outer Reflexion run. No-op when OTel is absent.

    Follows the same lifecycle contract as the inner :class:`LoopSpan`
    (start/.../end + best-effort degradation). The only difference is that the
    recorded timeline contains **episodes / epoch boundaries / lesson admission
    decisions** rather than **iterations**. The span lifecycle is managed by
    :class:`~loop_agent.reflexion_observe.ReflexionObserver`:

    - :meth:`start` starts the span and attaches run-invariant GenAI attributes
      plus configuration (declared_keys/epoch_len/epsilon).
    - :meth:`add_episode` records one episode as an ``episode`` event (with
      attributes for epoch number, evaluator version = grader id, primary
      aggregate / reward, lesson admission decision / provenance).
    - :meth:`add_epoch` records one epoch boundary as an ``epoch_boundary`` event
      (evaluator promotion/rejection and version transition).
    - :meth:`end` attaches the outer-loop termination reason plus aggregates to
      ``loop_agent.reflexion.*`` and closes the span.

    If OTel is not installed or ``enabled=False``, all methods safely do nothing
    (:attr:`recording` returns ``False``). tracer/span exceptions are swallowed
    best-effort and do not kill the outer loop (observation must not kill the loop).
    """

    def __init__(
        self,
        *,
        tracer: "Optional[Any]" = None,
        enabled: bool = True,
        span_name: str = REFLEXION_SPAN_NAME,
    ) -> None:
        self._span_name = span_name
        self._span: "Optional[Any]" = None
        self._ended = False
        self._enabled = bool(enabled) and _OTEL_AVAILABLE
        if self._enabled and tracer is None:
            tracer = _otel_trace.get_tracer(__name__)
        self._tracer = tracer

    @property
    def recording(self) -> bool:
        """Return whether this span is actually recording (``False`` for no-op)."""
        return self._span is not None and not self._ended

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        warnings.warn(
            f"OTel reflexion span {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )

    def start(
        self,
        *,
        declared_keys: "tuple[str, ...]" = (),
        evaluator_version: str = "",
        epoch_len: Optional[int] = None,
        epsilon: Optional[float] = None,
    ) -> "ReflexionSpan":
        """Start the span and attach immutable GenAI attributes plus configuration. Do nothing for no-op."""
        if not self._enabled or self._span is not None:
            return self
        try:
            self._span = self._tracer.start_span(self._span_name)
            self._span.set_attribute(GEN_AI_OPERATION_NAME, REFLEXION_OPERATION_NAME)
            self._span.set_attribute(GEN_AI_SYSTEM, SYSTEM_NAME)
            if declared_keys:
                # OTel attribute values only allow scalar sequences. Store declared axes as an array attribute.
                self._span.set_attribute(
                    ATTR_REFLEXION_DECLARED_KEYS, list(declared_keys)
                )
            if evaluator_version:
                self._span.set_attribute(
                    ATTR_REFLEXION_EVALUATOR_VERSION, evaluator_version
                )
            if epoch_len is not None:
                self._span.set_attribute(ATTR_REFLEXION_EPOCH_LEN, epoch_len)
            if epsilon is not None:
                self._span.set_attribute(ATTR_REFLEXION_EPSILON, epsilon)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._span = None  # Drop the partial span and fall back to no-op.
            self._warn("start", exc)
        return self

    def add_episode_begin(self, *, episode: int, epoch: int, evaluator_version: str) -> None:
        """Record episode start as an ``episode_begin`` event."""
        if not self.recording:
            return
        try:
            self._span.add_event(
                "episode_begin",
                attributes={
                    "episode": episode,
                    "epoch": epoch,
                    "evaluator_version": evaluator_version,
                },
            )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("add_episode_begin", exc)

    def add_episode(
        self,
        *,
        episode: int,
        epoch: int,
        evaluator_version: str,
        gt_aggregate: float,
        reward: float,
        succeeded: bool,
        ground_truth_backed: bool,
        best_gt_aggregate: float,
        lesson_admitted: bool,
        lesson_provenance: str = "",
        detail: str = "",
    ) -> None:
        """Record one episode on the span timeline as an ``episode`` event."""
        if not self.recording:
            return
        attributes: "dict[str, Any]" = {
            "episode": episode,
            "epoch": epoch,
            "evaluator_version": evaluator_version,
            "gt_aggregate": gt_aggregate,
            "reward": reward,
            "succeeded": succeeded,
            "ground_truth_backed": ground_truth_backed,
            "lesson_admitted": lesson_admitted,
            "lesson_provenance": lesson_provenance,
            "detail": detail,
        }
        # Do not attach best when it is -inf (no ground-truth-backed episode has arrived),
        # avoiding -inf in OTel. This follows the same convention as the run-end end() guard.
        if best_gt_aggregate != float("-inf"):
            attributes["best_gt_aggregate"] = best_gt_aggregate
        try:
            self._span.add_event("episode", attributes=attributes)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("add_episode", exc)

    def add_lesson(
        self,
        *,
        episode: int,
        admitted: bool,
        provenance: str = "",
        support: float = 0.0,
        reason: str = "",
    ) -> None:
        """Record lesson admission/rejection as a ``lesson_decision`` event."""
        if not self.recording:
            return
        try:
            self._span.add_event(
                "lesson_decision",
                attributes={
                    "episode": episode,
                    "admitted": admitted,
                    "provenance": provenance,
                    "support": support,
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("add_lesson", exc)

    def add_epoch(
        self,
        *,
        epoch: int,
        boundary_episode: int,
        decision: str,
        previous_version: str,
        evaluator_version: str,
        incumbent_agreement: Optional[float] = None,
        candidate_agreement: Optional[float] = None,
    ) -> None:
        """Record one epoch boundary as an ``epoch_boundary`` event (evaluator promotion/rejection)."""
        if not self.recording:
            return
        attributes: "dict[str, Any]" = {
            "epoch": epoch,
            "boundary_episode": boundary_episode,
            "evaluator_decision": decision,
            "previous_version": previous_version,
            "evaluator_version": evaluator_version,
        }
        if incumbent_agreement is not None:
            attributes["incumbent_agreement"] = incumbent_agreement
        if candidate_agreement is not None:
            attributes["candidate_agreement"] = candidate_agreement
        try:
            self._span.add_event("epoch_boundary", attributes=attributes)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("add_epoch", exc)

    def end(
        self,
        *,
        status: str,
        reason: str,
        episodes: int,
        epochs: int,
        best_gt_aggregate: float,
        reflections: int,
        evaluator_updates: int,
        evaluator_version: str,
        stop: Optional[str] = None,
        error: "Optional[BaseException]" = None,
    ) -> None:
        """Attach the outer-loop termination reason plus aggregates to ``loop_agent.reflexion.*`` and close the span.

        If ``status="error"`` or ``error`` is passed, set the span status to
        ERROR and record the exception. ``converged`` / ``stopped`` / ``paused``
        are treated as normal completion and marked OK. Duplicate end calls are
        ignored. Even if setting attributes fails, still attempt to reach
        span.end() (leak prevention) and reliably move to ended.
        """
        if not self.recording:
            self._ended = True
            return
        span = self._span
        try:
            span.set_attribute(ATTR_REFLEXION_STATUS, status)
            span.set_attribute(ATTR_REFLEXION_REASON, reason)
            span.set_attribute(ATTR_REFLEXION_EPISODES, episodes)
            span.set_attribute(ATTR_REFLEXION_EPOCHS, epochs)
            # Do not attach best when it is -inf (there are no ground-truth-backed
            # episodes), avoiding -inf in OTel and preserving dashboard-side numeric aggregation.
            if best_gt_aggregate != float("-inf"):
                span.set_attribute(ATTR_REFLEXION_BEST, best_gt_aggregate)
            span.set_attribute(ATTR_REFLEXION_REFLECTIONS, reflections)
            span.set_attribute(ATTR_REFLEXION_EVALUATOR_UPDATES, evaluator_updates)
            if evaluator_version:
                span.set_attribute(
                    ATTR_REFLEXION_EVALUATOR_VERSION, evaluator_version
                )
            if stop is not None:
                span.set_attribute(ATTR_REFLEXION_STOP, stop)
            if error is not None:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            elif status == "error":
                span.set_status(Status(StatusCode.ERROR, reason))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            self._warn("end", exc)
        finally:
            try:
                span.end()
            except Exception as exc:  # noqa: BLE001 - observation is best-effort
                self._warn("end", exc)
            self._ended = True
