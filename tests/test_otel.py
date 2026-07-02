"""Tests for OTel GenAI span integration (report.md S4.5).

Cover two aspects:
- **active path**: when opentelemetry-sdk is installed, the whole run becomes one span;
  gen_ai.* attributes, the iteration count, and the termination reason are recorded as
  attributes; and each iteration is recorded as a span event. Inspect the span with an
  in-memory exporter (skip if unavailable).
- **degrade path**: when OTel is disabled or unavailable, LoopSpan becomes a no-op and
  the observer sink side still works. otel=False always verifies degradation.
"""

from __future__ import annotations

import pytest

from loop_agent import (
    LoopObserver,
    LoopSpan,
    MaxIterations,
    Timeout,
    TokenBudget,
    ListSink,
    otel_available,
    run_observed_loop,
)
from loop_agent.otel import (
    ATTR_ITERATIONS,
    ATTR_STATUS,
    ATTR_STOP,
    ATTR_TERMINATION_REASON,
    ATTR_TOKENS_USED,
    GEN_AI_OPERATION_NAME,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from conftest import ManualClock, acting, done_after, never_done, stepping_for


# -- degrade path (does not break when OTel is absent or disabled) ----------


def test_loop_span_noop_when_disabled():
    span = LoopSpan(enabled=False)
    span.start()
    assert span.recording is False
    # All methods remain callable without raising, even as no-ops.
    span.add_step(iteration=0, tokens=0, tokens_used=0, elapsed=0.0, goal_met=False)
    span.end(status="stopped", reason="x", iterations=0, tokens_used=0, elapsed=0.0)


def test_loop_span_degrades_when_otel_unavailable(monkeypatch):
    # Simulate OTel being unavailable: with _OTEL_AVAILABLE=False, even a tracer is a no-op.
    import loop_agent.otel as otel_mod

    monkeypatch.setattr(otel_mod, "_OTEL_AVAILABLE", False)
    span = otel_mod.LoopSpan(tracer="would-be-a-tracer", enabled=True)
    span.start()
    assert span.recording is False
    span.add_step(iteration=0, tokens=1, tokens_used=1, elapsed=0.0, goal_met=False)
    span.end(status="goal_met", reason="goal met", iterations=1, tokens_used=1, elapsed=0.0)
    assert otel_mod.otel_available() is False


def test_observed_loop_runs_with_otel_disabled():
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=1),
        verify=done_after(1),
        conditions=[MaxIterations(3)],
        sinks=[sink],
        otel=False,
    )
    assert result.goal_met
    assert [e.kind for e in sink.events][0] == "loop_begin"
    assert [e.kind for e in sink.events][-1] == "loop_end"


# -- active path (inspect spans with an in-memory exporter) -----------------

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


@pytest.fixture
def otel_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return tracer, exporter


def test_otel_available_is_true_when_sdk_present():
    assert otel_available() is True


def test_run_creates_single_span_with_genai_attributes(otel_tracer):
    tracer, exporter = otel_tracer
    run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(3)],
        tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "loop_agent.loop"
    attrs = dict(span.attributes)
    assert attrs[GEN_AI_OPERATION_NAME] == "loop"
    assert attrs[GEN_AI_SYSTEM] == "loop_agent"
    assert attrs[ATTR_STATUS] == "stopped"
    assert attrs[ATTR_STOP] == "max_iterations"
    assert attrs[ATTR_ITERATIONS] == 3
    assert attrs[ATTR_TOKENS_USED] == 30
    assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 30
    assert "max iterations" in attrs[ATTR_TERMINATION_REASON]


def test_each_iteration_is_a_span_event(otel_tracer):
    tracer, exporter = otel_tracer
    run_observed_loop(
        act=acting(tokens=5),
        verify=never_done,
        conditions=[MaxIterations(4)],
        tracer=tracer,
    )
    span = exporter.get_finished_spans()[0]
    step_events = [e for e in span.events if e.name == "loop_step"]
    assert len(step_events) == 4
    assert [e.attributes["iteration"] for e in step_events] == [0, 1, 2, 3]
    assert [e.attributes["tokens_used"] for e in step_events] == [5, 10, 15, 20]


@pytest.mark.parametrize(
    "make_run, expected_stop, expected_status",
    [
        (
            lambda t: run_observed_loop(
                act=acting(tokens=1), verify=done_after(2),
                conditions=[MaxIterations(9)], tracer=t,
            ),
            None,
            "goal_met",
        ),
        (
            lambda t: run_observed_loop(
                act=acting(tokens=1), verify=never_done,
                conditions=[MaxIterations(2)], tracer=t,
            ),
            "max_iterations",
            "stopped",
        ),
        (
            lambda t: run_observed_loop(
                act=acting(tokens=30), verify=never_done,
                conditions=[TokenBudget(50), MaxIterations(99)], tracer=t,
            ),
            "token_budget",
            "stopped",
        ),
    ],
)
def test_termination_reasons_land_on_span(otel_tracer, make_run, expected_stop, expected_status):
    tracer, exporter = otel_tracer
    make_run(tracer)
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs[ATTR_STATUS] == expected_status
    if expected_stop is None:
        assert ATTR_STOP not in attrs  # goal_met does not set the stop attribute
    else:
        assert attrs[ATTR_STOP] == expected_stop


def test_timeout_termination_lands_on_span(otel_tracer):
    tracer, exporter = otel_tracer
    clock = ManualClock()
    run_observed_loop(
        act=stepping_for(clock, seconds=1.0),
        verify=never_done,
        conditions=[Timeout(3.0), MaxIterations(99)],
        tracer=tracer,
        time_fn=clock,
    )
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs[ATTR_STOP] == "timeout"
    assert "timed out" in attrs[ATTR_TERMINATION_REASON]


def test_exception_marks_span_error(otel_tracer):
    from opentelemetry.trace import StatusCode

    tracer, exporter = otel_tracer

    def boom(_ctx):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_loop(
            act=boom,
            verify=never_done,
            conditions=[MaxIterations(3)],
            tracer=tracer,
        )
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert dict(span.attributes)[ATTR_STATUS] == "error"
    # The exception is recorded on the span.
    assert any(e.name == "exception" for e in span.events)


def test_misbehaving_tracer_does_not_crash_the_loop(monkeypatch):
    # The observation layer does not kill the loop: even if the tracer/span raises,
    # it handles the error best-effort, emits a warning, lets the loop finish, and
    # keeps the event sink intact through normal completion.
    import loop_agent.otel as otel_mod
    import warnings

    monkeypatch.setattr(otel_mod, "_OTEL_AVAILABLE", True)

    class FlakySpan:
        def __init__(self):
            self.ended = False

        def set_attribute(self, *_a):
            pass

        def add_event(self, *_a, **_k):
            raise RuntimeError("add_event boom")

        def record_exception(self, *_a):
            pass

        def set_status(self, *_a):
            pass

        def end(self):
            self.ended = True

    span = FlakySpan()

    class FlakyTracer:
        def start_span(self, _name):
            return span

    sink = ListSink()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_observed_loop(
            act=acting(tokens=5),
            verify=never_done,
            conditions=[MaxIterations(3)],
            sinks=[sink],
            tracer=FlakyTracer(),
        )
    # The loop completes, the result and events are intact, and the span is closed.
    assert result.status == "stopped"
    assert [e.kind for e in sink.events][-1] == "loop_end"
    assert span.ended is True
    assert any("add_event" in str(w.message) for w in caught)


def test_start_failure_degrades_to_noop(monkeypatch):
    # Even if start_span itself fails, subsequent operations degrade to no-ops and the loop completes.
    import loop_agent.otel as otel_mod

    monkeypatch.setattr(otel_mod, "_OTEL_AVAILABLE", True)

    class ExplodingTracer:
        def start_span(self, _name):
            raise RuntimeError("cannot start")

    span = otel_mod.LoopSpan(tracer=ExplodingTracer(), enabled=True)
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        span.start()
    assert span.recording is False
    assert any("start" in str(w.message) for w in caught)


def test_default_tracer_used_when_none_supplied():
    # With tracer=None, the default OTel tracer still runs safely, even without a global provider.
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=0),
        verify=done_after(1),
        conditions=[MaxIterations(2)],
        sinks=[sink],
    )
    assert result.goal_met
    assert sink.of_kind("loop_end")[0].payload["status"] == "goal_met"
