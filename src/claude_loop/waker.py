"""ループ wake と transport 配送の配線 (report.md S5 Phase3, Issue #23)。

:mod:`claude_loop.transport` は配送機構 (push 一次 / pull fallback / at-most-once) を
提供するが、**ループのどの瞬間がどの wake になるか** はループ側の関心である。本モジュールは
その対応付け (loop 完了 / 次反復 / 判断要求 -> :class:`~claude_loop.transport.Wake`) を担い、
:class:`~claude_loop.observe.LoopObserver` / :class:`~claude_loop.store.DBProgressLog` と同じ
作法 (``record_result`` 観測フック) に乗る drop-in として配線できるようにする。

配送する 3 wake (report.md S5 Phase3「ループの完了/次反復/判断要求の wake を配送」):

- **完了** (:data:`~claude_loop.transport.WAKE_LOOP_DONE`): ``run_loop`` が終端した
  (``goal_met`` / ``stopped``)。受信側 (coordinator / 窓口) に終了と理由を届ける。
- **判断要求** (:data:`~claude_loop.transport.WAKE_DECISION_REQUEST`): 人間ゲートで
  ``paused`` した。不可逆 action の判断を人間に要求する wake (gate_key を載せる)。
- **次反復** (:data:`~claude_loop.transport.WAKE_NEXT_ITERATION`): 完了 -> 次反復の接続を
  起こす wake。完了後に次候補へ進む合図 (人間ゲート維持の前提で、提案として配送)。

wake id は **決定的** に組む (``f"{run_id}:{kind}:{iteration}"``)。これにより resume での
再配送指示や push/pull の継ぎ目で同じ wake を二度 deliver しても、queue の二重 enqueue 冪等性
(:meth:`~claude_loop.transport.InMemoryWakeQueue.enqueue`) で de-dup され、受信側に二重に
届かない (at-most-once の土台)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .transport import (
    WAKE_DECISION_REQUEST,
    WAKE_LOOP_DONE,
    WAKE_NEXT_ITERATION,
    Transport,
    Wake,
)

if TYPE_CHECKING:  # 実行時 import cycle を避ける (型注釈のためだけ)。
    from .loop import LoopResult


def wake_id_for(run_id: str, kind: str, iteration: int) -> str:
    """決定的な wake id を組む (``"{run_id}:{kind}:{iteration}"``)。

    同一 (run_id, kind, iteration) には常に同じ id を割り当て、再配送/二重 deliver を
    queue 側で de-dup させる (at-most-once)。
    """
    return f"{run_id}:{kind}:{iteration}"


def wakes_for_result(
    result: "LoopResult",
    *,
    run_id: str,
    recipient: str,
    next_recipient: Optional[str] = None,
) -> list[Wake]:
    """``LoopResult`` を配送すべき :class:`Wake` 群へ写す (純粋関数・副作用なし)。

    - ``paused`` (人間ゲート中断): **判断要求** wake 1 件 (gate_key 同梱)。次反復 wake は
      出さない (人間判断待ちで先へ進まない)。
    - それ以外 (``goal_met`` / ``stopped``): **完了** wake 1 件 (status / 理由 / 集計を同梱)。
      ``next_recipient`` が指定されれば **次反復** wake も 1 件足す (完了 -> 次反復の接続。
      人間ゲート維持の前提で「次候補の提案」として配送する)。

    純粋関数なので、配送 (:class:`Transport.deliver`) と分離してテスト/合成しやすい。
    """
    it = result.iterations
    if result.paused:
        gate_key = ""
        if isinstance(result.pending, dict):
            gate_key = result.pending.get("gate_key", "")
        return [
            Wake(
                id=wake_id_for(run_id, WAKE_DECISION_REQUEST, it),
                kind=WAKE_DECISION_REQUEST,
                recipient=recipient,
                run_id=run_id,
                payload={"gate_key": gate_key, "reason": result.reason},
            )
        ]

    wakes = [
        Wake(
            id=wake_id_for(run_id, WAKE_LOOP_DONE, it),
            kind=WAKE_LOOP_DONE,
            recipient=recipient,
            run_id=run_id,
            payload={
                "status": result.status,
                "succeeded": result.succeeded,
                "reason": result.reason,
                "iterations": it,
                "tokens_used": result.tokens_used,
            },
        )
    ]
    if next_recipient is not None:
        wakes.append(
            Wake(
                id=wake_id_for(run_id, WAKE_NEXT_ITERATION, it),
                kind=WAKE_NEXT_ITERATION,
                recipient=next_recipient,
                run_id=run_id,
                payload={"after_iteration": it},
            )
        )
    return wakes


class LoopWaker:
    """ループの wake を :class:`Transport` 経由で配送する drop-in 配線。

    :class:`~claude_loop.observe.LoopObserver` / :class:`~claude_loop.store.DBProgressLog`
    と同じ ``record_result`` フック形を実装するので、観測の配線にそのまま並べられる::

        waker = LoopWaker(transport, run_id="r1", recipient="coordinator")
        result = run_loop(act=..., verify=..., conditions=...)
        waker.record_result(result)   # 完了/判断要求 wake を配送

    ``next_recipient`` を渡すと、完了時に「次反復」wake も配送する (完了 -> 次反復の接続を
    人間ゲート維持の前提で起こす)。配送は :class:`Transport.deliver` に委ねるので、push が
    通れば即配送、backend 不通でも queue に残り受信側の pull poll で配送が継続する。

    返り値は wake id -> 配送経路 (``"push"`` | ``"queued"``) の dict で、テスト/監視に使える。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        run_id: str,
        recipient: str,
        next_recipient: Optional[str] = None,
    ) -> None:
        self._transport = transport
        self._run_id = run_id
        self._recipient = recipient
        self._next_recipient = next_recipient

    def record_result(self, result: "LoopResult") -> dict[str, str]:
        """``LoopResult`` から wake を組み立てて配送する。observer の ``record_result`` 互換。"""
        routes: dict[str, str] = {}
        for wake in wakes_for_result(
            result,
            run_id=self._run_id,
            recipient=self._recipient,
            next_recipient=self._next_recipient,
        ):
            routes[wake.id] = self._transport.deliver(wake)
        return routes

    def deliver_wake(
        self, kind: str, *, iteration: int, recipient: Optional[str] = None, **payload: Any
    ) -> str:
        """任意の wake を 1 件直接配送する低レベル口 (決定的 id を自動付与)。

        ``record_result`` の対応に収まらないアドホックな wake (例: ループ外からの割り込み
        通知) を、同じ決定的 id 規則 + at-most-once 配送に乗せたいとき用。
        """
        rcpt = recipient if recipient is not None else self._recipient
        wake = Wake(
            id=wake_id_for(self._run_id, kind, iteration),
            kind=kind,
            recipient=rcpt,
            run_id=self._run_id,
            payload=payload,
        )
        return self._transport.deliver(wake)


__all__ = [
    "wake_id_for",
    "wakes_for_result",
    "LoopWaker",
]
