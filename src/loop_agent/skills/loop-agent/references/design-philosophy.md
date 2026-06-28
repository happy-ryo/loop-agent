# loop-agent 設計哲学 — Embeddable / 5 シーム / coding-agent driven

> これは bundle 内の概念アンカーで、シームの型・契約の正本は `seams.md`。本ファイルは README の positioning と seams.md の核を合成した短い導入であり、設計判断に踏み込むときは各 reference を on-demand で読む。

loop-agent を user の domain に当てはめる前に、まずこの 1 枚で「何を提供し、何を提供しないライブラリか」を掴む。core は `gather → act → verify → repeat` のオーケストレーションと安全装置だけで、policy（何を選び・どう実行し・何を成功とするか）は全部あなたが 5 つのシームに注入する。

## Embeddable Loop Engine

提供するのは `gather → act → verify → repeat` のオーケストレーション本体と暴走防止だけだ。**何を選ぶか・どう実行するか・何を成功とするか — その policy は全部呼び出し側に置く**。だから loop-agent は user の domain を何も知らないまま、既存アプリの内側に小さく住んで「安全にループだけ回すエンジン」として機能する。これが "Embeddable" の本物の意味で、合言葉は **"Bring your own `gather` / `act` / `verify`. We provide the loop."**（policy はあなたが持ち、ループは私たちが回す）。

立ち位置は「取り込む側 vs 組み込まれる側」で区別できる。LangGraph / AutoGen / OpenAI Agents SDK が「アプリを自分の枠組みに**取り込む**」フレームワークなのに対し、loop-agent は既存アプリの中に**組み込まれる**ループエンジンだ。あなたのアーキテクチャを置き換えず、その内側に `while not goal: gather → act → verify` を一つ足すだけ。組み込み先は自前 Python スクリプト / 既存 CLI / Web アプリ / MCP サーバー / cron 常駐 / Slack bot / 別の AI フレームワーク — どれの内側にも後付けできる。

依存は最小だ。ループコアは Python stdlib のみで動く。OTel（観測）/ SQLite（状態 SoT）/ `tomli`（TOML 読み）等はすべて optional で、未導入でも no-op に degrade する。

## Loop Engineering の位置づけ

Loop Engineering とは、人間がエージェントに一手ずつプロンプトを打つのをやめ、**エージェントをプロンプトし・検証し・記憶させ・再実行する「システム（=ループ）そのもの」を設計する**実践だ。`prompt engineering → context engineering → loop engineering` という 3 層スタックの最上位（制御層）に位置する。loop-agent はこの制御層を最小の core として切り出したもので、シーム設計こそが Loop Engineering の実体になる。

## 5 つのシーム（policy 注入口）

ループが「持つ」のはオーケストレーション本体だけ。policy は全部この 5 つのシームに注入する。

| シーム | 型 | あなたが決めること（=注入する policy） |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | 次に何をやるか（候補選定・triage・キュー戦略・公平 scheduling） |
| `act` | `Callable[[ctx], ActOutcome]` | どう実行するか（`ClaudeCodeAct` / `CodexAct` / 自作 adapter・モデル選択・subprocess かローカル fn か） |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | 何を「成功」とするか（pytest / AST / regex — **ground truth で判定**） |
| `conditions` | `list[StopCondition]`（`AnyOf` で OR 合成） | いつ止めるか（回数 / 予算 / 時間 / 目標 / 進捗停滞） |
| `gate` | `ActionGate`（`HumanGate` 等、`on=` で対象選定） | 何に人間承認を要求するか（commit / push / 任意の不可逆操作） |

擬似コードにすると、loop-agent が持つのはこの 4 行だけだ。

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # 何を      (gather)
    outcome = act(ctx)         # どう実行  (act)
    v = verify(outcome)        # 何が成功  (verify)
    state.update(v)
```

この 5 つのシームを書けば、それがあなたの domain の loop になる。型・契約の正本は `seams.md`。

## 鉄則 3 つ（設計時に外さない）

- **verify は ground truth（機械判定）で書く**。何でも差せるのがシームの本質だが、成功判定を LLM-as-judge に委ねるとループは「成功したフリ」に収束しやすい。pytest の exit-code / AST 比較 / 文字列スキャンなど、機械的に判定できるものを使う。
- **機械的上限は必ず置く**。`MaxIterations` / `TokenBudget` / `Timeout` を `AnyOf` で OR 合成し、最低 1 つは載せる。無いと `ConfigError` になるし、ゴール未達でも上限で必ず止まるという暴走防止が崩れる。
- **不可逆操作は gate またはループ外**に置く。`HumanGate` は `gather` が返す離散 action だけを審査し、`act` subprocess 内部の `git commit` は見えない。だから commit / push / deploy は「ループの離散 action にして `on` で拾う」か「ループ外の人間ステップに隔離する」のどちらか。

加えて、停止条件には機械的上限のほかに**意味的 stop** を載せられる。`GoalMet`（検証可能ゴールが満たされたら成功停止）と `NoProgress`（同じアクションが反復され進捗が出ないときに打ち切り停止）を機械的上限と同じ `AnyOf` に並べると、二重の終了条件で安全に締まる。成否はチャネルを問わず `result.succeeded` で判定する。

## act は差し替え自由

`act` シームには `ClaudeCodeAct` / `CodexAct` / 自作 adapter（`ActHook` Protocol）が first-class な adapter として既に揃っている。複数の LLM プロバイダーが最初から揃い、`ActHook` に適合する callable なら何でも同じ `act` シームに載る。`ActOutcome` を返す callable でありさえすれば、subprocess（`claude --print` / `codex exec` 等）でも in-process 関数でも構わない — そこは呼び出し側の自由だ。act を外部 CLI に出すときは adapter の 4 か条契約（例外でループを殺さない / token を予算に積む / auth は CLI に委譲 / stdin を塞ぐ）が効いてくる。

## coding-agent driven（動線 E）

loop-agent の第一の使い手は人間でなく coding agent だ。動線はこうなる。

```
prose intent（人間の自然言語）
  → coding agent が gather/act/verify/conditions/gate を書く
  → run_loop 起動
  → 結果を観察して policy を書き直す
  → loop core（薄い・不変）
```

自然言語 intent で駆動できるので、コードを書かない user にも届く。**この skill 自体がその動線の公式支援**であり、あなた（coding agent）が user の domain を 5 シームに synthesize するための reference bundle である。recipe を丸写しするためのものではない — examples は inspiration として読み、原理を借りてコードは user の domain に書き直す。

## どの reference をいつ読むか

核を掴んだら、user の domain に必要なシームから設計し、深掘りは on-demand で次を読む。

- シームの型・契約・二重終了条件・ground-truth 鉄則 → `seams.md`
- act を外部 CLI subprocess にする / 自作 adapter / 4 か条 / token 二重計上の罠 → `writing-an-adapter.md`
- gate の射程 / 不可逆操作の隔離 / `allowed_tools` 規律 / 暴走防止 → `safety.md`
- 中断 → 再開 / state.db SoT → `persistence-and-resume.md`
- Reflexion を足すべきか（systematic vs stochastic） → `reflexion-when-to-use.md`
- 非同期シーム / `async_run_loop` / sync-async 境界 → `async.md`
- multi-item の公平 scheduling / wake 配送 / work-discovery → `transport.md`
- 例外捕捉（`LoopError` / `ConfigError` / `StateError`） → `errors.md`
- 発想例（写経しない） → `examples/translation.md`, `examples/flaky-test.md`, `examples/refactor.md`

bundle 内 reference は bare filename で参照できる。bundle に含まれない quickstart 等は GitHub の正本（例: <https://github.com/happy-ryo/loop-agent/blob/main/docs/quickstart.md>）を辿る。まず `seams.md` でシームの契約を固め、act を subprocess 化するなら `writing-an-adapter.md`、不可逆操作を扱うなら `safety.md` へ進むのが定石だ。
