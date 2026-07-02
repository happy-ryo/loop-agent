"""Loop state SoT (state.db) validation: transaction / crash safety / schema independence.

Targets the minimal "state.db SoT" implementation (Issue #11) from report.md
S3.4 / S4.6 / S5 Phase 2, proving that (a) each iteration is persisted
atomically, (b) transactions are crash-safe (no partial rows remain when the
process exits before commit), and (c) the schema is a minimal schema independent
from the org core. It also verifies that DBProgressLog is a drop-in replacement
for the same observation hook as the JSONL ProgressLog.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from loop_agent import (
    DBProgressLog,
    LoopStore,
    MaxIterations,
    LoopState,
    StepRecord,
    VerifyOutcome,
    connect,
    run_loop,
)
from loop_agent.store import (
    EVENT_BEGIN,
    EVENT_END,
    EVENT_STEP,
    SCHEMA_VERSION,
)
from conftest import acting, done_after, never_done


def _run_with_db(conn, run_id, *, act, verify, conditions, on_step=None):
    """Wire run_loop to DBProgressLog, record through final state, and return the result."""
    db = DBProgressLog(conn, run_id)

    if on_step is None:
        observer = db.on_step
    else:

        def observer(record, state):
            db.on_step(record, state)
            on_step(record, state)

    result = run_loop(
        act=act, verify=verify, conditions=conditions, on_step=observer
    )
    db.record_result(result)
    return result, db


# -- Schema independence (minimal schema independent of org core) -------------


def test_schema_has_only_the_minimal_loop_tables(tmp_path):
    conn = connect(tmp_path / "state.db")
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    # Exclude sqlite_sequence because it is an AUTOINCREMENT side effect.
    names.discard("sqlite_sequence")
    # Four tables for run / step / event / stop_reason, plus pending_decision
    # for the limited human gate (Issue #15). All are self-contained schemas
    # independent of the org core.
    assert names == {"run", "step", "event", "stop_reason", "pending_decision"}


def test_schema_carries_no_claude_org_tables(tmp_path):
    # Ensure org core schema tables (projects / workstreams / worker_dirs /
    # plural runs / org_sessions, etc.) have not slipped in, preserving loose
    # coupling.
    conn = connect(tmp_path / "state.db")
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for org_table in ("projects", "workstreams", "worker_dirs", "runs",
                      "org_sessions", "events", "schema_migrations"):
        assert org_table not in names


def test_store_module_does_not_import_org_state_db(tmp_path):
    # loop_agent.store / connect must not import claude-org's tools.state_db at
    # all; importing it would tightly couple the package to org.
    connect(tmp_path / "state.db")
    assert not any("tools.state_db" in m for m in sys.modules)


def test_connect_sets_schema_version(tmp_path):
    conn = connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_connect_is_idempotent_on_existing_db(tmp_path):
    # Opening twice should reapply the schema through IF NOT EXISTS without
    # errors and preserve existing data.
    path = tmp_path / "state.db"
    store = LoopStore(connect(path))
    store.load_or_init("r1")
    store.conn.close()

    conn2 = connect(path)
    assert conn2.execute(
        "SELECT run_id FROM run WHERE run_id = 'r1'"
    ).fetchone() is not None


# -- load_or_init (run lifecycle / resume foundation) -------------------------


def test_load_or_init_new_run_returns_empty_state_and_logs_begin(tmp_path):
    store = LoopStore(connect(tmp_path / "state.db"))
    state = store.load_or_init("r1")

    assert isinstance(state, LoopState)
    assert state.iteration == 0 and state.tokens_used == 0 and state.history == []

    run = store.get_run("r1")
    assert run["status"] == "running"
    events = store.read_events("r1")
    assert [e["kind"] for e in events] == [EVENT_BEGIN]


def test_load_or_init_is_idempotent_and_does_not_relog_begin(tmp_path):
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    store.load_or_init("r1")  # The second call only returns the existing run.
    begins = [e for e in store.read_events("r1") if e["kind"] == EVENT_BEGIN]
    assert len(begins) == 1


def test_load_or_init_reconstructs_state_from_persisted_steps(tmp_path):
    # Resume foundation (#14): loading an existing run reconstructs LoopState
    # from persisted steps (history / iteration / tokens_used / goal_met).
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=10, observation="work"),
        verify=done_after(3),
        conditions=[MaxIterations(10)],
    )

    # Reopen through another connection and reconstruct, simulating resume
    # across processes.
    reopened = LoopStore(connect(path))
    state = reopened.load_or_init("r1")
    assert state.iteration == 3
    assert state.tokens_used == 30
    assert state.goal_met is True
    assert [r.iteration for r in state.history] == [0, 1, 2]
    assert all(isinstance(r, StepRecord) for r in state.history)
    assert state.history[-1].goal_met is True
    assert state.history[0].observation == "work"


def test_load_or_init_rejects_empty_run_id(tmp_path):
    store = LoopStore(connect(tmp_path / "state.db"))
    with pytest.raises(ValueError):
        store.load_or_init("")


# -- Per-step persistence (atomic) -------------------------------------------


def test_every_iteration_is_persisted_in_order(tmp_path):
    path = tmp_path / "state.db"
    result, db = _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=10, observation="work"),
        verify=never_done,
        conditions=[MaxIterations(5)],
    )
    steps = db.store.read_steps("r1")
    assert len(steps) == result.iterations == 5
    assert [s["iteration"] for s in steps] == [0, 1, 2, 3, 4]
    assert [s["tokens_used"] for s in steps] == [10, 20, 30, 40, 50]
    assert all(s["tokens"] == 10 for s in steps)
    assert all(s["observation"] == "work" for s in steps)
    assert all(s["goal_met"] is False for s in steps)


def test_run_aggregate_and_events_match_the_steps(tmp_path):
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )
    store = LoopStore(connect(path))
    run = store.get_run("r1")
    assert run["iterations"] == 3 and run["tokens_used"] == 30

    kinds = [e["kind"] for e in store.read_events("r1")]
    # One begin, one step per iteration, and one end, in this order.
    assert kinds == [EVENT_BEGIN, EVENT_STEP, EVENT_STEP, EVENT_STEP, EVENT_END]


def test_records_are_durable_after_each_step_not_only_at_the_end(tmp_path):
    # At the Nth on_step, another connection can already see N step rows,
    # proving each iteration is committed instead of dumped in bulk at the end.
    path = tmp_path / "state.db"
    seen_counts = []

    def observe(_record, _state):
        probe = LoopStore(connect(path))
        seen_counts.append(len(probe.read_steps("r1")))
        probe.conn.close()

    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(4)],
        on_step=observe,
    )
    assert seen_counts == [1, 2, 3, 4]


def test_record_step_overwrites_row_on_same_iteration(tmp_path):
    # Repersisting the same iteration with a different result overwrites instead
    # of creating duplicate rows.
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    st = LoopState(iteration=1, tokens_used=5)
    store.record_step(
        "r1", StepRecord(iteration=0, observation="a", tokens=5, goal_met=False), st
    )
    store.record_step(
        "r1", StepRecord(iteration=0, observation="b", tokens=7, goal_met=True), st
    )

    steps = store.read_steps("r1")
    assert len(steps) == 1
    assert steps[0]["observation"] == "b"
    assert steps[0]["tokens"] == 7
    assert steps[0]["goal_met"] is True
    # Repersistence with changed content appends one event with the new content,
    # and the latest event remains consistent with the current step row
    # (event[-1] == current value).
    step_events = [e for e in store.read_events("r1") if e["kind"] == EVENT_STEP]
    assert len(step_events) == 2
    assert step_events[-1]["payload"]["tokens"] == 7
    assert step_events[-1]["payload"]["goal_met"] is True


def test_record_step_identical_replay_does_not_duplicate_event(tmp_path):
    # Repersisting identical content (a pure resume replay) does not duplicate
    # the step row or event, so the append-only journal does not add noise for
    # identical content.
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    rec = StepRecord(iteration=0, observation={"k": 1}, tokens=5, goal_met=False)
    st = LoopState(iteration=1, tokens_used=5, elapsed=0.5)
    store.record_step("r1", rec, st)
    store.record_step("r1", rec, st)  # Replay with identical content.

    assert len(store.read_steps("r1")) == 1
    step_events = [e for e in store.read_events("r1") if e["kind"] == EVENT_STEP]
    assert len(step_events) == 1


# -- Final state confirmation ------------------------------------------------


def test_record_result_for_a_capped_run(tmp_path):
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=30),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )
    store = LoopStore(connect(path))
    run = store.get_run("r1")
    assert run["status"] == "stopped" and run["goal_met"] == 0
    assert run["ended_at"] is not None

    stop = store.get_stop_reason("r1")
    assert stop["status"] == "stopped"
    assert stop["name"] == "max_iterations"
    assert "max iterations" in stop["reason"]


def test_record_result_for_a_goal_met_run(tmp_path):
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=1),
        verify=done_after(2),
        conditions=[MaxIterations(10)],
    )
    store = LoopStore(connect(path))
    run = store.get_run("r1")
    assert run["status"] == "goal_met" and run["goal_met"] == 1

    stop = store.get_stop_reason("r1")
    assert stop["status"] == "goal_met"
    assert stop["name"] is None  # Goal completion has no triggering condition.
    assert stop["reason"] == "goal met"


# -- Transaction atomicity / crash safety ------------------------------------


def test_transaction_rolls_back_on_exception(tmp_path):
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.conn.execute(
                "INSERT INTO step (run_id, iteration, tokens) VALUES "
                "('r1', 0, 99)"
            )
            raise RuntimeError("boom mid-transaction")

    # The exception rolls back the transaction, leaving no partial step row.
    assert store.read_steps("r1") == []


def test_record_step_is_all_or_nothing_when_event_insert_fails(tmp_path, monkeypatch):
    # record_step groups the step row, aggregate, and event into one
    # transaction. If appending the event fails, the step row is also rolled back
    # with no partial persistence.
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")

    def boom(*_a, **_k):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(store, "_append_event", boom)
    rec = StepRecord(iteration=0, observation="x", tokens=5, goal_met=False)
    with pytest.raises(RuntimeError):
        store.record_step("r1", rec, LoopState(iteration=1, tokens_used=5))

    assert store.read_steps("r1") == []
    assert store.get_run("r1")["iterations"] == 0  # The aggregate is unchanged.


def test_composed_transaction_persists_multiple_steps_atomically(tmp_path):
    # The caller's transaction() can group multiple steps; inner record_step
    # calls join the outer transaction. A midstream exception rolls back the
    # whole group.
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    st = LoopState(iteration=1, tokens_used=1)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.record_step(
                "r1", StepRecord(0, "a", 1, False), st
            )
            store.record_step(
                "r1", StepRecord(1, "b", 1, False), st
            )
            raise RuntimeError("abort the batch")

    assert store.read_steps("r1") == []  # Neither step has been committed.
    assert [e["kind"] for e in store.read_events("r1")] == [EVENT_BEGIN]


def test_composed_transaction_commits_multiple_steps_atomically(tmp_path):
    # Verify the normal commit path for the join branch (in transaction True ->
    # the outermost transaction() commits): group two record_step calls in the
    # outer transaction, then confirm another connection can see both steps and
    # both loop_step events at once after successful completion.
    path = tmp_path / "state.db"
    store = LoopStore(connect(path))
    store.load_or_init("r1")
    st = LoopState(iteration=1, tokens_used=1)
    with store.transaction():
        store.record_step("r1", StepRecord(0, "a", 1, False), st)
        store.record_step("r1", StepRecord(1, "b", 1, False), st)

    reopened = LoopStore(connect(path))
    steps = reopened.read_steps("r1")
    assert [s["iteration"] for s in steps] == [0, 1]
    assert [s["observation"] for s in steps] == ["a", "b"]
    kinds = [e["kind"] for e in reopened.read_events("r1")]
    assert kinds == [EVENT_BEGIN, EVENT_STEP, EVENT_STEP]


def test_non_finite_float_observation_is_persisted_not_rejected(tmp_path):
    # Regression: observations containing non-finite floats (NaN/Infinity) are
    # persisted without being rejected by the json_valid CHECK by stringifying
    # with repr. One odd value must not break the entire step persistence.
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    obs = {"score": float("nan"), "ratio": float("inf"), "low": float("-inf"), "ok": 1.5}
    store.record_step("r1", StepRecord(0, obs, 0, False), LoopState(iteration=1))

    steps = store.read_steps("r1")
    assert len(steps) == 1
    stored = steps[0]["observation"]
    assert stored["score"] == "nan"
    assert stored["ratio"] == "inf"
    assert stored["low"] == "-inf"
    assert stored["ok"] == 1.5  # Finite floats are preserved as-is.


def test_committed_steps_survive_a_crash_before_the_next_commit(tmp_path):
    # Crash safety: committed iterations can be read from another process
    # (another connection). Even if the process dies before committing the next
    # iteration (closing the connection without committing an open transaction),
    # committed rows remain and uncommitted rows do not appear.
    path = tmp_path / "state.db"
    store = LoopStore(connect(path))
    store.load_or_init("r1")
    store.record_step(
        "r1", StepRecord(0, "done", 10, False), LoopState(iteration=1, tokens_used=10)
    )  # Committed through this point.

    # "Crash" while the next iteration is partially written: BEGIN + INSERT,
    # then close without committing.
    store.conn.execute("BEGIN")
    store.conn.execute(
        "INSERT INTO step (run_id, iteration, tokens) VALUES ('r1', 1, 77)"
    )
    store.conn.close()  # Equivalent to process exit before commit.

    # Reopening leaves only the one committed row.
    reopened = LoopStore(connect(path))
    steps = reopened.read_steps("r1")
    assert len(steps) == 1
    assert steps[0]["iteration"] == 0
    assert steps[0]["observation"] == "done"


def test_state_db_persists_across_independent_connections(tmp_path):
    # Minimal proof that the SoT persists across processes (connections).
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=2),
        verify=never_done,
        conditions=[MaxIterations(2)],
    )
    fresh = LoopStore(connect(path))
    assert len(fresh.read_steps("r1")) == 2
    assert fresh.get_stop_reason("r1")["name"] == "max_iterations"


# -- Multiple-run isolation / observation robustness --------------------------


def test_multiple_runs_are_isolated_in_one_db(tmp_path):
    path = tmp_path / "state.db"
    conn = connect(path)
    _run_with_db(
        conn, "r1", act=acting(tokens=1), verify=never_done,
        conditions=[MaxIterations(2)],
    )
    _run_with_db(
        conn, "r2", act=acting(tokens=1), verify=never_done,
        conditions=[MaxIterations(5)],
    )
    store = LoopStore(conn)
    assert len(store.read_steps("r1")) == 2
    assert len(store.read_steps("r2")) == 5
    assert store.read_steps("r1") != store.read_steps("r2")


def test_non_serializable_observation_is_stored_as_repr(tmp_path):
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")

    class Widget:
        def __repr__(self):
            return "Widget(stuck)"

    store.record_step(
        "r1",
        StepRecord(0, Widget(), 0, False),
        LoopState(iteration=1),
    )
    assert store.read_steps("r1")[0]["observation"] == "Widget(stuck)"


def test_unicode_detail_round_trips(tmp_path):
    path = tmp_path / "state.db"

    def verify(_outcome):
        return VerifyOutcome(goal_met=True, detail="Converged ✓")

    _run_with_db(
        connect(path), "r1", act=acting(tokens=0), verify=verify,
        conditions=[MaxIterations(5)],
    )
    store = LoopStore(connect(path))
    assert store.read_steps("r1")[0]["detail"] == "Converged ✓"


def test_foreign_key_cascade_removes_child_rows_with_the_run(tmp_path):
    # Deleting a run removes step / event / stop_reason through CASCADE,
    # proving foreign_keys=ON + ON DELETE CASCADE.
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path), "r1", act=acting(tokens=1), verify=never_done,
        conditions=[MaxIterations(2)],
    )
    conn = connect(path)
    conn.execute("DELETE FROM run WHERE run_id = 'r1'")
    store = LoopStore(conn)
    assert store.read_steps("r1") == []
    assert store.read_events("r1") == []
    assert store.get_stop_reason("r1") is None


# -- ProgressLog compatibility (drop-in) -------------------------------------


def test_dbprogresslog_owns_path_connection_and_closes_it(tmp_path):
    path = tmp_path / "state.db"
    with DBProgressLog(path, "r1") as db:
        assert db._owns_conn is True
        store = LoopStore(connect(path))
        assert store.get_run("r1") is not None
    # After close, the connection cannot be used because the owned connection
    # was closed.
    with pytest.raises(sqlite3.ProgrammingError):
        db.conn.execute("SELECT 1")


def test_dbprogresslog_borrows_connection_and_keeps_it_open(tmp_path):
    conn = connect(tmp_path / "state.db")
    db = DBProgressLog(conn, "r1")
    assert db._owns_conn is False
    db.close()  # Borrowed connections are not closed.
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_loopstore_initializes_a_bare_sqlite_connection(tmp_path):
    # Even when passed a borrowed connection opened directly with
    # sqlite3.connect() instead of connect(), LoopStore defensively applies the
    # schema + PRAGMA + row_factory and works without "no such table: run", with
    # rows accessible by column name.
    bare = sqlite3.connect(str(tmp_path / "state.db"))
    store = LoopStore(bare)
    store.load_or_init("r1")
    assert store.get_run("r1")["status"] == "running"  # Converted to Row.


def test_dbprogresslog_accepts_a_bare_sqlite_connection(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "state.db"))
    db = DBProgressLog(bare, "r1")
    assert db.store.get_run("r1") is not None
