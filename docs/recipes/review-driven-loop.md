# Review-driven Loop

Use this pattern when an LLM-backed `act` step changes files and tests alone are
not enough to judge scope, design fit, or release risk.

The stable core loop remains:

```text
gather -> act -> verify -> repeat
```

Until a first-class `ReviewHook` is designed, model review as explicit work items
or as a verify substage:

```text
gather finding -> act fix -> review check -> verify tests -> repeat
```

## Recommended Shape

- `gather`: returns one review finding or one file/task to fix.
- `act`: performs the edit, typically with `ClaudeCodeAct`, `CodexAct`, or a local
  patch function.
- `review`: implemented as deterministic checks, a separate LLM reviewer, or a
  human review adapter. It should produce actionable feedback.
- `verify`: runs ground-truth checks such as tests, build, packaging, schema
  checks, or AST checks.
- `HumanGate`: stays reserved for irreversible actions such as commit, push,
  release tags, deploys, or destructive operations.

The key distinction is that review evaluates the post-act artifact, while
`HumanGate` approves whether an irreversible action may run.

## Minimal Skeleton

```python
from loop_agent import ActOutcome, MaxIterations, VerifyOutcome, WorkItem
from loop_agent import WorkListDrained, WorkListGather, run_loop

items = [
    WorkItem(id="api-contract", payload={"finding": "classify every __all__ export"}),
    WorkItem(id="artifact-scope", payload={"finding": "remove one-off local artifacts"}),
]


def done_when(_item, record):
    obs = record.observation
    return isinstance(obs, dict) and obs.get("review_passed") and obs.get("verified")


gather = WorkListGather(items, strategy="fifo", done_when=done_when)


def act(ctx):
    # Apply the fix for ctx["finding"]. This can call an LLM adapter or a local tool.
    return ActOutcome(observation={"finding": ctx["finding"], "changed": True})


def review(outcome):
    # Review the changed artifact. Return structured feedback, not just pass/fail.
    obs = outcome.observation
    return {"review_passed": True, "feedback": "", **obs}


def verify(outcome):
    reviewed = review(outcome)
    tests_pass = True  # Replace with pytest/build/schema checks.
    return VerifyOutcome(
        goal_met=False,
        detail="reviewed+verified" if reviewed["review_passed"] and tests_pass else "needs work",
    )


result = run_loop(
    gather=gather,
    act=act,
    verify=verify,
    conditions=[WorkListDrained(gather), MaxIterations(10)],
)
```

## When to Use LLM Review

LLM review is useful for scope, naming, migration risk, and documentation
coherence. It should not replace ground-truth verification or human PR review.
Prefer deterministic checks for invariants that can be encoded mechanically.

## Current Design Status

This is an optional pattern, not a stable public `review=` API. Track the API
design in Issue #128.
