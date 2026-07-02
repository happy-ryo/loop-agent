# Recipes — How to build coding-agent-driven loops (Path E)

This directory contains concrete examples for **Path E** (coding-agent driven). Each recipe has the following shape:

1. **prose intent** — Natural-language instructions that can be passed directly to Claude Code (or Cursor / Codex).
2. **resulting harness** — The approximate shape of the `gather / act / verify / conditions / gate` code that the coding agent writes.
3. **key points** — Task-specific pitfalls and tips for writing a sharp `verify` function against ground truth.

The common design principle is simple: **write `verify` against machine-checkable ground truth** (pytest exit codes, ASTs, string scans, and so on). If success is delegated to an LLM-as-judge, the loop converges on pretending it succeeded.
## Canonical production harnesses

If you need a production starting point, start with [production-harnesses.md](./production-harnesses.md). It narrows the catalog to three first-choice shapes:

| Harness | Use when | Primary docs |
|---|---|---|
| Single verified edit loop | One bounded task has one machine oracle. | [production-harnesses.md](./production-harnesses.md#1-single-verified-edit-loop) |
| Multi-item work queue | N independent items need fair scheduling and per-item caps. | [production-harnesses.md](./production-harnesses.md#2-multi-item-work-queue) |
| Gated irreversible action flow | The loop may deploy, publish, push, or mutate external state. | [production-harnesses.md](./production-harnesses.md#3-gated-irreversible-action-flow) |

The rest of this directory is supporting material for those shapes.

| Recipe | Task type | Ground truth for `verify` |
|---|---|---|
| [production-harnesses.md](./production-harnesses.md) | Selection guide for representative production harnesses | single verified edit / multi-item / gated irreversible action |
| [flaky-test-stabilization.md](./flaky-test-stabilization.md) | Stabilizing flaky tests (N items) | After the fix, the target tests pass N consecutive times |
| [translation.md](./translation.md) | Bulk translation of docstrings/comments (N files) | 0 instances of the target language in translated regions + unchanged AST + relevant tests pass |
| [refactor.md](./refactor.md) | Behavior-preserving refactor (N modules) | All existing tests pass + behavior equivalence at the AST level |
| [multi-item-work-list.md](./multi-item-work-list.md) | Fairly process N items in one loop (cross-cutting) | Apply each recipe's `verify` unchanged per item |
| [self-maintenance.md](./self-maintenance.md) | Small consistency fixes in loop-agent itself | stale wording scan + docs link + pytest |
| [review-driven-loop.md](./review-driven-loop.md) | Post-act review for LLM-backed edits | review approval + ground-truth verify |
| [circuit-breakers.md](./circuit-breakers.md) | Stop repeated failures early | `NoProgress` / custom `StopCondition` |

> The first filter for "is this task a good fit for loop-agent?" is **whether `verify` can be written sharply**. Tasks where it cannot, such as "make this writing better" or other goals that cannot be judged mechanically, should be triaged out by the coding agent as a matter of discipline.

## Fairness in multi-item loops (note for all recipes)

All of the recipes above are multi-item loops that process N items. With a naive `gather` implementation that returns the first unfinished item, one item can monopolize `MaxIterations` when it fails `verify` repeatedly, starving the rest. `WorkListGather` (`loop_agent.discovery.work_list`, Issue #56) standardizes this pattern: it lets you inject **fair scheduling + per-item limits + a done-check hook** as `gather`:

```python
from loop_agent import WorkListGather, WorkListDrained, run_loop, MaxIterations

gather = WorkListGather(
    ["a.py", "b.py", "c.py"],
    strategy="fewest_attempts",     # round-robin: pick from the fewest attempts
    max_attempts_per_item=3,        # stop each item individually so one item cannot monopolize the loop
    done_when=lambda item, rec: rec.observation["passed"],   # whether this item is done
)
result = run_loop(
    act=my_act, verify=my_verify, gather=gather,
    conditions=[WorkListDrained(gather), MaxIterations(50)],  # stop when drained
)
```

For detailed construction patterns and strategy selection, see **[multi-item-work-list.md](./multi-item-work-list.md)**. You can do the same thing with a handwritten round-robin expression such as `min(rem, key=lambda x: (attempts[x], items.index(x)))`, but `WorkListGather` takes over attempt-counter management, the done set, and resume safety.

## Stop a runaway single call (per-call timeout / kill)

When a single `act` / `review` / `verify` call runs away because the model thinks too long or a tool hangs, you can terminate just that one call **without giving up on the entire loop**. Pass a `TimeoutPolicy` to the `timeout=` argument of `run_loop` / `async_run_loop` (`graceful` = abandon that call and continue to the next iteration; `kill` = raise `SeamTimeout`). This is separate from the whole-run `Timeout` *stop condition*, which does not interrupt an in-progress step.

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations

result = run_loop(
    act=my_act, verify=my_verify, conditions=[MaxIterations(20)],
    timeout=TimeoutPolicy(act=30.0, review=20.0, verify=10.0, on_timeout="graceful"),
)
```

For syntax, modes, and **platform differences (hard kill for sync seams is available only on the POSIX main thread; Windows raises an explicit error)**, see **[timeout-and-kill.md](./timeout-and-kill.md)**.
