# Verifier Helpers

`verify` is still caller-owned policy. These helpers only cover cases where the
truth source is already mechanical: a command exit code, pytest, or a regex over
an adapter result. They are intentionally small so they do not turn loop-agent
into an LLM judge.

## CommandVerifier

Use `CommandVerifier` when an existing command is the oracle: tests, lint,
compile, schema check, or a smoke probe.

```python
from loop_agent import CommandVerifier, MaxIterations, run_loop

verify = CommandVerifier(["python", "-m", "pytest", "tests/test_loop.py", "-q"], timeout=60)
result = run_loop(act=act, verify=verify, conditions=[MaxIterations(5)])
```

The command is run with `stdin=subprocess.DEVNULL`, captured output, and no
exception on non-zero exit. Non-zero exit becomes `VerifyOutcome(goal_met=False)`
with stdout/stderr in `detail`.

## PytestVerifier

`PytestVerifier` is a thin convenience wrapper around `python -m pytest`.

```python
from loop_agent import PytestVerifier

verify = PytestVerifier(["tests/test_verifiers.py", "-q"], timeout=30)
```

Keep it focused inside loops. A full suite is a good release gate, but a narrow
pytest target usually gives a faster and sharper loop verifier.

## RegexVerifier

`RegexVerifier` checks `outcome.observation.text` when present, falling back to
`str(outcome.observation)`. It is useful for adapter smoke checks, not for
semantic correctness.

```python
from loop_agent import RegexVerifier

verify = RegexVerifier(r"\bDONE\b")
```

## Non-goals

These helpers do not judge vague quality. If correctness cannot be grounded in a
command, test, parser, AST check, probe, or other deterministic oracle, write a
domain verifier first and pass it as the `verify` seam.
