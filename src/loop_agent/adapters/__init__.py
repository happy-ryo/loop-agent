"""Adapters connecting external agent runners to loop-agent's ``act`` hook.

Provides :class:`ClaudeCodeAct`, which plugs Claude Code (headless
``claude --print``) into ``run_loop`` in one line; :class:`CodexAct`, which does the
same for Codex CLI (headless ``codex exec``); and subprocess-free test doubles
:class:`MockClaudeCodeAct` / :class:`MockCodexAct`. All can be used as ``ActHook``
(``Callable[[context], ActOutcome]``).

Usage::

    from loop_agent import run_loop, MaxIterations, TokenBudget
    from loop_agent.adapters import ClaudeCodeAct, CodexAct

    act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], timeout=600)
    # Or through Codex:
    act = CodexAct(model="gpt-5.5", effort="medium", timeout=600)
    result = run_loop(
        act=act,
        verify=my_verify,
        gather=lambda state: {"prompt": "Write the next single fix"},
        conditions=[MaxIterations(10), TokenBudget(200_000)],
    )
"""

from __future__ import annotations

from .base import ActResult, ActResultBase, Runner, render_prompt
from .claude_code import (
    ClaudeCodeAct,
    ClaudeCodeResult,
    MockClaudeCodeAct,
    parse_tokens,
)
from .codex import (
    CodexAct,
    CodexResult,
    MockCodexAct,
)
from .model_ladder import (
    EscalationContext,
    EscalationPredicate,
    ModelLadder,
    after_attempts,
    on_failure,
)

__all__ = [
    # Shared foundation: contract for new adapters, result base, rendering/execution seams.
    "ActResult",
    "ActResultBase",
    "Runner",
    "render_prompt",
    # Claude Code adapter.
    "ClaudeCodeAct",
    "ClaudeCodeResult",
    "MockClaudeCodeAct",
    "parse_tokens",
    # Codex adapter.
    "CodexAct",
    "CodexResult",
    "MockCodexAct",
    # ModelLadder: canonical act-composition example, not a subprocess adapter.
    "ModelLadder",
    "EscalationContext",
    "EscalationPredicate",
    "on_failure",
    "after_attempts",
]
