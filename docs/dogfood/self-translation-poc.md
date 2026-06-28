# Dogfood PoC: Self-translation of loop-agent with loop-agent

**Issue:** #37 ([dogfood] Self-translation PoC)
**Branch:** `experiment/self-translation-poc` (experiment only -- **not merged to `main`**)
**Date:** 2026-06-28
**Verification depth:** full

## 1. What this PoC does

We pointed loop-agent's *own* loop engine at loop-agent's *own* source: the
harness drives `run_loop` / `run_gated_loop` / `run_reflexion` plus the
`ClaudeCodeAct` adapter to translate the Japanese docstrings and comments of ten
`src/loop_agent` modules into English -- **without changing any code, public
API, type signature, or test name**, and keeping the full `pytest` suite green
(552 -> 559 tests, the +7 being this PoC's own wiring tests).

It is a genuine "the tool builds the tool" loop: `act` is `claude --print`
launched by loop-agent's adapter, and `verify` is loop-agent's own
ground-truth-driven termination contract.

### Artifacts

| Artifact | Path |
| --- | --- |
| Harness (driver + CLI) | `examples/self_translation_poc/harness.py` |
| Three-stage verifier | `examples/self_translation_poc/verify.py` |
| Wiring tests | `tests/test_self_translation_poc.py` (7 tests) |
| Run 1 log (no Reflexion) | `examples/self_translation_poc/run_no_reflexion.jsonl` |
| Run 2 log (Reflexion) | `examples/self_translation_poc/run_reflexion.jsonl` |

## 2. The ten target files

Selected from `git ls-files src/loop_agent` after the `claude-loop -> loop-agent`
rename: ten modules that actually carry Japanese in comments/docstrings **and**
each have a dedicated test module (so the per-module pytest verify stage is
real). This aligns with Issue #37's recommended candidates where those still
hold Japanese; `conditions.py` and `progress.py` from the original list were
dropped because they were already English (0 Japanese characters).

| File | Lines | Japanese hits (comments+docstrings) |
| --- | ---: | ---: |
| `waker.py` | 174 | 7 |
| `convergence.py` | 194 | 9 |
| `observe.py` | 322 | 24 |
| `events.py` | 209 | 27 |
| `memory.py` | 234 | 23 |
| `evaluator.py` | 318 | 19 |
| `adapters/claude_code.py` | 392 | 34 |
| `reflexion_store.py` | 569 | 46 |
| `transport.py` | 576 | 53 |
| `gate.py` | 491 | 48 |
| **total** | | **290** |

(A "hit" is one comment or docstring containing Japanese, as counted by the
verifier; not raw characters.)

## 3. Loop design (gather -> act -> verify)

Following Issue #37's pseudocode:

- **gather** picks the next not-yet-done file (round-robin by fewest attempts, so
  one stubborn file never starves the others) and renders a strict translation
  prompt. When running under Reflexion it weaves in the prior episode's lessons.
- **act** is `ClaudeCodeAct(allowed_tools=["Read","Edit"],
  permission_mode="acceptEdits", model="haiku")`. It launches `claude --print`,
  which reads the file and edits it in place. *The adapter does the work* -- that
  is the dogfood.
- **verify** is a three-stage ground-truth check (`examples/.../verify.py`),
  evaluated in increasing cost order; a file is **done** only when all three pass:
  1. `parses_ok` -- `ast.parse` still succeeds (a botched edit fails cheaply).
  2. `japanese_cleared` -- no Japanese remains in the *translation targets*:
     comments and docstrings. **Non-docstring string literals are out of scope**
     and ignored, because user-facing message strings may legitimately keep
     Japanese; flagging them would make the goal unreachable. (Targeting is done
     with `tokenize` for comments and `ast` docstring nodes; a unit test proves a
     Japanese string literal is *not* flagged while a same-line trailing comment
     is.)
  3. `tests_pass` -- the module's own `pytest` file passes (a subprocess
     re-imports the edited module, so a behaviour-breaking edit is caught).

**Stop conditions** compose the mechanical caps with the semantic goal, per the
brief: `MaxIterations(20)`, `TokenBudget(...)`, `GoalMet(done == files)`, plus a
`Timeout` backstop. The loop also ends naturally when the `verify` hook reports
all ten done.

## 4. Design decisions (worker discretion points)

The brief flagged three judgement calls. Decisions and rationale:

### 4.1 HumanGate: auto-approve, gating every step

**Decision:** wire a real `HumanGate` via `run_gated_loop` with `on=lambda _:
True` (every translation action is gated) and a resolver that auto-approves
(`Decision("approve")`).

**Why:** the PoC is itself a human-delegated task (secretary delegates to the
worker), so the worker runs the approval cycle itself, exactly the "human =
secretary delegate" framing in the brief. Gating *every* step deliberately
exercises the full gate path each iteration -- `LoopStore` decision registration,
the in-progress lease lifecycle (`resolved -> executing -> executed`), and the
JSON-native action guard -- giving maximum dogfood coverage of `gate.py` and
`store.py`.

**Honest caveat:** translation edits are git-reversible, so a strict reading of
the gate's "irreversible actions only" contract would leave them *ungated*. A
production config would instead reserve the gate for the genuinely irreversible
action -- `commit` / `push` -- which here stays a manual worker step outside the
loop. We chose coverage over literalism and document it rather than hide it. The
alternative the brief offered (`gate = 0` actions, act-only) is available behind
`--no-gate`.

### 4.2 Verify thresholds

**Decision:** binary all-three-must-pass, no fuzzy threshold. `japanese_cleared`
requires **zero** residual Japanese in comments/docstrings (not "mostly
translated"); `tests_pass` requires exit code 0.

**Why:** the ground truth is mechanical and non-gameable (parse + scan + real
pytest). A partial-credit threshold would let an almost-translated file count as
done and ship Japanese; there is no reason to soften a check that is already
cheap and exact. This is the same philosophy as the repo's existing
verify-driven demo (real pytest exit code as ground truth, not an LLM judge).

### 4.3 Reflexion: real `run_reflexion`, evaluator promotion disabled

**Decision:** Run 2 uses the real `run_reflexion` (two-signal model + RQGM epoch
core) with `propose_evaluator=None`, so the incumbent evaluator is frozen and no
evaluator promotion happens. Each **episode** is a full inner `run_gated_loop`
over the still-failing files; `reflect` turns a failed-verify trajectory into a
concrete language lesson (e.g. "`transport.py` still has Japanese in a comment at
line N -- translate every inline trailing comment too") wired into the next
episode's prompts. Convergence = `RubricThreshold(target=1.0)` (all files done);
`MaxEpisodes(4)`, `epoch_len=2`.

**Why disable promotion:** the RQGM evaluator-promotion safety core defends
against *evaluator gaming* -- it matters when the reward signal is an
LLM-as-judge that could be inflated. Here the ground truth is parse + Japanese
scan + pytest, which is already non-gameable, so a moving evaluator would add
risk and noise with no benefit. Using `run_reflexion` with a frozen evaluator
exercises the real outer-loop machinery (episodes, epoch boundaries, episodic
memory, lesson admission/grounding) while keeping the signal honest.

To make the **no-Reflexion vs Reflexion** comparison meaningful, Run 2's first
episode is capped at one attempt per file (`inner_max_iterations=10`): the hard
files fail episode 0, and Reflexion's lesson-guided second episode recovers them.

## 5. Results

### 5.1 Run 1 -- no Reflexion (blind retry within one inner loop)

| Metric | Value |
| --- | --- |
| Status | `goal_met` (10/10 files done) |
| Iterations | 13 |
| Wall clock | ~1995 s (~33 min) |
| Tokens charged | 11,171,891 |
| Files needing a retry | `events.py` (2), `reflexion_store.py` (2), `transport.py` (2) |
| Files done first try | the other 7 |
| Suite after | 559 passed |

Seven files translated cleanly on the first attempt. Three files left residual
Japanese or a partial translation on attempt 1 and were fixed by a **blind**
round-robin retry (no lesson) on attempt 2. The model was `haiku` throughout.

### 5.2 Run 2 -- Reflexion outer loop

| Metric | Value |
| --- | --- |
| Status | `converged` (rubric threshold = all files done) |
| Episodes | 2 |
| Inner iterations (total) | 14 |
| Wall clock | ~1906 s (~32 min) |
| Tokens charged | 10,722,919 |
| Suite after | 559 passed |

Per-episode (from `run_reflexion.metrics.json`):

| Episode | ground-truth aggregate | files done | succeeded | lesson admitted |
| ---: | ---: | ---: | --- | --- |
| 0 (one-shot, cap 10) | 0.60 | 6/10 | no | **yes** |
| 1 (lesson-guided) | 1.00 | 10/10 | yes | no |

Episode 0 gave each file exactly one attempt; `waker`, `convergence`, `observe`,
`memory`, `evaluator`, `gate` succeeded, while `events`, `adapters/claude_code`,
`reflexion_store`, `transport` left residual Japanese and failed verify. `reflect`
turned that failed trajectory into a grounded, admitted lesson ("file X still has
Japanese in a comment at line N -- translate every inline trailing comment, never
code/strings"), wired into episode 1's prompts. Episode 1 re-attempted only the
four failing files and recovered all of them (aggregate 1.00 -> `RubricThreshold`
fires -> `converged`).

The two-signal model behaved exactly as designed: `ground_truth_backed=True`
episodes drove convergence, the frozen evaluator's `reward` (0.0 then 1.0) was
consumed only by `reflect`, and `propose_evaluator=None` meant the epoch boundary
crossed at episode 2 without any evaluator change.

### 5.3 Comparison: no-Reflexion vs Reflexion

| | Run 1 (no Reflexion) | Run 2 (Reflexion) |
| --- | --- | --- |
| Outcome | 10/10 (`goal_met`) | 10/10 (`converged`) |
| Inner iterations | 13 | 14 (10 + 4) |
| Wall clock | ~1995 s | ~1906 s |
| Tokens charged | 11.17M | 10.72M |
| Retry mechanism | blind round-robin retry | lesson-guided episode |
| Files needing a 2nd attempt | 3 (`events`, `reflexion_store`, `transport`) | 4 (those + `adapters/claude_code`) |
| Suite after | 559 passed | 559 passed |

**Finding: for this task, Reflexion did not materially beat blind retry.** Both
converged to 10/10 at near-identical cost (within run-to-run noise). The reason is
diagnostic, and is the most useful result of the PoC:

- The first-attempt failures here are **stochastic**, not **systematic** -- `haiku`
  occasionally drops a single trailing comment or makes a partial edit on a long
  file, but it does not make the *same conceptual mistake* every time. A blind
  retry resamples and usually succeeds; a lesson ("you missed a comment") adds
  little, because the model already "knew" the rule and simply slipped.
- Reflexion's structural advantage -- *not repeating a systematic mistake* -- only
  pays off when verify failures are correlated across attempts (a recurring
  misunderstanding of the task, a consistently mishandled construct). This
  translation task does not exhibit that, so the outer loop's lesson channel ran
  correctly but had little to bite on.

So the honest read is: **the inner loop's mechanical retry + ground-truth verify
is already sufficient for self-translation; Reflexion is the right tool for tasks
with systematic failure modes, not stochastic slips.** Both runs nonetheless
exercised the full machinery end to end (gate + store + lease in the inner loop;
episodes + epoch boundary + episodic memory + grounded lesson admission in the
outer loop), which was the dogfood goal.

### 5.4 Behaviour-preservation guarantee (code unchanged)

Beyond the per-module pytest stage, we proved mechanically that **no code
changed**. For each of the ten files we parsed `HEAD` and the working tree, set
every string constant to `""` (so docstring translation is neutralised), and
compared `ast.dump`. All ten are **identical** -- i.e. the only differences are in
comment text and docstring/string *values*; no identifier, signature, control
flow, import, or decorator changed. Combined with `559 passed`, the translation
is provably behaviour-preserving. (The ten files happened to contain **no**
Japanese in non-docstring string literals, so the "don't touch string literals"
scope rule was vacuously satisfied; the verifier and prompt still enforce it, and
a unit test proves a Japanese string literal is not flagged as a target.)

## 6. Bugs / design gaps found (PoC scope: file Issues, do not fix here)

Per the brief, issues discovered during the PoC are reported here rather than
fixed in-scope.

### Finding A -- `ClaudeCodeAct` token accounting double-counts cache-read tokens

**Symptom:** a single `claude --print` translation of one ~170-line file is
charged ~340k "tokens"; Run 1 (13 iterations) was charged **11.17M tokens** and
Run 2 (14 iterations) **10.72M**. The brief's `TokenBudget(2_000_000)` would
therefore trip around iteration 3, long before any real token-cost concern.

**Root cause:** `parse_tokens` / `_sum_token_fields` sum *every* `*tokens*` field
in the result's top-level `usage`, including `cache_read_input_tokens`. With
tool-use (`Read` + `Edit`), Claude Code runs several internal turns and each turn
re-reads the cached context, so the cumulative `cache_read_input_tokens` reported
for a single `act` call is an order of magnitude larger than the real
input+output. Charging that sum to `TokenBudget` makes the cap fire far too
early.

**Repro:** `ClaudeCodeAct(allowed_tools=["Read","Edit"], output_format="json")`
on any non-trivial file; inspect `ActOutcome.tokens`.

**Suggested fix (for the Issue, not this PoC):** offer a token-cost policy on the
adapter -- e.g. count only `input_tokens + output_tokens + cache_creation_input_tokens`
(treat cache *reads* as ~free, which also matches their billing weight), or expose
a `token_fields` allowlist. Relevant hunk: `src/loop_agent/adapters/claude_code.py`
`_sum_token_fields` / `parse_tokens`.

**Workaround used here:** raised the run budget so the loop is governed by
`MaxIterations` instead, and recorded the inflated token figure as-is.

**Cross-references:** relates to the token/cost accounting touched by the
ModelLadder work (#53) and the self-improvement RFC (#54); the standalone Issue
for this adapter bug should reference both.

### Finding B -- `MaxIterations` cannot fairly bound a multi-file loop alone

**Symptom:** with a naive `gather` that always returns the first not-done file, a
single stubborn file consumes the whole `MaxIterations` budget and starves the
rest (20 attempts on file 0, files 1-9 never tried).

**Status:** *worked around in this PoC, not a loop-core bug.* We made `gather`
round-robin by fewest attempts so every file gets a first attempt before any
retry. Still worth noting that for multi-item work-lists, the loop's fairness
lives entirely in the caller's `gather`; a reusable "work-list driver" (fair
scheduling + per-item attempt caps) would be a natural addition above the core,
and pairs with the existing `discovery.triage` work-selection seam.

## 7. Reproduce

```bash
# Fast wiring check (no claude, deterministic):
python examples/self_translation_poc/harness.py --selfcheck
pytest tests/test_self_translation_poc.py

# Real run 1 (no Reflexion):
python examples/self_translation_poc/harness.py --real --model haiku

# Real run 2 (Reflexion); reset the ten files to their Japanese originals first:
#   for f in ...; do git show HEAD:src/loop_agent/$f.py > src/loop_agent/$f.py; done
python examples/self_translation_poc/harness.py --real --reflexion --model haiku
```
