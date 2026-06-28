"""Loop wake and transport delivery wiring (report.md S5 Phase3, Issue #23).

:mod:`loop_agent.transport` provides a delivery mechanism (push first / pull fallback / at-most-once),
but **which moment in the loop corresponds to which wake** is a concern of the loop side. This module
handles that mapping (loop completion / next iteration / decision request -> :class:`~loop_agent.transport.Wake`),
and wires it as a drop-in compatible with :class:`~loop_agent.observe.LoopObserver` / :class:`~loop_agent.store.DBProgressLog`
using the same pattern (``record_result`` observation hook).

3 wakes to deliver (report.md S5 Phase3 'deliver loop completion / next iteration / decision request wakes'):

- **completion** (:data:`~loop_agent.transport.WAKE_LOOP_DONE`): ``run_loop`` has terminated
  (``goal_met`` / ``stopped``). Deliver the termination and reason to the receiver (coordinator / interface).
- **decision request** (:data:`~loop_agent.transport.WAKE_DECISION_REQUEST`): from a human gate
  ``paused``. A wake that requests a human to decide on irreversible actions (carrying gate_key).
- **next iteration** (:data:`~loop_agent.transport.WAKE_NEXT_ITERATION`): completion -> next iteration connection
  wake. A signal to advance to the next candidate after completion (delivered as a proposal, under the assumption of maintaining the human gate).

wake id is built **deterministically** (``f"{run_id}:{kind}:{iteration}\"``). This allows
redelivery instruction or at the seam of push/pull: even if the same wake is delivered twice, the queue's idempotent duplicate enqueue
(:meth:`~loop_agent.transport.InMemoryWakeQueue.enqueue`) de-duplicates it, so the receiver does not
receive it twice (foundation of at-most-once).
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

if TYPE_CHECKING:  # avoid runtime import cycle (only for type annotations).
    from .loop import LoopResult


def wake_id_for(run_id: str, kind: str, iteration: int) -> str:
    """Assemble a deterministic wake id (``"{run_id}:{kind}:{iteration}\"``).

    Always assign the same id to identical (run_id, kind, iteration) tuples,
    and let the queue de-duplicate redelivery / double delivery (at-most-once).
    """
    return f"{run_id}:{kind}:{iteration}"


def wakes_for_result(
    result: "LoopResult",
    *,
    run_id: str,
    recipient: str,
    next_recipient: Optional[str] = None,
) -> list[Wake]:
    """Map ``LoopResult`` to the set of :class:`Wake` to be delivered (pure function, no side effects).

    - ``paused`` (human gate interrupted): **decision request** wake x1 (gate_key included). Next iteration wake is
      not emitted (does not advance pending human judgment).
    - otherwise (``goal_met`` / ``stopped``): **completion** wake x1 (status / reason / summary included).
      If ``next_recipient`` is specified, also add **next iteration** wake x1 (completion -> next iteration connection,
      delivered as 'next candidate proposal' under the assumption of maintaining the human gate).

    Being a pure function, it is easy to test and compose separately from delivery (:class:`Transport.deliver`).
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
    """A drop-in wiring that delivers loop wakes via :class:`Transport`.

    Implements the same ``record_result`` hook pattern as :class:`~loop_agent.observe.LoopObserver` / :class:`~loop_agent.store.DBProgressLog`,
    so it can be placed directly in the observation wiring::

        waker = LoopWaker(transport, run_id="r1", recipient="coordinator")
        result = run_loop(act=..., verify=..., conditions=...)
        waker.record_result(result)   # deliver completion / decision request wake

    If ``next_recipient`` is passed, the 'next iteration' wake is also delivered upon completion (completion -> next iteration connection
    occurs under the assumption of maintaining the human gate). Delivery is delegated to :class:`Transport.deliver`, so if push
    succeeds, it is delivered immediately; if the backend is unavailable, it remains in the queue and delivery continues via the receiver's pull poll.

    The return value is a dict of wake id -> delivery route (``"push"`` | ``"queued"``) usable for testing / monitoring.
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
        """Assemble and deliver wakes from ``LoopResult``. Compatible with observer's ``record_result``."""
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
        """Low-level interface for directly delivering an arbitrary wake x1 (deterministic id auto-assigned).

        Use for ad hoc wakes not covered by the ``record_result`` pattern (e.g., interrupt
        notification from outside the loop) to place on the same deterministic id rule + at-most-once delivery.
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
