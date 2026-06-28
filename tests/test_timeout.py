"""Per-call timeout / kill for the act and verify seams (Issue #42).

Covers the four-quadrant behaviour matrix: {async, sync} x {graceful, kill}, plus
the platform fallbacks for a synchronous seam where POSIX ``SIGALRM`` is
unavailable (best-effort post-hoc detection for graceful; an explicit
``UnsupportedTimeoutKill`` for kill). The async tests use *tiny* real timeouts
(the trip fires at the deadline, not at the seam's long sleep), so the suite
stays fast and dependency-free (no pytest-asyncio): each drives the coroutine
with :func:`asyncio.run`.
"""

from __future__ import annotations

import asyncio
import functools
import signal
import threading
import time

import pytest

import loop_agent.loop as loop_mod
from loop_agent import (
    ACT_TIMEOUT_OBSERVATION,
    TIMEOUT_GRACEFUL,
    TIMEOUT_KILL,
    VERIFY_TIMEOUT_OBSERVATION,
    ActOutcome,
    GateReview,
    MaxIterations,
    NoProgress,
    SeamTimeout,
    TimeoutPolicy,
    UnsupportedTimeoutKill,
    VerifyOutcome,
    async_run_loop,
    run_loop,
)
from loop_agent.loop import GATE_PROCEED
from conftest import ManualClock, acting, done_after, never_done, stepping_for


# -- async stubs ------------------------------------------------------------


def sleeping_aact(delay: float, *, tokens: int = 0, observation=None, flag=None):
    """An async ``act`` that sleeps ``delay`` s; sets ``flag['done']`` if it finishes."""

    async def _act(_ctx):
        await asyncio.sleep(delay)
        if flag is not None:
            flag["done"] = True
        return ActOutcome(observation=observation, tokens=tokens)

    return _act


def sleeping_averify(delay: float, *, goal_met: bool = False, flag=None):
    async def _verify(_outcome):
        await asyncio.sleep(delay)
        if flag is not None:
            flag["done"] = True
        return VerifyOutcome(goal_met=goal_met)

    return _verify


async def afast_verify(_outcome):
    await asyncio.sleep(0)
    return VerifyOutcome(goal_met=False)


# -- TimeoutPolicy validation / resolution ----------------------------------


@pytest.mark.parametrize("bad", [0, -1.0, 0.0])
def test_policy_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        TimeoutPolicy(act=bad)
    with pytest.raises(ValueError):
        TimeoutPolicy(default=bad)
    with pytest.raises(ValueError):
        TimeoutPolicy(verify=bad)


def test_policy_rejects_bad_mode():
    with pytest.raises(ValueError):
        TimeoutPolicy(default=1.0, on_timeout="paused")


def test_policy_rejects_bool():
    # bool is an int subclass; reject it for parity with the bare-number path.
    with pytest.raises(TypeError):
        TimeoutPolicy(act=True)
    with pytest.raises(TypeError):
        TimeoutPolicy(default=False)
    with pytest.raises(TypeError):
        TimeoutPolicy(verify=True)


def test_policy_per_seam_resolution():
    p = TimeoutPolicy(default=5.0, act=2.0)
    assert p.act_seconds == 2.0
    assert p.verify_seconds == 5.0  # falls back to default
    assert TimeoutPolicy(verify=3.0).act_seconds is None


def test_resolve_bare_number_is_graceful_both_seams():
    resolved = loop_mod._resolve_timeout(2.5)
    assert resolved.act_seconds == 2.5
    assert resolved.verify_seconds == 2.5
    assert resolved.on_timeout == TIMEOUT_GRACEFUL


def test_resolve_none_and_noop_policy():
    assert loop_mod._resolve_timeout(None) is None
    # A policy with no effective deadline collapses to the zero-overhead path.
    assert loop_mod._resolve_timeout(TimeoutPolicy()) is None


def test_resolve_rejects_bool_and_bad_type():
    with pytest.raises(TypeError):
        loop_mod._resolve_timeout(True)
    with pytest.raises(TypeError):
        loop_mod._resolve_timeout("5s")


# -- async kill (asyncio.wait_for cancels the coroutine) --------------------


def test_async_act_kill_raises_seam_timeout_and_cancels():
    flag = {"done": False}
    with pytest.raises(SeamTimeout) as exc:
        asyncio.run(
            async_run_loop(
                act=sleeping_aact(delay=5.0, flag=flag),
                verify=afast_verify,
                conditions=[MaxIterations(10)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )
    assert exc.value.seam == "act"
    assert exc.value.seconds == 0.01
    assert flag["done"] is False  # the coroutine was cancelled, never finished


def test_async_verify_kill_raises_seam_timeout():
    with pytest.raises(SeamTimeout) as exc:
        asyncio.run(
            async_run_loop(
                act=acting(tokens=1),  # sync, fast
                verify=sleeping_averify(delay=5.0),
                conditions=[MaxIterations(10)],
                timeout=TimeoutPolicy(verify=0.01, on_timeout=TIMEOUT_KILL),
            )
        )
    assert exc.value.seam == "verify"


# -- async graceful (record synthetic step, continue) -----------------------


def test_async_act_graceful_records_and_continues():
    result = asyncio.run(
        async_run_loop(
            act=sleeping_aact(delay=5.0, tokens=99),
            verify=afast_verify,
            conditions=[MaxIterations(3)],
            timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert result.status == "stopped"
    assert result.stop.name == "max_iterations"
    assert result.iterations == 3
    # Every step is a synthetic act-timeout: no tokens charged, marker observation.
    assert result.tokens_used == 0
    assert [r.observation for r in result.history] == [ACT_TIMEOUT_OBSERVATION] * 3
    assert all(r.goal_met is False for r in result.history)
    assert "act timed out after 0.01s" in result.history[0].detail


def test_async_verify_graceful_keeps_act_tokens():
    result = asyncio.run(
        async_run_loop(
            act=acting(tokens=10),  # sync act succeeds, tokens charged
            verify=sleeping_averify(delay=5.0),
            conditions=[MaxIterations(2)],
            timeout=TimeoutPolicy(verify=0.01, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert result.iterations == 2
    assert result.tokens_used == 20  # act's tokens survive a verify timeout
    assert [r.observation for r in result.history] == [VERIFY_TIMEOUT_OBSERVATION] * 2
    assert [r.tokens for r in result.history] == [10, 10]


def test_async_graceful_composes_with_no_progress():
    """Repeated graceful timeouts trip NoProgress on the marker observation."""
    result = asyncio.run(
        async_run_loop(
            act=sleeping_aact(delay=5.0),
            verify=afast_verify,
            conditions=[MaxIterations(100), NoProgress(window=3, repeat=3)],
            timeout=0.01,  # bare number == graceful, both seams
        )
    )
    assert result.stop.name == "no_progress"
    assert result.iterations == 3


# -- async path needs no SIGALRM (portable kill) ----------------------------


def test_async_kill_works_without_alarm(monkeypatch):
    """An async seam is cancelled via wait_for even where SIGALRM is unavailable."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=sleeping_aact(delay=5.0),
                verify=afast_verify,
                conditions=[MaxIterations(10)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


# -- timeout never trips when the seam finishes in time ---------------------


def test_no_false_trip_when_fast():
    result = asyncio.run(
        async_run_loop(
            act=sleeping_aact(delay=0.0, tokens=1),
            verify=done_after(2),
            conditions=[MaxIterations(10)],
            timeout=TimeoutPolicy(default=5.0, on_timeout=TIMEOUT_KILL),
        )
    )
    assert result.succeeded is True
    assert result.iterations == 2
    assert result.tokens_used == 2


# -- synchronous seam via POSIX SIGALRM (real interruption) -----------------

_HAVE_ALARM = loop_mod._alarm_capable()
_alarm_only = pytest.mark.skipif(
    not _HAVE_ALARM, reason="requires POSIX SIGALRM on the main thread"
)


@_alarm_only
def test_sync_act_kill_via_sigalrm():
    """A blocking synchronous act is interrupted by SIGALRM under kill mode."""

    def blocking_act(_ctx):
        time.sleep(5.0)  # interrupted at the deadline
        return ActOutcome(tokens=1)

    started = time.monotonic()
    with pytest.raises(SeamTimeout) as exc:
        run_loop(
            act=blocking_act,
            verify=never_done,
            conditions=[MaxIterations(10)],
            timeout=TimeoutPolicy(act=0.05, on_timeout=TIMEOUT_KILL),
        )
    assert exc.value.seam == "act"
    assert time.monotonic() - started < 2.0  # really interrupted, not slept 5s


@_alarm_only
def test_sync_act_graceful_via_sigalrm():
    """A blocking synchronous act under graceful mode records a step and continues."""

    def blocking_act(_ctx):
        time.sleep(5.0)
        return ActOutcome(tokens=1)

    started = time.monotonic()
    result = run_loop(
        act=blocking_act,
        verify=never_done,
        conditions=[MaxIterations(3)],
        timeout=TimeoutPolicy(act=0.05, on_timeout=TIMEOUT_GRACEFUL),
    )
    assert result.iterations == 3
    assert result.tokens_used == 0
    assert [r.observation for r in result.history] == [ACT_TIMEOUT_OBSERVATION] * 3
    assert time.monotonic() - started < 3.0


@_alarm_only
def test_sync_seam_under_alarm_completes_normally_when_fast():
    """SIGALRM guarding does not disturb a fast synchronous seam."""
    result = run_loop(
        act=acting(tokens=2),
        verify=done_after(3),
        conditions=[MaxIterations(10)],
        timeout=TimeoutPolicy(default=5.0, on_timeout=TIMEOUT_KILL),
    )
    assert result.succeeded is True
    assert result.iterations == 3
    assert result.tokens_used == 6


# -- synchronous seam without SIGALRM (Windows / non-main-thread) ------------


def test_sync_kill_unsupported_without_alarm(monkeypatch):
    """Hard-kill of a synchronous seam is refused up front where SIGALRM is absent."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    with pytest.raises(UnsupportedTimeoutKill):
        run_loop(
            act=acting(tokens=1),  # synchronous
            verify=never_done,
            conditions=[MaxIterations(10)],
            timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
        )


def test_sync_graceful_post_hoc_detection_without_alarm(monkeypatch):
    """Without SIGALRM, graceful detects an overrun *after* the sync call returns.

    Modelled with a ManualClock the seam advances past the deadline -- the
    completed call is then judged a timeout and a synthetic step is recorded.
    """
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    clock = ManualClock()
    result = run_loop(
        act=stepping_for(clock, seconds=10.0),  # advances the clock by 10s per call
        verify=never_done,
        conditions=[MaxIterations(2)],
        time_fn=clock,
        timeout=TimeoutPolicy(act=1.0, on_timeout=TIMEOUT_GRACEFUL),
    )
    assert result.iterations == 2
    assert [r.observation for r in result.history] == [ACT_TIMEOUT_OBSERVATION] * 2


def test_sync_graceful_no_trip_when_within_deadline(monkeypatch):
    """Post-hoc graceful: a fast sync call within the deadline runs normally."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    clock = ManualClock()
    result = run_loop(
        act=stepping_for(clock, seconds=0.1, tokens=4),  # well under 5s
        verify=done_after(2),
        conditions=[MaxIterations(10)],
        time_fn=clock,
        timeout=TimeoutPolicy(act=5.0, on_timeout=TIMEOUT_GRACEFUL),
    )
    assert result.succeeded is True
    assert result.iterations == 2
    assert result.tokens_used == 8


# -- run_loop still rejects an async seam even with a timeout configured -----


def test_run_loop_rejects_async_seam_with_timeout():
    from loop_agent import AsyncSeamInSyncLoop

    with pytest.raises(AsyncSeamInSyncLoop):
        run_loop(
            act=sleeping_aact(delay=0.0, tokens=1),  # async
            verify=never_done,
            conditions=[MaxIterations(5)],
            timeout=TimeoutPolicy(act=1.0, on_timeout=TIMEOUT_GRACEFUL),
        )


# -- parity: timeout=None leaves behaviour byte-for-byte unchanged ----------


def test_timeout_none_is_parity():
    base = run_loop(act=acting(tokens=3), verify=done_after(4), conditions=[MaxIterations(10)])
    timed = run_loop(
        act=acting(tokens=3),
        verify=done_after(4),
        conditions=[MaxIterations(10)],
        timeout=None,
    )
    assert (base.status, base.iterations, base.tokens_used) == (
        timed.status,
        timed.iterations,
        timed.tokens_used,
    )


# -- on_step fires for synthetic graceful-timeout steps ---------------------


def test_on_step_fires_for_synthetic_timeout_steps():
    seen = []

    def on_step(record, state):
        seen.append((record.observation, record.iteration, state.iteration))

    asyncio.run(
        async_run_loop(
            act=sleeping_aact(delay=5.0),
            verify=afast_verify,
            conditions=[MaxIterations(2)],
            on_step=on_step,
            timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert seen == [
        (ACT_TIMEOUT_OBSERVATION, 0, 1),
        (ACT_TIMEOUT_OBSERVATION, 1, 2),
    ]


# -- gate lease + graceful timeout (gate_on_complete fired once) ------------


class _ProceedOnCompleteGate:
    """Minimal ActionGate that proceeds and counts on_complete (lease) calls."""

    def __init__(self) -> None:
        self.reviews = 0
        self.completes = 0

    def review(self, context, state):
        self.reviews += 1

        def _complete():
            self.completes += 1

        return GateReview(disposition=GATE_PROCEED, on_complete=_complete)


def test_gate_on_complete_fires_on_graceful_act_timeout():
    gate = _ProceedOnCompleteGate()
    result = asyncio.run(
        async_run_loop(
            act=sleeping_aact(delay=5.0),
            verify=afast_verify,
            conditions=[MaxIterations(2)],
            gate=gate,
            timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert result.iterations == 2
    # The synthetic step is recorded AND the lease is confirmed executed, once each.
    assert gate.completes == 2
    assert [r.observation for r in result.history] == [ACT_TIMEOUT_OBSERVATION] * 2


def test_gate_on_complete_fires_on_graceful_verify_timeout():
    gate = _ProceedOnCompleteGate()
    result = asyncio.run(
        async_run_loop(
            act=acting(tokens=1),
            verify=sleeping_averify(delay=5.0),
            conditions=[MaxIterations(2)],
            gate=gate,
            timeout=TimeoutPolicy(verify=0.01, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert gate.completes == 2
    assert [r.observation for r in result.history] == [VERIFY_TIMEOUT_OBSERVATION] * 2


# -- _looks_async heuristic on the no-SIGALRM kill path ---------------------


def test_looks_async_partial_allows_kill_without_alarm(monkeypatch):
    """functools.partial-wrapped async seam is recognised async (kill via wait_for)."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    part = functools.partial(sleeping_aact(delay=5.0))
    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=part,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


def test_looks_async_partial_of_async_call_instance_allows_kill_without_alarm(monkeypatch):
    """functools.partial wrapping an instance with async __call__ is recognised async."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)

    class AsyncCallAct:
        async def __call__(self, _ctx):
            await asyncio.sleep(5.0)
            return ActOutcome(tokens=1)

    part = functools.partial(AsyncCallAct())
    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=part,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


def test_looks_async_async_dunder_call_allows_kill_without_alarm(monkeypatch):
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)

    class AsyncCallAct:
        async def __call__(self, _ctx):
            await asyncio.sleep(5.0)
            return ActOutcome(tokens=1)

    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=AsyncCallAct(),
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


def test_looks_async_plain_returning_coroutine_refused_kill_without_alarm(monkeypatch):
    """A plain def that RETURNS a coroutine is conservatively treated as sync ->
    kill refused up front (documented: use async def for guaranteed kill)."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)

    def plain_returns_coro(_ctx):
        async def inner():
            return ActOutcome(tokens=1)

        return inner()

    with pytest.raises(UnsupportedTimeoutKill):
        asyncio.run(
            async_run_loop(
                act=plain_returns_coro,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


# -- synchronous VERIFY seam timeout ----------------------------------------


@_alarm_only
def test_sync_verify_kill_via_sigalrm():
    def blocking_verify(_outcome):
        time.sleep(5.0)
        return VerifyOutcome(goal_met=True)

    started = time.monotonic()
    with pytest.raises(SeamTimeout) as exc:
        run_loop(
            act=acting(tokens=2),
            verify=blocking_verify,
            conditions=[MaxIterations(5)],
            timeout=TimeoutPolicy(verify=0.05, on_timeout=TIMEOUT_KILL),
        )
    assert exc.value.seam == "verify"
    assert time.monotonic() - started < 2.0


def test_sync_verify_graceful_post_hoc_without_alarm(monkeypatch):
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    clock = ManualClock()

    def slow_verify(_outcome):
        clock.advance(10.0)  # overruns the 1.0s deadline
        return VerifyOutcome(goal_met=True)

    result = run_loop(
        act=acting(tokens=3),
        verify=slow_verify,
        conditions=[MaxIterations(2)],
        time_fn=clock,
        timeout=TimeoutPolicy(verify=1.0, on_timeout=TIMEOUT_GRACEFUL),
    )
    assert result.iterations == 2
    assert [r.observation for r in result.history] == [VERIFY_TIMEOUT_OBSERVATION] * 2
    assert result.tokens_used == 6  # act tokens (3/iter) retained across verify timeout


# -- real (un-monkeypatched) no-SIGALRM fallback: worker thread -------------


def test_real_worker_thread_refuses_sync_kill():
    """Off the main thread, _alarm_capable() is genuinely False -> sync kill refused."""
    captured = {}

    def worker():
        try:
            run_loop(
                act=acting(tokens=1),  # synchronous
                verify=never_done,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        except BaseException as exc:  # noqa: BLE001 - capturing across the thread
            captured["exc"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert isinstance(captured.get("exc"), UnsupportedTimeoutKill)


# -- SIGALRM restores a pre-existing interval timer -------------------------


@_alarm_only
def test_sigalrm_restores_prior_itimer():
    """An embedder's own ITIMER_REAL must survive a per-call sync timeout."""
    prev_handler = signal.signal(signal.SIGALRM, lambda *_a: None)
    signal.setitimer(signal.ITIMER_REAL, 100.0)
    try:
        run_loop(
            act=acting(tokens=1),  # fast sync seam, guarded by SIGALRM
            verify=done_after(1),
            conditions=[MaxIterations(5)],
            timeout=TimeoutPolicy(act=5.0, on_timeout=TIMEOUT_GRACEFUL),
        )
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        # Restored (re-armed), not clobbered to 0 by our finally.
        assert 0.0 < remaining <= 100.0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prev_handler)


@_alarm_only
def test_sync_seam_own_exception_propagates_and_disarms_under_alarm():
    """A guarded sync seam raising its own exception propagates it AND leaves our
    SIGALRM timer disarmed (teardown runs on the exception path too)."""

    def boom(_ctx):
        raise ValueError("seam boom")

    with pytest.raises(ValueError, match="seam boom"):
        run_loop(
            act=boom,
            verify=never_done,
            conditions=[MaxIterations(5)],
            timeout=TimeoutPolicy(act=5.0, on_timeout=TIMEOUT_KILL),
        )
    remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
    assert remaining == 0.0  # our 5s timer was disarmed, not left armed


# -- single budget: a sync prefix that overruns trips before awaiting --------


def test_no_alarm_sync_prefix_overrun_trips_before_await(monkeypatch):
    """No-SIGALRM: a seam whose synchronous prefix blows the (real wall-clock)
    deadline before returning an awaitable trips immediately, not a fresh budget.

    The prefix is measured on the real monotonic clock (not the injectable
    time_fn), so this uses a small real sleep.
    """
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)

    def blocking_then_coro(_ctx):
        time.sleep(0.1)  # synchronous prefix exceeds the 0.02s deadline

        async def inner():
            return ActOutcome(observation="should-not-run", tokens=5)

        return inner()

    result = asyncio.run(
        async_run_loop(
            act=blocking_then_coro,
            verify=afast_verify,
            conditions=[MaxIterations(2)],
            timeout=TimeoutPolicy(act=0.02, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert result.iterations == 2
    assert [r.observation for r in result.history] == [ACT_TIMEOUT_OBSERVATION] * 2
    assert result.tokens_used == 0  # the returned awaitable was never awaited


def test_prefix_overrun_cancels_returned_task(monkeypatch):
    """When the sync prefix exhausts the budget and the seam returned a scheduled
    Task, that Task is cancelled (not left running side effects in background)."""
    monkeypatch.setattr(loop_mod, "_alarm_capable", lambda: False)
    ran = {"flag": False}

    async def background():
        await asyncio.sleep(1.0)
        ran["flag"] = True
        return ActOutcome(tokens=1)

    def blocking_then_task(_ctx):
        time.sleep(0.05)  # synchronous prefix exceeds the 0.02s deadline
        return asyncio.ensure_future(background())  # an already-scheduled Task

    result = asyncio.run(
        async_run_loop(
            act=blocking_then_task,
            verify=afast_verify,
            conditions=[MaxIterations(1)],
            timeout=TimeoutPolicy(act=0.02, on_timeout=TIMEOUT_GRACEFUL),
        )
    )
    assert result.iterations == 1
    assert result.history[0].observation == ACT_TIMEOUT_OBSERVATION
    assert ran["flag"] is False  # the returned Task was cancelled, never ran


# -- seam's OWN TimeoutError is preserved, not conflated with ours ----------


def test_seam_own_timeout_error_propagates_kill():
    """An asyncio.TimeoutError raised BY the seam (before our deadline) must
    propagate as-is, not be converted into SeamTimeout."""

    async def act_raises_timeout(_ctx):
        raise asyncio.TimeoutError("inner network timeout")

    with pytest.raises(asyncio.TimeoutError, match="inner"):
        asyncio.run(
            async_run_loop(
                act=act_raises_timeout,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=5.0, on_timeout=TIMEOUT_KILL),
            )
        )


def test_seam_own_timeout_error_propagates_graceful():
    """Even in graceful mode, the seam's own TimeoutError surfaces (the loop does
    NOT swallow it as a synthetic timeout step and continue)."""

    async def verify_raises_timeout(_outcome):
        raise asyncio.TimeoutError("inner")

    with pytest.raises(asyncio.TimeoutError, match="inner"):
        asyncio.run(
            async_run_loop(
                act=acting(tokens=1),
                verify=verify_raises_timeout,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(verify=5.0, on_timeout=TIMEOUT_GRACEFUL),
            )
        )


# -- cooperative cancellation: kill wins even if the seam swallows CancelledError --


def test_async_kill_wins_over_cancellederror_swallow():
    """A seam that swallows CancelledError and returns is still killed: the
    timeout is decided by the task being PENDING at the deadline, so SeamTimeout
    is raised rather than the swallowed value being returned."""

    async def swallowing_act(_ctx):
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return ActOutcome(observation="swallowed", tokens=1)
        return ActOutcome(tokens=1)

    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=swallowing_act,
                verify=done_after(1),
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.01, on_timeout=TIMEOUT_KILL),
            )
        )


def test_outer_cancellation_cancels_inflight_seam_task():
    """If async_run_loop is cancelled while a timed async seam is in flight, the
    inner seam task is cancelled too (no background side effects) -- parity with
    the direct-await path."""
    ran = {"flag": False}

    async def slow_act(_ctx):
        await asyncio.sleep(0.1)
        ran["flag"] = True
        return ActOutcome(tokens=1)

    async def driver():
        loop_task = asyncio.ensure_future(
            async_run_loop(
                act=slow_act,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=10.0, on_timeout=TIMEOUT_KILL),
            )
        )
        await asyncio.sleep(0.02)  # let the loop enter the seam
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        await asyncio.sleep(0.2)  # an un-cancelled seam would set the flag by now
        assert ran["flag"] is False

    asyncio.run(driver())


def test_async_kill_bounds_despite_slow_cancellation_cleanup():
    """The timeout reliably bounds the call: a seam that swallows CancelledError
    and then runs slow cleanup must not delay SeamTimeout (we do not await its
    cleanup)."""

    async def stubborn_act(_ctx):
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            await asyncio.sleep(5.0)  # slow cleanup after swallowing cancellation
            return ActOutcome(tokens=1)
        return ActOutcome(tokens=1)

    t0 = time.monotonic()
    with pytest.raises(SeamTimeout):
        asyncio.run(
            async_run_loop(
                act=stubborn_act,
                verify=afast_verify,
                conditions=[MaxIterations(5)],
                timeout=TimeoutPolicy(act=0.02, on_timeout=TIMEOUT_KILL),
            )
        )
    assert time.monotonic() - t0 < 2.0  # not blocked on the 5s cleanup
