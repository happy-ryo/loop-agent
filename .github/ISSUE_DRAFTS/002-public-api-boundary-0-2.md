Title: Define the stable public API boundary before 1.0

Suggested labels: api, release, version-readiness

## Problem

`loop_agent.__all__` currently exports a broad surface. If the project ships
`1.0.0`, those exported symbols become the practical compatibility contract unless
the project explicitly separates stable and experimental surfaces.

## Scope

- Review every symbol exported from `loop_agent.__all__`.
- Classify each symbol as stable, advanced-but-stable, or experimental.
- Decide whether experimental surfaces should remain exported, move to submodule
  imports only, or stay exported with an explicit compatibility disclaimer.
- Update docs so users know which imports are safe to depend on.

## Acceptance Criteria

- A stable API table exists for the core loop API.
- Experimental or advanced surfaces are documented separately.
- Any removal or rename from `__all__` is treated as a breaking change.
- Release docs explain how deprecations work after `1.0.0`.
- Tests cover the intended stable import paths.

## Version Outcome

If `__all__` or import paths change, release as `0.2.0`. If no API changes are
needed, this can be completed as documentation before `1.0.0`.
