# Version Readiness Roadmap

This roadmap turns the version-readiness review into issue-sized work. The goal
is to avoid using the current version label as evidence for itself: each version
step should be justified by the API, CLI, persistence, documentation, and release
contracts the project is willing to support.

## Recommended Sequence

1. The documentation and metadata consistency work has been folded into the
   `1.0.0` release branch.
2. No `__all__` removals were required; the stable/advanced boundary is
   documented instead.
3. Ship `1.0.0` after the release gate passes and the tag is cut.

## Issue Drafts

The GitHub issue drafts live in `.github/ISSUE_DRAFTS/`:

- `001-docs-consistency-0-1-1.md`
- `002-public-api-boundary-0-2.md`
- `003-cli-persistence-contract.md`
- `004-release-metadata-policy.md`
- `005-1-0-release-gate.md`

Created GitHub issues:

- https://github.com/happy-ryo/loop-agent/issues/117
- https://github.com/happy-ryo/loop-agent/issues/118
- https://github.com/happy-ryo/loop-agent/issues/119
- https://github.com/happy-ryo/loop-agent/issues/120
- https://github.com/happy-ryo/loop-agent/issues/121

The helper script `scripts/create_version_readiness_issues.ps1` creates missing
issues and skips already-created titles.

## 1.0 Gate

`1.0.0` is appropriate when all of these are true:

- The stable public API is documented separately from experimental or advanced
  helpers.
- `__all__` is intentionally reviewed and no accidental symbols are exported as
  stable.
- CLI commands and exit codes have an explicit compatibility promise.
- `state.db` schema and migration compatibility are documented.
- Adapter contracts are documented as stable or explicitly marked experimental.
- README, API docs, release docs, and package metadata agree on maturity.
- `python -m pytest`, `python -m build`, `python -m twine check dist/*`, and
  `python scripts/verify_wheel_skill_bundle.py` pass.

## Dogfood Verification

This plan is verified by loop-agent itself via:

```bash
loop-agent run examples/version_readiness_task.toml
loop-agent status version-readiness-issues-final
loop-agent logs version-readiness-issues-final
```

The task uses deterministic local hooks in `scripts/version_readiness_harness.py`.
It does not change release state; it records an audit trail in `loop-state.db`.

## Review-driven Follow-up

LLM-backed `act` steps should be reviewed before merge. The current stable core
does not have a first-class post-act `review` seam yet; use the optional pattern
in `docs/recipes/review-driven-loop.md` until Issue #128 decides whether to add a
public `ReviewHook` / `ReviewOutcome` API.

This release PR can be review-checked with:

```bash
python scripts/review_version_readiness_pr.py
```
