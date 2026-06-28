"""全 act アダプタ横断の共通契約テスト(Issue #52)。

:data:`tests.adapters.conftest.ADAPTER_SPECS` に登録された各アダプタ
(:class:`ClaudeCodeAct` / :class:`CodexAct` / 将来追加分)に対して、``act`` シームの
4 か条と :class:`ActResult` の形を 1 つの parametrize 群で検証する。アダプタ固有の
output/token スキーマ(claude の stream-json / codex の JSONL イベント揺れ等)は
``test_adapters_claude_code.py`` / ``test_adapters_codex.py`` に残し、ここでは
**どのアダプタでも同一であるべき契約** だけを扱う。
"""

from __future__ import annotations

import subprocess

import pytest

from loop_agent import MaxIterations, TokenBudget, VerifyOutcome, run_loop
from loop_agent.adapters import ActResult, ActResultBase


# -- 結果の形(ActResult 契約) ---------------------------------------------


def test_success_result_shape(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout, returncode=0)
    act = adapter_spec.make_act(runner=runner)

    outcome = act({"prompt": "fix the bug"})

    result = outcome.observation
    assert isinstance(result, adapter_spec.result_cls)
    # 観測オブジェクトは共通契約に構造適合する(異種アダプタ合成のため)。
    assert isinstance(result, ActResult)
    assert isinstance(result, ActResultBase)
    assert result.failed is False
    assert result.text == adapter_spec.success_text
    assert result.returncode == 0
    assert str(result) == adapter_spec.success_text  # __str__ は本文を返す
    assert outcome.tokens == adapter_spec.success_tokens
    assert result.tokens == adapter_spec.success_tokens
    # command は実行した引数列(tuple)で、末尾はプロンプトの位置引数。
    assert isinstance(result.command, tuple)
    assert result.command[-2:] == ("--", "fix the bug")


def test_result_has_all_eight_fields(adapter_spec):
    # 8 フィールドを ActResultBase から継承し、再定義していないこと。
    import dataclasses

    names = [f.name for f in dataclasses.fields(adapter_spec.result_cls)]
    assert names == [
        "text",
        "tokens",
        "failed",
        "returncode",
        "error",
        "stdout",
        "stderr",
        "command",
    ]


# -- failed セマンティクス(例外でなく failed で graceful) -------------------


def test_nonzero_exit_is_failed(adapter_spec, make_runner):
    runner = make_runner(stdout="", stderr="boom", returncode=2)
    act = adapter_spec.make_act(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 2
    assert "boom" in result.error


def test_timeout_returns_failed_without_raising(adapter_spec, make_timeout_runner):
    act = adapter_spec.make_act(timeout=0.01, runner=make_timeout_runner(0.01))

    outcome = act({"prompt": "long task"})  # 例外が漏れないこと自体が検証点

    assert outcome.tokens == 0
    result = outcome.observation
    assert result.failed is True
    assert "timeout" in result.error


def test_missing_executable_is_graceful(adapter_spec):
    act = adapter_spec.make_act(**{adapter_spec.bin_kwarg: f"{adapter_spec.name}-does-not-exist-xyz"})
    outcome = act({"prompt": "x"})
    assert outcome.observation.failed is True
    assert "could not launch" in outcome.observation.error


def test_timeout_does_not_kill_the_loop(adapter_spec, make_timeout_runner):
    # timeout が続いても run_loop は MaxIterations で必ず止まる(境界 guard が効く)。
    act = adapter_spec.make_act(timeout=0.01, runner=make_timeout_runner(0.01))

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


# -- token: 二重計上ガード(Issue #55 の bug class を構造的に catch) ----------


def test_token_guard_no_double_count(adapter_spec):
    # 部分集合キー(codex の cached/reasoning 等)を含む usage でも、そのアダプタの
    # 意味論で正しい総量になる。二重計上していたら不一致で落ちる。
    assert adapter_spec.parse_tokens(adapter_spec.token_guard_stdout) == adapter_spec.token_guard_expected


def test_token_guard_via_actoutcome(adapter_spec, make_runner):
    # parse_tokens 単体だけでなく、__call__ -> ActOutcome.tokens の経路でも
    # 二重計上しないことを固定する(driver が積む値の正しさ)。
    runner = make_runner(stdout=adapter_spec.token_guard_stdout)
    outcome = adapter_spec.make_act(runner=runner)({"prompt": "x"})
    assert outcome.tokens == adapter_spec.token_guard_expected


def test_tokens_accumulate_into_token_budget(adapter_spec):
    # 1 反復 1200 tokens。予算 2000 なら 2 反復計上後に境界で止まる。
    mock = adapter_spec.mock_cls(responses=[{"text": "step", "tokens": 1200}])

    result = run_loop(
        act=mock,
        verify=lambda o: VerifyOutcome(goal_met=False),
        gather=lambda s: {"prompt": "go"},
        conditions=[TokenBudget(2000), MaxIterations(100)],
    )
    assert result.stop.name == "token_budget"
    assert result.tokens_used == 2400
    assert result.iterations == 2


# -- auth: 環境継承 + env= 上書きマージ(CLI に委譲) -------------------------


def test_env_inherits_and_overrides(adapter_spec, make_runner, monkeypatch):
    # 子は os.environ を継承し(既存 CLI セッション / API キー経路)、env= で渡した
    # 値を上書きマージする。subprocess に渡る env を runner 経由で覗いて固定する。
    monkeypatch.setenv("LOOP_AGENT_INHERITED", "from-parent")
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(runner=runner, env={"LOOP_AGENT_MARKER": "injected"})

    act({"prompt": "x"})

    _, kwargs = runner.calls[-1]
    passed_env = kwargs["env"]
    assert passed_env["LOOP_AGENT_INHERITED"] == "from-parent"
    assert passed_env["LOOP_AGENT_MARKER"] == "injected"


# -- stdin 安全性(対話入力読み込みによるハング防止) -------------------------


def test_stdin_safety(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(runner=runner)

    act({"prompt": "hi"})

    sent_command, kwargs = runner.calls[-1]
    # プロンプトは "--" の後ろの位置引数に確定している(可変長オプションに飲まれない)。
    assert sent_command[-2:] == ["--", "hi"]
    if adapter_spec.expects_devnull:
        assert kwargs.get("stdin") == subprocess.DEVNULL
    else:
        # stdin を明示しないアダプタは親環境を継承する(DEVNULL を強制しない)。
        assert "stdin" not in kwargs


# -- prompt placeholder の整形(全アダプタ共通の render_prompt) ---------------


def test_placeholder_reaches_the_command(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(prompt_template="fix iter {iteration}", runner=runner)

    act({"iteration": 7})

    sent_command = runner.calls[-1][0]
    assert sent_command[-1] == "fix iter 7"


# -- Mock 契約(subprocess 無しで同じ act 契約) -----------------------------


def test_mock_cycles_then_sticks_to_last(adapter_spec):
    mock = adapter_spec.mock_cls(responses=["first", {"text": "second", "tokens": 5}])
    a = mock({"prompt": "p1"})
    b = mock({"prompt": "p2"})
    c = mock({"prompt": "p3"})  # 使い切ったら最後に張り付く

    assert a.observation.text == "first"
    assert b.observation.text == "second" and b.tokens == 5
    assert c.observation.text == "second"
    assert mock.prompts == ["p1", "p2", "p3"]


def test_mock_failed_response_drives_verify(adapter_spec):
    mock = adapter_spec.mock_cls(responses=[{"failed": True, "error": "nope"}])
    outcome = mock({"prompt": "x"})
    assert outcome.observation.failed is True
    assert outcome.observation.error == "nope"


def test_mock_requires_at_least_one_response(adapter_spec):
    with pytest.raises(ValueError):
        adapter_spec.mock_cls(responses=[])


def test_mock_rejects_unsupported_response_type(adapter_spec):
    with pytest.raises(TypeError):
        adapter_spec.mock_cls(responses=[object()])


def test_mock_reaches_goal_through_run_loop(adapter_spec):
    mock = adapter_spec.mock_cls(responses=["work", "work", "done"])

    def verify(outcome):
        return VerifyOutcome(goal_met=outcome.observation.text == "done")

    result = run_loop(
        act=mock,
        verify=verify,
        gather=lambda s: {"prompt": "iterate"},
        conditions=[MaxIterations(10)],
    )
    assert result.goal_met is True
    assert result.iterations == 3
