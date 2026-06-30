# Operations Roadmap

loop-agent 0.1.0 は、ループの判断ロジックを変えずに外へ出す **emit 層**を持つ:

- `loop_begin` / `loop_step` / `loop_end` の構造化イベント
- 外側 Reflexion の `episode_*` / `epoch_boundary` / `lesson_decision`
- OTel GenAI span
- `state.db` の run / step / event / stop_reason

このページは、その上に載せる運用機能をまとめる。ここで扱うものは loop core の必須条件ではなく、長時間運用や複数 loop 運用で必要になる policy / UI / 自動制御である。

## Dashboard

dashboard は、イベントと state.db をそのまま可視化する薄い read-only 層にする。`loop-agent summary` が `state.db` の run 一覧を提供し、`loop-agent dashboard --output dashboard.html` が静的 HTML dashboard を出力する。

- run 一覧: `status` / `iterations` / `tokens_used` / `elapsed` / `stop_reason`
- step 時系列: iteration ごとの `tokens` / `tokens_used` / `elapsed`
- paused run: pending decision の `gate_key` / `created_at` / `status`
- Reflexion: episode score / best score / evaluator version / lesson 採否

実装済み:

- `state.db` への read-only SQL と静的 HTML
- `loop-agent summary` / JSONL event sink を読む CLI summary
- step timeline / pending decision / Reflexion summary の HTML 表示

外部連携候補:

- OTel collector + Grafana（loop-agent は span/event を emit し、Grafana 構築は運用側）

境界: dashboard は観測結果の表示に徹し、停止判定・人間ゲート・評価器昇格の判断ロジックを変えない。

## Spike Detection

自動制御の前に、まず検出だけを実装する。

- token spike: 直近 N step の中央値に対し 3x を超える
- latency spike: 直近 N step の elapsed 差分に対し 3x を超える
- error spike: adapter result の `failed=True` が連続する
- verify spike: verify timeout / failed detail が連続する
- no-progress spike: `NoProgress` の key が同一 observation に偏る

検出結果は `loop_spike` event として記録し、初期段階では run を止めない。止めるかどうかはアプリ側 policy が決める。`SpikeDetector` は `on_step` observer として opt-in で使える。保存済み run は `loop-agent spikes [run-id]` で post-hoc scan できる。

## Throttling

throttling は library default ではなく opt-in policy として扱う。

- launch throttling: 新しい run を開始しない
- step throttling: 次 iteration 前に sleep / wake queue へ戻す
- model throttling: `ModelLadder` で安いモデルへ戻す
- budget throttling: `TokenBudget` / `Timeout` をより低くする

実装境界:

- loop-agent が提供するのは観測値、stop condition、transport、adapter の注入点。
- どの閾値で止めるか、遅らせるか、モデルを切り替えるかはアプリ側 policy。

設計詳細は [throttling.md](./throttling.md)。

## Circuit Breakers

circuit breaker は「同じ失敗を続けるループを早く止める」ための stop condition / gate policy として実装する。

候補:

- adapter failure breaker: `failed=True` が K 回連続
- verify failure breaker: 同一 verify detail が K 回連続
- timeout breaker: `ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION` が K 回
- spend breaker: 1 step の token が予算の X% を超える
- human breaker: 同じ gate が reject/respond されたら同一 action を再提案しない

`NoProgress` で表現できるものはまず `NoProgress(key=...)` を使う。共通ケースは `AdapterFailureBreaker` / `VerifyDetailBreaker` / `TimeoutMarkerBreaker` / `PerStepTokenCap` として実装済み。具体例は [recipes/circuit-breakers.md](./recipes/circuit-breakers.md)。

## Tracking

- Dashboard / summary: Issue #107
- Circuit breaker recipes: Issue #108
- Throttling design: Issue #109
- Spike detection: Issue #110
- Circuit breaker helpers: Issue #112
- Post-hoc spike scan: Issue #113
- Throttling helper primitives: Issue #114
- Static HTML dashboard: Issue #115

関連:

- [observability.md](./observability.md)
- [api-reference.md](./api-reference.md)
- [recipes/timeout-and-kill.md](./recipes/timeout-and-kill.md)
- [recipes/circuit-breakers.md](./recipes/circuit-breakers.md)
- [throttling.md](./throttling.md)
