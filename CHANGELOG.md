# Changelog

All notable changes to this project are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/lang/ja/)
(see [`docs/releasing.md`](./docs/releasing.md) for the detailed policy).

## [Unreleased]

## [1.0.0] - 2026-07-01

### Added

- **Stable API and compatibility contract**: documented the public API boundary,
  deprecation policy, CLI compatibility, and state.db migration policy for the
  first stable release.
- **Version-readiness dogfood loop**: added deterministic loop-agent tasks that
  verify the version-readiness issue plan and the issue-consumption gate.
- **Review-driven release check**: added an optional review-loop recipe and
  repository-maintenance script for post-act review before final verification.

### Changed

- Promoted package metadata from Beta to Production/Stable.
- Aligned README, API reference, operations docs, release docs, and bundled skill
  references with the `1.0.0` stability contract.

## [0.1.0] - 2026-06-29

The first functional release of loop-agent. It spans from the minimal
`gather -> act -> verify -> repeat` loop core through the outer Reflexion loop
and RQGM epoch safety kernel. It also includes an asynchronous API
(`async_run_loop`), per-call timeout / kill for act/verify (`TimeoutPolicy`), a
unified exception hierarchy (`LoopError`), fair multi-item scheduling
(`WorkListGather`), and a bundled reference-bundled skill for coding agents
(`install-skills` CLI). The canonical design document is
[`report.md`](./report.md).

### Added

- **Bundled reference-bundled skill for Claude Code + `install-skills` CLI**
  (Issue #73): bundles a **load-on-demand reference bundle** in the Python
  package (`loop_agent/skills/loop-agent/`) so coding agents (Claude Code /
  Cursor / Codex, etc.) can use loop-agent effectively. It consists of
  `SKILL.md` (triggers plus active instructions on how to design) and
  `references/` (references and idea examples for the five seams, the four
  adapter rules, safety, async, errors, and more). Agents read only the
  references they need on demand and synthesize the five seams for the user's
  domain. This is designed to use the agent's synthesis ability rather than
  serve as a cookbook to copy recipes from.
  - **`loop-agent install-skills`**: a subcommand that idempotently copies the
    bundled skill to `.claude/skills/loop-agent/` (default, project-local),
    `--user` (`~/.claude/skills/`), or `--target <path>` (arbitrary target).
    Because the skill is included in the wheel / sdist, it always matches the
    installed loop-agent version.
  - **Automatic docs -> references sync**:
    `scripts/sync_skill_references.py` (deterministic regeneration, stdlib
    only) derives eight verbatim bundle files from `docs/`. CI
    (`sync-skill-references`) verifies synchronization with `--check`
    (verify-only, no commit-back). `SKILL.md` / `design-philosophy.md` /
    `examples/` are handwritten and excluded.
  - Also fixed the `conditions=MaxIterations(...)` example in `docs/async.md`
    (which raised `ConfigError` because it was not wrapped in a list) by
    wrapping it in a list.
- **Per-call timeout / kill for act/verify (`TimeoutPolicy`)** (Issue #42): a
  mechanism for setting a time limit on each `act` / `verify` call. The
  `timeout=` argument to `run_loop` / `async_run_loop` accepts a
  `TimeoutPolicy` (seconds for `act` / `verify` / `default` plus an
  `on_timeout` mode), a number of seconds (shorthand that applies graceful mode
  to both seams), or `None` (the default, with zero added cost). The full
  implementation lives in the async-first core `_drive_loop`, so it is
  **automatically applied to both sync and async APIs** (closing the duplicate
  implementation concern from #40).
  - **Modes**: `graceful` (default) abandons the current seam, records a
    synthetic step with `goal_met=False` (observation markers
    `ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`), and advances to
    the **next iteration**. `MaxIterations` / `Timeout` stop conditions and
    `NoProgress` on the markers converge repeated timeouts. `kill` cancels the
    current seam and raises `SeamTimeout` **outside the loop**.
  - **Mechanics and platform differences**: async seams are actually cancelled
    with asyncio task cancellation (`asyncio.wait` + `task.cancel()`), which is
    portable. The implementation checks whether the task is pending at the
    deadline, so it does not confuse this with the seam's own
    `asyncio.TimeoutError`. Sync seams are actually interrupted on the POSIX
    main thread with `SIGALRM` (`signal.setitimer`). Where `SIGALRM` is
    unavailable (Windows / non-main thread), sync seams cannot be forcibly
    interrupted. In that case, `graceful` detects overruns **after the call
    completes** (best effort; it cannot constrain a hung call), while `kill`
    raises `UnsupportedTimeoutKill` before the call (so an impossible hard kill
    does not silently hang). Per-call deadlines use real wall-clock time
    (`time_fn` affects only the clock used by stop conditions; only the
    post-hoc fallback is measured with `time_fn`).
  - **Known limitations**: async cancellation is cooperative. Because kill is
    determined by whether the task is pending at the deadline, it still takes
    effect even for seams that swallow `CancelledError`; the loop reports
    immediately without waiting for cleanup, so it does not hang. A seam that
    swallows cancellation and never completes only leaks as an orphan task. The
    seam's own `asyncio.TimeoutError` propagates separately. `SIGALRM` is not
    reentrant (the embedded `ITIMER_REAL` is restored when the call exits).
    Per-call deadlines use a single budget across synchronous and await
    sections, carrying forward the remaining time. See
    [`docs/recipes/timeout-and-kill.md`](./docs/recipes/timeout-and-kill.md)
    for details.
  - This is separate from the existing whole-run `Timeout` *stop condition*,
    which caps cumulative wall-clock time at iteration boundaries and does not
    interrupt an in-flight step. New exports: `TimeoutPolicy` / `SeamTimeout` /
    `UnsupportedTimeoutKill` / `TIMEOUT_GRACEFUL` / `TIMEOUT_KILL` /
    `ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`.
- **Unified exception hierarchy `LoopError`** (Issue #43): organized all
  library-raised exceptions under the single base `LoopError`. Added
  `loop_agent.errors` as the canonical home, introduced `ConfigError`
  (invalid argument values/types and configuration mistakes), `StateError`
  (runtime invariant/lifecycle violations), and `AsyncSeamInSyncLoop` (moved
  from #40), and exported them from the `loop_agent` top level. Validation and
  state-violation sites that previously raised `ValueError` / `TypeError` /
  `RuntimeError` directly now raise the corresponding `LoopError` subtype.
  **Backward compatibility**: each subtype also inherits from the previous
  built-in exception (`ConfigError` from `ValueError`/`TypeError`,
  `StateError` from `ValueError`/`RuntimeError`, and `AsyncSeamInSyncLoop` from
  `RuntimeError`), so existing `except ValueError` and similar handlers
  continue to work. The `KeyError` raised for missing `prompt_template` fields
  remains intentionally outside the hierarchy because it follows `str.format`
  semantics. See [`docs/errors.md`](./docs/errors.md).
  - **Integration of #42 exceptions into the hierarchy (Issue #71)**: moved
    `SeamTimeout` / `UnsupportedTimeoutKill`, introduced by #42
    (per-call timeout/kill) and initially outside the `LoopError` hierarchy,
    into the unified hierarchy. `SeamTimeout` now derives from `StateError`
    (kill triggered = runtime invariant violation; it previously derived from
    bare `Exception`, so this only broadens catch coverage and preserves
    `except SeamTimeout`). `UnsupportedTimeoutKill` now derives from
    `ConfigError` (configuration mismatch between seam and environment) while
    also retaining `RuntimeError` as a base for compatibility with pre-#71
    `except RuntimeError` handlers. The canonical home moved to
    `loop_agent.errors`, with a backward-compatible re-export from
    `loop_agent.loop` (behavior and attributes are fully unchanged).
- **Fair multi-item scheduling `WorkListGather`** (Issue #56): a `gather` hook
  that rotates N items through one loop fairly. It provides fair scheduling
  strategies (`round_robin` / `fewest_attempts` / `fifo` / `priority` / custom
  callable), per-item limits (`max_attempts_per_item`, preventing one item from
  monopolizing `MaxIterations` and starving others), and a per-item completion
  hook (`done_when`, independent of the loop-level `verify`). `attempts` /
  `done` / `exhausted` are derived from `state.history` every time, so it is
  **resume-safe** and keeps no in-process counters. Includes a
  `WorkListDrained` stop condition that stops when all items are done/exhausted,
  and `WorkListGather.from_triage(...)`, which delegates priority and ordering
  calculation to `triage`. The default context is a JSON-native dict, so it can
  be saved in state.db even when composed with a persistent human gate
  (`run_gated_loop`). For configurations where a human gate can make the
  offered item and recorded item diverge (`GATE_SKIP` / `edit`), the `item_of`
  hook returns the actual recorded item so attribution remains correct. This
  formalizes the hand-written round-robin pattern from the #37 Self-translation
  PoC (`loop_agent.discovery.work_list`).
  - **Internal change**: converted `loop_agent.discovery` from a single module
    to a package (input selection implementation in `_triage`, scheduling in
    `work_list`). Public imports (`from loop_agent import ...` /
    `from loop_agent.discovery import ...`) are unchanged.
- **async/await support (`async_run_loop`)**: converted the single loop control
  flow implementation to `async def` and exposed it as the new asynchronous
  entry point `async_run_loop` (Issue #40). The synchronous API `run_loop` is
  fully preserved (same arguments, same `LoopResult`, same stop-condition
  evaluation timing, and same resume semantics). Internally, the shared
  coroutine is driven **directly in the caller's context**; if all hooks are
  synchronous, it is never awaited and no event loop is created. This preserves
  `contextvars` propagation, exception types, and overhead. Each seam -
  `gather` / `act` / `verify` / each `conditions` `check` / `gate.review` /
  `on_step` - still accepts synchronous callables and now also accepts
  asynchronous callables (acallables) (`loop_agent._async.maybe_await` awaits
  results; synchronous hooks add no cost). Mixed usage (for example async
  gather + sync act + async verify) is supported. The `GoalMet` verifier and
  `AnyOf.afirst_triggered` also accept asynchronous `check` functions. If an
  asynchronous seam (any hook, a `conditions` `check`, `gate.review`,
  `on_step`, or `on_complete`) is passed to `run_loop` (the synchronous API),
  strict-sync detection during execution raises `AsyncSeamInSyncLoop`
  (a `RuntimeError` subclass) **consistently** as soon as an awaitable is
  detected, regardless of whether that seam would actually suspend. Use
  `await async_run_loop(...)` for asynchronous seams.
- **Loop core (PoC)**: a single-agent, single-process
  `gather -> act -> verify -> repeat` driver. `act` / `verify` are injectable
  hooks. Reaching a limit returns a `LoopResult` with a reason instead of
  raising an exception (`run_loop`).
- **Composable stop conditions**: `MaxIterations` / `TokenBudget` / `Timeout`
  are OR-evaluated with `AnyOf`. Triggering conditions and human-readable
  reasons are preserved.
- **Runaway-prevention guarantee**: sandbox tests demonstrate that the loop
  always stops at a limit even when the goal is unmet, progress is absent, or
  actions repeat (`tests/test_runaway_guard.py`).
- **Dual termination conditions (semantic stops)**: in addition to mechanical
  limits, `GoalMet` (verifiable goal reached = successful termination) and
  `NoProgress` (no progress / repeated action detected = aborted) are composed
  into the same `AnyOf`.
- **Minimal state (progress file)**: appends each iteration to an external file
  as JSON Lines, preserving progress across processes (`ProgressLog` /
  `read_progress`).
- **Observation (structured events + OTel span)**: emits `loop_begin` /
  `loop_step` / `loop_end` to sinks so termination reasons and metrics can be
  analyzed after the run (`run_observed_loop` / `JsonlEventSink`). OTel GenAI
  spans are an **optional dependency** and degrade to no-op when not installed
  (`LoopSpan` / `[otel]` extra).
- **Loop state SoT (state.db)**: atomically persists each step in a transaction
  to the minimal SQLite schema for loops (`run` / `step` / `event` /
  `stop_reason`). `DBProgressLog` is a drop-in replacement for `ProgressLog`
  (`LoopStore` / `connect`).
- **Interrupt -> resume**: restores `LoopState` from persisted steps and
  continues with `run_loop(initial_state=...)` without losing state. Regression
  tests demonstrate equivalence with an uninterrupted run (`tests/test_resume.py`).
- **Limited human gate**: interrupts only irreversible operations with
  approve/edit/reject/respond. State persistence supports pause/resume, and
  irreversible actions are exactly-once (`HumanGate` / `run_gated_loop` /
  `Decision`).
- **Coordinated multi-process concurrent resume (in-progress lease)**: even
  when multiple processes resume the same `run_id` concurrently, irreversible
  actions remain exactly-once and ordered consistently. If the winner crashes,
  another process reacquires the lease after expiry
  (`tests/test_concurrent_resume.py`).
- **Wake delivery transport**: delivers completion / next-iteration /
  decision-request wakes with push-first / pull-fallback delivery
  (at-most-once, claim-then-confirm). Includes in-memory and callable backends
  for the backend-independent `PushBackend` protocol (`Transport` /
  `WakeQueue` / `LoopWaker`).
- **work-discovery (next-iteration input selection)**: separates deterministic
  triage computation from the propose-only human-gate delivery layer. The next
  iteration does not start until an item is accepted (`WorkDiscovery` /
  `discover_next` / `triage`).
- **Outer Reflexion loop + RQGM epoch safety kernel**: wraps the inner ReAct
  loop as one episode, incorporates language-level guidance from failures into
  episodic memory, and wires it into the next context for self-improvement. The
  safety kernel promotes evaluators only at epoch boundaries (`run_reflexion` /
  `EpisodicMemory` / `Evaluator` / `admit_evaluator`).
- **Outer Reflexion persistence / resume**: epoch and lesson tables plus an
  evaluator version registry allow episode count, epoch, admitted lessons,
  evaluator version, and best score to continue across processes
  (`ReflexionStore` / `DBReflexionLog`).
- **Outer Reflexion observation**: observes episodes / epochs / lesson
  acceptance or rejection / evaluator promotion / convergence as events and
  OTel spans (`run_observed_reflexion` / `ReflexionObserver` /
  `ReflexionSpan`).
- **Outer-loop convergence conditions**: `MaxEpisodes` / `RubricThreshold` /
  `ScorePlateau` / `ReflectionBudget` / `EvaluatorUpdateBudget`.
- **examples**: verification-driven demo, observation demo, and outer
  Reflexion demo (`examples/verify_driven_demo.py` / `observed_demo.py` /
  `reflexion_demo.py`).
- **Research and design report**: an in-depth Loop Engineering survey and
  LoopAgent design (recommending proposal C), inventory of claude-org-ja
  assets, and staged roadmap (`report.md` / `report.html`).
- **Release operations**: OIDC Trusted Publishing workflow for PyPI
  (`.github/workflows/release.yml`, automatically publishes on `v*` tag push).

### Packaging

- Filled out `description` / `keywords` / `classifiers`
  (Development Status :: 4 - Beta) / `project.urls`.
- Organized optional extras to match implemented functionality: `[otel]`
  (OTel span integration) / `[test]` (test execution) / `[dev]`
  (test + build/twine).

## [0.0.1] - 2026-06-28

### Added

- Placeholder release (reserved the `loop-agent` name on PyPI). Published via
  OIDC Trusted Publishing: https://pypi.org/project/loop-agent/0.0.1/

[Unreleased]: https://github.com/happy-ryo/loop-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/happy-ryo/loop-agent/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/happy-ryo/loop-agent/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/happy-ryo/loop-agent/releases/tag/v0.0.1
