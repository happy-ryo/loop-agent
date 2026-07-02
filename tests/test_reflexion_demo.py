"""Runtime verification for the outer Reflexion demo (Issue #22 / Phase 3 success condition a).

This imports and runs the exact scenario from ``examples/reflexion_demo.py``, so the
shipped demo and the verification target match. Proposition: linguistic guidance
extracted from a failed episode is wired into the next episode's context, and
ground truth (whether the inner verify succeeds) actually improves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

import reflexion_demo as demo  # noqa: E402


def test_lesson_wiring_lifts_next_episode_ground_truth():
    result = demo.run()
    history = result.state.gt_aggregate_history

    # ep0: Fails with empty memory (the off-by-one bug remains).
    assert history[0] < 0.3
    # ep1: Succeeds with the wired-in lesson -> ground truth jumps.
    assert history[1] > 0.9

    # The lesson is admitted into memory and wired into the next context.
    assert any(rec.admitted for rec in result.state.episodes)
    assert demo.LESSON_HINT in result.state.memory.render()

    # The primary signal (ground truth) drives convergence and ends in success.
    assert result.succeeded is True


def test_demo_runs_as_script():
    """The shipped demo runs from a terminal, and print does not crash under cp932."""
    proc = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / "reflexion_demo.py")],
        capture_output=True,
        text=True,
        env={**_clean_env(), "PYTHONPATH": str(EXAMPLES_DIR.parent / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "succeeded=True" in proc.stdout


def _clean_env() -> dict:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
