# Recipe: Batch Translation of Docstrings / Comments (Path E)

This is a loop for translating Japanese docstrings / comments in N source files into English (or the reverse). The constraint is that **no code may be changed at all**. This recipe formalizes loop-agent's **Self-translation PoC**: a dogfooding run where loop-agent translated its own source code into English using loop-agent itself.

## Prose Intent (Pass Directly to Claude Code)

> This repository contains loop-agent, a thin loop engine whose `act` can use `ClaudeCodeAct`.
> **Create and run a loop that translates docstrings and comments under `src/loop_agent/` into English.** However:
> - Do not change code, public APIs, types, test names, or **string literals** at all; only comments and docstrings may change.
> - gather: pick one file at a time where Japanese still remains, starting with the fewest attempts for fair scheduling.
> - act: `ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"])`.
> - verify: done only after all five mechanical checks below pass.
> - conditions: `MaxIterations(20)` and a large `TokenBudget`.

## Verify Uses Five Levels of Ground Truth (Ascending Cost)

A file becomes **done** only when all five checks pass:

1. **`parses_ok`** - `ast.parse` succeeds, rejecting broken edits at the lowest cost.
2. **`japanese_cleared`** - no Japanese remains in the *translation targets* (comments and docstrings). **Non-docstring string literals are out of scope** because user-facing messages may legitimately remain Japanese. Rejecting them would make the goal unreachable. Comments are targeted precisely with `tokenize`, and docstrings are targeted precisely with `ast` docstring nodes.
3. **`code_unchanged`** - **code and non-docstring string literals have not changed**. This mechanically enforces the constraint "do not change code/API/types/test names/string literals." Parse both `HEAD` and the worktree, **replace only docstring values with `""`** (allowing translation), then compare `ast.dump`. String literals other than docstrings, such as error messages and CLI output, remain in the comparison with their values intact, so any changes to them are also detected as diffs. If the dumps differ, some identifier, signature, control flow, import, decorator, or non-docstring string has changed, so **reject**. `tests_pass` alone can miss "behavior changes not covered by tests," so this stage guards the no-code-change constraint.
4. **`changed_files_scoped`** - `git diff --name-only` returns only the permitted target file set. Agents often make incidental changes for test temporary directories or settings, so reject as soon as any out-of-scope tracked file changes. Even if `act` is instructed "only this file," there is no guarantee unless `verify` enforces it mechanically. For an `n`-file translation batch, the permitted set is `FILES`; for a one-file-per-commit workflow, use `{f}`.
5. **`tests_pass`** - the module's own `pytest` passes. Because this re-imports through a subprocess, it detects edits that break behavior.

```python
import ast, tokenize, io, subprocess

def code_signature(source):
    """AST dump with only docstring *values* neutralized. String literals other than
    docstrings, such as error messages and CLI output, keep their values, so changes
    to them are also detected as diffs. Only docstring translation is allowed; changes
    to code structure or non-docstring strings are rejected."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = node.body[0] if node.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                doc.value.value = ""               # neutralize only the docstring value
    return ast.dump(tree)

def changed_files():
    proc = subprocess.run(["git", "diff", "--name-only"],
                          capture_output=True, text=True, check=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

def verify(outcome):
    f = current["file"]
    src = open(f, encoding="utf-8").read()
    try:
        tree = ast.parse(src)                      # 1. parses_ok
    except SyntaxError:
        return VerifyOutcome(goal_met=False, detail=f"{f}: parse error")
    if has_japanese_in_comments_or_docstrings(tree, src):   # 2. japanese_cleared (excluding string literals)
        return VerifyOutcome(goal_met=False, detail=f"{f}: japanese remains")
    head = subprocess.run(["git", "show", f"HEAD:{f}"],     # 3. code_unchanged (reject non-docstring diffs)
                          capture_output=True, text=True).stdout
    if code_signature(head) != code_signature(src):
        return VerifyOutcome(goal_met=False, detail=f"{f}: code or non-docstring string changed")
    unexpected = changed_files() - set(FILES)       # 4. changed_files_scoped (reject out-of-scope changes)
    if unexpected:
        return VerifyOutcome(goal_met=False, detail=f"unexpected changed files: {sorted(unexpected)}")
    if subprocess.run(["pytest", test_for(f), "-q"]).returncode != 0:   # 5. tests_pass
        return VerifyOutcome(goal_met=False, detail=f"{f}: tests fail")
    done.add(f)
    return VerifyOutcome(goal_met=len(done) == len(FILES), detail=f"{f}: done")
```

## Keep the Act Prompt Lean (Cost Design)

This recipe's constraints are **mechanically enforced by verify**, so do not paste them into the `act` prompt every time. In an iterative loop, prompt bloat is multiplied by the number of iterations. Give `act` only "the one file to edit now" and "the translation target in that file."

Recommended prompt shape:

```text
Edit only: {file}
Translate Japanese comments and docstrings in this file to English.
Do not change code or non-docstring string literals.
Do not edit other files. Do not run tests. Return after editing.
```

Anti-patterns to avoid:

- Pasting the full text of the five verify checks into every prompt.
- Passing the list of already completed files, the full list of remaining files, or the test plan every time.
- Writing `Do not inspect tests` while also giving `act` a verification responsibility such as `adapter tests must pass`.

Those are `verify`'s responsibility. If `act` starts doing extra repository exploration, diff checks, or test runs, prompt/tool cost can outweigh the failure-isolation benefit of a file-by-file loop. In dogfooding, one batch iteration cost 801,370 tokens, while a file-by-file loop with an excessive `act` prompt grew to 7 iterations / 2,470,874 tokens. If you choose a file-by-file loop, review "how much information the agent reads per iteration" before running it.

## Actual PoC Results (Proof of Embeddability)

loop-agent translated its own 10 files (290 total Japanese hits) into English with `haiku`:

| | Run 1 (no Reflexion) | Run 2 (Reflexion) |
|---|---|---|
| Result | 10/10 (`goal_met`) | 10/10 (`converged`) |
| Inner iterations | 13 | 14 (10 + 4) |
| Wall clock | About 33 minutes | About 32 minutes |
| Token accounting | 11.17M | 10.72M |
| Files needing a second attempt | 3 | 4 |
| Suite after translation | 559 passed | 559 passed |

**Mechanical proof of unchanged behavior**: The same method as verify stages 3 (`code_unchanged`) and 4 (`changed_files_scoped`) above was applied to all 10 files: parse `HEAD` and the worktree, replace only docstring values with `""`, and compare `ast.dump` -> all matched. No identifiers, signatures, control flow, imports, decorators, or non-docstring string literals changed. This is also the gate that verify applies to each file, so behavior-breaking edits cannot become done. Together with `559 passed`, the translation was **behavior-preserving with proof**. The 10 PoC files contained no Japanese in non-docstring string literals, and the "do not touch string literals" constraint was in fact preserved.

## Key Points

- **Reject out-of-scope file changes in verify**. In actual runs, the agent sometimes touched `.gitignore` / `pyproject.toml` as a mitigation for test temporary directories. Compare `git diff --name-only` with the permitted set, and do not mark the file done if any out-of-scope tracked file changed.
- **The critical point is excluding string literals from translation targets**. Target only comments / docstrings; do not touch strings in `print()` and similar code. If this is wrong, the goal can become unreachable because of legitimate Japanese that should not be translated.
- **Token accounting**: When `ClaudeCodeAct` runs with `Read`+`Edit`, `cache_read` accumulates across internal multi-turn work. Token accounting has been fixed to exclude `cache_read`, so `TokenBudget` now scales with real cost (Issue #55; previously it fired too early). For reliable rate limiting in long runs, also using `MaxIterations` is prudent. For details, see [quickstart troubleshooting](../quickstart.md#5-troubleshooting-common-failure-points).
- **Reflexion is often unnecessary for stochastic misses**: Initial failures in this translation task were often *stochastic*, such as haiku dropping one trailing comment in a long file, and a blind retry could pass by resampling. Run 1 (no Reflexion) and Run 2 (Reflexion) had nearly the same cost and result.
- **Use Reflexion or deterministic pre-processing for repeated structural failures**: If review or verify reports the same issue repeatedly, such as broken generated anchors, unstable link fragments, or the same file receiving the same rejected edit, treat it as systematic. Stop the inner loop with `NoProgress`, turn the failure into a lesson or deterministic normalizer, and resume from persisted state.
- **Fair scheduling**: Use file-level round robin, starting with the fewest attempts, so one difficult file does not monopolize all iterations.

## Efficient Harness Shape

For large translation jobs, do not let a coding agent repeatedly inspect the
repository. Use deterministic scanning and patching, and keep the LLM call scoped
to one chunk:

```bash
python scripts/efficient_translation_harness.py --dry-run --target docs
python scripts/efficient_translation_harness.py --codex --target docs --run-id docs-translation \
  --manifest efficient-translation-manifest.json --batch-size 8
```

The harness in `scripts/efficient_translation_harness.py` uses this split:

| Stage | Responsibility |
|---|---|
| `scan_japanese_chunks` | Find Japanese-containing Markdown lines, Python comments, and Python docstrings without LLM calls. |
| manifest | Save the initial chunk list so resume replays the same work items instead of re-scanning a changed tree. |
| `WorkListGather` | Schedule manifest chunks fairly and cap attempts per chunk. |
| `CodexChunkTranslator` | Send one chunk, or a same-file text chunk batch, and compact constraints to Codex; require JSON output. |
| `apply_translation` | Patch the line/column-anchored old chunk locally and parse Python after docstring edits. |
| `verify_patch` | Record a machine-readable patch result; `WorkListDrained` decides completion. |

Use `--batch-size` to amortize the fixed cost of model startup across nearby
Markdown/text chunks. Keep Python comments and docstrings as single chunks unless
you add stronger code-aware batching. This keeps loop-agent in charge of state,
fairness, retry, token budget, and stuck detection while removing repository
exploration from the model call. It is the preferred starting shape when token
efficiency matters. For a production translation pipeline, add domain-specific
normalizers for Markdown anchors and project-specific glossary enforcement.
