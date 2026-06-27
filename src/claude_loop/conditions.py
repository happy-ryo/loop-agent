"""Composable stop conditions (report.md S4.4 / S4.5, R2+R3).

Each condition is a small object with a ``check(state) -> reason | None``
contract. ``AnyOf`` evaluates a set of them with OR semantics and reports the
first one that fired together with a human-readable reason, so termination is
always a *control output* (which condition, and why) rather than an exception.

The PoC ships the three mechanical hard caps from Phase 1 -- MaxIterations,
TokenBudget, Timeout. Semantic conditions (GoalMet / NoProgress) are handled by
the ``verify`` hook in the driver for now and become first-class condition
objects in Phase 2; any object satisfying the ``StopCondition`` protocol can be
dropped into ``AnyOf`` without engine changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional, Protocol, runtime_checkable

from .state import LoopState


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
