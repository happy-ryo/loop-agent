"""Shared test helpers for the loop core."""

from __future__ import annotations

from typing import Callable

from claude_loop import ActOutcome, VerifyOutcome


class FakeClock:
    """Deterministic monotonic clock for timeout tests.

    Returns ``start`` on the first call and advances by ``step`` seconds on each
    subsequent call, so the loop's elapsed time is fully predictable regardless
    of real wall-clock speed.
    """

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self._now = start
        self._step = step
        self._first = True

    def __call__(self) -> float:
        if self._first:
            self._first = False
            return self._now
        self._now += self._step
        return self._now


def acting(tokens: int = 0, observation: object = None) -> Callable[[object], ActOutcome]:
    """An ``act`` stub charging a fixed token cost per step."""

    def _act(_ctx: object) -> ActOutcome:
        return ActOutcome(observation=observation, tokens=tokens)

    return _act


def never_done(_outcome: ActOutcome) -> VerifyOutcome:
    """A ``verify`` stub that never reaches the goal (drives cap tests)."""
    return VerifyOutcome(goal_met=False)


def done_after(n: int) -> Callable[[ActOutcome], VerifyOutcome]:
    """A ``verify`` stub that reports the goal met on the ``n``-th call (1-based)."""
    calls = {"count": 0}

    def _verify(_outcome: ActOutcome) -> VerifyOutcome:
        calls["count"] += 1
        met = calls["count"] >= n
        return VerifyOutcome(goal_met=met, detail="converged" if met else "")

    return _verify
