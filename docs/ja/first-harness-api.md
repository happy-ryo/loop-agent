# First Harness API

This page is the shortest import map for a first production-style loop. The
full public surface stays documented in [stability.md](./stability.md) and
[api-reference.md](./api-reference.md). When a coding agent is choosing
production harness helpers, use [ai-api-map.md](./ai-api-map.md) as the
capability map. This page is only the daily starting surface.

## The Daily Surface

Start with the `CORE_API` shape. In code, import only the symbols you need:

```python
from loop_agent import (
    ActOutcome,
    VerifyOutcome,
    MaxIterations,
    Timeout,
    TokenBudget,
    run_loop,
)
```

They cover the core shape:

| Need | Use |
|---|---|
| Run the loop | `run_loop` |
| Return work output from `act` | `ActOutcome` |
| Return the machine verdict from `verify` | `VerifyOutcome` |
| Bound attempts | `MaxIterations` |
| Bound wall-clock at iteration boundaries | `Timeout` |
| Bound reported model cost | `TokenBudget` |

That is enough for the first harness: write `gather` only when the next action
depends on state; otherwise omit it and the loop runs on a single default context.

```python
from loop_agent import ActOutcome, MaxIterations, Timeout, VerifyOutcome, run_loop


def act(_ctx):
    return ActOutcome(observation="did one bounded unit of work")


def verify(outcome):
    return VerifyOutcome(
        goal_met="done" in str(outcome.observation),
        detail="machine-checkable signal was absent",
    )


result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(5), Timeout(300)],
)
```

## Add Helpers Only When Needed

After the first loop works, add one practical `HARNESS_API` helper at a time:

| Need | Import |
|---|---|
| Verify with an existing command | `CommandVerifier` |
| Verify with pytest | `PytestVerifier` |
| Verify with a text signal | `RegexVerifier` |
| Persist steps and resume | `DBProgressLog` |
| Run a coding-agent CLI as `act` | `loop_agent.adapters.ClaudeCodeAct` or `loop_agent.adapters.CodexAct` |
| Gate a discrete irreversible action | `HumanGate` |
| Fairly schedule N items | `WorkListGather`, `WorkListDrained` |

These helpers do not own policy. They only make the seams easier to write. The
caller still decides what to gather, how to act, what ground truth means, and
where irreversible operations are allowed.

## What To Ignore At First

Do not start with Reflexion, transport, operations dashboards, notifier backends,
or custom evaluator APIs unless the harness already needs them. They are stable
surfaces for advanced composition, not prerequisites for a first loop.

The upgrade path is:

1. `run_loop` with a mechanical cap.
2. A sharp `verify` based on a command, test, AST check, or regex.
3. Persistence with `DBProgressLog` if the run can outlive one process.
4. A gate or work-list scheduler only when the domain demands it.

This keeps the first decision path small while preserving the full public API for
larger applications.
