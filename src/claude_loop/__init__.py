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
    GoalCheck,
    GoalMet,
    MaxIterations,
    NoProgress,
    StopCondition,
    StopTrigger,
    Timeout,
    TokenBudget,
)
from .discovery import (
    AdoptionResult,
    BlockedCandidate,
    Candidate,
    Proposal,
    Triage,
    WorkDiscovery,
    discover_next,
    triage,
)
from .events import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    CallableSink,
    EventSink,
    JsonlEventSink,
    ListSink,
    LoopEvent,
    read_events,
)
from .convergence import (
    EvaluatorUpdateBudget,
    MaxEpisodes,
    OuterState,
    ReflectionBudget,
    RubricThreshold,
    ScorePlateau,
    is_success_condition,
)
from .evaluator import (
    AdmissionResult,
    Evaluator,
    GroundTruthSignal,
    HeldOut,
    Probe,
    Score,
    admit_evaluator,
    agreement,
)
from .gate import Decision, HumanGate, run_gated_loop
from .loop import ActionGate, ActOutcome, GateReview, LoopResult, VerifyOutcome, run_loop
from .memory import (
    EpisodicMemory,
    Lesson,
    LessonVerdict,
    default_admit,
    step_signature,
)
from .observe import LoopObserver, run_observed_loop
from .reflexion import (
    EpisodeOutcome,
    EpisodeRecord,
    EpochRecord,
    ReflexionContext,
    ReflexionState,
    ReflexiveResult,
    run_reflexion,
)
from .reflexion_observe import (
    EPISODE_BEGIN,
    EPISODE_END,
    EPOCH_BOUNDARY,
    LESSON_DECISION,
    REFLEXION_BEGIN,
    REFLEXION_END,
    ReflexionObserver,
    run_observed_reflexion,
)
from .otel import LoopSpan, ReflexionSpan, otel_available
from .progress import ProgressLog, read_progress
from .state import LoopState, StepRecord
from .store import DBProgressLog, DECISION_KINDS, LoopStore, connect
from .transport import (
    CADENCE_SECONDS,
    CallablePushBackend,
    DEFAULT_CADENCE_SECONDS,
    InMemoryWakeQueue,
    NullPushBackend,
    PushBackend,
    Transport,
    WAKE_DECISION_REQUEST,
    WAKE_KINDS,
    WAKE_LOOP_DONE,
    WAKE_NEXT_ITERATION,
    Wake,
    WakeQueue,
    cadence_for,
    due_to_poll,
)
from .waker import LoopWaker, wake_id_for, wakes_for_result

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
    "GoalMet",
    "GoalCheck",
    "NoProgress",
    "ProgressLog",
    "read_progress",
    # observability (report.md S4.5 / S5 Phase 2)
    "LoopEvent",
    "EventSink",
    "ListSink",
    "CallableSink",
    "JsonlEventSink",
    "read_events",
    "LOOP_BEGIN",
    "LOOP_STEP",
    "LOOP_END",
    "LoopObserver",
    "run_observed_loop",
    "LoopSpan",
    "otel_available",
    # state SoT (report.md S3.4 / S4.6 / S5 Phase 2)
    "connect",
    "LoopStore",
    "DBProgressLog",
    "DECISION_KINDS",
    # 限定人間ゲート (report.md S4.5 / R6 / S5 Phase 2; Issue #15)
    "ActionGate",
    "GateReview",
    "HumanGate",
    "Decision",
    "run_gated_loop",
    # 外側 Reflexion ループ + RQGM epoch 安全核 (report.md S4.4 / S5 Phase3; Issue #22)
    "run_reflexion",
    "ReflexionContext",
    "ReflexionState",
    "ReflexiveResult",
    "EpisodeRecord",
    "EpochRecord",
    "EpisodeOutcome",
    # 外側 Reflexion 観測 (report.md S4.5 を外側へ延伸; Issue #30)
    "ReflexionObserver",
    "run_observed_reflexion",
    "ReflexionSpan",
    "REFLEXION_BEGIN",
    "EPISODE_BEGIN",
    "EPISODE_END",
    "LESSON_DECISION",
    "EPOCH_BOUNDARY",
    "REFLEXION_END",
    "Score",
    "GroundTruthSignal",
    "Evaluator",
    "Probe",
    "HeldOut",
    "agreement",
    "admit_evaluator",
    "AdmissionResult",
    "Lesson",
    "LessonVerdict",
    "EpisodicMemory",
    "default_admit",
    "step_signature",
    "OuterState",
    "MaxEpisodes",
    "RubricThreshold",
    "ScorePlateau",
    "ReflectionBudget",
    "EvaluatorUpdateBudget",
    "is_success_condition",
    # wake 配送 transport (report.md S3.3 / S4.6 / S5 Phase3; Issue #23)
    "Wake",
    "WAKE_LOOP_DONE",
    "WAKE_NEXT_ITERATION",
    "WAKE_DECISION_REQUEST",
    "WAKE_KINDS",
    "PushBackend",
    "CallablePushBackend",
    "NullPushBackend",
    "WakeQueue",
    "InMemoryWakeQueue",
    "Transport",
    "CADENCE_SECONDS",
    "DEFAULT_CADENCE_SECONDS",
    "cadence_for",
    "due_to_poll",
    "LoopWaker",
    "wakes_for_result",
    "wake_id_for",
    # work-discovery 入力選定 (report.md S3.5 / S4.6 / S5 Phase 3; Issue #24)
    "Candidate",
    "BlockedCandidate",
    "Triage",
    "triage",
    "Proposal",
    "AdoptionResult",
    "WorkDiscovery",
    "discover_next",
]

__version__ = "0.0.1"
