"""``act`` composition adapter that escalates hard tasks to stronger models.

``ModelLadder`` is **not a new loop-agent core feature**. The ``act`` seam is already
``Callable[[context], ActOutcome]``, so users can already write escalation such as
"if the previous model failed, pass the task to the next stronger model" themselves
(README path D's ``escalating_act`` is exactly that). This module is the canonical
example that **implements the frequently written ModelLadder pattern correctly in one
place**, improving discoverability and avoiding common pitfalls (stateful attempt
counts, the fact that act cannot see verify's goal verdict, and heterogeneous adapter
composition) (Issue #53). It does not change core ``run_loop`` at all; it plugs in as
an ``act`` hook returning ``ActOutcome``.

Usage::

    from loop_agent import run_loop
    from loop_agent.adapters import ModelLadder, ClaudeCodeAct

    act = ModelLadder([
        ClaudeCodeAct(model="haiku"),
        ClaudeCodeAct(model="sonnet"),
        ClaudeCodeAct(model="opus"),
    ], escalate_on="failure")

    result = run_loop(act=act, verify=..., gather=..., conditions=...)

Heterogeneous chains across LLM providers work as-is. Start with a cost-optimal model
and fall back to a stronger provider only for hard spots::

    from loop_agent.adapters import ModelLadder, ClaudeCodeAct, CodexAct

    act = ModelLadder([
        ClaudeCodeAct(model="haiku"),
        CodexAct(model="gpt-5.5"),
        ClaudeCodeAct(model="opus"),
    ])

Each rung may be any ``ActHook``-compatible callable. If its result satisfies the
shared :class:`~loop_agent.adapters.base.ActResult` contract (has
``observation.failed``), ``ModelLadder`` can apply the same decision logic even when
mixing adapter types. The #52 ``ActResult`` Protocol provides that composability.

Design position, and why this shape:

- **Stateful**: an ``act`` hook receives only ``context`` and **cannot see** the later
  ``verify`` goal verdict (run_loop is gather -> act -> verify, and the verdict does
  not return to act). Therefore ``ModelLadder`` keeps the previous outcome and
  per-candidate attempt counts itself, then decides which rung to call next from that
  history. One ladder instance corresponds to one run; call :meth:`reset` before
  reusing it for another run.
- **The only failure act can observe is ``observation.failed``** (crash, non-zero
  exit, timeout, or launch failure). The ``failure`` strategy cannot catch "act
  succeeded (``failed=False``), but verify judged the goal unmet." The
  ``attempt_count`` strategy fills that gap by escalating after N tries per rung,
  regardless of success/failure. The two strategies are complementary; see
  ``escalate_on`` on :class:`ModelLadder` below.
- **Monotonic**: once it moves to a higher rung, it never moves back down; it keeps
  escalating to stronger models. At the strongest rung, it sticks there and retries,
  matching ``MockClaudeCodeAct``'s "keep returning the current best action" behavior.
  Boundaries such as ``MaxIterations`` still stop it safely.
- **Only responsible for which rung to call**: ``ModelLadder`` does not rewrite prompts
  or inject Reflexion lessons. Lesson accumulation composes orthogonally through
  ``run_reflexion`` / ``gather`` (README path D's Reflexion composition). Keeping the
  responsibility to model selection makes it easy to layer with Reflexion and
  WorkListGather.

``ModelLadder`` is **an adapter that composes act hooks**, not a subprocess-launching
CLI adapter like ``ClaudeCodeAct`` / ``CodexAct``. It therefore has no
``build_command`` / ``runner`` / ``parse_tokens`` and is not included in the
``tests/adapters`` subprocess contract harness (``ADAPTER_SPECS``). Token accounting,
``failed``, and graceful-exit guarantees belong to each rung (the composed CLI
adapter). ``ModelLadder`` passes each rung's ``ActOutcome`` through **unchanged**,
including tokens, so ``TokenBudget`` works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Union

from ..errors import ConfigError
from ..loop import ActHook, ActOutcome

__all__ = [
    "ModelLadder",
    "EscalationContext",
    "EscalationPredicate",
    "on_failure",
    "after_attempts",
]


@dataclass(frozen=True)
class EscalationContext:
    """Snapshot of ladder state passed to an escalation predicate.

    The predicate is evaluated **before** the next ``act`` call. If it returns
    ``True``, ``ModelLadder`` escalates to the next stronger candidate before calling
    it. All fields describe history observed so far; they do not include the result of
    the candidate about to be called, so decisions are based only on the past.

    Attributes:
        candidate_index: zero-based index of the currently active candidate: the one
            that produced ``last_outcome``. If no escalation happens, this candidate
            will be called again.
        num_candidates: total number of ladder candidates, useful for detecting the
            final rung.
        attempts: number of times the active candidate has been called so far. Reset
            to 0 on escalation.
        total_attempts: total ladder calls across all candidates.
        last_outcome: :class:`ActOutcome` returned by the previous ``act`` call, or
            ``None`` before the first call.
        last_failed: convenience value for ``last_outcome.observation.failed``.
            ``False`` when ``last_outcome`` is ``None`` or the observation has no
            ``failed`` attribute. The ``failure`` strategy reads this value.
    """

    candidate_index: int
    num_candidates: int
    attempts: int
    total_attempts: int
    last_outcome: Optional[ActOutcome]
    last_failed: bool


# Escalation decision: inspect current state and return whether to move up one rung
# before the next call.
EscalationPredicate = Callable[[EscalationContext], bool]


def on_failure(ec: EscalationContext) -> bool:
    """Strategy that escalates when the previous ``act`` **failed**.

    ``escalate_on="failure"`` resolves to this function. If the previous rung returned
    ``failed=True`` because of a crash, non-zero exit, timeout, or launch failure, the
    next iteration is passed to one stronger model.

    Note: this **cannot catch** the case where act returns ``failed=False`` but verify
    judges the goal unmet, because act cannot see verify's verdict. Use or compose
    :func:`after_attempts` for that case; see the module docstring.
    """
    return ec.last_failed


def after_attempts(n: int) -> EscalationPredicate:
    """Create a strategy that escalates after calling the same candidate **N times**.

    ``escalate_on=N`` (int) resolves to this function. It gives up on a model after N
    attempts even when act itself succeeded, covering "act succeeded but verify still
    says the goal is unmet" cases that :func:`on_failure` cannot observe.

    For example, ``after_attempts(2)`` calls each candidate twice before moving to the
    next rung.

    To escalate after **N failures**, compose a predicate::

        escalate_on=lambda ec: ec.last_failed and ec.attempts >= 2
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ConfigError(f"after_attempts(n) requires a positive int, got {n!r}")

    def _predicate(ec: EscalationContext) -> bool:
        return ec.attempts >= n

    return _predicate


def _resolve_strategy(
    escalate_on: Union[str, int, EscalationPredicate],
) -> EscalationPredicate:
    """Resolve the ``escalate_on`` argument to the concrete predicate.

    - callable -> used as-is (custom predicate)
    - ``"failure"`` -> :func:`on_failure`
    - positive int N -> ``after_attempts(N)``
    Anything else raises a clear :class:`~loop_agent.errors.ConfigError` (``True``,
    ``0``, unknown strings, and so on).
    """
    # bool is an int subclass, so reject it before int handling (e.g. escalate_on=True).
    if isinstance(escalate_on, bool):
        raise ConfigError(
            f"escalate_on must be 'failure', a positive int, or a predicate; "
            f"got bool {escalate_on!r}"
        )
    if callable(escalate_on):
        return escalate_on
    if isinstance(escalate_on, int):
        return after_attempts(escalate_on)
    if isinstance(escalate_on, str):
        if escalate_on == "failure":
            return on_failure
        raise ConfigError(
            f"unknown escalate_on strategy {escalate_on!r}; "
            "use 'failure', a positive int (attempt count), or a predicate "
            "Callable[[EscalationContext], bool]"
        )
    raise ConfigError(
        f"escalate_on must be 'failure', a positive int, or a predicate; "
        f"got {type(escalate_on).__name__}"
    )


def _outcome_failed(outcome: Optional[ActOutcome]) -> bool:
    """Return whether ``outcome`` is a failed observation; missing values are false."""
    if outcome is None:
        return False
    return bool(getattr(outcome.observation, "failed", False))


@dataclass
class ModelLadder:
    """``act`` composition hook that escalates to stronger models step by step.

    This is a canonical example, not a new core feature. Put ``candidates`` in
    weak-to-strong order and choose when to move to the next rung with the
    ``escalate_on`` strategy. The ladder itself is an ``ActHook``
    (``Callable[[context], ActOutcome]``), so ``run_loop(act=ladder, ...)`` plugs it in
    directly. On each iteration it calls one active candidate and passes that
    :class:`ActOutcome` through **unchanged**, including tokens, so ``TokenBudget``
    works unchanged.

    The escalation decision is made **before calling the candidate for that
    iteration**, using only previous history (:class:`EscalationContext`). Because act
    cannot see the later verify goal verdict, the ladder decides from attempt history
    it stores itself. Escalation is monotonic: once it moves up, it never moves down.
    At the strongest rung it sticks there and retries until boundary conditions stop it.

    Args:
        candidates: ``act`` hooks ordered weak-to-strong, one per rung. Empty is not
            allowed. Each rung is any callable returning ``ActOutcome``; heterogeneous
            adapters such as ``ClaudeCodeAct`` + ``CodexAct`` can be mixed.
        escalate_on: escalation strategy, one of:

            - ``"failure"`` (default): escalate when the previous rung returned
              ``failed=True``
              (:func:`on_failure`).
            - positive int ``N``: call the same rung N times, then escalate regardless
              of success/failure (``after_attempts(N)``). This complements failure
              handling for cases where act succeeds but verify keeps the loop going.
            - predicate ``Callable[[EscalationContext], bool]``: custom decision;
              ``True`` escalates. Useful for composing strategies, for example
              ``lambda ec: ec.last_failed and ec.attempts >= 2``.

    Read-only attributes:
        current_index: index of the currently active candidate.
        current: currently active candidate callable.
        attempts: number of times the active candidate has been called.
        total_attempts: total ladder calls.
        at_top: whether the strongest rung (last candidate) has been reached.
    """

    candidates: Sequence[ActHook]
    escalate_on: Union[str, int, EscalationPredicate] = "failure"

    _index: int = field(default=0, init=False, repr=False)
    _attempts: int = field(default=0, init=False, repr=False)
    _total: int = field(default=0, init=False, repr=False)
    _last_outcome: Optional[ActOutcome] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ConfigError("ModelLadder requires at least one candidate")
        self._should_escalate: EscalationPredicate = _resolve_strategy(self.escalate_on)

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def current(self) -> ActHook:
        return self.candidates[self._index]

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def total_attempts(self) -> int:
        return self._total

    @property
    def at_top(self) -> bool:
        return self._index >= len(self.candidates) - 1

    def reset(self) -> None:
        """Reset internal state: index, attempt counts, and previous outcome.

        ``ModelLadder`` is stateful, so call this before reusing one instance for
        another run. Otherwise it carries over the previous run's escalation state.
        """
        self._index = 0
        self._attempts = 0
        self._total = 0
        self._last_outcome = None

    def __call__(self, context: Any) -> ActOutcome:
        # If not at the final rung, decide whether to escalate before this iteration
        # using history only. Escalation moves one rung at a time and resets attempts.
        if not self.at_top:
            ec = EscalationContext(
                candidate_index=self._index,
                num_candidates=len(self.candidates),
                attempts=self._attempts,
                total_attempts=self._total,
                last_outcome=self._last_outcome,
                last_failed=_outcome_failed(self._last_outcome),
            )
            if self._should_escalate(ec):
                self._index += 1
                self._attempts = 0

        outcome = self.candidates[self._index](context)

        # Update attempt history for the next decision. Pass outcome through unchanged.
        self._attempts += 1
        self._total += 1
        self._last_outcome = outcome
        return outcome
