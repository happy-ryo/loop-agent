# wake 配送 transport と work-discovery

ループの **完了 / 次反復 / 判断要求** の wake を別ループや窓口へ届ける配送層（transport）と、
完了したループが「次に何を反復するか」を決める入力選定層（work-discovery）の解説。
どちらも stdlib のみ・依存ゼロで実装されている。

## wake 配送 transport（push 一次 / pull fallback / at-most-once）

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

## backend 拡張点（WakeQueue / PushBackend Protocol）

同梱の backend は **stdlib のみ**の最小実装に絞ってある:

- **`InMemoryWakeQueue`** — RLock でスレッド安全な in-process の `WakeQueue` 実装。
- **`NullPushBackend`** — 「常に push 失敗（= backend 不通）」を表す `PushBackend` 実装。pull
  fallback の挙動を素のまま使いたいときの既定。
- **`CallablePushBackend(fn)`** — 任意の `push(wake) -> bool` 関数を `PushBackend` に持ち上げる
  薄いアダプタ。

これらを超える backend は **Protocol を実装して注入する**のが拡張点である。`WakeQueue` /
`PushBackend` の Protocol に適合すれば、たとえば次のようなものを利用者側で実装して差し込める:

- **SQLite-backed な永続 `WakeQueue`** — プロセス再起動をまたいで wake を残す永続キュー。
- **Redis ベースの `PushBackend`** — 即応 push の accelerator を分散環境で動かす。
- **broker / renga CLI bridge** — 外部の窓口・broker へ wake をブリッジする `PushBackend`。

これらは Protocol への適合のみで成立する利用者実装であり、リポジトリに同梱されるものではない。
配送のセマンティクス（at-most-once / at-least-once の倒し方、claim-then-confirm の fencing、
wake id de-dup）は Protocol 契約として固定されているので、backend を差し替えても受信側の
idempotent handler 前提は変わらない。

## work-discovery（次反復対象の入力選定・propose-only / 人間ゲート維持）

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

## multi-item を 1 本のループで公平に回す `WorkListGather`

**`WorkListGather`**（`loop_agent.discovery.work_list`, Issue #56）: triage が「何を どの順で
回すか」を決めるのに対し、`WorkListGather` は「採択済みの複数 item を **1 本のループで どう公平に
回すか**」を担う `gather` フック。素朴な「先頭未完を返す gather」は 1 件が `MaxIterations` を独占して
他を starve させるが、`WorkListGather` は公平 scheduling（`round_robin` / `fewest_attempts` /
`fifo` / `priority` / custom）+ per-item 上限 + per-item の done 判定で starve を防ぐ。attempts /
done / exhausted は毎回 `state.history` から導出する（**resume 安全** = in-process カウンタを持たない）。

```python
from loop_agent import WorkListGather, WorkListDrained, run_loop, MaxIterations

gather = WorkListGather(
    ["a.py", "b.py", "c.py"], strategy="fewest_attempts",
    max_attempts_per_item=3,                                  # 1 件 3 回で打ち止め（exhausted）
    done_when=lambda item, rec: rec.observation["passed"],    # この item は終わったか
)
result = run_loop(act=act, verify=verify, gather=gather,
                  conditions=[WorkListDrained(gather), MaxIterations(50)])  # 全件 done/exhausted で停止
gather.report(result.state)   # WorkListProgress(done=…, exhausted=…, remaining=…, attempts=…)

# triage に優先度・順序計算を委譲（依存が解けた ready 候補だけを取り込む）
gather = WorkListGather.from_triage([Candidate(id="hi", priority=9), Candidate(id="lo")])
```

詳細は [recipes/multi-item-work-list.md](./recipes/multi-item-work-list.md)。

## 関連

- [../README.md](../README.md) — プロジェクト入口と動線サマリ
- [persistence-and-resume.md](./persistence-and-resume.md) — state.db / resume の永続化層
- [safety.md](./safety.md) — 人間ゲート（HumanGate）の射程と安全テンプレ
- [recipes/multi-item-work-list.md](./recipes/multi-item-work-list.md) — multi-item ループの実践レシピ
