"""multi-item 公平 scheduling (Issue #56) の検証.

検証の柱:

- 各 scheduling 戦略 (round_robin / fewest_attempts / fifo / priority / custom) の選択順。
- per-item 上限 (exhausted) と done 判定フックの独立性。
- attempt counter / 進捗の正規 API が state から導出され resume 安全であること。
- 故意に失敗し続ける item が他 item を starve させないこと (統合テスト)。
- triage との接続 (from_triage)。
"""

from __future__ import annotations

import pytest

from loop_agent import (
    DRAINED,
    ActOutcome,
    Candidate,
    MaxIterations,
    VerifyOutcome,
    WorkItem,
    WorkListDrained,
    WorkListGather,
    WorkListProgress,
    run_loop,
)
from loop_agent.discovery.work_list import ScheduleContext
from loop_agent.loop import GATE_PROCEED, GATE_SKIP, GateReview
from loop_agent.state import LoopState, StepRecord

from conftest import never_done


# -- テスト用ハーネス ---------------------------------------------------------


def _ctx_id(ctx) -> str:
    """build_ctx の出力 (既定の JSON dict / WorkItem / 素の id 文字列) から item id を取る。"""
    if isinstance(ctx, dict):
        return ctx["id"]
    if isinstance(ctx, WorkItem):
        return ctx.id
    return ctx


def scripted_act(dispatched: list[str], completes: dict[str, int]):
    """dispatch された item id を記録し、``completes`` 回目で done フラグを立てる ``act``。

    ``completes[id] == n`` なら id は **n 回目の dispatch** で完了する。``id`` が ``completes``
    に無ければ永久に未完。done シグナルは ``observation["done"]`` に焼くので、リプレイ
    (resume) でも安定 (act の内部カウンタは attribution に使わない)。
    """
    counts: dict[str, int] = {}

    def _act(ctx) -> ActOutcome:
        item_id = _ctx_id(ctx)
        counts[item_id] = counts.get(item_id, 0) + 1
        dispatched.append(item_id)
        need = completes.get(item_id)
        done = need is not None and counts[item_id] >= need
        return ActOutcome(observation={"item": item_id, "done": done}, tokens=1)

    return _act


def done_from_observation(_item: WorkItem, record: StepRecord) -> bool:
    """``scripted_act`` が焼いた done フラグを読む done 判定フック。

    gate SKIP 行など ``done`` キーを持たない observation には ``False`` (未完了扱い)。
    """
    obs = record.observation
    return bool(isinstance(obs, dict) and obs.get("done"))


def item_of_observation(record: StepRecord):
    """``scripted_act`` が焼いた実 item id を返す item_of (gate 合成用)。

    skip 行 (``{"skipped": True}`` で ``item`` 無し) には ``None`` (非実行) を返す。
    """
    obs = record.observation
    return obs.get("item") if isinstance(obs, dict) else None


def history_of(*ids_done: tuple[str, bool]) -> LoopState:
    """``(item_id, done)`` の並びから ``LoopState.history`` を組む (導出テスト用)。"""
    state = LoopState()
    for i, (item_id, done) in enumerate(ids_done):
        state.history.append(
            StepRecord(
                iteration=i,
                observation={"item": item_id, "done": done},
                tokens=1,
                goal_met=False,
            )
        )
    state.iteration = len(ids_done)
    return state


def drive(gatherer: WorkListGather, completes: dict[str, int], *, max_iters: int = 100):
    """drained または ``MaxIterations`` まで回し、(dispatch 順, LoopResult) を返す。"""
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, completes),
        verify=never_done,
        gather=gatherer,
        conditions=[WorkListDrained(gatherer), MaxIterations(max_iters)],
    )
    return dispatched, result


# -- WorkItem / 構築バリデーション -------------------------------------------


def test_workitem_rejects_empty_id():
    with pytest.raises(ValueError, match="non-empty"):
        WorkItem(id="")


def test_bare_strings_promote_to_workitems():
    g = WorkListGather(["a", "b"])
    assert [it.id for it in g.items] == ["a", "b"]
    assert all(isinstance(it, WorkItem) for it in g.items)


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        WorkListGather(["a", "a"])


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        WorkListGather(["a"], strategy="bogus")


def test_bad_max_attempts_rejected():
    with pytest.raises(ValueError, match=">= 1"):
        WorkListGather(["a"], max_attempts_per_item=0)


# -- scheduling 戦略の選択順 -------------------------------------------------


def test_fewest_attempts_interleaves_fairly():
    # どれも完了しない -> 試行回数最小から選ぶので厳密にラウンドロビンする。
    g = WorkListGather(["a", "b", "c"], strategy="fewest_attempts")
    dispatched, _ = drive(g, completes={}, max_iters=9)
    assert dispatched == ["a", "b", "c", "a", "b", "c", "a", "b", "c"]


def test_round_robin_rotates_positionally():
    g = WorkListGather(["a", "b", "c"], strategy="round_robin")
    dispatched, _ = drive(g, completes={}, max_iters=7)
    assert dispatched == ["a", "b", "c", "a", "b", "c", "a"]


def test_round_robin_skips_completed_and_keeps_rotating():
    # b が 1 回で完了したら、a,b,c,(b done),... 以降 b を飛ばして a<->c を巡回する。
    g = WorkListGather(
        ["a", "b", "c"], strategy="round_robin", done_when=done_from_observation
    )
    dispatched, _ = drive(g, completes={"b": 1}, max_iters=7)
    # a, b(done), c, a, c, a, c  -- b は完了後二度と出ない。
    assert dispatched == ["a", "b", "c", "a", "c", "a", "c"]
    assert "b" not in dispatched[2:]


def test_fifo_is_naive_head_selection():
    # fifo は「先頭の未完」を返す素朴戦略。完了しなければ先頭を回し続ける。
    g = WorkListGather(["a", "b", "c"], strategy="fifo")
    dispatched, _ = drive(g, completes={}, max_iters=4)
    assert dispatched == ["a", "a", "a", "a"]


def test_fifo_advances_as_items_complete():
    g = WorkListGather(["a", "b", "c"], strategy="fifo", done_when=done_from_observation)
    dispatched, _ = drive(g, completes={"a": 2, "b": 1, "c": 1}, max_iters=20)
    # a,a(done),b(done),c(done) -> drained。
    assert dispatched == ["a", "a", "b", "c"]


def test_priority_is_strict_highest_first():
    # priority は厳密に優先度降順: 最高優先度の item が done/exhausted になるまで独占する。
    items = [
        WorkItem(id="lo", priority=0),
        WorkItem(id="hi", priority=10),
        WorkItem(id="mid", priority=5),
    ]
    g = WorkListGather(items, strategy="priority")
    dispatched, _ = drive(g, completes={}, max_iters=4)
    assert dispatched == ["hi", "hi", "hi", "hi"]


def test_priority_is_fair_within_equal_priority():
    # 同一優先度内では試行回数で公平 (round-robin)。下位優先度は上位が片付くまで回らない。
    items = [
        WorkItem(id="z", priority=0),
        WorkItem(id="x", priority=5),
        WorkItem(id="y", priority=5),
    ]
    g = WorkListGather(items, strategy="priority")
    dispatched, _ = drive(g, completes={}, max_iters=4)
    assert dispatched == ["x", "y", "x", "y"]
    assert "z" not in dispatched


def test_custom_callable_strategy():
    # 常に最後の selectable を選ぶ custom 戦略。
    def pick_last(ctx: ScheduleContext) -> WorkItem:
        return ctx.selectable[-1]

    g = WorkListGather(["a", "b", "c"], strategy=pick_last)
    dispatched, _ = drive(g, completes={}, max_iters=3)
    assert dispatched == ["c", "c", "c"]


def test_custom_strategy_may_return_id_string():
    def pick_b(ctx: ScheduleContext) -> str:
        return "b" if any(it.id == "b" for it in ctx.selectable) else ctx.selectable[0].id

    g = WorkListGather(["a", "b"], strategy=pick_b)
    dispatched, _ = drive(g, completes={"b": 1}, max_iters=4)
    assert dispatched[0] == "b"


def test_custom_strategy_selecting_unselectable_fails_loud():
    def pick_ghost(ctx: ScheduleContext) -> str:
        return "ghost"

    g = WorkListGather(["a"], strategy=pick_ghost)
    with pytest.raises(ValueError, match="not selectable"):
        g(LoopState())


# -- per-item 上限 (exhausted) -----------------------------------------------


def test_per_item_cap_exhausts_failing_item():
    g = WorkListGather(["a"], strategy="fifo", max_attempts_per_item=3)
    dispatched, result = drive(g, completes={}, max_iters=50)
    assert dispatched == ["a", "a", "a"]  # 3 回で打ち止め
    rep = g.report(result.state)
    assert rep.exhausted == ("a",)
    assert rep.done == ()
    assert rep.drained


def test_done_beats_cap_on_same_attempt():
    # cap=1 で 1 回目に done になれば exhausted ではなく done に入る。
    g = WorkListGather(["a"], max_attempts_per_item=1, done_when=done_from_observation)
    _, result = drive(g, completes={"a": 1}, max_iters=10)
    rep = g.report(result.state)
    assert rep.done == ("a",)
    assert rep.exhausted == ()


# -- done 判定フック ---------------------------------------------------------


def test_done_when_is_independent_of_verify():
    # verify は常に goal 未達 (ループ全体は never_done) でも、done_when で個々の item を
    # 完了扱いにできる。
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    _, result = drive(g, completes={"a": 1, "b": 1}, max_iters=20)
    assert result.status == "stopped"  # WorkListDrained で停止 (goal_met ではない)
    assert g.done_items(result.state) == {"a", "b"}


def test_default_done_uses_goal_met():
    # done_when を省略すると record.goal_met を done シグナルとして見る。
    g = WorkListGather(["a", "b"])
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, {}),
        verify=lambda _o: VerifyOutcome(goal_met=True),  # 最初の step で goal 到達
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    # goal_met はループ全体も終わらせる。最初に回した a が done 扱いになる。
    assert dispatched == ["a"]
    assert g.done_items(result.state) == {"a"}


# -- attempt counter / 進捗 API + resume 安全 --------------------------------


def test_attempts_and_report_derive_from_history():
    g = WorkListGather(
        ["a", "b", "c"], strategy="fewest_attempts", done_when=done_from_observation
    )
    # 導出は observation の item ではなく **戦略のリプレイ** で帰属する。fewest_attempts の
    # 並びは a,b,c,a,b なので、b の 2 回目 (5 step 目) を done にする履歴を組む。
    state = history_of(
        ("a", False), ("b", False), ("c", False), ("a", False), ("b", True)
    )
    assert g.attempts(state) == {"a": 2, "b": 2, "c": 1}
    assert g.done_items(state) == {"b"}
    rep = g.report(state)
    assert isinstance(rep, WorkListProgress)
    assert rep.remaining == ("a", "c")
    assert not rep.drained


def test_derivation_is_resume_safe_across_fresh_instances():
    # 別プロセス相当: 同じ items 設定の *新しい* gatherer が同じ state で同じ導出を返す。
    state = history_of(("a", False), ("b", True), ("c", False))
    g1 = WorkListGather(["a", "b", "c"], done_when=done_from_observation)
    g2 = WorkListGather(["a", "b", "c"], done_when=done_from_observation)
    assert g1.attempts(state) == g2.attempts(state)
    assert g1.done_items(state) == g2.done_items(state) == {"b"}
    # 次に dispatch する item も一致 (in-process カウンタに依存しない)。
    assert g1(state)["id"] == g2(state)["id"]


def test_empty_work_list_is_immediately_drained():
    g = WorkListGather([])
    assert g.drained(LoopState())
    assert g(LoopState()) is DRAINED
    assert WorkListDrained(g).check(LoopState()) is not None


def test_gather_returns_drained_sentinel_when_all_done():
    g = WorkListGather(["a"], done_when=done_from_observation)
    state = history_of(("a", True))
    assert g(state) is DRAINED
    assert bool(DRAINED) is False
    assert "drained" in repr(DRAINED)


def test_build_ctx_receives_attempt_count():
    seen: list[tuple[str, int]] = []

    def build_ctx(item: WorkItem, attempt: int, _state: LoopState):
        seen.append((item.id, attempt))
        return item

    g = WorkListGather(["a"], max_attempts_per_item=3, build_ctx=build_ctx)
    drive(g, completes={}, max_iters=10)
    # attempt は dispatch 前の既試行回数 (0,1,2)。
    assert seen == [("a", 0), ("a", 1), ("a", 2)]


# -- WorkListDrained 停止条件 ------------------------------------------------


def test_work_list_drained_stops_before_gather_runs():
    # drained 後に gather が呼ばれて DRAINED が act に渡る、ということが起きない。
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    dispatched, result = drive(g, completes={"a": 1, "b": 1}, max_iters=50)
    assert result.stop is not None
    assert result.stop.name == "work_list_drained"
    # act は実在 item にだけ呼ばれた (DRAINED が混じらない)。
    assert set(dispatched) <= {"a", "b"}


# -- 統合: starve しないこと -------------------------------------------------


def test_failing_item_does_not_starve_others_integration():
    # "a" は永久に失敗、"b"/"c" は 1 回で完了。公平戦略 + per-item 上限なら b/c は
    # ちゃんと順番が回ってきて完了し、a だけが cap で打ち止めになる。
    g = WorkListGather(
        ["a", "b", "c"],
        strategy="fewest_attempts",
        max_attempts_per_item=3,
        done_when=done_from_observation,
    )
    dispatched, result = drive(g, completes={"b": 1, "c": 1}, max_iters=100)
    rep = g.report(result.state)
    assert rep.done == ("b", "c")  # starve されず完了
    assert rep.exhausted == ("a",)
    assert rep.attempts == {"a": 3, "b": 1, "c": 1}
    assert result.stop.name == "work_list_drained"


def test_naive_fifo_without_cap_starves_others():
    # 対照: 素朴 fifo + 上限なしだと失敗し続ける先頭が全反復を独占し、他は一度も回らない。
    g = WorkListGather(["a", "b", "c"], strategy="fifo")  # cap なし、drained 条件なし
    dispatched: list[str] = []
    run_loop(
        act=scripted_act(dispatched, {"b": 1, "c": 1}),  # b,c は本来すぐ終わるはず
        verify=never_done,
        gather=g,
        conditions=[MaxIterations(10)],
    )
    # a が永久に未完なので fifo は a を 10 回独占。b/c は starve。
    assert dispatched == ["a"] * 10
    assert "b" not in dispatched and "c" not in dispatched


# -- triage との接続 ---------------------------------------------------------


def test_from_triage_orders_by_ranking_and_excludes_blocked():
    candidates = [
        Candidate(id="low", priority=1),
        Candidate(id="high", priority=9, payload={"seed": 1}),
        Candidate(id="blocked", depends_on=("missing",)),  # 依存未充足 -> 除外
    ]
    g = WorkListGather.from_triage(candidates)
    ids = [it.id for it in g.items]
    assert ids == ["high", "low"]  # triage ランキング順 (優先度降順)、blocked は除外
    # priority / payload を引き継ぐ。
    assert g.items[0].priority == 9
    assert g.items[0].payload == {"seed": 1}


def test_default_ctx_is_json_native_for_persistent_gate():
    # 既定 build_ctx は永続人間ゲート (run_gated_loop) と合成しても state.db に保存できる
    # JSON ネイティブ dict を返す。WorkItem を返していた頃は request_decision の
    # JSON-native 検査で ValueError になっていた (#56 codex review 3)。
    from loop_agent import LoopStore, connect, run_gated_loop

    store = LoopStore(connect(":memory:"))
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    result = run_gated_loop(
        act=scripted_act([], {}),
        verify=never_done,
        gather=g,
        on=lambda _ctx: True,  # 全 action を不可逆扱い -> 最初の dispatch で pause
        store=store,
        run_id="r1",
        conditions=[WorkListDrained(g), MaxIterations(5)],
    )
    # JSON-native なので ValueError にならず pause し、保存された context が読める。
    assert result.status == "paused"
    assert result.pending is not None
    assert result.pending["action"]["id"] == "a"  # 既定 ctx dict が round-trip した


def test_item_of_excludes_gate_skips_from_exhaustion():
    # gate が item の action を SKIP すると run_loop は act せず StepRecord を積む。既定では
    # それも 1 試行として数え、走ってもいない item が per-item 上限で exhausted になりうる
    # (#56 codex review)。item_of が skip 行に None を返せば非実行として外せる。

    class SkipFirstTwo:
        """最初の 2 回は SKIP、以降は PROCEED する gate (skip 行に印を付ける)。"""

        def __init__(self) -> None:
            self.n = 0

        def review(self, context, state):
            self.n += 1
            if self.n <= 2:
                return GateReview(
                    disposition=GATE_SKIP, observation={"skipped": True}
                )
            return GateReview(disposition=GATE_PROCEED)

    # 単一 item, cap=2。skip を試行に数えると 2 回の skip で即 exhausted (act 0 回) になる。
    g = WorkListGather(
        ["a"],
        max_attempts_per_item=2,
        done_when=done_from_observation,
        item_of=item_of_observation,
    )
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, {"a": 1}),  # 実際に act すれば 1 回で done
        verify=never_done,
        gather=g,
        gate=SkipFirstTwo(),
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    # skip は試行に数えないので a は exhausted されず、PROCEED 後に実 act して done になる。
    rep = g.report(result.state)
    assert rep.done == ("a",)
    assert rep.exhausted == ()
    assert dispatched == ["a"]  # 実 act は 1 回だけ (skip 2 回は act を呼ばない)


def test_excluded_skips_still_rotate_fairly_no_starvation():
    # item_of で skip を非実行にしても、公平性は selections (offer 回数) で測るので先頭 item を
    # skip し続けても他 item が offer される (#56 codex review 2: starve 防止)。
    offered: list[str] = []

    class SkipEverything:
        def review(self, context, state):
            offered.append(_ctx_id(context))  # gate に提示された item
            return GateReview(disposition=GATE_SKIP, observation={"skipped": True})

    g = WorkListGather(
        ["a", "b", "c"], strategy="fewest_attempts", item_of=item_of_observation
    )
    run_loop(
        act=scripted_act([], {}),
        verify=never_done,
        gather=g,
        gate=SkipEverything(),
        conditions=[MaxIterations(6)],  # drained にはならない (skip は exhaust しない)
    )
    # 先頭 a に張り付かず a,b,c,a,b,c と巡回して全 item が human に提示される。
    assert offered == ["a", "b", "c", "a", "b", "c"]


def test_item_of_attributes_gate_edits_to_actual_item():
    # scheduler は a を offer するが、gate が最初の a を b の action に edit して PROCEED する。
    # record は b のものなので item_of で b に帰属する (#56 codex review 4: edit 取り違え防止)。
    # item_of を渡さないと b の record が offer 元 a に誤帰属する。
    class EditFirstAToB:
        def __init__(self) -> None:
            self.edited = False

        def review(self, context, state):
            if _ctx_id(context) == "a" and not self.edited:
                self.edited = True
                return GateReview(disposition=GATE_PROCEED, context={"id": "b"})
            return GateReview(disposition=GATE_PROCEED)

    g = WorkListGather(
        ["a", "b", "c"], done_when=done_from_observation, item_of=item_of_observation
    )
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, {"a": 1, "b": 1, "c": 1}),
        verify=never_done,
        gather=g,
        gate=EditFirstAToB(),
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    rep = g.report(result.state)
    # 各 item は実 act 1 回ずつ正しい item に帰属して done (a は edit step では実行されず、
    # 後の素通り step で実行された)。誤帰属なら a が 2 回・b が 0 回等になる。
    assert set(rep.done) == {"a", "b", "c"}
    assert rep.attempts == {"a": 1, "b": 1, "c": 1}
    assert dispatched == ["b", "c", "a"]  # offer a->edit b, 次 c, 最後 a
    assert result.stop.name == "work_list_drained"


def test_skips_counted_as_attempts_by_default():
    # 対照: item_of を渡さなければ skip 行も 1 試行として offer 元 item に数える (既定挙動)。
    class AlwaysSkip:
        def review(self, context, state):
            return GateReview(disposition=GATE_SKIP, observation={"skipped": True})

    g = WorkListGather(["a"], max_attempts_per_item=2, done_when=done_from_observation)
    result = run_loop(
        act=scripted_act([], {}),
        verify=never_done,
        gather=g,
        gate=AlwaysSkip(),
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    # skip 2 回で a が exhausted (act は 0 回)。既定の数え方を明示。
    assert g.exhausted_items(result.state) == {"a"}
    assert g.attempts(result.state) == {"a": 2}


def test_schedule_context_is_exported_from_facades():
    # custom strategy を型付けするのに必要なので facade から import できること。
    import loop_agent
    import loop_agent.discovery as discovery_pkg

    assert loop_agent.ScheduleContext is ScheduleContext
    assert discovery_pkg.ScheduleContext is ScheduleContext


def test_resume_with_same_gatherer_continues_consistently():
    # 同一 gatherer を initial_state で再開すると、attempts が引き継がれて最後まで drained する。
    g = WorkListGather(
        ["a", "b", "c"],
        strategy="fewest_attempts",
        max_attempts_per_item=2,
        done_when=done_from_observation,
    )
    completes = {"b": 1, "c": 1}

    # leg 1: 早めに打ち切る。
    disp1: list[str] = []
    leg1 = run_loop(
        act=scripted_act(disp1, completes),
        verify=never_done,
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(2)],
    )
    assert leg1.status == "stopped" and leg1.stop.name == "max_iterations"

    # leg 2: 中断地点 (同じ state) から同一 gatherer で再開。
    disp2: list[str] = []
    leg2 = run_loop(
        act=scripted_act(disp2, completes),
        verify=never_done,
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(50)],
        initial_state=leg1.state,
    )
    rep = g.report(leg2.state)
    # 2 leg 通算で b/c は完了、a は cap2 で exhausted。starve していない。
    assert rep.done == ("b", "c")
    assert rep.exhausted == ("a",)
    assert rep.attempts == {"a": 2, "b": 1, "c": 1}
    assert leg2.stop.name == "work_list_drained"


def test_from_triage_respects_done_dependencies():
    candidates = [
        Candidate(id="dep"),
        Candidate(id="needs_dep", depends_on=("dep",)),
    ]
    # dep 未完了なら needs_dep は blocked で除外。
    g0 = WorkListGather.from_triage(candidates)
    assert [it.id for it in g0.items] == ["dep"]
    # dep 完了後に呼び直すと needs_dep が ready になり取り込まれる。
    g1 = WorkListGather.from_triage(candidates, done=["dep"])
    assert [it.id for it in g1.items] == ["needs_dep"]
