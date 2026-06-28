"""RQGM epoch-evaluator: 固定基準 + held-out ground truth ゲートの安全核 (Issue #22/#4).

外側 Reflexion ループの **評価器** を、self-optimize による reward hacking から守るための
中核 (report.md S6 + RQGM arXiv:2606.26294 / Issue #4 コメント)。本モジュールは葉モジュールで
LLM 依存を持たない (評価は注入された純関数 :data:`ScoreFn`)。

安全設計の要点 ― ここで担保する不変条件:

- **多様評価 (diverse evaluation)**: :class:`Score` は単一スカラではなく
  ``ground_truth`` (一次) + 宣言された ``components`` の複数軸を持ち、
  :meth:`Score.aggregate` は **宣言キーの最小値** を取る。1 軸だけ高くしても aggregate は
  上がらない (single-scalar gaming を弾く)。宣言キー欠落は 0.0 に潰す。
- **epsilon-best-belief 昇格ゲート**: :func:`admit_evaluator` は候補評価器を、**held-out の
  固定 gold ラベル** に対する一致度 (:func:`agreement`) で incumbent と比較し、
  ``agreement(candidate) > agreement(incumbent) + epsilon`` を **厳格に**満たし、かつ
  どの fold でも (および critical probe でも) 後退しないときに限り採用する。さもなくば
  incumbent 据え置き (status-quo bias)。``epsilon <= 0`` は churn 防止の安全余白を失うため拒否。
- **gold ラベルは評価器が作らない**: :class:`Probe` の ``gold_label`` は評価器と独立な
  外部 ground truth。「全部 1.0 と返す」緩い候補は varied な gold に対して一致度が **低く**
  なり昇格できない (meta-reward-hacking を弾く)。
- **dual-component 分離**: 本モジュールの測定経路 (:func:`agreement` / :func:`admit_evaluator`)
  は **事前収録された** :class:`Probe` を採点するだけで、production の act/gate/memory に
  一切触れない (性能測定と本番実行の分離)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from .errors import ConfigError


@dataclass(frozen=True)
class Score:
    """1 試行の多軸スコア。``ground_truth`` が一次、``judge`` は助言のみ。

    - ``ground_truth`` : test/lint/exit-code 由来の一次信号 (report.md 原則: ground-truth 優先)。
    - ``components``   : 多様評価の宣言軸 (例: 'correctness' / 'safety' / 'completeness')。
    - ``judge``       : LLM-as-judge の助言値。**aggregate に含めない** (バイアス源を制御に乗せない)。
    - ``detail``      : ログ用の説明。
    """

    ground_truth: float
    components: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    judge: Optional[float] = None
    detail: str = ""

    def aggregate(self, declared_keys: tuple[str, ...]) -> float:
        """宣言された全軸の **最小値** を集約値とする (多様評価; 欠落軸は 0.0)。

        ``ground_truth`` と ``declared_keys`` 各軸の min を取るので、1 軸だけ高い「単一スカラ
        gaming」は集約を押し上げられない。宣言キーが ``components`` に無ければ 0.0 として
        扱い、報告軸を間引いて threshold を超える抜け道を塞ぐ。``judge`` は意図的に除外する。
        """
        values = [self.ground_truth]
        for key in declared_keys:
            values.append(float(self.components.get(key, 0.0)))
        return min(values)


@dataclass(frozen=True)
class GroundTruthSignal:
    """episode の一次信号。内側 verify (test/lint/exit-code) に由来する権威ある成否。

    - ``succeeded``           : 内側 :class:`~loop_agent.loop.LoopResult` の成否。
    - ``score``               : ``ground_truth`` 軸が verify から埋まった :class:`Score`。
    - ``ground_truth_backed`` : test/lint 等の実信号が存在したか。``False`` の episode は
      収束判定 (:class:`~loop_agent.convergence.RubricThreshold`) に算入しない
      (緩い評価器が一次信号を捏造して収束を宣言するのを防ぐ)。
    """

    succeeded: bool
    score: Score
    ground_truth_backed: bool = True


# 注入される採点関数。outcome (EpisodeOutcome view; ``.history`` 等) -> Score。純関数想定。
ScoreFn = Callable[[Any], Score]
# 一次信号源。outcome -> GroundTruthSignal。**評価器ではなく内側 verify** に由来させる。
GroundTruthFn = Callable[[Any], GroundTruthSignal]


def _scorer_identity(score: ScoreFn) -> str:
    """採点関数の **再現可能な** 同一性キー (audit/version 用)。

    ``__qualname__`` だけだと別ソース位置の lambda が同名 (``<lambda>``) で衝突し、振る舞いの
    違う評価器が同じ version になって epoch-freeze の監査証跡を壊す。``__code__`` があれば
    **定義ソース位置** (filename:firstlineno) に加え、**実装そのもの** ― バイトコード
    ``co_code`` + 定数 ``co_consts`` + 参照名 ``co_names`` + **既定引数** ``__defaults__`` /
    ``__kwdefaults__`` ― のハッシュを足す。これにより定義位置を動かさず本体を書き換えた
    (例: ``0.5`` を ``1.0`` に) 評価器も、既定引数で振る舞いを変える factory
    (``def score(o, bias=bias): ...``) も別 version になり、resume/audit が振る舞い変更を
    silently 受理しない。同一ソース・同一既定引数なら同じ version になる (プロセスを跨いで再現)。

    **境界**: free 変数を ``__closure__`` cell で capture した closure (同一 code object・
    既定引数なし・cell 値だけ違う) はなお衝突しうる。cell 内容は任意オブジェクトで安定ハッシュ
    できないため、本関数は意図的にそこまで踏み込まない。そのような parameterized scorer は
    振る舞いが違うなら明示 ``version`` を渡すこと (docstring 参照)。
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
    """評価器の固定基準キー (content-hash)。同じ署名/ソース位置なら同じ version になる。"""
    import hashlib

    payload = f"{name}|{_scorer_identity(score)}|{'/'.join(rubric)}"
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class Evaluator:
    """epoch 内で **固定** される rubric 評価器。``score`` は注入された純関数。

    ``version`` は固定基準の同一性キー (content-hash)。明示しなければ ``score`` の同一性
    (qualname + 定義ソース位置) / ``rubric`` / ``name`` から導出する (:func:`_scorer_identity`)。
    **同じ code object を共有する closure** (同一ソース位置で capture 変数だけ違う) は version が
    衝突しうるので、振る舞いが違うなら ``version`` を明示すること (audit 証跡の忠実性のため)。
    epoch をまたぐ昇格 (:func:`admit_evaluator`) のときだけ別 version の評価器に置き換わる。``score`` の出力 (reward 信号) は Reflexion の verbal reinforcement
    としてのみ使い、収束/採用判定には載せない (それは :class:`GroundTruthSignal` の役目)。
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
    """held-out 測定用の事前収録ケース。``gold_label`` は評価器と独立な外部 ground truth。

    - ``case_id``    : 一意 ID (production task と **素な** 名前空間であることを driver が検証)。
    - ``outcome``    : 評価器が採点する事前収録 outcome (live act しない = dual-component 分離)。
    - ``gold_label`` : 固定された正解値。評価器が生成した値では **ない**。
    - ``fold``       : held-out の分割。境界ごとに回転して adaptive overfit を抑える。
    - ``critical``   : ここでの後退は決して許さない (危険ケースの dominance ガード)。
    """

    case_id: str
    outcome: Any
    gold_label: float
    fold: int = 0
    critical: bool = False


@dataclass(frozen=True)
class HeldOut:
    """固定 gold ラベル付き probe の集合 (評価器昇格の測定基盤)。"""

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
        """境界 ``k`` で使う fold を回転選択した部分集合を返す (anti adaptive-overfit)。

        fold が 1 つしか無ければ全体を返す。複数あれば ``k`` で循環選択し、毎境界で同じ
        固定 probe 集合を hill-climb されるのを防ぐ。
        """
        folds = self.folds
        if len(folds) <= 1:
            return self
        chosen = folds[k % len(folds)]
        return HeldOut(tuple(p for p in self.probes if p.fold == chosen))


def agreement(evaluator: Evaluator, held_out: HeldOut) -> float:
    """評価器の ``ground_truth`` 出力と固定 gold ラベルの **一致度** (校正; 高いほど良い)。

    各 probe で ``evaluator.score(probe.outcome).ground_truth`` と ``probe.gold_label`` の
    二乗誤差を取り、**負の平均二乗誤差** を返す (0 が完全一致、負へ行くほど乖離)。gold は
    評価器と独立なので、「全部高く返す」緩い評価器は varied な gold に対して誤差が大きく
    一致度が低くなる (= 昇格できない)。これが meta-reward-hacking ガードの肝。
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
    """:func:`admit_evaluator` の結果: 採用された評価器と両者の一致度。

    ``promoted`` は **候補が実際に採用されたか** を表す明示フラグ。version 比較ではなく
    採用判定そのものを持つので、候補と incumbent が (明示 version 指定や衝突で) 同 version
    でも、却下されたなら ``False`` を正しく返す。
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
    """epsilon-best-belief + dominance で評価器昇格を判定する (RQGM 安全ゲート)。

    候補を採用する条件 (すべて満たすときのみ。さもなくば incumbent 据え置き):

    1. **集約一致度の厳格な改善**: ``agreement(candidate) > agreement(incumbent) + epsilon``。
       ``epsilon`` は churn 防止の安全余白で **正** を要求する (``<= 0`` は拒否)。この集約ゲート
       のみ ``measure_fold`` (= 回転選択した held-out 部分集合) で測ってよい (anti adaptive-overfit:
       毎境界で同じ固定集合を hill-climb させない)。``None`` なら ``held_out`` 全体で測る。
    2. **fold 単位で後退しない**: **``held_out`` 全 fold** で候補の一致度が incumbent から
       ``delta`` 超で下がらない (集約だけ上げて特定 fold を犠牲にする gaming を弾く)。回転選択
       した fold だけでなく **全 fold** を見る (選ばれなかった fold での後退も弾く)。
    3. **critical probe で後退しない**: ``held_out`` 全体の ``critical=True`` probe で候補の
       二乗誤差が incumbent を超えない (危険ケースを犠牲にする昇格を弾く)。回転で選ばれなかった
       fold にある critical probe も必ずチェックする。

    gold ラベルは評価器と独立なので、候補が自分を高く採点しても一致度は上がらない。

    安全上の要点: 集約ゲート (1) は anti-overfit のため部分集合で測ってよいが、後退チェック
    (2)(3) は **常に held-out 全体** を見る。これにより「選ばれた fold だけ改善し、別 fold の
    critical probe を犠牲にする」昇格を構造的に弾く。
    """
    if epsilon <= 0:
        raise ConfigError("admit_evaluator epsilon must be > 0 (anti-churn margin)")

    # 集約ゲートの測定対象 (回転 fold があればそれ、無ければ全体)。後退チェックは常に全体。
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

    # (1) 集約一致度の厳格改善 (anti-overfit のため measure 上で評価)。
    if not (cand_agree > inc_agree + epsilon):
        return keep()

    # (2) fold 単位の後退チェック (held-out 全 fold を見る)。
    for f in held_out.folds:
        sub = HeldOut(tuple(p for p in held_out.probes if p.fold == f))
        if agreement(candidate, sub) < agreement(incumbent, sub) - delta:
            return keep()

    # (3) critical probe の後退チェック (held-out 全体。二乗誤差が増えていないこと)。
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
