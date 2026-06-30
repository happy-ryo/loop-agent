"""Operational helper tests."""

from __future__ import annotations

from loop_agent import (
    ACT_TIMEOUT_OBSERVATION,
    ActOutcome,
    ListSink,
    LoopState,
    MaxIterations,
    SpikeDetector,
    StepRecord,
    VerifyOutcome,
    detect_spikes,
    run_loop,
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
