"""Shared contract test harness for all act adapters (Issue #52).

This defines :class:`AdapterSpec` and fixtures so one parametrized suite can
verify across adapters that each adapter (:class:`ClaudeCodeAct` /
:class:`CodexAct`, plus future additions) satisfies the **four act seam rules**
and the **`ActResult` shape**. Concrete shared cases live in
``test_contract.py``.

After adding a new adapter, registering one row in :data:`ADAPTER_SPECS`
automatically applies result shape / ``failed`` semantics / graceful timeout /
graceful startup failure / **token double-counting guard** / budget accounting /
Mock contract / auth environment inheritance / stdin safety checks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from loop_agent.adapters import (
    ClaudeCodeAct,
    ClaudeCodeResult,
    CodexAct,
    CodexResult,
    MockClaudeCodeAct,
    MockCodexAct,
)

# parse_tokens has **adapter-specific semantics**: claude counts
# input+output+cache_creation and excludes cache_read, while codex counts only
# input+output and excludes subset fields. Import directly from each submodule
# instead of a shared __init__ re-export. The token double-counting guard locks
# in this difference.
from loop_agent.adapters.claude_code import parse_tokens as claude_parse_tokens
from loop_agent.adapters.codex import parse_tokens as codex_parse_tokens


# -- Fake runner: replace subprocess.run to control commands/output --------


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a runner that returns a ``subprocess.run``-compatible CompletedProcess.

    It records each call's ``(command, kwargs)`` in ``.calls`` so tests can
    verify the passed command/environment/stdin.
    """

    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


def _timeout_runner(timeout_value: float = 600.0):
    """Create a runner that always raises :class:`subprocess.TimeoutExpired`."""

    def _runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_value)

    return _runner


# -- Adapter spec: minimal data the shared harness needs to drive adapters --------


@dataclass(frozen=True)
class AdapterSpec:
    """Description for adding one act adapter to the shared contract tests.

    Attributes:
        name: Parametrize id (such as ``"claude_code"``).
        act_cls: Adapter implementation (a ``@dataclass`` accepting ``runner=``
            and ``<bin>_bin=``).
        result_cls: Observed object type (inherits ``ActResultBase`` and
            conforms to ``ActResult``).
        mock_cls: Mock that does not use subprocess (accepts ``responses=``).
        parse_tokens: Token parser for that adapter (semantics are
            adapter-specific).
        bin_kwarg: Argument name for replacing the executable
            (``"claude_bin"`` / ``"codex_bin"``).
        success_stdout: Raw stdout sample for success (contains
            ``success_text`` in the body).
        success_text: Response body that should be extracted from
            ``success_stdout``.
        success_tokens: Total token count that should be accounted from
            ``success_stdout``.
        token_guard_stdout: Usage sample that **could be over-counted by naive
            summation** (codex includes subset keys cached/reasoning, while
            claude includes the excluded cache_read field with a huge value.
            Each adapter's sample is shaped so an incorrect summation changes
            the total).
        token_guard_expected: Correct total token count under that adapter's
            semantics (double-counting makes this mismatch; catches the Issue
            #55 bug class).
        expects_devnull: Whether ``__call__`` should pass ``stdin=DEVNULL``
            (prevents CLIs that read interactive input from hanging).
    """

    name: str
    act_cls: type
    result_cls: type
    mock_cls: type
    parse_tokens: Callable[[str], int]
    bin_kwarg: str
    success_stdout: str
    success_text: str
    success_tokens: int
    token_guard_stdout: str
    token_guard_expected: int
    expects_devnull: bool

    def make_act(self, **kwargs: Any):
        """Small test helper that creates an adapter, passing ``runner`` etc. through."""
        return self.act_cls(**kwargs)


# Claude Code: count usage input/output/cache_creation but **exclude** cache_read
# by token-cost policy (low cost but grows cumulatively; Issue #55). The success
# total is 100+40+10=150 (cache_read=5 is not counted).
_CLAUDE_SUCCESS = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}'
)

# token_guard: make cache_read **intentionally huge (999999)** so an incorrect
# summation can never match 150. This strongly detects the Issue #55 cumulative
# cache_read bug, with the same intent as codex using 9999/8888 for subset keys.
_CLAUDE_TOKEN_GUARD = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999}}'
)

# Codex: cached_input_tokens is a subset of input, and reasoning_output_tokens is
# a subset of output. The total is only input+output (100+40=140). In
# token_guard, make the subset values intentionally huge (9999/8888) so summing
# them can never match 140 (= strongly detects double-counting regressions).
_CODEX_SUCCESS = "\n".join(
    [
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"done fixing"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,'
        '"output_tokens":40,"reasoning_output_tokens":10}}',
    ]
)
_CODEX_TOKEN_GUARD = (
    '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":9999,'
    '"output_tokens":40,"reasoning_output_tokens":8888}}'
)


ADAPTER_SPECS = [
    AdapterSpec(
        name="claude_code",
        act_cls=ClaudeCodeAct,
        result_cls=ClaudeCodeResult,
        mock_cls=MockClaudeCodeAct,
        parse_tokens=claude_parse_tokens,
        bin_kwarg="claude_bin",
        success_stdout=_CLAUDE_SUCCESS,
        success_text="done fixing",
        success_tokens=150,
        token_guard_stdout=_CLAUDE_TOKEN_GUARD,
        token_guard_expected=150,  # cache_read(=999999) is not counted (cost policy).
        expects_devnull=False,  # claude inherits stdin without setting it explicitly.
    ),
    AdapterSpec(
        name="codex",
        act_cls=CodexAct,
        result_cls=CodexResult,
        mock_cls=MockCodexAct,
        parse_tokens=codex_parse_tokens,
        bin_kwarg="codex_bin",
        success_stdout=_CODEX_SUCCESS,
        success_text="done fixing",
        success_tokens=140,
        token_guard_stdout=_CODEX_TOKEN_GUARD,
        token_guard_expected=140,  # exclude subset fields (cached/reasoning).
        expects_devnull=True,  # codex uses stdin=DEVNULL to prevent misreads/hangs.
    ),
]


@pytest.fixture(params=ADAPTER_SPECS, ids=lambda spec: spec.name)
def adapter_spec(request) -> AdapterSpec:
    """Fixture that parametrizes across all registered adapters."""
    return request.param


@pytest.fixture
def make_runner() -> Callable[..., Any]:
    """Factory for fake runners that return ``CompletedProcess`` and record ``.calls``."""
    return _completed


@pytest.fixture
def make_timeout_runner() -> Callable[..., Any]:
    """Factory for fake runners that raise ``TimeoutExpired``."""
    return _timeout_runner
