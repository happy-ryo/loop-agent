# Optional Post-act Review

`review` is an optional seam for evaluating artifacts produced by LLM-backed `act` before the ground-truth `verify` step.

- `verify` decides whether the goal was mechanically satisfied: pytest, build, AST, schema, regex, and so on.
- `HumanGate` stops irreversible operations before they run: commit, push, deploy, deletion, and so on.
- `review` evaluates artifacts after `act`: scope, API fit, maintainability, migration risk, documentation consistency, and alignment with user intent.

Even when tests pass, a change can still be too broad, incompatible with a public API, or misaligned with the intent. `review` is the public API for handling that layer explicitly.

## API

```python
from loop_agent import ReviewOutcome, run_loop


def review(outcome):
    if changed_too_much(outcome.observation):
        return ReviewOutcome(
            approved=False,
            severity="blocking",
            feedback="scope is too broad; keep the change inside docs/",
        )
    return ReviewOutcome(approved=True, feedback="scope looks acceptable")


result = run_loop(
    act=act,
    review=review,
    verify=verify,
    conditions=conditions,
)
```

`ReviewOutcome` has the following fields.

| Field | Meaning |
|---|---|
| `approved: bool` | Whether the review accepts the artifact |
| `feedback: str = ""` | Concise feedback passed to the next iteration |
| `severity: "info" / "warning" / "blocking" = "info"` | Skip `verify` only when this is `blocking` and `approved=False` |

## Execution Order

```text
gather -> gate? -> act -> review? -> verify -> repeat
```

If you do not pass `review`, the behavior remains `gather -> act -> verify` as before. If `review` returns `approved=True`, or if `severity` is `info` / `warning`, `verify` runs normally.

When `ReviewOutcome(approved=False, severity="blocking")` is returned, that iteration is recorded as a step with `goal_met=False`, and `verify` is not run. The next `gather` can read the feedback from `state.history[-1].detail` and feed it back into the next `act` prompt.


## LLM-backed Review: Structured Decisions

When using Codex / Claude / another LLM for `review`, do not approve by grepping natural-language responses such as `LGTM`, `No findings`, or `looks good`. The review seam is a control point that can stop loop progress before `verify`, so responses should be fixed to a structure that can be judged mechanically.

JSON is the recommended shape.

```json
{
  "decision": "approved",
  "findings": [],
  "residual_risk": "docs-only change; tests still need to pass"
}
```

For blocking:

```json
{
  "decision": "blocking",
  "findings": ["README changed unrelated release instructions"],
  "residual_risk": "scope is too broad"
}
```

In the `review` hook, return `ReviewOutcome(approved=True)` only when `decision == "approved"`. Otherwise, return `ReviewOutcome(False, ..., "blocking")`. Treat JSON parse failures as blocking as well. This avoids treating ambiguous natural language as success.

```python
import json
from loop_agent import ReviewOutcome


def review_with_llm(outcome):
    review_outcome = run_review_agent(outcome)  # CodexAct / ClaudeCodeAct / custom adapter
    raw = review_outcome.observation
    try:
        decision = json.loads(raw.text)
    except json.JSONDecodeError:
        return ReviewOutcome(False, "review did not return JSON", "blocking")
    if not isinstance(decision, dict):
        return ReviewOutcome(False, "review JSON was not an object", "blocking")


    findings = decision.get("findings") or []
    if isinstance(findings, str):
        findings = [findings]
    if not isinstance(findings, list):
        findings = ["review findings had an invalid shape"]
    residual_risk = decision.get("residual_risk", "")
    if not isinstance(residual_risk, str):
        residual_risk = ""

    if decision.get("decision") != "approved":
        feedback = findings or ["review did not approve"]
        return ReviewOutcome(False, "; ".join(map(str, feedback)), "blocking")
    return ReviewOutcome(True, residual_risk)
```

When claiming that dogfood or self-improvement loops used a real adapter, validating the artifact alone is not enough. At minimum, also check the following in `verify` or `review`.

- `ActOutcome.observation` is the expected adapter result type.
- For subprocess adapters, `observation.command` contains the expected command, such as `codex exec` / `claude --print`.
- If the adapter is configured to return usage, `tokens > 0`.
- When `review` itself is run through an LLM adapter, either check the review-side command and token usage inside the review hook, or save them to an external record so `verify` can read them.

This distinction matters. A post-hoc recorder that only ran `review` / `verify` inside the loop is not full dogfood. To call it full dogfood, at minimum `gather -> real act adapter -> structured review -> ground-truth verify` must be observed within the same run.

## State Representation

Feedback from a blocking review is stored as JSON in the existing `StepRecord.detail`. When using state.db, the same string is persisted to `step.detail`, so feedback remains readable after resume. When the review is not blocking, `StepRecord.detail` remains the raw `verify.detail` string as before.

Example detail for a blocking review:

```json
{"review":{"approved":false,"feedback":"scope is too broad","severity":"blocking"}}
```

When both review and verify run, detail remains the verify detail as before:

```text
pytest passed
```

Do not design a loop that uses review without `verify`. Review evaluates design, intent, and risk; success judgment should stay in ground-truth `verify` as much as possible.

## Retry Behavior

A blocking review is treated as a failed step and retried according to the existing stop conditions. Always combine it with a mechanical limit such as `MaxIterations`, `TokenBudget`, `Timeout`, or `WorkListGather(max_attempts_per_item=...)`.

In multi-item loops, require both review approval and ground-truth verify in `done_when`.

```python
import json


def done_when(_item, record):
    try:
        detail = json.loads(record.detail or "{}")
    except json.JSONDecodeError:
        detail = {}
    return bool(detail.get("review", {}).get("approved", True) and record.detail == "pytest passed")
```

Using `WorkListGather(max_attempts_per_item=...)` prevents a single item that repeatedly receives review feedback from monopolizing the entire work list.

## HumanGate Boundary

`review` is not an approval mechanism for irreversible operations. Treat commit, push, tag, publish, deploy, and deletion as pre-execution gates through `HumanGate`, or as explicit human actions outside the loop.

## Related

- [recipes/review-driven-loop.md](./recipes/review-driven-loop.md) - concrete harness pattern
- [seams.md](./seams.md) - seam overview
- [safety.md](./safety.md) - HumanGate and irreversible actions
- [api-surface.md](./api-surface.md) - criteria for adding public symbols
