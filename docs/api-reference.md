# API リファレンス

LoopAgent が公開する API の索引ページ。0.1.0 Beta のスコープ、インストール手順、エクスポートされる全要素の一覧表、テストスイートのカバレッジをまとめる。

## 0.1.0 Beta のスコープ

report.md §4.4 から始まったループコアを、組み込み用 Beta ランタイムとして拡張した実装。**単一エージェント・単一プロセス**で `gather → act → verify → repeat` を回し、**合成可能なハード上限**（`MaxIterations` / `TokenBudget` / `Timeout`）を OR 評価する。上限到達は**例外ではなく理由付きの制御出力**（`LoopResult`）で返る。

スコープ（欲張らない = *simpler loops win*）:

- ✅ ループドライバ + 機械的な合成 stop 条件（発火した条件と理由を保持）
- ✅ `act` / `verify` は**注入可能なフック**（in-memory 関数 / subprocess / Claude Code / Codex / 自作 adapter を同じシームに載せる）
- ✅ **暴走防止の保証**: ゴール未達・無進捗・反復アクションでも、上限で必ず停止することを sandbox test で証明（`tests/test_runaway_guard.py`）
- ✅ **二重終了条件（意味的 stop）**: 機械的上限に加え、`GoalMet`（検証可能ゴールの達成＝成功終了）と `NoProgress`（無進捗・反復アクションの検出＝打ち切り）を同じ `AnyOf` 合成に載せる
- ✅ **最小状態（進捗ファイル）**: 各反復の記録を JSON Lines で外部ファイルに追記し、プロセスをまたいで進捗が残る（`ProgressLog` / state.db SoT の最小の前身）
- ✅ **観測（構造化イベント + OTel span）**: `loop_begin/step/end` を sink へ流し、終了理由/メトリクスを事後解析できる（`run_observed_loop` / OTel GenAI span）
- ✅ **ループ状態の SoT（state.db）**: loop 用最小 SQLite スキーマ（`run` / `step` / `event` / `stop_reason`）に各 step を **transaction で atomic 永続化**。`DBProgressLog` は `ProgressLog` の drop-in（Issue #11）
- ✅ **中断 → 再開（resume）**: 永続化済み step から `LoopState` を復元し、`run_loop(initial_state=…)` で状態欠落なく途中から継続（iteration・コスト累積・`elapsed`・history を引き継ぐ）。中断して再開した結果が通し実行と一致することを回帰テストで実証（`tests/test_resume.py` / Issue #14）
- ✅ **async/await 対応**: 非同期エントリポイント `async_run_loop`（`await async_run_loop(…)`）。同期 API `run_loop` は完全維持（内部は `asyncio.run` ラッパ）。`gather`/`act`/`verify`/`conditions`/`gate`/`on_step` の各シームは **同期 callable のまま受けつつ非同期（acallable）も受ける**（混在可・同期フックは追加コストなし）。`asyncio.gather` で複数ループを並行実行できる（`tests/test_async_loop.py` / Issue #40）
- ✅ **限定人間ゲート**: 不可逆操作のみ approve/edit/reject/respond で interrupt（state 永続化で pause/resume・不可逆は exactly-once。Issue #15）
- ✅ **複数プロセス同時 resume の協調（in-progress リース）**: 同一 `run_id` を複数プロセスで同時に resume しても、不可逆 action は **exactly-once + 順序整合**（`pending → resolved → executing → executed` 多段化 + リース single-winner）。敗者は `executed` まで pause、勝者クラッシュ時はリース失効で別プロセスが取り直し step も欠落しない。並行プロセス模擬で実証（`tests/test_concurrent_resume.py` / Issue #21）
- ✅ **wake 配送 transport / 次反復入力選定 work-discovery**: 完了/次反復/判断要求 wake を push 一次 / pull fallback で配送（`tests/test_transport.py` / Issue #23）。次反復対象を計算層（決定的 triage）+ 配達層（propose-only 人間ゲート）で選定（`tests/test_discovery.py` / Issue #24）
- ✅ **外側 Reflexion ループ + RQGM epoch 安全核**: 内側 ReAct を 1 episode として包み、失敗からの言語的指針を episodic memory へ取り込み次 context へ配線する self-improving（report.md §5 Phase 3 / Issue #22。下記）
- ⛔ dashboard 化・3x スパイク自動スロットル・サーキットブレーカは**運用 follow-up**（[operations-roadmap.md](./operations-roadmap.md)）

## インストール

```bash
python3 -m pip install -e .        # ループコア本体
python3 -m pip install -e .[dev]   # + pytest（テスト実行用）
```

## API 概要

| 要素 | 役割 |
|---|---|
| `run_loop(*, act, verify, conditions, gather=…, on_step=…, gate=…, time_fn=…, initial_state=…, timeout=…)` | ループドライバ。`LoopResult` を返す。`gate` を渡すと不可逆操作を interrupt、`initial_state` に復元 `LoopState` を渡すと中断地点から**再開**（resume #14）、`timeout` で `act`/`verify` の per-call timeout（#42） |
| `ActOutcome(observation, tokens)` | `act` フックの返り値（行動結果 + 消費トークン） |
| `VerifyOutcome(goal_met, detail)` | `verify` フックの返り値（`goal_met=True` で自然終了） |
| `MaxIterations(n)` / `TokenBudget(b)` / `Timeout(s)` | 機械的ハード上限（合成可能 stop 条件） |
| `TimeoutPolicy(act=…, verify=…, default=…, on_timeout=…)` | `act`/`verify` の **per-call** timeout（#42）。`run_loop`/`async_run_loop` の `timeout=` に渡す（`TimeoutPolicy` か裸の秒数）。`on_timeout="graceful"`（既定）は諦めて合成 step を記録し次反復／`"kill"` は `SeamTimeout` を送出。async シームは asyncio の task cancel、sync シームは POSIX main thread の `SIGALRM` で実中断（不在環境は graceful=post-hoc、kill=`UnsupportedTimeoutKill`）。whole-run の `Timeout` stop 条件とは別物。詳細 [recipes/timeout-and-kill.md](./recipes/timeout-and-kill.md) |
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
| `SpikeDetector(sinks, *, token_window=…, latency_window=…, multiplier=…, repeated_failure=…)` / `detect_spikes(state, …)` / `scan_spikes(steps, …)` / `LOOP_SPIKE` | opt-in の運用 spike 検出。live `on_step` observer または保存済み step scan として token / latency / repeated failure / timeout marker を検出する。**制御は変えない**（止めるかは別 StopCondition / application policy） |
| `AdapterFailureBreaker(repeat)` / `VerifyDetailBreaker(repeat)` / `TimeoutMarkerBreaker(repeat)` / `PerStepTokenCap(limit)` | よく使う circuit breaker の `StopCondition` helper。adapter failure / verify detail / timeout marker / per-step spend を明示 policy として止める |
| `launch_throttle_decision(...)` / `step_throttle(act, delay_seconds, sleep)` | opt-in throttling primitives。launch 判定は純粋関数、step throttling は注入された `sleep` を明示的に呼ぶ wrapper。`run_loop` 既定挙動は変えない |
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
| `WorkListGather(items, *, strategy="fewest_attempts", max_attempts_per_item=None, done_when=…, build_ctx=…)` | multi-item を 1 本のループで公平に回す `gather` フック（Issue #56）。`strategy`=`round_robin`/`fewest_attempts`/`fifo`/`priority`/custom callable。`max_attempts_per_item` で per-item 上限（*exhausted*）。`done_when(item, record)` で per-item の完了判定。`attempts`/`done_items`/`exhausted_items`/`remaining`/`report` で進捗を `state` から導出（resume 安全）。`from_triage(candidates, *, done=, strategy=, …)` で triage に優先度・順序を委譲 |
| `WorkListDrained(gatherer)` | 全 item が done/exhausted になったら止める stop 条件（gather より先に評価され `DRAINED` の漏れを防ぐ）。`WorkItem(id, priority=0, payload=None)` が scheduling 対象の 1 件 |
| `loop_agent.cli:main(argv=None)` | CLI エントリポイント（`[project.scripts]` の `loop-agent`, Issue #31）。`run`/`status`/`resume`/`logs` サブコマンド + 引数なしでクイックヘルプ。プロセス終了コードを返す（成功 0 / 停止 1 / 設定エラー 2） |
| `cli.load_config(path)` / `cli.parse_config(data)` | `task.toml` を検証済み `Config` に読み込む（`[loop]`/`[conditions]`/`[act]`/`[verify]`/`[state]`）。stdlib `tomllib`（3.11+）か 3.10 では `tomli` を使用 |
| `cli.build_conditions(cfg, *, max_iter=…, token_budget=…, timeout=…)` | `Config` から stop 条件を合成（CLI フラグ > TOML 値 > 未指定）。1 つも無ければ `ConfigError`（R3） |
| `cli.build_act(cfg)` / `cli.build_verify(cfg)` | act/verify フックを構築。subprocess（`{prompt}`/`{goal}`/`{iteration}` 置換・exit-code 0 = goal）か Python callable（`module:attr`）の両モード |
| `cli.resolve_callable(spec)` | `module:attr`（または `module.attr`）参照を callable へ解決（Python モード用） |
| `loop-agent summary [--db PATH] [--limit N]` | `state.db` の read-only run 一覧。run id / status / iterations / tokens / elapsed / pending 数 / event 数 / stop reason を表示する（判断ロジックは変更しない） |
| `loop-agent dashboard --output PATH [--db PATH]` | `state.db` から read-only 静的 HTML dashboard を生成する。run list / step timeline / pending decisions / Reflexion summary を表示 |
| `loop-agent spikes [run-id] [--db PATH]` | 保存済み step から token / latency / repeated failure / timeout marker spike を post-hoc scan する |

- `conditions` は stop 条件のリスト（または `AnyOf`）。**宣言順**に OR 評価し、最初に発火したものを `result.stop` として報告する。
- 終了条件は**各反復の先頭（while ガード）で評価**される。`TokenBudget` / `Timeout` は反復境界での判定で、実行中のステップは中断しないため、1 ステップ分だけ上限を超過しうる（消費済みのトークン・時間は取り消せない = "使い切ったら新規ステップを始めない"意味）。
- `gather` を省略すると `LoopState` がそのまま `act` の context になる。`on_step(record, state)` は各反復完了後に呼ばれる最小の観測フック。
- stop 条件を 1 つも渡さないと `ConfigError`（無限ループ防止 = R3）。`ConfigError` は `LoopError` 階層の一員で、後方互換のため `ValueError` も継承する（[errors.md](./errors.md)）。

## テスト

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

## 関連

- [../README.md](../README.md) — プロジェクト全体像と動線
- [seams.md](./seams.md) — 5 つのシーム（gather/act/verify/conditions/gate）の詳細仕様と型
- [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) — act シームへ繋ぐ adapter の書き方（ActHook Protocol）
- [errors.md](./errors.md) — `LoopError` 階層と例外契約
