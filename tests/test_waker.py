"""Wiring verification for loop wakes and transport delivery (Issue #23, report.md S5 Phase3).

Demonstrates, using real ``run_loop`` results, that the three wakes for loop
completion, next iteration, and decision request are built correctly, and that
delivery continues through the primary push path or the pull fallback when the
backend is unavailable.
"""

from __future__ import annotations

from loop_agent import (
    ActOutcome,
    InMemoryWakeQueue,
    LoopWaker,
    LoopStore,
    MaxIterations,
    NullPushBackend,
    Transport,
    WAKE_DECISION_REQUEST,
    WAKE_LOOP_DONE,
    WAKE_NEXT_ITERATION,
    connect,
    run_gated_loop,
    run_loop,
)
from loop_agent.waker import wake_id_for, wakes_for_result

from conftest import ManualClock, acting, done_after, never_done


def _transport(backend=None) -> Transport:
    return Transport(InMemoryWakeQueue(), backend, time_fn=ManualClock())


# -- wakes_for_result (pure mapping) -----------------------------------------


def test_completed_run_yields_done_wake():
    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])
    wakes = wakes_for_result(result, run_id="r1", recipient="coordinator")
    assert len(wakes) == 1
    assert wakes[0].kind == WAKE_LOOP_DONE
    assert wakes[0].payload["succeeded"] is True
    assert wakes[0].recipient == "coordinator"


def test_completed_run_with_next_recipient_adds_next_iteration_wake():
    result = run_loop(act=acting(), verify=never_done, conditions=[MaxIterations(2)])
    wakes = wakes_for_result(
        result, run_id="r1", recipient="coordinator", next_recipient="planner"
    )
    kinds = {w.kind: w for w in wakes}
    assert set(kinds) == {WAKE_LOOP_DONE, WAKE_NEXT_ITERATION}
    assert kinds[WAKE_NEXT_ITERATION].recipient == "planner"
    # The completion wake carries status=stopped (stopped by MaxIterations).
    assert kinds[WAKE_LOOP_DONE].payload["status"] == "stopped"


def test_paused_run_yields_decision_request_only(tmp_path):
    """A run paused by a human gate emits only a decision request wake, not a next-iteration wake."""
    store = LoopStore(connect(tmp_path / "s.db"))

    def gather(state):
        return ["work", "deploy"][state.iteration]

    def act(action):
        return ActOutcome(observation=action, tokens=0)

    result = run_gated_loop(
        act=act,
        verify=never_done,
        conditions=[MaxIterations(3)],
        gather=gather,
        on=lambda a: a == "deploy",
        store=store,
        run_id="rp",
    )
    assert result.paused
    wakes = wakes_for_result(
        result, run_id="rp", recipient="human", next_recipient="planner"
    )
    assert len(wakes) == 1
    assert wakes[0].kind == WAKE_DECISION_REQUEST
    assert "gate_key" in wakes[0].payload


# -- LoopWaker drop-in wiring ------------------------------------------------


def test_loopwaker_delivers_via_push_when_backend_up():
    pushed = []
    from loop_agent import CallablePushBackend

    backend = CallablePushBackend(lambda w: (pushed.append(w.id), True)[1])
    t = _transport(backend)
    waker = LoopWaker(t, run_id="r1", recipient="coordinator")

    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])
    routes = waker.record_result(result)

    assert all(r == "push" for r in routes.values())
    assert pushed  # Delivered through the push path.
    assert t.poll("coordinator") == []  # Nothing remains for pull.


def test_loopwaker_pull_fallback_when_backend_down():
    """LoopWaker wakes reach recipients through pull fallback even when the backend is down."""
    t = _transport(NullPushBackend())
    waker = LoopWaker(
        t, run_id="r1", recipient="coordinator", next_recipient="planner"
    )

    result = run_loop(act=acting(), verify=never_done, conditions=[MaxIterations(2)])
    routes = waker.record_result(result)
    assert all(r == "queued" for r in routes.values())  # All push attempts failed.

    # The coordinator receives the completion wake through pull.
    done = t.poll("coordinator")
    assert [w.kind for w in done] == [WAKE_LOOP_DONE]
    # The planner receives the next-iteration wake through pull.
    nxt = t.poll("planner")
    assert [w.kind for w in nxt] == [WAKE_NEXT_ITERATION]


def test_loopwaker_redeliver_is_idempotent_across_resume():
    """Repeated record_result calls for the same run, as in resume, de-dup by deterministic IDs."""
    t = _transport(NullPushBackend())
    waker = LoopWaker(t, run_id="r1", recipient="coordinator")
    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])

    waker.record_result(result)
    waker.record_result(result)  # Redelivery requested during resume or similar flows.

    assert len(t.poll("coordinator")) == 1  # Not delivered twice.


def test_wake_id_is_deterministic():
    assert wake_id_for("r1", WAKE_LOOP_DONE, 3) == "r1:loop_done:3"
