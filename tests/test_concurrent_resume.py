"""複数プロセス同時 resume の協調 (Issue #21, Phase3) の検証.

report.md S5 Phase3 / Issue #21 の成功条件「並行 resume で不可逆 action exactly-once
かつ順序整合」を、in-progress リース (pending -> resolved -> executing -> executed の
多段化) で実証する:

(a) store レベル: ``acquire_lease`` が single-winner で ``resolved -> executing`` を取得し、
    敗者は WAIT を受ける。``complete_execution`` はリース保持者だけが executed を確定する。
    勝者クラッシュ時はリース失効で別プロセスが取り直す (``took_over``)。
(b) gate レベル: 敗者は executing を見て ``executed`` まで pause する (順序整合)。勝者が
    完了すれば敗者は skip する。失効リースは別プロセスが取り直して実行を完遂する。
(c) end-to-end: 並行プロセス (スレッド + 独立接続) を模擬し、不可逆 action が
    プロセス全体で 1 回だけ実行され、敗者が完了前に後続へ進まないことを示す。
(d) 既存 v1 DB は executing/lease 列へ非破壊に migration される。
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
    """``gather`` が ``actions[iteration]`` を提案し ``act`` が実行を記録する世界。"""
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
    """run を作り、不可逆 action を 1 件 resolve した状態の DB を用意する。"""
    store = LoopStore(connect(db_path))
    store.load_or_init(RUN)
    store.request_decision(RUN, gate_key, action)
    store.resolve_decision(RUN, gate_key, decision)
    return store


# -- (a) store レベル: リースの single-winner / 完了 / 失効取り直し -----------


def test_acquire_lease_is_single_winner_across_connections(tmp_path):
    # 別接続 (並行 resume 模擬) からの取得でも resolved->executing に成功するのは 1 者。
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))

    ra = store_a.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=30)
    rb = store_b.acquire_lease(RUN, "gate-0", "B", now=0.0, ttl=30)
    assert ra["outcome"] == LEASE_ACQUIRED and ra["took_over"] is False
    # 敗者は有効リース保持者 (A) を見て WAIT。
    assert rb["outcome"] == LEASE_WAIT and rb["owner"] == "A"
    # status は executing でリース情報が載る。
    row = store_a.get_decision(RUN, "gate-0")
    assert row["status"] == "executing" and row["lease_owner"] == "A"
    assert row["lease_expires_at"] == 30.0

    # 勝者が完了 -> executed。敗者の再取得は EXECUTED (skip)、敗者 complete は False。
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
    # 別 owner は完了確定できない (リース保持者ではない)。
    assert store.complete_execution(RUN, "gate-0", "B") is False
    assert store.get_decision(RUN, "gate-0")["status"] == "executing"
    assert store.complete_execution(RUN, "gate-0", "A") is True


def test_lease_reentrant_same_owner_extends_expiry(tmp_path):
    db_path = tmp_path / "s.db"
    store = _seed_resolved(db_path)
    store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)  # expires 10
    r2 = store.acquire_lease(RUN, "gate-0", "A", now=5.0, ttl=10)  # 再入 -> expires 15
    assert r2["outcome"] == LEASE_ACQUIRED and r2["took_over"] is False
    assert store.get_decision(RUN, "gate-0")["lease_expires_at"] == 15.0


def test_expired_lease_is_taken_over_after_winner_crash(tmp_path):
    # 勝者がリースを取得後にクラッシュ (complete しない) -> 失効後に別プロセスが取り直す。
    db_path = tmp_path / "s.db"
    store_a = _seed_resolved(db_path)
    store_b = LoopStore(connect(db_path))
    store_a.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)
    # 失効前: B は待たされる。
    assert store_b.acquire_lease(RUN, "gate-0", "B", now=5.0, ttl=10)["outcome"] == (
        LEASE_WAIT
    )
    # 失効後 (now > expires=10): B が取り直す (took_over)。
    taken = store_b.acquire_lease(RUN, "gate-0", "B", now=20.0, ttl=10)
    assert taken["outcome"] == LEASE_ACQUIRED and taken["took_over"] is True
    # 旧勝者 A の遅れた完了は no-op (リースを失っている)。二重 executed を防ぐ。
    assert store_a.complete_execution(RUN, "gate-0", "A") is False
    assert store_b.complete_execution(RUN, "gate-0", "B") is True
    # 取り直しは loop_gate(executing, took_over=True) を journal に残す。
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
    # reject/respond は実行系でないのでリースを張れない。
    store.request_decision(RUN, "gr", "deploy")
    store.resolve_decision(RUN, "gr", "reject")
    with pytest.raises(ValueError, match="not executable"):
        store.acquire_lease(RUN, "gr", "o", now=0.0, ttl=1)


# -- (b) gate レベル: 敗者 pause / 勝者完了後 skip / 失効取り直し -------------


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

    # A が deploy@0 のリースを取得 -> proceed (まだ complete していない)。
    review_a = gate_a.review("deploy", state)
    assert review_a.disposition == GATE_PROCEED
    assert review_a.on_complete is not None

    # A 実行中に B が同じゲートを審査 -> 順序整合のため pause (executed まで待つ)。
    review_b = gate_b.review("deploy", state)
    assert review_b.disposition == GATE_PAUSE
    assert review_b.pending["status"] == "executing"
    assert review_b.pending["gate_key"] == "gate-0"

    # A が完了確定 -> executed。
    review_a.on_complete()
    assert store_a.get_decision(RUN, "gate-0")["status"] == "executed"

    # B が再審査 -> 既実行なので skip (二重実行しない)。
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

    # A が取得 -> proceed。だが complete せずクラッシュしたとする。
    assert gate_a.review("deploy", state).disposition == GATE_PROCEED

    # 失効前は B は待たされる。
    clock.now = 5.0
    assert gate_b.review("deploy", state).disposition == GATE_PAUSE

    # 失効後は B が取り直して実行する (proceed)。
    clock.now = 100.0
    review_b = gate_b.review("deploy", state)
    assert review_b.disposition == GATE_PROCEED
    review_b.on_complete()
    assert store_b.get_decision(RUN, "gate-0")["status"] == "executed"


def test_winner_crash_recovery_records_step_via_loop(tmp_path):
    # 勝者クラッシュ -> 失効 -> 別プロセスが full loop で取り直し、step が欠落しない。
    db_path = tmp_path / "s.db"
    seed = _seed_resolved(db_path)
    clock = ManualClock(0.0)

    # 勝者 A: リースを取得 (proceed) するが act/on_complete を呼ばず "クラッシュ"。
    gate_a = HumanGate(
        on=is_deploy, store=seed, run_id=RUN, owner="A", now_fn=clock, lease_ttl=5
    )
    assert gate_a.review("deploy", LoopState()).disposition == GATE_PROCEED
    assert seed.get_decision(RUN, "gate-0")["status"] == "executing"

    # リース失効後、敗者 B が full loop で resume して取り直す。
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
    assert executed == ["deploy", "work2"]  # B が deploy を取り直して実行
    assert db_b.store.get_decision(RUN, "gate-0")["status"] == "executed"
    # deploy の step 行が永続化されている (勝者クラッシュでも step が欠落しない)。
    steps = db_b.store.read_steps(RUN)
    assert any(s["observation"] == "deploy" for s in steps)
    conn_b.close()


# -- (c) end-to-end: 並行プロセス模擬で exactly-once + 順序整合 ----------------


def test_concurrent_resume_runs_irreversible_action_exactly_once(tmp_path):
    # 2 スレッド + 独立接続で同一 run_id を *同時に* resume する。各ラウンドで不可逆 action
    # (deploy) はプロセス全体で 1 回だけ実行され、敗者は完了前に後続へ進まない (順序整合)。
    # barrier で両者をゲート審査時点で衝突させ、複数ラウンドで競合を繰り返し叩く。
    actions = ["deploy", "work2"]
    # 各スレッドの executed として許される形:
    #   ("deploy","work2") 勝者 / () WAIT で pause した敗者 / ("work2",) 既実行 skip の敗者。
    # いずれも「deploy より前に work2 を実行しない」順序整合と「deploy は高々 1 回」を満たす。
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
            # setup 失敗で barrier 手前で抜けると相手が無限待ちするため、worker 全体を
            # try で囲み、barrier には timeout を付けて fail-fast にする (deadlock 防止)。
            try:
                conn = connect(db_path)
                store = LoopStore(conn)
                gather, act, executed = make_world(actions)
                gate = HumanGate(on=is_deploy, store=store, run_id=run_id, owner=name)
                barrier.wait(timeout=10)  # 両スレッドをゲート審査直前で揃える。
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
            except BaseException as exc:  # noqa: BLE001 - テスト失敗を握り潰さず記録
                with lock:
                    errors[name] = exc
                try:
                    barrier.abort()  # 相手の barrier 待ちを即座に解く。
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
        # 各スレッドの実行列は許容形のいずれか (順序整合)。
        assert ex_a in allowed, (i, ex_a)
        assert ex_b in allowed, (i, ex_b)
        # deploy はプロセス全体でちょうど 1 回 (exactly-once)。
        assert (ex_a + ex_b).count("deploy") == 1, (i, ex_a, ex_b)
        # ちょうど 1 スレッドが勝者 (deploy を実行)。
        winners = [n for n in ("A", "B") if "deploy" in results[n][1]]
        assert len(winners) == 1, (i, ex_a, ex_b)
        # 最終的にゲートは executed (勝者が完了確定)。
        final = LoopStore(connect(db_path))
        assert final.get_decision(run_id, "gate-0")["status"] == "executed"
        final.conn.close()


# -- (d) 既存 v1 DB の非破壊 migration ----------------------------------------


# v1 (Issue #15) 時点の pending_decision DDL: executing status と lease 列が無い。
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

    # connect が migration を走らせる。
    store = LoopStore(connect(db_path))
    decision = store.get_decision(RUN, "gate-0")
    # 既存行は保存される。新リース列が追加される (default NULL)。
    assert decision["status"] == "resolved" and decision["action"] == "deploy"
    assert decision["lease_owner"] is None
    assert "lease_expires_at" in decision
    # executing が許可される (= CHECK が再構築された) ことをリース取得で確認。
    res = store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=10)
    assert res["outcome"] == LEASE_ACQUIRED
    assert store.get_decision(RUN, "gate-0")["status"] == "executing"
    assert store.complete_execution(RUN, "gate-0", "A") is True


def test_migration_recovers_from_leftover_temp_table(tmp_path):
    # 前回中断で一時テーブル pending_decision_mig が残っていても、migration はそれを落として
    # やり直せる (CREATE は IF NOT EXISTS でないため、放置すると connect が恒久失敗する)。
    db_path = tmp_path / "stale.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_SCHEMA)
    raw.execute("INSERT INTO run (run_id, status) VALUES (?, 'running')", (RUN,))
    raw.execute(
        "INSERT INTO pending_decision (run_id, gate_key, status, decision, action) "
        'VALUES (?, ?, ?, ?, ?)',
        (RUN, "gate-0", "resolved", "approve", '"deploy"'),
    )
    # 中断で取り残された一時テーブルを模擬。
    raw.execute("CREATE TABLE pending_decision_mig (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    # connect が落ちずに migration を完遂し、本来の決定が保たれる。
    store = LoopStore(connect(db_path))
    assert store.get_decision(RUN, "gate-0")["status"] == "resolved"
    assert store.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=5)["outcome"] == (
        LEASE_ACQUIRED
    )
    # 一時テーブルは残っていない。
    leftover = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name='pending_decision_mig'"
    ).fetchone()
    assert leftover is None


def test_migration_is_idempotent_on_already_v2_db(tmp_path):
    # 新スキーマで作った DB を再度開いても migration は no-op で、決定は壊れない。
    db_path = tmp_path / "v2.db"
    store = _seed_resolved(db_path)
    store.conn.close()
    reopened = LoopStore(connect(db_path))
    assert reopened.get_decision(RUN, "gate-0")["status"] == "resolved"
    assert reopened.acquire_lease(RUN, "gate-0", "A", now=0.0, ttl=5)["outcome"] == (
        LEASE_ACQUIRED
    )
