"""Tests for observation orchestration (report.md S5 Phase 2 success condition (b)).

Cover the core requirements:
- all termination reasons (goal_met / max_iterations / token_budget / timeout) remain in
  the loop_end event,
- metrics (iteration number, cumulative tokens, elapsed) are traceable from begin to step to end,
- begin/step/end order and counts,
- an error loop_end remains even when the loop body exits with an exception,
- the user's on_step and the observation hook are composed,
- sink observation continues to work unchanged when OTel is disabled (otel=False) (degrade).
"""

from __future__ import annotations

import pytest

from loop_agent import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    ActOutcome,
    JsonlEventSink,
    ListSink,
    LoopObserver,
    LoopState,
    MaxIterations,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    read_events,
    run_loop,
    run_observed_loop,
)
from conftest import ManualClock, acting, done_after, never_done, stepping_for


def _kinds(sink):
    return [e.kind for e in sink.events]


def _only(sink, kind):
    evs = sink.of_kind(kind)
    assert len(evs) == 1, f"expected exactly one {kind}, got {len(evs)}"
    return evs[0]


# -- begin / step / end skeleton --------------------------------------------


def test_emits_begin_steps_end_in_order(tmp_path):
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        otel=False,
    )
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_STEP, LOOP_END]


def test_begin_carries_condition_names():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=0),
        verify=done_after(1),
        conditions=[MaxIterations(5), TokenBudget(100)],
        sinks=[sink],
        otel=False,
    )
    begin = _only(sink, LOOP_BEGIN)
    assert begin.payload["conditions"] == ["max_iterations", "token_budget"]


def test_zero_iteration_run_still_emits_begin_and_end():
    # MaxIterations(0) stops immediately: there are no steps, but begin/end always remain.
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(0)],
        sinks=[sink],
        otel=False,
    )
    assert result.iterations == 0
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_END]
    assert _only(sink, LOOP_END).payload["status"] == "stopped"


# -- All termination reasons remain in loop_end -----------------------------


def test_goal_met_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=1),
        verify=done_after(2),
        conditions=[MaxIterations(10)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "goal_met"
    assert end.payload["stop"] is None
    assert end.payload["goal_met"] is True
    assert end.payload["reason"] == "goal met"
    assert end.payload["iterations"] == 2


def test_max_iterations_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=5),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "max_iterations"
    assert "max iterations" in end.payload["reason"]


def test_token_budget_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=40),
        verify=never_done,
        conditions=[TokenBudget(100), MaxIterations(100)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "token_budget"
    assert "token budget" in end.payload["reason"]


def test_timeout_reason_in_end_event():
    clock = ManualClock()
    sink = ListSink()
    run_observed_loop(
        act=stepping_for(clock, seconds=1.0, tokens=0),
        verify=never_done,
        conditions=[Timeout(3.0), MaxIterations(100)],
        sinks=[sink],
        otel=False,
        time_fn=clock,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "timeout"
    assert "timed out" in end.payload["reason"]


@pytest.mark.parametrize(
    "conditions, act, verify, expected_stop",
    [
        ([MaxIterations(10)], acting(tokens=1), done_after(2), None),
        ([MaxIterations(3)], acting(tokens=5), never_done, "max_iterations"),
        ([TokenBudget(50), MaxIterations(99)], acting(tokens=25), never_done, "token_budget"),
    ],
)
def test_all_non_timeout_terminations_recorded(
    conditions, act, verify, expected_stop
):
    # Use one parameter table to comprehensively verify that all termination reasons remain in end.
    sink = ListSink()
    result = run_observed_loop(
        act=act, verify=verify, conditions=conditions, sinks=[sink], otel=False
    )
    end = _only(sink, LOOP_END)
    assert end.payload["stop"] == expected_stop
    assert end.payload["reason"] == result.reason
    assert end.payload["status"] == result.status


# -- Metrics are traceable from begin to step to end ------------------------


def test_metrics_are_traceable_across_events():
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(4)],
        sinks=[sink],
        otel=False,
    )
    steps = sink.of_kind(LOOP_STEP)
    # Iteration numbers are 0..3, and cumulative tokens increase monotonically as 10, 20, 30, 40.
    assert [s.iteration for s in steps] == [0, 1, 2, 3]
    assert [s.payload["tokens_used"] for s in steps] == [10, 20, 30, 40]
    assert all(s.payload["tokens"] == 10 for s in steps)
    # elapsed is non-decreasing.
    elapsed = [s.elapsed for s in steps]
    assert elapsed == sorted(elapsed)
    # The end aggregate matches the loop result and is consistent with the final step's cumulative value.
    end = _only(sink, LOOP_END)
    assert end.payload["iterations"] == result.iterations == 4
    assert end.payload["tokens_used"] == steps[-1].payload["tokens_used"] == 40


def test_step_event_carries_observation_and_detail():
    sink = ListSink()

    def verify(_outcome):
        return VerifyOutcome(goal_met=True, detail="done!")

    run_observed_loop(
        act=acting(tokens=0, observation={"k": "v"}),
        verify=verify,
        conditions=[MaxIterations(5)],
        sinks=[sink],
        otel=False,
    )
    step = sink.of_kind(LOOP_STEP)[0]
    assert step.payload["observation"] == {"k": "v"}
    assert step.payload["detail"] == "done!"
    assert step.payload["goal_met"] is True


def test_non_serializable_observation_stored_as_repr():
    sink = ListSink()

    class Widget:
        def __repr__(self):
            return "Widget(z)"

    def act(_ctx):
        return ActOutcome(observation=Widget(), tokens=0)

    run_observed_loop(
        act=act, verify=never_done, conditions=[MaxIterations(1)], sinks=[sink], otel=False
    )
    assert sink.of_kind(LOOP_STEP)[0].payload["observation"] == "Widget(z)"


# -- Multiple sinks / post-hoc analysis from JSONL -------------------------


def test_events_persist_to_jsonl_for_post_hoc_analysis(tmp_path):
    path = tmp_path / "events.jsonl"
    mem = ListSink()
    run_observed_loop(
        act=acting(tokens=7),
        verify=never_done,
        conditions=[MaxIterations(2)],
        sinks=[JsonlEventSink(path), mem],
        otel=False,
    )
    # Both sinks receive the same event sequence.
    on_disk = read_events(path)
    assert [r["kind"] for r in on_disk] == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    assert [e.kind for e in mem.events] == [r["kind"] for r in on_disk]
    assert on_disk[-1]["stop"] == "max_iterations"
    assert on_disk[-1]["tokens_used"] == 14


# -- Composition with the user's on_step ------------------------------------


def test_user_on_step_is_composed_with_observer():
    sink = ListSink()
    seen = []

    run_observed_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        on_step=lambda record, state: seen.append(record.iteration),
        otel=False,
    )
    assert seen == [0, 1, 2]  # The user hook is also called on each iteration.
    assert len(sink.of_kind(LOOP_STEP)) == 3  # The observation hook also remains active.


def test_observer_emits_step_only_after_user_on_step_succeeds():
    sink = ListSink()

    def fail_on_step(_record, _state):
        raise RuntimeError("db write failed")

    with pytest.raises(RuntimeError, match="db write failed"):
        run_observed_loop(
            act=acting(tokens=0),
            verify=never_done,
            conditions=[MaxIterations(1)],
            sinks=[sink],
            on_step=fail_on_step,
            otel=False,
        )
    assert not sink.of_kind(LOOP_STEP)
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"


# -- Exception path: error loop_end -----------------------------------------


def test_exception_in_act_records_error_end_and_reraises():
    sink = ListSink()

    def boom(_ctx):
        raise ValueError("act exploded")

    with pytest.raises(ValueError, match="act exploded"):
        run_observed_loop(
            act=boom,
            verify=never_done,
            conditions=[MaxIterations(3)],
            sinks=[sink],
            otel=False,
        )
    # begin has been emitted, end remains as error, and the exception propagates.
    assert sink.of_kind(LOOP_BEGIN)
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"
    assert "ValueError" in end.payload["reason"]
    assert end.payload["goal_met"] is False


def test_error_end_keeps_metrics_of_completed_iterations():
    # When the loop fails after two successful iterations, the error loop_end keeps the finalized
    # cumulative metrics (iterations=2 / tokens_used=20) instead of 0.
    sink = ListSink()
    calls = {"n": 0}

    def act(_ctx):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("boom on third")
        return ActOutcome(observation="ok", tokens=10)

    with pytest.raises(ValueError, match="boom on third"):
        run_observed_loop(
            act=act,
            verify=never_done,
            conditions=[MaxIterations(10)],
            sinks=[sink],
            otel=False,
        )
    assert len(sink.of_kind(LOOP_STEP)) == 2
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"
    assert end.payload["iterations"] == 2
    assert end.payload["tokens_used"] == 20
    assert end.iteration == 2  # Common fields also use finalized values.


def test_incomplete_path_emits_loop_end_with_last_known_metrics():
    # Case where the context manager exits without an exception, but record_result was forgotten:
    # keep an incomplete loop_end so span and event sink completion observations stay aligned.
    sink = ListSink()
    observer = LoopObserver([sink], otel=False)
    with observer:
        run_loop(
            act=acting(tokens=5),
            verify=never_done,
            conditions=[MaxIterations(2)],
            on_step=observer.on_step,
        )
        # Intentionally do not call record_result.
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "incomplete"
    assert end.payload["iterations"] == 2  # Keep finalized metrics.
    assert end.payload["tokens_used"] == 10
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]


# -- Manual wiring (same pattern as ProgressLog) ----------------------------


def test_manual_wiring_matches_run_observed_loop():
    sink = ListSink()
    observer = LoopObserver([sink], conditions=[MaxIterations(2)], otel=False)
    with observer:
        result = run_loop(
            act=acting(tokens=3),
            verify=never_done,
            conditions=[MaxIterations(2)],
            on_step=observer.on_step,
        )
        observer.record_result(result)
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    assert _only(sink, LOOP_END).payload["tokens_used"] == 6


def test_run_observed_loop_forwards_initial_state_for_resume():
    # The observation entrypoint also passes initial_state through for resume: step/end
    # iteration and cumulative metrics continue from the restored seed (the new run's begin starts at iteration 0).
    sink = ListSink()
    seed = LoopState(iteration=2, tokens_used=20)
    result = run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(4)],
        sinks=[sink],
        otel=False,
        initial_state=seed,
    )
    # Continue from seed iteration 2 -> run 2 steps and stop at cap 4.
    assert result.iterations == 4
    assert result.tokens_used == 40
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    # Step event iterations continue from the restored state (2, 3).
    assert [e.iteration for e in sink.of_kind(LOOP_STEP)] == [2, 3]
    assert _only(sink, LOOP_END).payload["iterations"] == 4
    assert _only(sink, LOOP_END).payload["tokens_used"] == 40
    # seed is not mutated (run_loop copies it).
    assert seed.iteration == 2 and seed.tokens_used == 20


def test_resumed_observed_loop_error_carries_seeded_metrics():
    # Even if an exception occurs during resume before the first new on_step (act, etc.), the error
    # loop_end carries the restored state's cumulative values (does not flatten completed pre-interruption iterations to 0).
    sink = ListSink()
    seed = LoopState(iteration=3, tokens_used=30, elapsed=1.5)

    def boom(_ctx):
        raise RuntimeError("act blew up on resume before any new step")

    with pytest.raises(RuntimeError):
        run_observed_loop(
            act=boom,
            verify=never_done,
            conditions=[MaxIterations(100)],
            sinks=[sink],
            otel=False,
            initial_state=seed,
        )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"
    assert end.payload["iterations"] == 3  # Not flattened to 0.
    assert end.payload["tokens_used"] == 30
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_END]  # There is no new step.


def test_record_result_is_idempotent():
    sink = ListSink()
    observer = LoopObserver([sink], otel=False)
    observer.begin()
    result = run_loop(
        act=acting(tokens=0), verify=done_after(1), conditions=[MaxIterations(2)]
    )
    observer.record_result(result)
    observer.record_result(result)  # The second call is ignored.
    assert len(sink.of_kind(LOOP_END)) == 1
