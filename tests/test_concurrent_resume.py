"""Validate coordination for concurrent multi-process resume (Issue #21, Phase 3).

Demonstrate the success criteria from report.md S5 Phase 3 / Issue #21:
"exactly-once irreversible actions under concurrent resume, with order consistency,"
using in-progress leases (multi-stage pending -> resolved -> executing -> executed):

(a) Store level: ``acquire_lease`` is single-winner and claims
    ``resolved -> executing``; losers receive WAIT. ``complete_execution`` only lets
    the lease holder finalize executed. If the winner crashes, another process
    reclaims the expired lease (``took_over``).
(b) Gate level: losers see executing and pause until ``executed`` (order
    consistency). Once the winner completes, losers skip. Another process
    reclaims expired leases and completes execution.
(c) End-to-end: simulate concurrent processes (threads + independent connections)
    and show that an irreversible action runs exactly once across the whole
    process set, and losers do not proceed to later work before completion.
(d) Existing v1 DBs are migrated nondestructively to executing/lease columns.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from loop_agent import (
    DBProgressLog,
    HumanGate,
    LoopState,
    LoopStore,
    MaxIterations,
    connect,
    run_loop,
)
from loop_agent.loop import GATE_PAUSE, GATE_PROCEED, GATE_SKIP
from loop_agent.store import (
    LEASE_ACQUIRED,
    LEASE_EXECUTED,
    LEASE_WAIT,
    EVENT_GATE,
)
from conftest import ManualClock, never_done

RUN = "run-concurrent"


def make_world(actions):
    """A world where ``gather`` proposes ``actions[iteration]`` and ``act`` records execution."""
    executed: list = []

    def gather(state):
        return actions[state.iteration]

    def act(action):
        executed.append(action)
        from loop_agent import ActOutcome

        return ActOutcome(observation=action, tokens=0)

    return gather, act, executed


def is_deploy(action) -> bool:
    return action == "deploy"


def _seed_resolved(db_path, gate_key="gate-0", action="deploy", decision="approve"):
    """Create a run and prepare a DB with one irreversible action resolved."""
    store = LoopStore(connect(db_path))
    store.load_or_init(RUN)
    store.request_decision(RUN, gate_key, action)
    store.resolve_decision(RUN, gate_key, decision)
    return store


# -- (a) store level: lease single-winner / completion / expired-lease takeover --


def test_acquire_lease_is_single_winner_across_connections(tmp_path):
    # Even when acquired from separate connections (simulated concurrent resume),
    # only one party can move resolved->executing.
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))

    ra = store_a.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=30)
    rb = store_b.acquire_lease(RUN, "gate-0", "B", now=0.0, ttl=30)
    assert ra["outcome"] == LEASE_ACQUIRED and ra["took_over"] is False
    # The loser sees the active lease holder (A) and waits.
    assert rb["outcome"] == LEASE_WAIT and rb["owner"] == "A"
    # The status is executing and includes lease information.
    row = store_a.get_decision(RUN, "gate-0")
    assert row["status"] == "executing" and row["lease_owner"] == "A"
    assert row["lease_expires_at"] == 30.0

    # Winner completes -> executed. Loser reacquire returns EXECUTED (skip), and
    # loser complete returns False.
    assert store_a.complete_execution(RUN, "gate-0", "A") is True
    done = store_a.get_decision(RUN, "gate-0")
    assert done["status"] == "executed" and done["lease_owner"] is None
    assert store_b.acquire_lease(RUN, "gate-0", "B", now=0.0, ttl=30)["outcome"] == (
        LEASE_EXECUTED
    )
    assert store_b.complete_execution(RUN, "gate-0", "B") is False


def test_complete_execution_only_by_current_lease_holder(tmp_path):
    db_path = tmp_path / "s.db"
    store = _seed_resolved(db_path)
    store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=30)
    # Another owner cannot finalize completion because it is not the lease holder.
    assert store.complete_execution(RUN, "gate-0", "B") is False
    assert store.get_decision(RUN, "gate-0")["status"] == "executing"
    assert store.complete_execution(RUN, "gate-0", "A") is True


def test_lease_reentrant_same_owner_extends_expiry(tmp_path):
    db_path = tmp_path / "s.db"
    store = _seed_resolved(db_path)
    store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)  # expires 10
    r2 = store.acquire_lease(RUN, "gate-0", "A", now=5.0, ttl=10)  # reentrant -> expires 15
    assert r2["outcome"] == LEASE_ACQUIRED and r2["took_over"] is False
    assert store.get_decision(RUN, "gate-0")["lease_expires_at"] == 15.0


def test_expired_lease_is_taken_over_after_winner_crash(tmp_path):
    # Winner crashes after acquiring the lease (does not complete) -> another
    # process reclaims it after expiration.
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))
    store_a.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)
    # Before expiration: B is made to wait.
    assert store_b.acquire_lease(RUN, "gate-0", "B", now=5.0, ttl=10)["outcome"] == (
        LEASE_WAIT
    )
    # After expiration (now > expires=10): B reclaims it (took_over).
    taken = store_b.acquire_lease(RUN, "gate-0", "B", now=20.0, ttl=10)
    assert taken["outcome"] == LEASE_ACQUIRED and taken["took_over"] is True
    # The old winner A's delayed completion is a no-op because it lost the lease.
    # This prevents duplicate executed finalization.
    assert store_a.complete_execution(RUN, "gate-0", "A") is False
    assert store_b.complete_execution(RUN, "gate-0", "B") is True
    # The takeover leaves loop_gate(executing, took_over=True) in the journal.
    gate_events = [
        e
        for e in store_b.read_events(RUN)
        if e["kind"] == EVENT_GATE and e["payload"].get("took_over") is True
    ]
    assert gate_events


def test_acquire_lease_validation_and_errors(tmp_path):
    db_path = tmp_path / "s.db"
    store = LoopStore(connect(db_path))
    store.load_or_init(RUN)
    with pytest.raises(ValueError, match="owner must be"):
        store.acquire_lease(RUN, "g", "", now=0.0, ttl=1)
    with pytest.raises(ValueError, match="no decision"):
        store.acquire_lease(RUN, "missing", "o", now=0.0, ttl=1)
    store.request_decision(RUN, "g", "deploy")
    with pytest.raises(ValueError, match="ttl must be positive"):
        store.acquire_lease(RUN, "g", "o", now=0.0, ttl=0)
    with pytest.raises(ValueError, match="unresolved"):
        store.acquire_lease(RUN, "g", "o", now=0.0, ttl=1)  # pending
    # reject/respond are not execution decisions, so they cannot be leased.
    store.request_decision(RUN, "gr", "deploy")
    store.resolve_decision(RUN, "gr", "reject")
    with pytest.raises(ValueError, match="not executable"):
        store.acquire_lease(RUN, "gr", "o", now=0.0, ttl=1)


# -- (b) gate level: loser pause / skip after winner completion / takeover ------


def test_loser_gate_pauses_while_winner_holds_lease(tmp_path):
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))
    clock = ManualClock(100.0)
    gate_a = HumanGate(
        on=is_deploy, store=store_a, run_id=RUN, owner="A", now_fn=clock, lease_ttl=30
    )
    gate_b = HumanGate(
        on=is_deploy, store=store_b, run_id=RUN, owner="B", now_fn=clock, lease_ttl=30
    )
    state = LoopState()  # iteration 0 -> gate-0

    # A acquires the deploy@0 lease -> proceed (not completed yet).
    review_a = gate_a.review("deploy", state)
    assert review_a.disposition == GATE_PROCEED
    assert review_a.on_complete is not None

    # While A is executing, B reviews the same gate -> pause for order consistency
    # until executed.
    review_b = gate_b.review("deploy", state)
    assert review_b.disposition == GATE_PAUSE
    assert review_b.pending["status"] == "executing"
    assert review_b.pending["gate_key"] == "gate-0"

    # A finalizes completion -> executed.
    review_a.on_complete()
    assert store_a.get_decision(RUN, "gate-0")["status"] == "executed"

    # B reviews again -> already executed, so skip (no duplicate execution).
    review_b2 = gate_b.review("deploy", state)
    assert review_b2.disposition == GATE_SKIP


def test_loser_gate_takes_over_after_winner_lease_expires(tmp_path):
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))
    clock = ManualClock(0.0)
    gate_a = HumanGate(
        on=is_deploy, store=store_a, run_id=RUN, owner="A", now_fn=clock, lease_ttl=10
    )
    gate_b = HumanGate(
        on=is_deploy, store=store_b, run_id=RUN, owner="B", now_fn=clock, lease_ttl=10
    )
    state = LoopState()

    # A acquires -> proceed, but assume it crashes without completing.
    assert gate_a.review("deploy", state).disposition == GATE_PROCEED

    # Before expiration, B is made to wait.
    clock.now = 5.0
    assert gate_b.review("deploy", state).disposition == GATE_PAUSE

    # After expiration, B reclaims and executes (proceed).
    clock.now = 100.0
    review_b = gate_b.review("deploy", state)
    assert review_b.disposition == GATE_PROCEED
    review_b.on_complete()
    assert store_b.get_decision(RUN, "gate-0")["status"] == "executed"


def test_winner_crash_recovery_records_step_via_loop(tmp_path):
    # Winner crash -> expiration -> another process reclaims through the full loop,
    # and the step is not lost.
    db_path = tmp_path / "s.db"
    seed = _seed_resolved(db_path)
    clock = ManualClock(0.0)

    # Winner A: acquires the lease (proceed) but "crashes" without calling
    # act/on_complete.
    gate_a = HumanGate(
        on=is_deploy, store=seed, run_id=RUN, owner="A", now_fn=clock, lease_ttl=5
    )
    assert gate_a.review("deploy", LoopState()).disposition == GATE_PROCEED
    assert seed.get_decision(RUN, "gate-0")["status"] == "executing"

    # After lease expiration, loser B resumes through the full loop and reclaims it.
    clock.now = 100.0
    conn_b = connect(db_path)
    db_b = DBProgressLog(conn_b, RUN)
    gather, act, executed = make_world(["deploy", "work2"])
    gate_b = HumanGate(
        on=is_deploy,
        store=db_b.store,
        run_id=RUN,
        owner="B",
        now_fn=clock,
        lease_ttl=5,
    )
    res = run_loop(
        act=act,
        verify=never_done,
        conditions=[MaxIterations(2)],
        gather=gather,
        gate=gate_b,
        on_step=db_b.on_step,
    )
    assert res.status == "stopped"
    assert executed == ["deploy", "work2"]  # B reclaims and executes deploy
    assert db_b.store.get_decision(RUN, "gate-0")["status"] == "executed"
    # The deploy step row is persisted, so the step is not lost even after winner crash.
    steps = db_b.store.read_steps(RUN)
    assert any(s["observation"] == "deploy" for s in steps)
    conn_b.close()


# -- (c) end-to-end: simulated concurrent processes with exactly-once + ordering -


def test_concurrent_resume_runs_irreversible_action_exactly_once(tmp_path):
    # Resume the same run_id *simultaneously* using two threads + independent
    # connections. In each round, the irreversible action (deploy) runs exactly
    # once across the whole process set, and losers do not proceed to later work
    # before completion (order consistency). The barrier makes both parties
    # collide at gate review time and repeats the race across multiple rounds.
    actions = ["deploy", "work2"]
    # Allowed executed shapes for each thread:
    #   ("deploy","work2") winner / () loser paused on WAIT /
    #   ("work2",) loser skipped already-executed deploy.
    # All satisfy order consistency ("do not execute work2 before deploy") and
    # "deploy runs at most once."
    allowed = {("deploy", "work2"), (), ("work2",)}

    for i in range(20):
        run_id = f"run-conc-{i}"
        db_path = tmp_path / f"c{i}.db"
        seed = LoopStore(connect(db_path))
        seed.load_or_init(run_id)
        seed.request_decision(run_id, "gate-0", "deploy")
        seed.resolve_decision(run_id, "gate-0", "approve")
        seed.conn.close()

        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}
        errors: dict[str, BaseException] = {}
        lock = threading.Lock()

        def worker(name: str) -> None:
            # If setup fails before reaching the barrier, the peer would wait
            # forever. Wrap the whole worker in try and give the barrier a timeout
            # to fail fast (deadlock prevention).
            try:
                conn = connect(db_path)
                store = LoopStore(conn)
                gather, act, executed = make_world(actions)
                gate = HumanGate(on=is_deploy, store=store, run_id=run_id, owner=name)
                barrier.wait(timeout=10)  # Align both threads just before gate review.
                res = run_loop(
                    act=act,
                    verify=never_done,
                    conditions=[MaxIterations(2)],
                    gather=gather,
                    gate=gate,
                )
                conn.close()
                with lock:
                    results[name] = (res, executed)
            except BaseException as exc:  # noqa: BLE001 - record test failures without swallowing them
                with lock:
                    errors[name] = exc
                try:
                    barrier.abort()  # Release the peer from barrier wait immediately.
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), (i, "worker thread hung")

        assert not errors, (i, errors)
        assert set(results) == {"A", "B"}, (i, set(results))
        ex_a = tuple(results["A"][1])
        ex_b = tuple(results["B"][1])
        # Each thread's execution sequence is one of the allowed shapes (order consistency).
        assert ex_a in allowed, (i, ex_a)
        assert ex_b in allowed, (i, ex_b)
        # deploy runs exactly once across the whole process set.
        assert (ex_a + ex_b).count("deploy") == 1, (i, ex_a, ex_b)
        # Exactly one thread is the winner (executes deploy).
        winners = [n for n in ("A", "B") if "deploy" in results[n][1]]
        assert len(winners) == 1, (i, ex_a, ex_b)
        # The gate is eventually executed (winner finalized completion).
        final = LoopStore(connect(db_path))
        assert final.get_decision(run_id, "gate-0")["status"] == "executed"
        final.conn.close()


# -- (d) nondestructive migration for existing v1 DBs ---------------------------


# pending_decision DDL from v1 (Issue #15): no executing status or lease columns.
_OLD_SCHEMA = """
CREATE TABLE run (
  run_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE pending_decision (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  gate_key     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','resolved','executed')),
  decision     TEXT CHECK (decision IS NULL OR
                 decision IN ('approve','edit','reject','respond')),
  action       TEXT CHECK (action IS NULL OR json_valid(action)),
  payload      TEXT CHECK (payload IS NULL OR json_valid(payload)),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at  TEXT,
  executed_at  TEXT,
  CHECK (status = 'pending' OR decision IS NOT NULL),
  UNIQUE (run_id, gate_key)
);
"""


def test_old_pending_decision_schema_is_migrated_nondestructively(tmp_path):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_SCHEMA)
    raw.execute("INSERT INTO run (run_id, status) VALUES (?, 'running')", (RUN,))
    raw.execute(
        "INSERT INTO pending_decision (run_id, gate_key, status, decision, action) "
        'VALUES (?, ?, ?, ?, ?)',
        (RUN, "gate-0", "resolved", "approve", '"deploy"'),
    )
    raw.commit()
    raw.close()

    # connect runs migration.
    store = LoopStore(connect(db_path))
    decision = store.get_decision(RUN, "gate-0")
    # Existing rows are preserved. New lease columns are added (default NULL).
    assert decision["status"] == "resolved" and decision["action"] == "deploy"
    assert decision["lease_owner"] is None
    assert "lease_expires_at" in decision
    # Confirm by acquiring a lease that executing is allowed (= CHECK was rebuilt).
    res = store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)
    assert res["outcome"] == LEASE_ACQUIRED
    assert store.get_decision(RUN, "gate-0")["status"] == "executing"
    assert store.complete_execution(RUN, "gate-0", "A") is True


def test_migration_recovers_from_leftover_temp_table(tmp_path):
    # Even if the temporary table pending_decision_mig was left by a previous
    # interruption, migration can drop it and retry. CREATE is not IF NOT EXISTS,
    # so leaving it behind would make connect fail permanently.
    db_path = tmp_path / "stale.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_SCHEMA)
    raw.execute("INSERT INTO run (run_id, status) VALUES (?, 'running')", (RUN,))
    raw.execute(
        "INSERT INTO pending_decision (run_id, gate_key, status, decision, action) "
        'VALUES (?, ?, ?, ?, ?)',
        (RUN, "gate-0", "resolved", "approve", '"deploy"'),
    )
    # Simulate a temporary table left behind by an interruption.
    raw.execute("CREATE TABLE pending_decision_mig (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    # connect completes migration without failing, and the original decision is preserved.
    store = LoopStore(connect(db_path))
    assert store.get_decision(RUN, "gate-0")["status"] == "resolved"
    assert store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=5)["outcome"] == (
        LEASE_ACQUIRED
    )
    # The temporary table is gone.
    leftover = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='pending_decision_mig'"
    ).fetchone()
    assert leftover is None


def test_migration_is_idempotent_on_already_v2_db(tmp_path):
    # Reopening a DB created with the new schema leaves migration as a no-op and
    # does not corrupt the decision.
    db_path = tmp_path / "v2.db"
    store = _seed_resolved(db_path)
    store.conn.close()
    reopened = LoopStore(connect(db_path))
    assert reopened.get_decision(RUN, "gate-0")["status"] == "resolved"
    assert reopened.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=5)["outcome"] == (
        LEASE_ACQUIRED
    )
