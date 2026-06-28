"""work-discovery 入力選定 + multi-item scheduling (Issue #24 / #56).

このパッケージは「次に何を反復するか」を決める二つの層を束ねる:

- :mod:`~loop_agent.discovery.triage` -- 依存解決・優先度ランキング・人間ゲート
  (propose-only) による **入力選定** (report.md S3.5 / Issue #24)。候補 (:class:`Candidate`)
  群から「N 件 + 推奨 1 件」を計算し、採否を人間ゲートに載せる。
- :mod:`~loop_agent.discovery.work_list` -- 採択済みの **複数 item を 1 本のループで
  公平に回す** scheduling gather (:class:`WorkListGather`, Issue #56)。1 item が
  ``MaxIterations`` を独占して他を starve させないための round-robin / per-item 上限を
  正規化する。

旧 ``loop_agent.discovery`` 単一モジュールの公開名はすべてここから再エクスポートするので、
``from loop_agent.discovery import triage`` 等の既存 import は不変 (互換維持)。
"""

from __future__ import annotations

from .triage import (
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
    # 入力選定 (triage / 人間ゲート, Issue #24)
    "Candidate",
    "BlockedCandidate",
    "Triage",
    "triage",
    "Proposal",
    "AdoptionResult",
    "WorkDiscovery",
    "discover_next",
    "GATE_KEY_PREFIX",
    # multi-item 公平 scheduling (Issue #56)
    "WorkItem",
    "WorkListGather",
    "WorkListProgress",
    "WorkListDrained",
    "ScheduleContext",
    "Scheduler",
    "Drained",
    "DRAINED",
]
