"""**Claude Code-specific** validation for ``loop_agent.adapters.claude_code`` (Issue #32).

The four ``act`` seam rules and ``ActResult`` shape (successful result shape /
``failed`` semantics / graceful timeout and startup failure handling / budget
accounting / mock contract / auth environment inheritance / stdin safety) have
been moved to the shared cross-adapter harness in ``tests/adapters/test_contract.py``.
Only **Claude Code-specific** behavior remains here:

1. ``build_command`` flag assembly (``--print`` / ``--output-format`` etc.).
2. Using ``is_error`` from ``--output-format json`` to determine failure.
3. Parsing token usage from JSON ``usage`` / stream-json / regex fallback
   (Claude semantics count input+output+cache_creation and exclude ``cache_read``;
   Issue #55).
4. ``render_prompt`` placeholder formatting (Mapping / ``LoopState`` / bare string / missing).
5. Real subprocess path (fake claude executable).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loop_agent.adapters import ClaudeCodeAct, parse_tokens, render_prompt


# -- Fake runner: replace subprocess.run to control commands/output ----------


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a ``subprocess.run``-compatible return value (CompletedProcess)."""

    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


JSON_OK = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}'
)


# -- 1. build_command(Claude-specific flags) --------------------------------


def test_build_command_includes_all_flags():
    act = ClaudeCodeAct(
        allowed_tools=["Read", "Edit"],
        model="opus",
        permission_mode="acceptEdits",
        output_format="json",
        extra_args=["--add-dir", "/tmp/x"],
    )
    cmd = act.build_command("the prompt")

    assert cmd[0] == "claude"
    assert "--print" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert cmd[cmd.index("--allowed-tools") + 1] == "Read,Edit"
    assert "--add-dir" in cmd
    # The prompt is a positional argument after "--" so variadic options do not consume it.
    assert cmd[-2:] == ["--", "the prompt"]


# -- 2. Use JSON is_error to determine failure (Claude-specific) -------------


def test_json_is_error_marks_failed_even_on_zero_exit():
    runner = _completed(
        stdout='{"is_error": true, "result": "rate limited", "usage": {"input_tokens": 3}}',
        returncode=0,
    )
    act = ClaudeCodeAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.tokens == 3


# -- 3. Token usage parsing (count input+output+cache_creation, exclude cache_read) --


def test_parse_tokens_from_json_usage():
    # 100(input)+40(output)+10(cache_creation)=150. cache_read(=5) is not counted.
    assert parse_tokens(JSON_OK) == 150


def test_parse_tokens_excludes_cache_read():
    # Reproduction/regression guard for Issue #55: even a huge cache_read is excluded.
    # Before the fix (summing all *tokens*), this was 100+40+10+999999. After the fix, it is 150.
    payload = (
        '{"type": "result", "is_error": false, "result": "ok", '
        '"usage": {"input_tokens": 100, "output_tokens": 40, '
        '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999}}'
    )
    assert parse_tokens(payload) == 150


def test_parse_tokens_regex_fallback_excludes_cache_read():
    # Even for mixed output that is not structured JSON, the fallback regex ignores cache_read.
    noisy = (
        'log\n"input_tokens": 100, "output_tokens": 40, '
        '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999 trailing'
    )
    assert parse_tokens(noisy) == 150


def test_actoutcome_tokens_exclude_cache_read():
    # cache_read is also excluded on the __call__ -> ActOutcome.tokens path (the value the driver records).
    runner = _completed(
        stdout=(
            '{"type": "result", "is_error": false, "result": "ok", '
            '"usage": {"input_tokens": 100, "output_tokens": 40, '
            '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999}}'
        )
    )
    outcome = ClaudeCodeAct(runner=runner)({"prompt": "x"})
    assert outcome.tokens == 150
    assert outcome.observation.tokens == 150


def test_parse_tokens_from_stream_json_last_result():
    stream = "\n".join(
        [
            '{"type": "system", "subtype": "init"}',
            '{"type": "assistant", "message": {"text": "thinking"}}',
            '{"type": "result", "usage": {"input_tokens": 7, "output_tokens": 3}}',
        ]
    )
    assert parse_tokens(stream) == 10


def test_parse_tokens_regex_fallback_from_noisy_text():
    # Representative keys are still picked up from mixed output that is not JSON.
    noisy = 'log line\nusage: "input_tokens": 20, "output_tokens": 5 trailing'
    assert parse_tokens(noisy) == 25


def test_parse_tokens_returns_zero_when_absent():
    assert parse_tokens("just some plain text, no usage here") == 0


# -- 4. render_prompt(placeholder formatting) -------------------------------


def test_render_prompt_from_mapping():
    rendered = render_prompt("{prompt} (iter={iteration})", {"prompt": "fix", "iteration": 2})
    assert rendered == "fix (iter=2)"


def test_render_prompt_from_loopstate_fields():
    from loop_agent import LoopState

    state = LoopState(iteration=4, tokens_used=99)
    rendered = render_prompt("step {iteration}, used {tokens_used}", state)
    assert rendered == "step 4, used 99"


def test_render_prompt_from_bare_string():
    assert render_prompt("{prompt}", "hello") == "hello"


def test_render_prompt_missing_field_raises_helpful_error():
    with pytest.raises(KeyError) as exc:
        render_prompt("{prompt}", {"iteration": 1})
    msg = str(exc.value)
    assert "prompt" in msg
    assert "iteration" in msg  # Shows an available field.


# -- 5. Real subprocess path (fake claude executable) -----------------------


def _write_fake_claude(tmp_path: Path, body: str) -> str:
    """Create a fake claude executable that works on Windows and POSIX."""
    script = tmp_path / "fake_claude.py"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    if sys.platform == "win32":
        shim = tmp_path / "fake_claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(shim)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def test_real_subprocess_success(tmp_path):
    body = (
        "import sys, json\n"
        "prompt = sys.argv[-1]\n"
        "print(json.dumps({'result': 'echo: ' + prompt, "
        "'usage': {'input_tokens': 12, 'output_tokens': 8}}))\n"
    )
    bin_path = _write_fake_claude(tmp_path, body)
    act = ClaudeCodeAct(claude_bin=bin_path)

    outcome = act({"prompt": "hello world"})
    assert outcome.observation.failed is False
    assert outcome.observation.text == "echo: hello world"
    assert outcome.tokens == 20


def test_real_subprocess_timeout(tmp_path):
    body = "import time\ntime.sleep(30)\n"
    bin_path = _write_fake_claude(tmp_path, body)
    act = ClaudeCodeAct(claude_bin=bin_path, timeout=0.5)

    outcome = act({"prompt": "x"})  # Should be killed after 0.5s and returned as failed.
    assert outcome.observation.failed is True
    assert "timeout" in outcome.observation.error


def test_real_subprocess_inherits_and_overrides_env(tmp_path):
    # The child inherits os.environ (existing claude session / ANTHROPIC_API_KEY path),
    # and values passed via env= are merged as overrides.
    body = (
        "import os, json\n"
        "print(json.dumps({'result': "
        "os.environ.get('LOOP_AGENT_INHERITED', 'MISSING') + '|' + "
        "os.environ.get('LOOP_AGENT_MARKER', 'MISSING'), "
        "'usage': {'output_tokens': 1}}))\n"
    )
    bin_path = _write_fake_claude(tmp_path, body)
    os.environ["LOOP_AGENT_INHERITED"] = "from-parent"
    try:
        act = ClaudeCodeAct(claude_bin=bin_path, env={"LOOP_AGENT_MARKER": "injected"})
        assert act({"prompt": "x"}).observation.text == "from-parent|injected"
    finally:
        del os.environ["LOOP_AGENT_INHERITED"]


