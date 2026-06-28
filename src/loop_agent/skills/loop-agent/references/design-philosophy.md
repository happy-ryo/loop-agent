# loop-agent design philosophy - Embeddable / 5 seams / coding-agent driven

> This is the conceptual anchor within the bundle; the canonical source for seam types and contracts is `seams.md`. This file is a short introduction that synthesizes the positioning from the README with the core of seams.md. When you need to dig into a design decision, read each reference on-demand.

Before you map loop-agent onto your domain, use this single page to grasp "what this library does and does not provide." The core is only the orchestration of `gather -> act -> verify -> repeat` plus safety guards; the policy (what to choose, how to run it, what counts as success) is entirely something you inject through the 5 seams.

## Embeddable Loop Engine

What it provides is only the orchestration body of `gather -> act -> verify -> repeat` and runaway prevention. **What to choose, how to run it, what counts as success - all of that policy lives on the caller's side.** So loop-agent, knowing nothing about your domain, lives small inside an existing app and functions as an engine that "just turns the loop safely." This is the real meaning of "Embeddable," and the motto is **"Bring your own `gather` / `act` / `verify`. We provide the loop."** (you hold the policy, we turn the loop).

Its stance can be distinguished by "the side that absorbs vs. the side that is embedded." Whereas LangGraph / AutoGen / OpenAI Agents SDK are frameworks that **absorb** your app into their own framework, loop-agent is a loop engine that is **embedded** inside an existing app. It does not replace your architecture; it just adds one `while not goal: gather -> act -> verify` inside it. The host can be your own Python script / an existing CLI / a web app / an MCP server / a cron daemon / a Slack bot / another AI framework - it can be retrofitted inside any of them.

Dependencies are minimal. The loop core runs on the Python stdlib alone. OTel (observability) / SQLite (state SoT) / `tomli` (TOML reading) and the like are all optional, degrading to no-op even when not installed.

## Positioning of Loop Engineering

Loop Engineering is the practice of stopping prompting an agent one move at a time by a human, and instead **designing the very "system (= the loop)" that prompts, verifies, memorizes, and re-runs the agent**. It sits at the top (the control layer) of the 3-layer stack `prompt engineering -> context engineering -> loop engineering`. loop-agent carves out this control layer as a minimal core, and seam design is precisely the substance of Loop Engineering.

## The 5 seams (policy injection points)

What the loop "owns" is only the orchestration body. All policy is injected through these 5 seams.

| Seam | Type | What you decide (= the policy you inject) |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | What to do next (candidate selection / triage / queue strategy / fair scheduling) |
| `act` | `Callable[[ctx], ActOutcome]` | How to run it (`ClaudeCodeAct` / `CodexAct` / your own adapter, model selection, subprocess vs. local fn) |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | What counts as "success" (pytest / AST / regex - **judged by ground truth**) |
| `conditions` | `list[StopCondition]` (OR-composed with `AnyOf`) | When to stop (count / budget / time / goal / progress stall) |
| `gate` | `ActionGate` (`HumanGate` etc., target selected with `on=`) | What requires human approval (commit / push / any irreversible operation) |

As pseudocode, what loop-agent owns is just these 4 lines.

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # what       (gather)
    outcome = act(ctx)         # how to run (act)
    v = verify(outcome)        # what is success (verify)
    state.update(v)
```

Write these 5 seams and that becomes the loop for your domain. The canonical source for types and contracts is `seams.md`.

## The 3 iron rules (do not drop them at design time)

- **Write verify with ground truth (machine judgment).** The essence of a seam is that anything can be plugged in, but if you delegate the success judgment to an LLM-as-judge, the loop tends to converge on "pretending to succeed." Use something judgeable mechanically: pytest exit-code / AST comparison / string scan.
- **Always place a mechanical upper bound.** OR-compose `MaxIterations` / `TokenBudget` / `Timeout` with `AnyOf` and load at least one. Without it you get a `ConfigError`, and the runaway prevention that guarantees it always stops at the bound even when the goal is unmet collapses.
- **Place irreversible operations in a gate or outside the loop.** `HumanGate` only reviews the discrete actions that `gather` returns; it cannot see a `git commit` inside an `act` subprocess. So commit / push / deploy must either "be made a discrete loop action and picked up with `on`" or "be isolated into a human step outside the loop."

In addition, beyond the mechanical upper bound, the stop conditions can carry a **semantic stop**. Lining up `GoalMet` (success-stop when a verifiable goal is satisfied) and `NoProgress` (cutoff-stop when the same action repeats and no progress emerges) in the same `AnyOf` as the mechanical bound closes things off safely with dual termination conditions. Success or failure is judged with `result.succeeded` regardless of the channel.

## act is freely swappable

The `act` seam already comes with `ClaudeCodeAct` / `CodexAct` / your own adapter (the `ActHook` Protocol) as first-class adapters. Multiple LLM providers are available from the start, and any callable conforming to `ActHook` rides the same `act` seam. As long as it is a callable that returns an `ActOutcome`, it can be a subprocess (`claude --print` / `codex exec` etc.) or an in-process function - that is the caller's freedom. When you push act out to an external CLI, the adapter's 4-rule contract (do not kill the loop with an exception / accrue tokens to the budget / delegate auth to the CLI / close stdin) comes into play.

## coding-agent driven (flow E)

The primary user of loop-agent is not a human but a coding agent. The flow looks like this.

```
prose intent (a human's natural language)
  -> coding agent writes gather/act/verify/conditions/gate
  -> run_loop launch
  -> observe the result and rewrite the policy
  -> loop core (thin, immutable)
```

Because it can be driven by natural-language intent, it reaches users who do not write code. **This skill itself is the official support for that flow**, and it is a reference bundle for you (the coding agent) to synthesize the user's domain into the 5 seams. It is not for copying recipes verbatim - read the examples as inspiration, borrow the principles, and rewrite the code into the user's domain.

## Which reference to read when

Once you grasp the core, design from the seams your domain needs, and read the following on-demand when you dig deeper.

- Seam types / contracts / dual termination conditions / the ground-truth iron rule -> `seams.md`
- Making act an external CLI subprocess / your own adapter / the 4 rules / the token double-counting trap -> `writing-an-adapter.md`
- The reach of the gate / isolating irreversible operations / `allowed_tools` discipline / runaway prevention -> `safety.md`
- Interrupt -> resume / state.db SoT -> `persistence-and-resume.md`
- Whether you should add Reflexion (systematic vs. stochastic) -> `reflexion-when-to-use.md`
- Async seams / `async_run_loop` / the sync-async boundary -> `async.md`
- Fair scheduling for multi-item / wake delivery / work-discovery -> `transport.md`
- Exception handling (`LoopError` / `ConfigError` / `StateError`) -> `errors.md`
- Idea examples (do not transcribe) -> `examples/translation.md`, `examples/flaky-test.md`, `examples/refactor.md`

References within the bundle can be referenced by bare filename. For things not included in the bundle, such as the quickstart, follow the canonical source on GitHub (e.g., <https://github.com/happy-ryo/loop-agent/blob/main/docs/quickstart.md>). The standard approach is to first nail down the seam contracts in `seams.md`, then proceed to `writing-an-adapter.md` if you are turning act into a subprocess, or to `safety.md` if you are handling irreversible operations.
