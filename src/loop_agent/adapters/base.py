"""Shared adapter foundation: result shape for the ``act`` seam and prompt rendering.

This module centralizes the **structural contract** shared by adapters that connect
external agent runners (Claude Code, Codex, and similar tools) to loop-agent's
``act`` hook. Individual adapters (:mod:`~loop_agent.adapters.claude_code` /
:mod:`~loop_agent.adapters.codex`) differ only in subprocess commands, flags, and
token/output parsing; the result object's shape (8 fields) and prompt rendering are
otherwise identical. The goal is to remove duplicate definitions and give new
adapters one place to reference for the shape they should follow (Issue #52).

Provided objects:

- :class:`ActResult` -- the **structural contract** (Protocol) adapter results must
  satisfy. It declares the fields/methods expected on objects stored in
  ``observation``. ``runtime_checkable`` also allows structural compatibility checks
  with ``isinstance``.
- :class:`ActResultBase` -- a concrete dataclass that satisfies that contract. It
  owns the 8 fields and ``__str__``; :class:`~loop_agent.adapters.claude_code.ClaudeCodeResult`
  and :class:`~loop_agent.adapters.codex.CodexResult` inherit it so they do **not**
  duplicate field definitions.
- :data:`Runner` -- a ``subprocess.run``-compatible execution seam for tests.
- :func:`render_prompt` / :func:`_format_fields` -- shared formatting that fills
  ``prompt_template`` from fields on the context (the gather return value or
  :class:`~loop_agent.state.LoopState`).

See ``docs/adapters/writing-an-adapter.md`` for how to write a new adapter.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# ``subprocess.run``-compatible execution seam used as a test injection point.
# Accepts capture_output / text / timeout / env / cwd / stdin and returns an object
# with ``returncode`` / ``stdout`` / ``stderr``.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@runtime_checkable
class ActResult(Protocol):
    """Structural contract for one adapter call result (``ActOutcome.observation``).

    :class:`~loop_agent.loop.ActOutcome` itself does not have ``failed``, so the
    information verify may need for decisions, such as success/failure and raw output,
    is collected on this observation object. Keeping the result shape consistent
    across adapters lets verify compose heterogeneous adapters without rewrites.

    Field meanings:

    - ``text`` -- assistant response body. ``str(result)`` returns the same text.
    - ``tokens`` -- total tokens consumed by this call, for budget accounting.
    - ``failed`` -- whether the call failed (non-zero exit, CLI-reported error,
      timeout, or launch failure). Failures are represented by this flag instead of
      exceptions so verify can decide whether to continue or stop.
    - ``returncode`` -- child process exit code (``None`` for launch failure/timeout).
    - ``error`` -- concise error text on failure (empty on success).
    - ``stdout`` / ``stderr`` -- raw child process output for debugging/re-parsing.
    - ``command`` -- command that was actually executed, as an argument tuple.

    Concrete implementations are :class:`ActResultBase` and Result classes that
    inherit it.

    Note: ``@runtime_checkable`` ``isinstance`` checks only for the presence of
    attribute names. It does not validate types or values, and ``__str__`` is present
    on all objects, so it does not contribute to the check. Treat this as structural
    contract documentation, not input validation. Adapter authors should not overtrust
    ``isinstance(result, ActResult)`` as proof that a Result is valid.
    """

    text: str
    tokens: int
    failed: bool
    returncode: Optional[int]
    error: str
    stdout: str
    stderr: str
    command: tuple[str, ...]

    def __str__(self) -> str:  # Return the response body when used as text.
        ...


@dataclass
class ActResultBase:
    """Shared concrete dataclass satisfying the :class:`ActResult` contract.

    All fields have defaults, so subclasses only need ``@dataclass`` plus their own
    docstring; they do not need to redefine fields. Keyword construction such as
    ``Result(text=..., tokens=...)`` and ``str(result)`` -> response body work as-is.
    New adapter Result classes can inherit this to automatically share the same
    8-field shape.
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
    """Build named fields from context for ``prompt_template.format(**...)``.

    - Mapping -> existing keys, such as ``{"prompt": ...}``
    - dataclass, for example :class:`~loop_agent.state.LoopState` -> field names, so
      templates can reference ``iteration`` / ``tokens_used`` / ``elapsed`` / ...
    - str -> ``{"prompt": <that string>}``, the shortest direct-prompt path
    - anything else with ``__dict__`` -> its attributes
    - final fallback -> ``{"prompt": <context>}``
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
    """Fill ``template`` from context fields and return the final prompt string.

    If the template references a field missing from context, raise :class:`KeyError`
    showing what was missing and what fields were available. This makes mistakes such
    as using the default ``"{prompt}"`` without passing ``prompt`` immediately obvious.
    """
    field_map = _format_fields(context)
    try:
        return template.format(**field_map)
    except KeyError as exc:  # .format raises missing keys as KeyError(key).
        missing = exc.args[0] if exc.args else exc
        raise KeyError(
            f"prompt_template {template!r} references {missing!r}, "
            f"not present in context fields {sorted(field_map)}; "
            "supply it via the gather hook (e.g. gather=lambda s: {'prompt': ...}) "
            "or adjust prompt_template to the available fields"
        ) from exc
