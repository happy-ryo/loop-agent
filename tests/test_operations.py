"""Operational helper tests."""

from __future__ import annotations

from loop_agent import (
    ACT_TIMEOUT_OBSERVATION,
    AdapterFailureBreaker,
    ActOutcome,
    LaunchThrottleDecision,
    ListSink,
    LoopState,
    MaxIterations,
    PerStepTokenCap,
    SpikeDetector,
    StepRecord,
    TimeoutMarkerBreaker,
    VerifyDetailBreaker,
    VerifyOutcome,
    launch_throttle_decision,
    render_dashboard_html,
    scan_spikes,
    detect_spikes,
    run_loop,
    step_throttle,
)
from loop_agent.operations import LOOP_SPIKE


def _state(records):
    return LoopState(
        iteration=len(records),
        tokens_used=sum(r.tokens for r in records),
        elapsed=float(len(records)),
        history=list(records),
    )


def test_detect_spikes_token_spike():
    records = [
        StepRecord(i, f"s{i}", tokens=t, goal_met=False)
        for i, t in enumerate([10, 12, 11, 50])
    ]
    spikes = detect_spikes(_state(records), token_window=3, multiplier=3.0)
    assert [s.kind for s in spikes] == ["token"]
    assert spikes[0].payload["tokens"] == 50


def test_detect_spikes_latency_spike():
    records = [
        StepRecord(i, f"s{i}", tokens=1, goal_met=False)
        for i in range(4)
    ]
    spikes = detect_spikes(
        _state(records),
        elapsed_deltas=[1.0, 1.2, 1.1, 8.0],
        latency_window=3,
        multiplier=3.0,
    )
    assert [s.kind for s in spikes] == ["latency"]


def test_detect_spikes_repeated_failure_and_detail():
    records = [
        StepRecord(i, {"failed": True}, tokens=1, goal_met=False, detail="red")
        for i in range(3)
    ]
    kinds = {s.kind for s in detect_spikes(_state(records), repeated_failure=3)}
    assert {"repeated_failure", "verify_detail"} <= kinds


def test_detect_spikes_timeout_marker():
    records = [
        StepRecord(i, ACT_TIMEOUT_OBSERVATION, tokens=0, goal_met=False)
        for i in range(3)
    ]
    spikes = detect_spikes(_state(records), repeated_failure=3)
    assert [s.kind for s in spikes] == ["timeout"]


def test_spike_detector_emits_events_without_stopping_loop():
    tokens = iter([10, 10, 50])
    sink = ListSink()
    detector = SpikeDetector([sink], token_window=2, multiplier=3.0)

    def act(_ctx):
        return ActOutcome(observation="ok", tokens=next(tokens))

    def verify(_outcome):
        return VerifyOutcome(goal_met=False)

    result = run_loop(
        act=act,
        verify=verify,
        conditions=[MaxIterations(3)],
        on_step=detector.on_step,
    )
    assert result.status == "stopped"
    events = sink.of_kind(LOOP_SPIKE)
    assert len(events) == 1
    assert events[0].payload["spike"] == "token"


def test_scan_spikes_from_persisted_step_rows():
    steps = [
        {"iteration": 0, "tokens": 10, "tokens_used": 10, "elapsed": 1.0,
         "goal_met": False, "observation": "a", "detail": ""},
        {"iteration": 1, "tokens": 10, "tokens_used": 20, "elapsed": 2.0,
         "goal_met": False, "observation": "b", "detail": ""},
        {"iteration": 2, "tokens": 50, "tokens_used": 70, "elapsed": 8.0,
         "goal_met": False, "observation": "c", "detail": ""},
    ]
    found = scan_spikes(steps, token_window=2, latency_window=2, multiplier=3.0)
    assert [(iteration, spike.kind) for iteration, spike in found] == [
        (2, "token"),
        (2, "latency"),
    ]


def test_circuit_breaker_helpers():
    failed = _state([
        StepRecord(i, {"failed": True}, tokens=1, goal_met=False)
        for i in range(3)
    ])
    assert AdapterFailureBreaker(3).check(failed)

    repeated_detail = _state([
        StepRecord(i, "x", tokens=1, goal_met=False, detail="same")
        for i in range(2)
    ])
    assert VerifyDetailBreaker(2).check(repeated_detail)

    timeout = _state([
        StepRecord(i, ACT_TIMEOUT_OBSERVATION, tokens=0, goal_met=False)
        for i in range(2)
    ])
    assert TimeoutMarkerBreaker(2).check(timeout)

    spend = _state([StepRecord(0, "x", tokens=101, goal_met=False)])
    assert PerStepTokenCap(100).check(spend)


def test_launch_throttle_decision():
    assert launch_throttle_decision(running=1, max_running=2) == (
        LaunchThrottleDecision(True, "allowed")
    )
    assert not launch_throttle_decision(running=2, max_running=2).allow
    assert not launch_throttle_decision(
        running=0, recent_spikes=3, max_recent_spikes=2
    ).allow


def test_step_throttle_uses_injected_sleep():
    calls = []

    def act(ctx):
        calls.append(("act", ctx))
        return "done"

    def sleep(seconds):
        calls.append(("sleep", seconds))

    wrapped = step_throttle(act, delay_seconds=1.5, sleep=sleep)
    assert wrapped("ctx") == "done"
    assert calls == [("sleep", 1.5), ("act", "ctx")]


def test_render_dashboard_html_contains_runs_steps_pending_and_reflexion():
    html = render_dashboard_html(
        runs=[{"run_id": "r1", "status": "stopped", "iterations": 1,
               "tokens_used": 10, "elapsed": 1.2}],
        steps_by_run={"r1": [{"iteration": 0, "tokens": 10, "tokens_used": 10,
                              "elapsed": 1.2, "goal_met": False, "detail": "red"}]},
        pending_by_run={"r1": [{"gate_key": "g", "status": "pending",
                                "created_at": "now"}]},
        events_by_run={"r1": [{"kind": "loop_begin"}]},
        stop_by_run={"r1": {"name": "max_iterations", "reason": "done"}},
        reflexion_runs=[{"run_id": "rr", "status": "stopped", "episode": 2,
                         "epoch": 1, "evaluator_version": "v1",
                         "best_gt_aggregate": 0.5, "reason": "cap"}],
        reflexion_episodes_by_run={"rr": [{"episode": 0}, {"episode": 1}]},
    )
    assert "loop-agent operations dashboard" in html
    assert "r1" in html and "Steps: r1" in html
    assert "max_iterations: done" in html
    assert "rr" in html and "v1" in html
