"""Unit tests for outer convergence conditions (Issue #22: early stop / thresholds / AnyOf reuse)."""

from __future__ import annotations

import pytest

from loop_agent import AnyOf
from loop_agent.convergence import (
    EvaluatorUpdateBudget,
    MaxEpisodes,
    OuterState,
    ReflectionBudget,
    RubricThreshold,
    ScorePlateau,
    is_success_condition,
)


def _state(**kw) -> OuterState:
    return OuterState(**kw)


# -- MaxEpisodes ----------------------------------------------------------------


def test_max_episodes_fires_at_limit():
    cond = MaxEpisodes(3)
    assert cond.check(_state(episode=2)) is None
    assert cond.check(_state(episode=3)) is not None


# -- RubricThreshold: sustained success ----------------------------------------


def test_rubric_threshold_requires_sustain():
    cond = RubricThreshold(target=0.8, sustain=2)
    # A single spike does not trigger it (resistant to variance gaming).
    assert cond.check(_state(gt_aggregate_history=(0.9, 0.2))) is None
    # It triggers when the latest 2 consecutive values are at or above target.
    assert cond.check(_state(gt_aggregate_history=(0.7, 0.9, 0.85))) is not None


def test_rubric_threshold_default_sustain_one():
    cond = RubricThreshold(target=0.8)
    assert cond.check(_state(gt_aggregate_history=(0.85,))) is not None
    assert cond.check(_state(gt_aggregate_history=(0.5,))) is None


def test_rubric_threshold_is_success_condition():
    assert is_success_condition(RubricThreshold(0.8)) is True
    assert is_success_condition(MaxEpisodes(3)) is False


# -- ScorePlateau: best-so-far trend, not range --------------------------------


def test_plateau_does_not_fire_on_slow_monotone_progress():
    cond = ScorePlateau(window=2, min_delta=0.005)
    # Do not stop while improvement is monotonic, even if slow.
    assert cond.check(_state(gt_aggregate_history=(0.70, 0.71, 0.72))) is None


def test_plateau_fires_on_sawtooth_with_no_net_best_gain():
    cond = ScorePlateau(window=2, min_delta=0.005)
    # Stop when best-so-far does not improve (zero net gain with a sawtooth pattern).
    assert cond.check(_state(gt_aggregate_history=(0.2, 0.9, 0.2, 0.9))) is not None


def test_plateau_quiet_until_window_filled():
    cond = ScorePlateau(window=3, min_delta=0.005)
    assert cond.check(_state(gt_aggregate_history=(0.5, 0.5))) is None


def test_plateau_zero_delta_fires_on_flat_history():
    """min_delta=0 triggers on zero net gain (it does not become a no-op)."""
    cond = ScorePlateau(window=2, min_delta=0.0)
    assert cond.check(_state(gt_aggregate_history=(0.5, 0.5, 0.5))) is not None
    # It does not trigger if there is even a small improvement.
    assert cond.check(_state(gt_aggregate_history=(0.5, 0.6, 0.7))) is None


# -- Budget conditions ----------------------------------------------------------


def test_reflection_budget_caps_lessons():
    cond = ReflectionBudget(5)
    assert cond.check(_state(reflections=4)) is None
    assert cond.check(_state(reflections=5)) is not None


def test_evaluator_update_budget_caps_promotions():
    cond = EvaluatorUpdateBudget(2)
    assert cond.check(_state(evaluator_updates=1)) is None
    assert cond.check(_state(evaluator_updates=2)) is not None


# -- AnyOf reuse (same composition protocol as the inner loop) ------------------


def test_anyof_composes_outer_conditions_over_outer_state():
    stop = AnyOf([RubricThreshold(0.8, sustain=1), MaxEpisodes(10)])
    trig = stop.first_triggered(_state(gt_aggregate_history=(0.9,), episode=1))
    assert trig is not None and trig.name == "rubric_threshold"
    trig2 = stop.first_triggered(_state(gt_aggregate_history=(0.1,), episode=10))
    assert trig2 is not None and trig2.name == "max_episodes"
    assert stop.first_triggered(_state(gt_aggregate_history=(0.1,), episode=2)) is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MaxEpisodes(-1),
        lambda: RubricThreshold(0.8, sustain=0),
        lambda: ScorePlateau(window=0, min_delta=0.1),
        lambda: ScorePlateau(window=2, min_delta=-0.1),
        lambda: ReflectionBudget(-1),
        lambda: EvaluatorUpdateBudget(-1),
    ],
)
def test_invalid_params_rejected(factory):
    with pytest.raises(ValueError):
        factory()
