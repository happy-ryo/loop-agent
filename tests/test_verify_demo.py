"""End-to-end verification for the verification-driven demo (report.md R1 / Phase 1, Issue #6).

Claims verified here:

1. The loop *terminates naturally* the moment it reaches a verifiable goal
   (green sandbox tests). This is reproduced and checked by running real pytest
   in a subprocess.
2. Verification is grounded in the *real test exit code*, not an LLM judge, and
   loop termination matches exit-code 0.
3. Even if none of the candidates fixes the issue, the hard cap (MaxIterations)
   always stops the loop (runaway prevention; full demonstration is #7).

This imports and runs the exact scenario from ``examples/verify_driven_demo.py``,
so the shipped demo and the verified target are identical.
"""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
from pathlib import Path

# Put examples/ on the path and import the shipped demo scenario directly.
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

import verify_driven_demo as demo  # noqa: E402

from loop_agent import MaxIterations  # noqa: E402
from loop_agent.demo import (  # noqa: E402
    DEFAULT_TEST_COMMAND,
    ExitCodeVerifier,
    sandbox_env,
)


# -- 1 & 2: naturally terminate on green, with exit-code as ground truth ------


def test_loop_terminates_naturally_when_sandbox_turns_green(tmp_path):
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    result = run.result

    # Natural termination (goal reached): no hard cap fired.
    assert result.goal_met is True
    assert result.status == "goal_met"
    assert result.stop is None
    assert result.reason == "goal met"

    # The third attempt (correct addition) turns green, and the loop stops there.
    assert result.iterations == 3
    assert run.act.applied == [0, 1, 2]

    # Termination is driven by the real test exit code: red, red, green.
    assert run.verify.exit_codes == [1, 1, 0]
    assert result.history[-1].goal_met is True
    assert result.history[-1].detail == "green"

    # The sandbox keeps the correct implementation, which is independently green.
    assert (tmp_path / demo.TARGET_FILENAME).read_text(encoding="utf-8") == demo.CORRECT_ADD
    proc = subprocess.run(
        list(DEFAULT_TEST_COMMAND),
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=sandbox_env(),
    )
    assert proc.returncode == 0


def test_verify_is_hermetic_against_pytest_addopts(tmp_path, monkeypatch):
    # The parent PYTEST_ADDOPTS can inject options into nested pytest and flip a
    # green sandbox to false red (rc=5 for "no tests collected" here). Sandbox
    # execution excludes this kind of env, so the ground truth (exit-code) stays
    # deterministic even under contamination and reaches green naturally on the
    # third attempt as usual.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-m this_marker_matches_nothing")
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    assert run.result.goal_met is True
    assert run.result.iterations == 3
    assert run.verify.exit_codes == [1, 1, 0]


def test_sandbox_env_enforces_hermetic_invariants(monkeypatch):
    # Sandbox execution is isolated no matter what pytest-related env the parent
    # has: result-flipping sources (ADDOPTS/PLUGINS) are removed, and autoload
    # disabling is forced to 1 rather than undone.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-m nope")
    monkeypatch.setenv("PYTEST_PLUGINS", "some_plugin")
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    env = sandbox_env()
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTEST_PLUGINS" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_history_tokens_track_completed_work(tmp_path):
    # Each iteration accounts for 10 tokens -> 30 over 3 iterations, matching the
    # observation hook's records.
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    assert run.result.tokens_used == 30
    assert [r.iteration for r in run.result.history] == [0, 1, 2]


# -- 3: stop at the cap when no candidate fixes the issue ---------------------


def test_loop_stops_at_cap_when_never_green(tmp_path):
    # Only broken candidates are provided -> always red. Confirm the cap stops it.
    run = demo.run_repair(
        tmp_path,
        candidates=[demo.BROKEN_SUBTRACT],
        conditions=[MaxIterations(3)],
    )
    result = run.result

    assert result.goal_met is False
    assert result.status == "stopped"
    assert result.stop is not None
    assert result.stop.name == "max_iterations"
    assert result.iterations == 3
    # All 3 iterations are red (exit-code 0 never appears).
    assert run.verify.exit_codes == [1, 1, 1]


# -- standalone ground-truth hook: correctly map exit-code 0/nonzero ----------


def test_verifier_ignores_stale_bytecode_cache(tmp_path):
    # Reproduce a stale __pycache__ left by a manual run without -B: compile the
    # broken version to create a .pyc, rewrite it to the correct version, then
    # restore the original mtime so it passes (mtime, size) validation (the
    # candidates have equal byte length). The verifier removes __pycache__ and
    # recompiles from source, so it returns the correct green instead of stale red.
    target = tmp_path / "add.py"
    (tmp_path / "test_add.py").write_text(
        "from add import add\ndef test_x():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # broken
    broken_mtime = target.stat().st_mtime
    py_compile.compile(str(target), doraise=True)  # writes __pycache__/add.*.pyc
    assert list((tmp_path / "__pycache__").glob("add.*.pyc"))

    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")  # fixed
    os.utime(target, (broken_mtime, broken_mtime))  # restore original mtime -> stale pyc appears valid

    verdict = ExitCodeVerifier(workdir=tmp_path)(None)
    assert verdict.goal_met is True
    assert verdict.detail == "green"


def test_verifier_times_out_on_hanging_test(tmp_path):
    # Add a hanging test (infinite loop) -> timeout kills it and treats it as red,
    # returning control to the loop (verify does not block forever and prevent cap
    # evaluation).
    (tmp_path / "test_hang.py").write_text(
        "def test_hang():\n    while True:\n        pass\n", encoding="utf-8"
    )
    verifier = ExitCodeVerifier(workdir=tmp_path, timeout=1.0)
    verdict = verifier(None)
    assert verdict.goal_met is False
    assert "timeout" in verdict.detail
    assert verifier.exit_codes == [ExitCodeVerifier.TIMEOUT_EXIT_CODE]


def test_verifier_maps_exit_code_to_goal(tmp_path):
    # Green sandbox.
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    verifier = ExitCodeVerifier(workdir=tmp_path)
    verdict = verifier(None)
    assert verdict.goal_met is True
    assert verifier.exit_codes == [0]

    # Adding one red test makes the same verifier return goal_met=False.
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    verdict = verifier(None)
    assert verdict.goal_met is False
    assert verifier.exit_codes[-1] != 0
