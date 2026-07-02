"""Verify work-discovery input selection (Issue #24): deterministic compute layer + human gate in delivery layer.

Targets report.md S3.5 / S4.6 / S5 Phase 3 success condition d: "the completion ->
next-iteration connection runs through a human gate."

This demonstrates that:

(a) the compute layer :func:`triage` is read-only and deterministic (same unordered
    inputs -> same output), resolves dependencies (ready when all deps are done), ranks
    by priority/effort, explains blocked candidates, and detects cycles;
(b) the delivery layer :class:`WorkDiscovery` places triage on a human gate
    (pending_decision) in **propose-only** mode and never adopts automatically;
(c) the four decisions approve / edit / reject / respond map correctly to adoption
    candidates;
(d) decisions are persisted in state.db and retained across
    **pause -> (resolve through another connection) -> resume** (do not ask the human
    twice);
(e) :func:`discover_next` connects "completion -> next iteration" and does not propose
    when the previous result is paused;
(f) the **full cycle** of completed loop -> triage -> human approve -> adopted
    candidate as next-loop input always passes through a human gate (= no fully
    automatic start).
"""

from __future__ import annotations

import pytest

from loop_agent import (
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
from loop_agent.discovery import GATE_KEY_PREFIX
from loop_agent.loop import LoopResult
from loop_agent.store import EVENT_GATE


# -- Compute layer (triage): deterministic and read-only ---------------------


def test_ready_when_deps_done_else_blocked():
    """Ready when all dependencies are done; blocked when any are missing (dependency resolution)."""
    cands = [
        Candidate(id="a"),  # no dependencies -> ready
        Candidate(id="b", depends_on=("a",)),  # a is not done -> blocked
        Candidate(id="c", depends_on=("x",)),  # x is done -> ready
    ]
    result = triage(cands, done=("x",))
    ready_ids = [c.id for c in result.ready]
    blocked_ids = [b.candidate.id for b in result.blocked]
    assert ready_ids == ["a", "c"]  # equal priority -> ascending id
    assert blocked_ids == ["b"]
    assert result.blocked[0].pending_deps == ("a",)  # a is waiting on a known candidate


def test_ranking_priority_then_effort_then_id():
    """Ready candidates are ranked deterministically by descending priority -> ascending effort -> ascending id."""
    cands = [
        Candidate(id="low", priority=1, effort=1),
        Candidate(id="hi_cheap", priority=5, effort=1),
        Candidate(id="hi_pricey", priority=5, effort=9),
        Candidate(id="hi_cheap_b", priority=5, effort=1),  # same priority and effort -> ascending id
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
    """The same unordered inputs always return the same Triage (deterministic and read-only)."""
    base = [
        Candidate(id="a", priority=3),
        Candidate(id="b", priority=3, depends_on=("a",)),
        Candidate(id="c", priority=7),
    ]
    forward = triage(base, done=())
    reversed_ = triage(list(reversed(base)), done=())
    assert forward == reversed_
    # The input list is not mutated (read-only).
    assert [c.id for c in base] == ["a", "b", "c"]


def test_recommended_none_when_nothing_ready():
    """The recommendation is None when ready is empty (normal all-blocked path)."""
    result = triage([Candidate(id="b", depends_on=("missing",))])
    assert result.ready == ()
    assert result.recommended is None
    assert result.blocked[0].unknown_deps == ("missing",)


def test_done_candidates_excluded():
    """Candidates with ids that are already done are excluded from the next iteration."""
    result = triage([Candidate(id="a"), Candidate(id="b")], done=("a",))
    assert [c.id for c in result.ready] == ["b"]
    assert result.blocked == ()


def test_duplicate_ids_rejected():
    """Duplicate candidate ids raise ValueError because they would break deterministic output."""
    with pytest.raises(ValueError, match="duplicate candidate id"):
        triage([Candidate(id="a"), Candidate(id="a")])


def test_cycle_detected_and_flagged():
    """Candidates in dependency cycles are blocked and marked with in_cycle."""
    cands = [
        Candidate(id="a", depends_on=("b",)),
        Candidate(id="b", depends_on=("a",)),
        Candidate(id="c"),  # outside the cycle -> ready
    ]
    result = triage(cands)
    assert [c.id for c in result.ready] == ["c"]
    blocked = {b.candidate.id: b for b in result.blocked}
    assert blocked["a"].in_cycle is True
    assert blocked["b"].in_cycle is True
    assert "dependency cycle" in blocked["a"].reason


def test_self_dependency_is_cycle():
    """Self-dependency is detected as an obvious cycle."""
    result = triage([Candidate(id="a", depends_on=("a",))])
    assert result.blocked[0].in_cycle is True


def test_cycle_member_reachable_only_via_finished_node():
    """Detect all SCC members, including BLACK-reached members that back-edge DFS misses.

    C1->C2, C2->{C3,C4}, C3->C1, C4->C3 form one SCC (all four nodes).
    Naive back-edge DFS finishes C3 (BLACK), misclassifies C4->C3 as a cross-edge,
    and misses C4. Tarjan SCC marks everyone, including C4, as in_cycle.
    """
    cands = [
        Candidate(id="C1", depends_on=("C2",)),
        Candidate(id="C2", depends_on=("C3", "C4")),
        Candidate(id="C3", depends_on=("C1",)),
        Candidate(id="C4", depends_on=("C3",)),
    ]
    import random

    for _ in range(8):  # independent of input order (deterministic).
        shuffled = list(cands)
        random.shuffle(shuffled)
        result = triage(shuffled)
        in_cycle = {b.candidate.id for b in result.blocked if b.in_cycle}
        assert in_cycle == {"C1", "C2", "C3", "C4"}
        assert result.ready == ()


def test_candidate_validation():
    """Reject empty ids and negative effort."""
    with pytest.raises(ValueError, match="non-empty string"):
        Candidate(id="")
    with pytest.raises(ValueError, match="effort must be >= 0"):
        Candidate(id="a", effort=-1)


# -- Delivery layer (WorkDiscovery): propose-only / human gate ---------------


def make_store():
    return LoopStore(connect(":memory:"))


def test_propose_is_propose_only_pending():
    """propose only registers the proposal as pending and does not auto-adopt (propose-only)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    prop = wd.propose([Candidate(id="a", priority=5), Candidate(id="b")], cycle=0)
    assert isinstance(prop, Proposal)
    assert prop.pending["status"] == "pending"
    assert prop.gate_key == f"{GATE_KEY_PREFIX}0"
    # No adoption has happened.
    assert wd.adopted(0).status == "pending"
    assert wd.adopted(0).adopted is False
    # The proposal is listed as one pending item in the pending_decision register.
    assert len(store.list_pending_decisions("run-1")) == 1


def test_approve_adopts_recommended():
    """approve adopts the recommended candidate."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a", priority=9), Candidate(id="b", priority=1)], cycle=0)
    result = wd.resolve(0, "approve")
    assert result.status == "resolved"
    assert result.adopted is True
    assert result.candidate.id == "a"  # recommendation = highest priority


def test_edit_adopts_chosen_ready_candidate():
    """edit adopts another ready candidate selected by the human."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a", priority=9), Candidate(id="b", priority=1)], cycle=0)
    result = wd.resolve(0, "edit", payload="b")
    assert result.decision == "edit"
    assert result.candidate.id == "b"


def test_edit_rejects_non_ready_selection():
    """Selecting a blocked or unknown candidate with edit fails loudly (delivery layer preserves dependency invariants)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose(
        [Candidate(id="a"), Candidate(id="blk", depends_on=("missing",))], cycle=0
    )
    with pytest.raises(ValueError, match="not a ready candidate"):
        wd.resolve(0, "edit", payload="blk")
    with pytest.raises(ValueError, match="not a ready candidate"):
        wd.resolve(0, "edit", payload="nope")
    # Invalid edits do not persist a decision (still pending).
    assert wd.adopted(0).status == "pending"


def test_reject_adopts_nothing():
    """reject adopts nothing (does not trigger the next iteration)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    result = wd.resolve(0, "reject")
    assert result.decision == "reject"
    assert result.adopted is False
    assert result.candidate is None


def test_respond_records_response_no_adoption():
    """respond records the response body without adoption (can be passed to the next triage context)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    result = wd.resolve(0, "respond", payload="Review the priority")
    assert result.adopted is False
    assert result.response == "Review the priority"


def test_propose_idempotent_per_cycle():
    """Re-proposing the same cycle does not break the first proposal or decision (idempotent)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose([Candidate(id="a")], cycle=0)
    wd.resolve(0, "approve")
    # Re-proposing in the same cycle with a changed candidate set leaves the resolved decision intact.
    again = wd.propose([Candidate(id="z")], cycle=0)
    assert again.pending["status"] == "resolved"
    # The returned triage matches the **persisted** proposal (recommended a), not recomputed z
    # (keeps Proposal.triage internally consistent with pending/adopted).
    assert again.triage.recommended.id == "a"
    assert wd.adopted(0).candidate.id == "a"  # the first proposal's recommendation remains adopted


def test_adopted_absent_when_not_proposed():
    """adopted is absent for a cycle that has not been proposed."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    assert wd.adopted(7).status == "absent"


def test_payload_carried_to_adopted_candidate():
    """Candidate payload is restored on the adopted candidate and can be passed to next-loop input (JSON round-trip)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    wd.propose(
        [Candidate(id="a", summary="Task A", payload={"task": "fix #1", "n": 3})],
        cycle=0,
    )
    cand = wd.resolve(0, "approve").candidate
    assert cand.payload == {"task": "fix #1", "n": 3}
    assert cand.summary == "Task A"


# -- Persistence: retained across pause -> resolve on another connection -> resume


def test_decision_persists_across_connections(tmp_path):
    """propose (connection A) -> resolve (connection B) -> adopted (connection C) reads the same decision (persistent)."""
    db = tmp_path / "loop.db"
    # Connection A: register the proposal and "pause" (remains pending in propose-only mode).
    wd_a = WorkDiscovery(LoopStore(connect(db)), "run-1")
    wd_a.propose([Candidate(id="a", priority=5), Candidate(id="b")], cycle=0)
    # Connection B: the human records adoption/rejection from another process.
    wd_b = WorkDiscovery(LoopStore(connect(db)), "run-1")
    wd_b.resolve(0, "edit", payload="b")
    # Connection C: reading the decision again on resume retains the adoption.
    wd_c = WorkDiscovery(LoopStore(connect(db)), "run-1")
    adopted = wd_c.adopted(0)
    assert adopted.status == "resolved"
    assert adopted.candidate.id == "b"


def test_gate_events_recorded():
    """Proposal (pending) and decision (resolved) remain in the journal as loop_gate events (audit)."""
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
    # gate_key is in the discovery namespace.
    assert all(
        e["payload"]["gate_key"].startswith(GATE_KEY_PREFIX) for e in gate_events
    )


# -- discover_next: completion -> next-iteration connection ------------------


def test_discover_next_proposes_after_completion():
    """Passing a completed loop result proposes the next candidate (completion -> next iteration)."""
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
    """Do not propose when the previous result is paused (human gate pause; human should resolve first)."""
    store = make_store()
    # Use the real LoopResult.paused property (status=="paused").
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


# -- full cycle: completion -> triage -> human approve -> next-loop input ----


def test_full_cycle_completion_to_next_iteration_through_human_gate(tmp_path):
    """Success condition d: completion -> next-iteration connection always passes through a human gate (no auto-start).

    1. Complete loop #1.
    2. triage -> propose the next candidate with discover_next (propose-only / pending).
    3. The next iteration does not happen until the *human* records approve (= no fully automatic start).
    4. After approve, run loop #2 with the adopted candidate's payload as input.
    """
    db = tmp_path / "loop.db"
    store = LoopStore(connect(db))

    # 1. Complete loop #1.
    first = run_loop(
        act=lambda _c: ActOutcome(observation="done-1", tokens=1),
        verify=lambda _o: VerifyOutcome(goal_met=True),
        conditions=[MaxIterations(2)],
    )
    assert first.succeeded

    # 2. Completion -> triage -> proposal (pending on the human gate).
    prop = discover_next(
        store=store,
        run_id="cycle",
        candidates=[
            Candidate(id="t1", priority=9, payload={"goal": "build feature X"}),
            Candidate(id="t2", priority=1, depends_on=("t1",)),  # blocked until t1 is complete
        ],
        result=first,
        cycle=1,
    )
    assert prop is not None
    assert prop.triage.recommended.id == "t1"
    assert [b.candidate.id for b in prop.triage.blocked] == ["t2"]

    # 3. Before the human decides, there is no adoption (= does not advance automatically).
    wd = WorkDiscovery(store, "cycle")
    assert wd.adopted(1).adopted is False
    assert len(store.list_pending_decisions("cycle")) == 1

    # Human approves (= passes the human gate).
    adoption = wd.resolve(1, "approve")
    assert adoption.adopted is True
    chosen = adoption.candidate
    assert chosen.id == "t1"

    # 4. Run loop #2 with the adopted candidate's payload as input (next-iteration connection).
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

    # Adoption is retained across resume (reading through another connection still returns t1).
    assert WorkDiscovery(LoopStore(connect(db)), "cycle").adopted(1).candidate.id == "t1"


def test_iterative_discovery_unblocks_dependent_next_cycle():
    """Accumulated completions make blocked candidates ready in the next cycle (iterative input-selection loop)."""
    store = make_store()
    wd = WorkDiscovery(store, "run-1")
    # cycle 1: only t1 is ready (t2 is waiting on t1).
    prop1 = wd.propose(
        [Candidate(id="t1"), Candidate(id="t2", depends_on=("t1",))], cycle=1
    )
    assert prop1.triage.recommended.id == "t1"
    wd.resolve(1, "approve")
    # cycle 2: adding t1 to done makes t2 ready.
    prop2 = wd.propose(
        [Candidate(id="t2", depends_on=("t1",))], done=("t1",), cycle=2
    )
    assert prop2.triage.recommended.id == "t2"


def test_adoption_result_is_frozen_value():
    """AdoptionResult is an immutable value object (provides the .adopted adoption predicate)."""
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
    """Triage uses value equality (the comparison foundation for determinism tests)."""
    a = triage([Candidate(id="a")])
    b = triage([Candidate(id="a")])
    assert a == b
    assert isinstance(a, Triage)
