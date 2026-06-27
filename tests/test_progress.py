"""Tests for the minimal external state: the append-only progress file.

These cover report.md S5 Phase 1's "最小状態(progress ファイル)": every iteration
leaves a durable record, the terminal verdict is recorded, and the file reads
back faithfully -- including the awkward cases (non-serializable observations,
a crash-truncated final line, Unicode) that a real run will eventually hit.
"""

from __future__ import annotations

import json

import pytest

from claude_loop import (
    ActOutcome,
    MaxIterations,
    ProgressLog,
    VerifyOutcome,
    read_progress,
    run_loop,
)
from conftest import acting, done_after, never_done


def _run_with_progress(path, *, act, verify, conditions, on_step=None):
    """Run a loop wired to a ProgressLog and record the terminal verdict."""
    progress = ProgressLog(path)

    if on_step is None:
        observer = progress.on_step
    else:

        def observer(record, state):
            progress.on_step(record, state)
            on_step(record, state)

    result = run_loop(
        act=act, verify=verify, conditions=conditions, on_step=observer
    )
    progress.record_result(result)
    return result, progress


def _steps(records):
    return [r for r in records if r["kind"] == "step"]


def _results(records):
    return [r for r in records if r["kind"] == "result"]


# -- per-iteration recording ------------------------------------------------


def test_every_iteration_is_recorded_in_order(tmp_path):
    path = tmp_path / "progress.jsonl"
    result, _ = _run_with_progress(
        path,
        act=acting(tokens=10, observation="work"),
        verify=never_done,
        conditions=[MaxIterations(5)],
    )

    steps = _steps(read_progress(path))
    assert len(steps) == result.iterations == 5
    # iterations are 0..4 in order, cumulative tokens grow monotonically.
    assert [s["iteration"] for s in steps] == [0, 1, 2, 3, 4]
    assert [s["tokens_used"] for s in steps] == [10, 20, 30, 40, 50]
    assert all(s["tokens"] == 10 for s in steps)
    assert all(s["observation"] == "work" for s in steps)
    assert all(s["goal_met"] is False for s in steps)


def test_terminal_result_is_recorded_for_a_capped_run(tmp_path):
    path = tmp_path / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=30),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )

    results = _results(read_progress(path))
    assert len(results) == 1
    res = results[0]
    assert res["status"] == "stopped"
    assert res["stop"] == "max_iterations"
    assert "max iterations" in res["reason"]
    assert res["iterations"] == 3
    assert res["tokens_used"] == 90


def test_goal_met_run_records_natural_termination(tmp_path):
    path = tmp_path / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=1),
        verify=done_after(2),
        conditions=[MaxIterations(10)],
    )

    records = read_progress(path)
    assert len(_steps(records)) == 2
    res = _results(records)[0]
    assert res["status"] == "goal_met"
    assert res["stop"] is None
    assert res["reason"] == "goal met"
    assert res["iterations"] == 2


def test_records_are_flushed_after_each_step_not_only_at_the_end(tmp_path):
    # Read the file back *from inside* the loop: after the Nth on_step the file
    # must already hold N step records, proving incremental durability rather
    # than a single buffered dump at the end.
    path = tmp_path / "progress.jsonl"
    seen_counts = []

    def observe(record, _state):
        seen_counts.append(len(_steps(read_progress(path))))

    _run_with_progress(
        path,
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(4)],
        on_step=observe,
    )

    assert seen_counts == [1, 2, 3, 4]


# -- robustness: odd values, encoding, crashes ------------------------------


def test_non_serializable_observation_is_stored_as_repr(tmp_path):
    path = tmp_path / "progress.jsonl"

    class Widget:
        def __repr__(self):
            return "Widget(stuck)"

    def act(_ctx):
        return ActOutcome(observation=Widget(), tokens=0)

    _run_with_progress(
        path, act=act, verify=never_done, conditions=[MaxIterations(1)]
    )

    step = _steps(read_progress(path))[0]
    assert step["observation"] == "Widget(stuck)"


def test_unicode_detail_round_trips_as_utf8(tmp_path):
    path = tmp_path / "progress.jsonl"

    def verify(_outcome):
        return VerifyOutcome(goal_met=True, detail="収束しました")

    _run_with_progress(
        path, act=acting(tokens=0), verify=verify, conditions=[MaxIterations(5)]
    )

    # Stored without ASCII escaping and decodes back to the original Japanese.
    raw = path.read_text(encoding="utf-8")
    assert "収束しました" in raw
    assert _steps(read_progress(path))[0]["detail"] == "収束しました"


def test_unicode_line_separators_do_not_split_a_record(tmp_path):
    # U+2028/U+2029/U+0085 are emitted literally by json.dumps(ensure_ascii=False)
    # but are NOT the record framing -- the writer frames only with '\n'. A
    # record carrying one of them must read back as a single intact record, not
    # be torn into two halves that read as corruption.
    path = tmp_path / "progress.jsonl"
    nasty = "stuck\u2028here\u2029and\u0085there"

    def verify(_outcome):
        return VerifyOutcome(goal_met=True, detail=nasty)

    _run_with_progress(
        path, act=acting(tokens=0), verify=verify, conditions=[MaxIterations(5)]
    )

    records = read_progress(path)
    assert _steps(records)[0]["detail"] == nasty
    assert len(_results(records)) == 1  # terminal record intact, not split off


def test_read_tolerates_a_truncated_final_line(tmp_path):
    path = tmp_path / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )
    # Simulate a crash mid-append: a partial, unterminated JSON line at the end.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "step", "iteration": 3, "tokens"')

    records = read_progress(path)
    # The 3 steps + 1 result survive; the partial line is dropped, not raised.
    assert len(records) == 4
    assert [r["kind"] for r in records] == ["step", "step", "step", "result"]


def test_read_raises_on_a_corrupt_but_complete_final_line(tmp_path):
    # A fully-written (newline-terminated) final record that is corrupt is NOT a
    # crash-truncation -- it is genuine corruption (e.g. a mangled terminal
    # `result` line) and must be raised, not silently dropped.
    path = tmp_path / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{corrupt but terminated}\n")  # note the trailing newline

    with pytest.raises(json.JSONDecodeError):
        read_progress(path)


def test_read_raises_on_a_corrupt_interior_line(tmp_path):
    path = tmp_path / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(3)],
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "{not valid json"  # corrupt a line that is NOT the last one
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        read_progress(path)


def test_read_missing_file_returns_empty(tmp_path):
    assert read_progress(tmp_path / "does-not-exist.jsonl") == []


def test_progress_log_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "deeper" / "progress.jsonl"
    _run_with_progress(
        path,
        act=acting(tokens=0),
        verify=never_done,
        conditions=[MaxIterations(1)],
    )
    assert path.exists()
    assert len(_steps(read_progress(path))) == 1
