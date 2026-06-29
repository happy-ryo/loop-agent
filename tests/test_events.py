"""イベントモデルと sink のテスト（report.md S4.5「観測性」）。

LoopEvent の畳み込み、ListSink / CallableSink / JsonlEventSink の振る舞い、
JSONL の round-trip（journal 風 sink の耐久性: クラッシュ末尾の許容・Unicode）、
best-effort な fan-out（sink 例外でループを殺さない）を押さえる。
"""

from __future__ import annotations

import json

import pytest

from loop_agent import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    CallableSink,
    JsonlEventSink,
    ListSink,
    LoopEvent,
    read_events,
)
from loop_agent.events import EventSink, _jsonable, fan_out


def test_loop_event_to_dict_flattens_payload():
    ev = LoopEvent(
        kind=LOOP_STEP,
        iteration=2,
        elapsed=1.5,
        payload={"tokens": 10, "goal_met": False},
    )
    assert ev.to_dict() == {
        "kind": LOOP_STEP,
        "iteration": 2,
        "elapsed": 1.5,
        "tokens": 10,
        "goal_met": False,
    }


def test_event_kind_constants_match_task_names():
    assert (LOOP_BEGIN, LOOP_STEP, LOOP_END) == (
        "loop_begin",
        "loop_step",
        "loop_end",
    )


# -- sinks ------------------------------------------------------------------


def test_list_sink_collects_and_filters_by_kind():
    sink = ListSink()
    begin = LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0)
    step = LoopEvent(kind=LOOP_STEP, iteration=0, elapsed=0.1)
    sink.emit(begin)
    sink.emit(step)
    assert sink.events == [begin, step]
    assert sink.of_kind(LOOP_BEGIN) == [begin]
    assert sink.of_kind(LOOP_STEP) == [step]


def test_sinks_satisfy_event_sink_protocol(tmp_path):
    assert isinstance(ListSink(), EventSink)
    assert isinstance(CallableSink(lambda d: None), EventSink)
    assert isinstance(JsonlEventSink(tmp_path / "e.jsonl"), EventSink)


def test_callable_sink_forwards_dict():
    seen = []
    sink = CallableSink(seen.append)
    sink.emit(LoopEvent(kind=LOOP_END, iteration=3, elapsed=2.0, payload={"status": "stopped"}))
    assert seen == [{"kind": LOOP_END, "iteration": 3, "elapsed": 2.0, "status": "stopped"}]


# -- JSONL sink: durability / round-trip ------------------------------------


def test_jsonl_sink_round_trips_events_in_order(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0, payload={"conditions": ["max_iterations"]}))
    sink.emit(LoopEvent(kind=LOOP_STEP, iteration=0, elapsed=0.1, payload={"tokens_used": 10}))
    sink.emit(LoopEvent(kind=LOOP_END, iteration=1, elapsed=0.2, payload={"status": "goal_met"}))

    records = read_events(path)
    assert [r["kind"] for r in records] == [LOOP_BEGIN, LOOP_STEP, LOOP_END]
    assert records[0]["conditions"] == ["max_iterations"]
    assert records[1]["tokens_used"] == 10
    assert records[2]["status"] == "goal_met"


def test_jsonl_sink_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / "events.jsonl"
    JsonlEventSink(path).emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0))
    assert path.exists()
    assert read_events(path)[0]["kind"] == LOOP_BEGIN


def test_jsonl_sink_flushes_each_event(tmp_path):
    # 各 emit の直後にファイルへ反映されている（バッファ溜め込みでない）こと。
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0))
    assert len(read_events(path)) == 1
    sink.emit(LoopEvent(kind=LOOP_STEP, iteration=0, elapsed=0.1))
    assert len(read_events(path)) == 2


def test_read_events_tolerates_truncated_final_line(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0))
    sink.emit(LoopEvent(kind=LOOP_STEP, iteration=0, elapsed=0.1))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "loop_step", "iteration": 1')  # 改行なしの部分行

    records = read_events(path)
    assert [r["kind"] for r in records] == [LOOP_BEGIN, LOOP_STEP]


def test_read_events_raises_on_corrupt_complete_final_line(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{corrupt but terminated}\n")  # 改行終端された壊れ行は本物の破損

    with pytest.raises(json.JSONDecodeError):
        read_events(path)


def test_read_events_missing_file_returns_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == []


def test_jsonl_unicode_round_trips_as_utf8(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit(
        LoopEvent(kind=LOOP_END, iteration=1, elapsed=0.0, payload={"reason": "収束しました"})
    )
    raw = path.read_text(encoding="utf-8")
    assert "収束しました" in raw  # ASCII エスケープされない
    assert read_events(path)[0]["reason"] == "収束しました"


def test_jsonl_unicode_line_separators_do_not_split_a_record(tmp_path):
    # U+2028/U+2029/U+0085 は値中にそのまま出るが record の framing ではない。
    path = tmp_path / "events.jsonl"
    nasty = "stuck here andthere"
    sink = JsonlEventSink(path)
    sink.emit(LoopEvent(kind=LOOP_STEP, iteration=0, elapsed=0.0, payload={"detail": nasty}))
    sink.emit(LoopEvent(kind=LOOP_END, iteration=1, elapsed=0.0, payload={"status": "stopped"}))

    records = read_events(path)
    assert records[0]["detail"] == nasty
    assert len(records) == 2  # 末尾 record は裂けず無事


def test_jsonl_sink_writes_strict_json_for_non_finite_floats(tmp_path):
    path = tmp_path / "events.jsonl"
    JsonlEventSink(path).emit(
        LoopEvent(
            kind=LOOP_STEP,
            iteration=0,
            elapsed=float("inf"),
            payload={"nan": float("nan"), "nested": [float("-inf")]},
        )
    )

    raw = path.read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert "Infinity" not in raw
    records = read_events(path)
    assert records[0]["elapsed"] == "inf"
    assert records[0]["nan"] == "nan"
    assert records[0]["nested"] == ["-inf"]


# -- jsonable coercion ------------------------------------------------------


def test_jsonable_coerces_non_serializable_to_repr():
    class Widget:
        def __repr__(self):
            return "Widget(x)"

    assert _jsonable(Widget()) == "Widget(x)"
    assert _jsonable([1, Widget()]) == [1, "Widget(x)"]
    assert _jsonable({"k": Widget()}) == {"k": "Widget(x)"}
    assert _jsonable({1: "v"}) == {"1": "v"}  # 非 str キーは str 化


# -- best-effort fan-out ----------------------------------------------------


def test_fan_out_isolates_a_failing_sink_with_a_warning():
    good = ListSink()

    class Boom:
        def emit(self, event):
            raise RuntimeError("sink down")

    ev = LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0)
    with pytest.warns(RuntimeWarning, match="failed to emit"):
        fan_out((Boom(), good), ev)
    # 壊れた sink があっても後続 sink には届く。
    assert good.events == [ev]


def test_fan_out_custom_error_handler_overrides_warning():
    seen = []

    class Boom:
        def emit(self, event):
            raise ValueError("nope")

    def handler(sink, event, exc):
        seen.append((type(sink).__name__, event.kind, type(exc).__name__))

    fan_out((Boom(),), LoopEvent(kind=LOOP_END, iteration=0, elapsed=0.0), on_error=handler)
    assert seen == [("Boom", LOOP_END, "ValueError")]
