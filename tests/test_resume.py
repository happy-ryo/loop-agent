"""Resume verification: interrupt -> resume matches a straight-through run without state loss (Issue #14).

Regression test for report.md S5 Phase 2 success criterion a. It restores
:class:`LoopState` with :meth:`LoopStore.load_or_init` from steps already
persisted in the state.db SoT, and demonstrates that
``run_loop(initial_state=...)`` can continue the loop from the interruption
point. The core claim is that "the result after crashing midway and resuming
matches a straight-through run that was never interrupted" (the persisted SoT
matches step-for-step, and the final aggregates / stop_reason also match).

Resume is meaningful when combined with a **state-based stop condition**
(GoalMet): across processes the act/verify hooks are recreated, but their
internal call counters are not restored. Hooks that derive their decision from
the (gathered) state can reproduce the same judgment in a new process -- this
test uses an act with a fixed token cost and GoalMet(state.iteration>=N).
"""

from __future__ import annotations

import json

import pytest

from loop_agent import (
    DBProgressLog,
    GoalMet,
    LoopState,
    LoopStore,
    MaxIterations,
    NoProgress,
    StepRecord,
    Timeout,
    VerifyOutcome,
    connect,
    run_loop,
)
from conftest import ManualClock, acting, never_done, stepping_for

# A state-based deterministic loop setup where resume can reproduce a match.
# The act token cost is fixed, and termination is GoalMet (state-based), so
# recreating hooks during resume does not change the decision.
GOAL_AT = 6


def _fresh_run_args() -> dict:
    return dict(
        act=acting(tokens=10, observation="w"),
        verify=never_done,
        conditions=[GoalMet(lambda s: s.iteration >= GOAL_AT), MaxIterations(100)],
    )


def _step_projection(store: LoopStore, run_id: str) -> list[tuple]:
    """Return a deterministic step projection excluding timestamp / elapsed."""
    return [
        (
            s["iteration"],
            s["tokens"],
            s["tokens_used"],
            s["goal_met"],
            s["detail"],
            s["observation"],
        )
        for s in store.read_steps(run_id)
    ]


def test_resume_after_crash_matches_straight_through(tmp_path):
    # (1) Use a straight-through run (no interruption) as the baseline.
    full_path = tmp_path / "full.db"
    full_result, _ = _run_with_db_resumable(full_path, "full")

    # (2) Interrupt: immediately after persisting 3 steps, raise an exception
    #     out of run_loop to "crash" it (record_result is not reached, so the
    #     run remains running).
    part_path = tmp_path / "part.db"

    class _Crash(RuntimeError):
        pass

    crash_db = DBProgressLog(part_path, "run")

    def crashing_observer(record, state):
        crash_db.on_step(record, state)  # This commits the step.
        if state.iteration == 3:  # Crash after persisting 3 steps, before step 4.
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(
            on_step=crashing_observer,
            initial_state=crash_db.state,  # New run, so empty = fresh start.
            **_fresh_run_args(),
        )
    crash_db.close()

    # At the interruption point, only 3 steps remain in the SoT and the run is
    # unfinished (running).
    probe = LoopStore(connect(part_path))
    assert len(probe.read_steps("run")) == 3
    assert probe.get_run("run")["status"] == "running"
    assert probe.get_stop_reason("run") is None
    probe.conn.close()

    # (3) Resume: reopen with another connection (= equivalent to another
    # process) and continue from the restored state.
    resume_db = DBProgressLog(part_path, "run")
    assert resume_db.state.iteration == 3  # Restore midpoint state from persisted steps.
    assert resume_db.state.tokens_used == 30
    assert [r.iteration for r in resume_db.state.history] == [0, 1, 2]

    resumed_result = run_loop(
        on_step=resume_db.on_step,
        initial_state=resume_db.state,
        **_fresh_run_args(),
    )
    resume_db.record_result(resumed_result)
    resume_db.close()

    # --- Resumed result matches the straight-through run ---
    assert resumed_result.iterations == full_result.iterations == GOAL_AT
    assert resumed_result.tokens_used == full_result.tokens_used == GOAL_AT * 10
    assert resumed_result.succeeded is full_result.succeeded is True
    assert resumed_result.stop.name == full_result.stop.name == "goal_met"

    # --- Persisted SoT also matches step-for-step, aggregates, and stop_reason ---
    full_store = LoopStore(connect(full_path))
    resume_store = LoopStore(connect(part_path))
    assert _step_projection(resume_store, "run") == _step_projection(full_store, "full")
    assert (
        resume_store.get_run("run")["iterations"]
        == full_store.get_run("full")["iterations"]
        == GOAL_AT
    )
    assert (
        resume_store.get_run("run")["tokens_used"]
        == full_store.get_run("full")["tokens_used"]
    )
    assert (
        resume_store.get_stop_reason("run")["name"]
        == full_store.get_stop_reason("full")["name"]
    )


def test_resume_does_not_replay_already_persisted_steps(tmp_path):
    # Resume only runs the *continuation* from the restored state; it does not
    # re-execute already persisted steps. Therefore the number of step events
    # matches the straight-through run (no replay noise is added).
    full_path = tmp_path / "full.db"
    _run_with_db_resumable(full_path, "full")
    full_store = LoopStore(connect(full_path))
    full_step_events = [
        e for e in full_store.read_events("full") if e["kind"] == "loop_step"
    ]

    part_path = tmp_path / "part.db"

    class _Crash(RuntimeError):
        pass

    crash_db = DBProgressLog(part_path, "run")

    def crashing_observer(record, state):
        crash_db.on_step(record, state)
        if state.iteration == 2:
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(
            on_step=crashing_observer,
            initial_state=crash_db.state,
            **_fresh_run_args(),
        )
    crash_db.close()

    resume_db = DBProgressLog(part_path, "run")
    result = run_loop(
        on_step=resume_db.on_step,
        initial_state=resume_db.state,
        **_fresh_run_args(),
    )
    resume_db.record_result(result)
    resume_db.close()

    resume_store = LoopStore(connect(part_path))
    resumed_step_events = [
        e for e in resume_store.read_events("run") if e["kind"] == "loop_step"
    ]
    # loop_begin has only the one event from before interruption (resume does
    # not record it again), and step events match the straight-through count.
    begins = [e for e in resume_store.read_events("run") if e["kind"] == "loop_begin"]
    assert len(begins) == 1
    assert len(resumed_step_events) == len(full_step_events) == GOAL_AT


def test_resume_from_a_capped_then_extended_run(tmp_path):
    # Even a plain cap setup without GoalMet continues from the restored state
    # and matches a straight-through run. The first run persists 2 steps with
    # MaxIterations(2); on resume the cap is widened to 5 and execution
    # continues.
    path = tmp_path / "state.db"

    db1 = DBProgressLog(path, "run")
    r1 = run_loop(
        act=acting(tokens=5, observation="w"),
        verify=never_done,
        conditions=[MaxIterations(2)],
        initial_state=db1.state,
        on_step=db1.on_step,
    )
    db1.record_result(r1)
    db1.close()
    assert r1.iterations == 2

    db2 = DBProgressLog(path, "run")
    assert db2.state.iteration == 2 and db2.state.tokens_used == 10
    r2 = run_loop(
        act=acting(tokens=5, observation="w"),
        verify=never_done,
        conditions=[MaxIterations(5)],
        initial_state=db2.state,
        on_step=db2.on_step,
    )
    db2.record_result(r2)
    db2.close()

    assert r2.iterations == 5
    assert r2.tokens_used == 25
    store = LoopStore(connect(path))
    steps = store.read_steps("run")
    assert [s["iteration"] for s in steps] == [0, 1, 2, 3, 4]
    assert [s["tokens_used"] for s in steps] == [5, 10, 15, 20, 25]


def test_resumed_elapsed_through_db_drives_timeout_like_straight_through(tmp_path):
    # Deterministically verify that elapsed still drives Timeout correctly after
    # persist -> reconstruct -> restore clock through the DB (explicitly
    # covering the DB-backed elapsed restoration path for the success condition's
    # "stop condition state"). The resume leg receives a fresh ManualClock,
    # equivalent to a new process (monotonic resets to 0 on restart), confirming
    # that back-dating preserves total elapsed time.
    def args(clock):
        return dict(
            act=stepping_for(clock, seconds=2.0),
            verify=never_done,
            conditions=[Timeout(7.0)],
            time_fn=clock,
        )

    # Straight-through run: step=2.0s, Timeout=7.0 -> the guard sees
    # 0,2,4,6,8 and fires at 8 (4 steps).
    full = DBProgressLog(tmp_path / "full.db", "full")
    full_result = run_loop(initial_state=full.state, on_step=full.on_step, **args(ManualClock()))
    full.record_result(full_result)
    full.close()

    # leg1: crash after persisting 1 step (elapsed=2.0 is committed into the
    # run aggregate).
    class _Crash(RuntimeError):
        pass

    db1 = DBProgressLog(tmp_path / "part.db", "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.iteration == 1:
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(on_step=crashing_observer, initial_state=db1.state, **args(ManualClock()))
    db1.close()

    # leg2: equivalent to another process = fresh ManualClock(0). Continue from
    # the restored elapsed value.
    db2 = DBProgressLog(tmp_path / "part.db", "run")
    assert db2.state.elapsed == 2.0  # 1 step * 2.0s is restored from the DB.
    resumed = run_loop(on_step=db2.on_step, initial_state=db2.state, **args(ManualClock()))
    db2.record_result(resumed)
    db2.close()

    assert resumed.stop.name == full_result.stop.name == "timeout"
    assert resumed.iterations == full_result.iterations == 4
    assert resumed.elapsed == full_result.elapsed == 8.0


def test_resume_at_cap_runs_zero_new_steps_via_db(tmp_path):
    # When resuming a run that crashed immediately after persisting the final
    # step (before record_result), the restored seed has already reached the cap,
    # so it exits immediately without running any new steps (the
    # guard-before-step contract also holds for a DB-restored seed).
    path = tmp_path / "state.db"

    class _Crash(RuntimeError):
        pass

    db1 = DBProgressLog(path, "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.iteration == 3:  # Crash immediately after reaching cap=3.
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(
            act=acting(tokens=10, observation="w"),
            verify=never_done,
            conditions=[MaxIterations(3)],
            initial_state=db1.state,
            on_step=crashing_observer,
        )
    db1.close()

    db2 = DBProgressLog(path, "run")
    assert db2.state.iteration == 3
    new_steps = []
    result = run_loop(
        act=acting(tokens=10, observation="w"),
        verify=never_done,
        conditions=[MaxIterations(3)],
        initial_state=db2.state,
        on_step=lambda record, state: new_steps.append(record),
    )
    db2.record_result(result)
    db2.close()

    assert new_steps == []  # Already reached the cap -> no new steps.
    assert result.iterations == 3
    assert result.tokens_used == 30
    assert result.stop.name == "max_iterations"
    store = LoopStore(connect(path))
    assert len(store.read_steps("run")) == 3  # Persistence also remains at 3 steps.


def test_resume_roundtrips_history_observations_through_json(tmp_path):
    # Pin a known limitation: history observations restored from state.db become
    # the JSON round-tripped value from storage (tuple -> list). Conditions that
    # use raw observations directly as keys need to account for this type drift
    # (see run_loop's initial_state docstring / README).
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("run")
    store.record_step("run", StepRecord(0, ("a", "b"), 0, False), LoopState(iteration=1))

    restored = store.load_or_init("run")
    assert restored.history[0].observation == ["a", "b"]  # tuple -> list
    assert isinstance(restored.history[0].observation, list)


def test_resume_noprogress_with_json_stable_key_matches_straight_through(tmp_path):
    # Demonstrate a mitigation for the limitation: if observations are used
    # directly as keys, tuple->list drift breaks resume (lists are unhashable).
    # But if NoProgress receives a key that projects to a JSON-stable signature,
    # resume matches the straight-through run (no_progress) even with tuple
    # observations.
    def _key(record):
        # Both tuple and list become the same JSON array, absorbing type drift
        # across the resume boundary.
        return json.dumps(record.observation, sort_keys=True, default=repr)

    def args():
        return dict(
            act=acting(tokens=0, observation=("noop", 1)),
            verify=never_done,
            conditions=[NoProgress(window=3, repeat=3, key=_key), MaxIterations(100)],
        )

    # Straight-through run: repeated identical observations -> no_progress fires
    # at iteration 3.
    full = DBProgressLog(tmp_path / "full.db", "full")
    full_result = run_loop(initial_state=full.state, on_step=full.on_step, **args())
    full.record_result(full_result)
    full.close()
    assert full_result.stop.name == "no_progress"
    assert full_result.iterations == 3

    # Crash after persisting 2 steps -> restore and continue. The key absorbs
    # type drift, so the result matches.
    class _Crash(RuntimeError):
        pass

    db1 = DBProgressLog(tmp_path / "part.db", "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.iteration == 2:
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(on_step=crashing_observer, initial_state=db1.state, **args())
    db1.close()

    db2 = DBProgressLog(tmp_path / "part.db", "run")
    resumed = run_loop(on_step=db2.on_step, initial_state=db2.state, **args())
    db2.record_result(resumed)
    db2.close()

    assert resumed.stop.name == full_result.stop.name == "no_progress"
    assert resumed.iterations == full_result.iterations == 3


def test_resume_of_verify_hook_completed_run_returns_goal_met_without_new_steps(tmp_path):
    # When resuming a run that crashed immediately after the final step that
    # reached the goal via the verify hook was persisted (before record_result),
    # the restored state.goal_met=True is respected and natural completion
    # (status=goal_met) is reproduced without running any new steps. Without
    # this, resuming an already completed run would run an extra act and diverge
    # from the straight-through result (Codex review P2).
    path = tmp_path / "state.db"

    class _Crash(RuntimeError):
        pass

    # leg1 verify: the goal is reached on the 3rd call (not re-evaluated on
    # resume).
    calls = {"n": 0}

    def verify_done_at_3(_outcome):
        calls["n"] += 1
        met = calls["n"] >= 3
        return VerifyOutcome(goal_met=met, detail="done" if met else "")

    db1 = DBProgressLog(path, "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.goal_met:  # Crash immediately after persisting the goal-met step.
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(
            act=acting(tokens=5, observation="w"),
            verify=verify_done_at_3,
            conditions=[MaxIterations(100)],
            initial_state=db1.state,
            on_step=crashing_observer,
        )
    db1.close()

    probe = LoopStore(connect(path))
    assert probe.get_run("run")["goal_met"] == 1
    assert len(probe.read_steps("run")) == 3
    probe.conn.close()

    db2 = DBProgressLog(path, "run")
    assert db2.state.goal_met is True

    def verify_must_not_run(_outcome):
        raise AssertionError("verify must not run when resuming a goal-met run")

    new_steps = []
    result = run_loop(
        act=acting(tokens=5, observation="w"),
        verify=verify_must_not_run,
        conditions=[MaxIterations(100)],
        initial_state=db2.state,
        on_step=lambda record, state: new_steps.append(record),
    )
    db2.record_result(result)
    db2.close()

    assert new_steps == []  # Already complete -> no new steps.
    assert result.status == "goal_met"
    assert result.goal_met is True
    assert result.stop is None
    assert result.iterations == 3
    assert result.tokens_used == 15


def _run_with_db_resumable(path, run_id):
    """Run straight through (no interruption) with resume-style wiring."""
    db = DBProgressLog(path, run_id)
    result = run_loop(
        initial_state=db.state, on_step=db.on_step, **_fresh_run_args()
    )
    db.record_result(result)
    db.close()
    return result, db
