> This file is a bundled on-demand copy of `docs/safety.md`. The repository's canonical source is `docs/safety.md`.


# Safety Mechanisms: Runaway Prevention and the Limited Human Gate

This document explains LoopAgent's two-layer safety mechanism. The lower layer is a runaway guard that stops the loop when any composed stop condition is triggered. The upper layer is a limited `HumanGate` that asks a human to approve irreversible operations only.

<a id="runaway-guard"></a>

## Runaway Guard

The minimal implementation, aligned with report.md §4.4 / §5 Phase 1, runs `gather → act → verify → repeat` while evaluating **composable hard limits** (`MaxIterations` / `TokenBudget` / `Timeout`) with OR semantics. Reaching a limit is not reported as an exception. Instead, the loop returns a **controlled result with a reason** (`LoopResult`) that records which condition was triggered and why.

This mechanism provides two safety guarantees.

- **Mechanical limits (always stop)**: `MaxIterations` / `TokenBudget` / `Timeout` are evaluated with OR semantics through `AnyOf` composition. Sandbox tests prove that the loop always stops at a limit, even when the goal remains unmet, no progress is made, or actions repeat (`tests/test_runaway_guard.py`). This structure prevents AutoGPT-style runaway behavior and cost spikes.
- **Dual termination conditions (semantic stop)**: In addition to mechanical limits, `GoalMet` (the verifiable goal has been achieved = successful termination) and `NoProgress` (no progress or repeated-action detection = cutoff) are included in the same `AnyOf` composition.

This satisfies report.md R3 (infinite-loop prevention). If no stop condition is supplied, startup is rejected with `ConfigError`; configurations that do not include at least one condition guaranteed to trigger are rejected before execution starts. For the full condition API and composition semantics, see [api-reference.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/api-reference.md) and [seams.md](seams.md).

<a id="limited-human-gate-irreversible-actions-only-approveeditrejectrespond"></a>

## Limited Human Gate (approve/edit/reject/respond only for irreversible operations)

In the MVP (report.md §4.5 / R6 / Principle 8 / §5 Phase 2 success condition c), the human gate applies only to actions that are **irreversible or have a large blast radius**, rather than to every step. It supports the same four decisions as LangGraph's `interrupt()` — **approve / edit / reject / respond** — and **persists** decisions in state.db so they are **retained across pause → resume**. It reuses claude-org's `org-escalation` + `pending_decisions` state machine by remapping its roles: "the secretary registers a worker's request for judgment and resolves it from the user's response" becomes "the loop registers an irreversible action and a human resolves it."

`HumanGate` fires **between** `gather` and `act` (= after an action has been proposed and before side effects occur). It reviews only actions for which `on(action)` is `True`; reversible actions pass through. If no decision has been resolved, `run_loop` returns with `status="paused"`. After a human records the decision, rerunning with the **same `run_id`** applies the persisted decision and continues. The same action is not submitted for review again.

```python
from loop_agent import run_loop, HumanGate, LoopStore, connect, MaxIterations

store = LoopStore(connect("state.db"))
gate = HumanGate(on=lambda a: a == "deploy",   # Irreversible check (large blast radius only)
                 store=store, run_id="my-run")

# run1: pause before the irreversible action (the decision is persisted as pending)
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=gate)
# result.paused is True / result.pending["gate_key"] == "gate-0"

# A human records the decision (a different process/connection is also fine)
store.resolve_decision("my-run", "gate-0", "approve")          # or "edit"/"reject"/"respond"

# run2: rerun with the same run_id -> apply the persisted approve and continue (no second pause)
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(10)],
                  gather=gather, gate=HumanGate(on=..., store=store, run_id="my-run"))
```

- **approve** → execute the proposed action as-is / **edit** → execute the replacement action supplied by the human
  (`resolve_decision(..., "edit", payload=replacement_action)`) / **reject** → do not execute; record the rejection as
  one step and continue / **respond** → do not execute; record the human response as one step and continue (the next
  `gather` can ingest the response through `state.history[-1]`).
- If the human is present in the same process, pass `HumanGate(..., resolver=fn)` to resolve inline without pausing
  (`fn(pending) -> Decision`). `run_gated_loop(...)` is a thin entry point that wires a `HumanGate` configuration into
  `run_loop`. `active=False` disables the gate entirely.
- The decision registry is stored in the `pending_decision` table in state.db (idempotent via `UNIQUE(run_id, gate_key)`),
  and gate firing, decision resolution, and execution are recorded as `loop_gate` events in the journal.
- **Resume contract (irreversible actions are exactly-once)**: The gate key is determined from `state.iteration` at
  review time and remains stable across both resume models.
  - **`initial_state` resume (#14, recommended)**: Passing the interrupted `LoopState` (`store.load_or_init(run_id)` /
    `DBProgressLog.state`) to `run_loop(initial_state=...)` restores `iteration` / `tokens_used` / `elapsed` /
    `history` and **continues** from the interruption point. `TokenBudget` / `Timeout` work correctly across runs, and
    `history`-dependent `gather` remains consistent with the first run. On resume, the first "interrupted gate" that is
    encountered receives the correct iteration-based key and matches the persisted decision.
  - **Replay resume (without `initial_state`)**: This backward-compatible mode replays from iteration 0 with fresh
    state. Irreversible actions that were **executed** after approve/edit are finalized as `executed`, and replay skips
    them so they are **not executed twice** (preventing accidental double deployments and similar incidents). However,
    cumulative totals appear to reset for the previous run, and skip placeholders for already executed gates can cause
    divergence in `history`-dependent `gather`; therefore, this mode assumes that **non-gated actions are idempotent and
    the proposal sequence is deterministic with respect to iteration**. Use `initial_state` resume if you need
    cumulative limits across runs or history-dependent resume behavior.
- **Coordination for concurrent multi-process resume (in-progress lease, #21)**: Multiple processes may resume the same
  `run_id` *concurrently*. For approve/edit irreversible actions, only one process obtains execution rights through a
  multi-stage `pending → resolved → executing → executed` flow and an **in-progress lease** (a single-winner
  `resolved → executing` transition by `acquire_lease` + `lease_owner` / `lease_expires_at`).
  - **Exactly-once + order consistency**: Only one process can successfully transition `resolved → executing`. Losers
    that review the same gate while execution is in progress (`executing` and not expired) **pause until `executed`**,
    so they do not run subsequent iterations before the winner's irreversible action completes. After `act` completes
    (after step persistence), the winner finalizes `executed` with `complete_execution`.
  - **Winner crash recovery**: If the winner crashes during `act` and the lease expires (`lease_expires_at ≤ now`), a
    waiting process reacquires the lease on resume (`took_over`) and completes execution. Because the step row is
    persisted *before* execution is finalized (the driver calls `GateReview.on_complete` after `on_step`), the step is
    not lost even if the winner crashes.
  - **Trade-off**: Lease reacquisition reruns `act`, so in the rare case where the winner crashes *after causing the side
    effect but before finalizing `executed`*, the side effect can be duplicated (**at-least-once**). True exactly-once
    requires an idempotency key on the side-effecting system (outside this module's scope). Set `lease_ttl` sufficiently
    longer than the maximum expected duration of the irreversible action to avoid unintended lease reacquisition. In
    production, include an `idempotency_key` (for example, `run_id:gate_key`) in action payloads such as deploy /
    external API / ticket update, and make the receiving system treat repeated executions with the same key as no-ops.
    LoopAgent's lease preserves ordering inside the loop, but exactly-once behavior in the external world is achieved
    only when combined with idempotency on the external side.
  By default, the lease owner is an automatically generated token that is unique to each process (`HumanGate(owner=...)` can
  also inject it explicitly). Concurrent-resume exactly-once behavior, order consistency, and crash recovery are
  demonstrated in `tests/test_concurrent_resume.py` (simulated concurrent processes).
- Passing a `paused` result to `record_result` leaves the run as `running` and does not write `stop_reason` (so resume can
  continue). The authoritative record of each step remains in the `step` rows, which should be used for auditing.

<a id="safety-template"></a>

## Safety Template

For self-improvement workflows, the recommended minimal safety template restricts **act to edits only, with commits outside the loop**. This principle applies to every act adapter (`ClaudeCodeAct` / `CodexAct` / custom adapter (ActHook Protocol)), but the concrete controls for restricting tool permissions differ by adapter: `ClaudeCodeAct` narrows `allowed_tools` to editing tools, while `CodexAct` uses `sandbox` (for example, `"read-only"` / `"workspace-write"`) and `allowed_args` to block commit/push. In all cases, keep irreversible operations as a human step outside the loop.

```python
# verify is ground truth (pytest exit code). Two limits ensure the loop stops.
# Do not allow the act subprocess to commit/push (the gate cannot see subprocess-internal operations).
result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # edits only
    verify=verify_with_pytest,
    conditions=[MaxIterations(20), Timeout(3600)],
)
# After convergence, a human reviews and runs commit / push (= irreversible operations are isolated outside the loop).
```

> **Important: HumanGate scope**: `HumanGate` reviews the **discrete loop action** returned by `gather`; it cannot see `git commit` or similar operations executed internally by the `act` subprocess (for example, `claude --print`) because the gate fires between `gather` and `act`. Therefore, to gate irreversible operations reliably, either (1) prevent the act subprocess from committing/pushing (restrict `allowed_tools` to editing tools) and make commit / push a **human step outside the loop**, or (2) have `gather` propose commit as a **discrete loop action**, catch it with `on`, and let `act` execute it. The [Limited Human Gate section](#limited-human-gate-irreversible-actions-only-approveeditrejectrespond) above is the canonical example of (2) (`on=lambda a: a == "deploy"`).

<a id="related"></a>

## Related

- [README](https://github.com/happy-ryo/loop-agent/blob/main/README.md) — overview, positioning, and seam overview
- [seams.md](seams.md) — detailed seam specification and types (boundaries for conditions / gate / act)
- [api-reference.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/api-reference.md) — complete API for stop conditions and HumanGate
- [persistence-and-resume.md](persistence-and-resume.md) — persistence contract for state.db / resume
