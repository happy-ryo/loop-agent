"""RQGM epoch evaluator: safety core for fixed criteria + held-out ground-truth gates (Issue #22/#4).

Core logic for protecting the outer Reflexion loop's **evaluator** from reward hacking
through self-optimization (report.md S6 + RQGM arXiv:2606.26294 / Issue #4 comment).
This module is a leaf module and has no LLM dependency; scoring is supplied through the
injected pure function :data:`ScoreFn`.

Safety design essentials and invariants enforced here:

- **Diverse evaluation**: :class:`Score` is not a single scalar. It carries
  ``ground_truth`` (primary) plus multiple declared ``components`` axes, and
  :meth:`Score.aggregate` takes the **minimum value across declared keys**. Raising only
  one axis cannot raise the aggregate, which blocks single-scalar gaming. Missing
  declared keys collapse to 0.0.
- **epsilon-best-belief promotion gate**: :func:`admit_evaluator` compares a candidate
  evaluator against the incumbent by its agreement (:func:`agreement`) with **fixed
  held-out gold labels**. It admits the candidate only when
  ``agreement(candidate) > agreement(incumbent) + epsilon`` is satisfied **strictly** and
  no fold, nor any critical probe, regresses. Otherwise the incumbent remains in place
  (status-quo bias). ``epsilon <= 0`` is rejected because it removes the anti-churn
  safety margin.
- **Gold labels are not produced by evaluators**: :class:`Probe` ``gold_label`` values
  are external ground truth independent of the evaluator. A lax candidate that "returns
  1.0 for everything" has **low** agreement against varied gold labels and cannot be
  promoted, blocking meta-reward hacking.
- **Dual-component separation**: the measurement paths in this module
  (:func:`agreement` / :func:`admit_evaluator`) only score **pre-recorded**
  :class:`Probe` instances and never touch production act/gate/memory, separating
  performance measurement from production execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from .errors import ConfigError


@dataclass(frozen=True)
class Score:
    """Multi-axis score for one trial. ``ground_truth`` is primary; ``judge`` is advisory.

    - ``ground_truth`` : primary signal from test/lint/exit-code results
      (report.md principle: ground truth first).
    - ``components``   : declared axes for diverse evaluation
      (for example: 'correctness' / 'safety' / 'completeness').
    - ``judge``       : advisory LLM-as-judge value. **Excluded from aggregate** so a
      bias source is not placed on the control path.
    - ``detail``      : explanation for logs.
    """

    ground_truth: float
    components: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    judge: Optional[float] = None
    detail: str = ""

    def aggregate(self, declared_keys: tuple[str, ...]) -> float:
        """Use the **minimum** across all declared axes as the aggregate.

        This is diverse evaluation with missing axes treated as 0.0. Taking the minimum
        across ``ground_truth`` and every ``declared_keys`` axis prevents single-scalar
        gaming, where only one axis is raised, from raising the aggregate. Declared keys
        absent from ``components`` are treated as 0.0, closing the loophole of omitting
        reported axes to cross a threshold. ``judge`` is intentionally excluded.
        """
        values = [self.ground_truth]
        for key in declared_keys:
            values.append(float(self.components.get(key, 0.0)))
        return min(values)


@dataclass(frozen=True)
class GroundTruthSignal:
    """Primary episode signal: authoritative success/failure from inner verification.

    This comes from inner verification such as tests, lint, and exit codes.

    - ``succeeded``           : success/failure from the inner
      :class:`~loop_agent.loop.LoopResult`.
    - ``score``               : :class:`Score` whose ``ground_truth`` axis was filled
      from verification.
    - ``ground_truth_backed`` : whether a real test/lint signal existed. Episodes with
      ``False`` are excluded from convergence decisions
      (:class:`~loop_agent.convergence.RubricThreshold`) to prevent a lax evaluator from
      fabricating a primary signal and declaring convergence.
    """

    succeeded: bool
    score: Score
    ground_truth_backed: bool = True


# Injected scoring function. outcome (EpisodeOutcome view; ``.history``, etc.) -> Score.
# Expected to be a pure function.
ScoreFn = Callable[[Any], Score]
# Primary signal source. outcome -> GroundTruthSignal. It must come from **inner
# verification, not from the evaluator**.
GroundTruthFn = Callable[[Any], GroundTruthSignal]


def _scorer_identity(score: ScoreFn) -> str:
    """**Reproducible** identity key for a scoring function, used for audit/versioning.

    ``__qualname__`` alone would make lambdas at different source locations collide under
    the same name (``<lambda>``), causing evaluators with different behavior to share a
    version and breaking the epoch-freeze audit trail. When ``__code__`` is available, we
    include the **definition source location** (filename:firstlineno) and add a hash of
    the **implementation itself**: bytecode ``co_code``, constants ``co_consts``,
    referenced names ``co_names``, and **default arguments** ``__defaults__`` /
    ``__kwdefaults__``. This gives different versions to evaluators whose bodies are
    changed in place without moving their definition, such as changing ``0.5`` to
    ``1.0``, and to factories that change behavior through default arguments
    (``def score(o, bias=bias): ...``). Resume/audit therefore will not silently accept a
    behavior change. The same source and same defaults produce the same version,
    reproducibly across processes.

    **Boundary**: closures that capture free variables in ``__closure__`` cells can still
    collide when they share the same code object, have no defaults, and differ only in
    cell values. Cell contents can be arbitrary objects without stable hashes, so this
    function intentionally does not inspect them. Such parameterized scorers should pass
    an explicit ``version`` when their behavior differs; see the docstring.
    """
    qual = getattr(score, "__qualname__", repr(score))
    code = getattr(score, "__code__", None)
    if code is not None:
        import hashlib

        def _b(value: Any) -> bytes:
            return repr(value).encode("utf-8", "surrogatepass")

        body = hashlib.sha256(
            code.co_code
            + _b(code.co_consts)
            + _b(code.co_names)
            + _b(getattr(score, "__defaults__", None))
            + _b(getattr(score, "__kwdefaults__", None))
        ).hexdigest()[:16]
        return f"{qual}@{code.co_filename}:{code.co_firstlineno}#{body}"
    return qual


def _content_version(score: ScoreFn, rubric: tuple[str, ...], name: str) -> str:
    """Fixed-criteria key for the evaluator (content hash).

    The same signature/source location produces the same version.
    """
    import hashlib

    payload = f"{name}|{_scorer_identity(score)}|{'/'.join(rubric)}"
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class Evaluator:
    """Rubric evaluator that is **fixed** within an epoch.

    ``score`` is an injected pure function. ``version`` is the fixed-criteria identity key
    (content hash). If omitted, it is derived from ``score`` identity (qualname +
    definition source location), ``rubric``, and ``name`` (:func:`_scorer_identity`).
    **Closures sharing the same code object** can collide when they have the same source
    location and differ only in captured variables, so pass an explicit ``version`` when
    behavior differs to preserve audit-trail fidelity. An evaluator is replaced by a
    different version only during cross-epoch promotion (:func:`admit_evaluator`).
    ``score`` output, the reward signal, is used only as verbal reinforcement for
    Reflexion and is not placed on convergence/admission decisions; that is the role of
    :class:`GroundTruthSignal`.
    """

    score: ScoreFn
    rubric: tuple[str, ...] = ()
    name: str = "evaluator"
    version: str = ""

    def __post_init__(self) -> None:
        if not self.version:
            object.__setattr__(
                self, "version", _content_version(self.score, self.rubric, self.name)
            )


@dataclass(frozen=True)
class Probe:
    """Pre-recorded case for held-out measurement.

    ``gold_label`` is external ground truth independent of the evaluator.

    - ``case_id``    : unique ID. The driver verifies that its namespace is **disjoint**
      from production tasks.
    - ``outcome``    : pre-recorded outcome scored by the evaluator; no live act is
      performed, preserving dual-component separation.
    - ``gold_label`` : fixed correct value. It is **not** generated by the evaluator.
    - ``fold``       : held-out partition. Rotated at each boundary to reduce adaptive
      overfit.
    - ``critical``   : regressions here are never allowed; this is the dominance guard
      for dangerous cases.
    """

    case_id: str
    outcome: Any
    gold_label: float
    fold: int = 0
    critical: bool = False


@dataclass(frozen=True)
class HeldOut:
    """Collection of probes with fixed gold labels for evaluator-promotion measurement."""

    probes: tuple[Probe, ...]

    def __post_init__(self) -> None:
        if not self.probes:
            raise ConfigError("HeldOut requires at least one probe")
        ids = [p.case_id for p in self.probes]
        if len(set(ids)) != len(ids):
            raise ConfigError("HeldOut probe case_id values must be unique")

    @property
    def folds(self) -> tuple[int, ...]:
        return tuple(sorted({p.fold for p in self.probes}))

    def fold(self, k: int) -> "HeldOut":
        """Return the fold subset selected by rotation for boundary ``k``.

        This is an anti-adaptive-overfit measure. If only one fold exists, the whole set
        is returned. With multiple folds, ``k`` selects cyclically so the same fixed probe
        set cannot be hill-climbed at every boundary.
        """
        folds = self.folds
        if len(folds) <= 1:
            return self
        chosen = folds[k % len(folds)]
        return HeldOut(tuple(p for p in self.probes if p.fold == chosen))


def agreement(evaluator: Evaluator, held_out: HeldOut) -> float:
    """**Agreement** between evaluator ``ground_truth`` output and fixed gold labels.

    This is calibration, where higher is better. For each probe, it computes the squared
    error between ``evaluator.score(probe.outcome).ground_truth`` and
    ``probe.gold_label``, then returns the **negative mean squared error**. A value of 0
    is a perfect match, and larger negative values indicate greater divergence. Because
    gold labels are independent of the evaluator, a lax evaluator that returns high values
    for everything has large error against varied gold labels, low agreement, and cannot
    be promoted. This is the core meta-reward-hacking guard.
    """
    probes = held_out.probes
    total = 0.0
    for p in probes:
        predicted = evaluator.score(p.outcome).ground_truth
        diff = predicted - p.gold_label
        total += diff * diff
    return -total / len(probes)


@dataclass(frozen=True)
class AdmissionResult:
    """Result of :func:`admit_evaluator`: chosen evaluator and both agreement scores.

    ``promoted`` is an explicit flag for whether the **candidate was actually admitted**.
    It stores the admission decision itself, not a version comparison, so it correctly
    returns ``False`` for a rejected candidate even when the candidate and incumbent have
    the same version due to an explicit version or collision.
    """

    chosen: Evaluator
    incumbent_agreement: float
    candidate_agreement: float
    promoted: bool = False


def _probe_squared_error(evaluator: Evaluator, probe: Probe) -> float:
    diff = evaluator.score(probe.outcome).ground_truth - probe.gold_label
    return diff * diff


def admit_evaluator(
    incumbent: Evaluator,
    candidate: Evaluator,
    held_out: HeldOut,
    *,
    epsilon: float,
    delta: float = 0.0,
    measure_fold: Optional[HeldOut] = None,
) -> AdmissionResult:
    """Decide evaluator promotion with epsilon-best-belief + dominance.

    This is the RQGM safety gate. The candidate is admitted only when all conditions are
    met; otherwise the incumbent remains in place:

    1. **Strict improvement in aggregate agreement**:
       ``agreement(candidate) > agreement(incumbent) + epsilon``. ``epsilon`` is the
       anti-churn safety margin and must be **positive**; ``<= 0`` is rejected. Only this
       aggregate gate may be measured on ``measure_fold``, the held-out subset selected
       by rotation. This avoids adaptive overfit by preventing hill-climbing against the
       same fixed set at every boundary. When ``None``, the whole ``held_out`` set is
       measured.
    2. **No fold-level regression**: across **all folds in ``held_out``**, the candidate's
       agreement must not fall below the incumbent by more than ``delta``. This blocks
       gaming that improves aggregate score while sacrificing a specific fold. It checks
       **all folds**, not only the rotated fold, so regressions in unselected folds are
       also blocked.
    3. **No critical-probe regression**: across all ``critical=True`` probes in
       ``held_out``, the candidate's squared error must not exceed the incumbent's. This
       blocks promotions that sacrifice dangerous cases. Critical probes in folds not
       selected by rotation are always checked.

    Gold labels are independent of the evaluator, so a candidate does not improve
    agreement by scoring itself highly.

    Key safety point: aggregate gate (1) may be measured on a subset for anti-overfit
    purposes, but regression checks (2)(3) **always inspect the full held-out set**. This
    structurally blocks promotions that improve only the selected fold while sacrificing
    critical probes in another fold.
    """
    if epsilon <= 0:
        raise ConfigError("admit_evaluator epsilon must be > 0 (anti-churn margin)")

    # Measurement target for the aggregate gate: rotated fold if supplied, else all.
    # Regression checks always inspect the full set.
    measure = measure_fold if measure_fold is not None else held_out
    inc_agree = agreement(incumbent, measure)
    cand_agree = agreement(candidate, measure)

    def keep() -> AdmissionResult:
        return AdmissionResult(
            chosen=incumbent,
            incumbent_agreement=inc_agree,
            candidate_agreement=cand_agree,
            promoted=False,
        )

    # (1) Strict aggregate-agreement improvement, evaluated on measure for anti-overfit.
    if not (cand_agree > inc_agree + epsilon):
        return keep()

    # (2) Fold-level regression check across all held-out folds.
    for f in held_out.folds:
        sub = HeldOut(tuple(p for p in held_out.probes if p.fold == f))
        if agreement(candidate, sub) < agreement(incumbent, sub) - delta:
            return keep()

    # (3) Critical-probe regression check across all held-out probes.
    # Squared error must not increase.
    for p in held_out.probes:
        if p.critical and _probe_squared_error(candidate, p) > _probe_squared_error(
            incumbent, p
        ):
            return keep()

    return AdmissionResult(
        chosen=candidate,
        incumbent_agreement=inc_agree,
        candidate_agreement=cand_agree,
        promoted=True,
    )


__all__ = [
    "Score",
    "GroundTruthSignal",
    "ScoreFn",
    "GroundTruthFn",
    "Evaluator",
    "Probe",
    "HeldOut",
    "agreement",
    "admit_evaluator",
    "AdmissionResult",
]
