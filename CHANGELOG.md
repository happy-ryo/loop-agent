# Changelog

本プロジェクトの注目すべき変更はすべてこのファイルに記録する。

書式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う
（方針の詳細は [`docs/releasing.md`](./docs/releasing.md) を参照）。

## [Unreleased]

### Added

- **act/verify の per-call timeout / kill（`TimeoutPolicy`）**（Issue #42）: 1 回の
  `act` / `verify` 呼び出しに制限時間を設ける機構。`run_loop` / `async_run_loop` の
  `timeout=` 引数に `TimeoutPolicy`（`act` / `verify` / `default` の秒数 + `on_timeout`
  モード）、秒数（両シームに graceful 適用の短縮形）、または `None`（既定・追加コスト
  ゼロ）を渡す。全実装は async-first core の `_drive_loop` に入っているので **sync /
  async 両 API へ自動適用**（#40 の二重実装回避の回収）。
  - **モード**: `graceful`（既定）は当該シームを諦め、`goal_met=False` の合成 step
    （observation マーカー `ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`）を
    記録して **次 iteration** へ進む。`MaxIterations` / `Timeout` stop / マーカーへの
    `NoProgress` が timeout の連続を収束させる。`kill` は当該シームを cancel し
    `SeamTimeout` を **ループ外へ送出**する。
  - **実機構とプラットフォーム差**: async シームは asyncio の task cancel
    （`asyncio.wait` + `task.cancel()`）で実際に cancel（移植性あり。締切時に task が
    pending かで判定するので seam 自身の `asyncio.TimeoutError` とも混同しない）。
    sync シームは POSIX main thread の `SIGALRM`（`signal.setitimer`）で
    実際に中断。`SIGALRM` 不在（Windows / 非 main thread）では sync シームを強制中断
    できないため、`graceful` は呼び出し **完了後** の超過検出（best-effort、hung call は
    縛れない）、`kill` は呼び出し前に `UnsupportedTimeoutKill` を送出（縛れない hard kill
    を黙ってハングさせない）。per-call の締切は実 wall-clock 基準（`time_fn` は stop 条件
    用クロックのみに作用。post-hoc fallback だけ `time_fn` で計測）。
  - **既知制限**: async の cancel は協調的（締切時の task pending で判定するため
    `CancelledError` 握り潰し seam でも kill は効き、cleanup を待たず即報告するのでループは
    ハングしない＝握り潰して完了しない seam は orphan task としてリークするのみ。seam 自身の
    `asyncio.TimeoutError` は別物として伝播）。`SIGALRM` は再入不可（呼び出し終了時に
    組み込み先の `ITIMER_REAL` は復元）。
    per-call 締切は同期区間＋await 区間で単一 budget（remaining を繰り越し）。詳細は
    [`docs/recipes/timeout-and-kill.md`](./docs/recipes/timeout-and-kill.md)。
  - 既存の whole-run `Timeout` *stop 条件*（iteration 境界で累積 wall-clock を上限化、
    進行中 step は中断しない）とは別物。新規 export: `TimeoutPolicy` / `SeamTimeout` /
    `UnsupportedTimeoutKill` / `TIMEOUT_GRACEFUL` / `TIMEOUT_KILL` /
    `ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`。
- **multi-item 公平 scheduling `WorkListGather`**（Issue #56）: N 件を 1 本のループで
  公平に回す `gather` フック。公平 scheduling 戦略（`round_robin` / `fewest_attempts` /
  `fifo` / `priority` / custom callable）+ per-item 上限（`max_attempts_per_item` で
  1 件が `MaxIterations` を独占して他を starve させるのを防ぐ）+ per-item の done 判定
  フック（`done_when`、ループ全体の `verify` と独立）。attempts / done / exhausted は
  毎回 `state.history` から導出するので **resume 安全**（in-process カウンタを持たない）。
  全件 done/exhausted で止める `WorkListDrained` stop 条件と、優先度・順序計算を `triage`
  に委譲する `WorkListGather.from_triage(...)` を同梱。既定 context は JSON ネイティブ dict
  なので永続人間ゲート（`run_gated_loop`）と合成しても state.db に保存できる。人間ゲートを
  挟んで offer と record がずれる構成（`GATE_SKIP` / `edit`）向けに、record の実 item を返す
  `item_of` フックで正しく帰属できる。#37 Self-translation PoC で手書きした round-robin
  pattern の正規化（`loop_agent.discovery.work_list`）。
  - **内部変更**: `loop_agent.discovery` を単一モジュールから package 化（入力選定実装は
    `_triage`・scheduling は `work_list`）。公開 import（`from loop_agent import ...` /
    `from loop_agent.discovery import ...`）は不変。
- **async/await 対応（`async_run_loop`）**: ループ制御フローの単一実装を `async def`
  化し、新たな非同期エントリポイント `async_run_loop` として公開（Issue #40）。
  同期 API `run_loop` は完全に維持される（同じ引数・同じ `LoopResult`・同じ stop
  条件評価タイミング・同じ resume 意味論）。内部は共有コルーチンを **呼び出し側の
  コンテキストでそのまま駆動**（全同期フックなら一度も await されないため event loop
  を生成しない）。これにより `contextvars` 伝播も例外型もオーバーヘッドも従来どおり。
  `gather` / `act` / `verify` / 各 `conditions` の `check` /
  `gate.review` / `on_step` の各シームは **同期 callable のまま受けつつ、非同期
  (acallable) も受けられる**（`loop_agent._async.maybe_await` で結果を await。
  同期フックは追加コストなし）。混在（async gather + sync act + async verify 等）も
  可能。`GoalMet` の verifier、`AnyOf.afirst_triggered` も非同期 `check` を受ける。
  非同期シーム（任意のフック・`conditions` の `check`・`gate.review`・`on_step`・
  `on_complete`）を `run_loop`（同期 API）へ渡した場合は、駆動中の strict-sync 判定に
  より awaitable を検出した時点で `AsyncSeamInSyncLoop`（`RuntimeError` サブクラス）を
  **一貫して**送出する（そのシームが実際に suspend するか否かに依存しない）。非同期
  シームには `await async_run_loop(...)` を使う。

## [0.1.0] - 2026-06-28

loop-agent の最初の機能リリース。`gather -> act -> verify -> repeat` の最小ループ
コアから、外側 Reflexion ループ + RQGM epoch 安全核までを含む（設計の正本は
[`report.md`](./report.md)）。

### Added

- **ループコア（PoC）**: 単一エージェント・単一プロセスの
  `gather -> act -> verify -> repeat` ドライバ。`act` / `verify` は注入可能な
  フック。上限到達は例外ではなく理由付きの `LoopResult` で返る（`run_loop`）。
- **合成可能な stop 条件**: `MaxIterations` / `TokenBudget` / `Timeout` を `AnyOf`
  で OR 評価。発火条件と人間可読の理由を保持する。
- **暴走防止の保証**: ゴール未達・無進捗・反復アクションでも必ず上限で停止する
  ことを sandbox test で実証（`tests/test_runaway_guard.py`）。
- **二重終了条件（意味的 stop）**: 機械的上限に加え、`GoalMet`（検証可能ゴール
  達成 = 成功終了）と `NoProgress`（無進捗・反復検出 = 打ち切り）を同じ `AnyOf`
  合成へ。
- **最小状態（進捗ファイル）**: 各反復を JSON Lines で外部ファイルへ追記し、
  プロセスをまたいで進捗が残る（`ProgressLog` / `read_progress`）。
- **観測（構造化イベント + OTel span）**: `loop_begin` / `loop_step` / `loop_end`
  を sink へ流し、終了理由・メトリクスを事後解析できる（`run_observed_loop` /
  `JsonlEventSink`）。OTel GenAI span は **optional 依存** で、未導入環境では
  no-op へ degrade する（`LoopSpan` / `[otel]` extra）。
- **ループ状態の SoT（state.db）**: loop 用最小 SQLite スキーマ
  （`run` / `step` / `event` / `stop_reason`）へ各 step を transaction で atomic
  永続化。`DBProgressLog` は `ProgressLog` の drop-in（`LoopStore` / `connect`）。
- **中断 -> 再開（resume）**: 永続化済み step から `LoopState` を復元し、
  `run_loop(initial_state=...)` で状態欠落なく継続。通し実行との一致を回帰テスト
  で実証（`tests/test_resume.py`）。
- **限定人間ゲート**: 不可逆操作のみ approve/edit/reject/respond で interrupt。
  state 永続化で pause/resume し、不可逆 action は exactly-once（`HumanGate` /
  `run_gated_loop` / `Decision`）。
- **複数プロセス同時 resume の協調（in-progress リース）**: 同一 `run_id` を複数
  プロセスで同時 resume しても不可逆 action は exactly-once + 順序整合。勝者
  クラッシュ時はリース失効で別プロセスが取り直す（`tests/test_concurrent_resume.py`）。
- **wake 配送 transport**: 完了 / 次反復 / 判断要求 wake を push 一次 / pull
  fallback で配送（at-most-once、claim-then-confirm）。backend 非依存の
  `PushBackend` プロトコルに対し in-memory / callable backend を同梱
  （`Transport` / `WakeQueue` / `LoopWaker`）。
- **work-discovery（次反復入力選定）**: 決定的 triage の計算層 + propose-only
  人間ゲートの配達層に分離。採択されるまで次反復は起きない（`WorkDiscovery` /
  `discover_next` / `triage`）。
- **外側 Reflexion ループ + RQGM epoch 安全核**: 内側 ReAct を 1 episode として
  包み、失敗からの言語的指針を episodic memory へ取り込み次 context へ配線する
  self-improving。epoch 境界でのみ評価器を昇格させる安全核
  （`run_reflexion` / `EpisodicMemory` / `Evaluator` / `admit_evaluator`）。
- **外側 Reflexion の永続化 / resume**: epoch・lesson テーブル + 評価器 version
  registry で、episode 数・epoch・採用 lesson・評価器 version・best score を跨
  プロセスで継続（`ReflexionStore` / `DBReflexionLog`）。
- **外側 Reflexion 観測**: episode / epoch / lesson 採否 / 評価器昇格 / 収束を
  イベント + OTel span として観測（`run_observed_reflexion` / `ReflexionObserver`
  / `ReflexionSpan`）。
- **外側ループの収束条件**: `MaxEpisodes` / `RubricThreshold` / `ScorePlateau` /
  `ReflectionBudget` / `EvaluatorUpdateBudget`。
- **examples**: 検証駆動デモ・観測デモ・外側 Reflexion デモ
  （`examples/verify_driven_demo.py` / `observed_demo.py` / `reflexion_demo.py`）。
- **調査・設計レポート**: Loop Engineering の徹底調査と LoopAgent 設計（案 C 推奨）、
  claude-org-ja 資産棚卸し、段階ロードマップ（`report.md` / `report.html`）。
- **リリース運用**: PyPI への OIDC Trusted Publishing ワークフロー
  （`.github/workflows/release.yml`、`v*` タグ push で自動 publish）。

### Packaging

- `description` / `keywords` / `classifiers`（Development Status :: 4 - Beta）/
  `project.urls` を整備。
- optional extras を実機能に対応づけて整理: `[otel]`（OTel span 連携）/
  `[test]`（テスト実行）/ `[dev]`（test + build/twine）。

## [0.0.1] - 2026-06-28

### Added

- Placeholder release（PyPI 上の `loop-agent` 名の予約）。OIDC Trusted Publishing
  経由で公開: https://pypi.org/project/loop-agent/0.0.1/

[Unreleased]: https://github.com/happy-ryo/loop-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/happy-ryo/loop-agent/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/happy-ryo/loop-agent/releases/tag/v0.0.1
