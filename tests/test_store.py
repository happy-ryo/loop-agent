"""ループ状態 SoT (state.db) の検証: transaction / クラッシュ耐性 / スキーマ独立性.

report.md S3.4 / S4.6 / S5 Phase 2 の「state.db SoT」の最小実装 (Issue #11) を対象に、
(a) 各反復が atomic に永続化され、(b) トランザクションがクラッシュ耐性を持ち
(commit 前のプロセス終了で半端な行が残らない)、(c) スキーマが org 本体から独立した
最小スキーマである、ことを実証する。DBProgressLog が JSONL の ProgressLog と同じ
観測フックの drop-in であることも併せて確認する。
"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from claude_loop import (
    DBProgressLog,
    LoopStore,
    MaxIterations,
    LoopState,
    StepRecord,
    VerifyOutcome,
    connect,
    run_loop,
)
from claude_loop.store import (
    EVENT_BEGIN,
    EVENT_END,
    EVENT_STEP,
    SCHEMA_VERSION,
)
from conftest import acting, done_after, never_done


def _run_with_db(conn, run_id, *, act, verify, conditions, on_step=None):
    """run_loop を DBProgressLog に配線し、終了状態まで記録して結果を返す。"""
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


# -- スキーマ独立性 (org 本体非依存の最小スキーマ) ----------------------------


def test_schema_has_only_the_four_minimal_loop_tables(tmp_path):
    conn = connect(tmp_path / "state.db")
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    # sqlite_sequence は AUTOINCREMENT の副産物なので除外して比較する。
    names.discard("sqlite_sequence")
    assert names == {"run", "step", "event", "stop_reason"}


def test_schema_carries_no_claude_org_tables(tmp_path):
    # org 本体のスキーマ (projects / workstreams / worker_dirs / runs(複数形) /
    # org_sessions 等) が紛れ込んでいないこと = 疎結合の担保。
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
    # claude_loop.store / connect が claude-org の tools.state_db を一切 import
    # しないこと (import するとパッケージとして org に密結合する)。
    connect(tmp_path / "state.db")
    assert not any("tools.state_db" in m for m in sys.modules)


def test_connect_sets_schema_version(tmp_path):
    conn = connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_connect_is_idempotent_on_existing_db(tmp_path):
    # 2 回開いても IF NOT EXISTS でスキーマ再適用がエラーにならず、既存データを保つ。
    path = tmp_path / "state.db"
    store = LoopStore(connect(path))
    store.load_or_init("r1")
    store.conn.close()

    conn2 = connect(path)
    assert conn2.execute(
        "SELECT run_id FROM run WHERE run_id = 'r1'"
    ).fetchone() is not None


# -- load_or_init (run ライフサイクル / resume 土台) --------------------------


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
    store.load_or_init("r1")  # 2 回目は既存 run を返すだけ
    begins = [e for e in store.read_events("r1") if e["kind"] == EVENT_BEGIN]
    assert len(begins) == 1


def test_load_or_init_reconstructs_state_from_persisted_steps(tmp_path):
    # resume (#14) の土台: 既存 run を load すると、永続化済み step から LoopState が
    # 復元される (history / iteration / tokens_used / goal_met)。
    path = tmp_path / "state.db"
    _run_with_db(
        connect(path),
        "r1",
        act=acting(tokens=10, observation="work"),
        verify=done_after(3),
        conditions=[MaxIterations(10)],
    )

    # 別接続で開き直して復元 (= プロセスをまたいだ resume を模す)。
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


# -- per-step 永続化 (atomic) -------------------------------------------------


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
    # begin が 1 件、step が反復数ぶん、end が 1 件、この順で並ぶ。
    assert kinds == [EVENT_BEGIN, EVENT_STEP, EVENT_STEP, EVENT_STEP, EVENT_END]


def test_records_are_durable_after_each_step_not_only_at_the_end(tmp_path):
    # Nth on_step の時点で、別接続から読むと既に N 件の step が見える
    # (= 反復ごとに commit されている。最後に一括ダンプではない)。
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


def test_record_step_is_idempotent_on_run_and_iteration(tmp_path):
    # 同一反復の再永続化 (resume #14) は重複行ではなく上書きになる。
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")
    rec = StepRecord(iteration=0, observation="a", tokens=5, goal_met=False)
    st = LoopState(iteration=1, tokens_used=5)
    store.record_step("r1", rec, st)
    rec2 = StepRecord(iteration=0, observation="b", tokens=7, goal_met=True)
    store.record_step("r1", rec2, st)

    steps = store.read_steps("r1")
    assert len(steps) == 1
    assert steps[0]["observation"] == "b"
    assert steps[0]["tokens"] == 7
    assert steps[0]["goal_met"] is True
    # 再永続化では loop_step event を重ねない (journal が step SoT と 1:1)。
    step_events = [e for e in store.read_events("r1") if e["kind"] == EVENT_STEP]
    assert len(step_events) == 1


# -- 終了状態の確定 -----------------------------------------------------------


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
    assert stop["name"] is None  # goal 達成は発火条件なし
    assert stop["reason"] == "goal met"


# -- transaction の atomicity / クラッシュ耐性 -------------------------------


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

    # 例外で巻き戻され、半端な step 行は残らない。
    assert store.read_steps("r1") == []


def test_record_step_is_all_or_nothing_when_event_insert_fails(tmp_path, monkeypatch):
    # record_step は step 行 + 集計 + event を 1 トランザクションに束ねる。
    # event 追記で失敗したら step 行も巻き戻る (部分永続化しない)。
    store = LoopStore(connect(tmp_path / "state.db"))
    store.load_or_init("r1")

    def boom(*_a, **_k):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(store, "_append_event", boom)
    rec = StepRecord(iteration=0, observation="x", tokens=5, goal_met=False)
    with pytest.raises(RuntimeError):
        store.record_step("r1", rec, LoopState(iteration=1, tokens_used=5))

    assert store.read_steps("r1") == []
    assert store.get_run("r1")["iterations"] == 0  # 集計も進んでいない


def test_composed_transaction_persists_multiple_steps_atomically(tmp_path):
    # 呼び出し側の transaction() で複数 step を束ねられる (内側 record_step は外側に
    # 参加する)。途中の例外で束ね全体が巻き戻る。
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

    assert store.read_steps("r1") == []  # どちらの step も確定していない
    assert [e["kind"] for e in store.read_events("r1")] == [EVENT_BEGIN]


def test_composed_transaction_commits_multiple_steps_atomically(tmp_path):
    # join 分岐の commit 側 (在 transaction True -> 最外の transaction() が COMMIT) を
    # 正常系で検証する: 外側 transaction() で 2 つの record_step を束ね、正常終了後に
    # 別接続から両 step + 両 loop_step event が一括で見えること。
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
    # 回帰: 非有限 float (NaN/Infinity) を含む observation でも json_valid CHECK に
    # 弾かれず永続化される (repr 文字列化)。1 つの変な値が step 永続化全体を壊さない。
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
    assert stored["ok"] == 1.5  # 有限 float はそのまま


def test_committed_steps_survive_a_crash_before_the_next_commit(tmp_path):
    # クラッシュ耐性: commit 済みの反復は別プロセス (別接続) から読める。続く
    # 反復を commit する前にプロセスが死んでも (= open トランザクションを commit
    # せず接続切断)、commit 済みの行は失われず、未 commit の行は現れない。
    path = tmp_path / "state.db"
    store = LoopStore(connect(path))
    store.load_or_init("r1")
    store.record_step(
        "r1", StepRecord(0, "done", 10, False), LoopState(iteration=1, tokens_used=10)
    )  # ここまで commit 済み

    # 次の反復を書きかけのまま「クラッシュ」: BEGIN + INSERT して commit せず close。
    store.conn.execute("BEGIN")
    store.conn.execute(
        "INSERT INTO step (run_id, iteration, tokens) VALUES ('r1', 1, 77)"
    )
    store.conn.close()  # commit 前にプロセス終了相当

    # 開き直すと commit 済みの 1 件だけが残る。
    reopened = LoopStore(connect(path))
    steps = reopened.read_steps("r1")
    assert len(steps) == 1
    assert steps[0]["iteration"] == 0
    assert steps[0]["observation"] == "done"


def test_state_db_persists_across_independent_connections(tmp_path):
    # SoT がプロセス (接続) をまたいで残る最小の証明。
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


# -- 複数 run の隔離 / observation の堅牢性 -----------------------------------


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
        return VerifyOutcome(goal_met=True, detail="収束しました")

    _run_with_db(
        connect(path), "r1", act=acting(tokens=0), verify=verify,
        conditions=[MaxIterations(5)],
    )
    store = LoopStore(connect(path))
    assert store.read_steps("r1")[0]["detail"] == "収束しました"


def test_foreign_key_cascade_removes_child_rows_with_the_run(tmp_path):
    # run を削除すると step / event / stop_reason が CASCADE で消える
    # (foreign_keys=ON + ON DELETE CASCADE の担保)。
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


# -- ProgressLog 互換 (drop-in) ----------------------------------------------


def test_dbprogresslog_owns_path_connection_and_closes_it(tmp_path):
    path = tmp_path / "state.db"
    with DBProgressLog(path, "r1") as db:
        assert db._owns_conn is True
        store = LoopStore(connect(path))
        assert store.get_run("r1") is not None
    # close 後は接続が使えない (所有接続を閉じた)。
    with pytest.raises(sqlite3.ProgrammingError):
        db.conn.execute("SELECT 1")


def test_dbprogresslog_borrows_connection_and_keeps_it_open(tmp_path):
    conn = connect(tmp_path / "state.db")
    db = DBProgressLog(conn, "r1")
    assert db._owns_conn is False
    db.close()  # 借用接続は閉じない
    assert conn.execute("SELECT 1").fetchone()[0] == 1
