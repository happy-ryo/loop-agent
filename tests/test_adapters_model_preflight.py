from __future__ import annotations

import subprocess

from loop_agent.adapters import (
    CLAUDE_CODE_FULL_MODEL_CANDIDATES,
    CLAUDE_CODE_MODEL_ALIASES,
    CODEX_MODEL_CANDIDATES,
    claude_code_model_candidates,
    codex_model_candidates,
    preflight_claude_code_models,
    preflight_codex_models,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    def _runner(command, **kwargs):
        _runner.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    _runner.calls = []
    return _runner


def _missing(command, **kwargs):
    raise FileNotFoundError(command[0])


def test_candidate_lists_are_deduped_and_extensible():
    assert codex_model_candidates(extra=("gpt-5.5", "custom-model")) == (
        *CODEX_MODEL_CANDIDATES,
        "custom-model",
    )
    assert claude_code_model_candidates() == CLAUDE_CODE_MODEL_ALIASES
    assert claude_code_model_candidates(include_full_names=True, extra=("sonnet", "custom")) == (
        *CLAUDE_CODE_MODEL_ALIASES,
        *CLAUDE_CODE_FULL_MODEL_CANDIDATES,
        "custom",
    )


def test_codex_preflight_without_smoke_lists_skipped_candidates():
    report = preflight_codex_models(models=["gpt-5.5"], smoke=False, codex_bin="codex")

    assert report.provider == "codex"
    assert report.smoke is False
    assert report.available_models() == ()
    result = report.results[0]
    assert result.status == "skipped"
    assert result.model == "gpt-5.5"
    assert result.command[:2] == ("codex", "exec")
    assert result.command[-2:] == ("--", "Reply exactly: LOOP_AGENT_MODEL_PREFLIGHT_OK")


def test_codex_smoke_success_reports_available_and_tokens():
    stdout = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"LOOP_AGENT_MODEL_PREFLIGHT_OK"}}',
            '{"type":"turn.completed","usage":{"input_tokens":3,"output_tokens":2}}',
        ]
    )
    runner = _completed(stdout=stdout)

    report = preflight_codex_models(models=["gpt-5.5"], smoke=True, codex_bin="codex", runner=runner)

    result = report.results[0]
    assert result.status == "available"
    assert result.tokens == 5
    assert result.error == ""
    assert report.available_models() == ("gpt-5.5",)


def test_codex_smoke_failed_cli_reports_unavailable():
    runner = _completed(stdout="model not supported", stderr="bad model", returncode=1)

    result = preflight_codex_models(
        models=["gpt-5"], smoke=True, codex_bin="codex", runner=runner
    ).results[0]

    assert result.status == "unavailable"
    assert result.returncode == 1
    assert result.error == "bad model"


def test_codex_smoke_missing_cli_reports_unknown():
    result = preflight_codex_models(
        models=["gpt-5.5"], smoke=True, codex_bin="missing-codex", runner=_missing
    ).results[0]

    assert result.status == "unknown"
    assert result.returncode is None
    assert "could not launch" in result.error


def test_claude_preflight_without_smoke_lists_skipped_aliases_and_full_names():
    report = preflight_claude_code_models(smoke=False, include_full_names=True, claude_bin="claude")

    assert report.provider == "claude-code"
    assert tuple(item.model for item in report.results) == (
        *CLAUDE_CODE_MODEL_ALIASES,
        *CLAUDE_CODE_FULL_MODEL_CANDIDATES,
    )
    assert {item.status for item in report.results} == {"skipped"}


def test_claude_smoke_success_reports_available_and_tokens():
    runner = _completed(
        stdout=(
            '{"result":"LOOP_AGENT_MODEL_PREFLIGHT_OK",'
            '"usage":{"input_tokens":4,"output_tokens":3}}'
        )
    )

    result = preflight_claude_code_models(
        models=["sonnet"], smoke=True, claude_bin="claude", runner=runner
    ).results[0]

    assert result.status == "available"
    assert result.tokens == 7
    assert result.command[:2] == ("claude", "--print")
    assert "--model" in result.command


def test_claude_smoke_failed_cli_reports_unavailable():
    runner = _completed(stdout='{"is_error": true, "result": "no access"}', returncode=0)

    result = preflight_claude_code_models(
        models=["fable"], smoke=True, claude_bin="claude", runner=runner
    ).results[0]

    assert result.status == "unavailable"
    assert result.returncode == 0
    assert result.error == "no access"


def test_claude_smoke_missing_cli_reports_unknown():
    result = preflight_claude_code_models(
        models=["sonnet"], smoke=True, claude_bin="missing-claude", runner=_missing
    ).results[0]

    assert result.status == "unknown"
    assert result.returncode is None
    assert "could not launch" in result.error