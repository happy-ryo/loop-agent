"""外部エージェント実行系を loop-agent の ``act`` フックに繋ぐアダプタ群。

Claude Code (headless ``claude --print``) を 1 行で ``run_loop`` に差し込む
:class:`ClaudeCodeAct` と、Codex CLI (headless ``codex exec``) を同様に差し込む
:class:`CodexAct`、および subprocess を使わないテスト用
:class:`MockClaudeCodeAct` / :class:`MockCodexAct` を提供する。いずれも ``ActHook``
(``Callable[[context], ActOutcome]``) として使える。

使い方::

    from loop_agent import run_loop, MaxIterations, TokenBudget
    from loop_agent.adapters import ClaudeCodeAct, CodexAct

    act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], timeout=600)
    # または Codex 経由:
    act = CodexAct(model="gpt-5.5", effort="medium", timeout=600)
    result = run_loop(
        act=act,
        verify=my_verify,
        gather=lambda state: {"prompt": "次の修正を 1 つ書け"},
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
    # 共通土台(新規アダプタが従う契約 / 結果の基底 / 整形・実行シーム)。
    "ActResult",
    "ActResultBase",
    "Runner",
    "render_prompt",
    # Claude Code アダプタ。
    "ClaudeCodeAct",
    "ClaudeCodeResult",
    "MockClaudeCodeAct",
    "parse_tokens",
    # Codex アダプタ。
    "CodexAct",
    "CodexResult",
    "MockCodexAct",
    # ModelLadder(act 合成の canonical example。subprocess アダプタではない)。
    "ModelLadder",
    "EscalationContext",
    "EscalationPredicate",
    "on_failure",
    "after_attempts",
]
