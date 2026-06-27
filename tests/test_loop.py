"""Driver-level tests: each cap firing, natural goal exit, reason discrimination."""

from __future__ import annotations

import pytest

from claude_loop import (
    ActOutcome,
    MaxIterations,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    run_loop,
)
from conftest import FakeClock, acting, done_after, never_done


# -- natural termination (goal met) ----------------------------------------


def test_goal_met_terminates_naturally():
    result = run_loop(
        act=acting(tokens=1),
        verify=done_after(3),
        conditions=[MaxIterations(100)],
    )
    assert result.goal_met is True
    assert result.status == "goal_met"
    assert result.stop is None
    assert result.reason == "goal met"
    assert result.iterations == 3
    assert result.history[-1].detail == "converged"


# -- each mechanical cap fires ---------------------------------------------


def test_max_iterations_cap_stops_loop():
    result = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(4)],
    )
    assert result.goal_met is False
    assert result.status == "stopped"
    assert result.stop.name == "max_iterations"
    assert result.iterations == 4


def test_token_budget_cap_stops_loop():
    # 30 tokens/step, budget 100 -> stops once cumulative reaches/passes 100.
    result = run_loop(
        act=acting(tokens=30),
        verify=never_done,
        conditions=[TokenBudget(100)],
    )
    assert result.stop.name == "token_budget"
    # 4 steps -> 120 tokens; checked at the 5th guard (boundary semantics).
    assert result.iterations == 4
    assert result.tokens_used == 120


def test_timeout_cap_stops_loop():
    clock = FakeClock(start=0.0, step=2.0)
    result = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[Timeout(5.0)],
        time_fn=clock,
    )
    assert result.stop.name == "timeout"
    # start consumes the 0.0 reading; guards then see elapsed 2, 4, 6 -> the
    # first >= 5 is 6 at the 3rd guard, after 2 completed steps.
    assert result.iterations == 2


# -- reason discrimination across composed caps ----------------------------


def test_first_declared_cap_wins_when_several_could_fire():
    # After one step both caps are exhausted (iteration 1 >= 1 and tokens
    # 1000 >= 1), so both fire on the same guard; MaxIterations is declared
    # first, so its reason must be the one reported (deterministic OR order).
    result = run_loop(
        act=acting(tokens=1000),
        verify=never_done,
        conditions=[MaxIterations(1), TokenBudget(1)],
    )
    assert result.stop.name == "max_iterations"
    assert "max iterations" in result.reason


def test_goal_beats_caps_on_the_same_iteration():
    # Goal is reached on iteration 3; MaxIterations(3) would fire on the guard
    # *before* a 4th step. Natural termination must win.
    result = run_loop(
        act=acting(tokens=0),
        verify=done_after(3),
        conditions=[MaxIterations(3)],
    )
    assert result.goal_met is True
    assert result.iterations == 3


# -- boundary / guard-before-first-step ------------------------------------


def test_zero_iteration_cap_stops_before_any_step():
    steps = []
    result = run_loop(
        act=acting(tokens=5),
        verify=never_done,
        conditions=[MaxIterations(0)],
        on_step=lambda record, state: steps.append(record),
    )
    assert result.status == "stopped"
    assert result.iterations == 0
    assert result.tokens_used == 0
    assert steps == []


# -- hooks: gather and on_step ---------------------------------------------


def test_gather_context_is_passed_to_act():
    seen = []

    def gather(state):
        return f"ctx@{state.iteration}"

    def act(ctx):
        seen.append(ctx)
        return ActOutcome(observation=ctx, tokens=0)

    run_loop(
        act=act,
        verify=done_after(2),
        conditions=[MaxIterations(10)],
        gather=gather,
    )
    assert seen == ["ctx@0", "ctx@1"]


def test_on_step_observes_every_completed_iteration():
    observed = []
    run_loop(
        act=acting(tokens=2),
        verify=never_done,
        conditions=[MaxIterations(3)],
        on_step=lambda record, state: observed.append(
            (record.iteration, state.tokens_used)
        ),
    )
    assert observed == [(0, 2), (1, 4), (2, 6)]


# -- validation -------------------------------------------------------------


def test_empty_conditions_rejected():
    with pytest.raises(ValueError):
        run_loop(act=acting(), verify=never_done, conditions=[])


def test_bare_condition_raises_clear_type_error():
    # A lone condition (no list) is a natural mistake; the error must name the
    # `conditions` argument rather than leak a cryptic "not iterable".
    with pytest.raises(TypeError, match="conditions must be"):
        run_loop(act=acting(), verify=never_done, conditions=MaxIterations(5))
