# Opt-In Throttling

Throttling is application policy. loop-agent provides the measurements and
injection points, but `run_loop` does not sleep, pause, change models, or lower
budgets by default.

## Prerequisites

Use these before adding automatic control:

- `loop-agent summary` for a read-only overview of run state.
- `loop-agent dashboard --output dashboard.html` for static HTML inspection.
- `SpikeDetector` / `loop_spike` events and `loop-agent spikes` for token,
  latency, timeout, and repeated failure signals.
- explicit `StopCondition` / circuit breaker recipes for known bad patterns.

## Launch Throttling

Launch throttling decides whether to start a new run. Keep it outside
`run_loop`:

- query `state.db` for currently `running` runs,
- inspect recent `loop_spike` events,
- refuse or delay launching a new task when the application threshold is hit.

This belongs in the scheduler, cron job, MCP server, or web app that creates
runs.

Use `launch_throttle_decision(...)` for the pure decision:

```python
from loop_agent import launch_throttle_decision

decision = launch_throttle_decision(
    running=active_runs,
    max_running=4,
    recent_spikes=spikes_last_hour,
    max_recent_spikes=10,
)
if not decision.allow:
    return f"delay launch: {decision.reason}"
```

## Step Throttling

Step throttling delays the next iteration. Prefer explicit policy in `gather` or
the outer scheduler:

- `gather` can return no work until a cooldown expires,
- `Transport` can leave a wake queued until a recipient polls later,
- a wrapper around `act` can sleep before invoking the expensive adapter.

Do not hide sleeps inside the core loop; hidden sleeps make tests, timeouts, and
resume behavior harder to reason about.

When an application explicitly wants a delay before a costly `act`, wrap it with
an injected sleep function:

```python
from loop_agent import step_throttle

act = step_throttle(expensive_act, delay_seconds=5.0, sleep=time.sleep)
```

## Model Throttling

Model throttling is a routing decision for the `act` seam. Use a wrapper such as
`ModelLadder` or your own adapter policy:

- default to a cheaper model,
- escalate only on hard tasks,
- de-escalate after token or latency spikes,
- keep token accounting in the returned `ActOutcome`.

The loop core only sees `act(context) -> ActOutcome`.

## Budget Throttling

Budget throttling is already expressible with stop conditions:

- lower `TokenBudget` for a run,
- add `Timeout` for wall-clock caps,
- add a custom per-step token cap when one call should not consume too much.

Changing budgets during a run should be explicit. If the application wants
adaptive budgets, make the budget condition read from an external policy object
or launch a new run with a new config.

## Boundary

loop-agent should provide:

- event/state surfaces,
- opt-in detectors,
- stop-condition composition,
- transport and adapter injection points,
- recipes showing safe policy shapes.

Applications should own:

- thresholds,
- whether a spike stops, pauses, delays, or only alerts,
- model selection business rules,
- scheduler-level concurrency limits.
