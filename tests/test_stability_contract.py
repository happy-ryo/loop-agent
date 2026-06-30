from __future__ import annotations

import re
from pathlib import Path

import loop_agent


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_version_sources_and_metadata_are_1_0_0():
    pyproject = _read("pyproject.toml")
    changelog = _read("CHANGELOG.md")

    assert 'version = "1.0.0"' in pyproject
    assert 'Development Status :: 5 - Production/Stable' in pyproject
    assert loop_agent.__version__ == "1.0.0"
    assert "## [1.0.0] - 2026-07-01" in changelog
    assert "[1.0.0]:" in changelog


def test_stability_docs_are_linked_and_not_beta_wording():
    readme = _read("README.md")
    api = _read("docs/api-reference.md")
    stability = _read("docs/stability.md")

    assert "docs/stability.md" in readme
    assert "1.0.0 Stable" in readme
    assert "0.1.0 Beta" not in readme
    assert "1.0.0 Stable" in api
    assert "0.1.0 Beta" not in api
    assert "Stable Public API" in stability
    assert "Advanced Stable API" in stability
    assert "Deprecation Policy" in stability


def test_cli_and_state_db_contract_sections_exist():
    cli = _read("docs/cli.md")
    persistence = _read("docs/persistence-and-resume.md")
    releasing = _read("docs/releasing.md")

    assert "## 互換性契約" in cli
    assert "終了コード" in cli
    assert "human-readable" not in cli.lower()
    assert "best-effort" in cli
    assert "## state.db 互換性契約" in persistence
    assert "patch release" in persistence
    assert "minor release" in persistence
    assert "major release" in persistence
    assert "1.0.0 release gate" in releasing
    assert "Development Status :: 5 - Production/Stable" in releasing


def test_core_stable_imports_are_available_from_top_level():
    names = {
        "run_loop",
        "async_run_loop",
        "ActOutcome",
        "VerifyOutcome",
        "LoopResult",
        "LoopState",
        "StepRecord",
        "AnyOf",
        "MaxIterations",
        "TokenBudget",
        "Timeout",
        "GoalMet",
        "NoProgress",
        "ProgressLog",
        "read_progress",
        "connect",
        "LoopStore",
        "DBProgressLog",
        "HumanGate",
        "Decision",
        "LoopError",
        "ConfigError",
        "StateError",
    }
    missing = sorted(name for name in names if not hasattr(loop_agent, name))
    assert missing == []
    assert names <= set(loop_agent.__all__)


def test_every_top_level_export_is_classified_in_stability_docs():
    stability = _read("docs/stability.md")
    missing = [
        name for name in loop_agent.__all__
        if f"`{name}`" not in stability
    ]
    assert missing == []


def test_changelog_compare_links_are_consistent():
    changelog = _read("CHANGELOG.md")
    assert re.search(r"\[Unreleased\]: .*/compare/v1\.0\.0\.\.\.HEAD", changelog)
    assert re.search(r"\[1\.0\.0\]: .*/compare/v0\.1\.0\.\.\.v1\.0\.0", changelog)


def test_review_followup_is_reproducible_not_one_off_local_artifact():
    roadmap = _read("docs/version-readiness-roadmap.md")
    recipe = _read("docs/recipes/review-driven-loop.md")
    gitignore = _read(".gitignore")

    assert "claude-llm-act-version-readiness-smoke" not in roadmap
    assert "LLM-act Smoke Audit" not in roadmap
    assert "review-driven-loop.md" in roadmap
    assert "Issue #128" in roadmap
    assert "HumanGate" in recipe
    assert "ReviewHook" in recipe
    assert "loop-state.db" in gitignore
    assert "loop-state.db-*" in gitignore
