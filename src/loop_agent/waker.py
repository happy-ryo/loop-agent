"""Wiring for loop wakes and transport delivery (report.md S5 Phase3, Issue #23).

:mod:`loop_agent.transport` provides the delivery mechanism (push first / pull
fallback with receiver-side de-duplication), but **which loop moment maps to
which wake** is a loop concern. This module handles that mapping (loop
completion / next iteration / decision request ->
:class:`~loop_agent.transport.Wake`) and can be wired as a drop-in using the
same convention as :class:`~loop_agent.observe.LoopObserver` /
:class:`~loop_agent.store.DBProgressLog` (the ``record_result`` observation
hook).

The three wakes to deliver (report.md S5 Phase3, "deliver wakes for loop
completion/next iteration/decision request"):

- **Completion** (:data:`~loop_agent.transport.WAKE_LOOP_DONE`): ``run_loop``
  terminated (``goal_met`` / ``stopped``). Delivers completion and the reason to
  the recipient (coordinator / front desk).
- **Decision request** (:data:`~loop_agent.transport.WAKE_DECISION_REQUEST`):
  paused at a human gate. A wake requesting human judgment for an irreversible
  action (with gate_key included).
- **Next iteration** (:data:`~loop_agent.transport.WAKE_NEXT_ITERATION`): a wake
  that connects completion -> next iteration. It signals moving to the next
  candidate after completion (delivered as a proposal, assuming the human gate
  remains in place).

Wake ids are built **deterministically** (``f"{run_id}:{kind}:{iteration}"``).
This lets queue enqueue idempotency
(:meth:`~loop_agent.transport.InMemoryWakeQueue.enqueue`) de-dup the same wake if
a resume redelivery instruction tries to enqueue it again. The stable id also
gives recipients a de-duplication key for rare push/pull boundary duplicates;
receivers should handle wake ids idempotently rather than relying on guaranteed
single delivery.
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

if TYPE_CHECKING:  # Avoid a runtime import cycle (only needed for type annotations).
    from .loop import LoopResult


def wake_id_for(run_id: str, kind: str, iteration: int) -> str:
    """Build a deterministic wake id (``"{run_id}:{kind}:{iteration}"``).

    Always assigns the same id to the same (run_id, kind, iteration), allowing
    the queue side to suppress duplicate enqueue attempts and receivers to
    de-dup rare push/pull boundary duplicates by wake id.
    """
    return f"{run_id}:{kind}:{iteration}"


def wakes_for_result(
    result: "LoopResult",
    *,
    run_id: str,
    recipient: str,
    next_recipient: Optional[str] = None,
) -> list[Wake]:
    """Map a ``LoopResult`` to the :class:`Wake` objects to deliver (pure, no side effects).

    - ``paused`` (interrupted at a human gate): one **decision request** wake
      (including gate_key). Does not emit a next-iteration wake (it does not
      advance while waiting for human judgment).
    - Otherwise (``goal_met`` / ``stopped``): one **completion** wake (including
      status / reason / aggregates). If ``next_recipient`` is specified, also
      adds one **next iteration** wake (completion -> next iteration connection,
      delivered as a "next candidate proposal" assuming the human gate remains in
      place).

    Because this is a pure function, it is easy to test/compose separately from
    delivery (:class:`Transport.deliver`).
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
    """Drop-in wiring that delivers loop wakes through :class:`Transport`.

    :class:`~loop_agent.observe.LoopObserver` / :class:`~loop_agent.store.DBProgressLog`
    implement the same ``record_result`` hook shape, so this can be placed
    alongside observation wiring as-is::

        waker = LoopWaker(transport, run_id="r1", recipient="coordinator")
        result = run_loop(act=..., verify=..., conditions=...)
        waker.record_result(result)   # Deliver completion/decision request wakes

    Passing ``next_recipient`` also delivers a "next iteration" wake at
    completion (creating the completion -> next iteration connection under the
    assumption that the human gate remains in place). Delivery is delegated to
    :class:`Transport.deliver`, so it is delivered immediately if push succeeds;
    if the backend is unavailable, it remains in the queue and delivery continues
    through recipient-side pull polling.

    The return value is a dict of wake id -> delivery route (``"push"`` |
    ``"queued"``), useful for tests/monitoring.
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
        """Build and deliver wakes from a ``LoopResult``. Compatible with observer ``record_result``."""
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
        """Low-level entry point to directly deliver one arbitrary wake (auto-assigns a deterministic id).

        Use this when an ad hoc wake that does not fit the ``record_result``
        mapping (for example, an interruption notification from outside the
        loop) should use the same deterministic id rule for enqueue suppression
        and receiver-side de-duplication.
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
