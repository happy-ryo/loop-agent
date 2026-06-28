> This file is a load-on-demand bundled copy of `docs/safety.md`. The canonical source is `docs/safety.md` in the repository.

# 安全装置 (暴走防止 / 限定人間ゲート)

LoopAgent の二段構えの安全機構を解説する。下段は「合成 stop 条件で必ず止まる」暴走防止 (runaway guard)、上段は「不可逆操作だけを人間が承認する」限定人間ゲート (HumanGate)。

## 暴走防止 (runaway guard)

report.md §4.4 / §5 Phase 1 に忠実な最小実装は、`gather → act → verify → repeat` を回しながら **合成可能なハード上限**（`MaxIterations` / `TokenBudget` / `Timeout`）を **OR 評価**する。上限到達は **例外ではなく理由付きの制御出力**（`LoopResult`）で返り、どの条件がなぜ発火したかを保持する。

安全に関わる保証は二段ある。

- **機械的上限（必ず止まる）**: `MaxIterations` / `TokenBudget` / `Timeout` を `AnyOf` 合成で OR 評価する。ゴール未達・無進捗・反復アクションでも、上限で必ず停止することを sandbox test で証明している（`tests/test_runaway_guard.py`）。AutoGPT 的な暴走・コスト爆発を「構造」で防ぐのが狙い。
- **二重終了条件（意味的 stop）**: 機械的上限に加え、`GoalMet`（検証可能ゴールの達成＝成功終了）と `NoProgress`（無進捗・反復アクションの検出＝打ち切り）を同じ `AnyOf` 合成に載せる。

report.md R3（無限ループ防止）の要請に対応する。stop 条件を 1 つも渡さないと `ConfigError` で拒否される（必ず発火する条件が無い構成を起動時に弾く）。条件 API の完全な一覧と合成セマンティクスは [api-reference.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/api-reference.md) と [seams.md](seams.md) を参照。

## 限定人間ゲート（不可逆操作のみ approve/edit/reject/respond）

MVP（report.md §4.5 / R6 / 原則8 / §5 Phase 2 成功条件 c）では、人間ゲートを **「不可逆・影響範囲大」のアクションに限定**する（全 step ではない）。LangGraph の `interrupt()` と同じ 4 種の決定 — **approve / edit / reject / respond** — を持ち、決定を state.db に**永続化**して **pause → resume をまたいで保持**する。claude-org の `org-escalation` + `pending_decisions`（state machine）を role 読み替えで reuse している（「secretary が worker の判断要求を register し user 応答で resolve」→「loop が不可逆 action を register し human が resolve」）。

`HumanGate` は `gather` と `act` の**間**で発火する（= 行動が提案された後・副作用が出る前）。`on(action)` が `True` の action だけを審査し、reversible な action は素通りする。決定が未解決なら `run_loop` は `status="paused"` で復帰し、人間が決定を記録した後に**同じ `run_id`** で再実行すると永続化済みの決定を適用して続行する（同じ action を二度問わない）。

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

## 安全テンプレ

self-improvement 系で推奨する最小の安全テンプレは、**act は編集のみ・commit はループ外**に隔離する形である。この原則はどの act adapter（`ClaudeCodeAct` / `CodexAct` / 自作 adapter (ActHook Protocol)）でも共通だが、ツール権限を絞る具体的な knob は adapter ごとに異なる — `ClaudeCodeAct` は `allowed_tools` を編集系に絞り、`CodexAct` は `sandbox`（例 `"read-only"` / `"workspace-write"`）や `allowed_args` で commit/push を断つ。いずれにせよ不可逆操作はループの外の人間ステップに置く。

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

> **HumanGate の射程に注意（重要）**: `HumanGate` が審査するのは `gather` が返す**ループの離散 action**であって、`act` の subprocess（例: `claude --print`）が内部で実行する `git commit` 等は**見えない**（ゲートは `gather` と `act` の間で発火する）。したがって不可逆操作を本当にゲートしたいなら、(1) act の subprocess に commit / push をさせず（`allowed_tools` を編集系に絞る）、commit / push は**ループ外の人間ステップ**にする、または (2) commit を**ループの離散 action として `gather` に提案させ**、`on` で拾って `act` に実行させる。上の[限定人間ゲート節](#限定人間ゲート不可逆操作のみ-approveeditrejectrespond)が (2) の正準例（`on=lambda a: a == "deploy"`）。

## 関連

- [README](https://github.com/happy-ryo/loop-agent/blob/main/README.md) — 全体像・positioning・シーム概観
- [seams.md](seams.md) — シーム詳細仕様と型（conditions / gate / act の境界）
- [api-reference.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/api-reference.md) — stop 条件と HumanGate の完全な API
- [persistence-and-resume.md](persistence-and-resume.md) — state.db / resume の永続化契約
