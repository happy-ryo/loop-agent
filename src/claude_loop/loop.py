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
from typing import Any, Callable, Optional, Union

from .conditions import AnyOf, GoalMet, StopCondition, StopTrigger
from .state import LoopState, StepRecord


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
class LoopResult:
    """Outcome of a loop run.

    ``stop`` is ``None`` only on *natural* termination (the ``verify`` hook met
    the goal). Otherwise it names the fired condition -- which may itself be a
    success (a ``GoalMet`` stop, ``stop.name == "goal_met"``) or a halt
    (``no_progress`` / a mechanical cap). Prefer :attr:`succeeded` over
    :attr:`goal_met` to test for success regardless of channel.
    """

    status: str  # "goal_met" | "stopped"
    stop: Optional[StopTrigger]
    state: LoopState

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
    def reason(self) -> str:
        """Human-readable reason the loop ended."""
        if self.goal_met:
            return "goal met"
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
    time_fn: Callable[[], float] = time.monotonic,
    initial_state: Optional[LoopState] = None,
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
        time_fn: Monotonic clock, injectable for deterministic timeout tests.
        initial_state: Seed the loop with already-accumulated state to **resume**
            an interrupted run (report.md S4.4 / S5 Phase 2, Issue #14). Pass the
            :class:`LoopState` reconstructed by
            :meth:`~claude_loop.store.LoopStore.load_or_init` (or
            :attr:`~claude_loop.store.DBProgressLog.state`): the loop continues
            from its ``iteration`` / ``tokens_used`` / ``goal_met`` / ``history``
            instead of starting empty, and ``elapsed`` keeps accumulating from
            the persisted value (the wall-clock origin is back-dated by it so
            stop conditions like :class:`~claude_loop.conditions.Timeout` see the
            *total* run time, not just this leg). ``None`` (the default) starts a
            fresh run; an empty :class:`LoopState` is equivalent to ``None``. The
            seed is copied, so the caller's object is not mutated.

    Returns:
        A :class:`LoopResult`. ``status`` is ``"goal_met"`` (``stop is None``) or
        ``"stopped"`` (``stop`` names the fired condition).

    Stop conditions are evaluated at the top of each cycle (the while-guard),
    *before* a new step starts -- including before the very first one, so e.g.
    ``MaxIterations(0)`` returns immediately with zero iterations. On resume this
    means a run already at or past a cap (e.g. resumed ``elapsed`` >= a
    ``Timeout``) stops immediately with no further step, exactly as a straight
    run would have. Resume is only meaningful for hooks that derive their verdict
    from the (gathered) state rather than from in-process call counters, since a
    fresh process rebuilds the hooks but not their private counters; pair resume
    with state-based stop conditions (e.g. :class:`~claude_loop.conditions.GoalMet`)
    for a run that reproduces a straight-through result exactly.

    One fidelity caveat when the seed was reconstructed from the state.db SoT
    (:meth:`~claude_loop.store.LoopStore.load_or_init`): ``history`` observations
    survive a JSON round-trip, so non-JSON-native types drift (``tuple`` ->
    ``list``, ``dict`` int-keys -> ``str``, sets/custom objects/NaN -> ``repr``
    string). A condition that *keys* directly on the raw ``observation`` --
    notably :class:`~claude_loop.conditions.NoProgress`'s default key -- can then
    diverge across the seam (a ``tuple`` becomes an unhashable ``list``; other
    types re-key), so its window straddling the resume point may fire at a
    different iteration or raise. Use JSON-stable observations, or give such a
    condition a ``key`` projecting onto a JSON-stable signature (e.g.
    ``NoProgress(key=lambda r: json.dumps(r.observation, sort_keys=True, default=repr))``),
    when the run must resume identically.
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

    # Copy the seed rather than mutate the caller's object: the loop mutates
    # `state` in place throughout, and aliasing the reconstructed state (e.g.
    # DBProgressLog.state) to the live loop would surprise a caller inspecting
    # it. history is shallow-copied -- StepRecords are only appended, never
    # mutated, so the records themselves can be shared.
    if initial_state is None:
        state = LoopState()
    else:
        state = LoopState(
            iteration=initial_state.iteration,
            tokens_used=initial_state.tokens_used,
            elapsed=initial_state.elapsed,
            goal_met=initial_state.goal_met,
            history=list(initial_state.history),
        )
    # Back-date the clock origin by the already-elapsed time so `elapsed`
    # continues accumulating from the persisted value across the resume seam
    # (for a fresh run state.elapsed is 0.0, so start == time_fn()).
    start = time_fn() - state.elapsed

    while True:
        state.elapsed = time_fn() - start
        triggered = stop.first_triggered(state)
        if triggered is not None:
            return LoopResult(status="stopped", stop=triggered, state=state)

        context = gather(state)
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
