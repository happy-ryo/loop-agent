"""外側 Reflexion ループの永続化/resume テスト (Issue #29)。

核心は「中断→resume が通し実行と一致する」(episode 数 / 採用 lesson / 評価器 version /
best ground-truth) の実証。さらに評価器 version 不一致の fail-loud、テーブル移行の非破壊、
評価器 version registry、paused episode を永続化しないことを固める。
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


# -- 共通スタブ (test_reflexion.py の意匠を踏襲) --------------------------------

DECLARED = ("primary",)


def fail_episode(ctx):
    """ctx.episode を観測に埋めた **常に失敗** する内側結果 (lesson を決定的にするため)。"""
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
    """episode 観測から決定的な grounded lesson を作る (呼び出し順に依存しない)。"""
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
# round-trip: 永続化 → 復元
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
    # signal / lesson が忠実に往復する。
    assert all(isinstance(e["signal"], GroundTruthSignal) for e in episodes)
    assert episodes[0]["signal"].score.ground_truth == pytest.approx(0.2)
    assert episodes[0]["lesson"].text == "lesson-ep0"


def test_loaded_state_matches_in_memory_state():
    """復元した ReflexionState が走行終了時の in-memory state と一致する。"""
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
# 目玉: 中断 → resume が通し実行と一致する
# ==============================================================================


def _straight_through(cap: int):
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref", memory=EpisodicMemory(cap=3))
    return run_reflexion(
        **_base(convergence=[MaxEpisodes(cap)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )


def test_interrupt_resume_equals_straight_through(tmp_path):
    """MaxEpisodes(3) で中断 → 同じ DB を別接続で開いて MaxEpisodes(6) へ継続。

    通し MaxEpisodes(6) と episode 数 / epoch / 採用 lesson / 評価器 version /
    best ground-truth / reflections / gt 履歴 が一致する。
    """
    straight = _straight_through(6)

    db = tmp_path / "outer.db"
    # 第 1 プロセス: 3 episode 走って中断 (接続を閉じる = プロセス終了相当)。
    log1 = DBReflexionLog(str(db), "ref", memory=EpisodicMemory(cap=3))
    run_reflexion(
        **_base(convergence=[MaxEpisodes(3)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    log1.close()

    # 第 2 プロセス: 同じ DB を開き直して resume (memory 容量も DB の保存値で復元)。
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
    # 採用 lesson (memory の現在像。cap=3 の eviction まで一致)。
    assert [(l.text, l.episode) for l in resumed.state.memory.lessons()] == [
        (l.text, l.episode) for l in straight.state.memory.lessons()
    ]


def test_boundary_interrupt_resume_recovers_epoch_and_promotion(tmp_path):
    """回帰: epoch 境界ちょうどで中断 → resume が抑止された末尾境界 (epoch 昇格 + 評価器昇格) を
    取り戻し、通し実行と epoch / 評価器 version / evaluator_updates が一致する。

    fix 前は、境界での昇格が「終端扱い」で抑止されたまま永続化され、resume が epoch=0 /
    version=FLAT / updates=0 へ silently 乖離していた (通しは epoch=1 / HONEST / updates=1)。
    """
    # 通し MaxEpisodes(4): ep2 の非終端境界で FLAT→HONEST に昇格 (ep4 は終端で抑止)。
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
    # 中断 MaxEpisodes(2): ep2 = epoch 境界ちょうど。昇格は「終端扱い」で抑止される。
    log1 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(
            episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,
        ),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    # 永続化 version は **抑止された昇格前** の FLAT を指す (= resume 時に渡すべき評価器)。
    assert ReflexionStore(log1.conn).get_run("ref")["evaluator_version"] == FLAT.version
    log1.close()

    # resume: 永続 version に一致する FLAT を渡す。recovery が末尾境界を取り戻し HONEST へ昇格する。
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
    """回帰: resume の末尾境界 recovery が episode を 1 つも完了させず即停止しても、record_result が
    recovery 後の状態を flush するので DB が返り値と一致し、再 resume が昇格を二度踏まない。"""
    db = tmp_path / "reconly.db"
    # 境界ちょうどで中断 → 昇格は抑止され FLAT/epoch0/updates0 が永続化される。
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

    # resume: recovery が昇格 (updates 0→1) した直後、次の while ガードで EvaluatorUpdateBudget(1) が
    # 発火し episode を 1 つも完了させずに停止する。persist フックは呼ばれない。
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
    assert result.state.episode == 2                       # 新規 episode は 0
    assert result.state.evaluator_version == HONEST.version  # recovery が in-memory で昇格
    log2.record_result(result)                             # ← settled state を flush
    run = ReflexionStore(log2.conn).get_run("ref")
    assert run["evaluator_version"] == HONEST.version       # DB が返り値と一致 (stale FLAT でない)
    assert run["epoch"] == 1
    assert run["evaluator_updates"] == 1
    log2.close()

    # 再 resume: epoch がもう lag していないので recovery は再発火しない。
    log3 = DBReflexionLog(str(db), "ref")
    assert log3.state.epoch == 1
    assert log3.state.evaluator_version == HONEST.version
    log3.close()


def test_resume_clears_stale_terminal_status(tmp_path):
    """回帰: stopped を記録した run を resume して episode を進めたら、status が running へ戻り
    ended_at がクリアされる (reflexion_run が lifecycle の SoT であり続ける)。"""
    db = tmp_path / "lifecycle.db"
    log1 = DBReflexionLog(str(db), "ref")
    r1 = run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    log1.record_result(r1)                                  # status=stopped, ended_at 立つ
    run1 = ReflexionStore(log1.conn).get_run("ref")
    assert run1["status"] == "stopped" and run1["ended_at"] is not None
    log1.close()

    # resume して episode を進める (persist フックのみ。record_result は呼ばない)。
    log2 = DBReflexionLog(str(db), "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(4)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    run2 = ReflexionStore(log2.conn).get_run("ref")
    assert run2["episode"] == 4
    assert run2["status"] == "running"                      # stale stopped でない
    assert run2["stop_name"] is None
    assert run2["ended_at"] is None
    log2.close()


def test_observed_resume_epoch_events_consistent_with_db(tmp_path):
    """統合 (#29×#30): 観測 (on_epoch) と永続化 (persist) + resume を 1 経路で両立し、emit した
    epoch_boundary event が DB の settled epoch / 評価器 version と整合する。

    境界ちょうどで中断すると末尾境界は抑止され epoch_boundary は出ない (DB も昇格前)。resume の
    recovery がその境界を取り戻すと on_epoch が 1 度 emit され、DB の epoch/version もそれと一致
    する (観測の epoch 数が DB の SoT と食い違わない)。
    """
    from loop_agent import EPOCH_BOUNDARY, ListSink, run_observed_reflexion

    db = tmp_path / "obs.db"
    common = dict(
        ground_truth=gt_fail(), reflect=reflect_per_episode,
        declared_keys=DECLARED, production_tasks=["task-a"],
        held_out=held_out_matching(0.0, 0.5, 1.0), epoch_len=2,
        propose_evaluator=lambda o, i: HONEST, otel=False,
    )

    # 中断 (observed + persisted): ep2 = 境界ちょうど → 末尾境界は抑止される。
    sink1 = ListSink()
    log1 = DBReflexionLog(str(db), "ref")
    run_observed_reflexion(
        episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(2)],
        persist=log1.on_episode, initial_state=log1.state, sinks=[sink1], **common,
    )
    assert len(sink1.of_kind(EPOCH_BOUNDARY)) == 0          # 抑止 → 観測も出ない
    assert ReflexionStore(log1.conn).get_run("ref")["evaluator_version"] == FLAT.version
    log1.close()

    # resume (observed + persisted): recovery が抑止境界を取り戻し on_epoch を 1 度 emit する。
    sink2 = ListSink()
    log2 = DBReflexionLog(str(db), "ref")
    result2 = run_observed_reflexion(
        episode=_ok_episode, evaluator=FLAT, convergence=[MaxEpisodes(4)],
        persist=log2.on_episode, initial_state=log2.state, sinks=[sink2], **common,
    )
    boundaries = sink2.of_kind(EPOCH_BOUNDARY)
    assert len(boundaries) == 1                              # recovery が末尾境界を観測
    assert boundaries[0].payload["promoted"] is True
    assert boundaries[0].payload["evaluator_version"] == HONEST.version
    # 観測 event が DB の settled SoT と整合する。
    run = ReflexionStore(log2.conn).get_run("ref")
    assert run["epoch"] == boundaries[0].payload["epoch"] == 1
    assert run["evaluator_version"] == boundaries[0].payload["evaluator_version"]
    assert result2.state.epoch == 1
    log2.close()


def test_raw_borrowed_connection_resume_works(tmp_path):
    """回帰: 素の sqlite3.connect() を借用しても、生成時に正規化されるので resume の read が壊れない。"""
    db = str(tmp_path / "raw.db")
    raw = sqlite3.connect(db)                  # row_factory なし・foreign_keys OFF の素の接続
    log = DBReflexionLog(raw, "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(3)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    raw.close()

    raw2 = sqlite3.connect(db)
    log2 = DBReflexionLog(raw2, "ref")         # reconstruct (列名アクセス) が TypeError にならない
    assert log2.state.episode == 3
    assert len(log2.state.episodes) == 3
    assert [l.text for l in log2.memory.lessons()] == ["lesson-ep0", "lesson-ep1", "lesson-ep2"]
    raw2.close()


def test_resume_continues_lesson_accumulation_across_processes(tmp_path):
    """resume をまたいで lesson が積み上がり続ける (途中状態が seed として効く)。"""
    db = tmp_path / "acc.db"
    log1 = DBReflexionLog(str(db), "ref", memory=EpisodicMemory(cap=10))
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log1.state, memory=log1.memory, persist=log1.on_episode,
    )
    assert len(log1.memory) == 2
    log1.close()

    log2 = DBReflexionLog(str(db), "ref")
    assert len(log2.memory) == 2  # 復元時点で前 2 件を保持
    run_reflexion(
        **_base(convergence=[MaxEpisodes(4)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert {l.text for l in log2.memory.lessons()} == {
        "lesson-ep0", "lesson-ep1", "lesson-ep2", "lesson-ep3"
    }


# ==============================================================================
# 評価器 version registry + 不一致 fail-loud
# ==============================================================================


def test_promotion_persisted_and_version_registry_recorded():
    """境界で評価器が昇格したら、永続化 version は昇格後を指し registry が両 version を持つ。"""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "promo")
    run_reflexion(
        **_base(
            episode=lambda ctx: _ok_episode(ctx),
            ground_truth=gt_fail(),
            evaluator=FLAT,                       # 初期 incumbent
            convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            propose_evaluator=lambda outer, inc: HONEST,  # 毎境界 honest を提案
        ),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    run = ReflexionStore(conn).get_run("promo")
    assert run["evaluator_version"] == HONEST.version       # 昇格後を指す
    versions = [v["version"] for v in ReflexionStore(conn).read_evaluator_versions("promo")]
    assert FLAT.version in versions and HONEST.version in versions


def _ok_episode(ctx):
    obs = f"ep{ctx.episode}"
    step = StepRecord(iteration=0, observation=obs, tokens=1, goal_met=True, detail=obs)
    state = LoopState(iteration=1, history=[step], goal_met=True)
    return LoopResult(status="goal_met", stop=None, state=state)


def test_resume_rejects_mismatched_evaluator_version(tmp_path):
    """昇格後に resume するとき、昇格前の評価器を渡すと version 不一致で loud に弾く。"""
    db = tmp_path / "mismatch.db"
    # MaxEpisodes(4), epoch_len=2: episode2 の **非終端** 境界で FLAT→HONEST に昇格する。
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
    assert promoted_version == HONEST.version     # 昇格後の version が永続化されている
    log1.close()

    log2 = DBReflexionLog(str(db), "ref")
    # 昇格前の FLAT を渡す → 復元 version (HONEST) と食い違い fail-loud。
    with pytest.raises(ValueError, match="resume"):
        run_reflexion(
            **_base(
                episode=_ok_episode,
                evaluator=FLAT,                   # 復元 version と不一致
                convergence=[MaxEpisodes(6)],
                held_out=held_out_matching(0.0, 0.5, 1.0),
            ),
            initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
        )
    # 昇格後の HONEST を渡せば resume できる。
    resumed = run_reflexion(
        **_base(
            episode=_ok_episode,
            evaluator=HONEST,                     # 復元 version と一致
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
        ),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert resumed.state.episode == 6
    log2.close()


def test_resume_rejects_mismatched_declared_keys():
    """別の declared_keys で resume すると stale 集約で誤収束しうるので loud に弾く。"""
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
# テーブル移行の非破壊 (旧 DB = reflexion 表を持たない内側専用 DB)
# ==============================================================================


def test_migration_non_destructive(tmp_path):
    """内側データだけの旧 DB を開き直しても、既存データ無傷で reflexion 表が追加される。"""
    db = tmp_path / "old.db"
    # connect() は内側スキーマのみ適用する (reflexion 表は ReflexionStore が作る)。
    inner = LoopStore(connect(str(db)))
    inner.load_or_init("inner-run")
    rec = StepRecord(iteration=0, observation={"k": 1}, tokens=5, goal_met=False, detail="d")
    inner.record_step("inner-run", rec, LoopState(iteration=1, tokens_used=5))
    inner.conn.close()

    # まだ reflexion 表は無い (旧 DB 相当)。
    raw = sqlite3.connect(str(db))
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "run" in tables and "step" in tables
    assert "reflexion_run" not in tables
    raw.close()

    # ReflexionStore 生成で reflexion 表が非破壊に追加される。
    conn = connect(str(db))
    rstore = ReflexionStore(conn)
    tables2 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reflexion_run", "reflexion_episode", "reflexion_lesson", "reflexion_evaluator"} <= tables2

    # 既存の内側データは無傷。
    inner2 = LoopStore(conn)
    steps = inner2.read_steps("inner-run")
    assert len(steps) == 1
    assert steps[0]["observation"] == {"k": 1}
    assert steps[0]["tokens"] == 5

    # かつ外側の永続化が機能する。
    log = DBReflexionLog(conn, "outer-run")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    assert rstore.get_run("outer-run")["episode"] == 2


def test_reflexion_store_init_is_idempotent():
    """ReflexionStore を複数回生成しても既存 reflexion データを壊さない (IF NOT EXISTS)。"""
    conn = connect(":memory:")
    log = DBReflexionLog(conn, "ref")
    run_reflexion(
        **_base(convergence=[MaxEpisodes(2)]),
        initial_state=log.state, memory=log.memory, persist=log.on_episode,
    )
    # 2 度目の生成 (= 別の reader) でも episode 行が残る。
    again = ReflexionStore(conn)
    assert len(again.read_episodes("ref")) == 2


# ==============================================================================
# paused episode は永続化しない (未確定 episode を書かない)
# ==============================================================================


def test_paused_episode_not_persisted():
    """内側 pause で外側が中断したら、その episode は persist されない (resume で再実行できる)。"""
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
    assert store.get_run("ref")["episode"] == 0      # 未確定 episode は進めない
    assert store.read_episodes("ref") == []          # episode 行は書かれない


def test_record_result_paused_is_not_terminal():
    """record_result で paused を記録しても ended_at は立てない (resume で続行できる)。"""
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
# memory 容量ポリシーの保存/復元 (eviction 挙動が resume をまたいで一致)
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
    # 復元 memory の cap が 2 のまま (DB の保存値で組み直す)。
    assert log2.memory.cap == 2
    run_reflexion(
        **_base(convergence=[MaxEpisodes(5)]),
        initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
    )
    assert len(log2.memory) == 2  # cap=2 で有界のまま
    log2.close()
