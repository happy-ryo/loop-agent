"""Validation for the limited human gate (Issue #15): irreversible-only triggering, four decisions, and pause/resume retention.

Targets report.md S4.5 / R6 / S5 Phase2 success condition c, "the human gate
triggers on irreversible operations and approve/reject is reflected":

(a) triggers only for irreversible actions while reversible actions pass through
    (= not a gate on every step),
(b) correctly maps the four decisions, approve / edit / reject / respond, to
    action execution,
(c) persists decisions to state.db and retains them across
    **pause -> (resolve on another connection) -> resume** (does not ask the
    human twice),
(d) proves the store-level decision registry (request/resolve/get/list) is
    idempotent and validated.
"""

from __future__ import annotations

import pytest

from loop_agent import (
    ActOutcome,
    DBProgressLog,
    DECISION_KINDS,
    Decision,
    HumanGate,
    LoopStore,
    MaxIterations,
    NoProgress,
    VerifyOutcome,
    connect,
    run_gated_loop,
    run_loop,
)
from loop_agent.loop import GATE_PROCEED, GateReview
from loop_agent.store import EVENT_GATE
from conftest import never_done


# -- Minimal Test World ------------------------------------------------------


def make_world(actions):
    """A world where ``gather`` proposes ``actions[iteration]`` and ``act`` records executions.

    For a step skipped by the gate, ``act`` is not called and the action is not
    executed (it does not appear in executed). The iteration still advances, so
    gather moves on to the next action.
    """
    executed: list = []

    def gather(state):
        return actions[state.iteration]

    def act(action):
        executed.append(action)
        return ActOutcome(observation=action, tokens=0)

    return gather, act, executed


def is_deploy(action) -> bool:
    """Predicate that treats ``"deploy"`` as irreversible (= representative high-impact action)."""
    return action == "deploy"


ACTIONS = ["work", "deploy", "work2"]
RUN_ID = "run-gate"


# -- (a) Trigger Scope: Irreversible Only ------------------------------------


def test_reversible_actions_never_trigger_the_gate(tmp_path):
    # A sequence without irreversible actions passes through without interruption even with a gate.
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
    # An irreversible action with only an unresolved decision pauses before the action and registers it.
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    result = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # Only "work" ran; execution stopped before "deploy" (no side effects occurred).
    assert result.paused is True
    assert result.status == "paused"
    assert result.stop is None
    assert result.succeeded is False
    assert executed == ["work"]
    # The gate key uses the iteration at review time (after "work"@0, "deploy"@1), so "gate-1".
    assert result.pending["gate_key"] == "gate-1"
    assert result.pending["action"] == "deploy"
    assert "paused" in result.reason and "gate-1" in result.reason
    # The pending decision is persisted, and loop_gate(pending) remains in the journal.
    pendings = store.list_pending_decisions(RUN_ID)
    assert [p["gate_key"] for p in pendings] == ["gate-1"]
    gate_events = [e for e in store.read_events(RUN_ID) if e["kind"] == EVENT_GATE]
    assert gate_events and gate_events[-1]["payload"]["status"] == "pending"


# -- (b)+(c) Four Decisions Survive pause -> cross-connection resolve -> resume


def _resume_after(tmp_path, decision, payload=None):
    """Common flow: pause in run1 -> resolve on another connection -> resume in run2.

    Returns the actions executed in run2 plus the post-resume result and recorded steps.
    """
    db_path = tmp_path / "s.db"

    # --- run1: up to pause ---
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

    # --- A human records the decision on another connection (proof of cross-process persistence) ---
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-1", decision, payload)  # "deploy"@iter1

    # --- run2: resume (new gate / new executed list) ---
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
    # approve: "deploy" executes as-is, then the loop runs to completion and stops (no second pause).
    assert res.status == "stopped"
    assert executed == ["work", "deploy", "work2"]


def test_reject_skips_action_on_resume(tmp_path):
    executed, res, steps = _resume_after(tmp_path, "reject")
    # reject: "deploy" is not executed; the rejection is recorded as one step and the loop continues.
    assert res.status == "stopped"
    assert executed == ["work", "work2"]
    rejected = [s for s in steps if s.detail.startswith("human rejected")]
    assert len(rejected) == 1
    # The skipped step observation is hashable (compatible with the default NoProgress key).
    assert rejected[0].observation == "gate-skipped:rejected:gate-1"


def test_edit_executes_replacement_action_on_resume(tmp_path):
    executed, res, _ = _resume_after(tmp_path, "edit", payload="deploy-safe")
    # edit: the human-provided replacement action executes instead of "deploy".
    assert res.status == "stopped"
    assert executed == ["work", "deploy-safe", "work2"]


def test_respond_records_response_without_executing(tmp_path):
    executed, res, steps = _resume_after(tmp_path, "respond", payload="use staging")
    # respond: records the human response without executing, then continues. The response body appears in observation.
    assert res.status == "stopped"
    assert executed == ["work", "work2"]
    responded = [s for s in steps if s.detail.startswith("human responded")]
    assert len(responded) == 1
    assert responded[0].observation == "use staging"


def test_decision_is_not_re_asked_and_irreversible_runs_at_most_once(tmp_path):
    # A resolved decision does not pause again (the human is not asked twice). The approved
    # irreversible action executes only once on the first resume, then later replays skip it
    # as executed (at-most-once).
    db_path = tmp_path / "s.db"
    store = LoopStore(connect(db_path))
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)  # ensure the run row exists
    store.request_decision(RUN_ID, "gate-1", "deploy")  # "deploy"@iter1
    store.resolve_decision(RUN_ID, "gate-1", "approve")

    executions = []
    for _ in range(3):
        gather, act, executed = make_world(ACTIONS)
        gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
        res = run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate,
        )
        assert res.status == "stopped"  # does not pause again
        executions.append(executed)

    # First run: executes deploy. Later runs: skips it as executed (deploy does not run).
    assert executions[0] == ["work", "deploy", "work2"]
    assert executions[1] == ["work", "work2"]
    assert executions[2] == ["work", "work2"]


# -- Stop Condition Integration ---------------------------------------------


def test_skip_observation_is_hashable_for_noprogress(tmp_path):
    # The skipped step observation must not crash when combined with the default NoProgress
    # behavior (which hashes observations with Counter). An unhashable dict would raise
    # TypeError at the next guard.
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-1", "deploy")  # "deploy"@iter1
    store.resolve_decision(RUN_ID, "gate-1", "reject")

    gather, act, executed = make_world(ACTIONS)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    res = run_loop(
        act=act, verify=never_done,
        conditions=[NoProgress(window=3, repeat=3), MaxIterations(3)],
        gather=gather, gate=gate,
    )
    assert res.status == "stopped"
    assert executed == ["work", "work2"]


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


# -- Synchronous Resolver Mode (inline resolution without pausing) ------------


def test_custom_gate_bare_proceed_keeps_gathered_context(tmp_path):
    # Even if an arbitrary ActionGate implementation returns a bare proceed without context,
    # the action proposed by gather is passed to act (it does not turn into None). This guards
    # the public extension point.
    seen = []

    class EchoGate:
        def review(self, context, state):
            return GateReview(disposition=GATE_PROCEED)  # context not set

    def gather(state):
        return f"ctx@{state.iteration}"

    def act(action):
        seen.append(action)
        return ActOutcome(observation=action, tokens=0)

    run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(2)],
        gather=gather, gate=EchoGate(),
    )
    assert seen == ["ctx@0", "ctx@1"]  # gathered context, not None


def test_unknown_gate_disposition_fails_closed(tmp_path):
    # An invalid disposition (such as a typo) is rejected loudly instead of falling through
    # to proceed (= fail closed so irreversible actions are not executed by mistake).
    executed = []

    class BadGate:
        def review(self, context, state):
            return GateReview(disposition="paused")  # typo for GATE_PAUSE

    def act(action):
        executed.append(action)
        return ActOutcome(observation=action, tokens=0)

    with pytest.raises(ValueError, match="unknown disposition"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(2)],
            gather=lambda s: "x", gate=BadGate(),
        )
    assert executed == []  # action is not executed


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
    # Completes without pausing, and the resolver is called only for the irreversible action ("deploy"@iter1).
    assert result.status == "stopped"
    assert executed == ["work", "deploy", "work2"]
    assert seen == ["gate-1"]
    assert store.get_decision(RUN_ID, "gate-1")["decision"] == "approve"


def test_existing_resolution_is_honored_over_resolver(tmp_path):
    # An already resolved decision (= a competing resume winner resolved it first) applies the
    # stored decision without calling the resolver even when one is configured (re-check guarantee).
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-1", "deploy")
    store.resolve_decision(RUN_ID, "gate-1", "reject")  # another actor rejected first

    resolver_calls = []

    def resolver(pending):
        resolver_calls.append(pending["gate_key"])
        return Decision("approve")  # resolver wants to approve, but the stored reject wins

    gather, act, executed = make_world(ACTIONS)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, resolver=resolver)
    run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    assert resolver_calls == []  # resolver is not called because the decision is already settled
    assert executed == ["work", "work2"]  # reject is applied, so deploy is not executed
    assert store.get_decision(RUN_ID, "gate-1")["decision"] == "reject"


def test_run_gated_loop_forwards_initial_state(tmp_path):
    # Passing initial_state to run_gated_loop performs #14 resume from the interruption point
    # (not a replay from iteration 0, and it does not re-execute completed reversible actions).
    db_path = tmp_path / "s.db"
    actions = ["work", "deploy", "work2"]

    conn1 = connect(db_path)
    db1 = DBProgressLog(conn1, RUN_ID)
    g1, a1, ex1 = make_world(actions)
    res1 = run_gated_loop(
        act=a1, verify=never_done, conditions=[MaxIterations(3)],
        on=is_deploy, store=db1.store, run_id=RUN_ID, gather=g1,
        on_step=db1.on_step,
    )
    db1.record_result(res1)
    assert res1.paused and ex1 == ["work"]
    conn1.close()

    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-1", "approve")
    db2 = DBProgressLog(conn2, RUN_ID)
    g2, a2, ex2 = make_world(actions)
    res2 = run_gated_loop(
        act=a2, verify=never_done, conditions=[MaxIterations(3)],
        on=is_deploy, store=db2.store, run_id=RUN_ID, gather=g2,
        on_step=db2.on_step, initial_state=db2.state,
    )
    assert res2.status == "stopped"
    assert ex2 == ["deploy", "work2"]  # does not re-execute "work" (continues from interruption point)
    conn2.close()


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
    assert executed == ["work", "work2"]  # "deploy" is not executed because of reject


# -- Store Level: Decision Registry Idempotency and Validation ----------------


def test_request_decision_is_idempotent(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    first = store.request_decision(RUN_ID, "g1", {"do": "x"})
    second = store.request_decision(RUN_ID, "g1", {"do": "DIFFERENT"})
    # The second request returns the existing pending decision as-is and does not overwrite the action.
    assert first["id"] == second["id"]
    assert second["action"] == {"do": "x"}
    # There is only one loop_gate(pending) event (duplicate registration does not add events).
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
    assert store.list_pending_decisions(RUN_ID) == []  # no longer pending


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


def test_gated_action_must_be_json_native(tmp_path):
    # Gated actions must be JSON-native because they are used for identity comparison on resume.
    # Non-native values (which could collapse to repr and falsely match another action) are
    # rejected loudly at registration time.
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)

    class Obj:
        pass

    with pytest.raises(ValueError, match="JSON-native"):
        store.request_decision(RUN_ID, "g1", Obj())
    # Tuples are also rejected because they become lists and are lossy (prevents false matches).
    with pytest.raises(ValueError, match="JSON-native"):
        store.request_decision(RUN_ID, "g2", (1, 2))
    # str/dict/list/numbers pass through unchanged.
    assert store.request_decision(RUN_ID, "g3", {"cmd": "deploy"})["action"] == {"cmd": "deploy"}


def test_edit_payload_must_be_json_native(tmp_path):
    # Edit replacement actions are restored from the store and executed on resume, so
    # non-native values that would be lost in a JSON round trip (arbitrary objects) are
    # rejected loudly when recorded.
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")

    class Obj:  # non-JSON-native (collapses to repr)
        pass

    with pytest.raises(ValueError, match="JSON-native"):
        store.resolve_decision(RUN_ID, "g1", "edit", payload=Obj())
    # JSON-native replacement actions pass (dict/list/str/numbers).
    ok = store.resolve_decision(RUN_ID, "g1", "edit", payload={"cmd": "deploy", "safe": True})
    assert ok["payload"] == {"cmd": "deploy", "safe": True}


def test_edit_with_structured_payload_executes_on_resume(tmp_path):
    # A JSON-native structured edit payload is passed to act unchanged after resume (fidelity).
    db_path = tmp_path / "s.db"
    actions = ["deploy"]
    replacement = {"cmd": "deploy", "target": "staging"}

    conn1 = connect(db_path)
    store1 = LoopStore(conn1)
    gather1, act1, _ = make_world(actions)
    res1 = run_loop(
        act=act1, verify=never_done, conditions=[MaxIterations(1)],
        gather=gather1, gate=HumanGate(on=is_deploy, store=store1, run_id=RUN_ID),
    )
    assert res1.paused
    conn1.close()

    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-0", "edit", payload=replacement)
    gather2, act2, executed2 = make_world(actions)
    run_loop(
        act=act2, verify=never_done, conditions=[MaxIterations(1)],
        gather=gather2, gate=HumanGate(on=is_deploy, store=store2, run_id=RUN_ID),
    )
    assert executed2 == [replacement]  # structured replacement action executed unchanged


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
    # All four valid kinds can be constructed.
    for kind in DECISION_KINDS:
        assert Decision(kind).kind == kind


# -- (c) Multiple Gates: irreversible actions are exactly-once across resume ---


def is_deploy_prefix(action) -> bool:
    return isinstance(action, str) and action.startswith("deploy")


def test_multi_gate_resume_executes_each_irreversible_action_once(tmp_path):
    # Replay resume (without initial_state) with two irreversible actions. Replay starts from
    # iteration 0, but approved irreversible actions that have already executed are skipped as
    # executed and do not run twice (= core gate guarantee; prevents double deploy). Gate keys
    # are iteration-based, so deploy1@0 -> gate-0 and deploy2@2 -> gate-2.
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

    # run1: pause before deploy1 (gate-0).
    conn1 = connect(db_path)
    res1, ex1 = run_once(LoopStore(conn1))
    assert res1.paused and res1.pending["gate_key"] == "gate-0" and ex1 == []
    conn1.close()

    # gate-0 approve (another connection) -> run2: execute deploy1, then pause at deploy2 (gate-2).
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-0", "approve")
    res2, ex2 = run_once(store2)
    assert res2.paused and res2.pending["gate_key"] == "gate-2"
    assert ex2 == ["deploy1", "work"]  # deploy1 executes exactly once here
    conn2.close()

    # gate-2 approve -> run3: replays from iteration 0, but deploy1 is skipped as executed.
    conn3 = connect(db_path)
    store3 = LoopStore(conn3)
    store3.resolve_decision(RUN_ID, "gate-2", "approve")
    res3, ex3 = run_once(store3)
    assert res3.status == "stopped"
    # deploy1 is not re-executed; only work (replayed) and deploy2 run.
    assert ex3 == ["work", "deploy2"]
    assert "deploy1" not in ex3
    # Result: deploy1 and deploy2 each executed only once across the whole process.
    assert store3.get_decision(RUN_ID, "gate-0")["status"] == "executed"
    assert store3.get_decision(RUN_ID, "gate-2")["status"] == "executed"
    conn3.close()


def test_same_gate_instance_reused_across_resume(tmp_path):
    # Even when the same HumanGate instance is reused across pause->resume, the gate key is
    # derived from the iteration at review time (no instance state), so replay still maps
    # "deploy"@iter1 to gate-1 and does not miss the approval (no key drift).
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)

    gather1, act1, ex1 = make_world(ACTIONS)
    res1 = run_loop(
        act=act1, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather1, gate=gate,
    )
    assert res1.paused and res1.pending["gate_key"] == "gate-1" and ex1 == ["work"]

    store.resolve_decision(RUN_ID, "gate-1", "approve")

    # Resume with the same gate instance.
    gather2, act2, ex2 = make_world(ACTIONS)
    res2 = run_loop(
        act=act2, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather2, gate=gate,
    )
    assert res2.status == "stopped"
    assert ex2 == ["work", "deploy", "work2"]
    # No extra pending decision is added, and exactly one decision (gate-1) is executed.
    assert store.list_pending_decisions(RUN_ID) == []
    assert store.get_decision(RUN_ID, "gate-1")["status"] == "executed"
    assert store.get_decision(RUN_ID, "gate-0") is None


def test_multi_gate_resume_with_db_does_not_corrupt_step_history(tmp_path):
    # When resuming multiple gates with DBProgressLog, replay skips for already executed gates
    # must not overwrite and corrupt the original step rows (observation/tokens) from prior runs.
    db_path = tmp_path / "s.db"
    actions = ["deploy1", "work", "deploy2"]

    def run_once(conn):
        db = DBProgressLog(conn, RUN_ID)
        gather, act, _ = make_world(actions)

        def act_with_tokens(action):
            # Give irreversible actions visible tokens (they would become 0 if overwritten).
            tokens = 10 if action.startswith("deploy") else 1
            return ActOutcome(observation=action, tokens=tokens)

        gate = HumanGate(on=is_deploy_prefix, store=db.store, run_id=RUN_ID)
        res = run_loop(
            act=act_with_tokens, verify=never_done, conditions=[MaxIterations(3)],
            gather=gather, gate=gate, on_step=db.on_step,
        )
        db.record_result(res)
        return res

    conn1 = connect(db_path)
    assert run_once(conn1).paused  # pause at gate-0
    LoopStore(conn1).resolve_decision(RUN_ID, "gate-0", "approve")
    conn1.close()

    conn2 = connect(db_path)
    assert run_once(conn2).paused  # after executing deploy1, pause at gate-2 (deploy2@iter2)
    LoopStore(conn2).resolve_decision(RUN_ID, "gate-2", "approve")
    conn2.close()

    conn3 = connect(db_path)
    assert run_once(conn3).status == "stopped"
    conn3.close()

    # Persisted step history has deploy1 / work / deploy2 with the original observation/tokens
    # and has not been overwritten or corrupted by replay skip placeholders
    # (observation=gate-skipped..., tokens=0) (= fix for codex P1 step-history corruption).
    store = LoopStore(connect(db_path))
    steps = store.read_steps(RUN_ID)
    assert [s["observation"] for s in steps] == ["deploy1", "work", "deploy2"]
    assert [s["tokens"] for s in steps] == [10, 1, 10]
    # Persisted per-step values (audit source of truth) are complete. Their sum is the true cost.
    assert sum(s["tokens"] for s in steps) == 21
    # Note: on replay-resume, run-level tokens_used reflects the last run's in-memory total and
    # is not the sum across all runs (it does not re-add skipped deploy1). Full aggregate
    # restoration = loop-state restoration belongs to #14. The contract that step rows are the
    # source of truth is preserved.


def test_pending_re_asks_again_on_resume_without_resolution(tmp_path):
    # If a registered decision is still unresolved on resume, the loop pauses again with the
    # same gate_key (without double-registering pending, and with only one loop_gate(pending)).
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
    assert res2.paused and res2.pending["gate_key"] == "gate-1"
    assert executed2 == ["work"]
    assert [p["gate_key"] for p in store2.list_pending_decisions(RUN_ID)] == ["gate-1"]
    pending_events = [
        e for e in store2.read_events(RUN_ID)
        if e["kind"] == EVENT_GATE and e["payload"].get("status") == "pending"
    ]
    assert len(pending_events) == 1
    conn2.close()


def test_respond_response_reaches_next_gather(tmp_path):
    # The next gather can pick up the response recorded by respond via state.history[-1].
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-1", "deploy")  # "deploy"@iter1
    store.resolve_decision(RUN_ID, "gate-1", "respond", payload="use staging")

    seen_followup = []

    def gather(state):
        if state.history:
            last = state.history[-1]
            # Identify the respond step by detail and read the response body from observation.
            if last.detail.startswith("human responded"):
                seen_followup.append(last.observation)
                return f"follow-up:{last.observation}"
        return ACTIONS[state.iteration]

    def act(action):
        return ActOutcome(observation=action, tokens=0)

    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID)
    run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # On the iteration immediately after respond is skipped, gather can read the response.
    assert seen_followup == ["use staging"]


# -- Gate and #14 initial_state resume interaction ---------------------------


def test_gate_resume_via_initial_state_honors_decision_and_continues(tmp_path):
    # The #14 initial_state resume path (continue from the interruption point) preserves gate
    # decisions and does not re-execute completed reversible actions. Because gate keys are
    # iteration-based, restoring initial_state.iteration maps back to the interrupted gate.
    db_path = tmp_path / "s.db"
    actions = ["work", "deploy", "work2"]

    # run1: persist steps while pausing at deploy (gate-1).
    conn1 = connect(db_path)
    db1 = DBProgressLog(conn1, RUN_ID)
    g1, a1, ex1 = make_world(actions)
    res1 = run_loop(
        act=a1, verify=never_done, conditions=[MaxIterations(3)],
        gather=g1, gate=HumanGate(on=is_deploy, store=db1.store, run_id=RUN_ID),
        on_step=db1.on_step,
    )
    db1.record_result(res1)
    assert res1.paused and res1.pending["gate_key"] == "gate-1" and ex1 == ["work"]
    conn1.close()

    # Human approves.
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-1", "approve")

    # run2: #14 resume with initial_state=db.state continues from the interruption point (iter1).
    db2 = DBProgressLog(conn2, RUN_ID)
    assert db2.state.iteration == 1 and [r.observation for r in db2.state.history] == ["work"]
    g2, a2, ex2 = make_world(actions)
    res2 = run_loop(
        act=a2, verify=never_done, conditions=[MaxIterations(3)],
        gather=g2, gate=HumanGate(on=is_deploy, store=db2.store, run_id=RUN_ID),
        on_step=db2.on_step, initial_state=db2.state,
    )
    db2.record_result(res2)
    # Continue from the interruption point: "work" is **not re-executed**; only deploy (approved) and work2 run.
    assert res2.status == "stopped"
    assert ex2 == ["deploy", "work2"]
    assert store2.get_decision(RUN_ID, "gate-1")["status"] == "executed"
    # Persisted step history contains work / deploy / work2, three iterations total.
    steps = store2.read_steps(RUN_ID)
    assert [s["observation"] for s in steps] == ["work", "deploy", "work2"]
    conn2.close()


def test_multi_gate_resume_via_initial_state_aligns_keys(tmp_path):
    # Even when multiple gates resume via initial_state, iteration-based keys map back to the
    # interrupted gate correctly (a seq-based scheme would drift when resuming past executed gates).
    db_path = tmp_path / "s.db"
    actions = ["deploy1", "work", "deploy2"]  # deploy1@0 -> gate-0, deploy2@2 -> gate-2

    def resume_leg(conn):
        db = DBProgressLog(conn, RUN_ID)
        g, a, ex = make_world(actions)
        res = run_loop(
            act=a, verify=never_done, conditions=[MaxIterations(3)],
            gather=g, gate=HumanGate(on=is_deploy_prefix, store=db.store, run_id=RUN_ID),
            on_step=db.on_step, initial_state=db.state,
        )
        db.record_result(res)
        return res, ex

    # leg1: pause at deploy1 (gate-0).
    conn1 = connect(db_path)
    res1, ex1 = resume_leg(conn1)
    assert res1.paused and res1.pending["gate_key"] == "gate-0" and ex1 == []
    LoopStore(conn1).resolve_decision(RUN_ID, "gate-0", "approve")
    conn1.close()

    # leg2: execute deploy1 -> pause at deploy2 (gate-2). initial_state is iter0 (deploy1 not executed).
    conn2 = connect(db_path)
    res2, ex2 = resume_leg(conn2)
    assert res2.paused and res2.pending["gate_key"] == "gate-2"
    assert ex2 == ["deploy1", "work"]
    LoopStore(conn2).resolve_decision(RUN_ID, "gate-2", "approve")
    conn2.close()

    # leg3: initial_state is iter2 (deploy1/work done). Do not revisit deploy1; execute only deploy2.
    conn3 = connect(db_path)
    res3, ex3 = resume_leg(conn3)
    assert res3.status == "stopped"
    assert ex3 == ["deploy2"]  # execute interrupted gate deploy2 exactly once; do not revisit deploy1
    store3 = LoopStore(conn3)
    assert store3.get_decision(RUN_ID, "gate-0")["status"] == "executed"
    assert store3.get_decision(RUN_ID, "gate-2")["status"] == "executed"
    steps = store3.read_steps(RUN_ID)
    assert [s["observation"] for s in steps] == ["deploy1", "work", "deploy2"]
    conn3.close()


# -- DBProgressLog integration: record_result accepts paused results without crashing


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
        db.record_result(result)  # must not crash on a CHECK constraint
    # pause is not terminal: the run remains running, and stop_reason is not written.
    store = LoopStore(connect(tmp_path / "s.db"))
    assert store.get_run(RUN_ID)["status"] == "running"
    assert store.get_stop_reason(RUN_ID) is None
    paused_events = [
        e for e in store.read_events(RUN_ID)
        if e["kind"] == EVENT_GATE and e["payload"].get("status") == "paused"
    ]
    assert len(paused_events) == 1


# -- Defensive Guards / Surrounding API --------------------------------------


def test_resume_with_diverged_action_is_rejected(tmp_path):
    # If the proposed sequence shifts between resumes and a different irreversible action gets
    # the same gate_key, reject loudly because it does not match the recorded action (do not
    # silently allow misapplication).
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-0", "deploy-A")
    store.resolve_decision(RUN_ID, "gate-0", "approve")

    # Resume in a world where a different action, "deploy-B", arrives at the same gate-0.
    gather, act, _ = make_world(["deploy-B"])
    gate = HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    with pytest.raises(ValueError, match="does not match"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(1)],
            gather=gather, gate=gate,
        )


def test_resume_with_diverged_action_on_executed_gate_is_rejected(tmp_path):
    # If a *different* irreversible action arrives at the same key for an already executed gate
    # because the proposal sequence shifted, reject loudly instead of silently skipping (do not
    # suppress a new irreversible action).
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-0", "deploy-A")
    store.resolve_decision(RUN_ID, "gate-0", "approve")
    store.claim_execution(RUN_ID, "gate-0")  # mark as already executed

    gather, act, _ = make_world(["deploy-B"])
    gate = HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    with pytest.raises(ValueError, match="does not match"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(1)],
            gather=gather, gate=gate,
        )


def test_resume_with_diverged_action_on_pending_gate_is_rejected(tmp_path):
    # If a pending decision registered in a previous run (action="deploy-A") is encountered as
    # another action, "deploy-B", on a shifted resume with a resolver, reject loudly to prevent
    # the resolver from approving the old pending decision and executing the current different action.
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    HumanGate(on=is_deploy_prefix, store=store, run_id=RUN_ID)
    store.request_decision(RUN_ID, "gate-0", "deploy-A")  # unresolved pending decision

    executed = []

    def gather(state):
        return "deploy-B"

    def act(action):
        executed.append(action)
        return ActOutcome(observation=action, tokens=0)

    gate = HumanGate(
        on=is_deploy_prefix, store=store, run_id=RUN_ID,
        resolver=lambda pending: Decision("approve"),
    )
    with pytest.raises(ValueError, match="does not match"):
        run_loop(
            act=act, verify=never_done, conditions=[MaxIterations(1)],
            gather=gather, gate=gate,
        )
    assert executed == []  # different action is not executed


def test_resolver_must_return_a_decision(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(ACTIONS)
    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID,
        resolver=lambda pending: "approve",  # raw string instead of Decision
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


def test_claim_execution_is_single_winner(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    store.request_decision(RUN_ID, "g1", "deploy")
    # Unresolved decisions cannot claim execution rights.
    with pytest.raises(ValueError, match="cannot mark unresolved"):
        store.claim_execution(RUN_ID, "g1")
    store.resolve_decision(RUN_ID, "g1", "approve")
    # The first claim is the winner (True) and transitions to executed.
    assert store.claim_execution(RUN_ID, "g1") is True
    row = store.get_decision(RUN_ID, "g1")
    assert row["status"] == "executed" and row["executed_at"] is not None
    # Subsequent claims lose (False): double execution is not allowed.
    assert store.claim_execution(RUN_ID, "g1") is False
    assert store.claim_execution(RUN_ID, "g1") is False


def test_claim_execution_rejects_non_executable_decisions(tmp_path):
    # reject/respond do not execute actions, so even direct store API calls must not transition
    # them to executed (a bad transition would make later resumes skip rejection/response records
    # and corrupt state).
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    for key, kind in (("gr", "reject"), ("gp", "respond")):
        store.request_decision(RUN_ID, key, "deploy")
        store.resolve_decision(RUN_ID, key, kind, payload=("msg" if kind == "respond" else None))
        with pytest.raises(ValueError, match="not executable"):
            store.claim_execution(RUN_ID, key)
        # status remains resolved (it has not turned into executed).
        assert store.get_decision(RUN_ID, key)["status"] == "resolved"


def test_claim_execution_single_winner_across_connections(tmp_path):
    # Even with claims from another connection (= simulating concurrent resumes), there is only one winner.
    db_path = tmp_path / "s.db"
    store_a = LoopStore(connect(db_path))
    store_a.load_or_init(RUN_ID)
    store_a.request_decision(RUN_ID, "g1", "deploy")
    store_a.resolve_decision(RUN_ID, "g1", "approve")

    store_b = LoopStore(connect(db_path))
    assert store_a.claim_execution(RUN_ID, "g1") is True
    assert store_b.claim_execution(RUN_ID, "g1") is False  # loser does not execute
