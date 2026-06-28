# Example: stabilizing flaky tests

**intent**: Fix a group of intermittently-failing CI tests one at a time at the root cause, and close out with "reproducible passes."

This is an **idea sketch** of how to map a domain onto the seams, not a copy-paste template.

## intent -> seam design (why this shape)

Here is the reasoning, seam by seam, for mapping the "flaky stabilization" domain onto the 5 seams.

- **gather - pick one item, fewest attempts first**: there are multiple flaky tests. If one
  stubborn item eats up all of `MaxIterations`, the others never get worked on. Use fair
  scheduling that picks the item with the fewest attempts first to prevent starvation. Deciding
  "what to do next" is the job of gather.

- **act - edits only (`ClaudeCodeAct(allowed_tools=["Read","Edit"])`)**: have it read and fix
  the root cause. This is the core safety design -> **do not give act permission to run tests or
  to commit/push**. Granting act unrestricted `Bash` enables both (a) injecting retries/sleeps to
  fool verify, and (b) running `git commit` internally, letting an irreversible operation slip
  past the out-of-loop human gate. Cutting it off at tool permissions is the most reliable
  approach (`HumanGate` cannot observe the shell that act's subprocess runs internally).

- **verify - make N consecutive passes the ground truth (most important)**: with flaky tests a
  *single pass cannot tell you whether the problem is gone*. A test passing once and failing the
  next time is the very definition of flaky. So make verify's success criterion a mechanical,
  reproducibility-based ground truth: "the test passes 10 times in a row." This works because
  even if act masks the symptom with retries/sleeps, a verify that measures *reproducibility* is
  hard to pass - the design of verify itself becomes pressure that "lets nothing but a real
  root-cause fix through." The larger N is, the higher your confidence (a tradeoff against cost).
  Turning this into an LLM-as-judge ("do you think it's fixed?") immediately converges on
  "pretending to succeed."

- **conditions - `MaxIterations` + a generous `TokenBudget`**: double protection against runaway.
  An upper bound for when a stubborn item won't go away. Combined with OR via `AnyOf`.

- **gate - none by default; isolate irreversible operations outside the loop**: file edits can be
  reverted with git, so no gate is needed. It is commit/push that are irreversible, but as above
  we removed those from act's permissions and designed it so a *human commits everything in one
  batch after the loop converges*. If you really must gate the commit inside the loop, promote the
  commit to a "discrete action" and catch it via `HumanGate`'s `on=` (see [safety](../safety.md)).

## Sketch

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct
import subprocess

FLAKY = ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]  # extracted from CI logs, etc.
done, attempts, current = set(), {t: 0 for t in FLAKY}, {"test": None}

def gather(state):
    rem = [t for t in FLAKY if t not in done]
    t = min(rem, key=lambda t: (attempts[t], FLAKY.index(t)))   # fair scheduling
    current["test"] = t
    attempts[t] += 1
    return {"prompt": f"Find and fix the root cause of flaky test `{t}`. "
                      f"Do NOT add retries or sleeps to mask it. Edit only. "
                      f"Do NOT commit or push -- a human commits after convergence.",
            "test": t}

def run_n_times(test, n=10):                                    # ground truth
    return all(subprocess.run(["pytest", test, "-q"]).returncode == 0 for _ in range(n))

def verify(outcome):
    t = current["test"]
    stable = (not outcome.observation.failed) and run_n_times(t)  # observation is ClaudeCodeResult
    if stable:
        done.add(t)
    return VerifyOutcome(goal_met=(len(done) == len(FLAKY)),
                         detail=f"{t}: {'stable' if stable else 'still flaky'}")

result = run_loop(
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"], timeout=600),  # edits only
    gather=gather, verify=verify,                              # verify owns reproducibility checks
    conditions=[MaxIterations(20), TokenBudget(20_000_000)],
)
print(result.status, result.reason, sorted(done))  # "goal_met"/"stopped", reason, stabilized set
```

## Key points when adapting

- The return value of `ClaudeCodeAct`'s `act` is an `ActOutcome`, whose `observation` is a
  `ClaudeCodeResult` carrying a failure flag `failed` (a struct for passing success/failure and
  raw output to verify).
- If the flaky failures are *systematic* (all tests share the same "time-dependent race," etc.),
  Reflexion that carries lessons forward to the next test pays off. If each flaky test has an
  independent cause, it's hard to beat blind retry
  ([reflexion-when-to-use](../reflexion-when-to-use.md)).
- Question whether your own verify is only looking at a "single success." In domains that require
  reproducibility, build reproducibility into the ground truth side - "N in a row," "multiple
  seeds," and so on.

---

This is not a copy-paste template. Redesign gather/act/verify to fit your own domain
(see [design-philosophy](../design-philosophy.md) / [seams.md](../seams.md)).
