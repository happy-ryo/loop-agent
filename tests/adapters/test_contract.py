"""Common contract tests across all act adapters (Issue #52).

For each adapter registered in :data:`tests.adapters.conftest.ADAPTER_SPECS`
(:class:`ClaudeCodeAct` / :class:`CodexAct` / future additions), validate the
four-part ``act`` seam and the shape of :class:`ActResult` in one parametrized
group. Adapter-specific output/token schemas (claude stream-json, codex JSONL
event variations, etc.) stay in ``test_adapters_claude_code.py`` /
``test_adapters_codex.py``; this file covers only the **contracts that should be
identical for every adapter**.
"""

from __future__ import annotations

import subprocess

import pytest

from loop_agent import MaxIterations, TokenBudget, VerifyOutcome, run_loop
from loop_agent.adapters import ActResult, ActResultBase


# -- Result shape (ActResult contract) -------------------------------------


def test_success_result_shape(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout, returncode=0)
    act = adapter_spec.make_act(runner=runner)

    outcome = act({"prompt": "fix the bug"})

    result = outcome.observation
    assert isinstance(result, adapter_spec.result_cls)
    # The observation object structurally matches the common contract so
    # heterogeneous adapters can be composed.
    assert isinstance(result, ActResult)
    assert isinstance(result, ActResultBase)
    assert result.failed is False
    assert result.text == adapter_spec.success_text
    assert result.returncode == 0
    assert str(result) == adapter_spec.success_text  # __str__ returns the body.
    assert outcome.tokens == adapter_spec.success_tokens
    assert result.tokens == adapter_spec.success_tokens
    # command is the executed argument sequence (tuple), ending with the prompt
    # positional argument.
    assert isinstance(result.command, tuple)
    assert result.command[-2:] == ("--", "fix the bug")


def test_result_has_all_eight_fields(adapter_spec):
    # The eight fields are inherited from ActResultBase and are not redefined.
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


# -- failed semantics (graceful failed state, not exceptions) ---------------


def test_nonzero_exit_is_failed(adapter_spec, make_runner):
    runner = make_runner(stdout="", stderr="boom", returncode=2)
    act = adapter_spec.make_act(runner=runner)

    result = act({"prompt": "x"}).observation
    assert result.failed is True
    assert result.returncode == 2
    assert "boom" in result.error


def test_timeout_returns_failed_without_raising(adapter_spec, make_timeout_runner):
    act = adapter_spec.make_act(timeout=0.01, runner=make_timeout_runner(0.01))

    outcome = act({"prompt": "long task"})  # Verifies exceptions do not leak.

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
    # Even repeated timeouts must stop at MaxIterations, proving the boundary
    # guard works.
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


# -- token: double-counting guard (structurally catches Issue #55 bug class) -


def test_token_guard_no_double_count(adapter_spec):
    # Even usage with subset keys (codex cached/reasoning, etc.) produces the
    # correct total under that adapter's semantics. Double counting fails this.
    assert adapter_spec.parse_tokens(adapter_spec.token_guard_stdout) == adapter_spec.token_guard_expected


def test_token_guard_via_actoutcome(adapter_spec, make_runner):
    # Lock down no double counting not only in parse_tokens alone, but also
    # through the __call__ -> ActOutcome.tokens path (the value drivers use).
    runner = make_runner(stdout=adapter_spec.token_guard_stdout)
    outcome = adapter_spec.make_act(runner=runner)({"prompt": "x"})
    assert outcome.tokens == adapter_spec.token_guard_expected


def test_tokens_accumulate_into_token_budget(adapter_spec):
    # One iteration costs 1200 tokens. With a 2000-token budget, the boundary
    # stops after charging two iterations.
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


# -- auth: environment inheritance + env= override merge (delegated to CLI) -


def test_env_inherits_and_overrides(adapter_spec, make_runner, monkeypatch):
    # The child inherits os.environ (existing CLI sessions / API key paths), then
    # merges env= values as overrides. Inspect the env passed to subprocess via
    # the runner to lock this down.
    monkeypatch.setenv("LOOP_AGENT_INHERITED", "from-parent")
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(runner=runner, env={"LOOP_AGENT_MARKER": "injected"})

    act({"prompt": "x"})

    _, kwargs = runner.calls[-1]
    passed_env = kwargs["env"]
    assert passed_env["LOOP_AGENT_INHERITED"] == "from-parent"
    assert passed_env["LOOP_AGENT_MARKER"] == "injected"


# -- stdin safety (prevent hangs from reading interactive input) ------------


def test_stdin_safety(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(runner=runner)

    act({"prompt": "hi"})

    sent_command, kwargs = runner.calls[-1]
    # The prompt is fixed as the positional argument after "--" so variadic
    # options cannot consume it.
    assert sent_command[-2:] == ["--", "hi"]
    if adapter_spec.expects_devnull:
        assert kwargs.get("stdin") == subprocess.DEVNULL
    else:
        # Adapters that do not explicitly set stdin inherit the parent
        # stdin/input stream instead of forcing DEVNULL.
        assert "stdin" not in kwargs


# -- prompt placeholder formatting (render_prompt shared by all adapters) ---


def test_placeholder_reaches_the_command(adapter_spec, make_runner):
    runner = make_runner(stdout=adapter_spec.success_stdout)
    act = adapter_spec.make_act(prompt_template="fix iter {iteration}", runner=runner)

    act({"iteration": 7})

    sent_command = runner.calls[-1][0]
    assert sent_command[-1] == "fix iter 7"


# -- Mock contract (same act contract without subprocess) -------------------


def test_mock_cycles_then_sticks_to_last(adapter_spec):
    mock = adapter_spec.mock_cls(responses=["first", {"text": "second", "tokens": 5}])
    a = mock({"prompt": "p1"})
    b = mock({"prompt": "p2"})
    c = mock({"prompt": "p3"})  # Once exhausted, it sticks to the last value.

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
