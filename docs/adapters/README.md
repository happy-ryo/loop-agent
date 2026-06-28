# act アダプタ — Claude Code / Codex / 自作

loop-agent は **first-class な act アダプタエコシステム**を同梱する。`ClaudeCodeAct`（headless の `claude --print`）/ `CodexAct`（headless の `codex exec`）/ `ActHook` / `ActResult` Protocol に適合する任意の**自作 adapter** の 3 系統が、いずれも `act` シームに**互換に差し込める**（callable → `ActOutcome`）。エコシステムは開かれていて、3 つ目以降のアダプタ（例 `GeminiAct`）も同じ契約に従えばそのまま `run_loop` の実行体になる。

つまり `act` は特定の宿主に固定された口ではなく、`ActHook` 契約を満たす callable なら何でも受ける拡張点である。以下では同梱の 2 アダプタ（Claude Code / Codex）と、それらを混ぜて使う合成パターン（`ModelLadder`）、そして共通 API を示す。新しいアダプタの書き方は [writing-an-adapter.md](./writing-an-adapter.md) を参照。

## ModelLadder — 異種アダプタ合成の正準例（困難タスクで強いモデルへエスカレーション）

`act` は `Callable` なので、試行回数を見てモデルを上げる act を自分で書ける。この高頻度パターンを正準例として `loop_agent.adapters.ModelLadder` に packagize した（**新機能ではなく** `act` 合成の reference 実装。落とし穴 — stateful な試行カウント / act は verify の goal 判定を見られない / 異種合成 — をヘッジ済み。Issue #53）:

```python
from loop_agent.adapters import ModelLadder, ClaudeCodeAct

act = ModelLadder([
    ClaudeCodeAct(model="haiku"),
    ClaudeCodeAct(model="sonnet"),
    ClaudeCodeAct(model="opus"),
], escalate_on="failure")        # 前段が failed=True なら次段へ昇格

result = run_loop(act=act, ...)
```

`escalate_on` は `"failure"`（前段失敗で昇格）/ 正の int `N`（同段を N 回試したら昇格。act が成功扱いでも verify が goal 未達で反復が続くケースを埋める相補戦略）/ 任意 predicate `Callable[[EscalationContext], bool]`（合成用、例 `lambda ec: ec.last_failed and ec.attempts >= 2`）。異種チェーンもそのまま組める（cost-optimal から始めて難所だけ別プロバイダーに渡す）:

```python
from loop_agent.adapters import ModelLadder, ClaudeCodeAct, CodexAct

act = ModelLadder([ClaudeCodeAct(model="haiku"), CodexAct(model="gpt-5.5"), ClaudeCodeAct(model="opus")])
```

各段は `ActOutcome` を返す任意の `act` フックでよく、結果が共通の `ActResult` 契約（`observation.failed`）に適合していれば異種を混ぜても同じ判断ロジックで扱える（#52 の `ActResult` Protocol が合成性を担保）。実装は `src/loop_agent/adapters/model_ladder.py`、検証は `tests/test_adapters_model_ladder.py`。

## Claude Code 経由でループを回す（headless adapter）

`loop_agent.adapters.ClaudeCodeAct` は、反復ごとに **headless の `claude --print` を subprocess で 1 回起動**する `act` フック。これにより `run_loop` の 1 行で「Claude Code をループの実行体に据える」ことができる（report.md S4.4 の act シーム / Issue #32）。

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

act = ClaudeCodeAct(
    allowed_tools=["Read", "Edit"],   # --allowed-tools
    timeout=600,                       # 超過は failed=True で graceful（例外を投げない）
    model="opus",                      # 省略可（--model のエイリアス可）
    permission_mode="acceptEdits",     # 省略可
    # env=None なら os.environ を継承 → 既存 claude セッション + ANTHROPIC_API_KEY が効く
)

def verify(outcome):
    # 応答は ActOutcome.observation（ClaudeCodeResult）に構造化される。
    # .failed / .text / .tokens / .returncode / .error を見て判定できる。
    res = outcome.observation
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act,
    verify=verify,
    gather=lambda state: {"prompt": f"次の修正を 1 つ書け（試行 {state.iteration}）"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

設計上の約束（ループコアの性質を壊さない）:

- **例外でループを殺さない**: timeout 超過・非 0 終了・実行ファイル不在/権限不足は、例外を送出せず `failed=True` の `ClaudeCodeResult` を載せた `ActOutcome` として graceful に返る。境界評価の `Timeout` / `MaxIterations` は常に効く。
- **token を予算に積む**: `--output-format json`（既定）の `usage` を解析し（無ければ stdout/stderr のフォールバック解析）、`ActOutcome.tokens` に載せる。driver がこれを `state.tokens_used` に積むので `TokenBudget` がそのまま効く。
- **auth は claude CLI に委譲**: 既定で `os.environ` を継承し、既存の claude CLI セッション（`~/.claude` ログイン）を第一義に、`ANTHROPIC_API_KEY` を CLI 側フォールバックとして使う。`env=` で上書きマージできる。
- **プロンプトの組み立て**: `prompt_template`（既定 `"{prompt}"`）を `gather` の戻り値（Mapping / `LoopState` / 文字列）で `str.format` 埋めする。`"... iter={iteration}"` のように `LoopState` のフィールドも埋め込める。

subprocess を使わないテスト/デモには `MockClaudeCodeAct(responses=[...])` を使う（`responses` の各要素は `str` / `dict` / `ClaudeCodeResult`。`{"text": ..., "tokens": ..., "failed": ...}` で `TokenBudget` や失敗系も in-memory で再現できる）。実装は `src/loop_agent/adapters/claude_code.py`、検証は `tests/test_adapters_claude_code.py`。

```python
from loop_agent.adapters import MockClaudeCodeAct
act = MockClaudeCodeAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

非スコープ: TUI モード / stream-json の深い統合 / Plan mode 連携。

## Codex 経由でループを回す（headless adapter）

`loop_agent.adapters.CodexAct` は `ClaudeCodeAct` と**完全同型**の `act` フックで、反復ごとに **headless の `codex exec` を subprocess で 1 回起動**する。差分は subprocess コマンド・フラグ・token/output 解析のみ。これにより同じ `run_loop` に Claude / Codex のどちらでも 1 行で差し替えられる（report.md S4.4 の act シーム / Issue #49）。

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import CodexAct

act = CodexAct(
    model="gpt-5.5",        # -m（ChatGPT アカウント運用では gpt-5.5 系を明示）
    effort="medium",        # -c model_reasoning_effort=<effort>
    timeout=600,            # 超過は failed=True で graceful（例外を投げない）
    # sandbox="workspace-write",  # 省略可（-s。None で codex 既定）
    # env=None なら os.environ を継承 → 既存 codex セッション + OPENAI_API_KEY が効く
)

def verify(outcome):
    # 応答は ActOutcome.observation（CodexResult）に構造化される。
    # .failed / .text / .tokens / .returncode / .error を見て判定できる。
    res = outcome.observation
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act,
    verify=verify,
    gather=lambda state: {"prompt": f"次の修正を 1 つ書け（試行 {state.iteration}）"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

設計上の約束は `ClaudeCodeAct` と同一（例外でループを殺さない / token を予算に積む / auth は CLI に委譲 / `prompt_template` 埋め込み）。Codex 固有の差分は次の 3 点:

- **token 種別の意味**: Codex/OpenAI の `usage` は `cached_input_tokens` が `input_tokens` の、`reasoning_output_tokens` が `output_tokens` の**部分集合**。そのため総処理量は `input_tokens + output_tokens` のみで取り、二重計上を避ける（`--json` の `turn.completed` を解析、無ければ正規表現フォールバック）。
- **応答本文**: 単一フィールドではなく `--json` の JSONL イベント列に乗るため、最後の `agent_message`（`item.completed`）の `text` を本文として採る。
- **stdin 固定**: codex は stdin が pipe だと追加入力を読みに行くため、子の stdin は `DEVNULL` に固定する（プロンプトは `--` 後の位置引数で確定済み）。`--skip-git-repo-check` を既定 on にし、git リポジトリ外でも起動失敗しない。

subprocess を使わないテスト/デモには `MockCodexAct(responses=[...])` を使う（`ClaudeCodeAct` 版と同じ契約。各要素は `str` / `dict` / `CodexResult`）。実装は `src/loop_agent/adapters/codex.py`、検証は `tests/test_adapters_codex.py`。

```python
from loop_agent.adapters import MockCodexAct
act = MockCodexAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

非スコープ: TUI モード（Issue #34）/ stream-json の深い統合。

## adapter API 概要

| 項目 | `ClaudeCodeAct` | `CodexAct` |
| --- | --- | --- |
| 起動コマンド | `claude --print [--output-format json] -- <prompt>` | `codex exec [--json] [--skip-git-repo-check] -m <model> -c model_reasoning_effort=<effort> -- <prompt>` |
| 主な引数 | `allowed_tools` / `model` / `permission_mode` / `output_format` / `extra_args` | `model="gpt-5.5"` / `effort="medium"` / `sandbox` / `json_output` / `skip_git_repo_check` / `allowed_args` |
| 共通引数 | `timeout` / `prompt_template` / `env` / `cwd` / `runner` | 同左 |
| 観測オブジェクト | `ClaudeCodeResult(.text/.failed/.tokens/.returncode/.error)` | `CodexResult(.text/.failed/.tokens/.returncode/.error)` |
| token 集計 | `usage` の全 `*tokens*` を合算 | `input_tokens + output_tokens`（cached/reasoning は部分集合のため除外） |
| auth | os.environ 継承（claude セッション + `ANTHROPIC_API_KEY`） | os.environ 継承（codex セッション + `OPENAI_API_KEY`） |
| 失敗時 | `failed=True` を観測に載せて graceful（例外なし） | 同左 |
| Mock | `MockClaudeCodeAct(responses=[...])` | `MockCodexAct(responses=[...])` |

両者は結果の形（8 フィールド）とプロンプト整形を共通土台 `loop_agent.adapters.base`（`ActResult` 契約 / `ActResultBase` / `render_prompt` / `Runner`）に集約していて、差分は subprocess コマンド・フラグ・token/output 解析だけ。**3 つ目以降のアダプタ（例 `GeminiAct`）を同じ契約で書く手引き**は [writing-an-adapter.md](./writing-an-adapter.md)（4 か条の契約 / `ActResult` の形 / token 二重計上の回避 / hard-won lessons / 共通テストハーネスへの登録 / 追加チェックリスト）。

## 関連

- [../../README.md](../../README.md) — loop-agent の入口（positioning / シーム / 動線サマリ）
- [writing-an-adapter.md](./writing-an-adapter.md) — 3 つ目以降のアダプタ（例 `GeminiAct`）を `ActHook` / `ActResult` 契約で書く手引き
- [../seams.md](../seams.md) — `act` シームを含む 5 シームの詳細仕様と型
- [../api-reference.md](../api-reference.md) — 全 API 概要表とループコアのスコープ
