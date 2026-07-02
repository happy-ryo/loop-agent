# Self-Maintenance

This recipe explains how to make small consistency fixes to loop-agent itself and then verify those fixes with loop-agent. The goal is not to "leave everything to AI"; it is to **narrow the edit target, make `verify` machine-checkable, and record a run artifact**.

## Prose Intent

Example instruction for a coding agent:

> Use loop-agent to fix documentation consistency in loop-agent itself. Limit changes to the README, docs, and module docstrings. Align outdated PoC/MVP/Beta wording with the current 1.0.0 Stable state. Do not change code behavior. Verify by scanning target files for strings, checking that documentation links exist, and running `python -m pytest`. Keep edits small, and leave the run artifact in `loop-state.db`.

## Harness Shape

When LLM credentials are unavailable, define `act` as a deterministic local function and run it through the verification loop. A human or coding agent makes the edits; loop-agent provides bounded verification and an audit trail.

```python
from pathlib import Path
from loop_agent import ActOutcome, MaxIterations, VerifyOutcome, run_loop

TARGETS = [
    Path("README.md"),
    Path("docs/api-reference.md"),
    Path("docs/observability.md"),
    Path("src/loop_agent/__init__.py"),
    Path("src/loop_agent/loop.py"),
    Path("src/loop_agent/state.py"),
    Path("src/loop_agent/progress.py"),
]

def act(ctx):
    # Deterministic audit step. A real coding-agent harness can replace this
    # with ClaudeCodeAct/CodexAct after keeping the same verify.
    return ActOutcome(observation={"checked": [str(p) for p in TARGETS]})

def verify(outcome):
    stale = []
    for path in TARGETS:
        text = path.read_text(encoding="utf-8")
        if "PoC loop core" in text or "loop core (PoC)" in text:
            stale.append(str(path))
    return VerifyOutcome(goal_met=not stale, detail=f"stale={stale}")

result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(1)],
)
print(result.status, result.reason)
```

For real maintenance, pair the local verify with `DBProgressLog`:

```python
from loop_agent import DBProgressLog

db = DBProgressLog("loop-state.db", "self-maintenance-docs")
result = run_loop(
    act=act,
    verify=verify,
    conditions=[MaxIterations(1)],
    initial_state=db.state,
    on_step=db.on_step,
)
db.record_result(result)
```

## Ground Truth

Use checks that fail on the exact regressions you want to prevent:

- Stale wording: `rg "PoC loop core|loop core \(PoC\)" README.md docs src/loop_agent`
- Navigation: every new docs page is linked from README or a recipes index
- Behavior preservation: `python -m pytest`
- Packaging: `python scripts/verify_wheel_skill_bundle.py dist/*.whl` after building a wheel

The key rule is that `verify` determines success. `act` can be a coding agent, a deterministic function, or a no-op audit step, but it cannot declare the task complete by assertion alone.

## Audit

After the run:

```bash
loop-agent status self-maintenance-docs
loop-agent logs self-maintenance-docs
sqlite3 loop-state.db "SELECT iteration, goal_met, detail FROM step WHERE run_id='self-maintenance-docs';"
```

For changes that update `docs/` files mirrored into the bundled skill, run:

```bash
python scripts/sync_skill_references.py
python scripts/sync_skill_references.py --check
```

This keeps the shipped coding-agent reference bundle aligned with the docs that humans read.
