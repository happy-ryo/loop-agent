"""Outer Reflexion loop convergence/stop conditions (report.md S2.6 / S4.5 / Issue #22).

Reuses the inner loop's :mod:`loop_agent.conditions` **same composition protocol** (``name`` +
``check(state) -> reason | None``), and :class:`~loop_agent.conditions.AnyOf` /
:class:`~loop_agent.conditions.StopTrigger` are **reused as-is**. The only difference is that
check examines :class:`OuterState` (at episode/epoch granularity).

Convergence is determined by three pillars from report.md S2.6 (AWS evaluator reflect-refine):
**rubric threshold exceedance** (:class:`RubricThreshold`) / **improvement plateau**
(:class:`ScorePlateau`) / **iteration limit** (:class:`MaxEpisodes`). Additionally, we add
safeguards against self-improvement pitfalls (report.md S6): **reflection budget**
(:class:`ReflectionBudget`) and **evaluator update budget** (:class:`EvaluatorUpdateBudget`).

**Critical safety design**: The ``gt_aggregate_history`` / ``best_gt_aggregate`` that these
examine are all **ground-truth primary signals** (derived from inner verify) and are independent
from the output (reward) of the rubric evaluator that is fixed within an epoch. Thus, there is
no structural loophole to declare convergence by gaming the evaluator scalar (report.md
principle: ground-truth first). Episodes with ``ground_truth_backed=False`` are not added to
``gt_aggregate_history`` by the driver, so episodes without real signal are excluded from
convergence/plateau detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional


@dataclass(frozen=True)
class OuterState:
    """Cumulative state of the outer loop (projection evaluated by convergence conditions at each episode).

    - ``episode``               : Number of completed episodes (all episodes. Observed by MaxEpisodes).
    - ``epoch``                 : Current epoch number (advances only at boundaries).
    - ``evaluator_version``     : Version of the current (fixed) evaluator.
    - ``gt_aggregate_history``  : Sequence of aggregated values from **ground_truth_backed** episodes (primary signal).
    - ``best_gt_aggregate``     : Best aggregated value so far (criterion for plateau/success detection).
    - ``reflections``           : Cumulative lessons incorporated into memory (bloat budget).
    - ``evaluator_updates``     : Cumulative evaluator promotion attempts at boundaries (overfit budget).
    - ``declared_keys``         : Declared axes for diverse evaluation (for audit and context).
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
            raise ValueError("MaxEpisodes limit must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.episode >= self.limit:
            return f"reached max episodes ({state.episode}/{self.limit})"
        return None


@dataclass(frozen=True)
class RubricThreshold:
    """**Success** convergence: Aggregated value of primary signal meets or exceeds ``target`` for ``sustain`` consecutive episodes.

    By requiring it to exceed ``sustain`` consecutive times, it does not trigger on single spikes
    due to variance (variance gaming resistance). This is a **success** condition (``success=True``),
    distinguished from hard limits or plateau termination. Detection looks only at the ground-truth
    primary ``gt_aggregate_history``.
    """

    target: float
    sustain: int = 1
    name: ClassVar[str] = "rubric_threshold"
    # Marker indicating success convergence (used by driver to determine outcome order-independently).
    success: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if self.sustain < 1:
            raise ValueError("RubricThreshold sustain must be >= 1")

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
    """**Plateau** termination: best-so-far grows less than ``min_delta`` over ``window`` episodes.

    By examining the **trend** of best-so-far (max(now) - max(before window)), even if it
    improves monotonically, it does not trigger; it triggers when improvement stops (flat /
    sawtooth with no net gain). Using range(max-min) would incorrectly terminate gradual progress
    and never terminate sawtooth, which we avoid. This is termination without success.

    The criterion is "best-so-far growth over the window interval is **at most** ``min_delta``"
    (``<=``). If using ``<``, then since best-so-far is monotonically non-decreasing, growth is
    always ``>= 0``, and ``min_delta=0`` (= net gain of zero for desired flat/sawtooth termination)
    becomes a no-op that never triggers. Using ``<=`` instead makes ``min_delta=0`` trigger only
    when there is zero growth, and positive ``min_delta`` trigger when the required minimum
    progress is not met.
    """

    window: int
    min_delta: float
    name: ClassVar[str] = "score_plateau"

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("ScorePlateau window must be >= 1")
        if self.min_delta < 0:
            raise ValueError("ScorePlateau min_delta must be >= 0")

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
    """Upper limit on cumulative reflections (incorporated lessons) (report.md S6: prevents reflection output bloat and degradation)."""

    max_reflections: int
    name: ClassVar[str] = "reflection_budget"

    def __post_init__(self) -> None:
        if self.max_reflections < 0:
            raise ValueError("ReflectionBudget max_reflections must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.reflections >= self.max_reflections:
            return (
                f"reflection budget exhausted "
                f"({state.reflections}/{self.max_reflections})"
            )
        return None


@dataclass(frozen=True)
class EvaluatorUpdateBudget:
    """Cumulative upper limit on evaluator promotion attempts (budget to prevent adaptive overfitting to held-out data)."""

    max_updates: int
    name: ClassVar[str] = "evaluator_update_budget"

    def __post_init__(self) -> None:
        if self.max_updates < 0:
            raise ValueError("EvaluatorUpdateBudget max_updates must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.evaluator_updates >= self.max_updates:
            return (
                f"evaluator update budget exhausted "
                f"({state.evaluator_updates}/{self.max_updates})"
            )
        return None


def is_success_condition(condition: object) -> bool:
    """Whether the condition represents **success** convergence (has ``success=True``)."""
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
