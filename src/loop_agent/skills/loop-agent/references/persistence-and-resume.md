> This file is a load-on-demand bundled copy of `docs/persistence-and-resume.md`. The canonical source is `docs/persistence-and-resume.md` in the repository.

# Persistence and Resume (progress file / state.db SoT / resume)

LoopAgent can persist loop state externally and resume an interrupted loop from the middle without losing state. This document explains the progression from the minimal progress file (JSONL), to `state.db` (SQLite) as the MVP Single Source of Truth, and then to resume (#14).

## Minimal State (progress file)

This is the minimal persistent state: records for each iteration are appended to an external file as JSON Lines. Simply pass `ProgressLog.on_step`
to `run_loop` as `on_step`, and after termination append one line for the termination reason. Because one line is one complete
record for one iteration, iterations up to the point immediately before a crash can be read back (the minimal predecessor to the state.db SoT).

```python
from loop_agent import run_loop, ProgressLog, read_progress

progress = ProgressLog("progress.jsonl")
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)],
                  on_step=progress.on_step)
progress.record_result(result)               # Append the termination reason (the "result" line)

records = read_progress("progress.jsonl")     # "step" lines per iteration + trailing "result" line
```

## Loop State SoT (state.db)

In the MVP (report.md §3.4 / §4.6 / §5 Phase 2), loop state is moved out to a **single SQLite SoT**.
`connect` creates the **minimal schema** for loops (only the four tables `run` / `step` / `event` / `stop_reason`),
and each step is **persisted atomically with `transaction`**. This was adapted from claude-org-ja's `tools/state_db`,
but it has been separated as a **self-contained schema** that has **no dependency at all** on the main org system
(projects / workstreams / snapshotter, etc.; loose coupling = report.md §6).

`DBProgressLog` is a drop-in replacement with **the same `on_step` / `record_result` signatures** as the JSONL
`ProgressLog`, so the SoT can be moved to the DB simply by swapping the observation hook (`run_loop`'s signature is unchanged).

```python
from loop_agent import run_loop, DBProgressLog, MaxIterations

with DBProgressLog("state.db", run_id="my-run") as db:   # Ensure the run row + loop_begin
    result = run_loop(act=act, verify=verify,
                      conditions=[MaxIterations(5)],
                      on_step=db.on_step)                 # Persist each iteration atomically
    db.record_result(result)                             # Finalize terminal state + stop_reason
```

Low-level API:

```python
from loop_agent import connect, LoopStore

store = LoopStore(connect("state.db"))
state = store.load_or_init("my-run")     # New runs get an empty LoopState; existing runs are restored from steps
store.read_steps("my-run")               # Per-iteration step rows (with observation decoded)
store.read_events("my-run")              # Journal (loop_begin / loop_step / loop_end)
store.get_stop_reason("my-run")          # Triggered stop condition or goal achieved
```

**Interrupt -> resume (resume, #14)**. When the `LoopState` restored from persisted steps is passed to
`run_loop(initial_state=...)`, an interrupted loop can continue from the middle without losing state
(the iteration counter, accumulated cost, `elapsed`, and history are carried over, and `elapsed` continues accumulating
from the persisted value). `DBProgressLog.state` is that restored result (empty for a new run = equivalent to a fresh start),
so new runs and resumed runs can use the same wiring:

```python
db = DBProgressLog("state.db", "my-run")   # For an existing run, restore state from steps
result = run_loop(act=act, verify=verify, conditions=[GoalMet(verifier), MaxIterations(100)],
                  initial_state=db.state,   # Continue from the interruption point (new runs have empty state)
                  on_step=db.on_step)
db.record_result(result)
```

Resume is meaningful when combined with **state-based stop conditions** (hooks such as `GoalMet` that decide from state).
Across process boundaries, the act/verify hooks are recreated, but their internal call-count counters are not restored.
If decisions are derived from the gathered state, the same decision can be reproduced in the new process, and the resumed
result matches a continuous run.

> **Type fidelity of observation (resume limitations)**. The `observation` in `history` restored from state.db
> becomes the value produced by round-tripping the JSON saved at persistence time (`tuple->list` / dict
> int keys->str / set, custom types, and NaN->repr string). Conditions that use the raw `observation` directly
> as a *key* (especially the default key for `NoProgress`) may see the value change across the resume boundary
> (`tuple` becomes an unhashable `list`). If exact-match resume is required, use JSON-stable observations,
> or pass `NoProgress(key=...)` a projection to a JSON-stable signature.

**JSONL and the DB coexist**. `ProgressLog` (JSONL) remains as a zero-dependency readable PoC artifact,
while `DBProgressLog` (state.db) becomes the state SoT from the MVP onward. Both share the same observation hook convention.

Persistence for each step **bundles the `step` row + `run` aggregate + `loop_step` event into one transaction**,
so even if the process dies before commit, no partial rows remain (crash tolerance). `UNIQUE(run_id, iteration)` makes
re-persisting the same iteration idempotent (replay safety during resume).

The case of **resuming the same run concurrently from multiple processes** (exclusion via an in-progress lease, #21)
is covered in [safety.md](safety.md) as the same kind of safety boundary as HumanGate pause/resume.

## state.db Compatibility Contract

From `1.0.0` onward, state.db is loop-agent's stable persistence format. Compatibility is defined by behavior observable
through the public APIs (`connect` / `LoopStore` / `DBProgressLog` / `ReflexionStore` / `DBReflexionLog`), not by internal SQL details.

- **patch release**: Bug fixes, index additions, and read-only query improvements that can read and write existing DBs non-destructively.
- **minor release**: Additive additions of tables, columns, or event kinds. Existing DBs must remain readable via migration.
- **major release**: Schema changes that make existing DBs unreadable without migration, semantic changes to existing columns, or incompatible changes to the resume contract.

`PRAGMA user_version` indicates the schema generation. Migrations inspect the actual schema and run idempotently without destroying existing steps / events / pending decisions. Because `observation` is JSON round-tripped, type-fidelity limits (such as tuple->list) are treated as stable behavior. When exact-match resume decisions are required, use JSON-stable observations or a stable signature for `NoProgress(key=...)`.

## Related

- [README](https://github.com/happy-ryo/loop-agent/blob/main/README.md) - Overview and navigation
- [transport.md](transport.md) - SQLite / Redis backends and where state.db is stored
- [observability.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/observability.md) - loop_begin / loop_step / loop_end events and OTel spans
- [safety.md](safety.md) - HumanGate and concurrent multi-process resume (in-progress lease, #21)
- [stability.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/stability.md) - Stable contract for `1.0.0`
