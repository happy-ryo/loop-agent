"""The PoC loop driver: gather -> act -> verify -> repeat (report.md S4.4).

A single-agent, single-process driver. ``act`` and ``verify`` are injected
callables (hooks), so the engine carries no LLM dependency -- the PoC drives it
with in-memory stubs and the same seam later wraps a real model call.

Termination is graceful and reason-bearing:

- the loop ends *naturally* when ``verify`` reports the goal is met, or
- it is *stopped* when one of the composed mechanical caps fires first.

Either way the driver returns a :class:`LoopResult` describing the outcome; it
never raises to signal "limit reached".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Union, runtime_checkable

from .conditions import AnyOf, GoalMet, StopCondition, StopTrigger
from .state import LoopState, StepRecord

# 人間ゲートの disposition: 提案 action をそのまま実行 / 実行せず記録だけ / 中断。
GATE_PROCEED = "proceed"
GATE_SKIP = "skip"
GATE_PAUSE = "pause"


class _KeepContext:
    """Sentinel for :attr:`GateReview.context` left unset on a PROCEED.

    Distinguishes "proceed with the gathered context unchanged" (the default)
    from "proceed with an explicitly *edited* context" -- including an edit to a
    literal ``None``. A bare ``GateReview(disposition=GATE_PROCEED)`` therefore
    runs ``act`` on the originally gathered action, never on ``None``.
    """

    _singleton: "Optional[_KeepContext]" = None

    def __new__(cls) -> "_KeepContext":
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __repr__(self) -> str:
        return "<keep-gathered-context>"


KEEP_CONTEXT = _KeepContext()


@dataclass
class ActOutcome:
    """What one ``act`` invocation produced.

    ``tokens`` is the cost charged to :class:`~claude_loop.conditions.TokenBudget`
    for this step; stubs may report ``0``.
    """

    observation: Any = None
    tokens: int = 0


@dataclass
class VerifyOutcome:
    """Ground-truth check on an :class:`ActOutcome` (report.md R1).

    ``goal_met=True`` ends the loop naturally; ``detail`` is recorded for logs.
    """

    goal_met: bool
    detail: str = ""


@dataclass
class GateReview:
    """A human gate's verdict on a proposed action, consumed by the driver.

    ``disposition`` is one of :data:`GATE_PROCEED` (run ``act`` on
    :attr:`context`; left at :data:`KEEP_CONTEXT` the gathered action runs
    unchanged, set it to supply an *edited* action), :data:`GATE_SKIP`
    (do *not* execute -- record :attr:`observation` / :attr:`detail` as a step
    and continue, e.g. a reject/respond), or :data:`GATE_PAUSE` (stop the loop
    now and return a ``"paused"`` result carrying :attr:`pending`, to be
    resumed once a human records a decision).

    The driver stays gate-agnostic: it only understands these three
    dispositions. The store/human lifecycle lives behind the gate object
    (:class:`claude_loop.gate.HumanGate`).
    """

    disposition: str
    context: Any = KEEP_CONTEXT
    observation: Any = None
    detail: str = ""
    pending: Optional[Any] = None
    # GATE_SKIP のとき、この skip を観測フック (on_step) に流すか。既定 True。
    # resume 再生で既実行ゲートを読み飛ばすだけの "replay no-op" な skip は False にして、
    # 前 run が永続化済みの本来の step 行を上書き (UNIQUE(run_id, iteration) upsert) で
    # 壊さないようにする。
    persist: bool = True


@runtime_checkable
class ActionGate(Protocol):
    """A pre-act interception point for limited human gating (report.md R6).

    Evaluated *before* ``act`` executes the gathered context, so an irreversible
    action can be intercepted before its side effect. Returns a
    :class:`GateReview` telling the driver to proceed, skip, or pause.
    """

    def review(self, context: Any, state: LoopState) -> GateReview:
        ...


@dataclass
class LoopResult:
    """Outcome of a loop run.

    ``stop`` is ``None`` on *natural* termination (the ``verify`` hook met the
    goal) and on a ``"paused"`` result (the loop was interrupted by a human gate
    before any cap fired). Otherwise it names the fired condition -- which may
    itself be a success (a ``GoalMet`` stop, ``stop.name == "goal_met"``) or a
    halt (``no_progress`` / a mechanical cap). Prefer :attr:`succeeded` over
    :attr:`goal_met` to test for success regardless of channel.

    ``pending`` is set only when ``status == "paused"``: it describes the gated
    action awaiting a human decision (the decision itself is persisted in the
    store, so resuming the run honours it).
    """

    status: str  # "goal_met" | "stopped" | "paused"
    stop: Optional[StopTrigger]
    state: LoopState
    pending: Optional[Any] = None

    @property
    def goal_met(self) -> bool:
        """True only for *natural* termination via the ``verify`` hook.

        This reflects the ``verify``-hook channel specifically (``status ==
        "goal_met"``). A goal reached instead by a :class:`~claude_loop.conditions.GoalMet`
        *stop condition* terminates with ``status == "stopped"`` and leaves this
        ``False`` -- use :attr:`succeeded` to detect success across both channels.
        """
        return self.status == "goal_met"

    @property
    def succeeded(self) -> bool:
        """True when the goal was reached by *either* success channel.

        The goal can be verified two ways (report.md S4.5): the ``verify`` hook
        ending the loop naturally (:attr:`goal_met`), or a
        :class:`~claude_loop.conditions.GoalMet` stop condition firing at the
        guard (``stop.name == "goal_met"``). Both are successes, distinct from a
        ``NoProgress`` abort or a mechanical cut-off; this collapses them so a
        caller can ask "did it succeed?" without knowing which channel fired.
        """
        if self.goal_met:
            return True
        return self.stop is not None and self.stop.name == GoalMet.name

    @property
    def iterations(self) -> int:
        return self.state.iteration

    @property
    def tokens_used(self) -> int:
        return self.state.tokens_used

    @property
    def elapsed(self) -> float:
        return self.state.elapsed

    @property
    def history(self) -> list[StepRecord]:
        return self.state.history

    @property
    def paused(self) -> bool:
        """True when the run was interrupted by a human gate (awaiting a decision)."""
        return self.status == "paused"

    @property
    def reason(self) -> str:
        """Human-readable reason the loop ended (or paused)."""
        if self.goal_met:
            return "goal met"
        if self.paused:
            key = ""
            if isinstance(self.pending, dict):
                key = self.pending.get("gate_key", "")
            suffix = f" ({key})" if key else ""
            return f"paused: awaiting human decision{suffix}"
        return self.stop.reason if self.stop is not None else ""


GatherHook = Callable[[LoopState], Any]
ActHook = Callable[[Any], ActOutcome]
VerifyHook = Callable[[ActOutcome], VerifyOutcome]
StepHook = Callable[[StepRecord, LoopState], None]
Conditions = Union[AnyOf, list[StopCondition], tuple[StopCondition, ...]]


def _default_gather(state: LoopState) -> LoopState:
    """Pass the state through as context when no gather hook is supplied."""
    return state


def run_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    gate: Optional[ActionGate] = None,
    time_fn: Callable[[], float] = time.monotonic,
) -> LoopResult:
    """Drive gather -> act -> verify -> repeat until the goal or a cap.

    Args:
        act: Hook producing an :class:`ActOutcome` from the gathered context.
        verify: Hook turning an :class:`ActOutcome` into a :class:`VerifyOutcome`;
            ``goal_met=True`` terminates the loop naturally.
        conditions: An :class:`~claude_loop.conditions.AnyOf`, or any non-empty
            sequence of stop conditions (wrapped in ``AnyOf`` automatically).
        gather: Hook building the context handed to ``act``. Defaults to passing
            the :class:`LoopState` through.
        on_step: Optional observer invoked with ``(record, state)`` after each
            completed iteration (a minimal observability seam; report.md R7).
        gate: Optional limited human gate (report.md R6). When supplied, its
            ``review(context, state)`` runs *between* ``gather`` and ``act`` --
            i.e. after the action is proposed but before it executes -- so an
            irreversible action can be intercepted before its side effect. The
            gate may let the step proceed (optionally with an *edited* context),
            skip it (record a non-executing step and continue, e.g. a reject /
            respond), or pause the run (return a ``"paused"`` result). Reversible
            actions and a ``None`` gate add no overhead and never interrupt.
        time_fn: Monotonic clock, injectable for deterministic timeout tests.

    Returns:
        A :class:`LoopResult`. ``status`` is ``"goal_met"`` (``stop is None``),
        ``"stopped"`` (``stop`` names the fired condition), or ``"paused"``
        (``stop is None``, ``pending`` describes the gated action awaiting a
        human decision).

    Stop conditions are evaluated at the top of each cycle (the while-guard),
    *before* a new step starts -- including before the very first one, so e.g.
    ``MaxIterations(0)`` returns immediately with zero iterations.
    """
    if isinstance(conditions, AnyOf):
        stop = conditions
    elif isinstance(conditions, (list, tuple)):
        stop = AnyOf(conditions)
    else:
        raise TypeError(
            "conditions must be an AnyOf or a sequence of stop conditions, "
            f"got {type(conditions).__name__}"
        )

    # Let a stateful gate reset any per-run counters at the start of this run, so
    # the same gate instance can be reused across pause/resume runs without its
    # gate-key sequence drifting (optional hook; gates without it are unaffected).
    if gate is not None:
        begin = getattr(gate, "begin", None)
        if callable(begin):
            begin()

    start = time_fn()
    state = LoopState()

    while True:
        state.elapsed = time_fn() - start
        triggered = stop.first_triggered(state)
        if triggered is not None:
            return LoopResult(status="stopped", stop=triggered, state=state)

        context = gather(state)

        if gate is not None:
            review = gate.review(context, state)
            if review.disposition == GATE_PAUSE:
                # Interrupt before the irreversible side effect. No step is
                # recorded for the un-executed action; the decision is persisted
                # behind the gate so a resumed run honours it (report.md R6).
                state.elapsed = time_fn() - start
                return LoopResult(
                    status="paused", stop=None, state=state, pending=review.pending
                )
            if review.disposition == GATE_SKIP:
                # The human declined to execute (reject/respond): record the
                # decision as a zero-cost step and re-enter the guard, so caps
                # and NoProgress still see and bound the gated cycle.
                record = StepRecord(
                    iteration=state.iteration,
                    observation=review.observation,
                    tokens=0,
                    goal_met=False,
                    detail=review.detail,
                )
                state.history.append(record)
                state.iteration += 1
                state.elapsed = time_fn() - start
                # replay no-op な skip (review.persist=False) は on_step を呼ばない:
                # 前 run が永続化した本来の step 行を上書きで壊さないため。
                if on_step is not None and review.persist:
                    on_step(record, state)
                continue
            if review.disposition != GATE_PROCEED:
                # Fail closed: an unrecognised disposition (e.g. a typo'd
                # "paused") must NOT silently fall through to executing the
                # action -- for a safety gate that could run an irreversible
                # side effect instead of pausing. Reject loudly instead.
                raise ValueError(
                    f"gate returned unknown disposition {review.disposition!r}; "
                    f"expected one of {GATE_PROCEED!r}/{GATE_SKIP!r}/{GATE_PAUSE!r}"
                )
            # GATE_PROCEED: execute the (possibly edited) action. An unset
            # context keeps the gathered action; only an explicit value (an
            # edit) replaces it -- so a bare proceed never passes None to act.
            if review.context is not KEEP_CONTEXT:
                context = review.context

        outcome = act(context)
        state.tokens_used += outcome.tokens

        verdict = verify(outcome)
        record = StepRecord(
            iteration=state.iteration,
            observation=outcome.observation,
            tokens=outcome.tokens,
            goal_met=verdict.goal_met,
            detail=verdict.detail,
        )
        state.history.append(record)
        state.iteration += 1
        # Refresh post-step fields *before* on_step so the observer (and the
        # returned result) see state consistent with this iteration's record:
        # elapsed includes the step just run, and goal_met reflects its verdict.
        state.elapsed = time_fn() - start
        if verdict.goal_met:
            state.goal_met = True

        if on_step is not None:
            on_step(record, state)

        if verdict.goal_met:
            return LoopResult(status="goal_met", stop=None, state=state)
