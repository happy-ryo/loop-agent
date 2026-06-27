"""Unit tests for the stop-condition objects in isolation."""

from __future__ import annotations

import pytest

from claude_loop import (
    AnyOf,
    GoalCheck,
    GoalMet,
    MaxIterations,
    NoProgress,
    Timeout,
    TokenBudget,
)
from claude_loop.state import LoopState, StepRecord


def _history(*observations) -> list[StepRecord]:
    """Build a history of step records carrying the given observations."""
    return [
        StepRecord(iteration=i, observation=obs, tokens=0, goal_met=False)
        for i, obs in enumerate(observations)
    ]


def test_max_iterations_fires_at_limit():
    cond = MaxIterations(3)
    assert cond.check(LoopState(iteration=2)) is None
    assert cond.check(LoopState(iteration=3)) is not None
    assert "3/3" in cond.check(LoopState(iteration=3))


def test_token_budget_fires_at_or_past_budget():
    cond = TokenBudget(100)
    assert cond.check(LoopState(tokens_used=99)) is None
    assert "100/100" in cond.check(LoopState(tokens_used=100))
    # overshoot is still reported (tokens already spent)
    assert "150/100" in cond.check(LoopState(tokens_used=150))


def test_timeout_fires_at_or_past_deadline():
    cond = Timeout(5.0)
    assert cond.check(LoopState(elapsed=4.999)) is None
    reason = cond.check(LoopState(elapsed=5.0))
    assert "5.000s/5s" in reason


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MaxIterations(-1),
        lambda: TokenBudget(-1),
        lambda: Timeout(-0.5),
    ],
)
def test_negative_limits_rejected(factory):
    with pytest.raises(ValueError):
        factory()


def test_anyof_requires_at_least_one_condition():
    with pytest.raises(ValueError):
        AnyOf([])


def test_anyof_reports_first_triggered_in_order():
    # Both fire; AnyOf must report the first in declaration order.
    state = LoopState(iteration=10, tokens_used=10)
    combo = AnyOf([MaxIterations(5), TokenBudget(5)])
    trigger = combo.first_triggered(state)
    assert trigger is not None
    assert trigger.name == "max_iterations"


def test_anyof_returns_none_when_nothing_fires():
    combo = AnyOf([MaxIterations(5), TokenBudget(5)])
    assert combo.first_triggered(LoopState(iteration=0, tokens_used=0)) is None


# -- GoalMet (semantic success) --------------------------------------------


def test_goal_met_fires_when_verifier_true():
    cond = GoalMet(lambda state: True)
    assert cond.check(LoopState()) == "goal verified"


def test_goal_met_silent_when_verifier_false():
    cond = GoalMet(lambda state: False)
    assert cond.check(LoopState()) is None


def test_goal_met_surfaces_detail_from_goalcheck():
    cond = GoalMet(lambda state: GoalCheck(met=True, detail="42 passed, 0 failed"))
    assert cond.check(LoopState()) == "goal verified: 42 passed, 0 failed"


def test_goal_met_goalcheck_unmet_is_silent():
    cond = GoalMet(lambda state: GoalCheck(met=False, detail="3 failing"))
    assert cond.check(LoopState()) is None


def test_goal_met_coerces_truthy_non_bool():
    # A rubric callable may return a score / non-empty result rather than a bool.
    assert GoalMet(lambda state: ["ok"]).check(LoopState()) == "goal verified"
    assert GoalMet(lambda state: []).check(LoopState()) is None


def test_goal_met_verifier_receives_state():
    seen = []

    def verifier(state: LoopState) -> bool:
        seen.append(state.iteration)
        return state.iteration >= 5

    assert GoalMet(verifier).check(LoopState(iteration=4)) is None
    assert GoalMet(verifier).check(LoopState(iteration=5)) == "goal verified"
    assert seen == [4, 5]


def test_goal_met_verifier_exception_propagates():
    # A check that cannot run must not masquerade as "goal unmet".
    def boom(state: LoopState) -> bool:
        raise RuntimeError("test runner crashed")

    with pytest.raises(RuntimeError, match="test runner crashed"):
        GoalMet(boom).check(LoopState())


def test_goal_met_condition_name():
    assert GoalMet(lambda state: False).name == "goal_met"


# -- NoProgress (semantic abort) -------------------------------------------


def test_no_progress_fires_on_consecutive_repeats():
    cond = NoProgress(window=3, repeat=3)
    state = LoopState(history=_history("a", "a", "a"))
    reason = cond.check(state)
    assert reason is not None
    assert "'a'" in reason and "repeated 3 times" in reason


def test_no_progress_catches_oscillation_within_window():
    # A B A B A -- never 3-in-a-row, but 'a' occurs 3x within the window of 5.
    cond = NoProgress(window=5, repeat=3)
    state = LoopState(history=_history("a", "b", "a", "b", "a"))
    assert cond.check(state) is not None


def test_no_progress_silent_below_repeat_threshold():
    cond = NoProgress(window=5, repeat=3)
    assert cond.check(LoopState(history=_history("a", "a"))) is None
    assert cond.check(LoopState(history=_history("a", "b", "a", "c"))) is None


def test_no_progress_silent_with_too_few_records():
    cond = NoProgress(window=4, repeat=3)
    assert cond.check(LoopState(history=_history("a", "a"))) is None


def test_no_progress_window_ages_out_stale_repeats():
    # Three 'a's, but only the last `window` records count: recent work moved on.
    cond = NoProgress(window=3, repeat=3)
    state = LoopState(history=_history("a", "a", "a", "b", "c"))
    assert cond.check(state) is None


def test_no_progress_window_boundary_is_inclusive_exact():
    # Pin the slice boundary in both directions: the 3rd-from-end 'a' must be
    # excluded at window=3 (last-3 = [a,a,b] -> 2 < 3, silent) and included at
    # window=4 (last-4 = [a,a,a,b] -> 3 >= 3, fires). A +1 / -1 off-by-one in
    # `history[-window:]` flips exactly one of these, so both are asserted.
    history = _history("a", "a", "a", "b")
    assert NoProgress(window=3, repeat=3).check(LoopState(history=history)) is None
    assert NoProgress(window=4, repeat=3).check(LoopState(history=history)) is not None


def test_no_progress_honours_custom_key():
    # Key on a projected field so structurally-equivalent actions collapse.
    cond = NoProgress(window=3, repeat=3, key=lambda record: record.detail)
    history = [
        StepRecord(iteration=i, observation=i, tokens=0, goal_met=False, detail="same")
        for i in range(3)
    ]
    assert cond.check(LoopState(history=history)) is not None


def test_no_progress_default_key_requires_hashable_observations():
    # The default key returns the raw observation; an unhashable one (a list)
    # must surface as a TypeError rather than being silently swallowed -- this
    # is the documented contract that the custom-`key` escape hatch exists for.
    cond = NoProgress(window=2, repeat=2)
    state = LoopState(history=_history(["unhashable"], ["unhashable"]))
    with pytest.raises(TypeError):
        cond.check(state)


def test_no_progress_reports_window_actually_examined():
    cond = NoProgress(window=10, repeat=3)
    state = LoopState(history=_history("a", "a", "a"))
    # Only 3 records exist; the reason must report the examined count, not 10.
    assert "within last 3 steps" in cond.check(state)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: NoProgress(window=0, repeat=1),
        lambda: NoProgress(window=3, repeat=0),
        lambda: NoProgress(window=2, repeat=3),  # repeat > window can never fire
    ],
)
def test_no_progress_invalid_params_rejected(factory):
    with pytest.raises(ValueError):
        factory()


def test_no_progress_condition_name():
    assert NoProgress(window=2, repeat=2).name == "no_progress"


# -- dual-stop composition in AnyOf ----------------------------------------


def test_anyof_composes_semantic_and_mechanical_conditions():
    # GoalMet + NoProgress + a hard cap coexist; the first declared that fires
    # wins. Here the goal is met, declared ahead of the (also-firing) cap.
    state = LoopState(iteration=9, history=_history("a", "a", "a"))
    combo = AnyOf(
        [
            GoalMet(lambda s: s.iteration >= 5),
            NoProgress(window=3, repeat=3),
            MaxIterations(5),
        ]
    )
    trigger = combo.first_triggered(state)
    assert trigger is not None
    assert trigger.name == "goal_met"


def test_anyof_reports_no_progress_when_goal_unmet():
    state = LoopState(iteration=2, history=_history("x", "x", "x"))
    combo = AnyOf(
        [
            GoalMet(lambda s: False),
            NoProgress(window=3, repeat=3),
            MaxIterations(100),
        ]
    )
    trigger = combo.first_triggered(state)
    assert trigger is not None
    assert trigger.name == "no_progress"
