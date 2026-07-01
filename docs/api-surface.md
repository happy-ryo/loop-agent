# API Surface Discipline

loop-agent is an embeddable loop engine, so the core API should stay small even
when advanced helpers exist. The stable surface is split into three layers.

| Layer | What belongs here | Compatibility expectation |
|---|---|---|
| Core | `run_loop`, `async_run_loop`, loop state, outcomes, stop conditions | Highest stability. Avoid expanding this without a direct loop-body need. |
| Practical helpers | verifier helpers, adapters, persistence, observability, CLI | Stable import paths, but policy remains caller-owned. Additions should be small and mechanical. |
| Advanced composition | Reflexion, transport, work discovery, operations helpers | Stable documented behavior, but explicitly opt-in and application-specific. |

## Adding a Public Symbol

Before adding a top-level export:

1. Confirm it removes repeated harness boilerplate without hiding policy.
2. Prefer a protocol or callable seam over a closed class hierarchy.
3. Add it to `docs/stability.md` and `docs/api-reference.md` in the right layer.
4. Add a narrow test that exercises the public behavior without depending on an
   external service.
5. Avoid adding symbols that make irreversible actions or LLM judgment look
   automatic.

## Keeping the Core Small

The library owns orchestration, state, and safety guards. Domain selection,
execution policy, and success criteria stay in the caller's seams. A helper is a
good fit when it makes the seam easier to write while preserving that boundary;
it is a bad fit when it decides what the user's goal means.
