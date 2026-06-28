"""Shared test helpers for the loop core."""

from __future__ import annotations

from typing import Callable

from loop_agent import ActOutcome, VerifyOutcome


class ManualClock:
    """Deterministic clock whose value only moves when explicitly advanced.

    Reads (``clock()``) are free of side effects, so the result is independent
    of *how many times* the loop happens to read the clock per iteration. Tests
    advance it inside the ``act`` hook to model wall-clock time elapsing during
    a step (see :func:`stepping_for`).
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def acting(tokens: int = 0, observation: object = None) -> Callable[[object], ActOutcome]:
    """An ``act`` stub charging a fixed token cost per step."""

    def _act(_ctx: object) -> ActOutcome:
        return ActOutcome(observation=observation, tokens=tokens)

    return _act


def stepping_for(clock: ManualClock, seconds: float, tokens: int = 0):
    """An ``act`` stub that advances ``clock`` by ``seconds`` each step."""

    def _act(_ctx: object) -> ActOutcome:
        clock.advance(seconds)
        return ActOutcome(observation=None, tokens=tokens)

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
