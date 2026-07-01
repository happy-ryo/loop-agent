# AI API Map

This page is the capability map for coding agents that assemble loop-agent
harnesses from prose intent. It is not a complete API reference. Use it to pick
the smallest surface that matches the loop you are building, then read the
specific reference page for the seam that needs detail.

## Start With The Public API Groups

`loop_agent` exposes machine-readable groups for API selection:

```python
import loop_agent

loop_agent.CORE_API        # first loop: driver, outcomes, caps, verifier helpers
loop_agent.HARNESS_API     # production harness: persistence, gate, work-list
loop_agent.ADVANCED_API    # reflexion, transport, discovery, notifier primitives
loop_agent.OPERATIONS_API  # observation, dashboards, spikes, wake helpers
```

Do not treat every top-level symbol as equal. For most harnesses, import from
`CORE_API` first, then add exactly the `HARNESS_API` pieces required by the
user's domain.

## If The User Wants A First Bounded Loop

Use:

```python
from loop_agent import ActOutcome, VerifyOutcome, MaxIterations, Timeout, run_loop
```

Design:

- `act(ctx) -> ActOutcome`: one bounded unit of reversible work.
- `verify(outcome) -> VerifyOutcome`: a machine oracle, not an LLM judge.
- `conditions=[MaxIterations(...), Timeout(...)]`: at least one mechanical cap.

Read next: `seams.md`.

## If Verify Can Be A Command Or Test

Use:

```python
from loop_agent import CommandVerifier, PytestVerifier, RegexVerifier
```

Pick the sharpest available oracle:

- test suite or focused test: `PytestVerifier([...], timeout=...)`
- compiler, linter, smoke probe, schema check: `CommandVerifier([...])`
- textual adapter output signal: `RegexVerifier(...)`

Do not use an LLM-as-judge when a command, AST check, regex, or probe can decide
success.

## If The Loop Must Resume After Interruption

Use:

```python
from loop_agent import DBProgressLog, MaxIterations, Timeout, run_loop

with DBProgressLog("loop-state.db", run_id="my-run") as db:
    result = run_loop(
        gather=gather,
        act=act,
        verify=verify,
        conditions=[MaxIterations(20), Timeout(1800)],
        initial_state=db.state,
        on_step=db.on_step,
    )
    db.record_result(result)
```

Rules:

- Treat `state.db` as the source of truth for resume.
- Pass both `initial_state=db.state` and `on_step=db.on_step`.
- Keep observations JSON-stable when `gather` or `NoProgress` depends on history.

Read next: `persistence-and-resume.md`.

## If Act Is A Coding-Agent CLI

Use:

```python
from loop_agent.adapters import ClaudeCodeAct, CodexAct
```

For self-maintenance or repo edits, start with reversible permissions:

```python
act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], timeout=600)
# or
act = CodexAct(sandbox="workspace-write", timeout=600)
```

Rules:

- Keep prompts lean; put policy and proof in the harness and verifier.
- Do not commit, push, deploy, or mutate external services inside the CLI act
  unless that action is modeled as a discrete gated action.
- Count tokens from the adapter result; do not re-parse or double-count usage.

Read next: `writing-an-adapter.md` and `safety.md`.


## If Act Needs Artifact Review Before Verify

Use:

```python
from loop_agent import ReviewOutcome
```

Shape:

```python
def review(outcome):
    if violates_required_shape(outcome.observation):
        return ReviewOutcome(
            approved=False,
            severity="blocking",
            feedback="artifact shape is invalid; fix the missing field",
        )
    return ReviewOutcome(approved=True)

result = run_loop(
    gather=gather,
    act=act,
    review=review,
    verify=verify,
    conditions=[MaxIterations(10), Timeout(900)],
)
```

Use `review=` when the agent's artifact should be checked before running the
final ground-truth verifier, especially when the feedback should guide the next
iteration. A blocking review records a failed step and skips `verify` for that
iteration, so the next `gather` can feed the review feedback back into `act`.

The review seam may itself call a generated-AI adapter, including
`ClaudeCodeAct`, `CodexAct`, or a custom `ActHook`. When you do this, control the
reviewer's output format. Prefer a small JSON object over free text, and treat
parse failures or unknown fields as blocking so the loop does not accept an
ambiguous "looks good" response:

```json
{"decision":"approved","findings":[],"residual_risk":"pytest still runs in verify"}
```

```json
{"decision":"blocking","findings":["public API docs omit ReviewOutcome"],"residual_risk":"AI map may guide agents to skip review"}
```

This pattern lets a generated-AI reviewer produce targeted feedback, while
`verify` still catches the final result with ground truth. For example, review can
require JSON shape, scope, API fit, or documentation consistency; verify can then
run pytest, an AST/schema check, or a regex that mechanically rejects outputs that
did not satisfy the required format. If review output is meant to be inspected by
`verify`, persist or encode it in a deterministic shape rather than relying on
natural-language phrasing.

Distinctions:

- `HumanGate` runs before `act` and protects irreversible proposed actions.
- `review=` runs after `act` and evaluates the produced artifact.
- `verify` remains the ground-truth success oracle; review is a pre-verifier
  quality or shape check, not a replacement for machine success criteria.

Read next: `seams.md` and the canonical docs page `docs/review.md`.

## If There Are N Files, Bugs, Rows, Or Tasks

Use:

```python
from loop_agent import MaxIterations, Timeout, WorkItem, WorkListDrained, WorkListGather
```

Shape:

```python
work = WorkListGather(
    [WorkItem("a", payload={"path": "a.py"}), WorkItem("b", payload={"path": "b.py"})],
    strategy="fewest_attempts",
    max_attempts_per_item=3,
    done_when=lambda item, record: record.goal_met,
)

result = run_loop(
    gather=work,
    act=act,
    verify=verify,
    conditions=[WorkListDrained(work), MaxIterations(20), Timeout(1800)],
)
```

Rules:

- Compose `WorkListDrained` with a mechanical cap.
- Prefer `fewest_attempts` or `round_robin` to avoid starvation.
- Keep per-item context small; do not paste the whole work list into every act
  prompt unless needed.

Read next: `transport.md`.

## If An Operation Is Irreversible

Use:

```python
from loop_agent import DBProgressLog, HumanGate, LoopStore, connect
```

Design:

- Make the irreversible operation a discrete action returned by `gather`.
- Set `HumanGate(on=...)` to match only that action kind.
- Persist gate decisions in `LoopStore` and progress in `DBProgressLog`.

Do not rely on `HumanGate` to see a `git commit`, `git push`, deploy, or API
mutation hidden inside an external CLI subprocess. The gate reviews only the
discrete context returned by `gather` before `act` runs.

Read next: `safety.md`.

## If The User Asks For Reflexion

Use Reflexion only when failures are stochastic or strategic and a verbal lesson
can improve the next episode. Do not add it for systematic failures that a sharper
verifier or better prompt can fix.

Use:

```python
from loop_agent import run_reflexion
```

Read next: `reflexion-when-to-use.md`.

## If The User Needs Operations Visibility

Use:

```python
from loop_agent import JsonlEventSink, run_observed_loop
from loop_agent import render_dashboard_html, scan_spikes
```

Rules:

- Observability should not change loop control policy.
- Dashboards and spike scans are read-only operations helpers.
- Circuit breakers are explicit stop conditions; add them only when the policy is
  clear.

## Import Guidance

Top-level imports are intentionally broad so coding agents can discover the
harness surface quickly. Prefer module imports for provider adapters and very
specialized surfaces:

```python
from loop_agent.adapters import ClaudeCodeAct, CodexAct, ModelLadder
from loop_agent.operations import render_dashboard_html, scan_spikes
from loop_agent.transport import Transport, SqliteWakeQueue
```

When in doubt, keep the loop small: choose the seam, choose the ground truth,
choose the cap, and isolate irreversible actions.
