> This file is a load-on-demand bundled copy of `docs/async.md`. The canonical source is `docs/async.md` in the repository.

# async/await 対応 (async_run_loop)

LoopAgent は同期 `run_loop` と非同期 `async_run_loop` の 2 つのエントリポイントを提供する。control flow の実装は一本化されており、同期 API は完全維持されている（Issue #40）。

## sync と async の対応関係

| | `run_loop` | `async_run_loop` |
|---|---|---|
| 呼び出し | `run_loop(...)` | `await async_run_loop(...)` |
| 用途 | 同期コードからそのまま回す | すでにイベントループ内にいる / いずれかのシームが coroutine function |
| 引数 | `act` / `review` / `verify` / `conditions` / `gather=…` / `on_step=…` / `gate=…` / `time_fn=…` / `initial_state=…` / `timeout=…` | 同一（completely identical） |
| 返り値 | `LoopResult` | `LoopResult` |
| シーム | **同期 callable のみ**（async シームを渡すと `AsyncSeamInSyncLoop` を送出） | 同期 callable も非同期（acallable）も受ける（混在可） |

`async_run_loop` がループ本体（gather -> act -> verify -> repeat）の **single source of truth** であり、同期 `run_loop` はその薄い `asyncio.run` ラッパとして同じ実装を共有する。同期フックだけで構成した場合、共有 coroutine は実際には一度も await せず（`maybe_await` は awaitable でない値をそのまま返す）、`run_loop` は呼び出し元自身の context でこれを走らせ切る。イベントループを作らず `asyncio.Task` でも包まないため、`contextvars` の伝播・フック例外の型・per-call オーバーヘッドは async 対応導入前と完全に一致する。つまり**同期フックに追加コストはない**。

`run_loop` は内部で `async_run_loop` を `_strict_sync=True` で呼ぶ。この間にいずれかのシーム（`act` / `review` / `verify` / `conditions` の各 `check` / `gate.review` / `on_step` / `on_complete`）が awaitable を返すと、その時点で `loop_agent.errors.AsyncSeamInSyncLoop`（`LoopError` かつ `RuntimeError`）を送出して `await async_run_loop(...)` へ誘導する。元々の同期 `run_loop` も async シームを受けつけなかったため、これは regression ではなく明確で一貫したエラーである。

## 基本例: async な act を回す

```python
import asyncio

from loop_agent import (
    ActOutcome,
    MaxIterations,
    VerifyOutcome,
    async_run_loop,
)


async def act(ctx):
    # 例: 非同期 I/O (HTTP / DB / LLM 呼び出し) をここで await する
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

同期コードから 1 回だけ非同期ループを回したい場合は、上記の `asyncio.run(main())` のように `async_run_loop(...)` を `asyncio.run` で駆動するのが正規の使い方である（テストスイートも pytest-asyncio に依存せずこの形で coroutine を駆動している）。

## 同期シームと非同期シームの混在

`gather` / `act` / `review` / `verify` / `conditions` の各 `check` / `gate.review` / `on_step`（および gate の `on_complete`）は、**プレーンな同期 callable でも async なもの（awaitable を返す）でもよい**。ドライバは各結果を `loop_agent._async.maybe_await` 経由で await するため、両者は自由に混在できる（例: async な `gather` + 同期 `act` + async な `verify`）。同期フックは返り値が awaitable でないため await のオーバーヘッドを一切足さず、そのまま使われる。

```python
def gather(state):              # 同期
    return state

async def act(ctx):             # 非同期
    await asyncio.sleep(0)
    return ActOutcome(observation=None, tokens=1)

result = await async_run_loop(
    act=act,
    verify=verify,              # 同期 / 非同期どちらでもよい
    conditions=[MaxIterations(5)],
    gather=gather,
)
```

各シームの型と契約の詳細は [seams.md](seams.md) を参照。

## 複数ループの並行実行 (asyncio.gather)

`async_run_loop` 自身は内部で並行処理を行わない。gather -> gate -> act -> verify の順序と stop 条件の評価タイミングを同期ループと完全一致させるため、各シームを**逐次** await する。複数の独立ループを並行に回したい場合は、呼び出し側でタスクとしてスケジュールする。

```python
results = await asyncio.gather(
    async_run_loop(act=act_a, verify=verify_a, conditions=[MaxIterations(10)]),
    async_run_loop(act=act_b, verify=verify_b, conditions=[MaxIterations(10)]),
)
# あるいは asyncio.create_task(async_run_loop(...)) で個別に走らせる
```

各呼び出しは自分専用の `LoopState` を持つため、並行 run どうしは干渉しない。

注意: `time_fn` は**同期**の monotonic クロックのまま（読まれるだけで await されない）。同期 `act` がブロックするとイベントループ全体をブロックするため、他タスクとループを共有する場合は本当にブロックする処理を async フック + `loop.run_in_executor` でラップすること。

## per-call timeout と async シーム

`act` / `verify` の **per-call** timeout は `TimeoutPolicy`（または裸の秒数）を `run_loop` / `async_run_loop` の `timeout=` に渡して指定する（Issue #42）。実中断の手段はシームが同期か非同期かで異なる。

- **async シーム**: asyncio の task cancel で per-call timeout を実現する（kill mode）。
- **sync シーム**: POSIX main thread の `SIGALRM` で実中断する（SIGALRM 不在環境では graceful=post-hoc、kill=`UnsupportedTimeoutKill`）。

`on_timeout="graceful"`（既定）は諦めて合成 step を記録し次反復へ進み、`"kill"` は `SeamTimeout` を送出する。これは whole-run の `Timeout` stop 条件とは別物である。詳細は [recipes/timeout-and-kill.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/timeout-and-kill.md)。

## 関連

- [../README.md](https://github.com/happy-ryo/loop-agent/blob/main/README.md) — LoopAgent 全体像と動線
- [seams.md](seams.md) — gather / act / verify / conditions / gate / on_step シームの型と契約
- [recipes/timeout-and-kill.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/recipes/timeout-and-kill.md) — per-call timeout（graceful / kill）の使い方

## Timeout portability note

Windows cannot hard-kill synchronous seams with POSIX `SIGALRM`. If a loop needs reliable interruption with `TimeoutPolicy(on_timeout="kill")`, prefer async seams and `await async_run_loop(...)`. In `graceful` mode, a blocking sync call can only be reported after it returns; it is not a force-stop for a hung native or blocking call.
