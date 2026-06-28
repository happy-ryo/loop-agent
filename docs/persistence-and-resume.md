# 永続化と再開 (progress file / state.db SoT / resume)

LoopAgent はループ状態を外部に永続化し、中断したループを状態欠落なく途中から再開できる。最小の進捗ファイル (JSONL) から、MVP の Single Source of Truth である `state.db` (SQLite)、そして resume (#14) までを段階的に解説する。

## 最小状態（進捗ファイル）

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

## ループ状態の SoT（state.db）

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

なお、**複数プロセスから同一 run を同時に再開する**ケース（in-progress lease による排他、#21）は、
HumanGate の pause/resume と同じ安全境界の話として [safety.md](./safety.md) で扱う。

## 関連

- [README](../README.md) — 全体像と動線
- [transport.md](./transport.md) — SQLite / Redis backend と state.db の格納先
- [observability.md](./observability.md) — loop_begin / loop_step / loop_end イベントと OTel span
- [safety.md](./safety.md) — HumanGate と複数プロセス同時 resume（in-progress lease, #21）
