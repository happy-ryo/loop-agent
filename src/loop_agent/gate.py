"""Limited human gate: interrupt only irreversible operations for human-in-the-loop (Issue #15).

report.md S4.5 / R6 / Principle 8 defines "human gates are **limited** to irreversible,
large-scope actions" (minimal implementation). Has the same 4 types of human decisions as
LangGraph's ``interrupt()`` (**approve / edit / reject / respond**), and persists decisions
to state.db (:mod:`loop_agent.store`) to **maintain decisions across pause -> resume**.

Design boundaries:

- **Loop core is gate-independent**. :func:`loop_agent.loop.run_loop` only knows the
  :class:`~loop_agent.loop.ActionGate` protocol (``review(context, state)``), and interprets
  only 3 dispositions: proceed / skip / pause. store and human lifecycle are encapsulated
  behind this module's :class:`HumanGate`.
- **Irreversibility judgment injected from outside the loop**. The predicate ``on(action) -> bool``
  decides "is this proposal irreversible?". reversible actions and ``gate=None`` never interrupt
  (= no total-step gating. "irreversible-only" guarantee is structural per report.md).
- **Reuse claude-org's pending_decisions with role remapping**. "secretary registers worker's
  judgment request, resolved by user response" maps to "loop registers irreversible action,
  resolved by human" (:meth:`loop_agent.store.LoopStore.request_decision` /
  :meth:`~loop_agent.store.LoopStore.resolve_decision`).
- **Irreversible actions maintain exactly-once + order-consistency across resume (Issue #21)**.
  Irreversible actions executed on approve/edit acquire an in-progress lease via
  :meth:`~loop_agent.store.LoopStore.acquire_lease` to a single winner (``resolved -> executing``),
  and after ``act`` completes, :meth:`~loop_agent.store.LoopStore.complete_execution` confirms
  ``executed``. While holding a lease (``executing``), loser processes seeing the same gate
  **pause until ``executed``** to wait, preventing iteration order skew from running subsequent
  iterations before the winner's irreversible action completes. If the winner crashes and the
  lease expires, another process takes the lease again and completes execution, so steps are
  not skipped. On replay resume (below), revisiting already-executed gates checks ``executed``
  and skips, **preventing double execution** (prevention of duplicate deploys, etc. = the
  whole point of gates).

**Resume's 2 models and gate consistency**: gate key is determined by ``state.iteration`` at
review time (:class:`HumanGate` reference). This is stable under both resume models:

1. **``initial_state`` resume (#14, recommended)**: Pass the :class:`~loop_agent.state.LoopState`
   at pause time (:meth:`~loop_agent.store.LoopStore.load_or_init` / :attr:`DBProgressLog.state`)
   to ``run_loop(initial_state=...)``. ``iteration`` / ``tokens_used`` / ``elapsed`` /
   ``history`` are restored, and execution **continues** from the paused iteration (does not
   revisit already-executed gates). gate key is iteration-based, so when resuming and hitting
   the "paused gate", it gets the correct key, and the persisted decision re-matches.
   Accumulated totals are also restored, so :class:`~loop_agent.conditions.TokenBudget` /
   :class:`~loop_agent.conditions.Timeout` work correctly across runs, and ``gather`` depending
   on ``history`` is consistent with the first run.
2. **replay resume (no ``initial_state``)**: Backwards-compatibility mode starting fresh from
   iteration 0. Already-executed gates skip (executed-skip, non-persistent), non-gate actions
   are re-executed by ``act``. In this mode, accumulated totals appear reset from the prior run,
   and already-executed gate skip placeholders can diverge ``history`` contents that depend on
   ``gather`` (= requires **idempotent non-gate actions and iteration-deterministic proposals**).
   If cross-run accumulated limits or history-dependent resume are needed, use ``initial_state``
   resume.

In either mode, the canonical source for each step remains in the ``step`` row, so audit is done
from there.

**Concurrent process simultaneous resume coordination (Issue #21, Phase3)**: The same run_id can
be resumed simultaneously by multiple processes. Irreversible actions on approve/edit acquire
in-progress leases (:meth:`~loop_agent.store.LoopStore.acquire_lease`'s ``resolved -> executing``
single-winner transition), giving only 1 process execution rights. This provides:

- **Exactly-once execution**: Only 1 process succeeds at ``resolved -> executing``. The winner
  executes ``act`` and confirms ``executed`` via :meth:`~loop_agent.store.LoopStore.complete_execution`.
  The rest prevent double execution via one of the following.
- **Order consistency**: Loser processes that reviewed the same gate while a lease holder was
  running (``executing`` and not expired) return ``GATE_PAUSE`` from :meth:`HumanGate.review`
  and pause until ``executed`` (= waiting for completion). Losers do not run subsequent iterations
  before the winner's irreversible action completes.
- **Winner crash recovery**: If the winner crashes during ``act`` and the lease expires
  (``lease_expires_at <= now``), another waiting process takes the lease again on resume
  (``took_over``) and completes execution. The step row is persisted before completion confirmation
  (driver calls :attr:`loop_agent.loop.GateReview.on_complete` after on_step), so step is not
  skipped even if the winner crashes.

Tradeoff: Lease expiry takeover re-executes ``act``, so in the rare case where the winner crashed
*after side effects but before ``executed`` confirmation*, side effects duplicate (**at-least-once**).
Truly exactly-once requires idempotence keys on the side-effect side (outside this module's scope).
To avoid this, set ``lease_ttl`` sufficiently longer than the max execution time of irreversible
actions, so lease expiry takeover doesn't happen (:class:`HumanGate`'s ``lease_ttl`` / ``now_fn``).
Lease owner is by default a unique token per process (explicit injection via ``owner``).

2 operating modes (both use :meth:`~loop_agent.store.LoopStore.resolve_decision` as the single path):

1. **async pause/resume** (``resolver=None``): If there are only unresolved decisions for
   irreversible actions, the loop returns ``status="paused"``. After the human records the
   decision via ``store.resolve_decision(...)``, re-running with the same run_id applies the
   persisted decision, **never asking the same action twice** (report.md S5 Phase2 success
   condition c).
2. **sync resolver** (pass ``resolver``): Single-process mode where a human (CLI prompt, etc.)
   returns a decision on the spot. Resolves inline without pausing, continues.

In either mode, 4 decision types map to dispositions as follows:

- ``approve`` -> proceed (execute the proposed action as-is in ``act``)
- ``edit``    -> proceed (execute the human-substituted action in ``act``)
- ``reject``  -> skip (do not execute, record rejection as 1 step, continue)
- ``respond`` -> skip (do not execute, record human response as 1 step, continue.
  Response is accessible to the next ``gather`` via ``state.history[-1]``)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .loop import (
    GATE_PAUSE,
    GATE_PROCEED,
    GATE_SKIP,
    ActHook,
    Conditions,
    GateReview,
    GatherHook,
    LoopResult,
    StepHook,
    VerifyHook,
    _default_gather,
    run_loop,
)
from .state import LoopState
from .store import (
    DECISION_KINDS,
    DEFAULT_LEASE_TTL,
    LEASE_ACQUIRED,
    LEASE_EXECUTED,
    LEASE_WAIT,
    LoopStore,
    _require_json_native,
)

# Irreversibility judgment predicate: given a proposed action (context from gather), decide whether to interrupt.
IrreversiblePredicate = Callable[[Any], bool]
# Gate key generation: (action, loop iteration) -> stable key. Must be deterministic so the same
# action gets the same key on resume. Default is iteration-based (see HumanGate below).
GateKeyFn = Callable[[Any, int], str]


@dataclass(frozen=True)
class Decision:
    """A single human gate decision (LangGraph interrupt parity).

    ``kind`` is one of 4 types in :data:`loop_agent.store.DECISION_KINDS`. ``payload`` carries
    the replacement action for ``edit`` or response message for ``respond`` (``None`` for
    ``approve`` / ``reject``).
    """

    kind: str
    payload: Any = None

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise ValueError(
                f"unknown decision {self.kind!r}; expected one of {DECISION_KINDS}"
            )


# resolver: receives pending info and returns a Decision for a synchronous human. pending is
# the dict row from request_decision (includes gate_key / action / status).
Resolver = Callable[[dict[str, Any]], Decision]


class HumanGate:
    """ActionGate implementation that interrupts only irreversible actions.

    Args:
        on: Irreversibility judgment predicate ``on(action) -> bool``. Only actions returning
            ``True`` are gate targets. reversible actions proceed unconditionally.
        store: LoopStore to persist decisions (:class:`~loop_agent.store.LoopStore`).
        run_id: Target run ID. On construction, ``load_or_init(run_id)`` reserves the run row
            (for FK and idempotent begin event).
        resolver: Optional. Synchronous human that returns a decision. ``None`` pauses on unresolved.
        key: Optional. Generates gate key via ``key(action, iteration) -> str``
            (default ``"gate-<iteration>"``). ``iteration`` is **the loop iteration at the time
            the irreversible action was reviewed**. This is the crux of stable keys: under either
            resume model — replay (fresh state from iteration 0) or #14's ``initial_state`` resume
            (continue from paused iteration) — the same action is reviewed at the same iteration,
            so keys align (counter-based seq would diverge on initial_state resume when skipping
            already-executed gates). Proposal sequence must be deterministic with respect to iteration.
        active: Gate enabled/disabled flag. ``False`` proceeds all actions (gate-wide kill switch.
            Similar to "kill" in report.md S4.5 runaway prevention).
        owner: Optional. In-progress lease holder token (Issue #21). Uniquely identifies each
            resume attempt of the same run_id. ``None`` auto-generates ``"pid<PID>-<uuid>"``
            **per construction** (= separate resume attempts in the same process get different owners).
            **Requirement: unique per resume attempt (including process restarts)**. If owner collides
            with another attempt, an unexpired lease could be misidentified as "self re-entry",
            causing double execution of irreversible actions (:meth:`~loop_agent.store.LoopStore.acquire_lease`
            re-entry branch). Do not pin stable owner across resume attempts (default auto-generation
            satisfies this requirement).
        lease_ttl: In-progress lease TTL (seconds). Must be significantly longer than single
            irreversible action execution time (too short risks lease expiry during winner execution,
            allowing another process to takeover and double-execute). Default :data:`~loop_agent.store.DEFAULT_LEASE_TTL`.
        now_fn: **Wall-clock** (epoch seconds) for lease acquisition/expiry judgment. Not monotonic,
            defaulting to ``time.time`` (different from loop's ``elapsed`` ``time_fn``), so multiple
            processes can compare clocks. Injectable for deterministic testing.
    """

    def __init__(
        self,
        *,
        on: IrreversiblePredicate,
        store: LoopStore,
        run_id: str,
        resolver: Optional[Resolver] = None,
        key: Optional[GateKeyFn] = None,
        active: bool = True,
        owner: Optional[str] = None,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.on = on
        self.store = store
        self.run_id = run_id
        self.resolver = resolver
        self.key = key
        self.active = active
        # Lease holder token: unique per process (resume attempt). Default generates process-unique.
        self.owner = owner or f"pid{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lease_ttl = lease_ttl
        self.now_fn = now_fn if now_fn is not None else time.time
        # Ensure run row (FK for request_decision and idempotent begin event).
        self.store.load_or_init(run_id)

    def review(self, context: Any, state: LoopState) -> GateReview:
        """Review proposed action and return disposition (ActionGate implementation).

        On reversible/inactive: proceed immediately. On irreversible: read persisted decision,

        - ``executed`` (= already execution-completed by approve/edit): skip. Guard against
          replay resume (fresh state from iteration 0) or loser in concurrent resume
          **double-executing** already-executed irreversible actions (#14's ``initial_state``
          resume continues from pause iteration, never revisiting executed gates).
        - ``resolved`` / ``executing``: verify action match and apply (approve/edit acquire
          in-progress lease via :meth:`~loop_agent.store.LoopStore.acquire_lease`: if acquired
          proceed, if another is executing pause until executed, if already executed skip;
          Issue #21).
        - Unresolved (unregistered or pending): if resolver exists, resolve and apply on spot;
          otherwise pause and wait for human decision.
        """
        if not self.active or not self.on(context):
            return GateReview(disposition=GATE_PROCEED, context=context)

        # Irreversible action: use loop iteration at the time of review as gate key. Under both
        # resume models (replay / initial_state), the same action is reviewed at the same iteration,
        # so persisted decision correctly re-maps to that action.
        gate_key = (
            self.key(context, state.iteration)
            if self.key is not None
            else f"gate-{state.iteration}"
        )

        # Register pending if unregistered. ``request_decision`` is idempotent, returning the
        # **authoritative current row read within its transaction**. Since there is a TOCTOU window
        # after get_decision returns None and before another connection insert/resolve, treat
        # request_decision's return value (= other's row if concurrent creation) as authoritative.
        entry = self.store.get_decision(self.run_id, gate_key)
        if entry is None:
            entry = self.store.request_decision(self.run_id, gate_key, context)

        # **Always** verify registered action matches current proposed action before branching.
        # If proposal sequence diverges across resumes and a different irreversible action arrives
        # at the same gate_key, prevent: (a) misapplying old decision to current different action,
        # (b) silently suppressing new irreversible action as "already executed", or (c) resolver
        # approving old pending for current different action. (New registration is context itself,
        # so match is trivial.)
        self._guard_action_matches(entry, context, gate_key)

        if entry["status"] == "executed":
            # Already-executed irreversible action (replay playback or concurrent resume winner). Skip without re-executing.
            return self._already_executed_skip(gate_key)
        if entry["status"] in ("resolved", "executing"):
            # Apply already-rendered decision (never ask human twice). Concurrent resolve also
            # merges here via get_decision/request_decision's authoritative row. ``executing`` is
            # state where another process holds lease execution (or self re-entry / lease expired);
            # only approve/edit reach here (reject/respond never become executing). _apply_resolved ->
            # lease acquisition determines WAIT/ACQUIRED/EXECUTED (Issue #21).
            return self._apply_resolved(
                Decision(entry["decision"], entry["payload"]), context, gate_key
            )

        # status == "pending": unresolved.
        if self.resolver is not None:
            decision = self.resolver(entry)
            if not isinstance(decision, Decision):
                raise TypeError(
                    "resolver must return a Decision, got "
                    f"{type(decision).__name__}"
                )
            # resolve_decision requires JSON-native edit payload, so payload is lossless by
            # the time we reach here. Apply the Decision returned by resolver directly without
            # store round-trip to avoid unnecessary serialization overhead.
            self.store.resolve_decision(
                self.run_id, gate_key, decision.kind, decision.payload
            )
            return self._apply_resolved(decision, context, gate_key)

        # No resolver: pause and wait for human decision. Decision is already persisted in store,
        # so re-running with the same run_id applies it via the resolved branch above.
        return GateReview(disposition=GATE_PAUSE, pending=entry)

    def _guard_action_matches(
        self, entry: dict[str, Any], context: Any, gate_key: str
    ) -> None:
        """Verify registered action matches current proposed action (defensive guard).

        gate key is determined from proposal sequence order, so if proposal sequence is
        deterministic across resume, the same action gets the same key as at registration
        (contract). If it diverges, reject loudly instead of silently **misapplying decision
        to a different irreversible action**.

        ``stored`` is lossless after :func:`_require_json_native` validation at registration.
        ``current`` context also demands JSON-native conversion for matching. Skipping this
        allows ``(1, 2)`` to become ``[1, 2]`` and misidentify as a different action.
        Deterministic, JSON-native proposal sequence prevents false positives.
        """
        stored = entry.get("action")
        current = json.loads(_require_json_native(context, "gated action"))
        if stored != current:
            raise ValueError(
                f"gate {gate_key}: proposed action does not match the action this "
                f"decision was recorded for (stored={stored!r}, current={current!r}); "
                "the proposal sequence is non-deterministic across resume"
            )

    def _already_executed_skip(self, gate_key: str) -> GateReview:
        """Return GateReview that skips already-executed irreversible action on replay.

        observation is kept hashable string (NoProgress default key workaround.
        See :meth:`_apply_resolved`). ``persist=False``: this is a replay no-op where resume
        just skips over a step already executed/persisted in prior run, so don't flow through
        on_step and corrupt the canonical step row (original observation / tokens).
        """
        return GateReview(
            disposition=GATE_SKIP,
            observation=f"gate-skipped:already-executed:{gate_key}",
            detail=f"gate {gate_key} already executed in a prior run",
            persist=False,
        )

    def _wait_for_executing(self, gate_key: str) -> GateReview:
        """Return GateReview that pauses at gate while another process holds executing lease (Issue #21).

        Loser does not execute irreversible action, waits for winner to confirm ``executed``
        (order consistency). pause means step is not recorded, does not advance to next iteration.
        ``pending`` carries current decision row (``status='executing'`` and lease info) so
        re-review after resume can judge executed/expired.
        """
        pending = self.store.get_decision(self.run_id, gate_key)
        return GateReview(disposition=GATE_PAUSE, pending=pending)

    def _make_on_complete(self, gate_key: str) -> Callable[[], None]:
        """Return completion closure for lease holder to call after ``act`` completes (Issue #21).

        driver calls this **after** persisting step (:func:`loop_agent.loop.run_loop`).
        :meth:`~loop_agent.store.LoopStore.complete_execution` confirms ``executing -> executed``.
        If own lease expired and was taken over, returns 0 rows updated (False), meaning
        this ``act`` side effects may have duplicated (at-least-once from lease expiry takeover).
        """

        def _complete() -> None:
            self.store.complete_execution(self.run_id, gate_key, self.owner)

        return _complete

    def _apply_resolved(
        self, decision: Decision, context: Any, gate_key: str
    ) -> GateReview:
        """Map 4 resolved decision types to driver's 3 dispositions.

        approve/edit acquire in-progress lease (:meth:`~loop_agent.store.LoopStore.acquire_lease`)
        as single-winner, **only acquired calls proceed** (Issue #21). Completion is confirmed
        via ``on_complete`` called by driver after step persistence (:meth:`~loop_agent.store.LoopStore.complete_execution`)
        to ``executed``. Lease outcomes:

        - ACQUIRED: proceed (acquirer executes ``act``). Completion confirmed via ``on_complete``.
        - WAIT: another process executing with valid lease. pause until ``executed`` (order consistency).
        - EXECUTED: already executed. skip (no double execution).

        reject/respond don't execute, don't acquire lease, don't transition to ``executed``
        (consistently skip on replay).

        skip-family steps (reject/respond and executed replay) must record **hashable** ``observation``.
        observation is stacked in ``state.history``, and next guard has :class:`~loop_agent.conditions.NoProgress`
        default key (= observation) hashed by ``Counter``. Structural notes go in ``detail`` string;
        respond payload goes to next ``gather`` as observation (if non-hashable, that's user responsibility
        like act-origin observations = NoProgress default contract).
        """
        if decision.kind in ("approve", "edit"):
            lease = self.store.acquire_lease(
                self.run_id,
                gate_key,
                self.owner,
                now=self.now_fn(),
                ttl=self.lease_ttl,
            )
            outcome = lease["outcome"]
            if outcome == LEASE_EXECUTED:
                # Already execution-completed (replay playback or concurrent resume winner completed). Skip.
                return self._already_executed_skip(gate_key)
            if outcome == LEASE_WAIT:
                # Another process executing. pause until executed for order consistency.
                return self._wait_for_executing(gate_key)
            # LEASE_ACQUIRED: execution rights acquired. Pass on_complete to driver that calls
            # complete_execution after act (called after step persistence, so if executed, step exists).
            on_complete = self._make_on_complete(gate_key)
            if decision.kind == "approve":
                # context unchanged (execute gathered proposal action as-is).
                return GateReview(disposition=GATE_PROCEED, on_complete=on_complete)
            # edit: execute human-substituted action.
            return GateReview(
                disposition=GATE_PROCEED,
                context=decision.payload,
                on_complete=on_complete,
            )
        if decision.kind == "reject":
            return GateReview(
                disposition=GATE_SKIP,
                observation=f"gate-skipped:rejected:{gate_key}",
                detail=f"human rejected gate {gate_key}",
            )
        # respond: don't execute, record human response (payload as observation for next).
        return GateReview(
            disposition=GATE_SKIP,
            observation=decision.payload,
            detail=f"human responded at gate {gate_key}",
        )


def run_gated_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    on: IrreversiblePredicate,
    store: LoopStore,
    run_id: str,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    resolver: Optional[Resolver] = None,
    key: Optional[GateKeyFn] = None,
    active: bool = True,
    owner: Optional[str] = None,
    lease_ttl: float = DEFAULT_LEASE_TTL,
    now_fn: Optional[Callable[[], float]] = None,
    time_fn: Optional[Callable[[], float]] = None,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """Entry point that assembles HumanGate and runs run_loop.

    Takes the same ``act`` / ``verify`` / ``conditions`` / ``gather`` / ``on_step`` /
    ``initial_state`` as ``run_loop``, and adds human gate assembly (``on`` / ``store`` /
    ``run_id`` / ``resolver`` / ``key`` / ``active``, plus lease coordination for
    concurrent-process resume: ``owner`` / ``lease_ttl`` / ``now_fn``; details in :class:`HumanGate`).
    To persist decisions, pass :meth:`loop_agent.store.DBProgressLog.on_step` to ``on_step``.
    To resume a paused run **continuing from pause point**, pass its persisted state
    (:attr:`~loop_agent.store.DBProgressLog.state`, etc.) to ``initial_state`` (omit for
    replay resume from iteration 0. Difference explained in HumanGate docstring "resume's 2 models").
    """
    gate = HumanGate(
        on=on,
        store=store,
        run_id=run_id,
        resolver=resolver,
        key=key,
        active=active,
        owner=owner,
        lease_ttl=lease_ttl,
        now_fn=now_fn,
    )
    run_kwargs: dict[str, Any] = {}
    if time_fn is not None:
        run_kwargs["time_fn"] = time_fn
    return run_loop(
        act=act,
        verify=verify,
        conditions=conditions,
        gather=gather,
        on_step=on_step,
        gate=gate,
        initial_state=initial_state,
        **run_kwargs,
    )


__all__ = [
    "Decision",
    "HumanGate",
    "run_gated_loop",
    "IrreversiblePredicate",
    "GateKeyFn",
    "Resolver",
]
