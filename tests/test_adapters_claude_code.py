"""Claude Code adapter (``loop_agent.adapters.claude_code``) の **固有** 検証 (Issue #32)。

``act`` シーム 4 か条と ``ActResult`` の形(成功時の結果形 / ``failed`` セマンティクス /
timeout・起動失敗の graceful / 予算計上 / Mock 契約 / auth 環境継承 / stdin 安全性)は
全アダプタ横断の共通ハーネス ``tests/adapters/test_contract.py`` に移譲済み。ここには
**Claude Code 固有** の挙動だけを残す:

1. ``build_command`` のフラグ組み立て(``--print`` / ``--output-format`` 等)。
2. ``--output-format json`` の ``is_error`` を失敗判定に使う。
3. token usage を JSON ``usage`` / stream-json / 正規表現フォールバックで解析する
   (Claude は input+output+cache_creation を計上し ``cache_read`` を除外する意味論;
   Issue #55)。
4. ``render_prompt`` のプレースホルダ整形(Mapping / ``LoopState`` / 素の文字列 / 欠落)。
5. 実 subprocess 経路(フェイク claude 実行ファイル)。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loop_agent.adapters import ClaudeCodeAct, parse_tokens, render_prompt


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


JSON_OK = (
    '{"type": "result", "subtype": "success", "is_error": false, '
    '"result": "done fixing", '
    '"usage": {"input_tokens": 100, "output_tokens": 40, '
    '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 5}}'
)


# -- 1. build_command(Claude 固有フラグ) -----------------------------------


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


# -- 2. JSON is_error を失敗判定に使う(Claude 固有) -------------------------


def test_json_is_error_marks_failed_even_on_zero_exit():
    runner = _completed(
        stdout='{"is_error": true, "result": "rate limited", "usage": {"input_tokens": 3}}',
        returncode=0,
    )
    act = ClaudeCodeAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.tokens == 3


# -- 3. token usage パース(input+output+cache_creation を計上、cache_read は除外) --


def test_parse_tokens_from_json_usage():
    # 100(input)+40(output)+10(cache_creation)=150。cache_read(=5)は計上しない。
    assert parse_tokens(JSON_OK) == 150


def test_parse_tokens_excludes_cache_read():
    # Issue #55 の再現/回帰ガード: cache_read を巨大値にしても計上に含めない。
    # 修正前(全 *tokens* 合算)なら 100+40+10+999999 で落ちる。修正後は 150。
    payload = (
        '{"type": "result", "is_error": false, "result": "ok", '
        '"usage": {"input_tokens": 100, "output_tokens": 40, '
        '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999}}'
    )
    assert parse_tokens(payload) == 150


def test_parse_tokens_regex_fallback_excludes_cache_read():
    # 構造化 JSON にならない混在出力でも、フォールバック正規表現は cache_read を拾わない。
    noisy = (
        'log\n"input_tokens": 100, "output_tokens": 40, '
        '"cache_creation_input_tokens": 10, "cache_read_input_tokens": 999999 trailing'
    )
    assert parse_tokens(noisy) == 150


def test_actoutcome_tokens_exclude_cache_read():
    # __call__ -> ActOutcome.tokens の経路でも cache_read を除外する(driver が積む値)。
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
    # JSON にならない混在出力でも代表キーを拾う。
    noisy = 'log line\nusage: "input_tokens": 20, "output_tokens": 5 trailing'
    assert parse_tokens(noisy) == 25


def test_parse_tokens_returns_zero_when_absent():
    assert parse_tokens("just some plain text, no usage here") == 0


# -- 4. render_prompt(プレースホルダ整形) ----------------------------------


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


# -- 5. 実 subprocess 経路(フェイク claude 実行ファイル) -------------------


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


