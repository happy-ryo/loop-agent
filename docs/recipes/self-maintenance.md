# Self-Maintenance

loop-agent 自身の小さな整合性修正を、loop-agent で検証しながら進める recipe。目的は「AI に全部任せる」ことではなく、**編集対象を絞り、verify を機械判定にし、run artifact を残す**こと。

## Prose Intent

coding agent に渡す意図の例:

> loop-agent を使って loop-agent 自身のドキュメント整合性を直す。対象は README / docs / module docstring のみ。古い PoC/MVP 表現を 0.1.0 Beta の実態に合わせる。コード挙動は変えない。verify は、対象ファイルの文字列スキャン、docs link の存在確認、`python -m pytest` で行う。編集は小さく、run artifact は `loop-state.db` に残す。

## Harness Shape

LLM credentials が無い環境では、`act` を deterministic なローカル関数にして「検証ループ」として使う。編集は人間または coding agent が行い、loop-agent は bounded verify / audit trail を担当する。

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
        if "PoC loop core" in text or "ループコア（PoC）" in text:
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

Use checks that fail for the exact regression you care about:

- Stale wording: `rg "PoC loop core|ループコア（PoC）" README.md docs src/loop_agent`
- Navigation: every new docs page is linked from README or a recipes index
- Behavior preservation: `python -m pytest`
- Packaging: `python scripts/verify_wheel_skill_bundle.py dist/*.whl` after building a wheel

The key rule is that `verify` owns success. `act` can be a coding agent, a deterministic function, or a no-op audit step, but it does not get to mark the task done by assertion.

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
