"""work-discovery input selection + multi-item scheduling (Issue #24 / #56).

This package brings together the two layers that decide "what to iterate next":

- **Input selection** (implemented internally by ``_triage``) -- dependency
  resolution, priority ranking, and the human gate (propose-only) (report.md S3.5
  / Issue #24). Computes "N items + 1 recommendation" from candidates
  (:class:`Candidate`) and sends adoption decisions through the human gate.
- :mod:`~loop_agent.discovery.work_list` -- a scheduling gather
  (:class:`WorkListGather`, Issue #56) that **rotates multiple adopted items
  fairly through a single loop**. It normalizes round-robin / per-item limits so
  one item cannot monopolize ``MaxIterations`` and starve the others.

All public names from the former single ``loop_agent.discovery`` module are
re-exported here, so existing imports such as ``from loop_agent.discovery import
triage`` / ``from loop_agent import triage`` remain unchanged for compatibility.
The input-selection implementation lives in the private ``_triage`` module to
avoid an accident from the #56 review: the public ``triage`` name (a function)
shadowing the submodule of the same name and causing ``import
loop_agent.discovery.triage`` to bind to the function. Callers should use this
facade.
"""

from __future__ import annotations

from ._triage import (
    GATE_KEY_PREFIX,
    AdoptionResult,
    BlockedCandidate,
    Candidate,
    Proposal,
    Triage,
    WorkDiscovery,
    discover_next,
    triage,
)
from .work_list import (
    DRAINED,
    Drained,
    ScheduleContext,
    Scheduler,
    WorkItem,
    WorkListDrained,
    WorkListGather,
    WorkListProgress,
)

__all__ = [
    # Input selection (triage / human gate, Issue #24)
    "Candidate",
    "BlockedCandidate",
    "Triage",
    "triage",
    "Proposal",
    "AdoptionResult",
    "WorkDiscovery",
    "discover_next",
    "GATE_KEY_PREFIX",
    # Multi-item fair scheduling (Issue #56)
    "WorkItem",
    "WorkListGather",
    "WorkListProgress",
    "WorkListDrained",
    "ScheduleContext",
    "Scheduler",
    "Drained",
    "DRAINED",
]
