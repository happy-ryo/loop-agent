"""Claude Code adapter (``loop_agent.adapters.claude_code``) の検証 (Issue #32)。

ここで確かめる命題:

1. subprocess 成功時、応答テキストとトークンを載せた ``ActOutcome`` を返す。
2. timeout 超過は例外ではなく ``failed=True`` の結果で graceful に返り、ループを
   殺さない(境界の ``MaxIterations`` 等が効く)。
3. token usage を JSON ``usage`` / stream-json / 正規表現フォールバックで解析し、
   ``state.tokens_used`` に積んで ``TokenBudget`` を効かせられる。
4. :class:`MockClaudeCodeAct` が subprocess 無しで同じ ``act`` 契約を満たす。
5. ``prompt_template`` のプレースホルダが context のフィールドで埋まる。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loop_agent import MaxIterations, TokenBudget, VerifyOutcome, run_loop
from loop_agent.adapters import (
    ClaudeCodeAct,
    ClaudeCodeResult,
    MockClaudeCodeAct,
    parse_tokens,
    render_prompt,
)


# -- フェイク runner: subprocess.run を差し替えてコマンド/出力を制御する --------


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """``subprocess.run`` 互換の戻り値(CompletedProcess)を作る。"""

    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


def _timeout_runner(timeout_value: float = 600.0):
    """常に :class:`subprocess.TimeoutExpired` を送出する runner。"""

    def _runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_value)

    return _runner


JSON_OK = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}'
)


# -- 1. subprocess 成功時の ActOutcome ------------------------------------


def test_success_returns_actoutcome_with_text_and_tokens():
    runner = _completed(stdout=JSON_OK, returncode=0)
    act = ClaudeCodeAct(runner=runner)

    outcome = act({"prompt": "fix the bug"})

    assert outcome.tokens == 155  # 100 + 40 + 10 + 5
    result = outcome.observation
    assert isinstance(result, ClaudeCodeResult)
    assert result.failed is False
    assert result.text == "done fixing"
    assert result.returncode == 0
    assert str(result) == "done fixing"  # __str__ は本文を返す


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
    # プロンプトは "--" 区切りの後ろの位置引数(可変長オプションに飲まれないため)。
    assert cmd[-2:] == ["--", "the prompt"]


def test_nonzero_exit_is_failed():
    runner = _completed(stdout="", stderr="boom", returncode=2)
    act = ClaudeCodeAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 2
    assert "boom" in result.error


def test_json_is_error_marks_failed_even_on_zero_exit():
    runner = _completed(
        stdout='{"is_error": true, "result": "rate limited", "usage": {"input_tokens": 3}}',
        returncode=0,
    )
    act = ClaudeCodeAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.tokens == 3


# -- 2. timeout は graceful(例外を投げない)--------------------------------


def test_timeout_returns_failed_without_raising():
    act = ClaudeCodeAct(timeout=0.01, runner=_timeout_runner(0.01))

    outcome = act({"prompt": "long task"})  # 例外が漏れないこと自体が検証点

    assert outcome.tokens == 0
    result = outcome.observation
    assert result.failed is True
    assert "timeout" in result.error


def test_timeout_does_not_kill_the_loop():
    # timeout が続いても run_loop は MaxIterations で必ず止まる。
    act = ClaudeCodeAct(timeout=0.01, runner=_timeout_runner(0.01))

    def verify(outcome):
        return VerifyOutcome(goal_met=not outcome.observation.failed)

    result = run_loop(
        act=act,
        verify=verify,
        gather=lambda state: {"prompt": "keep trying"},
        conditions=[MaxIterations(3)],
    )
    assert result.status == "stopped"
    assert result.stop.name == "max_iterations"
    assert result.iterations == 3


def test_missing_executable_is_graceful():
    act = ClaudeCodeAct(claude_bin="claude-does-not-exist-xyz")
    outcome = act({"prompt": "x"})
    assert outcome.observation.failed is True
    assert "could not launch" in outcome.observation.error


# -- 3. token usage パース --------------------------------------------------


def test_parse_tokens_from_json_usage():
    assert parse_tokens(JSON_OK) == 155


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
    # JSON にならない混在出力でも代表キーを拾う。
    noisy = 'log line\nusage: "input_tokens": 20, "output_tokens": 5 trailing'
    assert parse_tokens(noisy) == 25


def test_parse_tokens_returns_zero_when_absent():
    assert parse_tokens("just some plain text, no usage here") == 0


def test_tokens_accumulate_into_token_budget():
    # 1 反復 1200 tokens。予算 2000 なら 2 反復目を始めず止まる
    # (TokenBudget は境界評価: 1200 -> 2400 で次の guard が発火)。
    mock = MockClaudeCodeAct(responses=[{"text": "step", "tokens": 1200}])

    result = run_loop(
        act=mock,
        verify=lambda o: VerifyOutcome(goal_met=False),
        gather=lambda s: {"prompt": "go"},
        conditions=[TokenBudget(2000), MaxIterations(100)],
    )
    assert result.stop.name == "token_budget"
    assert result.tokens_used == 2400  # 2 反復計上後に境界で停止
    assert result.iterations == 2


# -- 4. MockClaudeCodeAct --------------------------------------------------


def test_mock_cycles_then_sticks_to_last():
    mock = MockClaudeCodeAct(
        responses=["first", {"text": "second", "tokens": 5}]
    )
    a = mock({"prompt": "p1"})
    b = mock({"prompt": "p2"})
    c = mock({"prompt": "p3"})  # 使い切ったら最後に張り付く

    assert a.observation.text == "first"
    assert b.observation.text == "second" and b.tokens == 5
    assert c.observation.text == "second"
    assert mock.prompts == ["p1", "p2", "p3"]


def test_mock_failed_response_drives_verify():
    mock = MockClaudeCodeAct(responses=[{"failed": True, "error": "nope"}])
    outcome = mock({"prompt": "x"})
    assert outcome.observation.failed is True
    assert outcome.observation.error == "nope"


def test_mock_requires_at_least_one_response():
    with pytest.raises(ValueError):
        MockClaudeCodeAct(responses=[])


def test_mock_reaches_goal_through_run_loop():
    mock = MockClaudeCodeAct(responses=["work", "work", "done"])

    def verify(outcome):
        met = outcome.observation.text == "done"
        return VerifyOutcome(goal_met=met)

    result = run_loop(
        act=mock,
        verify=verify,
        gather=lambda s: {"prompt": "iterate"},
        conditions=[MaxIterations(10)],
    )
    assert result.goal_met is True
    assert result.iterations == 3


# -- 5. prompt placeholder -------------------------------------------------


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
    assert "iteration" in msg  # 使えるフィールドを示す


def test_placeholder_reaches_the_command():
    runner = _completed(stdout=JSON_OK)
    act = ClaudeCodeAct(prompt_template="fix iter {iteration}", runner=runner)
    act({"iteration": 7})
    sent_command = runner.calls[-1][0]
    assert sent_command[-1] == "fix iter 7"


# -- 実 subprocess 経路(フェイク claude 実行ファイル)----------------------


def _write_fake_claude(tmp_path: Path, body: str) -> str:
    """``sys.executable`` をインタプリタにした実行可能なフェイク claude を作る。"""
    script = tmp_path / "fake_claude.py"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
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

    outcome = act({"prompt": "x"})  # 0.5s で kill され failed で返るはず
    assert outcome.observation.failed is True
    assert "timeout" in outcome.observation.error


def test_real_subprocess_inherits_and_overrides_env(tmp_path):
    # 子は os.environ を継承し(既存 claude セッション / ANTHROPIC_API_KEY 経路)、
    # env= で渡した値を上書きマージする。
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
