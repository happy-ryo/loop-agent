# Recipe: Per-Call Timeout / Kill for act / review / verify (Issue #42)

This mechanism stops only a single runaway `act` / `review` / `verify` call (a model call or tool execution) **without giving up on the whole loop**. Pass it to the `timeout=` argument of `run_loop` / `async_run_loop`. The full implementation lives in the async-first core (`_drive_loop`), so it applies with the **same behavior to both the sync and async APIs**.

> **Difference from the whole-run `Timeout` *stop condition***: `loop_agent.Timeout(seconds)` is a stop condition that limits cumulative wall-clock time **at iteration boundaries** and **does not interrupt a step already in progress** ("do not start new work after the deadline has passed"). The `timeout=` described in this recipe interrupts **one call**. It is a separate mechanism, and you can use both together.

## Usage

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations

result = run_loop(
    act=my_act, verify=my_verify,
    conditions=[MaxIterations(20)],
    # Stop act after 30s, review after 20s, and verify after 10s.
    # Give up on the timed-out seam and continue to the next iteration.
    timeout=TimeoutPolicy(act=30.0, review=20.0, verify=10.0, on_timeout="graceful"),
)
```

- `TimeoutPolicy(act=, review=, verify=, default=, on_timeout=)`: per-seam timeout values in seconds. Each seam uses its own value; if absent, it uses `default`; if that is also absent, it is unlimited.
- `timeout=30.0` (a bare number of seconds): shorthand that applies **graceful** handling with `default=30.0` to every timed seam.
- `timeout=None` (the default): no per-call timeout, preserving the previous zero-overhead path.

## Two Modes

| `on_timeout` | Behavior on timeout |
|---|---|
| `"graceful"` (default) | Give up on the affected seam, record a **synthetic step** with `goal_met=False`, and continue to the **next iteration**. The loop keeps returning normally and does not raise an exception. |
| `"kill"` | Cancel the affected seam and raise `SeamTimeout` **out of the loop**. No `LoopResult` is returned. |

For a graceful timeout, the synthetic step's observation is a marker string (`ACT_TIMEOUT_OBSERVATION` / `REVIEW_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`). This value is JSON-native and hashable, so it remains **stable through persistence / resume**, and `NoProgress` can detect "consecutive timeouts" using its default key (the observation itself):

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations, NoProgress

# Stop as "no progress" if act times out 3 times in a row.
result = run_loop(
    act=flaky_slow_act, verify=my_verify,
    conditions=[MaxIterations(100), NoProgress(window=3, repeat=3)],
    timeout=TimeoutPolicy(default=30.0, on_timeout="graceful"),
)
assert result.stop.name in ("no_progress", "max_iterations")
```

Use kill when you want to stop explicitly:

```python
from loop_agent import async_run_loop, TimeoutPolicy, SeamTimeout, MaxIterations

try:
    result = await async_run_loop(
        act=call_model, verify=run_tests,
        conditions=[MaxIterations(20)],
        timeout=TimeoutPolicy(act=60.0, on_timeout="kill"),
    )
except SeamTimeout as e:
    print(f"{e.seam} was stopped after {e.seconds}s")
```

`SeamTimeout` is part of the unified `LoopError` hierarchy and derives from `StateError` (#71). Therefore, in addition to `except SeamTimeout` (where you can read the `seam` / `seconds` attributes), you can also catch it with `except StateError` or `except LoopError`. Before #71 it was a bare `Exception`; this change only broadens the catch paths, and existing `except SeamTimeout` handlers are unchanged. See [../errors.md](../errors.md) for the full hierarchy.

## Known Limitations (Platform Differences): Read This

The mechanism that **actually interrupts** a per-call timeout depends on the seam type and platform.

| Seam | Interruption mechanism | Portability |
|---|---|---|
| **async** (`async def` / returns an awaitable) | Real cancellation at await points via asyncio task cancellation (`asyncio.wait` + `task.cancel()`) | **All platforms** (as long as there is an event loop; i.e. `async_run_loop`) |
| **sync** (POSIX main thread) | Real interruption via `SIGALRM` (`signal.setitimer`) | **Main thread only** on Linux / macOS |
| **sync** (Windows / non-main thread) | Forced interruption is **not possible** | - |

In environments where `SIGALRM` is unavailable (Windows or a non-main thread), blocking synchronous seam calls cannot be forcibly stopped. Therefore:

- **`graceful`**: timeout detection is best-effort and happens **after the call returns**. It **cannot constrain a truly hung call** such as `time.sleep`. If you need reliable enforcement, make the seam **async** (for example, wrap synchronous work with `run_in_executor`).
- **`kill`**: for a synchronous seam, `UnsupportedTimeoutKill` is raised **before** entering the call. The policy is to fail with an explicit error rather than silently hang on a hard kill that cannot be enforced. Async seams are fine because they can be stopped with asyncio task cancellation. `UnsupportedTimeoutKill` derives from `ConfigError` in the unified `LoopError` hierarchy (#71, because it is a seam/environment configuration mismatch), so it can also be caught with `except ConfigError` / `except LoopError`. It also keeps `RuntimeError` as a base class for backward compatibility, so handlers for the pre-#71 bare `RuntimeError` continue to work.

Additional notes:

- **Async cancellation is cooperative**. On timeout, the seam's task is cancelled (`asyncio.CancelledError` at the next await point), and the timeout is reported **immediately without waiting** for cleanup. The decision is based on whether the task is pending at the deadline, so kill still works even if a seam swallows `CancelledError` and returns a value, and the loop does not hang. An `asyncio.TimeoutError` raised by the seam itself is different and propagates as-is. A seam that swallows `CancelledError` and never completes merely leaks as an **orphan background task** (the loop does not block), so do not catch and ignore `CancelledError` in seams where timeout enforcement matters. A **synchronous blocking section** inside an `async def` seam also cannot be interrupted until the next await.
- **The per-call budget is singular**. The deadline applies to the entire call. Even for an unusual seam that "blocks synchronously, then returns an awaitable", the real time spent in the synchronous section is subtracted from the await section's budget (`asyncio.wait`); if the synchronous section has already used up the deadline, it times out immediately. The total is constrained by one deadline and does not become roughly twice as long.
- **Use with human gates**: if a gated action (`GATE_PROCEED` + `on_complete`) times out under `graceful`, the lease is marked **executed** after recording the synthetic step. That action is consumed exactly once and is not retried. Under `kill`, the lease remains in progress (`SeamTimeout` exits before completion is confirmed), so it expires and another process can reclaim it through the crash-recovery path.
- **Deadlines use real wall-clock time**. The `SIGALRM` / `asyncio.wait` budget does not look at the loop's injected `time_fn`. `time_fn` only affects stop-condition clocks and non-`SIGALRM` post-hoc measurement (detecting completed synchronous calls after the fact).
- **`SIGALRM` is not reentrant** because it uses the process-wide `ITIMER_REAL`. Avoid setting another `SIGALRM`-based timeout inside a synchronous seam that already has this timeout enabled. If the embedding application has installed its own `ITIMER_REAL`, this mechanism **restores the previous timer** when the call finishes (re-arming the remaining time, so it is effectively paused during the call).
- A synchronous seam interrupted by kill can raise `BaseException` at an arbitrary point, so side effects may be left in an intermediate state. This is a general property of preemptive timeouts. If you choose `kill`, treat that step as failed.
