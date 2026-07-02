# loop-agent

[![PyPI](https://img.shields.io/pypi/v/loop-agent.svg)](https://pypi.org/project/loop-agent/)
[![Python](https://img.shields.io/pypi/pyversions/loop-agent.svg)](https://pypi.org/project/loop-agent/)
[![CI](https://github.com/happy-ryo/loop-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/happy-ryo/loop-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

loop-agent is a small Python runtime for practicing Loop Engineering. Inside an agent or an existing application, it runs the following process:

1. Use `gather` to retrieve the next task to work on
2. Use `act` to execute that task
3. Use `verify` to validate the result
4. If successful, end the loop; if not yet complete, continue to the next iteration
5. Stop when a limit is reached, such as the maximum number of iterations, time, budget, or stagnation

What matters in Loop Engineering is not having a person instruct an agent one step at a time, but designing the loop: what to gather, how to execute it, how to verify it, and when to stop.
loop-agent is the engine for running that loop. It lets users focus on the events inside the loop: what to work on, how to work on it, how to verify that it is complete, and under what conditions to stop when things are not going well.

A defining feature is that loops can be expressed as Python functions or through the CLI. You can have coding agents such as Claude Code or Codex implement and run loops for you. The loops they write remain as Python code, so you can inspect the code and deepen your understanding.

## What It Is For

Use loop-agent when you want to safely repeat processes like the following. The result of each iteration is kept in history, and the final result is returned as something like "succeeded", "stopped at a limit", or "stopped pending approval".

- Process accumulated GitHub issues
- Have a coding agent keep fixing code until tests pass
- Process multiple files one by one, recording each completed item
- Run an external CLI or model call, then move to the next attempt if it fails
- Persist long-running work in state.db and resume it after interruption
- Require manual approval only for irreversible operations such as commit / push

## Installation

```bash
pip install loop-agent
```

If you want coding agents such as Claude Code / Codex / Cursor to write loops for you, also install the skill for loop-agent.

```bash
loop-agent install-skills
loop-agent install-skills --target-agent codex
loop-agent install-skills --target-agent cursor
```

## Minimal Example

```python
from loop_agent import ActOutcome, MaxIterations, VerifyOutcome, run_loop

n = {"value": 0}

def act(_ctx):
    n["value"] += 1
    return ActOutcome(observation=f"step {n['value']}")

def verify(_outcome):
    return VerifyOutcome(goal_met=n["value"] >= 3)

result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(5)],
)

print(result.status, result.reason)
```

When `verify` returns `goal_met=True`, the loop stops as a success. Even if it does not succeed, stop conditions such as `MaxIterations` ensure that the loop will stop.

## Loop Components

A loop-agent loop is mainly composed of five elements.

| Name | Role |
|---|---|
| `gather` | Selects the next target to execute |
| `act` | Performs the actual work |
| `verify` | Checks whether the work succeeded |
| `conditions` | Stops based on count, time, budget, stagnation, and similar limits |
| `gate` | Inserts manual approval only for operations that need it |

If you omit `gather`, the current state is passed directly to `act`. For a small loop, you can start with only `act`, `verify`, and `conditions`.

## Create a Loop Template

You can generate a loop template from the CLI.

```bash
loop-agent init-harness --template light  --output ./harness-light
loop-agent init-harness --template claude --output ./harness-claude
loop-agent init-harness --template codex  --output ./harness-codex
```

This generates a short `harness.py` and README. After generation, edit the prompt, verification command, stop conditions, and targets for manual approval to fit your use case. The Claude and Codex templates include resumable state, JSONL progress events, a review stub, and a repeated-failure cutoff so long runs can be observed and resumed.

Reflexion is intentionally not in the starter harness. Start by making the inner
loop observable and verifiable. Add Reflexion after the run shows a repeated,
lesson-shaped failure; adding it before the prompt, verifier, and review are clear
can turn noisy first attempts into bad stored lessons.

## Using It with Coding Agents

loop-agent is also designed for workflows where coding agents such as Claude Code, Codex, or Cursor write loops from prompts like this:

```text
Using loop-agent, write a loop that fixes failing pytest tests.
For act, delegate the fixes to a coding agent, and for verify, judge success by the pytest exit code.
Stop after at most 5 attempts, and keep commit and push outside the loop.
```

Installing the skill makes it easier for coding agents to find loop-agent APIs and design patterns.

## Main Features

- Synchronous / asynchronous loop execution: `run_loop`, `async_run_loop`
- Stop conditions: maximum iterations, time, tokens, stagnation detection, and more
- Verification helpers: `CommandVerifier`, `PytestVerifier`, `RegexVerifier`
- State recording and resume: progress file / state.db
- Manual approval: pause / resume only for irreversible operations
- Adapters: `ClaudeCodeAct`, `CodexAct`
- Processing multiple targets: `WorkListGather`
- Observation and operations: summary, dashboard, spike scan
- Outer improvement loop: Reflexion for repeated, lesson-shaped failures

## Fit Criteria

loop-agent is well suited to work whose completion can be judged mechanically, such as by tests or command results.

Good examples:

- `pytest` passes
- Only specific files have changed
- A command exits with code 0
- String or AST conditions are satisfied
- All N tasks become done

Poor fits:

- "Make the writing better"
- "Generally improve the quality"
- Work where success judgment depends on human intuition every time

You can still use loop-agent for ambiguous goals, but in that case, designing `verify` becomes the central concern.

## Documentation

| Document | Contents |
|---|---|
| [docs/quickstart.md](./docs/quickstart.md) | Run your first loop |
| [docs/first-harness-api.md](./docs/first-harness-api.md) | APIs to use first |
| [docs/seams.md](./docs/seams.md) | Details of `gather` / `act` / `verify` and related components |
| [docs/verifiers.md](./docs/verifiers.md) | Verification helpers |
| [docs/recipes/](./docs/recipes/README.md) | Concrete loop examples |
| [docs/adapters/README.md](./docs/adapters/README.md) | Claude Code / Codex adapters |
| [docs/persistence-and-resume.md](./docs/persistence-and-resume.md) | State persistence and resume |
| [docs/safety.md](./docs/safety.md) | Stop conditions and manual approval |
| [docs/cli.md](./docs/cli.md) | CLI |
| [docs/stability.md](./docs/stability.md) | Compatibility contract |
| [docs/api-reference.md](./docs/api-reference.md) | API reference |

## Status

**1.0.0 Stable**. The canonical compatibility contract is [docs/stability.md](./docs/stability.md).

This README is kept short as an entry point, with detailed specifications split into docs.

## License / Development

The license is [MIT](./LICENSE).

Issues / PRs are handled in English. The default branch is `main`.
