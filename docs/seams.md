# Seam Details — gather / act / review / verify / conditions / gate

loop-agent owns only the orchestration core; all policy is injected through seams. This page is the canonical explanation that gathers the types and contracts for the five required seams plus the optional `review` seam, along with concrete `run_loop` usage patterns: basic use, runaway prevention, dual stop conditions, and a verification-driven demo.

## Seam List

The loop itself owns only the orchestration core. Policy is injected into these seams. `review` is an optional seam used only by loops that need post-act artifact review:

| Seam | Type | What you decide |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | What to do next: candidate selection, triage, queue strategy |
| `act` | `Callable[[ctx], ActOutcome]` | How to execute: model selection, LLM provider, subprocess, local function |
| `review` | `Callable[[ActOutcome], ReviewOutcome]` (optional) | Whether to accept the artifact produced by `act`: scope / API fit / intent match. When blocking, verification is skipped and feedback is left for the next iteration |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | What counts as success: pytest / AST / regex / anything else. Technically any verifier can be plugged in, but **ground truth is recommended** for success judgment |
| `conditions` | `list[StopCondition]` (stop conditions such as `MaxIterations`; compose with `AnyOf` for OR) | When to stop: iteration count / budget / goal / time |
| `gate` | `ActionGate` (`HumanGate`, etc.; implements `review(context, state)`. Target selection uses `on=Callable[[action], bool]`) | Which actions require human approval: commit / push / anything else |

> **Write verify against ground truth (recommended)**: The essence of seams is that anything can be plugged in, but when success judgment is delegated to an LLM-as-judge, the loop tends to converge on "pretending it succeeded" (report.md R1). Put LLM-backed design and scope judgments in `review`, and use mechanically decidable checks such as pytest exit codes, AST checks, or string scans for success judgment. For concrete examples, see [recipes/](./recipes/).

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # what to do      (gather)
    outcome = act(ctx)         # how to execute  (act)
    r = review(outcome)        # artifact review (review, optional)
    v = verify(outcome)        # what succeeded  (verify)
    state.update(v)
```

Only this loop body is loop-agent. Write the required seams, add `review` only when needed, and that becomes the loop for your domain.

For the `act` seam, `ClaudeCodeAct`, `CodexAct`, and custom adapters (`ActHook` Protocol) are already available as first-class act adapters. Any callable that conforms to `ActHook` can be plugged into the act seam, so you can freely choose a model, LLM provider, subprocess, or local function. See [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) for how to write an adapter.

## Usage

Pass `act` (action) and `verify` (verification = ground truth), compose the stop conditions, and pass them to `run_loop`:

```python
from loop_agent import run_loop, ActOutcome, ReviewOutcome, VerifyOutcome, MaxIterations, TokenBudget, Timeout

state = {"n": 0}

def act(ctx):
    """One step of action. Returns an observation and consumed tokens."""
    state["n"] += 1
    return ActOutcome(observation=f"did work #{state['n']}", tokens=10)

def review(outcome):
    """Optional: post-act artifact review."""
    return ReviewOutcome(approved=True, feedback="scope ok")


def verify(outcome):
    """Ground-truth verification. The loop exits naturally when goal_met=True."""
    done = state["n"] >= 3
    return VerifyOutcome(goal_met=done, detail="converged" if done else "")

result = run_loop(
    act=act,
    review=review,
    verify=verify,
    conditions=[MaxIterations(5), TokenBudget(1000), Timeout(30.0)],  # OR evaluation
)

print(result.status)   # "goal_met" / "stopped"
print(result.reason)   # "goal met" / "reached max iterations (5/5)", etc.
print(result.iterations, result.tokens_used)
```

Even if the goal is not met, the loop always stops at the configured limit, preventing AutoGPT-style runaway behavior:

```python
result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # never succeeds
    conditions=[MaxIterations(2)],
)
assert result.status == "stopped"
assert result.stop.name == "max_iterations"   # the condition that fired
print(result.reason)                          # "reached max iterations (2/2)"
```

## Dual Stop Conditions (GoalMet / NoProgress)

Semantic stops can be layered onto the same `AnyOf` composition as mechanical limits. `GoalMet` stops with **success** when a verifiable goal (a callable for tests / lint / a rubric) is satisfied, and `NoProgress` stops as **aborted** when the same action repeats without progress. Both fire through the existing `StopTrigger` format (`stop.name` = `"goal_met"` / `"no_progress"`), and they coexist consistently with mechanical limits through declaration-order OR composition:

```python
from loop_agent import run_loop, GoalMet, GoalCheck, NoProgress, MaxIterations

result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # do not use the verify hook; judge through conditions
    conditions=[
        GoalMet(lambda state: GoalCheck(met=run_tests() == 0, detail="suite green")),
        NoProgress(window=5, repeat=3),   # same action appears 3 times in the last 5 steps -> abort
        MaxIterations(50),                # mechanical backstop (R3)
    ],
)
# Success judgment is result.succeeded, which covers both natural verify-hook exits and GoalMet conditions.
# If stuck, stop.name == "no_progress"; if neither fires, "max_iterations" always stops the loop.
```

> `result.goal_met` represents **only natural exits from the verify hook** (`status == "goal_met"`).
> Success from a fired `GoalMet` condition returns as `status == "stopped"` / `stop.name == "goal_met"`,
> so `goal_met` remains False. Use `result.succeeded` when you want to judge success regardless of channel.

## Verification-Driven Demo (Run Until Sandbox Tests Turn Green)

This concrete demo applies the loop core to **real code**. It writes an intentionally broken function and its pytest into a temporary sandbox, then repeats `act` (apply a fix candidate) -> `verify` (judge using the **actual pytest exit code** as ground truth) **until the tests turn green**. With `goal_met=True` (exit code 0), the loop exits **naturally**; even in a scenario that cannot be fixed, limits such as `MaxIterations` always stop the loop, preventing runaway behavior. It does not rely on an LLM judge (report.md R1).

```bash
python3 examples/verify_driven_demo.py
# iter 0: applied candidate #0 -> verify=red   (red (exit=1))
# iter 1: applied candidate #1 -> verify=red   (red (exit=1))
# iter 2: applied candidate #2 -> verify=GREEN (green)
# status: goal_met / iterations: 3 / exit-codes: [1, 1, 0]
```

The reusable hooks live in `loop_agent.demo` (`CandidateApplier` = act / `ExitCodeVerifier` = verify / `attempt_index` = gather). `tests/test_verify_demo.py` reproduces and verifies this actual run with pytest (the shipped artifact is the verification target).

## Related

- [../README.md](../README.md) — entry point (positioning / seam overview / navigation summary)
- [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) — write an act adapter with the `ActHook` Protocol
- [review.md](./review.md) — API for optional post-act review and handling retry/state
- [recipes/](./recipes/) — concrete examples of ground-truth verify
- [safety.md](./safety.md) — scope of the `gate` seam and HumanGate
