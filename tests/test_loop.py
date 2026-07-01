"""Driver-level tests: each cap firing, natural goal exit, reason discrimination."""

from __future__ import annotations

import json

import pytest

from loop_agent import (
    ActOutcome,
    ConfigError,
    GoalCheck,
    GoalMet,
    LoopState,
    MaxIterations,
    NoProgress,
    ReviewOutcome,
    StepRecord,
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


# -- resume: initial_state seeding (Issue #14) -----------------------------


def test_initial_state_none_is_a_fresh_run():
    # The default and an explicit None must both start empty (no behaviour drift
    # from adding the resume seam to the signature).
    seeded = run_loop(
        act=acting(tokens=1),
        verify=never_done,
        conditions=[MaxIterations(3)],
        initial_state=None,
    )
    plain = run_loop(
        act=acting(tokens=1), verify=never_done, conditions=[MaxIterations(3)]
    )
    assert seeded.iterations == plain.iterations == 3
    assert seeded.tokens_used == plain.tokens_used == 3


def test_empty_initial_state_equals_a_fresh_run():
    # An empty LoopState is equivalent to None, so DBProgressLog.state for a new
    # run can be wired in unconditionally (same call path for new and resumed).
    result = run_loop(
        act=acting(tokens=2),
        verify=never_done,
        conditions=[MaxIterations(2)],
        initial_state=LoopState(),
    )
    assert result.iterations == 2 and result.tokens_used == 4


def test_initial_state_continues_iteration_tokens_and_history():
    seed = LoopState(
        iteration=2,
        tokens_used=20,
        history=[StepRecord(0, "a", 10, False), StepRecord(1, "b", 10, False)],
    )
    result = run_loop(
        act=acting(tokens=10, observation="c"),
        verify=never_done,
        conditions=[MaxIterations(4)],
        initial_state=seed,
    )
    # Continues from iteration 2: two more steps reach the cap of 4, and the
    # reconstructed history is preserved ahead of the newly appended records.
    assert result.iterations == 4
    assert result.tokens_used == 40
    assert [r.iteration for r in result.history] == [0, 1, 2, 3]
    assert [r.observation for r in result.history] == ["a", "b", "c", "c"]


def test_initial_state_is_copied_not_mutated():
    # The loop must not mutate the caller's seed (e.g. DBProgressLog.state),
    # neither its scalars nor its history list.
    hist = [StepRecord(0, "a", 1, False)]
    seed = LoopState(iteration=1, tokens_used=1, elapsed=0.5, history=hist)
    run_loop(
        act=acting(tokens=1),
        verify=never_done,
        conditions=[MaxIterations(3)],
        initial_state=seed,
    )
    assert seed.iteration == 1
    assert seed.tokens_used == 1
    assert seed.elapsed == 0.5
    assert seed.history is hist and len(seed.history) == 1


def test_resumed_elapsed_continues_from_seed():
    # elapsed keeps accumulating from the persisted value: the clock origin is
    # back-dated by seed.elapsed so Timeout sees the *total* run time.
    clock = ManualClock()
    seed = LoopState(iteration=2, elapsed=4.0)
    result = run_loop(
        act=stepping_for(clock, seconds=1.0),
        verify=never_done,
        conditions=[Timeout(7.0)],
        time_fn=clock,
        initial_state=seed,
    )
    # Guards see elapsed 4, 5, 6, 7 -> first >= 7 after 3 new steps (2 seeded +
    # 3 = iteration 5); the deadline counts time from before the interruption.
    assert result.stop.name == "timeout"
    assert result.iterations == 5
    assert result.elapsed == 7.0


def test_resume_already_past_a_cap_stops_before_any_new_step():
    # A run resumed at/after a cap terminates immediately, running no new step --
    # the same guard-before-step contract a straight run obeys.
    seed = LoopState(iteration=5, tokens_used=50)
    steps = []
    result = run_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(5)],
        initial_state=seed,
        on_step=lambda record, state: steps.append(record),
    )
    assert result.status == "stopped"
    assert result.stop.name == "max_iterations"
    assert result.iterations == 5
    assert result.tokens_used == 50
    assert steps == []


# -- optional post-act review ----------------------------------------------


def test_review_runs_between_act_and_verify_and_records_json_detail():
    order = []

    def act(_ctx):
        order.append("act")
        return ActOutcome(observation="artifact", tokens=3)

    def review(outcome):
        order.append("review")
        assert outcome.observation == "artifact"
        return ReviewOutcome(approved=True, feedback="scope ok")

    def verify(outcome):
        order.append("verify")
        assert outcome.observation == "artifact"
        return VerifyOutcome(goal_met=True, detail="pytest passed")

    result = run_loop(
        act=act,
        review=review,
        verify=verify,
        conditions=[MaxIterations(5)],
    )

    assert order == ["act", "review", "verify"]
    assert result.status == "goal_met"
    detail = json.loads(result.history[-1].detail)
    assert detail == {
        "review": {"approved": True, "feedback": "scope ok", "severity": "info"},
        "verify": {"detail": "pytest passed"},
    }


def test_blocking_review_skips_verify_and_feedback_reaches_next_gather():
    review_calls = 0
    verify_calls = 0
    gather_seen = []

    def gather(state):
        gather_seen.append(state.history[-1].detail if state.history else "")
        return {"iteration": state.iteration}

    def act(ctx):
        return ActOutcome(observation={"iteration": ctx["iteration"]}, tokens=2)

    def review(_outcome):
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return ReviewOutcome(False, "narrow the edit", "blocking")
        return ReviewOutcome(True, "scope ok")

    def verify(_outcome):
        nonlocal verify_calls
        verify_calls += 1
        return VerifyOutcome(goal_met=True, detail="pytest passed")

    result = run_loop(
        gather=gather,
        act=act,
        review=review,
        verify=verify,
        conditions=[MaxIterations(5)],
    )

    assert result.status == "goal_met"
    assert result.iterations == 2
    assert verify_calls == 1
    first_detail = json.loads(result.history[0].detail)
    assert first_detail == {
        "review": {
            "approved": False,
            "feedback": "narrow the edit",
            "severity": "blocking",
        }
    }
    assert "narrow the edit" in gather_seen[1]


def test_review_outcome_rejects_unknown_severity():
    with pytest.raises(ConfigError, match="ReviewOutcome severity"):
        ReviewOutcome(True, severity="critical")

# -- validation -------------------------------------------------------------


def test_empty_conditions_rejected():
    with pytest.raises(ValueError):
        run_loop(act=acting(), verify=never_done, conditions=[])


def test_bare_condition_raises_clear_type_error():
    # A lone condition (no list) is a natural mistake; the error must name the
    # `conditions` argument rather than leak a cryptic "not iterable".
    with pytest.raises(TypeError, match="conditions must be"):
        run_loop(act=acting(), verify=never_done, conditions=MaxIterations(5))
