"""Verify fair multi-item scheduling (Issue #56).

Main validation points:

- Selection order for each scheduling strategy (round_robin / fewest_attempts / fifo /
  priority / custom).
- Independence of per-item caps (exhausted) and the done predicate hook.
- The canonical attempt counter / progress APIs are derived from state and resume-safe.
- An item that intentionally keeps failing does not starve other items (integration test).
- Connection to triage (from_triage).
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


# -- Test harness ------------------------------------------------------------


def _ctx_id(ctx) -> str:
    """Get the item id from build_ctx output (default JSON dict / WorkItem / bare id string)."""
    if isinstance(ctx, dict):
        return ctx["id"]
    if isinstance(ctx, WorkItem):
        return ctx.id
    return ctx


def scripted_act(dispatched: list[str], completes: dict[str, int]):
    """Record dispatched item ids and set the done flag on the ``completes``-th call.

    If ``completes[id] == n``, that id completes on the **n-th dispatch**. If ``id`` is
    not in ``completes``, it never completes. The done signal is baked into
    ``observation["done"]``, so replay (resume) stays stable (the internal act counter
    is not used for attribution).
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
    """Done predicate hook that reads the done flag baked in by ``scripted_act``.

    Observations without a ``done`` key, such as gate SKIP rows, return ``False`` (not done).
    """
    obs = record.observation
    return bool(isinstance(obs, dict) and obs.get("done"))


def item_of_observation(record: StepRecord):
    """Return the actual item id baked in by ``scripted_act`` for item_of (gate composition).

    Skip rows (``{"skipped": True}`` with no ``item``) return ``None`` (not executed).
    """
    obs = record.observation
    return obs.get("item") if isinstance(obs, dict) else None


def history_of(*ids_done: tuple[str, bool]) -> LoopState:
    """Build ``LoopState.history`` from a ``(item_id, done)`` sequence (for derivation tests)."""
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
    """Run until drained or ``MaxIterations`` and return (dispatch order, LoopResult)."""
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, completes),
        verify=never_done,
        gather=gatherer,
        conditions=[WorkListDrained(gatherer), MaxIterations(max_iters)],
    )
    return dispatched, result


# -- WorkItem / construction validation --------------------------------------


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


# -- Scheduling strategy selection order --------------------------------------


def test_fewest_attempts_interleaves_fairly():
    # Nothing completes -> selecting the fewest attempts creates strict round-robin.
    g = WorkListGather(["a", "b", "c"], strategy="fewest_attempts")
    dispatched, _ = drive(g, completes={}, max_iters=9)
    assert dispatched == ["a", "b", "c", "a", "b", "c", "a", "b", "c"]


def test_round_robin_rotates_positionally():
    g = WorkListGather(["a", "b", "c"], strategy="round_robin")
    dispatched, _ = drive(g, completes={}, max_iters=7)
    assert dispatched == ["a", "b", "c", "a", "b", "c", "a"]


def test_round_robin_skips_completed_and_keeps_rotating():
    # Once b completes on the first attempt, a,b,c,(b done),... skips b and cycles a<->c.
    g = WorkListGather(
        ["a", "b", "c"], strategy="round_robin", done_when=done_from_observation
    )
    dispatched, _ = drive(g, completes={"b": 1}, max_iters=7)
    # a, b(done), c, a, c, a, c  -- b never appears again after completion.
    assert dispatched == ["a", "b", "c", "a", "c", "a", "c"]
    assert "b" not in dispatched[2:]


def test_fifo_is_naive_head_selection():
    # fifo is a naive strategy that returns the first unfinished item. If it never
    # completes, the head item keeps running.
    g = WorkListGather(["a", "b", "c"], strategy="fifo")
    dispatched, _ = drive(g, completes={}, max_iters=4)
    assert dispatched == ["a", "a", "a", "a"]


def test_fifo_advances_as_items_complete():
    g = WorkListGather(["a", "b", "c"], strategy="fifo", done_when=done_from_observation)
    dispatched, _ = drive(g, completes={"a": 2, "b": 1, "c": 1}, max_iters=20)
    # a,a(done),b(done),c(done) -> drained.
    assert dispatched == ["a", "a", "b", "c"]


def test_priority_is_strict_highest_first():
    # priority is strictly descending: the highest-priority item runs until done/exhausted.
    items = [
        WorkItem(id="lo", priority=0),
        WorkItem(id="hi", priority=10),
        WorkItem(id="mid", priority=5),
    ]
    g = WorkListGather(items, strategy="priority")
    dispatched, _ = drive(g, completes={}, max_iters=4)
    assert dispatched == ["hi", "hi", "hi", "hi"]


def test_priority_is_fair_within_equal_priority():
    # Equal priority is fair by attempt count (round-robin). Lower priority waits until
    # higher-priority items are cleared.
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
    # Custom strategy that always selects the last selectable item.
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


# -- Per-item cap (exhausted) ------------------------------------------------


def test_per_item_cap_exhausts_failing_item():
    g = WorkListGather(["a"], strategy="fifo", max_attempts_per_item=3)
    dispatched, result = drive(g, completes={}, max_iters=50)
    assert dispatched == ["a", "a", "a"]  # capped after 3 attempts
    rep = g.report(result.state)
    assert rep.exhausted == ("a",)
    assert rep.done == ()
    assert rep.drained


def test_done_beats_cap_on_same_attempt():
    # With cap=1, completion on the first attempt goes to done, not exhausted.
    g = WorkListGather(["a"], max_attempts_per_item=1, done_when=done_from_observation)
    _, result = drive(g, completes={"a": 1}, max_iters=10)
    rep = g.report(result.state)
    assert rep.done == ("a",)
    assert rep.exhausted == ()


# -- Done predicate hook -----------------------------------------------------


def test_done_when_is_independent_of_verify():
    # Even if verify never reaches the goal (the whole loop is never_done), done_when can
    # mark individual items complete.
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    _, result = drive(g, completes={"a": 1, "b": 1}, max_iters=20)
    assert result.status == "stopped"  # stopped by WorkListDrained (not goal_met)
    assert g.done_items(result.state) == {"a", "b"}


def test_default_done_uses_goal_met():
    # When done_when is omitted, record.goal_met is used as the done signal.
    g = WorkListGather(["a", "b"])
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, {}),
        verify=lambda _o: VerifyOutcome(goal_met=True),  # goal reached on the first step
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    # goal_met also ends the whole loop. The first dispatched item, a, is treated as done.
    assert dispatched == ["a"]
    assert g.done_items(result.state) == {"a"}


# -- Attempt counter / progress API + resume safety --------------------------


def test_attempts_and_report_derive_from_history():
    g = WorkListGather(
        ["a", "b", "c"], strategy="fewest_attempts", done_when=done_from_observation
    )
    # Derivation attributes via **strategy replay**, not the observation item. The
    # fewest_attempts order is a,b,c,a,b, so build history where b's second attempt
    # (5th step) is done.
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
    # Equivalent to another process: a *new* gatherer with the same item configuration
    # returns the same derivation for the same state.
    state = history_of(("a", False), ("b", True), ("c", False))
    g1 = WorkListGather(["a", "b", "c"], done_when=done_from_observation)
    g2 = WorkListGather(["a", "b", "c"], done_when=done_from_observation)
    assert g1.attempts(state) == g2.attempts(state)
    assert g1.done_items(state) == g2.done_items(state) == {"b"}
    # The next item to dispatch also matches (does not depend on an in-process counter).
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
    # attempt is the existing attempt count before dispatch (0,1,2).
    assert seen == [("a", 0), ("a", 1), ("a", 2)]


# -- WorkListDrained stop condition ------------------------------------------


def test_work_list_drained_stops_before_gather_runs():
    # After drained, gather is not called and DRAINED is not passed to act.
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    dispatched, result = drive(g, completes={"a": 1, "b": 1}, max_iters=50)
    assert result.stop is not None
    assert result.stop.name == "work_list_drained"
    # act was only called with real items (DRAINED is not mixed in).
    assert set(dispatched) <= {"a", "b"}


# -- Integration: no starvation ----------------------------------------------


def test_failing_item_does_not_starve_others_integration():
    # "a" fails forever; "b"/"c" complete in one attempt. With a fair strategy plus
    # per-item cap, b/c get their turns and complete, while only a is capped.
    g = WorkListGather(
        ["a", "b", "c"],
        strategy="fewest_attempts",
        max_attempts_per_item=3,
        done_when=done_from_observation,
    )
    dispatched, result = drive(g, completes={"b": 1, "c": 1}, max_iters=100)
    rep = g.report(result.state)
    assert rep.done == ("b", "c")  # completed without starvation
    assert rep.exhausted == ("a",)
    assert rep.attempts == {"a": 3, "b": 1, "c": 1}
    assert result.stop.name == "work_list_drained"


def test_naive_fifo_without_cap_starves_others():
    # Contrast: naive fifo with no cap lets the failing head item monopolize all
    # iterations, so the other items never run.
    g = WorkListGather(["a", "b", "c"], strategy="fifo")  # no cap, no drained condition
    dispatched: list[str] = []
    run_loop(
        act=scripted_act(dispatched, {"b": 1, "c": 1}),  # b,c should normally finish quickly
        verify=never_done,
        gather=g,
        conditions=[MaxIterations(10)],
    )
    # Since a never completes, fifo lets a monopolize 10 attempts. b/c starve.
    assert dispatched == ["a"] * 10
    assert "b" not in dispatched and "c" not in dispatched


# -- Triage connection --------------------------------------------------------


def test_from_triage_orders_by_ranking_and_excludes_blocked():
    candidates = [
        Candidate(id="low", priority=1),
        Candidate(id="high", priority=9, payload={"seed": 1}),
        Candidate(id="blocked", depends_on=("missing",)),  # unmet dependency -> excluded
    ]
    g = WorkListGather.from_triage(candidates)
    ids = [it.id for it in g.items]
    assert ids == ["high", "low"]  # triage ranking order (descending priority), blocked excluded
    # priority / payload are inherited.
    assert g.items[0].priority == 9
    assert g.items[0].payload == {"seed": 1}


def test_default_ctx_is_json_native_for_persistent_gate():
    # The default build_ctx returns a JSON-native dict that can be stored in state.db
    # even when composed with the persistent human gate (run_gated_loop). When it
    # returned WorkItem, request_decision's JSON-native check raised ValueError
    # (#56 codex review 3).
    from loop_agent import LoopStore, connect, run_gated_loop

    store = LoopStore(connect(":memory:"))
    g = WorkListGather(["a", "b"], done_when=done_from_observation)
    result = run_gated_loop(
        act=scripted_act([], {}),
        verify=never_done,
        gather=g,
        on=lambda _ctx: True,  # treat every action as irreversible -> pause on first dispatch
        store=store,
        run_id="r1",
        conditions=[WorkListDrained(g), MaxIterations(5)],
    )
    # Because it is JSON-native, this pauses without ValueError and the stored context is readable.
    assert result.status == "paused"
    assert result.pending is not None
    assert result.pending["action"]["id"] == "a"  # default ctx dict round-tripped


def test_item_of_excludes_gate_skips_from_exhaustion():
    # When gate SKIPs an item's action, run_loop appends a StepRecord without calling act.
    # By default this still counts as one attempt, so an item that never ran can become
    # exhausted by the per-item cap (#56 codex review). If item_of returns None for skip
    # rows, they can be excluded as not executed.

    class SkipFirstTwo:
        """Gate that SKIPs the first 2 times, then PROCEEDs (marking skip rows)."""

        def __init__(self) -> None:
            self.n = 0

        def review(self, context, state):
            self.n += 1
            if self.n <= 2:
                return GateReview(
                    disposition=GATE_SKIP, observation={"skipped": True}
                )
            return GateReview(disposition=GATE_PROCEED)

    # Single item, cap=2. If skips count as attempts, two skips immediately exhaust it
    # (0 act calls).
    g = WorkListGather(
        ["a"],
        max_attempts_per_item=2,
        done_when=done_from_observation,
        item_of=item_of_observation,
    )
    dispatched: list[str] = []
    result = run_loop(
        act=scripted_act(dispatched, {"a": 1}),  # if act actually runs, it completes once
        verify=never_done,
        gather=g,
        gate=SkipFirstTwo(),
        conditions=[WorkListDrained(g), MaxIterations(20)],
    )
    # Skips do not count as attempts, so a is not exhausted and becomes done after the
    # real act following PROCEED.
    rep = g.report(result.state)
    assert rep.done == ("a",)
    assert rep.exhausted == ()
    assert dispatched == ["a"]  # only one real act (the two skips do not call act)


def test_excluded_skips_still_rotate_fairly_no_starvation():
    # Even when item_of treats skips as not executed, fairness is measured by selections
    # (offer count), so other items are still offered when the head item is repeatedly
    # skipped (#56 codex review 2: starvation prevention).
    offered: list[str] = []

    class SkipEverything:
        def review(self, context, state):
            offered.append(_ctx_id(context))  # item presented to gate
            return GateReview(disposition=GATE_SKIP, observation={"skipped": True})

    g = WorkListGather(
        ["a", "b", "c"], strategy="fewest_attempts", item_of=item_of_observation
    )
    run_loop(
        act=scripted_act([], {}),
        verify=never_done,
        gather=g,
        gate=SkipEverything(),
        conditions=[MaxIterations(6)],  # does not drain (skips do not exhaust)
    )
    # It does not stick to head item a; it cycles a,b,c,a,b,c and presents every item to human.
    assert offered == ["a", "b", "c", "a", "b", "c"]


def test_item_of_attributes_gate_edits_to_actual_item():
    # The scheduler offers a, but gate edits the first a into b's action and PROCEEDs.
    # The record belongs to b, so item_of attributes it to b (#56 codex review 4:
    # preventing edit misattribution). Without item_of, b's record is incorrectly
    # attributed to the offered source a.
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
    # Each item gets one real act correctly attributed and becomes done (a does not run
    # on the edited step; it runs later on a pass-through step). With misattribution,
    # this would look like a=2 and b=0, etc.
    assert set(rep.done) == {"a", "b", "c"}
    assert rep.attempts == {"a": 1, "b": 1, "c": 1}
    assert dispatched == ["b", "c", "a"]  # offer a->edit b, then c, finally a
    assert result.stop.name == "work_list_drained"


def test_skips_counted_as_attempts_by_default():
    # Contrast: without item_of, skip rows count as one attempt on the offered source item
    # (default behavior).
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
    # Two skips exhaust a (0 act calls). This documents the default counting behavior.
    assert g.exhausted_items(result.state) == {"a"}
    assert g.attempts(result.state) == {"a": 2}


def test_schedule_context_is_exported_from_facades():
    # It must be importable from facades because custom strategies need it for typing.
    import loop_agent
    import loop_agent.discovery as discovery_pkg

    assert loop_agent.ScheduleContext is ScheduleContext
    assert discovery_pkg.ScheduleContext is ScheduleContext


def test_triage_function_does_not_shadow_a_submodule():
    # Input selection implementation lives in _triage (private). If triage is placed in
    # a submodule with the same name, facade's `from ._triage import triage` (function)
    # overwrites the package attribute triage, causing `import loop_agent.discovery.triage`
    # to bind to the *function* by mistake (#56 review).
    import loop_agent
    import loop_agent.discovery as discovery_pkg

    # The facade triage is a function (returning Triage), and Candidate is also available
    # from the facade.
    assert callable(discovery_pkg.triage)
    assert loop_agent.triage is discovery_pkg.triage
    rec = discovery_pkg.triage([discovery_pkg.Candidate(id="x")])
    assert rec.recommended.id == "x"
    # There is no public submodule named triage (no shadowing).
    with pytest.raises(ModuleNotFoundError):
        import loop_agent.discovery.triage  # noqa: F401


def test_resume_with_same_gatherer_continues_consistently():
    # Resuming the same gatherer with initial_state carries attempts forward and drains.
    g = WorkListGather(
        ["a", "b", "c"],
        strategy="fewest_attempts",
        max_attempts_per_item=2,
        done_when=done_from_observation,
    )
    completes = {"b": 1, "c": 1}

    # leg 1: stop early.
    disp1: list[str] = []
    leg1 = run_loop(
        act=scripted_act(disp1, completes),
        verify=never_done,
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(2)],
    )
    assert leg1.status == "stopped" and leg1.stop.name == "max_iterations"

    # leg 2: resume the same gatherer from the interruption point (same state).
    disp2: list[str] = []
    leg2 = run_loop(
        act=scripted_act(disp2, completes),
        verify=never_done,
        gather=g,
        conditions=[WorkListDrained(g), MaxIterations(50)],
        initial_state=leg1.state,
    )
    rep = g.report(leg2.state)
    # Across both legs, b/c complete and a is exhausted by cap2. No starvation.
    assert rep.done == ("b", "c")
    assert rep.exhausted == ("a",)
    assert rep.attempts == {"a": 2, "b": 1, "c": 1}
    assert leg2.stop.name == "work_list_drained"


def test_from_triage_respects_done_dependencies():
    candidates = [
        Candidate(id="dep"),
        Candidate(id="needs_dep", depends_on=("dep",)),
    ]
    # If dep is unfinished, needs_dep is blocked and excluded.
    g0 = WorkListGather.from_triage(candidates)
    assert [it.id for it in g0.items] == ["dep"]
    # Calling again after dep is done makes needs_dep ready and includes it.
    g1 = WorkListGather.from_triage(candidates, done=["dep"])
    assert [it.id for it in g1.items] == ["needs_dep"]
