> This file is a load-on-demand bundled copy of `docs/adapters/writing-an-adapter.md`. The canonical source is `docs/adapters/writing-an-adapter.md` in the repository.

# Writing a New `act` Adapter

The loop-agent `act` seam wraps the executor for one iteration in a single
function (`Callable[[context], ActOutcome]`). An **adapter** is the component that
starts an external agent CLI (Claude Code, Codex, or any future tool) headlessly
and connects it to `act`. `loop_agent.adapters` includes
[`ClaudeCodeAct`](https://github.com/happy-ryo/loop-agent/blob/main/src/loop_agent/adapters/claude_code.py) and
[`CodexAct`](https://github.com/happy-ryo/loop-agent/blob/main/src/loop_agent/adapters/codex.py) as reference
implementations. They differ **only in subprocess command flags and token/output
parsing**; the result shape and prompt formatting are completely isomorphic.

This document is the canonical guide for writing a third adapter and any later
adapters, such as `GeminiAct`, `AiderAct`, or internal tools, **correctly under
the same contract**. The pitfalls that
[`ClaudeCodeAct` / `CodexAct` already ran into](#hard-won-lessons-from-real-runs)
are collected in one place. Reading that section before you start is the fastest
way to orient yourself.

> Adapters are not a new loop-agent feature. They standardize a pattern that
> users can already write through the `act` seam today, including its pitfalls.
> A new adapter can be plugged in without changing the core (`run_loop`) at all,
> as long as it is a function that returns `ActOutcome`.

> **There are two kinds of adapters.** This document covers **executor adapters**
> that start an external CLI as a subprocess (`ClaudeCodeAct` / `CodexAct`), and
> the four rules, token parsing, and mock guidance below are for that kind. The
> other kind is an **adapter that composes other `act` hooks**. The canonical
> example is
> [`ModelLadder`](https://github.com/happy-ryo/loop-agent/blob/main/src/loop_agent/adapters/model_ladder.py)
> (automatic escalation to a stronger model for difficult tasks, Issue #53).
> Composition adapters do not start subprocesses, so they do not have
> `build_command`, `runner`, or `parse_tokens`, and they **are not listed** in the
> common contract harness (`ADAPTER_SPECS`). The token, `failed`, and graceful
> termination guarantees are satisfied by each composed executor adapter, while
> the composition layer passes through the step's `ActOutcome`. `ModelLadder`
> packages a pattern that users can also write because `act` is `Callable`, and
> it avoids pitfalls around stateful attempt counts, the constraint that `act`
> cannot see the `verify` goal decision, and heterogeneous composition
> (`ClaudeCodeAct` + `CodexAct`). Use `ModelLadder` as the template when writing a
> new composition adapter.

---

## The `act` Seam Contract: Four Rules

Adapters must always follow the four rules below so they do not break core loop
properties: always stopping at boundaries, respecting budgets, and delegating
authentication externally. Both `ClaudeCodeAct` and `CodexAct` satisfy these
rules.

1. **Do not kill the loop with exceptions.** Cases where execution did not happen
   or failed, such as a timeout, non-zero exit, or missing executable, should
   return gracefully with `failed=True` in `ActOutcome.observation` instead of raising
   an exception. This lets `verify` inspect `outcome.observation.failed` and
   decide whether to continue or stop, while boundary checks such as `Timeout`
   and `MaxIterations` remain effective. As a rule, **no exceptions should leak
   out**. Catch `subprocess.TimeoutExpired` and `OSError` and convert them to
   `failed`. The only exception is `render_prompt`: when `prompt_template`
   references a field that is not present in the context, it **eagerly raises
   `KeyError`**. This is intentional: a pre-execution configuration error should
   fail immediately rather than be swallowed. The skeleton below also calls
   `render_prompt` outside the `try` block. This `KeyError` is the one deliberate
   built-in exception outside the `LoopError` hierarchy, following the semantics
   of `str.format`. See [../errors.md](errors.md) for the full library exception
   hierarchy.

2. **Add tokens to the budget.** Extract the total number of processing tokens
   from the response and put it in `ActOutcome.tokens`. The driver adds this to
   `state.tokens_used`, so `TokenBudget` works as-is. **Use 0 when the count is
   unavailable**; text output without usage data is normal, and 0 is the safe
   fallback. Count tokens **regardless of success or failure**, because failed
   attempts can still consume tokens.

3. **Delegate authentication to the CLI.** By default, the child process inherits the
   launcher's `os.environ`, so the external CLI can primarily use its existing
   session, such as login state under `~/.claude` or `~/.codex`. If API keys such
   as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` are present in the environment, they
   can act as a CLI-side fallback. The adapter itself must not read or paste
   keys. If callers need to inject secret values, provide only an `env=` path that
   performs an **override merge**.

4. **Close stdin to prevent hangs.** In headless loops, the parent stdin may be a
   pipe or closed endpoint. If a child CLI tries to read "additional input", it
   can **hang**. Always finalize the prompt as a positional argument after `--`,
   and pass `stdin=subprocess.DEVNULL` to CLIs that read interactive input. See
   [the Codex failure mode](#1-codex-reads-additional-input-and-hangs-when-stdin-is-a-pipe).

---

## Result Shape: The `ActResult` Contract

The result object placed in `ActOutcome.observation` follows the common
structural contract
[`ActResult`](https://github.com/happy-ryo/loop-agent/blob/main/src/loop_agent/adapters/base.py)
(`Protocol`). It has eight fields and `__str__`, which returns the response body.

| Field | Type | Meaning |
|---|---|---|
| `text` | `str` | The assistant response body. `str(result)` returns the same body. |
| `tokens` | `int` | Total tokens consumed by this call, used for budget accounting. |
| `failed` | `bool` | Whether the call failed: non-zero exit, CLI-reported error, timeout, or launch failure. |
| `returncode` | `Optional[int]` | Child process exit code; `None` for launch failures and timeouts. |
| `error` | `str` | Concise error body on failure; empty string on success. |
| `stdout` / `stderr` | `str` | Raw child process output for debugging and reparsing. |
| `command` | `tuple[str, ...]` | The command that was actually executed, as an argument sequence. |

The shortest path for your adapter's Result is to inherit from the common
concrete dataclass
[`ActResultBase`](https://github.com/happy-ryo/loop-agent/blob/main/src/loop_agent/adapters/base.py).
Because all fields have defaults, adding `@dataclass` and a docstring is enough
to get the eight-field shape, keyword construction, and `str(result)` -> body:

```python
from dataclasses import dataclass
from loop_agent.adapters import ActResultBase

@dataclass
class GeminiResult(ActResultBase):
    """Structured result for one Gemini call."""
    # No field redefinition is needed. The eight fields come from ActResultBase.
```

> Why both a Protocol and a base dataclass exist:
> **`ActResult` (Protocol) is the contract to satisfy**, while **`ActResultBase`
> (dataclass) is the shortest implementation that satisfies it**. You may build a
> result as a separate custom dataclass; as long as it has the eight fields plus
> `__str__`, it structurally conforms to the `ActResult` contract
> (`isinstance(result, ActResult)` is also `True`). Even in chains that mix
> heterogeneous adapters, `verify` only needs to look at `ActResult`, preserving
> composability.

---

## Adapter Body Skeleton

Write the adapter with `@dataclass` using the same skeleton as `ClaudeCodeAct` and
`CodexAct`. The essential shape is:

```python
import os, subprocess
from dataclasses import dataclass
from typing import Any, Optional, Mapping
from loop_agent import ActOutcome
from loop_agent.adapters import Runner, render_prompt   # Common execution seam/formatting

@dataclass
class GeminiAct:
    timeout: float = 600.0
    prompt_template: str = "{prompt}"
    env: Optional[Mapping[str, str]] = None
    gemini_bin: str = "gemini"
    cwd: Optional[str] = None
    runner: Optional[Runner] = None        # Injection point for replacing subprocess.run in tests

    def build_command(self, prompt: str) -> list[str]:
        cmd = [self.gemini_bin, "...flags..."]
        cmd += ["--", prompt]              # The prompt must be a positional arg after "--"
        return cmd

    def _build_env(self) -> dict[str, str]:
        base = dict(os.environ)            # Inherit the existing CLI session
        if self.env:
            base.update(self.env)          # Override-merge through env= (secret values use this path)
        return base

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        command = self.build_command(prompt)
        run = self.runner or subprocess.run
        try:
            proc = run(
                command, capture_output=True, text=True,
                timeout=self.timeout, env=self._build_env(), cwd=self.cwd,
                stdin=subprocess.DEVNULL,  # Required to prevent hangs for CLIs that read interactive input
            )
        except subprocess.TimeoutExpired:
            return ActOutcome(observation=GeminiResult(
                failed=True, error=f"timeout ({self.timeout:g}s)",
                command=tuple(command)), tokens=0)
        except OSError as exc:             # Missing executable / permission error (FileNotFound, etc.)
            return ActOutcome(observation=GeminiResult(
                failed=True, error=f"could not launch {self.gemini_bin!r}: {exc}",
                command=tuple(command)), tokens=0)

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        text, tokens, is_error = _parse_result(stdout, stderr)   # CLI-specific parsing
        failed = proc.returncode != 0 or is_error
        error = (stderr.strip() or text.strip() or f"exit={proc.returncode}") if failed else ""
        result = GeminiResult(text=text, tokens=tokens, failed=failed,
                              returncode=proc.returncode, error=error,
                              stdout=stdout, stderr=stderr, command=tuple(command))
        return ActOutcome(observation=result, tokens=tokens)  # Count tokens regardless of success
```

Only `build_command` (flags) and `_parse_result` / `parse_tokens` (output and
token parsing) are CLI-specific. The shape of `render_prompt`, `Runner`,
`_build_env`, and the way the four rules are enforced are common to all adapters,
so you can copy the skeleton above directly.

---

## Token Accounting Notes (Most Important)

Token parsing is the place where **double counting is most likely**. Each adapter
must confirm the usage semantics of its CLI before deciding the summation rule,
because those semantics differ by adapter.

- **Claude Code**: The `usage` fields `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, and `cache_read_input_tokens` are mutually
  disjoint additive buckets, but only **three fields are counted:
  `input_tokens + output_tokens + cache_creation_input_tokens`**. This is the
  allowlist `_COUNTED_TOKEN_FIELDS` used by `_sum_token_fields`.
  `cache_read_input_tokens` is **excluded** because it has a low billing weight
  (usually about 0.1x normal input and effectively close to free), and because
  internal multi-turn execution rereads cache every turn, making the cumulative
  value grow by orders of magnitude and falsely trigger `TokenBudget`. See
  [Issue #55](#2-double-counting-tokens-makes-tokenbudget-fire-too-early).
- **Codex / OpenAI**: In `usage`, `cached_input_tokens` is a **subset** of
  `input_tokens`, and `reasoning_output_tokens` is a **subset** of
  `output_tokens`. Adding all fields would double count them, so total processing
  volume is **only `input_tokens + output_tokens`**. When those breakdown fields
  are absent and only `total_tokens` exists, fall back to it (`_sum_codex_tokens`).

> **Always confirm the CLI's usage schema and determine whether fields are
> additive buckets or subsets before writing the summation rule.** Copying an
> "add every field" rule silently double counts subset fields for CLIs that have
> them and causes `TokenBudget` to fire too early. This is the
> [Issue #55 bug class](#2-double-counting-tokens-makes-tokenbudget-fire-too-early).

The **regex fallback** for cases where JSON/JSONL usage is unavailable must also
anchor on the leading quote so it does not accidentally match subset keys. For
example, match only `"input_tokens"`, not `"cached_input_tokens"`. Likewise, do
**not sum across multiple sources** such as stdout and stderr; return the value
from the first source that matches to avoid double counting.

---

<a id="hard-won-lessons-from-real-runs"></a>

## Hard-Won Lessons

<a id="1-codex-reads-additional-input-and-hangs-when-stdin-is-a-pipe"></a>

### 1. Codex Hangs When stdin Is a Pipe and It Reads "Additional Input"

In headless loops, the parent stdin may be a pipe or closed endpoint, and
`codex exec` interprets it as "additional input" and tries to read it. It can
**hang even when the prompt was passed as a positional argument**. Passing
`stdin=subprocess.DEVNULL` to close that input path is required. When adapting any
CLI that reads interactive input, suspect this first.

<a id="2-double-counting-tokens-makes-tokenbudget-fire-too-early"></a>

### 2. Double-Counting Tokens Causes TokenBudget to Fire Incorrectly

The self-translation PoC found a bug where an early `ClaudeCodeAct`
implementation accumulated `cache_read` every iteration and triggered
`TokenBudget` much earlier than reality (Issue #55). The cause was an "add all
usage fields" rule that greedily included `cache_read_input_tokens`, whose
billing weight is low and whose cumulative value grows large. **Fixed**: counted
fields were narrowed to the allowlist
`input_tokens + output_tokens + cache_creation_input_tokens`, and `cache_read`
was excluded (token-cost policy, `_sum_token_fields`). **Every time you add a new
adapter, add a parametrized test that proves it is not counting non-cost or
subset usage fields**. The token guard in
[`tests/adapters/test_contract.py`](https://github.com/happy-ryo/loop-agent/blob/main/tests/adapters/test_contract.py)
catches this structurally across all adapters.

### 3. CLI `--json` Schemas Vary by Version

The event types emitted by `codex exec --json` vary by version between dotted
forms (`item.completed`) and snake_case forms (`item_completed` /
`task_complete`). The response body can also appear in several places:
`agent_message` inside `item.completed`, a direct `agent_message` event,
streaming deltas, or `last_agent_message` on a completion event. The robust
approach is to cover the representative shapes, accept the body from whichever
one is present, and use the priority **complete body > last_message >
concatenated deltas**. On the Claude Code side, `--output-format` also differs
between `json` and `stream-json`; the latter reads the final `result` line.
**Capture real CLI output once before writing the schema.**

### 4. Variable-Length Options Consume the Prompt

Options that take values or variable-length values, such as
`--allowed-tools <tools...>` and `--add-dir <path>`, greedily consume the
following prompt as "the next value" when there is no separator. The CLI then
loses the prompt and may send an empty request or hang until timeout. Use the
POSIX convention `--` to stop option parsing and make the prompt a positional
argument (`cmd += ["--", prompt]`).

---

## How to Write Mocks (Test Replacement Points)

An in-memory implementation that satisfies the `act` contract without using
subprocesses lets tests verify loop construction, `TokenBudget`, and failure
paths quickly. Follow the same contract as `MockClaudeCodeAct` and
`MockCodexAct`:

- Return `responses` (`str`, `Mapping`, or Result) in order, and after they are
  exhausted, keep returning the final response. This lets boundaries such as
  `MaxIterations` stop safely.
- Convert `str` to `text` with 0 tokens; expand `Mapping` into Result fields; and
  return Result values as-is.
- Record rendered prompts in `prompts` so tests can inspect them.
- Treat `responses=[]` and unsupported response types as `ConfigError`
  (`LoopError` hierarchy; for backward compatibility these also inherit from
  `ValueError` and `TypeError`, respectively. See [../errors.md](errors.md)).

```python
from loop_agent.adapters import MockClaudeCodeAct
act = MockClaudeCodeAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

---

## How to Write Tests

Verify adapters in three layers. The first two layers are mostly covered by
**registering the adapter with the common harness**.

1. **Common harness (cross-adapter contract)** -
   Register your adapter once in the `AdapterSpec` in
   [`tests/adapters/conftest.py`](https://github.com/happy-ryo/loop-agent/blob/main/tests/adapters/conftest.py)
   with its Act, Result, Mock, `parse_tokens`, success stdout sample, token guard
   sample, and expected stdin value. Then the parametrized tests in
   [`tests/adapters/test_contract.py`](https://github.com/happy-ryo/loop-agent/blob/main/tests/adapters/test_contract.py)
   automatically apply to your adapter as well: result shape, `failed`
   semantics, graceful timeout, graceful launch failure, **token double-counting
   guard**, budget accounting, mock contract, inherited auth environment, and
   stdin safety.
2. **Loop through mock** - Plug the Mock into `run_loop` and verify that
   `goal_met` and `TokenBudget` stopping behavior work as expected without
   subprocesses.
3. **Real subprocess path (CLI-specific)** - Write a fake executable to
   `tmp_path` using `sys.executable` as the interpreter. The script should
   `print` that CLI's output format. Substitute it through `<bin>_bin=` and run
   through the real launch path once. Pin CLI-specific token parsing
   (`parse_tokens`) cases here too.

Even when the real CLI is unavailable in CI, layers 1 and 2 are complete with a
fake runner or fake executable. Integration tests that touch real `codex` or
`claude` binaries should skip when the binary is not installed.

---

## Monitoring External CLI Compatibility

`ClaudeCodeAct` and `CodexAct` read JSON / JSONL events from external CLIs, so
schema drift can happen when those CLIs are updated. Keep normal unit tests as
fast, reproducible contract tests, and separate real CLI monitoring into an
opt-in smoke check.

### 1. Required: fake-runner / fake-subprocess contract tests

This is the layer that always runs in normal CI.

```bash
python -m pytest tests/adapters tests/test_adapters_claude_code.py tests/test_adapters_codex.py
```

This layer guarantees:

- The eight `ActResult` fields, `failed=True` graceful termination, and handling
  of timeouts and launch failures.
- Token semantics for `TokenBudget`, including not double-counting subset fields
  or cheap cache reads.
- Parser support for representative known schemas: Claude `json` /
  `stream-json`, and Codex dotted / snake_case JSONL events.
- Subprocess contracts that loop-agent can fix on its side, including stdin,
  env, cwd, and the prompt separator.

This layer does not start real `claude` or `codex`. It is important that tests do
not fail because the external CLI is not installed, the user is not logged in,
billing is unavailable, or the network is unhealthy.

### 2. Optional: real-CLI smoke job

Maintainers who want early detection of real CLI schema drift can run a small
smoke job locally or in scheduled CI as opt-in. It should skip when the CLI is
not found and must not be a required condition for normal CI.

```yaml
name: adapter-real-cli-smoke
on:
  workflow_dispatch:
  schedule:
    - cron: "0 3 * * 1"

jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install -e ".[test]"
      - name: Codex smoke
        run: |
          if ! command -v codex >/dev/null 2>&1; then
            echo "codex not installed; skipping"
            exit 0
          fi
          python - <<'PY'
          from loop_agent.adapters import CodexAct
          result = CodexAct(timeout=60)({"prompt": "Reply with exactly: LOOP_AGENT_SMOKE_OK"}).observation
          assert not result.failed, result.error
          assert "LOOP_AGENT_SMOKE_OK" in result.text
          assert isinstance(result.tokens, int) and result.tokens >= 0
          PY
      - name: Claude Code smoke
        run: |
          if ! command -v claude >/dev/null 2>&1; then
            echo "claude not installed; skipping"
            exit 0
          fi
          python - <<'PY'
          from loop_agent.adapters import ClaudeCodeAct
          result = ClaudeCodeAct(timeout=60)({"prompt": "Reply with exactly: LOOP_AGENT_SMOKE_OK"}).observation
          assert not result.failed, result.error
          assert "LOOP_AGENT_SMOKE_OK" in result.text
          assert isinstance(result.tokens, int) and result.tokens >= 0
          PY
```

Operational rules:

- Make the job runnable manually with `workflow_dispatch`; add a schedule of
  about once per week if needed.
- If the CLI is not installed, skip with `exit 0`. If the CLI is installed but
  login or auth fails, treat that as a smoke failure.
- Use only a fixed prompt string that is safe to disclose. Do not send repository
  contents, customer data, issue bodies, API keys, local paths, or similar data.
- Keep the smoke job limited to checking whether the schema is still readable. Do
  not put quality evaluation or long generations here.

### 3. Handling real-output fixtures

Keep real CLI stdout / stderr as fixtures only when all of the following are
true:

- The prompt is a short fixed public-safe string, and the output contains no
  secrets, personal information, or internal paths.
- Keeping the fixture does not violate the CLI's terms of use or redistribution
  conditions for outputs.
- The fixture is minimized to only the events and usage fields required by the
  parser. Remove unnecessary conversation body text and trace IDs.
- For every new fixture, add a test comment that records the CLI name, version,
  capture date, and which parser branch it pins.

Do not commit outputs that cannot be kept safely. Instead, add a minimal
handwritten JSON / JSONL sample containing only the field names and event shape
to the parser test.

### 4. Update procedure when an upstream schema changes

1. Save stdout / stderr from the real-CLI smoke failure locally, limited to the
   parts that do not contain secrets.
2. Determine whether the failure is an adapter contract violation or simply a
   parser that does not know the new event shape yet.
3. Add the new schema shape as a minimal fixture in
   `tests/test_adapters_claude_code.py` or `tests/test_adapters_codex.py`, and
   first make it a failing test.
4. Update the parser. Keep existing schema tests and cross-adapter contract tests,
   and preserve backward compatibility for old CLI shapes as long as they can
   still be read.
5. If token usage semantics changed, also update the token guard sample in
   `tests/adapters/conftest.py` and pin non-double-counting through the
   cross-adapter contract.
6. Before committing a fixture, re-check that it contains no secrets, personal
   information, private prompts, or local absolute paths.

---

## Checklist for Adding a New Adapter

- [ ] Define `XxxResult(ActResultBase)` without redefining the eight fields.
  `isinstance(r, ActResult)` is `True`.
- [ ] `XxxAct` is a `@dataclass` and has a `runner` injection point, a
  replaceable `<bin>_bin`, `cwd`, and `env`.
- [ ] `build_command` places the prompt as a positional argument after `--`.
- [ ] `__call__` catches `TimeoutExpired` / `OSError` and returns gracefully with
  `failed=True`; it does not leak those exceptions.
- [ ] For CLIs that read interactive input, pass `stdin=subprocess.DEVNULL`.
- [ ] Token parsing follows the CLI's usage semantics: additive buckets vs.
  subsets. It does not double count. Missing usage means 0.
- [ ] Tokens are counted regardless of success or failure.
- [ ] `_build_env` inherits `os.environ` and override-merges `env=`. Auth is
  delegated to the CLI.
- [ ] Provide `MockXxxAct` supporting `str`, `Mapping`, and Result. Empty
  responses and unsupported types raise `ConfigError`.
- [ ] Register the adapter in `AdapterSpec` in `tests/adapters/conftest.py` and
  pass the common contract tests.
- [ ] Add a **token double-counting guard** sample to the spec, including usage
  with subset keys and the expected token count.
- [ ] Run the real subprocess path through a fake executable once each for
  success, timeout, and env inheritance.
- [ ] If real CLI schema drift monitoring is needed, add the adapter to the
  real-CLI smoke job above and cleanly skip when the CLI is not installed.
- [ ] Add public symbols to `__all__` in `loop_agent.adapters.__init__`.
- [ ] `mypy` / `pytest` are green.
