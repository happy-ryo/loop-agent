# act Adapters - Claude Code / Codex / Custom

loop-agent ships with a **first-class act adapter ecosystem**. Three adapter families can all be plugged compatibly into the `act` seam (callable -> `ActOutcome`): `ClaudeCodeAct` (headless `claude --print`), `CodexAct` (headless `codex exec`), and any **custom adapter** that conforms to the `ActHook` / `ActResult` Protocols. The ecosystem is open: third and later adapters, such as `GeminiAct`, can become `run_loop` executors as-is when they follow the same contract.

In other words, `act` is not an entry point fixed to one host. It is an extension point that accepts any callable satisfying the `ActHook` contract. This document covers model preflight before starting a loop, the two bundled adapters (Claude Code / Codex), a composition pattern that mixes them (`ModelLadder`), and the common API. See [writing-an-adapter.md](./writing-an-adapter.md) for how to write a new adapter and how to monitor schema drift in external CLIs.

## Model preflight - Check candidate availability before starting a loop

`loop_agent.adapters` provides a small preflight surface for listing and smoke-testing candidate Codex / Claude Code models before `run_loop`. This is **visibility**, not policy. The loop core does not choose a model; the caller inspects the results and decides how to assemble `CodexAct`, `ClaudeCodeAct`, or `ModelLadder`.

```python
from loop_agent.adapters import preflight_codex_models, preflight_claude_code_models

codex = preflight_codex_models(smoke=True, timeout=60)
claude = preflight_claude_code_models(smoke=True, include_full_names=True, timeout=60)

for report in (codex, claude):
    for item in report.results:
        print(item.provider, item.model, item.status, item.tokens, item.error)
```

`status` is normalized across providers:

| status | Meaning |
|---|---|
| `available` | The smoke run completed without adapter failure. |
| `unavailable` | The CLI started, but that candidate model was rejected or failed. |
| `unknown` | Model-specific availability could not be determined because the CLI is not installed, could not start, timed out, or hit a similar condition. |
| `skipped` | `smoke=False`. Candidates were listed only, not executed. |

The default Codex candidates are `gpt-5.5` / `gpt-5.4-mini` / `gpt-5.4` / `gpt-5.3-codex-spark`. The Codex manual describes `gpt-5.5` as the starting point for normal tasks, `gpt-5.4-mini` for lightweight tasks, and `gpt-5.3-codex-spark` as a ChatGPT Pro research preview. `gpt-5.4` is included as a candidate observed to work locally, but the smoke result is the source of truth for availability for any given user.

The default Claude Code candidates are the aliases `sonnet` / `opus` / `haiku` / `fable`. With `include_full_names=True`, `claude-sonnet-5` / `claude-opus-4-8` / `claude-haiku-4-5` / `claude-fable-5` are also added as candidates. Claude Code may restrict selectable models through workspace / enterprise `availableModels` / `enforceAvailableModels` settings, so do not infer availability from the candidate list alone.

Candidates can be added or overridden:

```python
from loop_agent.adapters import preflight_codex_models

report = preflight_codex_models(
    models=["gpt-5.5", "my-provider/model"],
    smoke=True,
    timeout=30,
)
```

Recording preflight results to a dashboard, logs, or an issue comment before the loop makes it easier to detect shallow `tokens=0` runs or stale model settings during dogfooding verification. Automatic escalation is a separate responsibility; if needed, the caller builds a `ModelLadder` after preflight.

## ModelLadder - Canonical heterogeneous adapter composition example (escalate to stronger models on hard tasks)

Because `act` is a `Callable`, you can write an act that looks at attempt counts and raises the model tier. This common pattern is packaged as the canonical example `loop_agent.adapters.ModelLadder` (**not a new feature**, but a reference implementation of `act` composition). It already handles the pitfalls: stateful attempt counts, the fact that act cannot see verify's goal decision, and heterogeneous composition. Issue #53:

```python
from loop_agent.adapters import ModelLadder, ClaudeCodeAct

act = ModelLadder([
    ClaudeCodeAct(model="haiku"),
    ClaudeCodeAct(model="sonnet"),
    ClaudeCodeAct(model="opus"),
], escalate_on="failure")        # Escalate to the next tier when the previous tier returns failed=True.

result = run_loop(act=act, ...)
```

`escalate_on` accepts `"failure"` (escalate after previous-tier failure), a positive int `N` (escalate after trying the same tier N times; a complementary strategy for cases where act reports success but verify keeps iterating because the goal is not met), or any predicate `Callable[[EscalationContext], bool]` (for composition, for example `lambda ec: ec.last_failed and ec.attempts >= 2`). Heterogeneous chains work directly, so you can start cost-optimally and hand only hard spots to another provider:

```python
from loop_agent.adapters import ModelLadder, ClaudeCodeAct, CodexAct

act = ModelLadder([ClaudeCodeAct(model="haiku"), CodexAct(model="gpt-5.5"), ClaudeCodeAct(model="opus")])
```

Each tier can be any `act` hook that returns an `ActOutcome`. As long as the result conforms to the common `ActResult` contract (`observation.failed`), heterogeneous adapters can use the same decision logic. The `ActResult` Protocol from #52 guarantees composability. The implementation is in `src/loop_agent/adapters/model_ladder.py`; tests are in `tests/test_adapters_model_ladder.py`.

## Run loops through Claude Code (headless adapter)

`loop_agent.adapters.ClaudeCodeAct` is an `act` hook that starts **one headless `claude --print` subprocess per iteration**. This lets one line of `run_loop` "use Claude Code as the loop executor" (the act seam from report.md S4.4 / Issue #32).

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

act = ClaudeCodeAct(
    allowed_tools=["Read", "Edit"],   # --allowed-tools
    timeout=600,                       # Timeout returns graceful failed=True instead of raising.
    model="opus",                      # Optional. --model aliases are accepted.
    permission_mode="acceptEdits",     # Optional.
    # env=None inherits os.environ, so the existing claude session + ANTHROPIC_API_KEY are used.
)

def verify(outcome):
    # The response is structured in ActOutcome.observation (ClaudeCodeResult).
    # Check .failed / .text / .tokens / .returncode / .error to decide.
    res = outcome.observation
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act,
    verify=verify,
    gather=lambda state: {"prompt": f"Write one next fix (attempt {state.iteration})"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

Design guarantees that preserve loop-core behavior:

- **Do not kill the loop with exceptions**: timeouts, nonzero exits, missing executables, and permission failures do not raise exceptions. They return gracefully as an `ActOutcome` carrying a `ClaudeCodeResult` with `failed=True`. Boundary conditions such as `Timeout` and `MaxIterations` always remain effective.
- **Accumulate tokens into the budget**: `usage` from `--output-format json` (default) is parsed, with fallback parsing from stdout/stderr when needed, and stored in `ActOutcome.tokens`. The driver adds this to `state.tokens_used`, so `TokenBudget` works directly.
- **Delegate auth to the claude CLI**: by default, the adapter inherits `os.environ`, using the existing claude CLI session (`~/.claude` login) first and `ANTHROPIC_API_KEY` as the CLI-side fallback. `env=` can override/merge environment values.
- **Prompt rendering**: `prompt_template` (default `"{prompt}"`) is filled with `str.format` using the `gather` return value (Mapping / `LoopState` / string). `LoopState` fields can also be embedded, as in `"... iter={iteration}"`.

Use `MockClaudeCodeAct(responses=[...])` for tests and demos that should not use subprocesses. Each `responses` item may be a `str`, `dict`, or `ClaudeCodeResult`; `{"text": ..., "tokens": ..., "failed": ...}` can reproduce `TokenBudget` and failure cases in memory. The implementation is in `src/loop_agent/adapters/claude_code.py`; tests are in `tests/test_adapters_claude_code.py`.

```python
from loop_agent.adapters import MockClaudeCodeAct
act = MockClaudeCodeAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

Out of scope: TUI mode / deep stream-json integration / Plan mode integration.

## Run loops through Codex (headless adapter)

`loop_agent.adapters.CodexAct` is an `act` hook that is **fully isomorphic** to `ClaudeCodeAct` and starts **one headless `codex exec` subprocess per iteration**. The only differences are the subprocess command, flags, and token/output parsing. This lets the same `run_loop` switch between Claude and Codex with one line (the act seam from report.md S4.4 / Issue #49).

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import CodexAct

act = CodexAct(
    model="gpt-5.5",        # -m (explicitly use the gpt-5.5 family for ChatGPT account operation)
    effort="medium",        # -c model_reasoning_effort=<effort>
    timeout=600,            # Timeout returns graceful failed=True instead of raising.
    # sandbox="workspace-write",  # Optional (-s). None uses the codex default.
    # env=None inherits os.environ, so the existing codex session + OPENAI_API_KEY are used.
)

def verify(outcome):
    # The response is structured in ActOutcome.observation (CodexResult).
    # Check .failed / .text / .tokens / .returncode / .error to decide.
    res = outcome.observation
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act,
    verify=verify,
    gather=lambda state: {"prompt": f"Write one next fix (attempt {state.iteration})"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

The design guarantees are the same as `ClaudeCodeAct`: do not kill the loop with exceptions, accumulate tokens into the budget, delegate auth to the CLI, and fill `prompt_template`. Codex-specific differences are the following three points:

- **Token type semantics**: In Codex/OpenAI `usage`, `cached_input_tokens` is a subset of `input_tokens`, and `reasoning_output_tokens` is a subset of `output_tokens`. Therefore total work is counted only as `input_tokens + output_tokens`, avoiding double counting (`turn.completed` from `--json` is parsed, with a regex fallback when absent).
- **Response body**: Because the response is not a single field but a sequence of `--json` JSONL events, the body is taken from the `text` of the last `agent_message` (`item.completed`).
- **Fixed stdin**: When codex receives a pipe on stdin, it tries to read additional input, so child stdin is fixed to `DEVNULL` (the prompt is already fixed as a positional argument after `--`). `--skip-git-repo-check` is on by default so startup does not fail outside a git repository.

Use `MockCodexAct(responses=[...])` for tests and demos that should not use subprocesses. It follows the same contract as the `ClaudeCodeAct` version: each item may be a `str`, `dict`, or `CodexResult`. The implementation is in `src/loop_agent/adapters/codex.py`; tests are in `tests/test_adapters_codex.py`.

```python
from loop_agent.adapters import MockCodexAct
act = MockCodexAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

Out of scope: TUI mode (Issue #34) / deep stream-json integration.

## Adapter API overview

| Item | `ClaudeCodeAct` | `CodexAct` |
| --- | --- | --- |
| Launch command | `claude --print [--output-format json] -- <prompt>` | `codex exec [--json] [--skip-git-repo-check] -m <model> -c model_reasoning_effort=<effort> -- <prompt>` |
| Main arguments | `allowed_tools` / `model` / `permission_mode` / `output_format` / `extra_args` | `model="gpt-5.5"` / `effort="medium"` / `sandbox` / `json_output` / `skip_git_repo_check` / `allowed_args` |
| Common arguments | `timeout` / `prompt_template` / `env` / `cwd` / `runner` | Same as left |
| Observation object | `ClaudeCodeResult(.text/.failed/.tokens/.returncode/.error)` | `CodexResult(.text/.failed/.tokens/.returncode/.error)` |
| Token aggregation | `input_tokens + output_tokens + cache_creation_input_tokens` (`cache_read_input_tokens` is excluded because it is cheaper and grows cumulatively. Issue #55) | `input_tokens + output_tokens` (cached/reasoning tokens are excluded because they are subsets) |
| auth | Inherits os.environ (claude session + `ANTHROPIC_API_KEY`) | Inherits os.environ (codex session + `OPENAI_API_KEY`) |
| On failure | Graceful, with `failed=True` on the observation (no exception) | Same as left |
| Mock | `MockClaudeCodeAct(responses=[...])` | `MockCodexAct(responses=[...])` |

Both adapters consolidate the result shape (8 fields) and prompt formatting into the common foundation `loop_agent.adapters.base` (`ActResult` contract / `ActResultBase` / `render_prompt` / `Runner`). Only the subprocess command, flags, and token/output parsing differ. For **guidance on writing third and later adapters, such as `GeminiAct`, under the same contract**, see [writing-an-adapter.md](./writing-an-adapter.md): the four-part contract, `ActResult` shape, avoiding token double-counting, hard-won lessons, registration with the common test harness, and an additional checklist.

## Related

- [../../README.md](../../README.md) - loop-agent entry point (positioning / seams / flow summary)
- [writing-an-adapter.md](./writing-an-adapter.md) - guidance for writing third and later adapters, such as `GeminiAct`, under the `ActHook` / `ActResult` contract, plus operations for monitoring external CLI compatibility
- [../seams.md](../seams.md) - detailed specifications and types for the five seams, including the `act` seam
- [../api-reference.md](../api-reference.md) - overview table for all APIs and the scope of the loop core
