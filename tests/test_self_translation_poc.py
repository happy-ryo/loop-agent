"""Wiring tests for the self-translation PoC harness (Issue #37 dogfood).

These exercise the PoC's verifier and its loop / gate / Reflexion wiring with a
deterministic local stub act -- no ``claude`` subprocess, no edits to the real
``src/loop_agent`` tree. The shipped harness (examples/self_translation_poc) is
therefore the verified artifact, matching the repo convention that example
demos are imported and run under pytest (see ``test_verify_demo``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from loop_agent import read_events

# Put examples/self_translation_poc on the path and import the shipped harness.
POC_DIR = Path(__file__).resolve().parent.parent / "examples" / "self_translation_poc"
if str(POC_DIR) not in sys.path:
    sys.path.insert(0, str(POC_DIR))

import harness as H  # noqa: E402
import verify as V  # noqa: E402

SAMPLE = '''\
"""モジュールの説明 (module docstring with Japanese)."""


def greet(name):
    """挨拶を返す."""
    # 日本語のコメント
    message = "こんにちは"
    label = name  # trailing 日本語 comment
    return message + label  # ASCII only here
'''


def _write_sample(tmp_path: Path) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


# -- verifier --------------------------------------------------------------


def test_japanese_hits_targets_comments_and_docstrings_only(tmp_path):
    p = _write_sample(tmp_path)
    hits = V.japanese_hits(p)
    kinds = sorted({h.kind for h in hits})
    # module docstring + function docstring + two comments == 4 hits, all of
    # kind comment/docstring. The Japanese STRING LITERAL ("こんにちは") is NOT
    # flagged: non-docstring strings are out of scope.
    assert kinds == ["comment", "docstring"]
    # 2 docstrings + 2 comments == 4. The Japanese string literal on its own
    # line is NOT a 5th hit.
    assert len(hits) == 4
    string_literal_line = 7  # `message = "こんにちは"`
    assert all(h.line != string_literal_line for h in hits)


def test_verify_file_clears_after_stub_translation(tmp_path):
    p = _write_sample(tmp_path)
    before = V.verify_file(p, run_tests=False)
    assert before.parses_ok
    assert not before.japanese_cleared
    assert not before.done

    H._strip_japanese_stub(p)
    after = V.verify_file(p, run_tests=False)
    assert after.parses_ok
    assert after.japanese_cleared
    assert after.done
    # The out-of-scope string literal still has its Japanese replaced by the
    # stub (stub is blunt); but the verifier already proved targeting works.


def test_verify_file_reports_syntax_error(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("def f(:\n    pass\n", encoding="utf-8")
    rep = V.verify_file(p, run_tests=False)
    assert not rep.parses_ok
    assert not rep.done
    assert "SyntaxError" in rep.detail


# -- loop + gate wiring ----------------------------------------------------


def _make_targets(tmp_path: Path, n: int) -> list[Path]:
    files = []
    for i in range(n):
        p = tmp_path / f"mod{i}.py"
        p.write_text(SAMPLE, encoding="utf-8")
        files.append(p)
    return files


def test_no_reflexion_loop_reaches_goal_with_gate(tmp_path):
    files = _make_targets(tmp_path, 3)
    translator = H.Translator(files, run_tests=False)
    act = H.make_stub_act(H._strip_japanese_stub, tokens_per_call=500)
    log = tmp_path / "run.jsonl"

    res = H.run_no_reflexion(
        translator,
        act,
        log_path=log,
        gate=True,
        store_path=tmp_path / "gate.sqlite3",
    )

    assert res.succeeded
    assert res.status in ("goal_met", "stopped")
    assert len(res.done) == 3
    assert res.failed == []
    # every target file is now Japanese-clear in its comments/docstrings.
    for f in files:
        assert not V.japanese_hits(f)

    events = read_events(log)
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "loop_begin"
    assert kinds[-1] == "loop_end"
    assert kinds.count("loop_step") == 3


def test_no_reflexion_loop_without_gate(tmp_path):
    files = _make_targets(tmp_path, 2)
    translator = H.Translator(files, run_tests=False)
    act = H.make_stub_act(H._strip_japanese_stub)
    res = H.run_no_reflexion(
        translator, act, log_path=tmp_path / "r.jsonl", gate=False
    )
    assert res.succeeded
    assert len(res.done) == 2


def test_max_iterations_cap_stops_a_stuck_loop(tmp_path):
    # An act that never clears Japanese must be stopped by the hard cap, not spin.
    files = _make_targets(tmp_path, 2)
    translator = H.Translator(files, run_tests=False)
    noop = H.make_stub_act(lambda _p: None)  # never edits -> verify never done
    res = H.run_no_reflexion(
        translator,
        noop,
        log_path=tmp_path / "r.jsonl",
        max_iterations=4,
        gate=False,
    )
    assert not res.succeeded
    assert res.status == "stopped"
    assert res.iterations == 4
    assert len(res.failed) == 2


# -- Reflexion wiring ------------------------------------------------------


def test_reflexion_recovers_after_a_failing_first_episode(tmp_path):
    files = _make_targets(tmp_path, 2)
    translator = H.Translator(files, run_tests=False)

    # A stub that "fails" the very first act (leaves Japanese) then translates
    # correctly thereafter -- so episode 0 cannot finish within a tiny inner cap
    # and Reflexion must run a second episode to converge.
    state = {"calls": 0}

    def flaky(path: Path) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            return  # first action is a no-op: leaves Japanese, verify fails
        H._strip_japanese_stub(path)

    act = H.make_stub_act(flaky)
    res = H.run_with_reflexion(
        translator,
        act,
        log_path=tmp_path / "ref.jsonl",
        inner_max_iterations=2,
        max_episodes=4,
        epoch_len=2,
    )

    assert res.succeeded
    assert len(res.done) == 2
    assert res.episodes >= 2  # needed more than one episode

    events = read_events(tmp_path / "ref.jsonl")
    kinds = {e["kind"] for e in events}
    # outer reflexion events + inner loop events share the one log file.
    assert "reflexion_begin" in kinds
    assert "reflexion_end" in kinds
    assert "loop_step" in kinds
