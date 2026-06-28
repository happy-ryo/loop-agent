"""resume の検証: 中断 -> 再開が通し実行と状態欠落なく一致すること (Issue #14).

report.md S5 Phase 2 成功条件 a の回帰テスト。state.db SoT に永続化済みの step から
:meth:`LoopStore.load_or_init` で :class:`LoopState` を復元し、
``run_loop(initial_state=...)`` で中断地点からループを継続できることを実証する。中核の
主張は「途中で落として再開した結果が、一度も中断しなかった通し実行と一致する」こと
(永続化 SoT が step-for-step で一致し、最終集計 / stop_reason も一致する)。

resume は **状態ベースの停止条件** (GoalMet) と組み合わせて意味を持つ: プロセスを
またぐと act/verify フックは作り直されるが、その内部のコール回数カウンタは復元され
ない。判定を (gather された) state から導くフックなら、新プロセスでも同じ判断を再現
できる -- ここではトークンコスト固定の act と GoalMet(state.iteration>=N) を使う。
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

# resume が一致を再現できる、状態ベースで決定的なループ構成。act はトークン固定、
# 終了は GoalMet(state ベース) なので、フックを作り直す resume でも判定が変わらない。
GOAL_AT = 6


def _fresh_run_args() -> dict:
    return dict(
        act=acting(tokens=10, observation="w"),
        verify=never_done,
        conditions=[GoalMet(lambda s: s.iteration >= GOAL_AT), MaxIterations(100)],
    )


def _step_projection(store: LoopStore, run_id: str) -> list[tuple]:
    """timestamp / elapsed を除いた、決定的に比較できる step 射影を返す。"""
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
    # (1) 通し実行 (中断なし) を基準にする。
    full_path = tmp_path / "full.db"
    full_result, _ = _run_with_db_resumable(full_path, "full")

    # (2) 中断: 3 step を永続化した直後に run_loop の外へ例外を投げて「クラッシュ」
    #     させる (record_result には到達しないので run は running のまま残る)。
    part_path = tmp_path / "part.db"

    class _Crash(RuntimeError):
        pass

    crash_db = DBProgressLog(part_path, "run")

    def crashing_observer(record, state):
        crash_db.on_step(record, state)  # ここで commit 済みになる
        if state.iteration == 3:  # 3 step 永続化後、4 step 目に入る前に落とす
            raise _Crash()

    with pytest.raises(_Crash):
        run_loop(
            on_step=crashing_observer,
            initial_state=crash_db.state,  # 新規 run なので空 = fresh start
            **_fresh_run_args(),
        )
    crash_db.close()

    # 中断時点では 3 step だけが SoT に残り、run は未終了 (running)。
    probe = LoopStore(connect(part_path))
    assert len(probe.read_steps("run")) == 3
    assert probe.get_run("run")["status"] == "running"
    assert probe.get_stop_reason("run") is None
    probe.conn.close()

    # (3) 再開: 別接続 (= 別プロセス相当) で開き直し、復元 state から継続する。
    resume_db = DBProgressLog(part_path, "run")
    assert resume_db.state.iteration == 3  # 永続化済み step から途中状態を復元
    assert resume_db.state.tokens_used == 30
    assert [r.iteration for r in resume_db.state.history] == [0, 1, 2]

    resumed_result = run_loop(
        on_step=resume_db.on_step,
        initial_state=resume_db.state,
        **_fresh_run_args(),
    )
    resume_db.record_result(resumed_result)
    resume_db.close()

    # --- 再開結果が通し実行と一致する ---
    assert resumed_result.iterations == full_result.iterations == GOAL_AT
    assert resumed_result.tokens_used == full_result.tokens_used == GOAL_AT * 10
    assert resumed_result.succeeded is full_result.succeeded is True
    assert resumed_result.stop.name == full_result.stop.name == "goal_met"

    # --- 永続化 SoT も step-for-step / 集計 / stop_reason まで一致する ---
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
    # 再開は復元 state から *続き* を回すだけで、既永続化 step を再実行しない。
    # よって step event は通し実行と同じ本数になる (replay でノイズが増えない)。
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
    # loop_begin は中断前の 1 件のみ (再開で再記録しない)、step event は通しと同数。
    begins = [e for e in resume_store.read_events("run") if e["kind"] == "loop_begin"]
    assert len(begins) == 1
    assert len(resumed_step_events) == len(full_step_events) == GOAL_AT


def test_resume_from_a_capped_then_extended_run(tmp_path):
    # GoalMet を使わない素の cap 構成でも、復元 state から継続して通しと一致する。
    # 1 回目は MaxIterations(2) で 2 step 永続化、再開時に cap を 5 へ広げて継続。
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
    # elapsed が DB へ persist -> reconstruct -> clock 復帰 を経ても Timeout を正しく
    # 駆動することを決定的に検証する (success 条件の「終了条件状態」のうち、DB 経由の
    # elapsed 復元パスを明示的に押さえる)。再開 leg は新プロセス相当に fresh ManualClock
    # (monotonic は再起動で 0 に戻る) を渡し、back-dating で総経過が継続することを確認。
    def args(clock):
        return dict(
            act=stepping_for(clock, seconds=2.0),
            verify=never_done,
            conditions=[Timeout(7.0)],
            time_fn=clock,
        )

    # 通し実行: step=2.0s, Timeout=7.0 -> guard が 0,2,4,6,8 を見て 8 で発火 (4 step)。
    full = DBProgressLog(tmp_path / "full.db", "full")
    full_result = run_loop(initial_state=full.state, on_step=full.on_step, **args(ManualClock()))
    full.record_result(full_result)
    full.close()

    # leg1: 1 step 永続化後にクラッシュ (elapsed=2.0 が run 集計へ確定)。
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

    # leg2: 別プロセス相当 = fresh ManualClock(0)。復元 elapsed から継続する。
    db2 = DBProgressLog(tmp_path / "part.db", "run")
    assert db2.state.elapsed == 2.0  # 1 step * 2.0s が DB から復元される
    resumed = run_loop(on_step=db2.on_step, initial_state=db2.state, **args(ManualClock()))
    db2.record_result(resumed)
    db2.close()

    assert resumed.stop.name == full_result.stop.name == "timeout"
    assert resumed.iterations == full_result.iterations == 4
    assert resumed.elapsed == full_result.elapsed == 8.0


def test_resume_at_cap_runs_zero_new_steps_via_db(tmp_path):
    # 最終 step を永続化した直後 (record_result 前) にクラッシュした run を再開すると、
    # 復元 seed が既に cap 到達済みなので新規 step を 1 つも回さず即終了する
    # (guard-before-step 契約が DB 復元 seed でも成り立つ)。
    path = tmp_path / "state.db"

    class _Crash(RuntimeError):
        pass

    db1 = DBProgressLog(path, "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.iteration == 3:  # cap=3 に到達した直後に落とす
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

    assert new_steps == []  # 既に cap 到達 -> 新規 step なし
    assert result.iterations == 3
    assert result.tokens_used == 30
    assert result.stop.name == "max_iterations"
    store = LoopStore(connect(path))
    assert len(store.read_steps("run")) == 3  # 永続化も 3 step のまま


def test_resume_roundtrips_history_observations_through_json(tmp_path):
    # 既知の限界を pin する: state.db から復元した history の observation は保存時の JSON を
    # round-trip した値になる (tuple -> list)。raw observation を直接キーにする条件は
    # この型ドリフトに注意が必要 (run_loop の initial_state docstring / README 参照)。
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("run")
    store.record_step("run", StepRecord(0, ("a", "b"), 0, False), LoopState(iteration=1))

    restored = store.load_or_init("run")
    assert restored.history[0].observation == ["a", "b"]  # tuple -> list
    assert isinstance(restored.history[0].observation, list)


def test_resume_noprogress_with_json_stable_key_matches_straight_through(tmp_path):
    # 限界の緩和策を実証: observation を直接キーにすると tuple->list ドリフトで再開が
    # 壊れる (list は unhashable) が、JSON 安定な signature へ射影する key を NoProgress に
    # 渡せば、tuple observation でも再開が通し実行 (no_progress) と一致する。
    def _key(record):
        # tuple も list も同じ JSON 配列になるので、再開境界の型ドリフトを吸収する。
        return json.dumps(record.observation, sort_keys=True, default=repr)

    def args():
        return dict(
            act=acting(tokens=0, observation=("noop", 1)),
            verify=never_done,
            conditions=[NoProgress(window=3, repeat=3, key=_key), MaxIterations(100)],
        )

    # 通し実行: 同一 observation の反復 -> iteration 3 で no_progress 発火。
    full = DBProgressLog(tmp_path / "full.db", "full")
    full_result = run_loop(initial_state=full.state, on_step=full.on_step, **args())
    full.record_result(full_result)
    full.close()
    assert full_result.stop.name == "no_progress"
    assert full_result.iterations == 3

    # 2 step 永続化後にクラッシュ -> 復元して継続。key が型ドリフトを吸収し一致する。
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
    # verify フックで goal 達成した最終 step が永続化された直後 (record_result 前) に
    # クラッシュした run を再開すると、復元 state.goal_met=True を尊重し、新規 step を
    # 1 つも回さず自然終了 (status=goal_met) を再現する。これがないと完了済み run の
    # 再開が余計な act を回し、通し実行と結果が乖離する (Codex review P2)。
    path = tmp_path / "state.db"

    class _Crash(RuntimeError):
        pass

    # leg1 の verify: 3 回目で goal 達成 (resume では再評価されない)。
    calls = {"n": 0}

    def verify_done_at_3(_outcome):
        calls["n"] += 1
        met = calls["n"] >= 3
        return VerifyOutcome(goal_met=met, detail="done" if met else "")

    db1 = DBProgressLog(path, "run")

    def crashing_observer(record, state):
        db1.on_step(record, state)
        if state.goal_met:  # goal 達成 step を永続化した直後に落とす
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

    assert new_steps == []  # 完了済み -> 新規 step なし
    assert result.status == "goal_met"
    assert result.goal_met is True
    assert result.stop is None
    assert result.iterations == 3
    assert result.tokens_used == 15


def _run_with_db_resumable(path, run_id):
    """通し実行 (中断なし) を resume と同じ配線 (initial_state=db.state) で回す。"""
    db = DBProgressLog(path, run_id)
    result = run_loop(
        initial_state=db.state, on_step=db.on_step, **_fresh_run_args()
    )
    db.record_result(result)
    db.close()
    return result, db
