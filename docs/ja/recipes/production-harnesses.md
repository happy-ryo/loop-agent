# Canonical Production Harnesses

Use this page when you want a production starting point instead of a catalog of
patterns. The recipes directory has supporting examples, but most deployments
should start from one of these three harness shapes.
## Scaffold First

Generate the starter files before hand-writing a harness:

```bash
loop-agent init-harness --template light  --output ./harness-light
loop-agent init-harness --template claude --output ./harness-claude
loop-agent init-harness --template codex  --output ./harness-codex
```

Each template writes `harness.py` and `README.md`. The generated files are not a
policy engine: edit the prompt, the verifier, the caps, and any gate predicate
for your domain. Existing files are preserved unless `--force` is passed.

## 1. Single Verified Edit Loop

Use this when one bounded task can be verified by one machine oracle: a focused
pytest target, a compiler, a linter, an AST check, or a smoke probe.

Seam choices:

| Seam | Production default |
|---|---|
| `gather` | Omit it, or return one prompt built from the latest `LoopState`. |
| `act` | A coding-agent adapter limited to reversible editing, such as `ClaudeCodeAct(allowed_tools=["Read", "Edit"])`. |
| `verify` | `PytestVerifier`, `CommandVerifier`, or a domain-specific deterministic check. |
| `conditions` | At least `MaxIterations`; add `Timeout` or `TokenBudget` for cost and time control. |
| `gate` | None. Commit, push, and deploy stay outside the loop. |

Choose this first for small code edits, documentation consistency fixes, test
repair, and local migrations. It is the smallest production-safe harness because
success is decided by ground truth and irreversible operations are isolated.

Supporting recipe: [review-driven-loop.md](./review-driven-loop.md) when an LLM
post-act review is needed before running the expensive ground-truth verify.

## 2. Multi-Item Work Queue

Use this when N independent items must converge without one hard item starving
the rest: file-by-file translation, many flaky tests, generated summaries, or a
batch of small refactors.

Seam choices:

| Seam | Production default |
|---|---|
| `gather` | `WorkListGather` with `strategy="fewest_attempts"` and a per-item attempt cap. |
| `act` | The same reversible editing adapter or in-process callable used for one item. |
| `verify` | A per-item ground-truth check that records done/failed status in the observation. |
| `conditions` | `WorkListDrained(gather)` plus a mechanical cap such as `MaxIterations`. |
| `gate` | Usually none; move irreversible batch publish steps after the drained result. |

Choose this when fairness matters. A plain "first unfinished item" gather is easy
but fragile: one repeatedly failing item can consume the whole iteration budget.

Supporting recipe: [multi-item-work-list.md](./multi-item-work-list.md). Domain
examples: [flaky-test-stabilization.md](./flaky-test-stabilization.md),
[translation.md](./translation.md), and [refactor.md](./refactor.md).

## 3. Gated Irreversible Action Flow

Use this when the loop proposes a discrete action that can change external state:
deploy, publish, rotate credentials, open a pull request, or mutate production
data.

Seam choices:

| Seam | Production default |
|---|---|
| `gather` | Return a JSON-native action object, for example `{ "kind": "deploy", "target": "staging" }`. |
| `act` | Execute only the action approved by the gate. Keep subprocess permissions narrow. |
| `verify` | Probe the external state after execution with a deterministic check. |
| `conditions` | A mechanical cap plus a semantic stop for verified completion or no progress. |
| `gate` | `HumanGate(on=...)` matching only irreversible action kinds. |

Choose this only when the irreversible operation is visible as the loop's
`gather` output. A human gate cannot inspect hidden operations performed inside a
coding-agent subprocess. If the agent edits files and then runs `git push`
inside `act`, the gate is bypassed by design. Make the push/deploy a discrete
action, or keep it outside the loop.

Supporting docs: [../safety.md](../safety.md) and
[../persistence-and-resume.md](../persistence-and-resume.md) for pause/resume.

## Selection Rule

Pick the first matching shape:

1. One task, one oracle, no irreversible side effect: **Single Verified Edit Loop**.
2. Many independent items: **Multi-Item Work Queue**.
3. External state mutation inside the loop: **Gated Irreversible Action Flow**.

Everything else in this directory is supporting material. Add Reflexion,
transport, dashboards, or notifier integrations only after one of these shapes is
working and the production need is concrete.
