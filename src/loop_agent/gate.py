"""Scoped human gate: human-in-the-loop that interrupts only irreversible operations (Issue #15).

This is a minimal implementation of report.md S4.5 / R6 / Principle 8, which
requires human gates to be **limited** to irreversible or broad-impact actions.
It supports the same four human decisions as LangGraph's ``interrupt()``
(**approve / edit / reject / respond**) and persists decisions in state.db
(:mod:`loop_agent.store`) so they are **kept across pause -> resume**.

Design boundaries:

- **The loop core is gate-independent**. :func:`loop_agent.loop.run_loop` only
  knows the :class:`~loop_agent.loop.ActionGate` protocol
  (``review(context, state)``) and only interprets the three dispositions
  proceed / skip / pause. The store and human lifecycle stay hidden behind this
  module's :class:`HumanGate`.
- **Irreversibility is injected from outside the loop**. The ``on(action) -> bool``
  predicate decides whether a proposal is irreversible. Reversible actions and
  ``gate=None`` never interrupt (= this is not an all-step gate; report.md's
  "irreversible only" rule is enforced structurally).
- **claude-org pending_decisions are reused by remapping roles**. The pattern
  "a secretary registers a worker's judgment request and a user response
  resolves it" maps to "the loop registers an irreversible action and a human
  resolves it" (:meth:`loop_agent.store.LoopStore.request_decision` /
  :meth:`~loop_agent.store.LoopStore.resolve_decision`).
- **Irreversible actions are exactly-once and ordered across resume (Issue #21)**.
  Irreversible actions executed for approve/edit acquire a single-winner
  **in-progress lease** with :meth:`~loop_agent.store.LoopStore.acquire_lease`
  (``resolved -> executing``), then mark ``executed`` with
  :meth:`~loop_agent.store.LoopStore.complete_execution` after ``act`` completes.
  Loser processes that see the same gate while the lease is held (``executing``)
  **pause until ``executed``**, so they cannot run a later iteration before the
  winner's irreversible action finishes. If the winner crashes and the lease
  expires, another process retakes the lease and completes execution, so the
  step is not lost. Revisiting an already executed gate during replay resume
  (below) sees ``executed``, skips, and **does not execute twice** (preventing
  duplicate deploys and similar failures, which is the point of the gate).

**The two resume models and gate consistency**: the gate key is determined from
``state.iteration`` at review time (see :class:`HumanGate`). This is stable in
both resume models:

1. **``initial_state`` resume (#14, recommended)**: pass the interrupted
   :class:`~loop_agent.state.LoopState`
   (:meth:`~loop_agent.store.LoopStore.load_or_init` / :attr:`DBProgressLog.state`)
   to ``run_loop(initial_state=...)``. ``iteration`` / ``tokens_used`` /
   ``elapsed`` / ``history`` are restored and execution **continues** from the
   interrupted iteration (already executed gates are not revisited). Because gate
   keys are iteration-based, the first gate reached after resume is assigned the
   correct key for the interrupted gate and matches the persisted decision.
   Cumulative metrics are also restored, so
   :class:`~loop_agent.conditions.TokenBudget` /
   :class:`~loop_agent.conditions.Timeout` work correctly across runs, and
   ``history``-dependent ``gather`` remains consistent with the first run.
2. **replay resume (without ``initial_state``)**: a backward-compatible mode that
   replays from iteration 0 with fresh state. Already executed gates are skipped
   with a non-persistent executed-skip, and non-gated actions rerun ``act``. In
   this mode, cumulative metrics appear reset for the prior run, and the skip
   placeholders for already executed gates can make ``history``-dependent
   ``gather`` diverge (= it assumes **idempotent non-gated actions and an
   iteration-deterministic proposal sequence**). Use ``initial_state`` resume if
   cumulative limits or history-dependent restart behavior must span runs.

In either mode, the authoritative record for each step remains in the ``step``
rows, so audits can be performed from there.

**Coordinating simultaneous multi-process resume (Issue #21, Phase3)**: multiple
processes may resume the same run_id *at the same time*. Irreversible approve/edit
actions use an in-progress lease (the ``resolved -> executing`` single-winner
transition in :meth:`~loop_agent.store.LoopStore.acquire_lease`) so only one
process gets execution rights. This provides:

- **exactly-once execution**: only one process can succeed at
  ``resolved -> executing``. The winner runs ``act`` and finalizes ``executed``
  with :meth:`~loop_agent.store.LoopStore.complete_execution`. The rest do not
  execute twice through one of the paths below.
- **ordered execution**: a loser that reviews the same gate while the lease is
  held (``executing`` and unexpired) returns ``GATE_PAUSE`` from
  :meth:`HumanGate.review` and pauses until ``executed`` (= waits for completion).
  Losers do not run later iterations before the winner's irreversible action
  completes.
- **winner crash recovery**: if the winner crashes during ``act`` and the lease
  expires (``lease_expires_at <= now``), another waiting process retakes the
  lease on resume (``took_over``) and finishes execution. The step row is
  persisted before completion is finalized (the driver calls
  :attr:`loop_agent.loop.GateReview.on_complete` after on_step), so a winner
  crash does not lose the step.

Tradeoff: retaking an expired lease reruns ``act``, so in the rare case where the
winner crashes *after causing side effects but before finalizing ``executed``*,
the side effect can be duplicated (**at-least-once**). True exactly-once requires
an idempotency key on the side-effecting system (outside this module's scope). To
avoid this, set ``lease_ttl`` comfortably longer than the maximum duration of an
irreversible action so lease retake does not happen
(:class:`HumanGate` ``lease_ttl`` / ``now_fn``). By default, the lease owner is a
per-process unique token (it can also be injected explicitly with ``owner``).

Two operating modes (both use :meth:`~loop_agent.store.LoopStore.resolve_decision`
as the single path for decisions):

1. **async pause/resume** (``resolver=None``): if an irreversible action only has
   an unresolved decision, the loop returns with ``status="paused"``. After a
   human records a decision with ``store.resolve_decision(...)`` and the same
   run_id is executed again, the gate reads the persisted decision, applies it,
   and continues **without asking about the same action twice** (report.md S5
   Phase2 success condition c).
2. **synchronous resolver** (pass ``resolver``): a mode for a single process where
   a human (for example, a CLI prompt) returns the decision immediately. The loop
   resolves inline and proceeds without pausing.

In both modes, the four decisions map to dispositions as follows:

- ``approve`` -> proceed (run the proposed action as-is with ``act``)
- ``edit``    -> proceed (run the human-replaced action with ``act``)
- ``reject``  -> skip (do not execute; record the rejection as one step and continue)
- ``respond`` -> skip (do not execute; record the human response as one step and
  continue. The response can be incorporated into the next ``gather`` context via
  ``state.history[-1]``)
"""

from __future__ import annotations

import json
import os
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

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
from .errors import ConfigError, StateError
from .notify import ApprovalDescriber, ApprovalRequest, Notifier, _summarize_action
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

# Irreversibility predicate: decides whether the proposed action (the context
# returned by gather) should interrupt.
IrreversiblePredicate = Callable[[Any], bool]
# Gate key generation: (action, loop iteration) -> stable key. It must be
# deterministic so the same action receives the same key on resume. The default
# is iteration-based (see HumanGate below).
GateKeyFn = Callable[[Any, int], str]


@dataclass(frozen=True)
class Decision:
    """One human gate decision (parity with LangGraph interrupt).

    ``kind`` is one of the four values in
    :data:`loop_agent.store.DECISION_KINDS`. ``payload`` carries the replacement
    action for ``edit`` or the response message for ``respond`` (``None`` for
    ``approve`` / ``reject``).
    """

    kind: str
    payload: Any = None

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise ConfigError(
                f"unknown decision {self.kind!r}; expected one of {DECISION_KINDS}"
            )


# Resolver: a synchronous human that receives pending information and returns a
# Decision. ``pending`` is the row dict returned by request_decision (including
# gate_key / action / status).
Resolver = Callable[[dict[str, Any]], Decision]


class HumanGate:
    """An :class:`~loop_agent.loop.ActionGate` implementation that interrupts only irreversible actions.

    Args:
        on: Irreversibility predicate ``on(action) -> bool``. Only actions where
            this returns ``True`` are gated. Reversible actions proceed
            unconditionally.
        store: :class:`~loop_agent.store.LoopStore` that persists decisions.
        run_id: ID of the target run. Construction calls ``load_or_init(run_id)``
            to ensure the run row exists (for the FK and idempotent begin event).
        resolver: Optional synchronous human decision provider. ``None`` pauses
            when a decision is unresolved.
        key: Optional ``key(action, iteration) -> str`` gate key generator
            (default ``"gate-<iteration>"``). ``iteration`` is **the loop
            iteration at which the irreversible action was reviewed**. This is
            the core of key stability: in both resume models, replay (replaying
            from iteration 0 with fresh state) and #14 ``initial_state`` resume
            (continuing from the interrupted iteration), the same action is
            reviewed at the same iteration and therefore receives the same key.
            An occurrence-order counter would shift when initial_state resume
            skips already executed gates. The proposal sequence must be
            deterministic with respect to iteration.
        active: Whether the gate is enabled. ``False`` proceeds all actions (a
            full gate-disable switch, similar to the "full stop" runaway
            prevention in report.md S4.5).
        owner: Optional in-progress lease owner token (Issue #21). It uniquely
            identifies each attempt to resume the same run_id. ``None`` generates
            ``"pid<PID>-<uuid>"`` **on each construction** (= separate resume
            attempts in the same process also get different owners). The
            requirement is uniqueness **per resume attempt, including process
            restarts**: if another attempt collides on owner, it can mistake an
            unexpired lease for its own re-entry and execute an irreversible
            action twice (the re-entry branch of
            :meth:`~loop_agent.store.LoopStore.acquire_lease`). Do not pin a
            stable owner across attempts; the default auto-generated owner
            satisfies this requirement.
        lease_ttl: TTL in seconds for the in-progress lease. It should be much
            longer than one irreversible action execution (if it is too short,
            the lease can expire while the winner is running and another process
            can take it, causing duplicate execution). The default is
            :data:`~loop_agent.store.DEFAULT_LEASE_TTL`.
        now_fn: **Wall-clock** time source (epoch seconds) used for lease
            acquisition and expiry checks. The default is ``time.time`` rather
            than monotonic time so multiple processes can compare timestamps
            (this is separate from the loop ``time_fn`` used for ``elapsed``).
            Injectable for deterministic tests.
        notifier: Optional notification backend. When a new approval request
            (pending) is registered **for the first time in this process**, it
            calls :meth:`loop_agent.notify.Notifier.notify` best-effort
            (:mod:`loop_agent.notify` webhook / Slack / email / fan-out).
            ``None`` (the default) preserves the previous no-notification
            behavior. Notification failures become warnings and do not stop
            :class:`HumanGate` (the approval request itself is already persisted
            in the store, so the loop can continue even if notification fails).
            Resuming an already registered gate does not notify again (because
            ``entry`` already exists). Notifications are also skipped when
            ``resolver`` is used, because synchronous inline resolution does not
            wait for a human.
        describe: Optional ``describe(action) -> Mapping`` that derives or
            overrides the notification payload
            (:class:`~loop_agent.notify.ApprovalRequest`) ``summary`` /
            ``action_kind`` / ``deadline``. If omitted, ``summary`` is generated
            from the action (:func:`loop_agent.notify._summarize_action`).
            Not called when ``notifier=None``.
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
        notifier: Optional[Notifier] = None,
        describe: Optional[ApprovalDescriber] = None,
    ) -> None:
        self.on = on
        self.store = store
        self.run_id = run_id
        self.resolver = resolver
        self.key = key
        self.active = active
        # Lease owner token: unique per process (resume attempt). The default
        # generates a process-unique value.
        self.owner = owner or f"pid{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lease_ttl = lease_ttl
        self.now_fn = now_fn if now_fn is not None else time.time
        self.notifier = notifier
        self.describe = describe
        # Ensure the run row exists (for the request_decision FK and idempotent
        # begin event).
        self.store.load_or_init(run_id)

    def review(self, context: Any, state: LoopState) -> GateReview:
        """Review the proposed action and return a disposition (:class:`ActionGate` implementation).

        Reversible actions and disabled gates proceed immediately. For
        irreversible actions, read the persisted decision:

        - ``executed`` (= approve/edit already completed execution): skip. This
          guards replay resume (replaying from iteration 0 with fresh state) and
          losers in concurrent resume from **executing an already executed
          irreversible action twice** (#14 ``initial_state`` resume continues
          from the interrupted iteration, so it does not revisit executed gates).
        - ``resolved`` / ``executing``: verify that the action matches, then
          apply the decision (approve/edit acquire an in-progress lease with
          :meth:`~loop_agent.store.LoopStore.acquire_lease`; proceed if acquired,
          pause until executed if another process is running, or skip if already
          executed; Issue #21).
        - Unresolved (unregistered or pending): resolve inline if a resolver is
          present; otherwise pause and wait for a human decision.
        """
        if not self.active or not self.on(context):
            return GateReview(disposition=GATE_PROCEED, context=context)

        # Irreversible action: use the loop iteration at review time as the gate
        # key. In both resume models (replay / initial_state), the same action is
        # reviewed at the same iteration, so the persisted decision matches the
        # action correctly.
        gate_key = (
            self.key(context, state.iteration)
            if self.key is not None
            else f"gate-{state.iteration}"
        )

        # If unregistered, register a pending decision. ``request_decision`` is
        # idempotent and returns **the authoritative current row read inside its
        # transaction**. Since another connection can insert/resolve in the
        # TOCTOU window after get_decision returns None, treat the return value
        # from request_decision (= the other row if created concurrently) as
        # authoritative when None was observed.
        entry = self.store.get_decision(self.run_id, gate_key)
        if entry is None:
            # register_decision returns the authoritative row plus whether
            # **this call inserted it**. A new approval request fires exactly on
            # this INSERT: notify only when ``created`` is true. Do not notify
            # when resume revisits an existing entry, or when a loser receives
            # another process's row after a TOCTOU race (created=False). Also do
            # not notify when using a resolver: the immediately following
            # resolver branch resolves synchronously inline and **does not wait
            # for a human** (so it must not incorrectly page an external channel
            # for "pending approval").
            entry, created = self.store.register_decision(
                self.run_id, gate_key, context
            )
            if created and self.resolver is None:
                self._notify_new_request(gate_key, context)

        # Before entering any branch, **always** verify that the registered
        # action matches the current proposed action. If the proposal sequence
        # shifts across resume and another irreversible action reaches the same
        # gate_key, this prevents (a) misapplying an old decision to a different
        # current action, (b) silently suppressing a new irreversible action as
        # already executed, or (c) a resolver approving an old pending row and
        # executing a different current action. Newly registered rows obviously
        # match because they store the context itself.
        self._guard_action_matches(entry, context, gate_key)

        if entry["status"] == "executed":
            # Already executed irreversible action (replay or concurrent resume
            # winner). Skip without re-executing.
            return self._already_executed_skip(gate_key)
        if entry["status"] in ("resolved", "executing"):
            # Apply an existing decision (do not ask the human twice).
            # Concurrent resolves also join here through the authoritative row
            # from get_decision/request_decision. ``executing`` means another
            # process is running under the lease (or this is our re-entry /
            # expiry); only approve/edit reach this state (reject/respond never
            # become executing). _apply_resolved -> lease acquisition determines
            # WAIT/ACQUIRED/EXECUTED (Issue #21).
            return self._apply_resolved(
                Decision(entry["decision"], entry["payload"]), context, gate_key
            )

        # status == "pending": unresolved.
        if self.resolver is not None:
            decision = self.resolver(entry)
            if not isinstance(decision, Decision):
                raise ConfigError(
                    "resolver must return a Decision, got "
                    f"{type(decision).__name__}"
                )
            # resolve_decision requires edit payloads to be JSON-native, so by
            # this point the payload is lossless. Apply the original Decision
            # returned by the resolver directly instead of round-tripping
            # through store decoding.
            self.store.resolve_decision(
                self.run_id, gate_key, decision.kind, decision.payload
            )
            return self._apply_resolved(decision, context, gate_key)

        # No resolver: interrupt and wait for a human decision. The decision is
        # persisted in the store, so running again with the same run_id applies
        # it through the resolved branch above.
        return GateReview(disposition=GATE_PAUSE, pending=entry)

    def _build_request(self, gate_key: str, action: Any) -> ApprovalRequest:
        """Build the notification payload (:class:`~loop_agent.notify.ApprovalRequest`).

        ``summary`` prefers ``describe`` when available; otherwise it is
        generated automatically from the action. ``summary`` / ``action_kind`` /
        ``deadline`` can be overridden by values returned from ``describe``.
        ``created_at`` uses the gate wall-clock (``now_fn``).
        """
        fields: dict[str, Any] = {"summary": _summarize_action(action)}
        if self.describe is not None:
            extra = self.describe(action)
            if extra:
                if not isinstance(extra, Mapping):
                    raise ConfigError(
                        "describe must return a Mapping of ApprovalRequest fields, "
                        f"got {type(extra).__name__}"
                    )
                fields.update(extra)
        return ApprovalRequest(
            run_id=self.run_id,
            gate_key=gate_key,
            action=action,
            summary=str(fields.get("summary", "")),
            action_kind=fields.get("action_kind"),
            deadline=fields.get("deadline"),
            created_at=self.now_fn(),
        )

    def _notify_new_request(self, gate_key: str, action: Any) -> None:
        """Notify the notifier of a new approval request best-effort (failures do not stop the loop).

        If ``notifier=None``, do nothing. Any failure in the notification path,
        including exceptions raised by ``describe``, is surfaced with
        ``warnings.warn`` (RuntimeWarning) without stopping :class:`HumanGate`.
        """
        if self.notifier is None:
            return
        try:
            request = self._build_request(gate_key, action)
            self.notifier.notify(request)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort and must not stop the loop
            warnings.warn(
                f"gate {gate_key}: notifier "
                f"{type(self.notifier).__name__} failed: "
                f"{type(exc).__name__}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _guard_action_matches(
        self, entry: dict[str, Any], context: Any, gate_key: str
    ) -> None:
        """Verify that the registered action matches the current proposed action (defense).

        The gate key is derived from the irreversible action occurrence sequence,
        so if the proposal sequence is deterministic across resume, the same key
        is assigned to the same action that was registered (the contract). If it
        ever shifts, fail loudly instead of silently allowing **a decision to be
        applied to the wrong irreversible action**.

        ``stored`` was validated by :func:`_require_json_native` at registration
        time and is lossless. Require the current ``context`` to be JSON-native
        too, making it lossless in the same way. Without this, ``(1, 2)`` could
        become ``[1, 2]`` and incorrectly compare equal to a different action.
        If the proposal sequence is deterministic and JSON-native, this does not
        produce false positives.
        """
        stored = entry.get("action")
        current = json.loads(_require_json_native(context, "gated action"))
        if stored != current:
            raise StateError(
                f"gate {gate_key}: proposed action does not match the action this "
                f"decision was recorded for (stored={stored!r}, current={current!r}); "
                "the proposal sequence is non-deterministic across resume"
            )

    def _already_executed_skip(self, gate_key: str) -> GateReview:
        """Return a GateReview that skips an already executed irreversible action during replay.

        The observation is a hashable string (for the NoProgress default key; see
        :meth:`_apply_resolved`). ``persist=False`` because this is a replay
        no-op where resume only skips a step that was executed and persisted in a
        prior run; do not pass it to on_step and overwrite the real step row
        (real observation / tokens).
        """
        return GateReview(
            disposition=GATE_SKIP,
            observation=f"gate-skipped:already-executed:{gate_key}",
            detail=f"gate {gate_key} already executed in a prior run",
            persist=False,
        )

    def _wait_for_executing(self, gate_key: str) -> GateReview:
        """Return a GateReview that pauses on a gate whose lease is executing in another process (Issue #21).

        The loser does not execute the irreversible action and waits until the
        winner finalizes ``executed`` (ordering). Because this pauses, no step is
        recorded and execution does not advance to later iterations. ``pending``
        carries the current decision row (``status='executing'`` plus lease
        information) so the next review after resume can determine whether it was
        executed or expired.
        """
        pending = self.store.get_decision(self.run_id, gate_key)
        return GateReview(disposition=GATE_PAUSE, pending=pending)

    def _make_on_complete(self, gate_key: str) -> Callable[[], None]:
        """Return the completion-finalizing closure called by the lease holder after ``act`` completes (Issue #21).

        The driver calls this *after* persisting the step
        (:func:`loop_agent.loop.run_loop`). :meth:`~loop_agent.store.LoopStore.complete_execution`
        finalizes ``executing -> executed``. If this holder's lease expired and
        another process retook it, the update affects 0 rows (False); in that
        case the side effects of this ``act`` may have been duplicates
        (at-least-once behavior from lease retake after expiry).
        """

        def _complete() -> None:
            self.store.complete_execution(self.run_id, gate_key, self.owner)

        return _complete

    def _apply_resolved(
        self, decision: Decision, context: Any, gate_key: str
    ) -> GateReview:
        """Map the four resolved decisions to the driver's three dispositions.

        approve/edit acquire the in-progress lease
        (:meth:`~loop_agent.store.LoopStore.acquire_lease`) as a single winner,
        and **only the call that acquires it** proceeds (Issue #21). Completion is
        finalized as ``executed`` by ``on_complete``
        (:meth:`~loop_agent.store.LoopStore.complete_execution`), which the
        driver calls after persisting the step. Lease outcomes:

        - ACQUIRED: proceed (the acquirer runs ``act``). ``on_complete``
          finalizes completion.
        - WAIT: another process is running under a valid lease. Pause until
          ``executed`` (ordering).
        - EXECUTED: already executed. Skip (do not execute twice).

        reject/respond do not execute, so they do not take a lease or transition
        to ``executed`` (they consistently skip even during replay).

        The ``observation`` recorded by skip paths (reject/respond and executed
        replay) must **always be hashable**. Observations are appended to
        ``state.history``, and the next guard may use the default key for
        :class:`~loop_agent.conditions.NoProgress` (= observation), which hashes
        values with ``Counter``. Put structural notes in the string ``detail``
        field, and pass the response body for respond as the observation for the
        next ``gather``. If that response is non-hashable, it is the user's
        responsibility, the same as an ``act``-derived observation under the
        NoProgress default contract.
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
                # Already completed execution (replay or a concurrent resume
                # winner has completed). Skip.
                return self._already_executed_skip(gate_key)
            if outcome == LEASE_WAIT:
                # Another process is executing. Pause and wait until executed to
                # preserve ordering.
                return self._wait_for_executing(gate_key)
            # LEASE_ACQUIRED: execution rights acquired. Pass the driver an
            # on_complete that calls complete_execution after act completes (it
            # is called after step persistence, so executed always has a step).
            on_complete = self._make_on_complete(gate_key)
            if decision.kind == "approve":
                # Leave context unchanged (execute the proposed action from gather as-is).
                return GateReview(disposition=GATE_PROCEED, on_complete=on_complete)
            # edit: execute the action supplied by the human.
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
        # respond: do not execute; record the human response (pass the response
        # body to the next step as the observation).
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
    notifier: Optional[Notifier] = None,
    describe: Optional[ApprovalDescriber] = None,
    time_fn: Optional[Callable[[], float]] = None,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """Entry point that builds :class:`HumanGate` and runs :func:`~loop_agent.loop.run_loop`.

    Takes the same ``act`` / ``verify`` / ``conditions`` / ``gather`` /
    ``on_step`` / ``initial_state`` as ``run_loop`` and adds human gate
    configuration (``on`` / ``store`` / ``run_id`` / ``resolver`` / ``key`` /
    ``active``; ``owner`` / ``lease_ttl`` / ``now_fn`` for multi-process resume
    lease coordination; and ``notifier`` / ``describe`` for external
    notifications when approval requests are created. See :class:`HumanGate` for
    details). To persist decisions together with step progress, pass
    :meth:`loop_agent.store.DBProgressLog.on_step` as ``on_step``.

    To resume a paused run **from the interruption point**, pass its persisted
    state (:attr:`~loop_agent.store.DBProgressLog.state`, for example) as
    ``initial_state``. Omitting it uses replay resume from iteration 0; see
    :class:`HumanGate`'s docstring section "The two resume models and gate
    consistency" for the difference.
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
        notifier=notifier,
        describe=describe,
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
