# Operations

loop-agent 1.0.0 provides an **emit layer** that exposes loop decisions without changing the loop's decision logic, plus read-only and opt-in operations helpers built on top of it:

- structured `loop_begin` / `loop_step` / `loop_end` events
- outer Reflexion `episode_*` / `epoch_boundary` / `lesson_decision` events
- OTel GenAI span
- run / step / event / stop_reason records in `state.db`

This page summarizes the operations features built on that layer. The items covered here are not requirements of the loop core; they are policy, UI, and automatic control mechanisms needed for long-running operations and multi-loop operations. The summary, dashboard, spike scan, common circuit breaker helpers, and throttling primitives are implemented. Business-specific thresholds and external observability infrastructure remain caller-side policy.

## Dashboard

The dashboard is a thin read-only layer that visualizes events and `state.db` as-is. `loop-agent summary` provides the run list from `state.db`, and `loop-agent dashboard --output dashboard.html` outputs a static HTML dashboard.

- run list: `status` / `iterations` / `tokens_used` / `elapsed` / `stop_reason`
- step timeline: per-iteration `tokens` / `tokens_used` / `elapsed`
- paused runs: `gate_key` / `created_at` / `status` for pending decisions
- Reflexion: episode score / best score / evaluator version / lesson acceptance

Implemented:

- read-only SQL against `state.db` and static HTML
- `loop-agent summary` / CLI summary that reads the JSONL event sink
- HTML display for the step timeline / pending decisions / Reflexion summary

Potential external integration:

- OTel collector + Grafana (loop-agent emits spans/events; operations owns the Grafana setup)

Boundary: the dashboard only displays observed results. It does not change the decision logic for stop conditions, human gates, or evaluator promotion.

## Spike Detection

Before automatic control, implement detection only.

- token spike: exceeds 3x the median of the most recent N steps
- latency spike: exceeds 3x the elapsed delta of the most recent N steps
- error spike: consecutive adapter results with `failed=True`
- verify spike: consecutive verify timeouts / failed details
- no-progress spike: `NoProgress` keys are concentrated on the same observation

Detection results are recorded as `loop_spike` events and do not stop the run in the initial phase. Application-side policy decides whether to stop. `SpikeDetector` can be used as an opt-in `on_step` observer. Saved runs can be scanned post hoc with `loop-agent spikes [run-id]`.

## Throttling

Throttling is treated as opt-in policy, not a library default.

- launch throttling: do not start new runs
- step throttling: sleep before the next iteration / return to the wake queue
- model throttling: move back to a cheaper model with `ModelLadder`
- budget throttling: lower `TokenBudget` / `Timeout`

Implementation boundary:

- loop-agent provides observed values plus injection points for stop conditions, transports, and adapters.
- Application-side policy decides which thresholds stop, delay, or switch models.

Design details are in [throttling.md](./throttling.md).

## Circuit Breakers

Circuit breakers are implemented as stop conditions / gate policies that stop loops quickly when they keep repeating the same failure.

Candidates:

- adapter failure breaker: `failed=True` occurs K times in a row
- verify failure breaker: the same verify detail occurs K times in a row
- timeout breaker: `ACT_TIMEOUT_OBSERVATION` / `REVIEW_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION` occurs K times
- spend breaker: one step's tokens exceed X% of the budget
- human breaker: after the same gate is rejected/responded to, do not propose the same action again

For anything that can be expressed with `NoProgress`, use `NoProgress(key=...)` first. Common cases are implemented as `AdapterFailureBreaker` / `VerifyDetailBreaker` / `TimeoutMarkerBreaker` / `PerStepTokenCap`. Concrete examples are in [recipes/circuit-breakers.md](./recipes/circuit-breakers.md).

## Tracking

- Dashboard / summary: Issue #107
- Circuit breaker recipes: Issue #108
- Throttling design: Issue #109
- Spike detection: Issue #110
- Circuit breaker helpers: Issue #112
- Post-hoc spike scan: Issue #113
- Throttling helper primitives: Issue #114
- Static HTML dashboard: Issue #115

Related:

- [observability.md](./observability.md)
- [api-reference.md](./api-reference.md)
- [recipes/timeout-and-kill.md](./recipes/timeout-and-kill.md)
- [recipes/circuit-breakers.md](./recipes/circuit-breakers.md)
- [throttling.md](./throttling.md)
