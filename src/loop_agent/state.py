"""Loop state: the single mutable record threaded through the loop.

For the PoC this lives in memory only. Phase 2 (report.md S4.6) externalises
the same shape to a state.db so the loop becomes resumable; keeping the state
in one explicit object now makes that migration mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepRecord:
    """One completed gather -> act -> verify iteration."""

    iteration: int
    observation: Any
    tokens: int
    goal_met: bool
    detail: str = ""


@dataclass
class LoopState:
    """Mutable accumulator inspected by stop conditions on every iteration.

    Counters reflect *completed* work:

    - ``iteration``   number of finished gather->act->verify cycles.
    - ``tokens_used`` cumulative tokens reported by the ``act`` hook.
    - ``elapsed``     seconds since the loop started (refreshed each cycle by
      the driver before stop conditions are evaluated).
    """

    iteration: int = 0
    tokens_used: int = 0
    elapsed: float = 0.0
    goal_met: bool = False
    history: list[StepRecord] = field(default_factory=list)
