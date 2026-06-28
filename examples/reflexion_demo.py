#!/usr/bin/env python3
"""外側 Reflexion ループのデモ: 失敗からの学びが次 episode を改善する (Issue #22)。

内側 ReAct ループ (``run_loop``) を 1 episode として包み、episode 境界で失敗軌跡から
言語的指針を抽出して episodic memory に取り込み、次 episode の context に配線する。
ground-truth 一次信号 (内側 verify) が収束を駆動し、epoch 内で固定された rubric 評価器は
reflect 用の reward だけを出す (二信号モデル)。評価器の更新は epoch 境界で held-out 固定
gold に勝ったときのみ (RQGM)。

CLI 出力は ASCII のみ (cp932 端末での --help / print クラッシュ回避)。

実行:

    python3 examples/reflexion_demo.py
"""

from __future__ import annotations

from loop_agent import (
    ActOutcome,
    Evaluator,
    GroundTruthSignal,
    HeldOut,
    Lesson,
    MaxEpisodes,
    MaxIterations,
    Probe,
    RubricThreshold,
    Score,
    VerifyOutcome,
    run_loop,
    run_reflexion,
)
from loop_agent.memory import step_signature

DECLARED_KEYS = ("correctness",)
LESSON_HINT = "increment the index by 1"


def make_episode():
    """production 経路: 1 episode = 内側 run_loop を 1 回。memory の学びで成否が変わる。"""

    def episode(ctx):
        has_lesson = LESSON_HINT in ctx.memory_block

        def act(_inner_ctx):
            obs = "off-by-one fixed" if has_lesson else "off-by-one bug remains"
            return ActOutcome(observation=obs, tokens=5)

        def verify(outcome):
            return VerifyOutcome(goal_met="fixed" in outcome.observation)

        # 内側 ReAct ループ。verify が goal_met を返せば自然終了する。
        return run_loop(act=act, verify=verify, conditions=[MaxIterations(2)])

    return episode


def ground_truth(outcome):
    """一次信号: 内側 verify の成否 (test/lint 相当) から作る。評価器ではない。"""
    val = 0.95 if outcome.succeeded else 0.2
    return GroundTruthSignal(
        succeeded=outcome.succeeded,
        score=Score(ground_truth=val, components={"correctness": val}),
    )


def reflect(history, signal, reward):
    """失敗軌跡から grounded な言語的指針を抽出する。"""
    if signal.succeeded or not history:
        return None
    return Lesson(
        text=LESSON_HINT,
        episode=0,
        provenance=step_signature(history[-1]),
        support=1.0,
    )


def build_evaluator() -> Evaluator:
    """rubric 評価器 (epoch 内で固定。reflect 用 reward を出すだけ)。"""

    def score(o):
        truth = (1.0 if o.succeeded else 0.0) if hasattr(o, "succeeded") else o["truth"]
        return Score(ground_truth=truth)

    return Evaluator(score=score, name="honest-rubric", rubric=("correctness",))


def build_held_out() -> HeldOut:
    """評価器昇格の測定基盤 (固定 gold ラベル。production task と素な名前空間)。"""
    return HeldOut(
        (
            Probe("hold-fail", {"truth": 0.0}, gold_label=0.0),
            Probe("hold-pass", {"truth": 1.0}, gold_label=1.0),
        )
    )


def run() -> object:
    """デモ本体。Reflexion 結果を返す (テストから検証できるよう副作用は print のみ)。"""
    return run_reflexion(
        episode=make_episode(),
        ground_truth=ground_truth,
        reflect=reflect,
        evaluator=build_evaluator(),
        convergence=[RubricThreshold(target=0.8, sustain=1), MaxEpisodes(5)],
        declared_keys=DECLARED_KEYS,
        production_tasks=["fix-off-by-one"],
        held_out=build_held_out(),
        epoch_len=2,
    )


def main() -> None:
    result = run()
    print("=== loop-agent Reflexion demo ===")
    for rec in result.state.episodes:
        outcome = "PASS" if rec.succeeded else "fail"
        print(
            f"episode {rec.episode} (epoch {rec.epoch}): "
            f"ground_truth_aggregate={rec.gt_aggregate:.2f} [{outcome}] "
            f"reward={rec.reward:.2f} admitted_lesson={rec.admitted}"
        )
    print(
        f"-> status={result.status} succeeded={result.succeeded} "
        f"best_score={result.best_score:.2f} episodes={result.episodes}"
    )
    print(
        "The lesson learned from episode 0's failure was wired into episode 1's "
        "context, lifting ground-truth from 0.20 to 0.95."
    )


if __name__ == "__main__":
    main()
