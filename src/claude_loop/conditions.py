"""Composable stop conditions (report.md S4.4 / S4.5, R2+R3).

Each condition is a small object with a ``check(state) -> reason | None``
contract. ``AnyOf`` evaluates a set of them with OR semantics and reports the
first one that fired together with a human-readable reason, so termination is
always a *control output* (which condition, and why) rather than an exception.

Two families of conditions share the one ``check(state)`` protocol and compose
freely in ``AnyOf``:

- *mechanical hard caps* -- MaxIterations, TokenBudget, Timeout -- bound the run
  regardless of what the agent is doing (R3: never loop unboundedly), and
- *semantic conditions* -- GoalMet, NoProgress -- end the run because of what the
  agent achieved (the goal is verified) or failed to achieve (it is stuck).

Together these give the loop a *dual* termination contract (report.md S4.5): a
run ends the moment the goal is verified, the moment it is provably stuck, or
the moment a hard cap is hit -- whichever comes first. A semantic stop is still
reported as a :class:`StopTrigger`; the trigger ``name`` ("goal_met" /
"no_progress") is what distinguishes a successful finish from an abort from a
mechanical cut-off. Any object satisfying the ``StopCondition`` protocol drops
into ``AnyOf`` with no engine changes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Optional, Protocol, Union, runtime_checkable

from .state import LoopState, StepRecord


@dataclass(frozen=True)
class StopTrigger:
    """The verdict for a fired stop condition: which one, and why."""

    name: str
    reason: str


@runtime_checkable
class StopCondition(Protocol):
    """A mechanical or semantic limit evaluated once per iteration.

    Implementations return ``None`` when not triggered, or a short
    human-readable reason string when they are. The reason is surfaced verbatim
    in :class:`StopTrigger.reason`, so write it for a human reading a log.
    """

    name: str

    def check(self, state: LoopState) -> Optional[str]:
        ...


@dataclass(frozen=True)
class MaxIterations:
    """Stop once ``limit`` gather->act->verify cycles have completed."""

    limit: int
    name: ClassVar[str] = "max_iterations"

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("MaxIterations limit must be >= 0")

    def check(self, state: LoopState) -> Optional[str]:
        if state.iteration >= self.limit:
            return f"reached max iterations ({state.iteration}/{self.limit})"
        return None


@dataclass(frozen=True)
class TokenBudget:
    """Stop once cumulative reported tokens reach ``budget``.

    Evaluated at the iteration boundary, so a single step may carry the running
    total past the budget before the loop notices on the next cycle; tokens are
    already spent and cannot be un-spent mid-step. The cap therefore means
    "do not *start* new work once exhausted", which matches the while-guard
    design in report.md S4.4.
    """

    budget: int
    name: ClassVar[str] = "token_budget"

    def __post_init__(self) -> None:
        if self.budget < 0:
            raise ValueError("TokenBudget budget must be >= 0")

    def check(self, state: LoopState) -> Optional[str]:
        if state.tokens_used >= self.budget:
            return f"token budget exhausted ({state.tokens_used}/{self.budget})"
        return None


@dataclass(frozen=True)
class Timeout:
    """Stop once ``state.elapsed`` reaches ``seconds`` (wall-clock cap).

    Like :class:`TokenBudget`, this is evaluated at the iteration boundary and
    an in-progress step is never interrupted, so a single long step can carry
    the elapsed time past the deadline before the loop notices on the next
    cycle. The cap therefore means "do not *start* new work past the deadline".
    """

    seconds: float
    name: ClassVar[str] = "timeout"

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise ValueError("Timeout seconds must be >= 0")

    def check(self, state: LoopState) -> Optional[str]:
        if state.elapsed >= self.seconds:
            return f"timed out ({state.elapsed:.3f}s/{self.seconds:g}s)"
        return None


@dataclass(frozen=True)
class GoalCheck:
    """Result of a :class:`GoalMet` verifier: was the goal met, and why.

    A verifier may return a bare ``bool`` (when no explanation is needed) or a
    ``GoalCheck`` to attach a ``detail`` such as ``"42 passed, 0 failed"`` that
    is surfaced in the stop reason.
    """

    met: bool
    detail: str = ""


GoalVerifier = Callable[[LoopState], Union[bool, GoalCheck]]


@dataclass(frozen=True)
class GoalMet:
    """Stop *successfully* once a verifiable goal predicate holds (R1, S4.5).

    ``verifier`` is the semantic counterpart of a hard cap: a callable that
    answers "is the goal actually achieved?" by running a ground-truth check --
    a test suite, a linter, a rubric -- against the current :class:`LoopState`.
    It signals "met" by returning ``True`` or ``GoalCheck(met=True, ...)``, and
    "not met" by returning a falsy value (e.g. ``False``) or, when it still wants
    to attach a detail, ``GoalCheck(met=False, ...)`` -- ``met`` is read off a
    ``GoalCheck`` directly, so the object's own truthiness never matters. The
    state argument lets the check inspect the latest step
    (``state.history[-1]``) or accumulated progress; verifiers that ignore it
    (``lambda _state: run_tests()``) are equally valid.

    When the goal is met this fires like any other condition, so the loop ends
    via the same :class:`AnyOf` seam as the mechanical caps -- but the
    ``"goal_met"`` trigger name marks it as a *success*, not a cut-off. A
    verifier raising an exception is left to propagate: a check that cannot run
    is not the same as a goal that is unmet, and silently swallowing it would
    let a broken verifier masquerade as "never done" until a hard cap fires.
    """

    verifier: GoalVerifier
    name: ClassVar[str] = "goal_met"

    def check(self, state: LoopState) -> Optional[str]:
        result = self.verifier(state)
        if isinstance(result, GoalCheck):
            met, detail = result.met, result.detail
        else:
            met, detail = bool(result), ""
        if met:
            return f"goal verified: {detail}" if detail else "goal verified"
        return None


@dataclass(frozen=True)
class NoProgress:
    """Stop once the loop is stuck: an action recurs without making progress.

    Looks at the trailing ``window`` step records and keys each one with ``key``
    (the step's ``observation`` by default). If any single key occurs at least
    ``repeat`` times within that window, the loop is judged to be thrashing --
    repeating an action that is not advancing the goal -- and is cut off with a
    ``"no_progress"`` trigger (S4.5). Unlike :class:`GoalMet`, this is an
    *abort*: termination without success.

    ``window`` bounds the look-back so that stale repeats age out (an action
    seen ``repeat`` times long ago, then abandoned, should not strand the loop);
    ``repeat`` sets the sensitivity. Counting is by frequency within the window,
    not strict adjacency, so oscillation (``A B A B A``) is caught as readily as
    a literal run (``A A A``). The default ``key`` requires observations to be
    hashable; pass a ``key`` projecting each record onto a hashable signature
    when they are not.
    """

    window: int
    repeat: int
    key: Callable[[StepRecord], Any] = lambda record: record.observation
    name: ClassVar[str] = "no_progress"

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("NoProgress window must be >= 1")
        if self.repeat < 1:
            raise ValueError("NoProgress repeat must be >= 1")
        if self.repeat > self.window:
            # max count within the window is `window`; repeat > window can never
            # fire, which is a silent mis-config -- reject it like a bad cap.
            raise ValueError("NoProgress repeat must be <= window")

    def check(self, state: LoopState) -> Optional[str]:
        recent = state.history[-self.window :]
        if len(recent) < self.repeat:
            return None
        counts = Counter(self.key(record) for record in recent)
        action, count = counts.most_common(1)[0]
        if count >= self.repeat:
            return (
                f"no progress: action {action!r} repeated {count} times "
                f"within last {len(recent)} steps "
                f"(>= repeat {self.repeat})"
            )
        return None


@dataclass(frozen=True)
class AnyOf:
    """OR-combine stop conditions; report the first that fires (R2).

    Accepts any iterable of conditions and normalises it to a tuple. At least
    one condition is required: a loop with no hard cap and a goal that is never
    met would never terminate (R3 -- guard against accidental unbounded loops).
    """

    conditions: tuple[StopCondition, ...]

    def __post_init__(self) -> None:
        conds = tuple(self.conditions)
        if not conds:
            raise ValueError("AnyOf requires at least one stop condition")
        object.__setattr__(self, "conditions", conds)

    def first_triggered(self, state: LoopState) -> Optional[StopTrigger]:
        for condition in self.conditions:
            reason = condition.check(state)
            if reason is not None:
                return StopTrigger(name=condition.name, reason=reason)
        return None
