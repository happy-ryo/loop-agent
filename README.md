# loop-agent

[![PyPI](https://img.shields.io/pypi/v/loop-agent.svg)](https://pypi.org/project/loop-agent/)
[![Python](https://img.shields.io/pypi/pyversions/loop-agent.svg)](https://pypi.org/project/loop-agent/)
[![CI](https://github.com/happy-ryo/loop-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/happy-ryo/loop-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

loop-agent は、Loop Engineering を実践するための小さな Python ランタイムです。エージェントや既存アプリの中で、次の処理を実行します。

1. `gather` で次にやる作業を取り出す
2. `act` でその作業を実行する
3. `verify` で結果を検証する
4. 成功ならループ終了、未達なら次のイテレーションへ進む
5. イテレーションの最大回数、時間、予算、停滞などの上限に達したら止まる

Loop Engineering で重要なのは、人がエージェントに一手ずつ指示するのではなく、何を集め、どう実行し、何で検証し、いつ止めるかというループを設計することです。
loop-agent は、ループを回すためのエンジンで、利用する人を「何に取り組むのか」「どのように取り組むのか」「どのように検証して完了するのか」「うまくいかないとき、どういう条件で止めるのか」というループの中のできごとに集中できるようにします。

特徴的なのは、ループを Python の関数や CLI で表現できることです。Claude Code や Codex などのコーディングエージェントにループを実装してもらい、実行できます。書いてもらったループは Python のコードとして残るので、コードを見て理解を深めることもできます。

## 何に使うものか

たとえば、次のような処理を安全に繰り返したいときに使います。各反復の結果は履歴に残り、最後は「成功した」「上限で止まった」「承認待ちで止まった」といった結果として返ります。

- たまった GitHub Issue を処理させたい
- テストが通るまでコーディングエージェントに修正させる
- 複数ファイルを 1 件ずつ処理し、終わったものから記録する
- 外部 CLI やモデル呼び出しを実行し、失敗したら次の試行へ進む
- 長い作業を state.db に残し、中断後に再開する
- commit / push などの不可逆操作だけ手動承認を挟む

## インストール

```bash
pip install loop-agent
```

Claude Code / Codex / Cursor などのコーディングエージェントにループを書かせる場合は、loop-agent 用の skill もインストールします。

```bash
loop-agent install-skills
loop-agent install-skills --target-agent codex
loop-agent install-skills --target-agent cursor
```

## 最小例

```python
from loop_agent import ActOutcome, MaxIterations, VerifyOutcome, run_loop

n = {"value": 0}

def act(_ctx):
    n["value"] += 1
    return ActOutcome(observation=f"step {n['value']}")

def verify(_outcome):
    return VerifyOutcome(goal_met=n["value"] >= 3)

result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(5)],
)

print(result.status, result.reason)
```

`verify` が `goal_met=True` を返すと成功として止まります。成功しなくても、`MaxIterations` などの停止条件で必ず止められます。

## ループを構成する要素

loop-agent のループは、主に 5 つの要素で構成します。

| 名前 | 役割 |
|---|---|
| `gather` | 次に実行する対象を選ぶ |
| `act` | 実際に処理する |
| `verify` | 成功したか検証する |
| `conditions` | 回数、時間、予算、停滞などで止める |
| `gate` | 必要な操作だけ手動承認を挟む |

`gather` を省略すると、現在の状態がそのまま `act` に渡ります。小さなループなら `act`、`verify`、`conditions` だけで始められます。

## ループのひな形を作る

CLI で、ループのひな形を生成できます。

```bash
loop-agent init-harness --template light  --output ./harness-light
loop-agent init-harness --template claude --output ./harness-claude
loop-agent init-harness --template codex  --output ./harness-codex
```

生成されるのは短い `harness.py` と README です。プロンプト、検証コマンド、停止条件、手動承認の対象は、生成後に自分の用途へ合わせて編集します。

## コーディングエージェントと使う

Claude Code、Codex、Cursor などのコーディングエージェントに以下のようなプロンプトでループを書かせる使い方も想定しています。

```text
loop-agent を使って、失敗している pytest を直すループを書いてください。
act はコーディングエージェントに修正を任せ、verify は pytest の終了コードで判定してください。
最大 5 回で止め、commit と push はループ外に置いてください。
```

skill を入れておくと、コーディングエージェントが loop-agent の API や設計パターンを見つけやすくなります。

## 主な機能

- 同期 / 非同期のループ実行: `run_loop`、`async_run_loop`
- 停止条件: 最大反復数、時間、トークン、停滞検出など
- 検証ヘルパー: `CommandVerifier`、`PytestVerifier`、`RegexVerifier`
- 状態記録と再開: progress file / state.db
- 手動承認: 不可逆操作だけを一時停止 / 再開
- アダプタ: `ClaudeCodeAct`、`CodexAct`
- 複数対象の処理: `WorkListGather`
- 観測と運用: summary、dashboard、spike scan
- 外側の改善ループ: Reflexion

## 判断基準

loop-agent に向いているのは、完了条件をテストやコマンド結果などで機械的に判定できる作業です。

良い例:

- `pytest` が通る
- 特定のファイルだけが変更されている
- コマンドの終了コードが 0
- 文字列や AST の条件を満たす
- N 件の作業がすべて done になる

向いていない例:

- 「もっと良い文章にする」
- 「なんとなく品質を上げる」
- 成功判定を毎回人間の感覚に頼る作業

曖昧な目標でも使えますが、その場合は `verify` をどう書くかが設計の中心になります。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/quickstart.md](./docs/quickstart.md) | 最初のループを動かす |
| [docs/first-harness-api.md](./docs/first-harness-api.md) | 初回に使う API |
| [docs/seams.md](./docs/seams.md) | `gather` / `act` / `verify` などの詳細 |
| [docs/verifiers.md](./docs/verifiers.md) | 検証ヘルパー |
| [docs/recipes/](./docs/recipes/README.md) | 具体的なループ例 |
| [docs/adapters/README.md](./docs/adapters/README.md) | Claude Code / Codex アダプタ |
| [docs/persistence-and-resume.md](./docs/persistence-and-resume.md) | 状態保存と再開 |
| [docs/safety.md](./docs/safety.md) | 停止条件と手動承認 |
| [docs/cli.md](./docs/cli.md) | CLI |
| [docs/stability.md](./docs/stability.md) | 互換性契約 |
| [docs/api-reference.md](./docs/api-reference.md) | API 一覧 |

## ステータス

**1.0.0 安定版**。互換性の正本は [docs/stability.md](./docs/stability.md) です。

README は入口として短く保ち、細かい仕様は docs に分けています。

## ライセンス / 開発

ライセンスは [MIT](./LICENSE) です。

Issue / PR は英語で扱います。既定ブランチは `main` です。
