> This file is a load-on-demand bundled copy of `docs/seams.md`. The canonical source is `docs/seams.md` in the repository.

# Seam Details - gather / act / review / verify / conditions / gate

loop-agent "owns" only the orchestration core; all policy is injected through seams. This page is the canonical explanation that gathers, in one place, the types and contracts for the five required seams and the optional `review` seam, plus concrete `run_loop` usage patterns: basic use, runaway prevention, dual termination conditions, and a verification-driven demo.

## Seam List

The loop "owns" only the orchestration core. Policy is injected through these seams. `review` is an optional seam used only by loops that need post-act artifact review:

| Seam | Type | What you decide |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | What to do next: candidate selection, triage, queue strategy |
| `act` | `Callable[[ctx], ActOutcome]` | How to execute: model selection, LLM provider, subprocess, local fn |
| `review` | `Callable[[ActOutcome], ReviewOutcome]` (optional) | Whether to accept the artifact produced by `act`: scope / API fit / intent match. If blocking, skip verify and leave feedback for the next iteration |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | What counts as "success": pytest / AST / regex / anything else. Technically any implementation can be plugged in, but success checks are **recommended to use ground truth** |
| `conditions` | `list[StopCondition]` (stop conditions such as `MaxIterations`; composed with OR through `AnyOf`) | When to stop: count / budget / goal / time |
| `gate` | `ActionGate` (`HumanGate`, etc.; implements `review(context, state)`. Target selection uses `on=Callable[[action], bool]`) | Which actions require human approval: commit / push / anything else |

> **Write verify against ground truth (recommended)**: the essence of seams is that anything can be plugged in, but if success judgment is delegated to LLM-as-judge, the loop can easily converge on "pretending to have succeeded" (report.md R1). Put LLM-backed design and scope judgments in `review`; for success checks, use something mechanically decidable such as a pytest exit code, AST inspection, or string scanning. See [recipes/](https://github.com/happy-ryo/loop-agent/tree/main/docs/recipes/) for concrete examples.

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # what to do       (gather)
    outcome = act(ctx)         # how to execute   (act)
    r = review(outcome)        # artifact review  (review, optional)
    v = verify(outcome)        # what succeeds    (verify)
    state.update(v)
```

This loop body alone is loop-agent. Write the required seams and add `review` only when needed, and that becomes the loop for your domain.

For the `act` seam, `ClaudeCodeAct` / `CodexAct` / custom adapters (the `ActHook` Protocol) are already available as first-class act adapters. Any callable that conforms to `ActHook` can be plugged into the act seam, so you can freely choose the model, LLM provider, subprocess, or local function. See [adapters/writing-an-adapter.md](writing-an-adapter.md) for how to write adapters.

## Usage

Pass `act` (action) and `verify` (verification = ground truth), compose the termination conditions, and pass them to `run_loop`:

```python
from loop_agent import run_loop, ActOutcome, ReviewOutcome, VerifyOutcome, MaxIterations, TokenBudget, Timeout

state = {"n": 0}

def act(ctx):
    """One step of action. Returns the observation and consumed tokens."""
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

Even if the goal is not reached, the upper bound always stops the loop, preventing AutoGPT-style runaway behavior:

```python
result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # never achieved
    conditions=[MaxIterations(2)],
)
assert result.status == "stopped"
assert result.stop.name == "max_iterations"   # triggered condition
print(result.reason)                          # "reached max iterations (2/2)"
```

## Dual Termination Conditions (GoalMet / NoProgress)

You can add **semantic stops** to the same `AnyOf` composition used for mechanical upper bounds. `GoalMet` stops as **success** when a verifiable goal (a callable for tests / lint / rubric checks) is satisfied, and `NoProgress` stops as **aborted** when the same action repeats without progress. Both fire using the existing `StopTrigger` format (`stop.name` = `"goal_met"` / `"no_progress"`), and coexist consistently with mechanical upper bounds through declaration-order OR composition:

```python
from loop_agent import run_loop, GoalMet, GoalCheck, NoProgress, MaxIterations

result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # do not use the verify hook; check on the condition side
    conditions=[
        GoalMet(lambda state: GoalCheck(met=run_tests() == 0, detail="suite green")),
        NoProgress(window=5, repeat=3),   # same action 3 times in the most recent 5 steps -> abort
        MaxIterations(50),                # mechanical backstop (R3)
    ],
)
# Success is determined by result.succeeded, which covers both natural exit from
# the verify hook and the GoalMet condition.
# If stuck, stop.name == "no_progress"; if neither happens, "max_iterations" always stops it.
```

> `result.goal_met` represents **only natural exit through the verify hook** (`status == "goal_met"`).
> Success triggered by a `GoalMet` condition is returned as `status == "stopped"` / `stop.name == "goal_met"`, so
> `goal_met` remains False. To determine success regardless of channel, use `result.succeeded`.

## Verification-Driven Demo (Run Until the Sandbox Tests Turn Green)

This concrete demo applies the loop core to **real code**. It writes an intentionally broken function and its pytest into a temporary sandbox, then repeats `act` (apply a candidate fix) -> `verify` (judge using the **actual pytest exit code** as ground truth) **until the tests turn green**. With `goal_met=True` (exit code 0), the loop **exits naturally**; even in a scenario that cannot be fixed, an upper bound such as `MaxIterations` always stops it (runaway prevention). It does not rely on an LLM judge (report.md R1).

```bash
python3 examples/verify_driven_demo.py
# iter 0: applied candidate #0 -> verify=red   (red (exit=1))
# iter 1: applied candidate #1 -> verify=red   (red (exit=1))
# iter 2: applied candidate #2 -> verify=GREEN (green)
# status: goal_met / iterations: 3 / exit-codes: [1, 1, 0]
```

The reusable hooks are in `loop_agent.demo` (`CandidateApplier` = act / `ExitCodeVerifier` = verify / `attempt_index` = gather). `tests/test_verify_demo.py` reproduces and verifies this actual run with pytest (the shipped artifact is the verification target).

## Related

- [../README.md](https://github.com/happy-ryo/loop-agent/blob/main/README.md) - entry point (positioning / seam overview / navigation summary)
- [adapters/writing-an-adapter.md](writing-an-adapter.md) - write an act adapter with the `ActHook` Protocol
- [review.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/review.md) - API for optional post-act review and handling of retry/state
- [recipes/](https://github.com/happy-ryo/loop-agent/tree/main/docs/recipes/) - concrete examples of ground-truth verify
- [safety.md](safety.md) - scope of the `gate` seam and HumanGate
