# claude-loop

本格的な **Loop Engineering** を実現する **LoopAgent** の設計・実装プロジェクト。

> Loop Engineering とは、人間がエージェントに一手ずつプロンプトを打つのをやめ、**エージェントをプロンプトし・検証し・記憶させ・再実行する「システム（=ループ）そのもの」を設計する**実践。`prompt engineering → context engineering → loop engineering` という3層スタックの最上位（制御層）に位置する。

## 現在のステータス

**PoC 実装フェーズ（Phase 1）**。設計レポートに加え、`gather → act → verify → repeat` の最小ループコア（`src/claude_loop/`）を実装済み。MVP / 本格（state.db SoT・Reflexion・人間ゲート・観測の本格化）は今後（report.md §5 Phase 2/3）。

## 成果物

| ファイル | 内容 |
|---|---|
| [`report.md`](./report.md) | 調査・設計レポート（**Single Source of Truth**, Markdown） |
| [`report.html`](./report.html) | 同内容の閲覧用単一 HTML（CSS インライン・ブラウザで直接開ける） |
| [`src/claude_loop/`](./src/claude_loop) | PoC ループコア（ループドライバ + 合成可能 stop 条件） |

`report.html` はブラウザで直接開けます（外部 CSS/JS 依存なし）。内容の正本は `report.md` です。

## ループコア（PoC）

report.md §4.4 / §5 Phase 1 に忠実な最小実装。**単一エージェント・単一プロセス**で `gather → act → verify → repeat` を回し、**合成可能なハード上限**（`MaxIterations` / `TokenBudget` / `Timeout`）を OR 評価する。上限到達は**例外ではなく理由付きの制御出力**（`LoopResult`）で返る。

スコープ（欲張らない = *simpler loops win*）:

- ✅ ループドライバ + 機械的な合成 stop 条件（発火した条件と理由を保持）
- ✅ `act` / `verify` は**注入可能なフック**（PoC は in-memory スタブで駆動。LLM 実呼び出しは抽象境界のみ用意）
- ✅ **暴走防止の保証**: ゴール未達・無進捗・反復アクションでも、上限で必ず停止することを sandbox test で証明（`tests/test_runaway_guard.py`）
- ✅ **最小状態（進捗ファイル）**: 各反復の記録を JSON Lines で外部ファイルに追記し、プロセスをまたいで進捗が残る（`ProgressLog` / state.db SoT の最小の前身）
- ⛔ 人間ゲート・state.db SoT・Reflexion・NoProgress 高度検出は**非スコープ**（Phase 2/3）

### インストール

```bash
python3 -m pip install -e .        # ループコア本体
python3 -m pip install -e .[dev]   # + pytest（テスト実行用）
```

### 使い方

`act`（行動）と `verify`（検証 = ground truth）を渡し、終了条件を合成して `run_loop` に渡すだけ:

```python
from claude_loop import run_loop, ActOutcome, VerifyOutcome, MaxIterations, TokenBudget, Timeout

state = {"n": 0}

def act(ctx):
    """1 ステップ分の行動。observation と消費トークンを返す。"""
    state["n"] += 1
    return ActOutcome(observation=f"did work #{state['n']}", tokens=10)

def verify(outcome):
    """ground truth 検証。goal_met=True でループは自然終了する。"""
    done = state["n"] >= 3
    return VerifyOutcome(goal_met=done, detail="converged" if done else "")

result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(5), TokenBudget(1000), Timeout(30.0)],  # OR 評価
)

print(result.status)   # "goal_met" / "stopped"
print(result.reason)   # "goal met" / "reached max iterations (5/5)" など
print(result.iterations, result.tokens_used)
```

ゴール未達でも上限で必ず止まる（AutoGPT 的な暴走を防ぐ）:

```python
result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # 決して達成しない
    conditions=[MaxIterations(2)],
)
assert result.status == "stopped"
assert result.stop.name == "max_iterations"   # 発火した条件
print(result.reason)                          # "reached max iterations (2/2)"
```

### 最小状態（進捗ファイル）

各反復の記録を JSON Lines で外部ファイルに追記する最小の永続状態。`ProgressLog.on_step`
を `run_loop` の `on_step` に渡し、終了後に終了理由を 1 行追記するだけ。1 行 = 1 反復の完結した
レコードなので、途中でクラッシュしても直前までの反復は読み戻せる（state.db SoT の最小の前身）。

```python
from claude_loop import run_loop, ProgressLog, read_progress

progress = ProgressLog("progress.jsonl")
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)],
                  on_step=progress.on_step)
progress.record_result(result)               # 終了理由（"result" 行）を追記

records = read_progress("progress.jsonl")     # 反復ごとの "step" 行 + 末尾 "result" 行
```

### API 概要

| 要素 | 役割 |
|---|---|
| `run_loop(*, act, verify, conditions, gather=…, on_step=…, time_fn=…)` | ループドライバ。`LoopResult` を返す |
| `ActOutcome(observation, tokens)` | `act` フックの返り値（行動結果 + 消費トークン） |
| `VerifyOutcome(goal_met, detail)` | `verify` フックの返り値（`goal_met=True` で自然終了） |
| `MaxIterations(n)` / `TokenBudget(b)` / `Timeout(s)` | 機械的ハード上限（合成可能 stop 条件） |
| `LoopResult` | `status` / `stop`(発火条件) / `reason` / `iterations` / `tokens_used` / `elapsed` / `history` |
| `ProgressLog(path)` | 各反復を JSON Lines で追記する最小の永続状態。`on_step` を `run_loop` に渡し、`record_result(result)` で終了理由を追記 |
| `read_progress(path)` | 進捗ファイルを読み戻す（末尾の途中書きクラッシュ行は許容、途中の破損行は送出） |

- `conditions` は stop 条件のリスト（または `AnyOf`）。**宣言順**に OR 評価し、最初に発火したものを `result.stop` として報告する。
- 終了条件は**各反復の先頭（while ガード）で評価**される。`TokenBudget` / `Timeout` は反復境界での判定で、実行中のステップは中断しないため、1 ステップ分だけ上限を超過しうる（消費済みのトークン・時間は取り消せない = "使い切ったら新規ステップを始めない"意味）。
- `gather` を省略すると `LoopState` がそのまま `act` の context になる。`on_step(record, state)` は各反復完了後に呼ばれる最小の観測フック。
- stop 条件を 1 つも渡さないと `ValueError`（無限ループ防止 = R3）。

### テスト

```bash
python3 -m pytest        # 各上限の発火 / goal 達成での自然終了 / 終了理由の判別 /
                         # 暴走防止の証明（test_runaway_guard）/ 進捗ファイル（test_progress）
```

## レポートの要約

- **Loop Engineering / LoopAgent の徹底調査**: 用語の定義・起源（2026年6月の普及）、agentic loop の系譜（ReAct / Reflexion / Self-Refine / Plan-and-Execute / OODA）、第一世代の教訓（AutoGPT / BabyAGI / AgentGPT）、プロダクションの harness（Anthropic / Claude Code / Cursor / Devin）、フレームワークの LoopAgent 構文（Google ADK / LangGraph / AutoGen / CrewAI / OpenAI Agents SDK）、ループ制御と安全性（終了条件・収束・暴走防止・コスト制御・人間ゲート・観測性・self-improving）。主要主張は出典付き・独立反証検証済み。
- **claude-org-ja 資産棚卸し**: `state.db`（状態 SoT）・transport（push一次/pull fallback）・フィードバックループ（retro/curate/knowledge）・観測/人間ゲート（attention/escalation/pending_decisions）・work-discovery を file 参照付きで再利用評価。
- **LoopAgent 設計**: アーキテクチャ3案を比較し、**「単一制御層 + 共有状態機械 + 段階的 org 資産組込」型（案C）**を推奨。コアループ構造・ループ制御・org 資産活用方針を提示。
- **段階ロードマップ**: PoC（最小ループ + ハード上限）→ MVP（状態機械 + state.db SoT + 二重終了条件 + 観測）→ 本格（フィードバックループ + transport + 入力選定の統合）。

詳細は [`report.md`](./report.md) を参照。

## ライセンス / 言語

Issue / PR は日本語。default branch は `main`。
