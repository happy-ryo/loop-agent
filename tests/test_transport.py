"""Validation for wake delivery transport (Issue #23, report.md S5 Phase3).

The core success condition is **"delivery continues through pull fallback even when
the backend is unreachable"** (report.md S5 Phase3 (b)).
This verifies push primary, pull fallback, at-most-once, and per-role cadence.
"""

from __future__ import annotations

import threading
import time

import pytest

from loop_agent.transport import (
    CLAIMED,
    DELIVERED,
    UNDELIVERED,
    CallablePushBackend,
    InMemoryWakeQueue,
    NullPushBackend,
    Transport,
    WAKE_LOOP_DONE,
    Wake,
    cadence_for,
    due_to_poll,
)


class ManualClock:
    """Deterministic clock that only moves when advanced explicitly (for lease expiry tests)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wake(i: int, recipient: str = "coordinator") -> Wake:
    return Wake(
        id=f"r1:{WAKE_LOOP_DONE}:{i}",
        kind=WAKE_LOOP_DONE,
        recipient=recipient,
        run_id="r1",
        payload={"n": i},
    )


# -- push primary -----------------------------------------------------------


def test_push_primary_delivers_and_marks_delivered():
    """When the backend is healthy, push primary delivers immediately and marks the queue DELIVERED."""
    pushed: list[Wake] = []
    backend = CallablePushBackend(lambda w: (pushed.append(w), True)[1])
    queue = InMemoryWakeQueue()
    t = Transport(queue, backend, time_fn=ManualClock())

    route = t.deliver(_wake(0))

    assert route == "push"
    assert [w.id for w in pushed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED
    # Already delivered by push, so pull finds nothing.
    assert t.poll("coordinator") == []


# -- pull fallback (core success condition) ---------------------------------


def test_pull_fallback_when_backend_down():
    """Even with an unreachable backend (NullPushBackend), pull polling keeps delivery moving."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)

    routes = [t.deliver(_wake(i)) for i in range(3)]
    # All push attempts fail -> every wake remains queued.
    assert routes == ["queued", "queued", "queued"]

    # The receiver uses poll_and_handle to claim -> handle -> confirm. Delivery works even when push is down.
    seen: list[str] = []
    handled = t.poll_and_handle("coordinator", lambda w: seen.append(w.id))
    assert seen == ["r1:loop_done:0", "r1:loop_done:1", "r1:loop_done:2"]
    assert [w.id for w in handled] == seen
    assert all(queue.state_of(i) == DELIVERED for i in seen)

    # Confirmed wakes are not redelivered after the lease expires (at-most-once).
    clock.advance(100.0)
    assert t.poll_and_handle("coordinator", lambda w: seen.append("DUP")) == []
    assert "DUP" not in seen


def test_pull_fallback_when_no_backend_configured():
    """Even with no backend configured (no push primary at all), pull can deliver."""
    t = Transport(InMemoryWakeQueue(), backend=None, time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "queued"
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_backend_recovers_midstream():
    """Backend recovers midstream: before recovery wakes queue, after recovery delivery switches to push primary."""
    up = {"ok": False}
    backend = CallablePushBackend(lambda w: up["ok"])
    t = Transport(InMemoryWakeQueue(), backend, time_fn=ManualClock())

    assert t.deliver(_wake(0)) == "queued"  # backend down
    up["ok"] = True
    assert t.deliver(_wake(1)) == "push"  # backend up

    # Wakes queued while down can be recovered by pull (delivery is uninterrupted).
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_push_raising_is_treated_as_failure_not_crash():
    """If the push backend raises, Transport does not crash and delegates to pull fallback."""
    def boom(_w: Wake) -> bool:
        raise RuntimeError("backend exploded")

    t = Transport(InMemoryWakeQueue(), CallablePushBackend(boom), time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "queued"
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


# -- at-most-once / three-state claim-then-confirm ---------------------------


def test_duplicate_enqueue_is_idempotent():
    """Duplicate deliver calls with the same id are deduped and do not reach the receiver twice."""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    t.deliver(_wake(0))
    t.deliver(_wake(0))  # Redeliver the same id.

    delivered = t.poll("coordinator")
    assert len(delivered) == 1


def test_poll_default_claims_without_confirming():
    """The poll default (confirm=False) only claims. Without confirmation, lease expiry redelivers."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    claimed = t.poll("coordinator")  # Default = do not confirm.
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED  # Not DELIVERED.

    # Lease expires without confirmation -> redelivered (crash recovery).
    clock.advance(31.0)
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_poll_confirm_true_marks_delivered():
    """A poll with explicit confirm=True confirms immediately before returning (simple case)."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    got = t.poll("coordinator", confirm=True)
    assert [w.id for w in got] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED
    clock.advance(100.0)
    assert t.poll("coordinator", confirm=True) == []  # Not redelivered.


def test_poll_and_handle_confirms_only_on_success():
    """poll_and_handle confirms only the wakes whose handler succeeds."""
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=ManualClock())
    t.deliver(_wake(0))

    handled = t.poll_and_handle("coordinator", lambda w: None)
    assert [w.id for w in handled] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_poll_and_handle_redelivers_when_handler_crashes():
    """Wakes whose handler raises are not confirmed and are redelivered after lease expiry (crash-safe)."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    def boom(_w: Wake) -> None:
        raise RuntimeError("handler failed mid-processing")

    # Handler exceptions propagate instead of being swallowed.
    with pytest.raises(RuntimeError):
        t.poll_and_handle("coordinator", boom)
    # The wake that died before processing remains unconfirmed (not lost).
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # After lease expiry it is redelivered and can be confirmed by a successful handler.
    clock.advance(31.0)
    ok: list[str] = []
    handled = t.poll_and_handle("coordinator", lambda w: ok.append(w.id))
    assert ok == ["r1:loop_done:0"]
    assert [w.id for w in handled] == ok
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_claim_then_confirm_requires_explicit_confirm():
    """A poll with confirm=False only claims. Before confirmation, polling again does not return the same wake."""
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=ManualClock())
    t.deliver(_wake(0))

    claimed = t.poll("coordinator", confirm=False)
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED
    # While the lease is held, another poll cannot take the same wake.
    assert t.poll("coordinator", confirm=False) == []

    n = t.confirm_wakes(claimed, owner="coordinator")
    assert n == 1
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_unconfirmed_claim_is_redelivered_after_lease_expiry():
    """If a claimed wake is not confirmed before lease expiry, it becomes eligible again and is redelivered."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    claimed = t.poll("coordinator", confirm=False)  # Claim, then "crash" (without confirmation).
    assert len(claimed) == 1

    clock.advance(31.0)  # Lease expiry.
    # A delayed confirm after expiry is rejected by fencing (it was not delivered, so it is not marked DELIVERED).
    assert t.confirm_wakes(claimed, owner="coordinator") == 0
    # It can be recovered by polling again (at-least-once: for idle wakes, loss is worse than duplication).
    redelivered = t.poll("coordinator")
    assert [w.id for w in redelivered] == ["r1:loop_done:0"]


def test_owner_fencing_blocks_stale_confirm():
    """If another owner reclaims after lease expiry, the original owner's confirm is rejected."""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    first = t.poll("coordinator", owner="worker-A", confirm=False)
    assert len(first) == 1

    clock.advance(31.0)  # A's lease expires.
    second = t.poll("coordinator", owner="worker-B", confirm=False)  # B reclaims.
    assert len(second) == 1
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # A's late confirm is rejected, and only B's confirm succeeds (prevents double confirmation).
    assert t.confirm_wakes(first, owner="worker-A") == 0
    assert t.confirm_wakes(second, owner="worker-B") == 1
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_redeliver_respects_inflight_claim():
    """Redelivering a CLAIMED wake does not let push steal and confirm the active claim.

    Even if retry/resume after backend recovery calls deliver again, a wake that the receiver
    has claimed with confirm=False remains CLAIMED and preserves the owner's lease-expiry
    redelivery protection (codex P2 regression guard).
    """
    up = {"ok": False}
    backend = CallablePushBackend(lambda w: up["ok"])
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, backend, lease=30.0, time_fn=clock)

    assert t.deliver(_wake(0)) == "queued"  # backend down -> queued
    claimed = t.poll("coordinator", confirm=False)  # Receiver claims it (processing).
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # Redeliver after backend recovery: respect the active claim and do not steal it.
    up["ok"] = True
    assert t.deliver(_wake(0)) == "queued"
    assert queue.state_of("r1:loop_done:0") == CLAIMED  # Still CLAIMED.

    # Owner crashes before confirmation -> lease expiry redelivers it (protection still works).
    clock.advance(31.0)
    redelivered = t.poll("coordinator")
    assert [w.id for w in redelivered] == ["r1:loop_done:0"]


def test_inflight_push_does_not_steal_active_claim():
    """If another poller claims during push I/O, push does not steal the claim or lose the wake.

    This recreates a receiver claiming the same wake just before push returns a confirmed
    delivery, and verifies that deliver's mark_delivered does not steal an active CLAIMED
    wake by marking it DELIVERED (= lease expiry still redelivers if the owner crashes)
    (codex P2 regression guard).
    """
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    box: dict = {}

    def racing_push(w: Wake) -> bool:
        # Recreate the receiver claiming the same wake while push is "in flight".
        box["claimed"] = box["transport"].poll("coordinator", owner="recv", confirm=False)
        return True  # Push itself returns success.

    t = Transport(queue, CallablePushBackend(racing_push), lease=30.0, time_fn=clock)
    box["transport"] = t

    route = t.deliver(_wake(0))
    assert route == "push"  # Push succeeded.
    # But the active claim was not stolen (still CLAIMED = pull owns delivery).
    assert [w.id for w in box["claimed"]] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # Even if the poller crashes before confirmation, lease expiry redelivers it (not lost).
    clock.advance(31.0)
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_delivered_wake_never_redelivered_even_on_redeliver_attempt():
    """Redelivering an already DELIVERED wake does not push again and is not returned by pull."""
    pushes: list[str] = []

    def push_ok(w: Wake) -> bool:
        pushes.append(w.id)
        return True

    t = Transport(InMemoryWakeQueue(), CallablePushBackend(push_ok), time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "push"
    assert t.deliver(_wake(0)) == "push"  # Redeliver.
    assert pushes == ["r1:loop_done:0"]  # Pushed exactly once.
    assert t.poll("coordinator") == []


# -- recipient routing ------------------------------------------------------


def test_poll_only_returns_own_recipient():
    """poll claims only wakes whose recipient matches (leaves wakes for others)."""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    t.deliver(_wake(0, recipient="alice"))
    t.deliver(_wake(1, recipient="bob"))

    assert [w.id for w in t.poll("alice")] == ["r1:loop_done:0"]
    assert [w.id for w in t.poll("bob")] == ["r1:loop_done:1"]


def test_poll_limit_bounds_batch():
    """limit bounds how many wakes one poll takes (the rest are left for the next poll)."""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    for i in range(5):
        t.deliver(_wake(i))
    first = t.poll("coordinator", limit=2)
    assert len(first) == 2
    rest = t.poll("coordinator")
    assert len(rest) == 3


# -- per-role cadence -------------------------------------------------------


def test_cadence_values_are_asymmetric_by_role():
    """Asymmetric design: dispatcher 3m / worker short interval / secretary 0 (every turn)."""
    assert cadence_for("dispatcher") == 180.0
    assert cadence_for("worker") == 60.0
    assert cadence_for("secretary") == 0.0
    # Unknown roles conservatively fall back to the default.
    assert cadence_for("unknown-role") == 60.0


def test_due_to_poll_respects_cadence():
    """due_to_poll is due after cadence elapses. Never-polled roles are always due."""
    # If it has never polled, it is always due.
    assert due_to_poll("dispatcher", last_poll=None, now=0.0) is True
    # Not due before cadence elapses.
    assert due_to_poll("dispatcher", last_poll=0.0, now=100.0) is False
    # Due once cadence elapses.
    assert due_to_poll("dispatcher", last_poll=0.0, now=180.0) is True
    # Secretary has cadence 0 -> always due (poll every turn start).
    assert due_to_poll("secretary", last_poll=0.0, now=0.0) is True


# -- concurrent poll thread safety (no double claim) ------------------------


def test_concurrent_pollers_never_double_claim():
    """Even if multiple threads poll the same recipient concurrently, each wake is claimed by at most one thread.

    This uses real time + a barrier to verify that InMemoryWakeQueue serializes
    check-and-set with a lock and that owner fencing / at-most-once claims actually
    hold for concurrent pollers.
    """
    # Use real time and a long enough lease that claims do not expire (verify confirmation only).
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), lease=3600.0, time_fn=time.monotonic)
    n_wakes = 200
    for i in range(n_wakes):
        t.deliver(_wake(i))

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    claimed_by: list[list[str]] = [[] for _ in range(n_threads)]

    def worker(idx: int) -> None:
        own = f"worker-{idx}"
        barrier.wait()  # Start all threads at once to maximize contention.
        while True:
            got = t.poll("coordinator", owner=own, limit=1)
            if not got:
                # Even if temporarily empty while other threads process, retry while unconfirmed wakes remain.
                if not t.pending("coordinator"):
                    return
                continue
            wake = got[0]
            assert t.confirm_wakes(got, owner=own) == 1
            claimed_by[idx].append(wake.id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    all_claimed = [wid for lst in claimed_by for wid in lst]
    # Every wake was claimed+confirmed exactly once (no double claims, no misses).
    assert sorted(all_claimed) == sorted(f"r1:loop_done:{i}" for i in range(n_wakes))
    assert len(all_claimed) == len(set(all_claimed)) == n_wakes


# -- invalid input -----------------------------------------------------------


def test_to_dict_canonical_fields_win_over_payload_collisions():
    """Canonical fields win even if payload contains reserved names (id/kind/recipient/run_id)."""
    w = Wake(
        id="r1:loop_done:0",
        kind=WAKE_LOOP_DONE,
        recipient="coordinator",
        run_id="r1",
        # Maliciously or accidentally include keys that collide with de-dup/routing keys.
        payload={"id": "EVIL", "recipient": "attacker", "extra": "ok"},
    )
    d = w.to_dict()
    assert d["id"] == "r1:loop_done:0"
    assert d["recipient"] == "coordinator"
    assert d["kind"] == WAKE_LOOP_DONE
    assert d["run_id"] == "r1"
    assert d["extra"] == "ok"  # Non-colliding payload keys remain.


def test_enqueue_rejects_empty_id():
    q = InMemoryWakeQueue()
    with pytest.raises(ValueError):
        q.enqueue(Wake(id="", kind=WAKE_LOOP_DONE, recipient="x"))


def test_transport_rejects_nonpositive_lease():
    with pytest.raises(ValueError):
        Transport(InMemoryWakeQueue(), lease=0.0)


def test_claim_rejects_nonpositive_lease():
    q = InMemoryWakeQueue()
    with pytest.raises(ValueError):
        q.claim("x", now=0.0, lease=0.0, owner="o")
