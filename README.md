# loop-agent

**複数の LLM プロバイダーを差し替えられる、Embeddable な Loop Engine。** 本格的な **Loop Engineering** を実現する **LoopAgent** の設計・実装プロジェクト。

> **Embeddable Loop Engine for Agents — Bring your own `gather` / `act` / `verify`. We provide the loop.**
> （どこの宿主にも組み込める、エージェント用のループエンジン。policy はあなたが持ち、ループは私たちが回す。）
>
> **Designed to be driven by coding agents — describe your loop in prose, let your agent assemble it.**
> （第一の使い手は人間でなく coding agent。「こういうループを回したい」と書けば、エージェントがシームを組み立てる。）

loop-agent は任意のエージェント / アプリに `pip install` で組み込めるループエンジンだ。提供するのは `gather → act → verify → repeat` のオーケストレーション本体と安全装置だけで、**policy（何を選び・どう実行し・何を成功とするか）は全部呼び出し側に置く**。`act`（実行体）は **`ClaudeCodeAct` / `CodexAct` / 自作 adapter（`ActHook` Protocol）** から選んで差し込める — 複数の LLM プロバイダーが最初から first-class に揃い、`ActHook` に適合する callable なら何でも同じ `act` シームに載る。だから loop-agent は自分の domain を何も知らないまま、user app の中に小さく住んで「安全にループだけ回すエンジン」として機能する。これが "Embeddable" の本物の意味。

> Loop Engineering とは、人間がエージェントに一手ずつプロンプトを打つのをやめ、**エージェントをプロンプトし・検証し・記憶させ・再実行する「システム（=ループ）そのもの」を設計する**実践。`prompt engineering → context engineering → loop engineering` という 3 層スタックの最上位（制御層）に位置する。

## 設計原則

- **依存最小**: ループコアは Python stdlib のみ。OTel（観測）/ SQLite（状態 SoT）/ `tomli`（3.10 の TOML 読み）等はすべて optional で、未導入でも no-op に degrade する。
- **Protocol ベースの抽象境界**: `gather` / `act` / `verify` / `conditions` / `gate`、さらに `Transport` / `PushBackend` / `WakeQueue` / `WorkDiscovery` / `ActHook` がすべて差し替え可能な注入点。**adapter エコシステムの拡張点**は `ActHook` Protocol で、`ClaudeCodeAct` / `CodexAct` はその reference 実装。
- **runtime 非依存**: tmux / broker / pty / Slack / Web のどれにも縛られない。`act` を subprocess（`claude --print` / `codex exec` 等）にするか in-process callable にするかは呼び出し側の自由。
- **安全装置はライブラリ側**: 暴走防止（合成 stop 条件で必ず止まる）/ 限定人間ゲート / Reflexion の安全核（二信号モデル・epoch 昇格ゲート）はコアが提供する。policy を間違えてもループは上限で停止する。

**組み込み先の例**: 自前 Python スクリプト / 既存の CLI ツール / Web アプリ / MCP サーバー / cron 常駐 / Slack bot / 自社 IDE / 別の AI フレームワーク — どれの内側にも後付けで組み込める。

**立ち位置（取り込む側 vs 組み込まれる側）**: LangGraph / AutoGen / OpenAI Agents SDK が「アプリを自分の枠組みに**取り込む**」フレームワークなのに対し、loop-agent は既存アプリの中に**組み込まれる**ループエンジン。あなたのアーキテクチャを置き換えず、その内側に `while not goal: gather → act → verify` を一つ足すだけ。

## シーム — policy を注入する 5 つの口

ループが「持つ」のはオーケストレーション本体だけ。policy は全部この 5 つのシームに注入する:

| シーム | あなたが決めること |
|---|---|
| `gather` | 次に何をやるか（候補選定・triage・キュー戦略） |
| `act` | どう実行するか（`ClaudeCodeAct` / `CodexAct` / 自作 adapter・モデル選択・subprocess・ローカル fn） |
| `verify` | 何を「成功」とするか（pytest / AST / regex — 成功判定は **ground truth 推奨**） |
| `conditions` | いつ止めるか（回数 / 予算 / 目標 / 時間。`AnyOf` で OR 合成） |
| `gate` | 何に人間承認を要求するか（commit / push / 任意の不可逆操作） |

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # 何を      (gather)
    outcome = act(ctx)         # どう実行  (act)
    v = verify(outcome)        # 何が成功  (verify)
    state.update(v)
```

この 5 つのシームを書けば、それがあなたの domain の loop になる。型・ground-truth の鉄則・二重終了条件（`GoalMet` / `NoProgress`）・検証駆動デモは **[docs/seams.md](./docs/seams.md)**。

## クイックスタート（動線 A〜E）

入り口は 5 つ。**初めてなら動線 E（coding-agent driven）が最短** — 自然言語で「こういうループを回したい」と書けば、coding agent（Claude Code / Cursor / Codex 等）が上のシームを Python / TOML に落として実行まで持っていく。手で組みたいなら A から読む。

| 動線 | 想定する使い手 | 形 |
|---|---|---|
| **A: 最短デモ** | 自分で書くエンジニア | 5 行 Python（`run_loop` を直接呼ぶ・下記） |
| **B: adapter 統合** | 自分で書くエンジニア | `ClaudeCodeAct` / `CodexAct` を `act` に差し込む 1 行 |
| **C: PoC 実走例** | 動く証拠が欲しい人 | Self-translation PoC の生ログを embeddability の実証として読む |
| **D: 応用パターン** | 経験者 | ModelLadder / Reflexion 合成 / WorkListGather — シームで**自分でも書ける**正準例 |
| **E: coding-agent driven（推奨）** | 意図を持つ全ユーザー | prose intent → coding agent が harness を組む → 実行 |

### 動線 A: 最短デモ（5 行 Python）

`act`（行動）と `verify`（検証 = ground truth）と止め方（`conditions`）を渡して `run_loop` を呼ぶだけ。

```python
from loop_agent import run_loop, ActOutcome, VerifyOutcome, MaxIterations

n = {"v": 0}
result = run_loop(
    act=lambda ctx: ActOutcome(observation=(n.update(v=n["v"] + 1) or f"step {n['v']}")),
    verify=lambda o: VerifyOutcome(goal_met=n["v"] >= 3),
    conditions=[MaxIterations(5)],   # ゴール未達でも必ず止まる
)
print(result.status, result.reason)   # goal_met / goal met
```

### 動線 B〜E

- **B（adapter 統合）**: `act=ClaudeCodeAct(...)` または `act=CodexAct(...)` を差すだけ。両者は `act` interface 同型（callable → `ActOutcome`）で 1 行で入れ替えられる。→ **[docs/adapters/](./docs/adapters/README.md)**
- **C（PoC 実走例）**: Self-translation PoC では loop-agent 自身のループエンジンを自身のソースに向け、`ClaudeCodeAct(haiku)` を `act` に据えて 10 ファイルの docstring を英訳した（コード・公開 API・型・テスト名は不変、`pytest` 559 件 green 維持。Run 1 は 10/10・13 反復・約 33 分）。「組み込まれたループエンジンが自分自身を改変しても挙動不変を保てる」ことの実証。→ **[docs/recipes/translation.md](./docs/recipes/translation.md)**
- **D（応用パターン）**: `act` / `gather` シームで今日でも書ける正準例。ModelLadder（困難タスクで強いモデルへエスカレーション） → **[docs/adapters/](./docs/adapters/README.md)**、WorkListGather（multi-item の公平 scheduling） → **[docs/transport.md](./docs/transport.md)**、Reflexion 合成 → **[docs/reflexion.md](./docs/reflexion.md)**。
- **E（coding-agent driven・推奨）**: `intent（人間の自然言語） → coding agent が gather/act/verify/conditions/gate を書く → run_loop 起動 → 結果を観察して policy を書き直す → loop-agent runtime（薄い loop core・不変）`。自然言語 intent で駆動できるので**コードを書かない user にも届く**。→ **[docs/quickstart.md](./docs/quickstart.md)**

## docs/ ナビゲーション

| ドキュメント | 内容 |
|---|---|
| [docs/quickstart.md](./docs/quickstart.md) | 30 分で動かす動線（E primary + 監視 / resume / トラブルシュート） |
| [docs/seams.md](./docs/seams.md) | シーム詳細（型・ground-truth の鉄則・二重終了条件・検証駆動デモ） |
| [docs/adapters/README.md](./docs/adapters/README.md) | act アダプタ（`ClaudeCodeAct` / `CodexAct` / ModelLadder + API 比較表） |
| [docs/adapters/writing-an-adapter.md](./docs/adapters/writing-an-adapter.md) | 自作 adapter の書き方（`ActHook` 契約・token 二重計上の回避） |
| [docs/recipes/](./docs/recipes/README.md) | 動線 E の prose intent → harness 具体例（flaky test / 翻訳 / リファクタ） |
| [docs/persistence-and-resume.md](./docs/persistence-and-resume.md) | 永続化と再開（progress file / state.db SoT / resume #14） |
| [docs/safety.md](./docs/safety.md) | 安全装置（暴走防止 / 限定人間ゲート / 安全テンプレ） |
| [docs/observability.md](./docs/observability.md) | 観測（loop events / OTel span / 外側 Reflexion 観測） |
| [docs/async.md](./docs/async.md) | async/await 対応（`async_run_loop`） |
| [docs/transport.md](./docs/transport.md) | wake 配送 transport と work-discovery / WorkListGather |
| [docs/reflexion.md](./docs/reflexion.md) | 外側 Reflexion ループ + RQGM epoch 安全核 |
| [docs/reflexion-when-to-use.md](./docs/reflexion-when-to-use.md) | Reflexion を使うべきか・blind retry で足りるかの判断 |
| [docs/cli.md](./docs/cli.md) | CLI ランチャ（`loop-agent run / status / resume / logs`） |
| [docs/api-reference.md](./docs/api-reference.md) | 全 API 概要表 + ループコアのスコープ + テスト |
| [docs/errors.md](./docs/errors.md) | 例外階層（`LoopError` / `ConfigError` / `StateError`）と捕捉 |
| [docs/releasing.md](./docs/releasing.md) | リリース手順 |

## 現在のステータス

**MVP → 本格（Phase 3）移行フェーズ**。`gather → act → verify → repeat` の最小ループコア（`src/loop_agent/`）に加え、状態の SoT（state.db）・中断 → 再開（resume）・限定人間ゲート・外側 Reflexion ループ + RQGM epoch 安全核・wake 配送 transport・work-discovery・async/await・CLI ランチャ・act アダプタ（Claude Code / Codex）を実装済み。残る本格化（dashboard・自動スロットル等）は今後（report.md §5 Phase 3 / Issue #4）。各機能の詳細は上の docs/ ナビゲーションから辿れる。

## 成果物

| ファイル | 内容 |
|---|---|
| [`report.md`](./report.md) | 調査・設計レポート（**Single Source of Truth**, Markdown） |
| [`report.html`](./report.html) | 同内容の閲覧用単一 HTML（CSS インライン・ブラウザで直接開ける） |
| [`src/loop_agent/`](./src/loop_agent) | ループコア（ループドライバ + 合成可能 stop 条件 + 各シーム実装） |
| [`examples/`](./examples) | 検証駆動デモ / 観測デモ / Reflexion デモの実走スクリプト |

レポートは Loop Engineering / LoopAgent の徹底調査（用語・系譜・第一世代の教訓・プロダクション harness・フレームワーク比較・ループ制御と安全性）、claude-org-ja 資産棚卸し、3 案比較に基づく設計（案 C 推奨）、段階ロードマップ（PoC → MVP → 本格）を含む。正本は `report.md`。

## ライセンス / 言語

Issue / PR は日本語。default branch は `main`。
