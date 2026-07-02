# Recipe: Rotate N Items Fairly in One Loop (WorkListGather)

Flaky test stabilization, bulk translation, and cross-cutting refactors all have the same shape: **process N independent items** one after another in a single loop. `WorkListGather` (`loop_agent.discovery.work_list`, Issue #56) is the reusable component that normalizes that kind of `gather`.

## Why a Naive gather Breaks Down

`gather` is only a hook that returns "what to do next" (`Callable[[state], ctx]`). When cycling through N items, the most straightforward implementation looks like this:

```python
def gather(state):
    return next(f for f in files if f not in done)   # Return the first unfinished item.
```

This **gets stuck when one item fails verification repeatedly**. If the first file, `a.py`, cannot be fixed, `gather` keeps returning `a.py` on every iteration and burns the entire `MaxIterations` budget on that file alone. `b.py` and `c.py` are never touched, so they starve and the loop ends. This is the trap we hit in the self-translation PoC (#37).

## What WorkListGather Provides

| Feature | What it solves |
|---|---|
| **Fair scheduling** (`round_robin` / `fewest_attempts` / `fifo` / `priority` / custom) | Rotates the order so one item cannot monopolize the loop |
| **Per-item limit** (`max_attempts_per_item`) | Stops retrying an unfixable item after a defined number of attempts (*exhausted*) and leaves the remaining budget for other items |
| **Done predicate hook** (`done_when`) | Determines whether *this item* is complete independently from the loop-wide `verify` |
| **Attempt counter / progress API** (`attempts` / `report` / `remaining`) | Reads attempts, completed items, and remaining items from `state` |
| **Triage integration** (`from_triage`) | Delegates priority calculation for which items to process and in what order to the existing `triage` machinery |

## Prose Intent (Pass Directly to a Coding Agent)

> Translate the docstrings in the three specified files under `src/` into English. If translation gets stuck on any one file, try that file three times and then give up on it, but make sure the remaining files are still handled; one file's failure must not block the others. Treat a file as complete when the remaining source-language count reaches zero and that file's relevant tests pass.

## Assembled Harness

```python
from loop_agent import (
    WorkListGather, WorkListDrained, run_loop, ActOutcome, VerifyOutcome, MaxIterations,
)

FILES = ["src/a.py", "src/b.py", "src/c.py"]

def act(ctx):                          # Default build_ctx is dict {"id","attempt","priority","payload"}.
    obs = translate_and_test(ctx["id"])  # Translate one file -> run tests.
    return ActOutcome(observation={"file": ctx["id"], "passed": obs.passed}, tokens=obs.tokens)

def verify(outcome):                   # Loop-wide goal. Optional; if stopping on drained, it can stay unmet.
    return VerifyOutcome(goal_met=False)

gather = WorkListGather(
    FILES,
    strategy="fewest_attempts",        # Start with the fewest attempts (= fair round-robin).
    max_attempts_per_item=3,           # Stop after three attempts for a file.
    done_when=lambda item, rec: rec.observation["passed"],   # Is this item complete?
)

result = run_loop(
    act=act, verify=verify, gather=gather,
    conditions=[WorkListDrained(gather), MaxIterations(50)],
)

report = gather.report(result.state)
print("done:", report.done, "exhausted:", report.exhausted)   # exhausted = items given up on
```

## Key Points

- **Always stop with `WorkListDrained`.** Once every item is either done or exhausted, `gather` has no item to return and yields `DRAINED`. The stopping condition, not `gather`, stops the loop. Stopping conditions are evaluated at the *start* of each iteration, before `gather`, so if `WorkListDrained` is included in `conditions`, the loop stops as soon as the work list is drained, before `gather` is called and before `DRAINED` can be passed to `act`. Include `MaxIterations` as a safety limit.
- **`done_when` is separate from `verify`.** `verify` represents the *loop-wide* goal, while `done_when` represents completion for *each item*. In a multi-item loop, the natural pattern is to decide whether a single file is complete with `done_when(item, record)`, keep `verify` fixed as unmet (`goal_met=False`), and delegate termination to `WorkListDrained`.
- **Bake the done signal into `observation`.** `done_when` receives only the `StepRecord`. Put a **JSON-native completion flag** such as `{"passed": bool}` in the `act` observation, then have `done_when` read it. During resume in another process, `observation` round-trips through JSON, so use plain bools and strings rather than drift-prone types such as `tuple` or `set`; this is the same contract noted by the loop core resume behavior.
- **How to choose a strategy:**
  - `fewest_attempts` (default) - Selects the item with the fewest attempts. It smooths work across the list without letting repeatedly failing items drag the whole loop along. Use this when unsure.
  - `round_robin` - Cycles strictly in list order. Use this when every item should receive an equal turn.
  - `priority` - Handles higher-priority items first in strict descending `WorkItem(priority=...)` order, with fairness only among items at the same priority. Use this when important items should finish first.
  - `fifo` - The naive version that returns the first unfinished item. Pairing it with `max_attempts_per_item` reduces starvation, but it is less fair than the other strategies.
  - custom callable - Accepts `ScheduleContext` (`selectable` / `attempts` / `last_selected` ...) and returns one item. Use this for custom priority logic.
- **Compose with ModelLadder.** The default ctx includes `attempt` (the number of previous attempts for this item), so `act` can inspect `ctx["attempt"]` and escalate from haiku to sonnet to opus. You can omit `build_ctx` for that case. If you need a custom shape, return JSON-native data, for example `build_ctx=lambda item, attempt, st: {"id": item.id, "attempt": attempt}`.
- **Resume-safe, but only with the same gatherer.** `WorkListGather` does not keep in-process counters. On every call, it replays `state.history` to derive attempts, done items, and exhausted items. If an interrupted run resumes from `initial_state`, it reproduces the same schedule from the same `state`. **Assumption**: attribution is determined by replaying with the current `items`, `strategy`, and `max_attempts_per_item`; `StepRecord` does not structurally store the dispatched item. Therefore, resume is limited to restarting the *same* gatherer with the same `state`. Feeding old history to a differently configured gatherer silently misattributes steps to different items; it does not crash.
- **Use `item_of` when composing with a human gate.** `WorkListGather` derives "which record belongs to which item" by replaying the schedule; by default, the offered item is treated as the acted item. If you insert `run_loop(gate=...)`, those can diverge: when the gate returns `GATE_SKIP` (reject/respond), no `act` runs but a record is still appended, and when an edit swaps in an action for another item, the record belongs to that other item. To handle this correctly, pass `item_of=lambda rec: ...`, returning the actual item id from the record or `None` when nothing executed. Then attempts, done status, and `max_attempts_per_item` attach to the actual item, so an item that did not run is not incorrectly marked *exhausted*, and an edited record is not attributed to the offered item. Fairness is measured by offer count, so an item that keeps being skipped moves back in the rotation and `fewest_attempts`, `round_robin`, and `priority` rotate to other items (`fifo` remains naive and does not rotate). In a standard loop without a gate, the offer and record are 1:1, so `item_of` is unnecessary.
- **Use `from_triage` when there are dependencies.** If items depend on each other, such as "`b` must run after `a`", declare those dependencies with `Candidate` and use `WorkListGather.from_triage(candidates, done=...)`. It imports only candidates that are **ready**, meaning their dependencies are satisfied, in triage ranking order. When a dependency is resolved, call it again with the current `done` set to create a new gatherer. Because the item set changes at that point, **start from a new `LoopState` without carrying over old history**. The correct behavior is for triage to exclude completed items and for newly ready items to start at attempt 0.
