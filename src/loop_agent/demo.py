"""Verification-driven demo engine: reusable hooks using real test exit codes as ground truth.

This module provides the minimal scaffolding needed to apply loop-agent's loop
core (:func:`loop_agent.run_loop`) to *actual test execution*. Verification does
not use an LLM judge; it treats the ``returncode`` from a subprocess-launched
test command as the sole source of truth (report.md R1: ground truth first).

It provides three injectable hooks:

- :class:`CandidateApplier` -- ``act``. Writes the "next candidate fix source"
  to the target file on each iteration (a deterministic stub for an LLM fixer).
- :class:`ExitCodeVerifier`    -- ``verify``. Runs the test command in a
  sandbox and returns ``goal_met=True`` for exit-code 0 (green). The loop exits
  naturally on green.
- :func:`attempt_index`    -- ``gather``. Passes the next candidate number
  (= iteration count) to act as a minimal observation seam.

See ``examples/verify_driven_demo.py`` for the concrete scenario (loop until a
broken function is fixed), and ``tests/test_verify_demo.py`` for live
verification with pytest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Mapping, Optional, Sequence

from .errors import ConfigError
from .loop import ActOutcome, VerifyOutcome
from .state import LoopState

# Default command for running sandbox tests in the simplest possible way:
# by exit code.
#
# ``-B`` is important: when the target file rewritten on each iteration has the
# same byte length and coarse mtime resolution, it can slip past CPython's .pyc
# validation (mtime+size) and keep reading cached bytecode for the broken
# version from the previous iteration (a false negative where red persists after
# the fix). Disable bytecode writes for every run so Python recompiles from
# source each time.
#
# ``-B`` prevents bytecode *writes* (avoiding false negatives from stale .pyc
# files caused by equal-length candidates plus coarse mtime resolution). It does
# not prevent *reading* existing .pyc files, so remove the sandbox __pycache__
# before each verify (:func:`_clear_pycache`). Together they guarantee
# recompilation from source on every run.
#
# The trailing ``"."`` and ``-o addopts=`` are important: if workdir is placed
# inside a checkout that has pytest configuration in an ancestor (rather than in
# a temporary directory), pytest without a positional argument can choose that
# ancestor as rootdir, collect another suite through ``testpaths``, or apply
# ``addopts``. Then the exit code is no longer ground truth for this sandbox.
#   - ``"."``: limit collection to cwd (= sandbox). An explicit path overrides
#     ``testpaths``.
#   - ``-o addopts=``: override ancestor ini ``addopts`` with an empty value and
#     prevent caller configuration from leaking in.
# ``-p no:cacheprovider`` avoids picking up pytest cache configuration.
DEFAULT_TEST_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-B",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "-o",
    "addopts=",
    ".",
)

# Per-test-run limit in seconds. If verify blocks forever on a hanging
# candidate (for example, a fix that introduces an infinite loop), even boundary
# conditions such as Timeout/MaxIterations cannot take effect. Put a timeout on
# the subprocess and treat overruns as red so control returns to the loop
# (runaway prevention; full demonstration in #7).
DEFAULT_TEST_TIMEOUT: float = 120.0


# Denylisted keys that keep child-process test execution (exit-code = ground
# truth) independent of the caller's environment. These can inject CLI options
# or other behavior into nested pytest and flip a green sandbox to false
# red/green, making verification nondeterministic:
#   - PYTEST_ADDOPTS: pytest's official mechanism for propagating options to
#     nested runs. For example, starting the outer process with
#     ``PYTEST_ADDOPTS='-m somemarker'`` makes the child return rc=5 (no tests),
#     turning a green sandbox into false red (this happens in practice with tox,
#     CI, and -W injection).
#   - PYTEST_PLUGINS: forces specific plugins to load.
#   - COV_CORE_*: outer --cov runs inject coverage measurement into the child.
# Pass a copy of os.environ with these removed to the child.
# PYTEST_DISABLE_PLUGIN_AUTOLOAD is not removed; it is forced to 1 later instead
# (removing it would undo a caller-side =1 and re-enable ambient plugins; see
# below).
_ENV_DENYLIST = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
)


def sandbox_env() -> dict[str, str]:
    """Return environment variables isolated from the caller for sandbox tests."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_DENYLIST and not key.startswith("COV_CORE_")
    }
    # Prevent all bytecode writes, matching DEFAULT_TEST_COMMAND's -B rationale.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Disable ambient pytest plugin autoload so execution is hermetic and does
    # not depend on installed plugins. Force 1 even if the caller leaves it
    # unset or sets it to 0 (sandbox tests use only plain pytest core).
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _clear_pycache(workdir: Path) -> None:
    """Remove all ``__pycache__`` dirs under ``workdir`` to avoid stale .pyc reads.

    ``-B`` only prevents .pyc *writes*; existing .pyc files (for example from a
    manual pytest run without -B) can still be read. With equal-byte-length
    candidates plus coarse mtime, (mtime, size) validation can be bypassed and
    broken bytecode from the previous iteration may be used, so remove caches on
    every verify. Deletion is limited to the sandbox.
    """
    for cache in workdir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def write_sandbox(workdir: Path, files: Mapping[str, str]) -> None:
    """Write ``files`` (relative path -> content) under ``workdir``.

    UTF-8 is explicit because content may contain Japanese. Intermediate
    directories are created automatically.
    """
    for rel, content in files.items():
        path = workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def attempt_index(state: LoopState) -> int:
    """``gather`` hook: return the next candidate number (= iterations so far).

    Verification (ground truth) should be centralized in the single
    :class:`ExitCodeVerifier` run, so gather does not run tests. It only passes
    lightweight context about which candidate to apply.
    """
    return state.iteration


@dataclass
class CandidateApplier:
    """``act`` hook: write the next candidate fix source to ``target`` each iteration.

    In production this is where an LLM would inspect the failure and generate a
    fix patch. In the PoC it is a deterministic stub that applies a sequence of
    candidates, keeping focus on the loop mechanism and ground-truth
    verification. Once candidates are exhausted it keeps reusing the final
    candidate (= "keep trying the current best move"), so even unfixable
    scenarios can iterate safely until a hard limit stops them.

    ``cost_per_step`` is the token amount charged per step, useful for demos of
    :class:`~loop_agent.conditions.TokenBudget` (default is 0).
    """

    target: Path
    candidates: Sequence[str]
    cost_per_step: int = 0
    applied: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ConfigError("CandidateApplier requires at least one candidate")

    def __call__(self, attempt: int) -> ActOutcome:
        index = min(attempt, len(self.candidates) - 1)
        self.target.write_text(self.candidates[index], encoding="utf-8")
        self.applied.append(index)
        return ActOutcome(
            observation=f"applied candidate #{index}",
            tokens=self.cost_per_step,
        )


@dataclass
class ExitCodeVerifier:
    """``verify`` hook: run tests in a sandbox and use exit code as ground truth.

    ``returncode == 0`` is treated as green and returns ``goal_met=True``. This
    makes the loop exit naturally the moment tests turn green. Each run's
    returncode is recorded in :attr:`exit_codes` for later tests or observation.

    A test run that exceeds ``timeout`` kills the child process, records the
    sentinel exit-code (124), and returns red (``goal_met=False``). This returns
    control to the loop even for hanging candidates, letting boundary
    Timeout/MaxIterations conditions work. ``None`` means unlimited.
    """

    workdir: Path
    command: Sequence[str] = DEFAULT_TEST_COMMAND
    timeout: Optional[float] = DEFAULT_TEST_TIMEOUT
    exit_codes: list[int] = field(default_factory=list)

    # Sentinel exit code for hangs (timeouts), matching the conventional timeout
    # exit code. ClassVar keeps it out of dataclass fields (constructor args).
    TIMEOUT_EXIT_CODE: ClassVar[int] = 124

    def __call__(self, _outcome: ActOutcome) -> VerifyOutcome:
        # Remove existing .pyc files before running so source is always recompiled.
        _clear_pycache(self.workdir)
        try:
            proc = subprocess.run(
                list(self.command),
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                env=sandbox_env(),
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run has already killed the child. It is not green, so
            # treat it as red.
            self.exit_codes.append(self.TIMEOUT_EXIT_CODE)
            return VerifyOutcome(
                goal_met=False, detail=f"red (timeout {self.timeout:g}s)"
            )
        self.exit_codes.append(proc.returncode)
        green = proc.returncode == 0
        detail = "green" if green else f"red (exit={proc.returncode})"
        return VerifyOutcome(goal_met=green, detail=detail)
