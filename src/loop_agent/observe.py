"""観測オーケストレーション: loop_begin/step/end を emit し OTel span を張る。

:class:`LoopObserver` は :class:`~loop_agent.progress.ProgressLog` と同じ作法
（``on_step`` 観測フック + ``record_result``）に乗りつつ、ループ境界の
``loop_begin`` / ``loop_end`` も足し、1 本の OTel GenAI span を run 全体に被せる。

使い方は 2 通り。手で配線する場合（既存 ``ProgressLog`` と同じ形）::

    obs = LoopObserver(sinks=[JsonlEventSink(path)])
    with obs:
        result = run_loop(act=..., verify=..., conditions=..., on_step=obs.on_step)
        obs.record_result(result)

一括の場合（推奨の入口）::

    result = run_observed_loop(
        act=..., verify=..., conditions=..., sinks=[JsonlEventSink(path)]
    )

この層は **ループコアにのみ依存** する。loop_begin は最初のステップ前に、loop_end は
ループ復帰後に出るので、``MaxIterations(0)`` の即時停止でも begin/end は必ず残る。
ループ本体が例外で抜けた場合も :meth:`__exit__` が ``status="error"`` の loop_end を
出して span を ERROR で閉じ、全終了パスが観測可能になる。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence, Union

from .conditions import AnyOf, StopCondition
from .events import (
    LOOP_BEGIN,
    LOOP_END,
    LOOP_STEP,
    EventSink,
    LoopEvent,
    SinkErrorHandler,
    _jsonable,
    fan_out,
)
from .loop import (
    ActHook,
    Conditions,
    GatherHook,
    LoopResult,
    StepHook,
    VerifyHook,
    _default_gather,
    run_loop,
)
from .otel import LoopSpan
from .state import LoopState, StepRecord


def _condition_names(conditions: Conditions) -> list[str]:
    """停止条件群から名前リストを取り出す（loop_begin の文脈用）。"""
    if isinstance(conditions, AnyOf):
        conds: Sequence[StopCondition] = conditions.conditions
    else:
        conds = conditions
    return [getattr(c, "name", type(c).__name__) for c in conds]


class LoopObserver:
    """1 回のループ run を観測し、構造化イベント + OTel span を emit する。

    sink へは best-effort で配る（sink の例外でループを殺さない）。span は OTel 不在
    なら自動で no-op になる（:class:`~loop_agent.otel.LoopSpan`）。
    """

    def __init__(
        self,
        sinks: Sequence[EventSink] = (),
        *,
        conditions: Optional[Conditions] = None,
        otel: bool = True,
        tracer: "Optional[Any]" = None,
        span_name: str = "loop_agent.loop",
        on_sink_error: Optional[SinkErrorHandler] = None,
        initial_state: Optional[LoopState] = None,
    ) -> None:
        self._sinks: tuple[EventSink, ...] = tuple(sinks)
        self._conditions = conditions
        self._on_sink_error = on_sink_error
        self._span = LoopSpan(tracer=tracer, enabled=otel, span_name=span_name)
        self._begun = False
        self._ended = False
        # on_step が見た最後の確定累積メトリクス。result を得られない終了パス
        # （例外/取りこぼし）でも、既に完了した反復ぶんを loop_end / span に残す。
        # resume では新プロセスがまだ on_step を一度も呼ぶ前に gather/act/条件で例外を
        # 投げうるので、復元 state の累積値で seed しておき、error/incomplete の
        # loop_end が「中断前に完了済みの反復ぶん」を 0 に潰さないようにする。
        self._last_iterations = initial_state.iteration if initial_state is not None else 0
        self._last_tokens_used = (
            initial_state.tokens_used if initial_state is not None else 0
        )
        self._last_elapsed = initial_state.elapsed if initial_state is not None else 0.0

    # -- 配線フック（ProgressLog と同じ作法）-------------------------------

    def begin(self) -> None:
        """``loop_begin`` を emit し OTel span を開始する。冪等。"""
        if self._begun:
            return
        self._begun = True
        self._span.start()
        payload: dict[str, Any] = {}
        if self._conditions is not None:
            payload["conditions"] = _condition_names(self._conditions)
        self._emit(LoopEvent(kind=LOOP_BEGIN, iteration=0, elapsed=0.0, payload=payload))

    def on_step(self, record: StepRecord, state: LoopState) -> None:
        """``loop_step`` を emit する。driver の ``StepHook`` に一致。"""
        # 確定累積メトリクスを snapshot しておく（state は反復ごとに再利用される
        # 可変オブジェクトなので、スカラ値を明示的に控える）。
        self._last_iterations = state.iteration
        self._last_tokens_used = state.tokens_used
        self._last_elapsed = state.elapsed
        self._span.add_step(
            iteration=record.iteration,
            tokens=record.tokens,
            tokens_used=state.tokens_used,
            elapsed=state.elapsed,
            goal_met=record.goal_met,
            detail=record.detail,
        )
        self._emit(
            LoopEvent(
                kind=LOOP_STEP,
                iteration=record.iteration,
                elapsed=state.elapsed,
                payload={
                    "tokens": record.tokens,
                    "tokens_used": state.tokens_used,
                    "goal_met": record.goal_met,
                    "detail": record.detail,
                    "observation": _jsonable(record.observation),
                },
            )
        )

    def record_result(self, result: LoopResult) -> None:
        """``loop_end`` を emit し、終了理由 + メトリクスで span を閉じる。"""
        stop_name = result.stop.name if result.stop is not None else None
        self._emit_end(
            status=result.status,
            stop=stop_name,
            reason=result.reason,
            goal_met=result.goal_met,
            iterations=result.iterations,
            tokens_used=result.tokens_used,
            elapsed=result.elapsed,
        )

    def record_error(self, error: BaseException) -> None:
        """ループが例外で抜けたときに ``status="error"`` の loop_end を残す。

        反復数/トークン等は on_step で控えた **最後の確定累積値** を載せる（完了済みの
        反復ぶんのコストを失わない。1 反復も完了していなければ 0）。終了理由に例外内容を
        載せ、span は ERROR で閉じて例外を記録する。
        """
        reason = f"{type(error).__name__}: {error}"
        self._emit_end(
            status="error",
            stop=None,
            reason=reason,
            goal_met=False,
            iterations=self._last_iterations,
            tokens_used=self._last_tokens_used,
            elapsed=self._last_elapsed,
            error=error,
        )

    def record_incomplete(self) -> None:
        """例外なしで result を取りこぼした保険パス用の ``status="incomplete"`` loop_end。

        record_error と同じく最後の確定累積メトリクスを載せ、span と event sink の
        終了観測を揃える（begin だけで end が無いレコードを残さない）。
        """
        self._emit_end(
            status="incomplete",
            stop=None,
            reason="observer closed without a result",
            goal_met=False,
            iterations=self._last_iterations,
            tokens_used=self._last_tokens_used,
            elapsed=self._last_elapsed,
        )

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "LoopObserver":
        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and isinstance(exc, BaseException):
            # ループ本体が例外で抜けた: record_result 未呼び出しなら error を残す。
            self.record_error(exc)
        elif not self._ended:
            # 例外なしで record_result を呼び忘れたケースの保険（span/sink 終了を揃える）。
            self.record_incomplete()
        return False  # 例外は握り潰さず伝播させる

    # -- 内部 --------------------------------------------------------------

    def _emit_end(
        self,
        *,
        status: str,
        stop: Optional[str],
        reason: str,
        goal_met: bool,
        iterations: int,
        tokens_used: int,
        elapsed: float,
        error: Optional[BaseException] = None,
    ) -> None:
        """全終了パス共通: span を閉じ、対になる ``loop_end`` event を emit する。

        span 終了と event emit を必ず対で行い、二重 end は冪等に無視する。これにより
        OTel 側と event sink 側の終了観測が常に一致する。
        """
        if self._ended:
            return
        self._ended = True
        self._span.end(
            status=status,
            reason=reason,
            iterations=iterations,
            tokens_used=tokens_used,
            elapsed=elapsed,
            stop=stop,
            error=error,
        )
        self._emit(
            LoopEvent(
                kind=LOOP_END,
                iteration=iterations,
                elapsed=elapsed,
                payload={
                    "status": status,
                    "stop": stop,
                    "reason": reason,
                    "goal_met": goal_met,
                    "iterations": iterations,
                    "tokens_used": tokens_used,
                },
            )
        )

    def _emit(self, event: LoopEvent) -> None:
        fan_out(self._sinks, event, on_error=self._on_sink_error)


def run_observed_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    sinks: Sequence[EventSink] = (),
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    otel: bool = True,
    tracer: "Optional[Any]" = None,
    span_name: str = "loop_agent.loop",
    on_sink_error: Optional[SinkErrorHandler] = None,
    time_fn: Optional[Callable[[], float]] = None,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """観測を配線して :func:`~loop_agent.loop.run_loop` を回す一括の入口。

    ``run_loop`` と同じ ``act`` / ``verify`` / ``conditions`` / ``gather`` を取り、
    観測用に ``sinks`` と OTel 設定を足す。利用者の ``on_step`` があれば観測フックと
    合成して両方呼ぶ。返り値は ``run_loop`` の :class:`~loop_agent.loop.LoopResult`。

    ``initial_state`` を渡すと中断したループを観測を保ったまま **resume** できる
    (``run_loop`` の同名引数へ素通し; 詳細・限界はそちらの docstring 参照)。観測は
    新プロセスの run として begin/step/end を出すので、loop_begin の iteration は 0 から
    だが、step/end の iteration・累積メトリクスは復元 state から継続する。

    loop_begin（最初のステップ前）→ loop_step×N → loop_end（復帰後）の順で必ず emit
    される。ループ本体の例外は ``status="error"`` の loop_end を残してから再送出する。
    """
    observer = LoopObserver(
        sinks,
        conditions=conditions,
        otel=otel,
        tracer=tracer,
        span_name=span_name,
        on_sink_error=on_sink_error,
        initial_state=initial_state,
    )

    if on_step is None:
        step_hook: StepHook = observer.on_step
    else:
        user_on_step = on_step

        def step_hook(record: StepRecord, state: LoopState):
            observer.on_step(record, state)
            # Return the user hook's result rather than swallowing it, so an
            # awaitable (async on_step) reaches run_loop's strict-sync gate and is
            # rejected with AsyncSeamInSyncLoop -- consistent with passing an async
            # on_step to run_loop directly -- instead of being silently dropped.
            # The observer's own on_step is synchronous (returns None).
            return user_on_step(record, state)

    # time_fn / initial_state は渡されたときだけ run_loop に転送し、既定（time.monotonic
    # / fresh start）を尊重する。
    run_kwargs: dict[str, Any] = {}
    if time_fn is not None:
        run_kwargs["time_fn"] = time_fn
    if initial_state is not None:
        run_kwargs["initial_state"] = initial_state

    with observer:
        result = run_loop(
            act=act,
            verify=verify,
            conditions=conditions,
            gather=gather,
            on_step=step_hook,
            **run_kwargs,
        )
        observer.record_result(result)
    return result
