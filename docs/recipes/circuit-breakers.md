# Circuit Breakers

Circuit breakers stop a loop when the same failure mode is repeating. They are
ordinary `StopCondition` objects or `NoProgress` projections; they are not a
separate runtime mode.

## Adapter Failures

Most adapters put structured details in `ActOutcome.observation`. For
`ClaudeCodeAct` / `CodexAct`, the result has a `failed` flag.

```python
from loop_agent import NoProgress

adapter_failed = NoProgress(
    window=3,
    repeat=3,
    key=lambda record: (
        "adapter_failed"
        if getattr(record.observation, "failed", False)
        else f"ok:{record.iteration}"
    ),
)
```

This fires only when the failing signature repeats. Successful or different
observations get distinct keys and do not trip the breaker.

## Verify Failures

Repeated verify detail is often a better signal than repeated observation,
because verify owns ground truth.

```python
verify_stuck = NoProgress(
    window=4,
    repeat=4,
    key=lambda record: ("verify_detail", record.detail),
)
```

Use this when a test runner, linter, or validator keeps returning the same
failure text after multiple attempts.

## Timeout Markers

`TimeoutPolicy(on_timeout="graceful")` records synthetic observations for timed
out seams. A breaker can stop after repeated timeout markers:

```python
from loop_agent import (
    ACT_TIMEOUT_OBSERVATION,
    VERIFY_TIMEOUT_OBSERVATION,
    NoProgress,
)

timeout_breaker = NoProgress(
    window=3,
    repeat=3,
    key=lambda record: (
        "timeout"
        if record.observation in {ACT_TIMEOUT_OBSERVATION, VERIFY_TIMEOUT_OBSERVATION}
        else f"not-timeout:{record.iteration}"
    ),
)
```

## Spend Breakers

For one-step spend spikes, use a small custom condition. Keep it explicit so the
application owns the threshold.

```python
from dataclasses import dataclass
from loop_agent import LoopState

@dataclass(frozen=True)
class PerStepTokenCap:
    limit: int
    name = "per_step_token_cap"

    def check(self, state: LoopState):
        if not state.history:
            return None
        latest = state.history[-1]
        if latest.tokens > self.limit:
            return f"step {latest.iteration} used {latest.tokens} tokens > {self.limit}"
        return None
```

Then compose it with the normal hard caps:

```python
conditions=[PerStepTokenCap(200_000), MaxIterations(20), Timeout(1800)]
```

## Human Gate Breakers

Human decisions live in `state.db`, not in the in-memory `LoopState`. Keep the
breaker at the gate policy layer: if a gate is rejected or responded to, do not
offer the same irreversible action again. The default `HumanGate` already keeps
decisions stable across pause/resume; application policy decides whether a
rejected action should be transformed, skipped, or escalated.

## Relationship to Spike Detection

`SpikeDetector` emits `loop_spike` events but does not stop the run. Use it for
visibility first. Promote a spike to a breaker only when the threshold is stable
enough to be application policy.
