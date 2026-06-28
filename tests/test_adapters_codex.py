"""Codex adapter (``loop_agent.adapters.codex``) の **固有** 検証 (Issue #49)。

``act`` シーム 4 か条と ``ActResult`` の形(成功時の結果形 / ``failed`` セマンティクス /
timeout・起動失敗の graceful / 予算計上 / Mock 契約 / auth 環境継承 / stdin 安全性)は
全アダプタ横断の共通ハーネス ``tests/adapters/test_contract.py`` に移譲済み(stdin の
DEVNULL 固定もそこで ``expects_devnull`` 経由で検証する)。ここには **Codex 固有** の
挙動だけを残す:

1. ``build_command`` のフラグ組み立て(``codex exec`` / ``--json`` / ``-m`` / ``-c`` 等)。
2. ``--json`` JSONL の応答本文抽出(item.completed / 直接 agent_message / delta /
   last_agent_message、dotted/snake_case の揺れ)と ``error`` / ``*.failed`` の判定。
3. token usage 解析(Codex は ``input+output`` のみ。cached/reasoning の部分集合を
   二重計上しない / ``total_tokens`` フォールバック)。
4. ``cwd`` の引き渡しと ``render_prompt`` 整形。
5. 実 subprocess 経路(フェイク codex 実行ファイル)。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from loop_agent.adapters import CodexAct, MockCodexAct, render_prompt

# parse_tokens は codex サブモジュールから直接取る。``adapters.__init__`` が公開する
# ``parse_tokens`` は claude_code 由来(input+output+cache_creation を計上し
# cache_read は除外する意味論)で、Codex の usage(部分集合 cached/reasoning を持ち、
# cache_creation は無い)とは意味論が違うため、Codex 用はモジュール側を使う。
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


# -- 1. build_command(Codex 固有フラグ) ------------------------------------


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


# -- 2. 応答本文抽出(JSONL スキーマの揺れ)と error/*.failed 判定 -----------


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


def test_cwd_is_passed_to_subprocess():
    runner = _completed(stdout=JSONL_OK)
    act = CodexAct(cwd="/tmp/work", runner=runner)
    act({"prompt": "hi"})
    _, kwargs = runner.calls[-1]
    assert kwargs["cwd"] == "/tmp/work"


# -- 3. token usage パース(Codex は input+output のみ; 部分集合を除外) -------


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


# -- 4. render_prompt / Mock のプレースホルダ整形 ----------------------------


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


# -- 5. 実 subprocess 経路(フェイク codex 実行ファイル) -------------------


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
