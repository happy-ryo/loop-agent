"""外側 Reflexion 駆動 + RQGM 安全核の統合/不変条件テスト (Issue #22 の核心)。

ここが「安全不変条件をテストで実証」の本体。各不変条件を **正例** と、ガードを外すと
攻撃が通る **反証例 (falsification)** の対で固める (ガードが load-bearing であることの証明)。
"""

from __future__ import annotations

import pytest

from claude_loop.conditions import StopTrigger
from claude_loop.convergence import MaxEpisodes, ReflectionBudget, RubricThreshold
from claude_loop.evaluator import Evaluator, HeldOut, Probe, Score, GroundTruthSignal
from claude_loop.loop import LoopResult
from claude_loop.memory import EpisodicMemory, Lesson, LessonVerdict, step_signature
from claude_loop.reflexion import ReflexionContext, run_reflexion
from claude_loop.state import LoopState, StepRecord


# -- 共通スタブ ----------------------------------------------------------------

DECLARED = ("primary",)


def make_result(succeeded: bool, observation: object = "obs") -> LoopResult:
    """内側 ``run_loop`` の結果スタンド (succeeded で goal_met / stopped を切替)。"""
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


def gt_from_success(hi: float = 0.9, lo: float = 0.2, backed: bool = True):
    """outcome.succeeded から一次信号を作る (内側 verify 由来; 評価器ではない)。"""

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
    """評価器が EpisodeOutcome / probe dict のどちらでも採点できるよう読む。"""
    if hasattr(o, "succeeded"):
        return 1.0 if o.succeeded else 0.0
    return o["truth"]


HONEST = Evaluator(score=lambda o: Score(ground_truth=_truth(o)), name="honest")
FLAT = Evaluator(score=lambda o: Score(ground_truth=0.5), name="flat")
LENIENT = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")


def held_out_matching(*golds: float) -> HeldOut:
    """gold==truth な probe 群 (honest が完全一致、flat/lenient は乖離)。"""
    return HeldOut(
        tuple(Probe(f"probe-{i}", {"truth": g}, gold_label=g) for i, g in enumerate(golds))
    )


def no_reflect(history, signal, reward):
    return None


def true_reflect(history, signal, reward):
    """失敗軌跡から grounded な言語的指針を抽出 (Reflexion 本来の挙動)。"""
    if signal.succeeded:
        return None
    return Lesson(text="use-the-fix", episode=0,
                  provenance=step_signature(history[0]), support=1.0)


def accept_all(lesson, outcome):
    return LessonVerdict(admit=True)


def reject_all(lesson, outcome):
    return LessonVerdict(admit=False, reason="control")


# ==============================================================================
# 構成時バリデーション
# ==============================================================================


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


@pytest.mark.parametrize(
    "override",
    [
        {"epoch_len": 1},          # 動く評価器への退化
        {"epsilon": 0.0},          # churn 防止余白の喪失
        {"epsilon": -0.1},
        {"declared_keys": ()},     # 多様評価の喪失
        {"production_tasks": []},
    ],
)
def test_constructor_validation_rejects_unsafe_config(override):
    with pytest.raises(ValueError):
        run_reflexion(**_base_kwargs(**override))


def test_dual_component_overlap_rejected():
    """production task と held-out probe の名前空間が交差したら拒否 (dual-component 分離)。"""
    with pytest.raises(ValueError):
        run_reflexion(
            **_base_kwargs(
                production_tasks=["probe-0"],  # held_out_matching の case_id と衝突
                held_out=held_out_matching(0.2, 0.8),
            )
        )


def test_epoch_len_one_is_moving_evaluator_rejected():
    """INV1 反証: epoch_len==1 (毎 episode 更新 = 動く評価器) は構造的に禁止。"""
    with pytest.raises(ValueError):
        run_reflexion(**_base_kwargs(epoch_len=1))


# ==============================================================================
# INV1: epoch 内で評価器が固定される (reward hacking 抑止)
# ==============================================================================


def test_evaluator_frozen_within_epoch_updates_only_at_boundary():
    """epoch 内で evaluator_version / reward は不変、境界でのみ昇格する。"""
    records = []
    run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),  # 同一軌跡を毎回
            ground_truth=gt_from_success(),
            evaluator=FLAT,                          # reward=0.5 一定
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            epoch_len=3,
            propose_evaluator=lambda outer, inc: HONEST,  # 毎境界 honest を提案
            on_episode=lambda rec, st: records.append(rec),
        )
    )
    # epoch 0 (ep0-2): 初期 incumbent FLAT で固定。reward は 0.5 一定。
    assert {r.evaluator_version for r in records[:3]} == {FLAT.version}
    assert [r.reward for r in records[:3]] == [0.5, 0.5, 0.5]
    # 境界で HONEST が held-out で勝ち昇格 → epoch 1 (ep3-5) は HONEST で固定。
    assert {r.evaluator_version for r in records[3:6]} == {HONEST.version}
    assert [r.reward for r in records[3:6]] == [1.0, 1.0, 1.0]
    # reward が epoch 内で動いていない (境界でのみ変化) ことが freeze の実証。


def test_no_evaluator_update_on_terminal_boundary():
    """収束/打ち切りが epoch 境界と同時に成立したら、その境界では昇格を走らせない。"""

    def explode(outer, inc):
        raise AssertionError("propose_evaluator must not run on a terminal boundary")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(),
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],   # episode==2 = epoch 境界 (epoch_len=2) と同時に発火
            held_out=held_out_matching(0.0, 0.5, 1.0),
            epoch_len=2,
            propose_evaluator=explode,
        )
    )
    assert result.stop.name == "max_episodes"
    assert result.state.evaluator_version == HONEST.version  # 終端で書き換えない
    assert result.epochs == 0


def test_gaming_evaluator_rejected_at_boundary():
    """境界で提案された緩い評価器は held-out 一致度が低く昇格できない。"""
    records = []
    run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            evaluator=HONEST,
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.3, 0.6),
            epoch_len=2,
            propose_evaluator=lambda outer, inc: LENIENT,  # gaming 候補
            on_episode=lambda rec, st: records.append(rec),
        )
    )
    # 全 episode で HONEST のまま (LENIENT は一度も採用されない)。
    assert {r.evaluator_version for r in records} == {HONEST.version}


# ==============================================================================
# INV3: ground-truth 一次が制御を駆動。評価器スカラは reflect 専用
# ==============================================================================


def test_convergence_reads_ground_truth_not_evaluator_reward():
    """緩い評価器が reward=1.0 を返しても、ground-truth 不達なら収束しない。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),  # ground-truth 失敗
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            evaluator=LENIENT,                        # reward=1.0 (高いが無関係)
            convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(4)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is False
    assert result.stop.name == "max_episodes"
    assert result.best_score == pytest.approx(0.2)
    # reward は高いが制御に載っていない (reflect 専用)。
    assert all(rec.reward == 1.0 for rec in result.state.episodes)


def test_unbacked_episodes_do_not_count_toward_convergence():
    """ground_truth_backed=False の episode は収束判定に算入されない。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(hi=0.99, backed=False),  # 実信号なし
            evaluator=HONEST,
            convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is False  # 高 aggregate でも backed=False なので未収束
    assert result.state.gt_aggregate_history == []


def test_unbacked_episode_lesson_not_admitted():
    """実信号の無い episode 由来の lesson は memory に入れない (次 context を汚さない)。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False, observation="real-step"),
            ground_truth=gt_from_success(lo=0.2, backed=False),  # 実信号なし
            reflect=true_reflect,  # grounded な lesson を返す
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 0  # backed=False なので取り込まれない


# ==============================================================================
# INV4b: memory 取込前検証 (false lesson 注入 / 自己申告 support を弾く)
# ==============================================================================


def poison_reflect(history, signal, reward):
    """実 step に紐づかない provenance + 詐称 support の注入 lesson。"""
    return Lesson(text="POISON", episode=0, provenance="step-99-fabricated", support=99.0)


def test_poison_lesson_rejected_by_default_admission():
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=poison_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 0  # 注入 lesson は memory に入らない
    assert "POISON" not in result.state.memory.render()


def test_poison_admission_is_load_bearing():
    """反証: 取込前検証を accept_all に差し替えると注入 lesson が通ってしまう。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=poison_reflect,
            admit_lesson=accept_all,   # ガードを外す
            evaluator=HONEST,
            convergence=[MaxEpisodes(1)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 1  # ガードを外すと poison が入る = 検証が効いていた


def test_self_reported_support_is_overwritten():
    """reflect が support を詐称しても driver が grounding から再計算して上書きする。"""
    captured = {}

    def reflect_with_fake_support(history, signal, reward):
        # 本物の provenance だが support を 99.0 と詐称。
        return Lesson(text="real lesson", episode=0,
                      provenance=step_signature(history[0]), support=99.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=reflect_with_fake_support,
            evaluator=HONEST,
            convergence=[MaxEpisodes(1)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    (stored,) = result.state.memory.lessons()
    assert stored.support == 1.0  # 自己申告 99.0 ではなく再計算値


# ==============================================================================
# INV5: 反省の肥大化を反復上限で抑える
# ==============================================================================


def test_reflection_budget_stops_outer_loop():
    """ReflectionBudget で取込 lesson が上限に達したら外側ループを打ち切る。"""
    counter = {"n": 0}

    def unique_reflect(history, signal, reward):
        counter["n"] += 1
        return Lesson(text=f"lesson {counter['n']}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=unique_reflect,
            evaluator=HONEST,
            convergence=[ReflectionBudget(2), MaxEpisodes(100)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.stop.name == "reflection_budget"
    assert result.state.reflections == 2


def test_admitted_lesson_stamped_with_actual_episode():
    """reflect の placeholder episode=0 を driver が実 episode 番号で上書きする。"""

    def reflect_each(history, signal, reward):
        # hook は常に placeholder episode=0 を返す (正しい番号を知らない)。
        return Lesson(text=f"lesson at {history[0].detail}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    counter = {"n": 0}

    def episode(ctx):
        counter["n"] += 1
        return make_result(False, observation=f"ep{counter['n']}")

    result = run_reflexion(
        **_base_kwargs(
            episode=episode,
            reflect=reflect_each,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    # 3 episode 分の lesson がそれぞれ正しい episode 番号でスタンプされている。
    episodes = sorted(l.episode for l in result.state.memory.lessons())
    assert episodes == [0, 1, 2]


def test_memory_size_bounded_under_many_reflections():
    counter = {"n": 0}

    def unique_reflect(history, signal, reward):
        counter["n"] += 1
        return Lesson(text=f"lesson {counter['n']}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=unique_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(20)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            memory=EpisodicMemory(cap=3),
        )
    )
    assert len(result.state.memory) == 3  # cap で有界


# ==============================================================================
# INV: reflect 例外は非致命
# ==============================================================================


def test_reflect_exception_is_non_fatal():
    def boom_reflect(history, signal, reward):
        raise RuntimeError("reflect blew up")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=boom_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.episodes == 2  # 例外で run が倒れない
    assert all("reflect failed" in rec.detail for rec in result.state.episodes)


# ==============================================================================
# 成功判定はトリガ順に依存しない
# ==============================================================================


@pytest.mark.parametrize(
    "conditions",
    [
        [MaxEpisodes(2), RubricThreshold(0.8, sustain=2)],
        [RubricThreshold(0.8, sustain=2), MaxEpisodes(2)],
    ],
)
def test_success_is_order_insensitive(conditions):
    """成功条件とハード上限が同一 guard で発火しても、成否は順序に依らない。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(hi=0.9),
            evaluator=HONEST,
            convergence=conditions,
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is True
    assert result.best_score == pytest.approx(0.9)


# ==============================================================================
# 目玉: 学びが次 episode の ground-truth を改善する (Phase3 成功条件 a)
# ==============================================================================


def succeed_if_lesson(ctx: ReflexionContext):
    """memory_block に前試行の指針があれば成功する memory-sensitive episode。"""
    helped = "use-the-fix" in ctx.memory_block
    return make_result(helped, observation="fixed" if helped else "broken")


def test_real_lesson_improves_next_episode_ground_truth():
    """ep0 失敗 → grounded lesson 取込 → ep1 が memory 配線で成功 (eval で改善確認)。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=succeed_if_lesson,
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            reflect=true_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    history = result.state.gt_aggregate_history
    assert history[0] == pytest.approx(0.2)  # ep0: memory 空で失敗
    assert history[1] == pytest.approx(0.9)  # ep1: 配線された学びで成功


def test_memory_unwired_control_shows_no_improvement():
    """反証(帰属): 取込を reject_all で潰すと ep1 は改善しない (= 配線が原因と確定)。"""
    result = run_reflexion(
        **_base_kwargs(
            episode=succeed_if_lesson,
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            reflect=true_reflect,
            admit_lesson=reject_all,   # 学びを memory に入れない
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    history = result.state.gt_aggregate_history
    assert history[0] == pytest.approx(0.2)
    assert history[1] == pytest.approx(0.2)  # 改善しない (memory 未配線)


def test_paused_inner_episode_propagates_pause():
    """内側 episode が人間ゲートで pause したら外側も pause を伝播する (Issue #15 契約)。"""
    paused = LoopResult(
        status="paused", stop=None, state=LoopState(), pending={"gate_key": "gate-0"}
    )

    def boom_reflect(history, signal, reward):
        raise AssertionError("reflect must not run on a paused episode")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: paused,
            reflect=boom_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(5)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.status == "paused"
    assert result.paused is True
    assert result.pending == {"gate_key": "gate-0"}
    # 未完了 episode は記録せず・進めない (resume で同じ episode を再実行できる)。
    assert result.state.episode == 0
    assert result.state.episodes == []
    assert "awaiting human decision" in result.reason


def test_resume_rejects_mismatched_evaluator_version():
    """外側 resume: 復元 evaluator_version と渡された評価器が食い違えば loud に弾く。"""
    from claude_loop.reflexion import ReflexionState

    seed = ReflexionState(episode=4, epoch=2, evaluator_version="some-promoted-version")
    with pytest.raises(ValueError, match="resume"):
        run_reflexion(
            **_base_kwargs(
                episode=lambda ctx: make_result(False),
                evaluator=HONEST,  # version != "some-promoted-version"
                convergence=[MaxEpisodes(6)],
                held_out=held_out_matching(0.2, 0.8),
                epoch_len=2,
                initial_state=seed,
            )
        )


def test_resume_accepts_matching_evaluator_version():
    """復元 version と一致する評価器なら resume できる (継続する)。"""
    from claude_loop.reflexion import ReflexionState

    seed = ReflexionState(episode=2, epoch=1, evaluator_version=HONEST.version)
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            evaluator=HONEST,
            convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            initial_state=seed,
        )
    )
    assert result.state.episode == 4  # 復元 episode=2 から継続して 4 で停止


def test_production_path_never_runs_held_out_probes():
    """dual-component: episode() は production task のみ受け取り probe を実行しない。"""
    seen_tasks = []

    def spy_episode(ctx):
        seen_tasks.append(ctx.task)
        return make_result(False)

    run_reflexion(
        **_base_kwargs(
            episode=spy_episode,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            production_tasks=["prod-x", "prod-y"],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert set(seen_tasks) <= {"prod-x", "prod-y"}
    assert all(not str(t).startswith("probe-") for t in seen_tasks)
