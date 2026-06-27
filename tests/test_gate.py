"""限定人間ゲート (Issue #15) の検証: 不可逆操作のみ発火・4 種決定・pause/resume 保持.

report.md S4.5 / R6 / S5 Phase2 成功条件 c 「不可逆操作で人間ゲートが発火し
approve/reject が反映される」を対象に、

(a) 不可逆 action でのみ発火し reversible は素通りする (= 全 step ゲートにしない)、
(b) approve / edit / reject / respond の 4 決定が action 実行へ正しく写像される、
(c) 決定が state.db に永続化され **pause -> (別接続で) resolve -> resume** をまたいで
    保持される (人間に二度問わない)、
(d) store レベルの決定レジスタ (request/resolve/get/list) が冪等・検証付きである、
ことを実証する。
"""

from __future__ import annotations

import pytest

from claude_loop import (
    ActOutcome,
    DBProgressLog,
    DECISION_KINDS,
    Decision,
    HumanGate,
    LoopStore,
    MaxIterations,
    VerifyOutcome,
    connect,
    run_gated_loop,
    run_loop,
)
from claude_loop.store import EVENT_GATE
from conftest import never_done


# -- テスト用の最小ワールド --------------------------------------------------


def make_world(actions):
    """``gather`` が ``actions[iteration]`` を提案し、``act`` が実行を記録する世界。

    ゲートが skip した step では ``act`` は呼ばれず実行されない (executed に載らない)
    一方、iteration は進むので gather は次の action へ移る。
    """
    executed: list = []

    def gather(state):
        return actions[state.iteration]

    def act(action):
        executed.append(action)
        return ActOutcome(observation=action, tokens=0)

    return gather, act, executed


def is_deploy(action) -> bool:
    """``"deploy"`` を不可逆操作とみなす述語 (= 影響範囲大の代表)。"""
    return action == "deploy"


ACTIONS = ["work", "deploy", "work2"]
RUN_ID = "run-gate"


# -- (a) 発火範囲: 不可逆のみ ------------------------------------------------


def test_reversible_actions_never_trigger_the_gate(tmp_path):
    # 不可逆 action を含まない列なら、ゲートがあっても一切 interrupt せず素通りする。
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(["work", "work2"])
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    result = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(2)],
        gather=gather, gate=gate,
    )
    assert result.status == "stopped"
    assert executed == ["work", "work2"]
    assert store.list_pending_decisions(RUN_ID) == []


def test_irreversible_action_pauses_and_registers_pending(tmp_path):
    # 不可逆 action に未解決の決定しか無ければ、その手前で pause して登録する。
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    result = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # "work" だけ実行され、"deploy" の手前で停止している (副作用は出ていない)。
    assert result.paused is True
    assert result.status == "paused"
    assert result.stop is None
    assert result.succeeded is False
    assert executed == ["work"]
    assert result.pending["gate_key"] == "gate-0"
    assert result.pending["action"] == "deploy"
    assert "paused" in result.reason and "gate-0" in result.reason
    # pending が永続化され、journal に loop_gate(pending) が残る。
    pendings = store.list_pending_decisions(RUN_ID)
    assert [p["gate_key"] for p in pendings] == ["gate-0"]
    gate_events = [e for e in store.read_events(RUN_ID) if e["kind"] == EVENT_GATE]
    assert gate_events and gate_events[-1]["payload"]["status"] == "pending"


# -- (b)+(c) 4 決定が pause -> 別接続 resolve -> resume をまたいで反映される ---


def _resume_after(tmp_path, decision, payload=None):
    """run1 で pause -> 別接続で resolve -> run2 で resume する共通フロー。

    run2 で実行された action 列と、resume 後の結果・記録 step を返す。
    """
    db_path = tmp_path / "s.db"

    # --- run1: pause まで ---
    conn1 = connect(db_path)
    store1 = LoopStore(conn1)
    gather1, act1, executed1 = make_world(ACTIONS)
    gate1 = HumanGate(on=is_deploy, store=store1, run_id=RUN_ID)
    res1 = run_loop(
        act=act1, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather1, gate=gate1,
    )
    assert res1.paused and executed1 == ["work"]
    conn1.close()

    # --- 人間が別接続で決定を記録する (プロセスをまたぐ永続性の証明) ---
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-0", decision, payload)

    # --- run2: resume (新しい gate / 新しい executed) ---
    gather2, act2, executed2 = make_world(ACTIONS)
    gate2 = HumanGate(on=is_deploy, store=store2, run_id=RUN_ID)
    steps: list = []
    res2 = run_loop(
        act=act2, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather2, gate=gate2,
        on_step=lambda record, state: steps.append(record),
    )
    conn2.close()
    return executed2, res2, steps


def test_approve_executes_action_on_resume(tmp_path):
    executed, res, _ = _resume_after(tmp_path, "approve")
    # approve: "deploy" がそのまま実行され、最後まで進んで停止する (再 pause しない)。
    assert res.status == "stopped"
    assert executed == ["work", "deploy", "work2"]


def test_reject_skips_action_on_resume(tmp_path):
    executed, res, steps = _resume_after(tmp_path, "reject")
    # reject: "deploy" は実行されず、却下が 1 step として記録され継続する。
    assert res.status == "stopped"
    assert executed == ["work", "work2"]
    rejected = [s for s in steps if isinstance(s.observation, dict)
                and s.observation.get("gate") == "rejected"]
    assert len(rejected) == 1
    assert rejected[0].observation["action"] == "deploy"


def test_edit_executes_replacement_action_on_resume(tmp_path):
    executed, res, _ = _resume_after(tmp_path, "edit", payload="deploy-safe")
    # edit: 人間が差し替えた action が "deploy" の代わりに実行される。
    assert res.status == "stopped"
    assert executed == ["work", "deploy-safe", "work2"]


def test_respond_records_response_without_executing(tmp_path):
    executed, res, steps = _resume_after(tmp_path, "respond", payload="use staging")
    # respond: 実行せず人間の応答を記録して継続する。
    assert res.status == "stopped"
    assert executed == ["work", "work2"]
    responded = [s for s in steps if isinstance(s.observation, dict)
                 and s.observation.get("gate") == "respond"]
    assert len(responded) == 1
    assert responded[0].observation["response"] == "use staging"


def test_decision_is_not_re_asked_and_irreversible_runs_at_most_once(tmp_path):
    # 一度 resolve した決定は再 pause しない (人間に二度問わない)。かつ approve した
    # 不可逆 action は最初の resume で 1 回だけ実行され、以降の再生では executed として
    # skip される (at-most-once)。
    db_path = tmp_path / "s.db"
    store = LoopStore(connect(db_path))
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)  # run 行を確保
    store.request_decision(RUN_ID, "gate-0", "deploy")
    store.resolve_decision(RUN_ID, "gate-0", "approve")

    executions = []
    for _ in range(3):
        gather, act, executed = make_world(ACTIONS)
        gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
        res = run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate,
        )
        assert res.status == "stopped"  # 再 pause しない
        executions.append(executed)

    # 1 回目: deploy を実行。2 回目以降: executed として skip (deploy 不実行)。
    assert executions[0] == ["work", "deploy", "work2"]
    assert executions[1] == ["work", "work2"]
    assert executions[2] == ["work", "work2"]


# -- 全停止スイッチ ----------------------------------------------------------


def test_inactive_gate_proceeds_without_interrupting(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, active=False)
    result = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    assert result.status == "stopped"
    assert executed == ["work", "deploy", "work2"]
    assert store.list_pending_decisions(RUN_ID) == []


# -- 同期 resolver モード (pause せず inline 解決) ---------------------------


def test_synchronous_resolver_resolves_inline(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    seen = []

    def resolver(pending):
        seen.append(pending["gate_key"])
        return Decision("approve")

    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID, resolver=resolver
    )
    result = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # pause せず一気に完走し、resolver は不可逆 action でのみ呼ばれる。
    assert result.status == "stopped"
    assert executed == ["work", "deploy", "work2"]
    assert seen == ["gate-0"]
    assert store.get_decision(RUN_ID, "gate-0")["decision"] == "approve"


def test_run_gated_loop_helper_wires_the_gate(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    result = run_gated_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        on=is_deploy, store=store, run_id=RUN_ID, gather=gather,
        resolver=lambda pending: Decision("reject"),
    )
    assert result.status == "stopped"
    assert executed == ["work", "work2"]  # "deploy" は reject で実行されない


# -- store レベル: 決定レジスタの冪等性・検証 --------------------------------


def test_request_decision_is_idempotent(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    first = store.request_decision(RUN_ID, "g1", {"do": "x"})
    second = store.request_decision(RUN_ID, "g1", {"do": "DIFFERENT"})
    # 2 回目は既存 pending をそのまま返し、action を上書きしない。
    assert first["id"] == second["id"]
    assert second["action"] == {"do": "x"}
    # loop_gate(pending) は 1 件だけ (重複登録でイベントを増やさない)。
    pending_events = [
        e for e in store.read_events(RUN_ID)
        if e["kind"] == EVENT_GATE and e["payload"]["status"] == "pending"
    ]
    assert len(pending_events) == 1


def test_resolve_decision_roundtrip_and_payload(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")
    resolved = store.resolve_decision(RUN_ID, "g1", "edit", payload={"safe": True})
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "edit"
    assert resolved["payload"] == {"safe": True}
    assert resolved["resolved_at"] is not None
    assert store.list_pending_decisions(RUN_ID) == []  # もう pending ではない


def test_resolve_unknown_gate_key_raises(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    with pytest.raises(ValueError, match="no pending decision"):
        store.resolve_decision(RUN_ID, "missing", "approve")


def test_double_resolve_is_rejected(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")
    store.resolve_decision(RUN_ID, "g1", "approve")
    with pytest.raises(ValueError, match="already resolved"):
        store.resolve_decision(RUN_ID, "g1", "reject")


def test_resolve_unknown_decision_kind_raises(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")
    with pytest.raises(ValueError, match="unknown decision"):
        store.resolve_decision(RUN_ID, "g1", "bogus")


def test_decision_dataclass_rejects_bad_kind():
    with pytest.raises(ValueError, match="unknown decision"):
        Decision("bogus")
    # 正規の 4 種はすべて構築可能。
    for kind in DECISION_KINDS:
        assert Decision(kind).kind == kind


# -- (c) 複数ゲート: 不可逆 action は resume をまたいで exactly-once -----------


def is_deploy_prefix(action) -> bool:
    return isinstance(action, str) and action.startswith("deploy")


def test_multi_gate_resume_executes_each_irreversible_action_once(tmp_path):
    # 2 つの不可逆 action を含む run。各 resume は iteration 0 から再生されるが、
    # approve 済みで実行した不可逆 action は executed として skip され二度実行されない
    # (= ゲートの中核保証。二重 deploy を防ぐ)。
    db_path = tmp_path / "s.db"
    actions = ["deploy1", "work", "deploy2"]

    def run_once(store):
        gather, act, executed = make_world(actions)
        gate = HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
        res = run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate,
        )
        return res, executed

    # run1: deploy1 (gate-0) の手前で pause。
    conn1 = connect(db_path)
    res1, ex1 = run_once(LoopStore(conn1))
    assert res1.paused and res1.pending["gate_key"] == "gate-0" and ex1 == []
    conn1.close()

    # gate-0 approve (別接続) → run2: deploy1 を実行し、deploy2 (gate-1) で pause。
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-0", "approve")
    res2, ex2 = run_once(store2)
    assert res2.paused and res2.pending["gate_key"] == "gate-1"
    assert ex2 == ["deploy1", "work"]  # deploy1 はここで 1 回だけ実行
    conn2.close()

    # gate-1 approve → run3: iteration 0 から再生するが deploy1 は executed で skip。
    conn3 = connect(db_path)
    store3 = LoopStore(conn3)
    store3.resolve_decision(RUN_ID, "gate-1", "approve")
    res3, ex3 = run_once(store3)
    assert res3.status == "stopped"
    # deploy1 は再実行されず、work(再生) と deploy2 だけが実行される。
    assert ex3 == ["work", "deploy2"]
    assert "deploy1" not in ex3
    # 結果: deploy1 / deploy2 ともプロセス全体で 1 回ずつのみ実行された。
    assert store3.get_decision(RUN_ID, "gate-0")["status"] == "executed"
    assert store3.get_decision(RUN_ID, "gate-1")["status"] == "executed"
    conn3.close()


def test_pending_re_asks_again_on_resume_without_resolution(tmp_path):
    # 登録済みだが未 resolve のまま resume すると、再び同じ gate_key で pause する
    # (pending を二重登録せず、loop_gate(pending) も 1 件のまま)。
    db_path = tmp_path / "s.db"

    conn1 = connect(db_path)
    store1 = LoopStore(conn1)
    gather1, act1, _ = make_world(ACTIONS)
    res1 = run_loop(
        act=act1, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather1, gate=HumanGate(on=is_deploy, store=store1, run_id=RUN_ID),
    )
    assert res1.paused
    conn1.close()

    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    gather2, act2, executed2 = make_world(ACTIONS)
    res2 = run_loop(
        act=act2, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather2, gate=HumanGate(on=is_deploy, store=store2, run_id=RUN_ID),
    )
    assert res2.paused and res2.pending["gate_key"] == "gate-0"
    assert executed2 == ["work"]
    assert [p["gate_key"] for p in store2.list_pending_decisions(RUN_ID)] == ["gate-0"]
    pending_events = [
        e for e in store2.read_events(RUN_ID)
        if e["kind"] == EVENT_GATE and e["payload"].get("status") == "pending"
    ]
    assert len(pending_events) == 1
    conn2.close()


def test_respond_response_reaches_next_gather(tmp_path):
    # respond で記録した応答を、次の gather が state.history[-1] 経由で取り込めること。
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-0", "deploy")
    store.resolve_decision(RUN_ID, "gate-0", "respond", payload="use staging")

    seen_followup = []

    def gather(state):
        if state.history:
            last = state.history[-1].observation
            if isinstance(last, dict) and last.get("gate") == "respond":
                seen_followup.append(last["response"])
                return f"follow-up:{last['response']}"
        return ACTIONS[state.iteration]

    def act(action):
        return ActOutcome(observation=action, tokens=0)

    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # respond を skip した直後の iteration で、gather が応答を読めている。
    assert seen_followup == ["use staging"]


# -- DBProgressLog 統合: pause した結果を record_result に渡してもクラッシュしない ---


def test_db_progress_log_record_result_handles_paused(tmp_path):
    conn = connect(tmp_path / "s.db")
    gather, act, _ = make_world(ACTIONS)
    with DBProgressLog(conn, RUN_ID) as db:
        gate = HumanGate(on=is_deploy, store=db.store, run_id=RUN_ID)
        result = run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate, on_step=db.on_step,
        )
        assert result.paused
        db.record_result(result)  # CHECK 制約でクラッシュしてはならない
    # pause は終端でない: run は running のまま、stop_reason も書かれない。
    store = LoopStore(connect(tmp_path / "s.db"))
    assert store.get_run(RUN_ID)["status"] == "running"
    assert store.get_stop_reason(RUN_ID) is None
    paused_events = [
        e for e in store.read_events(RUN_ID)
        if e["kind"] == EVENT_GATE and e["payload"].get("status") == "paused"
    ]
    assert len(paused_events) == 1


# -- 防御ガード / 周辺 API --------------------------------------------------


def test_resume_with_diverged_action_is_rejected(tmp_path):
    # 提案列が resume 間でずれ、別の不可逆 action に同じ gate_key が割り当たると、
    # 記録済み action と一致しないため loud に弾く (誤適用を silent に許さない)。
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-0", "deploy-A")
    store.resolve_decision(RUN_ID, "gate-0", "approve")

    # 同じ gate-0 に別 action "deploy-B" が来る世界で再開する。
    gather, act, _ = make_world(["deploy-B"])
    gate = HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    with pytest.raises(ValueError, match="does not match"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(1)],
            gather=gather, gate=gate,
        )


def test_resolver_must_return_a_decision(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(ACTIONS)
    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID,
        resolver=lambda pending: "approve",  # Decision ではなく素の文字列
    )
    with pytest.raises(TypeError, match="must return a Decision"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate,
        )


def test_get_decision_unknown_returns_none(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    assert store.get_decision(RUN_ID, "missing") is None


def test_mark_executed_requires_resolved(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")
    with pytest.raises(ValueError, match="cannot mark unresolved"):
        store.mark_executed(RUN_ID, "g1")
    store.resolve_decision(RUN_ID, "g1", "approve")
    once = store.mark_executed(RUN_ID, "g1")
    assert once["status"] == "executed" and once["executed_at"] is not None
    # 冪等: 二度目の mark_executed は no-op で同じ行を返す。
    twice = store.mark_executed(RUN_ID, "g1")
    assert twice["status"] == "executed"
