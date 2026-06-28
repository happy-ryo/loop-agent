"""Codex adapter (``loop_agent.adapters.codex``) の検証 (Issue #49)。

ClaudeCodeAct (PR #47) と完全同型。ここで確かめる命題:

1. subprocess 成功時、応答テキストとトークンを載せた ``ActOutcome`` を返す。
2. timeout 超過は例外ではなく ``failed=True`` の結果で graceful に返り、ループを
   殺さない(境界の ``MaxIterations`` 等が効く)。
3. token usage を JSONL ``turn.completed`` の ``usage`` / 正規表現フォールバックで
   解析し、``state.tokens_used`` に積んで ``TokenBudget`` を効かせられる。Codex の
   ``cached_input_tokens`` / ``reasoning_output_tokens`` は部分集合なので二重計上しない。
4. :class:`MockCodexAct` が subprocess 無しで同じ ``act`` 契約を満たす。
5. ``prompt_template`` のプレースホルダが context のフィールドで埋まる。
6. プロンプトは ``--`` 区切りの後ろの位置引数に確定し、stdin は DEVNULL に固定される。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loop_agent import MaxIterations, TokenBudget, VerifyOutcome, run_loop

# CodexAct / CodexResult / MockCodexAct はパッケージ公開 API から。
from loop_agent.adapters import CodexAct, CodexResult, MockCodexAct, render_prompt

# parse_tokens は codex サブモジュールから直接取る。``adapters.__init__`` が公開する
# ``parse_tokens`` は claude_code 由来(全 *tokens* を合算)で、Codex の部分集合
# (cached/reasoning)を二重計上してしまうため、Codex 用はモジュール側を使う。
from loop_agent.adapters.codex import parse_tokens


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


# 実際の ``codex exec --json`` イベント列を模した JSONL。トークンは
# input(100) + output(40) = 140 のみが総量(cached 60 / reasoning 10 は部分集合)。
JSONL_OK = "\n".join(
    [
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"done fixing"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":60,'
        '"output_tokens":40,"reasoning_output_tokens":10}}',
    ]
)


# -- 1. subprocess 成功時の ActOutcome ------------------------------------


def test_success_returns_actoutcome_with_text_and_tokens():
    runner = _completed(stdout=JSONL_OK, returncode=0)
    act = CodexAct(runner=runner)

    outcome = act({"prompt": "fix the bug"})

    assert outcome.tokens == 140  # input 100 + output 40(部分集合は加えない)
    result = outcome.observation
    assert isinstance(result, CodexResult)
    assert result.failed is False
    assert result.text == "done fixing"
    assert result.returncode == 0
    assert str(result) == "done fixing"  # __str__ は本文を返す


def test_build_command_includes_all_flags():
    act = CodexAct(
        model="gpt-5.5",
        effort="high",
        sandbox="workspace-write",
        allowed_args=["--add-dir", "/tmp/x"],
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
    # プロンプトは "--" 区切りの後ろの位置引数(値取りオプションに飲まれないため)。
    assert cmd[-2:] == ["--", "the prompt"]


def test_build_command_minimal_omits_optional_flags():
    # json/skip を切ると付かない。sandbox 未指定なら -s も付かない。
    act = CodexAct(json_output=False, skip_git_repo_check=False)
    cmd = act.build_command("p")
    assert "--json" not in cmd
    assert "--skip-git-repo-check" not in cmd
    assert "-s" not in cmd
    assert cmd[-2:] == ["--", "p"]


def test_nonzero_exit_is_failed():
    runner = _completed(stdout="", stderr="boom", returncode=2)
    act = CodexAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 2
    assert "boom" in result.error


def test_text_from_direct_agent_message_event():
    # 旧 --json 形: item.completed ラッパ無しの直接 agent_message イベント。
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
    # 完了イベントが last_agent_message を別フィールドで持つ形。
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
    # codex のバージョンによっては type が snake_case(item_completed)で出る。
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
    # task_complete(snake_case)が last_agent_message を持つ形。
    stream = '{"type":"task_complete","last_agent_message":"done","usage":{"input_tokens":4,"output_tokens":2}}'
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "done"
    assert result.tokens == 6


def test_snake_case_failed_event_marks_failed():
    # snake_case の *_failed(turn_failed)もエラーとして拾う。
    stream = "\n".join(
        [
            '{"type":"turn_failed","message":"boom"}',
            '{"type":"turn_completed","usage":{"input_tokens":1,"output_tokens":0}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream, returncode=0)).__call__({"prompt": "x"}).observation
    assert result.failed is True
    assert result.error == "boom"


def test_consolidated_message_preferred_over_deltas():
    # delta と完全本文が両方あれば完全本文(item.completed)を優先する。
    stream = "\n".join(
        [
            '{"type":"agent_message_content_delta","delta":"partial"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"complete answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
        ]
    )
    result = CodexAct(runner=_completed(stdout=stream)).__call__({"prompt": "x"}).observation
    assert result.text == "complete answer"


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
    assert result.returncode == 0  # zero exit でも error イベントで failed になる
    assert result.tokens == 3
    # error 本文は error イベントの message を採り、JSONL 全文を載せない。
    assert result.error == "stream interrupted"


def test_failed_event_type_marks_failed_even_on_zero_exit():
    # ``error`` 型だけでなく ``*.failed`` 型(turn.failed / step.failed 等)も拾う。
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
    # stderr が空・error イベントも無いが非 0 終了。error は応答本文へフォールバック。
    runner = _completed(stdout="plain failure detail", stderr="", returncode=1)
    act = CodexAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 1
    assert result.error == "plain failure detail"
    assert "exit=" not in result.error


def test_error_message_fallback_to_exit_code_when_all_empty():
    # stderr / stdout ともに空で非 0 終了。最終フォールバックの exit=<code> を使う。
    runner = _completed(stdout="", stderr="", returncode=3)
    act = CodexAct(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.error == "exit=3"


def test_stdin_is_devnull_and_command_passed():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(runner=runner)
    act({"prompt": "hi"})
    sent_command, kwargs = runner.calls[-1]
    assert kwargs["stdin"] == subprocess.DEVNULL  # codex の stdin 誤読/ハング防止
    assert sent_command[-2:] == ["--", "hi"]


def test_cwd_is_passed_to_subprocess():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(cwd="/tmp/work", runner=runner)
    act({"prompt": "hi"})
    _, kwargs = runner.calls[-1]
    assert kwargs["cwd"] == "/tmp/work"


# -- 2. timeout は graceful(例外を投げない)--------------------------------


def test_timeout_returns_failed_without_raising():
    act = CodexAct(timeout=0.01, runner=_timeout_runner(0.01))

    outcome = act({"prompt": "long task"})  # 例外が漏れないこと自体が検証点

    assert outcome.tokens == 0
    result = outcome.observation
    assert result.failed is True
    assert "timeout" in result.error


def test_timeout_does_not_kill_the_loop():
    # timeout が続いても run_loop は MaxIterations で必ず止まる。
    act = CodexAct(timeout=0.01, runner=_timeout_runner(0.01))

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
    act = CodexAct(codex_bin="codex-does-not-exist-xyz")
    outcome = act({"prompt": "x"})
    assert outcome.observation.failed is True
    assert "could not launch" in outcome.observation.error


# -- 3. token usage パース --------------------------------------------------


def test_parse_tokens_from_jsonl_usage():
    assert parse_tokens(JSONL_OK) == 140


def test_parse_tokens_uses_last_usage_event():
    stream = "\n".join(
        [
            '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
            '{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}',
        ]
    )
    assert parse_tokens(stream) == 10  # 最後の usage を採用


def test_parse_tokens_regex_fallback_from_noisy_text():
    # JSONL にならない混在出力でも代表キーを拾う。
    noisy = 'log line\nusage: "input_tokens": 20, "output_tokens": 5 trailing'
    assert parse_tokens(noisy) == 25


def test_parse_tokens_regex_fallback_excludes_subset_keys():
    # cached_input_tokens / reasoning_output_tokens は先頭引用符アンカーで誤マッチしない。
    noisy = 'x "cached_input_tokens": 999 "reasoning_output_tokens": 888 end'
    assert parse_tokens(noisy) == 0


def test_parse_tokens_falls_back_to_total_tokens_when_no_split():
    # input/output が無く total_tokens だけの usage はそれを使う(0 にしない)。
    stream = '{"type":"turn.completed","usage":{"total_tokens":77}}'
    assert parse_tokens(stream) == 77


def test_parse_tokens_prefers_split_over_total_tokens():
    # input/output があれば total_tokens は使わない(二重計上を避ける)。
    stream = '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":999}}'
    assert parse_tokens(stream) == 15


def test_parse_tokens_regex_fallback_to_total_tokens():
    # JSONL にならない出力でも total_tokens のみなら拾う。
    assert parse_tokens('blah "total_tokens": 42 blah') == 42


def test_parse_tokens_returns_zero_when_absent():
    assert parse_tokens("just some plain text, no usage here") == 0


def test_parse_tokens_fallback_prefers_stdout_and_does_not_sum_across_sources():
    # 意図的な挙動の固定: stdout にヒットがあれば stderr は見ない(ソース間で合算
    # しない)。両ソースがトークンを出力した場合の二重計上を避けるため
    # (ClaudeCodeAct と同じ。codex の usage は --json の stdout に出るのが既定)。
    stdout = '"input_tokens": 100'
    stderr = '"output_tokens": 40'
    assert parse_tokens(stdout, stderr) == 100  # stdout 単独。40 は加えない。
    # stdout が空なら stderr にフォールバックする。
    assert parse_tokens("", stderr) == 40


def test_tokens_accumulate_into_token_budget():
    # 1 反復 1200 tokens。予算 2000 なら 2 反復目を始めず止まる
    # (TokenBudget は境界評価: 1200 -> 2400 で次の guard が発火)。
    mock = MockCodexAct(responses=[{"text": "step", "tokens": 1200}])

    result = run_loop(
        act=mock,
        verify=lambda o: VerifyOutcome(goal_met=False),
        gather=lambda s: {"prompt": "go"},
        conditions=[TokenBudget(2000), MaxIterations(100)],
    )
    assert result.stop.name == "token_budget"
    assert result.tokens_used == 2400  # 2 反復計上後に境界で停止
    assert result.iterations == 2


# -- 4. MockCodexAct -------------------------------------------------------


def test_mock_cycles_then_sticks_to_last():
    mock = MockCodexAct(responses=["first", {"text": "second", "tokens": 5}])
    a = mock({"prompt": "p1"})
    b = mock({"prompt": "p2"})
    c = mock({"prompt": "p3"})  # 使い切ったら最後に張り付く

    assert a.observation.text == "first"
    assert b.observation.text == "second" and b.tokens == 5
    assert c.observation.text == "second"
    assert mock.prompts == ["p1", "p2", "p3"]


def test_mock_failed_response_drives_verify():
    mock = MockCodexAct(responses=[{"failed": True, "error": "nope"}])
    outcome = mock({"prompt": "x"})
    assert outcome.observation.failed is True
    assert outcome.observation.error == "nope"


def test_mock_requires_at_least_one_response():
    with pytest.raises(ValueError):
        MockCodexAct(responses=[])


def test_mock_rejects_unsupported_response_type():
    with pytest.raises(TypeError):
        MockCodexAct(responses=[object()])


def test_mock_reaches_goal_through_run_loop():
    mock = MockCodexAct(responses=["work", "work", "done"])

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


def test_placeholder_reaches_the_command():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(prompt_template="fix iter {iteration}", runner=runner)
    act({"iteration": 7})
    sent_command = runner.calls[-1][0]
    assert sent_command[-1] == "fix iter 7"


def test_mock_renders_prompt_template():
    mock = MockCodexAct(responses=["ok"], prompt_template="step {iteration}")
    mock({"iteration": 3})
    assert mock.prompts == ["step 3"]


def test_render_prompt_missing_field_raises_helpful_error():
    with pytest.raises(KeyError) as exc:
        render_prompt("{prompt}", {"iteration": 1})
    msg = str(exc.value)
    assert "prompt" in msg
    assert "iteration" in msg  # 使えるフィールドを示す


# -- 実 subprocess 経路(フェイク codex 実行ファイル)----------------------


def _write_fake_codex(tmp_path: Path, body: str) -> str:
    """``sys.executable`` をインタプリタにした実行可能なフェイク codex を作る。"""
    script = tmp_path / "fake_codex.py"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
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

    outcome = act({"prompt": "x"})  # 0.5s で kill され failed で返るはず
    assert outcome.observation.failed is True
    assert "timeout" in outcome.observation.error


def test_real_subprocess_inherits_and_overrides_env(tmp_path):
    # 子は os.environ を継承し(既存 codex セッション / OPENAI_API_KEY 経路)、
    # env= で渡した値を上書きマージする。
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
