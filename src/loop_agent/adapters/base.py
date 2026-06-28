"""アダプタ共通の土台: ``act`` シームが返す結果の形と、プロンプト整形ユーティリティ。

このモジュールは「外部エージェント実行系(Claude Code / Codex 等)を loop-agent の
``act`` フックに繋ぐ」アダプタが共有する **構造的な契約** を 1 か所に集約する。
個々のアダプタ(:mod:`~loop_agent.adapters.claude_code` /
:mod:`~loop_agent.adapters.codex`)は subprocess コマンド・フラグ・token/output 解析
だけが異なり、結果オブジェクトの形(8 フィールド)とプロンプト整形は完全に同型で
ある。重複定義を解消し、新しいアダプタが「同じ契約に従うべき形」をここから参照
できるようにするのが狙い(Issue #52)。

提供物:

- :class:`ActResult` -- アダプタの結果が満たすべき **構造的契約** (Protocol)。
  ``observation`` に載るオブジェクトが持つべきフィールド/メソッドを宣言する。
  ``isinstance`` でも(``runtime_checkable``)構造適合を確かめられる。
- :class:`ActResultBase` -- その契約を満たす具体 dataclass。8 フィールドと
  ``__str__`` を持ち、:class:`~loop_agent.adapters.claude_code.ClaudeCodeResult` /
  :class:`~loop_agent.adapters.codex.CodexResult` はこれを継承して **フィールド定義の
  重複を持たない**。
- :data:`Runner` -- ``subprocess.run`` 互換の実行関数シーム(テストでの注入点)。
- :func:`render_prompt` / :func:`_format_fields` -- ``prompt_template`` を context
  (gather の戻り値や :class:`~loop_agent.state.LoopState`)のフィールドで埋める
  共通整形。

新しいアダプタの書き方は ``docs/adapters/writing-an-adapter.md`` を参照。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# subprocess.run 互換の実行関数シーム(テストで差し替えるための注入点)。
# capture_output / text / timeout / env / cwd / stdin を受け取り、
# ``returncode`` / ``stdout`` / ``stderr`` を持つオブジェクトを返す。
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@runtime_checkable
class ActResult(Protocol):
    """アダプタ 1 回呼び出しの結果が満たすべき構造的契約(``ActOutcome.observation``)。

    :class:`~loop_agent.loop.ActOutcome` 自体は ``failed`` を持たないため、成否や
    生出力といった「verify が判断に使いたい情報」はこの観測オブジェクトに集約する。
    アダプタ間で結果の形を揃えることで、異種アダプタを合成しても verify 側を
    書き換えずに済む(composability)。

    フィールドの意味:

    - ``text`` -- アシスタント応答の本文。``str(result)`` も同じ本文を返す。
    - ``tokens`` -- この呼び出しが消費したトークン総数(予算計上用)。
    - ``failed`` -- 失敗(非 0 終了 / CLI が報告したエラー / timeout / 起動失敗)か。
      例外ではなくこのフラグで失敗を表し、verify が続行/終了を判断できる。
    - ``returncode`` -- 子プロセスの終了コード(起動失敗・timeout では ``None``)。
    - ``error`` -- 失敗時の簡潔なエラー本文(成功時は空文字)。
    - ``stdout`` / ``stderr`` -- 子プロセスの生出力(デバッグ・再解析用)。
    - ``command`` -- 実際に実行したコマンド(引数列)。

    具体実装は :class:`ActResultBase`(およびそれを継承する各アダプタの Result)。

    注意: ``@runtime_checkable`` の ``isinstance`` 判定は **属性名の有無のみ** を見る
    (型も値の妥当性も検査しない。``__str__`` は全オブジェクトが持つので判定に寄与
    しない)。つまり「契約の構造的ドキュメント」であって入力バリデーションではない。
    アダプタ作者は ``isinstance(result, ActResult)`` を「正しい Result である」証明と
    して過信しないこと。
    """

    text: str
    tokens: int
    failed: bool
    returncode: Optional[int]
    error: str
    stdout: str
    stderr: str
    command: tuple[str, ...]

    def __str__(self) -> str:  # テキストとして使われたとき応答本文を返す。
        ...


@dataclass
class ActResultBase:
    """:class:`ActResult` 契約を満たす共通の具体 dataclass(各アダプタの Result の基底)。

    全フィールドに既定値があるので、サブクラスは ``@dataclass`` を付けてドキュメント
    文字列を足すだけでよく(フィールド再定義は不要)、``Result(text=..., tokens=...)``
    のキーワード生成・``str(result)`` -> 本文 がそのまま使える。新しいアダプタの
    Result もこれを継承すれば 8 フィールドの形が自動的に揃う。
    """

    text: str = ""
    tokens: int = 0
    failed: bool = False
    returncode: Optional[int] = None
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()

    def __str__(self) -> str:  # テキストとして使われたとき応答本文を返す。
        return self.text


def _format_fields(context: Any) -> dict[str, Any]:
    """``prompt_template.format(**...)`` に渡す名前付きフィールドを context から作る。

    - Mapping -> そのままのキー(``{"prompt": ...}`` など)
    - dataclass(例: :class:`~loop_agent.state.LoopState`)-> 各フィールド名
      (``iteration`` / ``tokens_used`` / ``elapsed`` ... をテンプレートに埋められる)
    - str -> ``{"prompt": <その文字列>}``(プロンプト直渡しの最短経路)
    - それ以外で ``__dict__`` を持つ -> その属性
    - 最後の保険 -> ``{"prompt": <context>}``
    """
    if isinstance(context, Mapping):
        return dict(context)
    if is_dataclass(context) and not isinstance(context, type):
        return {f.name: getattr(context, f.name) for f in fields(context)}
    if isinstance(context, str):
        return {"prompt": context}
    if hasattr(context, "__dict__"):
        return dict(vars(context))
    return {"prompt": context}


def render_prompt(template: str, context: Any) -> str:
    """``template`` を context のフィールドで埋めて最終プロンプト文字列を返す。

    テンプレートが context に無いフィールドを参照していた場合は、何が無くて何が
    使えるのかを示す :class:`KeyError` を送出する(既定の ``"{prompt}"`` に対して
    ``prompt`` を渡し忘れた、といった取り違えをすぐ気付けるようにする)。
    """
    field_map = _format_fields(context)
    try:
        return template.format(**field_map)
    except KeyError as exc:  # .format は欠落キーを KeyError(key) で投げる。
        missing = exc.args[0] if exc.args else exc
        raise KeyError(
            f"prompt_template {template!r} references {missing!r}, "
            f"not present in context fields {sorted(field_map)}; "
            "supply it via the gather hook (e.g. gather=lambda s: {'prompt': ...}) "
            "or adjust prompt_template to the available fields"
        ) from exc
