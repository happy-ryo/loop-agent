#!/usr/bin/env python3
"""Verification-driven demo: repeat gather->act->verify until sandbox tests are green.

A concrete demo that applies loop-agent's loop core to *real code*. It writes an
intentionally broken ``add`` function and its pytest tests to a temporary
directory, then runs the loop until verification sees the real pytest exit code
turn green.

Scenario:

1. Prepare a sandbox with a broken ``add`` implementation (``a - b``) and
   ``test_add.py``.
2. act    = write the "next repair candidate" to ``add.py`` on each iteration
   (a stub for the repair role). Candidates are tried in the order
   [subtraction -> multiplication -> correct addition], and only the third
   attempt is correct.
3. verify = run pytest in the sandbox as a subprocess and read its exit code
   (ground truth). Exit code 0 (green) sets ``goal_met=True``, so the loop
   terminates naturally.
4. Even if no candidate fixes the code, hard limits such as ``MaxIterations``
   always stop the loop (runaway prevention; the full proof is #7).

Run:

    python3 examples/verify_driven_demo.py

This module is also imported by ``tests/test_verify_demo.py``, where this exact
scenario is verified by actually running pytest (the shipped artifact is the
verification target).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from loop_agent import (
    LoopResult,
    MaxIterations,
    StepRecord,
    Timeout,
    TokenBudget,
    run_loop,
)
from loop_agent.demo import (
    CandidateApplier,
    ExitCodeVerifier,
    attempt_index,
    write_sandbox,
)
from loop_agent.state import LoopState

# -- sandbox contents ------------------------------------------------------

TARGET_FILENAME = "add.py"
TEST_FILENAME = "test_add.py"

# Repair candidates. The first two stay red; the third makes all tests green.
BROKEN_SUBTRACT = "def add(a, b):\n    return a - b\n"   # add(2,3) -> -1 (red)
BROKEN_MULTIPLY = "def add(a, b):\n    return a * b\n"   # add(2,3) ->  6 (red)
CORRECT_ADD = "def add(a, b):\n    return a + b\n"       # add(2,3) ->  5 (green)

DEFAULT_CANDIDATES: tuple[str, ...] = (
    BROKEN_SUBTRACT,
    BROKEN_MULTIPLY,
    CORRECT_ADD,
)

TEST_SOURCE = (
    "from add import add\n"
    "\n"
    "\n"
    "def test_add_small():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
)


class DemoRun(NamedTuple):
    """The result of one demo run and records from the hooks used for observation."""

    result: LoopResult
    act: CandidateApplier
    verify: ExitCodeVerifier


def prepare_sandbox(workdir: Path) -> None:
    """Write the tests and the initially broken implementation to the sandbox.

    The initial ``add.py`` is intentionally broken, so running ``pytest``
    manually before starting the loop is red (= there is something to repair).
    """
    write_sandbox(
        workdir,
        {
            TEST_FILENAME: TEST_SOURCE,
            TARGET_FILENAME: BROKEN_SUBTRACT,
        },
    )


def run_repair(
    workdir: Path,
    *,
    candidates: Sequence[str] = DEFAULT_CANDIDATES,
    conditions: Optional[list] = None,
    on_step=None,
) -> DemoRun:
    """Use ``workdir`` as the sandbox and run the repair loop until tests are green.

    Args:
        workdir: Directory to use as the sandbox; the caller creates and cleans it up.
        candidates: Sequence of ``add.py`` sources to apply on each iteration.
        conditions: Stop conditions. When omitted, a practical set of hard limits is used.
        on_step: Observation hook called after each iteration completes.
    """
    prepare_sandbox(workdir)

    act = CandidateApplier(
        target=workdir / TARGET_FILENAME,
        candidates=candidates,
        cost_per_step=10,
    )
    verify = ExitCodeVerifier(workdir=workdir)

    if conditions is None:
        conditions = [MaxIterations(10), TokenBudget(1000), Timeout(60.0)]

    result = run_loop(
        act=act,
        verify=verify,
        conditions=conditions,
        gather=attempt_index,
        on_step=on_step,
    )
    return DemoRun(result=result, act=act, verify=verify)


def _print_step(record: StepRecord, _state: LoopState) -> None:
    status = "GREEN" if record.goal_met else "red  "
    print(
        f"  iter {record.iteration}: {record.observation:<20} "
        f"-> verify={status} ({record.detail})"
    )


def main() -> int:
    print("=== loop-agent verification-driven demo ===")
    print("goal: keep gather->act->verify until the sandbox tests are GREEN")
    print("verify = real pytest exit-code (ground truth, not an LLM judge)\n")

    with tempfile.TemporaryDirectory(prefix="loop-agent-demo-") as tmp:
        workdir = Path(tmp)
        run = run_repair(workdir, on_step=_print_step)
        result = run.result

        print("\n--- result ---")
        print(f"status     : {result.status}")
        print(f"reason     : {result.reason}")
        print(f"iterations : {result.iterations}")
        print(f"tokens     : {result.tokens_used}")
        print(f"exit-codes : {run.verify.exit_codes}  (0 == tests green)")

    # Ensure the loop naturally terminated after reaching a verifiable goal (green tests).
    ok = result.goal_met and result.stop is None
    print("\nOK: loop terminated naturally on a verified GREEN goal."
          if ok else "\nNG: loop did not reach the verified goal.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
