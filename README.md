# loop-agent

本格的な **Loop Engineering** を実現する **LoopAgent** の設計・実装プロジェクト。

> Loop Engineering とは、人間がエージェントに一手ずつプロンプトを打つのをやめ、**エージェントをプロンプトし・検証し・記憶させ・再実行する「システム（=ループ）そのもの」を設計する**実践。`prompt engineering → context engineering → loop engineering` という3層スタックの最上位（制御層）に位置する。

## Embeddable Loop Engine for Agents

> **Embeddable Loop Engine for Agents — Bring your own `gather` / `act` / `verify`. We provide the loop.**
> （どこの宿主にも組み込める、エージェント用のループエンジン。policy はあなたが持ち、ループは私たちが回す。）
>
> **Designed to be driven by coding agents — describe your loop in prose, let your agent assemble it.**
> （第一の使い手は人間でなく coding agent。「こういうループを回したい」と書けば、エージェントがシームを組み立てる。）

loop-agent は **Claude Code 専用ライブラリではない**。特定の宿主に縛られず、任意のエージェント / アプリに `pip install` で組み込める。提供するのは `gather → act → verify → repeat` のオーケストレーション本体と安全装置だけで、**policy（何を選び・どう実行し・何を成功とするか）は全部呼び出し側に置く**。だから loop-agent は自分の domain を何も知らないまま、user app の中に小さく住んで「安全にループだけ回すエンジン」として機能する。これが "Embeddable" の本物の意味。

### 設計原則

- **依存最小**: ループコアは Python stdlib のみ。OTel（観測）/ SQLite（状態 SoT）/ `tomli`（3.10 の TOML 読み）等はすべて optional で、未導入でも no-op に degrade する。ランタイムを引きずらない。
- **Protocol ベースの抽象境界**: `gather` / `act` / `verify` / `conditions` / `gate`、さらに `Transport` / `PushBackend` / `WakeQueue` / `WorkDiscovery` がすべて差し替え可能な注入点。
- **runtime 非依存**: tmux / broker / pty / Slack / Web のどれにも縛られない。`act` を subprocess（`claude --print` 等）にするか in-process callable にするかは呼び出し側の自由。
- **安全装置はライブラリ側**: 暴走防止（合成 stop 条件で必ず止まる）/ 限定人間ゲート / Reflexion の安全核（二信号モデル・epoch 昇格ゲート）はコアが提供する。policy を間違えてもループは上限で停止する。

### 組み込み先の例（Claude Code に限らない）

自前 Python スクリプト / 既存の CLI ツール / Web アプリ / MCP サーバー / cron 常駐 / Slack bot / 自社 IDE / 別の AI フレームワーク — どれの内側にも後付けで組み込める。

### 立ち位置（取り込む側 vs 組み込まれる側）

LangGraph / AutoGen / OpenAI Agents SDK が「アプリを自分の枠組みに**取り込む**」フレームワークなのに対し、loop-agent は既存アプリの中に**組み込まれる**ループエンジン。あなたのアーキテクチャを置き換えず、その内側に `while not goal: gather → act → verify` を一つ足すだけ。

### シーム一覧

ループが「持つ」のはオーケストレーション本体だけ。policy は全部この 5 つのシームに注入する:

| シーム | 型 | あなたが決めること |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | 次に何をやるか（候補選定・triage・キュー戦略） |
| `act` | `Callable[[ctx], ActOutcome]` | どう実行するか（モデル選択・LLM provider・subprocess・ローカル fn） |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | 何を「成功」とするか（pytest / AST / regex / 何でも。技術的には何でも差せるが成功判定は **ground truth 推奨**） |
| `conditions` | `list[StopCondition]`（`MaxIterations` 等の stop 条件。`AnyOf` で OR 合成） | いつ止めるか（回数 / 予算 / 目標 / 時間） |
| `gate` | `ActionGate`（`HumanGate` 等。`review(context, state)` 実装。対象選定は `on=Callable[[action], bool]`） | 何に人間承認を要求するか（commit / push / 任意） |

> **verify は ground truth で書く（推奨）**: 何でも差せるのがシームの本質だが、成功判定を LLM-as-judge に委ねるとループは「成功したフリ」に収束しやすい（report.md R1）。pytest の exit-code / AST / 文字列スキャンなど機械的に判定できるものを使う。具体例は [docs/recipes/](./docs/recipes/)。

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # 何を      (gather)
    outcome = act(ctx)         # どう実行  (act)
    v = verify(outcome)        # 何が成功  (verify)
    state.update(v)
```

このループ本体だけが loop-agent。5 つのシームを書けば、それがあなたの domain の loop になる。

## クイックスタート（動線 A〜E）

入り口は 5 つ。**初めてなら動線 E（coding-agent driven）が最短** — 自然言語で「こういうループを回したい」と書けば、Claude Code のような coding agent が上のシームを Python / TOML に落として実行まで持っていく。手で組みたいなら A から読む。

| 動線 | 想定する使い手 | 形 |
|---|---|---|
| **A: 最短デモ** | 自分で書くエンジニア | 5 行 Python（`run_loop` を直接呼ぶ） |
| **B: Claude Code 統合** | 自分で書くエンジニア | `ClaudeCodeAct` を `act` に差し込む 1 行 |
| **C: PoC 実走例** | 動く証拠が欲しい人 | Self-translation PoC の生ログを embeddability の実証として読む |
| **D: 応用パターン** | 経験者 | ModelLadder / Reflexion 合成 / WorkListGather — シームで**自分でも書ける**正準例 |
| **E: coding-agent driven（推奨）** | 意図を持つ全ユーザー | prose intent → coding agent が harness を組む → 実行 |

- Claude Code ユーザー向けの 30 分動線（E primary + 監視 / resume / トラブルシュート）: **[docs/quickstart.md](./docs/quickstart.md)**
- 動線 E の具体 recipe（flaky test 安定化 / 翻訳 / リファクタ）: **[docs/recipes/](./docs/recipes/)**
- Reflexion を使うべきか・blind retry で足りるかの判断: **[docs/reflexion-when-to-use.md](./docs/reflexion-when-to-use.md)**

### 動線 A: 最短デモ（5 行 Python）

`act`（行動）と `verify`（検証 = ground truth）と止め方（`conditions`）を渡して `run_loop` を呼ぶだけ。詳細と完全な例は下の [使い方](#使い方)。

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

### 動線 B: Claude Code 統合（1 行）

`act` に `ClaudeCodeAct` を差し込むと、反復ごとに headless の `claude --print` が 1 回起動してループの実行体になる。詳細は下の [Claude Code 経由でループを回す（headless adapter）](#claude-code-経由でループを回すheadless-adapter)。Codex を使うなら `act` を `CodexAct` に差し替える（`act` の interface は同型。ただし引数は Codex 固有 — `allowed_tools` ではなく `model="gpt-5.5"` / `effort` / `sandbox` / `allowed_args`）。

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="haiku"),
    verify=lambda o: VerifyOutcome(goal_met=(not o.observation.failed) and "DONE" in o.observation.text),
    gather=lambda s: {"prompt": f"次の修正を 1 つ書け（試行 {s.iteration}）"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

### 動線 C: PoC 実走例（embeddability の実証）

**Self-translation PoC** では、loop-agent の*自分自身*のループエンジンを loop-agent の*自分自身*のソースに向け、`ClaudeCodeAct(haiku)` を `act` に据えて `src/loop_agent/` の 10 ファイルの日本語 docstring / コメントを英訳した — **コード・公開 API・型・テスト名は一切変えず**、`pytest` 全件 green を維持して。Run 1（no-Reflexion）は **10/10 ファイル・13 反復・約 33 分・559 件 green 維持**。これは「組み込まれたループエンジンが自分自身を改変しても挙動不変を保てる」ことの実証であり、同時に「実走するから現実の落とし穴が見つかる」例でもある（`ClaudeCodeAct` の token 計上が `cache_read` を累積し `TokenBudget` を誤発火させる bug を発見 → cache_read を計上から除外して修正済み。詳細は [docs/quickstart.md のトラブルシュート](./docs/quickstart.md)）。PoC の設計・全結果は [docs/recipes/translation.md](./docs/recipes/translation.md) に要約。

### 動線 D: 応用パターン（シームで自分でも書ける正準例）

これらは **loop-agent の新機能ではなく、`act` / `gather` シームで user が今日でも書けるパターン**。よく書くことになるので「正しく組まれた書き方」を示す（一部は将来 `adapters/` に reference 実装として packagize 予定）。

**ModelLadder（困難タスクで強いモデルへエスカレーション）** — `act` は `Callable` なので、試行回数を見てモデルを上げる act を自分で書ける。この高頻度パターンを正準例として `loop_agent.adapters.ModelLadder` に packagize した（**新機能ではなく** `act` 合成の reference 実装。落とし穴 — stateful な試行カウント / act は verify の goal 判定を見られない / 異種合成 — をヘッジ済み。Issue #53）:

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

**WorkListGather（multi-item ループの公平 scheduling）** — N ファイル / N bug を回すとき、素朴な「先頭未完を返す `gather`」は 1 件が `MaxIterations` を独占して他を starve させる。試行回数最小から選ぶ round-robin を `gather` に書けば公平になる（Self-translation PoC でこの形を実走）:

```python
def gather(state):
    rem = [f for f in files if f not in done]
    return min(rem, key=lambda f: (attempts[f], files.index(f)))   # 公平 scheduling
```

**Reflexion 合成** — `run_reflexion` は実装済み。escalating act と重ねれば「lessons 蓄積 + モデル昇格」の二段防御になる。ただし **Reflexion が効くのは systematic failure のタスクだけ**で、stochastic な取りこぼしには blind retry と差が出ない（[docs/reflexion-when-to-use.md](./docs/reflexion-when-to-use.md) に判断基準と PoC 実証）。

### 動線 E: coding-agent driven（推奨）

「Bring your own `gather` / `act` / `verify`」をもう一段抽象化すると、**policy を書く主体は人間でなく coding agent** になる:

```
intent（人間の自然言語）
  ↓
coding agent（Claude Code / Cursor / Codex 等）が
  - gather / act / verify / conditions / gate を Python（or TOML）で書く
  - run_loop を起動する
  - 結果（LoopObserver の JSONL）を観察し、必要なら policy を書き直す
  ↓
loop-agent runtime（薄い loop core・不変）
  ↓
results
```

Self-translation PoC は実はこの構造そのもの: 人間が intent を発し、coding agent（Claude Code）が harness / verify を author し、loop-agent runtime が実行し、結果を見て次の判断（Run 2 起動・bug 起票）をした。自然言語 intent で駆動できるので、**コードを書かない user にも届く**。

→ 具体的な進め方は **[docs/quickstart.md](./docs/quickstart.md)**、prose intent から組む recipe は **[docs/recipes/](./docs/recipes/)**。

## 現在のステータス

**MVP → 本格（Phase 3）移行フェーズ**。設計レポートに加え、`gather → act → verify → repeat` の最小ループコア（`src/loop_agent/`）を実装済み。MVP の基盤として**ループ状態の SoT（loop 用最小 SQLite スキーマ + transaction 永続化）**・**中断 → 再開（resume）**・**限定人間ゲート**を導入し、Phase 3 として内側 ReAct の外に**外側 Reflexion ループ + RQGM epoch 安全核**を載せた（report.md §5 Phase 2/3）。残りの本格（transport / work-discovery / dashboard・外側ループの永続化）は今後（report.md §5 Phase 3 / Issue #4）。

## 成果物

| ファイル | 内容 |
|---|---|
| [`report.md`](./report.md) | 調査・設計レポート（**Single Source of Truth**, Markdown） |
| [`report.html`](./report.html) | 同内容の閲覧用単一 HTML（CSS インライン・ブラウザで直接開ける） |
| [`src/loop_agent/`](./src/loop_agent) | PoC ループコア（ループドライバ + 合成可能 stop 条件） |
| [`examples/verify_driven_demo.py`](./examples/verify_driven_demo.py) | 検証駆動デモ（sandbox テストが green になるまで回す実走デモ） |
| [`examples/observed_demo.py`](./examples/observed_demo.py) | 観測デモ（`loop_begin/step/end` を JSONL へ流し、終了理由/メトリクスを見る） |
| [`examples/reflexion_demo.py`](./examples/reflexion_demo.py) | 外側 Reflexion デモ（失敗 episode の学びを次 episode の context へ配線し ground-truth を改善する） |

`report.html` はブラウザで直接開けます（外部 CSS/JS 依存なし）。内容の正本は `report.md` です。

## ループコア（PoC）

report.md §4.4 / §5 Phase 1 に忠実な最小実装。**単一エージェント・単一プロセス**で `gather → act → verify → repeat` を回し、**合成可能なハード上限**（`MaxIterations` / `TokenBudget` / `Timeout`）を OR 評価する。上限到達は**例外ではなく理由付きの制御出力**（`LoopResult`）で返る。

スコープ（欲張らない = *simpler loops win*）:

- ✅ ループドライバ + 機械的な合成 stop 条件（発火した条件と理由を保持）
- ✅ `act` / `verify` は**注入可能なフック**（PoC は in-memory スタブで駆動。LLM 実呼び出しは抽象境界のみ用意）
- ✅ **暴走防止の保証**: ゴール未達・無進捗・反復アクションでも、上限で必ず停止することを sandbox test で証明（`tests/test_runaway_guard.py`）
- ✅ **二重終了条件（意味的 stop）**: 機械的上限に加え、`GoalMet`（検証可能ゴールの達成＝成功終了）と `NoProgress`（無進捗・反復アクションの検出＝打ち切り）を同じ `AnyOf` 合成に載せる
- ✅ **最小状態（進捗ファイル）**: 各反復の記録を JSON Lines で外部ファイルに追記し、プロセスをまたいで進捗が残る（`ProgressLog` / state.db SoT の最小の前身）
- ✅ **観測（構造化イベント + OTel span）**: `loop_begin/step/end` を sink へ流し、終了理由/メトリクスを事後解析できる（`run_observed_loop` / OTel GenAI span）
- ✅ **ループ状態の SoT（state.db）**: loop 用最小 SQLite スキーマ（`run` / `step` / `event` / `stop_reason`）に各 step を **transaction で atomic 永続化**。`DBProgressLog` は `ProgressLog` の drop-in（Issue #11 / MVP の基盤）
- ✅ **中断 → 再開（resume）**: 永続化済み step から `LoopState` を復元し、`run_loop(initial_state=…)` で状態欠落なく途中から継続（iteration・コスト累積・`elapsed`・history を引き継ぐ）。中断して再開した結果が通し実行と一致することを回帰テストで実証（`tests/test_resume.py` / Issue #14）
- ✅ **限定人間ゲート**: 不可逆操作のみ approve/edit/reject/respond で interrupt（state 永続化で pause/resume・不可逆は exactly-once。Issue #15）
- ✅ **複数プロセス同時 resume の協調（in-progress リース）**: 同一 `run_id` を複数プロセスで同時に resume しても、不可逆 action は **exactly-once + 順序整合**（`pending → resolved → executing → executed` 多段化 + リース single-winner）。敗者は `executed` まで pause、勝者クラッシュ時はリース失効で別プロセスが取り直し step も欠落しない。並行プロセス模擬で実証（`tests/test_concurrent_resume.py` / Issue #21）
- ✅ **wake 配送 transport / 次反復入力選定 work-discovery**: 完了/次反復/判断要求 wake を push 一次 / pull fallback で配送（`tests/test_transport.py` / Issue #23）。次反復対象を計算層（決定的 triage）+ 配達層（propose-only 人間ゲート）で選定（`tests/test_discovery.py` / Issue #24）
- ✅ **外側 Reflexion ループ + RQGM epoch 安全核**: 内側 ReAct を 1 episode として包み、失敗からの言語的指針を episodic memory へ取り込み次 context へ配線する self-improving（report.md §5 Phase 3 / Issue #22。下記）
- ⛔ dashboard 化・3x スパイク自動スロットル・サーキットブレーカ・外側ループ永続化は**非スコープ**（report.md §5 Phase 3 残り / Issue #4）

### インストール

```bash
python3 -m pip install -e .        # ループコア本体
python3 -m pip install -e .[dev]   # + pytest（テスト実行用）
```

### 使い方

`act`（行動）と `verify`（検証 = ground truth）を渡し、終了条件を合成して `run_loop` に渡すだけ:

```python
from loop_agent import run_loop, ActOutcome, VerifyOutcome, MaxIterations, TokenBudget, Timeout

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

#### 二重終了条件（GoalMet / NoProgress）

機械的上限と同じ `AnyOf` 合成に**意味的 stop** を載せられる。`GoalMet` は検証可能ゴール
（テスト / lint / rubric の callable）が満たされたら**成功**として停止し、`NoProgress` は同じ
アクションが反復されて進捗が出ない場合に**打ち切り**として停止する。どちらも発火は既存の
`StopTrigger` 形式（`stop.name` = `"goal_met"` / `"no_progress"`）で、宣言順 OR で機械的上限と
矛盾なく共存する:

```python
from loop_agent import run_loop, GoalMet, GoalCheck, NoProgress, MaxIterations

result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # verify フックは使わず条件側で判定
    conditions=[
        GoalMet(lambda state: GoalCheck(met=run_tests() == 0, detail="suite green")),
        NoProgress(window=5, repeat=3),   # 直近 5 ステップで同じアクションが 3 回 → 打ち切り
        MaxIterations(50),                # 機械的バックストップ（R3）
    ],
)
# 成功判定は result.succeeded（verify フック自然終了と GoalMet 条件の両方を吸収）。
# スタックなら stop.name == "no_progress"、どちらも起きなければ "max_iterations" が必ず止める。
```

> `result.goal_met` は **verify フックによる自然終了のみ** を表す（`status == "goal_met"`）。
> `GoalMet` 条件が発火した成功は `status == "stopped"` / `stop.name == "goal_met"` で返るため
> `goal_met` は False のまま。チャネルを問わず成功を判定したい場合は `result.succeeded` を使う。

### 最小状態（進捗ファイル）

各反復の記録を JSON Lines で外部ファイルに追記する最小の永続状態。`ProgressLog.on_step`
を `run_loop` の `on_step` に渡し、終了後に終了理由を 1 行追記するだけ。1 行 = 1 反復の完結した
レコードなので、途中でクラッシュしても直前までの反復は読み戻せる（state.db SoT の最小の前身）。

```python
from loop_agent import run_loop, ProgressLog, read_progress

progress = ProgressLog("progress.jsonl")
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)],
                  on_step=progress.on_step)
progress.record_result(result)               # 終了理由（"result" 行）を追記

records = read_progress("progress.jsonl")     # 反復ごとの "step" 行 + 末尾 "result" 行
```

### 観測（loop_begin / loop_step / loop_end + OTel span）

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

実走デモは [`examples/observed_demo.py`](./examples/observed_demo.py)。

### ループ状態の SoT（state.db）

MVP（report.md §3.4 / §4.6 / §5 Phase 2）では、ループ状態を **SQLite の単一 SoT** に外出しする。
loop 用の**最小スキーマ**（`run` / `step` / `event` / `stop_reason` の 4 テーブルだけ）を `connect`
で生成し、各 step を **`transaction` で atomic に永続化**する。claude-org-ja の `tools/state_db` を
adapt 元にしたが、org 本体（projects / workstreams / snapshotter 等）には**一切依存しない自己完結
スキーマ**として切り出している（疎結合 = report.md §6）。

`DBProgressLog` は JSONL の `ProgressLog` と**同じ `on_step` / `record_result` シグネチャ**を持つ
drop-in なので、観測フックの差し替えだけで SoT を DB に移せる（`run_loop` のシグネチャは不変）。

```python
from loop_agent import run_loop, DBProgressLog, MaxIterations

with DBProgressLog("state.db", run_id="my-run") as db:   # run 行 + loop_begin を確保
    result = run_loop(act=act, verify=verify,
                      conditions=[MaxIterations(5)],
                      on_step=db.on_step)                 # 各反復を atomic 永続化
    db.record_result(result)                             # 終了状態 + stop_reason を確定
```

低レベル API:

```python
from loop_agent import connect, LoopStore

store = LoopStore(connect("state.db"))
state = store.load_or_init("my-run")     # 新規は空 LoopState、既存は step から復元
store.read_steps("my-run")               # 反復ごとの step 行（observation 復号済み）
store.read_events("my-run")              # journal（loop_begin / loop_step / loop_end）
store.get_stop_reason("my-run")          # 発火した停止条件 or goal 達成
```

**中断 → 再開（resume, #14）**。永続化済み step から復元した `LoopState` を
`run_loop(initial_state=…)` に渡すと、中断したループを状態欠落なく途中から継続できる
（iteration カウンタ・コスト累積・`elapsed`・history が引き継がれ、`elapsed` は永続化値から
継続加算される）。`DBProgressLog.state` がその復元結果（新規 run なら空 = fresh start と同義）
なので、新規・再開で同じ配線にできる:

```python
db = DBProgressLog("state.db", "my-run")   # 既存 run なら state を step から復元
result = run_loop(act=act, verify=verify, conditions=[GoalMet(verifier), MaxIterations(100)],
                  initial_state=db.state,   # 中断地点から継続（新規 run は空 state）
                  on_step=db.on_step)
db.record_result(result)
```

resume は**状態ベースの停止条件**（`GoalMet` など state から判定するフック）と組み合わせて
意味を持つ。プロセスをまたぐと act/verify フックは作り直されるが、その内部のコール回数
カウンタは復元されない — 判定を（gather された）state から導けば、新プロセスでも同じ判断を
再現でき、再開結果が通し実行と一致する。

> **observation の型忠実度（resume の限界）**。state.db から復元した `history` の
> `observation` は保存時の JSON を round-trip した値になる（`tuple→list` / dict の
> int キー→str / set・カスタム型・NaN→repr 文字列）。raw な `observation` を直接
> *キー*にする条件（特に `NoProgress` の既定 key）は再開境界で値が変わりうる
> （`tuple` は unhashable な `list` になる）。完全一致で再開したい場合は JSON 安定な
> observation を使うか、`NoProgress(key=…)` に JSON 安定な signature への射影を渡す。

**JSONL と DB は併存**する。`ProgressLog`（JSONL）は依存ゼロで読める PoC アーティファクトとして残し、
`DBProgressLog`（state.db）が MVP 以降の状態 SoT になる。両者は同じ観測フック規約を共有する。

各 step の永続化は「`step` 行 + `run` 集計 + `loop_step` event」を**1 トランザクションに束ねる**ので、
commit 前にプロセスが死んでも半端な行は残らない（クラッシュ耐性）。`UNIQUE(run_id, iteration)` により
同一反復の再永続化は冪等（再開時の replay 安全性）。

### 限定人間ゲート（不可逆操作のみ approve/edit/reject/respond）

MVP（report.md §4.5 / R6 / 原則8 / §5 Phase 2 成功条件 c）では、人間ゲートを
**「不可逆・影響範囲大」のアクションに限定**する（全 step ではない）。LangGraph の
`interrupt()` と同じ 4 種の決定 — **approve / edit / reject / respond** — を持ち、決定を
state.db に**永続化**して **pause → resume をまたいで保持**する。claude-org の
`org-escalation` + `pending_decisions`（state machine）を role 読み替えで reuse している
（「secretary が worker の判断要求を register し user 応答で resolve」→「loop が不可逆
action を register し human が resolve」）。

`HumanGate` は `gather` と `act` の**間**で発火する（= 行動が提案された後・副作用が出る前）。
`on(action)` が `True` の action だけを審査し、reversible な action は素通りする。決定が
未解決なら `run_loop` は `status="paused"` で復帰し、人間が決定を記録した後に**同じ
`run_id`** で再実行すると永続化済みの決定を適用して続行する（同じ action を二度問わない）。

```python
from loop_agent import run_loop, HumanGate, LoopStore, connect, MaxIterations

store = LoopStore(connect("state.db"))
gate = HumanGate(on=lambda a: a == "deploy",   # 不可逆判定（影響範囲大のみ）
                 store=store, run_id="my-run")

# run1: 不可逆 action の手前で pause（決定は pending として永続化される）
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=gate)
# result.paused is True / result.pending["gate_key"] == "gate-0"

# 人間が決定を記録（別プロセス／別接続でも可）
store.resolve_decision("my-run", "gate-0", "approve")          # or "edit"/"reject"/"respond"

# run2: 同じ run_id で再実行 → 永続化済みの approve を適用して続行（再 pause しない）
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=HumanGate(on=..., store=store, run_id="my-run"))
```

- **approve** → 提案 action をそのまま実行 / **edit** → 人間が差し替えた action を実行
  （`resolve_decision(..., "edit", payload=置換 action)`）/ **reject** → 実行せず却下を 1 step
  として記録し継続 / **respond** → 実行せず人間の応答を 1 step として記録し継続（応答は
  `state.history[-1]` 経由で次の `gather` が取り込める）。
- 単一プロセスで人間がその場に居る場合は `HumanGate(..., resolver=fn)` を渡すと pause せず
  inline で解決する（`fn(pending) -> Decision`）。`run_gated_loop(...)` は `HumanGate` の
  構成を `run_loop` に組む薄い入口。`active=False` でゲートを全停止できる。
- 決定レジスタは state.db の `pending_decision` 表（`UNIQUE(run_id, gate_key)` で冪等）に
  載り、発火/決定/実行は journal の `loop_gate` event に残る。
- **resume の契約（不可逆は exactly-once）**: gate key は審査時点の `state.iteration` で決まり、
  resume の 2 モデルのどちらでも安定する。
  - **`initial_state` resume（#14, 推奨）**: 中断時の `LoopState`（`store.load_or_init(run_id)` /
    `DBProgressLog.state`）を `run_loop(initial_state=…)` に渡すと、`iteration` / `tokens_used` /
    `elapsed` / `history` が復元され中断地点から**継続**する。`TokenBudget` / `Timeout` が run を
    跨いで正しく効き、`history` 依存の `gather` も初回と整合する。再開で最初に当たる「中断した
    ゲート」へ iteration ベースのキーが正しく振られ、永続化済み決定が再対応する。
  - **replay resume（`initial_state` なし）**: fresh state で iteration 0 から再生する後方互換
    モード。approve/edit で**実行した不可逆 action は `executed` に確定**され、再生では skip して
    **二度実行しない**（二重 deploy 等の暴発防止）。ただし累積集計は前 run 分リセットされて見え、
    既実行ゲートの skip placeholder で `history` 依存の `gather` が乖離しうるため、**非ゲート
    action は冪等・提案列は iteration に対し決定的**を前提とする。run を跨ぐ累積上限や history 依存の
    再開が要るなら `initial_state` resume を使う。
- **複数プロセス同時 resume の協調（in-progress リース, #21）**: 同一 `run_id` を複数プロセスで
  *同時に* resume してもよい。approve/edit の不可逆 action は `pending → resolved → executing →
  executed` の多段化と **in-progress リース**（`acquire_lease` の `resolved → executing` single-winner
  遷移 + `lease_owner` / `lease_expires_at`）で 1 者だけが実行権を得る。
  - **exactly-once + 順序整合**: `resolved → executing` に成功するのは 1 プロセスだけ。実行中
    （`executing` かつ未失効）に同一ゲートを審査した敗者は **`executed` まで pause** して待つので、
    勝者の不可逆 action 完了前に後続 iteration を走らせない。勝者は `act` 完了後（step 永続化後）に
    `complete_execution` で `executed` を確定する。
  - **勝者クラッシュ復旧**: 勝者が `act` 途中でクラッシュしリースが失効（`lease_expires_at ≤ now`）すると、
    待っていた別プロセスが resume 時にリースを取り直して（`took_over`）実行を完遂する。step 行は完了確定の
    *前* に永続化されるため（driver が `GateReview.on_complete` を `on_step` の後に呼ぶ）、勝者クラッシュでも
    step が欠落しない。
  - **トレードオフ**: 失効取り直しは `act` を再実行するので、勝者が *副作用を起こした後・`executed` 確定の前* に
    クラッシュした稀なケースでは副作用が重複する（**at-least-once**）。完全な exactly-once は副作用側の冪等鍵が
    要る（本モジュール範囲外）。`lease_ttl` を不可逆 action の最大所要より十分長くすれば失効取り直し自体を避けられる。
  リース owner は既定でプロセス毎に一意なトークンを自動生成する（`HumanGate(owner=…)` で明示注入も可）。
  並行 resume の exactly-once / 順序整合 / クラッシュ復旧は `tests/test_concurrent_resume.py`（並行プロセス模擬）で実証。
- `record_result` に `paused` の結果を渡しても run は `running` のまま残り、`stop_reason` も
  書かれない（resume で続行できる）。各 step の正本は `step` 行に残るので監査はそこから行う。

### 外側 Reflexion ループ + RQGM epoch 安全核（self-improving）

本格（report.md §4.4 / §5 Phase 3 / §6 / Issue #22・#4 の RQGM コメント）では、内側 ReAct
ループの**外**に Reflexion 型の試行間ループを重ねる。`run_reflexion(...)` は内側 `run_loop`
を **1 episode** として呼び（driver は内側に手を入れない）、episode 境界で
`reflect(trajectory, signal, reward)` を回して**言語的指針（lesson）**を episodic memory に
取り込み、次 episode の context へ配線する。失敗トラジェクトリからの学びが次ループで eval
改善につながることを実証する（成功条件 a）。

**二信号モデル（設計の肝・安全核）**: 各 episode は 2 つの異なる信号を生む。

- `signal`（**ground-truth 一次**）: 内側 verify（test/lint/exit-code）と `LoopResult.succeeded`
  に由来し driver が計算する。収束/頭打ち/best/評価器昇格/lesson 採用 ― **帰結ある制御は
  すべてこれが駆動**する（評価器の入れ替えに依存しないスケール）。
- `reward`（**epoch 内で固定**した rubric 評価器の出力）: Reflexion の verbal reinforcement
  として **`reflect` だけが消費**する。収束/採用判定には一切載らない。

これにより「gameable な評価器スカラを押し上げて収束を宣言する」抜け道が**構造的に**塞がれる。

**安全不変条件（report.md §6 + RQGM。コメントでなく `tests/test_reflexion.py` 等で実証）**:

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

#### 外側 Reflexion の永続化/resume（epoch・lesson テーブル + 評価器 version registry）

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
実装済み（Issue #29。`ReflexionStore` / `DBReflexionLog`）**。外側ループの **OTel 観測** も次節
（Issue #30）で接続済み。残る追跡 follow-up は観測の dashboard 化（安全核 = 二信号モデル /
epoch 昇格ゲート / 取込前検証には踏み込まない）。

### 外側 Reflexion 観測（episode/epoch/lesson/評価器/収束 + OTel span）

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

### wake 配送 transport（push 一次 / pull fallback / at-most-once）

Phase 3（report.md §3.3 / §4.6 / §5 Phase3 / Issue #23）では、ループの **完了 / 次反復 /
判断要求** の wake を別ループや窓口（受信側）へ届ける配送層を新設する。claude-org runtime の
broker sidecar は runtime 所属で直接再利用できないため、**パターンだけ抽出**して loop-agent 側に
**依存ゼロ（stdlib のみ）**で実装した。

- **push 一次 / pull fallback**: push（即応 accelerator）が通れば即配送、通らなくても wake は
  queue に残り受信側の**能動 poll（pull）で配送が継続**する。push は accelerator、pull poll が
  正準配送路。→ **backend 不通でも配送は途切れない**（§5 Phase3 成功条件 b）。
- **三状態 claim-then-confirm による at-most-once**: `UNDELIVERED → CLAIMED(lease, owner)
  → DELIVERED`。claim で lease 占有して返し、受信側が処理し切ってから confirm で確定する。
  confirm 前に lease 失効した行は再 eligible に戻る（受信側 crash でも配送継続 = at-least-once 側に
  倒す。idle-wake では喪失 > 重複）。owner 一致 + lease 失効チェックの fencing が「届いていないのに
  DELIVERED」喪失窓を塞ぐ（並行 poll は worker ごとに distinct な owner を渡す前提）。確定済みは
  二度と再配達しない。in-memory queue は RLock でスレッド安全（並行 poll の二重 claim を防ぐ）。
- **wake id で de-dup**: wake は決定的 id（`{run_id}:{kind}:{iteration}`）を持ち、二重 enqueue は
  no-op。resume での再配送指示や push/pull の継ぎ目の二重配送を受信側が id で de-dup できる
  （受信側は idempotent handler 前提）。
- **role 別 cadence**: push が失効する pull 環境では「待機」を idle 待機ではなく**能動 poll** に
  翻訳する。受信契機を役割別に非対称設計する（dispatcher 180s / worker 60s / secretary 0 =
  ターン冒頭で毎回 poll）。`cadence_for(role)` / `due_to_poll(role, last_poll, now)`。

```python
from loop_agent import (
    Transport, InMemoryWakeQueue, NullPushBackend, LoopWaker, run_loop, MaxIterations,
)

# backend 不通（push 一次なし）でも pull fallback で配送が継続する構成。
transport = Transport(InMemoryWakeQueue(), NullPushBackend())
waker = LoopWaker(transport, run_id="r1", recipient="coordinator", next_recipient="planner")

result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)])
waker.record_result(result)          # 完了 wake（+ 次反復 wake）を配送 → push 失敗で queue 滞留

# 受信側は役割 cadence で能動 poll。push が落ちていても届く。poll_and_handle は
# handler が成功した wake だけ confirm する crash-safe な受信ループ（処理前に死んだら
# lease 失効で再配送 = at-least-once。受信側は wake.id で de-dup する idempotent handler）。
transport.poll_and_handle("coordinator", lambda wake: handle(wake))
```

`PushBackend` は `push(wake) -> bool` の best-effort 契約（確定配送のみ `True`、不通・例外は
`False` 扱いで pull fallback に委ねる）。実 backend（renga / broker CLI 等）はこの Protocol を
実装して注入する。`CallablePushBackend(fn)` は任意関数を、`NullPushBackend` は「常に push 失敗
（= backend 不通）」を表す。

受信は **claim-then-confirm** が既定: `poll(recipient)` は wake を claim するだけで確定しない
（処理し切ってから `confirm_wakes(wakes, owner=…)`）。処理前にクラッシュした wake は lease 失効で
再配送される（idle-wake では**喪失より重複**を選ぶ設計）。確定漏れを避けたい一般ケースは
`poll_and_handle(recipient, handler)` が handler 成功後に wake 単位で confirm するので推奨。
プロセス内自己完結で handler が決して失敗しない単純ケースのみ `poll(recipient, confirm=True)`
で即確定できる（その経路は poll 後のクラッシュで喪失しうる at-most-once）。

### work-discovery（次反復対象の入力選定・propose-only / 人間ゲート維持）

Phase 3（report.md §3.5 / §4.6 / §5 Phase 3 成功条件 d）では、完了したループの「次に何を
反復するか」を決める**入力選定**を、**計算層（read-only・決定的）と配達層（人間ゲート）の
二層**で実装する。「発見の自律性は上げるが、着手判断は人間に残す」を構造で担保する。

- **計算層 `triage(candidates, *, done=())`**: 副作用ゼロ・同一入力同一出力の純関数。候補
  （`Candidate`）を `done`（完了済み id 集合）に対して triage する — **依存解決**（`depends_on`
  が全て `done` なら *ready*）、**優先度↓ → 工数↑ → id↑** の決定的ランキング、未充足依存の理由
  付け（既知候補待ち / 未知 id）、**依存循環の検出**。「N 件の候補 + 推奨 1 件」を `Triage` で返す。
- **配達層 `WorkDiscovery`**: triage 結果を**提案**として state.db の人間ゲートレジスタ
  （MVP の `pending_decision` を reuse、gate_key は `discovery-<cycle>`）に登録する。**ここで
  必ず止まる（propose-only）**: 完全自動では一切採択せず、人間が `resolve(...)`（= 限定人間
  ゲートと同一経路）で採否を決めるまで pending のまま保持する。4 決定の採択写像 — `approve`→
  推奨を採択 / `edit`→人間が指定した別の *ready* 候補を採択（ready 外は fail loud）/ `reject`→
  採択なし / `respond`→採択なし + 応答記録。決定は pause→resume をまたいで保持される。
- **完了→次反復の接続 `discover_next(...)`**: 直前の `LoopResult` が**完了**しているときだけ
  提案を出す（`paused` なら `None` = まだ何も完了していないので先に人間がゲートを解決すべき）。
  提案 (pending) を登録するだけで採択も次ループ起動もしない（**完全自動着手しない**）。

```python
from loop_agent import discover_next, WorkDiscovery, Candidate, LoopStore, connect

store = LoopStore(connect("state.db"))

# 完了したループ結果 first を受けて次候補を triage → 提案（人間ゲートに pending）
prop = discover_next(store=store, run_id="cycle", result=first, cycle=1,
                     candidates=[Candidate(id="t1", priority=9, payload={"goal": "X"}),
                                 Candidate(id="t2", depends_on=("t1",))])  # t2 は t1 待ちで blocked
# prop.triage.recommended.id == "t1" / prop.pending["status"] == "pending"（採択ゼロ）

# 人間が採否を決めるまで次反復は起きない（propose-only）
wd = WorkDiscovery(store, "cycle")
adoption = wd.resolve(1, "approve")     # or "edit"(payload=id)/"reject"/"respond"
# adoption.candidate.payload == {"goal": "X"} → これを次ループの gather 入力にする
```

### API 概要

| 要素 | 役割 |
|---|---|
| `run_loop(*, act, verify, conditions, gather=…, on_step=…, gate=…, time_fn=…, initial_state=…)` | ループドライバ。`LoopResult` を返す。`gate` を渡すと不可逆操作を interrupt、`initial_state` に復元 `LoopState` を渡すと中断地点から**再開**（resume #14） |
| `ActOutcome(observation, tokens)` | `act` フックの返り値（行動結果 + 消費トークン） |
| `VerifyOutcome(goal_met, detail)` | `verify` フックの返り値（`goal_met=True` で自然終了） |
| `MaxIterations(n)` / `TokenBudget(b)` / `Timeout(s)` | 機械的ハード上限（合成可能 stop 条件） |
| `GoalMet(verifier)` | 検証可能ゴールの達成で**成功**停止（`stop.name="goal_met"`）。`verifier(state)` は `bool` か `GoalCheck(met, detail)` を返す |
| `NoProgress(window, repeat, key=…)` | 直近 `window` ステップで同一 `key`（既定は observation）が `repeat` 回以上 → 無進捗として**打ち切り**（`stop.name="no_progress"`） |
| `LoopResult` | `status`(`goal_met`/`stopped`/`paused`) / `stop`(発火条件) / `reason` / `succeeded`(成功=goal_met 自然終了 or GoalMet 条件発火) / `goal_met`(verify フック自然終了のみ) / `paused`(人間ゲートで中断) / `pending`(中断中の不可逆 action) / `iterations` / `tokens_used` / `elapsed` / `history` |
| `ProgressLog(path)` | 各反復を JSON Lines で追記する最小の永続状態。`on_step` を `run_loop` に渡し、`record_result(result)` で終了理由を追記 |
| `read_progress(path)` | 進捗ファイルを読み戻す（末尾の途中書きクラッシュ行は許容、途中の破損行は送出） |
| `run_observed_loop(*, act, verify, conditions, sinks=…, otel=True, tracer=…, on_step=…, …)` | 観測を配線して `run_loop` を回す入口。`loop_begin/step/end` を emit し OTel span を張る |
| `LoopObserver(sinks, *, conditions=…, otel=True, tracer=…)` | 観測オーケストレータ（context manager）。`on_step` を `run_loop` に渡し `record_result(result)` を呼ぶ |
| `LoopEvent(kind, iteration, elapsed, payload)` | 構造化イベント。`kind` は `loop_begin`/`loop_step`/`loop_end` |
| `ListSink` / `JsonlEventSink(path)` / `CallableSink(fn)` | event sink（in-memory / journal 風 JSONL / 任意関数アダプタ） |
| `read_events(path)` | JSONL イベントを読み戻す（末尾の途中書きクラッシュ行は許容、途中の破損行は送出） |
| `LoopSpan` / `otel_available()` | OTel GenAI span の薄いラッパ（未導入なら no-op）/ OTel 利用可否 |
| `connect(path)` | loop 用 state DB を開き（無ければ作り）最小スキーマを適用した接続を返す（`":memory:"` 可） |
| `LoopStore(conn)` | state.db の writer/reader。`transaction()`（atomic）/ `load_or_init(run_id)`（新規は空・既存は復元 = resume seed）/ `record_step` / `record_result` / `read_steps` / `read_events` / `get_run` / `get_stop_reason` / `request_decision` / `resolve_decision` / `get_decision` / `list_pending_decisions` / `claim_execution`（単一プロセス at-most-once）/ `acquire_lease` / `complete_execution`（複数プロセス同時 resume の in-progress リース, #21） |
| `DBProgressLog(db, run_id)` | DB-backed の進捗記録。`ProgressLog` 互換の `on_step` / `record_result` を持つ drop-in（path か既存接続を受ける context manager）。`.state` が復元した `LoopState`（resume の seed） |
| `HumanGate(*, on, store, run_id, resolver=…, key=…, active=True, owner=…, lease_ttl=…, now_fn=…)` | 不可逆操作のみ interrupt する人間ゲート（`ActionGate` 実装）。`review(context, state)` を `run_loop(gate=…)` に渡す。`owner` / `lease_ttl` / `now_fn` は複数プロセス同時 resume の in-progress リース調整用（#21） |
| `Decision(kind, payload=…)` | 人間の決定（`kind` ∈ `approve`/`edit`/`reject`/`respond`）。`resolver` の返り値 |
| `run_gated_loop(*, act, verify, conditions, on, store, run_id, gather=…, on_step=…, resolver=…, key=…, active=True, owner=…, lease_ttl=…, now_fn=…)` | `HumanGate` を組んで `run_loop` を回す入口（`owner` / `lease_ttl` / `now_fn` は複数プロセス同時 resume 用, #21） |
| `run_reflexion(*, episode, ground_truth, reflect, evaluator, convergence, declared_keys, production_tasks, held_out, epoch_len=4, epsilon=0.02, delta=0.0, propose_evaluator=…, admit_lesson=…, memory=…, on_episode=…, on_epoch=…, persist=…, initial_state=…)` | 外側 Reflexion ループ駆動。内側 `run_loop` を 1 episode として呼び、`reflect` の言語的指針を memory へ取り込み次 context へ配線。`ReflexiveResult` を返す。`on_epoch` は epoch 境界の観測フック（`EpochRecord`）、`persist` は各 episode の **settled state**（epoch 境界処理後）を受ける永続化フック、`initial_state` に復元 `ReflexionState` を渡すと中断地点から**再開**（resume #29） |
| `ReflexionStore(conn)` | 外側 Reflexion 状態の writer/reader（内側 `LoopStore` の対）。生成時に `reflexion_run`/`reflexion_episode`/`reflexion_lesson`/`reflexion_evaluator` の 4 表を **additive・非破壊**に適用。`load_or_init(run_id, memory=…)`（新規は空・既存は復元 = resume seed）/ `persist_episode(run_id, record, state)`（episode+memory+スカラ+version を 1 tx で atomic 永続化）/ `record_result(run_id, result)`（終端メタデータ）/ `get_run` / `read_episodes` / `read_evaluator_versions`（評価器 version registry） |
| `DBReflexionLog(db, run_id, *, memory=…)` | DB-backed の外側進捗記録（内側 `DBProgressLog` の対・drop-in）。`.state`（復元した `ReflexionState` = resume seed）/ `.memory`（live memory）/ `on_episode`（`run_reflexion(persist=…)` に渡す）/ `record_result(result)` / context manager。`memory` は fresh run の容量ポリシー指定（resume では DB 保存値が優先） |
| `ReflexionContext(episode, epoch, task, evaluator, memory_block)` | `episode` フックに渡る文脈。`memory_block`（前試行の学び）を内側 gather に折り込む |
| `ReflexiveResult` | `status`(`converged`/`stopped`/`paused`) / `succeeded`（成功条件が成立 = 順序非依存）/ `paused`（内側 episode が人間ゲートで中断）/ `pending`（中断中の内側 pending）/ `best_score` / `episodes` / `epochs` / `reason` / `state`（`ReflexionState`: `episodes` / `gt_aggregate_history` / `memory` …）。内側 episode が `HumanGate` で pause すると外側も score/reflect せず pause を伝播し、決定を永続化して resume すれば同じ episode を再実行する（Issue #15 の pause/resume 契約） |
| `Score(ground_truth, components=…, judge=…)` | 多軸スコア。`aggregate(declared_keys)` は宣言軸の**最小値**（欠落軸=0.0・judge は除外） |
| `GroundTruthSignal(succeeded, score, ground_truth_backed=True)` | 一次信号（内側 verify 由来）。`ground_truth_backed=False` は収束に算入しない |
| `Evaluator(score, rubric=…, name=…, version=…)` | epoch 内で固定する rubric 評価器（reflect 用 reward を出す。`version` は content-hash） |
| `Probe(case_id, outcome, gold_label, fold=0, critical=False)` / `HeldOut(probes)` | 評価器昇格の測定基盤（固定 gold ラベル。`fold(k)` で回転） |
| `agreement(evaluator, held_out)` / `admit_evaluator(inc, cand, held_out, *, epsilon, delta=0.0)` | gold への一致度（校正）/ ε-best-belief + dominance の昇格ゲート（`AdmissionResult`） |
| `Lesson(text, episode, provenance, support)` / `LessonVerdict(admit, reason)` | 言語的指針 / 取込前検証の判定 |
| `EpisodicMemory(*, cap=8, per_lesson_chars=512, render_byte_cap=4096)` | 有界な episodic memory（`admit` / `render` / 決定的・価値考慮 eviction） |
| `default_admit(lesson, outcome)` | LLM 非依存の構造的取込前検証（grounding + support + 上限。注入 lesson を弾く） |
| `MaxEpisodes(n)` / `RubricThreshold(target, sustain=1)` / `ScorePlateau(window, min_delta)` / `ReflectionBudget(n)` / `EvaluatorUpdateBudget(n)` | 外側収束条件（`AnyOf` 互換。`RubricThreshold` は成功条件） |
| `run_observed_reflexion(*, episode, ground_truth, reflect, evaluator, convergence, declared_keys, production_tasks, held_out, …, sinks=…, otel=True, tracer=…, span_name=…, on_episode=…, on_sink_error=…)` | 観測を配線して `run_reflexion` を回す入口（Issue #30）。`reflexion_begin/episode_*/lesson_decision/epoch_boundary/reflexion_end` を emit し外側 OTel span を張る。判断ロジックは不変（`ReflexiveResult` をそのまま返す） |
| `ReflexionObserver(sinks, *, convergence=…, declared_keys=…, evaluator_version=…, epoch_len=…, epsilon=…, otel=True, tracer=…)` | 外側観測オーケストレータ（context manager）。`on_episode_begin(ctx)` / `on_episode(record, state)` / `on_epoch(record)` を `run_reflexion` の各観測点へ配線し `record_result(result)` を呼ぶ（best-effort・観測フック本体も握る） |
| `EpochRecord(epoch, boundary_episode, previous_version, evaluator_version, admission=…)` | epoch 境界の観測単位。`decision`（`promoted`/`rejected`/`unchanged`）/ `proposed` / `promoted` を導出（`run_reflexion(on_epoch=…)` が渡す純粋な側チャネル記録） |
| `ReflexionSpan` | 外側 Reflexion run の OTel GenAI span 薄ラッパ（未導入なら no-op）。`gen_ai.*` + `loop_agent.reflexion.*`（epoch/version/lesson 由来）を載せ、遷移を span event に刻む |
| event kind: `reflexion_begin` / `episode_begin` / `episode_end` / `lesson_decision` / `epoch_boundary` / `reflexion_end` | 外側観測の構造化イベント種別（内側の `loop_*` と同じ `LoopEvent` / sink / `read_events` を再利用） |
| `Wake(id, kind, recipient, run_id=…, payload=…)` | 配送する 1 wake。`id` が at-most-once / de-dup の鍵。`kind` ∈ `loop_done`/`next_iteration`/`decision_request` |
| `Transport(queue=…, backend=…, *, lease=30.0, time_fn=…)` | push 一次 / pull fallback のオーケストレータ。`deliver(wake)`（→ `"push"`/`"queued"`）/ `poll(recipient, *, owner=…, limit=…, confirm=False)`（claim のみ）/ `poll_and_handle(recipient, handler, …)`（handler 成功後に確定 = crash-safe・推奨）/ `confirm_wakes(wakes, *, owner)` / `pending(recipient=…)` |
| `InMemoryWakeQueue()` | 配送の正本（三状態 claim-then-confirm）。`WakeQueue` Protocol 実装。`enqueue`（冪等）/ `claim` / `confirm` / `release_expired` / `mark_delivered` / `pending` |
| `PushBackend` / `CallablePushBackend(fn)` / `NullPushBackend()` | push（即応 accelerator）の口（`push(wake)->bool` best-effort）/ 任意関数アダプタ / 常に失敗（= backend 不通） |
| `LoopWaker(transport, *, run_id, recipient, next_recipient=…)` | ループ wake を配送する drop-in。`record_result(result)` が完了/判断要求（+次反復）wake を deliver（observer 互換） |
| `wakes_for_result(result, *, run_id, recipient, next_recipient=…)` | `LoopResult` → 配送すべき `Wake` 群への純粋写像（副作用なし） |
| `cadence_for(role)` / `due_to_poll(role, last_poll, now)` | role 別 poll cadence（dispatcher 180s / worker 60s / secretary 0）/ 能動 poll の要否判定 |
| `Candidate(id, priority=0, effort=1, depends_on=(), summary="", payload=None)` | 次反復の仕事候補（全フィールド JSON ネイティブ）。`payload` が採択時に次ループ入力へ渡る値 |
| `triage(candidates, *, done=())` | 計算層（read-only・決定的）。依存解決・ランキング・循環検出して `Triage(ready, blocked, recommended)` を返す |
| `WorkDiscovery(store, run_id)` | 配達層。`propose(candidates, *, done=, cycle=)` で提案を人間ゲートに pending 登録（propose-only・冪等）/ `resolve(cycle, decision, payload=)` で採否記録（edit は ready 候補のみ）/ `adopted(cycle)` で採択結果 `AdoptionResult` を読む（resume をまたいで安定） |
| `AdoptionResult` | 採否解決の結果。`status`(`pending`/`resolved`/`absent`) / `decision` / `candidate`(採択候補 or None) / `recommended` / `response` / `adopted`(候補が採択されたか) |
| `discover_next(*, store, run_id, candidates, result=None, done=(), cycle=0)` | 完了→次反復の接続。`result.paused` なら提案せず `None`、完了していれば `propose` を呼ぶ（採択・起動はしない） |
| `loop_agent.cli:main(argv=None)` | CLI エントリポイント（`[project.scripts]` の `loop-agent`, Issue #31）。`run`/`status`/`resume`/`logs` サブコマンド + 引数なしでクイックヘルプ。プロセス終了コードを返す（成功 0 / 停止 1 / 設定エラー 2） |
| `cli.load_config(path)` / `cli.parse_config(data)` | `task.toml` を検証済み `Config` に読み込む（`[loop]`/`[conditions]`/`[act]`/`[verify]`/`[state]`）。stdlib `tomllib`（3.11+）か 3.10 では `tomli` を使用 |
| `cli.build_conditions(cfg, *, max_iter=…, token_budget=…, timeout=…)` | `Config` から stop 条件を合成（CLI フラグ > TOML 値 > 未指定）。1 つも無ければ `ConfigError`（R3） |
| `cli.build_act(cfg)` / `cli.build_verify(cfg)` | act/verify フックを構築。subprocess（`{prompt}`/`{goal}`/`{iteration}` 置換・exit-code 0 = goal）か Python callable（`module:attr`）の両モード |
| `cli.resolve_callable(spec)` | `module:attr`（または `module.attr`）参照を callable へ解決（Python モード用） |

- `conditions` は stop 条件のリスト（または `AnyOf`）。**宣言順**に OR 評価し、最初に発火したものを `result.stop` として報告する。
- 終了条件は**各反復の先頭（while ガード）で評価**される。`TokenBudget` / `Timeout` は反復境界での判定で、実行中のステップは中断しないため、1 ステップ分だけ上限を超過しうる（消費済みのトークン・時間は取り消せない = "使い切ったら新規ステップを始めない"意味）。
- `gather` を省略すると `LoopState` がそのまま `act` の context になる。`on_step(record, state)` は各反復完了後に呼ばれる最小の観測フック。
- stop 条件を 1 つも渡さないと `ValueError`（無限ループ防止 = R3）。

### CLI ランチャ（loop-agent run / status / resume / logs）

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

`task.toml`（[`examples/task.toml`](./examples/task.toml) も参照）:

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

### 検証駆動デモ（sandbox テストが green になるまで回す）

ループコアを **実コード** に当てた具体デモ。一時 sandbox にわざと壊した関数とその pytest を書き出し、`act`（修正候補を当てる）→ `verify`（**実際の pytest の exit-code** を ground truth に判定）を **テストが green になるまで** 反復する。`goal_met=True`（exit-code 0）でループは**自然終了**し、直らないシナリオでも `MaxIterations` 等の上限で必ず止まる（暴走防止）。LLM judge には頼らない（report.md R1）。

```bash
python3 examples/verify_driven_demo.py
# iter 0: applied candidate #0 -> verify=red   (red (exit=1))
# iter 1: applied candidate #1 -> verify=red   (red (exit=1))
# iter 2: applied candidate #2 -> verify=GREEN (green)
# status: goal_met / iterations: 3 / exit-codes: [1, 1, 0]
```

再利用フックは `loop_agent.demo`（`CandidateApplier` = act / `ExitCodeVerifier` = verify / `attempt_index` = gather）。この実走そのものを `tests/test_verify_demo.py` が pytest で再現・検証する（出荷物 == 検証対象）。

### Claude Code 経由でループを回す（headless adapter）

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

### Codex 経由でループを回す（headless adapter）

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

#### adapter API 概要

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

両者は結果の形（8 フィールド）とプロンプト整形を共通土台 `loop_agent.adapters.base`（`ActResult` 契約 / `ActResultBase` / `render_prompt` / `Runner`）に集約していて、差分は subprocess コマンド・フラグ・token/output 解析だけ。**3 つ目以降のアダプタ（例 `GeminiAct`）を同じ契約で書く手引き**は [docs/adapters/writing-an-adapter.md](./docs/adapters/writing-an-adapter.md)（4 か条の契約 / `ActResult` の形 / token 二重計上の回避 / hard-won lessons / 共通テストハーネスへの登録 / 追加チェックリスト）。

### テスト

```bash
python3 -m pytest        # 各上限の発火 / goal 達成での自然終了 / 終了理由の判別 /
                         # 暴走防止の証明（test_runaway_guard）/ 進捗ファイル（test_progress）/
                         # 検証駆動デモの実走（test_verify_demo）/
                         # 観測: 全終了理由が event に残る・メトリクスが追える・OTel span
                         #   （test_events / test_observe / test_otel）/
                         # 状態 SoT: 永続化・transaction・クラッシュ耐性・スキーマ独立性
                         #   （test_store）/
                         # wake 配送: backend 不通でも pull fallback で配送継続・at-most-once・
                         #   lease 失効再配送・owner fencing・並行 poll 安全・role 別 cadence
                         #   （test_transport / test_waker）/
                         # work-discovery: triage の決定性・依存解決・循環検出 /
                         #   propose-only 人間ゲート・採択写像・完了→次反復の full cycle
                         #   （test_discovery）
```

## レポートの要約

- **Loop Engineering / LoopAgent の徹底調査**: 用語の定義・起源（2026年6月の普及）、agentic loop の系譜（ReAct / Reflexion / Self-Refine / Plan-and-Execute / OODA）、第一世代の教訓（AutoGPT / BabyAGI / AgentGPT）、プロダクションの harness（Anthropic / Claude Code / Cursor / Devin）、フレームワークの LoopAgent 構文（Google ADK / LangGraph / AutoGen / CrewAI / OpenAI Agents SDK）、ループ制御と安全性（終了条件・収束・暴走防止・コスト制御・人間ゲート・観測性・self-improving）。主要主張は出典付き・独立反証検証済み。
- **claude-org-ja 資産棚卸し**: `state.db`（状態 SoT）・transport（push一次/pull fallback）・フィードバックループ（retro/curate/knowledge）・観測/人間ゲート（attention/escalation/pending_decisions）・work-discovery を file 参照付きで再利用評価。
- **LoopAgent 設計**: アーキテクチャ3案を比較し、**「単一制御層 + 共有状態機械 + 段階的 org 資産組込」型（案C）**を推奨。コアループ構造・ループ制御・org 資産活用方針を提示。
- **段階ロードマップ**: PoC（最小ループ + ハード上限）→ MVP（状態機械 + state.db SoT + 二重終了条件 + 観測）→ 本格（フィードバックループ + transport + 入力選定の統合）。

詳細は [`report.md`](./report.md) を参照。

## ライセンス / 言語

Issue / PR は日本語。default branch は `main`。
