"""Transport layer for wake delivery: push first / pull fallback / at-most-once (Issue #23).

report.md S3.3 / S4.6 / S5 Phase3. This module adds the concrete wake delivery
mechanism that sends loop **completion / next iteration / decision request** events
to another loop or a gateway (receiver). The claude-org runtime broker sidecar
belongs to that runtime and cannot be reused directly[^pattern-only], so this
implementation extracts only the **pattern** and keeps loop-agent dependency-free
(stdlib only).

[^pattern-only]: The runtime-specific broker implementation is not imported or
    wrapped here; only its delivery pattern is mirrored in this module.

Extracted patterns (source: ``knowledge/curated/broker-transport.md`` / backend
contract):

- **push first / pull fallback** (report.md S3.3). push (in-band injection) is a
  *low-latency accelerator*, while pull polling is the *canonical delivery path*
  (backend-neutral and free of interruption hazards). Even if push expires or is
  unavailable, delivery continues as long as the receiver actively polls at its
  role cadence. This layer directly models that asymmetry and satisfies "delivery
  continues through pull fallback even when the backend is unavailable" (report.md
  S5 Phase3 success criterion b).
- **at-most-once through three-state claim-then-confirm** (broker
  lost-message-window finding). A single ``delivered`` boolean has a loss window
  where the "delivered" flag is set even though the receiver never got the wake.
  The daemon-owned three-state model
  ``UNDELIVERED -> CLAIMED(lease, owner) -> DELIVERED`` plus claim-then-confirm
  closes that window: claim returns a row while holding it by lease, and the
  receiver confirms it as DELIVERED only after processing finishes. Rows whose
  lease expires before confirm return to UNDELIVERED (eligible again). Finalization
  (DELIVERED) is protected by fencing that requires matching ``owner`` and an
  unexpired lease, and finalized rows are never redelivered (at-most-once). When
  polling the same recipient concurrently with multiple workers, pass a distinct
  ``owner`` per worker (owner fencing assumes this to reject double finalization).
- **role-specific cadence** (broker pull-first finding). In pull environments
  where push can expire, "waiting" is translated into *active polling* rather than
  idle waiting. Receive triggers are designed asymmetrically by role (dispatcher
  3m / worker bounded / secretary turn-prologue). :data:`CADENCE_SECONDS` /
  :func:`due_to_poll` is the minimal form of that pattern.

Design boundaries (report.md S6 "runtime dependency of transport"):

- Runtime-independent and self-contained. This module does not depend on ``pane``,
  ``tmux``, ``renga``, or the ``broker`` CLI. The push backend is replaceable by
  injecting the :class:`PushBackend` Protocol (following the best-effort ``bool``
  contract from ``tools/peer_notify.py``), while the source of truth for delivery
  (the queue) is owned independently from the backend.
- Receivers are assumed to use **idempotent handlers**. A wake is identified by
  :attr:`Wake.id`; duplicate enqueue is a no-op, and even if a rare duplicate
  delivery occurs at the push/pull boundary, the receiver can de-dup by id
  (following report.md's policy that the remaining window is acceptable as
  at-least-once + idempotent display, and for idle-wake loss is worse than
  duplicate display).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, runtime_checkable

from .errors import ConfigError

# -- wake kinds (report.md S5 Phase3 "deliver wakes for loop completion/next iteration/decision request") ---------
#
# Constants let readers filter / dispatch without scattering string literals.
WAKE_LOOP_DONE = "loop_done"  # The loop ended (goal_met / stopped).
WAKE_NEXT_ITERATION = "next_iteration"  # Advance to the next iteration / wake the next task.
WAKE_DECISION_REQUEST = "decision_request"  # Request human judgment for an irreversible action (human gate).

WAKE_KINDS = (WAKE_LOOP_DONE, WAKE_NEXT_ITERATION, WAKE_DECISION_REQUEST)

# Receiver-side delivery states (three states). Owned by the daemon (= this queue).
UNDELIVERED = "undelivered"
CLAIMED = "claimed"
DELIVERED = "delivered"


@dataclass(frozen=True)
class Wake:
    """One wake to deliver.

    ``id`` is the **delivery identity** and the key for at-most-once / de-dup.
    For loop wakes, using a deterministic id such as
    ``f"{run_id}:{kind}:{iteration}"`` lets receivers de-dup resume redelivery or
    push/pull boundary duplicates by id (duplicate enqueue of the same id is a
    no-op). ``recipient`` is the destination (role name or peer id). ``payload``
    carries kind-specific supplemental information (finish reason, gate_key, etc.).
    """

    id: str
    kind: str
    recipient: str
    run_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Flatten into a dict that is easy to encode as JSON (for sink / backend serialization).

        ``payload`` is expanded first, then canonical fields (``id`` / ``kind`` /
        ``recipient`` / ``run_id``) overwrite it with **last value wins** semantics.
        This guarantees canonical fields, the source of truth for de-dup / routing,
        are preserved even if payload contains the same keys (preventing cases such
        as a payload-derived ``id`` disagreeing with the queue de-dup key and being
        sent to another destination). Same-name payload keys are hidden by canonical
        values (the contract is that canonical fields win for reserved names).
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
    """Minimal interface for push (primary low-latency accelerator).

    ``push(wake) -> bool`` is **best-effort** (following the bool contract from
    ``tools/peer_notify.py``): return ``True`` only when delivery is finalized;
    return ``False`` for anything else (backend unavailable, timeout, missing
    recipient, etc.). Implementations may raise (:class:`Transport` catches that
    and treats it as ``False``), but ideally should return ``False`` instead. Wakes
    that do not return ``True`` remain in the queue and are picked up by the
    receiver's pull poll (= pull fallback).
    """

    def push(self, wake: Wake) -> bool:
        ...


class CallablePushBackend:
    """Thin adapter that makes any ``callable(Wake) -> bool`` fit :class:`PushBackend`."""

    def __init__(self, fn: Callable[[Wake], bool]) -> None:
        self._fn = fn

    def push(self, wake: Wake) -> bool:
        return self._fn(wake)


class NullPushBackend:
    """Backend that always fails push (= explicit model of an unavailable backend).

    Represents configurations with no primary push path or a down backend. All
    wakes remain in the queue and are delivered only through pull fallback. This is
    the default configuration for "delivery continues through pull fallback even
    when the backend is unavailable" and also serves as the test baseline.
    """

    def push(self, wake: Wake) -> bool:
        return False


@dataclass
class _Entry:
    """Delivery state for one wake in the queue (three states + lease ownership)."""

    wake: Wake
    seq: int
    state: str = UNDELIVERED
    owner: Optional[str] = None
    lease_expiry: float = 0.0


@runtime_checkable
class WakeQueue(Protocol):
    """Source of truth for delivery (durable spine). Provides three-state claim-then-confirm.

    :class:`Transport` owns this queue as the source of truth independently from
    the backend (push). Even if push cannot finalize delivery, the wake remains in
    the queue and is delivered by the receiver's :meth:`claim` -> :meth:`confirm`
    pull path.
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
    """:class:`WakeQueue` in-memory implementation (three-state claim-then-confirm).

    The default queue that keeps wakes inside the loop process itself. If separate
    ``state.db`` persistence is needed, implement the same :class:`WakeQueue`
    Protocol with SQLite (this PoC demonstrates the at-most-once / fallback
    semantics in memory).

    State transitions (daemon-owned, row-level ownership preserves single-drainer
    behavior):

    - ``enqueue`` : no-op if the same ``id`` already exists (idempotent duplicate
      enqueue = foundation for de-dup).
    - ``claim``   : reclaim expired leases, then return destination-matching
      ``UNDELIVERED`` rows in seq order after marking them ``CLAIMED`` (owner +
      lease_expiry).
    - ``confirm`` : if the row is ``CLAIMED``, **owner is still the claim-time
      owner**, and the lease has not expired, mark it ``DELIVERED`` (terminal).
      A stale confirm for a row that another owner re-claimed after lease expiry is
      rejected by owner mismatch (fencing), so the loss window cannot mark a wake
      "DELIVERED even though it was not received" (assumption: concurrent pollers
      use distinct owners; see :meth:`~loop_agent.transport.Transport.poll`).
    - ``release_expired`` : return expired ``CLAIMED`` rows to ``UNDELIVERED``
      (eligible again).
    - ``mark_delivered`` : directly mark a wake whose push was finalized as
      ``DELIVERED`` (terminal). It transitions only from ``UNDELIVERED`` and
      returns ``False`` for ``CLAIMED`` so it does not steal an active pull claim
      (absorbing the push/pull boundary without breaking lease recovery).

    **Thread safety**: To prevent double claim when multiple workers (threads) poll
    the same recipient concurrently, all state-changing operations
    (enqueue/claim/confirm/release_expired/mark_delivered) and reads are serialized
    with one reentrant lock (making check-and-set atomic). ``claim`` uses
    :class:`threading.RLock` (reentrant) because it calls ``release_expired``
    internally. This makes concurrent poller owner fencing and at-most-once claim
    hold in practice.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._seq = 0
        # Reentrant lock that serializes state transitions (RLock for claim -> release_expired reentry).
        self._lock = threading.RLock()

    def enqueue(self, wake: Wake) -> bool:
        """Register a wake as ``UNDELIVERED``. If the same ``id`` already exists, no-op and return ``False``.

        Making duplicate enqueue idempotent means deliver retries or resume
        redelivery requests do not disturb existing rows (in-progress claims or
        DELIVERED rows), which is the foundation for avoiding duplicate delivery to
        humans / receivers.
        """
        if not wake.id:
            raise ConfigError("enqueue: Wake.id must be a non-empty string")
        with self._lock:
            if wake.id in self._entries:
                return False
            self._entries[wake.id] = _Entry(wake=wake, seq=self._seq)
            self._seq += 1
            return True

    def release_expired(self, *, now: float) -> int:
        """Return expired ``CLAIMED`` rows to ``UNDELIVERED`` and return the count.

        If the receiver dies before confirm (crashes between claim and confirm),
        the wake remains CLAIMED. Lease expiry makes it eligible again, so delivery
        does not stop and it can be re-claimed (falling back toward at-least-once:
        for idle-wake, loss is worse than duplication). ``owner`` is reset to
        ``None`` to reliably reject delayed confirms from the old owner.
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
        """Claim ``UNDELIVERED`` wakes for ``recipient`` by lease and return them (pull claim).

        First reclaim expired leases (:meth:`release_expired`), then mark up to
        ``limit`` destination-matching ``UNDELIVERED`` rows as ``CLAIMED`` in
        **registration order (seq)**. Each row records ``owner`` and the
        ``now + lease`` deadline. The caller finalizes returned wakes with
        :meth:`confirm` only after processing them fully (claim-then-confirm).
        """
        if lease <= 0:
            raise ConfigError("claim: lease must be > 0")
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
        """Finalize a claimed wake as ``DELIVERED`` (terminal).

        Finalizes and returns ``True`` only if the wake is ``CLAIMED``, the current
        ``owner`` matches the owner from claim time, and the lease has not expired.
        Everything else (already DELIVERED / owner mismatch = another party
        re-claimed after lease expiry / lease expired / missing) returns ``False``.
        This owner + expiry check acts as fencing and prevents a stale claimant
        from incorrectly marking a wake DELIVERED in the loss window.
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
                # Lease expired: this claim is no longer valid. release_expired will
                # return it to UNDELIVERED (even if it has not yet), so do not mark
                # it DELIVERED here.
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def mark_delivered(self, wake_id: str) -> bool:
        """Directly mark an ``UNDELIVERED`` wake as ``DELIVERED`` (terminal) for finalized push delivery.

        Used when the push backend returns ``True`` (finalized delivery). It
        transitions **only from ``UNDELIVERED``** and returns ``True`` if it could
        transition; otherwise (already ``DELIVERED`` / ``CLAIMED`` / missing) it
        returns ``False``.

        The key point is **not stealing ``CLAIMED``**: another poller can claim the
        same wake while push I/O is running (outside the queue lock). If this
        unconditionally marked it DELIVERED, it would clear the active claim's
        owner, and if that poller crashed before confirm, lease expiry could not
        make it eligible again, **losing the wake** (breaking claim-then-confirm
        crash recovery). When push and pull race for the same wake, the **pull
        claim is the delivery owner** and the push side is treated as a possible
        duplicate that receiver id de-dup handles (at-least-once; loss > duplicate).
        Rows push successfully marks DELIVERED are no longer UNDELIVERED, so pull
        will not claim them (absorbing the boundary).
        """
        with self._lock:
            e = self._entries.get(wake_id)
            if e is None:
                return False
            if e.state != UNDELIVERED:
                # Already DELIVERED, or already claimed by another poller (CLAIMED). Do not steal the claim.
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """Return unfinalized (``UNDELIVERED`` / ``CLAIMED``) wakes in registration order, optionally filtered by recipient."""
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
        """Return the current delivery state for ``wake_id`` (or ``None`` if missing). For tests/introspection."""
        with self._lock:
            e = self._entries.get(wake_id)
            return e.state if e is not None else None


# Role-specific poll cadence (seconds). Asymmetric design from report.md S3.2 /
# broker pull-first findings.
# In pull environments where push can expire, "waiting" is translated into active
# polling rather than idle waiting.
#
# - dispatcher : monitoring /loop 3m equivalent = active poll every 180s.
# - worker     : bounded review-watch after completion report = short-interval poll.
# - secretary  : human-dialogue-centered, cannot block poll -> poll at every turn prologue (0 = always due).
CADENCE_SECONDS: dict[str, float] = {
    "dispatcher": 180.0,
    "worker": 60.0,
    "secretary": 0.0,
}

# Default cadence for unknown roles (conservatively worker-equivalent).
DEFAULT_CADENCE_SECONDS = 60.0


def cadence_for(role: str) -> float:
    """Return the poll interval (seconds) for ``role``. Unknown roles use :data:`DEFAULT_CADENCE_SECONDS`."""
    return CADENCE_SECONDS.get(role, DEFAULT_CADENCE_SECONDS)


def due_to_poll(role: str, last_poll: Optional[float], now: float) -> bool:
    """Return whether ``role`` should actively poll at ``now``.

    If ``last_poll`` is ``None`` (never polled), it is always due. Otherwise this
    checks ``now - last_poll >= cadence_for(role)``. Roles with cadence ``0``
    (secretary: poll at every turn prologue) are always due. This is the minimal
    helper for receiver poll loops to decide "is it my turn?", and it is the core
    pattern that translates idle waiting into active polling in pull environments.
    """
    if last_poll is None:
        return True
    return (now - last_poll) >= cadence_for(role)


class Transport:
    """Wake delivery orchestrator for push first / pull fallback.

    Binds one :class:`WakeQueue` (delivery source of truth) with an optional
    :class:`PushBackend` (primary low-latency accelerator). :meth:`deliver`
    **first durably enqueues into the queue**, then tries push. If push finalizes
    delivery it marks the wake DELIVERED; otherwise it leaves the wake
    ``UNDELIVERED`` and delegates to pull fallback. Receivers use :meth:`poll` to
    take wakes addressed to them with claim-then-confirm.

    This "queue is source of truth, push is accelerator" structure keeps delivery
    going through pull even when the backend is unavailable (or
    :class:`NullPushBackend` makes push always fail), satisfying report.md S5
    Phase3 success criterion b.
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
            raise ConfigError("Transport: lease must be > 0")
        self._lease = lease
        if time_fn is None:
            import time

            time_fn = time.monotonic
        self._time_fn = time_fn

    # -- Sender side (deliver) ----------------------------------------------

    def deliver(self, wake: Wake) -> str:
        """Deliver one wake. Return ``"push"`` (finalized by primary path) or ``"queued"`` (waiting for pull).

        Procedure (source of truth first): durably enqueue into the queue first
        (so push failure cannot lose the wake). If a backend exists, try push
        best-effort; if delivery is finalized (``True``), mark it DELIVERED and
        return ``"push"``. If the backend is absent, push fails, or push raises,
        leave it ``UNDELIVERED`` and return ``"queued"`` -- the receiver's
        :meth:`poll` will pick it up by pull (= fallback).

        Re-delivering the same ``id`` makes enqueue a no-op and **does not disturb
        in-progress delivery**. Push is (re)tried only for wakes that were newly
        enqueued this time or are still ``UNDELIVERED`` (not claimed by anyone):

        - Already ``DELIVERED``: delivery is finalized. Do not add another push or
          pull delivery (return ``"push"``).
        - Already ``CLAIMED``: the receiver has claimed it by pull (waiting for
          confirm). Adding push and then calling :meth:`~WakeQueue.mark_delivered`
          here would steal the owner's lease and **break expiry redelivery
          protection** in claim-then-confirm (a wake whose owner crashed before
          confirm would not become eligible again). Respect the active claim and
          delegate delivery to pull (``"queued"``).
        - ``UNDELIVERED`` / ``None`` for queues that cannot be introspected: there
          is no active claim, so retrying push is safe (after backend recovery,
          ``queued`` can be promoted to ``push``).
        """
        newly = self.queue.enqueue(wake)
        if not newly:
            state = _state_of(self.queue, wake.id)
            if state == DELIVERED:
                return "push"  # Delivery is already finalized. Do not resend.
            if state == CLAIMED:
                # Respect the in-progress pull claim (do not steal finalization by push).
                return "queued"
            # state is UNDELIVERED or None: no active claim, so push retry is safe.
        if self.backend is not None and self._try_push(wake):
            self.queue.mark_delivered(wake.id)
            return "push"
        return "queued"

    def _try_push(self, wake: Wake) -> bool:
        """Call backend.push best-effort. Catch exceptions and treat them as ``False`` (= not delivered)."""
        try:
            return bool(self.backend.push(wake))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - push is best-effort. Failures are delegated to pull fallback.
            return False

    # -- Receiver side (poll) ------------------------------------------------

    def poll(
        self,
        recipient: str,
        *,
        owner: Optional[str] = None,
        limit: Optional[int] = None,
        confirm: bool = False,
    ) -> list[Wake]:
        """Claim undelivered wakes for ``recipient`` by pull (claim step of claim-then-confirm).

        Claims ``UNDELIVERED`` wakes by lease and returns them. It **does not
        finalize** by default (``confirm=False``): the caller is responsible for
        calling :meth:`confirm_wakes` to mark wakes ``DELIVERED`` **after fully
        processing them**. If processing crashes (= dies before confirm), lease
        expiry makes that wake eligible again and it is redelivered
        (at-least-once: for idle-wake, loss is worse than duplication). This
        claim-then-confirm path is the key to crash recovery, so the default is
        **not to confirm**. For the common case that wants to avoid missed confirms,
        prefer :meth:`poll_and_handle`, a crash-safe receive loop that confirms
        each wake after handler success.

        If ``confirm=True`` is explicit, claimed wakes are **confirmed immediately
        before being returned** (receiving the return value can be treated as
        delivery complete; intended only for simple cases where the handler never
        fails / processing is self-contained in-process). In this mode, if
        processing crashes after poll returns, the wake is already ``DELIVERED`` and
        will not be redelivered (= only that path can lose wakes while remaining
        at-most-once).

        ``owner`` identifies the claim owner (defaults to ``recipient``). If
        multiple workers poll concurrently for the same receiver, pass a **distinct
        owner** per worker. The three-state owner fencing rejects "stale confirms
        for wakes another worker re-claimed after lease expiry."
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
        """Crash-safe receive loop that runs claim -> handler(wake) -> confirm per wake (recommended).

        Claims each wake and confirms it as ``DELIVERED`` **only if**
        ``handler(wake)`` returns without exception. This removes the "received but
        died before processing" loss window: wakes whose handler raises (and later
        unprocessed wakes) are not confirmed and are redelivered after lease expiry
        (at-least-once; assumes the receiver uses an idempotent handler that
        de-dups by :attr:`Wake.id`).

        Returns the list of wakes that were processed and finalized successfully.
        ``handler`` exceptions are **not swallowed** and propagate (the caller can
        observe failure; unfinalized wakes are picked up by redelivery). Confirm is
        performed at the current time *after* handler success, so wakes whose
        handler runs longer than the lease are rejected by fencing (not finalized)
        and redelivered -- use a large enough ``lease`` for long processing.

        ``owner`` / ``limit`` have the same meaning as :meth:`poll` (default
        owner=recipient).
        """
        own = owner if owner is not None else recipient
        claimed = self.queue.claim(
            recipient, now=self._time_fn(), lease=self._lease, owner=own, limit=limit
        )
        handled: list[Wake] = []
        for w in claimed:
            handler(w)  # If this raises, it propagates unconfirmed -> redelivery after lease expiry.
            if self.queue.confirm(w.id, owner=own, now=self._time_fn()):
                handled.append(w)
        return handled

    def confirm_wakes(self, wakes: Iterable[Wake], *, owner: str) -> int:
        """Finalize claimed wakes (confirmation API for using :meth:`poll` with ``confirm=False``).

        Returns the count that could be finalized (this ``owner`` held the lease).
        Pass the same ``owner`` that was used at claim time (if poll defaulted to
        owner=recipient, pass recipient). Calls after lease expiry are rejected by
        owner/expiry fencing, and that wake remains in the queue for redelivery.
        """
        now = self._time_fn()
        confirmed = 0
        for w in wakes:
            if self.queue.confirm(w.id, owner=owner, now=now):
                confirmed += 1
        return confirmed

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """Return unfinalized (undelivered) wakes by delegating to the queue. For tests/monitoring."""
        return self.queue.pending(recipient)


def _state_of(queue: WakeQueue, wake_id: str) -> Optional[str]:
    """Read queue delivery state (``state_of`` is part of the :class:`WakeQueue` Protocol).

    Protocols are structural and not enforced at runtime, so use getattr to keep
    :meth:`Transport.deliver` from failing even with a non-conforming queue that
    lacks ``state_of`` (missing means ``None`` = "state unknown"; this only gives
    up the early return that prevents duplicate push, while delivery itself
    continues).
    """
    fn = getattr(queue, "state_of", None)
    if fn is None:
        return None
    return fn(wake_id)


# ---------------------------------------------------------------------------
# Cross-process backends (Issue #41)
#
# :class:`InMemoryWakeQueue` can share the source of truth only inside one
# process. To deliver wakes to loops / gateways in other processes, the queue's
# source of truth must live in an **out-of-process durable store**. Because the
# :class:`WakeQueue` Protocol is backend-neutral, implementing the same three-state
# claim-then-confirm semantics in SQLite (stdlib only) / Redis (optional dep) lets
# backends be swapped without changing the :class:`Transport` public API (in-memory
# by default, SQLite/Redis when explicit).
#
# Design decisions:
#
# - **serialization = JSON** (not pickle). ``payload`` is JSON-shaped (as assumed
#   by :meth:`Wake.to_dict`), and JSON is safe across processes/languages without
#   arbitrary-code-execution risk. ``payload`` may contain only JSON-serializable
#   values (unsupported values raise ``ConfigError`` during enqueue).
# - **key namespace convention**. Redis uses keys such as ``{namespace}:wake:{id}``
#   / ``{namespace}:recipient:{r}`` to avoid collisions with other uses (default
#   namespace ``"loop_agent"``). SQLite separates by table name (default
#   ``wakes``).
# - **TTL / cleanup**. DELIVERED records remain in long-running loops. Redis sets
#   ``EXPIRE`` on finalization (``delivered_ttl``), while SQLite is explicitly
#   reclaimed by :meth:`SqliteWakeQueue.purge_delivered` (because monotonic clocks
#   cannot be used for wall-clock TTL, explicit cleanup is the SQLite default).
#
# **Important cross-process clock note**: lease expiry compares ``now`` supplied by
# :class:`Transport` (default ``time.monotonic``) against the shared backend.
# ``time.monotonic`` has a **different origin per process**, so configurations that
# share the same SQLite/Redis backend across multiple processes must pass a
# **wall-clock (``time_fn=time.time``) to each process's :class:`Transport` to align
# clocks**. Otherwise, comparing a ``lease_expiry`` written by one process against
# another process's monotonic clock breaks lease decisions.


def _dumps_payload(payload: Mapping[str, Any]) -> str:
    """Collapse ``payload`` into a JSON string (for backend persistence).

    Non-JSON-serializable values translate ``TypeError`` into ``ConfigError`` so
    callers get a clear enqueue input-validation error (not using pickle = not
    introducing arbitrary-code-execution risk). The original ``TypeError`` is
    preserved as ``__cause__`` (``raise ... from exc``).
    """
    try:
        return json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    except TypeError as exc:
        raise ConfigError(f"Wake.payload must be JSON-serializable: {exc}") from exc


def _make_wake(id: str, kind: str, recipient: str, run_id: str, payload_json: str) -> Wake:
    """Restore fields read from a backend into :class:`Wake`."""
    return Wake(
        id=id,
        kind=kind,
        recipient=recipient,
        run_id=run_id,
        payload=json.loads(payload_json) if payload_json else {},
    )


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, *, what: str) -> str:
    """Validate an SQL identifier (table name) to prevent SQL injection; table names cannot be bound."""
    if not _IDENT_RE.match(name):
        raise ConfigError(f"{what} must match {_IDENT_RE.pattern!r}, got {name!r}")
    return name


class SqliteWakeQueue:
    """:class:`WakeQueue` SQLite implementation (stdlib ``sqlite3`` only, out-of-process durability).

    The three-state claim-then-confirm / owner fencing semantics are **fully
    equivalent** to :class:`InMemoryWakeQueue`, and placing the source of truth in a
    SQLite file (or ``:memory:``) enables delivery across processes. Passing a file
    path to ``path`` lets multiple processes share the same source of truth;
    ``":memory:"`` (default) is for durable tests inside a single process.

    **Atomicity**: State-changing operations
    (enqueue/claim/confirm/release_expired/mark_delivered) take a write lock with
    ``BEGIN IMMEDIATE`` and fit check-and-set into one transaction. Concurrent
    pollers inside a process are serialized with an in-process
    :class:`threading.RLock`; cross-process access waits on SQLite file locks plus
    ``busy_timeout`` (``WAL`` improves read/write concurrency). This prevents double
    claim even when multiple processes/threads poll concurrently.

    **Connection**: Opens one connection with ``check_same_thread=False`` and
    protects it with the lock above (``:memory:`` creates a separate DB per
    connection, so a single connection is required). ``isolation_level=None``
    (autocommit) is used for explicit transaction control. Call :meth:`close` when
    finished, or use a ``with`` statement.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        table: str = "wakes",
        busy_timeout: float = 5.0,
    ) -> None:
        self._path = path
        self._t = _validate_identifier(table, what="table")
        # claim calls release_expired-equivalent SQL inside the same tx. RLock allows reentry.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        # WAL improves read/write concurrency for file DBs (effectively a no-op for :memory:).
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx():
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._t} (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    id           TEXT    NOT NULL UNIQUE,
                    kind         TEXT    NOT NULL,
                    recipient    TEXT    NOT NULL,
                    run_id       TEXT    NOT NULL,
                    payload      TEXT    NOT NULL,
                    state        TEXT    NOT NULL,
                    owner        TEXT,
                    lease_expiry REAL    NOT NULL DEFAULT 0
                )
                """
            )
            # claim/pending: fetch destination + state in seq order. release_expired: sweep by state + lease.
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._t}_recipient_state "
                f"ON {self._t}(recipient, state, seq)"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._t}_state_lease "
                f"ON {self._t}(state, lease_expiry)"
            )

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Take a write lock with ``BEGIN IMMEDIATE`` and serialize through commit/rollback."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def enqueue(self, wake: Wake) -> bool:
        if not wake.id:
            raise ConfigError("enqueue: Wake.id must be a non-empty string")
        payload = _dumps_payload(wake.payload)
        with self._tx():
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO {self._t} "
                "(id, kind, recipient, run_id, payload, state, owner, lease_expiry) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                (wake.id, wake.kind, wake.recipient, wake.run_id, payload, UNDELIVERED),
            )
            return cur.rowcount > 0  # INSERT OR IGNORE: inserted=1 / ignored duplicate id=0.

    def _release_expired_locked(self, now: float) -> int:
        cur = self._conn.execute(
            f"UPDATE {self._t} SET state = ?, owner = NULL "
            "WHERE state = ? AND lease_expiry <= ?",
            (UNDELIVERED, CLAIMED, now),
        )
        return cur.rowcount

    def release_expired(self, *, now: float) -> int:
        with self._tx():
            return self._release_expired_locked(now)

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        if lease <= 0:
            raise ConfigError("claim: lease must be > 0")
        with self._tx():
            self._release_expired_locked(now)
            sql = (
                f"SELECT id, kind, recipient, run_id, payload FROM {self._t} "
                "WHERE state = ? AND recipient = ? ORDER BY seq"
            )
            params: list[Any] = [UNDELIVERED, recipient]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = self._conn.execute(sql, params).fetchall()
            new_expiry = now + lease
            for r in rows:
                self._conn.execute(
                    f"UPDATE {self._t} SET state = ?, owner = ?, lease_expiry = ? WHERE id = ?",
                    (CLAIMED, owner, new_expiry, r["id"]),
                )
            return [
                _make_wake(r["id"], r["kind"], r["recipient"], r["run_id"], r["payload"])
                for r in rows
            ]

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        with self._tx():
            # Finalize only CLAIMED rows with matching owner + unexpired lease (lease_expiry > now), as fencing.
            cur = self._conn.execute(
                f"UPDATE {self._t} SET state = ?, owner = NULL "
                "WHERE id = ? AND state = ? AND owner = ? AND lease_expiry > ?",
                (DELIVERED, wake_id, CLAIMED, owner, now),
            )
            return cur.rowcount > 0

    def mark_delivered(self, wake_id: str) -> bool:
        with self._tx():
            # Finalize only from UNDELIVERED (do not steal an active CLAIMED claim).
            cur = self._conn.execute(
                f"UPDATE {self._t} SET state = ?, owner = NULL WHERE id = ? AND state = ?",
                (DELIVERED, wake_id, UNDELIVERED),
            )
            return cur.rowcount > 0

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        with self._lock:
            sql = (
                f"SELECT id, kind, recipient, run_id, payload FROM {self._t} "
                "WHERE state != ?"
            )
            params: list[Any] = [DELIVERED]
            if recipient is not None:
                sql += " AND recipient = ?"
                params.append(recipient)
            sql += " ORDER BY seq"
            rows = self._conn.execute(sql, params).fetchall()
            return [
                _make_wake(r["id"], r["kind"], r["recipient"], r["run_id"], r["payload"])
                for r in rows
            ]

    def state_of(self, wake_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT state FROM {self._t} WHERE id = ?", (wake_id,)
            ).fetchone()
            return row["state"] if row is not None else None

    def purge_delivered(self) -> int:
        """Physically delete finalized (``DELIVERED``) records and return the deleted count (cleanup).

        Long-running loops leave finalized rows behind, so call this periodically
        as maintenance to reclaim them. It does not touch unfinalized
        (``UNDELIVERED`` / ``CLAIMED``) rows, so in-flight wakes are not lost.
        """
        with self._tx():
            cur = self._conn.execute(
                f"DELETE FROM {self._t} WHERE state = ?", (DELIVERED,)
            )
            return cur.rowcount

    def close(self) -> None:
        """Close the SQLite connection (for ``:memory:``, this discards the whole DB)."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteWakeQueue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _import_redis() -> Any:
    """Load optional dependency ``redis`` through an import gate (friendly error if missing)."""
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "RedisWakeQueue requires the optional 'redis' dependency. "
            "Install it with: pip install 'loop-agent[redis]'"
        ) from exc
    return redis


def _text(value: Any) -> str:
    """Normalize bytes/str returned by redis-py to str (independent of ``decode_responses``)."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class RedisWakeQueue:
    """:class:`WakeQueue` Redis implementation (optional dep ``redis``, out-of-process durability).

    The three-state claim-then-confirm / owner fencing semantics are equivalent to
    :class:`InMemoryWakeQueue` / :class:`SqliteWakeQueue`. Placing the source of
    truth in Redis enables wake delivery between processes on different hosts. In
    environments without ``redis`` installed, construction raises a friendly
    :class:`ImportError` (import gate).

    **Data model** (key namespace convention; default ``namespace`` is
    ``"loop_agent"``):

    - ``{ns}:wake:{id}``        : hash for one wake
      (kind/recipient/run_id/payload/state/owner/lease_expiry/seq).
    - ``{ns}:recipient:{r}``    : sorted set for destination ``r`` (score=seq,
      member=id). Used to scan in **seq order** for claim/pending. Finalized /
      delivered ids are removed from here.
    - ``{ns}:claimed``          : sorted set for CLAIMED rows (score=lease_expiry,
      member=id). :meth:`release_expired` can efficiently sweep expired rows with
      ``ZRANGEBYSCORE -inf now``.
    - ``{ns}:seq``              : monotonic counter (``INCR``).
    - ``{ns}:recipients``       : set of known recipients (for the full scan in
      ``pending(None)``).
    - ``{ns}:lock``             : distributed lock that serializes state changes
      (``SET NX PX``).

    **Atomicity**: Each state-changing operation is wrapped in an in-process
    :class:`threading.RLock` (in-process serialization) and the Redis distributed
    lock ``{ns}:lock`` (``SET NX PX`` + token-checked **atomic** release),
    serializing check-and-set across processes (preventing double claim). The lock
    auto-expires after ``lock_ttl`` seconds, so a crashed lock holder does not
    deadlock. Release is performed by server-side Lua compare-and-delete
    (:meth:`_release_lock`) **only when the token is ours**, so it cannot
    accidentally delete a lock that another party reacquired after expiry.

    **Known limitation (cross-process strength)**: The distributed lock is a TTL
    lock based on ``lock_ttl``. If one operation **exceeds** ``lock_ttl`` (STW-GC /
    network delay / sweep of a huge recipient zset, etc.), the lock can expire
    mid-operation and another process may double-claim the same wake. In that
    window, delivery degrades to **at-least-once** (including resurrection where a
    finalized wake returns to CLAIMED). The transport contract is already
    at-least-once + idempotent handler (the receiver de-dups by :attr:`Wake.id`),
    so this is recoverable, but ``lock_ttl`` should be large enough to reliably
    contain one operation. For cross-process configurations that require **strict
    at-most-once, do not depend on TTL locks**; prefer :class:`SqliteWakeQueue`
    (``BEGIN IMMEDIATE`` does not expire during an operation). Equivalence with
    :class:`InMemoryWakeQueue` / :class:`SqliteWakeQueue` assumes each operation
    finishes within ``lock_ttl``.

    **TTL / cleanup**: On finalization (DELIVERED), set ``EXPIRE`` on the wake hash
    (``delivered_ttl`` seconds, default 1 day) to automatically reclaim remnants
    from long-running loops. Disable with ``delivered_ttl=None``. When a
    recipient's pending set becomes empty, remove it from the ``{ns}:recipients``
    registry too, preventing unbounded registry growth (no leak even with
    high-cardinality peer ids as destinations).

    For tests or DI, a redis-py-compatible ``client`` can be injected directly. If
    omitted, ``redis.Redis.from_url`` is used with ``url`` (if neither is provided,
    ``ConfigError`` is raised).
    """

    def __init__(
        self,
        client: Any = None,
        *,
        url: Optional[str] = None,
        namespace: str = "loop_agent",
        delivered_ttl: Optional[float] = 86400.0,
        lock_ttl: float = 10.0,
        lock_timeout: float = 10.0,
    ) -> None:
        if client is None:
            redis = _import_redis()
            if url is None:
                raise ConfigError("RedisWakeQueue: provide either `client` or `url`")
            client = redis.Redis.from_url(url)
        self._r = client
        self._ns = namespace
        self._delivered_ttl = delivered_ttl
        self._lock_ttl = lock_ttl
        self._lock_timeout = lock_timeout
        self._lock = threading.RLock()
        self._k_seq = f"{namespace}:seq"
        self._k_claimed = f"{namespace}:claimed"
        self._k_recipients = f"{namespace}:recipients"
        self._k_lock = f"{namespace}:lock"

    def _k_wake(self, wake_id: str) -> str:
        return f"{self._ns}:wake:{wake_id}"

    def _k_recipient(self, recipient: str) -> str:
        return f"{self._ns}:recipient:{recipient}"

    # Lua that releases the lock only when we hold it (compare-and-delete; atomic in one command).
    _RELEASE_LOCK_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )

    def _release_lock(self, token: str) -> None:
        """Release the lock atomically **only when the token is ours** (do not delete another lock).

        If check-then-delete takes two round trips, the lock can expire between GET
        and DELETE and another process can reacquire it, causing us to delete that
        other lock by mistake. Server-side Lua compare-and-delete collapses this
        into one command. Clients without ``eval`` support fall back to best-effort
        check-then-delete.
        """
        try:
            self._r.eval(self._RELEASE_LOCK_LUA, 1, self._k_lock, token)
        except Exception:  # noqa: BLE001 - clients without eval support fall back to best-effort release.
            if _text(self._r.get(self._k_lock)) == token:
                self._r.delete(self._k_lock)

    @contextmanager
    def _dlock(self) -> Iterator[None]:
        """Serialize state changes with an in-process RLock + Redis distributed lock."""
        import time as _time

        with self._lock:
            token = uuid.uuid4().hex
            start = _time.monotonic()
            while True:
                if self._r.set(self._k_lock, token, nx=True, px=int(self._lock_ttl * 1000)):
                    break
                if _time.monotonic() - start > self._lock_timeout:
                    raise TimeoutError(
                        f"RedisWakeQueue: could not acquire {self._k_lock} "
                        f"within {self._lock_timeout}s"
                    )
                _time.sleep(0.01)
            try:
                yield
            finally:
                self._release_lock(token)

    def _hgetall(self, key: str) -> dict[str, str]:
        raw = self._r.hgetall(key)
        return {_text(k): _text(v) for k, v in raw.items()}

    def _terminalize(self, wake_id: str, recipient: str) -> None:
        """Mark a wake DELIVERED (terminal) and remove it from ordering/claimed indexes (+TTL)."""
        wkey = self._k_wake(wake_id)
        rkey = self._k_recipient(recipient)
        self._r.hset(wkey, mapping={"state": DELIVERED, "owner": ""})
        self._r.zrem(rkey, wake_id)
        self._r.zrem(self._k_claimed, wake_id)
        # Remove recipient from the registry when its pending set is empty (prevents
        # unbounded growth of {ns}:recipients). Re-enqueue restores it with sadd, so
        # the full scan in pending(None) remains correct.
        if self._r.zcard(rkey) == 0:
            self._r.srem(self._k_recipients, recipient)
        if self._delivered_ttl is not None:
            self._r.expire(wkey, int(self._delivered_ttl))

    def enqueue(self, wake: Wake) -> bool:
        if not wake.id:
            raise ConfigError("enqueue: Wake.id must be a non-empty string")
        payload = _dumps_payload(wake.payload)
        wkey = self._k_wake(wake.id)
        with self._dlock():
            if self._r.exists(wkey):
                return False  # Same id is a no-op (foundation for de-dup).
            seq = int(self._r.incr(self._k_seq))
            self._r.hset(
                wkey,
                mapping={
                    "id": wake.id,
                    "kind": wake.kind,
                    "recipient": wake.recipient,
                    "run_id": wake.run_id,
                    "payload": payload,
                    "state": UNDELIVERED,
                    "owner": "",
                    "lease_expiry": "0",
                    "seq": str(seq),
                },
            )
            self._r.zadd(self._k_recipient(wake.recipient), {wake.id: seq})
            self._r.sadd(self._k_recipients, wake.recipient)
            return True

    def _release_expired_locked(self, now: float) -> int:
        expired = self._r.zrangebyscore(self._k_claimed, "-inf", now)
        count = 0
        for raw in expired:
            wid = _text(raw)
            self._r.hset(self._k_wake(wid), mapping={"state": UNDELIVERED, "owner": ""})
            self._r.zrem(self._k_claimed, wid)
            count += 1
        return count

    def release_expired(self, *, now: float) -> int:
        with self._dlock():
            return self._release_expired_locked(now)

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        if lease <= 0:
            raise ConfigError("claim: lease must be > 0")
        with self._dlock():
            self._release_expired_locked(now)
            ids = [_text(x) for x in self._r.zrange(self._k_recipient(recipient), 0, -1)]
            out: list[Wake] = []
            new_expiry = now + lease
            for wid in ids:
                if limit is not None and len(out) >= limit:
                    break
                h = self._hgetall(self._k_wake(wid))
                if not h:
                    # Stale index member whose hash disappeared by TTL, etc. Clean it up and skip.
                    self._r.zrem(self._k_recipient(recipient), wid)
                    continue
                if h["state"] != UNDELIVERED:
                    continue
                self._r.hset(
                    self._k_wake(wid),
                    mapping={"state": CLAIMED, "owner": owner, "lease_expiry": repr(new_expiry)},
                )
                self._r.zadd(self._k_claimed, {wid: new_expiry})
                out.append(
                    _make_wake(h["id"], h["kind"], h["recipient"], h["run_id"], h["payload"])
                )
            return out

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        with self._dlock():
            h = self._hgetall(self._k_wake(wake_id))
            if not h:
                return False
            if h["state"] != CLAIMED:
                return False
            if h["owner"] != owner:
                return False
            if float(h["lease_expiry"]) <= now:
                return False
            self._terminalize(wake_id, h["recipient"])
            return True

    def mark_delivered(self, wake_id: str) -> bool:
        with self._dlock():
            h = self._hgetall(self._k_wake(wake_id))
            if not h:
                return False
            if h["state"] != UNDELIVERED:
                return False
            self._terminalize(wake_id, h["recipient"])
            return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        with self._lock:
            if recipient is not None:
                recipients = [recipient]
            else:
                recipients = sorted(_text(x) for x in self._r.smembers(self._k_recipients))
            items: list[tuple[float, Wake]] = []
            for rcp in recipients:
                for raw, score in self._r.zrange(
                    self._k_recipient(rcp), 0, -1, withscores=True
                ):
                    wid = _text(raw)
                    h = self._hgetall(self._k_wake(wid))
                    if not h or h["state"] == DELIVERED:
                        continue
                    items.append(
                        (
                            float(score),
                            _make_wake(
                                h["id"], h["kind"], h["recipient"], h["run_id"], h["payload"]
                            ),
                        )
                    )
            items.sort(key=lambda t: t[0])  # Sort by seq (= score) across all recipients.
            return [w for _, w in items]

    def state_of(self, wake_id: str) -> Optional[str]:
        with self._lock:
            st = self._r.hget(self._k_wake(wake_id), "state")
            return _text(st) if st is not None else None


def open_wake_queue(backend: str = "memory", **opts: Any) -> WakeQueue:
    """Convenience factory that creates a :class:`WakeQueue` from a backend name.

    - ``"memory"`` (default) : :class:`InMemoryWakeQueue` (inside one process).
    - ``"sqlite"``          : :class:`SqliteWakeQueue` (``path`` / ``table`` etc. via ``opts``).
    - ``"redis"``           : :class:`RedisWakeQueue` (``client`` or ``url`` via ``opts``).

    Pass the created queue to :class:`Transport` to choose a backend without
    changing the public API (in-memory by default, SQLite/Redis when explicit).
    """
    if backend == "memory":
        if opts:
            raise ConfigError(f"open_wake_queue('memory') takes no options, got {sorted(opts)}")
        return InMemoryWakeQueue()
    if backend == "sqlite":
        return SqliteWakeQueue(**opts)
    if backend == "redis":
        return RedisWakeQueue(**opts)
    raise ConfigError(f"unknown backend {backend!r} (expected 'memory' / 'sqlite' / 'redis')")


__all__ = [
    # wake kinds
    "WAKE_LOOP_DONE",
    "WAKE_NEXT_ITERATION",
    "WAKE_DECISION_REQUEST",
    "WAKE_KINDS",
    # delivery states
    "UNDELIVERED",
    "CLAIMED",
    "DELIVERED",
    # types
    "Wake",
    "PushBackend",
    "CallablePushBackend",
    "NullPushBackend",
    "WakeQueue",
    "InMemoryWakeQueue",
    "SqliteWakeQueue",
    "RedisWakeQueue",
    "open_wake_queue",
    "Transport",
    # role-specific cadence
    "CADENCE_SECONDS",
    "DEFAULT_CADENCE_SECONDS",
    "cadence_for",
    "due_to_poll",
]
