# クイックスタート — Claude Code ユーザーが 30 分で動かす

このページは、Claude Code を日常的に使っているあなたが loop-agent を **30 分で実走させる**ための動線です。最短は **動線 E（coding-agent driven）** — 自然言語で「こういうループを回したい」と書けば、Claude Code 自身が `gather / act / verify / conditions / gate` を組み立てて実行まで持っていきます。手で組みたい場合の最小形（動線 A / B）もあとに置いてあります。

前提知識は [docs/seams.md](./seams.md) のシーム一覧だけ。初回 harness で触る API は [first-harness-api.md](./first-harness-api.md) に絞ってあります。ループが「持つ」のはオーケストレーション本体だけで、policy（何を選び・どう実行し・何を成功とするか）は全部あなた（または coding agent）が書きます。

---

## 0. インストール（2 分）

```bash
git clone https://github.com/happy-ryo/loop-agent
cd loop-agent
python3 -m pip install -e .          # ループコア本体（依存は stdlib 中心）
python3 -m pip install -e '.[dev]'   # + pytest（テスト実行用。動線 E/C で verify に使う）
# 注: zsh では extras を必ずクォートする（'.[dev]' / '.[otel]'）。素の .[dev] は glob 展開で失敗する。
```

確認:

```bash
loop-agent          # クイックヘルプ + サンプル task.toml が出れば OK
python3 -m pytest   # 全件 green を一度確認しておくと、self-improvement 系の verify が安定する
# Windows で user Temp の権限に詰まる場合: python3 -m pytest --basetemp .pytest-tmp
```

Claude Code の認証は **claude CLI にそのまま委譲**されます（loop-agent は `os.environ` を継承）。既に `claude` でログイン済み（`~/.claude`）か `ANTHROPIC_API_KEY` が通っていれば、追加設定は不要です。

---

## 1. 動線 E: coding agent にループを組ませる（推奨・最短）

### 考え方

loop-agent の最上位の使い方は「自分でシームを書く」ではなく、**Claude Code に書かせる**ことです。あなたは意図を prose で渡すだけ:

```
intent（あなたの自然言語）
  ↓ Claude Code が
  - gather / act / verify / conditions / gate を書く
  - run_loop を起動する
  - 結果（JSONL）を観察し、必要なら policy を書き直す
  ↓
loop-agent runtime（薄い loop core・不変）
  ↓ results
```

### やること（Claude Code セッションの中で）

1. このリポジトリを Claude Code で開く（または `loop-agent` を `pip install` した自分のプロジェクトで）。
2. Claude Code に、loop-agent のシームを教えたうえで意図を渡す。例:

> このリポジトリには loop-agent が入っている（`gather → act → verify → repeat` の薄いループエンジン。シームは `gather/act/verify/conditions/gate`、`act` には `loop_agent.adapters.ClaudeCodeAct` が使える）。
> **このリポジトリの flaky test を見つけて安定化するループを組んで走らせて。** verify は「修正後に対象テストを 10 回連続 pass」、`act` は `ClaudeCodeAct(model="sonnet")`（編集のみ。commit はさせない）、止め方は `MaxIterations(20)` と `TokenBudget`。commit / push は収束後に人間（私）が確認して行う。

3. Claude Code が `harness.py`（gather/act/verify を配線）を書き、`run_loop` を起動します。出来上がる harness はおおよそこの形:

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

flaky = discover_flaky_tests()          # gather の素材（CI ログ等から抽出）

def gather(state):
    rem = [t for t in flaky if t not in done]
    return {"prompt": f"Fix the root cause of flaky test {rem[0]}. ...", "test": rem[0]}

def verify(outcome):
    test = current_test
    passed = run_test_n_times(test, n=10)            # ground truth = 実テスト
    return VerifyOutcome(goal_met=passed, detail=f"{test}: 10x" if passed else "still flaky")

result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # 編集のみ。テスト実行は verify が持つ
    gather=gather, verify=verify,
    conditions=[MaxIterations(20), TokenBudget(2_000_000)],
)
```

4. 結果を観察し、必要なら Claude Code に「verify を 10 回 → 20 回に上げて」「act を haiku に下げてコスト削減して」と指示すれば、policy を書き直して再実行します。**進化するのは loop-agent ではなく、あなたの policy**です。

> production の出発点は [docs/recipes/production-harnesses.md](./recipes/production-harnesses.md) の 3 パターン（single verified edit / multi-item work queue / gated irreversible action）から選びます。完全な recipe（flaky test / 翻訳 / リファクタ）は [docs/recipes/](./recipes/) にあります。Claude Code にそのまま渡せる prose intent 例つきです。

---

## 2. 動線 A / B: 自分で最小ループを書く

coding agent を介さず手で書く場合の最小形です。

### 動線 A: 5 行で `run_loop`

```python
from loop_agent import run_loop, ActOutcome, VerifyOutcome, MaxIterations

n = {"v": 0}
result = run_loop(
    act=lambda ctx: ActOutcome(observation=(n.update(v=n["v"] + 1) or f"step {n['v']}")),
    verify=lambda o: VerifyOutcome(goal_met=n["v"] >= 3),
    conditions=[MaxIterations(5)],   # ゴール未達でも必ず止まる（AutoGPT 的暴走を防ぐ）
)
print(result.status, result.reason)
```

### 動線 B: `ClaudeCodeAct` を `act` に差す

反復ごとに headless の `claude --print` が 1 回起動します。`verify` は **ground truth**（pytest の exit-code 等）で書くのが鉄則 — LLM-as-judge に成功判定を委ねないこと。

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="haiku", timeout=600)

def verify(outcome):
    res = outcome.observation                       # ClaudeCodeResult
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act, verify=verify,
    gather=lambda s: {"prompt": f"次の修正を 1 つ書け（試行 {s.iteration}）"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

Codex を使うなら `act` を `CodexAct` に差し替える。**`act` の interface（callable → `ActOutcome`）は同型**だが、コンストラクタ引数は Codex 固有なので注意 — `allowed_tools` は無く、代わりに `model="gpt-5.5"` / `effort` / `sandbox` / `allowed_args` を取る:

```python
from loop_agent.adapters import CodexAct
act = CodexAct(model="gpt-5.5", effort="medium", timeout=600)   # allowed_tools は CodexAct には無い
# 観測は CodexResult（.text/.failed/.tokens/.returncode/.error）。verify はそのまま使える
```

### verify helper を使う

既存の機械的 oracle がある場合は `verify` を一から書かず、薄い helper を使えます。

```python
from loop_agent import PytestVerifier, CommandVerifier, RegexVerifier

verify = PytestVerifier(["tests/test_loop.py", "-q"], timeout=60)
# or: verify = CommandVerifier(["python", "-m", "ruff", "check", "src"], timeout=60)
# or: verify = RegexVerifier(r"\bDONE\b")
```

これらは LLM-as-judge ではありません。exit-code や regex といった機械的 signal を `VerifyOutcome` に変換するだけです。

### TOML + CLI で回す（コードを書かない最小形）

`task.toml` を書いて `loop-agent run` でも起動できます。詳細は [docs/cli.md](./cli.md)。

```bash
loop-agent run ./examples/task.toml --max-iter 5
loop-agent status <run-id>          # 進捗
loop-agent logs <run-id> --follow   # event を loop_end まで追尾
```

---

## 3. 監視: ループが「何を・なぜ・どう終わったか」を見る

### CLI で見る

```bash
loop-agent status <run-id>   # status / iterations / tokens / stop 理由 / pending
loop-agent logs <run-id>     # loop_begin / loop_step×N / loop_end の構造化イベント
```

### state.db を sqlite3 で覗く

各反復は SQLite の単一 SoT に atomic 永続化されます（`run` / `step` / `event` / `stop_reason` の最小スキーマ）。

```bash
sqlite3 loop-state.db '.tables'
sqlite3 loop-state.db "SELECT iteration, tokens_used, elapsed FROM step WHERE run_id='<run-id>' ORDER BY iteration;"
sqlite3 loop-state.db "SELECT name, detail FROM stop_reason WHERE run_id='<run-id>';"
```

Python からは `LoopStore`:

```python
from loop_agent import connect, LoopStore
store = LoopStore(connect("loop-state.db"))
store.read_steps("<run-id>")        # 反復ごとの step（observation 復号済み）
store.get_stop_reason("<run-id>")   # 発火した停止条件 or goal 達成
```

### OTel span で見る

OTel SDK が入っていれば、各 run は 1 本の GenAI span（`gen_ai.*` + 反復番号 + 終了理由）になります。**未導入でも no-op に degrade** し、JSONL / event sink はそのまま機能します。span を実検査したいときは `pip install -e '.[dev]'`（OTel **SDK** を含む）。注意: `.[otel]` extra は `opentelemetry-api` だけで **SDK を含まない**ため no-op tracer のままで実 span は出ない — span を見るなら `.[dev]` か `opentelemetry-sdk` を明示インストールする。コードは `run_observed_loop(...)` を入口に。

---

## 4. resume: 中断したループを途中から再開する

state.db に永続化済みの step から `LoopState` を復元し、中断地点から状態欠落なく継続できます（iteration・コスト累積・elapsed・history を引き継ぐ）。

```bash
loop-agent resume <run-id> ./examples/task.toml   # CLI
```

```python
from loop_agent import run_loop, DBProgressLog, GoalMet, MaxIterations

db = DBProgressLog("loop-state.db", "<run-id>")   # 既存 run なら state を step から復元
result = run_loop(act=act, verify=verify,
                  conditions=[GoalMet(verifier), MaxIterations(100)],
                  initial_state=db.state,          # 中断地点から継続（新規 run は空 state）
                  on_step=db.on_step)
db.record_result(result)
```

> **resume のコツ**: 停止判定は (gather された) **state から導く**こと。プロセスをまたぐと act/verify フックは作り直され、その内部のコール回数カウンタは復元されません。state から判定すれば新プロセスでも同じ判断を再現できます。詳細は [docs/persistence-and-resume.md](./persistence-and-resume.md)。

---

## 5. トラブルシュート（よくある詰まり）

### Claude Code の認証で落ちる

`ClaudeCodeAct` は `os.environ` を継承して claude CLI に auth を委譲します。`failed=True` で返って `error` に auth 系メッセージが出る場合は、シェルで `claude --print "hi"` が単体で通るか先に確認してください。`env=` で API key を明示注入もできます。

### `TokenBudget` が早すぎるタイミングで発火する（修正済み: Issue #55）

`ClaudeCodeAct` を `Read` + `Edit` 付きで回すと、Claude Code は内部で複数ターン回り、各ターンが cache 済み context を読み直すため、1 回の `act` で報告される `cache_read_input_tokens` の累計が実 input+output の桁違いに膨らみます。初期実装はこれを合算していたため **`TokenBudget` が想定よりはるか手前で発火** していました（Self-translation PoC で発見: 約 170 行 1 ファイルの翻訳が ~340k tokens と計上された）。

**現在は修正済み**です。`ClaudeCodeAct` の token 計上は `input_tokens + output_tokens + cache_creation_input_tokens` のみを積み、課金が軽く累積で膨らむ `cache_read_input_tokens` は除外します（token-cost ポリシ）。そのため `TokenBudget` は実コストに比例して効きます。

- それでも長時間 run を確実に律速したいときは、`MaxIterations` / `Timeout` を併用するのが堅実です（`TokenBudget` 単独に頼らずバックストップを重ねる）。

### verify が timeout する / 永遠に止まらない

- subprocess の `act` / `verify` には**必ず有限の timeout**が掛かります（`[act]`/`[verify]`.`timeout_seconds` > ループ `timeout_seconds` > 既定 3600s）。停止条件は反復境界でのみ評価され、実行中ステップは中断しないので、無制限の subprocess が hang すると全 cap が無効化されます。長い処理には明示 timeout を。
- 停止条件を 1 つも渡さないと `ConfigError`（無限ループ防止）。`max_iterations` か `timeout_seconds` を必ず 1 つ入れること。

### `act` の例外でループが死なないか不安

`ClaudeCodeAct` / `CodexAct` は timeout 超過・非 0 終了・実行ファイル不在を**例外にせず** `failed=True` の結果として graceful に返します。境界評価の `Timeout` / `MaxIterations` は常に効きます。policy が安全に失敗できる設計です。

設定ミス（不正な引数値・型、停止条件なし 等）は `loop_agent.errors.ConfigError`、実行時の状態違反（解決済みゲートの再決定 等）は `StateError` として送出されます。いずれも基底 `LoopError` を `except` すれば一括で捕捉でき、後方互換のため従来の `ValueError` / `RuntimeError` でも捕捉できます。詳細は [errors.md](./errors.md)。

---

## 6. 安全装置の即効メリット

| 装置 | 何が嬉しいか | 書き方 |
|---|---|---|
| **MaxIterations / Timeout / TokenBudget** | ゴール未達でも必ず止まる。AutoGPT 的な暴走・コスト爆発を構造で防ぐ | `conditions=[MaxIterations(20), TokenBudget(...)]`（OR 評価） |
| **HumanGate** | **ループ自身が提案する離散 action だけ**人間承認を挟む。pause→resume をまたいで決定を保持し、不可逆は exactly-once | `HumanGate(on=lambda action: action in {"commit", "push", "deploy"}, store=..., run_id=...)` |
| **Reflexion** | 同じ誤りを繰り返す **systematic failure** で、失敗 episode の lesson を次 episode に配線して改善。ただし stochastic な取りこぼしには効かない（→ [reflexion-when-to-use.md](./reflexion-when-to-use.md)） | `run_reflexion(...)` |

> **HumanGate の射程に注意（重要）**: `HumanGate` が審査するのは `gather` が返す**ループの離散 action**であって、`act` の subprocess（`claude --print`）が内部で実行する `git commit` 等は**見えない**（ゲートは `gather` と `act` の間で発火する）。したがって不可逆操作を本当にゲートしたいなら、(1) act の subprocess に commit / push をさせず（`allowed_tools` を編集系に絞る）、commit / push は**ループ外の人間ステップ**にする、または (2) commit を**ループの離散 action として `gather` に提案させ**、`on` で拾って `act` に実行させる。[docs/safety.md の限定人間ゲート節](./safety.md) が (2) の正準例（`on=lambda a: a == "deploy"`）。

最小の安全テンプレ（self-improvement 系で推奨。act は編集のみ、commit は外）:

```python
# verify は ground truth（pytest exit-code）。必ず止まる上限を 2 つ。
# act の subprocess には commit/push をさせない（ゲートは subprocess の内部操作を見られないため）。
result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # 編集だけ
    verify=verify_with_pytest,
    conditions=[MaxIterations(20), Timeout(3600)],
)
# 収束後、commit / push は人間が確認して実行する（= 不可逆操作をループ外に隔離）。
```

---

## 次に読む

- [docs/recipes/production-harnesses.md](./recipes/production-harnesses.md) — 代表 production harness 3 パターンの選択ガイド
- [docs/recipes/](./recipes/) — 動線 E の prose intent → harness の具体例（flaky test / 翻訳 / リファクタ）
- [docs/first-harness-api.md](./first-harness-api.md) — 初回 harness で使う最小 API surface
- [docs/reflexion-when-to-use.md](./reflexion-when-to-use.md) — Reflexion が効くタスク / 効かないタスクの判断基準（PoC 実証データ）
- [docs/api-reference.md](./api-reference.md) — 全 API 概要・ループコアのスコープ
- [docs/persistence-and-resume.md](./persistence-and-resume.md) / [docs/transport.md](./transport.md) / [docs/reflexion.md](./reflexion.md) — state.db / transport / work-discovery / 外側 Reflexion の詳細
- [README](../README.md) — 全体像と docs/ ナビゲーション
