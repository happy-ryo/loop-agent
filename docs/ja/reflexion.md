# 外側 Reflexion ループ + RQGM epoch 安全核

内側 ReAct ループ (`run_loop`) の **外** に Reflexion 型の試行間ループを重ね、失敗からの言語的指針を episodic memory に取り込んで次 episode の context へ配線する self-improving の仕組み。安全核は「二信号モデル」と「epoch 昇格ゲート」。

本格（report.md §4.4 / §5 Phase 3 / §6 / Issue #22・#4 の RQGM コメント）では、内側 ReAct
ループの**外**に Reflexion 型の試行間ループを重ねる。`run_reflexion(...)` は内側 `run_loop`
を **1 episode** として呼び（driver は内側に手を入れない）、episode 境界で
`reflect(trajectory, signal, reward)` を回して**言語的指針（lesson）**を episodic memory に
取り込み、次 episode の context へ配線する。失敗トラジェクトリからの学びが次ループで eval
改善につながることを実証する（成功条件 a）。

## 二信号モデル（signal vs reward・設計の肝・安全核）

各 episode は 2 つの異なる信号を生む。

- `signal`（**ground-truth 一次**）: 内側 verify（test/lint/exit-code）と `LoopResult.succeeded`
  に由来し driver が計算する。収束/頭打ち/best/評価器昇格/lesson 採用 ― **帰結ある制御は
  すべてこれが駆動**する（評価器の入れ替えに依存しないスケール）。
- `reward`（**epoch 内で固定**した rubric 評価器の出力）: Reflexion の verbal reinforcement
  として **`reflect` だけが消費**する。収束/採用判定には一切載らない。

これにより「gameable な評価器スカラを押し上げて収束を宣言する」抜け道が**構造的に**塞がれる。

## 安全不変条件

安全不変条件（report.md §6 + RQGM。コメントでなく `tests/test_reflexion.py` 等で実証）:

- **評価器を固定して self-optimize させない**: epoch 構造で epoch 内は評価基準を凍結し、
  評価器の更新は **epoch 境界でのみ**。更新は held-out の**固定 gold ラベル**に対する一致度で
  incumbent を ε 超で上回り、かつどの fold/critical probe でも後退しないときに限る
  （ε-best-belief + dominance。`admit_evaluator`）。`epoch_len>=2` / `epsilon>0` を構成時に強制。
- **ground-truth 一次**（test/lint/exit-code）、judge は rubric + 限定（`Score` は多様軸の
  最小値で集約し、欠落軸は 0.0・judge は集約から除外）。
- **早期停止**（`ScorePlateau` の best-so-far トレンドで頭打ちを打ち切り）/ **多様評価** /
  **dual-component 分離**（測定経路は事前収録 probe を採点するだけで production の act/gate に
  触れない。task 名前空間の素性を構成時に検証）/ **memory 取込前検証**（`default_admit` の
  構造的ゲートで grounding を要求し、support は driver が再計算して上書き ＝ 自己申告を信用
  しない。false lesson 注入を弾く）。
- **反省の肥大化・劣化を反復上限で防ぐ**（`EpisodicMemory` の件数/文字/描画バイト上限 +
  `ReflectionBudget` / `MaxEpisodes`）。

```python
from loop_agent import (
    run_reflexion, Evaluator, Score, GroundTruthSignal, HeldOut, Probe,
    Lesson, MaxEpisodes, RubricThreshold, run_loop, ActOutcome, VerifyOutcome,
    MaxIterations,
)
from loop_agent.memory import step_signature

def episode(ctx):                                    # 1 episode = 内側 run_loop を 1 回
    has_lesson = "increment by 1" in ctx.memory_block
    act = lambda _c: ActOutcome(observation="fixed" if has_lesson else "bug", tokens=5)
    verify = lambda o: VerifyOutcome(goal_met="fixed" in o.observation)
    return run_loop(act=act, verify=verify, conditions=[MaxIterations(2)])

def ground_truth(o):                                 # 一次信号は内側 verify 由来（評価器ではない）
    v = 0.95 if o.succeeded else 0.2
    return GroundTruthSignal(succeeded=o.succeeded,
                             score=Score(ground_truth=v, components={"correctness": v}))

def reflect(history, signal, reward):                # 失敗から grounded な lesson を抽出
    if signal.succeeded: return None
    return Lesson(text="increment by 1", episode=0,
                  provenance=step_signature(history[-1]), support=1.0)

result = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect,
    evaluator=Evaluator(score=lambda o: Score(ground_truth=1.0 if o.succeeded else 0.0),
                        name="rubric"),
    convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
    declared_keys=("correctness",),
    production_tasks=["fix-off-by-one"],
    held_out=HeldOut((Probe("h0", {"truth": 0.0}, 0.0), Probe("h1", {"truth": 1.0}, 1.0))),
    epoch_len=2,
)
# ep0 は memory 空で fail(0.20) → 学びを取込 → ep1 は配線された指針で pass(0.95)
# result.succeeded is True / result.best_score == 0.95
```

## 外側 Reflexion の永続化/resume（epoch・lesson テーブル + 評価器 version registry）

外側ループの**学習状態**（epoch 進行・episodic memory の lesson・各 epoch で固定された評価器の
version）を state.db に永続化し、**再起動後も学習の続きから resume** する（Issue #29）。内側
resume（`LoopStore.load_or_init` / #14）と store lease（#21）を土台に、外側専用の 4 表
（`reflexion_run` / `reflexion_episode` / `reflexion_lesson` / `reflexion_evaluator`）を**内側
スキーマと独立・additive**（`IF NOT EXISTS`）に追加する。

- **settled state を SoT に**: `run_reflexion(..., persist=log.on_episode)` の `persist` フックは
  各 episode が**完全に確定した後**（epoch 昇格・評価器入れ替えを含む境界処理の*後*）に発火する。
  `DBReflexionLog` がそれを受けて「episode 行 + memory の全 lesson + reflexion_run スカラ + 評価器
  version 登録」を**1 トランザクション**に束ねて書く。中断地点から resume すると **通し実行と一致**
  する（episode 数 / epoch / 採用 lesson / 評価器 version / best ground-truth）。
- **評価器 version registry + fail-loud**: 各 epoch で固定された評価器の version を
  `reflexion_evaluator` に追記（audit）、現行 version を `reflexion_run` が持つ。resume 時に復元
  `evaluator_version` と渡された `evaluator.version` が食い違えば `run_reflexion` が**loud に弾く**
  （callable は直列化できないので別評価器に silently 差し替えない。PR #28 の安全核を継ぐ）。
  `declared_keys` も同様に整合を要求する（stale な集約での誤収束を防ぐ）。
- **memory 容量ポリシーも往復**: `cap` / `per_lesson_chars` / `render_byte_cap` を保存し、復元時に
  同じ上限の `EpisodicMemory` を組み直すので eviction 挙動が resume をまたいで一致する。`paused`
  episode は未確定なので persist しない（resume で同じ episode を再実行できる）。

```python
from loop_agent import DBReflexionLog, run_reflexion, MaxEpisodes

# 第 1 プロセス: 3 episode 走って中断（接続を閉じる = プロセス終了相当）
log = DBReflexionLog("outer.db", "run-1")          # 新規なら空・既存なら復元した途中状態
result = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect, evaluator=evaluator,
    convergence=[MaxEpisodes(3)], declared_keys=("correctness",),
    production_tasks=["fix"], held_out=held_out,
    initial_state=log.state, memory=log.memory, persist=log.on_episode,   # ← 永続化配線
)
log.record_result(result); log.close()

# 第 2 プロセス: 同じ DB を開き直して resume（epoch・採用 lesson・評価器 version ごと継続）
log2 = DBReflexionLog("outer.db", "run-1")          # state.db から学習状態を復元
result2 = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect, evaluator=evaluator,
    convergence=[MaxEpisodes(6)], declared_keys=("correctness",),
    production_tasks=["fix"], held_out=held_out,
    initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
)
# result2 は通し MaxEpisodes(6) と episode 数/epoch/採用 lesson/評価器 version/best が一致する
```

**スコープ境界**: 単一プロセスの self-improving に集中する（分散協調は Issue #21）。外側
ループの**永続化/resume**（epoch・lesson テーブル + 評価器 version registry）は **state.db へ
実装済み（Issue #29。`ReflexionStore` / `DBReflexionLog`）**。外側ループの **OTel 観測** も
[observability.md](./observability.md)（Issue #30。`run_observed_reflexion`）で接続済み。残る追跡
follow-up は観測の dashboard 化（安全核 = 二信号モデル / epoch 昇格ゲート / 取込前検証には
踏み込まない）。

## 関連

- [README](../README.md) — 全体の入口と動線サマリ
- [reflexion-when-to-use.md](./reflexion-when-to-use.md) — Reflexion を使うべきか・blind retry で足りるかの判断
- [observability.md](./observability.md) — 外側 Reflexion 観測（`run_observed_reflexion`）
- [seams.md](./seams.md) — act / verify などのシーム詳細
