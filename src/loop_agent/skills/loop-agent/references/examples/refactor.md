# Example: behavior-preserving refactor

> intent: tidy up N modules "without changing behavior" (remove duplication, unify naming, split things up). The hard part is writing verify sharply as "behavior has not changed".

## intent -> seam design (why these 5 seams)

| seam | choice for this domain | why |
| --- | --- | --- |
| gather | emit target modules one at a time, fewest attempts first | scoping to 1 module / 1 iteration localizes act and lets a verify failure be attributed to a single module. `WorkListGather(strategy="fewest_attempts")` provides fair scheduling so "one item doesn't monopolize iterations and starve the rest" |
| act | tidy one module with `ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"])` | refactoring only needs Read/Edit; don't hand irreversible operations (commit/push) to act. The edits themselves are revertible via git |
| **verify** | **the full existing test suite passes (ground truth) + public signatures unchanged** | this is the core. See below |
| conditions | `MaxIterations(15)` + `TokenBudget` (generous) | a mechanical hard cap. The per-item limit is carried on the `WorkListGather(max_attempts_per_item=...)` side |
| gate | none in principle. commit/push are isolated outside the loop | irreversible operations are performed by a human after convergence (see the pitfall below) |

## verify ground truth: how to mechanically judge "behavior unchanged"

For a refactor, verify's **contract is the existing test suite**. If tests that were green before the refactor are all still green afterward, then the behavior the tests cover is preserved.
- For modules with thin coverage, the discipline is to **add characterization tests before running** (tests first -> refactor -> all pass). How thorough the tests are is exactly how safe you are.
- Adding a **public-signature invariance check** cheaply detects "breakage of the external shape that the tests don't observe."

```python
import ast
import subprocess

from loop_agent import run_loop, VerifyOutcome, MaxIterations, TokenBudget
from loop_agent.adapters import ClaudeCodeAct
from loop_agent.discovery import WorkListGather, WorkListDrained

MODULES = ["src/foo/a.py", "src/foo/b.py", "src/foo/c.py"]

def public_signatures(path: str):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return sorted(
        (n.name, len(n.args.args)) for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    )

baseline = {m: public_signatures(m) for m in MODULES}  # fix before running
current = {}  # gather writes this iteration's target here (ClaudeCodeAct's observation is
              # model output text and does not include the gather ctx, so the target is shared separately)

def verify(outcome):
    m = current["module"]
    # 1. public signatures unchanged (the external-shape contract)
    if public_signatures(m) != baseline[m]:
        return VerifyOutcome(goal_met=False, detail=f"{m}: public signature changed")
    # 2. all existing tests pass (the ground truth of behavior)
    if subprocess.run(["pytest", "-q"]).returncode != 0:
        return VerifyOutcome(goal_met=False, detail=f"{m}: suite red")
    return VerifyOutcome(goal_met=True, detail=f"{m}: refactored")

gather = WorkListGather(
    MODULES,
    strategy="fewest_attempts",
    build_ctx=lambda item, attempt, state: current.update(module=item.id) or {
        "prompt": f"Refactor {item.id} without changing behavior (Read/Edit only, do not commit)",
    },
)

result = run_loop(
    gather=gather,
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"]),
    verify=verify,
    conditions=[
        WorkListDrained(gather),   # pass the gatherer. Stops successfully when all items are done
        MaxIterations(15),
        TokenBudget(2_000_000),
    ],
)
print(result.status, result.succeeded, result.reason, result.iterations)
```

> More strictly, an `ast.dump` comparison with string constants collapsed could verify that *purely the internal structure* changed, but since the whole point of a refactor is to change structure, the **test contract + public signatures** are usually enough.

## hard-won lessons (pitfalls of this domain)

- **Isolate commit / push outside the loop.** Edits are revertible via git, so keep the loop to edits only and have a human perform the irreversible commit/push after convergence. `HumanGate` reviews **the discrete loop action that gather returns**; it cannot see a `git commit` that act's subprocess issues internally. If you want to gate the commit, make the commit itself a discrete loop action.
- **Reflexion works well here.** Refactor failures tend to be systematic (e.g. "it breaks this import order every time"), and if the same mistake recurs, the lesson carries over to the next module (more Reflexion-friendly than translation/flaky).
- **verify runs the whole suite.** act touches only one module, but verify runs the entire suite to check that a local change hasn't broken the whole.

---

This is not a copy-paste template. Redesign gather / act / verify to fit your own domain (see [design-philosophy](../design-philosophy.md) / [seams.md](../seams.md)).
