# Review-driven Loop

This recipe is for cases where an LLM-backed `act` edits files and tests alone cannot fully judge scope, design fit, or release risk.

Use `review=` explicitly as the stable API. The callable passed to `review` is a `ReviewHook`, and it returns a `ReviewOutcome`.

```text
gather finding -> act fix -> review artifact -> verify ground truth -> repeat
```

## Prose Intent

Example natural-language instruction to pass to a coding agent:

> In loop-agent, create a harness for small LLM-backed code-editing tasks. Limit `act` to file edits, and keep commit/push/deploy outside the loop. After each edit, run `review` to check scope, alignment with the public API, and consistency with the task intent. For a blocking review, do not run `verify`; pass the review feedback into the next iteration. Once review passes, run ground-truth verification with pytest. For multi-item work, use `WorkListDrained` and `MaxIterations`, and cap the number of attempts per item.

## Harness Shape

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loop_agent import ActOutcome, MaxIterations, ReviewOutcome, VerifyOutcome, WorkItem
from loop_agent import WorkListDrained, WorkListGather, run_loop


items = [
    WorkItem(id="api-contract", payload={"target": "src/loop_agent/__init__.py"}),
    WorkItem(id="docs", payload={"target": "docs/api-reference.md"}),
]


def _detail(record):
    try:
        return json.loads(record.detail or "{}")
    except json.JSONDecodeError:
        return {}


def done_when(_item, record):
    detail = _detail(record)
    return bool(detail.get("review", {}).get("approved", True) and record.detail == "pytest passed")


gather = WorkListGather(
    items,
    strategy="fewest_attempts",
    max_attempts_per_item=3,
    done_when=done_when,
)


def act(ctx):
    target = ctx["payload"]["target"]
    # In a real harness, call ClaudeCodeAct/CodexAct and include the review
    # feedback from the previous state.history[-1].detail in the prompt.
    return ActOutcome(observation={"target": target, "changed": True})


def review_artifact(outcome):
    target = Path(outcome.observation["target"])
    if not target.exists():
        return ReviewOutcome(False, f"missing target {target}", "blocking")
    return ReviewOutcome(True, "scope and target look acceptable", "info")


def verify(outcome):
    proc = subprocess.run(
        ["python", "-m", "pytest", "tests/test_stability_contract.py", "-q"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    detail = "pytest passed" if proc.returncode == 0 else f"pytest failed: {proc.returncode}"
    return VerifyOutcome(goal_met=proc.returncode == 0, detail=detail)


result = run_loop(
    gather=gather,
    act=act,
    review=review_artifact,
    verify=verify,
    conditions=[WorkListDrained(gather), MaxIterations(10)],
)
```


## Structured LLM Review

When delegating `review` to an LLM, require a JSON decision rather than free-form prose. If approval is based on exact string matches such as `No findings` or `LGTM`, a minor change in the review agent's writing style can incorrectly stop the loop, or an ambiguous response can be treated as success.

```python
import json


def review_artifact(outcome):
    prompt = f"""
Review this change. Return JSON only:
{{"decision":"approved|blocking","findings":["..."],"residual_risk":"..."}}

Criteria:
- scope matches the requested files
- public API compatibility is preserved
- docs and tests are consistent
- no irreversible operation was performed

Artifact summary:
{outcome.observation}
"""
    review_result = review_act({"prompt": prompt}).observation
    try:
        decision = json.loads(review_result.text)
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

In a dogfood harness, also verify that a real adapter was used, in addition to checking the review JSON decision. For example, if Codex is used for both `act` and `review`, the act side should have `verify` confirm that `CodexResult.command` contains `codex exec` and that `tokens > 0`. On the review side, either check the result of `review_act(...)` immediately inside `review_artifact`, or store command / tokens in an external record and read them from `verify`. Do not assume the review-side adapter result is implicitly available to `VerifyHook`, because `VerifyHook` directly receives only the act `ActOutcome`. This prevents a post-hoc recorder that merely records manual edits afterward as `ActOutcome(tokens=0)` from being mistaken for dogfooding.

## Feedback Representation

For a blocking review, the `ReviewOutcome` is placed in `StepRecord.detail` as JSON. If the review is not blocking, `verify` runs, and `detail` is the raw `verify.detail` string as before.

```json
{"review":{"approved":false,"feedback":"missing target docs/api-reference.md","severity":"blocking"}}
```

```text
pytest passed
```

Do not put the entire large diff in `detail`; store only the finding summary, severity, and file path. The next `act` can read the repository directly.

## WorkListGather Interaction

`done_when` should require both review approval and ground-truth verification. Set `max_attempts_per_item` so that one noisy review feedback item cannot consume the entire loop budget.

## HumanGate Boundary

`review` evaluates the post-act artifact. Irreversible operations are the responsibility of `HumanGate`. Keep commit, push, tag, publish, and deploy outside the loop, or make them explicit gated actions.
