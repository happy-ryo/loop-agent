"""Small ground-truth verifier helpers for common loop-agent harnesses.

The core loop deliberately keeps ``verify`` as caller-owned policy. These
helpers cover the lowest-risk cases where the ground truth is already
mechanical: a command exit code, pytest, or a regex over the act observation.
They are optional conveniences, not an LLM judge.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Pattern, Sequence

from .loop import ActOutcome, VerifyOutcome


def _tail(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[-limit:]


@dataclass(frozen=True)
class CommandVerifier:
    """Verify success by running a command and checking its exit code.

    Use this when the task has an existing machine oracle, such as a test suite,
    linter, compiler, smoke probe, or schema checker. The command receives no
    stdin, so interactive tools fail fast instead of waiting for user input.
    """

    command: Sequence[str]
    cwd: Optional[str | Path] = None
    timeout: Optional[float] = None
    success_codes: tuple[int, ...] = (0,)
    detail_chars: int = 4000

    def __call__(self, outcome: ActOutcome) -> VerifyOutcome:
        try:
            completed = subprocess.run(
                tuple(self.command),
                cwd=None if self.cwd is None else str(self.cwd),
                timeout=self.timeout,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return VerifyOutcome(
                goal_met=False,
                detail=f"command timed out after {exc.timeout:g}s: {self.command!r}",
            )
        except OSError as exc:
            return VerifyOutcome(
                goal_met=False,
                detail=f"command failed to launch: {type(exc).__name__}: {exc}",
            )

        detail = (
            f"exit={completed.returncode}; "
            f"stdout={_tail(completed.stdout, self.detail_chars)!r}; "
            f"stderr={_tail(completed.stderr, self.detail_chars)!r}"
        )
        return VerifyOutcome(
            goal_met=completed.returncode in self.success_codes,
            detail=detail,
        )


@dataclass(frozen=True)
class PytestVerifier:
    """Verify success with ``python -m pytest``.

    ``args`` is passed after ``pytest``. For example, use
    ``PytestVerifier(["tests/test_loop.py", "-q"], timeout=60)`` for a focused
    oracle inside a loop.
    """

    args: tuple[str, ...] = ()
    cwd: Optional[str | Path] = None
    timeout: Optional[float] = None
    python: str = sys.executable
    detail_chars: int = 4000

    def __init__(
        self,
        args: Iterable[str] = (),
        *,
        cwd: Optional[str | Path] = None,
        timeout: Optional[float] = None,
        python: str = sys.executable,
        detail_chars: int = 4000,
    ) -> None:
        object.__setattr__(self, "args", tuple(args))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "python", python)
        object.__setattr__(self, "detail_chars", detail_chars)

    def __call__(self, outcome: ActOutcome) -> VerifyOutcome:
        return CommandVerifier(
            (self.python, "-m", "pytest", *self.args),
            cwd=self.cwd,
            timeout=self.timeout,
            detail_chars=self.detail_chars,
        )(outcome)


@dataclass(frozen=True)
class RegexVerifier:
    """Verify that a regex matches text from the act outcome.

    The default extractor reads ``outcome.observation.text`` when present and
    falls back to ``str(outcome.observation)``.
    """

    pattern: str | Pattern[str]
    flags: int = 0
    extractor: Callable[[ActOutcome], str] | None = None
    detail_chars: int = 1000

    def __call__(self, outcome: ActOutcome) -> VerifyOutcome:
        text = self._extract(outcome)
        regex = (
            self.pattern
            if hasattr(self.pattern, "search")
            else re.compile(str(self.pattern), self.flags)
        )
        matched = regex.search(text) is not None
        return VerifyOutcome(
            goal_met=matched,
            detail=(
                f"regex matched: {regex.pattern!r}"
                if matched
                else f"regex did not match {regex.pattern!r} in {_tail(text, self.detail_chars)!r}"
            ),
        )

    def _extract(self, outcome: ActOutcome) -> str:
        if self.extractor is not None:
            return self.extractor(outcome)
        text = getattr(outcome.observation, "text", None)
        if text is not None:
            return str(text)
        return str(outcome.observation)


__all__ = ["CommandVerifier", "PytestVerifier", "RegexVerifier"]
