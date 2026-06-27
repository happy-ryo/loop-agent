"""観測オーケストレーションのテスト（report.md S5 Phase 2 成功条件 (b)）。

中核要件を押さえる:
- 全終了理由（goal_met / max_iterations / token_budget / timeout）が loop_end
  event に残ること、
- メトリクス（反復番号・累積トークン・elapsed）が begin→step→end で追えること、
- begin/step/end の順序と件数、
- ループ本体が例外で抜けても error の loop_end が残ること、
- 利用者の on_step と観測フックが合成されること、
- OTel 無効時（otel=False）でも sink 観測がそのまま機能すること（degrade）。
"""

from __future__ import annotations

import pytest

from claude_loop import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    ActOutcome,
    JsonlEventSink,
    ListSink,
    LoopObserver,
    LoopState,
    MaxIterations,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    read_events,
    run_loop,
    run_observed_loop,
)
from conftest import ManualClock, acting, done_after, never_done, stepping_for


def _kinds(sink):
    return [e.kind for e in sink.events]


def _only(sink, kind):
    evs = sink.of_kind(kind)
    assert len(evs) == 1, f"expected exactly one {kind}, got {len(evs)}"
    return evs[0]


# -- begin / step / end の骨格 ----------------------------------------------


def test_emits_begin_steps_end_in_order(tmp_path):
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        otel=False,
    )
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_STEP, LOOP_END]


def test_begin_carries_condition_names():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=0),
        verify=done_after(1),
        conditions=[MaxIterations(5), TokenBudget(100)],
        sinks=[sink],
        otel=False,
    )
    begin = _only(sink, LOOP_BEGIN)
    assert begin.payload["conditions"] == ["max_iterations", "token_budget"]


def test_zero_iteration_run_still_emits_begin_and_end():
    # MaxIterations(0) は即時停止: step は無いが begin/end は必ず残る。
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(0)],
        sinks=[sink],
        otel=False,
    )
    assert result.iterations == 0
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_END]
    assert _only(sink, LOOP_END).payload["status"] == "stopped"


# -- 全終了理由が loop_end に残る -------------------------------------------


def test_goal_met_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=1),
        verify=done_after(2),
        conditions=[MaxIterations(10)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "goal_met"
    assert end.payload["stop"] is None
    assert end.payload["goal_met"] is True
    assert end.payload["reason"] == "goal met"
    assert end.payload["iterations"] == 2


def test_max_iterations_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=5),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "max_iterations"
    assert "max iterations" in end.payload["reason"]


def test_token_budget_reason_in_end_event():
    sink = ListSink()
    run_observed_loop(
        act=acting(tokens=40),
        verify=never_done,
        conditions=[TokenBudget(100), MaxIterations(100)],
        sinks=[sink],
        otel=False,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "token_budget"
    assert "token budget" in end.payload["reason"]


def test_timeout_reason_in_end_event():
    clock = ManualClock()
    sink = ListSink()
    run_observed_loop(
        act=stepping_for(clock, seconds=1.0, tokens=0),
        verify=never_done,
        conditions=[Timeout(3.0), MaxIterations(100)],
        sinks=[sink],
        otel=False,
        time_fn=clock,
    )
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "stopped"
    assert end.payload["stop"] == "timeout"
    assert "timed out" in end.payload["reason"]


@pytest.mark.parametrize(
    "conditions, act, verify, expected_stop",
    [
        ([MaxIterations(10)], acting(tokens=1), done_after(2), None),
        ([MaxIterations(3)], acting(tokens=5), never_done, "max_iterations"),
        ([TokenBudget(50), MaxIterations(99)], acting(tokens=25), never_done, "token_budget"),
    ],
)
def test_all_non_timeout_terminations_recorded(
    conditions, act, verify, expected_stop
):
    # 1 つのパラメタ表で「全終了理由が end に残る」を網羅的に確認する。
    sink = ListSink()
    result = run_observed_loop(
        act=act, verify=verify, conditions=conditions, sinks=[sink], otel=False
    )
    end = _only(sink, LOOP_END)
    assert end.payload["stop"] == expected_stop
    assert end.payload["reason"] == result.reason
    assert end.payload["status"] == result.status


# -- メトリクスが begin→step→end で追える ----------------------------------


def test_metrics_are_traceable_across_events():
    sink = ListSink()
    result = run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(4)],
        sinks=[sink],
        otel=False,
    )
    steps = sink.of_kind(LOOP_STEP)
    # 反復番号は 0..3、累積トークンは単調増加で 10,20,30,40。
    assert [s.iteration for s in steps] == [0, 1, 2, 3]
    assert [s.payload["tokens_used"] for s in steps] == [10, 20, 30, 40]
    assert all(s.payload["tokens"] == 10 for s in steps)
    # elapsed は非減少。
    elapsed = [s.elapsed for s in steps]
    assert elapsed == sorted(elapsed)
    # end の集計はループ結果と一致し、最後の step の累積と整合する。
    end = _only(sink, LOOP_END)
    assert end.payload["iterations"] == result.iterations == 4
    assert end.payload["tokens_used"] == steps[-1].payload["tokens_used"] == 40


def test_step_event_carries_observation_and_detail():
    sink = ListSink()

    def verify(_outcome):
        return VerifyOutcome(goal_met=True, detail="done!")

    run_observed_loop(
        act=acting(tokens=0, observation={"k": "v"}),
        verify=verify,
        conditions=[MaxIterations(5)],
        sinks=[sink],
        otel=False,
    )
    step = sink.of_kind(LOOP_STEP)[0]
    assert step.payload["observation"] == {"k": "v"}
    assert step.payload["detail"] == "done!"
    assert step.payload["goal_met"] is True


def test_non_serializable_observation_stored_as_repr():
    sink = ListSink()

    class Widget:
        def __repr__(self):
            return "Widget(z)"

    def act(_ctx):
        return ActOutcome(observation=Widget(), tokens=0)

    run_observed_loop(
        act=act, verify=never_done, conditions=[MaxIterations(1)], sinks=[sink], otel=False
    )
    assert sink.of_kind(LOOP_STEP)[0].payload["observation"] == "Widget(z)"


# -- 複数 sink / JSONL からの事後解析 --------------------------------------


def test_events_persist_to_jsonl_for_post_hoc_analysis(tmp_path):
    path = tmp_path / "events.jsonl"
    mem = ListSink()
    run_observed_loop(
        act=acting(tokens=7),
        verify=never_done,
        conditions=[MaxIterations(2)],
        sinks=[JsonlEventSink(path), mem],
        otel=False,
    )
    # 両 sink に同じイベント列が届く。
    on_disk = read_events(path)
    assert [r["kind"] for r in on_disk] == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    assert [e.kind for e in mem.events] == [r["kind"] for r in on_disk]
    assert on_disk[-1]["stop"] == "max_iterations"
    assert on_disk[-1]["tokens_used"] == 14


# -- 利用者 on_step との合成 ------------------------------------------------


def test_user_on_step_is_composed_with_observer():
    sink = ListSink()
    seen = []

    run_observed_loop(
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
        sinks=[sink],
        on_step=lambda record, state: seen.append(record.iteration),
        otel=False,
    )
    assert seen == [0, 1, 2]  # 利用者フックも各反復で呼ばれる
    assert len(sink.of_kind(LOOP_STEP)) == 3  # 観測フックも生きている


# -- 例外パス: error の loop_end ------------------------------------------


def test_exception_in_act_records_error_end_and_reraises():
    sink = ListSink()

    def boom(_ctx):
        raise ValueError("act exploded")

    with pytest.raises(ValueError, match="act exploded"):
        run_observed_loop(
            act=boom,
            verify=never_done,
            conditions=[MaxIterations(3)],
            sinks=[sink],
            otel=False,
        )
    # begin は出ており、end は error として残り、例外は伝播する。
    assert sink.of_kind(LOOP_BEGIN)
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"
    assert "ValueError" in end.payload["reason"]
    assert end.payload["goal_met"] is False


def test_error_end_keeps_metrics_of_completed_iterations():
    # 2 反復成功した後に落ちると、error の loop_end は 0 ではなく確定済みの
    # 累積メトリクス（iterations=2 / tokens_used=20）を残す。
    sink = ListSink()
    calls = {"n": 0}

    def act(_ctx):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ValueError("boom on third")
        return ActOutcome(observation="ok", tokens=10)

    with pytest.raises(ValueError, match="boom on third"):
        run_observed_loop(
            act=act,
            verify=never_done,
            conditions=[MaxIterations(10)],
            sinks=[sink],
            otel=False,
        )
    assert len(sink.of_kind(LOOP_STEP)) == 2
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "error"
    assert end.payload["iterations"] == 2
    assert end.payload["tokens_used"] == 20
    assert end.iteration == 2  # 共通フィールドも確定値


def test_incomplete_path_emits_loop_end_with_last_known_metrics():
    # context manager を例外なしで抜けたが record_result を呼び忘れたケース:
    # span と event sink の終了観測を揃えるため incomplete の loop_end を残す。
    sink = ListSink()
    observer = LoopObserver([sink], otel=False)
    with observer:
        run_loop(
            act=acting(tokens=5),
            verify=never_done,
            conditions=[MaxIterations(2)],
            on_step=observer.on_step,
        )
        # わざと record_result を呼ばない
    end = _only(sink, LOOP_END)
    assert end.payload["status"] == "incomplete"
    assert end.payload["iterations"] == 2  # 確定済みメトリクスを保持
    assert end.payload["tokens_used"] == 10
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]


# -- 手動配線（ProgressLog と同じ作法）-------------------------------------


def test_manual_wiring_matches_run_observed_loop():
    sink = ListSink()
    observer = LoopObserver([sink], conditions=[MaxIterations(2)], otel=False)
    with observer:
        result = run_loop(
            act=acting(tokens=3),
            verify=never_done,
            conditions=[MaxIterations(2)],
            on_step=observer.on_step,
        )
        observer.record_result(result)
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    assert _only(sink, LOOP_END).payload["tokens_used"] == 6


def test_run_observed_loop_forwards_initial_state_for_resume():
    # 観測入口でも initial_state を素通しして resume できる: 復元 seed から step/end の
    # iteration・累積メトリクスが継続する (新規 run の begin は iteration 0 から)。
    sink = ListSink()
    seed = LoopState(iteration=2, tokens_used=20)
    result = run_observed_loop(
        act=acting(tokens=10),
        verify=never_done,
        conditions=[MaxIterations(4)],
        sinks=[sink],
        otel=False,
        initial_state=seed,
    )
    # seed の iteration 2 から継続 -> 2 step 回して cap 4 で停止。
    assert result.iterations == 4
    assert result.tokens_used == 40
    assert _kinds(sink) == [LOOP_BEGIN, LOOP_STEP, LOOP_STEP, LOOP_END]
    # step event の iteration は復元 state から継続 (2, 3)。
    assert [e.iteration for e in sink.of_kind(LOOP_STEP)] == [2, 3]
    assert _only(sink, LOOP_END).payload["iterations"] == 4
    assert _only(sink, LOOP_END).payload["tokens_used"] == 40
    # seed は mutate されない (run_loop が copy する)。
    assert seed.iteration == 2 and seed.tokens_used == 20


def test_record_result_is_idempotent():
    sink = ListSink()
    observer = LoopObserver([sink], otel=False)
    observer.begin()
    result = run_loop(
        act=acting(tokens=0), verify=done_after(1), conditions=[MaxIterations(2)]
    )
    observer.record_result(result)
    observer.record_result(result)  # 二度目は無視される
    assert len(sink.of_kind(LOOP_END)) == 1
