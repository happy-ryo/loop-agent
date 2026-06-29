"""Adapter connecting Claude Code (headless ``claude --print``) to the ``act`` hook.

:class:`ClaudeCodeAct` starts ``claude --print <prompt>`` once per iteration via
subprocess and returns the response inside :class:`ActOutcome`. This lets one line of
``run_loop`` (``act=ClaudeCodeAct(...)``) run a loop through Claude Code (the
``act`` seam from report.md S4.4 / Issue #32).

Design commitments that preserve loop-core behavior:

- **Do not kill the loop with exceptions**: timeout, non-zero exit, or missing
  executable are returned gracefully as :class:`ActOutcome` carrying a
  :class:`ClaudeCodeResult` with ``failed=True`` instead of raising. The verify side
  can inspect ``failed`` to decide whether to continue or stop. Boundary conditions
  evaluated by ``Timeout`` / ``MaxIterations`` still always apply (the while-guard
  design from report.md S4.4).
- **Account tokens against the budget**: extract token counts from the response
  (``usage`` in ``--output-format json``, or stdout/stderr fallback parsing) and put
  them in ``ActOutcome.tokens``. The driver adds this to ``state.tokens_used``, so
  :class:`~loop_agent.conditions.TokenBudget` works unchanged.
- **Delegate auth to the claude CLI**: the child process inherits the caller's
  ``os.environ`` by default. This makes an existing claude CLI session
  (``~/.claude`` login) the primary auth path, with ``ANTHROPIC_API_KEY`` as a CLI
  fallback if present. Passing ``env`` merges overrides into that environment, which
  is the path for injecting secrets.

Use :class:`MockClaudeCodeAct` for tests/demos that should not use subprocesses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from ..errors import ConfigError
from ..loop import ActOutcome

# Result shape, prompt rendering, and Runner seam live in the shared adapter base.
# This module defines only the Claude Code-specific differences: subprocess command,
# flags, and token parsing. ``render_prompt`` / ``Runner`` are also re-exported from
# this module namespace to preserve existing ``adapters.claude_code.render_prompt``
# references.
from .base import ActResultBase, Runner, render_prompt

__all__ = [
    "ClaudeCodeAct",
    "ClaudeCodeResult",
    "MockClaudeCodeAct",
    "Runner",
    "parse_tokens",
    "render_prompt",
]

# Accepted shapes for each mock response: str is response text as-is, dict expands
# into ClaudeCodeResult fields, and ClaudeCodeResult is used directly.
MockResponse = Union[str, Mapping[str, Any], "ClaudeCodeResult"]


@dataclass
class ClaudeCodeResult(ActResultBase):
    """Structured result for one Claude Code call, stored in ``ActOutcome.observation``.

    Inherits :class:`~loop_agent.adapters.base.ActResultBase` and therefore reuses the
    8 fields (``text`` / ``tokens`` / ``failed`` / ``returncode`` / ``error`` /
    ``stdout`` / ``stderr`` / ``command``) and ``__str__``. ``str(result)`` returns the
    response text (``text``), so existing code that treats results as text still works.
    It has the same shape as :class:`~loop_agent.adapters.codex.CodexResult` and
    satisfies the :class:`~loop_agent.adapters.base.ActResult` contract.
    """


# Token-cost policy: the budget (:class:`~loop_agent.conditions.TokenBudget`) counts
# only tokens that are **meaningful as cost**. This allowlist counts ``input_tokens``,
# ``output_tokens``, and ``cache_creation_input_tokens`` (cache writes), and
# **excludes** ``cache_read_input_tokens`` (Issue #55).
#
# Exclusion rationale: cache_read is (1) lightly priced (roughly 0.1x normal input in
# Anthropic pricing, effectively near-free) and (2) when Claude Code runs multiple
# internal turns, **each turn re-reads cached context**, so the cumulative total
# reported for one ``act`` can grow orders of magnitude beyond real input+output.
# Including it would make ``TokenBudget`` fire much earlier than intended (a
# self-translation PoC counted about 340k tokens for translating one ~170-line file).
#
# The old "sum every value whose name contains *tokens*" behavior was future-proof,
# but also greedily counted fields like cache_read that are reported yet are not real
# cost, or are inflated by repeated reads. Match CodexAct's
# :func:`~loop_agent.adapters.codex._sum_codex_tokens` approach (explicit input+output
# allowlist) so accounting rules stay predictable.
_COUNTED_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
)


def _sum_token_fields(usage: Mapping[str, Any]) -> int:
    """Sum integer values for **accounted tokens** from a ``usage`` mapping.

    Accounted fields are the :data:`_COUNTED_TOKEN_FIELDS` allowlist
    (``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens``).
    ``cache_read_input_tokens`` is **excluded** because it is cheap and can inflate
    cumulatively; see the comment above / Issue #55. Budgets
    (:class:`~loop_agent.conditions.TokenBudget`) cut off this "real cost total".
    """
    total = 0
    for key in _COUNTED_TOKEN_FIELDS:
        value = usage.get(key)
        if isinstance(value, bool) is False and isinstance(value, int):
            total += value
    return total


def _try_json(text: str) -> Any:
    """Try reading all of ``text`` or each line (for stream-json) as JSON.

    - First parse the whole string as one JSON value (single result from
      ``--output-format json``).
    - If that fails, scan line-by-line and return the last object with ``usage`` so
      ``--output-format stream-json`` final result lines are captured.
    Return ``None`` if nothing is JSON.
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


# Fallback for human-readable or partial output when usage was not available as
# structured JSON. It captures representative keys once each (first occurrence only)
# and sums them. Greedily reading stream-json intermediate rows or modelUsage
# breakdowns can double-count, so this intentionally anchors to each key name.
# Accounted fields match the JSON path via :data:`_COUNTED_TOKEN_FIELDS`
# (cache_read excluded; see the comment above :func:`_sum_token_fields` / Issue #55).
_TOKEN_FIELD_RES = tuple(
    re.compile(rf'"{key}"\s*:\s*(\d+)') for key in _COUNTED_TOKEN_FIELDS
)


def parse_tokens(stdout: str, stderr: str = "") -> int:
    """Extract total tokens from ``claude`` output, or return 0 if unavailable.

    Priority:

    1. Parse stdout as JSON and sum token values from the ``usage`` object
       (``--output-format json`` / ``stream-json``). Only top-level ``usage`` is read,
       not breakdowns such as ``modelUsage``, so values are not double-counted.
    2. If stdout is not JSON, scan stdout then stderr for the first occurrence of
       representative token keys and sum them. This is a fallback for debug output or
       mixed text.

    Return ``0`` if nothing is found; missing usage is normal for text output.
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
    """Extract response text, token count, and the CLI-reported error flag.

    For ``--output-format json`` results, use ``result`` as the body, ``is_error`` for
    error detection, and ``usage`` as the token source. If the output is not JSON, use
    stdout as the body and get tokens through the :func:`parse_tokens` fallback.
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
    """``act`` hook that starts Claude Code headlessly.

    Args:
        allowed_tools: tool names to pass to ``--allowed-tools``, for example
            ``["Read", "Edit"]``. ``None`` omits the flag and follows the CLI default.
        timeout: maximum seconds for one call. On timeout the child is killed and a
            ``failed=True`` result is returned gracefully, without raising.
        prompt_template: ``str.format`` template for the final prompt. The default
            ``"{prompt}"`` assumes the context (gather return value) has ``prompt``.
            If passing ``LoopState`` directly as context, templates can embed state
            fields such as ``"... iter={iteration}"``.
        model: ``--model`` value; aliases such as ``opus`` / ``sonnet`` are allowed.
            ``None`` uses the default.
        permission_mode: ``--permission-mode``
            (``default`` / ``acceptEdits`` / ``bypassPermissions`` / etc.).
            ``None`` uses the default.
        env: overrides merged into the child process environment. ``None`` inherits
            ``os.environ`` unchanged, so an existing claude session plus
            ``ANTHROPIC_API_KEY`` fallback works.
        output_format: ``--output-format``. Default ``"json"`` yields a single result
            with usage, making token parsing reliable. ``"text"`` usually only gives
            body text, so tokens tend to be 0.
        claude_bin: executable name/path (default ``"claude"``). Replaceable in tests.
        extra_args: additional flags inserted before the prompt.
        cwd: child process working directory. ``None`` uses the current directory.
        runner: ``subprocess.run``-compatible execution function for tests. ``None``
            uses ``subprocess.run``.
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
        """Build the ``claude`` command (argument list) for this call."""
        cmd: list[str] = [self.claude_bin, "--print"]
        if self.output_format:
            cmd += ["--output-format", self.output_format]
        if self.model:
            cmd += ["--model", self.model]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            # The CLI accepts comma/space separators. Join with commas so tool specs
            # containing spaces, such as "Bash(git *)", stay one argument.
            cmd += ["--allowed-tools", ",".join(self.allowed_tools)]
        cmd += list(self.extra_args)
        # Always place the prompt after "--". Variadic options such as
        # ``--allowed-tools <tools...>`` and value-taking options supplied through
        # extra_args (for example ``--add-dir``) can greedily consume the next token as
        # another value. Without the separator, appending the prompt at the end can
        # cause the CLI to lose it (empty request or hang until timeout). POSIX "--"
        # ends option parsing and fixes the prompt as a positional argument.
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
                timeout=self.timeout,
                env=self._build_env(),
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            # The child has already been killed. Return failed instead of killing the loop.
            result = ClaudeCodeResult(
                failed=True,
                error=f"timeout ({self.timeout:g}s)",
                command=tuple(command),
            )
            return ActOutcome(observation=result, tokens=0)
        except OSError as exc:
            # Launch failures such as missing executable or missing execute permission
            # (FileNotFoundError / PermissionError are OSError). Return failed
            # gracefully; MaxIterations and other boundaries still stop the loop.
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
        # Account tokens regardless of success; failed attempts can still spend tokens.
        return ActOutcome(observation=result, tokens=tokens)


@dataclass
class MockClaudeCodeAct:
    """In-memory ``ClaudeCodeAct`` substitute for tests/demos, without subprocesses.

    Returns each element of ``responses`` in order. Elements may be:

    - ``str`` -> that string as ``text`` (success, tokens 0)
    - ``Mapping`` -> expanded as :class:`ClaudeCodeResult` fields, for example
      ``{"text": "...", "tokens": 1200}`` or ``{"failed": True, "error": "..."}``
    - :class:`ClaudeCodeResult` -> used as-is

    Once responses are exhausted, it sticks to the last response, matching the
    ``CandidateApplier`` "keep returning the current best action" behavior. Boundaries
    such as ``MaxIterations`` still stop it safely. Rendered prompts are recorded in
    :attr:`prompts` for tests. ``prompt_template`` has the same meaning as in
    :class:`ClaudeCodeAct`, reproducing placeholder behavior without subprocesses.
    """

    responses: Sequence[MockResponse]
    prompt_template: str = "{prompt}"
    prompts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.responses:
            raise ConfigError("MockClaudeCodeAct requires at least one response")
        self._responses = [self._coerce(r) for r in self.responses]

    @staticmethod
    def _coerce(response: MockResponse) -> ClaudeCodeResult:
        if isinstance(response, ClaudeCodeResult):
            return response
        if isinstance(response, str):
            return ClaudeCodeResult(text=response)
        if isinstance(response, Mapping):
            return ClaudeCodeResult(**response)
        raise ConfigError(
            "MockClaudeCodeAct responses must be str, Mapping, or ClaudeCodeResult, "
            f"got {type(response).__name__}"
        )

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        self.prompts.append(prompt)
        index = min(len(self.prompts) - 1, len(self._responses) - 1)
        result = self._responses[index]
        return ActOutcome(observation=result, tokens=result.tokens)
