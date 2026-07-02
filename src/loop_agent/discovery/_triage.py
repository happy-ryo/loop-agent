"""work-discovery: input selection for the next iteration (propose-only / human gate preserved, Issue #24).

Implements the **work-discovery** described by report.md S3.5 / S4.6 / S5 Phase 3.
This is the input-selection loop that decides "what to iterate on next" after a
completed loop. Its **two-layer structure of a compute layer (read-only,
deterministic) and a delivery layer (human gate)** structurally guarantees that
"discovery autonomy increases, but the decision to start remains with the human"
(report.md S3.5 INV).

Separation of responsibilities between the two layers:

- **Compute layer (:func:`triage`)**: a pure function with no side effects and the
  same output for the same input. It triages a set of candidates (:class:`Candidate`)
  against ``done`` (the set of completed ids): dependency resolution (*ready* when
  all deps are done), deterministic ranking by priority and effort, reasons for
  unmet dependencies, and dependency-cycle detection. It returns "N candidates +
  one recommendation" (report.md S3.5) as :class:`Triage`. It never touches loop
  state (read-only).
- **Delivery layer (:class:`WorkDiscovery`)**: registers the triage result as a
  **proposal** in the human-gate register in state.db (``pending_decision`` on
  :class:`~loop_agent.store.LoopStore`). It **always stops here (propose-only)**:
  nothing starts fully automatically; the proposal remains pending until the human
  accepts or rejects it through :meth:`~loop_agent.store.LoopStore.resolve_decision`
  (= the same path as the MVP limited human gate). Only an accepted candidate becomes
  input to the next loop.

**propose-only inheritance** (report.md S5 Phase 3): the MVP limited human gate
(:mod:`loop_agent.gate`) stopped before executing an irreversible *action*. This
layer reinterprets that human gate as **input selection**: it stops before *adopting*
the target of the next iteration. The four decisions from LangGraph interrupt parity
(approve / edit / reject / respond) map to adoption as follows:

- ``approve`` -> adopt the recommended candidate
- ``edit``    -> adopt another human-selected *ready* candidate (id supplied in payload)
- ``reject``  -> adopt nothing (do not start a next iteration)
- ``respond`` -> adopt nothing and record the human response (available to later triage context)

**Completed -> next-iteration connection** (:func:`discover_next`, report.md S5
Phase 3 success criterion d): a proposal is created only when the previous loop
result (:class:`~loop_agent.loop.LoopResult`) has **completed**. When it is
``paused`` (interrupted at a human gate), nothing has completed yet, so no proposal
is created. This keeps the "completed -> next-iteration input selection (through a
human gate)" chain passing through a human adoption decision every time (= no fully
automatic start).

**Reuse boundary**: the delivery layer is newly designed instead of directly using
the claude-org work-discovery delivery layer (skill / dispatcher) (report.md S4.6,
"the delivery layer is newly designed"). Human-gate persistence, however, fully
reuses the ``pending_decision`` register established in the MVP (the gate_key prefix
``"discovery-"`` separates the namespace from in-loop action gates). This gives it
pause/resume, idempotence, auditability (event log), and "a decision already made is
not decided again" for free. It is independent of Reflexion / transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..errors import ConfigError
from ..store import LoopStore

# Prefix for gate_key values used by the delivery layer. This separates the namespace
# from in-loop irreversible action gates (gate-<iteration>) so both can safely share
# the same ``pending_decision`` register.
GATE_KEY_PREFIX = "discovery-"


@dataclass(frozen=True)
class Candidate:
    """One work candidate that may become the next iteration target (compute-layer input).

    All fields must be **JSON native** because candidates are persisted to state.db by
    the delivery layer (stored as the proposal action) and later restored/adopted
    across resume. ``payload`` carries any JSON-native value to pass to the next loop
    input on adoption (task text, seed, etc.).

    Args:
        id: Stable candidate identifier (non-empty and unique within the candidate set).
            Key for dependency resolution and adoption selection.
        priority: Priority. **Higher means earlier** (descending in the ranking). Default 0.
        effort: Estimated effort (``>= 0``). For same-priority tiebreaks,
            **lower means earlier**. Default 1.
        depends_on: IDs this candidate depends on. It is *ready* if all are in ``done``.
        summary: One-line summary shown to the human at the gate.
        payload: Optional JSON-native value passed to the next loop input on adoption.
    """

    id: str
    priority: int = 0
    effort: int = 1
    depends_on: tuple[str, ...] = ()
    summary: str = ""
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ConfigError("Candidate.id must be a non-empty string")
        if self.effort < 0:
            raise ConfigError("Candidate.effort must be >= 0")
        # Normalize depends_on to a tuple (even if passed as a list) so the frozen
        # instance consistently stores an immutable tuple.
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """Deterministic key for ready ranking: priority desc -> effort asc -> id asc.

        Because ids are unique, this gives a total order and a stable ranking that does
        not depend on input order.
        """
        return (-self.priority, self.effort, self.id)


@dataclass(frozen=True)
class BlockedCandidate:
    """A candidate that is not ready and the reason (unmet / unknown / cyclic deps).

    To help the human understand at the gate why this candidate cannot be selected yet,
    unmet dependencies are classified as *waiting on known candidates* (``pending_deps``)
    or *unknown ids* (``unknown_deps``). ``in_cycle`` is set when the candidate belongs
    to a dependency cycle. ``reason`` is a one-line summary of those facts.
    """

    candidate: Candidate
    unmet: tuple[str, ...]
    pending_deps: tuple[str, ...]
    unknown_deps: tuple[str, ...]
    in_cycle: bool
    reason: str


@dataclass(frozen=True)
class Triage:
    """Compute-layer output: ranked ready, blocked, and one recommendation (report.md S3.5).

    ``ready`` is ranked by :attr:`Candidate.sort_key` (recommendation order).
    ``recommended`` is the first ready item (or ``None`` when ready is empty).
    ``blocked`` is stabilized by id ascending instead of registration order.
    """

    ready: tuple[Candidate, ...]
    blocked: tuple[BlockedCandidate, ...]
    recommended: Optional[Candidate]


def _find_cycle_ids(candidates_by_id: dict[str, Candidate]) -> set[str]:
    """Return candidate ids that **belong to cycles** in the dependency graph (diagnostic).

    Readiness itself is determined only by ``deps ⊆ done``, so cycles do not affect
    readiness. They are still detected so impossible dependencies that would keep all
    candidates permanently blocked can be shown to the human.

    Uses **Tarjan's strongly connected component (SCC) decomposition**: candidates in
    SCCs of size 2 or larger, or self-dependencies (their own id in ``depends_on``), are
    considered cyclic. A naive back-edge DFS can misclassify members that return to a
    cycle only through an already completed (BLACK) node as cross-edges and miss them
    (false negatives), so SCCs provide complete detection. External dependencies (ids
    not present as candidates) do not create edges. The iterative implementation (no
    recursion) is safe for deep graphs. Nodes are scanned in ``sorted`` order, and the
    output is a set, so it does not depend on ``depends_on`` order (deterministic).
    """
    # Internal edges only (external deps are not part of the cycle graph). Order does
    # not affect the SCC *member sets*.
    succ = {
        cid: [d for d in c.depends_on if d in candidates_by_id]
        for cid, c in candidates_by_id.items()
    }
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    scc_stack: list[str] = []
    counter = 0
    in_cycle: set[str] = set()

    for root in sorted(candidates_by_id):
        if root in index:
            continue
        # work: (node, next successor index). This turns recursive Tarjan into an
        # iterative algorithm with an explicit stack.
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter
                counter += 1
                scc_stack.append(node)
                on_stack.add(node)
            recursed = False
            succs = succ[node]
            while pi < len(succs):
                w = succs[pi]
                pi += 1
                if w not in index:
                    work[-1] = (node, pi)
                    work.append((w, 0))
                    recursed = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if recursed:
                continue
            if low[node] == index[node]:
                # node is the SCC root: pop the component from scc_stack.
                comp: list[str] = []
                while True:
                    w = scc_stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in succ[node]:
                    in_cycle.update(comp)
            work.pop()
            if work:  # Propagate child completion to the parent low value (like return).
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return in_cycle


def triage(candidates: Iterable[Candidate], *, done: Iterable[str] = ()) -> Triage:
    """Pure function that triages candidates against ``done`` (compute layer, read-only, deterministic).

    Steps (report.md S3.5 "dependency resolution, priority, and effort"):

    1. **Dependency resolution**: a candidate is *ready* when all of its ``depends_on``
       ids are in ``done``. If any dependency is missing, it is *blocked*, and the
       missing dependencies are classified as "waiting on known candidates (pending)"
       or "unknown ids (unknown)".
    2. **Ranking**: stably sort ready candidates by :attr:`Candidate.sort_key`
       (priority desc -> effort asc -> id asc), and set **recommended = first**. This
       does not depend on input order.
    3. **Cycle detection**: annotate blocked candidates with cycles in the candidate
       dependency graph as diagnostics.

    Candidates whose id is already in ``done`` are excluded as completed (not targets
    for the next iteration). The same input (regardless of order) always returns the
    same :class:`Triage`.

    Raises:
        ConfigError: Candidate ids are duplicated (unique ids are required for
            deterministic output).
    """
    items = list(candidates)
    done_set = set(done)

    by_id: dict[str, Candidate] = {}
    for c in items:
        if c.id in by_id:
            raise ConfigError(f"duplicate candidate id {c.id!r}; ids must be unique")
        by_id[c.id] = c

    # Exclude completed candidates because they are not next-iteration targets (done is
    # still used when checking dependency satisfaction).
    pending_candidates = {cid: c for cid, c in by_id.items() if cid not in done_set}
    cycle_ids = _find_cycle_ids(pending_candidates)

    ready: list[Candidate] = []
    blocked: list[BlockedCandidate] = []
    for cid, c in pending_candidates.items():
        unmet = tuple(d for d in c.depends_on if d not in done_set)
        if not unmet:
            ready.append(c)
            continue
        # Classify unmet deps as "waiting on unfinished known candidates" or "unknown
        # ids" (deduplicated while preserving order).
        seen: set[str] = set()
        pending_deps: list[str] = []
        unknown_deps: list[str] = []
        for d in unmet:
            if d in seen:
                continue
            seen.add(d)
            (pending_deps if d in by_id else unknown_deps).append(d)
        in_cycle = cid in cycle_ids
        parts: list[str] = []
        if in_cycle:
            parts.append("dependency cycle")
        if pending_deps:
            parts.append(f"unfinished dependencies: {pending_deps}")
        if unknown_deps:
            parts.append(f"unknown dependencies: {unknown_deps}")
        blocked.append(
            BlockedCandidate(
                candidate=c,
                unmet=tuple(dict.fromkeys(unmet)),
                pending_deps=tuple(pending_deps),
                unknown_deps=tuple(unknown_deps),
                in_cycle=in_cycle,
                reason="; ".join(parts),
            )
        )

    ready.sort(key=lambda c: c.sort_key)
    blocked.sort(key=lambda b: b.candidate.id)
    recommended = ready[0] if ready else None
    return Triage(
        ready=tuple(ready), blocked=tuple(blocked), recommended=recommended
    )


# -- Delivery layer (human gate, propose-only) --------------------------------


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    """Convert a candidate to a JSON-native dict (with depends_on as a list)."""
    return {
        "id": c.id,
        "priority": c.priority,
        "effort": c.effort,
        "depends_on": list(c.depends_on),
        "summary": c.summary,
        "payload": c.payload,
    }


def _candidate_from_dict(d: dict[str, Any]) -> Candidate:
    """Inverse of :func:`_candidate_to_dict` (converts depends_on back to a tuple)."""
    return Candidate(
        id=d["id"],
        priority=d.get("priority", 0),
        effort=d.get("effort", 1),
        depends_on=tuple(d.get("depends_on", ())),
        summary=d.get("summary", ""),
        payload=d.get("payload"),
    )


def _triage_to_action(triage_result: Triage, cycle: int) -> dict[str, Any]:
    """Encode a triage result as a proposal action (JSON-native dict).

    It is persisted as ``pending_decision.action`` and restored with
    :func:`_candidate_from_dict` during resume / adoption. ``recommended`` stores an id
    reference and is resolved from ready candidates on restore (avoids saving the
    candidate body twice).
    """
    return {
        "kind": "work-discovery",
        "cycle": cycle,
        "recommended": triage_result.recommended.id
        if triage_result.recommended is not None
        else None,
        "ready": [_candidate_to_dict(c) for c in triage_result.ready],
        "blocked": [
            {
                "candidate": _candidate_to_dict(b.candidate),
                "unmet": list(b.unmet),
                "pending_deps": list(b.pending_deps),
                "unknown_deps": list(b.unknown_deps),
                "in_cycle": b.in_cycle,
                "reason": b.reason,
            }
            for b in triage_result.blocked
        ],
    }


def _action_to_triage(action: dict[str, Any]) -> Triage:
    """Restore :class:`Triage` from a persisted proposal action (inverse of :func:`_triage_to_action`).

    Used by the delivery layer so the *persisted proposal* is the authority for reads
    and returns. Even if the same cycle is proposed again with a different candidate
    set (``request_decision`` does not overwrite the existing row), the returned
    :class:`Triage` always matches the **persisted proposal that is actually available
    for adoption** (avoids internal inconsistency).
    """
    ready = tuple(_candidate_from_dict(c) for c in action["ready"])
    ready_by_id = {c.id: c for c in ready}
    blocked = tuple(
        BlockedCandidate(
            candidate=_candidate_from_dict(b["candidate"]),
            unmet=tuple(b.get("unmet", ())),
            pending_deps=tuple(b.get("pending_deps", ())),
            unknown_deps=tuple(b.get("unknown_deps", ())),
            in_cycle=b.get("in_cycle", False),
            reason=b.get("reason", ""),
        )
        for b in action["blocked"]
    )
    rec_id = action.get("recommended")
    recommended = ready_by_id.get(rec_id) if rec_id is not None else None
    return Triage(ready=ready, blocked=blocked, recommended=recommended)


@dataclass(frozen=True)
class Proposal:
    """One registered proposal (triage result + persisted human-gate row).

    Returned by :meth:`WorkDiscovery.propose`. ``pending`` is the ``pending_decision``
    row returned by ``request_decision`` (including gate_key / status / action). Because
    this is **propose-only**, immediately after creation it is always
    ``status == "pending"`` (except when re-proposing a cycle that has already been
    resolved).
    """

    triage: Triage
    cycle: int
    gate_key: str
    pending: dict[str, Any]


@dataclass(frozen=True)
class AdoptionResult:
    """Result of resolving the human adoption decision (which candidate becomes next input).

    ``status`` is ``"pending"`` (undecided), ``"resolved"`` (decided), or ``"absent"``
    (no proposal exists for that cycle). ``candidate`` is the adopted candidate
    (approve/edit), or ``None`` (reject/respond/undecided). ``recommended`` is the
    proposal-time recommendation (for display). ``response`` is the response body from
    a respond decision.
    """

    status: str
    decision: Optional[str]
    candidate: Optional[Candidate]
    recommended: Optional[Candidate]
    response: Any = None

    @property
    def adopted(self) -> bool:
        """Whether a next-iteration input candidate was adopted (= ``candidate`` exists)."""
        return self.candidate is not None


class WorkDiscovery:
    """work-discovery delivery layer: put triage on the human gate as a proposal (propose-only).

    Human-gate persistence reuses the MVP ``pending_decision`` register
    (:class:`~loop_agent.store.LoopStore`). The gate_key is stable per cycle
    (``discovery-<cycle>``) and uses a separate namespace from in-loop irreversible
    action gates (``gate-<iteration>``). During construction,
    ``load_or_init(run_id)`` ensures the run row exists (for the FK).

    Args:
        store: :class:`~loop_agent.store.LoopStore` that persists proposals and decisions.
        run_id: ID of the target run.
    """

    def __init__(self, store: LoopStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        # Ensure the run row exists (idempotently satisfies the request_decision FK
        # and begin event).
        self.store.load_or_init(run_id)

    def gate_key(self, cycle: int) -> str:
        """Stable gate_key for the cycle (``discovery-<cycle>``)."""
        return f"{GATE_KEY_PREFIX}{cycle}"

    def propose(
        self,
        candidates: Iterable[Candidate],
        *,
        done: Iterable[str] = (),
        cycle: int = 0,
    ) -> Proposal:
        """Triage candidates and register the proposal as ``pending`` on the human gate (propose-only).

        The compute layer (:func:`triage`) calculates "N items + 1 recommendation",
        encodes that proposal with :func:`_triage_to_action`, and registers it with
        ``request_decision``. **It stops here**: nothing is adopted, and the proposal
        remains pending until the human decides through :meth:`resolve` (or directly
        through ``store.resolve_decision``).

        Idempotent for the same ``(run_id, cycle)``: because ``request_decision`` does
        not overwrite an existing row, re-proposing the same cycle does not corrupt
        the first proposal or decision (use another ``cycle`` to propose again with a
        new candidate set). Since triage itself is deterministic, the returned
        :class:`Triage` is the same every time.
        """
        triage_result = triage(candidates, done=done)
        gk = self.gate_key(cycle)
        action = _triage_to_action(triage_result, cycle)
        pending = self.store.request_decision(self.run_id, gk, action)
        # Restore the returned triage from the **persisted proposal** (pending["action"]).
        # Since request_decision does not overwrite an existing row when the same cycle
        # is re-proposed with a different candidate set, align the return value with the
        # authoritative source so it cannot contradict the actual adoption target
        # (pending / adopted).
        return Proposal(
            triage=_action_to_triage(pending["action"]),
            cycle=cycle,
            gate_key=gk,
            pending=pending,
        )

    def resolve(
        self, cycle: int, decision: str, payload: Any = None
    ) -> AdoptionResult:
        """Typed wrapper that records the human adoption decision (= human-gate resolution).

        This is a thin delegation to ``store.resolve_decision``, but for ``edit`` it
        validates *before persistence* that the payload (the candidate id to adopt) is
        **a ready candidate in that proposal** and fails loudly otherwise (to avoid
        accidentally adopting a blocked / unknown candidate = preserving the dependency
        resolution invariant in the delivery layer too). Returns the recorded
        :class:`AdoptionResult` (= the same mapping as :meth:`adopted`).
        """
        if decision == "edit":
            self._require_ready_selection(cycle, payload)
        self.store.resolve_decision(self.run_id, self.gate_key(cycle), decision, payload)
        return self.adopted(cycle)

    def _load_proposal_action(self, cycle: int) -> Optional[dict[str, Any]]:
        """Return the registered proposal action for the cycle (or ``None`` if absent)."""
        row = self.store.get_decision(self.run_id, self.gate_key(cycle))
        return row["action"] if row is not None else None

    def _require_ready_selection(self, cycle: int, selected_id: Any) -> None:
        """Validate that the ``edit`` selection id is a ready candidate in the proposal (else ConfigError)."""
        action = self._load_proposal_action(cycle)
        if action is None:
            raise ConfigError(
                f"no proposal for cycle {cycle} (run {self.run_id!r}); propose first"
            )
        ready_ids = {c["id"] for c in action["ready"]}
        if selected_id not in ready_ids:
            raise ConfigError(
                f"edit selection {selected_id!r} is not a ready candidate of "
                f"cycle {cycle}; ready={sorted(ready_ids)}"
            )

    def adopted(self, cycle: int = 0) -> AdoptionResult:
        """Read the human decision for the cycle and map it to the adopted candidate (stable across resume).

        Restores from the persisted ``pending_decision`` row and proposal action, so
        calls from another process / after resume produce the same adoption result
        (pure read, idempotent). Decision mapping:

        - ``approve`` -> adopt the recommended candidate
        - ``edit``    -> adopt the ready candidate identified by payload (ConfigError if not ready)
        - ``reject``  -> adopt nothing (``candidate=None``)
        - ``respond`` -> adopt nothing; place the response body in ``response``
        - undecided (pending) / no proposal (absent) -> adopt nothing
        """
        gk = self.gate_key(cycle)
        row = self.store.get_decision(self.run_id, gk)
        if row is None:
            return AdoptionResult(
                status="absent", decision=None, candidate=None, recommended=None
            )
        action = row["action"]
        triage_result = _action_to_triage(action)
        ready_by_id = {c.id: c for c in triage_result.ready}
        recommended = triage_result.recommended

        if row["status"] == "pending":
            return AdoptionResult(
                status="pending",
                decision=None,
                candidate=None,
                recommended=recommended,
            )

        decision = row["decision"]
        payload = row["payload"]
        candidate: Optional[Candidate] = None
        response: Any = None
        if decision == "approve":
            candidate = recommended
        elif decision == "edit":
            if payload not in ready_by_id:
                raise ConfigError(
                    f"edit selection {payload!r} is not a ready candidate of "
                    f"cycle {cycle}; ready={sorted(ready_by_id)}"
                )
            candidate = ready_by_id[payload]
        elif decision == "respond":
            response = payload
        # reject -> candidate remains None (nothing adopted).
        return AdoptionResult(
            status="resolved",
            decision=decision,
            candidate=candidate,
            recommended=recommended,
            response=response,
        )


def discover_next(
    *,
    store: LoopStore,
    run_id: str,
    candidates: Iterable[Candidate],
    result: Optional[Any] = None,
    done: Iterable[str] = (),
    cycle: int = 0,
) -> Optional[Proposal]:
    """Completed -> next-iteration connection point: propose next candidates if the previous loop completed (propose-only).

    Entry point that embodies report.md S5 Phase 3 success criterion d, "Completed
    -> next-iteration connection runs through the human gate." When ``result`` (the
    previous :class:`~loop_agent.loop.LoopResult`) is passed and it is **``paused``**,
    no proposal is created (``None`` is returned), because nothing has completed yet
    and the human should resolve that gate first. :meth:`WorkDiscovery.propose` is
    called only after completion (goal_met / stopped) or when ``result=None``.

    **No fully automatic start**: this function only registers a proposal (pending)
    and does not adopt anything or start the next loop. The human decides adoption
    with :meth:`WorkDiscovery.resolve`, and the caller uses the adopted candidate
    (:meth:`WorkDiscovery.adopted`) as the next loop input.
    """
    if result is not None and getattr(result, "paused", False):
        return None
    return WorkDiscovery(store, run_id).propose(candidates, done=done, cycle=cycle)


__all__ = [
    "Candidate",
    "BlockedCandidate",
    "Triage",
    "triage",
    "Proposal",
    "AdoptionResult",
    "WorkDiscovery",
    "discover_next",
    "GATE_KEY_PREFIX",
]
