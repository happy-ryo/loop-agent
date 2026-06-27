"""外側 Reflexion 観測 (ReflexionObserver + run_observed_reflexion) のテスト (Issue #30)。

押さえる面:

- **遷移の網羅**: episode 開始/終了・epoch 開始/境界・lesson 採用/拒否・採点係昇格/拒否・
  収束理由が、構造化イベント (sink) と OTel span event に残ること。
- **metric 一貫性**: emit したイベント個数と最終集計 (reflexion_end / span 属性) が
  権威ある ``result.state`` と整合すること。
- **optional degrade**: OTel 無効/未導入で no-op に倒れ、event sink 側は機能すること。
- **best-effort**: sink / tracer / 観測フックが例外を投げても外側 driver が落ちないこと。
- **判断ロジック不変**: 観測の有無で ``run_reflexion`` の結果が一致すること (安全核を壊さない)。
- **core hook**: ``run_reflexion(on_epoch=...)`` が epoch 境界で正しく発火すること。
"""

from __future__ import annotations

import warnings
from collections import Counter

import pytest

from claude_loop import (
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
from claude_loop.conditions import StopTrigger
from claude_loop.evaluator import AdmissionResult
from claude_loop.loop import LoopResult
from claude_loop.memory import LessonVerdict, step_signature
from claude_loop.reflexion_observe import (
    EPISODE_BEGIN,
    EPISODE_END,
    EPOCH_BOUNDARY,
    LESSON_DECISION,
    REFLEXION_BEGIN,
    REFLEXION_END,
)
from claude_loop.state import LoopState, StepRecord


# -- 共通スタブ (test_reflexion.py と同じ作法) -------------------------------

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
# 遷移の網羅 (event sink)
# ==============================================================================


def test_lifecycle_events_emitted_in_order():
    """begin → (episode_begin/end)* → end の順で最小ライフサイクルが残る。"""
    sink = ListSink()
    run_observed_reflexion(**_base_kwargs(sinks=[sink], otel=False))
    ks = kinds(sink)
    assert ks[0] == REFLEXION_BEGIN
    assert ks[-1] == REFLEXION_END
    # 2 episode (MaxEpisodes(2)) ぶんの begin/end が残る。
    assert ks.count(EPISODE_BEGIN) == 2
    assert ks.count(EPISODE_END) == 2
    # 各 episode は begin が end より先。
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
    assert end.payload["gt_aggregate"] == 0.2  # 失敗 episode の一次集約
    assert end.payload["reward"] == 0.5        # FLAT 評価器の reward (reflect 専用)
    assert end.payload["succeeded"] is False
    assert end.payload["ground_truth_backed"] is True


# ==============================================================================
# lesson 採用 / 拒否
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
    # episode_end 側も整合する。
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
# epoch 境界 + 採点係昇格 / 拒否
# ==============================================================================


def _promote_setup(candidate, *, sink, convergence, golds=(0.2, 0.8)):
    """incumbent=FLAT から候補を提案する epoch 境界 1 回ぶんの最小構成。"""
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
    """honest 候補は held-out 一致度で FLAT を ε 超で上回り昇格する。"""
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
    """lenient (全部 1.0) 候補は varied gold に乖離し却下される。"""
    sink = ListSink()
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")
    _promote_setup(lenient, sink=sink, convergence=[MaxEpisodes(4)])
    b = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert b.payload["evaluator_decision"] == "rejected"
    assert b.payload["promoted"] is False
    assert b.payload["proposed"] is True
    # 却下なので version は据え置き。
    assert b.payload["evaluator_version"] == FLAT.version


def test_epoch_boundary_unchanged_when_no_candidate():
    """propose_evaluator が無ければ境界は unchanged で記録される。"""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, epoch_len=2,
                       convergence=[MaxEpisodes(4)])
    )
    b = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert b.payload["evaluator_decision"] == "unchanged"
    assert b.payload["proposed"] is False


def test_no_boundary_event_when_converged_before_epoch_end():
    """終端 run では境界で昇格しない設計に合わせ、境界イベントも出ない。"""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False, epoch_len=2,
                       convergence=[MaxEpisodes(2)])
    )
    assert sink.of_kind(EPOCH_BOUNDARY) == []


# ==============================================================================
# 収束理由 + metric 一貫性
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
    """episode_end 個数 = result.episodes、end 集計 = result.state (権威) と整合。"""
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
    # lesson_decision 個数 = admit された lesson の数 (admit_all なので全 fail episode)。
    assert kinds(sink).count(LESSON_DECISION) == 3


def test_best_gt_aggregate_omitted_when_no_grounded_episode():
    """ground_truth_backed=False のみの run では best が -inf になり payload に載せない。"""
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False,
                       ground_truth=gt_from_success(backed=False))
    )
    end = sink.of_kind(REFLEXION_END)[0]
    assert "best_gt_aggregate" not in end.payload
    # 個々の episode_end にも -inf を載せない (run-end と同じ規約; 仕様外 JSON 値を出さない)。
    for ev in sink.of_kind(EPISODE_END):
        assert "best_gt_aggregate" not in ev.payload


def test_episode_end_never_emits_negative_infinity_in_jsonl(tmp_path):
    """非 ground-truth-backed の先行 episode が -Infinity (仕様外 JSON) を書かないこと。"""
    from claude_loop import JsonlEventSink

    path = tmp_path / "reflexion.jsonl"
    run_observed_reflexion(
        **_base_kwargs(sinks=[JsonlEventSink(str(path))], otel=False,
                       ground_truth=gt_from_success(backed=False))
    )
    raw = path.read_text(encoding="utf-8")
    # json.dumps は -inf を仕様外の literal `-Infinity` として書く。これが残っていないこと。
    assert "-Infinity" not in raw
    assert "Infinity" not in raw


def test_episode_event_omits_best_on_span_when_ungrounded(otel_tracer):
    """span の episode event も -inf を属性化しない (OTel に -inf を載せない)。"""
    tracer, exporter = otel_tracer
    run_observed_reflexion(
        **_base_kwargs(tracer=tracer, ground_truth=gt_from_success(backed=False))
    )
    span = exporter.get_finished_spans()[0]
    for ev in [e for e in span.events if e.name == "episode"]:
        assert "best_gt_aggregate" not in dict(ev.attributes)


def test_error_after_promoting_boundary_reports_evaluator_updates():
    """境界で昇格 → 次 episode が例外、の error 終了で evaluator_updates が不足しない。"""
    sink = ListSink()
    calls = {"n": 0}

    def episode(ctx):
        calls["n"] += 1
        if calls["n"] >= 3:  # 境界 (episode 2 完了後) を越えた 3 回目で落とす
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
            propose_evaluator=lambda outer, inc: HONEST,  # 境界で昇格
            sinks=[sink],
            otel=False,
        )
    boundary = sink.of_kind(EPOCH_BOUNDARY)[0]
    assert boundary.payload["evaluator_decision"] == "promoted"
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "error"
    # 昇格を 1 回観測したので、error 終了の集計でも 1 が残る (境界後の取りこぼし無し)。
    assert end.payload["evaluator_updates"] == 1
    assert end.payload["evaluator_version"] == HONEST.version


# ==============================================================================
# 判断ロジック不変 (安全核を壊さない)
# ==============================================================================


def test_observation_does_not_change_decisions():
    """観測ありと素の run_reflexion で結果が一致する (観測は側チャネル)。"""
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
# best-effort degrade (観測の失敗で driver を殺さない)
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
    # driver は完走し、健全な sink には全イベントが届く。
    assert result.episodes == 2
    assert kinds(good)[0] == REFLEXION_BEGIN
    assert kinds(good)[-1] == REFLEXION_END
    assert any("boom" in str(w.message) for w in caught)


def test_observer_hook_swallows_internal_errors(monkeypatch):
    """観測フック内部 (span など) の例外も握り、driver を落とさない。"""
    obs = ReflexionObserver(otel=False)

    # span を例外を投げるダミーに差し替えて、フック本体が握ることを確認する。
    class BoomSpan:
        def add_episode_begin(self, **_k):
            raise RuntimeError("span boom")

    obs._span = BoomSpan()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from claude_loop.reflexion import ReflexionContext
        obs.on_episode_begin(
            ReflexionContext(episode=0, epoch=0, task="t", evaluator=FLAT, memory_block="")
        )
    assert any("on_episode_begin" in str(w.message) for w in caught)


def test_error_in_episode_records_error_end():
    """episode が例外で抜けたら status=error の reflexion_end を残して再送出する。"""
    sink = ListSink()

    def boom(ctx):
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        run_observed_reflexion(**_base_kwargs(episode=boom, sinks=[sink], otel=False))
    end = sink.of_kind(REFLEXION_END)[0]
    assert end.payload["status"] == "error"
    assert "kaboom" in end.payload["reason"]


# ==============================================================================
# paused 伝播
# ==============================================================================


def test_resume_error_end_preserves_prior_state_metrics():
    """外側 resume 中に最初の episode 前で例外 → error end が復元 state の集計を保つ。"""
    from claude_loop import ReflexionState

    sink = ListSink()
    seed = ReflexionState(
        episode=3, epoch=1, evaluator_version=FLAT.version,
        gt_aggregate_history=[0.2, 0.9, 0.9], best_gt_aggregate=0.9,
        reflections=2, evaluator_updates=1, declared_keys=DECLARED,
    )

    def boom(ctx):
        raise RuntimeError("kaboom")  # 最初の episode で即落 (on_episode 未呼び出し)

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
    # 復元 state の確定済み集計を 0 に潰さない。
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
    # pause した episode は確定しないので episode_end は出ない (begin のみ)。
    assert sink.of_kind(EPISODE_END) == []
    assert len(sink.of_kind(EPISODE_BEGIN)) == 1


# ==============================================================================
# EpochRecord の意味論
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
    # 4 episode / epoch_len 2 → 境界 2 回 (episode 2 と 4 の後)。だが episode 4 後は
    # MaxEpisodes(4) が既に発火しているので昇格はしない設計 → 境界は episode 2 後の 1 回。
    assert [r.epoch for r in seen] == [1]
    assert seen[0].boundary_episode == 2
    assert seen[0].decision == "unchanged"


def test_on_epoch_hook_composes_with_user_on_episode():
    """run_observed_reflexion は利用者の on_episode を観測と合成して両方呼ぶ。"""
    user_seen: list[int] = []
    sink = ListSink()
    run_observed_reflexion(
        **_base_kwargs(sinks=[sink], otel=False,
                       on_episode=lambda rec, st: user_seen.append(rec.episode))
    )
    assert user_seen == [0, 1]
    assert kinds(sink).count(EPISODE_END) == 2


# ==============================================================================
# OTel degrade path (otel=False / 未導入)
# ==============================================================================


def test_reflexion_span_noop_when_disabled():
    span = ReflexionSpan(enabled=False)
    span.start(declared_keys=("a",), evaluator_version="v", epoch_len=2, epsilon=0.02)
    assert span.recording is False
    # no-op でも全メソッドが例外なく呼べる。
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
    import claude_loop.otel as otel_mod

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
# OTel active path (in-memory exporter で span を実検査)
# ==============================================================================

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from claude_loop.otel import (  # noqa: E402
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
    assert span.name == "claude_loop.reflexion"
    attrs = dict(span.attributes)
    assert attrs[GEN_AI_OPERATION_NAME] == "reflexion"
    assert attrs[GEN_AI_SYSTEM] == "claude_loop"
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
    assert names["lesson_decision"] == 4   # 全 fail episode で grounded lesson 採用
    assert names["epoch_boundary"] >= 1
    # epoch_boundary event に昇格判定が載る。
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
    import claude_loop.otel as otel_mod

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
    """span の episode event 個数 = 終了属性 episodes と一致する (metric 一貫性)。"""
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
