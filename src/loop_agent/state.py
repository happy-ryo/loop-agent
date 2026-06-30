"""Loop state: the single mutable record threaded through the loop.

The in-memory state is the runtime view used by stop conditions and hooks.
Persistence layers such as :mod:`loop_agent.store` externalise the same shape to
state.db so runs can be inspected and resumed without changing loop policy.
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
