"""work-discovery 入力選定 (Issue #24) の検証: 計算層の決定性 + 配達層の人間ゲート.

report.md S3.5 / S4.6 / S5 Phase 3 成功条件 d 「完了 -> 次反復の接続が人間ゲート越しに
回る」を対象に、

(a) 計算層 :func:`triage` が read-only・決定的 (順不同同一入力 -> 同一出力)・依存解決
    (deps が全て done なら ready)・優先度/工数ランキング・blocked 理由付け・循環検出を行う、
(b) 配達層 :class:`WorkDiscovery` が triage を **propose-only** で人間ゲート (pending_decision)
    に載せ、自動では一切採択しない、
(c) approve / edit / reject / respond の 4 決定が採択候補へ正しく写像される、
(d) 決定が state.db に永続化され **pause -> (別接続で) resolve -> resume** をまたいで
    保持される (人間に二度問わない)、
(e) :func:`discover_next` が「完了 -> 次反復」を接続し、直前が paused なら提案しない、
(f) 完了したループ -> triage -> 人間 approve -> 採択候補を次ループ入力、の **full cycle** が
    人間ゲートを必ず挟んで回る (= 完全自動着手しない)、
ことを実証する。
"""

from __future__ import annotations

import pytest

from claude_loop import (
    ActOutcome,
    AdoptionResult,
    Candidate,
    LoopState,
    LoopStore,
    MaxIterations,
    Proposal,
    Triage,
    VerifyOutcome,
    WorkDiscovery,
    connect,
    discover_next,
    run_loop,
    triage,
)
from claude_loop.discovery import GATE_KEY_PREFIX
from claude_loop.loop import LoopResult
from claude_loop.store import EVENT_GATE


# -- 計算層 (triage): 決定的・read-only -------------------------------------


def test_ready_when_deps_done_else_blocked():
    """依存が全て done なら ready、1 つでも欠ければ blocked になる (依存解決)。"""
    cands = [
        Candidate(id="a"),  # 依存なし -> ready
        Candidate(id="b", depends_on=("a",)),  # a は done でない -> blocked
        Candidate(id="c", depends_on=("x",)),  # x は done -> ready
    ]
    result = triage(cands, done=("x",))
    ready_ids = [c.id for c in result.ready]
    blocked_ids = [b.candidate.id for b in result.blocked]
    assert ready_ids == ["a", "c"]  # priority 同値 -> id 昇順
    assert blocked_ids == ["b"]
    assert result.blocked[0].pending_deps == ("a",)  # a は既知候補待ち


def test_ranking_priority_then_effort_then_id():
    """ready は優先度降順 -> 工数昇順 -> id 昇順で決定的にランキングされる。"""
    cands = [
        Candidate(id="low", priority=1, effort=1),
        Candidate(id="hi_cheap", priority=5, effort=1),
        Candidate(id="hi_pricey", priority=5, effort=9),
        Candidate(id="hi_cheap_b", priority=5, effort=1),  # 同優先・同工数 -> id 昇順
    ]
    result = triage(cands)
    assert [c.id for c in result.ready] == [
        "hi_cheap",
        "hi_cheap_b",
        "hi_pricey",
        "low",
    ]
    assert result.recommended.id == "hi_cheap"


def test_triage_is_order_independent_deterministic():
    """順不同の同一入力は必ず同一の Triage を返す (決定的・read-only)。"""
    base = [
        Candidate(id="a", priority=3),
        Candidate(id="b", priority=3, depends_on=("a",)),
        Candidate(id="c", priority=7),
    ]
    forward = triage(base, done=())
    reversed_ = triage(list(reversed(base)), done=())
    assert forward == reversed_
    # 入力リストは変更されない (read-only)。
    assert [c.id for c in base] == ["a", "b", "c"]


def test_recommended_none_when_nothing_ready():
    """ready が空なら推奨は None (全 blocked の正常系)。"""
    result = triage([Candidate(id="b", depends_on=("missing",))])
    assert result.ready == ()
    assert result.recommended is None
    assert result.blocked[0].unknown_deps == ("missing",)


def test_done_candidates_excluded():
    """既に done の id を持つ候補は次反復対象から除外される。"""
    result = triage([Candidate(id="a"), Candidate(id="b")], done=("a",))
    assert [c.id for c in result.ready] == ["b"]
    assert result.blocked == ()


def test_duplicate_ids_rejected():
    """候補 id 重複は決定的出力を壊すので ValueError。"""
    with pytest.raises(ValueError, match="duplicate candidate id"):
        triage([Candidate(id="a"), Candidate(id="a")])


def test_cycle_detected_and_flagged():
    """依存循環に属する候補は blocked かつ in_cycle で注記される。"""
    cands = [
        Candidate(id="a", depends_on=("b",)),
        Candidate(id="b", depends_on=("a",)),
        Candidate(id="c"),  # 循環外 -> ready
    ]
    result = triage(cands)
    assert [c.id for c in result.ready] == ["c"]
    blocked = {b.candidate.id: b for b in result.blocked}
    assert blocked["a"].in_cycle is True
    assert blocked["b"].in_cycle is True
    assert "依存循環" in blocked["a"].reason


def test_self_dependency_is_cycle():
    """自己依存は自明な循環として検出される。"""
    result = triage([Candidate(id="a", depends_on=("a",))])
    assert result.blocked[0].in_cycle is True


def test_cycle_member_reachable_only_via_finished_node():
    """SCC 全メンバーを検出する (back-edge DFS が取りこぼす BLACK 経由メンバーも)。

    C1->C2, C2->{C3,C4}, C3->C1, C4->C3 は 1 つの SCC (全 4 ノード)。素朴な back-edge DFS は
    C3 を探索完了 (BLACK) してから C4->C3 を cross-edge と誤判定し C4 を取りこぼす。Tarjan SCC
    なら C4 も含め全員 in_cycle になる。
    """
    cands = [
        Candidate(id="C1", depends_on=("C2",)),
        Candidate(id="C2", depends_on=("C3", "C4")),
        Candidate(id="C3", depends_on=("C1",)),
        Candidate(id="C4", depends_on=("C3",)),
    ]
    import random

    for _ in range(8):  # 入力順に依存しない (決定的)。
        shuffled = list(cands)
        random.shuffle(shuffled)
        result = triage(shuffled)
        in_cycle = {b.candidate.id for b in result.blocked if b.in_cycle}
        assert in_cycle == {"C1", "C2", "C3", "C4"}
        assert result.ready == ()


def test_candidate_validation():
    """空 id / 負の effort は弾く。"""
    with pytest.raises(ValueError, match="non-empty string"):
        Candidate(id="")
    with pytest.raises(ValueError, match="effort must be >= 0"):
        Candidate(id="a", effort=-1)


# -- 配達層 (WorkDiscovery): propose-only / 人間ゲート -----------------------


def make_store():
    return LoopStore(connect(":memory:"))


def test_propose_is_propose_only_pending():
    """propose は提案を pending で登録するだけで自動採択しない (propose-only)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    prop = wd.propose([Candidate(id="a", priority=5), Candidate(id="b")], cycle=0)
    assert isinstance(prop, Proposal)
    assert prop.pending["status"] == "pending"
    assert prop.gate_key == f"{GATE_KEY_PREFIX}0"
    # 採択は何も起きていない。
    assert wd.adopted(0).status == "pending"
    assert wd.adopted(0).adopted is False
    # 提案は pending_decision レジスタに 1 件 pending で載っている。
    assert len(store.list_pending_decisions("run-1")) == 1


def test_approve_adopts_recommended():
    """approve は推奨候補を採択する。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a", priority=9), Candidate(id="b", priority=1)], cycle=0)
    result = wd.resolve(0, "approve")
    assert result.status == "resolved"
    assert result.adopted is True
    assert result.candidate.id == "a"  # 推奨 = 最高優先


def test_edit_adopts_chosen_ready_candidate():
    """edit は人間が指定した別の ready 候補を採択する。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a", priority=9), Candidate(id="b", priority=1)], cycle=0)
    result = wd.resolve(0, "edit", payload="b")
    assert result.decision == "edit"
    assert result.candidate.id == "b"


def test_edit_rejects_non_ready_selection():
    """edit で blocked / 未知の候補を選ぶと fail loud (依存不変条件を配達層で守る)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose(
        [Candidate(id="a"), Candidate(id="blk", depends_on=("missing",))], cycle=0
    )
    with pytest.raises(ValueError, match="not a ready candidate"):
        wd.resolve(0, "edit", payload="blk")
    with pytest.raises(ValueError, match="not a ready candidate"):
        wd.resolve(0, "edit", payload="nope")
    # 不正な edit では決定は永続化されていない (まだ pending)。
    assert wd.adopted(0).status == "pending"


def test_reject_adopts_nothing():
    """reject は何も採択しない (次反復を起こさない)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    result = wd.resolve(0, "reject")
    assert result.decision == "reject"
    assert result.adopted is False
    assert result.candidate is None


def test_respond_records_response_no_adoption():
    """respond は採択せず応答本文を記録する (次の triage 文脈に渡せる)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    result = wd.resolve(0, "respond", payload="優先度を見直して")
    assert result.adopted is False
    assert result.response == "優先度を見直して"


def test_propose_idempotent_per_cycle():
    """同一 cycle の再 propose は最初の提案・決定を壊さない (冪等)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    wd.resolve(0, "approve")
    # 候補集合を変えて同 cycle で再 propose しても、確定済み決定はそのまま。
    again = wd.propose([Candidate(id="z")], cycle=0)
    assert again.pending["status"] == "resolved"
    # 返す triage は **永続化された** 提案 (推奨 a) と一致する。recompute した z ではない
    # (Proposal.triage と pending/adopted の内部整合を保つ)。
    assert again.triage.recommended.id == "a"
    assert wd.adopted(0).candidate.id == "a"  # 最初の提案の推奨が採択されたまま


def test_adopted_absent_when_not_proposed():
    """提案されていない cycle の adopted は absent。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    assert wd.adopted(7).status == "absent"


def test_payload_carried_to_adopted_candidate():
    """候補 payload が採択候補に復元され、次ループ入力に渡せる (JSON round-trip)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose(
        [Candidate(id="a", summary="タスクA", payload={"task": "fix #1", "n": 3})],
        cycle=0,
    )
    cand = wd.resolve(0, "approve").candidate
    assert cand.payload == {"task": "fix #1", "n": 3}
    assert cand.summary == "タスクA"


# -- 永続化: pause -> 別接続 resolve -> resume をまたいで保持 ----------------


def test_decision_persists_across_connections(tmp_path):
    """propose (接続A) -> resolve (接続B) -> adopted (接続C) が同一決定を読む (永続)。"""
    db = tmp_path / "loop.db"
    # 接続A: 提案を登録して「中断」(propose-only で pending のまま)。
    wd_a = WorkDiscovery(LoopStore(connect(db)), "run-1")
    wd_a.propose([Candidate(id="a", priority=5), Candidate(id="b")], cycle=0)
    # 接続B: 人間が別プロセスで採否を記録。
    wd_b = WorkDiscovery(LoopStore(connect(db)), "run-1")
    wd_b.resolve(0, "edit", payload="b")
    # 接続C: 再開時に決定を読み直すと採択は保持されている。
    wd_c = WorkDiscovery(LoopStore(connect(db)), "run-1")
    adopted = wd_c.adopted(0)
    assert adopted.status == "resolved"
    assert adopted.candidate.id == "b"


def test_gate_events_recorded():
    """提案 (pending) と決定 (resolved) が journal に loop_gate として残る (監査)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    wd.resolve(0, "approve")
    gate_events = [
        e for e in store.read_events("run-1") if e["kind"] == EVENT_GATE
    ]
    statuses = [e["payload"]["status"] for e in gate_events]
    assert "pending" in statuses
    assert "resolved" in statuses
    # gate_key は discovery 名前空間。
    assert all(
        e["payload"]["gate_key"].startswith(GATE_KEY_PREFIX) for e in gate_events
    )


# -- discover_next: 完了 -> 次反復の接続 -------------------------------------


def test_discover_next_proposes_after_completion():
    """完了したループ結果を渡すと次候補を提案する (完了 -> 次反復)。"""
    store = make_store()
    result = run_loop(
        act=lambda _c: ActOutcome(tokens=1),
        verify=lambda _o: VerifyOutcome(goal_met=True),
        conditions=[MaxIterations(3)],
    )
    assert result.succeeded
    prop = discover_next(
        store=store,
        run_id="run-1",
        candidates=[Candidate(id="next", priority=1)],
        result=result,
        cycle=1,
    )
    assert prop is not None
    assert prop.triage.recommended.id == "next"
    assert prop.pending["status"] == "pending"  # propose-only


def test_discover_next_skips_when_paused():
    """直前が paused (人間ゲート中断中) なら提案しない (= 先に人間が解決すべき)。"""
    store = make_store()
    # 実物の LoopResult.paused プロパティを通す (status=="paused")。
    paused = LoopResult(
        status="paused",
        stop=None,
        state=LoopState(),
        pending={"gate_key": "gate-0"},
    )
    assert paused.paused is True
    prop = discover_next(
        store=store,
        run_id="run-1",
        candidates=[Candidate(id="next")],
        result=paused,
        cycle=1,
    )
    assert prop is None


# -- full cycle: 完了 -> triage -> 人間 approve -> 次ループ入力 --------------


def test_full_cycle_completion_to_next_iteration_through_human_gate(tmp_path):
    """成功条件 d: 完了 -> 次反復の接続が人間ゲートを必ず挟んで回る (自動着手しない)。

    1. ループ#1 を完了させる。
    2. discover_next で次候補を triage -> 提案 (propose-only / pending)。
    3. *人間* が approve を記録するまでは次反復が起きない (= 完全自動着手しない)。
    4. approve 後、採択候補の payload を入力にループ#2 を走らせる。
    """
    db = tmp_path / "loop.db"
    store = LoopStore(connect(db))

    # 1. ループ#1 完了。
    first = run_loop(
        act=lambda _c: ActOutcome(observation="done-1", tokens=1),
        verify=lambda _o: VerifyOutcome(goal_met=True),
        conditions=[MaxIterations(2)],
    )
    assert first.succeeded

    # 2. 完了 -> triage -> 提案 (人間ゲートに pending)。
    prop = discover_next(
        store=store,
        run_id="cycle",
        candidates=[
            Candidate(id="t1", priority=9, payload={"goal": "build feature X"}),
            Candidate(id="t2", priority=1, depends_on=("t1",)),  # t1 完了まで blocked
        ],
        result=first,
        cycle=1,
    )
    assert prop is not None
    assert prop.triage.recommended.id == "t1"
    assert [b.candidate.id for b in prop.triage.blocked] == ["t2"]

    # 3. 人間が決める前は採択ゼロ (= 自動では次反復に進まない)。
    wd = WorkDiscovery(store, "cycle")
    assert wd.adopted(1).adopted is False
    assert len(store.list_pending_decisions("cycle")) == 1

    # 人間が approve (= 人間ゲート通過)。
    adoption = wd.resolve(1, "approve")
    assert adoption.adopted is True
    chosen = adoption.candidate
    assert chosen.id == "t1"

    # 4. 採択候補の payload を入力にループ#2 を走らせる (次反復の接続)。
    seen_inputs: list = []

    def gather2(_state):
        return chosen.payload

    def act2(ctx):
        seen_inputs.append(ctx)
        return ActOutcome(observation=ctx, tokens=1)

    second = run_loop(
        act=act2,
        verify=lambda _o: VerifyOutcome(goal_met=True),
        conditions=[MaxIterations(2)],
        gather=gather2,
    )
    assert second.succeeded
    assert seen_inputs == [{"goal": "build feature X"}]

    # 採択は resume をまたいでも保持される (別接続で読み直しても t1)。
    assert WorkDiscovery(LoopStore(connect(db)), "cycle").adopted(1).candidate.id == "t1"


def test_iterative_discovery_unblocks_dependent_next_cycle():
    """完了の蓄積で blocked 候補が次 cycle で ready になる (反復入力選定ループ)。"""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    # cycle 1: t1 のみ ready (t2 は t1 待ち)。
    prop1 = wd.propose(
        [Candidate(id="t1"), Candidate(id="t2", depends_on=("t1",))], cycle=1
    )
    assert prop1.triage.recommended.id == "t1"
    wd.resolve(1, "approve")
    # cycle 2: t1 を done に積むと t2 が ready になる。
    prop2 = wd.propose(
        [Candidate(id="t2", depends_on=("t1",))], done=("t1",), cycle=2
    )
    assert prop2.triage.recommended.id == "t2"


def test_adoption_result_is_frozen_value():
    """AdoptionResult は不変の値オブジェクト (採択判定 .adopted を提供)。"""
    r = AdoptionResult(
        status="resolved",
        decision="reject",
        candidate=None,
        recommended=None,
    )
    assert r.adopted is False
    with pytest.raises(Exception):
        r.status = "x"  # frozen


def test_triage_value_equality():
    """Triage は値等価 (決定性テストの比較基盤)。"""
    a = triage([Candidate(id="a")])
    b = triage([Candidate(id="a")])
    assert a == b
    assert isinstance(a, Triage)
