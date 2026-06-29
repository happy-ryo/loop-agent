# Example: bulk translation of docstrings / comments (a ground-truth verify design example)

**intent**: translate the Japanese docstrings / comments of N source files into English, while **changing no code at all**.

This is a distillation of a self-translation PoC (dogfood) in which loop-agent's own source was translated into English by loop-agent itself. It is not a copy-paste template; it is a sketch for tracing the reasoning behind "how to design the 5 seams for a constrained bulk-edit task", and in particular **how to build verify on top of ground truth**.

## Why these 5 seams

| Seam | Choice in this domain | Reason |
|---|---|---|
| `gather` | pick files that still contain Japanese one at a time, starting from the fewest attempts (fair scheduling) | so that a single hard file does not monopolize all iterations and starve the others. `WorkListGather(strategy="fewest_attempts", max_attempts_per_item=...)` is the core. |
| `act` | `ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"])` | a mechanical translation job, so a cheap model is enough. Allow only Read+Edit so it cannot touch anything outside the file. |
| `verify` | **done only when all 5 mechanical checks pass** (below) | asking an LLM-as-judge "did you translate it?" converges on "pretending it's done". Measure success against ground truth. |
| `conditions` | `MaxIterations(20)` + `TokenBudget(generous)` | a reliable rate limiter for a long-running run. Reserve enough attempts to allow blind retries of stochastic failures, while still bounding runaway behavior. |
| `gate` | none | there is no destructive action (push, etc.) that needs human approval. |

## verify is the crux - ground truth in 5 stages ordered by ascending cost

A file becomes **done** only when all 5 checks pass. Order them from the cheapest gate so broken edits get rejected early:

1. **`parses_ok`** - `ast.parse` succeeds (rejects broken edits at the lowest cost).
2. **`japanese_cleared`** - no Japanese remains in the *translation targets* (comments and docstrings). **Non-docstring string literals are out of scope**. Rejecting on those would make the goal unreachable for "messages where Japanese is legitimately user-facing". Target comments precisely via `tokenize`, and docstrings via the docstring nodes of the `ast`.
3. **`code_unchanged`** - **code and non-docstring string literals are unchanged**. Parse both HEAD and the working tree, **collapse only the docstring values to `""`**, then compare with `ast.dump`. Everything other than docstrings (identifiers, signatures, control flow, imports, decorators, string literals such as error messages, etc.) is detected as a diff -> reject. Because `tests_pass` alone misses "breakage of behavior the tests don't observe", this stage becomes the guardian of the no-code-change constraint.
4. **`changed_files_scoped`** - `git diff --name-only` returns only the allowed target set. Agents can opportunistically edit test temp configuration, ignore files, or project settings unless verify rejects off-target tracked changes. For a batch run the allowed set is `FILES`; for a one-file-at-a-time commit flow it can be `{f}`.
5. **`tests_pass`** - that module's own `pytest` passes (re-importing in a subprocess detects behavioral breakage).

```python
import ast, subprocess
from loop_agent import VerifyOutcome

def code_signature(source: str) -> str:
    """AST dump that neutralizes only the *values* of docstrings. Non-docstring string
    literals are kept value and all, so changes to them are also detected as diffs. Only
    docstring translation is allowed."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = node.body[0] if node.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                doc.value.value = ""          # collapse only the docstring value
    return ast.dump(tree)

def changed_files() -> set[str]:
    proc = subprocess.run(["git", "diff", "--name-only"],
                          capture_output=True, text=True, check=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

def verify(outcome):                          # outcome: ActOutcome
    f = current_file()                         # the file gather selected
    src = open(f, encoding="utf-8").read()
    try:
        ast.parse(src)                                              # 1. parses_ok
    except SyntaxError:
        return VerifyOutcome(goal_met=False, detail=f"{f}: parse error")
    if has_japanese_in_comments_or_docstrings(src):                # 2. japanese_cleared
        return VerifyOutcome(goal_met=False, detail=f"{f}: japanese remains")
    head = subprocess.run(["git", "show", f"HEAD:{f}"],            # 3. code_unchanged
                          capture_output=True, text=True).stdout
    if code_signature(head) != code_signature(src):
        return VerifyOutcome(goal_met=False, detail=f"{f}: code or non-docstring string changed")
    unexpected = changed_files() - set(FILES)                         # 4. changed_files_scoped
    if unexpected:
        return VerifyOutcome(goal_met=False, detail=f"unexpected changed files: {sorted(unexpected)}")
    if subprocess.run(["pytest", test_for(f), "-q"]).returncode != 0:  # 5. tests_pass
        return VerifyOutcome(goal_met=False, detail=f"{f}: tests fail")
    mark_done(f)
    return VerifyOutcome(goal_met=all_files_done(), detail=f"{f}: done")
```

## Keep the act prompt lean

Do not paste the five verifier stages into every `act` prompt. They are policy enforced by the harness, not context the model needs to edit. For a file-by-file loop, pass only the selected file and the target spans or target rule:

```text
Edit only: {file}
Translate Japanese comments and docstrings in this file to English.
Do not change code or non-docstring string literals.
Do not edit other files. Do not run tests. Return after editing.
```

Avoid giving the agent a global progress table, the full remaining-file list, or test instructions such as "adapter tests must pass" unless that information is required for the edit. Those details trigger repeated repo exploration and tool use in every iteration. Let `verify` enforce changed-file scope, AST equivalence, residual Japanese scans, and tests mechanically.

Drive it with `run_loop(gather=WorkListGather(files, strategy="fewest_attempts", max_attempts_per_item=2),
act=ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"]), verify=verify,
conditions=[MaxIterations(20), TokenBudget(...)])`.

## Lessons that paid off in this domain

- **Reject off-target file changes mechanically**. A dogfood Codex run edited `.gitignore` and `pyproject.toml` while trying to work around pytest temp behavior. Treat `git diff --name-only` outside the allowed target set as red before declaring the file done.
- **Excluding string literals from the translation targets** is the crux. Trying to translate even user-facing strings like those in `print()` makes the goal unreachable for "legitimate Japanese". verify stage 2 (limit the targets) and stage 3 (freeze the non-targets) are two sides of this same constraint.
- **Reflexion is often unnecessary**. If the first-attempt failure is *stochastic* (the kind where haiku drops a single trailing comment in a long file), a blind-retry resample gets it through. Reserving enough retries via `max_attempts_per_item` is sufficient. Reflexion only wins when the failure is *systematic* (see [reflexion-when-to-use](../reflexion-when-to-use.md)).
- **A mechanical proof of behavior invariance**: apply verify stage 3 (`code_unchanged`) to every file and the `ast.dump` match proves nothing other than docstrings changed. Combined with `pytest all pass`, the translation preserves behavior with proof.

---

This is not a copy-paste template. Redesign gather / act / verify to fit your own domain (see [design-philosophy](../design-philosophy.md) / [seams.md](../seams.md)).
