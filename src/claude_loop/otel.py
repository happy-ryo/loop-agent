"""OTel GenAI span 連携（report.md S4.5「観測性」）。**optional 依存**。

ループの一生を 1 本の OpenTelemetry span として表し、GenAI semantic conventions
の ``gen_ai.*`` 属性 + 反復番号 + 終了理由を載せる（task 指定）。

``opentelemetry`` は **optional 依存** であり、未導入環境でも壊れない。import に
失敗した場合、:class:`LoopSpan` は no-op（記録しないダミー）へ degrade し、観測の
JSONL/event sink 側はそのまま機能する。``enabled=False`` でも同じ no-op になる。

semantic conventions の対応（experimental な GenAI 規約に準拠しつつ、ループ固有の
情報は ``claude_loop.*`` 名前空間に置く）:

- ``gen_ai.operation.name`` = ``"loop"``      （この span が表す操作）
- ``gen_ai.system``         = ``"claude_loop"``
- ``gen_ai.usage.output_tokens`` = 累積 tokens（ダッシュボード互換のため GenAI usage に写像）
- ``claude_loop.iterations``       = 総反復数（= 反復番号）
- ``claude_loop.status``           = ``"goal_met" | "stopped" | "error" | "incomplete"``
- ``claude_loop.stop``             = 発火した停止条件名（無ければ未設定）
- ``claude_loop.termination_reason`` = 人間可読の終了理由
- ``claude_loop.tokens_used`` / ``claude_loop.elapsed`` = メトリクス

各反復は span の add_event（``loop_step``）としてタイムラインに刻む。
"""

from __future__ import annotations

import warnings
from typing import Any, Optional

try:  # optional 依存: 未導入でも壊れないよう degrade する
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 環境依存（OTel 未導入時のみ通る）
    _otel_trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False

# GenAI semantic-convention の属性キー（文字列リテラルを散在させない）。
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# ループ固有の属性は claude_loop.* 名前空間に置く（GenAI 規約を汚さない）。
ATTR_ITERATIONS = "claude_loop.iterations"
ATTR_STATUS = "claude_loop.status"
ATTR_STOP = "claude_loop.stop"
ATTR_TERMINATION_REASON = "claude_loop.termination_reason"
ATTR_TOKENS_USED = "claude_loop.tokens_used"
ATTR_ELAPSED = "claude_loop.elapsed"

DEFAULT_SPAN_NAME = "claude_loop.loop"
OPERATION_NAME = "loop"
SYSTEM_NAME = "claude_loop"

# 外側 Reflexion ループ (run_reflexion) 観測用の span 規約 (Issue #30)。内側 loop と
# 同じ GenAI 規約 (gen_ai.operation.name / gen_ai.system) を踏襲しつつ、外側固有の情報は
# claude_loop.reflexion.* 名前空間へ置く。span event (episode/epoch_boundary/lesson_decision)
# が epoch 番号・評価器 version (= 採点係 id)・lesson 由来 (provenance) をタイムラインに刻む。
REFLEXION_SPAN_NAME = "claude_loop.reflexion"
REFLEXION_OPERATION_NAME = "reflexion"

ATTR_REFLEXION_STATUS = "claude_loop.reflexion.status"
ATTR_REFLEXION_STOP = "claude_loop.reflexion.stop"
ATTR_REFLEXION_REASON = "claude_loop.reflexion.termination_reason"
ATTR_REFLEXION_EPISODES = "claude_loop.reflexion.episodes"
ATTR_REFLEXION_EPOCHS = "claude_loop.reflexion.epochs"
ATTR_REFLEXION_BEST = "claude_loop.reflexion.best_gt_aggregate"
ATTR_REFLEXION_REFLECTIONS = "claude_loop.reflexion.reflections"
ATTR_REFLEXION_EVALUATOR_UPDATES = "claude_loop.reflexion.evaluator_updates"
ATTR_REFLEXION_EVALUATOR_VERSION = "claude_loop.reflexion.evaluator_version"
ATTR_REFLEXION_DECLARED_KEYS = "claude_loop.reflexion.declared_keys"
ATTR_REFLEXION_EPOCH_LEN = "claude_loop.reflexion.epoch_len"
ATTR_REFLEXION_EPSILON = "claude_loop.reflexion.epsilon"


def otel_available() -> bool:
    """``opentelemetry`` が import できる環境かどうかを返す。"""
    return _OTEL_AVAILABLE


class LoopSpan:
    """ループ run 1 回を表す OTel span の薄いラッパ。OTel 不在なら no-op。

    span のライフサイクルは :class:`~claude_loop.observe.LoopObserver` が握る:
    :meth:`start` で開始、:meth:`add_step` で反復をタイムラインに刻み、
    :meth:`end` で gen_ai.* 属性 + 終了理由を載せて終了する。

    OTel が未導入、または ``enabled=False`` の場合は全メソッドが安全に何もしない
    （:attr:`recording` は ``False`` を返す）。
    """

    def __init__(
        self,
        *,
        tracer: "Optional[Any]" = None,
        enabled: bool = True,
        span_name: str = DEFAULT_SPAN_NAME,
    ) -> None:
        self._span_name = span_name
        self._span: "Optional[Any]" = None
        self._ended = False
        # OTel 不在 / 明示無効化のどちらでも no-op に倒す。
        self._enabled = bool(enabled) and _OTEL_AVAILABLE
        if self._enabled and tracer is None:
            tracer = _otel_trace.get_tracer(__name__)
        self._tracer = tracer

    @property
    def recording(self) -> bool:
        """この span が実際に記録中か（no-op なら ``False``）。"""
        return self._span is not None and not self._ended

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        """span 操作の失敗を可視化しつつ握り潰す（観測はループを殺さない）。"""
        warnings.warn(
            f"OTel span {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )

    def start(self) -> "LoopSpan":
        """span を開始し、不変の GenAI 属性を載せる。no-op なら何もしない。

        tracer 例外でループを殺さないよう best-effort。開始に失敗したら以後 no-op。
        """
        if not self._enabled or self._span is not None:
            return self
        try:
            self._span = self._tracer.start_span(self._span_name)
            self._span.set_attribute(GEN_AI_OPERATION_NAME, OPERATION_NAME)
            self._span.set_attribute(GEN_AI_SYSTEM, SYSTEM_NAME)
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._span = None  # 半端な span は捨て、以後 no-op に倒す
            self._warn("start", exc)
        return self

    def add_step(
        self,
        *,
        iteration: int,
        tokens: int,
        tokens_used: int,
        elapsed: float,
        goal_met: bool,
        detail: str = "",
    ) -> None:
        """1 反復を span の add_event（``loop_step``）としてタイムラインに刻む。

        ``add_step`` は driver の hot な on_step 経路で呼ばれるため、tracer 例外が
        ループへ伝播しないよう best-effort で握る。
        """
        if not self.recording:
            return
        try:
            self._span.add_event(
                "loop_step",
                attributes={
                    "iteration": iteration,
                    "tokens": tokens,
                    "tokens_used": tokens_used,
                    "elapsed": elapsed,
                    "goal_met": goal_met,
                    "detail": detail,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("add_step", exc)

    def end(
        self,
        *,
        status: str,
        reason: str,
        iterations: int,
        tokens_used: int,
        elapsed: float,
        stop: Optional[str] = None,
        error: "Optional[BaseException]" = None,
    ) -> None:
        """終了理由 + メトリクスを gen_ai.* / claude_loop.* に載せて span を閉じる。

        ``status="error"`` または ``error`` が渡された場合は span status を ERROR に
        し、例外を記録する。goal_met / stopped は正常終了として OK 扱い。
        二重 end は無視する。tracer 例外でループを殺さないよう best-effort で、失敗時も
        span.end() の到達を試み（span リークを避ける）、確実に ended 状態へ倒す。
        """
        if not self.recording:
            self._ended = True
            return
        span = self._span
        try:
            span.set_attribute(ATTR_STATUS, status)
            span.set_attribute(ATTR_ITERATIONS, iterations)
            span.set_attribute(ATTR_TERMINATION_REASON, reason)
            span.set_attribute(ATTR_TOKENS_USED, tokens_used)
            span.set_attribute(ATTR_ELAPSED, elapsed)
            # ダッシュボード互換のため累積トークンを GenAI usage にも写像する。
            span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, tokens_used)
            if stop is not None:
                span.set_attribute(ATTR_STOP, stop)
            if error is not None:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            elif status == "error":
                span.set_status(Status(StatusCode.ERROR, reason))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("end", exc)
        finally:
            # 属性設定が失敗しても span は必ず閉じる（リーク防止）。
            try:
                span.end()
            except Exception as exc:  # noqa: BLE001 - 観測は best-effort
                self._warn("end", exc)
            self._ended = True


class ReflexionSpan:
    """外側 Reflexion run 1 回を表す OTel span の薄いラッパ。OTel 不在なら no-op。

    内側 :class:`LoopSpan` と同じライフサイクル契約 (start/…/end + best-effort degrade) を
    踏襲する。違いは、刻むタイムラインが **反復** ではなく **episode / epoch 境界 / lesson 採否**
    である点だけ。span のライフサイクルは :class:`~claude_loop.reflexion_observe.ReflexionObserver`
    が握る:

    - :meth:`start` で span を開始し、run 不変の GenAI 属性 + 構成 (declared_keys/epoch_len/epsilon)
      を載せる。
    - :meth:`add_episode` で 1 episode を ``episode`` event として刻む (epoch 番号・評価器
      version = 採点係 id・一次集約 / reward・lesson 採否 / 由来 provenance を属性化)。
    - :meth:`add_epoch` で 1 epoch 境界を ``epoch_boundary`` event として刻む (評価器昇格/却下と
      version 遷移)。
    - :meth:`end` で外側の終了理由 + 集計を ``claude_loop.reflexion.*`` に載せて span を閉じる。

    OTel が未導入、または ``enabled=False`` の場合は全メソッドが安全に何もしない
    (:attr:`recording` は ``False`` を返す)。tracer/span の例外は best-effort で握り、外側
    ループを殺さない (観測はループを殺さない)。
    """

    def __init__(
        self,
        *,
        tracer: "Optional[Any]" = None,
        enabled: bool = True,
        span_name: str = REFLEXION_SPAN_NAME,
    ) -> None:
        self._span_name = span_name
        self._span: "Optional[Any]" = None
        self._ended = False
        self._enabled = bool(enabled) and _OTEL_AVAILABLE
        if self._enabled and tracer is None:
            tracer = _otel_trace.get_tracer(__name__)
        self._tracer = tracer

    @property
    def recording(self) -> bool:
        """この span が実際に記録中か (no-op なら ``False``)。"""
        return self._span is not None and not self._ended

    @staticmethod
    def _warn(op: str, exc: BaseException) -> None:
        warnings.warn(
            f"OTel reflexion span {op} failed: {type(exc).__name__}: {exc}",
            RuntimeWarning,
            stacklevel=3,
        )

    def start(
        self,
        *,
        declared_keys: "tuple[str, ...]" = (),
        evaluator_version: str = "",
        epoch_len: Optional[int] = None,
        epsilon: Optional[float] = None,
    ) -> "ReflexionSpan":
        """span を開始し、不変の GenAI 属性 + 構成を載せる。no-op なら何もしない。"""
        if not self._enabled or self._span is not None:
            return self
        try:
            self._span = self._tracer.start_span(self._span_name)
            self._span.set_attribute(GEN_AI_OPERATION_NAME, REFLEXION_OPERATION_NAME)
            self._span.set_attribute(GEN_AI_SYSTEM, SYSTEM_NAME)
            if declared_keys:
                # OTel 属性値はスカラ列のみ許容。宣言軸はそのまま配列属性で載せる。
                self._span.set_attribute(
                    ATTR_REFLEXION_DECLARED_KEYS, list(declared_keys)
                )
            if evaluator_version:
                self._span.set_attribute(
                    ATTR_REFLEXION_EVALUATOR_VERSION, evaluator_version
                )
            if epoch_len is not None:
                self._span.set_attribute(ATTR_REFLEXION_EPOCH_LEN, epoch_len)
            if epsilon is not None:
                self._span.set_attribute(ATTR_REFLEXION_EPSILON, epsilon)
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._span = None  # 半端な span は捨て、以後 no-op に倒す
            self._warn("start", exc)
        return self

    def add_episode_begin(self, *, episode: int, epoch: int, evaluator_version: str) -> None:
        """episode 開始を ``episode_begin`` event として刻む。"""
        if not self.recording:
            return
        try:
            self._span.add_event(
                "episode_begin",
                attributes={
                    "episode": episode,
                    "epoch": epoch,
                    "evaluator_version": evaluator_version,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("add_episode_begin", exc)

    def add_episode(
        self,
        *,
        episode: int,
        epoch: int,
        evaluator_version: str,
        gt_aggregate: float,
        reward: float,
        succeeded: bool,
        ground_truth_backed: bool,
        best_gt_aggregate: float,
        lesson_admitted: bool,
        lesson_provenance: str = "",
        detail: str = "",
    ) -> None:
        """1 episode を ``episode`` event として span タイムラインに刻む。"""
        if not self.recording:
            return
        attributes: "dict[str, Any]" = {
            "episode": episode,
            "epoch": epoch,
            "evaluator_version": evaluator_version,
            "gt_aggregate": gt_aggregate,
            "reward": reward,
            "succeeded": succeeded,
            "ground_truth_backed": ground_truth_backed,
            "lesson_admitted": lesson_admitted,
            "lesson_provenance": lesson_provenance,
            "detail": detail,
        }
        # best が -inf (ground-truth-backed episode が未到来) なら属性化しない (OTel に -inf を
        # 載せない。run-end の end() ガードと同じ規約)。
        if best_gt_aggregate != float("-inf"):
            attributes["best_gt_aggregate"] = best_gt_aggregate
        try:
            self._span.add_event("episode", attributes=attributes)
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("add_episode", exc)

    def add_lesson(
        self,
        *,
        episode: int,
        admitted: bool,
        provenance: str = "",
        support: float = 0.0,
        reason: str = "",
    ) -> None:
        """lesson 採用/拒否を ``lesson_decision`` event として刻む。"""
        if not self.recording:
            return
        try:
            self._span.add_event(
                "lesson_decision",
                attributes={
                    "episode": episode,
                    "admitted": admitted,
                    "provenance": provenance,
                    "support": support,
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("add_lesson", exc)

    def add_epoch(
        self,
        *,
        epoch: int,
        boundary_episode: int,
        decision: str,
        previous_version: str,
        evaluator_version: str,
        incumbent_agreement: Optional[float] = None,
        candidate_agreement: Optional[float] = None,
    ) -> None:
        """1 epoch 境界を ``epoch_boundary`` event として刻む (評価器昇格/却下)。"""
        if not self.recording:
            return
        attributes: "dict[str, Any]" = {
            "epoch": epoch,
            "boundary_episode": boundary_episode,
            "evaluator_decision": decision,
            "previous_version": previous_version,
            "evaluator_version": evaluator_version,
        }
        if incumbent_agreement is not None:
            attributes["incumbent_agreement"] = incumbent_agreement
        if candidate_agreement is not None:
            attributes["candidate_agreement"] = candidate_agreement
        try:
            self._span.add_event("epoch_boundary", attributes=attributes)
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("add_epoch", exc)

    def end(
        self,
        *,
        status: str,
        reason: str,
        episodes: int,
        epochs: int,
        best_gt_aggregate: float,
        reflections: int,
        evaluator_updates: int,
        evaluator_version: str,
        stop: Optional[str] = None,
        error: "Optional[BaseException]" = None,
    ) -> None:
        """外側の終了理由 + 集計を ``claude_loop.reflexion.*`` に載せて span を閉じる。

        ``status="error"`` または ``error`` が渡された場合は span status を ERROR にし例外を
        記録する。``converged`` / ``stopped`` / ``paused`` は正常終了として OK 扱い。二重 end は
        無視する。属性設定が失敗しても span.end() の到達を試み (リーク防止)、確実に ended へ倒す。
        """
        if not self.recording:
            self._ended = True
            return
        span = self._span
        try:
            span.set_attribute(ATTR_REFLEXION_STATUS, status)
            span.set_attribute(ATTR_REFLEXION_REASON, reason)
            span.set_attribute(ATTR_REFLEXION_EPISODES, episodes)
            span.set_attribute(ATTR_REFLEXION_EPOCHS, epochs)
            # best が -inf (ground-truth-backed episode が 1 つも無い) のときは属性化しない
            # (OTel に -inf を載せない / ダッシュボード側の数値集計を壊さない)。
            if best_gt_aggregate != float("-inf"):
                span.set_attribute(ATTR_REFLEXION_BEST, best_gt_aggregate)
            span.set_attribute(ATTR_REFLEXION_REFLECTIONS, reflections)
            span.set_attribute(ATTR_REFLEXION_EVALUATOR_UPDATES, evaluator_updates)
            if evaluator_version:
                span.set_attribute(
                    ATTR_REFLEXION_EVALUATOR_VERSION, evaluator_version
                )
            if stop is not None:
                span.set_attribute(ATTR_REFLEXION_STOP, stop)
            if error is not None:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
            elif status == "error":
                span.set_status(Status(StatusCode.ERROR, reason))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            self._warn("end", exc)
        finally:
            try:
                span.end()
            except Exception as exc:  # noqa: BLE001 - 観測は best-effort
                self._warn("end", exc)
            self._ended = True
