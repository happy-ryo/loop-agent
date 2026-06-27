"""claude-loop PoC loop core.

A minimal, single-process ``gather -> act -> verify -> repeat`` driver with
composable, reason-bearing stop conditions. See report.md S4.4 / S5 Phase 1.

Quick start::

    from claude_loop import run_loop, ActOutcome, VerifyOutcome, MaxIterations

    def act(ctx):
        return ActOutcome(observation="did one unit of work", tokens=10)

    def verify(outcome):
        return VerifyOutcome(goal_met=False)

    result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)])
    print(result.status, result.reason, result.iterations)
"""

from __future__ import annotations

from .conditions import (
    AnyOf,
    MaxIterations,
    StopCondition,
    StopTrigger,
    Timeout,
    TokenBudget,
)
from .loop import ActOutcome, LoopResult, VerifyOutcome, run_loop
from .state import LoopState, StepRecord

__all__ = [
    "run_loop",
    "ActOutcome",
    "VerifyOutcome",
    "LoopResult",
    "LoopState",
    "StepRecord",
    "AnyOf",
    "StopCondition",
    "StopTrigger",
    "MaxIterations",
    "TokenBudget",
    "Timeout",
]

__version__ = "0.0.1"
