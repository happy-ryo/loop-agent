> This file is a load-on-demand bundled copy of `docs/transport.md`. The canonical source is `docs/transport.md` in the repository.

# wake delivery transport and work-discovery

This document explains the delivery layer (transport), which delivers wake events for loop **completion / next iteration / decision requests** to other loops or entry points, and the input-selection layer (work-discovery), which decides "what to iterate on next" after a loop completes.
Both are implemented with only the stdlib and have zero dependencies.

## wake delivery transport (push first / pull fallback / at-most-once)

Phase 3 (report.md §3.3 / §4.6 / §5 Phase3 / Issue #23) adds a delivery layer that delivers wake events for loop **completion / next iteration / decision requests** to other loops or entry points (receivers). The claude-org runtime broker sidecar belongs to the runtime and cannot be reused directly, so loop-agent extracts only the **pattern** and implements it with **zero dependencies (stdlib only)**.

- **Push first / pull fallback**: if push (the immediate-response accelerator) succeeds, the wake is delivered immediately. If it does not, the wake remains in the queue and delivery continues through the receiver's **active polling (pull)**. Push is the accelerator; pull polling is the canonical delivery path. -> **delivery does not stop even when the backend is unavailable** (§5 Phase3 success condition b).
- **At-most-once through three-state claim-then-confirm**: `UNDELIVERED -> CLAIMED(lease, owner) -> DELIVERED`. A claim occupies the wake with a lease and returns it; the receiver finalizes it with confirm only after it has fully processed the wake. Rows whose leases expire before confirm become eligible again (delivery continues even if the receiver crashes = bias toward at-least-once. For idle-wake, loss is worse than duplication). Fencing with owner matching + lease-expiration checks closes the loss window where a wake could be marked `DELIVERED` even though it was never delivered (parallel polling assumes each worker passes a distinct owner). Confirmed wakes are never redelivered. The in-memory queue is thread-safe via RLock (preventing double claims during parallel polling).
- **De-dup by wake id**: each wake has a deterministic id (`{run_id}:{kind}:{iteration}`), and duplicate enqueue is a no-op. The receiver can de-dup by id across redelivery instructions from resume and duplicate delivery at the push/pull boundary (receivers are expected to use idempotent handlers).
- **Role-specific cadence**: in pull environments where push expires, "waiting" is translated into **active polling** instead of idle waiting. Receive triggers are designed asymmetrically by role (dispatcher 180s / worker 60s / secretary 0 = poll at the start of every turn). `cadence_for(role)` / `due_to_poll(role, last_poll, now)`.

```python
from loop_agent import (
    Transport, InMemoryWakeQueue, NullPushBackend, LoopWaker, run_loop, MaxIterations,
)

# Even if the backend is unavailable (no push-first path), delivery continues via pull fallback.
transport = Transport(InMemoryWakeQueue(), NullPushBackend())
waker = LoopWaker(transport, run_id="r1", recipient="coordinator", next_recipient="planner")

result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)])
waker.record_result(result)          # Deliver completion wake (+ next-iteration wake) -> push failure leaves it in the queue

# The receiver actively polls at the role cadence. Wakes still arrive even when push is down.
# poll_and_handle is a crash-safe receive loop that confirms only wakes whose handler succeeded
# (if it dies before processing, lease expiration causes redelivery = at-least-once. Receivers
# de-dup by wake.id with an idempotent handler).
transport.poll_and_handle("coordinator", lambda wake: handle(wake))
```

`PushBackend` has a best-effort `push(wake) -> bool` contract (`True` only for confirmed delivery; unavailable backends and exceptions are treated as `False` and left to pull fallback). Real backends (renga / broker CLI, etc.) implement this Protocol and are injected. `CallablePushBackend(fn)` lifts an arbitrary function, and `NullPushBackend` represents "push always fails (= backend unavailable)".

Receiving defaults to **claim-then-confirm**: `poll(recipient)` only claims wakes and does not finalize them (call `confirm_wakes(wakes, owner=...)` after processing finishes). Wakes that crash before processing are redelivered after lease expiration (idle-wake deliberately chooses **duplication over loss**). For the general case, `poll_and_handle(recipient, handler)` is recommended because it confirms each wake after its handler succeeds and avoids missed confirmation. Only simple process-local cases where the handler never fails should use `poll(recipient, confirm=True)` for immediate finalization (that path is at-most-once and can lose wakes if the process crashes after poll).

## backend extension points (WakeQueue / PushBackend Protocol)

`WakeQueue` (the source of truth for delivery) ships with three implementations, and `open_wake_queue(backend, **opts)` can create one by name (selecting a backend without changing the Public API. In-memory is the default; SQLite / Redis are explicit):

- **`InMemoryWakeQueue`** (`"memory"`, default) - an in-process implementation that is thread-safe via RLock. For delivery within a single process.
- **`SqliteWakeQueue`** (`"sqlite"` with `path` / `table`, etc. in `opts`) - a **persistent queue** that keeps wakes across process restarts. It is safe in cross-process configurations without depending on TTL locks (`BEGIN IMMEDIATE` does not expire in the middle of an operation). Confirmed rows can be cleaned up with `purge_delivered`.
- **`RedisWakeQueue`** (`"redis"` with `client` or `url` in `opts`) - stores the source of truth in Redis and delivers wakes across processes on different hosts. It requires the optional `redis` dependency (creation fails loudly if it is not installed).

`PushBackend` (the immediate-response accelerator) ships with stdlib-only implementations:

- **`NullPushBackend`** - represents "push always fails (= backend unavailable)". This is the default when you want plain pull fallback behavior.
- **`CallablePushBackend(fn)`** - a thin adapter that lifts any `push(wake) -> bool` function into a `PushBackend`.

```python
from loop_agent import Transport, open_wake_queue, NullPushBackend

queue = open_wake_queue("sqlite", path="wakes.db")   # Persistent queue (survives restarts)
transport = Transport(queue, NullPushBackend())       # Public API stays unchanged when the backend changes
```

Backends beyond the bundled ones (for example, a `PushBackend` that bridges wakes to external entry points or a broker / renga CLI) should be extended by **implementing and injecting the Protocol**. Any user implementation that conforms to the `WakeQueue` / `PushBackend` Protocols can be plugged in. The delivery semantics (how it biases at-most-once / at-least-once, claim-then-confirm fencing, and wake id de-dup) are fixed as Protocol contracts, so replacing the backend does not change the receiver-side assumption of idempotent handlers.

## work-discovery (input selection for the next iteration / propose-only / human gate preserved)

Phase 3 (report.md §3.5 / §4.6 / §5 Phase 3 success condition d) implements **input selection** for deciding "what to iterate on next" after a loop completes, using two layers: a **calculation layer (read-only and deterministic)** and a **delivery layer (human gate)**. The structure guarantees that "discovery can become more autonomous, but the decision to start remains with a human."

- **Calculation layer `triage(candidates, *, done=())`**: a pure function with no side effects and identical output for identical input. It triages candidates (`Candidate`) against `done` (the set of completed ids): **dependency resolution** (*ready* when every `depends_on` entry is in `done`), deterministic ranking by **priority descending -> effort ascending -> id ascending**, reasons for unsatisfied dependencies (waiting for known candidates / unknown ids), and **dependency-cycle detection**. It returns "N candidates + 1 recommendation" as `Triage`.
- **Delivery layer `WorkDiscovery`**: registers the triage result as a **proposal** in the human-gate registry in state.db (reusing the MVP `pending_decision`; `gate_key` is `discovery-<cycle>`). **It always stops here (propose-only)**: it never adopts anything fully automatically, and keeps the decision pending until a human decides acceptance or rejection through `resolve(...)` (= the same path as the limited human gate). Adoption mapping for the four decisions: `approve` -> adopt the recommendation / `edit` -> adopt a different *ready* candidate specified by the human (fail loudly if it is not ready) / `reject` -> adopt nothing / `respond` -> adopt nothing + record the response. Decisions are preserved across pause -> resume.
- **Completion-to-next-iteration connection `discover_next(...)`**: emits a proposal only when the previous `LoopResult` has **completed** (`paused` returns `None` = nothing has completed yet, so the human should resolve the gate first). It only registers a proposal (pending); it does not adopt it or start the next loop (**no fully automatic start**).

```python
from loop_agent import discover_next, WorkDiscovery, Candidate, LoopStore, connect

store = LoopStore(connect("state.db"))

# Given the completed loop result first, triage the next candidates -> proposal (pending in the human gate)
prop = discover_next(store=store, run_id="cycle", result=first, cycle=1,
                     candidates=[Candidate(id="t1", priority=9, payload={"goal": "X"}),
                                 Candidate(id="t2", depends_on=("t1",))])  # t2 is blocked waiting for t1
# prop.triage.recommended.id == "t1" / prop.pending["status"] == "pending" (zero adoption)

# The next iteration does not happen until a human decides acceptance or rejection (propose-only)
wd = WorkDiscovery(store, "cycle")
adoption = wd.resolve(1, "approve")     # or "edit"(payload=id)/"reject"/"respond"
# adoption.candidate.payload == {"goal": "X"} -> use this as gather input for the next loop
```

## `WorkListGather`: fairly cycling multiple items through one loop

**`WorkListGather`** (`loop_agent.discovery.work_list`, Issue #56): triage decides "what to run, and in what order"; `WorkListGather` is the `gather` hook responsible for "how to fairly cycle multiple adopted items through **one loop**." A naive gather that returns the first unfinished item lets one item monopolize `MaxIterations` and starves the rest. `WorkListGather` prevents starvation through fair scheduling (`round_robin` / `fewest_attempts` / `fifo` / `priority` / custom) + per-item limits + per-item done checks. attempts / done / exhausted are derived from `state.history` every time (**resume-safe** = no in-process counters).

```python
from loop_agent import WorkListGather, WorkListDrained, run_loop, MaxIterations

gather = WorkListGather(
    ["a.py", "b.py", "c.py"], strategy="fewest_attempts",
    max_attempts_per_item=3,                                  # Stop after 3 attempts for each item (exhausted)
    done_when=lambda item, rec: rec.observation["passed"],    # Whether this item is done
)
result = run_loop(act=act, verify=verify, gather=gather,
                  conditions=[WorkListDrained(gather), MaxIterations(50)])  # Stop when all items are done/exhausted
gather.report(result.state)   # WorkListProgress(done=..., exhausted=..., remaining=..., attempts=...)

# Delegate priority and ordering calculation to triage (take only ready candidates whose dependencies are resolved)
gather = WorkListGather.from_triage([Candidate(id="hi", priority=9), Candidate(id="lo")])
```

For details, see [recipes/multi-item-work-list.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/multi-item-work-list.md).

## Related

- [../README.md](https://github.com/happy-ryo/loop-agent/blob/main/README.md) - project entry point and navigation summary
- [persistence-and-resume.md](persistence-and-resume.md) - persistence layer for state.db / resume
- [safety.md](safety.md) - scope of the human gate (HumanGate) and safety templates
- [recipes/multi-item-work-list.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/multi-item-work-list.md) - practical recipe for multi-item loops
