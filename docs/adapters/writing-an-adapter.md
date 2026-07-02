# How to Write a New `act` Adapter

The loop-agent `act` seam contains the "executor for one iteration" in a single
function (`Callable[[context], ActOutcome]`). An **adapter** is the component that
starts an external agent CLI (Claude Code, Codex, or any future tool) in headless
mode and plugs it into `act`. `loop_agent.adapters` provides
[`ClaudeCodeAct`](../../src/loop_agent/adapters/claude_code.py) and
[`CodexAct`](../../src/loop_agent/adapters/codex.py) as reference
implementations. The only differences between them are the **subprocess command,
flags, and token/output parsing**; their result shape and prompt rendering are
fully isomorphic.

This document is the canonical guide for writing a third or later adapter (for
example, `GeminiAct`, `AiderAct`, or an internal tool) **correctly under the same
contract**. The pitfalls
[`ClaudeCodeAct` / `CodexAct` have already hit](#hard-won-lessons-pitfalls-found-in-real-runs)
are collected in one place. Reading that section first will usually make the
implementation faster.

> Adapters are not a new loop-agent feature. They standardize, along with the
> known pitfalls, a pattern users can already write today through the `act` seam.
> As long as a new adapter is a function that returns `ActOutcome`, it can be
> plugged in without changing the core (`run_loop`) at all.

> **There are two kinds of adapters.** This document covers **executor
> adapters** that start an external CLI as a subprocess (`ClaudeCodeAct` /
> `CodexAct`). The four rules, token parsing, and Mock guidance below are for
> that kind of adapter. The other kind is an **adapter that composes other `act`
> hooks**. The canonical example is
> [`ModelLadder`](../../src/loop_agent/adapters/model_ladder.py), which
> automatically escalates difficult tasks to stronger models (Issue #53).
> Composition adapters do not start subprocesses, so they do not have
> `build_command`, `runner`, or `parse_tokens`, and they are **not included** in
> the common contract harness (`ADAPTER_SPECS`). Token, `failed`, and graceful
> termination guarantees are satisfied by the executor adapters used at each
> stage; the composition layer passes through the stage's `ActOutcome`.
> `ModelLadder` packages the pattern that "users can also write because `act` is
> `Callable`", and it hedges the pitfalls around stateful attempt counts, the
> constraint that `act` cannot see `verify`'s goal decision, and heterogeneous
> composition (`ClaudeCodeAct` + `CodexAct`). Use `ModelLadder` as the template
> for new composition adapters.

---

## The `act` Seam Contract (Four Rules)

Adapters must follow these four rules so they do not break the loop core's
properties: stopping at boundaries, respecting budgets, and delegating
authentication externally. Both `ClaudeCodeAct` and `CodexAct` satisfy them.

1. **Do not kill the loop with exceptions.** Cases that mean "execution failed
   or could not be performed", such as timeout, non-zero exit, or missing
   executable, are returned gracefully as `failed=True` in
   `ActOutcome.observation` instead of being raised. This lets `verify` inspect
   `outcome.observation.failed` and decide whether to continue or stop, while
   boundary checks such as `Timeout` / `MaxIterations` continue to work. As a
   rule, **no exceptions should escape** (`subprocess.TimeoutExpired` and
   `OSError` must be caught and converted to `failed`). The only exception is
   `render_prompt`: if `prompt_template` references a field that is absent from
   the context, it **eagerly raises `KeyError`**. This is by design: configuration
   errors before execution should fail immediately rather than being swallowed.
   In the skeleton below, `render_prompt` is also called outside the `try` block.
   This `KeyError` is intentionally the only built-in exception outside the
   `LoopError` hierarchy, following `str.format` semantics. See
   [../errors.md](../errors.md) for the library's full exception hierarchy.

2. **Add tokens to the budget.** Extract the total number of processing tokens
   from the response and put it in `ActOutcome.tokens`. The driver adds this to
   `state.tokens_used`, so `TokenBudget` works directly. **Use 0 when tokens
   cannot be extracted**. It is normal for text output to omit usage, and 0 is
   the conservative value. Count tokens **whether the call succeeds or fails**,
   because failed attempts may still consume tokens.

3. **Delegate auth to the CLI.** By default, the child process inherits the
   caller's `os.environ` and primarily uses the external CLI's existing session
   (logins such as `~/.claude` / `~/.codex`). If API keys such as
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are present in the environment, they
   act as fallbacks on the CLI side. The adapter itself must not read or inject
   keys. If secret values must be supplied, provide only an `env=` path that
   **overrides by merging**.

4. **Close stdin to prevent hangs.** In a headless loop, the parent stdin may be a
   pipe or closed endpoint. If the child CLI tries to read "additional input", it
   can **hang**. Always finalize the prompt as a positional argument after `--`,
   and pass `stdin=subprocess.DEVNULL` to CLIs that read interactive input. See
   [the real Codex issue](#1-codex-hangs-by-reading-additional-input-when-stdin-is-a-pipe).

---

## Result Shape (`ActResult` Contract)

The result object placed in `ActOutcome.observation` follows the common
structural contract [`ActResult`](../../src/loop_agent/adapters/base.py)
(`Protocol`). It has eight fields and `__str__`, which returns the response body:

| Field | Type | Meaning |
|---|---|---|
| `text` | `str` | The assistant response body. `str(result)` returns the same body. |
| `tokens` | `int` | Total tokens consumed by this call, used for budget accounting. |
| `failed` | `bool` | Whether the call failed: non-zero exit, CLI-reported error, timeout, or launch failure. |
| `returncode` | `Optional[int]` | Child process exit code (`None` for launch failure or timeout). |
| `error` | `str` | Concise error text on failure, or an empty string on success. |
| `stdout` / `stderr` | `str` | Raw child process output, for debugging and reparsing. |
| `command` | `tuple[str, ...]` | The command that was actually executed, as an argument sequence. |

The shortest path is for your adapter's Result to inherit the shared concrete
dataclass [`ActResultBase`](../../src/loop_agent/adapters/base.py). All fields
have defaults, so adding `@dataclass` and a docstring gives you the eight-field
shape, keyword construction, and `str(result)` -> body behavior as-is:

```python
from dataclasses import dataclass
from loop_agent.adapters import ActResultBase

@dataclass
class GeminiResult(ActResultBase):
    """Structured result for one Gemini call."""
    # No field redefinition is needed. The eight fields are inherited from ActResultBase.
```

> Why both a Protocol and a base dataclass exist:
> **`ActResult` (Protocol) is the contract to satisfy**, while
> **`ActResultBase` (dataclass) is the shortest implementation that satisfies the
> contract**. You may build the result as a separate dataclass; as long as it has
> the eight fields plus `__str__`, it structurally conforms to `ActResult`
> (`isinstance(result, ActResult)` is also `True`). Even in chains that mix
> heterogeneous adapters, `verify` only needs to look at `ActResult`, preserving
> composability.

---

## Adapter Skeleton

Write the adapter with `@dataclass`, using the same skeleton as `ClaudeCodeAct` /
`CodexAct`. The essentials are:

```python
import os, subprocess
from dataclasses import dataclass
from typing import Any, Optional, Mapping
from loop_agent import ActOutcome
from loop_agent.adapters import Runner, render_prompt   # Common execution seam/rendering

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
        cmd += ["--", prompt]              # The prompt must be a positional argument after "--"
        return cmd

    def _build_env(self) -> dict[str, str]:
        base = dict(os.environ)            # Inherit existing CLI sessions
        if self.env:
            base.update(self.env)          # Override-merge through env= (the path for secrets)
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
        except OSError as exc:             # Missing executable / permission denied (FileNotFound, etc.)
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

Only `build_command` (flags) and `_parse_result`/`parse_tokens` (output/token
parsing) are CLI-specific. The shape of `render_prompt`, `Runner`, `_build_env`,
and the four rules are common to all adapters, so this skeleton can be copied
directly.

---

## Token Accounting Notes (Most Important)

Token parsing is the place where **double counting is most likely**. Each adapter
has different usage semantics, so inspect the CLI's schema before deciding how to
sum fields.

- **Claude Code**: The `usage` fields `input_tokens` / `output_tokens` /
  `cache_creation_input_tokens` / `cache_read_input_tokens` are mutually
  exclusive additive buckets, but only **three fields are counted:
  `input_tokens + output_tokens + cache_creation_input_tokens`** (the
  `_COUNTED_TOKEN_FIELDS` allowlist used by `_sum_token_fields`).
  `cache_read_input_tokens` is **excluded** because its billing weight is low
  (usually around 0.1x normal input, effectively close to free), and internal
  multi-turn runs reread the cache every turn. Counting it makes the cumulative
  total grow by orders of magnitude and can falsely trip `TokenBudget`
  ([Issue #55](#2-double-counting-tokens-falsely-trips-tokenbudget)).
- **Codex / OpenAI**: In `usage`, `cached_input_tokens` is a **subset** of
  `input_tokens`, and `reasoning_output_tokens` is a **subset** of
  `output_tokens`. Adding all fields double-counts, so total processing volume is
  **only `input_tokens + output_tokens`**. If there is no breakdown and only
  `total_tokens` is available, fall back to that (`_sum_codex_tokens`).

> **Always inspect the CLI's usage schema and distinguish additive buckets from
> subsets before writing the summing rule.** Copying an "add every field" rule
> silently double-counts CLIs with subset fields and causes early false
> `TokenBudget` failures (the [Issue #55 bug class](#2-double-counting-tokens-falsely-trips-tokenbudget)).

The **regular-expression fallback** for cases where JSON/JSONL usage cannot be
extracted must also anchor on the opening quote so it does not accidentally match
subset keys (match only `"input_tokens"`, not `"cached_input_tokens"`). It should
also **not sum across multiple sources** such as stdout and stderr; return the
value from the first source that matches to avoid double counting.

---

## Hard-Won Lessons (Pitfalls Found in Real Runs)

### 1. Codex hangs by reading "additional input" when stdin is a pipe

In a headless loop, the parent stdin may be a pipe or closed endpoint.
`codex exec` interprets that as "additional input" and tries to read from it,
which **hangs even when the prompt was passed as a positional argument**. Passing
`stdin=subprocess.DEVNULL` to close stdin is required. Suspect this first when
adapting any CLI that reads interactive input.

### 2. Double-counting tokens falsely trips TokenBudget

In the self-translation PoC, an early `ClaudeCodeAct` implementation accumulated
`cache_read` on every iteration and triggered `TokenBudget` much earlier than it
should have (Issue #55). The cause was a "sum every usage field" implementation
that greedily picked up `cache_read_input_tokens`, even though it is cheap and can
balloon cumulatively. **Fixed**: counted fields are restricted to the allowlist
`input_tokens + output_tokens + cache_creation_input_tokens`, and `cache_read` is
excluded (the token-cost policy in `_sum_token_fields`). **Every time you add a
new adapter, add a parametrized test proving it does not count non-cost or subset
usage fields**. The token guard in
[`tests/adapters/test_contract.py`](../../tests/adapters/test_contract.py)
structurally catches this across all adapters.

### 3. CLI `--json` schemas drift across versions

`codex exec --json` event types vary by version between dotted (`item.completed`)
and snake_case (`item_completed` / `task_complete`). The response body may also
appear in several places: `agent_message` inside `item.completed`, a direct
`agent_message` event, streaming deltas, or `last_agent_message` on a completion
event. It is more robust to cover the representative shapes and treat any of
them as a valid body, with priority **complete body > last_message > concatenated
delta**. Claude Code also differs between `--output-format json` and
`stream-json`; the latter uses the final `result` line. The safest approach is to
**capture real CLI output once before writing the schema**.

### 4. Variable-length options consume the prompt

Options that **take values, especially variable-length values**, such as
`--allowed-tools <tools...>` or `--add-dir <path>`, greedily consume the following
prompt as another value when there is no separator. The CLI then loses the prompt,
leading to an empty request or a hang until timeout. Stop option parsing with the
POSIX `--` convention and finalize the prompt as a positional argument
(`cmd += ["--", prompt]`).

---

## Writing Mocks (Replacement Point for Tests)

An in-memory implementation that satisfies the `act` contract without using
subprocesses makes it fast to verify loop assembly, `TokenBudget`, and failure
paths. Follow the same contract as `MockClaudeCodeAct` / `MockCodexAct`:

- Return `responses` (`str`, `Mapping`, or Result) in order, and once exhausted,
  stick to the final response so boundaries such as `MaxIterations` can stop
  safely.
- Convert `str` -> `text` (tokens 0), `Mapping` -> expanded Result fields, and
  Result -> unchanged.
- Record rendered prompts in `prompts` so tests can inspect them.
- `responses=[]` and unsupported response types raise `ConfigError` (the
  `LoopError` hierarchy; for backward compatibility they also inherit
  `ValueError` / `TypeError`, respectively. See [../errors.md](../errors.md)).

```python
from loop_agent.adapters import MockClaudeCodeAct
act = MockClaudeCodeAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

---

## Writing Tests

Verify adapters in three layers. The first two layers are mostly covered by
**registering the adapter with the common harness**.

1. **Common harness (cross-adapter contract)** -
   Register one entry for your adapter (Act / Result / Mock / `parse_tokens` /
   success stdout sample / token guard sample / expected stdin value) in
   `AdapterSpec` in
   [`tests/adapters/conftest.py`](../../tests/adapters/conftest.py). The
   parametrized tests in
   [`tests/adapters/test_contract.py`](../../tests/adapters/test_contract.py)
   then automatically apply to your adapter as well: result shape, `failed`
   semantics, graceful timeout, graceful launch failure, **token double-counting
   guard**, budget accounting, Mock contract, auth environment inheritance, and
   stdin safety.
2. **Loop through the mock** - Pass the Mock into `run_loop` and verify
   `goal_met` and `TokenBudget` stopping behavior without subprocesses.
3. **Real subprocess path (CLI-specific)** - Write a fake executable to
   `tmp_path` that uses `sys.executable` as the interpreter and `print`s that
   CLI's output format. Substitute it through `<bin>_bin=` and run the real
   launch path once. Pin CLI-specific token parsing (`parse_tokens`) cases here
   as well.

Even in CI environments without real CLIs, layers 1 and 2 are fully covered by
fake runners and fake executables. Integration tests that touch real `codex` /
`claude` binaries should skip when those binaries are not installed.

---

## Monitoring External CLI Compatibility

`ClaudeCodeAct` / `CodexAct` read external CLI JSON / JSONL events, so CLI
version upgrades can introduce schema drift. Keep normal unit tests fast and
reproducible as contract tests, and separate real CLI monitoring as an opt-in
smoke check.

### 1. Required: fake-runner / fake-subprocess contract tests

This is the layer that always runs in normal CI.

```bash
python -m pytest tests/adapters tests/test_adapters_claude_code.py tests/test_adapters_codex.py
```

This layer guarantees:

- The eight `ActResult` fields, `failed=True` graceful termination, and timeout /
  launch-failure behavior.
- The token semantics used for `TokenBudget` accounting, without double-counting
  subset fields or cheap cache reads.
- Parser support for representative known schemas: Claude `json` /
  `stream-json`, and Codex dotted / snake_case JSONL events.
- The subprocess contract fixed by loop-agent: stdin, env, cwd, prompt separator,
  and related behavior.

This layer does not start real `claude` / `codex`. It is important that tests do
not fail because an external CLI is missing, not logged in, billable, or affected
by network issues.

### 2. Optional: real-CLI smoke job

Maintainers who want to detect real CLI schema drift early can opt in to a small
smoke job locally or in scheduled CI. The job skips when the CLI is unavailable
and must not be a requirement for normal CI.

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

- Make the job manually runnable with `workflow_dispatch`, and schedule it around
  once a week if needed.
- If the CLI is not installed, skip with `exit 0`. If the CLI is installed but
  not logged in or auth fails, treat that as a smoke failure.
- Use only fixed prompt strings that are safe to disclose. Do not send repository
  contents, customer data, issue bodies, API keys, local paths, or similar data.
- Keep the smoke job limited to checking whether the schema is still readable.
  Do not put quality evaluation or long generations here.

### 3. Handling real-output fixtures

Keep real CLI stdout / stderr as fixtures only when all of the following are
true:

- The prompt is a short fixed string safe for disclosure, and the output contains
  no secrets, personal information, or internal paths.
- Keeping the fixture does not violate the CLI's terms of use or output
  redistribution rules.
- The fixture is minimized to only the events and usage needed by the parser.
  Remove unnecessary conversation body text and trace IDs.
- New fixtures include a test comment with the CLI name, version, capture date,
  and the parser branch being pinned.

Do not commit output that cannot be kept safely. In that case, add a hand-written
minimal JSON / JSONL sample to the parser test containing only the field names and
event shape.

### 4. Update procedure when an upstream schema changes

1. Save the stdout / stderr from the real-CLI smoke failure locally, within the
   limits of what contains no secrets.
2. Determine whether the failure is an adapter contract violation or simply a
   parser that does not know the new event shape.
3. Add the new schema shape as a minimal fixture in
   `tests/test_adapters_claude_code.py` or `tests/test_adapters_codex.py`, and
   first make it a failing test.
4. Update the parser. Keep existing schema tests and cross-adapter contract tests,
   and preserve backward compatibility for older CLI shapes as long as they can
   still be read.
5. If token usage semantics changed, update the token guard sample in
   `tests/adapters/conftest.py` as well, and lock in non-double-counting through
   the cross-adapter contract.
6. Before committing the fixture, re-check that it contains no secrets, personal
   information, private prompts, or local absolute paths.

---

## New Adapter Checklist

- [ ] Define `XxxResult(ActResultBase)` without redefining the eight fields.
      `isinstance(r, ActResult)` is `True`.
- [ ] `XxxAct` is a `@dataclass` and has a `runner` injection point,
      `<bin>_bin` substitution, `cwd`, and `env`.
- [ ] `build_command` places the prompt as a positional argument after `--`.
- [ ] `__call__` catches `TimeoutExpired` / `OSError` and returns gracefully with
      `failed=True` without leaking exceptions.
- [ ] If the CLI reads interactive input, pass `stdin=subprocess.DEVNULL`.
- [ ] Token parsing follows the CLI's usage semantics (additive buckets vs.
      subsets), does not double-count, and returns 0 when usage is absent.
- [ ] Tokens are counted regardless of success or failure.
- [ ] `_build_env` inherits `os.environ` and override-merges `env=`; auth is
      delegated to the CLI.
- [ ] Provide `MockXxxAct` (`str` / `Mapping` / Result; empty responses and
      unsupported types both raise `ConfigError`).
- [ ] Register the adapter in `AdapterSpec` in `tests/adapters/conftest.py` and
      pass the common contract tests.
- [ ] Add a **token double-counting guard** sample to the spec, including usage
      with subset keys and the expected token count.
- [ ] Exercise the real subprocess path (fake executable) once each for success,
      timeout, and env inheritance.
- [ ] If real CLI schema drift monitoring is needed, add it to the real-CLI smoke
      job above and cleanly skip when the CLI is not installed.
- [ ] Add public symbols to `__all__` in `loop_agent.adapters.__init__`.
- [ ] `mypy` / `pytest` are green.
