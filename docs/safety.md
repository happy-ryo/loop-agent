# Safety Mechanisms (Runaway Prevention / Limited Human Gate)

This document explains LoopAgent's two-layer safety mechanism. The lower layer is the runaway guard, which "always stops on composite stop conditions." The upper layer is the limited HumanGate, where humans approve only irreversible operations.

## Runaway Guard

The minimal implementation faithful to report.md §4.4 / §5 Phase 1 runs `gather -> act -> verify -> repeat` while evaluating **composable hard limits** (`MaxIterations` / `TokenBudget` / `Timeout`) with OR semantics. Reaching a limit is returned as a **control output with a reason** (`LoopResult`), not as an exception, and preserves which condition fired and why.

There are two layers of safety guarantees.

- **Mechanical limits (guaranteed termination)**: `MaxIterations` / `TokenBudget` / `Timeout` are evaluated with OR semantics through `AnyOf` composition. Sandbox tests prove that the loop always stops at a limit even when the goal is unmet, progress is absent, or actions repeat (`tests/test_runaway_guard.py`). The aim is to prevent AutoGPT-style runaway execution and cost explosions through structure.
- **Dual termination conditions (semantic stops)**: In addition to mechanical limits, `GoalMet` (successful termination after achieving a verifiable goal) and `NoProgress` (termination after detecting no progress or repeated actions) are placed in the same `AnyOf` composition.

This addresses the requirement in report.md R3 (infinite-loop prevention). If no stop condition is supplied, startup is rejected with `ConfigError` because the configuration has no condition guaranteed to fire. See [api-reference.md](./api-reference.md) and [seams.md](./seams.md) for the complete condition API and composition semantics.

## Limited Human Gate (approve/edit/reject/respond only for irreversible operations)

In the MVP (report.md §4.5 / R6 / Principle 8 / §5 Phase 2 success criterion c), the human gate is limited to actions that are **irreversible or have a large impact radius**; it is not applied to every step. It has the same four decision types as LangGraph's `interrupt()` - **approve / edit / reject / respond** - and **persists** decisions in state.db so they are retained across **pause -> resume**. It reuses claude-org's `org-escalation` + `pending_decisions` state machine with roles reinterpreted: "the secretary registers a worker's request for judgment and resolves it with the user's response" becomes "the loop registers an irreversible action and the human resolves it."

`HumanGate` fires **between** `gather` and `act` (= after an action is proposed and before side effects occur). It reviews only actions for which `on(action)` returns `True`; reversible actions pass through. If the decision is unresolved, `run_loop` returns with `status="paused"`. After a human records the decision, rerunning with the **same `run_id`** applies the persisted decision and continues without asking about the same action twice.

```python
from loop_agent import run_loop, HumanGate, LoopStore, connect, MaxIterations

store = LoopStore(connect("state.db"))
gate = HumanGate(on=lambda a: a == "deploy",   # Irreversibility predicate (large impact radius only)
                 store=store, run_id="my-run")

# run1: pause before the irreversible action (the decision is persisted as pending)
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=gate)
# result.paused is True / result.pending["gate_key"] == "gate-0"

# A human records the decision (a separate process/connection is also allowed)
store.resolve_decision("my-run", "gate-0", "approve")          # or "edit"/"reject"/"respond"

# run2: rerun with the same run_id -> apply the persisted approve decision and continue (no second pause)
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=HumanGate(on=..., store=store, run_id="my-run"))
```

- **approve** -> execute the proposed action as-is / **edit** -> execute the human-replaced action
  (`resolve_decision(..., "edit", payload=replacement action)`) / **reject** -> do not execute, record the rejection
  as one step, and continue / **respond** -> do not execute, record the human response as one step, and continue
  (the next `gather` can consume the response through `state.history[-1]`).
- When the human is present in a single process, passing `HumanGate(..., resolver=fn)` resolves inline without pausing
  (`fn(pending) -> Decision`). `run_gated_loop(...)` is a thin entry point that wires the `HumanGate`
  configuration into `run_loop`. `active=False` disables the gate completely.
- The decision registry is stored in the `pending_decision` table in state.db (`UNIQUE(run_id, gate_key)` makes it idempotent),
  and gate firing, decision, and execution are recorded as `loop_gate` events in the journal.
- **Resume contract (irreversible actions are exactly-once within the loop)**: The gate key is determined from
  `state.iteration` at review time, so it is stable under both resume models.
  - **`initial_state` resume (#14, recommended)**: Passing the interrupted `LoopState`
    (`store.load_or_init(run_id)` / `DBProgressLog.state`) to `run_loop(initial_state=...)` restores `iteration` /
    `tokens_used` / `elapsed` / `history` and **continues** from the interruption point. `TokenBudget` / `Timeout`
    work correctly across runs, and `history`-dependent `gather` remains consistent with the first run. On resume,
    the first encountered "interrupted gate" receives the correct iteration-based key, which matches the persisted decision.
  - **replay resume (without `initial_state`)**: A backward-compatible mode that replays from iteration 0 with fresh state.
    Irreversible actions that were **executed** after approve/edit are finalized as `executed`, so replay skips them and
    **does not execute them twice** (preventing accidental duplicate deploys and similar failures). However, cumulative
    counters appear reset for the previous run, and skip placeholders for already executed gates can cause
    `history`-dependent `gather` to diverge. Therefore, this mode assumes that **non-gated actions are idempotent and the
    proposed action sequence is deterministic with respect to iteration**. Use `initial_state` resume when cumulative
    cross-run limits or history-dependent resume behavior are required.
- **Coordination for concurrent resume across multiple processes (in-progress lease, #21)**: Multiple processes may resume
  the same `run_id` *at the same time*. For approve/edit irreversible actions, only one process obtains execution rights
  through the multi-stage `pending -> resolved -> executing -> executed` lifecycle and an **in-progress lease**
  (`acquire_lease` performs a single-winner `resolved -> executing` transition with `lease_owner` / `lease_expires_at`).
  - **exactly-once + ordering consistency**: Only one process can successfully transition `resolved -> executing`. Losers
    that review the same gate while it is executing (`executing` and not expired) **pause until `executed`**, so they do
    not run subsequent iterations before the winner completes the irreversible action. After `act` completes (after the
    step is persisted), the winner finalizes `executed` with `complete_execution`.
  - **Winner crash recovery**: If the winner crashes during `act` and the lease expires (`lease_expires_at <= now`), another
    waiting process can resume, reacquire the lease (`took_over`), and complete execution. Because the step row is persisted
    *before* completion is finalized (the driver calls `GateReview.on_complete` after `on_step`), the step is not lost even
    if the winner crashes.
  - **Trade-off**: Expiration takeover reruns `act`, so in the rare case where the winner crashes *after causing the side
    effect but before finalizing `executed`*, the side effect is duplicated (**at-least-once**). Full exactly-once requires
    an idempotency key on the side-effecting system, which is outside this module's scope. Setting `lease_ttl` comfortably
    longer than the maximum duration of the irreversible action avoids expiration takeover itself. In production, include
    an `idempotency_key` (for example, `run_id:gate_key`) in action payloads such as deploys, external API calls, and ticket
    updates, and make the receiver treat repeated executions with the same key as no-ops. loop-agent's lease preserves
    ordering consistency inside the loop, but exactly-once behavior in the external world is achieved only in combination
    with idempotency on the external side-effecting system.
  The lease owner defaults to an automatically generated per-process unique token (explicit injection is also available with
  `HumanGate(owner=...)`). Concurrent resume exactly-once behavior, ordering consistency, and crash recovery are demonstrated
  by `tests/test_concurrent_resume.py` (simulated concurrent processes).
- Passing a `paused` result to `record_result` leaves the run as `running` and does not write `stop_reason`, so it can
  continue on resume. Each step's source of truth remains in the `step` rows, which should be used for auditing.

## Safety Template

The recommended minimal safety template for self-improvement workflows is to isolate **act to editing only, with commit outside the loop**. This principle applies to every act adapter (`ClaudeCodeAct` / `CodexAct` / custom adapters implementing the ActHook Protocol), but the concrete knobs for narrowing tool permissions differ by adapter: `ClaudeCodeAct` narrows `allowed_tools` to editing tools, while `CodexAct` uses `sandbox` (for example, `"read-only"` / `"workspace-write"`) or `allowed_args` to block commit/push. In all cases, keep irreversible operations in a human step outside the loop.

```python
# verify is ground truth (pytest exit code). Use two limits that guarantee termination.
# Do not allow the act subprocess to commit/push (the gate cannot see subprocess-internal operations).
result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # Editing only
    verify=verify_with_pytest,
    conditions=[MaxIterations(20), Timeout(3600)],
)
# After convergence, a human reviews and runs commit / push (= irreversible operations are isolated outside the loop).
```

> **Note the scope of HumanGate (important)**: `HumanGate` reviews the **discrete loop action** returned by `gather`; it cannot see `git commit` or similar operations executed internally by the `act` subprocess (for example, `claude --print`). The gate fires between `gather` and `act`. Therefore, to truly gate irreversible operations, either (1) do not let the act subprocess commit / push (narrow `allowed_tools` to editing tools) and make commit / push a **human step outside the loop**, or (2) make `gather` propose commit as a **discrete loop action**, catch it with `on`, and let `act` execute it. The [limited Human Gate section](#limited-human-gate-approveeditrejectrespond-only-for-irreversible-operations) above is the canonical example of (2) (`on=lambda a: a == "deploy"`).

## Related

- [README](../README.md) - overall picture, positioning, and seam overview
- [seams.md](./seams.md) - detailed seam specification and types (conditions / gate / act boundaries)
- [api-reference.md](./api-reference.md) - complete API for stop conditions and HumanGate
- [persistence-and-resume.md](./persistence-and-resume.md) - persistence contract for state.db / resume
