# API Reference

This is the index page for the API exposed by LoopAgent. It summarizes the stable `1.0.0` scope, installation steps, a table of all exported elements, and test suite coverage.

## 1.0.0 Stable Scope

This implementation extends the loop core that began in report.md §4.4 into a stable embeddable runtime. It runs `gather -> act -> review? -> verify -> repeat` in a **single-agent, single-process** model and OR-composes **hard limits that can be combined** (`MaxIterations` / `TokenBudget` / `Timeout`). Reaching a limit is returned as **control output with a reason** (`LoopResult`), not as an exception. [stability.md](./stability.md) is the authoritative source for compatibility boundaries.

Scope (intentionally modest: *simpler loops win*):

- ✅ Loop driver plus mechanical, composable stop conditions that preserve the triggered condition and reason.
- ✅ `act` / `review` / `verify` are **injectable hooks**. In-memory functions, subprocesses, Claude Code, Codex, and custom adapters all attach to the same seam.
- ✅ Ground-truth verifier helpers: `CommandVerifier` / `PytestVerifier` / `RegexVerifier` are thin helpers that attach existing mechanical oracles to the `verify` seam. The caller retains the policy for success criteria.
- ✅ **Runaway-prevention guarantee**: sandbox tests prove the loop always stops at a limit, even when the goal is unmet, progress stalls, or actions repeat (`tests/test_runaway_guard.py`).
- ✅ **Dual termination conditions (semantic stop)**: in addition to mechanical limits, `GoalMet` (successful completion of a verifiable goal) and `NoProgress` (termination after detecting no progress or repeated actions) participate in the same `AnyOf` composition.
- ✅ **Minimal state (progress file)**: each iteration is appended to an external file as JSON Lines so progress survives across processes (`ProgressLog` / the minimal predecessor of the state.db SoT).
- ✅ **Observability (structured events + OTel span)**: `loop_begin/step/end` events flow to sinks so termination reasons and metrics can be analyzed after the run (`run_observed_loop` / OTel GenAI span).
- ✅ **Loop state SoT (state.db)**: each step is persisted **atomically in a transaction** to a minimal SQLite schema for loops (`run` / `step` / `event` / `stop_reason`). `DBProgressLog` is a drop-in replacement for `ProgressLog` (Issue #11).
- ✅ **Interrupt -> resume**: `LoopState` is restored from persisted steps, and `run_loop(initial_state=...)` continues from the interruption point without losing state. It carries forward the iteration count, accumulated cost, `elapsed`, and history. Regression tests prove that an interrupted-and-resumed run matches a continuous run (`tests/test_resume.py` / Issue #14).
- ✅ **async/await support**: asynchronous entry point `async_run_loop` (`await async_run_loop(...)`). The synchronous API `run_loop` is fully preserved and internally wraps `asyncio.run`. The `gather`/`act`/`review`/`verify`/`conditions`/`gate`/`on_step` seams continue to accept synchronous callables while also accepting asynchronous callables (they may be mixed; synchronous hooks add no extra cost). Multiple loops can run concurrently with `asyncio.gather` (`tests/test_async_loop.py` / Issue #40).
- ✅ **Limited human gate**: only irreversible operations interrupt for approve/edit/reject/respond. State persistence provides pause/resume and exactly-once semantics for irreversible actions (Issue #15).
- ✅ **Optional post-act review**: a `review=` hook returning `ReviewOutcome` evaluates artifacts from LLM-backed `act` before `verify`. A blocking review is recorded as a failed step with JSON feedback and skips `verify` for that iteration (Issue #128).
- ✅ **Coordination for concurrent multi-process resume (in-progress leases)**: even when several processes resume the same `run_id` concurrently, irreversible actions are **exactly-once and ordered**. This uses a multi-stage lifecycle (`pending -> resolved -> executing -> executed`) plus a single-winner lease. Losers pause until `executed`; if the winner crashes, lease expiry allows another process to reclaim the step without losing it. This is proven with simulated concurrent processes (`tests/test_concurrent_resume.py` / Issue #21).
- ✅ **Wake delivery transport / work-discovery for selecting next-iteration input**: completion, next-iteration, and decision-request wakes are delivered with push as the primary path and pull as fallback (`tests/test_transport.py` / Issue #23). The next iteration target is selected through a compute layer (deterministic triage) plus a delivery layer (propose-only human gate) (`tests/test_discovery.py` / Issue #24).
- ✅ **Outer Reflexion loop + RQGM epoch safety core**: wraps an inner ReAct loop as one episode, absorbs linguistic guidance from failures into episodic memory, and wires it into the next context for self-improvement (report.md §5 Phase 3 / Issue #22; see below).
- ✅ **Operational read-only helpers**: `summary` / static HTML `dashboard` / post-hoc `spikes` / live `SpikeDetector` / circuit breaker `StopCondition` helpers / opt-in throttling primitives ([operations-roadmap.md](./operations-roadmap.md)). External infrastructure such as Grafana and business-specific automated control policy remain the caller's responsibility.

## Stable API Boundary

In `1.0.0`, symbols in `loop_agent.__all__` are treated as the public API. [stability.md](./stability.md) classifies these into core APIs, which form the center of the stable contract for everyday use, and advanced APIs, which are more specialized but still compatibility-preserved. Removing, renaming, or incompatibly changing the signature of a public API requires a major release.

The top level of `loop_agent` is intentionally broad so coding agents can discover the pieces needed for a harness. However, it is grouped rather than presented as a flat list: `CORE_API` / `HARNESS_API` / `ADVANCED_API` / `OPERATIONS_API` / `PUBLIC_API_GROUPS`. For the shortest import path, see [first-harness-api.md](./first-harness-api.md). When a coding agent needs to choose an API by use case, use [ai-api-map.md](./ai-api-map.md).

## Installation

```bash
python3 -m pip install -e .        # Loop core package
python3 -m pip install -e .[dev]   # + pytest for running tests
```

## API Overview

| Element | Role |
|---|---|
| `run_loop(*, act, verify, conditions, gather=..., review=..., on_step=..., gate=..., time_fn=..., initial_state=..., timeout=...)` | Loop driver. Returns `LoopResult`. Passing `review` runs post-act artifact review after `act` and before `verify`; passing `gate` interrupts irreversible operations; passing a restored `LoopState` as `initial_state` **resumes** from the interruption point (resume #14); `timeout` sets per-call timeouts for `act`/`review`/`verify` (#42). |
| `ActOutcome(observation, tokens)` | Return value from the `act` hook: action result plus consumed tokens. |
| `ReviewOutcome(approved, feedback="", severity="info")` | Return value from the `review` hook. If `approved=False` and `severity="blocking"`, that iteration is recorded with `goal_met=False` and `verify` is skipped. Feedback from a blocking review is stored as JSON in `StepRecord.detail` / state.db `step.detail`; normal step detail continues to store `verify.detail`. |
| `VerifyOutcome(goal_met, detail)` | Return value from the `verify` hook. `goal_met=True` causes natural termination. |
| `MaxIterations(n)` / `TokenBudget(b)` / `Timeout(s)` | Mechanical hard limits, represented as composable stop conditions. |
| `TimeoutPolicy(act=..., review=..., verify=..., default=..., on_timeout=...)` | **Per-call** timeout for `act`/`review`/`verify` (#42). Pass it to `timeout=` on `run_loop`/`async_run_loop` as either a `TimeoutPolicy` or bare seconds. `on_timeout="graceful"` (default) gives up, records a synthetic step, and continues to the next iteration; `"kill"` raises `SeamTimeout`. Async seams use asyncio task cancellation. Sync seams use `SIGALRM` on the POSIX main thread; where unavailable, graceful mode is post-hoc and kill mode raises `UnsupportedTimeoutKill`. This is separate from the whole-run `Timeout` stop condition. Details: [recipes/timeout-and-kill.md](./recipes/timeout-and-kill.md). |
| `GoalMet(verifier)` | Stops successfully when a verifiable goal is met (`stop.name="goal_met"`). `verifier(state)` returns either `bool` or `GoalCheck(met, detail)`. |
| `NoProgress(window, repeat, key=...)` | If the same `key` appears at least `repeat` times in the latest `window` steps (default key is observation), stop as no progress (`stop.name="no_progress"`). |
| `CommandVerifier(command, cwd=..., timeout=...)` / `PytestVerifier(args=..., timeout=...)` / `RegexVerifier(pattern, ...)` | Common ground-truth verification helpers. They convert command exit codes, pytest results, or regex matches on adapter output into `VerifyOutcome`. These are helpers for thinly wrapping mechanical oracles, not LLM-as-judge. Details: [verifiers.md](./verifiers.md). |
| `LoopResult` | `status`(`goal_met`/`stopped`/`paused`) / `stop`(triggered condition) / `reason` / `succeeded`(success = natural `goal_met` termination or a triggered `GoalMet` condition) / `goal_met`(natural termination from the verify hook only) / `paused`(interrupted by a human gate) / `pending`(interrupted irreversible action) / `iterations` / `tokens_used` / `elapsed` / `history`. |
| `ProgressLog(path)` | Minimal persistent state that appends each iteration as JSON Lines. Pass `on_step` to `run_loop`, then append the termination reason with `record_result(result)`. |
| `read_progress(path)` | Reads back the progress file. A crash-truncated final line is tolerated; a corrupt line in the middle raises. |
| `run_observed_loop(*, act, verify, conditions, sinks=..., otel=True, tracer=..., on_step=..., ...)` | Entry point that wires observability into `run_loop`. Emits `loop_begin/step/end` and creates an OTel span. |
| `LoopObserver(sinks, *, conditions=..., otel=True, tracer=...)` | Observability orchestrator and context manager. Pass its `on_step` to `run_loop` and call `record_result(result)`. |
| `LoopEvent(kind, iteration, elapsed, payload)` | Structured event. `kind` is `loop_begin`/`loop_step`/`loop_end`. |
| `ListSink` / `JsonlEventSink(path)` / `CallableSink(fn)` | Event sinks: in-memory, journal-style JSONL, or arbitrary function adapter. |
| `read_events(path)` | Reads back JSONL events. A crash-truncated final line is tolerated; a corrupt line in the middle raises. |
| `LoopSpan` / `otel_available()` | Thin wrapper for an OTel GenAI span, no-op when OTel is not installed / OTel availability check. |
| `SpikeDetector(sinks, *, token_window=..., latency_window=..., multiplier=..., repeated_failure=...)` / `detect_spikes(state, ...)` / `scan_spikes(steps, ...)` / `LOOP_SPIKE` | Opt-in operational spike detection. Detects token, latency, repeated failure, and timeout-marker spikes as either a live `on_step` observer or a scan over saved steps. **It does not change control flow**; stopping remains the responsibility of a separate `StopCondition` or application policy. |
| `AdapterFailureBreaker(repeat)` / `VerifyDetailBreaker(repeat)` / `TimeoutMarkerBreaker(repeat)` / `PerStepTokenCap(limit)` | Common circuit-breaker `StopCondition` helpers. They stop explicitly by policy for adapter failures, verify detail, timeout markers, or per-step spend. |
| `launch_throttle_decision(...)` / `step_throttle(act, delay_seconds, sleep)` | Opt-in throttling primitives. Launch decisions are pure functions. Step throttling is a wrapper that explicitly calls the injected `sleep`. The default behavior of `run_loop` is unchanged. |
| `connect(path)` | Opens or creates the loop state DB, applies the minimal schema, and returns the connection (`":memory:"` is supported). |
| `LoopStore(conn)` | Writer/reader for state.db. `transaction()` (atomic) / `load_or_init(run_id)` (new runs are empty; existing runs are restored as a resume seed) / `record_step` / `record_result` / `read_steps` / `read_events` / `get_run` / `get_stop_reason` / `request_decision` / `resolve_decision` / `get_decision` / `list_pending_decisions` / `claim_execution` (single-process at-most-once) / `acquire_lease` / `complete_execution` (in-progress leases for concurrent multi-process resume, #21). |
| `DBProgressLog(db, run_id)` | DB-backed progress log. It is a drop-in replacement for `ProgressLog`, with compatible `on_step` / `record_result` methods and a context manager that accepts a path or existing connection. `.state` is the restored `LoopState`, used as the resume seed. |
| `HumanGate(*, on, store, run_id, resolver=..., key=..., active=True, owner=..., lease_ttl=..., now_fn=...)` | Human gate that interrupts only irreversible operations (`ActionGate` implementation). Pass `review(context, state)` to `run_loop(gate=...)`. `owner` / `lease_ttl` / `now_fn` configure in-progress leases for concurrent multi-process resume (#21). |
| `Decision(kind, payload=...)` | Human decision. `kind` is one of `approve`/`edit`/`reject`/`respond`. This is the return value from `resolver`. |
| `run_gated_loop(*, act, verify, conditions, on, store, run_id, gather=..., on_step=..., resolver=..., key=..., active=True, owner=..., lease_ttl=..., now_fn=...)` | Entry point that constructs `HumanGate` and runs `run_loop`. `owner` / `lease_ttl` / `now_fn` are for concurrent multi-process resume (#21). |
| `run_reflexion(*, episode, ground_truth, reflect, evaluator, convergence, declared_keys, production_tasks, held_out, epoch_len=4, epsilon=0.02, delta=0.0, propose_evaluator=..., admit_lesson=..., memory=..., on_episode=..., on_epoch=..., persist=..., initial_state=...)` | Outer Reflexion loop driver. Calls the inner `run_loop` as one episode, absorbs linguistic guidance from `reflect` into memory, and wires it into the next context. Returns `ReflexiveResult`. `on_epoch` is an observation hook at epoch boundaries (`EpochRecord`), `persist` is a persistence hook that receives each episode's **settled state** after epoch-boundary processing, and passing a restored `ReflexionState` as `initial_state` **resumes** from the interruption point (resume #29). |
| `ReflexionStore(conn)` | Writer/reader for outer Reflexion state, paired with the inner `LoopStore`. On creation, it applies the four tables `reflexion_run`/`reflexion_episode`/`reflexion_lesson`/`reflexion_evaluator` **additively and non-destructively**. `load_or_init(run_id, memory=...)` (new runs are empty; existing runs are restored as a resume seed) / `persist_episode(run_id, record, state)` (atomically persists episode + memory + scalars + version in one transaction) / `record_result(run_id, result)` (terminal metadata) / `get_run` / `read_episodes` / `read_evaluator_versions` (evaluator version registry). |
| `DBReflexionLog(db, run_id, *, memory=...)` | DB-backed outer progress log, paired with the inner `DBProgressLog`, and a drop-in replacement. `.state` (restored `ReflexionState` = resume seed) / `.memory` (live memory) / `on_episode` (passed to `run_reflexion(persist=...)`) / `record_result(result)` / context manager. `memory` configures capacity policy for fresh runs; on resume, the DB-stored value takes precedence. |
| `ReflexionContext(episode, epoch, task, evaluator, memory_block)` | Context passed to the `episode` hook. `memory_block` (lessons from prior attempts) is folded into the inner gather. |
| `ReflexiveResult` | `status`(`converged`/`stopped`/`paused`) / `succeeded` (success condition is satisfied, independent of ordering) / `paused` (inner episode was interrupted by a human gate) / `pending` (inner pending action during interruption) / `best_score` / `episodes` / `epochs` / `reason` / `state` (`ReflexionState`: `episodes` / `gt_aggregate_history` / `memory` ...). If an inner episode pauses in `HumanGate`, the outer loop also pauses without scoring or reflecting. After the decision is persisted and the run resumes, the same episode is re-executed, preserving the pause/resume contract from Issue #15. |
| `Score(ground_truth, components=..., judge=...)` | Multi-axis score. `aggregate(declared_keys)` returns the **minimum** over declared axes. Missing axes count as 0.0 and judge is excluded. |
| `GroundTruthSignal(succeeded, score, ground_truth_backed=True)` | Primary signal from inner `verify`. `ground_truth_backed=False` is excluded from convergence. |
| `Evaluator(score, rubric=..., name=..., version=...)` | Rubric evaluator fixed within an epoch. It provides reward for reflection. `version` is a content hash. |
| `Probe(case_id, outcome, gold_label, fold=0, critical=False)` / `HeldOut(probes)` | Measurement basis for evaluator promotion using fixed gold labels. `fold(k)` rotates folds. |
| `agreement(evaluator, held_out)` / `admit_evaluator(inc, cand, held_out, *, epsilon, delta=0.0)` | Agreement with gold labels (calibration) / promotion gate using ε-best-belief plus dominance (`AdmissionResult`). |
| `Lesson(text, episode, provenance, support)` / `LessonVerdict(admit, reason)` | Linguistic guidance / verdict from pre-admission validation. |
| `EpisodicMemory(*, cap=8, per_lesson_chars=512, render_byte_cap=4096)` | Bounded episodic memory with `admit` / `render` / deterministic, value-aware eviction. |
| `default_admit(lesson, outcome)` | LLM-independent structural pre-admission validation. Enforces grounding, support, and limits, and rejects injected lessons. |
| `MaxEpisodes(n)` / `RubricThreshold(target, sustain=1)` / `ScorePlateau(window, min_delta)` / `ReflectionBudget(n)` / `EvaluatorUpdateBudget(n)` | Outer convergence conditions, compatible with `AnyOf`. `RubricThreshold` is a success condition. |
| `run_observed_reflexion(*, episode, ground_truth, reflect, evaluator, convergence, declared_keys, production_tasks, held_out, ..., sinks=..., otel=True, tracer=..., span_name=..., on_episode=..., on_sink_error=...)` | Entry point that wires observability into `run_reflexion` (Issue #30). Emits `reflexion_begin/episode_*/lesson_decision/epoch_boundary/reflexion_end` and creates an outer OTel span. Decision logic is unchanged and the same `ReflexiveResult` is returned. |
| `ReflexionObserver(sinks, *, convergence=..., declared_keys=..., evaluator_version=..., epoch_len=..., epsilon=..., otel=True, tracer=...)` | Outer observability orchestrator and context manager. Wires `on_episode_begin(ctx)` / `on_episode(record, state)` / `on_epoch(record)` into the observation points of `run_reflexion` and calls `record_result(result)`. This is best effort and also catches errors from observation hooks themselves. |
| `EpochRecord(epoch, boundary_episode, previous_version, evaluator_version, admission=...)` | Observation unit for epoch boundaries. Derives `decision` (`promoted`/`rejected`/`unchanged`) / `proposed` / `promoted`. It is a pure side-channel record passed by `run_reflexion(on_epoch=...)`. |
| `ReflexionSpan` | Thin wrapper for an OTel GenAI span around an outer Reflexion run. It is a no-op when OTel is not installed. Carries `gen_ai.*` + `loop_agent.reflexion.*` attributes from epochs, versions, and lessons, and records transitions as span events. |
| event kind: `reflexion_begin` / `episode_begin` / `episode_end` / `lesson_decision` / `epoch_boundary` / `reflexion_end` | Structured event kinds for outer observability. They reuse the same `LoopEvent` / sink / `read_events` as the inner `loop_*` events. |
| `Wake(id, kind, recipient, run_id=..., payload=...)` | One wake to deliver. `id` is the key for at-most-once delivery and de-duplication. `kind` is one of `loop_done`/`next_iteration`/`decision_request`. |
| `Transport(queue=..., backend=..., *, lease=30.0, time_fn=...)` | Orchestrator with push as the primary path and pull fallback. `deliver(wake)` (returns `"push"`/`"queued"`) / `poll(recipient, *, owner=..., limit=..., confirm=False)` (claim only) / `poll_and_handle(recipient, handler, ...)` (confirms after handler success; crash-safe and recommended) / `confirm_wakes(wakes, *, owner)` / `pending(recipient=...)`. |
| `InMemoryWakeQueue()` | Source of truth for delivery, with three-state claim-then-confirm. Implements the `WakeQueue` protocol. `enqueue` (idempotent) / `claim` / `confirm` / `release_expired` / `mark_delivered` / `pending`. |
| `PushBackend` / `CallablePushBackend(fn)` / `NullPushBackend()` | Push interface for immediate-delivery acceleration (`push(wake)->bool`, best effort) / arbitrary function adapter / always fails (= backend unavailable). |
| `LoopWaker(transport, *, run_id, recipient, next_recipient=...)` | Drop-in component that delivers loop wakes. `record_result(result)` delivers completion and decision-request wakes, plus next-iteration wakes, and is compatible with observers. |
| `wakes_for_result(result, *, run_id, recipient, next_recipient=...)` | Pure mapping from `LoopResult` to the `Wake` objects that should be delivered, with no side effects. |
| `cadence_for(role)` / `due_to_poll(role, last_poll, now)` | Poll cadence by role (dispatcher 180s / worker 60s / secretary 0) / determines whether active polling is due. |
| `Candidate(id, priority=0, effort=1, depends_on=(), summary="", payload=None)` | Work candidate for the next iteration. All fields are JSON-native. `payload` is the value passed as the next loop input when the candidate is adopted. |
| `triage(candidates, *, done=())` | Compute layer: read-only and deterministic. Resolves dependencies, ranks work, detects cycles, and returns `Triage(ready, blocked, recommended)`. |
| `WorkDiscovery(store, run_id)` | Delivery layer. `propose(candidates, *, done=, cycle=)` registers a proposal as pending in the human gate (propose-only and idempotent) / `resolve(cycle, decision, payload=)` records acceptance or rejection (edit is allowed only for ready candidates) / `adopted(cycle)` reads the adoption result as `AdoptionResult`, stable across resume. |
| `AdoptionResult` | Result of adoption resolution. `status`(`pending`/`resolved`/`absent`) / `decision` / `candidate`(adopted candidate or None) / `recommended` / `response` / `adopted`(whether a candidate was adopted). |
| `discover_next(*, store, run_id, candidates, result=None, done=(), cycle=0)` | Connects completion to the next iteration. If `result.paused`, returns `None` without proposing; if the run is complete, calls `propose` but does not adopt or launch. |
| `WorkListGather(items, *, strategy="fewest_attempts", max_attempts_per_item=None, done_when=..., build_ctx=...)` | `gather` hook that fairly runs multiple items through a single loop (Issue #56). `strategy` is `round_robin`/`fewest_attempts`/`fifo`/`priority`/custom callable. `max_attempts_per_item` sets a per-item limit (*exhausted*). `done_when(item, record)` determines per-item completion. Progress is derived from `state` through `attempts`/`done_items`/`exhausted_items`/`remaining`/`report`, making it resume-safe. `from_triage(candidates, *, done=, strategy=, ...)` delegates priority and ordering to triage. |
| `WorkListDrained(gatherer)` | Stop condition that stops when every item is done or exhausted. It is evaluated before `gather` to prevent `DRAINED` from leaking. `WorkItem(id, priority=0, payload=None)` is one schedulable item. |
| `loop_agent.cli:main(argv=None)` | CLI entry point (`loop-agent` in `[project.scripts]`, Issue #31). Includes `run`/`status`/`resume`/`logs` subcommands plus quick help when called with no arguments. Returns a process exit code: success 0 / stopped 1 / configuration error 2. |
| `cli.load_config(path)` / `cli.parse_config(data)` | Loads `task.toml` into a validated `Config` (`[loop]`/`[conditions]`/`[act]`/`[verify]`/`[state]`). Uses stdlib `tomllib` on 3.11+ and `tomli` on 3.10. |
| `cli.build_conditions(cfg, *, max_iter=..., token_budget=..., timeout=...)` | Composes stop conditions from `Config` (CLI flags > TOML values > unspecified). Raises `ConfigError` when none are configured (R3). |
| `cli.build_act(cfg)` / `cli.build_verify(cfg)` | Builds `act`/`verify` hooks. Supports subprocess mode (`{prompt}`/`{goal}`/`{iteration}` substitution; exit code 0 = goal) and Python callable mode (`module:attr`). |
| `cli.resolve_callable(spec)` | Resolves a `module:attr` (or `module.attr`) reference to a callable for Python mode. |
| `loop-agent summary [--db PATH] [--limit N]` | Read-only listing of runs in `state.db`. Displays run id / status / iterations / tokens / elapsed / pending count / event count / stop reason. It does not change decision logic. |
| `loop-agent dashboard --output PATH [--db PATH]` | Generates a read-only static HTML dashboard from `state.db`. Shows run list / step timeline / pending decisions / Reflexion summary. |
| `loop-agent spikes [run-id] [--db PATH]` | Post-hoc scan for token, latency, repeated-failure, and timeout-marker spikes from saved steps. |

- `conditions` is a list of stop conditions, or an `AnyOf`. It is OR-evaluated in **declaration order**, and the first condition that triggers is reported as `result.stop`.
- Termination conditions are **evaluated at the start of each iteration, as the while guard**. `TokenBudget` / `Timeout` are checked at iteration boundaries and do not interrupt a step already in progress, so the run may exceed the limit by one step. Consumed tokens and elapsed time cannot be undone; the semantics are "do not start a new step once the limit has been spent."
- If `gather` is omitted, `LoopState` itself becomes the context passed to `act`. `review(outcome)` is an optional post-act hook. A blocking review skips `verify` and leaves feedback for the next iteration. `on_step(record, state)` is the minimal observation hook called after each iteration completes.
- Passing no stop conditions raises `ConfigError` to prevent infinite loops (R3). `ConfigError` is part of the `LoopError` hierarchy and also inherits `ValueError` for backward compatibility ([errors.md](./errors.md)).

## Tests

```bash
python3 -m pytest        # Triggering each limit / natural termination on goal completion /
                         # distinguishing termination reasons /
                         # proving runaway prevention (test_runaway_guard) /
                         # progress file coverage (test_progress) /
                         # real execution of the verification-driven demo (test_verify_demo) /
                         # observability: every termination reason is recorded in events,
                         #   metrics are traceable, and OTel spans are emitted
                         #   (test_events / test_observe / test_otel) /
                         # state SoT: persistence, transactions, crash tolerance,
                         #   and schema independence
                         #   (test_store) /
                         # wake delivery: pull fallback keeps delivery working when
                         #   the backend is unavailable; at-most-once delivery;
                         #   lease-expiry redelivery; owner fencing;
                         #   concurrent polling safety; role-specific cadence
                         #   (test_transport / test_waker) /
                         # work-discovery: deterministic triage, dependency resolution,
                         #   cycle detection / propose-only human gate /
                         #   adoption mapping / full cycle from completion to next iteration
                         #   (test_discovery)
```

## Related

- [../README.md](../README.md) — Project overview and navigation.
- [seams.md](./seams.md) — Detailed specification and types for the seams (`gather`/`act`/`review`/`verify`/`conditions`/`gate`).
- [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) — How to write an adapter that connects to the `act` seam (`ActHook` protocol).
- [errors.md](./errors.md) — `LoopError` hierarchy and exception contract.
- [review.md](./review.md) — Optional post-act review.
- [verifiers.md](./verifiers.md) — Ground-truth verifier helpers.
- [ai-api-map.md](./ai-api-map.md) — Capability map for coding agents.
- [api-surface.md](./api-surface.md) — Criteria for adding public APIs.
