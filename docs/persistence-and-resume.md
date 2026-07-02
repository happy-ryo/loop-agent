# Persistence and Resume (progress file / state.db SoT / resume)

LoopAgent persists loop state externally and can resume an interrupted loop from the middle without losing state. This guide explains the progression from a minimal progress file (JSONL), to `state.db` (SQLite) as the MVP Single Source of Truth, and then to resume (#14).

## Minimal State (Progress File)

The minimal persistent state appends one JSON Lines record per iteration to an external file. Pass `ProgressLog.on_step` to `run_loop` as `on_step`, then append one final line with the termination reason after the loop finishes. Because each line is a complete record for one iteration, iterations up to the last completed one can be read back even if the process crashes midway. This is the smallest predecessor of the state.db SoT.

```python
from loop_agent import run_loop, ProgressLog, read_progress

progress = ProgressLog("progress.jsonl")
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)],
                  on_step=progress.on_step)
progress.record_result(result)               # Append the termination reason (a "result" line)

records = read_progress("progress.jsonl")     # Per-iteration "step" lines + final "result" line
```

## Loop State SoT (state.db)

In the MVP (report.md §3.4 / §4.6 / §5 Phase 2), loop state is externalized into a **single SQLite SoT**. `connect` creates the **minimal schema** for loops, consisting of only four tables (`run` / `step` / `event` / `stop_reason`), and each step is **persisted atomically with `transaction`**. This was adapted from claude-org-ja `tools/state_db`, but is extracted as a **self-contained schema** with no dependency on the org system itself (projects / workstreams / snapshotter, etc.). This keeps it loosely coupled, as described in report.md §6.

`DBProgressLog` has the **same `on_step` / `record_result` signatures** as the JSONL `ProgressLog`, so it is a drop-in replacement. The SoT can be moved to the DB by only replacing the observation hook; the `run_loop` signature is unchanged.

```python
from loop_agent import run_loop, DBProgressLog, MaxIterations

with DBProgressLog("state.db", run_id="my-run") as db:   # Reserve the run row + loop_begin
    result = run_loop(act=act, verify=verify,
                      conditions=[MaxIterations(5)],
                      on_step=db.on_step)                 # Atomically persist each iteration
    db.record_result(result)                             # Finalize termination state + stop_reason
```

Low-level API:

```python
from loop_agent import connect, LoopStore

store = LoopStore(connect("state.db"))
state = store.load_or_init("my-run")     # New runs get an empty LoopState; existing runs are restored from steps
store.read_steps("my-run")               # Per-iteration step rows (with observation decoded)
store.read_events("my-run")              # Journal (loop_begin / loop_step / loop_end)
store.get_stop_reason("my-run")          # Triggered stop condition or achieved goal
```

**Interrupt → resume (resume, #14)**. Pass a `LoopState` restored from persisted steps to `run_loop(initial_state=...)` to continue an interrupted loop from the middle without losing state. The iteration counter, accumulated cost, `elapsed`, and history are carried forward, and `elapsed` continues accumulating from the persisted value. `DBProgressLog.state` is that restored result (for a new run, it is empty, which is equivalent to a fresh start), so new and resumed runs can use the same wiring:

```python
db = DBProgressLog("state.db", "my-run")   # For an existing run, restore state from steps
result = run_loop(act=act, verify=verify, conditions=[GoalMet(verifier), MaxIterations(100)],
                  initial_state=db.state,   # Continue from the interruption point (new runs use empty state)
                  on_step=db.on_step)
db.record_result(result)
```

Resume is meaningful when combined with **state-based stop conditions**, such as hooks that decide from state (`GoalMet`, for example). Across processes, the act/verify hooks are recreated and their internal call counters are not restored. If the decision is derived from the gathered state, the same judgment can be reproduced in the new process and the resumed result will match a continuous run.

> **Observation type fidelity (a resume limitation)**. The `observation` values in `history` restored from state.db are JSON round-tripped values from the time they were saved (`tuple→list` / int keys in dicts → str / set, custom types, and NaN → repr strings). Conditions that use raw `observation` values directly as *keys* (especially the default key for `NoProgress`) may see values change across a resume boundary (`tuple` becomes an unhashable `list`). If you need exact resume equivalence, use JSON-stable observations or pass `NoProgress(key=...)` a projection to a JSON-stable signature.

**JSONL and DB coexist**. `ProgressLog` (JSONL) remains as a zero-dependency PoC artifact that can be read easily, while `DBProgressLog` (state.db) becomes the state SoT from the MVP onward. Both share the same observation hook convention.

Persistence for each step bundles the "`step` row + `run` aggregate + `loop_step` event" into **one transaction**, so no partial rows remain if the process dies before commit. `UNIQUE(run_id, iteration)` makes re-persisting the same iteration idempotent, which keeps replay safe during resume.

The case where **multiple processes resume the same run at the same time** (mutual exclusion through an in-progress lease, #21) is covered in [safety.md](./safety.md) as the same kind of safety boundary as HumanGate pause/resume.

## state.db Compatibility Contract

From `1.0.0` onward, state.db is loop-agent's stable persistence format. The unit of compatibility is behavior observable through the public APIs (`connect` / `LoopStore` / `DBProgressLog` / `ReflexionStore` / `DBReflexionLog`), not the internal details of the SQL.

- **patch release**: Bug fixes, index additions, and read-only query improvements that can read and write existing DBs without destructive changes.
- **minor release**: Additive table, column, and event kind additions. Existing DBs must remain readable through migration.
- **major release**: Schema changes that make existing DBs unreadable without migration, semantic changes to existing columns, or incompatible changes to the resume contract.

`PRAGMA user_version` indicates the schema generation. Migrations inspect the actual schema and run idempotently without destroying existing steps, events, or pending decisions. Because `observation` is JSON round-tripped, type fidelity limits such as `tuple→list` are treated as stable specification. If exact-match resume decisions are required, use JSON-stable observations or a stable signature for `NoProgress(key=...)`.

## Related

- [README](../README.md) — Overview and navigation
- [transport.md](./transport.md) — SQLite / Redis backends and state.db storage location
- [observability.md](./observability.md) — loop_begin / loop_step / loop_end events and OTel spans
- [safety.md](./safety.md) — HumanGate and simultaneous multi-process resume (in-progress lease, #21)
- [stability.md](./stability.md) — The `1.0.0` stability contract
