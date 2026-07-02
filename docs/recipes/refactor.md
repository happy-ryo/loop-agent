# Recipe: Behavior-Preserving Refactoring (Path E)

This loop organizes N modules without changing their behavior, such as removing duplication, standardizing names, or splitting modules. The hard part is to define **`verify` precisely around the invariant that behavior has not changed.**

## Prose Intent (Pass Directly to Claude Code)

> This repository contains loop-agent, a thin loop engine whose `act` can use `ClaudeCodeAct`.
> **Build and run a loop that refactors each module under `src/foo/` without changing behavior.**
> - gather: Take target modules one at a time, starting with the fewest attempts for fair scheduling.
> - act: Organize one module with `ClaudeCodeAct(model="sonnet", allowed_tools=["Read","Edit"])`.
> - verify: **All existing tests pass** as the ground truth for public behavior, and public signatures remain unchanged.
> - conditions: `MaxIterations(15)` and a large `TokenBudget`.
> - Irreversible operations: Do not let `act` commit or push; it should only edit. A human reviews and commits after convergence.

## Ground Truth for Verify: How to Machine-Check That "Behavior Has Not Changed"

For refactoring, **the existing test suite is the contract** for verify. If tests that were green before the refactor are still all green afterward, then the behavior covered by those tests has been preserved. For modules with thin coverage, the discipline is to **add characterization tests before refactoring**: write tests first, refactor, then get everything passing.

```python
import subprocess, ast

def public_signatures(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return sorted((n.name, len(n.args.args)) for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and not n.name.startswith("_"))

def verify(outcome):
    m = current["module"]
    # 1. Public signatures are unchanged (the external contract)
    if public_signatures(m) != baseline_sigs[m]:
        return VerifyOutcome(goal_met=False, detail=f"{m}: public signature changed")
    # 2. All existing tests pass (the ground truth for behavior)
    if subprocess.run(["pytest", "-q"]).returncode != 0:
        return VerifyOutcome(goal_met=False, detail=f"{m}: suite red")
    done.add(m)
    return VerifyOutcome(goal_met=len(done) == len(MODULES), detail=f"{m}: refactored")
```

If you want to be stricter, you can use the same approach as the translation recipe: compare `ast.dump` output after normalizing string constants to verify that *only internal structure* changed. However, refactoring is meant to change structure, so **the test contract plus public signatures** is usually enough.

## Key Points

- **Tests are the contract**. Once verify is defined as "all existing tests pass," test coverage directly determines safety. Add characterization tests first for thinly covered areas, then run the loop.
- **Add a public signature invariant check** to verify. This cheaply detects breakage in the external shape that the tests do not cover.
- **Keep the scope to one module per iteration**. Tell `act` to organize only this module, and have verify run the full suite to check whether a local change broke the whole system.
- **A task where Reflexion is likely to help**: Refactoring failures tend to be *systematic*, for example, repeatedly breaking the same import order or making the same abstraction mistake. If the same error recurs, a lesson can help the next module, so this is a better fit for Reflexion than translation or flaky tasks. Use [reflexion-when-to-use.md](../reflexion-when-to-use.md) to decide.
- **Keep commit / push outside the loop**. Edits can be reverted with git, so the loop should only edit. Irreversible commit / push operations should be done by a human after convergence. `HumanGate` reviews the loop's discrete actions returned by `gather`; it cannot see a `git commit` that an `act` subprocess runs internally. If you want to gate commits, make commit a discrete loop action; see the limited human gate section in [docs/safety.md](../safety.md).
