---
name: loop-agent
description: Use this when you need to design and implement a gather-act-verify loop (loop-agent) for the user's domain. Trigger when the user says things like "I want to write a loop", "automate a repeated task", "run the same processing over N items", "drive a loop with a coding agent", or "design a gather-act-verify loop". A load-on-demand reference bundle for synthesizing the 5 seams (gather/act/verify/conditions/gate) from the user's intent.
---

# loop-agent -- design and implement a gather-act-verify loop for the user's domain

loop-agent is an embeddable loop engine whose motto is "you own the policy, we run the loop." This skill is a reference bundle for a coding agent (you) to design and implement the 5 seams (gather / act / verify / conditions / gate) for the user's domain. This SKILL.md holds the trigger and the thinking procedure; the files under `references/` hold the details you read on demand. **It is not a recipe to copy verbatim** -- synthesize the user's intent into the 5 seams and write the code to fit the user's domain.

## Trigger (restated)

When the user says any of the following, reason through this skill's procedure.

- "I want to write a loop"
- "automate a repeated task"
- "run the same processing over N items"
- "drive a loop with a coding agent"
- "design a gather-act-verify loop"

Where to draw the line: a "one-off, single-pass operation" or a "task that needs no loop structure" is out of scope -- apply this only to tasks that have iteration, convergence, and stopping conditions.

## How the AI should think (procedure)

Proceed through the following 5 steps. For each step, the reference to read is called out explicitly. Don't read everything at once -- read only the references relevant to the issue you hit, on demand.

1. **Grasp the core first** -- read `references/design-philosophy.md` and internalize the 5 seams (gather / act / verify / conditions / gate) and the embeddable core (policy is injected; only the loop body itself is the library). **This one file is the only thing to read first.**
2. **Design the seams your user's domain needs** -- map the user's request (database / DevOps / scientific computing / document processing / anything) onto the 5 seams. Follow the "5-seam design checklist" below for each seam's design questions. If you need to dig into seam types, contracts, the dual termination condition, and the ground-truth iron rule, see `references/seams.md`.
3. **Read only the references you need, on demand** -- don't read them all. Pick from the table below according to the issue your seam design hits.
4. **Read `examples/` as inspiration (no literal copying)** -- `references/examples/{translation,flaky-test,refactor}.md` are illustrations of "intent -> seam design." Even if the user's domain matches, **do not copy them verbatim**. Borrow the **design principles** -- the sharpness of verify, fair scheduling, commit isolation -- and rewrite the code for the user's domain.
5. **Present design decisions to the user before implementing** -- briefly present to the user how you filled in the 5 seams (especially the ground-truth basis for verify, the stopping conditions, and the gate targets), get agreement, and then write the harness.

### Step -> reference table

| Situation | Reference to read |
|---|---|
| The initial core (always first) | `references/design-philosophy.md` |
| Seam types, contracts, the dual termination condition, the ground-truth iron rule | `references/seams.md` |
| Making act an external CLI subprocess / a custom adapter / the 4 clauses / token double-counting | `references/writing-an-adapter.md` |
| Gate scope / isolating irreversible operations / `allowed_tools` discipline / runaway prevention | `references/safety.md` |
| Interrupt -> resume / state.db SoT / the resume contract | `references/persistence-and-resume.md` |
| Whether to add Reflexion (systematic vs stochastic) | `references/reflexion-when-to-use.md` |
| Async seams / `async_run_loop` / the sync-async boundary | `references/async.md` |
| Multi-item fair scheduling / wake delivery / work-discovery | `references/transport.md` |
| Exception handling (`LoopError` / `ConfigError` / `StateError`) | `references/errors.md` |
| Illustrations (don't copy by rote) | `references/examples/translation.md`, `references/examples/flaky-test.md`, `references/examples/refactor.md` |

## 5-seam design checklist

For each seam, map the user's domain using the questions below. The types have been checked against the top-level public symbols of `from loop_agent import ...`.

- **gather (what to do next)** -- how do you enumerate candidates? What is the triage / queue strategy? For multi-item work, do you need fair scheduling (start from the item with the fewest attempts) (-> `references/transport.md`)? Type is `Callable[[state], ctx]`. Omitting it runs the loop over a single context.
- **act (how to execute)** -- do you launch an external agent CLI as a subprocess (`from loop_agent.adapters import ClaudeCodeAct, CodexAct`; a custom adapter is the `ActHook` Protocol), or an in-process callable? What is the model selection? If you escalate on hard tasks, use `from loop_agent.adapters import ModelLadder`. Type is `Callable[[ctx], ActOutcome]`, returning `ActOutcome(observation=..., tokens=...)`.
- **verify (what counts as success)** -- is it machine-verifiable? **Can you write it sharply against ground truth** (pytest exit-code / AST / regex)? Don't defer to an LLM-as-judge (it converges on "pretending to succeed"). If it's flaky, measure reproducibility, e.g. "N passes in a row." Type is `Callable[[ActOutcome], VerifyOutcome]`, returning `VerifyOutcome(goal_met=..., detail=...)`.
- **conditions (when to stop)** -- **always place at least one** mechanical bound (`MaxIterations` / `TokenBudget` / `Timeout`, OR-composed with `AnyOf`) (without one you get a `ConfigError`). Do you also add a semantic stop (`GoalMet` / `NoProgress`)? Pass these to `run_loop(..., conditions=[...])`.
- **gate (what requires human approval)** -- are there irreversible operations (commit / push / deploy)? **`HumanGate` only reviews the discrete actions returned by `gather`; a `git commit` inside the `act` subprocess is invisible to it.** So do one of two things: "act only edits, and irreversible steps are a human step outside the loop," or "make commit a discrete action of the loop and pick it up via `on=`" (-> `references/safety.md`).

## The 4-clause contract and adapter pitfalls (surface this when making act a subprocess)

When you write a custom adapter / make `act` an external CLI, you must follow the 4 clauses in `references/writing-an-adapter.md`.

1. **Don't kill the loop with exceptions** -- timeout / non-zero exit / missing executable should return gracefully as an `ActOutcome` with `failed=True`. In principle, zero exceptions should leak. The one intentional exception is the `KeyError` from `render_prompt`, which is eager by design.
2. **Account tokens into the budget** -- when you can't get them, use 0; count them regardless of success or failure.
3. **Delegate auth to the CLI** -- inherit `os.environ` and merge overrides via `env=`. The adapter must not read keys itself.
4. **Close stdin** -- `stdin=subprocess.DEVNULL`, and pass the prompt as a positional argument after `--`.

**The token double-counting trap (most important)**: check per-CLI whether usage is "an additive bucket or a subset." Claude Code **excludes** `cache_read_input_tokens` (counting only `input+output+cache_creation`); Codex's `cached_input_tokens` / `reasoning_output_tokens` are subsets, so count only `input+output`. Adding all fields makes `TokenBudget` misfire (Issue #55).

## Hard-won lessons (surface these so you don't rediscover them every time)

The details go to the references. Don't miss these at design time.

- **token accounting** -- the `cache_read` double-counting above (-> `references/writing-an-adapter.md`).
- **hard-kill of sync seams depends on POSIX SIGALRM** -- per-call timeout/kill for act/verify is `TimeoutPolicy` (graceful + kill). **The actual interruption of a sync seam depends on POSIX main-thread `SIGALRM`, and on Windows / non-main threads it degrades to graceful or raises `UnsupportedTimeoutKill`.** If you need a reliable kill, use async seams + `await async_run_loop(...)` (-> `references/async.md` / `references/errors.md`).
- **stdin hang** -- `codex exec` reads additional input and hangs if stdin is a pipe. `stdin=DEVNULL` is mandatory (-> `references/writing-an-adapter.md`).
- **the async-sync boundary** -- passing an awaitable seam (act/verify/gather/condition.check/gate.review) to the synchronous `run_loop` raises `AsyncSeamInSyncLoop`. Async seams require `await async_run_loop(...)` (-> `references/async.md` / `references/errors.md`).
- **narrowing `allowed_tools`** -- for self-improvement work, narrow act to editing tools (`Read` / `Edit`) and isolate commit / push outside the loop. `HumanGate` can't see operations inside the subprocess (-> `references/safety.md`).

## examples are inspiration, not literal

`references/examples/` is a catalog of ideas mapping "prose intent -> seam-design sketch." Transplant the **principles** -- verify's ground truth, gather's scheduling, gate's isolation -- to fit the user's domain, and rewrite the code. Don't use them as copy-paste templates.

## Prohibitions

- Don't apply recipes by rote (copying).
- Don't reuse a cookbook that diverges from the user's request (e.g., applying it to an unrelated task just "because there's a translation recipe").
- Don't make verify an LLM-as-judge (ground truth comes first).
- Don't build a configuration with no stopping conditions at all (it becomes a `ConfigError`, and runaway prevention breaks down).
