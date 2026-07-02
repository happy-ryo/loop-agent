"""Convergence/stop conditions for the outer Reflexion loop (report.md S2.6 / S4.5 / Issue #22).

This follows the **same composition protocol** as the inner-loop
:mod:`loop_agent.conditions` (``name`` + ``check(state) -> reason | None``), and
reuses :class:`~loop_agent.conditions.AnyOf` /
:class:`~loop_agent.conditions.StopTrigger` **as-is**. The only difference is
that check observes :class:`OuterState` (episode/epoch granularity).

The convergence decision has the three pillars from report.md S2.6
(AWS evaluator reflect-refine): **rubric threshold exceeded**
(:class:`RubricThreshold`) / **improvement plateau** (:class:`ScorePlateau`) /
**iteration limit** (:class:`MaxEpisodes`). It also adds a **reflection budget**
(:class:`ReflectionBudget`) and an **evaluator update budget**
(:class:`EvaluatorUpdateBudget`) to curb self-improving traps (report.md S6).

**Most important safety design**: the ``gt_aggregate_history`` /
``best_gt_aggregate`` observed by these conditions are all **ground-truth
primary signals** (from inner verify), and they **do not depend** on the output
(reward) of the rubric evaluator fixed within an epoch. Therefore, there is no
structural path for "inflating a gameable evaluator scalar and declaring
convergence" (report.md principle: ground-truth first). Episodes with
``ground_truth_backed=False`` are not pushed into ``gt_aggregate_history`` by
the driver, so episodes without real signals are excluded from convergence and
plateau decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional

from .errors import ConfigError


@dataclass(frozen=True)
class OuterState:
    """Cumulative state for the outer loop (projection evaluated each episode by convergence conditions).

    - ``episode``               : Number of completed episodes (all episodes; observed by MaxEpisodes).
    - ``epoch``                 : Current epoch number (advanced only at boundaries).
    - ``evaluator_version``     : Version of the current (fixed) evaluator.
    - ``gt_aggregate_history``  : Aggregate values for **ground_truth_backed** episodes (primary signal).
    - ``best_gt_aggregate``     : Best aggregate value so far (basis for plateau/success decisions).
    - ``reflections``           : Cumulative lessons incorporated into memory (bloat budget).
    - ``evaluator_updates``     : Cumulative boundaries where evaluator promotion was attempted (overfit budget).
    - ``declared_keys``         : Declared axes for diverse evaluation (for audit/context).
    """

    episode: int = 0
    epoch: int = 0
    evaluator_version: str = ""
    gt_aggregate_history: tuple[float, ...] = ()
    best_gt_aggregate: float = float("-inf")
    reflections: int = 0
    evaluator_updates: int = 0
    declared_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaxEpisodes:
    """Hard limit for the outer loop (report.md R3: last line of defense against infinite loops)."""

    limit: int
    name: ClassVar[str] = "max_episodes"

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ConfigError("MaxEpisodes limit must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.episode >= self.limit:
            return f"reached max episodes ({state.episode}/{self.limit})"
        return None


@dataclass(frozen=True)
class RubricThreshold:
    """**Success** convergence: primary-signal aggregates meet or exceed ``target`` for ``sustain`` consecutive checks.

    Because this requires exceeding the threshold for ``sustain`` consecutive
    checks, it is not triggered by a one-off variance spike (resistant to
    variance gaming). This is a **success** condition (``success=True``), and is
    distinguished from hard-limit or plateau termination. The decision only
    observes the ground-truth primary ``gt_aggregate_history``.
    """

    target: float
    sustain: int = 1
    name: ClassVar[str] = "rubric_threshold"
    # Marker for success convergence (used by the driver to decide success independent of order).
    success: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if self.sustain < 1:
            raise ConfigError("RubricThreshold sustain must be >= 1")

    def check(self, state: OuterState) -> Optional[str]:
        recent = state.gt_aggregate_history[-self.sustain :]
        if len(recent) < self.sustain:
            return None
        if all(v >= self.target for v in recent):
            return (
                f"rubric threshold reached: last {self.sustain} ground-truth "
                f"aggregates all >= {self.target:g}"
            )
        return None


@dataclass(frozen=True)
class ScorePlateau:
    """**Plateau** termination: best-so-far improves by less than ``min_delta`` across ``window``.

    This observes the **trend** in best-so-far (max(now) - max(before window)),
    so it does not trigger while there is monotonic improvement, even if slow;
    it triggers when improvement has stopped (flat / sawtooth with no net gain).
    A range(max-min)-based check would wrongly terminate gentle real progress
    and fail to terminate sawtooth behavior forever (this avoids that). This is
    a non-success termination.

    The decision is "best-so-far growth across the window is **at most**
    ``min_delta``" (``<=``). With ``<``, because best-so-far is monotonically
    non-decreasing, growth is always ``>= 0`` and ``min_delta=0`` (= flat/sawtooth
    cases with zero net gain that should terminate) would become a no-op that
    never triggers. Using ``<=`` makes ``min_delta=0`` trigger only when there is
    no growth at all, and positive ``min_delta`` trigger when the specified
    minimum progress is not reached.
    """

    window: int
    min_delta: float
    name: ClassVar[str] = "score_plateau"

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ConfigError("ScorePlateau window must be >= 1")
        if self.min_delta < 0:
            raise ConfigError("ScorePlateau min_delta must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        history = state.gt_aggregate_history
        if len(history) <= self.window:
            return None
        best_now = max(history)
        best_past = max(history[: len(history) - self.window])
        if best_now - best_past <= self.min_delta:
            return (
                f"no progress: best ground-truth aggregate improved by "
                f"{best_now - best_past:.4f} over last {self.window} episodes "
                f"(<= min_delta {self.min_delta:g})"
            )
        return None


@dataclass(frozen=True)
class ReflectionBudget:
    """Cumulative cap on reflections (incorporated lessons) (report.md S6: prevents reflection output bloat/decay)."""

    max_reflections: int
    name: ClassVar[str] = "reflection_budget"

    def __post_init__(self) -> None:
        if self.max_reflections < 0:
            raise ConfigError("ReflectionBudget max_reflections must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.reflections >= self.max_reflections:
            return (
                f"reflection budget exhausted "
                f"({state.reflections}/{self.max_reflections})"
            )
        return None


@dataclass(frozen=True)
class EvaluatorUpdateBudget:
    """Cumulative cap on evaluator promotion attempts (budget to curb adaptive overfit to held-out data)."""

    max_updates: int
    name: ClassVar[str] = "evaluator_update_budget"

    def __post_init__(self) -> None:
        if self.max_updates < 0:
            raise ConfigError("EvaluatorUpdateBudget max_updates must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.evaluator_updates >= self.max_updates:
            return (
                f"evaluator update budget exhausted "
                f"({state.evaluator_updates}/{self.max_updates})"
            )
        return None


def is_success_condition(condition: object) -> bool:
    """Return whether the condition represents **success** convergence (has ``success=True``)."""
    return bool(getattr(condition, "success", False))


__all__ = [
    "OuterState",
    "MaxEpisodes",
    "RubricThreshold",
    "ScorePlateau",
    "ReflectionBudget",
    "EvaluatorUpdateBudget",
    "is_success_condition",
]
