"""RQGM epoch-evaluator: safety core for fixed-standard + held-out ground truth gate (Issue #22/#4).

Core defense mechanism for the **evaluator** of the outer Reflexion loop from reward hacking via self-optimize
(report.md S6 + RQGM arXiv:2606.26294 / Issue #4 comments). This module is a leaf module with
no LLM dependency (evaluation is injected pure function :data:`ScoreFn`).

Key points of safety design — invariants guaranteed here:

- **Diverse evaluation**: :class:`Score` is not a single scalar but has multiple dimensions:
  ``ground_truth`` (primary) + declared ``components``, and :meth:`Score.aggregate` takes the
  **minimum of declared keys**. Even if only one axis is high, aggregate cannot increase
  (rejects single-scalar gaming). Missing declared keys are treated as 0.0.
- **epsilon-best-belief promotion gate**: :func:`admit_evaluator` compares the candidate evaluator
  against incumbent using **agreement** (:func:`agreement`) against fixed held-out gold labels,
  and adopts it only when ``agreement(candidate) > agreement(incumbent) + epsilon`` is **strictly** satisfied and
  there is no regression on any fold (or critical probe). Otherwise, keep incumbent (status-quo bias).
  ``epsilon <= 0`` is rejected as it loses safety margin against churn.
- **gold labels are not created by the evaluator**: The ``gold_label`` of :class:`Probe` is independent
  external ground truth. A permissive candidate that "returns all 1.0" has **low** agreement
  against varied gold and cannot be promoted (rejects meta-reward-hacking).
- **dual-component separation**: The measurement path of this module (:func:`agreement` / :func:`admit_evaluator`)
  scores only **pre-recorded** :class:`Probe` without touching production act/gate/memory
  (separation of performance measurement and production execution).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional


@dataclass(frozen=True)
class Score:
    """Multi-axis score for a single trial. ``ground_truth`` is primary, ``judge`` is advisory only.

    - ``ground_truth`` : primary signal from test/lint/exit-code (report.md principle: ground-truth first).
    - ``components``   : declared dimensions for diverse evaluation (e.g., 'correctness' / 'safety' / 'completeness').
    - ``judge``       : advisory value from LLM-as-judge. **not included in aggregate** (control bias sources).
    - ``detail``      : explanation for logging.
    """

    ground_truth: float
    components: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    judge: Optional[float] = None
    detail: str = ""

    def aggregate(self, declared_keys: tuple[str, ...]) -> float:
        """Aggregate as the **minimum** of all declared dimensions (diverse evaluation; missing axes are 0.0).

        Takes the min of ``ground_truth`` and each axis in ``declared_keys``, so single-axis high "single-scalar
        gaming" cannot push up the aggregate. If a declared key is missing from ``components``, it is treated as 0.0,
        closing the loophole of omitting reporting dimensions to exceed threshold. ``judge`` is intentionally excluded.
        """
        values = [self.ground_truth]
        for key in declared_keys:
            values.append(float(self.components.get(key, 0.0)))
        return min(values)


@dataclass(frozen=True)
class GroundTruthSignal:
    """Primary signal for an episode. Authoritative success/failure from inner verify (test/lint/exit-code).

    - ``succeeded``           : success/failure of inner :class:`~loop_agent.loop.LoopResult`.
    - ``score``               : :class:`Score` with ``ground_truth`` axis filled from verify.
    - ``ground_truth_backed`` : whether real signal from test/lint etc. exists. Episodes with ``False``
      are not included in convergence judgment (:class:`~loop_agent.convergence.RubricThreshold`)
      (prevents permissive evaluators from fabricating primary signal to declare convergence).
    """

    succeeded: bool
    score: Score
    ground_truth_backed: bool = True


# Injected scoring function. outcome (EpisodeOutcome view; ``.history`` etc.) -> Score. Assumed to be pure.
ScoreFn = Callable[[Any], Score]
# Primary signal source. outcome -> GroundTruthSignal. **From inner verify, not from evaluator**.
GroundTruthFn = Callable[[Any], GroundTruthSignal]


def _scorer_identity(score: ScoreFn) -> str:
    """**Reproducible** identity key for scoring function (for audit/version).

    Using ``__qualname__`` alone causes collision when lambdas at different source locations have the same name
    (``<lambda>``), causing evaluators with different behavior to have the same version and breaking epoch-freeze
    audit trail. With ``__code__``, we add **definition source location** (filename:firstlineno) plus **implementation itself** —
    bytecode ``co_code`` + constants ``co_consts`` + referenced names ``co_names`` + **default arguments** ``__defaults__`` /
    ``__kwdefaults__`` — to the hash. This way, an evaluator whose implementation is modified without moving the
    definition location (e.g., ``0.5`` to ``1.0``) or a factory that changes behavior via default arguments
    (``def score(o, bias=bias): ...``) also gets a different version, so resume/audit does not silently
    accept behavior changes. Same source and same default arguments give the same version (reproducible across processes).

    **Boundary**: closure capturing free variables in ``__closure__`` cell (same code object,
    no default arguments, only cell values differ) can still collide. Cell contents are arbitrary objects without stable
    hash, so this function intentionally does not go that far. For such parameterized scorers,
    pass explicit ``version`` if behavior differs (see docstring).
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
    """Fixed-standard key for evaluator (content-hash). Same signature/source location gives same version."""
    import hashlib

    payload = f"{name}|{_scorer_identity(score)}|{'/'.join(rubric)}"
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class Evaluator:
    """Rubric evaluator **fixed** within epoch. ``score`` is injected pure function.

    ``version`` is identity key for fixed standard (content-hash). If not explicitly provided, derived from ``score`` identity
    (qualname + definition source location) / ``rubric`` / ``name`` (:func:`_scorer_identity`).
    **Closures sharing same code object** (same source location, only capture variables differ) can have
    version collision, so pass explicit ``version`` if behavior differs (for audit trail fidelity).
    Only replaced with different-version evaluator during promotion across epochs (:func:`admit_evaluator`). The output of ``score`` (reward signal) is used as verbal reinforcement
    for Reflexion only, not used in convergence/adoption judgment (that is the role of :class:`GroundTruthSignal`).
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
    """Pre-recorded case for held-out measurement. ``gold_label`` is external ground truth independent from evaluator.

    - ``case_id``    : unique ID (driver verifies it is in **clean** namespace distinct from production task).
    - ``outcome``    : pre-recorded outcome scored by evaluator (no live act = dual-component separation).
    - ``gold_label`` : fixed ground truth. **Not** generated by evaluator.
    - ``fold``       : held-out partition. Rotated at each boundary to suppress adaptive overfit.
    - ``critical``   : no regression here is ever allowed (dominance guard for danger cases).
    """

    case_id: str
    outcome: Any
    gold_label: float
    fold: int = 0
    critical: bool = False


@dataclass(frozen=True)
class HeldOut:
    """Collection of fixed gold-labeled probes (measurement basis for evaluator promotion)."""

    probes: tuple[Probe, ...]

    def __post_init__(self) -> None:
        if not self.probes:
            raise ValueError("HeldOut requires at least one probe")
        ids = [p.case_id for p in self.probes]
        if len(set(ids)) != len(ids):
            raise ValueError("HeldOut probe case_id values must be unique")

    @property
    def folds(self) -> tuple[int, ...]:
        return tuple(sorted({p.fold for p in self.probes}))

    def fold(self, k: int) -> "HeldOut":
        """Return subset with fold rotation-selected for boundary ``k`` (anti adaptive-overfit).

        If only one fold, return entire set. If multiple, cycle-select by ``k`` to prevent the same
        fixed probe set from being hill-climbed at each boundary.
        """
        folds = self.folds
        if len(folds) <= 1:
            return self
        chosen = folds[k % len(folds)]
        return HeldOut(tuple(p for p in self.probes if p.fold == chosen))


def agreement(evaluator: Evaluator, held_out: HeldOut) -> float:
    """**Agreement** (calibration; higher is better) between evaluator's ``ground_truth`` output and fixed gold label.

    For each probe, compute squared error between ``evaluator.score(probe.outcome).ground_truth`` and ``probe.gold_label``,
    and return **negative mean squared error** (0 is perfect agreement, more negative is divergence). Since gold is
    independent from evaluator, a permissive evaluator that "returns all high" has large error against varied gold,
    giving low agreement (= cannot be promoted). This is the key to meta-reward-hacking guard.
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
    """Result of :func:`admit_evaluator`: chosen evaluator and agreement of both.

    ``promoted`` is explicit flag showing **whether candidate was actually adopted**. Rather than version comparison,
    it carries the adoption decision itself, so even if candidate and incumbent have the same version
    (via explicit version specification or collision), it correctly returns ``False`` if rejected.
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
    """Determine evaluator promotion with epsilon-best-belief + dominance (RQGM safety gate).

    Conditions for adopting candidate (only when all satisfied; otherwise keep incumbent):

    1. **Strict improvement in aggregate agreement**: ``agreement(candidate) > agreement(incumbent) + epsilon``.
       ``epsilon`` is safety margin against churn, must be **positive** (``<= 0`` rejected). This aggregate gate
       alone can be measured on ``measure_fold`` (= rotation-selected held-out subset) (anti adaptive-overfit:
       do not hill-climb same fixed set at each boundary). If ``None``, measure on entire ``held_out``.
    2. **No regression per fold**: agreement of candidate on **all folds of ``held_out``** does not drop by more than
       ``delta`` from incumbent (rejects gaming that improves aggregate at cost of specific fold). Rotation-selected
       folds and **all folds** are checked (rejects regression on unselected folds too).
    3. **No regression on critical probes**: on ``critical=True`` probes of entire ``held_out``, candidate's
       squared error does not exceed incumbent (rejects promotion at cost of danger cases). Critical probes in folds
       not selected by rotation are also checked.

    Since gold labels are independent from evaluator, candidate rating itself highly does not increase agreement.

    Safety note: aggregate gate (1) can be measured on subset for anti-overfit, but regression checks
    (2)(3) **always check entire held_out**. This structurally prevents "improve only selected fold,
    sacrifice critical probes in other fold" promotion.
    """
    if epsilon <= 0:
        raise ValueError("admit_evaluator epsilon must be > 0 (anti-churn margin)")

    # Measurement target for aggregate gate (rotation fold if exists, else entire set). Regression checks always on entire set.
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

    # (1) Strict improvement in aggregate agreement (evaluated on measure for anti-overfit).
    if not (cand_agree > inc_agree + epsilon):
        return keep()

    # (2) Regression check per fold (check all folds of held_out).
    for f in held_out.folds:
        sub = HeldOut(tuple(p for p in held_out.probes if p.fold == f))
        if agreement(candidate, sub) < agreement(incumbent, sub) - delta:
            return keep()

    # (3) Regression check on critical probes (entire held_out. squared error not increased).
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
