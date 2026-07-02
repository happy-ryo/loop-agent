"""Codex adapter (``loop_agent.adapters.codex``) **specific** validation (Issue #49).

The four ``act`` seam rules and the shape of ``ActResult`` (successful result shape /
``failed`` semantics / graceful timeout and startup failure handling / budget accounting /
Mock contract / auth environment inheritance / stdin safety) have moved to the shared
cross-adapter harness in ``tests/adapters/test_contract.py`` (fixed stdin DEVNULL is also
verified there through ``expects_devnull``). Only **Codex-specific** behavior remains here:

1. ``build_command`` flag assembly (``codex exec`` / ``--json`` / ``-m`` / ``-c``, etc.).
2. Response body extraction from ``--json`` JSONL (item.completed / direct agent_message /
   delta / last_agent_message, including dotted/snake_case variants) and ``error`` /
   ``*.failed`` detection.
3. Token usage parsing (Codex counts only ``input+output``. Do not double-count the
   cached/reasoning subsets / ``total_tokens`` fallback).
4. Passing ``cwd`` and formatting ``render_prompt``.
5. Real subprocess path (fake codex executable).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import loop_agent.adapters.codex as codex_module
from loop_agent.adapters import CodexAct, MockCodexAct, render_prompt

# Import parse_tokens directly from the codex submodule. The ``parse_tokens`` exposed by
# ``adapters.__init__`` comes from claude_code (it counts input+output+cache_creation and
# excludes cache_read), which has different semantics from Codex usage (it has cached and
# reasoning subsets but no cache_creation), so the Codex tests use the module-level helper.
from loop_agent.adapters.codex import parse_tokens


# -- Fake runner: replace subprocess.run and control commands/output --------


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a ``subprocess.run``-compatible return value (CompletedProcess)."""

    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


# JSONL modeled after a real ``codex exec --json`` event sequence. Only input(100) +
# output(40) = 140 count toward the total (cached 60 / reasoning 10 are subsets).
JSONL_OK = "\n".join(
    [
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"done fixing"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,'
        '"output_tokens":40,"reasoning_output_tokens":10}}',
    ]
)


# -- 1. build_command(Codex-specific flags) ------------------------------------


def test_build_command_includes_all_flags():
    act = CodexAct(
        model="gpt-5.5",
        effort="high",
        sandbox="workspace-write",
        allowed_args=["--add-dir", "/tmp/x"],
        codex_bin="codex",
    )
    cmd = act.build_command("the prompt")

    assert cmd[0] == "codex"
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "--add-dir" in cmd
    # The prompt is the positional argument after "--" so value-taking options do not eat it.
    assert cmd[-2:] == ["--", "the prompt"]


def test_default_codex_bin_prefers_cmd_shim_on_windows(monkeypatch):
    monkeypatch.setattr(codex_module.os, "name", "nt")
    monkeypatch.setattr(
        codex_module.shutil,
        "which",
        lambda name: "C:\\Users\\me\\AppData\\Roaming\\npm\\codex.cmd"
        if name == "codex.cmd"
        else None,
    )

    assert codex_module._default_codex_bin() == "C:\\Users\\me\\AppData\\Roaming\\npm\\codex.cmd"
    assert CodexAct().build_command("p")[0].endswith("codex.cmd")


def test_default_codex_bin_falls_back_to_cmd_name_on_windows(monkeypatch):
    monkeypatch.setattr(codex_module.os, "name", "nt")
    monkeypatch.setattr(codex_module.shutil, "which", lambda name: None)

    assert codex_module._default_codex_bin() == "codex.cmd"

def test_build_command_minimal_omits_optional_flags():
    # Disabling json/skip omits them. Leaving sandbox unset also omits -s.
    act = CodexAct(json_output=False, skip_git_repo_check=False)
    cmd = act.build_command("p")
    assert "--json" not in cmd
    assert "--skip-git-repo-check" not in cmd
    assert "-s" not in cmd
    assert cmd[-2:] == ["--", "p"]


# -- 2. Response body extraction(JSONL schema variants) and error/*.failed detection -----------


def test_text_from_direct_agent_message_event():
    # Old --json shape: direct agent_message event without an item.completed wrapper.
    stream = "\n".join(
        [
            '{"type":"agent_message","message":"hi there"}',
            '{"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":1}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "hi there"
    assert result.failed is False


def test_text_from_last_agent_message_field():
    # Shape where the completion event carries last_agent_message as a separate field.
    stream = "\n".join(
        [
            '{"type":"task_complete","last_agent_message":"final answer","usage":{"input_tokens":4,"output_tokens":2}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "final answer"


def test_text_from_streaming_deltas_when_no_consolidated_message():
    stream = "\n".join(
        [
            '{"type":"agent_message_content_delta","delta":"hel"}',
            '{"type":"agent_message_content_delta","delta":"lo"}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "hello"


def test_text_from_snake_case_item_completed():
    # Some codex versions emit type as snake_case(item_completed).
    stream = "\n".join(
        [
            '{"type":"item_completed","item":{"type":"agent_message","text":"snake answer"}}',
            '{"type":"turn_completed","usage":{"input_tokens":2,"output_tokens":1}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "snake answer"
    assert result.failed is False


def test_text_from_task_complete_last_agent_message():
    # Shape where task_complete(snake_case) carries last_agent_message.
    stream = '{"type":"task_complete","last_agent_message":"done","usage":{"input_tokens":4,"output_tokens":2}}'
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "done"
    assert result.tokens == 6


def test_consolidated_message_preferred_over_deltas():
    # Prefer the complete body(item.completed) when both deltas and a complete body exist.
    stream = "\n".join(
        [
            '{"type":"agent_message_content_delta","delta":"partial"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"complete answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "complete answer"


def test_snake_case_failed_event_marks_failed():
    # Also treat snake_case *_failed(turn_failed) as an error.
    stream = "\n".join(
        [
            '{"type":"turn_failed","message":"boom"}',
            '{"type":"turn_completed","usage":{"input_tokens":1,"output_tokens":0}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream, returncode=0)).__call__({"prompt": "x"}).observation
    assert result.failed is True
    assert result.error == "boom"


def test_error_event_marks_failed_even_on_zero_exit():
    stream = "\n".join(
        [
            '{"type":"turn.started"}',
            '{"type":"error","message":"stream interrupted"}',
            '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":0}}',
        ]
    )
    act = CodexAct(runner=_completed(stdout=stream, returncode=0))

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 0  # An error event marks failed even with zero exit.
    assert result.tokens == 3
    # Use the error event message as the error body, not the full JSONL stream.
    assert result.error == "stream interrupted"


def test_failed_event_type_marks_failed_even_on_zero_exit():
    # Also catch ``*.failed`` types(turn.failed / step.failed, etc.), not only ``error``.
    stream = "\n".join(
        [
            '{"type":"turn.started"}',
            '{"type":"step.failed","message":"step interrupted"}',
            '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":0}}',
        ]
    )
    act = CodexAct(runner=_completed(stdout=stream, returncode=0))

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 0
    assert result.tokens == 3
    assert result.error == "step interrupted"


def test_error_message_fallback_to_text_when_stderr_empty():
    # Non-zero exit with empty stderr and no error event. The error falls back to response text.
    runner = _completed(stdout="plain failure detail", stderr="", returncode=1)
    act = CodexAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 1
    assert result.error == "plain failure detail"
    assert "exit=" not in result.error


def test_error_message_fallback_to_exit_code_when_all_empty():
    # Non-zero exit with both stderr/stdout empty. Use the final fallback exit=<code>.
    runner = _completed(stdout="", stderr="", returncode=3)
    act = CodexAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.error == "exit=3"


def test_subprocess_capture_uses_utf8_with_replacement():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(runner=runner)
    act({"prompt": "hi"})

    _, kwargs = runner.calls[-1]
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"

def test_cwd_is_passed_to_subprocess():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(cwd="/tmp/work", runner=runner)
    act({"prompt": "hi"})
    _, kwargs = runner.calls[-1]
    assert kwargs["cwd"] == "/tmp/work"


# -- 3. Token usage parsing(Codex only counts input+output; subsets excluded) -------


def test_parse_tokens_from_jsonl_usage():
    assert parse_tokens(JSONL_OK) == 140


def test_parse_tokens_uses_last_usage_event():
    stream = "\n".join(
        [
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
            '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}',
        ]
    )
    assert parse_tokens(stream) == 10  # Use the last usage event.


def test_parse_tokens_regex_fallback_from_noisy_text():
    # Pick up representative keys even from mixed output that is not JSONL.
    noisy = 'log line\nusage: "input_tokens": 20, "output_tokens": 5 trailing'
    assert parse_tokens(noisy) == 25


def test_parse_tokens_regex_fallback_excludes_subset_keys():
    # Leading quote anchors prevent false matches on cached_input_tokens / reasoning_output_tokens.
    noisy = 'x "cached_input_tokens": 999 "reasoning_output_tokens": 888 end'
    assert parse_tokens(noisy) == 0


def test_parse_tokens_falls_back_to_total_tokens_when_no_split():
    # Usage with only total_tokens and no input/output uses total_tokens instead of 0.
    stream = '{"type":"turn.completed","usage":{"total_tokens":77}}'
    assert parse_tokens(stream) == 77


def test_parse_tokens_prefers_split_over_total_tokens():
    # Prefer input/output over total_tokens to avoid double-counting.
    stream = '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":999}}'
    assert parse_tokens(stream) == 15


def test_parse_tokens_regex_fallback_to_total_tokens():
    # Pick up total_tokens-only output even when it is not JSONL.
    assert parse_tokens('blah "total_tokens": 42 blah') == 42


def test_parse_tokens_returns_zero_when_absent():
    assert parse_tokens("just some plain text, no usage here") == 0


def test_parse_tokens_fallback_prefers_stdout_and_does_not_sum_across_sources():
    # Intentional behavior: if stdout has a hit, do not inspect stderr or sum across sources.
    # This avoids double-counting when both sources output tokens (same as ClaudeCodeAct;
    # codex usage is emitted to stdout by default with --json).
    stdout = '"input_tokens": 100'
    stderr = '"output_tokens": 40'
    assert parse_tokens(stdout, stderr) == 100  # stdout only. Do not add 40.
    # Fall back to stderr when stdout is empty.
    assert parse_tokens("", stderr) == 40


# -- 4. render_prompt / Mock placeholder formatting ----------------------------


def test_mock_renders_prompt_template():
    mock = MockCodexAct(responses=["ok"], prompt_template="step {iteration}")
    mock({"iteration": 3})
    assert mock.prompts == ["step 3"]


def test_render_prompt_missing_field_raises_helpful_error():
    with pytest.raises(KeyError) as exc:
        render_prompt("{prompt}", {"iteration": 1})
    msg = str(exc.value)
    assert "prompt" in msg
    assert "iteration" in msg  # Shows the available fields.


# -- 5. Real subprocess path(fake codex executable) -------------------


def _write_fake_codex(tmp_path: Path, body: str) -> str:
    """Create a fake codex executable that works on Windows and POSIX."""
    script = tmp_path / "fake_codex.py"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    if sys.platform == "win32":
        shim = tmp_path / "fake_codex.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(shim)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def test_real_subprocess_success(tmp_path):
    body = (
        "import sys, json\n"
        "prompt = sys.argv[-1]\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'echo: ' + prompt}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': "
        "{'input_tokens': 12, 'output_tokens': 8}}))\n"
    )
    bin_path = _write_fake_codex(tmp_path, body)
    act = CodexAct(codex_bin=bin_path)

    outcome = act({"prompt": "hello world"})
    assert outcome.observation.failed is False
    assert outcome.observation.text == "echo: hello world"
    assert outcome.tokens == 20


def test_real_subprocess_timeout(tmp_path):
    body = "import time\ntime.sleep(30)\n"
    bin_path = _write_fake_codex(tmp_path, body)
    act = CodexAct(codex_bin=bin_path, timeout=0.5)

    outcome = act({"prompt": "x"})  # Should be killed at 0.5s and returned as failed.
    assert outcome.observation.failed is True
    assert "timeout" in outcome.observation.error


def test_real_subprocess_inherits_and_overrides_env(tmp_path):
    # The child inherits os.environ (existing codex session / OPENAI_API_KEY path), and
    # values passed via env= are merged as overrides.
    body = (
        "import os, json\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', "
        "'text': os.environ.get('LOOP_AGENT_INHERITED', 'MISSING') + '|' + "
        "os.environ.get('LOOP_AGENT_MARKER', 'MISSING')}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'output_tokens': 1}}))\n"
    )
    bin_path = _write_fake_codex(tmp_path, body)
    os.environ["LOOP_AGENT_INHERITED"] = "from-parent"
    try:
        act = CodexAct(codex_bin=bin_path, env={"LOOP_AGENT_MARKER": "injected"})
        assert act({"prompt": "x"}).observation.text == "from-parent|injected"
    finally:
        del os.environ["LOOP_AGENT_INHERITED"]
