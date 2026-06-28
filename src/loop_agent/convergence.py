"""外側 Reflexion ループの収束/停止条件 (report.md S2.6 / S4.5 / Issue #22).

内側ループの :mod:`loop_agent.conditions` と **同じ合成プロトコル** (``name`` +
``check(state) -> reason | None``) を踏襲し、:class:`~loop_agent.conditions.AnyOf` /
:class:`~loop_agent.conditions.StopTrigger` を **そのまま再利用** する。違いは check が
:class:`OuterState` (episode/epoch 粒度) を見る点だけ。

収束判定は report.md S2.6 (AWS evaluator reflect-refine) の三本柱:
**rubric しきい値超え** (:class:`RubricThreshold`) / **改善の頭打ち** (:class:`ScorePlateau`)
/ **反復上限** (:class:`MaxEpisodes`)。さらに self-improving の罠 (report.md S6) を抑える
**反省予算** (:class:`ReflectionBudget`) と **評価器更新予算** (:class:`EvaluatorUpdateBudget`)
を足す。

**最重要の安全設計**: これらが見る ``gt_aggregate_history`` / ``best_gt_aggregate`` は
すべて **ground-truth 一次信号** (内側 verify 由来) であり、epoch 内で固定される rubric
評価器の出力 (reward) には **依存しない**。よって「gameable な評価器スカラを押し上げて
収束を宣言する」抜け道が構造的に存在しない (report.md 原則: ground-truth 優先)。
``ground_truth_backed=False`` の episode は driver が ``gt_aggregate_history`` に積まない
ので、実信号の無い episode は収束/頭打ち判定に算入されない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional

from .errors import ConfigError


@dataclass(frozen=True)
class OuterState:
    """外側ループの累積状態 (収束条件が毎 episode 評価する射影)。

    - ``episode``               : 完了した episode 数 (全 episode。MaxEpisodes が見る)。
    - ``epoch``                 : 現在の epoch 番号 (境界でのみ進む)。
    - ``evaluator_version``     : 現行 (固定) 評価器の version。
    - ``gt_aggregate_history``  : **ground_truth_backed な** episode の集約値列 (一次信号)。
    - ``best_gt_aggregate``     : これまでの最良集約値 (頭打ち/成功判定の基準)。
    - ``reflections``           : memory に取り込まれた lesson の累計 (bloat 予算)。
    - ``evaluator_updates``     : 評価器昇格を試行した境界の累計 (overfit 予算)。
    - ``declared_keys``         : 多様評価の宣言軸 (監査・文脈用)。
    """

    episode: int = 0
    epoch: int = 0
    evaluator_version: str = ""
    gt_aggregate_history: tuple[float, ...] = ()
    best_gt_aggregate: float = float("-inf")
    reflections: int = 0
    evaluator_updates: int = 0
    declared_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaxEpisodes:
    """外側ループのハード上限 (report.md R3: 無限ループ防止の最後の砦)。"""

    limit: int
    name: ClassVar[str] = "max_episodes"

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ConfigError("MaxEpisodes limit must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.episode >= self.limit:
            return f"reached max episodes ({state.episode}/{self.limit})"
        return None


@dataclass(frozen=True)
class RubricThreshold:
    """**成功**収束: 一次信号の集約値が ``target`` 以上を ``sustain`` 連続で満たす。

    ``sustain`` 回連続で超えることを要求するので、分散による単発スパイクでは発火しない
    (variance gaming 耐性)。これは **成功**条件で (``success=True``)、ハード上限や頭打ちの
    打ち切りと区別される。判定は ground-truth 一次の ``gt_aggregate_history`` のみを見る。
    """

    target: float
    sustain: int = 1
    name: ClassVar[str] = "rubric_threshold"
    # 成功収束であることのマーカ (driver が成否を順序非依存に判定するのに使う)。
    success: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if self.sustain < 1:
            raise ConfigError("RubricThreshold sustain must be >= 1")

    def check(self, state: OuterState) -> Optional[str]:
        recent = state.gt_aggregate_history[-self.sustain :]
        if len(recent) < self.sustain:
            return None
        if all(v >= self.target for v in recent):
            return (
                f"rubric threshold reached: last {self.sustain} ground-truth "
                f"aggregates all >= {self.target:g}"
            )
        return None


@dataclass(frozen=True)
class ScorePlateau:
    """**頭打ち**打ち切り: best-so-far が ``window`` の間 ``min_delta`` 未満しか伸びない。

    best-so-far の **トレンド** (max(now) - max(window 前)) を見るので、ゆっくりでも
    単調改善していれば発火せず、改善が止まった (flat / sawtooth で正味ゲイン無し) ときに
    発火する。range(max-min) ベースだと、緩い実進捗を誤って打ち切り、sawtooth を永遠に
    打ち切れない (それを避ける)。これは成功ではない打ち切り。

    判定は「window 区間の best-so-far の伸びが ``min_delta`` **以下**」(``<=``)。``<`` だと
    best-so-far は単調非減少なので伸びは常に ``>= 0`` となり、``min_delta=0`` (= 正味ゲインゼロで
    打ち切りたい flat/sawtooth) が一度も発火しない no-op になってしまう。``<=`` にすることで
    ``min_delta=0`` は「全く伸びていない」ときだけ発火し、正の ``min_delta`` は「規定の最小進捗に
    届かない」ときに発火する。
    """

    window: int
    min_delta: float
    name: ClassVar[str] = "score_plateau"

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ConfigError("ScorePlateau window must be >= 1")
        if self.min_delta < 0:
            raise ConfigError("ScorePlateau min_delta must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        history = state.gt_aggregate_history
        if len(history) <= self.window:
            return None
        best_now = max(history)
        best_past = max(history[: len(history) - self.window])
        if best_now - best_past <= self.min_delta:
            return (
                f"no progress: best ground-truth aggregate improved by "
                f"{best_now - best_past:.4f} over last {self.window} episodes "
                f"(<= min_delta {self.min_delta:g})"
            )
        return None


@dataclass(frozen=True)
class ReflectionBudget:
    """反省 (取込 lesson) 累計の上限 (report.md S6: reflection 出力の肥大化・劣化を防ぐ)。"""

    max_reflections: int
    name: ClassVar[str] = "reflection_budget"

    def __post_init__(self) -> None:
        if self.max_reflections < 0:
            raise ConfigError("ReflectionBudget max_reflections must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.reflections >= self.max_reflections:
            return (
                f"reflection budget exhausted "
                f"({state.reflections}/{self.max_reflections})"
            )
        return None


@dataclass(frozen=True)
class EvaluatorUpdateBudget:
    """評価器昇格試行の累計上限 (held-out への adaptive overfit を抑える予算)。"""

    max_updates: int
    name: ClassVar[str] = "evaluator_update_budget"

    def __post_init__(self) -> None:
        if self.max_updates < 0:
            raise ConfigError("EvaluatorUpdateBudget max_updates must be >= 0")

    def check(self, state: OuterState) -> Optional[str]:
        if state.evaluator_updates >= self.max_updates:
            return (
                f"evaluator update budget exhausted "
                f"({state.evaluator_updates}/{self.max_updates})"
            )
        return None


def is_success_condition(condition: object) -> bool:
    """その条件が **成功**収束を表すか (``success=True`` を持つか)。"""
    return bool(getattr(condition, "success", False))


__all__ = [
    "OuterState",
    "MaxEpisodes",
    "RubricThreshold",
    "ScorePlateau",
    "ReflectionBudget",
    "EvaluatorUpdateBudget",
    "is_success_condition",
]
