# CLI ランチャ (loop-agent run / status / resume / logs)

宣言的な `task.toml` から `gather -> act -> verify -> repeat` ループを起動する stdlib（argparse）製の CLI（Issue #31）。`act` / `verify` は **(1) subprocess command** か **(2) Python callable**（`module:attr`）のどちらでも書ける。各反復は state.db SoT（`DBProgressLog`）へ永続化されるので、run-id で進捗確認・**resume**・event 追跡ができる。

```bash
pip install -e .            # [project.scripts] の loop-agent を入れる
loop-agent                  # クイックヘルプ + サンプル task.toml を表示

# 起動（TOML 定義から。--max-iter / --token-budget / --timeout で TOML を上書き）
loop-agent run ./examples/task.toml
loop-agent run ./examples/task.toml --max-iter 5 --timeout 600
# run-id     : run-20260628-002431-ab12cd
# status     : goal_met / stopped / paused
# reason     : goal met
# iterations : 3 / tokens : 0 / elapsed : 0.123s

loop-agent status <run-id>            # state.db の進捗（status/iterations/tokens/stop 理由/pending）
loop-agent resume <run-id> ./examples/task.toml   # 中断ループを途中から再開（復元 state を seed）
loop-agent logs <run-id>              # LoopObserver の event（loop_begin/step/end）を表示
loop-agent logs <run-id> --follow     # 新規 event を loop_end まで追尾（tail -f 風）
```

`task.toml`（[`examples/task.toml`](../examples/task.toml) も参照）:

```toml
[loop]
goal = "make the test suite pass"
# run_id = "demo-run"        # 省略時は自動採番

[conditions]                 # 1 つ以上必須（無いと打ち切れない = R3 で拒否）
max_iterations = 20
token_budget = 500000
timeout_seconds = 3600
# no_progress = { window = 5, repeat = 3 }   # 任意: スタック検出で打ち切り

[act]
# subprocess モード: {prompt}/{goal} -> [loop].goal, {iteration} -> 反復番号
# ここでは ClaudeCodeAct 相当の claude を例示。codex exec / 自作ツールなど
# 任意の subprocess command が同じ書式で act シームに刺さる（ActHook Protocol）。
command = ["claude", "--print", "{prompt}"]
cost_per_step = 0            # 1 ステップに計上するトークン（token_budget 用）
# timeout_seconds = 120      # 任意: act subprocess の上限
# python = "mypkg.hooks:act" # OR: in-process callable act(context) -> ActOutcome

[verify]
# subprocess モード: exit-code 0 == goal 達成（ground truth）
command = ["pytest", "-q"]
# python = "mypkg.hooks:verify"  # OR: callable verify(outcome) -> VerifyOutcome

[state]
# db = "loop-state.db"       # 任意: 既定は loop-state.db（複数 run を保持）
# events = "events.jsonl"    # 任意: state.db に加えて JSONL event journal も出力
```

- **条件の上書き優先順**: CLI フラグ > `[conditions]` > 未指定。`verify` の自然終了（`goal_met`）でゴール到達するため、明示的な `GoalMet` 条件は不要。
- **必ず止まる条件を 1 つ以上**（R3）: `max_iterations` / `timeout_seconds` は必ず発火する。`token_budget` 単独は「トークンが毎ステップ増える」場合のみ有効（subprocess act では `cost_per_step > 0` が必要・既定 0 では発火しないため拒否）。`no_progress` 単独は同一行動の反復に依存し保証されないため拒否。いずれも満たさない設定は `ConfigError`（終了コード 2）。
- **終了コード**: ゴール到達（`result.succeeded`）で `0`、ハード上限などで停止すると `1`、設定/使用法エラーは `2`（メッセージは stderr）。
- **db は複数 run を 1 ファイルに保持**し run-id で識別する。`--db` で明示でき、既定は `[state].db`、無ければ `loop-state.db`。
- **subprocess の act/verify には必ず有限の timeout が掛かる**（`[act]`/`[verify]`.`timeout_seconds` > ループ `timeout_seconds` > 既定 3600s）。停止条件は反復境界でのみ評価され実行中ステップは中断しないため、無制限の subprocess が hang すると全 cap を無効化してしまうのを防ぐ。
- `--help` の文字列は ASCII のみ（cp932 コンソールでもクラッシュしない）。

## 関連

- [../README.md](../README.md) — プロジェクト全体の入口
- [./seams.md](./seams.md) — gather / act / verify / conditions / gate の 5 シーム詳細
- [./adapters/README.md](./adapters/README.md) — ClaudeCodeAct / CodexAct / 自作 adapter（ActHook Protocol）の act adapter エコシステム
- [./persistence-and-resume.md](./persistence-and-resume.md) — state.db SoT・run-id・resume の仕組み
