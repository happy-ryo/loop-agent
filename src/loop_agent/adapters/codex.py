"""Codex CLI (headless ``codex exec``) を ``act`` フックに繋ぐアダプタ。

:class:`CodexAct` は反復ごとに ``codex exec -m <model> -c
model_reasoning_effort=<effort> -- <prompt>`` を subprocess で 1 回起動し、その
応答を :class:`ActOutcome` に詰めて返す。これにより ``run_loop`` の 1 行
(``act=CodexAct(...)``)で「Codex 経由でループを回す」ことができる
(report.md S4.4 の act シーム / Issue #49)。:class:`ClaudeCodeAct`
(``loop_agent.adapters.claude_code``、PR #47)と完全同型で、差分は subprocess
コマンド・フラグ・token/output 解析だけである。

設計上の約束(loop コアの性質を壊さないため。ClaudeCodeAct と同一):

- **例外でループを殺さない**: timeout 超過・非 0 終了・実行ファイル不在は、例外を
  送出せず ``failed=True`` の :class:`CodexResult` を観測に載せた
  :class:`ActOutcome` として graceful に返す。検証(verify)側がこの ``failed`` を
  見て続行/終了を決められる。境界で評価される ``Timeout`` / ``MaxIterations`` は
  常に効く(report.md S4.4 の while-guard 設計)。
- **token を予算に積む**: 応答(``--json`` の JSONL に含まれる ``turn.completed``
  の ``usage``、無ければ stdout/stderr の正規表現フォールバック解析)から
  トークン数を取り出し ``ActOutcome.tokens`` に載せる。driver がこれを
  ``state.tokens_used`` に積むので :class:`~loop_agent.conditions.TokenBudget`
  がそのまま効く。
- **auth は codex CLI に委譲**: 子プロセスは既定で起動側の ``os.environ`` を継承
  する。これにより「既存の codex CLI セッション(``~/.codex`` のログイン)」が
  第一義に使われ、``OPENAI_API_KEY`` が環境にあれば CLI 側のフォールバックとして
  働く。``env`` を渡すとこの環境へ上書きマージする(秘匿値の注入はこの経路で行う)。

Codex 固有の差分(ClaudeCodeAct との違い):

- token 種別の意味が違う。Codex/OpenAI の ``usage`` は ``cached_input_tokens`` が
  ``input_tokens`` の、``reasoning_output_tokens`` が ``output_tokens`` の **部分集合**
  なので、ClaudeCodeAct のように全 ``*tokens*`` を合算すると二重計上になる。
  総処理量は ``input_tokens + output_tokens`` のみで取る(:func:`_sum_codex_tokens`)。
- 応答本文は単一フィールドではなく JSONL イベント列の ``agent_message`` に乗る。
  最後の ``agent_message`` の ``text`` を本文として採る(:func:`_parse_result`)。
- 子の標準入力は ``DEVNULL`` に固定する。codex は stdin が pipe だと「追加入力」を
  読みに行くため、headless ループで親 stdin が pipe/閉端の場合にハング・誤読する
  のを防ぐ(プロンプトは ``--`` 後の位置引数で確定済み)。

subprocess を使わないテスト/デモ用には :class:`MockCodexAct` を使う。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from ..errors import ConfigError
from ..loop import ActOutcome
# 結果の形・プロンプト整形・Runner シームはアダプタ共通の土台(base)に集約済み。
# ``render_prompt`` / ``Runner`` は base から直接参照する(claude_code 経由の
# re-import を避け、依存をフラットに保つ)。``render_prompt`` は本モジュール名前空間
# にも再公開して既存の ``adapters.codex.render_prompt`` 参照を壊さない(後方互換)。
from .base import ActResultBase, Runner, render_prompt

__all__ = [
    "CodexAct",
    "CodexResult",
    "MockCodexAct",
    "Runner",
    "parse_tokens",
    "render_prompt",
]

# Mock の各応答に許す形。str はそのまま応答テキスト、dict は CodexResult の
# フィールド、CodexResult はそのまま使う。
MockResponse = Union[str, Mapping[str, Any], "CodexResult"]


def _default_codex_bin() -> str:
    """Return the executable name/path to use for the default Codex CLI."""
    if os.name == "nt":
        # npm installs Codex as codex.cmd/codex.ps1 on Windows. subprocess with
        # shell=False does not resolve PowerShell scripts, so prefer the cmd shim.
        return shutil.which("codex.cmd") or "codex.cmd"
    return "codex"


@dataclass
class CodexResult(ActResultBase):
    """1 回の Codex 呼び出しの構造化結果(``ActOutcome.observation`` に載る)。

    :class:`~loop_agent.adapters.base.ActResultBase` を継承し 8 フィールド
    (``text`` / ``tokens`` / ``failed`` / ``returncode`` / ``error`` / ``stdout`` /
    ``stderr`` / ``command``)と ``__str__`` をそのまま受け継ぐ。``str(result)`` は
    応答テキスト(``text``)を返すので、テキストとして直接扱う既存コードとも素直に
    繋がる(:class:`~loop_agent.adapters.claude_code.ClaudeCodeResult` と同型で
    :class:`~loop_agent.adapters.base.ActResult` 契約に適合)。
    """


def _iter_json_events(text: str) -> "list[dict[str, Any]]":
    """``codex exec --json`` の JSONL を行ごとに parse した dict 列を返す。

    JSON にならない行(人間可読のステータス行など)は黙って読み飛ばす。
    """
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _sum_codex_tokens(usage: Mapping[str, Any]) -> int:
    """Codex の ``usage`` から総処理トークン数を取り出す。

    Codex/OpenAI の ``usage`` は ``cached_input_tokens`` が ``input_tokens`` の、
    ``reasoning_output_tokens`` が ``output_tokens`` の **部分集合** なので、
    総処理量は ``input_tokens + output_tokens`` のみを足す(部分集合フィールドを
    加えると二重計上になる)。予算
    (:class:`~loop_agent.conditions.TokenBudget`)は「処理した総トークン」で
    切りたいだけなので、種別の内訳は問わずこの 2 つの総和で十分。

    詳細な内訳(input/output)が片方も無く ``total_tokens`` だけを持つ usage
    (一部の provider/CLI サマリ)では ``total_tokens`` にフォールバックする。
    こうしないと CLI が usage を報告したのに ``tokens=0`` になり TokenBudget が
    その呼び出しを勘定しない。internal split があるときは二重計上を避けるため
    ``total_tokens`` は使わない(input/output の総和を優先)。
    """
    total = 0
    have_detail = False
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, bool) is False and isinstance(value, int):
            total += value
            have_detail = True
    if have_detail:
        return total
    fallback = usage.get("total_tokens")
    if isinstance(fallback, bool) is False and isinstance(fallback, int):
        return fallback
    return 0


# usage が JSONL で取れなかったとき用のフォールバック。代表キーの最初の出現のみを
# 拾う。先頭の二重引用符でアンカーするので ``cached_input_tokens`` /
# ``reasoning_output_tokens`` (部分集合)には誤マッチせず二重計上を避ける。
_TOKEN_FIELD_RES = (
    re.compile(r'"input_tokens"\s*:\s*(\d+)'),
    re.compile(r'"output_tokens"\s*:\s*(\d+)'),
)
# input/output が拾えなかったときの最後の保険(total_tokens のみのサマリ向け)。
_TOTAL_TOKENS_RE = re.compile(r'"total_tokens"\s*:\s*(\d+)')


def parse_tokens(stdout: str, stderr: str = "") -> int:
    """``codex`` の出力からトークン総数を取り出す(取れなければ 0)。

    優先順位:

    1. stdout を JSONL として読み、``usage`` を持つ最後のイベント
       (``turn.completed``)の ``input_tokens + output_tokens`` を採る。
       単一 turn の exec ではこれが総量。
    2. JSONL に usage が無い場合は stdout を優先して(無ければ stderr を)見て、
       代表トークンキーの最初の出現を正規表現で拾い **そのソース内で** 合算する。
       ソース間では合算せず、最初にヒットしたソースの値を返す。両ソースが
       ともにトークンを出力した場合の二重計上を避けるためで、ClaudeCodeAct と
       同じ挙動(codex の usage は ``--json`` の stdout に出るのが既定)。

    どちらの経路も input/output が無く ``total_tokens`` のみのときはそれを使う
    (:func:`_sum_codex_tokens` / 正規表現フォールバック双方)。いずれも見つから
    なければ ``0`` を返す。
    """
    last_usage: Optional[Mapping[str, Any]] = None
    for obj in _iter_json_events(stdout):
        usage = obj.get("usage")
        if isinstance(usage, dict):
            last_usage = usage  # 最後の usage(累積後の最終値)を採用。
    if last_usage is not None:
        return _sum_codex_tokens(last_usage)

    for source in (stdout, stderr):
        if not source:
            continue
        total = 0
        hit = False
        for pattern in _TOKEN_FIELD_RES:
            match = pattern.search(source)
            if match is not None:
                total += int(match.group(1))
                hit = True
        if hit:
            return total
        # input/output が無ければ total_tokens のみのサマリにフォールバック。
        total_match = _TOTAL_TOKENS_RE.search(source)
        if total_match is not None:
            return int(total_match.group(1))
    return 0


def _first_str(obj: Mapping[str, Any], *keys: str) -> Optional[str]:
    """``obj`` から ``keys`` を順に見て最初の ``str`` 値を返す(無ければ ``None``)。"""
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _norm_type(value: Any) -> Optional[str]:
    """イベント/アイテムの ``type`` を正規化する(``.`` -> ``_``)。

    codex の ``--json`` はバージョンで dotted (``item.completed``) と snake_case
    (``item_completed`` / ``task_complete``)が揺れるため、``.`` を ``_`` に寄せて
    どちらの綴りでも同じ分岐で拾えるようにする。``str`` でなければ ``None``。
    """
    return value.replace(".", "_") if isinstance(value, str) else None


def _extract_text(events: "list[dict[str, Any]]") -> Optional[str]:
    """JSONL イベント列から最終アシスタント応答を取り出す(無ければ ``None``)。

    codex の ``--json`` スキーマは CLI バージョンで揺れるため、代表的な形をすべて
    拾う(どれか 1 つでも取れれば本文として返す)。イベント型は dotted /
    snake_case の両綴りを :func:`_norm_type` で正規化して扱う:

    - ``item.completed`` / ``item_completed`` で item 型が ``agent_message`` ->
      ``item.text``(codex 0.129 で確認した現行形)
    - 直接の ``agent_message`` イベント -> ``message`` / ``text``(別 ``--json`` 形)
    - ストリーミングの ``agent_message_content_delta`` / ``agent_message_delta``
      -> ``delta`` / ``text`` を連結(consolidated 形が無い場合の保険)
    - 完了イベント(``task_complete`` / ``turn.completed`` 等)の
      ``last_agent_message`` フィールド(type を問わず拾う)

    優先順位は「完全な本文 > last_message フィールド > delta 連結」。完全な本文
    (item-completed / 直接 ``agent_message``)が 1 つでも取れたらそれを最終応答
    とし(最後の出現を採用)、無いときだけ last_message / delta にフォールバックする。
    """
    text: Optional[str] = None
    last_message: Optional[str] = None
    delta_parts: list[str] = []
    for obj in events:
        event_type = _norm_type(obj.get("type"))
        if event_type == "item_completed":
            item = obj.get("item")
            if isinstance(item, dict) and _norm_type(item.get("type")) == "agent_message":
                candidate = _first_str(item, "text", "message")
                if candidate is not None:
                    text = candidate  # 最後の agent_message を最終応答とする。
        elif event_type == "agent_message":
            candidate = _first_str(obj, "message", "text")
            if candidate is not None:
                text = candidate
        elif event_type in ("agent_message_content_delta", "agent_message_delta"):
            delta = _first_str(obj, "delta", "text", "content")
            if delta is not None:
                delta_parts.append(delta)
        # 完了イベントが最終メッセージを別フィールドで持つ形(type を問わず拾う)。
        candidate = _first_str(obj, "last_agent_message")
        if candidate is not None:
            last_message = candidate
    if text is not None:
        return text
    if last_message is not None:
        return last_message
    if delta_parts:
        return "".join(delta_parts)
    return None


def _is_error_event(event_type: Any) -> bool:
    """``error`` イベント、もしくは ``*.failed`` 型(``turn.failed`` 等)か判定する。

    dotted / snake_case 両綴りに対応するため :func:`_norm_type` で正規化してから
    ``error`` 完全一致と ``_failed`` 末尾一致で判定する。
    """
    norm = _norm_type(event_type)
    return norm == "error" or (norm is not None and norm.endswith("_failed"))


def _parse_result(stdout: str, stderr: str) -> tuple[str, int, bool, str]:
    """応答テキスト・トークン数・エラーフラグ・エラー本文を取り出す。

    ``--json`` の JSONL なら :func:`_extract_text` で最終アシスタント応答を本文、
    ``usage`` をトークン源とし、``error`` / ``*.failed`` イベントの有無をエラー判定に
    使う。エラーイベントが ``message`` 等を持てばそれを ``error_message`` として返す
    (呼び出し側が JSONL 全文ではなく簡潔なエラー本文を ``CodexResult.error`` に
    載せられるようにする)。JSONL でなければ(あるいは本文が取れなければ)stdout を
    本文とし、トークンは :func:`parse_tokens` のフォールバックで拾う。
    """
    events = _iter_json_events(stdout)
    if events:
        is_error = False
        error_message = ""
        for obj in events:
            if _is_error_event(obj.get("type")):
                is_error = True
                if not error_message:  # 最初のエラーイベントの本文を採用。
                    message = _first_str(obj, "message", "error", "text")
                    if message is not None:
                        error_message = message
        text = _extract_text(events)
        if text is None:
            text = stdout
        return text, parse_tokens(stdout, stderr), is_error, error_message
    return stdout, parse_tokens(stdout, stderr), False, ""


@dataclass
class CodexAct:
    """Codex CLI を headless 起動する ``act`` フック(:class:`ClaudeCodeAct` と同型)。

    Args:
        model: ``-m/--model``。既定 ``"gpt-5.5"``。ChatGPT アカウント運用では
            ``gpt-5.5`` 系を明示する(API キー専用 surface は避ける)。
        effort: ``-c model_reasoning_effort=<effort>`` に渡す推論強度
            (``"low"`` / ``"medium"`` / ``"high"`` など)。既定 ``"medium"``。
        timeout: 1 回の呼び出しに課す上限秒。超過は子プロセスを kill し、
            ``failed=True`` の結果で graceful に返す(例外を投げない)。
        prompt_template: 最終プロンプトを組み立てる ``str.format`` テンプレート。
            既定 ``"{prompt}"`` は context(gather の戻り値)に ``prompt`` がある前提。
            ``LoopState`` をそのまま context にするなら ``"... iter={iteration}"`` の
            ように state のフィールドを埋め込める。
        env: 子プロセス環境への上書きマージ。``None`` なら ``os.environ`` をそのまま
            継承(既存 codex セッション + ``OPENAI_API_KEY`` フォールバックが効く)。
        allowed_args: 上記以外に渡したい追加フラグ列(プロンプト ``--`` の手前に挿入)。
            ``["--add-dir", "/path"]`` のように codex の任意フラグを通せる。
        json_output: ``True`` (既定)で ``--json`` を付け JSONL を得る(usage を含み
            トークン解析が確実)。``False`` ならテキスト出力(tokens は 0 になりがち)。
        sandbox: ``-s/--sandbox`` (``read-only`` / ``workspace-write`` /
            ``danger-full-access``)。``None`` で codex 既定に従う。
        skip_git_repo_check: ``True`` (既定)で ``--skip-git-repo-check`` を付け、
            git リポジトリ外でも起動失敗しないようにする(embeddability のため)。
        codex_bin: 実行ファイル名/パス。既定は POSIX で ``"codex"``、Windows で npm の ``codex.cmd`` shim。テストで差し替え可。
        cwd: 子プロセスの作業ディレクトリ。``None`` で現在のディレクトリ。
        runner: ``subprocess.run`` 互換の実行関数(テスト用の注入点)。``None`` で
            ``subprocess.run`` を使う。
    """

    model: str = "gpt-5.5"
    effort: str = "medium"
    timeout: float = 600.0
    prompt_template: str = "{prompt}"
    env: Optional[Mapping[str, str]] = None
    allowed_args: Optional[Sequence[str]] = None
    json_output: bool = True
    sandbox: Optional[str] = None
    skip_git_repo_check: bool = True
    codex_bin: str = field(default_factory=_default_codex_bin)
    cwd: Optional[str] = None
    runner: Optional[Runner] = None

    def build_command(self, prompt: str) -> list[str]:
        """この呼び出しで実行する ``codex exec`` コマンド(引数列)を組み立てる。"""
        cmd: list[str] = [self.codex_bin, "exec"]
        if self.json_output:
            cmd += ["--json"]
        if self.skip_git_repo_check:
            cmd += ["--skip-git-repo-check"]
        if self.model:
            cmd += ["-m", self.model]
        if self.effort:
            cmd += ["-c", f"model_reasoning_effort={self.effort}"]
        if self.sandbox:
            cmd += ["-s", self.sandbox]
        if self.allowed_args:
            cmd += list(self.allowed_args)
        # プロンプトは必ず "--" の後ろに置く。``-i/--image`` や ``--add-dir`` 等の
        # 値を取るオプションが、区切り無しだと直後のプロンプトを「次の値」として
        # 飲み込みうる。POSIX 慣例の "--" でオプション解析を打ち切り、プロンプトを
        # 位置引数に確定させる(ClaudeCodeAct と同パターン)。
        cmd += ["--", prompt]
        return cmd

    def _build_env(self) -> dict[str, str]:
        """子プロセスに渡す環境。``os.environ`` を継承し ``env`` で上書きマージ。"""
        base = dict(os.environ)
        if self.env:
            base.update(self.env)
        return base

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        command = self.build_command(prompt)
        run = self.runner or subprocess.run

        try:
            proc = run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._build_env(),
                cwd=self.cwd,
                # codex は stdin が pipe だと追加入力を読みに行く。プロンプトは
                # "--" 後の位置引数で確定済みなので、DEVNULL に固定してハング/誤読を防ぐ。
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            # 子は kill 済み。例外でループを殺さず failed として返す。
            result = CodexResult(
                failed=True,
                error=f"timeout ({self.timeout:g}s)",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)
        except OSError as exc:
            # codex 実行ファイルが見つからない / 実行権限が無い等の起動失敗
            # (FileNotFoundError / PermissionError は OSError)。これも graceful に
            # failed で返す(境界の MaxIterations 等で必ず止まる)。
            result = CodexResult(
                failed=True,
                error=f"could not launch {self.codex_bin!r}: {exc}",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        text, tokens, is_error, error_message = _parse_result(stdout, stderr)
        returncode = proc.returncode
        failed = returncode != 0 or is_error
        error = ""
        if failed:
            # 簡潔なエラー本文を優先する: stderr -> エラーイベント本文 -> 応答本文 ->
            # 終了コード。これにより error イベント時に JSONL 全文が error に乗らない。
            error = stderr.strip() or error_message or text.strip() or f"exit={returncode}"

        result = CodexResult(
            text=text,
            tokens=tokens,
            failed=failed,
            returncode=returncode,
            error=error,
            stdout=stdout,
            stderr=stderr,
            command=tuple(command),
        )
        # tokens は成否に関わらず計上する(失敗試行も実際にトークンを消費しうる)。
        return ActOutcome(observation=result, tokens=tokens)


@dataclass
class MockCodexAct:
    """subprocess を使わない in-memory な ``CodexAct`` 代替(テスト/デモ用)。

    ``responses`` の各要素を順に返す。要素は次のいずれか:

    - ``str`` -> その文字列を ``text``(成功・tokens 0)とする
    - ``Mapping`` -> :class:`CodexResult` のフィールドとして展開
      (例 ``{"text": "...", "tokens": 1200}`` や ``{"failed": True, "error": "..."}``)
    - :class:`CodexResult` -> そのまま使う

    応答を使い切ったら最後の応答に張り付く(``MockClaudeCodeAct`` と同じ
    「現状の最善手を返し続ける」挙動。``MaxIterations`` 等の境界で安全に止まる)。
    レンダリング済みプロンプトは :attr:`prompts` に記録され、テストから検証できる。
    """

    responses: Sequence[MockResponse]
    prompt_template: str = "{prompt}"
    prompts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.responses:
            raise ConfigError("MockCodexAct requires at least one response")
        self._responses = [self._coerce(r) for r in self.responses]

    @staticmethod
    def _coerce(response: MockResponse) -> CodexResult:
        if isinstance(response, CodexResult):
            return response
        if isinstance(response, str):
            return CodexResult(text=response)
        if isinstance(response, Mapping):
            return CodexResult(**response)
        raise ConfigError(
            "MockCodexAct responses must be str, Mapping, or CodexResult, "
            f"got {type(response).__name__}"
        )

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self._responses) - 1)
        result = self._responses[index]
        return ActOutcome(observation=result, tokens=result.tokens)
