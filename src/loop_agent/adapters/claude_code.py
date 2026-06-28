"""Adapter connecting Claude Code (headless ``claude --print``) to the ``act`` hook.

:class:`ClaudeCodeAct` launches ``claude --print <prompt>`` as a subprocess once per
iteration, packages the response into :class:`ActOutcome`, and returns it. This allows
a single line in ``run_loop`` (``act=ClaudeCodeAct(...)``) to "run the loop via
Claude Code" (report.md S4.4 act seam / Issue #32).

Design commitments (to avoid breaking loop core semantics):

- **Do not kill the loop on exceptions**: Timeout exceeded, non-zero exit, or missing
  executable are handled gracefully by returning a :class:`ActOutcome` with a failed
  :class:`ClaudeCodeResult` carrying ``failed=True``, rather than raising an exception.
  The verify side can inspect this ``failed`` flag to decide whether to continue or
  abort. Boundary conditions ``Timeout`` / ``MaxIterations`` always take effect
  (report.md S4.4 while-guard design).
- **Accumulate tokens to budget**: Extract token count from the response
  (``--output-format json`` ``usage``, or fall back to stdout/stderr parsing),
  and place it in ``ActOutcome.tokens``. The driver adds this to
  ``state.tokens_used``, so :class:`~loop_agent.conditions.TokenBudget` works
  as-is.
- **Delegate auth to claude CLI**: The child process inherits ``os.environ`` by default,
  allowing any existing claude CLI session (~/.claude login) to take precedence. If
  ``ANTHROPIC_API_KEY`` is in the environment, the CLI falls back to it. Passing
  ``env`` merges it as overrides (secrecy values are injected this way).

For tests/demos that avoid subprocess, use :class:`MockClaudeCodeAct`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from ..loop import ActOutcome

# subprocess.run-compatible execution function seam (injection point for test mocking).
# Accepts capture_output / text / timeout / env / cwd; returns an object with
# ``returncode`` / ``stdout`` / ``stderr``.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Permitted response shape for each Mock response. str becomes response text as-is,
# dict expands as ClaudeCodeResult fields, ClaudeCodeResult is used as-is.
MockResponse = Union[str, Mapping[str, Any], "ClaudeCodeResult"]


@dataclass
class ClaudeCodeResult:
    """Structured result of a single Claude Code invocation (placed in ``ActOutcome.observation``).

    Since ``ActOutcome`` itself lacks a ``failed`` field, information such as success/failure
    and raw output -- needed by verify for decision-making -- is consolidated in this
    observation object. ``str(result)`` returns the response text (``text``), so it
    integrates seamlessly with existing code that treats it as text directly.
    """

    text: str = ""
    tokens: int = 0
    failed: bool = False
    returncode: Optional[int] = None
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()

    def __str__(self) -> str:  # Return the response body when used as text.
        return self.text


def _format_fields(context: Any) -> dict[str, Any]:
    """Build named fields from context to pass to ``prompt_template.format(**...)``.

    - Mapping -> keys as-is (e.g., ``{"prompt": ...}``)
    - dataclass (e.g., :class:`~loop_agent.state.LoopState`) -> each field name
      (``iteration`` / ``tokens_used`` / ``elapsed`` ... can be embedded in template)
    - str -> ``{"prompt": <that string>}`` (direct prompt passthrough, shortest path)
    - anything with ``__dict__`` -> those attributes
    - fallback -> ``{"prompt": <context>}``
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
    """Fill ``template`` with context fields and return the final prompt string.

    If the template references a field not present in context, raise :class:`KeyError`
    indicating what is missing and what is available. This helps catch mistakes like
    forgetting to pass ``prompt`` to the default ``"{prompt}"`` template early.
    """
    field_map = _format_fields(context)
    try:
        return template.format(**field_map)
    except KeyError as exc:  # .format raises KeyError(key) for missing keys.
        missing = exc.args[0] if exc.args else exc
        raise KeyError(
            f"prompt_template {template!r} references {missing!r}, "
            f"not present in context fields {sorted(field_map)}; "
            "supply it via the gather hook (e.g. gather=lambda s: {'prompt': ...}) "
            "or adjust prompt_template to the available fields"
        ) from exc


def _sum_token_fields(usage: Mapping[str, Any]) -> int:
    """Sum integer values containing ``*tokens*`` in their name from the ``usage`` map.

    Captures ``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` without omission and adapts to future token types. Since
    the budget (:class:`~loop_agent.conditions.TokenBudget`) counts "total tokens
    processed," we sum all types without distinction.
    """
    return sum(
        value
        for key, value in usage.items()
        if isinstance(value, bool) is False
        and isinstance(value, int)
        and "tokens" in key.lower()
    )


def _try_json(text: str) -> Any:
    """Attempt to parse ``text`` as JSON, either as a whole or (for stream-json) line-by-line.

    - First try to parse the entire text as a single JSON object
      (``--output-format json`` single result).
    - On failure, scan line-by-line and return the last object with a ``usage`` field
      (to capture the final result line in ``--output-format stream-json``).
    Return ``None`` if none parse as JSON.
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


# Fallback for human-readable / partial output when usage is not available as structured JSON.
# Pick each representative key (first occurrence only) and sum them. To avoid double-counting
# when greedily picking intermediate lines in stream-json or details in modelUsage, we
# deliberately restrict to first match per key.
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
    """Extract total token count from ``claude`` output (return 0 if not found).

    Priority:

    1. Parse stdout as JSON and sum token values from the ``usage`` object
       (``--output-format json`` / ``stream-json``). Only inspect top-level ``usage``,
       not ``modelUsage`` details, to avoid double-counting.
    2. If not JSON, scan stdout then stderr in order, using regex to find the first
       occurrence of each representative token key and sum them (fallback for debug
       output or mixed text).

    Return ``0`` if neither finds anything (absence of usage in text output is normal).
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
    """Extract response text, token count, and error flag (as reported by CLI).

    For ``--output-format json`` results, use ``result`` for text, ``is_error`` for
    the error flag, and ``usage`` for tokens. Otherwise, treat stdout as text and
    extract tokens via :func:`parse_tokens` fallback.
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
    """``act`` hook for headless Claude Code invocation.

    Args:
        allowed_tools: List of tool names to pass to ``--allowed-tools``
            (e.g., ``["Read", "Edit"]``). Omitted if ``None`` (use CLI default).
        timeout: Maximum seconds per invocation. On timeout, kill the child process
            and return gracefully with ``failed=True`` (no exception).
        prompt_template: ``str.format`` template to build the final prompt. The default
            ``"{prompt}"`` assumes context (from gather) has a ``prompt`` field.
            Pass ``LoopState`` directly as context to embed state fields like
            ``"... iter={iteration}"`` in the template.
        model: ``--model`` (aliases like ``opus`` / ``sonnet`` also work). ``None``
            uses CLI default.
        permission_mode: ``--permission-mode`` (``default`` / ``acceptEdits`` /
            ``bypassPermissions``, etc.). ``None`` uses CLI default.
        env: Override dict to merge into child process environment. ``None`` inherits
            ``os.environ`` as-is (existing claude session + ``ANTHROPIC_API_KEY``
            fallback remain active).
        output_format: ``--output-format``. Default ``"json"`` (single result with
            usage, token parsing is reliable). ``"text"`` gives body only (tokens
            often become 0).
        claude_bin: Executable name/path (default ``"claude"``). Can be overridden in tests.
        extra_args: Additional flags to pass before the prompt.
        cwd: Child process working directory. ``None`` uses the current directory.
        runner: ``subprocess.run``-compatible execution function (test injection point).
            ``None`` uses ``subprocess.run`` directly.
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
        """Build the ``claude`` command (argument list) to execute for this invocation."""
        cmd: list[str] = [self.claude_bin, "--print"]
        if self.output_format:
            cmd += ["--output-format", self.output_format]
        if self.model:
            cmd += ["--model", self.model]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            # CLI accepts comma or space-separated values. When tool names contain spaces
            # (e.g. "Bash(git *)"), we join with commas to keep it as a single token.
            cmd += ["--allowed-tools", ",".join(self.allowed_tools)]
        cmd += list(self.extra_args)
        # Prompt must always come after "--". Variadic options like
        # ``--allowed-tools <tools...>`` or ``--add-dir`` from extra_args greedily
        # consume the following token as their value. Without a separator, appending
        # the prompt at the end causes the CLI to lose it (empty request or hang until
        # timeout). Use the POSIX convention "--" to end option parsing and fix the
        # prompt as a positional argument.
        cmd += ["--", prompt]
        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the environment to pass to the child process. Inherit ``os.environ`` and merge ``env`` as overrides."""
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
            # Child is killed. Return gracefully with failed, don't raise to kill the loop.
            result = ClaudeCodeResult(
                failed=True,
                error=f"timeout ({self.timeout:g}s)",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)
        except OSError as exc:
            # Launch failure: claude executable not found / no execute permission / etc.
            # (FileNotFoundError / PermissionError are OSError subclasses). Return gracefully
            # with failed (boundary conditions like MaxIterations ensure loop stops).
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
        # Account tokens regardless of success (failed attempts can consume tokens).
        return ActOutcome(observation=result, tokens=tokens)


@dataclass
class MockClaudeCodeAct:
    """In-memory ``ClaudeCodeAct`` substitute without subprocess (for tests/demos).

    Returns each element of ``responses`` in sequence. Each element can be:

    - ``str`` -> use that string as ``text`` (success, tokens = 0)
    - ``Mapping`` -> unpack as :class:`ClaudeCodeResult` fields
      (e.g., ``{"text": "...", "tokens": 1200}`` or ``{"failed": True, "error": "..."}``)
    - :class:`ClaudeCodeResult` -> use as-is

    Once responses are exhausted, stick to the last one (same "keep returning the
    best current option" behavior as ``CandidateApplier``; boundary conditions like
    ``MaxIterations`` ensure safe stopping). Rendered prompts are recorded in
    :attr:`prompts` for test verification. ``prompt_template`` has the same meaning
    as in :class:`ClaudeCodeAct`, reproducing placeholder behavior without subprocess.
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
