"""Outer Reflexion loop driver: linguistic self-improvement across attempts + RQGM epoch safety core (Issue #22).

Wrap the inner ReAct loop (:func:`loop_agent.loop.run_loop`) as **one episode**,
run ``reflect(trajectory, signal, reward)`` at episode boundaries, admit the
resulting linguistic guidance into :class:`~loop_agent.memory.EpisodicMemory`,
and wire it into the next episode's context (report.md S4.4 pseudocode /
S5 Phase3).

**Two-signal model (the heart of this design and its safety core)**: each
episode produces two distinct signals.

- ``signal`` (:class:`~loop_agent.evaluator.GroundTruthSignal`): **ground-truth
  primary**. It comes from inner verification (test/lint/exit-code) and
  ``LoopResult.succeeded`` and is computed by the driver. All **consequential
  control** decisions -- convergence, plateauing, best score, evaluator
  promotion gates, and lesson admission -- are driven by this signal. It does
  not depend on evaluator replacement across epochs (evaluator-independent
  scale).
- ``reward`` (float): output from the rubric evaluator **fixed within the
  epoch**. Only **``reflect`` consumes** it as Reflexion verbal reinforcement.
  It is never used for convergence or admission decisions.

This structurally closes the loophole of "pushing up a gameable evaluator scalar
and declaring convergence" (report.md principle: ground truth first). Evaluators
may be replaced only at **epoch boundaries**, and only after passing the
epsilon-best-belief gate (:func:`loop_agent.evaluator.admit_evaluator`) against
fixed held-out gold data (RQGM; Issue #4).

**Dual-component separation**: separate the production path (``episode`` ->
inner run_loop, with side effects) from the evaluator-promotion measurement path
(scoring predefined :class:`~loop_agent.evaluator.HeldOut` probes, with no side
effects). Configuration verifies that their task namespaces are disjoint.

This module focuses on **single-process** self-improvement. Distributed
coordination and outer-loop persistence are out of scope for this issue (the
former is #21; the latter is a tracked follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition, StopTrigger
from .convergence import OuterState, is_success_condition
from .evaluator import (
    AdmissionResult,
    Evaluator,
    GroundTruthFn,
    GroundTruthSignal,
    HeldOut,
    admit_evaluator,
)
from .errors import ConfigError
from .loop import LoopResult
from .memory import (
    EpisodicMemory,
    Lesson,
    LessonVerdict,
    LessonVerifier,
    default_admit,
    trajectory_signatures,
)
from .state import StepRecord


@dataclass(frozen=True)
class EpisodeOutcome:
    """A **read-only** view of an inner :class:`~loop_agent.loop.LoopResult`.

    Authoritative source for the ground-truth primary signal (the result of
    inner verification). ``reflect``, pre-admission verification, and evaluators
    consult ``history`` (trajectory) and ``succeeded`` (authoritative outcome).
    """

    result: LoopResult

    @property
    def history(self) -> tuple[StepRecord, ...]:
        return tuple(self.result.history)

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    @property
    def tokens_used(self) -> int:
        return self.result.tokens_used

    @property
    def elapsed(self) -> float:
        return self.result.elapsed


@dataclass(frozen=True)
class ReflexionContext:
    """Context passed to the ``episode`` hook.

    The caller incorporates ``memory_block`` into the inner episode context or
    prompt before running the inner loop.

    - ``episode`` / ``epoch`` : current outer counters.
    - ``task``                : production task for this episode (namespace
      disjoint from held-out tasks).
    - ``evaluator``           : evaluator **fixed** for this epoch (for reward scoring).
    - ``memory_block``        : string from :meth:`EpisodicMemory.render`, used
      to wire lessons from prior attempts.
    """

    episode: int
    epoch: int
    task: Any
    evaluator: Evaluator
    memory_block: str


@dataclass
class EpisodeRecord:
    """Finalized record for one episode (audit and observation unit)."""

    episode: int
    epoch: int
    evaluator_version: str
    signal: GroundTruthSignal  # Primary signal.
    reward: float  # Label from the epoch-fixed evaluator for reflect.
    gt_aggregate: float
    lesson: Optional[Lesson] = None
    admitted: bool = False
    succeeded: bool = False
    detail: str = ""


@dataclass(frozen=True)
class EpochRecord:
    """Finalized record for one epoch boundary (audit and observation unit).

    Carries the evaluator-replacement decision. An epoch boundary is the
    **only** point where the incumbent evaluator may be replaced (RQGM safety
    gate). This record is a **read-only** observation of what happened at that
    boundary -- whether a candidate was proposed, promoted, or rejected, and how
    the version moved -- and is never used by :func:`run_reflexion` decision
    logic (an observation side channel). If ``admission`` is ``None``, no
    candidate was proposed (the evaluator is unchanged).
    """

    epoch: int  # New epoch number after advancing at the boundary.
    boundary_episode: int  # Completed episode count when the boundary occurred.
    previous_version: str  # Incumbent version before the decision.
    evaluator_version: str  # Incumbent version after the decision.
    admission: Optional[AdmissionResult] = None  # Promotion decision result, only if a candidate was proposed.

    @property
    def proposed(self) -> bool:
        """Whether a candidate evaluator was proposed at this boundary."""
        return self.admission is not None

    @property
    def promoted(self) -> bool:
        """Whether the candidate was actually promoted (proposed and accepted)."""
        return self.admission is not None and self.admission.promoted

    @property
    def decision(self) -> str:
        """Evaluator decision at the boundary: ``"unchanged"`` (not proposed), ``"promoted"``, or ``"rejected"``."""
        if self.admission is None:
            return "unchanged"
        return "promoted" if self.admission.promoted else "rejected"


@dataclass
class ReflexionState:
    """Mutable accumulator for the outer loop (convergence conditions inspect the :meth:`outer_state` projection)."""

    episode: int = 0
    epoch: int = 0
    evaluator_version: str = ""
    gt_aggregate_history: list[float] = field(default_factory=list)
    best_gt_aggregate: float = float("-inf")
    reflections: int = 0
    evaluator_updates: int = 0
    declared_keys: tuple[str, ...] = ()
    episodes: list[EpisodeRecord] = field(default_factory=list)
    memory: EpisodicMemory = field(default_factory=EpisodicMemory)

    def outer_state(self) -> OuterState:
        """Return an immutable projection for convergence conditions (:class:`~loop_agent.convergence.OuterState`)."""
        return OuterState(
            episode=self.episode,
            epoch=self.epoch,
            evaluator_version=self.evaluator_version,
            gt_aggregate_history=tuple(self.gt_aggregate_history),
            best_gt_aggregate=self.best_gt_aggregate,
            reflections=self.reflections,
            evaluator_updates=self.evaluator_updates,
            declared_keys=self.declared_keys,
        )


@dataclass
class ReflexiveResult:
    """Result of the outer loop. ``status`` is ``"converged"``, ``"stopped"``, or ``"paused"``.

    ``succeeded`` is determined from state **without depending on trigger
    order**: if a success condition (:class:`~loop_agent.convergence.RubricThreshold`)
    is satisfied at termination, the run succeeded (avoiding the ordering issue
    of inner-loop checks that depend on ``stop.name``).

    ``status == "paused"`` means the inner episode paused at a human gate
    (``stop`` is ``None`` and ``pending`` carries the pending value from the
    inner :class:`~loop_agent.loop.LoopResult`). This episode is treated as
    **incomplete**: it is not recorded, gt/reflect are not run, and the episode
    counter is not advanced. After the human gate decision is persisted,
    resuming with the same arguments reruns the same episode and lets the inner
    gate apply the decision and complete (propagating the inner pause/resume
    contract outward unchanged; Issue #15).
    """

    status: str
    stop: Optional[StopTrigger]
    state: ReflexionState
    pending: Optional[Any] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "converged"

    @property
    def paused(self) -> bool:
        return self.status == "paused"

    @property
    def best_score(self) -> float:
        return self.state.best_gt_aggregate

    @property
    def episodes(self) -> int:
        return self.state.episode

    @property
    def epochs(self) -> int:
        return self.state.epoch

    @property
    def reason(self) -> str:
        if self.paused:
            return f"paused: awaiting human decision (episode {self.state.episode})"
        return self.stop.reason if self.stop is not None else ""


# Hook types.
EpisodeFn = Callable[[ReflexionContext], LoopResult]
# reflect: (trajectory, primary signal, fixed evaluator reward) -> linguistic lesson (or None).
# The driver overwrites ``episode`` and ``support`` on the returned Lesson as
# the source of truth because the hook does not know the correct episode number
# or authoritative support. Only ``text`` and ``provenance`` come from the hook.
ReflectHook = Callable[
    [tuple[StepRecord, ...], GroundTruthSignal, float], Optional[Lesson]
]
EpisodeHook = Callable[[EpisodeRecord, ReflexionState], None]
# Observation hook for epoch boundaries. Called after the evaluator-replacement
# decision is finalized at the boundary, as a pure side channel (not used for
# control). Implementations are responsible for best-effort exception handling
# so they do not bring down the run (same contract as existing on_episode;
# observation degradation belongs to
# :class:`~loop_agent.reflexion_observe.ReflexionObserver`).
EpochHook = Callable[[EpochRecord], None]
ProposeEvaluatorFn = Callable[[OuterState, Evaluator], Optional[Evaluator]]
OuterConditions = Union[AnyOf, Sequence[StopCondition]]


def _normalize_conditions(conditions: OuterConditions) -> AnyOf:
    if isinstance(conditions, AnyOf):
        return conditions
    if isinstance(conditions, (list, tuple)):
        return AnyOf(conditions)
    raise ConfigError(
        "convergence must be an AnyOf or a sequence of stop conditions, "
        f"got {type(conditions).__name__}"
    )


def _is_success(stop: AnyOf, state: OuterState) -> bool:
    """Whether **any success condition** is satisfied at termination (order-independent).

    Because AnyOf returns the first triggered condition, deciding success from
    ``stop.name`` becomes order-dependent if a success condition and a hard
    limit trigger simultaneously under the same guard. Instead, directly ask
    whether a success condition is currently satisfied, so the outcome is stable
    regardless of condition ordering.
    """
    for condition in stop.conditions:
        if is_success_condition(condition) and condition.check(state) is not None:
            return True
    return False


def _advance_epoch_boundary(
    state: ReflexionState,
    incumbent: Evaluator,
    *,
    propose_evaluator: Optional[ProposeEvaluatorFn],
    held_out: HeldOut,
    epsilon: float,
    delta: float,
    on_epoch: Optional[EpochHook] = None,
) -> Evaluator:
    """Epoch-boundary processing: advance one epoch, the **only** place the incumbent may be replaced.

    This helper is factored out of the ``run_reflexion`` main loop with
    **behavior unchanged**. It does not alter the safety-core promotion gate
    (:func:`~loop_agent.evaluator.admit_evaluator`), the two-signal model, or
    adoption criteria; it only adds call sites. It is called both from normal
    main-loop boundaries and from resume recovery that restores a "tail
    boundary suppressed as terminal at the interruption point." This lets a
    resume that continues across a boundary reproduce the same epoch advance,
    evaluator promotion, and **observation (on_epoch) emission** as an
    uninterrupted run (Issue #29: interrupt -> resume matches uninterrupted
    execution / Issue #30: epoch observation consistency).

    The aggregate gate is measured on a rotating fold (anti-overfit), while
    fold/critical regression checks run across the full held-out set (rejecting
    sacrifices on folds that were not selected). Since ``state.epoch`` advances
    before ``held_out.fold`` is taken, fold rotation is determined by the epoch
    **after** advancing (same as the original main loop).

    ``on_epoch`` (side-channel observation) is called only after the decision is
    fully finalized, so it cannot intervene in consequential control. Passing it
    from both call sites ensures that when resume recovery restores a suppressed
    tail boundary, one ``epoch_boundary`` event is emitted and the observed
    epoch count agrees with final ``state.epoch`` (without a recovery event,
    there would be one fewer epoch_boundary than in uninterrupted execution).
    """
    state.epoch += 1
    # Keep the pre-decision version and promotion result for observation
    # (decision logic is unchanged). ``admission`` is populated only when a
    # candidate is proposed; otherwise None means the evaluator is unchanged.
    previous_version = incumbent.version
    admission: Optional[AdmissionResult] = None
    if propose_evaluator is not None:
        candidate = propose_evaluator(state.outer_state(), incumbent)
        if candidate is not None:
            admission = admit_evaluator(
                incumbent,
                candidate,
                held_out,
                epsilon=epsilon,
                delta=delta,
                measure_fold=held_out.fold(state.epoch),
            )
            incumbent = admission.chosen
            state.evaluator_version = incumbent.version
            state.evaluator_updates += 1
    if on_epoch is not None:
        on_epoch(
            EpochRecord(
                epoch=state.epoch,
                boundary_episode=state.episode,
                previous_version=previous_version,
                evaluator_version=state.evaluator_version,
                admission=admission,
            )
        )
    return incumbent


def run_reflexion(
    *,
    episode: EpisodeFn,
    ground_truth: GroundTruthFn,
    reflect: ReflectHook,
    evaluator: Evaluator,
    convergence: OuterConditions,
    declared_keys: tuple[str, ...],
    production_tasks: Sequence[Any],
    held_out: HeldOut,
    epoch_len: int = 4,
    epsilon: float = 0.02,
    delta: float = 0.0,
    propose_evaluator: Optional[ProposeEvaluatorFn] = None,
    admit_lesson: LessonVerifier = default_admit,
    memory: Optional[EpisodicMemory] = None,
    task_id: Callable[[Any], str] = str,
    on_episode: Optional[EpisodeHook] = None,
    on_epoch: Optional[EpochHook] = None,
    persist: Optional[EpisodeHook] = None,
    initial_state: Optional[ReflexionState] = None,
) -> ReflexiveResult:
    """Entry point for running the outer Reflexion loop (two-signal model + RQGM epoch gate).

    Args:
        episode: production path. Receives ``ReflexionContext``, runs the inner
            ``run_loop`` once, and returns :class:`~loop_agent.loop.LoopResult`
            (the driver does not modify the inner loop).
        ground_truth: **Primary signal source**. Builds
            :class:`~loop_agent.evaluator.GroundTruthSignal` from
            ``EpisodeOutcome`` (derived from inner verification).
        reflect: hook that extracts a linguistic lesson from
            trajectory/primary signal/reward at an episode boundary. Exceptions
            are **non-fatal** (the lesson is discarded and execution continues).
        evaluator: initial incumbent evaluator. It is fixed within each epoch
            and scores reward (the label for reflect). It is replaced only at
            boundaries via :func:`~loop_agent.evaluator.admit_evaluator`.
        convergence: :class:`~loop_agent.conditions.AnyOf` or a sequence of
            stop conditions (:mod:`loop_agent.convergence`). Reuses the same
            composition protocol as the inner loop.
        declared_keys: declared axes for diverse evaluation (aggregate is the
            minimum over declared axes; missing axes are 0.0). Must be non-empty.
        production_tasks: sequence of production tasks per episode (cycles with
            ``episode % len``).
        held_out: measurement substrate for evaluator promotion (probes with
            fixed gold labels). This is the dual-component measurement path.
        epoch_len: number of episodes in one epoch. Must be ``>= 2`` (1 would
            mean "update every episode = moving evaluator").
        epsilon: anti-churn margin for epsilon-best-belief. Must be ``> 0``.
        delta: allowed per-fold regression margin.
        propose_evaluator: hook that proposes a candidate evaluator at a
            boundary (``None`` means the evaluator is unchanged).
        admit_lesson: pre-admission verification hook (default
            :func:`~loop_agent.memory.default_admit`). **support is recomputed
            from grounding and overwritten by the driver**, so self-reported
            support has no effect. Replace this hook for semantic/effect-based
            verification.
        memory: existing :class:`EpisodicMemory` (for resume, etc.). ``None``
            creates a new memory.
        task_id: production task -> identifier. Used to verify disjointness
            from held-out tasks (default ``str``).
        on_episode: observation hook called after each episode is finalized and
            **before epoch-boundary processing** (for record auditing).
        on_epoch: observation hook called at each epoch boundary after the
            evaluator-replacement decision is finalized (pure side channel
            receiving :class:`EpochRecord`; not used for control).
        persist: persistence hook called after each episode is **fully
            finalized** (*after* boundary processing, including epoch promotion
            and evaluator replacement). While ``on_episode`` sees state *before*
            boundary processing, ``persist`` sees the **settled**
            :class:`ReflexionState` (post-boundary epoch / evaluator_version /
            evaluator_updates). Writing this to state.db makes resume from an
            interruption point match uninterrupted execution (epoch advancement,
            admitted lessons, evaluator version, and best ground truth).
            Exceptions are **not non-fatal**: if persistence fails, resume is
            impossible, so fail loudly. :class:`~loop_agent.reflexion_store.DBReflexionLog`
            is an example wiring.
        initial_state: seed for outer resume (corresponds to inner
            ``run_loop``'s ``initial_state``). Passing a :class:`ReflexionState`
            restored from state.db by
            :meth:`~loop_agent.reflexion_store.ReflexionStore.load_or_init`
            resumes from the persisted continuation point.

    Raises:
        ConfigError: if ``epoch_len < 2``, ``epsilon <= 0``, ``declared_keys``
            is empty, ``production_tasks`` is empty, or production and held-out
            task namespaces overlap.
    """
    if epoch_len < 2:
        raise ConfigError(
            "epoch_len must be >= 2 (epoch_len==1 degenerates to a moving evaluator)"
        )
    if epsilon <= 0:
        raise ConfigError("epsilon must be > 0 (anti-churn margin for evaluator promotion)")
    if not declared_keys:
        raise ConfigError("declared_keys must be non-empty (diverse evaluation)")
    if not production_tasks:
        raise ConfigError("production_tasks must be non-empty")
    # Dual-component separation: verify that production and held-out task
    # namespaces are disjoint.
    prod_ids = {task_id(t) for t in production_tasks}
    held_ids = {p.case_id for p in held_out.probes}
    overlap = prod_ids & held_ids
    if overlap:
        raise ConfigError(
            "production_tasks and held_out probes must be disjoint "
            f"(dual-component separation); overlapping ids: {sorted(overlap)}"
        )

    stop = _normalize_conditions(convergence)

    if initial_state is not None:
        # Outer resume: if the previous run promoted an evaluator at a boundary,
        # the restored state's evaluator_version points to the post-promotion
        # version. Because evaluators (callables) cannot be serialized and
        # restored, do **not silently swap in a different evaluator**. If the
        # restored version and supplied evaluator.version differ, fail loudly and
        # require the evaluator that was active at the resume point (preserving
        # the epoch-freeze audit trail across the resume join). Full restoration
        # through a version -> Evaluator registry is a follow-up.
        if (
            initial_state.evaluator_version
            and initial_state.evaluator_version != evaluator.version
        ):
            raise ConfigError(
                f"resume: persisted evaluator_version {initial_state.evaluator_version!r} does "
                f"not match supplied evaluator.version {evaluator.version!r}. Outer resume cannot "
                "reconstruct an evaluator (callables are not serializable); supply the evaluator "
                "that was active at the resume point (its version must match the persisted one)."
            )
        # Likewise, require declared_keys to match: the restored state's
        # gt_aggregate_history / best_gt_aggregate were **aggregated under the
        # declared_keys of that run**. Resuming with a different axis set could
        # make RubricThreshold and similar conditions fire on stale aggregates,
        # declaring "convergence" for a rubric the past episodes did not satisfy.
        # Reject mismatches loudly (aggregates are intentionally not recomputed
        # retroactively).
        if (
            initial_state.declared_keys
            and tuple(initial_state.declared_keys) != tuple(declared_keys)
        ):
            raise ConfigError(
                f"resume: persisted declared_keys {tuple(initial_state.declared_keys)!r} do not "
                f"match supplied {tuple(declared_keys)!r}; the persisted ground-truth aggregate "
                "history was computed under the old axes and would be stale. Supply the same "
                "declared_keys used for the original run (or start a fresh run)."
            )
        # As in the inner run_loop, do **not mutate the seed destructively**: if
        # the caller's resume snapshot were advanced in place, a failed retry
        # could resume from an already-advanced seed and skip episodes. Copy into
        # an independent state with duplicated lists and memory (EpisodeRecord /
        # Lesson are append-only and frozen, so shallow sharing is fine). If
        # memory was supplied explicitly, use it live (the caller passed that
        # object for a reason).
        state = ReflexionState(
            episode=initial_state.episode,
            epoch=initial_state.epoch,
            evaluator_version=initial_state.evaluator_version,
            gt_aggregate_history=list(initial_state.gt_aggregate_history),
            best_gt_aggregate=initial_state.best_gt_aggregate,
            reflections=initial_state.reflections,
            evaluator_updates=initial_state.evaluator_updates,
            declared_keys=initial_state.declared_keys,
            episodes=list(initial_state.episodes),
            memory=memory if memory is not None else initial_state.memory.copy(),
        )
    else:
        # Do not use `memory or EpisodicMemory()`: an empty EpisodicMemory is
        # falsy because __len__==0, which would discard an explicitly supplied
        # empty memory. Check for None explicitly.
        state = ReflexionState(memory=memory if memory is not None else EpisodicMemory())
    state.declared_keys = declared_keys
    incumbent = evaluator
    state.evaluator_version = incumbent.version

    # Tail-boundary recovery for resume: if the previous run stopped **exactly
    # at an epoch boundary** (episode % epoch_len == 0), that boundary processing
    # may have been suppressed as "terminal", leaving epoch advancement and
    # evaluator promotion unprocessed in persisted state. Main-loop boundaries
    # are suppressed when the current stop condition is already triggered. That
    # is correct for a single run, but a resume with a looser budget should not
    # miss a boundary it must cross. A continuing resume restores this **only
    # tail boundary that can be suppressed** here, reproducing the same epoch
    # advancement and evaluator promotion as uninterrupted execution. Criteria:
    # the episode count is a boundary multiple, epoch is one short of the implied
    # boundary count (= tail boundary unprocessed), and the supplied convergence
    # is not terminal right now (if terminal, keep it suppressed like a single
    # run and let the immediately following while guard return). At most one
    # tail boundary can be suppressed (all earlier boundaries were non-terminal
    # and already processed), so recovery runs at most once.
    if (
        initial_state is not None
        and state.episode > 0
        and state.episode % epoch_len == 0
        and state.epoch < state.episode // epoch_len
        and stop.first_triggered(state.outer_state()) is None
    ):
        incumbent = _advance_epoch_boundary(
            state,
            incumbent,
            propose_evaluator=propose_evaluator,
            held_out=held_out,
            epsilon=epsilon,
            delta=delta,
            on_epoch=on_epoch,
        )

    while True:
        triggered = stop.first_triggered(state.outer_state())
        if triggered is not None:
            status = "converged" if _is_success(stop, state.outer_state()) else "stopped"
            return ReflexiveResult(status=status, stop=triggered, state=state)

        task = production_tasks[state.episode % len(production_tasks)]
        ctx = ReflexionContext(
            episode=state.episode,
            epoch=state.epoch,
            task=task,
            evaluator=incumbent,
            memory_block=state.memory.render(),
        )
        result = episode(ctx)

        # If the inner episode pauses at a human gate, pause the outer loop
        # there as well and propagate pending. This episode is incomplete, so do
        # not score/reflect it and do not advance the episode counter. After the
        # human decision is persisted and resume is called, the same episode is
        # rerun and the inner gate applies the decision and completes (preventing
        # irreversible actions from being reproposed or double-executed before
        # approval; Issue #15 pause contract).
        if getattr(result, "paused", False):
            return ReflexiveResult(
                status="paused", stop=None, state=state, pending=result.pending
            )

        outcome = EpisodeOutcome(result)

        # (1) Primary signal: the driver computes it from inner verification
        # (not from the evaluator).
        signal = ground_truth(outcome)
        gt_aggregate = signal.score.aggregate(declared_keys)
        # (2) Reward: label from the evaluator fixed within the epoch (for
        # reflect only).
        reward = incumbent.score(outcome).ground_truth

        record = EpisodeRecord(
            episode=state.episode,
            epoch=state.epoch,
            evaluator_version=incumbent.version,
            signal=signal,
            reward=reward,
            gt_aggregate=gt_aggregate,
            succeeded=signal.succeeded,
        )

        # (3) Reflect at the episode boundary. Exceptions from reflect or
        # pre-admission verification are non-fatal (the lesson is discarded).
        lesson: Optional[Lesson] = None
        admitted = False
        try:
            lesson = reflect(outcome.history, signal, reward)
        except Exception as exc:  # noqa: BLE001 - reflect failure should not fail the whole run
            record.detail = f"reflect failed: {type(exc).__name__}: {exc}"
            lesson = None
        if lesson is not None:
            # Recompute and overwrite support from **authoritative grounding**
            # (do not trust self-reported support). The driver also rewrites the
            # episode as the source of truth: reflect receives only (trajectory,
            # signal, reward) and does not know the correct episode number, so
            # leaving a hook placeholder would break episode-based eviction and
            # auditing in memory (for example, a later episode's lesson could be
            # treated as ep0).
            #
            # Grounding also requires **ground_truth_backed**: episodes without
            # a real signal (tests/lint/etc.) are not included in convergence
            # history (RubricThreshold/Plateau). For the same reason, lessons
            # from such episodes get support 0 and are not admitted into memory.
            # Otherwise, an unverified episode could "not count toward
            # convergence but still rewrite the next context", affecting
            # production behavior and violating the ground-truth-primary
            # invariant.
            grounded = (
                signal.ground_truth_backed
                and lesson.provenance in trajectory_signatures(outcome.history)
            )
            lesson = replace(
                lesson,
                support=1.0 if grounded else 0.0,
                episode=state.episode,
            )
            try:
                verdict: LessonVerdict = admit_lesson(lesson, outcome)
            except Exception as exc:  # noqa: BLE001
                verdict = LessonVerdict(admit=False, reason=f"verifier error: {exc}")
            admitted = state.memory.admit(lesson, verdict)
            if admitted:
                state.reflections += 1
        record.lesson = lesson
        record.admitted = admitted

        # Add only primary signals to convergence history (episodes without real
        # signals do not count).
        if signal.ground_truth_backed:
            state.gt_aggregate_history.append(gt_aggregate)
            state.best_gt_aggregate = max(state.best_gt_aggregate, gt_aggregate)
        state.episodes.append(record)
        state.episode += 1

        if on_episode is not None:
            on_episode(record, state)

        # (4) Epoch boundary: the **only** place the incumbent may be replaced.
        # However, do not promote if this episode already satisfies convergence
        # or cutoff conditions: running propose/admit when there are no later
        # episodes could unnecessarily rewrite the terminal run's
        # evaluator_version or let a proposal hook exception bring down a run
        # that has already reached termination (the next while guard will
        # immediately fire the same stop and return).
        if (
            state.episode % epoch_len == 0
            and stop.first_triggered(state.outer_state()) is None
        ):
            incumbent = _advance_epoch_boundary(
                state,
                incumbent,
                propose_evaluator=propose_evaluator,
                held_out=held_out,
                epsilon=epsilon,
                delta=delta,
                on_epoch=on_epoch,
            )

        # (5) Persistence join: after the episode is **fully finalized**
        # (including epoch-boundary processing), write the settled state. Unlike
        # on_episode (which runs *before* the boundary), the epoch /
        # evaluator_version / evaluator_updates visible here are post-boundary.
        # If this is the SoT written to state.db, "resume from interruption point
        # = uninterrupted execution" holds. Paused episodes never reach this
        # line and are not persisted, so incomplete episodes are not written.
        if persist is not None:
            persist(record, state)


__all__ = [
    "EpisodeOutcome",
    "ReflexionContext",
    "EpisodeRecord",
    "EpochRecord",
    "ReflexionState",
    "ReflexiveResult",
    "run_reflexion",
    "EpisodeFn",
    "ReflectHook",
    "EpisodeHook",
    "EpochHook",
    "ProposeEvaluatorFn",
]
