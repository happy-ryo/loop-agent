# Stability Contract

This page defines what `1.0.0` means for loop-agent. It is the compatibility
contract for users who depend on the package from Python, the CLI, or persisted
state.

## Stable Public API

The stable core API is the small embeddable loop surface:

| Area | Stable symbols |
|---|---|
| Loop driver | `run_loop`, `async_run_loop`, `ActOutcome`, `ReviewOutcome`, `ReviewHook`, `VerifyOutcome`, `LoopResult` |
| Loop state | `LoopState`, `StepRecord` |
| Stop conditions | `AnyOf`, `StopCondition`, `StopTrigger`, `MaxIterations`, `TokenBudget`, `Timeout`, `GoalMet`, `GoalCheck`, `NoProgress` |
| Per-call timeout | `TimeoutPolicy`, `SeamTimeout`, `UnsupportedTimeoutKill`, `TIMEOUT_GRACEFUL`, `TIMEOUT_KILL`, `ACT_TIMEOUT_OBSERVATION`, `REVIEW_TIMEOUT_OBSERVATION`, `VERIFY_TIMEOUT_OBSERVATION` |
| Persistence | `ProgressLog`, `read_progress`, `connect`, `LoopStore`, `DBProgressLog` |
| Human gate | `ActionGate`, `GateReview`, `HumanGate`, `Decision`, `run_gated_loop` |
| Observability | `LoopEvent`, `EventSink`, `ListSink`, `CallableSink`, `JsonlEventSink`, `read_events`, `LOOP_BEGIN`, `LOOP_STEP`, `LOOP_END`, `LoopObserver`, `run_observed_loop` |
| Verifier helpers | `CommandVerifier`, `PytestVerifier`, `RegexVerifier` |
| Errors | `LoopError`, `ConfigError`, `StateError`, `AsyncSeamInSyncLoop` |
| API grouping metadata | `CORE_API`, `HARNESS_API`, `ADVANCED_API`, `OPERATIONS_API`, `PUBLIC_API_GROUPS` |

These symbols are available from `import loop_agent`. Removing them, renaming
them, or changing their call signatures incompatibly requires a major release.
Adding optional parameters with compatible defaults is allowed in a minor release.

## Typed Package Contract

loop-agent ships a `py.typed` marker in the wheel and declares `Typing :: Typed`
metadata. The inline annotations for the stable public API are therefore part of
the downstream type-checking surface under PEP 561. Runtime behavior remains the
compatibility authority; annotation-only fixes that make the documented behavior
more accurately typed may ship in minor or patch releases.


## Public API Groups

The top-level namespace intentionally remains broad for coding-agent discovery,
but it is not flat. These machine-readable lists classify the surface:

| Group | Intended use |
|---|---|
| `CORE_API` | Daily loop construction: driver, outcome dataclasses, stop conditions, verifier helpers, and errors. |
| `HARNESS_API` | Production harness helpers: persistence/resume, human gates, and multi-item work-list scheduling. |
| `ADVANCED_API` | Opt-in advanced composition: per-call timeout primitives, notifications, Reflexion, evaluator/memory, transport, and work discovery. |
| `OPERATIONS_API` | Read-only and observational helpers: events, observed loop runners, OTel, dashboards, spikes, throttling, and wake helpers. |
| `PUBLIC_API_GROUPS` | Mapping from group name to the corresponding list for tools and coding agents. |

`__all__` is the concatenation of these groups plus the group names themselves.
New top-level exports must be placed in exactly one group, then documented in the
classification table below. Human-facing docs should lead with `CORE_API`; coding
agent docs should expose `CORE_API` plus `HARNESS_API` before advanced surfaces.

## Advanced Stable API

The following surfaces are stable but advanced. They are still compatibility
covered, but their policies are intentionally opt-in and application-owned:

- Reflexion and evaluator APIs.
- Transport and wake queues.
- Work discovery and `WorkListGather`.
- Operations helpers such as spike detection, circuit breaker stop conditions,
  static dashboard rendering, and throttling primitives.
- Notifier integrations.
- `loop_agent.adapters` contracts and bundled Claude Code / Codex adapters.

For these areas, loop-agent preserves import paths, data-class field names, and
documented behavior. External provider CLI output can still change outside this
project; adapter parsers are maintained on a best-effort basis with regression
tests for known schemas.

## Complete Top-level Export Classification

Every symbol in `loop_agent.__all__` is classified here. A new top-level export
must be added to one of these rows before release.

| Classification | Symbols |
|---|---|
| Core loop driver | `run_loop`, `async_run_loop`, `ActOutcome`, `ReviewOutcome`, `ReviewHook`, `VerifyOutcome`, `LoopResult` |
| Core state | `LoopState`, `StepRecord` |
| Core stop conditions | `AnyOf`, `StopCondition`, `StopTrigger`, `MaxIterations`, `TokenBudget`, `Timeout`, `GoalMet`, `GoalCheck`, `NoProgress` |
| Practical verifier helpers | `CommandVerifier`, `PytestVerifier`, `RegexVerifier` |
| Per-call timeout | `TimeoutPolicy`, `SeamTimeout`, `UnsupportedTimeoutKill`, `TIMEOUT_GRACEFUL`, `TIMEOUT_KILL`, `ACT_TIMEOUT_OBSERVATION`, `REVIEW_TIMEOUT_OBSERVATION`, `VERIFY_TIMEOUT_OBSERVATION` |
| Persistence | `ProgressLog`, `read_progress`, `connect`, `LoopStore`, `DBProgressLog` |
| Human gate | `ActionGate`, `GateReview`, `HumanGate`, `Decision`, `run_gated_loop`, `DECISION_KINDS` |
| Observability | `LoopEvent`, `EventSink`, `ListSink`, `CallableSink`, `JsonlEventSink`, `read_events`, `LOOP_BEGIN`, `LOOP_STEP`, `LOOP_END`, `LOOP_SPIKE`, `LoopObserver`, `run_observed_loop` |
| Operations helpers | `Spike`, `SpikeDetector`, `detect_spikes`, `scan_spikes`, `state_from_steps`, `render_dashboard_html`, `AdapterFailureBreaker`, `VerifyDetailBreaker`, `TimeoutMarkerBreaker`, `PerStepTokenCap`, `LaunchThrottleDecision`, `launch_throttle_decision`, `step_throttle` |
| OpenTelemetry helpers | `LoopSpan`, `otel_available`, `ReflexionSpan` |
| Notifications | `Notifier`, `ApprovalRequest`, `ApprovalDescriber`, `Redaction`, `redact_payload`, `DEFAULT_SENSITIVE_KEY_PARTS`, `REDACTED`, `WebhookNotifier`, `SlackNotifier`, `EmailNotifier`, `ConsoleNotifier`, `MultiNotifier` |
| Reflexion driver | `run_reflexion`, `ReflexionContext`, `ReflexionState`, `ReflexiveResult`, `EpisodeRecord`, `EpochRecord`, `EpisodeOutcome`, `ReflexionStore`, `DBReflexionLog`, `ReflexionObserver`, `run_observed_reflexion` |
| Reflexion events | `REFLEXION_BEGIN`, `EPISODE_BEGIN`, `EPISODE_END`, `LESSON_DECISION`, `EPOCH_BOUNDARY`, `REFLEXION_END` |
| Evaluator and memory | `Score`, `GroundTruthSignal`, `Evaluator`, `Probe`, `HeldOut`, `agreement`, `admit_evaluator`, `AdmissionResult`, `Lesson`, `LessonVerdict`, `EpisodicMemory`, `default_admit`, `step_signature`, `OuterState`, `MaxEpisodes`, `RubricThreshold`, `ScorePlateau`, `ReflectionBudget`, `EvaluatorUpdateBudget`, `is_success_condition` |
| Transport and wakes | `Wake`, `WAKE_LOOP_DONE`, `WAKE_NEXT_ITERATION`, `WAKE_DECISION_REQUEST`, `WAKE_KINDS`, `PushBackend`, `CallablePushBackend`, `NullPushBackend`, `WakeQueue`, `InMemoryWakeQueue`, `SqliteWakeQueue`, `RedisWakeQueue`, `open_wake_queue`, `Transport`, `CADENCE_SECONDS`, `DEFAULT_CADENCE_SECONDS`, `cadence_for`, `due_to_poll`, `LoopWaker`, `wakes_for_result`, `wake_id_for` |
| Work discovery | `Candidate`, `BlockedCandidate`, `Triage`, `triage`, `Proposal`, `AdoptionResult`, `WorkDiscovery`, `discover_next`, `WorkItem`, `WorkListGather`, `WorkListProgress`, `WorkListDrained`, `ScheduleContext`, `Scheduler`, `Drained`, `DRAINED` |
| Errors | `LoopError`, `ConfigError`, `StateError`, `AsyncSeamInSyncLoop` |
| API grouping metadata | `CORE_API`, `HARNESS_API`, `ADVANCED_API`, `OPERATIONS_API`, `PUBLIC_API_GROUPS` |

## API Surface Discipline

The core loop surface should stay small. New top-level exports must preserve the boundary that loop-agent owns orchestration while callers own policy. See [api-surface.md](./api-surface.md) for the checklist used before adding public symbols.
## Explicit Non-Contracts

- Human-readable CLI formatting is best-effort unless a document says otherwise.
  CLI exit codes and command meanings are stable.
- OpenTelemetry GenAI semantic convention names are experimental upstream; the
  presence of loop-agent spans/events is stable, but exact third-party attribute
  names may change when upstream conventions change.
- Generated dashboard HTML structure is not a CSS/DOM integration contract. It is
  a read-only operations artifact.

## Deprecation Policy

After `1.0.0`, breaking a stable public API requires:

1. Deprecating the old symbol or behavior in a minor release when practical.
2. Documenting the replacement in the changelog and API docs.
3. Removing or changing it only in a later major release.

Security fixes or correctness fixes may change behavior without a long
deprecation period when preserving the old behavior would be unsafe.

## Version Sources

Before a release, these must agree:

- `pyproject.toml` `[project].version`
- `loop_agent.__version__`
- `CHANGELOG.md`
- git tag `vX.Y.Z`

The release gate is:

```bash
python -m ruff check .
python -m mypy
python -m pytest
python -m build
python -m twine check dist/*
python scripts/verify_wheel_skill_bundle.py
```
