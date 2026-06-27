"""外側 Reflexion ループ駆動: 試行間の言語的自己改善 + RQGM epoch 安全核 (Issue #22).

内側 ReAct ループ (:func:`claude_loop.loop.run_loop`) を **1 episode** として包み、episode
境界で ``reflect(trajectory, signal, reward)`` を回して言語的指針を
:class:`~claude_loop.memory.EpisodicMemory` に取り込み、次 episode の context へ配線する
(report.md S4.4 擬似コード / S5 Phase3)。

**二信号モデル (本設計の肝・安全核)**: 各 episode は 2 つの異なる信号を生む。

- ``signal`` (:class:`~claude_loop.evaluator.GroundTruthSignal`): **ground-truth 一次**。
  内側 verify (test/lint/exit-code) と ``LoopResult.succeeded`` に由来し、駆動側が計算する。
  収束/頭打ち/best/評価器昇格ゲート/lesson 採用 ― すべての **帰結ある制御** はこれが駆動する。
  epoch をまたぐ評価器の入れ替えに依存しない (評価器非依存スケール)。
- ``reward`` (float): **epoch 内で固定**された rubric 評価器の出力。Reflexion の verbal
  reinforcement として **``reflect`` だけが消費** する。収束/採用判定には一切載らない。

これにより「gameable な評価器スカラを押し上げて収束を宣言する」抜け道が構造的に塞がれる
(report.md 原則: ground-truth 優先)。評価器の入れ替えは **epoch 境界** でのみ、かつ held-out
固定 gold に対する epsilon-best-belief ゲート (:func:`claude_loop.evaluator.admit_evaluator`)
を通ったときに限る (RQGM。Issue #4)。

**dual-component 分離**: production 経路 (``episode`` -> 内側 run_loop。副作用あり) と、評価器
昇格の測定経路 (事前収録 :class:`~claude_loop.evaluator.HeldOut` probe の採点。副作用なし) を
分ける。両者の task 名前空間が素であることを構成時に検証する。

本モジュールは **単一プロセス** の self-improving に集中する。分散協調・外側ループの永続化は
本 issue の範囲外 (前者は #21、後者は追跡 follow-up)。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition, StopTrigger
from .convergence import OuterState, is_success_condition
from .evaluator import (
    Evaluator,
    GroundTruthFn,
    GroundTruthSignal,
    HeldOut,
    admit_evaluator,
)
from .loop import LoopResult
from .memory import (
    EpisodicMemory,
    Lesson,
    LessonVerdict,
    LessonVerifier,
    default_admit,
    trajectory_signatures,
)
from .state import StepRecord


@dataclass(frozen=True)
class EpisodeOutcome:
    """内側 :class:`~claude_loop.loop.LoopResult` の **読み取り専用** ビュー。

    ground-truth 一次信号 (内側 verify の結果) の権威ソース。reflect / 取込前検証 / 評価器が
    参照するのは ``history`` (軌跡) と ``succeeded`` (権威ある成否)。
    """

    result: LoopResult

    @property
    def history(self) -> tuple[StepRecord, ...]:
        return tuple(self.result.history)

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    @property
    def tokens_used(self) -> int:
        return self.result.tokens_used

    @property
    def elapsed(self) -> float:
        return self.result.elapsed


@dataclass(frozen=True)
class ReflexionContext:
    """``episode`` フックに渡す文脈。``memory_block`` を内側 gather に折り込むのは呼び出し側。

    - ``episode`` / ``epoch`` : 現在の外側カウンタ。
    - ``task``                : この episode の production タスク (held-out と素な名前空間)。
    - ``evaluator``           : この epoch で **固定**された評価器 (reward 採点用)。
    - ``memory_block``        : :meth:`EpisodicMemory.render` の文字列。前試行の学びの配線元。
    """

    episode: int
    epoch: int
    task: Any
    evaluator: Evaluator
    memory_block: str


@dataclass
class EpisodeRecord:
    """1 episode の確定記録 (監査・観測単位)。"""

    episode: int
    epoch: int
    evaluator_version: str
    signal: GroundTruthSignal  # 一次
    reward: float  # epoch 固定評価器の reflect 用ラベル
    gt_aggregate: float
    lesson: Optional[Lesson] = None
    admitted: bool = False
    succeeded: bool = False
    detail: str = ""


@dataclass
class ReflexionState:
    """外側ループの可変アキュムレータ (収束条件が射影 :meth:`outer_state` を見る)。"""

    episode: int = 0
    epoch: int = 0
    evaluator_version: str = ""
    gt_aggregate_history: list[float] = field(default_factory=list)
    best_gt_aggregate: float = float("-inf")
    reflections: int = 0
    evaluator_updates: int = 0
    declared_keys: tuple[str, ...] = ()
    episodes: list[EpisodeRecord] = field(default_factory=list)
    memory: EpisodicMemory = field(default_factory=EpisodicMemory)

    def outer_state(self) -> OuterState:
        """収束条件 (:class:`~claude_loop.convergence.OuterState`) 用の不変射影を返す。"""
        return OuterState(
            episode=self.episode,
            epoch=self.epoch,
            evaluator_version=self.evaluator_version,
            gt_aggregate_history=tuple(self.gt_aggregate_history),
            best_gt_aggregate=self.best_gt_aggregate,
            reflections=self.reflections,
            evaluator_updates=self.evaluator_updates,
            declared_keys=self.declared_keys,
        )


@dataclass
class ReflexiveResult:
    """外側ループの結果。``status`` は ``"converged"`` / ``"stopped"`` / ``"paused"``。

    ``succeeded`` は **トリガ順に依存せず** 状態から判定する: 終了時点で成功条件
    (:class:`~claude_loop.convergence.RubricThreshold`) が満たされていれば成功
    (内側ループの ``stop.name`` 依存判定が抱える順序問題を踏まない)。

    ``status == "paused"`` は内側 episode が人間ゲートで中断した場合 (``stop`` は ``None``、
    ``pending`` に内側 :class:`~claude_loop.loop.LoopResult` の pending を載せる)。この episode は
    **未完了**として記録せず (gt/reflect も走らせず) episode を進めない。人間がゲート決定を
    永続化した後に同じ引数で resume すれば、同じ episode が再実行され内側ゲートが決定を適用して
    完了する (内側の pause/resume 契約をそのまま外側へ伝播する。Issue #15)。
    """

    status: str
    stop: Optional[StopTrigger]
    state: ReflexionState
    pending: Optional[Any] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "converged"

    @property
    def paused(self) -> bool:
        return self.status == "paused"

    @property
    def best_score(self) -> float:
        return self.state.best_gt_aggregate

    @property
    def episodes(self) -> int:
        return self.state.episode

    @property
    def epochs(self) -> int:
        return self.state.epoch

    @property
    def reason(self) -> str:
        if self.paused:
            return f"paused: awaiting human decision (episode {self.state.episode})"
        return self.stop.reason if self.stop is not None else ""


# フック型。
EpisodeFn = Callable[[ReflexionContext], LoopResult]
# reflect: (軌跡, 一次信号, 固定評価器の reward) -> 言語的 lesson (or None)。
# 返した Lesson の ``episode`` / ``support`` は driver が正本で上書きする (hook は正しい
# episode 番号や権威 support を知らないため。``text`` / ``provenance`` のみ hook 由来)。
ReflectHook = Callable[
    [tuple[StepRecord, ...], GroundTruthSignal, float], Optional[Lesson]
]
EpisodeHook = Callable[[EpisodeRecord, ReflexionState], None]
ProposeEvaluatorFn = Callable[[OuterState, Evaluator], Optional[Evaluator]]
OuterConditions = Union[AnyOf, Sequence[StopCondition]]


def _normalize_conditions(conditions: OuterConditions) -> AnyOf:
    if isinstance(conditions, AnyOf):
        return conditions
    if isinstance(conditions, (list, tuple)):
        return AnyOf(conditions)
    raise TypeError(
        "convergence must be an AnyOf or a sequence of stop conditions, "
        f"got {type(conditions).__name__}"
    )


def _is_success(stop: AnyOf, state: OuterState) -> bool:
    """終了時点で **いずれかの成功条件** が満たされているか (順序非依存)。

    AnyOf が最初に発火した条件を返すため、成功条件とハード上限が同一 guard で同時発火した
    場合に ``stop.name`` で成否を決めると順序に依存する。代わりに「成功条件が現在満たされて
    いるか」を直接問うことで、どの順で並んでいても成否が一定になる。
    """
    for condition in stop.conditions:
        if is_success_condition(condition) and condition.check(state) is not None:
            return True
    return False


def run_reflexion(
    *,
    episode: EpisodeFn,
    ground_truth: GroundTruthFn,
    reflect: ReflectHook,
    evaluator: Evaluator,
    convergence: OuterConditions,
    declared_keys: tuple[str, ...],
    production_tasks: Sequence[Any],
    held_out: HeldOut,
    epoch_len: int = 4,
    epsilon: float = 0.02,
    delta: float = 0.0,
    propose_evaluator: Optional[ProposeEvaluatorFn] = None,
    admit_lesson: LessonVerifier = default_admit,
    memory: Optional[EpisodicMemory] = None,
    task_id: Callable[[Any], str] = str,
    on_episode: Optional[EpisodeHook] = None,
    initial_state: Optional[ReflexionState] = None,
) -> ReflexiveResult:
    """外側 Reflexion ループを回す入口 (二信号モデル + RQGM epoch ゲート)。

    Args:
        episode: production 経路。``ReflexionContext`` を受け取り内側 ``run_loop`` を 1 回
            回して :class:`~claude_loop.loop.LoopResult` を返す (driver は内側に手を入れない)。
        ground_truth: **一次信号源**。``EpisodeOutcome`` から
            :class:`~claude_loop.evaluator.GroundTruthSignal` を作る (内側 verify 由来)。
        reflect: episode 境界で軌跡/一次信号/reward から言語的 lesson を抽出するフック。
            例外は **非致命** (lesson を捨てて続行する)。
        evaluator: 初期 incumbent 評価器。各 epoch 内で固定され、reward (reflect 用ラベル)
            を採点する。境界でのみ :func:`~claude_loop.evaluator.admit_evaluator` 経由で交代。
        convergence: :class:`~claude_loop.conditions.AnyOf` または停止条件列
            (:mod:`claude_loop.convergence`)。内側と同じ合成プロトコルを再利用する。
        declared_keys: 多様評価の宣言軸 (集約は宣言軸の最小値。欠落は 0.0)。非空必須。
        production_tasks: episode ごとの production タスク列 (``episode % len`` で循環)。
        held_out: 評価器昇格の測定基盤 (固定 gold ラベル付き probe)。dual-component の測定経路。
        epoch_len: 1 epoch の episode 数。``>= 2`` 必須 (1 は「毎 episode 更新 = 動く評価器」)。
        epsilon: epsilon-best-belief の churn 防止余白。``> 0`` 必須。
        delta: fold 単位後退の許容幅。
        propose_evaluator: 境界で候補評価器を提案するフック (``None`` なら評価器は不変)。
        admit_lesson: 取込前検証フック (既定 :func:`~claude_loop.memory.default_admit`)。
            **support は driver が grounding から再計算して上書き** するので、自己申告 support は
            効かない。意味的/効果ベースの検証はここを差し替える。
        memory: 既存の :class:`EpisodicMemory` (resume 等)。``None`` なら新規。
        task_id: production タスク -> 識別子。held-out との素性検証に使う (既定 ``str``)。
        on_episode: 各 episode 確定後に呼ぶ観測フック。
        initial_state: 外側 resume の seed (内側 ``run_loop`` の ``initial_state`` に対応)。

    Raises:
        ValueError: ``epoch_len < 2`` / ``epsilon <= 0`` / ``declared_keys`` 空 /
            ``production_tasks`` 空 / production と held-out の task 名前空間が交差する場合。
    """
    if epoch_len < 2:
        raise ValueError(
            "epoch_len must be >= 2 (epoch_len==1 degenerates to a moving evaluator)"
        )
    if epsilon <= 0:
        raise ValueError("epsilon must be > 0 (anti-churn margin for evaluator promotion)")
    if not declared_keys:
        raise ValueError("declared_keys must be non-empty (diverse evaluation)")
    if not production_tasks:
        raise ValueError("production_tasks must be non-empty")
    # dual-component 分離: production と held-out の task 名前空間が素であることを検証。
    prod_ids = {task_id(t) for t in production_tasks}
    held_ids = {p.case_id for p in held_out.probes}
    overlap = prod_ids & held_ids
    if overlap:
        raise ValueError(
            "production_tasks and held_out probes must be disjoint "
            f"(dual-component separation); overlapping ids: {sorted(overlap)}"
        )

    stop = _normalize_conditions(convergence)

    if initial_state is not None:
        # 外側 resume: 前 run が境界で評価器を昇格していたら、復元 state の evaluator_version は
        # その昇格後の version を指す。評価器 (callable) は直列化できず復元できないため、
        # **silently 別 evaluator に差し替えない**。復元 version と渡された evaluator.version が
        # 食い違うなら、resume 地点で有効だった評価器を渡すよう loud に要求する (epoch-freeze の
        # 監査証跡を resume の継ぎ目で壊さない。version→Evaluator registry での完全復元は follow-up)。
        if (
            initial_state.evaluator_version
            and initial_state.evaluator_version != evaluator.version
        ):
            raise ValueError(
                f"resume: persisted evaluator_version {initial_state.evaluator_version!r} does "
                f"not match supplied evaluator.version {evaluator.version!r}. Outer resume cannot "
                "reconstruct an evaluator (callables are not serializable); supply the evaluator "
                "that was active at the resume point (its version must match the persisted one)."
            )
        # 同様に declared_keys の整合も要求する: 復元 state の gt_aggregate_history /
        # best_gt_aggregate は **当時の declared_keys で集約された値**。別の軸集合で resume すると、
        # その stale な集約に対して RubricThreshold 等が誤発火し、過去 episode が満たしていない
        # rubric で「収束」を宣言しうる。一致しなければ loud に弾く (集約は遡及再計算しない方針)。
        if (
            initial_state.declared_keys
            and tuple(initial_state.declared_keys) != tuple(declared_keys)
        ):
            raise ValueError(
                f"resume: persisted declared_keys {tuple(initial_state.declared_keys)!r} do not "
                f"match supplied {tuple(declared_keys)!r}; the persisted ground-truth aggregate "
                "history was computed under the old axes and would be stale. Supply the same "
                "declared_keys used for the original run (or start a fresh run)."
            )
        # 内側 run_loop と同じく seed を **破壊的に使わない**: caller が保持する resume snapshot を
        # その場で進めてしまうと、失敗/再試行の再 resume が既に進んだ seed から始まり episode を
        # 飛ばす。list と memory を複製した独立 state にコピーする (EpisodeRecord/Lesson は
        # append-only かつ frozen なので浅く共有してよい)。memory が明示指定されていればそれを
        # live で使う (caller が満たす目的で渡した live オブジェクト)。
        state = ReflexionState(
            episode=initial_state.episode,
            epoch=initial_state.epoch,
            evaluator_version=initial_state.evaluator_version,
            gt_aggregate_history=list(initial_state.gt_aggregate_history),
            best_gt_aggregate=initial_state.best_gt_aggregate,
            reflections=initial_state.reflections,
            evaluator_updates=initial_state.evaluator_updates,
            declared_keys=initial_state.declared_keys,
            episodes=list(initial_state.episodes),
            memory=memory if memory is not None else initial_state.memory.copy(),
        )
    else:
        # `memory or EpisodicMemory()` は不可: 空の EpisodicMemory は __len__==0 で falsy のため
        # 渡された空 memory が捨てられる。明示的に None 判定する。
        state = ReflexionState(memory=memory if memory is not None else EpisodicMemory())
    state.declared_keys = declared_keys
    incumbent = evaluator
    state.evaluator_version = incumbent.version

    while True:
        triggered = stop.first_triggered(state.outer_state())
        if triggered is not None:
            status = "converged" if _is_success(stop, state.outer_state()) else "stopped"
            return ReflexiveResult(status=status, stop=triggered, state=state)

        task = production_tasks[state.episode % len(production_tasks)]
        ctx = ReflexionContext(
            episode=state.episode,
            epoch=state.epoch,
            task=task,
            evaluator=incumbent,
            memory_block=state.memory.render(),
        )
        result = episode(ctx)

        # 内側 episode が人間ゲートで中断したら、外側もそこで中断して pending を伝播する。
        # この episode は未完了なので score/reflect せず episode も進めない。人間が決定を
        # 永続化して resume すれば同じ episode が再実行され、内側ゲートが決定を適用して完了する
        # (不可逆 action が承認前に再提案・二重実行されるのを防ぐ。Issue #15 の pause 契約)。
        if getattr(result, "paused", False):
            return ReflexiveResult(
                status="paused", stop=None, state=state, pending=result.pending
            )

        outcome = EpisodeOutcome(result)

        # (1) 一次信号: 内側 verify 由来を driver が計算 (評価器ではない)。
        signal = ground_truth(outcome)
        gt_aggregate = signal.score.aggregate(declared_keys)
        # (2) reward: epoch 内で固定された評価器のラベル (reflect 専用)。
        reward = incumbent.score(outcome).ground_truth

        record = EpisodeRecord(
            episode=state.episode,
            epoch=state.epoch,
            evaluator_version=incumbent.version,
            signal=signal,
            reward=reward,
            gt_aggregate=gt_aggregate,
            succeeded=signal.succeeded,
        )

        # (3) episode 境界の reflect。reflect/取込前検証の例外は非致命 (lesson 破棄)。
        lesson: Optional[Lesson] = None
        admitted = False
        try:
            lesson = reflect(outcome.history, signal, reward)
        except Exception as exc:  # noqa: BLE001 - reflect 失敗で run 全体を倒さない
            record.detail = f"reflect failed: {type(exc).__name__}: {exc}"
            lesson = None
        if lesson is not None:
            # support は **権威ある grounding から再計算して上書き** (自己申告を信用しない)。
            # episode も driver が正本で打ち直す: reflect は (軌跡, 信号, reward) しか受け取らず
            # 正しい episode 番号を知らないため、hook の placeholder を残すと memory の
            # episode ベース eviction/監査が誤る (例: 後続 episode の lesson が ep0 扱い)。
            #
            # grounding には **ground_truth_backed も要求** する: 実信号 (test/lint 等) の無い
            # episode は収束履歴に算入しない (RubricThreshold/Plateau)。同じ理由で、その episode
            # 由来の lesson も support 0 にして memory に入れない。さもないと「収束には算入しないが
            # 次 context は書き換える」未検証 episode が production 挙動に影響してしまい、
            # ground-truth 一次の不変条件と矛盾する。
            grounded = (
                signal.ground_truth_backed
                and lesson.provenance in trajectory_signatures(outcome.history)
            )
            lesson = replace(
                lesson,
                support=1.0 if grounded else 0.0,
                episode=state.episode,
            )
            try:
                verdict: LessonVerdict = admit_lesson(lesson, outcome)
            except Exception as exc:  # noqa: BLE001
                verdict = LessonVerdict(admit=False, reason=f"verifier error: {exc}")
            admitted = state.memory.admit(lesson, verdict)
            if admitted:
                state.reflections += 1
        record.lesson = lesson
        record.admitted = admitted

        # 一次信号のみ収束履歴に積む (実信号の無い episode は算入しない)。
        if signal.ground_truth_backed:
            state.gt_aggregate_history.append(gt_aggregate)
            state.best_gt_aggregate = max(state.best_gt_aggregate, gt_aggregate)
        state.episodes.append(record)
        state.episode += 1

        if on_episode is not None:
            on_episode(record, state)

        # (4) epoch 境界: incumbent を入れ替えてよい **唯一** の場所。
        # ただし、この episode で収束/打ち切り条件が既に成立しているなら昇格しない:
        # 次以降の episode が無いのに propose/admit を走らせると、終端 run の evaluator_version を
        # 無用に書き換えたり、proposal hook の例外で終端 run を倒したりしうるため
        # (次の while ガードで同じ stop が発火して即終了する)。
        if (
            state.episode % epoch_len == 0
            and stop.first_triggered(state.outer_state()) is None
        ):
            state.epoch += 1
            if propose_evaluator is not None:
                candidate = propose_evaluator(state.outer_state(), incumbent)
                if candidate is not None:
                    # 集約ゲートは回転 fold で測る (anti-overfit) が、fold/critical 後退
                    # チェックは held-out 全体で行う (選ばれなかった fold の犠牲を弾く)。
                    admission = admit_evaluator(
                        incumbent,
                        candidate,
                        held_out,
                        epsilon=epsilon,
                        delta=delta,
                        measure_fold=held_out.fold(state.epoch),
                    )
                    incumbent = admission.chosen
                    state.evaluator_version = incumbent.version
                    state.evaluator_updates += 1


__all__ = [
    "EpisodeOutcome",
    "ReflexionContext",
    "EpisodeRecord",
    "ReflexionState",
    "ReflexiveResult",
    "run_reflexion",
    "EpisodeFn",
    "ReflectHook",
    "EpisodeHook",
    "ProposeEvaluatorFn",
]
