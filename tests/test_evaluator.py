"""Unit tests for the RQGM epoch evaluator (Issue #22/#4 safety core: diverse scoring / held-out promotion gate)."""

from __future__ import annotations

import pytest

from loop_agent.evaluator import (
    Evaluator,
    HeldOut,
    Probe,
    Score,
    admit_evaluator,
    agreement,
)


def _ev(key: str, name: str, offset: float = 0.0, fixed: float | None = None) -> Evaluator:
    """Evaluator that reads probe.outcome[key] (offset adds error, fixed returns a constant)."""

    def score(o):
        if fixed is not None:
            return Score(ground_truth=fixed)
        return Score(ground_truth=o[key] + offset)

    return Evaluator(score=score, name=name)


# -- Score: diverse scoring (minimum of declared axes; missing axes are 0.0) ---


def test_aggregate_is_min_over_declared_keys():
    s = Score(ground_truth=0.9, components={"a": 0.9, "b": 0.9})
    assert s.aggregate(("a", "b")) == pytest.approx(0.9)


def test_aggregate_penalizes_missing_declared_axis():
    """Reporting only one axis and omitting other declared axes cannot raise the aggregate (no single-scalar gaming)."""
    s = Score(ground_truth=1.0, components={"a": 1.0})  # Declared axis 'b' is missing.
    assert s.aggregate(("a", "b")) == pytest.approx(0.0)


def test_aggregate_floored_by_ground_truth():
    s = Score(ground_truth=0.1, components={"a": 1.0, "b": 1.0})
    assert s.aggregate(("a", "b")) == pytest.approx(0.1)


def test_judge_excluded_from_aggregate():
    s = Score(ground_truth=0.8, components={"a": 0.8}, judge=0.0)
    assert s.aggregate(("a",)) == pytest.approx(0.8)


# -- agreement: calibration against fixed gold --------------------------------


def _held(*specs) -> HeldOut:
    """Convert specs: (case_id, gold, {key: predicted_value}, fold, critical) into Probe objects."""
    probes = []
    for case_id, gold, values, fold, critical in specs:
        probes.append(
            Probe(case_id=case_id, outcome=values, gold_label=gold, fold=fold, critical=critical)
        )
    return HeldOut(tuple(probes))


def test_agreement_perfect_when_predictions_match_gold():
    held = _held(
        ("c1", 0.2, {"inc": 0.2}, 0, False),
        ("c2", 0.8, {"inc": 0.8}, 0, False),
    )
    honest = _ev("inc", "honest")
    assert agreement(honest, held) == pytest.approx(0.0)


def test_rate_everything_high_has_low_agreement():
    """A lenient evaluator that returns 1.0 for everything has low agreement with varied gold (prevents meta-hacking)."""
    held = _held(
        ("c1", 0.0, {"inc": 0.0}, 0, False),
        ("c2", 0.2, {"inc": 0.2}, 0, False),
    )
    honest = _ev("inc", "honest")
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")
    assert agreement(lenient, held) < agreement(honest, held)


# -- admit_evaluator: epsilon-best-belief + dominance --------------------------


def test_epsilon_must_be_positive():
    held = _held(("c1", 0.5, {"inc": 0.5, "cand": 0.5}, 0, False))
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    with pytest.raises(ValueError):
        admit_evaluator(inc, cand, held, epsilon=0.0)


def test_strictly_better_candidate_promoted():
    held = _held(
        ("c1", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("c2", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02)
    assert res.chosen is cand
    assert res.promoted is True


@pytest.mark.parametrize("cand_offset", [0.30, 0.31, 0.295])
def test_worse_equal_or_within_epsilon_keeps_incumbent(cand_offset):
    """Keep the incumbent when the candidate is worse, equal, or improves by less than epsilon (status-quo bias)."""
    held = _held(
        ("c1", 1.0, {"inc": 0.7, "cand": 1.0 - cand_offset}, 0, False),
        ("c2", 0.0, {"inc": 0.3, "cand": 0.0 + cand_offset}, 0, False),
    )
    inc = _ev("inc", "inc")  # Constant 0.3 error.
    cand = Evaluator(score=lambda o: Score(ground_truth=o["cand"]), name="cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02)
    assert res.chosen is inc


def test_lenient_candidate_rejected_against_gold():
    """A lenient candidate that scores itself highly has low agreement with gold and cannot be promoted."""
    held = _held(
        ("c1", 0.0, {"inc": 0.0}, 0, False),
        ("c2", 0.3, {"inc": 0.3}, 0, False),
    )
    inc = _ev("inc", "inc")  # Perfect match.
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")
    res = admit_evaluator(inc, lenient, held, epsilon=0.02)
    assert res.chosen is inc


def test_candidate_regressing_critical_probe_rejected():
    """Reject a candidate that regresses on a critical probe even if its aggregate improves (risky-case dominance)."""
    held = _held(
        ("benign1", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("benign2", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("critical", 1.0, {"inc": 1.0, "cand": 0.7}, 0, True),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02)
    assert res.chosen is inc  # Rejected for critical regression despite aggregate improvement (passes 1).


def test_candidate_regressing_one_fold_rejected():
    """Reject a candidate that regresses on a specific fold even if its aggregate improves."""
    held = _held(
        ("f0a", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f0b", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f1", 1.0, {"inc": 1.0, "cand": 0.5}, 1, False),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02, delta=0.0)
    assert res.chosen is inc


# -- HeldOut: fold rotation ---------------------------------------------------


def test_heldout_fold_rotation_partitions():
    held = _held(
        ("a", 0.1, {}, 0, False),
        ("b", 0.2, {}, 1, False),
        ("c", 0.3, {}, 2, False),
    )
    assert held.folds == (0, 1, 2)
    assert [p.case_id for p in held.fold(0).probes] == ["a"]
    assert [p.case_id for p in held.fold(1).probes] == ["b"]
    assert [p.case_id for p in held.fold(3).probes] == ["a"]  # Rotates because 3 % 3 == 0.


def test_heldout_single_fold_returns_self():
    held = _held(("a", 0.1, {}, 0, False), ("b", 0.2, {}, 0, False))
    assert held.fold(5).probes == held.probes


def test_heldout_requires_probes_and_unique_ids():
    with pytest.raises(ValueError):
        HeldOut(())
    with pytest.raises(ValueError):
        HeldOut((Probe("dup", {}, 0.1), Probe("dup", {}, 0.2)))


# -- Regression checks inspect all held-out data, not measure_fold (P1 fix) ---


def test_promotion_checks_all_folds_not_just_measured_fold():
    """Reject critical regressions on other folds even when the aggregate is measured on a rotating fold (protect unselected folds too)."""
    held = _held(
        ("f0", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),     # Measured fold: candidate improves.
        ("f1crit", 1.0, {"inc": 1.0, "cand": 0.0}, 1, True),  # Other fold: critical regression.
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    # The aggregate gate only measures fold 0 (anti-overfit), but regression checks inspect all held-out data.
    res = admit_evaluator(inc, cand, held, epsilon=0.02, measure_fold=held.fold(0))
    assert res.chosen is inc  # Rejected for critical regression on another fold.


def test_promotion_succeeds_when_better_on_all_folds():
    held = _held(
        ("f0", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f1", 1.0, {"inc": 0.0, "cand": 1.0}, 1, True),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02, measure_fold=held.fold(0))
    assert res.chosen is cand


# -- Evaluator version identity (P2 fix) --------------------------------------


def test_lambda_evaluators_distinguished_by_source_location():
    """Same-named lambdas at different source locations get different versions (preserves audit evidence)."""
    e1 = Evaluator(score=lambda o: Score(ground_truth=0.0), name="dup")
    e2 = Evaluator(score=lambda o: Score(ground_truth=1.0), name="dup")
    assert e1.version != e2.version


def test_version_detects_in_place_body_change():
    """A scorer whose body (constant) changes without moving its definition gets a different version."""
    src_a = "def s(o):\n    return __import__('loop_agent').Score(ground_truth=0.5)\n"
    src_b = "def s(o):\n    return __import__('loop_agent').Score(ground_truth=1.0)\n"
    ns_a: dict = {}
    ns_b: dict = {}
    # Create functions with the same filename/firstlineno and different bodies (simulates an in-place rewrite).
    exec(compile(src_a, "scorer.py", "exec"), ns_a)
    exec(compile(src_b, "scorer.py", "exec"), ns_b)
    e_a = Evaluator(score=ns_a["s"], name="s")
    e_b = Evaluator(score=ns_b["s"], name="s")
    assert e_a.version != e_b.version


def test_version_detects_factory_default_argument_change():
    """A factory scorer whose behavior changes through default arguments gets a different version."""

    def make(bias):
        def score(o, bias=bias):
            return Score(ground_truth=bias)

        return score

    e1 = Evaluator(score=make(0.1), name="f")
    e2 = Evaluator(score=make(0.9), name="f")
    assert e1.version != e2.version
    # The same default argument matches reproducibly.
    assert Evaluator(score=make(0.1), name="f").version == e1.version


def test_version_reproducible_for_identical_source():
    src = "def s(o):\n    return __import__('loop_agent').Score(ground_truth=0.5)\n"
    ns1: dict = {}
    ns2: dict = {}
    exec(compile(src, "scorer.py", "exec"), ns1)
    exec(compile(src, "scorer.py", "exec"), ns2)
    assert Evaluator(score=ns1["s"], name="s").version == Evaluator(score=ns2["s"], name="s").version


def test_explicit_version_is_preserved():
    e = Evaluator(score=lambda o: Score(ground_truth=0.0), name="x", version="v-pinned")
    assert e.version == "v-pinned"


def test_promoted_flag_false_on_reject_even_with_shared_version():
    """promoted is False on rejection (does not depend on version comparison even when candidate and incumbent share a version)."""
    held = _held(
        ("c1", 0.0, {"inc": 0.0}, 0, False),
        ("c2", 0.3, {"inc": 0.3}, 0, False),
    )
    inc = Evaluator(score=lambda o: Score(ground_truth=o["inc"]), name="x", version="shared")
    # Give a lenient candidate (which should be rejected) the same explicit version as the incumbent.
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="x", version="shared")
    res = admit_evaluator(inc, lenient, held, epsilon=0.02)
    assert res.chosen is inc
    assert res.promoted is False  # Do not incorrectly set promoted=True even when versions match.
