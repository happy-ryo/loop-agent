"""Fair scheduling gather for multi-item loops (Issue #56).

The ``run_loop`` ``gather`` hook is ``Callable[[LoopState], ctx]``: one point
that selects "what to do next" from state (report.md S4.4). When running N files
or N bugs through a single loop, a naive ``gather`` ("return the first unfinished
item") lets **one item monopolize ``MaxIterations`` and starve the rest**: one
repeatedly failing item can consume every iteration, ending the loop before the
remaining items are touched even once.

#37 (Self-translation PoC) avoided this with a handwritten round-robin gather::

    def gather(state):
        rem = [f for f in files if f not in done]
        return min(rem, key=lambda f: (attempts[f], files.index(f)))   # fair scheduling

:class:`WorkListGather` normalizes this pattern into a reusable component. It
provides:

- **fair scheduling strategies** (``round_robin`` / ``fewest_attempts`` /
  ``fifo`` / ``priority`` / any custom callable): which item receives the next
  iteration.
- **per-item limits** (``max_attempts_per_item``): prevent one item from
  monopolizing the loop by marking it *exhausted* and removing it after the
  configured number of attempts if it is still incomplete (independent of the
  global ``MaxIterations``).
- **done predicate hook** (``done_when``): a user policy that decides whether
  "*this item* is done", independent of verify (the loop-wide goal).
- **canonical attempt counter API**: read progress through
  :meth:`WorkListGather.attempts`, :meth:`~WorkListGather.report`, and related
  methods.
- **triage integration** (:meth:`WorkListGather.from_triage`): delegate
  work-list priority and ordering calculations to the existing
  :func:`loop_agent.discovery.triage`.

**Resume safety (derived from state)**: :class:`WorkListGather` keeps no
in-process counters. attempts / done / exhausted are deterministically derived
by replaying ``state.history`` each time (because the scheduling strategy is a
pure function of ``(attempts, done, exhausted, last_selected)``, so each
iteration can reconstruct what it dispatched). Calling it with the same
``LoopState`` in another process or after resume therefore makes the same
decision, following the README policy that if decisions are derived from
gathered state, a new process reaches the same decision (resume #14). Because
``done_when`` reads ``StepRecord``, it should inspect fields that do not drift
across JSON round trips (``goal_met`` / JSON-native ``observation``), matching
the resume fidelity note in loop.py.

**Drained and loop stopping**: Once every item is done or exhausted, gather has
no item to return (it returns :data:`DRAINED`). Stopping the loop is the stop
condition's job, not gather's, so always compose :class:`WorkListDrained` into
``conditions``. Stop conditions are evaluated at the *start* of each iteration
(before gather), so the loop stops before gather is called once the work list is
drained (:data:`DRAINED` is never passed to ``act``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Mapping, Optional, Sequence, Union

from ..errors import ConfigError
from ..state import LoopState, StepRecord
from ._triage import triage


@dataclass(frozen=True)
class WorkItem:
    """One schedulable unit (file / bug / task).

    Args:
        id: Stable identifier (non-empty and unique within the work list). Used
            as the aggregation key for attempts and done.
        priority: Priority used by the ``priority`` strategy. **Higher means
            higher priority**. Defaults to 0.
        payload: Arbitrary value to pass to ``act`` when selected (file path,
            task body, seed, etc.). The default ``build_ctx`` makes the
            JSON-native dict ``{"id", "attempt", "priority", "payload"}`` the
            ``act`` context, so ``act`` can read ``ctx["payload"]`` /
            ``ctx["id"]``. ``payload`` itself should also be JSON-native if
            composed with a persistent gate.
    """

    id: str
    priority: int = 0
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ConfigError("WorkItem.id must be a non-empty string")


@dataclass(frozen=True)
class ScheduleContext:
    """Read-only view passed to scheduling strategies.

    It contains the items selectable in this iteration and aggregate values. A
    custom callable strategy receives this object and returns one item from
    ``selectable`` (either a :class:`WorkItem` or its ``id``). Returning anything
    outside ``selectable`` makes :class:`WorkListGather` fail loudly with
    ``ConfigError`` so done / exhausted items are not accidentally reselected.

    **Difference between ``attempts`` and ``selections``**: ``attempts[id]`` is
    the number of *executed* attempts (for per-item limits, done checks, and
    ModelLadder). ``selections[id]`` is the number of times the item was
    *selected* (offered), including offers that ``item_of`` classified as
    non-executed (``None``), such as gate SKIP. **Fairness is measured by
    ``selections``**: skipped items move back as "offered once" so the same item
    is not presented forever while starving the rest. Without ``item_of``, the
    two counts are identical.
    """

    selectable: tuple[WorkItem, ...]
    attempts: Mapping[str, int]
    selections: Mapping[str, int]
    position: Mapping[str, int]
    last_selected: Optional[str]
    done: frozenset[str]
    exhausted: frozenset[str]


# Scheduling strategy: a pure function that picks the next item from ``ScheduleContext``.
Scheduler = Callable[[ScheduleContext], Union[WorkItem, str]]

# Hook that converts an item into the ``act`` context (item, prior attempts, state).
ContextBuilder = Callable[[WorkItem, int, LoopState], Any]

# User policy that decides whether "*this item* is done" (independent of verify).
DonePredicate = Callable[[WorkItem, StepRecord], bool]

# Returns which item a record was actually produced by acting on
# (``None`` = non-executed / unattributed). The default (``None`` hook)
# treats "selected item == acted item" during schedule replay.
ItemAttributor = Callable[[StepRecord], Optional[str]]


def _strat_fewest_attempts(ctx: ScheduleContext) -> WorkItem:
    """Fewest selections, then original order.

    This is the same fair strategy as the #37 PoC (round-robin equivalent).
    Fairness is measured by ``selections`` (offer count). Without ``item_of``,
    ``selections == attempts``, so this matches "fewest attempts". Offers marked
    non-executed by gate SKIP or similar still move back, so a continually
    skipped item does not starve the rest.
    """
    return min(
        ctx.selectable, key=lambda it: (ctx.selections[it.id], ctx.position[it.id])
    )


def _strat_fifo(ctx: ScheduleContext) -> WorkItem:
    """First unfinished item in original order.

    This is a naive strategy; combine it with per-item limits to reduce
    starvation. Because it has no fairness counter, it does not rotate after an
    offer that ``item_of`` marked non-executed (it keeps offering the skipped
    first item). Use ``fewest_attempts`` / ``round_robin`` when composing with a
    gate that treats skips as non-executed.
    """
    return min(ctx.selectable, key=lambda it: ctx.position[it.id])


def _strat_priority(ctx: ScheduleContext) -> WorkItem:
    """Priority descending, then selection count ascending, then original order.

    This respects priority while staying fair within the same priority. Fairness
    within equal priorities is measured by ``selections`` (offer count), as in
    ``_strat_fewest_attempts``.
    """
    return min(
        ctx.selectable,
        key=lambda it: (-it.priority, ctx.selections[it.id], ctx.position[it.id]),
    )


def _strat_round_robin(ctx: ScheduleContext) -> WorkItem:
    """Cycle to the selectable item *after* last_selected by position.

    This is classic round-robin. Unlike ``fewest_attempts``, which measures
    fairness by selection count, this strictly cycles by original order (it
    does not dispatch the same item consecutively). Even if last_selected has
    become done / exhausted and is no longer selectable, its *position* remains
    the cycling reference point (positions are immutable, so this is
    deterministic).
    """
    if ctx.last_selected is None:
        return min(ctx.selectable, key=lambda it: ctx.position[it.id])
    last_pos = ctx.position[ctx.last_selected]
    after = [it for it in ctx.selectable if ctx.position[it.id] > last_pos]
    pool = after if after else ctx.selectable
    return min(pool, key=lambda it: ctx.position[it.id])


_BUILTIN_STRATEGIES: dict[str, Scheduler] = {
    "round_robin": _strat_round_robin,
    "fewest_attempts": _strat_fewest_attempts,
    "fifo": _strat_fifo,
    "priority": _strat_priority,
}


class Drained:
    """Sentinel type indicating that :meth:`WorkListGather.__call__` has no item to run.

    Gather returns this when every item is done / exhausted. Refer to it via
    :data:`DRAINED` (the only instance). Loop stopping is handled by the
    :class:`WorkListDrained` stop condition, so when composed correctly this
    value is never passed to ``act`` (stop conditions are evaluated before
    gather).
    """

    _singleton: "Optional[Drained]" = None

    def __new__(cls) -> "Drained":
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __repr__(self) -> str:
        return "<work-list-drained>"

    def __bool__(self) -> bool:
        return False


DRAINED = Drained()


@dataclass(frozen=True)
class WorkListProgress:
    """Work-list progress snapshot returned by :meth:`WorkListGather.report`.

    ``done`` / ``exhausted`` / ``remaining`` are tuples of item ids in original
    order. ``attempts`` maps id to prior attempt count. ``drained`` means there
    are no items left to run (= ``remaining`` is empty).
    """

    total: int
    done: tuple[str, ...]
    exhausted: tuple[str, ...]
    remaining: tuple[str, ...]
    attempts: Mapping[str, int]

    @property
    def drained(self) -> bool:
        """Whether there are no items left to run (all done or exhausted)."""
        return not self.remaining


@dataclass(frozen=True)
class _Derivation:
    """Result of replaying ``state.history`` (internal)."""

    attempts: dict[str, int]
    selections: dict[str, int]
    done: set[str]
    exhausted: set[str]
    last_selected: Optional[str]
    selectable: tuple[WorkItem, ...]


def _default_done(item: WorkItem, record: StepRecord) -> bool:
    """Default done predicate: whether verify reported goal completion for the iteration.

    This is a straightforward default for single-goal loops. True multi-item
    loops often need another signal to decide whether "*this item* is done", so
    override it with ``done_when`` (store per-item completion signals in
    record.observation / record.detail).
    """
    return bool(record.goal_met)


def _default_build_ctx(item: WorkItem, attempt: int, state: LoopState) -> dict[str, Any]:
    """Default context: JSON-native dict ``{"id", "attempt", "priority", "payload"}``.

    ``act`` reads ``ctx["id"]`` / ``ctx["payload"]`` / ``ctx["attempt"]``.
    **JSON-native is intentional**: when composed with a persistent human gate
    (:class:`~loop_agent.gate.HumanGate` /
    :func:`~loop_agent.gate.run_gated_loop`), if the gate pauses, context is
    saved to state.db as the proposed action (``request_decision`` requires
    JSON-native values). Returning :class:`WorkItem` itself would not round-trip
    and would raise ``ConfigError``, so the default is a dict (safe as long as
    ``payload`` is JSON-native). Override with ``build_ctx`` if the raw
    ``WorkItem`` or another shape is desired.
    """
    return {
        "id": item.id,
        "attempt": attempt,
        "priority": item.priority,
        "payload": item.payload,
    }


class WorkListGather:
    """``gather`` hook that fairly runs multiple items through one loop (Issue #56).

    Pass it as ``gather`` as in ``run_loop(gather=WorkListGather(items, ...),
    ...)`` (``__call__(state) -> ctx`` conforms to ``GatherHook``). On each
    iteration:

    1. Deterministically replay ``state.history`` to derive attempts / done /
       exhausted for each item.
    2. Select one next item with the scheduling strategy (selectable = not done
       and not exhausted).
    3. Return ``build_ctx(item, attempt, state)`` as the context for ``act``.
    4. Return :data:`DRAINED` if selectable is empty (stopping is handled by
       :class:`WorkListDrained`).

    Args:
        items: :class:`WorkItem` values to run (ids must be unique). Empty means
            always drained. Plain strings are also accepted and promoted to
            ``WorkItem`` as the ``id``.
        strategy: ``"round_robin"`` / ``"fewest_attempts"`` (default) /
            ``"fifo"`` / ``"priority"``, or a custom
            ``ScheduleContext -> WorkItem|id`` callable.
        max_attempts_per_item: Per-item limit. ``None`` (default) means
            unlimited (bounded only by the global ``MaxIterations``). Must be
            ``>= 1``. Items that do not become done after the configured number
            of attempts are marked *exhausted* and removed from selectable. This
            is the core protection against one item monopolizing
            ``MaxIterations`` and starving the rest; #37 avoided that starvation
            with a handwritten round-robin gather.
        done_when: User policy ``(item, record) -> bool`` that decides whether
            "*this item* is done" (independent of verify). Defaults to
            ``record.goal_met``. Once an item becomes done, it is never
            dispatched again (sticky).
        build_ctx: ``(item, attempt, state) -> ctx`` converter from the selected
            item to the context for ``act``. ``attempt`` is the prior attempt
            count before this dispatch (0-based), useful for composition such
            as raising the model by attempt count with ModelLadder. The default
            returns the JSON-native dict ``{"id", "attempt", "priority",
            "payload"}`` so it can be saved to state.db when composed with a
            persistent human gate.
        item_of: ``(record) -> item_id | None`` hook that returns which item
            each history record was *actually* produced by acting on. The
            default ``None`` uses schedule replay and treats "offered item ==
            acted item" (correct for standard 1:1 loops without a gate). Pass
            this when composing with a ``gate`` where offers and records can
            diverge: ``GATE_SKIP`` (reject/respond) adds a record without
            calling ``act``, so return ``None`` to mark it non-executed; ``edit``
            can replace the context with another item, so return the actual item
            from the record (for example an item id embedded in ``observation``).
            ``None`` or an id outside the work list is not counted as execution
            and does not update attempts / done / per-item limits, which avoids
            incorrectly exhausting an item that did not run or attributing
            another item's record to it (#56 review). **Fairness (offer count)
            is measured by schedule**, so offers advance even when ``item_of``
            returns ``None`` for a skip, rotating to other items and preventing
            starvation.

    Raises:
        ConfigError: duplicate item ids, unknown ``strategy`` string, or
            ``max_attempts_per_item < 1``.
    """

    def __init__(
        self,
        items: Sequence[Union[WorkItem, str]],
        *,
        strategy: Union[str, Scheduler] = "fewest_attempts",
        max_attempts_per_item: Optional[int] = None,
        done_when: DonePredicate = _default_done,
        build_ctx: ContextBuilder = _default_build_ctx,
        item_of: Optional[ItemAttributor] = None,
    ) -> None:
        normalized: list[WorkItem] = [
            it if isinstance(it, WorkItem) else WorkItem(id=it) for it in items
        ]
        by_id: dict[str, WorkItem] = {}
        for it in normalized:
            if it.id in by_id:
                raise ConfigError(f"duplicate WorkItem id {it.id!r}; ids must be unique")
            by_id[it.id] = it
        self._items: tuple[WorkItem, ...] = tuple(normalized)
        self._by_id = by_id
        self._position = {it.id: i for i, it in enumerate(self._items)}

        if isinstance(strategy, str):
            if strategy not in _BUILTIN_STRATEGIES:
                raise ConfigError(
                    f"unknown strategy {strategy!r}; "
                    f"expected one of {sorted(_BUILTIN_STRATEGIES)} or a callable"
                )
            self._strategy: Scheduler = _BUILTIN_STRATEGIES[strategy]
            self.strategy_name = strategy
        else:
            self._strategy = strategy
            self.strategy_name = getattr(strategy, "__name__", "custom")

        if max_attempts_per_item is not None and max_attempts_per_item < 1:
            raise ConfigError("max_attempts_per_item must be >= 1 or None")
        self._max = max_attempts_per_item
        self._done_when = done_when
        self._build_ctx = build_ctx
        self._item_of = item_of

    @property
    def items(self) -> tuple[WorkItem, ...]:
        """Registered work items in original order."""
        return self._items

    # -- scheduling internals -----------------------------------------------

    def _selectable(
        self, done: set[str], exhausted: set[str]
    ) -> tuple[WorkItem, ...]:
        """Items that are neither done nor exhausted, in original order."""
        return tuple(
            it for it in self._items if it.id not in done and it.id not in exhausted
        )

    def _select_id(
        self,
        attempts: dict[str, int],
        selections: dict[str, int],
        done: set[str],
        exhausted: set[str],
        last_selected: Optional[str],
    ) -> Optional[str]:
        """Let the strategy select one item and return its id (or ``None`` if empty)."""
        selectable = self._selectable(done, exhausted)
        if not selectable:
            return None
        ctx = ScheduleContext(
            selectable=selectable,
            attempts=attempts,
            selections=selections,
            position=self._position,
            last_selected=last_selected,
            done=frozenset(done),
            exhausted=frozenset(exhausted),
        )
        chosen = self._strategy(ctx)
        chosen_id = chosen.id if isinstance(chosen, WorkItem) else chosen
        if chosen_id not in {it.id for it in selectable}:
            raise ConfigError(
                f"strategy {self.strategy_name!r} selected {chosen_id!r}, "
                f"which is not selectable (done/exhausted/unknown); "
                f"selectable={[it.id for it in selectable]}"
            )
        return chosen_id

    def _derive(self, state: LoopState) -> _Derivation:
        """Deterministically replay ``state.history`` to derive progress.

        Each history record is treated as the result of acting on the item this
        gatherer dispatched in the immediately preceding iteration (because the
        strategy is deterministic, the selection at step k can be reconstructed).
        This makes resume safe without in-process counters. ``done`` /
        ``exhausted`` are sticky (once set, the item is not reselected).

        **Prerequisite for resume correctness**: attribution is derived by
        replaying ``state.history`` with the *current* ``items`` / ``strategy`` /
        ``max_attempts_per_item`` / ``done_when``. ``StepRecord`` does not
        structurally record which item was dispatched (it is rederived from the
        strategy), so this is correct **only when the current settings match the
        settings that produced the history**. Feeding history from another
        gatherer configuration silently misattributes the step k record to a
        different item (it does not crash). Therefore resume is limited to
        restarting the *same* interrupted gatherer via ``initial_state``. If
        :meth:`from_triage` creates a new gatherer because the ready set changed,
        the item order/composition changes, so **do not carry over prior
        ``state.history``; start a new loop with a fresh ``LoopState``** (triage
        excludes already-done items, and new ready items should start at attempt
        0).

        **Separating offers from attribution**: ``selections`` (offer count) is
        determined by schedule and drives fairness. attempts / done / exhausted
        attach to the item the record is *attributed to*. By default the two are
        identified (offer == act in a 1:1 loop). When a gate sits between them
        and they can diverge:

        - ``GATE_SKIP`` (reject/respond): adds a record without calling ``act``.
          If ``item_of`` returns ``None``, it is treated as non-executed and does
          not count toward attempts (the offer still advances, so rotation stays
          fair).
        - ``edit``: replaces the context and acts on another item. Because the
          record belongs to that item, reading the actual item from the record
          with ``item_of`` attributes it correctly (the originally offered item
          remains at zero executions). Without ``item_of``, the record is
          incorrectly attributed to the offered item.

        Because fairness is measured by ``selections``, skipped items also move
        back and ``fewest_attempts`` / ``priority`` / ``round_robin`` do not
        present the same item forever (only ``fifo`` stays naive and does not
        rotate). In standard ``run_loop`` usage (no gate, or a gate that neither
        skips nor edits), offers and records are 1:1 and ``selections ==
        attempts``, so ``item_of`` is unnecessary.
        """
        attempts: dict[str, int] = {it.id: 0 for it in self._items}
        selections: dict[str, int] = {it.id: 0 for it in self._items}
        done: set[str] = set()
        exhausted: set[str] = set()
        last_selected: Optional[str] = None

        for record in state.history:
            # sel = item that gather *offered* (from schedule). If history
            # continues after selectable is exhausted (steps not produced by
            # this gatherer / surplus after everything drained), leave it
            # unattributed.
            sel = self._select_id(attempts, selections, done, exhausted, last_selected)
            if sel is None:
                break
            # Always advance the offer for fairness (selections) and the
            # round_robin rotation reference. Even non-executed skips advance,
            # so a continually skipped item does not starve the rest.
            selections[sel] += 1
            last_selected = sel
            # Attribution = item this record was *actually acted* on. The
            # default (item_of=None) treats "offered item == acted item"
            # (standard 1:1 loop). When a gate can SKIP (no item) / EDIT
            # (replace with another item), offers and records diverge, so read
            # the actual item from the record via item_of (None=non-executed).
            # This attaches attempts/done/exhausted to the correct item and
            # avoids exhausting an item that did not run (#56 review).
            actual = sel if self._item_of is None else self._item_of(record)
            if actual is None or actual not in self._by_id:
                # Non-executed (skip), or edit to an id outside this work list:
                # do not count it as execution.
                continue
            attempts[actual] += 1
            if self._done_when(self._by_id[actual], record):
                done.add(actual)
            elif self._max is not None and attempts[actual] >= self._max:
                exhausted.add(actual)

        selectable = self._selectable(done, exhausted)
        return _Derivation(
            attempts, selections, done, exhausted, last_selected, selectable
        )

    # -- gather hook body ----------------------------------------------------

    def __call__(self, state: LoopState) -> Any:
        """``GatherHook`` body: return the next item context, or :data:`DRAINED`."""
        d = self._derive(state)
        if not d.selectable:
            return DRAINED
        sel = self._select_id(
            d.attempts, d.selections, d.done, d.exhausted, d.last_selected
        )
        assert sel is not None  # selectable is non-empty, so selection must succeed
        item = self._by_id[sel]
        return self._build_ctx(item, d.attempts[sel], state)

    # -- attempt counter / canonical progress API ----------------------------

    def attempts(self, state: LoopState) -> dict[str, int]:
        """Return prior attempt counts for each item (id -> count), derived from state."""
        return dict(self._derive(state).attempts)

    def done_items(self, state: LoopState) -> set[str]:
        """Set of item ids considered complete by ``done_when``."""
        return set(self._derive(state).done)

    def exhausted_items(self, state: LoopState) -> set[str]:
        """Set of incomplete item ids removed after reaching the per-item limit."""
        return set(self._derive(state).exhausted)

    def remaining(self, state: LoopState) -> tuple[WorkItem, ...]:
        """Items still runnable (not done and not exhausted), in original order."""
        return self._derive(state).selectable

    def drained(self, state: LoopState) -> bool:
        """Whether there are no items left to run (all done or exhausted)."""
        return not self._derive(state).selectable

    def report(self, state: LoopState) -> WorkListProgress:
        """Return a progress snapshot (:class:`WorkListProgress`) from one derivation."""
        d = self._derive(state)
        return WorkListProgress(
            total=len(self._items),
            done=tuple(it.id for it in self._items if it.id in d.done),
            exhausted=tuple(it.id for it in self._items if it.id in d.exhausted),
            remaining=tuple(it.id for it in d.selectable),
            attempts=dict(d.attempts),
        )

    # -- triage integration --------------------------------------------------

    @classmethod
    def from_triage(
        cls,
        candidates,
        *,
        done: Sequence[str] = (),
        strategy: Union[str, Scheduler] = "fewest_attempts",
        **kwargs: Any,
    ) -> "WorkListGather":
        """Build by delegating priority and ordering to :func:`loop_agent.discovery.triage`.

        Run ``triage`` over ``candidates`` (a collection of
        :class:`~loop_agent.discovery.Candidate` values), then map only *ready*
        candidates (dependencies satisfied) to :class:`WorkItem` in triage
        ranking order (priority descending -> effort ascending -> id), carrying
        over ``priority`` / ``payload``. *blocked* candidates (dependencies not
        satisfied) are excluded because they cannot run yet.

        This keeps responsibilities separate: triage decides **what is worth
        running, and in what order** (dependency resolution + priority), while
        :class:`WorkListGather` decides **how to run those items fairly**
        (scheduling + per-item limits). To include newly ready candidates after
        dependencies are resolved, call ``from_triage`` again with the current
        ``done`` set and create a new gatherer. Because the item composition
        changes, do not carry over prior ``state.history``; **start the loop
        with a new ``LoopState``** (see the :meth:`_derive` prerequisites:
        history from another configuration will be misattributed).

        Args:
            candidates: Candidates to triage.
            done: Ids already complete at triage time (used for dependency
                satisfaction).
            strategy: Scheduling strategy (default ``"fewest_attempts"``).
                Because triage determines order, even ``"fifo"`` runs in triage
                ranking order.
            **kwargs: Other :class:`WorkListGather` arguments
                (``max_attempts_per_item`` / ``done_when`` / ``build_ctx``).
        """
        result = triage(candidates, done=done)
        items = tuple(
            WorkItem(id=c.id, priority=c.priority, payload=c.payload)
            for c in result.ready
        )
        return cls(items, strategy=strategy, **kwargs)


class WorkListDrained:
    """Stop condition that stops once :class:`WorkListGather` is drained.

    Drained means every item is done/exhausted. This conforms to the
    ``StopCondition`` protocol (``check(state) -> reason|None`` + ``name``) and
    can be composed directly with ``AnyOf`` / ``run_loop(conditions=...)``. Stop
    conditions are evaluated at the *start* of each iteration (before gather),
    so once the gatherer is drained the loop stops before gather is called. This
    is the key design point that prevents :data:`DRAINED` from leaking into
    ``act``.

    This is a *neutral* stop (neither success nor abort): it means "all items to
    run have been completed or tried up to their limits". Read individual item
    outcomes from :meth:`WorkListGather.report`.

    Args:
        gatherer: :class:`WorkListGather` to monitor (the instance sharing the
            same ``items`` configuration).
    """

    name: ClassVar[str] = "work_list_drained"

    def __init__(self, gatherer: WorkListGather) -> None:
        self.gatherer = gatherer

    def check(self, state: LoopState) -> Optional[str]:
        report = self.gatherer.report(state)
        if report.drained:
            return (
                f"work list drained: {len(report.done)} done, "
                f"{len(report.exhausted)} exhausted "
                f"(of {report.total})"
            )
        return None


__all__ = [
    "WorkItem",
    "ScheduleContext",
    "Scheduler",
    "WorkListGather",
    "WorkListProgress",
    "WorkListDrained",
    "Drained",
    "DRAINED",
]
