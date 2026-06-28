"""Runaway-guard proof: a never-converging loop ALWAYS halts at a hard cap.

These tests are the sandbox evidence for report.md S5 Phase 1 success criteria
(b) "未達でも上限で必ず停止" and (c) "AutoGPT 的暴走（無限ループ・コスト爆発）
を再現しない". Every scenario drives the loop with a goal that is never met and
an ``act`` that makes *no forward progress* -- it returns the identical
observation every iteration, the operational signature of a repeating action /
stuck agent.

The guarantee proven here is deliberately precise:

- :class:`MaxIterations` is an **unconditional** bound -- ``state.iteration``
  advances by exactly one every cycle, so it halts *any* loop regardless of cost
  or clock. It is the safety net every run should compose.
- :class:`TokenBudget` and :class:`Timeout` bound a run **only while steps
  actually consume tokens / advance the wall-clock** -- true of any real model
  call, but not of a degenerate 0-cost, instant no-op. The negative tests below
  pin that boundary so the conditional nature is part of the proven contract,
  not a hidden assumption.

To keep "infinite loop" from manifesting as a hung test, the no-progress act
carries a ``tripwire``: if it is ever invoked more than a generous ceiling, it
raises instead of letting pytest stall. A bounded loop never reaches it; an
unbounded one fails loudly with a count -- which the negative tests assert on
purpose.
"""

from __future__ import annotations

import time

import pytest

from loop_agent import (
    ActOutcome,
    AnyOf,
    MaxIterations,
    Timeout,
    TokenBudget,
    run_loop,
)
from conftest import ManualClock, never_done

# A constant observation: the same "action" repeated, never any new state.
NO_CHANGE = "<no forward progress>"


class NoProgressAct:
    """An ``act`` stub that repeats one action forever and never converges.

    It optionally charges a fixed token cost and/or advances an injected clock,
    so the same stub can exercise the iteration, token, and timeout caps. The
    ``tripwire`` turns a hypothetical unbounded loop into an ``AssertionError``
    (with the offending call count) rather than a hang.
    """

    def __init__(self, *, tokens=0, clock=None, seconds=0.0, tripwire=10_000):
        self.tokens = tokens
        self.clock = clock
        self.seconds = seconds
        self.tripwire = tripwire
        self.calls = 0

    def __call__(self, _ctx):
        self.calls += 1
        if self.calls > self.tripwire:
            raise AssertionError(
                f"act invoked {self.calls} times (> tripwire {self.tripwire}): "
                "loop is not bounded"
            )
        if self.clock is not None:
            self.clock.advance(self.seconds)
        return ActOutcome(observation=NO_CHANGE, tokens=self.tokens)


def _assert_made_no_progress(result):
    """Every recorded step shows the same stuck observation and goal unmet."""
    assert result.goal_met is False
    assert result.status == "stopped"
    assert len(result.history) == result.iterations
    assert all(step.goal_met is False for step in result.history)
    assert all(step.observation == NO_CHANGE for step in result.history)


# -- (b) each hard cap alone halts a never-converging, no-progress loop ------


def test_no_progress_loop_halts_at_max_iterations():
    act = NoProgressAct(tokens=0, tripwire=1_000)
    result = run_loop(act=act, verify=never_done, conditions=[MaxIterations(50)])

    assert result.stop.name == "max_iterations"
    assert result.iterations == 50
    assert act.calls == 50  # not one act beyond the cap
    _assert_made_no_progress(result)


def test_no_progress_loop_halts_at_token_budget_with_bounded_cost():
    # 10 tokens/step, budget 200 -> halts at the boundary after 20 steps.
    act = NoProgressAct(tokens=10, tripwire=1_000)
    result = run_loop(act=act, verify=never_done, conditions=[TokenBudget(200)])

    assert result.stop.name == "token_budget"
    assert result.iterations == 20
    # Cost is bounded: total never exceeds budget + at most one step's overshoot.
    assert result.tokens_used <= 200 + 10
    assert result.tokens_used == 200
    _assert_made_no_progress(result)


def test_no_progress_loop_halts_at_timeout():
    clock = ManualClock()
    act = NoProgressAct(clock=clock, seconds=2.0, tripwire=1_000)
    result = run_loop(
        act=act,
        verify=never_done,
        conditions=[Timeout(5.0)],
        time_fn=clock,
    )

    assert result.stop.name == "timeout"
    # guards see elapsed 0, 2, 4 (3 steps run), then 6 >= 5 -> halt.
    assert result.iterations == 3
    assert result.elapsed == 6.0
    _assert_made_no_progress(result)


@pytest.mark.parametrize(
    "cap",
    [MaxIterations(7), TokenBudget(35), Timeout(7.0)],
    ids=["max_iterations", "token_budget", "timeout"],
)
def test_each_cap_alone_halts_when_its_dimension_advances(cap):
    # Given a step that advances every dimension (charges tokens AND elapses the
    # clock), each cap in isolation halts a never-converging no-progress loop in
    # finitely many steps without tripping the unboundedness tripwire. (Whether
    # the token/time caps fire at all is contingent on that advance -- see the
    # negative tests below.) Each cap above is sized to fire after exactly 7
    # steps: MaxIterations(7); TokenBudget(35) at 5 tok/step; Timeout(7.0) at 1s.
    clock = ManualClock()
    act = NoProgressAct(tokens=5, clock=clock, seconds=1.0, tripwire=1_000)
    result = run_loop(
        act=act,
        verify=never_done,
        conditions=[cap],
        time_fn=clock,
    )

    assert result.status == "stopped"
    assert result.stop is not None
    assert act.calls == 7  # exact, not merely finite -- catches a regression
    _assert_made_no_progress(result)


# -- (c) cost / wall-clock cannot explode -----------------------------------


def test_cost_cannot_explode_beyond_budget_plus_one_step():
    # Even with an expensive action (1000 tokens/step), the cumulative spend is
    # capped: it can overshoot the budget by at most a single in-flight step,
    # never run away unbounded.
    step_tokens = 1_000
    budget = 5_000
    act = NoProgressAct(tokens=step_tokens, tripwire=1_000)
    result = run_loop(
        act=act, verify=never_done, conditions=[TokenBudget(budget)]
    )

    assert result.stop.name == "token_budget"
    assert result.tokens_used <= budget + step_tokens
    assert result.iterations == budget // step_tokens
    _assert_made_no_progress(result)


def test_token_budget_overshoots_by_at_most_one_step():
    # When the budget is NOT a multiple of the per-step cost the loop genuinely
    # overshoots -- this exercises the "+ one in-flight step" slack that the
    # exact-multiple cases above only ever touch at equality. 205 / 10 -> the
    # 21st step's guard sees 210 >= 205 only after spending it.
    step_tokens = 10
    budget = 205
    act = NoProgressAct(tokens=step_tokens, tripwire=1_000)
    result = run_loop(
        act=act, verify=never_done, conditions=[TokenBudget(budget)]
    )

    assert result.stop.name == "token_budget"
    assert result.iterations == 21
    assert result.tokens_used == 210
    # The overshoot is real (strictly past budget) yet bounded by one step.
    assert budget < result.tokens_used <= budget + step_tokens
    _assert_made_no_progress(result)


def test_tightest_of_several_caps_wins_and_loop_still_halts():
    # All three caps present; at 100 tokens/step the token budget (1000) is the
    # tightest and fires after 10 steps -- well before MaxIterations(100) or the
    # timeout. The point: composing caps never weakens the guarantee.
    clock = ManualClock()
    act = NoProgressAct(tokens=100, clock=clock, seconds=0.1, tripwire=1_000)
    result = run_loop(
        act=act,
        verify=never_done,
        conditions=[MaxIterations(100), TokenBudget(1_000), Timeout(50.0)],
        time_fn=clock,
    )

    assert result.stop.name == "token_budget"
    assert result.iterations == 10
    assert act.calls == 10
    _assert_made_no_progress(result)


def test_runaway_guard_holds_under_the_real_monotonic_clock():
    # The deterministic clock above isolates the cap logic; this one proves the
    # same bound holds with the default, un-injected time source -- no hang.
    act = NoProgressAct(tokens=0, tripwire=2_000)
    start = time.monotonic()
    result = run_loop(act=act, verify=never_done, conditions=[MaxIterations(500)])
    elapsed = time.monotonic() - start

    assert result.iterations == 500
    assert act.calls == 500
    assert elapsed < 5.0  # 500 trivial iterations are effectively instant
    _assert_made_no_progress(result)


# -- the conditional caps: what they do NOT bound (proving the contract) ----


def test_token_budget_alone_does_not_bound_a_zero_cost_loop():
    # A genuinely stuck agent that emits a free/instant no-op spends no tokens,
    # so TokenBudget never fires. The tripwire converts the resulting unbounded
    # spin into a loud failure -- we assert it IS reached, pinning the fact that
    # TokenBudget is a cost cap, not a liveness guarantee on its own.
    act = NoProgressAct(tokens=0, tripwire=300)
    with pytest.raises(AssertionError, match="not bounded"):
        run_loop(act=act, verify=never_done, conditions=[TokenBudget(100)])
    assert act.calls == 301  # ran well past the budget's nominal reach


def test_timeout_alone_does_not_bound_a_stalled_clock_loop():
    # If wall-clock never advances during a step, Timeout never fires either.
    # (Unreachable with the real time.monotonic, but the explicit negative makes
    # the dependence on a moving clock part of the proven contract.)
    stalled = ManualClock()
    act = NoProgressAct(clock=stalled, seconds=0.0, tripwire=300)
    with pytest.raises(AssertionError, match="not bounded"):
        run_loop(
            act=act,
            verify=never_done,
            conditions=[Timeout(5.0)],
            time_fn=stalled,
        )


def test_max_iterations_is_the_unconditional_safety_net():
    # The flip side: even with BOTH degenerate conditions at once -- zero token
    # cost and a frozen clock -- MaxIterations still halts the loop, because the
    # iteration counter advances every cycle no matter what. This is why every
    # run should compose MaxIterations as the backstop.
    stalled = ManualClock()
    act = NoProgressAct(tokens=0, clock=stalled, seconds=0.0, tripwire=1_000)
    result = run_loop(
        act=act,
        verify=never_done,
        conditions=[MaxIterations(10)],
        time_fn=stalled,
    )

    assert result.stop.name == "max_iterations"
    assert result.iterations == 10
    assert result.tokens_used == 0
    assert result.elapsed == 0.0
    _assert_made_no_progress(result)


# -- structural guarantee: a cap-less loop cannot be built ------------------


def test_a_loop_with_no_cap_is_structurally_impossible():
    # The runaway guard is enforced at construction: you cannot assemble a loop
    # whose only exit is a goal that may never arrive. Both the condition set
    # and the driver reject an empty cap list.
    with pytest.raises(ValueError):
        AnyOf([])
    with pytest.raises(ValueError):
        run_loop(act=NoProgressAct(), verify=never_done, conditions=[])
