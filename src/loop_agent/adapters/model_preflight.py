"""Provider-aware model availability preflight for bundled act adapters.

The loop core intentionally does not choose models. This module gives callers a
small adapter-layer visibility surface so they can inspect candidate model names in
their current CLI/auth environment before starting a loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from .base import Runner
from .claude_code import ClaudeCodeAct, ClaudeCodeResult
from .codex import CodexAct, CodexResult

AvailabilityStatus = Literal["available", "unavailable", "unknown", "skipped"]
ProviderName = Literal["codex", "claude-code"]

CODEX_MODEL_CANDIDATES: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex-spark",
)

CLAUDE_CODE_MODEL_ALIASES: tuple[str, ...] = ("sonnet", "opus", "haiku", "fable")
CLAUDE_CODE_FULL_MODEL_CANDIDATES: tuple[str, ...] = (
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "claude-fable-5",
)

DEFAULT_SMOKE_PROMPT = "Reply exactly: LOOP_AGENT_MODEL_PREFLIGHT_OK"


@dataclass(frozen=True)
class ModelAvailability:
    """Availability evidence for one adapter model candidate.

    ``status`` is intentionally descriptive, not policy:

    - ``available``: a smoke run completed without adapter failure.
    - ``unavailable``: the CLI ran but rejected or failed the candidate model.
    - ``unknown``: the CLI could not be launched, timed out, or otherwise could not
      prove model-specific availability.
    - ``skipped``: no smoke run was requested; the candidate is only listed.
    """

    provider: ProviderName
    model: str
    status: AvailabilityStatus
    command: tuple[str, ...] = ()
    tokens: int = 0
    returncode: Optional[int] = None
    error: str = ""


@dataclass(frozen=True)
class ModelAvailabilityReport:
    """Provider-neutral report returned by adapter preflight helpers."""

    provider: ProviderName
    smoke: bool
    results: tuple[ModelAvailability, ...]

    def available_models(self) -> tuple[str, ...]:
        """Return models whose smoke result was ``available``."""
        return tuple(item.model for item in self.results if item.status == "available")


def codex_model_candidates(extra: Sequence[str] = ()) -> tuple[str, ...]:
    """Return default Codex candidates plus caller-supplied additions."""
    return _dedupe((*CODEX_MODEL_CANDIDATES, *extra))


def claude_code_model_candidates(
    *, include_full_names: bool = False, extra: Sequence[str] = ()
) -> tuple[str, ...]:
    """Return Claude Code alias candidates, optionally including full model IDs."""
    base = CLAUDE_CODE_MODEL_ALIASES
    if include_full_names:
        base = (*base, *CLAUDE_CODE_FULL_MODEL_CANDIDATES)
    return _dedupe((*base, *extra))


def preflight_codex_models(
    models: Optional[Sequence[str]] = None,
    *,
    smoke: bool = False,
    timeout: float = 60.0,
    prompt: str = DEFAULT_SMOKE_PROMPT,
    codex_bin: Optional[str] = None,
    effort: str = "medium",
    sandbox: Optional[str] = "read-only",
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> ModelAvailabilityReport:
    """List or smoke-test Codex model candidates for the current CLI/auth environment."""
    candidates = tuple(models) if models is not None else codex_model_candidates()
    results = tuple(
        _preflight_codex_one(
            model,
            smoke=smoke,
            timeout=timeout,
            prompt=prompt,
            codex_bin=codex_bin,
            effort=effort,
            sandbox=sandbox,
            env=env,
            cwd=cwd,
            runner=runner,
        )
        for model in candidates
    )
    return ModelAvailabilityReport(provider="codex", smoke=smoke, results=results)


def preflight_claude_code_models(
    models: Optional[Sequence[str]] = None,
    *,
    smoke: bool = False,
    include_full_names: bool = False,
    timeout: float = 60.0,
    prompt: str = DEFAULT_SMOKE_PROMPT,
    claude_bin: str = "claude",
    permission_mode: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> ModelAvailabilityReport:
    """List or smoke-test Claude Code model candidates for the current environment."""
    candidates = (
        tuple(models)
        if models is not None
        else claude_code_model_candidates(include_full_names=include_full_names)
    )
    results = tuple(
        _preflight_claude_one(
            model,
            smoke=smoke,
            timeout=timeout,
            prompt=prompt,
            claude_bin=claude_bin,
            permission_mode=permission_mode,
            env=env,
            cwd=cwd,
            runner=runner,
        )
        for model in candidates
    )
    return ModelAvailabilityReport(provider="claude-code", smoke=smoke, results=results)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _preflight_codex_one(
    model: str,
    *,
    smoke: bool,
    timeout: float,
    prompt: str,
    codex_bin: Optional[str],
    effort: str,
    sandbox: Optional[str],
    env: Optional[dict[str, str]],
    cwd: Optional[str],
    runner: Optional[Runner],
) -> ModelAvailability:
    if codex_bin is None:
        act = CodexAct(
            model=model,
            effort=effort,
            timeout=timeout,
            sandbox=sandbox,
            env=env,
            cwd=cwd,
            runner=runner,
        )
    else:
        act = CodexAct(
            model=model,
            effort=effort,
            timeout=timeout,
            sandbox=sandbox,
            env=env,
            cwd=cwd,
            runner=runner,
            codex_bin=codex_bin,
        )
    command = tuple(act.build_command(prompt))
    if not smoke:
        return ModelAvailability(provider="codex", model=model, status="skipped", command=command)

    result = act({"prompt": prompt}).observation
    assert isinstance(result, CodexResult)
    return _availability_from_result("codex", model, result)


def _preflight_claude_one(
    model: str,
    *,
    smoke: bool,
    timeout: float,
    prompt: str,
    claude_bin: str,
    permission_mode: Optional[str],
    env: Optional[dict[str, str]],
    cwd: Optional[str],
    runner: Optional[Runner],
) -> ModelAvailability:
    act = ClaudeCodeAct(
        model=model,
        timeout=timeout,
        prompt_template="{prompt}",
        claude_bin=claude_bin,
        permission_mode=permission_mode,
        env=env,
        cwd=cwd,
        runner=runner,
    )
    command = tuple(act.build_command(prompt))
    if not smoke:
        return ModelAvailability(
            provider="claude-code", model=model, status="skipped", command=command
        )

    result = act({"prompt": prompt}).observation
    assert isinstance(result, ClaudeCodeResult)
    return _availability_from_result("claude-code", model, result)


def _availability_from_result(
    provider: ProviderName, model: str, result: CodexResult | ClaudeCodeResult
) -> ModelAvailability:
    if not result.failed:
        status: AvailabilityStatus = "available"
    elif result.returncode is None:
        status = "unknown"
    else:
        status = "unavailable"
    return ModelAvailability(
        provider=provider,
        model=model,
        status=status,
        command=result.command,
        tokens=result.tokens,
        returncode=result.returncode,
        error=_concise_error(result.error),
    )


def _concise_error(text: str, limit: int = 500) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "..."