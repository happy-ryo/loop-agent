> This file is a load-on-demand bundled copy of `docs/transport.md`. The canonical source is `docs/transport.md` in the repository.

# Wake Transport and Work Discovery

This document explains the transport layer that delivers loop **completion / next-iteration /
decision-request** wakes to other loops or intake points, and the work-discovery layer that selects
the input a completed loop should iterate on next. Both are implemented with the stdlib only and
zero dependencies.

## Wake Transport (Push-First / Pull Fallback / At-Most-Once)

Phase 3 (report.md §3.3 / §4.6 / §5 Phase3 / Issue #23) adds a transport layer that delivers loop
**completion / next-iteration / decision-request** wakes to other loops or intake points
(recipients). Because the claude-org runtime broker sidecar belongs to the runtime and cannot be
reused directly, loop-agent extracts only the **pattern** and implements it with **zero
dependencies (stdlib only)**.

- **Push-first / pull fallback**: if push (the low-latency accelerator) succeeds, the wake is
  delivered immediately. Even if it does not, the wake remains in the queue and delivery continues
  through the recipient's **active polling (pull)**. Push is the accelerator; pull polling is the
  canonical delivery path. Therefore, **delivery does not stop even when the backend is down**
  (§5 Phase3 success condition b).
- **At-most-once through three-state claim-then-confirm**: `UNDELIVERED → CLAIMED(lease, owner)
  → DELIVERED`. Claim reserves the lease and returns the wake; the recipient confirms only after
  finishing processing. Rows whose lease expires before confirm become eligible again (delivery
  continues even if the recipient crashes = bias toward at-least-once. For idle wakes, loss is worse
  than duplication). Fencing by owner match + lease-expiration check closes the loss window where a
  wake could become `DELIVERED` without having been received (parallel poll assumes each worker
  passes a distinct owner). Confirmed wakes are never redelivered. The in-memory queue is
  thread-safe via `RLock` (preventing double claims under parallel polling).
- **De-dup by wake id**: wakes have deterministic ids (`{run_id}:{kind}:{iteration}`), and duplicate
  enqueue is a no-op. Recipients can de-dup by id when resume requests redelivery or when push and
  pull meet at a delivery boundary (recipients are assumed to use idempotent handlers).
- **Role-specific cadence**: in pull environments where push expires, "waiting" is translated into
  **active polling** rather than idle waiting. Receive triggers are designed asymmetrically by role
  (dispatcher 180s / worker 60s / secretary 0 = poll at the start of every turn).
  `cadence_for(role)` / `due_to_poll(role, last_poll, now)`.

```python
from loop_agent import (
    Transport, InMemoryWakeQueue, NullPushBackend, LoopWaker, run_loop, MaxIterations,
)

# A configuration where delivery continues through pull fallback even when the backend is down
# (no push-first path).
transport = Transport(InMemoryWakeQueue(), NullPushBackend())
waker = LoopWaker(transport, run_id="r1", recipient="coordinator", next_recipient="planner")

result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)])
waker.record_result(result)          # Deliver completion wake (+ next-iteration wake) -> push failure leaves it queued

# The recipient actively polls according to its role cadence. Wakes still arrive even if push is down.
# poll_and_handle is a crash-safe receive loop that confirms only wakes whose handler succeeded
# (if it dies before processing, lease expiration causes redelivery = at-least-once. The recipient
# de-dups by wake.id with an idempotent handler).
transport.poll_and_handle("coordinator", lambda wake: handle(wake))
```

`PushBackend` has a best-effort `push(wake) -> bool` contract (`True` only for confirmed delivery;
outages and exceptions are treated as `False` and left to pull fallback). Real backends (renga /
broker CLI, etc.) implement this Protocol and are injected. `CallablePushBackend(fn)` lifts an
arbitrary function, and `NullPushBackend` represents "push always fails (= backend down)".

Receiving uses **claim-then-confirm** by default: `poll(recipient)` only claims wakes and does not
confirm them (call `confirm_wakes(wakes, owner=...)` after processing has fully completed). Wakes
claimed by a recipient whose process crashes after polling but before processing or confirming are
redelivered after lease expiration (for idle wakes, the design chooses **duplication over loss**).
For the general case where missed confirmations should be avoided, `poll_and_handle(recipient,
handler)` is recommended because it confirms each wake after the handler succeeds. Only simple
process-local cases where the handler never fails should use `poll(recipient, confirm=True)` for
immediate confirmation (that path is at-most-once and can lose wakes if the process crashes after
polling).

## Backend Extension Points (WakeQueue / PushBackend Protocol)

Three `WakeQueue` implementations (the source of truth for delivery) are included and can be
constructed by name with `open_wake_queue(backend, **opts)` (backend selection without changing the
Public API; in-memory is the default, SQLite / Redis are explicit):

- **`InMemoryWakeQueue`** (`"memory"`, default) - an in-process implementation that is thread-safe
  via `RLock`. For delivery within a single process.
- **`SqliteWakeQueue`** (`"sqlite"`, with `path` / `table` and other values in `opts`) - a
  **persistent queue** that keeps wakes across process restarts. In cross-process configurations it
  is safe without relying on TTL locks (`BEGIN IMMEDIATE` does not expire in the middle of an
  operation). Confirmed wakes can be cleaned up with `purge_delivered`.
- **`RedisWakeQueue`** (`"redis"`, with `client` or `url` in `opts`) - stores the source of truth in
  Redis and delivers wakes between processes on different hosts. It requires the optional `redis`
  dependency (construction fails loudly if it is not installed).

`PushBackend` (the low-latency accelerator) includes stdlib-only implementations:

- **`NullPushBackend`** - represents "push always fails (= backend down)". This is the default when
  you want to use the plain pull-fallback behavior.
- **`CallablePushBackend(fn)`** - a thin adapter that lifts any `push(wake) -> bool` function into a
  `PushBackend`.

```python
from loop_agent import Transport, open_wake_queue, NullPushBackend

queue = open_wake_queue("sqlite", path="wakes.db")   # Persistent queue (survives restarts)
transport = Transport(queue, NullPushBackend())       # Public API stays unchanged when backend changes
```

Backends beyond the bundled ones (such as a `PushBackend` that bridges wakes to an external intake
point, broker, or renga CLI) are extended by **implementing the Protocol and injecting it**. Any
user implementation that conforms to the `WakeQueue` / `PushBackend` Protocol can be plugged in.
The delivery semantics (how to bias at-most-once / at-least-once, claim-then-confirm fencing, wake
id de-dup) are fixed as Protocol contracts, so the recipient-side assumption of idempotent handlers
does not change when the backend is swapped.

## Work Discovery (Input Selection for the Next Iteration / Propose-Only / Human Gate Preserved)

Phase 3 (report.md §3.5 / §4.6 / §5 Phase 3 success condition d) implements **input selection** for
deciding what a completed loop should iterate on next as two layers: a **computation layer
(read-only and deterministic)** and a **delivery layer (human gate)**. The structure guarantees:
"increase discovery autonomy, but leave the decision to start work with the human."

- **Computation layer `triage(candidates, *, done=())`**: a pure function with zero side effects and
  identical output for identical input. It triages candidates (`Candidate`) against `done` (the set
  of completed ids): **dependency resolution** (*ready* when every `depends_on` entry is in
  `done`), deterministic ranking by **priority descending -> effort ascending -> id ascending**,
  reasons for unsatisfied dependencies (waiting on known candidates / unknown ids), and
  **dependency-cycle detection**. It returns "N candidates + 1 recommendation" as `Triage`.
- **Delivery layer `WorkDiscovery`**: registers the triage result as a **proposal** in the human-gate
  registry in state.db (reusing the MVP `pending_decision`; `gate_key` is `discovery-<cycle>`).
  **It always stops here (propose-only)**: it never adopts anything fully automatically, and the
  proposal remains pending until a human decides acceptance or rejection through `resolve(...)`
  (= the same path as the bounded human gate). The four-decision adoption mapping is:
  `approve` -> adopt the recommendation / `edit` -> adopt a different *ready* candidate specified
  by the human (fail loudly if it is not ready) / `reject` -> adopt nothing / `respond` -> adopt
  nothing + record the response. Decisions are preserved across pause -> resume.
- **Completion -> next-iteration connection `discover_next(...)`**: emits a proposal only when the
  previous `LoopResult` is **completed** (`paused` returns `None` = nothing has completed yet, so a
  human should resolve the gate first). It only registers the proposal (pending); it does not adopt
  it or start the next loop (**no fully automatic work start**).

```python
from loop_agent import discover_next, WorkDiscovery, Candidate, LoopStore, connect

store = LoopStore(connect("state.db"))

# Given the completed loop result first, triage the next candidates -> proposal (pending in the human gate)
prop = discover_next(store=store, run_id="cycle", result=first, cycle=1,
                     candidates=[Candidate(id="t1", priority=9, payload={"goal": "X"}),
                                 Candidate(id="t2", depends_on=("t1",))])  # t2 is blocked waiting on t1
# prop.triage.recommended.id == "t1" / prop.pending["status"] == "pending" (zero adoption)

# The next iteration does not happen until a human decides whether to adopt it (propose-only)
wd = WorkDiscovery(store, "cycle")
adoption = wd.resolve(1, "approve")     # or "edit"(payload=id)/"reject"/"respond"
# adoption.candidate.payload == {"goal": "X"} -> use this as gather input for the next loop
```

## `WorkListGather`: Fairly Cycling Multiple Items Through One Loop

**`WorkListGather`** (`loop_agent.discovery.work_list`, Issue #56): triage decides "what to run and
in what order"; `WorkListGather` is the `gather` hook responsible for "how to cycle multiple adopted
items **fairly through one loop**." A naive gather that returns the first unfinished item lets one
item monopolize `MaxIterations` and starves the rest. `WorkListGather` prevents starvation with
fair scheduling (`round_robin` / `fewest_attempts` / `fifo` / `priority` / custom) + per-item limits
+ per-item done checks. `attempts` / `done` / `exhausted` are derived from `state.history` every
time (**resume-safe** = no in-process counters).

```python
from loop_agent import WorkListGather, WorkListDrained, run_loop, MaxIterations

gather = WorkListGather(
    ["a.py", "b.py", "c.py"], strategy="fewest_attempts",
    max_attempts_per_item=3,                                  # Stop after 3 attempts for one item (exhausted)
    done_when=lambda item, rec: rec.observation["passed"],    # Whether this item is done
)
result = run_loop(act=act, verify=verify, gather=gather,
                  conditions=[WorkListDrained(gather), MaxIterations(50)])  # Stop when all items are done/exhausted
gather.report(result.state)   # WorkListProgress(done=..., exhausted=..., remaining=..., attempts=...)

# Delegate priority and ordering computation to triage (take only ready candidates whose dependencies are satisfied)
gather = WorkListGather.from_triage([Candidate(id="hi", priority=9), Candidate(id="lo")])
```

Details: [recipes/multi-item-work-list.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/multi-item-work-list.md).

## Related

- [../README.md](https://github.com/happy-ryo/loop-agent/blob/main/README.md) - project entry point and flow summary
- [persistence-and-resume.md](persistence-and-resume.md) - persistence layer for state.db / resume
- [safety.md](safety.md) - scope of the human gate (`HumanGate`) and safety template
- [recipes/multi-item-work-list.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/multi-item-work-list.md) - practical recipe for multi-item loops
