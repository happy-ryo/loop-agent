"""Deterministic checks for the version-readiness issue plan.

This is a repository-maintenance script, not package runtime code. It is used by
`examples/version_readiness_task.toml` through subprocess hooks so the dogfood
loop can verify the issue plan without importing a private module from
`loop_agent`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ISSUE_DRAFTS = [
    Path(".github/ISSUE_DRAFTS/001-docs-consistency-0-1-1.md"),
    Path(".github/ISSUE_DRAFTS/002-public-api-boundary-0-2.md"),
    Path(".github/ISSUE_DRAFTS/003-cli-persistence-contract.md"),
    Path(".github/ISSUE_DRAFTS/004-release-metadata-policy.md"),
    Path(".github/ISSUE_DRAFTS/005-1-0-release-gate.md"),
]

ROADMAP = Path("docs/version-readiness-roadmap.md")
CREATE_SCRIPT = Path("scripts/create_version_readiness_issues.ps1")

REQUIRED_SECTIONS = [
    "Title:",
    "Suggested labels:",
    "## Problem",
    "## Scope",
    "## Acceptance Criteria",
    "## Version Outcome",
]


def failures() -> list[str]:
    problems: list[str] = []
    if not ROADMAP.exists():
        problems.append(f"missing roadmap: {ROADMAP}")
    else:
        roadmap_text = ROADMAP.read_text(encoding="utf-8")
        for path in ISSUE_DRAFTS:
            if path.name not in roadmap_text:
                problems.append(f"roadmap does not link {path.name}")

    for path in ISSUE_DRAFTS:
        if not path.exists():
            problems.append(f"missing issue draft: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS:
            if section not in text:
                problems.append(f"{path} missing section {section!r}")
        if "Version Outcome" not in text:
            problems.append(f"{path} missing explicit version outcome")

    if not CREATE_SCRIPT.exists():
        problems.append(f"missing issue creation script: {CREATE_SCRIPT}")
    else:
        script_text = CREATE_SCRIPT.read_text(encoding="utf-8")
        if "--state open" not in script_text:
            problems.append(f"{CREATE_SCRIPT} must check open issues before creating")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else "verify"
    problems = failures()
    if mode == "act":
        print(json.dumps({
            "roadmap": str(ROADMAP),
            "create_script": str(CREATE_SCRIPT),
            "issue_drafts": [str(path) for path in ISSUE_DRAFTS],
        }, ensure_ascii=True))
        return 0
    if mode == "verify":
        if problems:
            print("; ".join(problems), file=sys.stderr)
            return 1
        print("ok")
        return 0
    print("usage: version_readiness_harness.py [act|verify]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
