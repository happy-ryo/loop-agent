"""Dogfood loop that consumes the version-readiness issue set.

Each GitHub issue is represented as one WorkItem. The loop runs deterministic
checks for that issue, records the result in state.db, and drains only after all
items pass. It does not call GitHub; closing issues remains the explicit final
human/tool step after the release verification commands pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import loop_agent
from loop_agent import (
    ActOutcome,
    DBProgressLog,
    MaxIterations,
    VerifyOutcome,
    WorkItem,
    WorkListDrained,
    WorkListGather,
    run_loop,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "consume-version-readiness-issues-final"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def check_117() -> list[str]:
    failures: list[str] = []
    readme = read("README.md")
    api = read("docs/api-reference.md")
    ops = read("docs/operations-roadmap.md")
    if "0.1.0 Beta" in readme or "0.1.0 Beta" in api:
        failures.append("Beta wording remains in README/API docs")
    if "operations follow-up" in api:
        failures.append("API docs still call implemented operations helpers follow-up")
    for needle in ("summary", "dashboard", "spike", "circuit breaker", "throttling"):
        if needle not in readme and needle not in ops:
            failures.append(f"missing operations feature wording: {needle}")
    return failures


def check_118() -> list[str]:
    stability = read("docs/stability.md")
    tests = read("tests/test_stability_contract.py")
    failures: list[str] = []
    for section in ("Stable Public API", "Advanced Stable API", "Deprecation Policy"):
        if section not in stability:
            failures.append(f"stability docs missing {section}")
    if "Removing them, renaming" not in stability:
        failures.append("breaking API rule missing")
    if "test_core_stable_imports_are_available_from_top_level" not in tests:
        failures.append("stable import path regression test missing")
    return failures


def check_119() -> list[str]:
    cli = read("docs/cli.md")
    persistence = read("docs/persistence-and-resume.md")
    failures: list[str] = []
    if "## Compatibility Contract" not in cli:
        failures.append("CLI compatibility section missing")
    for needle in ("exit codes", "best-effort", "read-only"):
        if needle not in cli:
            failures.append(f"CLI compatibility missing {needle}")
    if "## state.db Compatibility Contract" not in persistence:
        failures.append("state.db compatibility section missing")
    for needle in ("patch release", "minor release", "major release", "PRAGMA user_version"):
        if needle not in persistence:
            failures.append(f"state.db compatibility missing {needle}")
    return failures


def check_120() -> list[str]:
    pyproject = read("pyproject.toml")
    releasing = read("docs/releasing.md")
    changelog = read("CHANGELOG.md")
    failures: list[str] = []
    if 'Development Status :: 5 - Production/Stable' not in pyproject:
        failures.append("stable classifier missing")
    if 'version = "1.0.0"' not in pyproject:
        failures.append("pyproject version is not 1.0.0")
    if loop_agent.__version__ != "1.0.0":
        failures.append(f"loop_agent.__version__ is {loop_agent.__version__}")
    if "1.0.0 release gate" not in releasing:
        failures.append("release gate missing from releasing docs")
    if "## [1.0.0]" not in changelog:
        failures.append("1.0.0 changelog section missing")
    return failures


def check_121() -> list[str]:
    readme = read("README.md")
    changelog = read("CHANGELOG.md")
    failures: list[str] = []
    if "docs/stability.md" not in readme:
        failures.append("README does not link stability docs")
    if "1.0.0 Stable" not in readme:
        failures.append("README does not describe 1.0.0 Stable")
    for path in ("dist/loop_agent-1.0.0-py3-none-any.whl", "dist/loop_agent-1.0.0.tar.gz"):
        if not (ROOT / path).exists():
            failures.append(f"release artifact not built yet: {path}")
    if "[Unreleased]: https://github.com/happy-ryo/loop-agent/compare/v1.0.0...HEAD" not in changelog:
        failures.append("changelog unreleased compare does not point at v1.0.0")
    return failures


CHECKS: dict[str, Callable[[], list[str]]] = {
    "117": check_117,
    "118": check_118,
    "119": check_119,
    "120": check_120,
    "121": check_121,
}


def main() -> int:
    items = [WorkItem(id=issue, payload={"issue": issue}) for issue in CHECKS]

    def done_when(_item: WorkItem, record) -> bool:
        obs = record.observation
        return isinstance(obs, dict) and obs.get("passed") is True

    gather = WorkListGather(
        items,
        strategy="fifo",
        done_when=done_when,
        build_ctx=lambda item, _attempt, _state: item.payload,
    )

    def act(ctx) -> ActOutcome:
        issue = str(ctx["issue"])
        failures = CHECKS[issue]()
        return ActOutcome(
            observation={"issue": issue, "passed": not failures, "failures": failures},
            tokens=0,
        )

    def verify(outcome) -> VerifyOutcome:
        obs = outcome.observation
        return VerifyOutcome(
            goal_met=False,
            detail=f"#{obs['issue']}: {'ok' if obs['passed'] else obs['failures']}",
        )

    db = DBProgressLog(ROOT / "loop-state.db", RUN_ID)
    try:
        result = run_loop(
            gather=gather,
            act=act,
            verify=verify,
            conditions=[WorkListDrained(gather), MaxIterations(10)],
            initial_state=db.state,
            on_step=db.on_step,
        )
        db.record_result(result)
    finally:
        db.close()

    report = gather.report(result.state)
    print(f"run-id     : {RUN_ID}")
    print(f"status     : {result.status}")
    print(f"reason     : {result.reason}")
    print(f"iterations : {result.iterations}")
    print(f"done       : {sorted(report.done)}")
    print(f"remaining  : {sorted(report.remaining)}")
    return 0 if not report.remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
