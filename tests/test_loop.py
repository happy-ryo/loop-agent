"""Driver-level tests: each cap firing, natural goal exit, reason discrimination."""

from __future__ import annotations

import pytest

from claude_loop import (
    ActOutcome,
    GoalCheck,
    GoalMet,
    MaxIterations,
    NoProgress,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    run_loop,
)
from conftest import ManualClock, acting, done_after, never_done, stepping_for


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
    clock = ManualClock()
    result = run_loop(
        act=stepping_for(clock, seconds=2.0),  # each step takes 2s
        verify=never_done,
        conditions=[Timeout(5.0)],
        time_fn=clock,
    )
    assert result.stop.name == "timeout"
    # guards see elapsed 0, 2, 4 (run), 6 -> first >= 5 at the 4th guard, after
    # 3 completed steps. Independent of how often the loop reads the clock.
    assert result.iterations == 3
    assert result.elapsed == 6.0


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


# -- dual stop: semantic conditions driving the loop -----------------------


def test_goal_met_condition_stops_loop_as_success():
    # The verify hook never fires; termination comes from the GoalMet condition
    # inspecting accumulated state. It is reported as a stop, but the trigger
    # name marks it a *success* rather than a mechanical cut-off.
    result = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[GoalMet(lambda state: state.iteration >= 3), MaxIterations(100)],
    )
    assert result.status == "stopped"
    assert result.stop.name == "goal_met"
    assert result.reason == "goal verified"
    assert result.iterations == 3
    # `goal_met` reflects only the verify-hook channel, so it stays False here;
    # `succeeded` collapses both success channels and must be True.
    assert result.goal_met is False
    assert result.succeeded is True


def test_succeeded_distinguishes_success_channels_from_aborts():
    # Natural (verify-hook) success: both goal_met and succeeded are True.
    natural = run_loop(
        act=acting(tokens=0),
        verify=done_after(2),
        conditions=[MaxIterations(100)],
    )
    assert natural.goal_met is True
    assert natural.succeeded is True

    # NoProgress abort: a stop, but not a success on either accessor.
    abort = run_loop(
        act=acting(tokens=0, observation="noop"),
        verify=never_done,
        conditions=[NoProgress(window=3, repeat=3), MaxIterations(100)],
    )
    assert abort.stop.name == "no_progress"
    assert abort.goal_met is False
    assert abort.succeeded is False

    # Mechanical cut-off: likewise not a success.
    capped = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(2)],
    )
    assert capped.goal_met is False
    assert capped.succeeded is False


def test_goal_met_detail_surfaces_in_result_reason():
    def verifier(state):
        met = state.iteration >= 2
        return GoalCheck(met=met, detail="suite green" if met else "")

    result = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[GoalMet(verifier), MaxIterations(100)],
    )
    assert result.stop.name == "goal_met"
    assert result.reason == "goal verified: suite green"
    assert result.iterations == 2


def test_no_progress_condition_aborts_thrashing_loop():
    # Every step emits the same observation -> the loop is stuck; NoProgress
    # cuts it off well before the mechanical backstop.
    result = run_loop(
        act=acting(tokens=0, observation="noop"),
        verify=never_done,
        conditions=[NoProgress(window=3, repeat=3), MaxIterations(100)],
    )
    assert result.status == "stopped"
    assert result.stop.name == "no_progress"
    assert result.iterations == 3
    assert "repeated 3 times" in result.reason


def test_dual_stop_goal_beats_no_progress_on_same_guard():
    # A loop that both makes no progress *and* has met its goal must terminate
    # as a success: GoalMet is declared first, so OR ordering reports it.
    result = run_loop(
        act=acting(tokens=0, observation="noop"),
        verify=never_done,
        conditions=[
            GoalMet(lambda state: state.iteration >= 3),
            NoProgress(window=3, repeat=3),
            MaxIterations(100),
        ],
    )
    assert result.stop.name == "goal_met"
    assert result.iterations == 3


def test_mechanical_cap_still_bounds_a_never_satisfied_dual_stop():
    # Neither semantic condition can fire (goal never met; every action is
    # distinct so there is no repeat); the hard cap must still terminate (R3).
    seq = iter(range(1000))

    def act(_ctx):
        return ActOutcome(observation=next(seq), tokens=0)

    result = run_loop(
        act=act,
        verify=never_done,
        conditions=[
            GoalMet(lambda state: False),
            NoProgress(window=3, repeat=3),
            MaxIterations(5),
        ],
    )
    assert result.stop.name == "max_iterations"
    assert result.iterations == 5


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


def test_on_step_sees_state_consistent_with_record():
    # On the goal-achieving iteration the observer must see the loop-level
    # goal_met flag already in sync with the per-step record (and elapsed
    # refreshed for this step), not the stale pre-step state.
    clock = ManualClock()
    seen = []
    run_loop(
        act=stepping_for(clock, seconds=1.0),
        verify=done_after(2),
        conditions=[MaxIterations(10)],
        time_fn=clock,
        on_step=lambda record, state: seen.append(
            (record.iteration, record.goal_met, state.goal_met, state.elapsed)
        ),
    )
    assert seen[0] == (0, False, False, 1.0)
    assert seen[-1] == (1, True, True, 2.0)


# -- validation -------------------------------------------------------------


def test_empty_conditions_rejected():
    with pytest.raises(ValueError):
        run_loop(act=acting(), verify=never_done, conditions=[])


def test_bare_condition_raises_clear_type_error():
    # A lone condition (no list) is a natural mistake; the error must name the
    # `conditions` argument rather than leak a cryptic "not iterable".
    with pytest.raises(TypeError, match="conditions must be"):
        run_loop(act=acting(), verify=never_done, conditions=MaxIterations(5))
