"""Claude Code (headless ``claude --print``) を ``act`` フックに繋ぐアダプタ。

:class:`ClaudeCodeAct` は反復ごとに ``claude --print <prompt>`` を subprocess で
1 回起動し、その応答を :class:`ActOutcome` に詰めて返す。これにより
``run_loop`` の 1 行(``act=ClaudeCodeAct(...)``)で「Claude Code 経由でループを
回す」ことができる(report.md S4.4 の act シーム / Issue #32)。

設計上の約束(loop コアの性質を壊さないため):

- **例外でループを殺さない**: timeout 超過・非 0 終了・実行ファイル不在は、例外を
  送出せず ``failed=True`` の :class:`ClaudeCodeResult` を観測に載せた
  :class:`ActOutcome` として graceful に返す。検証(verify)側がこの ``failed`` を
  見て続行/終了を決められる。境界で評価される ``Timeout`` / ``MaxIterations`` は
  常に効く(report.md S4.4 の while-guard 設計)。
- **token を予算に積む**: 応答(``--output-format json`` の ``usage``、無ければ
  stdout/stderr のフォールバック解析)からトークン数を取り出し、
  ``ActOutcome.tokens`` に載せる。driver がこれを ``state.tokens_used`` に積むので、
  :class:`~loop_agent.conditions.TokenBudget` がそのまま効く。
- **auth は claude CLI に委譲**: 子プロセスは既定で起動側の ``os.environ`` を継承
  する。これにより「既存の claude CLI セッション(~/.claude のログイン)」が第一義に
  使われ、``ANTHROPIC_API_KEY`` が環境にあれば CLI 側のフォールバックとして働く。
  ``env`` を渡すとこの環境へ上書きマージする(秘匿値の注入はこの経路で行う)。

subprocess を使わないテスト/デモ用には :class:`MockClaudeCodeAct` を使う。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from ..loop import ActOutcome

# subprocess.run 互換の実行関数シーム(テストで差し替えるための注入点)。
# capture_output / text / timeout / env / cwd を受け取り、
# ``returncode`` / ``stdout`` / ``stderr`` を持つオブジェクトを返す。
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Mock の各応答に許す形。str はそのまま応答テキスト、dict は ClaudeCodeResult の
# フィールド、ClaudeCodeResult はそのまま使う。
MockResponse = Union[str, Mapping[str, Any], "ClaudeCodeResult"]


@dataclass
class ClaudeCodeResult:
    """1 回の Claude Code 呼び出しの構造化結果(``ActOutcome.observation`` に載る)。

    ``ActOutcome`` 自体は ``failed`` を持たないため、成否や生出力といった
    「verify が判断に使いたい情報」はこの観測オブジェクトに集約する。
    ``str(result)`` は応答テキスト(``text``)を返すので、テキストとして直接
    扱う既存コードとも素直に繋がる。
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


def _sum_token_fields(usage: Mapping[str, Any]) -> int:
    """``usage`` マップから ``*tokens*`` を名前に含む整数値を合計する。

    ``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` を漏れなく拾い、将来増えるトークン種別にも追従する。
    予算(:class:`~loop_agent.conditions.TokenBudget`)は「処理した総トークン」で
    切りたいので、種別を区別せず総和を取る。
    """
    return sum(
        value
        for key, value in usage.items()
        if isinstance(value, bool) is False
        and isinstance(value, int)
        and "tokens" in key.lower()
    )


def _try_json(text: str) -> Any:
    """``text`` 全体、もしくは(stream-json 用に)各行を JSON として読む試み。

    - まず全体を 1 つの JSON として読む(``--output-format json`` の単一結果)。
    - 失敗したら行単位で走査し、``usage`` を持つ最後のオブジェクトを返す
      (``--output-format stream-json`` の最終 result 行を拾うため)。
    どれも JSON でなければ ``None``。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        pass
    found: Any = None
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            found = obj
    return found


# usage が構造化 JSON で取れなかったとき用の、人間可読/部分出力向けフォールバック。
# 代表的なキーを 1 つずつ(最初の出現のみ)拾って合算する。stream-json の途中行や
# modelUsage の内訳まで貪欲に拾うと二重計上しうるため、敢えて各キー先頭一致に絞る。
_TOKEN_FIELD_RES = tuple(
    re.compile(rf'"{key}"\s*:\s*(\d+)')
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
)


def parse_tokens(stdout: str, stderr: str = "") -> int:
    """``claude`` の出力からトークン総数を取り出す(取れなければ 0)。

    優先順位:

    1. stdout を JSON として読み、``usage`` オブジェクトのトークン値を合算する
       (``--output-format json`` / ``stream-json``)。``modelUsage`` 等の内訳は
       読まず、トップレベル ``usage`` のみを見るので二重計上しない。
    2. JSON にならない場合は stdout -> stderr の順に、代表トークンキーの最初の
       出現を正規表現で拾って合算する(debug 出力やテキスト混在への保険)。

    いずれも見つからなければ ``0`` を返す(テキスト出力で usage が無いのは正常)。
    """
    obj = _try_json(stdout)
    if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
        return _sum_token_fields(obj["usage"])

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
    return 0


def _parse_result(stdout: str, stderr: str) -> tuple[str, int, bool]:
    """応答テキスト・トークン数・(CLI が報告する)エラーフラグを取り出す。

    ``--output-format json`` の結果なら ``result`` を本文、``is_error`` を
    エラー判定、``usage`` をトークン源として使う。JSON でなければ stdout を本文と
    し、トークンは :func:`parse_tokens` のフォールバックで拾う。
    """
    obj = _try_json(stdout)
    if isinstance(obj, dict):
        text = obj.get("result")
        if not isinstance(text, str):
            text = stdout
        usage = obj.get("usage")
        tokens = _sum_token_fields(usage) if isinstance(usage, dict) else parse_tokens(stdout, stderr)
        is_error = bool(obj.get("is_error", False))
        return text, tokens, is_error
    return stdout, parse_tokens(stdout, stderr), False


@dataclass
class ClaudeCodeAct:
    """Claude Code を headless 起動する ``act`` フック。

    Args:
        allowed_tools: ``--allowed-tools`` に渡すツール名列(例 ``["Read", "Edit"]``)。
            ``None`` なら付けない(CLI 既定に従う)。
        timeout: 1 回の呼び出しに課す上限秒。超過は子プロセスを kill し、
            ``failed=True`` の結果で graceful に返す(例外を投げない)。
        prompt_template: 最終プロンプトを組み立てる ``str.format`` テンプレート。
            既定 ``"{prompt}"`` は context(gather の戻り値)に ``prompt`` がある前提。
            ``LoopState`` をそのまま context にするなら ``"... iter={iteration}"`` の
            ように state のフィールドを埋め込める。
        model: ``--model``(``opus`` / ``sonnet`` などのエイリアスも可)。``None`` で既定。
        permission_mode: ``--permission-mode``
            (``default`` / ``acceptEdits`` / ``bypassPermissions`` など)。``None`` で既定。
        env: 子プロセス環境への上書きマージ。``None`` なら ``os.environ`` をそのまま継承
            (既存 claude セッション + ``ANTHROPIC_API_KEY`` フォールバックが効く)。
        output_format: ``--output-format``。既定 ``"json"``(usage を含む単一結果が
            得られトークン解析が確実)。``"text"`` にすると本文のみ(tokens は 0 になりがち)。
        claude_bin: 実行ファイル名/パス(既定 ``"claude"``)。テストで差し替え可。
        extra_args: 上記以外に渡したい追加フラグ(プロンプトの手前に挿入)。
        cwd: 子プロセスの作業ディレクトリ。``None`` で現在のディレクトリ。
        runner: ``subprocess.run`` 互換の実行関数(テスト用の注入点)。``None`` で
            ``subprocess.run`` を使う。
    """

    allowed_tools: Optional[Sequence[str]] = None
    timeout: float = 600.0
    prompt_template: str = "{prompt}"
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    env: Optional[Mapping[str, str]] = None
    output_format: str = "json"
    claude_bin: str = "claude"
    extra_args: Sequence[str] = ()
    cwd: Optional[str] = None
    runner: Optional[Runner] = None

    def build_command(self, prompt: str) -> list[str]:
        """この呼び出しで実行する ``claude`` コマンド(引数列)を組み立てる。"""
        cmd: list[str] = [self.claude_bin, "--print"]
        if self.output_format:
            cmd += ["--output-format", self.output_format]
        if self.model:
            cmd += ["--model", self.model]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            # CLI はカンマ/空白区切りを受け付ける。ツール指定に空白を含む
            # (例 "Bash(git *)")場合でも 1 トークンに保つためカンマで連結する。
            cmd += ["--allowed-tools", ",".join(self.allowed_tools)]
        cmd += list(self.extra_args)
        cmd.append(prompt)
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
                timeout=self.timeout,
                env=self._build_env(),
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            # 子は kill 済み。例外でループを殺さず failed として返す。
            result = ClaudeCodeResult(
                failed=True,
                error=f"timeout ({self.timeout:g}s)",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)
        except OSError as exc:
            # claude 実行ファイルが見つからない / 実行権限が無い等の起動失敗
            # (FileNotFoundError / PermissionError は OSError)。これも graceful に
            # failed で返す(境界の MaxIterations 等で必ず止まる)。
            result = ClaudeCodeResult(
                failed=True,
                error=f"could not launch {self.claude_bin!r}: {exc}",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        text, tokens, is_error = _parse_result(stdout, stderr)
        returncode = proc.returncode
        failed = returncode != 0 or is_error
        error = ""
        if failed:
            error = (stderr.strip() or text.strip() or f"exit={returncode}")

        result = ClaudeCodeResult(
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
class MockClaudeCodeAct:
    """subprocess を使わない in-memory な ``ClaudeCodeAct`` 代替(テスト/デモ用)。

    ``responses`` の各要素を順に返す。要素は次のいずれか:

    - ``str`` -> その文字列を ``text``(成功・tokens 0)とする
    - ``Mapping`` -> :class:`ClaudeCodeResult` のフィールドとして展開
      (例 ``{"text": "...", "tokens": 1200}`` や ``{"failed": True, "error": "..."}``)
    - :class:`ClaudeCodeResult` -> そのまま使う

    応答を使い切ったら最後の応答に張り付く(``CandidateApplier`` と同じ「現状の
    最善手を返し続ける」挙動。``MaxIterations`` 等の境界で安全に止まる)。
    レンダリング済みプロンプトは :attr:`prompts` に記録され、テストから検証できる。
    ``prompt_template`` は :class:`ClaudeCodeAct` と同じ意味で、プレースホルダ挙動を
    subprocess 無しで再現する。
    """

    responses: Sequence[MockResponse]
    prompt_template: str = "{prompt}"
    prompts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.responses:
            raise ValueError("MockClaudeCodeAct requires at least one response")
        self._responses = [self._coerce(r) for r in self.responses]

    @staticmethod
    def _coerce(response: MockResponse) -> ClaudeCodeResult:
        if isinstance(response, ClaudeCodeResult):
            return response
        if isinstance(response, str):
            return ClaudeCodeResult(text=response)
        if isinstance(response, Mapping):
            return ClaudeCodeResult(**response)
        raise TypeError(
            "MockClaudeCodeAct responses must be str, Mapping, or ClaudeCodeResult, "
            f"got {type(response).__name__}"
        )

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self._responses) - 1)
        result = self._responses[index]
        return ActOutcome(observation=result, tokens=result.tokens)
