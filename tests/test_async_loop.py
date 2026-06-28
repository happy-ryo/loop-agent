"""async_run_loop tests: async entry point, sync/async hook mixing, parity.

The suite stays dependency-free (no pytest-asyncio): each async test drives the
coroutine with :func:`asyncio.run`, which is exactly the supported usage of
:func:`loop_agent.async_run_loop` from synchronous code (Issue #40).
"""

from __future__ import annotations

import asyncio

import pytest

from loop_agent import (
    ActOutcome,
    AnyOf,
    AsyncSeamInSyncLoop,
    GoalCheck,
    GoalMet,
    LoopState,
    MaxIterations,
    NoProgress,
    StepRecord,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    async_run_loop,
    run_loop,
)
from conftest import ManualClock, acting, done_after, never_done, stepping_for


# -- async hook stubs (mirror conftest's sync ones) -------------------------


def aacting(tokens: int = 0, observation: object = None):
    """An async ``act`` stub charging a fixed token cost per step."""

    async def _act(_ctx: object) -> ActOutcome:
        await asyncio.sleep(0)
        return ActOutcome(observation=observation, tokens=tokens)

    return _act


def adone_after(n: int):
    """An async ``verify`` stub reporting the goal met on the ``n``-th call."""
    calls = {"count": 0}

    async def _verify(_outcome: ActOutcome) -> VerifyOutcome:
        await asyncio.sleep(0)
        calls["count"] += 1
        met = calls["count"] >= n
        return VerifyOutcome(goal_met=met, detail="converged" if met else "")

    return _verify


async def anever_done(_outcome: ActOutcome) -> VerifyOutcome:
    await asyncio.sleep(0)
    return VerifyOutcome(goal_met=False)


# -- basic async drive ------------------------------------------------------


def test_async_goal_met_terminates_naturally():
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=1),
            verify=adone_after(3),
            conditions=[MaxIterations(100)],
        )
    )
    assert result.goal_met is True
    assert result.status == "goal_met"
    assert result.stop is None
    assert result.iterations == 3
    assert result.history[-1].detail == "converged"


def test_async_max_iterations_cap():
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=0),
            verify=anever_done,
            conditions=[MaxIterations(4)],
        )
    )
    assert result.status == "stopped"
    assert result.stop.name == "max_iterations"
    assert result.iterations == 4


def test_async_token_budget_cap():
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=30),
            verify=anever_done,
            conditions=[TokenBudget(100)],
        )
    )
    assert result.stop.name == "token_budget"
    assert result.iterations == 4
    assert result.tokens_used == 120


# -- sync/async parity ------------------------------------------------------


def test_parity_sync_vs_async_same_sync_hooks():
    """async_run_loop with sync hooks matches run_loop exactly."""
    sync_result = run_loop(
        act=acting(tokens=7),
        verify=done_after(5),
        conditions=[MaxIterations(100)],
    )
    async_result = asyncio.run(
        async_run_loop(
            act=acting(tokens=7),
            verify=done_after(5),
            conditions=[MaxIterations(100)],
        )
    )
    assert async_result.status == sync_result.status
    assert async_result.iterations == sync_result.iterations
    assert async_result.tokens_used == sync_result.tokens_used
    assert [r.detail for r in async_result.history] == [
        r.detail for r in sync_result.history
    ]


# -- mixed sync + async seams ----------------------------------------------


def test_mixed_async_gather_sync_act_async_verify():
    seen_states = []

    async def agather(state: LoopState) -> LoopState:
        await asyncio.sleep(0)
        seen_states.append(state.iteration)
        return state

    result = asyncio.run(
        async_run_loop(
            gather=agather,
            act=acting(tokens=2),  # sync
            verify=adone_after(3),  # async
            conditions=[MaxIterations(100)],
        )
    )
    assert result.succeeded is True
    assert result.iterations == 3
    assert seen_states == [0, 1, 2]


def test_mixed_sync_gather_async_act_sync_verify():
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=5),  # async
            verify=done_after(2),  # sync
            conditions=[MaxIterations(100)],
        )
    )
    assert result.succeeded is True
    assert result.iterations == 2
    assert result.tokens_used == 10


# -- async conditions -------------------------------------------------------


def test_async_goal_met_stop_condition():
    """A GoalMet verifier may be async; it fires via the stop-condition seam."""

    async def averifier(state: LoopState):
        await asyncio.sleep(0)
        return GoalCheck(met=state.iteration >= 3, detail="async-check")

    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=1),
            verify=anever_done,  # never via the hook
            conditions=[MaxIterations(100), GoalMet(averifier)],
        )
    )
    assert result.succeeded is True
    assert result.status == "stopped"
    assert result.stop.name == "goal_met"
    assert "async-check" in result.stop.reason
    assert result.iterations == 3


def test_async_condition_order_matches_sync():
    """afirst_triggered reports the first condition in declared order."""

    async def always(_state: LoopState):
        await asyncio.sleep(0)
        return "async fired"

    trigger = asyncio.run(
        AnyOf([MaxIterations(0), GoalMet(always)]).afirst_triggered(LoopState())
    )
    assert trigger.name == "max_iterations"


def test_sync_first_triggered_rejects_async_goalmet_verifier():
    """Sync first_triggered must NOT mis-read an async verifier's coroutine."""

    async def averifier(_state: LoopState):
        await asyncio.sleep(0)
        return True

    with pytest.raises(AsyncSeamInSyncLoop):
        AnyOf([GoalMet(averifier)]).first_triggered(LoopState())


def test_sync_first_triggered_rejects_async_check_condition():
    class _AsyncCheck:
        name = "async_check"

        async def check(self, _state: LoopState):
            await asyncio.sleep(0)
            return "fired"

    with pytest.raises(AsyncSeamInSyncLoop):
        AnyOf([_AsyncCheck()]).first_triggered(LoopState())


def test_sync_goalmet_verifier_still_works_through_first_triggered():
    trigger = AnyOf([GoalMet(lambda s: GoalCheck(met=True, detail="sync"))]).first_triggered(
        LoopState()
    )
    assert trigger.name == "goal_met"
    assert "sync" in trigger.reason


def test_afirst_triggered_with_sync_conditions():
    """afirst_triggered handles plain synchronous conditions too."""
    trigger = asyncio.run(AnyOf([TokenBudget(10)]).afirst_triggered(LoopState()))
    assert trigger is None
    state = LoopState(tokens_used=10)
    trigger = asyncio.run(AnyOf([TokenBudget(10)]).afirst_triggered(state))
    assert trigger.name == "token_budget"


# -- async gate -------------------------------------------------------------


class _AsyncProceedGate:
    """A minimal async ActionGate that always proceeds (records calls)."""

    def __init__(self) -> None:
        self.calls = 0

    async def review(self, context, state):
        await asyncio.sleep(0)
        self.calls += 1
        from loop_agent import GateReview
        from loop_agent.loop import GATE_PROCEED

        return GateReview(disposition=GATE_PROCEED, context=context)


def test_async_gate_review():
    gate = _AsyncProceedGate()
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=1),
            verify=adone_after(2),
            conditions=[MaxIterations(100)],
            gate=gate,
        )
    )
    assert result.succeeded is True
    assert result.iterations == 2
    assert gate.calls == 2


# -- async on_step ----------------------------------------------------------


def test_async_on_step():
    observed = []

    async def aon_step(record: StepRecord, state: LoopState) -> None:
        await asyncio.sleep(0)
        observed.append((record.iteration, state.iteration))

    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=0),
            verify=anever_done,
            conditions=[MaxIterations(3)],
            on_step=aon_step,
        )
    )
    assert result.iterations == 3
    assert observed == [(0, 1), (1, 2), (2, 3)]


# -- run_loop rejects async hooks (points at async_run_loop) ----------------


def test_run_loop_with_suspending_async_hook_raises():
    """An async hook that actually awaits is rejected (not a raw loop error)."""
    with pytest.raises(AsyncSeamInSyncLoop, match="async_run_loop"):
        run_loop(
            act=aacting(tokens=3),  # contains `await asyncio.sleep(0)`
            verify=never_done,
            conditions=[MaxIterations(100)],
        )
    assert issubclass(AsyncSeamInSyncLoop, RuntimeError)


def test_run_loop_with_non_suspending_async_hook_raises():
    """An async hook that never internally awaits is ALSO rejected (consistency).

    Without strict-sync rejection this case would have run silently to success,
    since awaiting a coroutine that never yields does not suspend the driver.
    """

    async def act_no_await(_ctx):
        return ActOutcome(tokens=1)  # async def, but no internal await

    with pytest.raises(RuntimeError, match="async_run_loop"):
        run_loop(
            act=act_no_await,
            verify=never_done,
            conditions=[MaxIterations(100)],
        )


def test_run_loop_with_async_condition_raises():
    class _AsyncCheck:
        name = "async_check"

        async def check(self, _state: LoopState):
            await asyncio.sleep(0)
            return None

    with pytest.raises(RuntimeError, match="async_run_loop"):
        run_loop(
            act=acting(tokens=0),
            verify=never_done,
            conditions=[MaxIterations(100), _AsyncCheck()],
        )


def test_run_loop_with_sync_goalmet_verifier_works():
    """run_loop + GoalMet(sync verifier) must keep working (uses sync check)."""
    result = run_loop(
        act=acting(tokens=1),
        verify=never_done,
        conditions=[MaxIterations(100), GoalMet(lambda s: s.iteration >= 3)],
    )
    assert result.succeeded is True
    assert result.stop.name == "goal_met"
    assert result.iterations == 3


def test_run_loop_with_async_goalmet_verifier_raises():
    async def averify(_state):
        await asyncio.sleep(0)
        return True

    with pytest.raises(AsyncSeamInSyncLoop):
        run_loop(
            act=acting(tokens=0),
            verify=never_done,
            conditions=[MaxIterations(100), GoalMet(averify)],
        )


def test_run_loop_ignores_acheck_uses_sync_check():
    """Under strict-sync run_loop, a condition's sync check is authoritative.

    A custom condition exposing both a sync ``check`` and an async ``acheck``
    must be evaluated via ``check`` (not silently via ``acheck``).
    """

    class _Both:
        name = "both"

        def check(self, _state):
            return None  # sync: never fires

        async def acheck(self, _state):
            await asyncio.sleep(0)
            return "fired-async"  # would fire if (wrongly) used

    result = run_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[_Both(), MaxIterations(2)],
    )
    assert result.stop.name == "max_iterations"  # not "both"
    assert result.iterations == 2


def test_async_run_loop_uses_acheck_for_async_condition():
    """In the async path, a condition's acheck IS used."""

    class _Both:
        name = "both"

        def check(self, _state):
            return None

        async def acheck(self, state):
            await asyncio.sleep(0)
            return "fired-async" if state.iteration >= 2 else None

    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=0),
            verify=anever_done,
            conditions=[_Both(), MaxIterations(100)],
        )
    )
    assert result.stop.name == "both"
    assert result.iterations == 2


def test_run_loop_with_async_on_step_raises():
    async def aon_step(_record, _state):
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="async_run_loop"):
        run_loop(
            act=acting(tokens=0),
            verify=never_done,
            conditions=[MaxIterations(3)],
            on_step=aon_step,
        )


def test_run_observed_loop_with_async_on_step_raises():
    """The observed-loop wrapper must not swallow an async user on_step."""
    from loop_agent import run_observed_loop

    async def aon_step(_record, _state):
        await asyncio.sleep(0)

    with pytest.raises(AsyncSeamInSyncLoop):
        run_observed_loop(
            act=acting(tokens=0),
            verify=never_done,
            conditions=[MaxIterations(3)],
            on_step=aon_step,
        )


def test_run_loop_inside_event_loop_rejects_async_hook():
    """run_loop called from inside a running loop still rejects async seams.

    Strict-sync is an explicit per-run flag, not ambient-loop detection, so the
    rejection holds even when an event loop happens to be running.
    """

    async def driver_non_suspending():
        async def act_no_await(_ctx):  # async, never suspends
            return ActOutcome(tokens=1)

        return run_loop(
            act=act_no_await, verify=never_done, conditions=[MaxIterations(5)]
        )

    async def driver_suspending():
        return run_loop(
            act=aacting(tokens=1),  # async, awaits asyncio.sleep(0)
            verify=never_done,
            conditions=[MaxIterations(5)],
        )

    with pytest.raises(AsyncSeamInSyncLoop):
        asyncio.run(driver_non_suspending())
    with pytest.raises(AsyncSeamInSyncLoop):
        asyncio.run(driver_suspending())


def test_sync_hook_may_run_nested_async_loop():
    """A sync run_loop hook may itself run asyncio.run(async_run_loop(...)).

    The strict-sync rejection keys off the absent event loop, so it must NOT leak
    into the nested run (which has its own running loop): the inner async seams
    must work, not be rejected as AsyncSeamInSyncLoop.
    """
    nested = {}

    def act(_ctx):
        # Inside the synchronous run_loop drive, run a full async loop with
        # legitimate async hooks.
        nested["result"] = asyncio.run(
            async_run_loop(
                act=aacting(tokens=5),
                verify=adone_after(2),
                conditions=[MaxIterations(10)],
            )
        )
        return ActOutcome(tokens=1)

    result = run_loop(act=act, verify=done_after(1), conditions=[MaxIterations(5)])
    assert result.succeeded is True
    assert nested["result"].succeeded is True
    assert nested["result"].iterations == 2
    assert nested["result"].tokens_used == 10


def test_run_observed_loop_with_sync_on_step_still_works():
    from loop_agent import run_observed_loop

    seen = []

    def on_step(record, _state):
        seen.append(record.iteration)

    result = run_observed_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
        on_step=on_step,
    )
    assert result.iterations == 3
    assert seen == [0, 1, 2]


# -- run_loop drives synchronously in the caller's context ------------------


def test_run_loop_works_inside_running_loop_with_sync_hooks():
    """No event loop is created, so a sync run_loop works even inside one."""

    async def driver():
        return run_loop(
            act=acting(tokens=1),
            verify=done_after(2),
            conditions=[MaxIterations(100)],
        )

    result = asyncio.run(driver())
    assert result.succeeded is True
    assert result.iterations == 2


def test_run_loop_propagates_seam_stopiteration_as_is():
    """A sync seam raising StopIteration must surface as StopIteration (PEP 479).

    Without unwrapping, crossing the coroutine boundary would rewrite it to
    'RuntimeError: coroutine raised StopIteration'.
    """
    it = iter([ActOutcome(tokens=1)])  # one item, then exhausts

    def act(_ctx):
        return next(it)  # raises StopIteration on the 2nd call

    with pytest.raises(StopIteration):
        run_loop(act=act, verify=never_done, conditions=[MaxIterations(5)])


def test_run_loop_propagates_other_exception_types_unchanged():
    def act(_ctx):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_loop(act=act, verify=never_done, conditions=[MaxIterations(5)])


def test_run_loop_propagates_genuine_runtimeerror():
    def act(_ctx):
        raise RuntimeError("genuine")

    with pytest.raises(RuntimeError, match="genuine"):
        run_loop(act=act, verify=never_done, conditions=[MaxIterations(5)])


def test_run_loop_preserves_caller_contextvar_mutation():
    """A sync hook's contextvar .set() propagates to the caller (exact parity)."""
    import contextvars

    cv = contextvars.ContextVar("loop_test_cv", default="orig")

    def act(_ctx):
        cv.set("mutated-by-hook")
        return ActOutcome(tokens=0)

    run_loop(act=act, verify=done_after(1), conditions=[MaxIterations(5)])
    assert cv.get() == "mutated-by-hook"


def test_run_loop_does_not_disturb_event_loop_policy():
    """run_loop must not reset the thread's current event loop (no asyncio.run)."""
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    try:
        run_loop(act=acting(tokens=0), verify=done_after(1), conditions=[MaxIterations(5)])
        assert _asyncio.get_event_loop() is loop
    finally:
        _asyncio.set_event_loop(None)
        loop.close()


# -- concurrent async runs are independent ----------------------------------


def test_concurrent_async_runs_independent():
    async def driver():
        return await asyncio.gather(
            async_run_loop(
                act=aacting(tokens=1),
                verify=adone_after(2),
                conditions=[MaxIterations(100)],
            ),
            async_run_loop(
                act=aacting(tokens=10),
                verify=adone_after(5),
                conditions=[MaxIterations(100)],
            ),
        )

    a, b = asyncio.run(driver())
    assert a.iterations == 2 and a.tokens_used == 2
    assert b.iterations == 5 and b.tokens_used == 50


# -- async path honours resume / early-return -------------------------------


def test_async_resume_from_initial_state():
    seed = LoopState(iteration=2, tokens_used=20, history=[])
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=10),
            verify=anever_done,
            conditions=[MaxIterations(4)],
            initial_state=seed,
        )
    )
    # Resumes at iteration 2, runs 2 more to hit MaxIterations(4).
    assert result.iterations == 4
    assert result.tokens_used == 40


def test_async_early_return_when_seed_goal_met():
    seed = LoopState(iteration=3, goal_met=True)
    result = asyncio.run(
        async_run_loop(
            act=aacting(tokens=99),
            verify=anever_done,
            conditions=[MaxIterations(100)],
            initial_state=seed,
        )
    )
    assert result.status == "goal_met"
    assert result.iterations == 3
    assert result.tokens_used == 0  # no new step ran


def test_async_timeout_with_manual_clock():
    clock = ManualClock()
    result = asyncio.run(
        async_run_loop(
            act=stepping_for(clock, seconds=1.0),
            verify=anever_done,
            conditions=[Timeout(3.0)],
            time_fn=clock,
        )
    )
    assert result.stop.name == "timeout"
    assert result.iterations == 3


def test_async_no_progress_abort():
    result = asyncio.run(
        async_run_loop(
            act=aacting(observation="stuck"),
            verify=anever_done,
            conditions=[MaxIterations(100), NoProgress(window=3, repeat=3)],
        )
    )
    assert result.stop.name == "no_progress"
    assert result.iterations == 3
