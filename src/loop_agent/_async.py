"""sync/async シーム共用の小さなユーティリティ (Issue #40)。

:func:`maybe_await` は「値が awaitable なら await し、そうでなければそのまま返す」
だけの薄いアダプタである。これにより ``gather`` / ``act`` / ``verify`` /
``conditions`` / ``gate`` の各フックを **同期 callable のまま受けつつ**、同一の
呼び出し地点で **非同期 (acallable) も await できる**。

使い方の規約: フックを **呼び出した結果** を渡す (フック自体ではない)。

    outcome = await maybe_await(act(context))   # act が sync でも async でも可

同期フックは普通の値を返すので :func:`inspect.isawaitable` が ``False`` となり、
追加コストはほぼゼロ (coroutine を生成しない) で即座に値が返る。非同期フックは
coroutine / future を返すので、その場で await される。

これは :func:`loop_agent.loop.async_run_loop` が単一の制御フローで sync/async 双方を
駆動するための土台で、``conditions`` (各 ``check``) と ``gate.review`` も同じ規約で
sync/async どちらでも受けられる。

**strict-sync モード (run_loop 用)**: 同期 API :func:`loop_agent.run_loop` は共有
コルーチンを *呼び出し側のコンテキストで一度に* 駆動する -- イベントループを作らず、
``coro.send(None)`` で手動ステップする。run_loop は :func:`loop_agent.async_run_loop` を
``_strict_sync=True`` で呼び、async_run_loop が自身の実行範囲だけ :data:`reject_awaitables`
を ``True`` にする。strict-sync 下で :func:`maybe_await` が awaitable を受け取ったら、
await せず :exc:`AsyncSeamInSyncLoop` を送出する。これにより「非同期フックが内部で実際に
suspend するか否か」に依存せず、**どの非同期シームでも一貫して** 「run_loop に async シーム
が渡された」ことを早期に・確実に弾ける (suspend するものだけ弾けて、suspend しないものは
黙って実行されてしまう不整合を防ぐ)。非同期シームには :func:`loop_agent.async_run_loop` を
使うこと。

フラグは async_run_loop が **入口で自分のモードに明示セット** する (継承値を上書き) ため、
ネストにも、ambient なイベントループの有無にも依存しない: 同期 ``run_loop`` のフックが内部で
``asyncio.run(async_run_loop(...))`` を回しても、その内側は ``_strict_sync=False`` で入るので
strict 扱いにならず内側の正当な非同期シームは await される。逆に ``run_loop`` を実行中ループ
内から呼んでも ``_strict_sync=True`` が効き、非同期シームは正しく拒否される。
"""

from __future__ import annotations

import contextvars
import inspect
from typing import Awaitable, TypeVar, Union

T = TypeVar("T")

# async_run_loop が自身の実行範囲だけ True にする (run_loop 駆動なら _strict_sync=True)。
# True の間 maybe_await / afirst_triggered は awaitable を AsyncSeamInSyncLoop で拒否する。
# 入口で明示セットするので継承値 (copy_context) を上書きし、ネストや ambient ループに漏れない。
reject_awaitables: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "loop_agent_reject_awaitables", default=False
)


class AsyncSeamInSyncLoop(RuntimeError):
    """同期 :func:`loop_agent.run_loop` に非同期 (awaitable) シームが渡された。

    ``act`` / ``verify`` / ``gather`` / ``conditions`` の ``check`` /
    ``gate.review`` / ``on_step`` / ``on_complete`` のいずれかが awaitable を返した
    ことを示す。非同期シームには :func:`loop_agent.async_run_loop` を使うこと。
    """


def driven_synchronously() -> bool:
    """strict-sync (= ``run_loop`` 駆動中) なら ``True``。

    :func:`loop_agent.async_run_loop` が ``_strict_sync`` に応じて入口でセットする
    :data:`reject_awaitables` を読む。ambient なイベントループの有無ではなく、driver が
    明示した値で判定するため、実行中ループ内からの ``run_loop`` 呼び出しでも、ネストした
    ``asyncio.run(async_run_loop(...))`` でも正しく分かれる。
    """
    return reject_awaitables.get()


async def maybe_await(value: Union[T, Awaitable[T]]) -> T:
    """``value`` が awaitable ならば await した結果を、そうでなければ ``value`` を返す。

    フック呼び出しの戻り値を渡すこと (フック関数そのものではない)。同期フックの
    戻り値は awaitable でないため、何も待たずに即座に返る。

    strict-sync (= :func:`driven_synchronously` が ``True``。同期 ``run_loop`` が
    手動ドライブ中) のときに awaitable を受け取った場合は、await せず
    :exc:`AsyncSeamInSyncLoop` を送出する (未 await 警告を出さないよう awaitable は
    ``close`` する)。
    """
    if inspect.isawaitable(value):
        if driven_synchronously():
            # 未 await の coroutine 警告を避けるため閉じてから弾く (future 等 close を
            # 持たないものは getattr ガードでスキップ)。
            close = getattr(value, "close", None)
            if close is not None:
                close()
            raise AsyncSeamInSyncLoop(
                "run_loop() received an async (awaitable) seam "
                "(act/verify/gather/condition/gate/on_step/on_complete); "
                "use `await async_run_loop(...)` for async seams"
            )
        return await value  # type: ignore[no-any-return]
    return value  # type: ignore[return-value]
