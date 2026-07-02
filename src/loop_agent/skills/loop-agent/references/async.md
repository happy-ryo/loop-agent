> This file is a load-on-demand bundled copy of `docs/async.md`. The canonical source is `docs/async.md` in the repository.

# async/await support (async_run_loop)

LoopAgent provides two entry points: synchronous `run_loop` and asynchronous `async_run_loop`. The control-flow implementation is unified, and the synchronous API remains fully preserved (Issue #40).

## Relationship between sync and async

| | `run_loop` | `async_run_loop` |
|---|---|---|
| Invocation | `run_loop(...)` | `await async_run_loop(...)` |
| Use case | Run directly from synchronous code | You are already inside an event loop / at least one seam is a coroutine function |
| Arguments | `act` / `review` / `verify` / `conditions` / `gather=...` / `on_step=...` / `gate=...` / `time_fn=...` / `initial_state=...` / `timeout=...` | Same (completely identical) |
| Return value | `LoopResult` | `LoopResult` |
| Seams | **Synchronous callables only** (passing an async seam raises `AsyncSeamInSyncLoop`) | Accepts both synchronous callables and async callables; mixing is allowed |

`async_run_loop` is the **single source of truth** for the loop body (gather -> act -> review? -> verify -> repeat), and synchronous `run_loop` uses a manual synchronous driver over the shared coroutine. When the loop is composed only of synchronous hooks, the shared coroutine never actually awaits anything (`maybe_await` returns non-awaitable values as-is), and `run_loop` runs it to completion in the caller's own context. Because it does not create an event loop or wrap the execution in an `asyncio.Task`, `contextvars` propagation, hook exception types, and per-call overhead exactly match the behavior before async support was introduced. In other words, **synchronous hooks have no additional cost**.

`run_loop` internally calls `async_run_loop` with `_strict_sync=True`. If any seam (`act` / `review` / `verify` / each `conditions` `check` / `gate.review` / `on_step` / `on_complete`) returns an awaitable during that call, `loop_agent.errors.AsyncSeamInSyncLoop` (both a `LoopError` and a `RuntimeError`) is raised immediately and directs the caller to use `await async_run_loop(...)`. The original synchronous `run_loop` did not accept async seams either, so this is not a regression; it is a clear and consistent error.

## Basic example: running an async act

```python
import asyncio

from loop_agent import (
    ActOutcome,
    MaxIterations,
    VerifyOutcome,
    async_run_loop,
)


async def act(ctx):
    # Example: await asynchronous I/O (HTTP / DB / LLM call) here
    await asyncio.sleep(0)
    return ActOutcome(observation="did something", tokens=1)


async def verify(outcome):
    await asyncio.sleep(0)
    met = outcome.observation == "did something"
    return VerifyOutcome(goal_met=met, detail="converged" if met else "")


async def main():
    result = await async_run_loop(
        act=act,
        verify=verify,
        conditions=[MaxIterations(10)],
    )
    print(result.status, result.iterations)


asyncio.run(main())
```

When you want to run the asynchronous loop exactly once from synchronous code, the canonical usage is to drive `async_run_loop(...)` with `asyncio.run`, as shown in `asyncio.run(main())` above. The test suite also drives coroutines this way without depending on pytest-asyncio.

## Mixing synchronous and asynchronous seams

Each of `gather` / `act` / `review` / `verify` / each `conditions` `check` / `gate.review` / `on_step` (and the gate's `on_complete`) may be **either a plain synchronous callable or an async callable that returns an awaitable**. The driver awaits each result through `loop_agent._async.maybe_await`, so the two styles can be mixed freely (for example, async `gather` + synchronous `act` + async `verify`). Synchronous hooks return non-awaitable values, so they are used as-is without adding any await overhead.

```python
def gather(state):              # synchronous
    return state

async def act(ctx):             # asynchronous
    await asyncio.sleep(0)
    return ActOutcome(observation=None, tokens=1)

result = await async_run_loop(
    act=act,
    verify=verify,              # may be either synchronous or asynchronous
    conditions=[MaxIterations(5)],
    gather=gather,
)
```

For details about each seam's type and contract, see [seams.md](seams.md).

## Concurrent execution of multiple loops (asyncio.gather)

`async_run_loop` itself does not perform concurrent processing internally. To keep the gather -> gate -> act -> review? -> verify order and the timing of stop-condition evaluation exactly aligned with the synchronous loop, it awaits each seam **sequentially**. If you want to run multiple independent loops concurrently, schedule them as tasks from the caller.

```python
results = await asyncio.gather(
    async_run_loop(act=act_a, verify=verify_a, conditions=[MaxIterations(10)]),
    async_run_loop(act=act_b, verify=verify_b, conditions=[MaxIterations(10)]),
)
# Or run them individually with asyncio.create_task(async_run_loop(...))
```

Each call owns its own `LoopState`, so concurrent runs do not interfere with each other.

Note: `time_fn` remains a **synchronous** monotonic clock; it is only read and is not awaited. If a synchronous `act` blocks, it blocks the entire event loop. When sharing the loop with other tasks, wrap truly blocking work in an async hook plus `loop.run_in_executor`.

## Per-call timeout and async seams

The **per-call** timeout for `act` / `review` / `verify` is specified by passing a `TimeoutPolicy` (or a bare number of seconds) to `timeout=` on `run_loop` / `async_run_loop` (Issue #42). The mechanism used for actual interruption differs depending on whether the seam is synchronous or asynchronous.

- **async seams**: per-call timeout is implemented by cancelling the asyncio task (kill mode).
- **sync seams**: actual interruption uses `SIGALRM` on the POSIX main thread (in environments without `SIGALRM`, graceful=post-hoc and kill=`UnsupportedTimeoutKill`).

`on_timeout="graceful"` (the default) gives up, records a synthetic step, and proceeds to the next iteration; `"kill"` raises `SeamTimeout`. This is separate from the whole-run `Timeout` stop condition. For details, see [recipes/timeout-and-kill.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/timeout-and-kill.md).

## Related

- [../README.md](https://github.com/happy-ryo/loop-agent/blob/main/README.md) - LoopAgent overview and navigation
- [seams.md](seams.md) - Types and contracts for gather / act / review / verify / conditions / gate / on_step seams
- [recipes/timeout-and-kill.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/timeout-and-kill.md) - How to use per-call timeout (graceful / kill)

## Timeout portability note

Windows cannot hard-kill synchronous seams with POSIX `SIGALRM`. If a loop needs reliable interruption with `TimeoutPolicy(on_timeout="kill")`, prefer async seams and `await async_run_loop(...)`. In `graceful` mode, a blocking sync call can only be reported after it returns; it is not a force-stop for a hung native or blocking call.
