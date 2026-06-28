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

> 「このタスクは loop-agent に向いているか?」の最初のフィルタは **verify が sharp に書けるか**。書けないタスク（「もっと良い文章にして」等、機械判定できない目標）は coding agent 側で triage 除外するのが規律です。

## multi-item ループの公平性（全 recipe 共通の注意）

3 つとも「N 件を回す」multi-item ループです。素朴な `gather`（先頭の未完を返す）だと、1 件が verify 失敗を連続したときに `MaxIterations` を独占し、残りが starve します。**試行回数最小から選ぶ round-robin** を `gather` に書いて公平にしてください:

```python
def gather(state):
    rem = [x for x in items if x not in done]
    return min(rem, key=lambda x: (attempts[x], items.index(x)))   # 公平 scheduling
```

（この「fair scheduling + per-item 上限」は将来 `WorkListGather` として packagize 予定。今は上記を自分で書きます。）
