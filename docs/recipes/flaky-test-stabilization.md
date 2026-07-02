# Recipe: Stabilizing Flaky Tests (Path E)

This loop fixes CI test failures that appear intermittently, one test at a time, using loop-agent + Claude Code, and finishes with a **reproducible passing result**.

## Prose Intent (pass directly to Claude Code)

> This repository includes loop-agent (a thin loop engine for `gather -> act -> verify -> repeat`; `loop_agent.adapters.ClaudeCodeAct` can be used for `act`).
> **Build and run a loop that stabilizes flaky tests in CI.**
> - gather: Choose flaky tests to stabilize one at a time (starting with the fewest attempts = fair scheduling).
> - act: Use `ClaudeCodeAct(model="sonnet", allowed_tools=["Read","Edit"])` to read the code, find the root cause, and fix it (test execution belongs to verify).
> - verify: After the fix, mark the test done only if it **passes 10 consecutive times** (if it fails even once, the goal has not been met).
> - conditions: `MaxIterations(20)` and a large `TokenBudget`.
> - Irreversible operations: Do not allow act to commit or push (edits only). After convergence, a human reviews the fix and commits it.

## Expected Harness Shape

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome, ActOutcome
from loop_agent.adapters import ClaudeCodeAct
import subprocess

FLAKY = ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]   # Extracted from CI logs, etc.
done, attempts = set(), {t: 0 for t in FLAKY}
current = {"test": None}

def gather(state):
    rem = [t for t in FLAKY if t not in done]
    t = min(rem, key=lambda t: (attempts[t], FLAKY.index(t)))    # Fair scheduling
    current["test"] = t
    attempts[t] += 1
    return {"prompt": f"Find and fix the root cause of the flaky test `{t}`. "
                      f"Do not add retries or sleeps to mask it. Edit the code/test as needed. "
                      f"Do NOT commit or push -- a human commits after the loop converges.",
            "test": t}

def run_n_times(test, n=10):
    for _ in range(n):
        if subprocess.run(["pytest", test, "-q"]).returncode != 0:
            return False
    return True

def verify(outcome):
    t = current["test"]
    stable = (not outcome.observation.failed) and run_n_times(t, n=10)   # Ground truth
    if stable:
        done.add(t)
    all_done = len(done) == len(FLAKY)
    return VerifyOutcome(goal_met=all_done, detail=f"{t}: {'stable' if stable else 'still flaky'}")

result = run_loop(
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"], timeout=600),   # Edits only
    gather=gather, verify=verify,                                                      # Reproducibility checks belong to verify
    conditions=[MaxIterations(20), TokenBudget(20_000_000)],
)
print(result.status, result.reason, sorted(done))
```

## Key Points

- **verify means "N consecutive passes"**. A single pass is not enough to determine whether a flaky test has been eliminated, so consecutive passes are the ground truth. Larger N increases confidence (with a cost tradeoff).
- **Forbid masking with retries or sleeps** in the prompt. If act is allowed to mask the issue, verify may pass while the flakiness remains. Because verify measures reproducibility, this design makes anything other than a root-cause fix hard to pass.
- **Fair scheduling is required**. Choose the test with the fewest attempts first so one difficult test does not consume all of `MaxIterations`.
- **Does Reflexion help?** If flaky failures are *systematic* (for example, all tests share the same "time-dependent race" pattern), a lesson such as "freeze time" can help on the next test, so this is a good fit for Reflexion. If each flaky test has an unrelated independent cause, it will behave almost the same as blind retry. Use [reflexion-when-to-use.md](../reflexion-when-to-use.md) to decide.
- **Keep commit / push outside the loop**. File edits from the fix can be reverted with git. **Reproducibility checks (test execution) belong to `verify`; do not give `act` permissions that would allow commit / push, such as unrestricted `Bash`** (`HumanGate` cannot see a `git commit` issued internally by the `act` subprocess, so removing that tool permission is the reliable boundary). Irreversible commit / push operations are performed by a human after convergence. If shell access is absolutely needed inside act, restrict it to the test command and do not provide commit-related capabilities. If you truly want to gate commits, make commit a **discrete loop action**; see [the limited human gate section in docs/safety.md](../safety.md).
