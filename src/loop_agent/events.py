"""構造化ループイベントとその sink（report.md S4.5「観測性」/ S5 Phase 2）。

観測層は loop_begin / loop_step / loop_end の 3 種を *構造化イベント* として
emit する。各イベントは反復番号・コスト/メトリクス・終了理由を運び、ループの
一生が事後解析できるだけの情報を残す（report.md S5 Phase 2 成功条件 (b)
「全終了理由が journal に残り事後解析できる」）。

イベントは sink へ流す。sink は ``emit(event) -> None`` だけを持つ最小の口で、
claude-org の ``journal_append`` と同じ「1 行 1 イベントの追記」を地で行く
:class:`JsonlEventSink`（journal 風 event sink）、テスト/インメモリ向けの
:class:`ListSink`、任意の関数へ橋渡しする :class:`CallableSink` を備える。

この層は **ループコアにのみ依存** する（state.db 永続化詳細には密結合しない、
report.md S4.6）。時刻はループの注入クロック由来の ``elapsed`` のみを使い、
イベントは与えられた run に対して決定的になる（:mod:`loop_agent.progress`
と同じ方針）。
"""

from __future__ import annotations

import json
import math
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# イベント種別（discriminator）。読み手が文字列リテラルを散在させずに filter
# できるよう定数化する。task 指定の loop_begin / loop_step / loop_end に一致。
LOOP_BEGIN = "loop_begin"
LOOP_STEP = "loop_step"
LOOP_END = "loop_end"


def _jsonable(value: Any) -> Any:
    """任意の値を JSON が表現できる形へ best-effort 変換する。

    スカラと JSON ネイティブのコンテナはそのまま、それ以外（独自の観測オブジェクト
    など）は ``repr`` で文字列化し、1 つの変な値がイベント全体を壊さないようにする。
    :func:`loop_agent.progress._to_jsonable` と同じ方針で、保存される形を予測可能に
    する（``json.dumps(default=...)`` を eager に適用したもの）。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)


@dataclass(frozen=True)
class LoopEvent:
    """1 つの構造化ループイベント。

    ``kind`` は :data:`LOOP_BEGIN` / :data:`LOOP_STEP` / :data:`LOOP_END` のいずれか。
    ``iteration`` は反復番号（begin では 0、step では 0 始まりの完了ステップ番号、
    end では総反復数）。``elapsed`` はループ開始からの秒数（注入クロック由来で
    決定的）。``payload`` は kind 固有のフィールド（メトリクス・終了理由など）。
    """

    kind: str
    iteration: int
    elapsed: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 化しやすいフラットな dict へ畳む（sink のシリアライズ用）。"""
        return {
            "kind": self.kind,
            "iteration": self.iteration,
            "elapsed": self.elapsed,
            **self.payload,
        }


@runtime_checkable
class EventSink(Protocol):
    """構造化イベントを受け取る最小の口。

    実装は :class:`LoopEvent` を 1 つ受け取り、自分のやり方で記録する。観測層は
    emit を best-effort 呼び出しする（sink の例外でループを殺さない、
    :class:`~loop_agent.observe.LoopObserver` を参照）ので、実装は理想的には
    例外を投げないことが望ましい。
    """

    def emit(self, event: LoopEvent) -> None:
        ...


# sink の emit が失敗したときの扱いを差し替えるためのフック（既定は warn）。
# 観測層（:mod:`loop_agent.observe`）でもこの型を共有する。
SinkErrorHandler = Callable[[EventSink, LoopEvent, BaseException], None]


@dataclass
class ListSink:
    """受け取ったイベントをメモリ上の list に貯める sink（テスト/インメモリ向け）。"""

    events: list[LoopEvent] = field(default_factory=list)

    def emit(self, event: LoopEvent) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[LoopEvent]:
        """指定 kind のイベントだけを書き込み順で返す小さなヘルパ。"""
        return [e for e in self.events if e.kind == kind]


class CallableSink:
    """任意の ``callable(dict) -> None`` へイベントを橋渡しする sink。

    ロガーや既存の ``journal_append`` 風関数へそのまま流したいときの最小アダプタ。
    イベントは :meth:`LoopEvent.to_dict` 済みの dict として渡す。
    """

    def __init__(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._fn = fn

    def emit(self, event: LoopEvent) -> None:
        self._fn(event.to_dict())


class JsonlEventSink:
    """追記専用の JSON Lines sink（claude-org ``journal_append`` の file 版）。

    1 イベント 1 行で追記し、行ごとに flush するので、外部の観測者（やクラッシュ後の
    読み手）は進行をそのまま見られる。各行が独立に parse 可能な完全レコードなので、
    追記が耐久性の単位になる。クラッシュは末尾の部分行 1 つを失うだけで、それ以前の
    イベントは全て読める（:func:`read_events` がその末尾を許容する）。

    state.db には依存しない。観測を emit 層として独立に保つための、最小で自己完結な
    journal 風 sink である（report.md S4.6: 観測は emit 層として独立、#11 と並列可）。
    """

    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self.path = Path(path)
        # 親ディレクトリを先に作り、最初の追記が「フォルダ無し」で失敗しないように
        # する（ファイル自体は最初の write で遅延生成）。
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: LoopEvent) -> None:
        line = json.dumps(
            _jsonable(event.to_dict()),
            ensure_ascii=False,
            allow_nan=False,
            default=repr,
        )
        # open-append-flush をレコード毎に行うことでライフサイクルを単純化し
        # （閉じるハンドル無し）、行を耐久性の単位にする。
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()


def read_events(path: "str | os.PathLike[str]") -> list[dict[str, Any]]:
    """JSONL イベントファイルを書き込み順に読み戻す。

    空行は飛ばす。許容する唯一の壊れ方は「末尾レコードの途中切れ」――クラッシュで
    途中まで追記された、改行終端を欠く最終行だけを落とす。改行終端された壊れた行や、
    末尾以外の壊れた行は本物の不整合として送出し、サイレントなデータ欠落でバグを
    隠さない。ファイルが無ければ空 list を返す（:func:`loop_agent.progress.read_progress`
    と同方針で、対象は別フォーマット=イベント列）。
    """
    p = Path(path)
    if not p.exists():
        return []

    text = p.read_text(encoding="utf-8")
    # writer の終端は常に '\n'。終端を欠く末尾だけが「書きかけ（クラッシュ切れ）」の
    # 署名。終端済みの壊れた最終行は本物の破損として raise する。
    final_is_truncated = bool(text) and not text.endswith("\n")

    records: list[dict[str, Any]] = []
    # '\n' のみで分割する（writer の framing そのもの）。``str.splitlines`` は
    # U+2028 / U+2029 / U+0085 でも切ってしまい、ensure_ascii=False の json.dumps が
    # 文字列値中にそれらをそのまま出すため、1 レコードが 2 つに裂けて誤って破損扱いに
    # なりうる。末尾改行が残す '' は strip フィルタで落ちる。
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for idx, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if idx == len(lines) - 1 and final_is_truncated:
                break  # 終端を欠く末尾 1 行だけ許容して落とす
            raise
    return records


def fan_out(
    sinks: "tuple[EventSink, ...]",
    event: LoopEvent,
    *,
    on_error: Optional[SinkErrorHandler] = None,
) -> None:
    """1 イベントを複数 sink へ best-effort で配る。

    sink 単位で例外を捕捉し、ループを観測の都合で殺さない（観測は best-effort）。
    既定では失敗を ``warnings.warn`` で可視化し、サイレントには握り潰さない。
    ``on_error(sink, event, exc)`` を渡せば挙動を差し替えられる（テストで厳格化する等）。
    """
    for sink in sinks:
        try:
            sink.emit(event)
        except Exception as exc:  # noqa: BLE001 - 観測は best-effort
            if on_error is not None:
                on_error(sink, event, exc)
            else:
                warnings.warn(
                    f"event sink {type(sink).__name__} failed to emit "
                    f"{event.kind!r}: {type(exc).__name__}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
