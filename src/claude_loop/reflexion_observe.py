"""外側 Reflexion ループの観測: 構造化イベント + OTel GenAI span (Issue #30)。

内側ループの :class:`~claude_loop.observe.LoopObserver` と同じ作法で、外側
:func:`~claude_loop.reflexion.run_reflexion` の **試行間ライフサイクル** を観測する。観測層は
``run_reflexion`` の判断ロジックには一切介入しない ― 既存安全核 (二信号モデル / RQGM epoch
ゲート) はそのままで、観測フックを **側チャネル** として足すだけである (report.md S4.5 の観測性を
外側ループへ延伸)。

emit する構造化イベント (:class:`~claude_loop.events.LoopEvent` を再利用):

- ``reflexion_begin`` : run 開始 (収束条件名・宣言軸・初期評価器 version・epoch 構成)。
- ``episode_begin``   : 1 episode 開始 (episode/epoch/task/評価器 version)。
- ``episode_end``     : 1 episode 確定 (一次集約 / reward / 成否 / lesson 採否 …)。
- ``lesson_decision`` : lesson が出た episode のみ。採用 (``admitted=True``) / 拒否を独立に残す。
- ``epoch_boundary``  : epoch 境界 (= 新 epoch 開始) + 評価器昇格/却下/不変の判定。
- ``reflexion_end``   : run 終了 (収束理由・status・集計。``state`` から導出して整合させる)。

同じ run は OTel が入っていれば 1 本の **GenAI span** (:class:`~claude_loop.otel.ReflexionSpan`)
にもなり、上記遷移が span event としてタイムラインに刻まれる (epoch 番号・評価器 version =
採点係 id・lesson 由来 provenance を属性化)。OTel は **optional 依存** で、未導入環境では
no-op に degrade する (MVP #13 と同方針)。

**best-effort**: sink への配布は :func:`~claude_loop.events.fan_out` 経由で sink 単位に握り、
span は :class:`~claude_loop.otel.ReflexionSpan` 内で握る。さらに観測フック本体も握るので、
観測の失敗 (sink/tracer の例外) が外側ループを殺すことはない (``run_reflexion`` の ``on_episode``
/ ``on_epoch`` は raw 呼び出しのため、self-guard する責務は観測側が負う)。
"""

from __future__ import annotations

import warnings
from typing import Any, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition
from .evaluator import (
    Evaluator,
    GroundTruthFn,
    HeldOut,
)
from .events import (
    EventSink,
    LoopEvent,
    SinkErrorHandler,
    _jsonable,
    fan_out,
)
from .memory import EpisodicMemory, LessonVerifier, default_admit
from .otel import ReflexionSpan
from .reflexion import (
    EpisodeFn,
    EpisodeRecord,
    EpochRecord,
    OuterConditions,
    ProposeEvaluatorFn,
    ReflectHook,
    ReflexionContext,
    ReflexionState,
    ReflexiveResult,
    run_reflexion,
)

# イベント種別 (discriminator)。読み手が文字列リテラルを散在させずに filter できるよう定数化。
REFLEXION_BEGIN = "reflexion_begin"
EPISODE_BEGIN = "episode_begin"
EPISODE_END = "episode_end"
LESSON_DECISION = "lesson_decision"
EPOCH_BOUNDARY = "epoch_boundary"
REFLEXION_END = "reflexion_end"

# 外側 run の status -> span/イベントの正常 or エラー区分。converged/stopped/paused はいずれも
# 正常な終了 (打ち切りも「なぜ終わったか」が確定した正常路)。error のみ ERROR。
_OUTER_STATUSES = ("converged", "stopped", "paused")


def _outer_condition_names(conditions: OuterConditions) -> list[str]:
    """外側停止条件群から名前リストを取り出す (reflexion_begin の文脈用)。"""
    if isinstance(conditions, AnyOf):
        conds: Sequence[StopCondition] = conditions.conditions
    else:
        conds = conditions  # 列はそのまま
    return [getattr(c, "name", type(c).__name__) for c in conds]


class ReflexionObserver:
    """1 回の外側 Reflexion run を観測し、構造化イベント + OTel span を emit する。

    sink へは best-effort で配り (sink の例外で外側ループを殺さない)、span は OTel 不在なら
    自動で no-op になる (:class:`~claude_loop.otel.ReflexionSpan`)。さらに観測フック本体も握る
    ので、観測の失敗が ``run_reflexion`` へ伝播しない。

    手で配線する場合は context manager として使い、``run_reflexion`` の各観測点へ渡す::

        obs = ReflexionObserver(sinks=[JsonlEventSink(path)], convergence=conds,
                                declared_keys=keys, evaluator_version=ev.version,
                                epoch_len=4, epsilon=0.02)
        with obs:
            result = run_reflexion(
                episode=lambda ctx: (obs.on_episode_begin(ctx), episode(ctx))[1],
                ..., on_episode=obs.on_episode, on_epoch=obs.on_epoch,
            )
            obs.record_result(result)

    一括の入口は :func:`run_observed_reflexion` (配線をすべて内部で行う。推奨)。
    """

    def __init__(
        self,
        sinks: Sequence[EventSink] = (),
        *,
        convergence: Optional[OuterConditions] = None,
        declared_keys: tuple[str, ...] = (),
        evaluator_version: str = "",
        epoch_len: Optional[int] = None,
        epsilon: Optional[float] = None,
        otel: bool = True,
        tracer: "Optional[Any]" = None,
        span_name: str = "claude_loop.reflexion",
        on_sink_error: Optional[SinkErrorHandler] = None,
        initial_state: Optional[ReflexionState] = None,
    ) -> None:
        self._sinks: tuple[EventSink, ...] = tuple(sinks)
        self._convergence = convergence
        self._declared_keys = tuple(declared_keys)
        self._evaluator_version = evaluator_version
        self._epoch_len = epoch_len
        self._epsilon = epsilon
        self._on_sink_error = on_sink_error
        self._span = ReflexionSpan(tracer=tracer, enabled=otel, span_name=span_name)
        self._begun = False
        self._ended = False
        # 最後に観測した確定累積値。result を得られない終了パス (例外) でも、確定済みの
        # episode/epoch ぶんを reflexion_end / span に残す (LoopObserver と同方針)。
        # 外側 resume では、新プロセスが on_episode を 1 度も呼ぶ前に episode/条件で例外を
        # 投げうるので、復元 state の累積値で seed しておき、error/incomplete の reflexion_end が
        # 「resume 前に確定済みの episode/epoch ぶん」を 0 に潰さないようにする。
        self._last_episode = initial_state.episode if initial_state is not None else 0
        self._last_epoch = initial_state.epoch if initial_state is not None else 0
        self._last_best = (
            initial_state.best_gt_aggregate
            if initial_state is not None
            else float("-inf")
        )
        self._last_reflections = (
            initial_state.reflections if initial_state is not None else 0
        )
        self._last_evaluator_updates = (
            initial_state.evaluator_updates if initial_state is not None else 0
        )
        # version は復元 state を優先 (run_reflexion が supplied evaluator と一致を検証済み)。
        self._last_evaluator_version = (
            initial_state.evaluator_version
            if initial_state is not None and initial_state.evaluator_version
            else evaluator_version
        )

    # -- 配線フック --------------------------------------------------------

    def begin(self) -> None:
        """``reflexion_begin`` を emit し OTel span を開始する。冪等。"""
        if self._begun:
            return
        self._begun = True
        self._span.start(
            declared_keys=self._declared_keys,
            evaluator_version=self._evaluator_version,
            epoch_len=self._epoch_len,
            epsilon=self._epsilon,
        )
        payload: dict[str, Any] = {}
        if self._convergence is not None:
            payload["conditions"] = _outer_condition_names(self._convergence)
        if self._declared_keys:
            payload["declared_keys"] = list(self._declared_keys)
        if self._evaluator_version:
            payload["evaluator_version"] = self._evaluator_version
        if self._epoch_len is not None:
            payload["epoch_len"] = self._epoch_len
        if self._epsilon is not None:
            payload["epsilon"] = self._epsilon
        self._emit(
            LoopEvent(kind=REFLEXION_BEGIN, iteration=0, elapsed=0.0, payload=payload)
        )

    def on_episode_begin(self, ctx: ReflexionContext) -> None:
        """``episode_begin`` を emit する。``run_reflexion`` の ``episode`` 直前で呼ぶ。

        観測の失敗で外側ループを殺さないよう、フック本体ごと best-effort で握る。
        """
        try:
            self._span.add_episode_begin(
                episode=ctx.episode,
                epoch=ctx.epoch,
                evaluator_version=ctx.evaluator.version,
            )
            self._emit(
                LoopEvent(
                    kind=EPISODE_BEGIN,
                    iteration=ctx.episode,
                    elapsed=0.0,
                    payload={
                        "epoch": ctx.epoch,
                        "evaluator_version": ctx.evaluator.version,
                        "task": _jsonable(ctx.task),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("on_episode_begin", exc)

    def on_episode(self, record: EpisodeRecord, state: ReflexionState) -> None:
        """``episode_end`` (+ lesson が出ていれば ``lesson_decision``) を emit する。

        ``run_reflexion`` の ``on_episode`` に一致。観測の失敗で外側ループを殺さないよう、
        フック本体ごと best-effort で握る。
        """
        try:
            # 確定累積値を snapshot (state は episode ごとに再利用される可変オブジェクト)。
            self._last_episode = state.episode
            self._last_epoch = state.epoch
            self._last_best = state.best_gt_aggregate
            self._last_reflections = state.reflections
            self._last_evaluator_updates = state.evaluator_updates
            self._last_evaluator_version = state.evaluator_version

            lesson = record.lesson
            provenance = lesson.provenance if lesson is not None else ""
            self._span.add_episode(
                episode=record.episode,
                epoch=record.epoch,
                evaluator_version=record.evaluator_version,
                gt_aggregate=record.gt_aggregate,
                reward=record.reward,
                succeeded=record.succeeded,
                ground_truth_backed=record.signal.ground_truth_backed,
                best_gt_aggregate=state.best_gt_aggregate,
                lesson_admitted=record.admitted,
                lesson_provenance=provenance,
                detail=record.detail,
            )
            payload: dict[str, Any] = {
                "epoch": record.epoch,
                "evaluator_version": record.evaluator_version,
                "gt_aggregate": record.gt_aggregate,
                "reward": record.reward,
                "succeeded": record.succeeded,
                "ground_truth_backed": record.signal.ground_truth_backed,
                "reflections": state.reflections,
                "lesson_admitted": record.admitted,
                "lesson_provenance": provenance,
                "detail": record.detail,
            }
            # best が -inf (ground-truth-backed episode が 1 つも来ていない) のときは載せない
            # ( -Infinity は仕様外 JSON。run-end と同じ規約で省く。downstream の数値集計を壊さない)。
            if state.best_gt_aggregate != float("-inf"):
                payload["best_gt_aggregate"] = state.best_gt_aggregate
            self._emit(
                LoopEvent(
                    kind=EPISODE_END,
                    iteration=record.episode,
                    elapsed=0.0,
                    payload=payload,
                )
            )
            # lesson が出た episode のみ採否を独立イベントに残す (採用/拒否の filter 容易化)。
            if lesson is not None:
                self._span.add_lesson(
                    episode=record.episode,
                    admitted=record.admitted,
                    provenance=lesson.provenance,
                    support=lesson.support,
                    reason="" if record.admitted else record.detail,
                )
                self._emit(
                    LoopEvent(
                        kind=LESSON_DECISION,
                        iteration=record.episode,
                        elapsed=0.0,
                        payload={
                            "epoch": record.epoch,
                            "admitted": record.admitted,
                            "text": lesson.text,
                            "provenance": lesson.provenance,
                            "support": lesson.support,
                            "reason": "" if record.admitted else record.detail,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("on_episode", exc)

    def on_epoch(self, record: EpochRecord) -> None:
        """``epoch_boundary`` を emit する。``run_reflexion`` の ``on_epoch`` に一致。

        評価器の昇格/却下/不変 (``record.decision``) と version 遷移を残す。観測の失敗で外側
        ループを殺さないよう、フック本体ごと best-effort で握る。
        """
        try:
            self._last_epoch = record.epoch
            self._last_evaluator_version = record.evaluator_version
            # 評価器更新カウンタも境界で同期する。run_reflexion は候補が提案された境界
            # (= record.proposed) でのみ state.evaluator_updates を 1 増やす (昇格/却下は不問)
            # ので、観測スナップショットも同じ条件で進める。これをしないと、境界の直後に
            # 次 episode が例外で抜けた error/incomplete パスで evaluator_updates が 1 不足し、
            # 既に emit 済みの epoch_boundary (proposed=True) と矛盾する。
            if record.proposed:
                self._last_evaluator_updates += 1
            admission = record.admission
            inc_agree = admission.incumbent_agreement if admission is not None else None
            cand_agree = (
                admission.candidate_agreement if admission is not None else None
            )
            self._span.add_epoch(
                epoch=record.epoch,
                boundary_episode=record.boundary_episode,
                decision=record.decision,
                previous_version=record.previous_version,
                evaluator_version=record.evaluator_version,
                incumbent_agreement=inc_agree,
                candidate_agreement=cand_agree,
            )
            payload: dict[str, Any] = {
                "epoch": record.epoch,
                "boundary_episode": record.boundary_episode,
                "evaluator_decision": record.decision,
                "proposed": record.proposed,
                "promoted": record.promoted,
                "previous_version": record.previous_version,
                "evaluator_version": record.evaluator_version,
            }
            if inc_agree is not None:
                payload["incumbent_agreement"] = inc_agree
            if cand_agree is not None:
                payload["candidate_agreement"] = cand_agree
            self._emit(
                LoopEvent(
                    kind=EPOCH_BOUNDARY,
                    iteration=record.boundary_episode,
                    elapsed=0.0,
                    payload=payload,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("on_epoch", exc)

    def record_result(self, result: ReflexiveResult) -> None:
        """``reflexion_end`` を emit し、収束理由 + 集計で span を閉じる。

        集計は権威ある ``result.state`` から導出するので、emit 済みの episode/epoch イベント
        個数と最終集計が常に整合する (metric 一貫性)。
        """
        stop_name = result.stop.name if result.stop is not None else None
        state = result.state
        self._emit_end(
            status=result.status,
            stop=stop_name,
            reason=result.reason,
            succeeded=result.succeeded,
            episodes=state.episode,
            epochs=state.epoch,
            best_gt_aggregate=state.best_gt_aggregate,
            reflections=state.reflections,
            evaluator_updates=state.evaluator_updates,
            evaluator_version=state.evaluator_version,
        )

    def record_error(self, error: BaseException) -> None:
        """外側ループが例外で抜けたときに ``status="error"`` の reflexion_end を残す。

        集計は観測済みの **最後の確定累積値** を載せる (確定済み episode/epoch ぶんを失わない)。
        """
        reason = f"{type(error).__name__}: {error}"
        self._emit_end(
            status="error",
            stop=None,
            reason=reason,
            succeeded=False,
            episodes=self._last_episode,
            epochs=self._last_epoch,
            best_gt_aggregate=self._last_best,
            reflections=self._last_reflections,
            evaluator_updates=self._last_evaluator_updates,
            evaluator_version=self._last_evaluator_version,
            error=error,
        )

    def record_incomplete(self) -> None:
        """例外なしで result を取りこぼした保険パス用の ``status="incomplete"`` reflexion_end。"""
        self._emit_end(
            status="incomplete",
            stop=None,
            reason="observer closed without a result",
            succeeded=False,
            episodes=self._last_episode,
            epochs=self._last_epoch,
            best_gt_aggregate=self._last_best,
            reflections=self._last_reflections,
            evaluator_updates=self._last_evaluator_updates,
            evaluator_version=self._last_evaluator_version,
        )

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "ReflexionObserver":
        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and isinstance(exc, BaseException):
            self.record_error(exc)
        elif not self._ended:
            self.record_incomplete()
        return False  # 例外は握り潰さず伝播させる

    # -- 内部 --------------------------------------------------------------

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        warnings.warn(
            f"reflexion observer {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    def _emit_end(
        self,
        *,
        status: str,
        stop: Optional[str],
        reason: str,
        succeeded: bool,
        episodes: int,
        epochs: int,
        best_gt_aggregate: float,
        reflections: int,
        evaluator_updates: int,
        evaluator_version: str,
        error: Optional[BaseException] = None,
    ) -> None:
        """全終了パス共通: span を閉じ、対になる ``reflexion_end`` event を emit する。

        span 終了と event emit を必ず対で行い、二重 end は冪等に無視する。これにより OTel 側と
        event sink 側の終了観測が常に一致する。
        """
        if self._ended:
            return
        self._ended = True
        self._span.end(
            status=status,
            reason=reason,
            episodes=episodes,
            epochs=epochs,
            best_gt_aggregate=best_gt_aggregate,
            reflections=reflections,
            evaluator_updates=evaluator_updates,
            evaluator_version=evaluator_version,
            stop=stop,
            error=error,
        )
        payload = {
            "status": status,
            "stop": stop,
            "reason": reason,
            "succeeded": succeeded,
            "episodes": episodes,
            "epochs": epochs,
            "reflections": reflections,
            "evaluator_updates": evaluator_updates,
            "evaluator_version": evaluator_version,
        }
        # best が -inf (ground-truth-backed episode が皆無) のときは JSON 非互換値を載せない。
        if best_gt_aggregate != float("-inf"):
            payload["best_gt_aggregate"] = best_gt_aggregate
        self._emit(
            LoopEvent(
                kind=REFLEXION_END,
                iteration=episodes,
                elapsed=0.0,
                payload=payload,
            )
        )

    def _emit(self, event: LoopEvent) -> None:
        fan_out(self._sinks, event, on_error=self._on_sink_error)


def run_observed_reflexion(
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
    task_id: Any = str,
    on_episode: Optional[Any] = None,
    persist: Optional[Any] = None,
    initial_state: Optional[ReflexionState] = None,
    sinks: Sequence[EventSink] = (),
    otel: bool = True,
    tracer: "Optional[Any]" = None,
    span_name: str = "claude_loop.reflexion",
    on_sink_error: Optional[SinkErrorHandler] = None,
) -> ReflexiveResult:
    """観測を配線して :func:`~claude_loop.reflexion.run_reflexion` を回す一括の入口。

    ``run_reflexion`` と同じ引数を取り、観測用に ``sinks`` と OTel 設定を足す。``episode`` は
    観測ラッパで包んで ``episode_begin`` を出し、``on_episode`` / ``on_epoch`` には観測フックを
    配線する (利用者の ``on_episode`` があれば合成して両方呼ぶ)。返り値は ``run_reflexion`` の
    :class:`~claude_loop.reflexion.ReflexiveResult` をそのまま返す (判断ロジックは不変)。

    ``persist`` / ``initial_state`` はそのまま ``run_reflexion`` へ素通しするので、外側 Reflexion の
    **永続化/resume** (Issue #29: :class:`~claude_loop.reflexion_store.DBReflexionLog`) と観測を
    1 回の呼び出しで両立できる。resume seed (``initial_state``) で再開した run でも、抑止された末尾
    境界は recovery で取り戻され ``on_epoch`` が emit されるので、観測の epoch 数が DB の settled
    ``epoch`` と整合する (``persist`` が書く SoT と観測 event が食い違わない)。観測は side-channel
    なので ``persist`` の永続化順序・内容には一切介入しない。

    ``reflexion_begin`` (最初の episode 前) → ``episode_begin`` / ``episode_end`` /
    ``lesson_decision`` / ``epoch_boundary`` × N → ``reflexion_end`` (復帰後) の順で必ず emit
    される。外側ループ本体の例外は ``status="error"`` の ``reflexion_end`` を残してから再送出する。
    内側 episode が人間ゲートで pause した場合は ``status="paused"`` の ``reflexion_end`` を残す
    (``run_reflexion`` の pause 伝播契約をそのまま観測する)。
    """
    observer = ReflexionObserver(
        sinks,
        convergence=convergence,
        declared_keys=declared_keys,
        evaluator_version=evaluator.version,
        epoch_len=epoch_len,
        epsilon=epsilon,
        otel=otel,
        tracer=tracer,
        span_name=span_name,
        on_sink_error=on_sink_error,
        initial_state=initial_state,
    )

    user_episode = episode

    def observed_episode(ctx: ReflexionContext):
        observer.on_episode_begin(ctx)
        return user_episode(ctx)

    if on_episode is None:
        episode_hook = observer.on_episode
    else:
        user_on_episode = on_episode

        def episode_hook(record: EpisodeRecord, state: ReflexionState) -> None:
            observer.on_episode(record, state)
            user_on_episode(record, state)

    with observer:
        result = run_reflexion(
            episode=observed_episode,
            ground_truth=ground_truth,
            reflect=reflect,
            evaluator=evaluator,
            convergence=convergence,
            declared_keys=declared_keys,
            production_tasks=production_tasks,
            held_out=held_out,
            epoch_len=epoch_len,
            epsilon=epsilon,
            delta=delta,
            propose_evaluator=propose_evaluator,
            admit_lesson=admit_lesson,
            memory=memory,
            task_id=task_id,
            on_episode=episode_hook,
            on_epoch=observer.on_epoch,
            persist=persist,
            initial_state=initial_state,
        )
        observer.record_result(result)
    return result


__all__ = [
    "ReflexionObserver",
    "run_observed_reflexion",
    "REFLEXION_BEGIN",
    "EPISODE_BEGIN",
    "EPISODE_END",
    "LESSON_DECISION",
    "EPOCH_BOUNDARY",
    "REFLEXION_END",
]
