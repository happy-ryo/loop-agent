"""The PoC loop driver: gather -> act -> verify -> repeat (report.md S4.4).

A single-agent, single-process driver. ``act`` and ``verify`` are injected
callables (hooks), so the engine carries no LLM dependency -- the PoC drives it
with in-memory stubs and the same seam later wraps a real model call.

Termination is graceful and reason-bearing:

- the loop ends *naturally* when ``verify`` reports the goal is met, or
- it is *stopped* when one of the composed mechanical caps fires first.

Either way the driver returns a :class:`LoopResult` describing the outcome; it
never raises to signal "limit reached".

Two entry points share one control-flow implementation (Issue #40):

- :func:`async_run_loop` -- the async driver and single source of truth. Each
  seam (``gather`` / ``act`` / ``verify`` / ``conditions`` / ``gate`` /
  ``on_step``) may be a synchronous callable *or* an async one; the driver awaits
  results via :func:`loop_agent._async.maybe_await`, so sync and async hooks mix.
- :func:`run_loop` -- the original synchronous API. It drives the shared
  coroutine to completion *in the caller's own context* (no event loop is
  created), so behaviour for synchronous hooks is byte-for-byte unchanged; it
  raises a clear ``RuntimeError`` if handed an async hook (use
  :func:`async_run_loop` for those).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, Union, runtime_checkable

from ._async import maybe_await, reject_awaitables
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

    ``tokens`` is the cost charged to :class:`~loop_agent.conditions.TokenBudget`
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
    (:class:`loop_agent.gate.HumanGate`).
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
    # GATE_PROCEED のとき、step を記録 (on_step) した *後* に driver が呼ぶ任意の完了通知。
    # gate が in-progress リース (report.md S5 Phase3 / Issue #21) を張った場合、ここで
    # executing -> executed を確定する (step 永続化後に呼ぶので「executed なら step 行は
    # 必ず存在」が保たれ、勝者クラッシュ時の step 欠落を防ぐ)。driver は中身を解さず呼ぶだけ。
    # 他シーム同様 sync/async どちらでも可 (driver が maybe_await で await する; Issue #40)。
    on_complete: Optional[Callable[[], Union[None, Awaitable[None]]]] = None


@runtime_checkable
class ActionGate(Protocol):
    """A pre-act interception point for limited human gating (report.md R6).

    Evaluated *before* ``act`` executes the gathered context, so an irreversible
    action can be intercepted before its side effect. Returns a
    :class:`GateReview` telling the driver to proceed, skip, or pause.

    ``review`` may also be **async** (return an awaitable resolving to a
    :class:`GateReview`) -- e.g. a gate that awaits a remote approval service.
    :func:`async_run_loop` awaits the result either way; :func:`run_loop` drives
    an async gate through its internal event loop too.
    """

    def review(
        self, context: Any, state: LoopState
    ) -> Union[GateReview, Awaitable[GateReview]]:
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
        "goal_met"``). A goal reached instead by a :class:`~loop_agent.conditions.GoalMet`
        *stop condition* terminates with ``status == "stopped"`` and leaves this
        ``False`` -- use :attr:`succeeded` to detect success across both channels.
        """
        return self.status == "goal_met"

    @property
    def succeeded(self) -> bool:
        """True when the goal was reached by *either* success channel.

        The goal can be verified two ways (report.md S4.5): the ``verify`` hook
        ending the loop naturally (:attr:`goal_met`), or a
        :class:`~loop_agent.conditions.GoalMet` stop condition firing at the
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


# 各シームは sync callable のまま受けつつ、async (acallable) も受けられる (Issue #40)。
# 戻り値が awaitable なら driver が await する (loop_agent._async.maybe_await)。
GatherHook = Callable[[LoopState], Union[Any, Awaitable[Any]]]
ActHook = Callable[[Any], Union[ActOutcome, Awaitable[ActOutcome]]]
VerifyHook = Callable[[ActOutcome], Union[VerifyOutcome, Awaitable[VerifyOutcome]]]
StepHook = Callable[[StepRecord, LoopState], Union[None, Awaitable[None]]]
Conditions = Union[AnyOf, list[StopCondition], tuple[StopCondition, ...]]


def _default_gather(state: LoopState) -> LoopState:
    """Pass the state through as context when no gather hook is supplied."""
    return state


async def async_run_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    gate: Optional[ActionGate] = None,
    time_fn: Callable[[], float] = time.monotonic,
    initial_state: Optional[LoopState] = None,
    # _strict_sync は run_loop 専用の内部フラグ (公開 API では使わない)。True の間
    # maybe_await / afirst_triggered は awaitable を AsyncSeamInSyncLoop で拒否する。
    _strict_sync: bool = False,
) -> LoopResult:
    """Async driver: gather -> act -> verify -> repeat until the goal or a cap.

    This is the **single source of truth** for the loop's control flow; the
    synchronous :func:`run_loop` is a thin ``asyncio.run`` wrapper around it. Use
    this entry point when you are already inside an event loop (``await
    async_run_loop(...)``) or when any hook is a coroutine function.

    **sync/async シーム (Issue #40).** Every injected seam -- ``gather``, ``act``,
    ``verify``, each ``conditions`` ``check``, ``gate.review``, ``on_step`` (and a
    gate's ``on_complete``) -- may be a plain synchronous callable *or* an async
    one (returning an awaitable). The driver awaits each result via
    :func:`loop_agent._async.maybe_await`, so synchronous and asynchronous hooks
    mix freely (e.g. an async ``gather`` + sync ``act`` + async ``verify``). A
    synchronous hook adds no awaiting overhead -- its return value is not
    awaitable, so it is used as-is.

    **asyncio の使い方.** This coroutine performs no concurrency of its own: it
    ``await``\\s each seam *sequentially* to preserve the exact gather -> gate ->
    act -> verify ordering and the stop-condition evaluation timing of the
    synchronous loop. It runs on whatever event loop awaits it. To run several
    independent loops concurrently, schedule them as tasks from the caller, e.g.
    ``await asyncio.gather(async_run_loop(...), async_run_loop(...))`` or
    ``asyncio.create_task(async_run_loop(...))`` -- each call owns its own
    :class:`LoopState`, so concurrent runs do not interfere. ``time_fn`` stays a
    *synchronous* monotonic clock (it is read, never awaited); a blocking
    synchronous ``act`` will block the event loop, so wrap genuinely blocking
    work (or use an async hook with ``loop.run_in_executor``) when sharing a loop
    with other tasks.

    Args:
        act: Hook producing an :class:`ActOutcome` from the gathered context.
            May be sync or async.
        verify: Hook turning an :class:`ActOutcome` into a :class:`VerifyOutcome`;
            ``goal_met=True`` terminates the loop naturally. May be sync or async.
        conditions: An :class:`~loop_agent.conditions.AnyOf`, or any non-empty
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
        initial_state: Seed the loop with already-accumulated state to **resume**
            an interrupted run (report.md S4.4 / S5 Phase 2, Issue #14). Pass the
            :class:`LoopState` reconstructed by
            :meth:`~loop_agent.store.LoopStore.load_or_init` (or
            :attr:`~loop_agent.store.DBProgressLog.state`): the loop continues
            from its ``iteration`` / ``tokens_used`` / ``goal_met`` / ``history``
            instead of starting empty, and ``elapsed`` keeps accumulating from
            the persisted value (the wall-clock origin is back-dated by it so
            stop conditions like :class:`~loop_agent.conditions.Timeout` see the
            *total* run time, not just this leg). ``None`` (the default) starts a
            fresh run; an empty :class:`LoopState` is equivalent to ``None``. The
            seed is copied, so the caller's object is not mutated.

    Returns:
        A :class:`LoopResult`. ``status`` is ``"goal_met"`` (``stop is None``),
        ``"stopped"`` (``stop`` names the fired condition), or ``"paused"``
        (``stop is None``, ``pending`` describes the gated action awaiting a
        human decision).

    Stop conditions are evaluated at the top of each cycle (the while-guard),
    *before* a new step starts -- including before the very first one, so e.g.
    ``MaxIterations(0)`` returns immediately with zero iterations. On resume this
    means a run already at or past a cap (e.g. resumed ``elapsed`` >= a
    ``Timeout``) stops immediately with no further step, exactly as a straight
    run would have. Resume is only meaningful for hooks that derive their verdict
    from the (gathered) state rather than from in-process call counters, since a
    fresh process rebuilds the hooks but not their private counters; pair resume
    with state-based stop conditions (e.g. :class:`~loop_agent.conditions.GoalMet`)
    for a run that reproduces a straight-through result exactly.

    One fidelity caveat when the seed was reconstructed from the state.db SoT
    (:meth:`~loop_agent.store.LoopStore.load_or_init`): ``history`` observations
    survive a JSON round-trip, so non-JSON-native types drift (``tuple`` ->
    ``list``, ``dict`` int-keys -> ``str``, sets/custom objects/NaN -> ``repr``
    string). A condition that *keys* directly on the raw ``observation`` --
    notably :class:`~loop_agent.conditions.NoProgress`'s default key -- can then
    diverge across the seam (a ``tuple`` becomes an unhashable ``list``; other
    types re-key), so its window straddling the resume point may fire at a
    different iteration or raise. Use JSON-stable observations, or give such a
    condition a ``key`` projecting onto a JSON-stable signature (e.g.
    ``NoProgress(key=lambda r: json.dumps(r.observation, sort_keys=True, default=repr))``),
    when the run must resume identically.
    """
    # Scope the strict-sync flag to THIS run by setting it explicitly at entry
    # (overriding any value inherited via copy_context from an outer run_loop):
    # run_loop passes _strict_sync=True and drives this coroutine manually, while a
    # nested asyncio.run(async_run_loop(...)) started from a synchronous hook gets the
    # default False here -- so strict rejection neither leaks into the nested run nor
    # depends on ambient event-loop presence (it works even when run_loop is called
    # from inside a running loop).
    token = reject_awaitables.set(_strict_sync)
    try:
        return await _drive_loop(
            act=act,
            verify=verify,
            conditions=conditions,
            gather=gather,
            on_step=on_step,
            gate=gate,
            time_fn=time_fn,
            initial_state=initial_state,
        )
    finally:
        reject_awaitables.reset(token)


async def _drive_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    gate: Optional[ActionGate] = None,
    time_fn: Callable[[], float] = time.monotonic,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """The loop body for :func:`async_run_loop` (sync/async-seam driver).

    Separated so :func:`async_run_loop` can scope the strict-sync contextvar
    around it; see that function for the full contract.
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
    # A run that already reached the goal via the `verify` hook persists
    # goal_met=True with its final step; if the process then died *before*
    # record_result, resume reconstructs that flag. Honor it: the run already
    # terminated naturally, so reproduce that result (status goal_met, stop None)
    # with zero new steps instead of running more work. A goal reached via a
    # GoalMet *stop condition* leaves this flag False (status "stopped") and is
    # handled below -- the condition re-fires on the first guard, also at zero
    # new steps -- so this early return is specific to the verify-hook channel.
    if state.goal_met:
        return LoopResult(status="goal_met", stop=None, state=state)

    # Back-date the clock origin by the already-elapsed time so `elapsed`
    # continues accumulating from the persisted value across the resume seam
    # (for a fresh run state.elapsed is 0.0, so start == time_fn()).
    #
    # The human gate (if any) keys each decision by `state.iteration` (see
    # HumanGate), so no per-run gate setup is needed here: that key is stable
    # across both resume models -- a replay (fresh state) re-reaches the same
    # iteration, and an `initial_state` resume restores it -- so a persisted
    # decision realigns to its action either way.
    start = time_fn() - state.elapsed

    while True:
        state.elapsed = time_fn() - start
        triggered = await stop.afirst_triggered(state)
        if triggered is not None:
            return LoopResult(status="stopped", stop=triggered, state=state)

        context = await maybe_await(gather(state))

        # gated PROCEED が張ったリースの完了通知 (executing->executed)。act 実行後・
        # step 永続化後に呼ぶため、ここで掴んでおく (非ゲート step では None のまま)。
        gate_on_complete: Optional[Callable[[], Union[None, Awaitable[None]]]] = None
        if gate is not None:
            review = await maybe_await(gate.review(context, state))
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
                    await maybe_await(on_step(record, state))
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
            gate_on_complete = review.on_complete

        outcome = await maybe_await(act(context))
        state.tokens_used += outcome.tokens

        verdict = await maybe_await(verify(outcome))
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
            await maybe_await(on_step(record, state))

        # step を永続化した *後* にリース完了を確定する (executing->executed)。順序が肝:
        # 「executed なら step 行は必ず存在」を満たし、勝者クラッシュ時の step 欠落を防ぐ。
        # ここで例外が出たら (DB エラー等) あえて握り潰さず伝播させる: status は executing の
        # まま残り、別プロセスがリース失効で取り直して完遂する (= クラッシュと同じ復旧経路)。
        # 握り潰して継続すると未確定のまま後続へ進み順序整合を壊すため、fail-loud が安全。
        if gate_on_complete is not None:
            await maybe_await(gate_on_complete())

        if verdict.goal_met:
            return LoopResult(status="goal_met", stop=None, state=state)


def run_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    gate: Optional[ActionGate] = None,
    time_fn: Callable[[], float] = time.monotonic,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """Drive gather -> act -> verify -> repeat until the goal or a cap (sync).

    This is the **synchronous entry point** and the original public API. It shares
    one control-flow implementation with :func:`async_run_loop` (which owns the
    loop body) and behaves identically for synchronous hooks: same arguments,
    same :class:`LoopResult`, same stop-condition timing, same resume semantics.
    See :func:`async_run_loop` for the full description of the arguments, the dual
    termination contract, the human gate, and ``resume`` via ``initial_state``.

    **Exact-parity drive (no event loop).** With fully synchronous hooks the
    shared coroutine never actually awaits anything (``maybe_await`` returns
    non-awaitable values without suspending), so ``run_loop`` runs it to
    completion in the **caller's own context** by stepping it once
    (``coro.send(None)`` -> ``StopIteration`` carries the result). It does *not*
    create an event loop, wrap the work in a :class:`asyncio.Task`, or touch the
    event-loop policy -- so caller-side context (e.g. :mod:`contextvars` set by a
    hook) propagates exactly as it did before async support existed, hook
    exceptions propagate with their own type unchanged, and there is zero asyncio
    overhead per call. ``run_loop`` may therefore be called from anywhere,
    including from within a running event loop (it blocks like any synchronous
    call, exactly as the original sync loop did).

    **Async seams belong on** :func:`async_run_loop`. ``run_loop`` calls it with
    ``_strict_sync=True``, which scopes the strict-sync flag
    (:data:`loop_agent._async.reject_awaitables`) to this run: if *any* seam
    returns an awaitable -- a hook, a ``conditions`` check, ``gate.review``,
    ``on_step``, or ``on_complete`` -- :func:`maybe_await` (and
    :meth:`AnyOf.afirst_triggered`) raise
    :class:`loop_agent._async.AsyncSeamInSyncLoop` (a ``RuntimeError``) at that
    point, reliably and regardless of whether the awaitable would have suspended.
    (The original sync ``run_loop`` never accepted async seams either, so this is
    not a regression -- it is a clear, consistent error directing you to
    ``await async_run_loop(...)``.) Because the flag is set explicitly per run, it
    works even when ``run_loop`` is invoked from inside a running event loop, and a
    nested ``asyncio.run(async_run_loop(...))`` started from a synchronous hook
    runs non-strict (it enters with the default ``_strict_sync=False``).
    """
    # Drive the shared coroutine synchronously in the caller's context. async_run_loop
    # (with _strict_sync=True) makes maybe_await / AnyOf.afirst_triggered reject any
    # awaitable seam instead of awaiting it, so an all-sync loop runs straight to
    # completion without ever suspending -- the first step raises StopIteration
    # carrying the LoopResult. No event loop is created and no Task copies the
    # context, so caller-side contextvars and exception types are preserved exactly.
    # Seam exceptions raised during the step propagate out of send() directly (not
    # via StopIteration), keeping their original type.
    coro = async_run_loop(
        act=act,
        verify=verify,
        conditions=conditions,
        gather=gather,
        on_step=on_step,
        gate=gate,
        time_fn=time_fn,
        initial_state=initial_state,
        _strict_sync=True,
    )
    try:
        coro.send(None)
    except StopIteration as completed:
        # Normal completion: the coroutine's `return` surfaces as StopIteration
        # carrying the LoopResult (this is the coroutine protocol, not a seam
        # raising StopIteration -- that case is handled just below).
        return completed.value
    except RuntimeError as exc:
        # PEP 479: a StopIteration *raised by a synchronous seam* inside the
        # coroutine (e.g. an iterator-backed act doing next() on exhaustion) is
        # rewritten to "RuntimeError: coroutine raised StopIteration" as it crosses
        # the coroutine boundary, with the original exception as __cause__. The
        # original purely-synchronous run_loop propagated the seam's own type, so
        # unwrap it to preserve exception-type parity (other RuntimeErrors pass
        # through unchanged).
        if isinstance(exc.__cause__, StopIteration):
            raise exc.__cause__
        raise
    else:
        # Unreachable: with strict-sync set, maybe_await never awaits, so the
        # coroutine cannot suspend. Guard defensively rather than hang.
        coro.close()
        raise RuntimeError("run_loop(): synchronous driver unexpectedly suspended")
