# CLI Launcher (loop-agent run / status / summary / dashboard / spikes / resume / logs / init-harness)

A stdlib (`argparse`) CLI that starts the `gather -> act -> verify -> repeat` loop from a declarative `task.toml` (Issue #31). `act` / `verify` can be defined as either **(1) a subprocess command** or **(2) a Python callable** (`module:attr`). Each iteration is persisted to the state.db SoT (`DBProgressLog`), so you can check progress by run-id, **resume** runs, and track events.

```bash
pip install -e .            # Install loop-agent from [project.scripts]
loop-agent                  # Show quick help + a sample task.toml

# Start a run (from a TOML definition. --max-iter / --token-budget / --timeout override TOML)
loop-agent run ./examples/task.toml
loop-agent run ./examples/task.toml --max-iter 5 --timeout 600
# run-id     : run-20260628-002431-ab12cd
# status     : goal_met / stopped / paused
# reason     : goal met
# iterations : 3 / tokens : 0 / elapsed : 0.123s

loop-agent status <run-id>            # Progress in state.db (status/iterations/tokens/stop reason/pending)
loop-agent summary                    # List runs in state.db (read-only)
loop-agent summary --db loop-state.db --limit 10
loop-agent dashboard --db loop-state.db --output dashboard.html
loop-agent spikes <run-id> --db loop-state.db
loop-agent resume <run-id> ./examples/task.toml   # Resume an interrupted loop from the middle (seed restored state)
loop-agent logs <run-id>              # Show LoopObserver events (loop_begin/step/end)
loop-agent logs <run-id> --follow     # Follow new events until loop_end (tail -f style)
loop-agent init-harness --template light  --output ./harness-light
loop-agent init-harness --template claude --output ./harness-claude
loop-agent init-harness --template codex  --output ./harness-codex
```

`task.toml` (see also [`examples/task.toml`](../examples/task.toml)):

```toml
[loop]
goal = "make the test suite pass"
# run_id = "demo-run"        # Auto-numbered when omitted

[conditions]                 # At least one is required (otherwise the loop cannot stop = rejected by R3)
max_iterations = 20
token_budget = 500000
timeout_seconds = 3600
# no_progress = { window = 5, repeat = 3 }   # Optional: stop on stuck detection

[act]
# subprocess mode: {prompt}/{goal} -> [loop].goal, {iteration} -> iteration number
# This example uses claude as the equivalent of ClaudeCodeAct. codex exec, custom tools, and other
# arbitrary subprocess commands can be plugged into the act seam with the same format (ActHook Protocol).
command = ["claude", "--print", "{prompt}"]
cost_per_step = 0            # Tokens counted per step (for token_budget)
# timeout_seconds = 120      # Optional: act subprocess limit
# python = "mypkg.hooks:act" # OR: in-process callable act(context) -> ActOutcome

[verify]
# subprocess mode: exit code 0 == goal achieved (ground truth)
command = ["pytest", "-q"]
# python = "mypkg.hooks:verify"  # OR: callable verify(outcome) -> VerifyOutcome

[state]
# db = "loop-state.db"       # Optional: defaults to loop-state.db (stores multiple runs)
# events = "events.jsonl"    # Optional: also write a JSONL event journal in addition to state.db
```

- **Condition override precedence**: CLI flags > `[conditions]` > unspecified. Because normal completion of `verify` (`goal_met`) marks the goal as reached, an explicit `GoalMet` condition is not required.
- **At least one condition that is guaranteed to stop** (R3): `max_iterations` / `timeout_seconds` always fire. `token_budget` alone is valid only when tokens increase on every step (`cost_per_step > 0` is required for subprocess act; the default `0` does not fire, so it is rejected). `no_progress` alone is rejected because it depends on repeated identical actions and is not guaranteed. Any configuration that satisfies none of these requirements raises `ConfigError` (exit code 2).
- **Exit Codes**: `0` when the goal is reached (`result.succeeded`), `1` when the run stops due to a hard limit or similar condition, and `2` for configuration/usage errors (message on stderr).
- **The db stores multiple runs in one file** and identifies them by run-id. It can be specified with `--db`; the default is `[state].db`, or `loop-state.db` when that is absent.
- **Operations commands are read-only**: `loop-agent summary` / `dashboard` / `spikes` only read the run list, stop reasons, pending counts, event counts, step timeline, and spike candidates. They do not modify run state or decision logic.
- **The scaffold does not own policy**: `init-harness` only generates starting points for `harness.py` / `README.md`. The caller edits the prompt, verify command, caps, and gate targets after generation. Existing files are not overwritten without `--force`.
- **subprocess act/verify always have a finite timeout** (`[act]`/`[verify]`.`timeout_seconds` > loop `timeout_seconds` > default 3600s). Stop conditions are evaluated only at iteration boundaries and do not interrupt a running step, so this prevents an unbounded subprocess hang from disabling all caps.
- `--help` strings are ASCII-only (so they do not crash on cp932 consoles).

## Compatibility Contract

Starting with `1.0.0`, the following CLI surfaces are part of the stability contract:

- Subcommand names: `run` / `status` / `summary` / `dashboard` / `spikes` / `resume` / `logs` / `init-harness` / `install-skills`
- TOML sections and primary keys for `run`: `[loop]` / `[conditions]` / `[act]` / `[verify]` / `[state]`
- Exit codes: success `0`, stopped `1`, configuration/usage error `2`
- The basic behavior of reading `state.db` by run-id
- The fact that `summary` / `dashboard` / `spikes` are read-only

Human-facing display layout (spacing, column widths, and wording details) is best-effort and is not a stability contract for machine integration. Use state.db / JSONL events / the Python API when machine integration is required. Removing or changing the meaning of an existing option requires a major release. Backward-compatible option additions can be made in a minor / patch release.

## Related

- [../README.md](../README.md) — Entry point for the whole project
- [./seams.md](./seams.md) — Details of the five seams: gather / act / verify / conditions / gate
- [./adapters/README.md](./adapters/README.md) — The act adapter ecosystem for ClaudeCodeAct / CodexAct / custom adapters (ActHook Protocol)
- [./persistence-and-resume.md](./persistence-and-resume.md) — How state.db SoT, run-id, and resume work
- [./stability.md](./stability.md) — The `1.0.0` stability contract
