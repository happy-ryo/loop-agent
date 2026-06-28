# 観測 (loop events / OTel span / 外側 Reflexion 観測)

LoopAgent のループの一生を構造化イベントと OpenTelemetry span として外に出す観測層。内側ループ (`run_observed_loop`) と外側 Reflexion ループ (`run_observed_reflexion`) を同じ作法で観測できる。

## loop_begin / loop_step / loop_end + OTel span

ループの一生を **構造化イベント** として外に出す観測層（report.md §4.5 / §5 Phase 2）。`run_observed_loop` にループを通すと、`loop_begin` → `loop_step` × N → `loop_end` のイベントが **sink** へ流れる。各イベントは反復番号・累積トークン・elapsed・**終了理由**を運び、ループが「なぜ・どう終わったか」を事後解析できる。同じ run は OTel が入っていれば 1 本の **GenAI span**（`gen_ai.*` + 反復番号 + 終了理由）にもなる。

```python
from loop_agent import run_observed_loop, JsonlEventSink, ListSink, read_events, MaxIterations

mem = ListSink()                                  # in-memory（テスト/検査向け）
result = run_observed_loop(
    act=act, verify=verify,
    conditions=[MaxIterations(5)],
    sinks=[JsonlEventSink("events.jsonl"), mem],  # journal 風 JSONL + in-memory（複数 sink 可）
)

events = read_events("events.jsonl")              # loop_begin / loop_step×N / loop_end
end = mem.of_kind("loop_end")[0]
print(end.payload["status"], end.payload["stop"], end.payload["reason"])
# "stopped" "max_iterations" "reached max iterations (5/5)"
```

- **全終了理由が `loop_end` に残る**: `goal_met` / `max_iterations` / `token_budget` / `timeout`、さらにループ本体が例外で抜けた場合の `error` まで、`status` / `stop` / `reason` として記録される。
- **メトリクスが追える**: `loop_step` は反復番号・`tokens`・累積 `tokens_used`・`elapsed` を運び、`loop_end` の集計と整合する。
- **OTel は optional 依存**: 未導入環境でも `LoopSpan` が **no-op に degrade** し、JSONL / event sink はそのまま機能する。SDK を入れて span を実検査したい場合は `pip install -e .[dev]`（or `.[otel]`）。
- **既存 `ProgressLog` と同じ作法**: 手で配線するなら `LoopObserver` を context manager として使い、`on_step` を `run_loop` に渡して `record_result(result)` を呼ぶ（`sink` 例外はループを殺さず警告に倒す best-effort）。

実走デモは [`examples/observed_demo.py`](../examples/observed_demo.py)。

## 外側 Reflexion 観測（episode/epoch/lesson/評価器/収束 + OTel span）

Phase 3 follow-up（report.md §4.5 の観測性を**外側ループへ延伸** / Issue #30）では、内側ループの
観測層（`run_observed_loop` / `LoopObserver` / `LoopSpan`）と**同じ作法**で外側
`run_reflexion` の試行間ライフサイクルを観測する。`run_observed_reflexion(...)` にループを通すと、
試行間の遷移が **構造化イベント**（`loop_*` と同じ sink へ流れる）として残り、同じ run は OTel が
入っていれば 1 本の **GenAI span**（`gen_ai.*` + epoch 番号 + 評価器 version = 採点係 id +
lesson 由来 provenance）にもなる。**観測は側チャネルであり、二信号モデル / RQGM epoch ゲートの
判断ロジックは一切変えない**（既存安全核はそのまま、観測フックを足すだけ）。

emit される構造化イベント:

- `reflexion_begin` … run 開始（収束条件名・宣言軸・初期評価器 version・epoch 構成）
- `episode_begin` / `episode_end` … 1 episode の開始 / 確定（一次集約・reward・成否・lesson 採否）
- `lesson_decision` … lesson が出た episode のみ。**採用 / 拒否**を独立に残す（filter 容易化）
- `epoch_boundary` … epoch 境界（= 新 epoch 開始）+ **評価器の昇格 / 却下 / 不変**の判定
- `reflexion_end` … run 終了（**収束理由**・status・集計。`result.state` から導出して整合）

```python
from loop_agent import (
    run_observed_reflexion, JsonlEventSink, ListSink, read_events,
    Evaluator, Score, GroundTruthSignal, HeldOut, Probe, Lesson,
    MaxEpisodes, RubricThreshold, run_loop, ActOutcome, VerifyOutcome, MaxIterations,
)
from loop_agent.memory import step_signature

mem = ListSink()
result = run_observed_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect,   # ↑ 前節と同じフック
    evaluator=Evaluator(score=lambda o: Score(ground_truth=1.0 if o.succeeded else 0.0),
                        name="rubric"),
    convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
    declared_keys=("correctness",), production_tasks=["fix-off-by-one"],
    held_out=HeldOut((Probe("h0", {"truth": 0.0}, 0.0), Probe("h1", {"truth": 1.0}, 1.0))),
    epoch_len=2,
    sinks=[JsonlEventSink("reflexion.jsonl"), mem],  # journal 風 JSONL + in-memory（複数 sink 可）
)

events = read_events("reflexion.jsonl")              # reflexion_begin / episode_* / … / reflexion_end
end = mem.of_kind("reflexion_end")[0]
print(end.payload["status"], end.payload["stop"], end.payload["reason"])
# "converged" "rubric_threshold" "rubric threshold reached: last 1 ground-truth aggregates all >= 0.8"
```

- **全遷移が残る**: episode 開始/終了・epoch 開始/境界・lesson 採用/拒否・**採点係（評価器）
  昇格/拒否**・収束理由が event と span event に残り、外側ループの一生を事後解析できる。
- **metric 一貫性**: emit したイベント個数（`episode_end`×N）と最終集計（`reflexion_end` /
  span 終了属性）は権威ある `result.state` から導出するので常に整合する。
- **OTel は optional 依存**: 未導入環境でも `ReflexionSpan` が **no-op に degrade** し、JSONL /
  event sink はそのまま機能する（MVP #13 / 内側 `LoopSpan` と同方針）。SDK を入れて span を実検査
  するなら `pip install -e .[dev]`（or `.[otel]`）。
- **best-effort**: sink / tracer / 観測フックが例外を投げても外側 driver を殺さない（sink 例外は
  警告に倒し、span 例外は握って no-op、フック本体も握る）。例外で抜けた run は `status="error"`、
  内側 episode が人間ゲートで pause した run は `status="paused"` の `reflexion_end` を残す。
- **手で配線も可**: `ReflexionObserver` を context manager として使い、`run_reflexion` の
  `on_episode` / `on_epoch` と `episode` 直前の `on_episode_begin` に配線する（`run_observed_loop`
  に対する `LoopObserver` と同じ関係）。

**スコープ境界**: 本 follow-up は **観測の追加**（events + OTel GenAI span への接続）に絞る。
**dashboard 化**（Grafana 等への可視化パイプライン）と **3x スパイク自動スロットル**（観測値を
使った自動制御＝外側ループへのフィードバック）は本タスクのスコープ**外**で、追跡 follow-up とする
（本 PR は emit 層に徹し、判断ロジック＝安全核には一切載せない）。

## 関連

- [README](../README.md) — 全体の入口と動線サマリ
- [reflexion.md](./reflexion.md) — 外側 Reflexion ループの仕組み
- [seams.md](./seams.md) — act / verify などのシーム詳細
