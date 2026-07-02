"""Tests for outer Reflexion observation (ReflexionObserver + run_observed_reflexion) (Issue #30).

Covered surfaces:

- **transition coverage**: episode begin/end, epoch start/boundary, lesson admission/rejection,
  evaluator promotion/rejection, and convergence reasons are recorded in structured events
  (sink) and OTel span events.
- **metric consistency**: emitted event counts and final aggregates (reflexion_end / span
  attributes) align with the authoritative ``result.state``.
- **optional degrade**: OTel disabled/unavailable falls back to no-op while the event sink
  side continues to work.
- **best-effort**: sink / tracer / observation hook exceptions do not crash the outer driver.
- **decision logic invariant**: ``run_reflexion`` results match with and without observation
  (the safety core remains unchanged).
- **core hook**: ``run_reflexion(on_epoch=...)`` fires correctly at epoch boundaries.
"""

from __future__ import annotations

import warnings
from collections import Counter

import pytest

from loop_agent import (
    EpochRecord,
    Evaluator,
    GroundTruthSignal,
    HeldOut,
    Lesson,
    ListSink,
    MaxEpisodes,
    Probe,
    ReflexionObserver,
    ReflexionSpan,
    RubricThreshold,
    Score,
    ScorePlateau,
    otel_available,
    run_observed_reflexion,
    run_reflexion,
)
from loop_agent.conditions import StopTrigger
from loop_agent.evaluator import AdmissionResult
from loop_agent.loop import LoopResult
from loop_agent.memory import LessonVerdict, step_signature
from loop_agent.reflexion_observe import (
    EPISODE_BEGIN,
    EPISODE_END,
    EPOCH_BOUNDARY,
    LESSON_DECISION,
    REFLEXION_BEGIN,
    REFLEXION_END,
)
from loop_agent.state import LoopState, StepRecord


# -- Shared stubs (same style as test_reflexion.py) ----------------------------

DECLARED = ("primary",)


def make_result(succeeded: bool, observation: object = "obs") -> LoopResult:
    step = StepRecord(iteration=0, observation=observation, tokens=1,
                      goal_met=succeeded, detail=str(observation))
    state = LoopState(iteration=1, history=[step], goal_met=succeeded)
    if succeeded:
        return LoopResult(status="goal_met", stop=None, state=state)
    return LoopResult(
        status="stopped",
        stop=StopTrigger(name="max_iterations", reason="cap"),
        state=state,
    )


def make_paused_result(pending: object = None) -> LoopResult:
    step = StepRecord(iteration=0, observation="obs", tokens=1, goal_met=False, detail="")
    state = LoopState(iteration=1, history=[step], goal_met=False)
    return LoopResult(
        status="paused", stop=None, state=state,
        pending=pending if pending is not None else {"gate_key": "gate-0"},
    )


def gt_from_success(hi: float = 0.9, lo: float = 0.2, backed: bool = True):
    def gt(outcome):
        val = hi if outcome.succeeded else lo
        comps = {k: val for k in DECLARED}
        return GroundTruthSignal(
            succeeded=outcome.succeeded,
            score=Score(ground_truth=val, components=comps),
            ground_truth_backed=backed,
        )

    return gt


def _truth(o):
    if hasattr(o, "succeeded"):
        return 1.0 if o.succeeded else 0.0
    return o["truth"]


HONEST = Evaluator(score=lambda o: Score(ground_truth=_truth(o)), name="honest")
FLAT = Evaluator(score=lambda o: Score(ground_truth=0.5), name="flat")


def held_out_matching(*golds: float) -> HeldOut:
    return HeldOut(
        tuple(Probe(f"probe-{i}", {"truth": g}, gold_label=g) for i, g in enumerate(golds))
    )


def no_reflect(history, signal, reward):
    return None


def true_reflect(history, signal, reward):
    if signal.succeeded:
        return None
    return Lesson(text="use-the-fix", episode=0,
                  provenance=step_signature(history[0]), support=1.0)


def accept_all(lesson, outcome):
    return LessonVerdict(admit=True)


def reject_all(lesson, outcome):
    return LessonVerdict(admit=False, reason="control")


def _base_kwargs(**override):
    kwargs = dict(
        episode=lambda ctx: make_result(False),
        ground_truth=gt_from_success(),
        reflect=no_reflect,
        evaluator=FLAT,
        convergence=[MaxEpisodes(2)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
    )
    kwargs.update(override)
    return kwargs


def kinds(sink: ListSink) -> list[str]:
    return [e.kind for e in sink.events]


# ==============================================================================
# Transition coverage (event sink)
# ==============================================================================


def test_lifecycle_events_emitted_in_order():
    """The minimal lifecycle is recorded as begin -> (episode_begin/end)* -> end."""
    sink = ListSink()
    run_observed_reflexion(**_base_kwargs(sinks=[sink], otel=False))
    ks = kinds(sink)
    assert ks[0] == REFLEXION_BEGIN
    assert ks[-1] == REFLEXION_END
    # begin/end remains for 2 episodes (MaxEpisodes(2)).
    assert ks.count(EPISODE_BEGIN) == 2
    assert ks.count(EPISODE_END) == 2
    # Each episode has begin before end.
    begins = [i for i, e in enumerate(sink.events) if e.kind == EPISODE_BEGIN]
    ends = [i for i, e in enumerate(sink.events) if e.kind == EPISODE_END]
    assert begins[0] < ends[0] < begins[1] < ends[1]


def test_begin_event_carries_config():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, epoch_len=2, epsilon=0.05)
    )
    begin = sink.of_kind(REFLEXION_BEGIN)[0]
    assert begin.payload["conditions"] == ["max_episodes"]
    assert begin.payload["declared_keys"] == list(DECLARED)
    assert begin.payload["epoch_len"] == 2
    assert begin.payload["epsilon"] == 0.05
    assert begin.payload["evaluator_version"] == FLAT.version


def test_episode_begin_carries_task_and_epoch():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, production_tasks=["alpha", "beta"],
                       convergence=[MaxEpisodes(2)])
    )
    begins = sink.of_kind(EPISODE_BEGIN)
    assert [b.iteration for b in begins] == [0, 1]
    assert [b.payload["task"] for b in begins] == ["alpha", "beta"]
    assert all(b.payload["epoch"] == 0 for b in begins)


def test_episode_end_carries_primary_signal_and_reward():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, evaluator=FLAT,
                       ground_truth=gt_from_success(hi=0.9, lo=0.2))
    )
    end = sink.of_kind(EPISODE_END)[0]
    assert end.payload["gt_aggregate"] == 0.2  # Primary aggregate for the failed episode
    assert end.payload["reward"] == 0.5        # Reward from the FLAT evaluator (reflect only)
    assert end.payload["succeeded"] is False
    assert end.payload["ground_truth_backed"] is True


# ==============================================================================
# Lesson admission / rejection
# ==============================================================================


def test_lesson_adopted_emits_decision_event():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, reflect=true_reflect,
                       admit_lesson=accept_all, convergence=[MaxEpisodes(1)])
    )
    decisions = sink.of_kind(LESSON_DECISION)
    assert len(decisions) == 1
    assert decisions[0].payload["admitted"] is True
    assert decisions[0].payload["text"] == "use-the-fix"
    # The episode_end side is also consistent.
    assert sink.of_kind(EPISODE_END)[0].payload["lesson_admitted"] is True


def test_lesson_rejected_emits_decision_event_with_reason():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, reflect=true_reflect,
                       admit_lesson=reject_all, convergence=[MaxEpisodes(1)])
    )
    decisions = sink.of_kind(LESSON_DECISION)
    assert len(decisions) == 1
    assert decisions[0].payload["admitted"] is False
    assert sink.of_kind(EPISODE_END)[0].payload["lesson_admitted"] is False


def test_no_lesson_means_no_decision_event():
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, reflect=no_reflect)
    )
    assert sink.of_kind(LESSON_DECISION) == []


# ==============================================================================
# Epoch boundary + evaluator promotion / rejection
# ==============================================================================


def _promote_setup(candidate, *, sink, convergence, golds=(0.2, 0.8)):
    """Minimal setup for one epoch boundary that proposes a candidate from incumbent=FLAT."""
    return run_observed_reflexion(
        episode=lambda ctx: make_result(False),
        ground_truth=gt_from_success(),
        reflect=no_reflect,
        evaluator=FLAT,
        convergence=convergence,
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(*golds),
        epoch_len=2,
        epsilon=0.02,
        propose_evaluator=lambda outer, inc: candidate,
        sinks=[sink],
        otel=False,
    )


def test_epoch_boundary_promotion_event():
    """The honest candidate beats FLAT on held-out agreement by more than epsilon and is promoted."""
    sink = ListSink()
    _promote_setup(HONEST, sink=sink, convergence=[MaxEpisodes(4)])
    boundaries = sink.of_kind(EPOCH_BOUNDARY)
    assert len(boundaries) == 1
    b = boundaries[0]
    assert b.payload["epoch"] == 1
    assert b.payload["evaluator_decision"] == "promoted"
    assert b.payload["promoted"] is True
    assert b.payload["previous_version"] == FLAT.version
    assert b.payload["evaluator_version"] == HONEST.version
    assert "candidate_agreement" in b.payload


def test_epoch_boundary_rejection_event():
    """The lenient (all 1.0) candidate diverges from varied gold labels and is rejected."""
    sink = ListSink()
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")
    _promote_setup(lenient, sink=sink, convergence=[MaxEpisodes(4)])
    b = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert b.payload["evaluator_decision"] == "rejected"
    assert b.payload["promoted"] is False
    assert b.payload["proposed"] is True
    # Rejected, so the version stays unchanged.
    assert b.payload["evaluator_version"] == FLAT.version


def test_epoch_boundary_unchanged_when_no_candidate():
    """Without propose_evaluator, the boundary is recorded as unchanged."""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, epoch_len=2,
                       convergence=[MaxEpisodes(4)])
    )
    b = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert b.payload["evaluator_decision"] == "unchanged"
    assert b.payload["proposed"] is False


def test_no_boundary_event_when_converged_before_epoch_end():
    """Terminal runs do not promote at the boundary, so no boundary event is emitted."""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, epoch_len=2,
                       convergence=[MaxEpisodes(2)])
    )
    assert sink.of_kind(EPOCH_BOUNDARY) == []


# ==============================================================================
# Convergence reason + metric consistency
# ==============================================================================


def test_end_event_carries_convergence_reason():
    sink = ListSink()
    result = run_observed_reflexion(
        episode=lambda ctx: make_result(True),
        ground_truth=gt_from_success(hi=0.9),
        reflect=no_reflect,
        evaluator=FLAT,
        convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
        sinks=[sink],
        otel=False,
    )
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "converged"
    assert end.payload["succeeded"] is True
    assert end.payload["stop"] == "rubric_threshold"
    assert "rubric threshold" in end.payload["reason"]
    assert result.succeeded is True


def test_metric_consistency_event_counts_match_state():
    """episode_end count = result.episodes, and end aggregates match result.state (authority)."""
    sink = ListSink()
    result = run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, reflect=true_reflect,
                       admit_lesson=accept_all, convergence=[MaxEpisodes(3)])
    )
    end = sink.of_kind(REFLEXION_END)[0]
    assert kinds(sink).count(EPISODE_END) == result.episodes == end.payload["episodes"]
    assert end.payload["reflections"] == result.state.reflections
    assert end.payload["best_gt_aggregate"] == result.best_score
    assert end.payload["evaluator_updates"] == result.state.evaluator_updates
    # lesson_decision count = admitted lesson count (every failed episode because admit_all).
    assert kinds(sink).count(LESSON_DECISION) == 3


def test_best_gt_aggregate_omitted_when_no_grounded_episode():
    """Runs with only ground_truth_backed=False have best=-inf, so it is omitted from payloads."""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False,
                       ground_truth=gt_from_success(backed=False))
    )
    end = sink.of_kind(REFLEXION_END)[0]
    assert "best_gt_aggregate" not in end.payload
    # Individual episode_end events also omit -inf (same convention as run-end; no invalid JSON values).
    for ev in sink.of_kind(EPISODE_END):
        assert "best_gt_aggregate" not in ev.payload


def test_episode_end_never_emits_negative_infinity_in_jsonl(tmp_path):
    """Leading non-ground-truth-backed episodes do not write -Infinity (invalid JSON)."""
    from loop_agent import JsonlEventSink

    path = tmp_path / "reflexion.jsonl"
    run_observed_reflexion(
        **_base_kwargs(sinks=[JsonlEventSink(str(path))], otel=False,
                       ground_truth=gt_from_success(backed=False))
    )
    raw = path.read_text(encoding="utf-8")
    # json.dumps writes -inf as the non-standard literal `-Infinity`; ensure it is absent.
    assert "-Infinity" not in raw
    assert "Infinity" not in raw


def test_episode_event_omits_best_on_span_when_ungrounded(otel_tracer):
    """The span's episode events also avoid attributes for -inf (do not put -inf in OTel)."""
    tracer, exporter = otel_tracer
    run_observed_reflexion(
        **_base_kwargs(tracer=tracer, ground_truth=gt_from_success(backed=False))
    )
    span = exporter.get_finished_spans()[0]
    for ev in [e for e in span.events if e.name == "episode"]:
        assert "best_gt_aggregate" not in dict(ev.attributes)


def test_error_after_promoting_boundary_reports_evaluator_updates():
    """Promotion at the boundary followed by an episode error still reports evaluator_updates."""
    sink = ListSink()
    calls = {"n": 0}

    def episode(ctx):
        calls["n"] += 1
        if calls["n"] >= 3:  # Fail on the 3rd call, after the boundary (after episode 2 completes)
            raise RuntimeError("kaboom")
        return make_result(False)

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_reflexion(
            episode=episode,
            ground_truth=gt_from_success(),
            reflect=no_reflect,
            evaluator=FLAT,
            convergence=[MaxEpisodes(9)],
            declared_keys=DECLARED,
            production_tasks=["task-a"],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            propose_evaluator=lambda outer, inc: HONEST,  # Promote at the boundary
            sinks=[sink],
            otel=False,
        )
    boundary = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert boundary.payload["evaluator_decision"] == "promoted"
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "error"
    # One promotion was observed, so the error-end aggregate keeps 1 (no loss after the boundary).
    assert end.payload["evaluator_updates"] == 1
    assert end.payload["evaluator_version"] == HONEST.version


# ==============================================================================
# Decision logic invariant (do not break the safety core)
# ==============================================================================


def test_observation_does_not_change_decisions():
    """Observed and bare run_reflexion results match (observation is a side channel)."""
    def episode(ctx):
        has = "use-the-fix" in ctx.memory_block
        return make_result(has)

    common = dict(
        episode=episode,
        ground_truth=gt_from_success(),
        reflect=true_reflect,
        evaluator=FLAT,
        convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
        admit_lesson=accept_all,
        epoch_len=2,
    )
    bare = run_reflexion(**common)
    observed = run_observed_reflexion(**common, sinks=[ListSink()], otel=False)
    assert bare.status == observed.status
    assert bare.succeeded == observed.succeeded
    assert bare.episodes == observed.episodes
    assert bare.best_score == observed.best_score
    assert bare.state.reflections == observed.state.reflections


# ==============================================================================
# Best-effort degrade (observation failures do not kill the driver)
# ==============================================================================


class FlakySink:
    def emit(self, event):
        raise RuntimeError("sink boom")


def test_flaky_sink_does_not_kill_driver():
    good = ListSink()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_observed_reflexion(
            **_base_kwargs(sinks=[FlakySink(), good], otel=False)
        )
    # The driver completes, and the healthy sink receives every event.
    assert result.episodes == 2
    assert kinds(good)[0] == REFLEXION_BEGIN
    assert kinds(good)[-1] == REFLEXION_END
    assert any("boom" in str(w.message) for w in caught)


def test_observer_hook_swallows_internal_errors(monkeypatch):
    """Internal observation hook exceptions (such as span errors) are swallowed and do not crash the driver."""
    obs = ReflexionObserver(otel=False)

    # Replace span with a dummy that raises and confirm the hook body swallows it.
    class BoomSpan:
        def add_episode_begin(self, **_k):
            raise RuntimeError("span boom")

    obs._span = BoomSpan()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from loop_agent.reflexion import ReflexionContext
        obs.on_episode_begin(
            ReflexionContext(episode=0, epoch=0, task="t", evaluator=FLAT, memory_block="")
        )
    assert any("on_episode_begin" in str(w.message) for w in caught)


def test_error_in_episode_records_error_end():
    """If an episode exits with an exception, record status=error reflexion_end and re-raise."""
    sink = ListSink()

    def boom(ctx):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_reflexion(**_base_kwargs(episode=boom, sinks=[sink], otel=False))
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "error"
    assert "kaboom" in end.payload["reason"]


# ==============================================================================
# Paused propagation
# ==============================================================================


def test_resume_error_end_preserves_prior_state_metrics():
    """Exception before the first episode during outer resume preserves restored state aggregates."""
    from loop_agent import ReflexionState

    sink = ListSink()
    seed = ReflexionState(
        episode=3, epoch=1, evaluator_version=FLAT.version,
        gt_aggregate_history=[0.2, 0.9, 0.9], best_gt_aggregate=0.9,
        reflections=2, evaluator_updates=1, declared_keys=DECLARED,
    )

    def boom(ctx):
        raise RuntimeError("kaboom")  # Fail immediately in the first episode (on_episode not called)

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_reflexion(
            episode=boom,
            ground_truth=gt_from_success(),
            reflect=no_reflect,
            evaluator=FLAT,
            convergence=[MaxEpisodes(9)],
            declared_keys=DECLARED,
            production_tasks=["task-a"],
            held_out=held_out_matching(0.2, 0.8),
            initial_state=seed,
            sinks=[sink],
            otel=False,
        )
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "error"
    # Do not zero out confirmed aggregates from the restored state.
    assert end.payload["episodes"] == 3
    assert end.payload["epochs"] == 1
    assert end.payload["reflections"] == 2
    assert end.payload["evaluator_updates"] == 1
    assert end.payload["best_gt_aggregate"] == 0.9
    assert end.payload["evaluator_version"] == FLAT.version


def test_paused_inner_episode_records_paused_end():
    sink = ListSink()
    result = run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False,
                       episode=lambda ctx: make_paused_result())
    )
    assert result.paused is True
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "paused"
    # The paused episode is not confirmed, so episode_end is not emitted (begin only).
    assert sink.of_kind(EPISODE_END) == []
    assert len(sink.of_kind(EPISODE_BEGIN)) == 1


# ==============================================================================
# EpochRecord semantics
# ==============================================================================


def test_epoch_record_decision_semantics():
    promoted = AdmissionResult(chosen=HONEST, incumbent_agreement=-1.0,
                               candidate_agreement=-0.1, promoted=True)
    rejected = AdmissionResult(chosen=FLAT, incumbent_agreement=-0.1,
                               candidate_agreement=-1.0, promoted=False)
    r_prom = EpochRecord(epoch=1, boundary_episode=2, previous_version="a",
                         evaluator_version="b", admission=promoted)
    r_rej = EpochRecord(epoch=1, boundary_episode=2, previous_version="a",
                        evaluator_version="a", admission=rejected)
    r_none = EpochRecord(epoch=1, boundary_episode=2, previous_version="a",
                         evaluator_version="a", admission=None)
    assert (r_prom.decision, r_prom.proposed, r_prom.promoted) == ("promoted", True, True)
    assert (r_rej.decision, r_rej.proposed, r_rej.promoted) == ("rejected", True, False)
    assert (r_none.decision, r_none.proposed, r_none.promoted) == ("unchanged", False, False)


# ==============================================================================
# run_reflexion(on_epoch=...) core hook
# ==============================================================================


def test_on_epoch_hook_fires_at_boundary():
    seen: list[EpochRecord] = []
    run_reflexion(
        **_base_kwargs(epoch_len=2, convergence=[MaxEpisodes(4)],
                       on_epoch=seen.append)
    )
    # 4 episodes / epoch_len 2 -> 2 boundaries (after episodes 2 and 4). However, after
    # episode 4, MaxEpisodes(4) has already fired, so promotion is not attempted by design
    # -> only the boundary after episode 2 remains.
    assert [r.epoch for r in seen] == [1]
    assert seen[0].boundary_episode == 2
    assert seen[0].decision == "unchanged"


def test_on_epoch_hook_composes_with_user_on_episode():
    """run_observed_reflexion composes the user's on_episode with observation and calls both."""
    user_seen: list[int] = []
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False,
                       on_episode=lambda rec, st: user_seen.append(rec.episode))
    )
    assert user_seen == [0, 1]
    assert kinds(sink).count(EPISODE_END) == 2


# ==============================================================================
# OTel degrade path (otel=False / unavailable)
# ==============================================================================


def test_reflexion_span_noop_when_disabled():
    span = ReflexionSpan(enabled=False)
    span.start(declared_keys=("a",), evaluator_version="v", epoch_len=2, epsilon=0.02)
    assert span.recording is False
    # Even as a no-op, every method can be called without raising.
    span.add_episode_begin(episode=0, epoch=0, evaluator_version="v")
    span.add_episode(episode=0, epoch=0, evaluator_version="v", gt_aggregate=0.1,
                     reward=0.5, succeeded=False, ground_truth_backed=True,
                     best_gt_aggregate=0.1, lesson_admitted=False)
    span.add_lesson(episode=0, admitted=False)
    span.add_epoch(epoch=1, boundary_episode=2, decision="unchanged",
                   previous_version="v", evaluator_version="v")
    span.end(status="stopped", reason="x", episodes=1, epochs=0,
             best_gt_aggregate=0.1, reflections=0, evaluator_updates=0,
             evaluator_version="v")


def test_reflexion_span_degrades_when_otel_unavailable(monkeypatch):
    import loop_agent.otel as otel_mod

    monkeypatch.setattr(otel_mod, "_OTEL_AVAILABLE", False)
    span = otel_mod.ReflexionSpan(tracer="would-be-a-tracer", enabled=True)
    span.start()
    assert span.recording is False
    span.end(status="converged", reason="ok", episodes=1, epochs=0,
             best_gt_aggregate=0.9, reflections=0, evaluator_updates=0,
             evaluator_version="v")
    assert otel_mod.otel_available() is False


def test_observed_reflexion_runs_with_otel_disabled():
    sink = ListSink()
    result = run_observed_reflexion(**_base_kwargs(sinks=[sink], otel=False))
    assert result.episodes == 2
    assert kinds(sink)[0] == REFLEXION_BEGIN
    assert kinds(sink)[-1] == REFLEXION_END


# ==============================================================================
# OTel active path (inspect the span with an in-memory exporter)
# ==============================================================================

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from loop_agent.otel import (  # noqa: E402
    ATTR_REFLEXION_BEST,
    ATTR_REFLEXION_EPISODES,
    ATTR_REFLEXION_EPOCHS,
    ATTR_REFLEXION_REFLECTIONS,
    ATTR_REFLEXION_STATUS,
    ATTR_REFLEXION_STOP,
    GEN_AI_OPERATION_NAME,
    GEN_AI_SYSTEM,
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


def test_run_creates_single_reflexion_span_with_genai_attrs(otel_tracer):
    tracer, exporter = otel_tracer
    run_observed_reflexion(
        episode=lambda ctx: make_result(True),
        ground_truth=gt_from_success(hi=0.9),
        reflect=no_reflect,
        evaluator=FLAT,
        convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
        tracer=tracer,
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "loop_agent.reflexion"
    attrs = dict(span.attributes)
    assert attrs[GEN_AI_OPERATION_NAME] == "reflexion"
    assert attrs[GEN_AI_SYSTEM] == "loop_agent"
    assert attrs[ATTR_REFLEXION_STATUS] == "converged"
    assert attrs[ATTR_REFLEXION_STOP] == "rubric_threshold"
    assert attrs[ATTR_REFLEXION_EPISODES] == 1
    assert attrs[ATTR_REFLEXION_BEST] == 0.9


def test_transitions_are_span_events(otel_tracer):
    tracer, exporter = otel_tracer
    run_observed_reflexion(
        episode=lambda ctx: make_result(False),
        ground_truth=gt_from_success(),
        reflect=true_reflect,
        admit_lesson=accept_all,
        evaluator=FLAT,
        convergence=[MaxEpisodes(4)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
        epoch_len=2,
        propose_evaluator=lambda outer, inc: HONEST,
        tracer=tracer,
    )
    span = exporter.get_finished_spans()[0]
    names = Counter(e.name for e in span.events)
    assert names["episode_begin"] == 4
    assert names["episode"] == 4
    assert names["lesson_decision"] == 4   # Grounded lesson admitted for every failed episode
    assert names["epoch_boundary"] >= 1
    # The epoch_boundary event carries the promotion decision.
    boundary = [e for e in span.events if e.name == "epoch_boundary"][0]
    assert boundary.attributes["evaluator_decision"] == "promoted"


def test_exception_marks_reflexion_span_error(otel_tracer):
    from opentelemetry.trace import StatusCode

    tracer, exporter = otel_tracer

    def boom(ctx):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_reflexion(**_base_kwargs(episode=boom, tracer=tracer))
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert dict(span.attributes)[ATTR_REFLEXION_STATUS] == "error"
    assert any(e.name == "exception" for e in span.events)


def test_misbehaving_tracer_does_not_crash_reflexion(monkeypatch):
    import loop_agent.otel as otel_mod

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
        result = run_observed_reflexion(
            **_base_kwargs(sinks=[sink], tracer=FlakyTracer())
        )
    assert result.episodes == 2
    assert kinds(sink)[-1] == REFLEXION_END
    assert span.ended is True
    assert any("add_event" in str(w.message) for w in caught)


def test_metric_consistency_span_events_match_attributes(otel_tracer):
    """Span episode event count matches the final episodes attribute (metric consistency)."""
    tracer, exporter = otel_tracer
    result = run_observed_reflexion(
        **_base_kwargs(tracer=tracer, reflect=true_reflect, admit_lesson=accept_all,
                       convergence=[MaxEpisodes(3)])
    )
    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes)
    episode_events = [e for e in span.events if e.name == "episode"]
    assert len(episode_events) == attrs[ATTR_REFLEXION_EPISODES] == result.episodes
    assert attrs[ATTR_REFLEXION_REFLECTIONS] == result.state.reflections
    assert attrs[ATTR_REFLEXION_EPOCHS] == result.epochs
