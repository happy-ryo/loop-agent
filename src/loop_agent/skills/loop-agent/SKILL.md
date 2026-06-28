---
name: loop-agent
description: gather-act-verify ループ (loop-agent) を user の domain 向けに設計・実装するときに使う。「ループ書きたい」「自動化したい」「N 件に同じ処理を回したい」「coding agent でループを回したい」「gather-act-verify を設計したい」と言われたら起動。5 シーム (gather/act/verify/conditions/gate) を user の意図から synthesize するための load-on-demand reference bundle。
---

# loop-agent — gather-act-verify ループを user の domain 向けに設計・実装する

loop-agent は「policy はあなたが持ち、ループは私たちが回す」Embeddable なループエンジンである。この skill は coding agent (あなた) が user の domain に合わせて 5 シーム (gather / act / verify / conditions / gate) を設計・実装するための reference bundle で、この SKILL.md がトリガと思考手順、`references/` 配下が on-demand で読む詳細を持つ。**recipe を丸写しするためのものではない** — user の意図を 5 シームへ synthesize し、コードは user の domain に合わせて書く。

## トリガ（再掲）

次のいずれかを言われたら、このスキルの手順で考える。

- 「ループ書きたい」
- 「自動化したい」
- 「N 件に同じ処理を回したい」
- 「coding agent でループを回したい」
- 「gather-act-verify を設計したい」

線引き: 「単発の 1 回処理」「ループ構造が要らないタスク」は対象外 — 反復・収束・停止条件のあるタスクにだけ適用する。

## AI がどう考えるか（手順）

次の 5 ステップで進める。各ステップで読む reference を明示する。全部を一度に読まず、当たった論点だけを on-demand で読む。

1. **まず核を把握する** — `references/design-philosophy.md` を読み、5 シーム (gather / act / verify / conditions / gate) と Embeddable core (policy は注入、ループ本体だけがライブラリ) を頭に入れる。**最初に読むのはこれ 1 本だけ**。
2. **user の domain に必要なシームを設計する** — user の要求 (database / DevOps / 科学計算 / 文書処理 / 何でも) を 5 シームに割り付ける。各シームの設計質問は下の「5 シーム設計チェックリスト」に従う。シームの型・契約・二重終了条件・ground-truth 鉄則を深掘りするなら `references/seams.md`。
3. **必要な reference だけを on-demand で読む** — 全部は読まない。シーム設計で当たった論点に応じて下の対応表から選ぶ。
4. **`examples/` は inspiration として読む（literal コピー禁止）** — `references/examples/{translation,flaky-test,refactor}.md` は「intent → シーム設計」の発想例。user の domain が一致しても**そのまま写さない**。verify の sharp さ・公平 scheduling・commit 隔離といった**設計原理**を借り、コードは user の domain に書き直す。
5. **設計判断を user に提示してから実装する** — 5 シームをどう埋めたか (特に verify の ground-truth 根拠、停止条件、gate 対象) を user に短く提示し、合意を取ってから harness を書く。

### ステップ ↔ reference 対応表

| 局面 | 読む reference |
|---|---|
| 最初の核（必ず最初） | `references/design-philosophy.md` |
| シームの型・契約・二重終了条件・ground-truth 鉄則 | `references/seams.md` |
| act を外部 CLI subprocess にする / 自作 adapter / 4 か条 / token 二重計上 | `references/writing-an-adapter.md` |
| gate の射程 / 不可逆操作の隔離 / `allowed_tools` 規律 / 暴走防止 | `references/safety.md` |
| 中断 → 再開 / state.db SoT / resume 契約 | `references/persistence-and-resume.md` |
| Reflexion を足すべきか（systematic vs stochastic） | `references/reflexion-when-to-use.md` |
| 非同期シーム / `async_run_loop` / sync-async 境界 | `references/async.md` |
| multi-item 公平 scheduling / wake 配送 / work-discovery | `references/transport.md` |
| 例外捕捉（`LoopError` / `ConfigError` / `StateError`） | `references/errors.md` |
| 発想例（写経しない） | `references/examples/translation.md`, `references/examples/flaky-test.md`, `references/examples/refactor.md` |

## 5 シーム設計チェックリスト

各シームについて、user の domain を以下の質問で割り付ける。型は `from loop_agent import ...` のトップレベル公開シンボルに照合済み。

- **gather（次に何をやるか）** — 候補をどう列挙するか。triage / キュー戦略は何か。multi-item なら公平 scheduling (試行回数が最小の item から) が要るか (→ `references/transport.md`)。型は `Callable[[state], ctx]`。省略すると単一文脈で回る。
- **act（どう実行するか）** — 外部 agent CLI を subprocess 起動するか (`from loop_agent.adapters import ClaudeCodeAct, CodexAct` / 自作 adapter は `ActHook` Protocol)、in-process callable か。モデル選択は何か。困難タスクでエスカレーションするなら `from loop_agent.adapters import ModelLadder`。型は `Callable[[ctx], ActOutcome]`、`ActOutcome(observation=..., tokens=...)` を返す。
- **verify（何が成功か）** — 機械検証可能か。**ground truth で sharp に書けるか** (pytest exit-code / AST / regex)。LLM-as-judge に委ねない (「成功したフリ」に収束する)。flaky なら「N 回連続 pass」のように再現性を測る。型は `Callable[[ActOutcome], VerifyOutcome]`、`VerifyOutcome(goal_met=..., detail=...)` を返す。
- **conditions（いつ止めるか）** — 機械的上限 (`MaxIterations` / `TokenBudget` / `Timeout`、`AnyOf` で OR 合成) は**必ず 1 つ以上**置く (無いと `ConfigError`)。意味的 stop (`GoalMet` / `NoProgress`) を載せるか。`run_loop(..., conditions=[...])` に渡す。
- **gate（何に人間承認を要求するか）** — 不可逆操作 (commit / push / deploy) はあるか。**`HumanGate` は `gather` が返す離散 action だけを審査し、`act` subprocess 内部の `git commit` は見えない**。だから「act は編集のみ・不可逆はループ外の人間ステップ」または「commit をループの離散 action にして `on=` で拾う」のどちらかにする (→ `references/safety.md`)。

## 4 か条契約と adapter 落とし穴（act を subprocess 化するとき surface する）

自作 adapter を書く / `act` を外部 CLI にする場合、`references/writing-an-adapter.md` の 4 か条を必ず守る。

1. **例外でループを殺さない** — timeout / 非 0 終了 / 実行ファイル不在は `failed=True` の `ActOutcome` で graceful に返す。漏らしてよい例外は原則ゼロ。唯一 `render_prompt` の `KeyError` は意図的に eager。
2. **token を予算に積む** — 取れないときは 0、成否に関わらず計上する。
3. **auth は CLI に委譲** — `os.environ` を継承し `env=` で上書きマージ。キーをアダプタが読まない。
4. **stdin を塞ぐ** — `stdin=subprocess.DEVNULL`、プロンプトは `--` の後ろの位置引数で渡す。

**token 二重計上の罠（最重要）**: usage は「加算バケットか / 部分集合か」を CLI ごとに確認する。Claude Code は `cache_read_input_tokens` を**除外** (`input+output+cache_creation` のみ)、Codex は `cached_input_tokens` / `reasoning_output_tokens` が部分集合なので `input+output` のみを積む。全フィールドを足すと `TokenBudget` が誤発火する (Issue #55)。

## hard-won lessons（毎回再発見しないために surface する）

詳細は reference に送る。設計時に外さない。

- **token accounting** — 上記 `cache_read` 二重計上 (→ `references/writing-an-adapter.md`)。
- **sync シームの hard-kill は POSIX SIGALRM 依存** — act/verify の per-call timeout/kill は `TimeoutPolicy` (graceful + kill)。**sync シームの実中断は POSIX main thread の `SIGALRM` に依存し、Windows / 非 main thread では graceful へ縮退するか `UnsupportedTimeoutKill` を送出する**。確実な kill が要るなら async シーム + `await async_run_loop(...)` (→ `references/async.md` / `references/errors.md`)。
- **stdin ハング** — `codex exec` は stdin が pipe だと追加入力を読みハングする。`stdin=DEVNULL` 必須 (→ `references/writing-an-adapter.md`)。
- **async-sync 境界** — 同期 `run_loop` に awaitable なシーム (act/verify/gather/condition.check/gate.review) を渡すと `AsyncSeamInSyncLoop`。非同期シームは `await async_run_loop(...)` (→ `references/async.md` / `references/errors.md`)。
- **`allowed_tools` 絞り込み** — self-improvement 系では act を編集系 (`Read` / `Edit`) に絞り、commit / push をループ外に隔離する。`HumanGate` は subprocess 内部操作を見られない (→ `references/safety.md`)。

## examples は inspiration、literal ではない

`references/examples/` は「prose intent → シーム設計スケッチ」の発想カタログである。user の domain に合わせて verify の ground-truth・gather の scheduling・gate の隔離という**原理**を移植し、コードは書き直す。写経テンプレとして使わない。

## 禁止事項

- recipe を rote（写経）適用しない。
- user 要求とズレた cookbook reuse をしない (「翻訳 recipe があるから」と無関係なタスクに当てはめる等)。
- verify を LLM-as-judge にしない (ground truth を最優先)。
- 停止条件を 1 つも置かない構成にしない (`ConfigError` になるし暴走防止が崩れる)。
