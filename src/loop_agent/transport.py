"""Transport layer for wake delivery: push-primary / pull-fallback / at-most-once (Issue #23).

report.md S3.3 / S4.6 / S5 Phase3. Instantiates the wake delivery mechanism that delivers
**completion / next iteration / decision request** to other loops or recipient endpoints.
The claude-org runtime broker sidecar cannot be directly reused as it belongs to the runtime[^pattern-only],
so we **extract the pattern only** and implement it in loop-agent with zero dependencies (stdlib only).

Extracted patterns (source: ``knowledge/curated/broker-transport.md`` / backend contract):

- **push-primary / pull-fallback** (report.md S3.3). push (in-band injection) is an *immediate accelerator*,
  and pull polling is the *canonical delivery path* (backend-neutral, no interrupt hazards).
  Even if push expires or becomes unavailable, the delivery does not break if the recipient
  actively polls on role cadence. This layer mirrors this asymmetry directly to establish
  "delivery continues via pull fallback even when backend is unavailable" (report.md S5 Phase3 success condition b).
- **At-most-once via three-state claim-then-confirm** (broker lost-message-window insight).
  A single ``delivered`` boolean has a loss window of "flag is set but not reached the recipient".
  We seal this with ``UNDELIVERED -> CLAIMED(lease, owner) -> DELIVERED`` daemon-owned three states +
  claim-then-confirm: claim returns with lease occupation of the row, confirm marks it DELIVERED
  after the recipient has finished processing. If lease expires before confirm, the row reverts to
  UNDELIVERED (re-eligible). Confirmation (DELIVERED) is guarded by fencing requiring ``owner`` match
  and non-expired lease; confirmed messages are never re-delivered (at-most-once). When multiple
  workers poll the same recipient in parallel, pass a distinct ``owner`` per worker (owner fencing
  prevents double confirmation).
- **Role-based cadence** (broker pull-first insight). In pull environments where push expires,
  "waiting" is translated not as idle waiting but as *active polling*. Receive triggers are
  designed asymmetrically by role (dispatcher 3m / worker bounded / secretary turn-prologue).
  :data:`CADENCE_SECONDS` / :func:`due_to_poll` provide the minimal form.

Design boundaries (report.md S6 "transport runtime dependency"):

- Runtime-independent, self-contained. No dependency on ``pane`` / ``tmux`` / ``renga`` / ``broker`` CLI.
  push backend is replaceable via :class:`PushBackend` Protocol injection (best-effort ``bool``
  contract inherited from ``tools/peer_notify.py``), and the delivery canonical store (queue)
  is held independently of the backend.
- Recipient assumes **idempotent handler**. wake carries identity via :attr:`Wake.id`;
  double enqueue is no-op, and even if rare double delivery occurs at push/pull boundary,
  the recipient can de-dup by id (report.md philosophy: "tolerate residual loss window as
  at-least-once + idempotent representation; loss > duplication for idle-wake").
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, runtime_checkable

# -- Wake kinds (report.md S5 Phase3 "deliver completion/next-iteration/decision-request wakes") ---------
#
# Constant-ize so readers can filter / dispatch without scattered string literals.
WAKE_LOOP_DONE = "loop_done"  # Loop has completed (goal_met / stopped).
WAKE_NEXT_ITERATION = "next_iteration"  # Proceed to next iteration / wake next task.
WAKE_DECISION_REQUEST = "decision_request"  # Request human judgment for irreversible action (human gate).

WAKE_KINDS = (WAKE_LOOP_DONE, WAKE_NEXT_ITERATION, WAKE_DECISION_REQUEST)

# Recipient-side delivery state (three-state). Owned by daemon (= this queue).
UNDELIVERED = "undelivered"
CLAIMED = "claimed"
DELIVERED = "delivered"


@dataclass(frozen=True)
class Wake:
    """A single wake to be delivered.

    ``id`` is **delivery identity** and the key for at-most-once / de-dup. For loop wakes,
    providing a deterministic id like ``f"{run_id}:{kind}:{iteration}"`` allows the recipient
    to de-dup by id for re-delivery on resume and double delivery at push/pull boundary
    (double enqueue of the same id becomes no-op). ``recipient`` is the destination (role name or peer id).
    ``payload`` is kind-specific supplementary information (completion reason, gate_key, etc.).
    """

    id: str
    kind: str
    recipient: str
    run_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Fold into a flat dict that is easy to JSON-ify (for sink / backend serialization).

        Expand ``payload`` first, then overwrite canonical fields (``id`` / ``kind`` / ``recipient`` /
        ``run_id``) with **canonical taking precedence**. This ensures canonical fields remain authoritative
        even if same-named keys leak into payload, preventing accidents like payload-derived ``id``
        diverging from the queue's de-dup key and being sent to the wrong recipient.
        Same-named payload keys are shadowed by canonical values (reserved names go canonical, by contract).
        """
        return {
            **dict(self.payload),
            "id": self.id,
            "kind": self.kind,
            "recipient": self.recipient,
            "run_id": self.run_id,
        }


@runtime_checkable
class PushBackend(Protocol):
    """Minimal interface for push (primary / immediate accelerator).

    ``push(wake) -> bool`` is **best-effort** (following bool contract from ``tools/peer_notify.py``):
    return ``True`` only when delivery is confirmed; return ``False`` for anything else
    (backend unavailable, timeout, recipient absent, etc.). Exceptions are permitted
    (:class:`Transport` catches and treats as ``False``), but ideally return ``False`` instead.
    Wakes that don't return ``True`` remain in queue and are picked up by recipient's pull poll (= pull fallback).
    """

    def push(self, wake: Wake) -> bool:
        ...


class CallablePushBackend:
    """Thin adapter that adapts any ``callable(Wake) -> bool`` to :class:`PushBackend`."""

    def __init__(self, fn: Callable[[Wake], bool]) -> None:
        self._fn = fn

    def push(self, wake: Wake) -> bool:
        return self._fn(wake)


class NullPushBackend:
    """Backend that always fails on push (= explicit model of backend unavailability).

    Represents a configuration with no push-primary / backend down. All wakes remain in queue
    and are delivered only via pull fallback. This is the default configuration for
    "delivery continues via pull fallback even when backend is unavailable" and also serves
    as the test baseline.
    """

    def push(self, wake: Wake) -> bool:
        return False


@dataclass
class _Entry:
    """Delivery state of one wake in queue (three-state + lease ownership)."""

    wake: Wake
    seq: int
    state: str = UNDELIVERED
    owner: Optional[str] = None
    lease_expiry: float = 0.0


@runtime_checkable
class WakeQueue(Protocol):
    """Canonical store for delivery (durable spine). Provides three-state claim-then-confirm.

    :class:`Transport` holds this queue as canonical independently of backend (push).
    Even if push cannot confirm delivery, wakes remain in queue and are delivered via
    pull through recipient's :meth:`claim` -> :meth:`confirm`.
    """

    def enqueue(self, wake: Wake) -> bool:
        ...

    def claim(
        self, recipient: str, *, now: float, lease: float, owner: str, limit: Optional[int] = None
    ) -> list[Wake]:
        ...

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        ...

    def release_expired(self, *, now: float) -> int:
        ...

    def mark_delivered(self, wake_id: str) -> bool:
        ...

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        ...

    def state_of(self, wake_id: str) -> Optional[str]:
        ...


class InMemoryWakeQueue:
    """In-memory implementation of :class:`WakeQueue` (three-state claim-then-confirm).

    Default queue that holds wakes within the loop's own process. To layer persistent
    ``state.db`` on top, simply implement the same :class:`WakeQueue` Protocol with SQLite
    (this PoC demonstrates at-most-once / fallback semantics in-memory).

    State transitions (daemon-owned, row-level ownership ensures single-drainer):

    - ``enqueue`` : If same ``id`` exists, no-op (double-enqueue idempotent = de-dup foundation).
    - ``claim``   : First collect expired leases, then transition recipient's ``UNDELIVERED``
      to ``CLAIMED`` (owner + lease_expiry) in seq order and return them.
    - ``confirm`` : Mark as ``DELIVERED`` (terminal) if ``CLAIMED`` and **owner still matches claim time**
      and lease is not expired. Stale confirms to rows whose lease expired and were re-claimed
      by another owner are rejected by owner mismatch (fencing), preventing loss-window
      "DELIVERED without actually reaching recipient" (precondition: parallel polls use distinct owner;
      see :meth:`~loop_agent.transport.Transport.poll`).
    - ``release_expired`` : Revert ``CLAIMED`` with expired lease back to ``UNDELIVERED`` (re-eligible).
    - ``mark_delivered`` : Directly mark push-confirmed wake as ``DELIVERED`` (terminal).
      Transitions idempotently from any non-terminal state (absorbs push/pull boundary).

    **Thread-safe**: To prevent double claims when multiple workers (threads) poll the same recipient
    in parallel, all state-mutating operations (enqueue/claim/confirm/release_expired/mark_delivered)
    and reads are serialized with a single reentrant lock (making check-and-set atomic).
    ``claim`` internally calls ``release_expired``, so we use :class:`threading.RLock` (reentrant).
    This ensures owner fencing and at-most-once claims work correctly across concurrent pollers.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._seq = 0
        # Reentrant lock to serialize state transitions (RLock for claim -> release_expired reentrancy).
        self._lock = threading.RLock()

    def enqueue(self, wake: Wake) -> bool:
        """Register wake as ``UNDELIVERED``. If same ``id`` exists, no-op and return ``False``.

        Making double enqueue idempotent ensures retry of deliver or re-delivery instruction
        on resume doesn't corrupt existing rows (in-progress claims or DELIVERED)
        (= foundation for not double-delivering to humans / recipients).
        """
        if not wake.id:
            raise ValueError("enqueue: Wake.id must be a non-empty string")
        with self._lock:
            if wake.id in self._entries:
                return False
            self._entries[wake.id] = _Entry(wake=wake, seq=self._seq)
            self._seq += 1
            return True

    def release_expired(self, *, now: float) -> int:
        """Revert ``CLAIMED`` with expired lease to ``UNDELIVERED``, return count reverted.

        If recipient dies before confirm (crash between claim and confirm), row stalls as CLAIMED.
        Lease expiry reverts it to re-eligible so delivery doesn't stop and row is re-claimed
        (tilts toward at-least-once: loss > duplication for idle-wake). Resets ``owner`` to ``None``
        to reliably reject stale confirms from old owner.
        """
        with self._lock:
            released = 0
            for e in self._entries.values():
                if e.state == CLAIMED and e.lease_expiry <= now:
                    e.state = UNDELIVERED
                    e.owner = None
                    released += 1
            return released

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        """Claim and return ``UNDELIVERED`` wakes for ``recipient`` with lease occupation (pull claim).

        First collect expired leases (:meth:`release_expired`), then transition up to ``limit``
        matching-recipient ``UNDELIVERED`` rows to ``CLAIMED`` in **registration order (seq)**.
        Stamp each row with ``owner`` and expiry time ``now + lease``. Returned wakes must be
        fully processed by caller before :meth:`confirm` confirms them (claim-then-confirm).
        """
        if lease <= 0:
            raise ValueError("claim: lease must be > 0")
        with self._lock:
            self.release_expired(now=now)
            out: list[Wake] = []
            for e in sorted(self._entries.values(), key=lambda x: x.seq):
                if limit is not None and len(out) >= limit:
                    break
                if e.state == UNDELIVERED and e.wake.recipient == recipient:
                    e.state = CLAIMED
                    e.owner = owner
                    e.lease_expiry = now + lease
                    out.append(e.wake)
            return out

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        """Confirm claimed wake as ``DELIVERED`` (terminal).

        Return ``True`` and confirm only if ``CLAIMED`` and current ``owner`` matches claim-time owner
        and lease is not expired. Otherwise (already DELIVERED / owner mismatch = re-claimed by another
        after expiry / lease expired / absent) return ``False``. This owner + expiry check acts as fencing
        to prevent stale claimers from erroneously marking as DELIVERED in loss windows.
        """
        with self._lock:
            e = self._entries.get(wake_id)
            if e is None:
                return False
            if e.state != CLAIMED:
                return False
            if e.owner != owner:
                return False
            if e.lease_expiry <= now:
                # Lease expired: this claim is no longer valid. release_expired will (or would have)
                # reverted to UNDELIVERED, so we don't mark as DELIVERED here.
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def mark_delivered(self, wake_id: str) -> bool:
        """Directly mark ``UNDELIVERED`` wake as ``DELIVERED`` (terminal) (confirm push delivery).

        Use when push backend returns ``True`` (confirmed delivery). Transitions **only if UNDELIVERED**;
        return ``True`` if transitioned, ``False`` otherwise (already DELIVERED / CLAIMED / absent).

        **Never steal CLAIMED** — this is the key point. During push I/O (outside queue lock),
        another poller may claim the same wake. Unconditionally marking DELIVERED would erase
        the owner of the active claim; if that poller crashes before confirm, lease expiry won't
        revert to re-eligible and the wake is **lost** (breaking claim-then-confirm crash recovery).
        When push and pull compete for the same wake, **make pull claim the delivery primary**;
        treat push side as duplicate of already-delivered and defer to recipient's id de-dup
        (at-least-once; loss > duplication policy). Rows where push successfully marked DELIVERED
        are no longer UNDELIVERED so pull won't claim them (absorbing the boundary).
        """
        with self._lock:
            e = self._entries.get(wake_id)
            if e is None:
                return False
            if e.state != UNDELIVERED:
                # Already DELIVERED, or another poller claimed (CLAIMED). Never steal the claim.
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """Return unconfirmed (``UNDELIVERED`` / ``CLAIMED``) wakes in registration order (optionally filtered by recipient)."""
        with self._lock:
            out: list[Wake] = []
            for e in sorted(self._entries.values(), key=lambda x: x.seq):
                if e.state == DELIVERED:
                    continue
                if recipient is not None and e.wake.recipient != recipient:
                    continue
                out.append(e.wake)
            return out

    def state_of(self, wake_id: str) -> Optional[str]:
        """Return current delivery state of ``wake_id`` (``None`` if absent). For testing / introspection."""
        with self._lock:
            e = self._entries.get(wake_id)
            return e.state if e is not None else None


# Role-based poll cadence (seconds). report.md S3.2 / asymmetric design from broker pull-first insight.
# In pull environments where push expires, translate "waiting" not as idle waiting but as active polling.
#
# - dispatcher : Monitoring /loop 3m equivalent = active poll at 180s intervals.
# - worker     : Bounded review-watch after completion report equivalent = short-interval poll.
# - secretary  : Human-interaction primary, blocking poll infeasible -> poll every turn prologue (0 = always due).
CADENCE_SECONDS: dict[str, float] = {
    "dispatcher": 180.0,
    "worker": 60.0,
    "secretary": 0.0,
}

# Default cadence for unknown roles (conservatively equivalent to worker).
DEFAULT_CADENCE_SECONDS = 60.0


def cadence_for(role: str) -> float:
    """Return poll interval (seconds) for ``role``. Unknown role returns :data:`DEFAULT_CADENCE_SECONDS`."""
    return CADENCE_SECONDS.get(role, DEFAULT_CADENCE_SECONDS)


def due_to_poll(role: str, last_poll: Optional[float], now: float) -> bool:
    """Return whether ``role`` should actively poll at ``now``.

    If ``last_poll`` is ``None`` (never polled), always due. Otherwise check
    ``now - last_poll >= cadence_for(role)``. Role with cadence ``0`` (secretary:
    poll every turn prologue) is always due. Minimal helper for recipient poll loop to
    determine "is it my turn", core of the pattern that translates idle waiting to
    active polling (maps "waiting" prose from report into active polling loop for pull environments).
    """
    if last_poll is None:
        return True
    return (now - last_poll) >= cadence_for(role)


class Transport:
    """Orchestrator for push-primary / pull-fallback wake delivery.

    Bundles one :class:`WakeQueue` (delivery canonical store) with any :class:`PushBackend`
    (primary / immediate accelerator). :meth:`deliver` **first durably enqueues in queue** then
    attempts push; if push confirms delivery, mark DELIVERED; if not, leave as ``UNDELIVERED``
    and defer to pull fallback. Recipient pulls own wakes via :meth:`poll` using claim-then-confirm.

    This "queue is canonical, push is accelerator" structure ensures delivery continues via pull
    even when backend is unavailable (even with :class:`NullPushBackend` or constant push failure)
    (report.md S5 Phase3 success condition b).
    """

    def __init__(
        self,
        queue: Optional[WakeQueue] = None,
        backend: Optional[PushBackend] = None,
        *,
        lease: float = 30.0,
        time_fn: Callable[[], float] = None,  # type: ignore[assignment]
    ) -> None:
        self.queue: WakeQueue = queue if queue is not None else InMemoryWakeQueue()
        self.backend = backend
        if lease <= 0:
            raise ValueError("Transport: lease must be > 0")
        self._lease = lease
        if time_fn is None:
            import time

            time_fn = time.monotonic
        self._time_fn = time_fn

    # -- Sender side (deliver) -----------------------------------------------

    def deliver(self, wake: Wake) -> str:
        """Deliver one wake. Return ``"push"`` (confirmed primary) or ``"queued"`` (awaiting pull).

        Procedure (canonical-first): First durably enqueue in queue (won't lose if push fails).
        If backend exists, attempt push best-effort; if confirmed delivery (``True``), mark DELIVERED
        and return ``"push"``. If backend absent / push fails / push raises, leave as ``UNDELIVERED``
        and return ``"queued"`` — recipient's :meth:`poll` picks it up via pull (= fallback).

        Re-delivering same ``id`` makes enqueue no-op, **not disrupting in-flight delivery**.
        Push is (re)attempted only for "newly enqueued this time" or "still ``UNDELIVERED`` (unclaimed)" wakes:

        - Already ``DELIVERED``: delivery confirmed. Don't overlay push or pull (return ``"push"``).
        - Already ``CLAIMED``: recipient already claimed via pull (awaiting confirm). Overlaying push with
          :meth:`~WakeQueue.mark_delivered` here steals the owner's lease and **breaks expiry-based
          redelivery protection** of claim-then-confirm (if owner crashes before confirm, wake won't revert
          to re-eligible). Respect active claim as-is, defer delivery to pull (return ``"queued"``).
        - ``UNDELIVERED`` / non-introspectable queue's ``None``: no active claim, so push retry is safe
          (can escalate from ``"queued"`` to ``"push"`` after backend recovery).
        """
        newly = self.queue.enqueue(wake)
        if not newly:
            state = _state_of(self.queue, wake.id)
            if state == DELIVERED:
                return "push"  # Already delivery confirmed. Don't re-send.
            if state == CLAIMED:
                # Respect ongoing pull claim (don't hijack with push confirmation).
                return "queued"
            # state is UNDELIVERED or None: no active claim. push retry is safe.
        if self.backend is not None and self._try_push(wake):
            self.queue.mark_delivered(wake.id)
            return "push"
        return "queued"

    def _try_push(self, wake: Wake) -> bool:
        """Call backend.push best-effort. Catch exceptions and treat as ``False`` (= undelivered)."""
        try:
            return bool(self.backend.push(wake))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - push is best-effort; failures defer to pull fallback.
            return False

    # -- Recipient side (poll) -----------------------------------------------

    def poll(
        self,
        recipient: str,
        *,
        owner: Optional[str] = None,
        limit: Optional[int] = None,
        confirm: bool = False,
    ) -> list[Wake]:
        """Claim undelivered wakes for ``recipient`` via pull (claim phase of claim-then-confirm).

        Claim and return ``UNDELIVERED`` wakes with lease occupation. **Does not confirm** (default
        ``confirm=False``): caller bears responsibility to **fully process the wake** then call
        :meth:`confirm_wakes` to mark ``DELIVERED``. If processing crashes (dies before confirm),
        lease expiry reverts wake to re-eligible and it is re-delivered (at-least-once: loss > duplication
        for idle-wake). Since claim-then-confirm is the crux of crash recovery, **default is no confirm**.
        For common case wanting to avoid confirm omission, use :meth:`poll_and_handle` (crash-safe
        receive loop that confirms per wake after handler succeeds).

        If ``confirm=True`` explicitly, immediately confirm claimed wakes **before returning** (receiving
        return value = delivery complete; intended only for simple cases where handler never fails /
        process-internal self-contained). Note: if processing crashes after poll returns, wakes are
        already ``DELIVERED`` and won't re-deliver (= that path tilts to at-most-once with loss risk).

        ``owner`` is claim ownership identifier (default to ``recipient`` if omitted). When polling the
        same recipient in parallel across multiple workers, pass **distinct owner per worker**. Three-state
        owner fencing rejects stale confirms to wakes re-claimed by another worker after lease expiry.
        """
        own = owner if owner is not None else recipient
        now = self._time_fn()
        wakes = self.queue.claim(
            recipient, now=now, lease=self._lease, owner=own, limit=limit
        )
        if confirm:
            for w in wakes:
                self.queue.confirm(w.id, owner=own, now=now)
        return wakes

    def poll_and_handle(
        self,
        recipient: str,
        handler: Callable[[Wake], Any],
        *,
        owner: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        """Crash-safe receive loop (recommended): claim -> handler(wake) -> confirm per wake.

        Claim each wake, confirm and mark ``DELIVERED`` **only for wakes handler returns without exception**.
        This eliminates loss window "received but died before processing": wakes where handler raises
        (and all subsequent unprocessed wakes) are not confirmed and re-delivered after lease expiry
        (at-least-once; assumes idempotent handler with :attr:`Wake.id` de-dup on recipient side).

        Return list of wakes successfully processed and confirmed. Handler exceptions **propagate uncaught**
        (caller observes failure; unconfirmed wakes are picked up on re-delivery). Confirm uses current
        time *after* handler success, so wakes where handler runs longer than lease are rejected by
        fencing (not confirmed) and re-delivered — set sufficiently large ``lease`` for long processing.

        ``owner`` / ``limit`` have same meaning as :meth:`poll` (default owner=recipient if omitted).
        """
        own = owner if owner is not None else recipient
        claimed = self.queue.claim(
            recipient, now=self._time_fn(), lease=self._lease, owner=own, limit=limit
        )
        handled: list[Wake] = []
        for w in claimed:
            handler(w)  # If handler raises, leave unconfirmed and propagate -> re-deliver after lease expiry.
            if self.queue.confirm(w.id, owner=own, now=self._time_fn()):
                handled.append(w)
        return handled

    def confirm_wakes(self, wakes: Iterable[Wake], *, owner: str) -> int:
        """Confirm batch of claimed wakes (confirmation API when using :meth:`poll` with ``confirm=False``).

        Return count successfully confirmed (where this ``owner`` holds the lease). Pass the same
        ``owner`` value as at claim time (if polled with default owner=recipient, pass recipient).
        If called after lease expiry, owner/expiry fencing rejects it and wake remains in queue as
        re-delivery candidate.
        """
        now = self._time_fn()
        confirmed = 0
        for w in wakes:
            if self.queue.confirm(w.id, owner=owner, now=now):
                confirmed += 1
        return confirmed

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """Return unconfirmed (undelivered) wakes (delegates to queue). For testing / monitoring."""
        return self.queue.pending(recipient)


def _state_of(queue: WakeQueue, wake_id: str) -> Optional[str]:
    """Fetch delivery state from queue (``state_of`` is a member of :class:`WakeQueue` Protocol).

    Since Protocol is structural and not enforced at runtime, defend with getattr so non-compliant
    queues lacking ``state_of`` don't crash :meth:`Transport.deliver` (absence returns ``None`` =
    "state unknown"; only forfeits early-return push dedup prevention, delivery itself continues).
    """
    fn = getattr(queue, "state_of", None)
    if fn is None:
        return None
    return fn(wake_id)


__all__ = [
    # Wake kinds
    "WAKE_LOOP_DONE",
    "WAKE_NEXT_ITERATION",
    "WAKE_DECISION_REQUEST",
    "WAKE_KINDS",
    # Delivery state
    "UNDELIVERED",
    "CLAIMED",
    "DELIVERED",
    # Types
    "Wake",
    "PushBackend",
    "CallablePushBackend",
    "NullPushBackend",
    "WakeQueue",
    "InMemoryWakeQueue",
    "Transport",
    # Role-based cadence
    "CADENCE_SECONDS",
    "DEFAULT_CADENCE_SECONDS",
    "cadence_for",
    "due_to_poll",
]
