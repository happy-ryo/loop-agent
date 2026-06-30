Title: Align release metadata and maturity policy

Suggested labels: packaging, release, version-readiness

## Problem

Package metadata, README wording, changelog entries, and release docs should agree
on project maturity. Before `1.0.0`, the project should decide when to move from
`Development Status :: 4 - Beta` to a stable classifier.

## Scope

- Define the maturity criteria for leaving Beta.
- Update `pyproject.toml` classifiers when the criteria are met.
- Keep `CHANGELOG.md`, README, API docs, and release docs consistent.
- Ensure tag version, `pyproject.toml`, and `loop_agent.__version__` stay aligned.

## Acceptance Criteria

- Release docs describe when `1.0.0` is allowed.
- Metadata classifier matches the chosen maturity.
- Version source checks are documented or automated.
- Build and twine checks pass.

## Version Outcome

Metadata-only consistency work can ship as `0.1.1`. Moving to stable classifier
should happen with `1.0.0`.
