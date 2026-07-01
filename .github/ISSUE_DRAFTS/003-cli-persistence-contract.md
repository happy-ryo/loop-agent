Title: Lock down CLI and state.db compatibility contracts

Suggested labels: cli, persistence, release, version-readiness

## Problem

The CLI and SQLite state store are useful enough to be depended on directly. A
`1.0.0` release should state what is stable: CLI commands, exit codes, output
shape, database schema, and migration behavior.

## Scope

- Document stable CLI commands and exit codes.
- Decide whether human-readable CLI output is stable or best-effort.
- Document `state.db` compatibility and migration policy.
- Clarify which schema changes are patch/minor/major changes.
- Confirm resume behavior and JSON observation limits are part of the contract.

## Acceptance Criteria

- `docs/cli.md` contains a compatibility section.
- `docs/persistence-and-resume.md` contains schema/migration compatibility rules.
- Existing CLI and store tests still pass.
- Any newly promised behavior has a regression test.

## Version Outcome

If behavior changes are required, release before `1.0.0` as `0.2.0`. If this is
documentation-only, include it in `0.1.1`.
