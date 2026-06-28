# Recipe: N 件を 1 本のループで公平に回す（WorkListGather）

flaky test 安定化・一括翻訳・横断リファクタ — どれも「**N 件の独立した item** を 1 本のループで順に片付ける」形です。`WorkListGather`（`loop_agent.discovery.work_list`, Issue #56）は、その `gather` を正規化した再利用部品です。

## なぜ素朴な gather だと壊れるか

`gather` は「次に何をやるか」を返すだけのフック（`Callable[[state], ctx]`）。N 件を回すとき、いちばん素直な実装はこうなります:

```python
def gather(state):
    return next(f for f in files if f not in done)   # 先頭の未完を返す
```

これは **1 件が verify を連続失敗すると詰みます**。先頭の `a.py` がどうしても直らないと、`gather` は毎反復 `a.py` を返し続け、`MaxIterations` を `a.py` だけで食い潰す。`b.py` / `c.py` は一度も触られずに（= starve して）ループが終わります。Self-translation PoC（#37）で実際に踏んだ罠です。

## WorkListGather が提供するもの

| 機能 | 何を解決するか |
|---|---|
| **公平 scheduling**（`round_robin` / `fewest_attempts` / `fifo` / `priority` / custom） | 1 件が独占しないよう順番を回す |
| **per-item 上限**（`max_attempts_per_item`） | 直らない item を規定回数で打ち止め（*exhausted*）し、残りの予算を他へ |
| **done 判定フック**（`done_when`） | ループ全体の `verify` とは独立に「*この item* は終わったか」を判定 |
| **attempt counter / 進捗 API**（`attempts` / `report` / `remaining`） | 試行回数・完了・残りを `state` から読む |
| **triage 接続**（`from_triage`） | 何を どの順で回すかの優先度計算を既存の `triage` に委譲 |

## prose intent（coding agent にそのまま渡す）

> `src/` 配下の指定 3 ファイルの docstring を英訳して。1 ファイルでも翻訳に詰まったら、そのファイルは 3 回試して諦め、残りのファイルは必ず触ること（1 ファイルの失敗で他を巻き込まない）。各ファイルは「対象言語が 0 になり、当該テストが pass」を完了条件にして。

## 組み上がる harness

```python
from loop_agent import (
    WorkListGather, WorkListDrained, run_loop, ActOutcome, VerifyOutcome, MaxIterations,
)

FILES = ["src/a.py", "src/b.py", "src/c.py"]

def act(item):                         # item は WorkItem（既定の build_ctx）
    obs = translate_and_test(item.id)  # 1 ファイル翻訳 -> テスト実行
    return ActOutcome(observation={"file": item.id, "passed": obs.passed}, tokens=obs.tokens)

def verify(outcome):                   # ループ全体のゴール（任意。drained で止めるなら未達固定でよい）
    return VerifyOutcome(goal_met=False)

gather = WorkListGather(
    FILES,
    strategy="fewest_attempts",        # 試行回数最小から（= 公平な round-robin）
    max_attempts_per_item=3,           # 1 ファイル 3 回で打ち止め
    done_when=lambda item, rec: rec.observation["passed"],   # この item は終わったか
)

result = run_loop(
    act=act, verify=verify, gather=gather,
    conditions=[WorkListDrained(gather), MaxIterations(50)],
)

report = gather.report(result.state)
print("done:", report.done, "exhausted:", report.exhausted)   # exhausted = 諦めた件
```

## 要点

- **停止は必ず `WorkListDrained` で。** 全 item が done か exhausted になると `gather` は返す item が無く `DRAINED` を返します。ループを止めるのは `gather` ではなく停止条件 — 停止条件は各反復の *先頭*（`gather` の前）で評価されるので、`WorkListDrained` を `conditions` に入れておけば drained になった瞬間に `gather` が呼ばれる前に止まり、`DRAINED` が `act` に渡ることはありません。`MaxIterations` は保険として併記します。
- **`done_when` は `verify` と別物。** `verify` は *ループ全体*のゴール、`done_when` は *item ごと*の完了。multi-item では「この 1 ファイルが終わったか」を `done_when(item, record)` で判定し、`verify` は未達固定（`goal_met=False`）にして停止を `WorkListDrained` に委ねるのが素直です。
- **done シグナルは `observation` に焼く。** `done_when` が受け取るのは `StepRecord` だけ。`act` の `observation` に `{"passed": bool}` のような **JSON ネイティブな完了フラグ**を載せ、`done_when` はそれを読みます。resume（別プロセス再開）では `observation` が JSON 往復するので、`tuple`/`set` 等のドリフトしうる型でなく素の bool / str を使うこと（loop core の resume 注記と同じ約束）。
- **戦略の選び方:**
  - `fewest_attempts`（既定）— 試行回数が最も少ない item から。失敗が多い item に引きずられず、全体を均す。迷ったらこれ。
  - `round_robin` — 並び順で厳密に巡回。各 item に等しく順番を与えたいとき。
  - `priority` — `WorkItem(priority=...)` 降順で **厳密に** 高優先度を先に片付ける（同優先度内のみ公平）。重要な item を先に終わらせたいとき。
  - `fifo` — 先頭の未完を返す素朴版。`max_attempts_per_item` と併用すれば starve は緩和されるが、公平性は他に劣る。
  - custom callable — `ScheduleContext`（`selectable` / `attempts` / `last_selected` …）を受け取り 1 件返す。独自の優先ロジック用。
- **ModelLadder と合成。** `build_ctx(item, attempt, state)` の `attempt`（この item の既試行回数）を使えば、試行回数でモデルを上げる act と組み合わせられます: `build_ctx=lambda item, attempt, st: {"item": item, "attempt": attempt}` → `act` 側で `attempt` を見て haiku → sonnet → opus と昇格。
- **resume 安全（ただし同一 gatherer に限る）。** `WorkListGather` は in-process カウンタを持たず、毎回 `state.history` をリプレイして attempts / done / exhausted を導出します。中断した run を `initial_state` で再開しても、同じ `state` から同じスケジュールを再現します。**前提**: 帰属は現在の `items` / `strategy` / `max_attempts_per_item` を使ったリプレイで決まり、`StepRecord` は dispatch した item を構造的に持ちません。よって resume は「中断した *同一* gatherer を同じ `state` で再開」に限ります。設定の違う gatherer に過去の history を食わせると、step を別 item に黙って誤帰属します（crash しません）。
- **人間ゲートと合成するなら `count_attempt`。** `run_loop(gate=...)` の gate が `GATE_SKIP`（reject/respond）を返すと、`act` を呼ばずに `StepRecord` だけが history に積まれます。既定ではこの非実行行も 1 試行として数える（人間が N 回拒否したら諦める、という妥当な既定）ので、`max_attempts_per_item` と併用すると「一度も走っていない item」が拒否回数で *exhausted* になりえます。実行された試行だけを数えたいなら `count_attempt=lambda rec: ...`（skip 行に印を付けて見分ける）を渡します。gate を使わない標準ループでは history と dispatch が 1:1 なので不要です。
- **依存があるなら `from_triage`。** item 間に依存（「`b` は `a` の後」）があるなら、`Candidate` で依存を書いて `WorkListGather.from_triage(candidates, done=...)` を使うと、**ready（依存充足）な候補だけ**を triage のランキング順で取り込みます。依存が解けたら、その時点の `done` で呼び直して新しい gatherer を作ります — このとき items の構成が変わるので、**過去の history は引き継がず新しい `LoopState` で開始**してください（triage が done 済みを除外し、新規 ready は試行 0 から始まるのが正しい挙動）。
