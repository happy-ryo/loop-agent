"""Review checks for the version-readiness release PR.

This script uses loop-agent itself to model a lightweight review phase without
adding a public `review=` API. Each review finding is one WorkItem. The act step
runs the review check, and verify accepts only findings whose review passed.
"""

from __future__ import annotations

import re
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
RUN_ID = "review-version-readiness-pr"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def review_all_exports_classified() -> list[str]:
    stability = read("docs/stability.md")
    missing = [
        name for name in loop_agent.__all__
        if f"`{name}`" not in stability
    ]
    return [f"unclassified top-level exports: {missing}"] if missing else []


def review_followup_is_reproducible() -> list[str]:
    roadmap = read("docs/version-readiness-roadmap.md")
    recipe_path = ROOT / "docs/recipes/review-driven-loop.md"
    failures: list[str] = []
    for needle in ("claude-llm-act-version-readiness-smoke", "LLM-act Smoke Audit"):
        if needle in roadmap:
            failures.append(f"roadmap contains one-off local run marker: {needle}")
    if "review-driven-loop.md" not in roadmap:
        failures.append("roadmap does not link the review-driven recipe")
    if not recipe_path.exists():
        failures.append("missing review-driven loop recipe")
    else:
        recipe = recipe_path.read_text(encoding="utf-8")
        for needle in ("ReviewHook", "HumanGate", "ground-truth verification"):
            if needle not in recipe:
                failures.append(f"review recipe missing {needle!r}")
    return failures


def review_sqlite_artifacts_ignored() -> list[str]:
    gitignore = read(".gitignore")
    required = ("loop-state.db", "loop-state.db-*")
    return [f".gitignore missing {item}" for item in required if item not in gitignore]


def review_release_gate_still_documented() -> list[str]:
    releasing = read("docs/releasing.md")
    changelog = read("CHANGELOG.md")
    failures: list[str] = []
    for needle in (
        "python -m pytest",
        "python -m build",
        "python -m twine check dist/*",
        "python scripts/verify_wheel_skill_bundle.py",
    ):
        if needle not in releasing:
            failures.append(f"release gate missing {needle}")
    if not re.search(r"\[Unreleased\]: .*/compare/v1\.0\.0\.\.\.HEAD", changelog):
        failures.append("changelog Unreleased compare does not point at v1.0.0")
    return failures


CHECKS: dict[str, Callable[[], list[str]]] = {
    "all-exports-classified": review_all_exports_classified,
    "review-followup-reproducible": review_followup_is_reproducible,
    "sqlite-artifacts-ignored": review_sqlite_artifacts_ignored,
    "release-gate-documented": review_release_gate_still_documented,
}


def main() -> int:
    items = [WorkItem(id=name, payload={"review": name}) for name in CHECKS]

    def done_when(_item: WorkItem, record) -> bool:
        obs = record.observation
        return isinstance(obs, dict) and obs.get("review_passed") is True

    gather = WorkListGather(
        items,
        strategy="fifo",
        done_when=done_when,
        build_ctx=lambda item, _attempt, _state: item.payload,
    )

    def act(ctx) -> ActOutcome:
        review_id = str(ctx["review"])
        failures = CHECKS[review_id]()
        return ActOutcome(
            observation={
                "review": review_id,
                "review_passed": not failures,
                "feedback": failures,
            },
            tokens=0,
        )

    def verify(outcome) -> VerifyOutcome:
        obs = outcome.observation
        passed = bool(obs["review_passed"])
        return VerifyOutcome(
            goal_met=False,
            detail=f"{obs['review']}: {'review-pass' if passed else obs['feedback']}",
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
