Title: Prepare 0.1.1 docs consistency release

Suggested labels: docs, release, version-readiness

## Problem

The current implementation has grown beyond some older wording. In particular,
some docs still describe dashboard, spike scan, and circuit breaker work as
follow-up while other docs and code describe them as implemented.

## Scope

- Align README, `docs/api-reference.md`, `docs/operations-roadmap.md`, and related
  recipe references with the current implementation.
- Keep this as a documentation/metadata patch release candidate.
- Do not change runtime behavior.

## Acceptance Criteria

- README, API reference, and operations docs agree on which operations features
  are implemented and which remain external integration work.
- References to `0.1.0 Beta` are either intentionally kept for the released
  version or updated in the release branch to `0.1.1`.
- `scripts/sync_skill_references.py --check` passes after any docs mirrored into
  the bundled skill are updated.
- `python -m pytest` passes.
- `python -m build`, `python -m twine check dist/*`, and
  `python scripts/verify_wheel_skill_bundle.py` pass.

## Version Outcome

If only documentation and metadata change, release as `0.1.1`.
