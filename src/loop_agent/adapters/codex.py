"""Adapter connecting Codex CLI (headless ``codex exec``) to the ``act`` hook.

:class:`CodexAct` starts ``codex exec -m <model> -c
model_reasoning_effort=<effort> -- <prompt>`` once per iteration via subprocess and
returns the response inside :class:`ActOutcome`. This lets one line of ``run_loop``
(``act=CodexAct(...)``) run a loop through Codex (the ``act`` seam from report.md S4.4
/ Issue #49). It has the same shape as :class:`ClaudeCodeAct`
(``loop_agent.adapters.claude_code``, PR #47); only subprocess commands, flags, and
token/output parsing differ.

Design commitments that preserve loop-core behavior, matching ClaudeCodeAct:

- **Do not kill the loop with exceptions**: timeout, non-zero exit, or missing
  executable are returned gracefully as :class:`ActOutcome` carrying a
  :class:`CodexResult` with ``failed=True`` instead of raising. The verify side can
  inspect ``failed`` to decide whether to continue or stop. Boundary conditions
  evaluated by ``Timeout`` / ``MaxIterations`` still always apply (the while-guard
  design from report.md S4.4).
- **Account tokens against the budget**: extract token counts from the response
  (``usage`` on ``turn.completed`` in ``--json`` JSONL, or stdout/stderr regex fallback
  parsing) and put them in ``ActOutcome.tokens``. The driver adds this to
  ``state.tokens_used``, so :class:`~loop_agent.conditions.TokenBudget` works
  unchanged.
- **Delegate auth to the codex CLI**: the child process inherits the caller's
  ``os.environ`` by default. This makes an existing codex CLI session (``~/.codex``
  login) the primary auth path, with ``OPENAI_API_KEY`` as a CLI fallback if present.
  Passing ``env`` merges overrides into that environment, which is the path for
  injecting secrets.

Codex-specific differences from ClaudeCodeAct:

- Token kinds have different semantics. In Codex/OpenAI ``usage``,
  ``cached_input_tokens`` is a **subset** of ``input_tokens`` and
  ``reasoning_output_tokens`` is a **subset** of ``output_tokens``. Summing every
  ``*tokens*`` field like ClaudeCodeAct would double-count. Total processing is
  therefore ``input_tokens + output_tokens`` only (:func:`_sum_codex_tokens`).
- Response text is not a single field; it appears in ``agent_message`` JSONL events.
  The final ``agent_message`` ``text`` is used as the body (:func:`_parse_result`).
- Child stdin is fixed to ``DEVNULL``. When stdin is a pipe, codex may try to read
  additional input, which can hang or misread in headless loops when parent stdin is a
  pipe/closed. The prompt is already fixed as a positional argument after ``--``.

Use :class:`MockCodexAct` for tests/demos that should not use subprocesses.
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
# Result shape, prompt rendering, and Runner seam live in the shared adapter base.
# ``render_prompt`` / ``Runner`` are imported directly from base to avoid re-importing
# through claude_code and to keep dependencies flat. ``render_prompt`` is also
# re-exported from this module namespace to preserve existing
# ``adapters.codex.render_prompt`` references.
from .base import ActResultBase, Runner, render_prompt

__all__ = [
    "CodexAct",
    "CodexResult",
    "MockCodexAct",
    "Runner",
    "parse_tokens",
    "render_prompt",
]

# Accepted shapes for each mock response: str is response text as-is, dict expands
# into CodexResult fields, and CodexResult is used directly.
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
    """Structured result for one Codex call, stored in ``ActOutcome.observation``.

    Inherits :class:`~loop_agent.adapters.base.ActResultBase` and therefore reuses the
    8 fields (``text`` / ``tokens`` / ``failed`` / ``returncode`` / ``error`` /
    ``stdout`` / ``stderr`` / ``command``) and ``__str__``. ``str(result)`` returns the
    response text (``text``), so existing code that treats results as text still works.
    It has the same shape as :class:`~loop_agent.adapters.claude_code.ClaudeCodeResult`
    and satisfies the :class:`~loop_agent.adapters.base.ActResult` contract.
    """


def _iter_json_events(text: str) -> "list[dict[str, Any]]":
    """Return dict events parsed line-by-line from ``codex exec --json`` JSONL.

    Lines that are not JSON, such as human-readable status lines, are silently skipped.
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
    """Extract total processed token count from Codex ``usage``.

    In Codex/OpenAI ``usage``, ``cached_input_tokens`` is a **subset** of
    ``input_tokens`` and ``reasoning_output_tokens`` is a **subset** of
    ``output_tokens``. Total processing therefore sums only ``input_tokens +
    output_tokens``; adding subset fields would double-count. Budgets
    (:class:`~loop_agent.conditions.TokenBudget`) only need to cut off total processed
    tokens, so this sum is sufficient regardless of category breakdown.

    If usage has no detailed input/output split and only ``total_tokens`` (some
    provider/CLI summaries), fall back to ``total_tokens``. Otherwise a CLI that
    reported usage would become ``tokens=0`` and TokenBudget would not count the call.
    When an internal split exists, do not use ``total_tokens`` to avoid double-counting;
    prefer the input/output sum.
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


# Fallback when usage was not available from JSONL. Capture only the first occurrence
# of representative keys. Anchoring on the leading quote avoids matching subset fields
# such as ``cached_input_tokens`` / ``reasoning_output_tokens`` and prevents
# double-counting.
_TOKEN_FIELD_RES = (
    re.compile(r'"input_tokens"\s*:\s*(\d+)'),
    re.compile(r'"output_tokens"\s*:\s*(\d+)'),
)
# Final fallback when input/output was not found, for total_tokens-only summaries.
_TOTAL_TOKENS_RE = re.compile(r'"total_tokens"\s*:\s*(\d+)')


def parse_tokens(stdout: str, stderr: str = "") -> int:
    """Extract total tokens from ``codex`` output, or return 0 if unavailable.

    Priority:

    1. Read stdout as JSONL and use ``input_tokens + output_tokens`` from the last
       event with ``usage`` (``turn.completed``). For single-turn exec, this is the
       total.
    2. If JSONL has no usage, inspect stdout first, then stderr if stdout has none.
       Capture the first occurrence of representative token keys with regex and sum
       them **within that source**. Do not sum across sources; return the first source
       with hits. This avoids double-counting if both sources contain tokens and
       matches ClaudeCodeAct behavior (codex usage is normally on stdout with
       ``--json``).

    Both paths use ``total_tokens`` when input/output fields are absent and only
    ``total_tokens`` exists (:func:`_sum_codex_tokens` and regex fallback). Return
    ``0`` if nothing is found.
    """
    last_usage: Optional[Mapping[str, Any]] = None
    for obj in _iter_json_events(stdout):
        usage = obj.get("usage")
        if isinstance(usage, dict):
            last_usage = usage  # Use the last usage value, the final cumulative total.
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
        # Fall back to total_tokens-only summaries when input/output is absent.
        total_match = _TOTAL_TOKENS_RE.search(source)
        if total_match is not None:
            return int(total_match.group(1))
    return 0


def _first_str(obj: Mapping[str, Any], *keys: str) -> Optional[str]:
    """Return the first ``str`` value from ``obj`` across ``keys``, or ``None``."""
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _norm_type(value: Any) -> Optional[str]:
    """Normalize an event/item ``type`` by replacing ``.`` with ``_``.

    codex ``--json`` varies by version between dotted forms (``item.completed``) and
    snake_case forms (``item_completed`` / ``task_complete``). Normalizing ``.`` to
    ``_`` lets the same branch handle both spellings. Return ``None`` for non-strings.
    """
    return value.replace(".", "_") if isinstance(value, str) else None


def _extract_text(events: "list[dict[str, Any]]") -> Optional[str]:
    """Extract the final assistant response from JSONL events, or ``None``.

    The codex ``--json`` schema varies by CLI version, so this captures representative
    shapes and returns the body if any of them are present. Event type dotted and
    snake_case spellings are normalized with :func:`_norm_type`:

    - ``item.completed`` / ``item_completed`` with item type ``agent_message`` ->
      ``item.text`` (current shape observed in codex 0.129)
    - direct ``agent_message`` event -> ``message`` / ``text`` (alternate ``--json``
      shape)
    - streaming ``agent_message_content_delta`` / ``agent_message_delta`` ->
      concatenate ``delta`` / ``text`` as a fallback when there is no consolidated
      shape
    - ``last_agent_message`` on completion events (``task_complete`` /
      ``turn.completed`` / etc.), regardless of type

    Priority is "complete text > last_message field > concatenated deltas". If any
    complete text (item-completed / direct ``agent_message``) is available, the last
    occurrence is the final response. Only then fall back to last_message / deltas.
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
                    text = candidate  # Use the last agent_message as the final response.
        elif event_type == "agent_message":
            candidate = _first_str(obj, "message", "text")
            if candidate is not None:
                text = candidate
        elif event_type in ("agent_message_content_delta", "agent_message_delta"):
            delta = _first_str(obj, "delta", "text", "content")
            if delta is not None:
                delta_parts.append(delta)
        # Some completion events carry the final message in a separate field.
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
    """Return whether this is an ``error`` event or a ``*.failed`` type.

    Normalize with :func:`_norm_type` to handle both dotted and snake_case spellings,
    then check exact ``error`` or ``_failed`` suffix (such as ``turn.failed``).
    """
    norm = _norm_type(event_type)
    return norm == "error" or (norm is not None and norm.endswith("_failed"))


def _parse_result(stdout: str, stderr: str) -> tuple[str, int, bool, str]:
    """Extract response text, token count, error flag, and error text.

    For ``--json`` JSONL, use :func:`_extract_text` for the final assistant response,
    ``usage`` as the token source, and ``error`` / ``*.failed`` event presence for
    error detection. If an error event has ``message`` or similar, return it as
    ``error_message`` so callers can put concise text in ``CodexResult.error`` instead
    of the full JSONL. If output is not JSONL, or the body cannot be extracted, use
    stdout as the body and get tokens through the :func:`parse_tokens` fallback.
    """
    events = _iter_json_events(stdout)
    if events:
        is_error = False
        error_message = ""
        for obj in events:
            if _is_error_event(obj.get("type")):
                is_error = True
                if not error_message:  # Use the first error event body.
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
    """``act`` hook that starts Codex CLI headlessly, shaped like ClaudeCodeAct.

    Args:
        model: ``-m/--model``. Default ``"gpt-5.5"``. For ChatGPT-account operation,
            explicitly use the ``gpt-5.5`` family to avoid API-key-only surfaces.
        effort: reasoning effort passed through ``-c model_reasoning_effort=<effort>``
            (``"low"`` / ``"medium"`` / ``"high"`` / etc.). Default ``"medium"``.
        timeout: maximum seconds for one call. On timeout the child is killed and a
            ``failed=True`` result is returned gracefully, without raising.
        prompt_template: ``str.format`` template for the final prompt. The default
            ``"{prompt}"`` assumes the context (gather return value) has ``prompt``.
            If passing ``LoopState`` directly as context, templates can embed state
            fields such as ``"... iter={iteration}"``.
        env: overrides merged into the child process environment. ``None`` inherits
            ``os.environ`` unchanged, so an existing codex session plus
            ``OPENAI_API_KEY`` fallback works.
        allowed_args: additional flags inserted before the prompt ``--``. This can
            pass arbitrary codex flags such as ``["--add-dir", "/path"]``.
        json_output: ``True`` (default) adds ``--json`` to get JSONL containing usage,
            making token parsing reliable. ``False`` uses text output, where tokens
            tend to be 0.
        sandbox: ``-s/--sandbox`` (``read-only`` / ``workspace-write`` /
            ``danger-full-access``). ``None`` follows the codex default.
        skip_git_repo_check: ``True`` (default) adds ``--skip-git-repo-check`` so codex
            does not fail outside git repositories, for embeddability.
        codex_bin: executable name/path. Default is ``"codex"`` on POSIX and the npm
            ``codex.cmd`` shim on Windows. Replaceable in tests.
        cwd: child process working directory. ``None`` uses the current directory.
        runner: ``subprocess.run``-compatible execution function for tests. ``None``
            uses ``subprocess.run``.
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
        """Build the ``codex exec`` command (argument list) for this call."""
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
        # Always place the prompt after "--". Value-taking options such as
        # ``-i/--image`` or ``--add-dir`` can consume the following prompt as their
        # next value without a separator. POSIX "--" ends option parsing and fixes the
        # prompt as a positional argument, matching ClaudeCodeAct.
        cmd += ["--", prompt]
        return cmd

    def _build_env(self) -> dict[str, str]:
        """Environment passed to the child process: inherit ``os.environ`` plus ``env``."""
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
                # codex may read additional input when stdin is a pipe. The prompt is
                # already fixed as the positional argument after "--", so DEVNULL
                # prevents hangs or accidental input reads.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            # The child has already been killed. Return failed instead of killing the loop.
            result = CodexResult(
                failed=True,
                error=f"timeout ({self.timeout:g}s)",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)
        except OSError as exc:
            # Launch failures such as missing executable or missing execute permission
            # (FileNotFoundError / PermissionError are OSError). Return failed
            # gracefully; MaxIterations and other boundaries still stop the loop.
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
            # Prefer concise error text: stderr -> error-event body -> response body ->
            # exit code. This avoids putting the full JSONL in error for error events.
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
        # Account tokens regardless of success; failed attempts can still spend tokens.
        return ActOutcome(observation=result, tokens=tokens)


@dataclass
class MockCodexAct:
    """In-memory ``CodexAct`` substitute for tests/demos, without subprocesses.

    Returns each element of ``responses`` in order. Elements may be:

    - ``str`` -> that string as ``text`` (success, tokens 0)
    - ``Mapping`` -> expanded as :class:`CodexResult` fields, for example
      ``{"text": "...", "tokens": 1200}`` or ``{"failed": True, "error": "..."}``
    - :class:`CodexResult` -> used as-is

    Once responses are exhausted, it sticks to the last response, matching
    ``MockClaudeCodeAct``'s "keep returning the current best action" behavior.
    Boundaries such as ``MaxIterations`` still stop it safely. Rendered prompts are
    recorded in :attr:`prompts` for tests.
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
