# Quickstart - Run loop-agent in 30 minutes as a Claude Code user

This page is a path for people who already use Claude Code day to day and want to get loop-agent **running for real in 30 minutes**. The shortest route is **Path E (coding-agent driven)**: describe the loop you want in natural language, and Claude Code itself will assemble `gather / act / verify / conditions / gate` and run it. Minimal examples for building the loop by hand (Paths A / B) are also included later.

The only prerequisite knowledge is the list of seams in [docs/seams.md](./seams.md). The API used in a first harness is intentionally limited in [first-harness-api.md](./first-harness-api.md). The loop owns only the orchestration core; the policy (what to select, how to execute it, and what counts as success) is entirely written by you or by your coding agent.

---

## 0. Installation (2 minutes)

```bash
git clone https://github.com/happy-ryo/loop-agent
cd loop-agent
python3 -m pip install -e .          # Loop core itself (dependencies are mostly stdlib)
python3 -m pip install -e '.[dev]'   # + pytest (for running tests; used by verify in Paths E/C)
# Note: in zsh, always quote extras ('.[dev]' / '.[otel]'). Bare .[dev] fails because of glob expansion.
```

Check the installation:

```bash
loop-agent          # OK if quick help + a sample task.toml are shown
python3 -m pytest   # Confirm the full suite is green once so self-improvement verify steps are stable
# If Windows user Temp permissions get in the way: python3 -m pytest --basetemp .pytest-tmp
```

Claude Code authentication is **delegated directly to the claude CLI** (loop-agent inherits `os.environ`). If you are already logged in with `claude` (`~/.claude`) or `ANTHROPIC_API_KEY` is available, no extra setup is required.

---

## 1. Path E: Have the coding agent build the loop (recommended and shortest)

### Concept

The highest-level way to use loop-agent is not to "write the seams yourself", but to **have Claude Code write them**. You only provide your intent as prose:

```
intent (your natural language)
  -> Claude Code
  - writes gather / act / verify / conditions / gate
  - starts run_loop
  - observes the result (JSONL), then rewrites the policy if needed
  ->
loop-agent runtime (thin, immutable loop core)
  -> results
```

### What to do (inside a Claude Code session)

1. Open this repository in Claude Code, or open your own project where `loop-agent` has been installed with `pip install`.
2. Tell Claude Code the loop-agent seams, then give it your intent. Example:

> This repository contains loop-agent (a thin loop engine for `gather -> act -> verify -> repeat`. The seams are `gather/act/verify/conditions/gate`, and `loop_agent.adapters.ClaudeCodeAct` can be used for `act`).
> **Build and run a loop that finds flaky tests in this repository and stabilizes them.** For verify, require "the target test passes 10 consecutive times after the fix"; for `act`, use `ClaudeCodeAct(model="sonnet")` (editing only; do not let it commit); stop with `MaxIterations(20)` and `TokenBudget`. After convergence, a human (me) will review and perform commit / push.

3. Claude Code writes `harness.py` (wiring `gather/act/verify`) and starts `run_loop`. The resulting harness will roughly look like this:

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

flaky = discover_flaky_tests()          # Input material for gather (extracted from CI logs, etc.)

def gather(state):
    rem = [t for t in flaky if t not in done]
    return {"prompt": f"Fix the root cause of flaky test {rem[0]}. ...", "test": rem[0]}

def verify(outcome):
    test = current_test
    passed = run_test_n_times(test, n=10)            # ground truth = real tests
    return VerifyOutcome(goal_met=passed, detail=f"{test}: 10x" if passed else "still flaky")

result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # Editing only; verify owns test execution
    gather=gather, verify=verify,
    conditions=[MaxIterations(20), TokenBudget(2_000_000)],
)
```

4. Observe the result. If needed, tell Claude Code things like "increase verify from 10 runs to 20" or "lower act to haiku to reduce cost", and it will rewrite the policy and run it again. **What evolves is your policy, not loop-agent**.

> For a production starting point, choose one of the three patterns in [docs/recipes/production-harnesses.md](./recipes/production-harnesses.md): single verified edit, multi-item work queue, or gated irreversible action. Complete recipes (flaky test / translation / refactor) are in [docs/recipes/](./recipes/). They include prose intent examples you can pass directly to Claude Code.

---

## 2. Paths A / B: Write the minimal loop yourself

These are the smallest forms for writing a loop by hand without going through a coding agent.

### Path A: `run_loop` in 5 lines

```python
from loop_agent import run_loop, ActOutcome, VerifyOutcome, MaxIterations

n = {"v": 0}
result = run_loop(
    act=lambda ctx: ActOutcome(observation=(n.update(v=n["v"] + 1) or f"step {n['v']}")),
    verify=lambda o: VerifyOutcome(goal_met=n["v"] >= 3),
    conditions=[MaxIterations(5)],   # Always stops even if the goal is not met (prevents AutoGPT-style runaway)
)
print(result.status, result.reason)
```

### Path B: Plug `ClaudeCodeAct` into `act`

Each iteration launches headless `claude --print` once. The core rule is to write `verify` against **ground truth** (for example, a pytest exit code) and not delegate success judgment to an LLM-as-judge.

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct

act = ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="haiku", timeout=600)

def verify(outcome):
    res = outcome.observation                       # ClaudeCodeResult
    return VerifyOutcome(goal_met=(not res.failed) and "DONE" in res.text)

result = run_loop(
    act=act, verify=verify,
    gather=lambda s: {"prompt": f"Write one next fix (attempt {s.iteration})"},
    conditions=[MaxIterations(10), TokenBudget(200_000)],
)
```

If you use Codex, replace `act` with `CodexAct`. **The `act` interface (callable -> `ActOutcome`) has the same shape**, but the constructor arguments are Codex-specific: there is no `allowed_tools`; instead it takes `model="gpt-5.5"` / `effort` / `sandbox` / `allowed_args`.

```python
from loop_agent.adapters import CodexAct
act = CodexAct(model="gpt-5.5", effort="medium", timeout=600)   # CodexAct has no allowed_tools
# The observation is CodexResult (.text/.failed/.tokens/.returncode/.error). verify can be used the same way.
```

### Use verify helpers

If you already have a mechanical oracle, you can use a thin helper instead of writing `verify` from scratch.

```python
from loop_agent import PytestVerifier, CommandVerifier, RegexVerifier

verify = PytestVerifier(["tests/test_loop.py", "-q"], timeout=60)
# or: verify = CommandVerifier(["python", "-m", "ruff", "check", "src"], timeout=60)
# or: verify = RegexVerifier(r"\bDONE\b")
```

These are not LLM-as-judge. They only convert mechanical signals such as exit codes or regex matches into `VerifyOutcome`.

### Run with TOML + CLI (the smallest no-code form)

You can also write `task.toml` and start it with `loop-agent run`. See [docs/cli.md](./cli.md) for details.

```bash
loop-agent run ./examples/task.toml --max-iter 5
loop-agent status <run-id>          # Progress
loop-agent logs <run-id> --follow   # Follow events through loop_end
```

---

## 3. Monitoring: See what the loop did, why, and how it ended

### View from the CLI

```bash
loop-agent status <run-id>   # status / iterations / tokens / stop reason / pending
loop-agent logs <run-id>     # Structured events: loop_begin / loop_step x N / loop_end
```

### Inspect state.db with sqlite3

Each iteration is persisted atomically to a single SQLite SoT (the minimal `run` / `step` / `event` / `stop_reason` schema).

```bash
sqlite3 loop-state.db '.tables'
sqlite3 loop-state.db "SELECT iteration, tokens_used, elapsed FROM step WHERE run_id='<run-id>' ORDER BY iteration;"
sqlite3 loop-state.db "SELECT name, detail FROM stop_reason WHERE run_id='<run-id>';"
```

From Python, use `LoopStore`:

```python
from loop_agent import connect, LoopStore
store = LoopStore(connect("loop-state.db"))
store.read_steps("<run-id>")        # Per-iteration steps (observation already decoded)
store.get_stop_reason("<run-id>")   # Triggered stop condition or goal reached
```

### View OTel spans

If the OTel SDK is installed, each run becomes one GenAI span (`gen_ai.*` + iteration number + stop reason). **If it is not installed, this degrades to no-op**, and JSONL / event sinks continue to work as usual. To inspect real spans, use `pip install -e '.[dev]'` (includes the OTel **SDK**). Note: the `.[otel]` extra includes only `opentelemetry-api` and **does not include the SDK**, so it remains on a no-op tracer and emits no real spans. If you want spans, install `.[dev]` or `opentelemetry-sdk` explicitly. Use `run_observed_loop(...)` as the entry point.

---

## 4. Resume: Continue an interrupted loop from the middle

`LoopState` can be restored from steps already persisted in state.db, so execution can continue from the interruption point without losing state (iteration, accumulated cost, elapsed time, and history are carried forward).

```bash
loop-agent resume <run-id> ./examples/task.toml   # CLI
```

```python
from loop_agent import run_loop, DBProgressLog, GoalMet, MaxIterations

db = DBProgressLog("loop-state.db", "<run-id>")   # For an existing run, restores state from steps
result = run_loop(act=act, verify=verify,
                  conditions=[GoalMet(verifier), MaxIterations(100)],
                  initial_state=db.state,          # Continue from the interruption point (new runs use empty state)
                  on_step=db.on_step)
db.record_result(result)
```

> **Resume tip**: derive stop decisions from the (gathered) **state**. Across processes, the act/verify hooks are rebuilt, and any internal call counters they hold are not restored. If the decision is derived from state, the same judgment can be reproduced in the new process. See [docs/persistence-and-resume.md](./persistence-and-resume.md) for details.

---

## 5. Troubleshooting (common failure points)

### Claude Code authentication fails

`ClaudeCodeAct` inherits `os.environ` and delegates auth to the claude CLI. If it returns `failed=True` and `error` contains an auth-related message, first check whether `claude --print "hi"` works by itself in your shell. You can also inject an API key explicitly with `env=`.

### `TokenBudget` fires too early (fixed: Issue #55)

When `ClaudeCodeAct` runs with `Read` + `Edit`, Claude Code performs multiple internal turns, and each turn rereads cached context. As a result, the cumulative `cache_read_input_tokens` reported for a single `act` can grow orders of magnitude larger than the real input+output. The initial implementation added this value to the total, so **`TokenBudget` fired far earlier than intended** (found in the self-translation PoC: translating one roughly 170-line file was counted as about 340k tokens).

**This is now fixed**. `ClaudeCodeAct` token accounting accumulates only `input_tokens + output_tokens + cache_creation_input_tokens`, and excludes `cache_read_input_tokens`, which is cheap and grows cumulatively (token-cost policy). Therefore `TokenBudget` now tracks actual cost proportionally.

- If you still want to reliably rate-limit long runs, combine `MaxIterations` / `Timeout` as well. This is more robust than relying on `TokenBudget` alone because it adds backstops.

### verify times out / the loop never stops

- Subprocess `act` / `verify` steps always need a finite timeout (`[act]`/`[verify]`.`timeout_seconds` > loop `timeout_seconds` > default 3600s). Stop conditions are evaluated only at iteration boundaries and do not interrupt a currently running step, so an unlimited subprocess hang disables all caps. Use explicit timeouts for long-running work.
- If no stop condition is provided, loop-agent raises `ConfigError` to prevent infinite loops. Always include at least one of `max_iterations` or `timeout_seconds`.

### Worried that an `act` exception will kill the loop

`ClaudeCodeAct` / `CodexAct` do **not** raise exceptions for timeout, non-zero exit, or missing executable. They return gracefully with `failed=True`. Boundary checks such as `Timeout` / `MaxIterations` always continue to apply. The policy is designed to be able to fail safely.

Configuration errors (invalid argument values or types, no stop conditions, etc.) are raised as `loop_agent.errors.ConfigError`; runtime state violations (such as deciding an already resolved gate again) are raised as `StateError`. Catch the base `LoopError` to handle them together. For backward compatibility, they can also still be caught as the previous `ValueError` / `RuntimeError`. See [errors.md](./errors.md) for details.

---

## 6. Immediate benefits of the safety mechanisms

| Mechanism | Why it helps | How to write it |
|---|---|---|
| **MaxIterations / Timeout / TokenBudget** | Always stops even if the goal is not met. Structurally prevents AutoGPT-style runaway behavior and cost explosions | `conditions=[MaxIterations(20), TokenBudget(...)]` (OR evaluation) |
| **HumanGate** | Inserts human approval only for the discrete actions proposed by **the loop itself**. Preserves decisions across pause -> resume, and makes irreversible actions exactly-once | `HumanGate(on=lambda action: action in {"commit", "push", "deploy"}, store=..., run_id=...)` |
| **Reflexion** | Improves on **systematic failures** where the same mistake repeats by wiring lessons from failed episodes into the next episode. It does not help with stochastic misses (see [reflexion-when-to-use.md](./reflexion-when-to-use.md)) | `run_reflexion(...)` |

> **Important scope note for HumanGate**: `HumanGate` reviews the **discrete loop action** returned by `gather`; it cannot see operations such as `git commit` that are executed internally by the `act` subprocess (`claude --print`). The gate fires between `gather` and `act`. Therefore, if you really want to gate irreversible operations, either (1) do not let the act subprocess commit / push (limit `allowed_tools` to editing tools) and make commit / push a **human step outside the loop**, or (2) make `gather` propose commit as a **discrete loop action**, catch it with `on`, and have `act` execute it. [The limited human gate section in docs/safety.md](./safety.md) is the canonical example of (2) (`on=lambda a: a == "deploy"`).

Minimal safety template (recommended for self-improvement workflows: act edits only, commit stays outside):

```python
# verify is ground truth (pytest exit code). Use two upper bounds so the loop always stops.
# Do not let the act subprocess commit/push because the gate cannot see internal subprocess operations.
result = run_loop(
    act=ClaudeCodeAct(allowed_tools=["Read", "Edit"], model="sonnet"),   # Editing only
    verify=verify_with_pytest,
    conditions=[MaxIterations(20), Timeout(3600)],
)
# After convergence, a human reviews and runs commit / push (= irreversible operations are isolated outside the loop).
```

---

## Next Reading

- [docs/recipes/production-harnesses.md](./recipes/production-harnesses.md) - selection guide for three representative production harness patterns
- [docs/recipes/](./recipes/) - concrete Path E examples from prose intent to harness (flaky test / translation / refactor)
- [docs/first-harness-api.md](./first-harness-api.md) - minimal API surface for a first harness
- [docs/reflexion-when-to-use.md](./reflexion-when-to-use.md) - criteria for tasks where Reflexion works or does not work (PoC evidence)
- [docs/api-reference.md](./api-reference.md) - overview of the full API and the loop core scope
- [docs/persistence-and-resume.md](./persistence-and-resume.md) / [docs/transport.md](./transport.md) / [docs/reflexion.md](./reflexion.md) - details on state.db / transport / work discovery / outer Reflexion
- [README](../README.md) - overall concept and docs/ navigation
