"""Persistence/resume tests for the outer Reflexion loop (Issue #29).

The core proof is that interrupt->resume matches a straight-through run (episode count /
adopted lessons / evaluator version / best ground-truth). It also locks down fail-loud
behavior for evaluator version mismatches, non-destructive table migration, the evaluator
version registry, and not persisting paused episodes.
"""

from __future__ import annotations

import sqlite3

import pytest

from loop_agent.conditions import StopTrigger
from loop_agent.convergence import EvaluatorUpdateBudget, MaxEpisodes, RubricThreshold
from loop_agent.evaluator import Evaluator, GroundTruthSignal, HeldOut, Probe, Score
from loop_agent.loop import LoopResult
from loop_agent.memory import EpisodicMemory, Lesson, step_signature
from loop_agent.reflexion import ReflexionState, run_reflexion
from loop_agent.reflexion_store import DBReflexionLog, ReflexionStore
from loop_agent.state import LoopState, StepRecord
from loop_agent.store import LoopStore, connect


# -- Shared stubs (following the style from test_reflexion.py) -----------------

DECLARED = ("primary",)


def fail_episode(ctx):
    """An inner result that **always fails** and embeds ctx.episode in the observation."""
    obs = f"ep{ctx.episode}"
    step = StepRecord(iteration=0, observation=obs, tokens=1, goal_met=False, detail=obs)
    state = LoopState(iteration=1, history=[step], goal_met=False)
    return LoopResult(
        status="stopped",
        stop=StopTrigger(name="max_iterations", reason="cap"),
        state=state,
    )


def gt_fail(hi: float = 0.9, lo: float = 0.2):
    def gt(outcome):
        val = hi if outcome.succeeded else lo
        return GroundTruthSignal(
            succeeded=outcome.succeeded,
            score=Score(ground_truth=val, components={k: val for k in DECLARED}),
        )

    return gt


def reflect_per_episode(history, signal, reward):
    """Build a deterministic grounded lesson from the episode observation."""
    if signal.succeeded:
        return None
    obs = history[0].detail
    return Lesson(
        text=f"lesson-{obs}", episode=0,
        provenance=step_signature(history[0]), support=1.0,
    )


def _truth(o):
    if hasattr(o, "succeeded"):
        return 1.0 if o.succeeded else 0.0
    return o["truth"]


HONEST = Evaluator(score=lambda o: Score(ground_truth=_truth(o)), name="honest")
FLAT = Evaluator(score=lambda o: Score(ground_truth=0.5), name="flat")


def held_out_matching(*golds: float) -> HeldOut:
    return HeldOut(
        tuple(Probe(f"probe-{i}", {"truth": g}, gold_label=g) for i, g in enumerate(golds))
    )


def _base(**override):
    kwargs = dict(
        episode=fail_episode,
        ground_truth=gt_fail(),
        reflect=reflect_per_episode,
        evaluator=HONEST,
        convergence=[MaxEpisodes(6)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.0, 0.5, 1.0),
        epoch_len=2,
    )
    kwargs.update(override)
    return kwargs


# ==============================================================================
# round-trip: persistence -> restore
# ==============================================================================


def test_fresh_run_persists_and_reads_back():
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "run-1", memory=EpisodicMemory(cap=8))
    result = run_reflexion(
        **_base(convergence=[MaxEpisodes(3)]),
        initial_state=log.state,
        memory=log.memory,
        persist=log.on_episode,
    )
    log.record_result(result)

    store = ReflexionStore(conn)
    run = store.get_run("run-1")
    assert run["episode"] == 3
    assert run["status"] == "stopped"
    assert run["stop_name"] == "max_episodes"
    episodes = store.read_episodes("run-1")
    assert [e["episode"] for e in episodes] == [0, 1, 2]
    # signal / lesson round-trip faithfully.
    assert all(isinstance(e["signal"], GroundTruthSignal) for e in episodes)
    assert episodes[0]["signal"].score.ground_truth == pytest.approx(0.2)
    assert episodes[0]["lesson"].text == "lesson-ep0"


def test_loaded_state_matches_in_memory_state():
    """The restored ReflexionState matches the in-memory state at run completion."""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "run-x")
    result = run_reflexion(
        **_base(convergence=[MaxEpisodes(5)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    reloaded = ReflexionStore(conn).load_or_init("run-x")
    assert reloaded.episode == result.state.episode
    assert reloaded.epoch == result.state.epoch
    assert reloaded.evaluator_version == result.state.evaluator_version
    assert reloaded.best_gt_aggregate == pytest.approx(result.state.best_gt_aggregate)
    assert reloaded.reflections == result.state.reflections
    assert reloaded.declared_keys == result.state.declared_keys
    assert reloaded.gt_aggregate_history == result.state.gt_aggregate_history
    assert [l.text for l in reloaded.memory.lessons()] == [
        l.text for l in result.state.memory.lessons()
    ]


# ==============================================================================
# Main case: interrupt -> resume matches straight-through execution
# ==============================================================================


def _straight_through(cap: int):
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref", memory=EpisodicMemory(cap=3))
    return run_reflexion(
        **_base(convergence=[MaxEpisodes(cap)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )


def test_interrupt_resume_equals_straight_through(tmp_path):
    """Interrupt at MaxEpisodes(3), then reopen the same DB and continue to MaxEpisodes(6).

    The result matches a straight-through MaxEpisodes(6) run for episode count / epoch /
    adopted lessons / evaluator version / best ground-truth / reflections / gt history.
    """
    straight = _straight_through(6)

    db = tmp_path / "outer.db"
    # First process: run 3 episodes and interrupt (closing the connection is process exit).
    log1 = DBReflexionLog(str(db), "ref", memory=EpisodicMemory(cap=3))
    run_reflexion(
        **_base(convergence=[MaxEpisodes(3)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    log1.close()

    # Second process: reopen the same DB and resume (memory cap restored from the DB).
    log2 = DBReflexionLog(str(db), "ref")
    resumed = run_reflexion(
        **_base(convergence=[MaxEpisodes(6)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    log2.close()

    assert resumed.state.episode == straight.state.episode == 6
    assert resumed.state.epoch == straight.state.epoch
    assert resumed.state.evaluator_version == straight.state.evaluator_version
    assert resumed.best_score == pytest.approx(straight.best_score)
    assert resumed.state.reflections == straight.state.reflections
    assert resumed.state.gt_aggregate_history == straight.state.gt_aggregate_history
    # Adopted lessons (the current memory view, including cap=3 eviction).
    assert [(l.text, l.episode) for l in resumed.state.memory.lessons()] == [
        (l.text, l.episode) for l in straight.state.memory.lessons()
    ]


def test_boundary_interrupt_resume_recovers_epoch_and_promotion(tmp_path):
    """Regression: interrupt exactly at an epoch boundary, then resume recovers the suppressed
    trailing boundary (epoch advancement + evaluator promotion) and matches straight-through
    execution for epoch / evaluator version / evaluator_updates.

    Before the fix, boundary promotion was suppressed as "terminal" and persisted that way,
    so resume silently diverged to epoch=0 / version=FLAT / updates=0 (straight-through was
    epoch=1 / HONEST / updates=1).
    """
    # Straight-through MaxEpisodes(4): promote FLAT->HONEST at the non-terminal ep2 boundary
    # (ep4 is terminal, so promotion is suppressed).
    conn = connect(":memory:")
    slog = DBReflexionLog(conn, "s")
    straight = run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,
        ),
        initial_state=slog.state, memory=slog.memory, persist=slog.on_episode,
    )
    assert straight.state.epoch == 1
    assert straight.state.evaluator_version == HONEST.version
    assert straight.state.evaluator_updates == 1

    db = tmp_path / "boundary.db"
    # Interrupt at MaxEpisodes(2): ep2 is exactly an epoch boundary. Promotion is suppressed
    # as "terminal".
    log1 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,
        ),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    # The persisted version points to FLAT, **before the suppressed promotion** (= the
    # evaluator to pass on resume).
    assert ReflexionStore(log1.conn).get_run("ref")["evaluator_version"] == FLAT.version
    log1.close()

    # Resume: pass FLAT, matching the persisted version. Recovery restores the trailing
    # boundary and promotes to HONEST.
    log2 = DBReflexionLog(str(db), "ref")
    resumed = run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,
        ),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    log2.close()
    assert resumed.state.episode == straight.state.episode == 4
    assert resumed.state.epoch == straight.state.epoch == 1
    assert resumed.state.evaluator_version == straight.state.evaluator_version == HONEST.version
    assert resumed.state.evaluator_updates == straight.state.evaluator_updates == 1


def test_recovery_only_resume_is_persisted_by_record_result(tmp_path):
    """Regression: when resume's trailing-boundary recovery stops before completing any
    episode, record_result flushes the recovered state so the DB matches the return value
    and the next resume does not apply the promotion twice.
    """
    db = tmp_path / "reconly.db"
    # Interrupt exactly at the boundary -> promotion is suppressed and FLAT/epoch0/updates0
    # is persisted.
    log1 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda o, i: HONEST,
        ),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    assert ReflexionStore(log1.conn).get_run("ref")["evaluator_version"] == FLAT.version
    log1.close()

    # Resume: immediately after recovery promotes (updates 0->1), the next while guard
    # triggers EvaluatorUpdateBudget(1) and stops before completing any episode. The persist
    # hook is not called.
    log2 = DBReflexionLog(str(db), "ref")
    result = run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT,
            convergence=[EvaluatorUpdateBudget(1), MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda o, i: HONEST,
        ),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert result.stop.name == "evaluator_update_budget"
    assert result.state.episode == 2                       # zero new episodes
    assert result.state.evaluator_version == HONEST.version  # recovery promoted in memory
    log2.record_result(result)                             # flush the settled state
    run = ReflexionStore(log2.conn).get_run("ref")
    assert run["evaluator_version"] == HONEST.version       # DB matches return value, not stale FLAT
    assert run["epoch"] == 1
    assert run["evaluator_updates"] == 1
    log2.close()

    # Resume again: epoch is no longer lagging, so recovery does not fire again.
    log3 = DBReflexionLog(str(db), "ref")
    assert log3.state.epoch == 1
    assert log3.state.evaluator_version == HONEST.version
    log3.close()


def test_resume_clears_stale_terminal_status(tmp_path):
    """Regression: when resuming a run recorded as stopped and advancing episodes, status
    returns to running and ended_at is cleared (reflexion_run remains the lifecycle SoT).
    """
    db = tmp_path / "lifecycle.db"
    log1 = DBReflexionLog(str(db), "ref")
    r1 = run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    log1.record_result(r1)                                  # status=stopped, ended_at is set
    run1 = ReflexionStore(log1.conn).get_run("ref")
    assert run1["status"] == "stopped" and run1["ended_at"] is not None
    log1.close()

    # Resume and advance episodes (persist hook only; record_result is not called).
    log2 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(4)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    run2 = ReflexionStore(log2.conn).get_run("ref")
    assert run2["episode"] == 4
    assert run2["status"] == "running"                      # not stale stopped
    assert run2["stop_name"] is None
    assert run2["ended_at"] is None
    log2.close()


def test_observed_resume_epoch_events_consistent_with_db(tmp_path):
    """Integration (#29 x #30): observation (on_epoch), persistence (persist), and resume all
    work on one path, and emitted epoch_boundary events agree with the DB's settled epoch /
    evaluator version.

    When interrupted exactly at a boundary, the trailing boundary is suppressed and no
    epoch_boundary is emitted (the DB also remains before promotion). When resume recovery
    restores that boundary, on_epoch emits once and the DB epoch/version matches it (the
    observed epoch count does not diverge from the DB SoT).
    """
    from loop_agent import EPOCH_BOUNDARY, ListSink, run_observed_reflexion

    db = tmp_path / "obs.db"
    common = dict(
        ground_truth=gt_fail(), reflect=reflect_per_episode,
        declared_keys=DECLARED, production_tasks=["task-a"],
        held_out=held_out_matching(0.0, 0.5, 1.0), epoch_len=2,
        propose_evaluator=lambda o, i: HONEST, otel=False,
    )

    # Interrupt (observed + persisted): ep2 is exactly the boundary -> trailing boundary is
    # suppressed.
    sink1 = ListSink()
    log1 = DBReflexionLog(str(db), "ref")
    run_observed_reflexion(
        episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(2)],
        persist=log1.on_episode, initial_state=log1.state, sinks=[sink1], **common,
    )
    assert len(sink1.of_kind(EPOCH_BOUNDARY)) == 0          # suppressed -> not observed either
    assert ReflexionStore(log1.conn).get_run("ref")["evaluator_version"] == FLAT.version
    log1.close()

    # Resume (observed + persisted): recovery restores the suppressed boundary and emits
    # on_epoch once.
    sink2 = ListSink()
    log2 = DBReflexionLog(str(db), "ref")
    result2 = run_observed_reflexion(
        episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(4)],
        persist=log2.on_episode, initial_state=log2.state, sinks=[sink2], **common,
    )
    boundaries = sink2.of_kind(EPOCH_BOUNDARY)
    assert len(boundaries) == 1                              # recovery observes the trailing boundary
    assert boundaries[0].payload["promoted"] is True
    assert boundaries[0].payload["evaluator_version"] == HONEST.version
    # The observed event agrees with the DB's settled SoT.
    run = ReflexionStore(log2.conn).get_run("ref")
    assert run["epoch"] == boundaries[0].payload["epoch"] == 1
    assert run["evaluator_version"] == boundaries[0].payload["evaluator_version"]
    assert result2.state.epoch == 1
    log2.close()


def test_raw_borrowed_connection_resume_works(tmp_path):
    """Regression: a raw borrowed sqlite3.connect() is normalized at construction, so resume
    reads do not break.
    """
    db = str(tmp_path / "raw.db")
    raw = sqlite3.connect(db)                  # raw connection: no row_factory, foreign_keys OFF
    log = DBReflexionLog(raw, "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(3)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    raw.close()

    raw2 = sqlite3.connect(db)
    log2 = DBReflexionLog(raw2, "ref")         # reconstruct (column-name access) avoids TypeError
    assert log2.state.episode == 3
    assert len(log2.state.episodes) == 3
    assert [l.text for l in log2.memory.lessons()] == ["lesson-ep0", "lesson-ep1", "lesson-ep2"]
    raw2.close()


def test_resume_continues_lesson_accumulation_across_processes(tmp_path):
    """Lessons keep accumulating across resume (the intermediate state acts as the seed)."""
    db = tmp_path / "acc.db"
    log1 = DBReflexionLog(str(db), "ref", memory=EpisodicMemory(cap=10))
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    assert len(log1.memory) == 2
    log1.close()

    log2 = DBReflexionLog(str(db), "ref")
    assert len(log2.memory) == 2  # retains the previous 2 entries at restore time
    run_reflexion(
        **_base(convergence=[MaxEpisodes(4)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert {l.text for l in log2.memory.lessons()} == {
        "lesson-ep0", "lesson-ep1", "lesson-ep2", "lesson-ep3"
    }


# ==============================================================================
# Evaluator version registry + mismatch fail-loud
# ==============================================================================


def test_promotion_persisted_and_version_registry_recorded():
    """When the evaluator is promoted at a boundary, the persisted version points to the
    promoted evaluator and the registry contains both versions.
    """
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "promo")
    run_reflexion(
        **_base(
            episode=lambda ctx: _ok_episode(ctx),
            ground_truth=gt_fail(),
            evaluator=FLAT,                       # initial incumbent
            convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,  # propose honest at each boundary
        ),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    run = ReflexionStore(conn).get_run("promo")
    assert run["evaluator_version"] == HONEST.version       # points to the promoted evaluator
    versions = [v["version"] for v in ReflexionStore(conn).read_evaluator_versions("promo")]
    assert FLAT.version in versions and HONEST.version in versions


def _ok_episode(ctx):
    obs = f"ep{ctx.episode}"
    step = StepRecord(iteration=0, observation=obs, tokens=1, goal_met=True, detail=obs)
    state = LoopState(iteration=1, history=[step], goal_met=True)
    return LoopResult(status="goal_met", stop=None, state=state)


def test_resume_rejects_mismatched_evaluator_version(tmp_path):
    """When resuming after promotion, passing the pre-promotion evaluator fails loudly."""
    db = tmp_path / "mismatch.db"
    # MaxEpisodes(4), epoch_len=2: promote FLAT->HONEST at the **non-terminal** episode2
    # boundary.
    log1 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(
            episode=_ok_episode,
            evaluator=FLAT,
            convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,
        ),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    promoted_version = ReflexionStore(log1.conn).get_run("ref")["evaluator_version"]
    assert promoted_version == HONEST.version     # the promoted version is persisted
    log1.close()

    log2 = DBReflexionLog(str(db), "ref")
    # Pass pre-promotion FLAT -> it mismatches the restored version (HONEST) and fails loudly.
    with pytest.raises(ValueError, match="resume"):
        run_reflexion(
            **_base(
                episode=_ok_episode,
                evaluator=FLAT,                   # mismatches the restored version
                convergence=[MaxEpisodes(6)],
                held_out=held_out_matching(0.0, 0.5, 1.0),
            ),
            initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
        )
    # Passing post-promotion HONEST allows resume.
    resumed = run_reflexion(
        **_base(
            episode=_ok_episode,
            evaluator=HONEST,                     # matches the restored version
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
        ),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert resumed.state.episode == 6
    log2.close()


def test_resume_rejects_mismatched_declared_keys():
    """Resuming with different declared_keys could converge on stale aggregates, so fail loudly."""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    reloaded = ReflexionStore(conn).load_or_init("ref")
    with pytest.raises(ValueError, match="declared_keys"):
        run_reflexion(
            **_base(declared_keys=("other_axis",), convergence=[MaxEpisodes(4)]),
            initial_state=reloaded, memory=reloaded.memory, persist=log.on_episode,
        )


# ==============================================================================
# Non-destructive table migration (old DB = inner-only DB without reflexion tables)
# ==============================================================================


def test_migration_non_destructive(tmp_path):
    """When reopening an old DB with only inner data, reflexion tables are added without
    damaging existing data.
    """
    db = tmp_path / "old.db"
    # connect() applies only the inner schema (ReflexionStore creates the reflexion tables).
    inner = LoopStore(connect(str(db)))
    inner.load_or_init("inner-run")
    rec = StepRecord(iteration=0, observation={"k": 1}, tokens=5, goal_met=False, detail="d")
    inner.record_step("inner-run", rec, LoopState(iteration=1, tokens_used=5))
    inner.conn.close()

    # There are still no reflexion tables (equivalent to an old DB).
    raw = sqlite3.connect(str(db))
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "run" in tables and "step" in tables
    assert "reflexion_run" not in tables
    raw.close()

    # Constructing ReflexionStore adds reflexion tables non-destructively.
    conn = connect(str(db))
    rstore = ReflexionStore(conn)
    tables2 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reflexion_run", "reflexion_episode", "reflexion_lesson", "reflexion_evaluator"} <= tables2

    # Existing inner data is intact.
    inner2 = LoopStore(conn)
    steps = inner2.read_steps("inner-run")
    assert len(steps) == 1
    assert steps[0]["observation"] == {"k": 1}
    assert steps[0]["tokens"] == 5

    # Outer persistence also works.
    log = DBReflexionLog(conn, "outer-run")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    assert rstore.get_run("outer-run")["episode"] == 2


def test_reflexion_store_init_is_idempotent():
    """Creating ReflexionStore multiple times does not damage existing reflexion data."""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    # Episode rows remain after a second construction (= another reader).
    again = ReflexionStore(conn)
    assert len(again.read_episodes("ref")) == 2


# ==============================================================================
# Paused episodes are not persisted (do not write unsettled episodes)
# ==============================================================================


def test_paused_episode_not_persisted():
    """When an inner pause interrupts the outer loop, that episode is not persisted."""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref")
    paused = LoopResult(
        status="paused", stop=None, state=LoopState(), pending={"gate_key": "g0"}
    )
    result = run_reflexion(
        **_base(episode=lambda ctx: paused, convergence=[MaxEpisodes(5)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    assert result.paused is True
    store = ReflexionStore(conn)
    assert store.get_run("ref")["episode"] == 0      # unsettled episodes do not advance
    assert store.read_episodes("ref") == []          # no episode rows are written


def test_record_result_paused_is_not_terminal():
    """Recording paused with record_result does not set ended_at, so resume can continue."""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref")
    paused = LoopResult(
        status="paused", stop=None, state=LoopState(), pending={"gate_key": "g0"}
    )
    result = run_reflexion(
        **_base(episode=lambda ctx: paused, convergence=[MaxEpisodes(5)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    log.record_result(result)
    run = ReflexionStore(conn).get_run("ref")
    assert run["status"] == "paused"
    assert run["ended_at"] is None


# ==============================================================================
# Save/restore the memory capacity policy (eviction behavior matches across resume)
# ==============================================================================


def test_memory_cap_persisted_across_resume(tmp_path):
    db = tmp_path / "cap.db"
    log1 = DBReflexionLog(str(db), "ref", memory=EpisodicMemory(cap=2))
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    log1.close()

    log2 = DBReflexionLog(str(db), "ref")
    # Restored memory keeps cap=2 (rebuilt from the DB's saved value).
    assert log2.memory.cap == 2
    run_reflexion(
        **_base(convergence=[MaxEpisodes(5)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert len(log2.memory) == 2  # remains bounded at cap=2
    log2.close()
