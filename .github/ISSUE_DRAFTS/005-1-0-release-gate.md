Title: Run the 1.0.0 release readiness gate

Suggested labels: release, quality, version-readiness

## Problem

`1.0.0` should be a compatibility promise, not just a confidence label. The
project needs a final checklist that proves the stable API, CLI, persistence, docs,
and packaging contracts are ready.

## Scope

- Execute the full test suite.
- Build wheel and sdist.
- Run `twine check`.
- Verify bundled skill contents.
- Confirm docs and metadata no longer describe the release as Beta.
- Confirm all earlier version-readiness issues are closed.

## Acceptance Criteria

- `python -m pytest` passes.
- `python -m build` passes.
- `python -m twine check dist/*` passes.
- `python scripts/verify_wheel_skill_bundle.py` passes.
- Stable API and compatibility docs are linked from README.
- `pyproject.toml`, `loop_agent.__version__`, changelog, and tag all agree on
  `1.0.0`.

## Version Outcome

If all criteria pass, release `1.0.0`.
