"""RQGM epoch-evaluator の単体テスト (Issue #22/#4 安全核: 多様評価 / held-out 昇格ゲート)。"""

from __future__ import annotations

import pytest

from claude_loop.evaluator import (
    Evaluator,
    HeldOut,
    Probe,
    Score,
    admit_evaluator,
    agreement,
)


def _ev(key: str, name: str, offset: float = 0.0, fixed: float | None = None) -> Evaluator:
    """probe.outcome[key] を読む評価器 (offset で誤差を、fixed で定数返しを作れる)。"""

    def score(o):
        if fixed is not None:
            return Score(ground_truth=fixed)
        return Score(ground_truth=o[key] + offset)

    return Evaluator(score=score, name=name)


# -- Score: 多様評価 (宣言軸の最小値・欠落は 0.0) -------------------------------


def test_aggregate_is_min_over_declared_keys():
    s = Score(ground_truth=0.9, components={"a": 0.9, "b": 0.9})
    assert s.aggregate(("a", "b")) == pytest.approx(0.9)


def test_aggregate_penalizes_missing_declared_axis():
    """1 軸だけ報告し他の宣言軸を間引いても集約は上がらない (single-scalar gaming 不可)。"""
    s = Score(ground_truth=1.0, components={"a": 1.0})  # 宣言軸 'b' が欠落
    assert s.aggregate(("a", "b")) == pytest.approx(0.0)


def test_aggregate_floored_by_ground_truth():
    s = Score(ground_truth=0.1, components={"a": 1.0, "b": 1.0})
    assert s.aggregate(("a", "b")) == pytest.approx(0.1)


def test_judge_excluded_from_aggregate():
    s = Score(ground_truth=0.8, components={"a": 0.8}, judge=0.0)
    assert s.aggregate(("a",)) == pytest.approx(0.8)


# -- agreement: 固定 gold への校正 ---------------------------------------------


def _held(*specs) -> HeldOut:
    """specs: (case_id, gold, {key: predicted_value}, fold, critical) を Probe に。"""
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
    """全部 1.0 と返す緩い評価器は varied な gold に対し一致度が低い (meta-hacking 防止)。"""
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
    """候補が悪い / 同等 / epsilon 未満の改善では incumbent 据え置き (status-quo bias)。"""
    held = _held(
        ("c1", 1.0, {"inc": 0.7, "cand": 1.0 - cand_offset}, 0, False),
        ("c2", 0.0, {"inc": 0.3, "cand": 0.0 + cand_offset}, 0, False),
    )
    inc = _ev("inc", "inc")  # 誤差 0.3 一定
    cand = Evaluator(score=lambda o: Score(ground_truth=o["cand"]), name="cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02)
    assert res.chosen is inc


def test_lenient_candidate_rejected_against_gold():
    """自分を高く採点する緩い候補は gold への一致度が低く昇格できない。"""
    held = _held(
        ("c1", 0.0, {"inc": 0.0}, 0, False),
        ("c2", 0.3, {"inc": 0.3}, 0, False),
    )
    inc = _ev("inc", "inc")  # 完全一致
    lenient = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")
    res = admit_evaluator(inc, lenient, held, epsilon=0.02)
    assert res.chosen is inc


def test_candidate_regressing_critical_probe_rejected():
    """集約は改善しても critical probe で後退する候補は弾く (危険ケース dominance)。"""
    held = _held(
        ("benign1", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("benign2", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("critical", 1.0, {"inc": 1.0, "cand": 0.7}, 0, True),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02)
    assert res.chosen is inc  # 集約改善 (passes 1) でも critical 後退で却下


def test_candidate_regressing_one_fold_rejected():
    """集約改善でも特定 fold で後退する候補は弾く。"""
    held = _held(
        ("f0a", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f0b", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f1", 1.0, {"inc": 1.0, "cand": 0.5}, 1, False),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02, delta=0.0)
    assert res.chosen is inc


# -- HeldOut: fold 回転 --------------------------------------------------------


def test_heldout_fold_rotation_partitions():
    held = _held(
        ("a", 0.1, {}, 0, False),
        ("b", 0.2, {}, 1, False),
        ("c", 0.3, {}, 2, False),
    )
    assert held.folds == (0, 1, 2)
    assert [p.case_id for p in held.fold(0).probes] == ["a"]
    assert [p.case_id for p in held.fold(1).probes] == ["b"]
    assert [p.case_id for p in held.fold(3).probes] == ["a"]  # 3 % 3 == 0 で回転


def test_heldout_single_fold_returns_self():
    held = _held(("a", 0.1, {}, 0, False), ("b", 0.2, {}, 0, False))
    assert held.fold(5).probes == held.probes


def test_heldout_requires_probes_and_unique_ids():
    with pytest.raises(ValueError):
        HeldOut(())
    with pytest.raises(ValueError):
        HeldOut((Probe("dup", {}, 0.1), Probe("dup", {}, 0.2)))


# -- 後退チェックは measure_fold ではなく held-out 全体を見る (P1 fix) -----------


def test_promotion_checks_all_folds_not_just_measured_fold():
    """集約は回転 fold で測っても、別 fold の critical 後退は弾く (選ばれない fold も守る)。"""
    held = _held(
        ("f0", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),     # 測定 fold: 候補が改善
        ("f1crit", 1.0, {"inc": 1.0, "cand": 0.0}, 1, True),  # 別 fold: critical で後退
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    # 集約ゲートは fold 0 のみで測る (anti-overfit) が、後退チェックは全 held-out を見る。
    res = admit_evaluator(inc, cand, held, epsilon=0.02, measure_fold=held.fold(0))
    assert res.chosen is inc  # 別 fold の critical 後退で却下


def test_promotion_succeeds_when_better_on_all_folds():
    held = _held(
        ("f0", 1.0, {"inc": 0.0, "cand": 1.0}, 0, False),
        ("f1", 1.0, {"inc": 0.0, "cand": 1.0}, 1, True),
    )
    inc = _ev("inc", "inc")
    cand = _ev("cand", "cand")
    res = admit_evaluator(inc, cand, held, epsilon=0.02, measure_fold=held.fold(0))
    assert res.chosen is cand


# -- 評価器 version の同一性 (P2 fix) ------------------------------------------


def test_lambda_evaluators_distinguished_by_source_location():
    """別ソース位置の同名 lambda は別 version になる (audit 証跡が壊れない)。"""
    e1 = Evaluator(score=lambda o: Score(ground_truth=0.0), name="dup")
    e2 = Evaluator(score=lambda o: Score(ground_truth=1.0), name="dup")
    assert e1.version != e2.version


def test_explicit_version_is_preserved():
    e = Evaluator(score=lambda o: Score(ground_truth=0.0), name="x", version="v-pinned")
    assert e.version == "v-pinned"
