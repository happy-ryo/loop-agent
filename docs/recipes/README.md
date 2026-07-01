# Recipes — coding-agent driven なループの組み方（動線 E）

ここは **動線 E**（coding-agent driven）の具体例集です。各 recipe は次の形をとります:

1. **prose intent** — Claude Code（や Cursor / Codex）にそのまま渡せる自然言語の指示。
2. **組み上がる harness** — coding agent が書く `gather / act / verify / conditions / gate` のおおよその姿。
3. **要点** — そのタスク特有の落とし穴と、verify を ground truth で sharp に書くコツ。

共通する設計の芯は 1 つ: **verify は機械的な ground truth で書く**（pytest の exit-code / AST / 文字列スキャン等）。LLM-as-judge に成功判定を委ねると、ループが「成功したフリ」に収束します。

| Recipe | タスク種別 | verify の ground truth |
|---|---|---|
| [flaky-test-stabilization.md](./flaky-test-stabilization.md) | flaky test の安定化（N 件） | 修正後に対象テストが N 回連続 pass |
| [translation.md](./translation.md) | docstring/コメントの一括翻訳（N ファイル） | 翻訳対象に対象言語が 0 + AST 不変 + 当該テスト pass |
| [refactor.md](./refactor.md) | 挙動不変リファクタ（N module） | 既存テスト全 pass + AST レベルで挙動同値 |
| [multi-item-work-list.md](./multi-item-work-list.md) | N 件を 1 本のループで公平に回す（横断） | （上記各 recipe の verify をそのまま per-item 適用） |
| [self-maintenance.md](./self-maintenance.md) | loop-agent 自身の小さな整合性修正 | stale wording scan + docs link + pytest |
| [review-driven-loop.md](./review-driven-loop.md) | LLM-backed edit の post-act review | review approval + ground-truth verify |
| [circuit-breakers.md](./circuit-breakers.md) | 同じ失敗の繰り返しを早く止める | `NoProgress` / custom `StopCondition` |

> 「このタスクは loop-agent に向いているか?」の最初のフィルタは **verify が sharp に書けるか**。書けないタスク（「もっと良い文章にして」等、機械判定できない目標）は coding agent 側で triage 除外するのが規律です。

## multi-item ループの公平性（全 recipe 共通の注意）

上の recipe はどれも「N 件を回す」multi-item ループです。素朴な `gather`（先頭の未完を返す）だと、1 件が verify 失敗を連続したときに `MaxIterations` を独占し、残りが starve します。これを正規化したのが `WorkListGather`（`loop_agent.discovery.work_list`, Issue #56）— **公平 scheduling + per-item 上限 + done 判定フック**を `gather` として注入できます:

```python
from loop_agent import WorkListGather, WorkListDrained, run_loop, MaxIterations

gather = WorkListGather(
    ["a.py", "b.py", "c.py"],
    strategy="fewest_attempts",     # 試行回数最小から選ぶ round-robin
    max_attempts_per_item=3,        # 1 件が独占しないよう per-item で打ち止め
    done_when=lambda item, rec: rec.observation["passed"],   # この item は終わったか
)
result = run_loop(
    act=my_act, verify=my_verify, gather=gather,
    conditions=[WorkListDrained(gather), MaxIterations(50)],  # drained で停止
)
```

詳しい組み方・戦略の選び方は **[multi-item-work-list.md](./multi-item-work-list.md)**。手書きの round-robin（`min(rem, key=lambda x: (attempts[x], items.index(x)))`）でも同じことはできますが、attempt counter / done 集合の管理と resume 安全を `WorkListGather` が肩代わりします。

## 暴走する 1 回の呼び出しを止める（per-call timeout / kill）

`act` / `verify` の 1 回が暴走（モデルの長考・ツールのハング）したとき、**ループ全体を諦めずに**その 1 回だけを打ち切れます。`run_loop` / `async_run_loop` の `timeout=` 引数に `TimeoutPolicy` を渡します（`graceful` = 諦めて次 iteration / `kill` = `SeamTimeout` を送出）。whole-run の `Timeout` *stop 条件*（進行中 step は中断しない）とは別物です。

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations

result = run_loop(
    act=my_act, verify=my_verify, conditions=[MaxIterations(20)],
    timeout=TimeoutPolicy(act=30.0, verify=10.0, on_timeout="graceful"),
)
```

書き方・モード・**プラットフォーム差（sync シームの hard kill は POSIX main thread のみ／Windows は明示エラー）** は **[timeout-and-kill.md](./timeout-and-kill.md)**。
