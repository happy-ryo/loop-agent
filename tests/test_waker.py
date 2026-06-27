"""ループ wake と transport 配送の配線検証 (Issue #23, report.md S5 Phase3)。

loop 完了 / 次反復 / 判断要求の 3 wake が正しく組み立てられ、push 一次でも backend 不通の
pull fallback でも配送が継続することを、``run_loop`` の実結果と結んで実証する。
"""

from __future__ import annotations

from claude_loop import (
    ActOutcome,
    InMemoryWakeQueue,
    LoopWaker,
    LoopStore,
    MaxIterations,
    NullPushBackend,
    Transport,
    WAKE_DECISION_REQUEST,
    WAKE_LOOP_DONE,
    WAKE_NEXT_ITERATION,
    connect,
    run_gated_loop,
    run_loop,
)
from claude_loop.waker import wake_id_for, wakes_for_result

from conftest import ManualClock, acting, done_after, never_done


def _transport(backend=None) -> Transport:
    return Transport(InMemoryWakeQueue(), backend, time_fn=ManualClock())


# -- wakes_for_result (純粋写像) ---------------------------------------------


def test_completed_run_yields_done_wake():
    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])
    wakes = wakes_for_result(result, run_id="r1", recipient="coordinator")
    assert len(wakes) == 1
    assert wakes[0].kind == WAKE_LOOP_DONE
    assert wakes[0].payload["succeeded"] is True
    assert wakes[0].recipient == "coordinator"


def test_completed_run_with_next_recipient_adds_next_iteration_wake():
    result = run_loop(act=acting(), verify=never_done, conditions=[MaxIterations(2)])
    wakes = wakes_for_result(
        result, run_id="r1", recipient="coordinator", next_recipient="planner"
    )
    kinds = {w.kind: w for w in wakes}
    assert set(kinds) == {WAKE_LOOP_DONE, WAKE_NEXT_ITERATION}
    assert kinds[WAKE_NEXT_ITERATION].recipient == "planner"
    # 完了 wake は status=stopped を運ぶ (MaxIterations で停止)。
    assert kinds[WAKE_LOOP_DONE].payload["status"] == "stopped"


def test_paused_run_yields_decision_request_only(tmp_path):
    """人間ゲートで paused した run は判断要求 wake だけを出す (次反復は出さない)。"""
    store = LoopStore(connect(tmp_path / "s.db"))

    def gather(state):
        return ["work", "deploy"][state.iteration]

    def act(action):
        return ActOutcome(observation=action, tokens=0)

    result = run_gated_loop(
        act=act,
        verify=never_done,
        conditions=[MaxIterations(3)],
        gather=gather,
        on=lambda a: a == "deploy",
        store=store,
        run_id="rp",
    )
    assert result.paused
    wakes = wakes_for_result(
        result, run_id="rp", recipient="human", next_recipient="planner"
    )
    assert len(wakes) == 1
    assert wakes[0].kind == WAKE_DECISION_REQUEST
    assert "gate_key" in wakes[0].payload


# -- LoopWaker drop-in 配線 --------------------------------------------------


def test_loopwaker_delivers_via_push_when_backend_up():
    pushed = []
    from claude_loop import CallablePushBackend

    backend = CallablePushBackend(lambda w: (pushed.append(w.id), True)[1])
    t = _transport(backend)
    waker = LoopWaker(t, run_id="r1", recipient="coordinator")

    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])
    routes = waker.record_result(result)

    assert all(r == "push" for r in routes.values())
    assert pushed  # push 経路で配送された
    assert t.poll("coordinator") == []  # pull には残らない


def test_loopwaker_pull_fallback_when_backend_down():
    """backend 不通でも LoopWaker の wake は pull fallback で受信側に届く (中核条件)。"""
    t = _transport(NullPushBackend())
    waker = LoopWaker(
        t, run_id="r1", recipient="coordinator", next_recipient="planner"
    )

    result = run_loop(act=acting(), verify=never_done, conditions=[MaxIterations(2)])
    routes = waker.record_result(result)
    assert all(r == "queued" for r in routes.values())  # push 全失敗

    # coordinator は完了 wake を pull で受け取る。
    done = t.poll("coordinator")
    assert [w.kind for w in done] == [WAKE_LOOP_DONE]
    # planner は次反復 wake を pull で受け取る。
    nxt = t.poll("planner")
    assert [w.kind for w in nxt] == [WAKE_NEXT_ITERATION]


def test_loopwaker_redeliver_is_idempotent_across_resume():
    """同一 run の再 record_result (resume 想定) は決定的 id で de-dup され二重配送しない。"""
    t = _transport(NullPushBackend())
    waker = LoopWaker(t, run_id="r1", recipient="coordinator")
    result = run_loop(act=acting(), verify=done_after(1), conditions=[MaxIterations(5)])

    waker.record_result(result)
    waker.record_result(result)  # resume 等で再配送指示

    assert len(t.poll("coordinator")) == 1  # 二重に届かない


def test_wake_id_is_deterministic():
    assert wake_id_for("r1", WAKE_LOOP_DONE, 3) == "r1:loop_done:3"
