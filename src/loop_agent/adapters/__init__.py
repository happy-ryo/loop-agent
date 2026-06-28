"""外部エージェント実行系を loop-agent の ``act`` フックに繋ぐアダプタ群。

現状は Claude Code (headless ``claude --print``) を 1 行で ``run_loop`` に
差し込む :class:`ClaudeCodeAct` と、subprocess を使わないテスト用
:class:`MockClaudeCodeAct` を提供する。いずれも ``ActHook``
(``Callable[[context], ActOutcome]``) として使える。

使い方::

    from loop_agent import run_loop, MaxIterations, TokenBudget
    from loop_agent.adapters import ClaudeCodeAct

    act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], timeout=600)
    result = run_loop(
        act=act,
        verify=my_verify,
        gather=lambda state: {"prompt": "次の修正を 1 つ書け"},
        conditions=[MaxIterations(10), TokenBudget(200_000)],
    )
"""

from __future__ import annotations

from .claude_code import (
    ClaudeCodeAct,
    ClaudeCodeResult,
    MockClaudeCodeAct,
    parse_tokens,
    render_prompt,
)

__all__ = [
    "ClaudeCodeAct",
    "ClaudeCodeResult",
    "MockClaudeCodeAct",
    "parse_tokens",
    "render_prompt",
]
