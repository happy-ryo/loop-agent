# Review-driven Loop

LLM-backed な `act` がファイルを編集する場合に、テストだけでは scope、設計 fit、release risk を判断しきれないときの recipe です。

安定 API として `review=` を明示的に使います。`review` に渡す callable は `ReviewHook` で、`ReviewOutcome` を返します。

```text
gather finding -> act fix -> review artifact -> verify ground truth -> repeat
```

## Prose Intent

coding agent へ渡す自然言語指示の例:

> loop-agent で、小さな LLM-backed code-editing task 用の harness を作ってください。`act` はファイル編集までに限定し、commit/push/deploy はループ外に置きます。各 edit の後に `review` を実行し、scope、公開 API との整合、タスク意図との一致を確認してください。blocking review の場合は `verify` を走らせず、review feedback を次の iteration に渡してください。review が通ったら pytest による ground-truth verification を実行します。multi-item では `WorkListDrained` と `MaxIterations` を使い、item ごとの試行回数を cap してください。

## Harness Shape

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from loop_agent import ActOutcome, MaxIterations, ReviewOutcome, VerifyOutcome, WorkItem
from loop_agent import WorkListDrained, WorkListGather, run_loop


items = [
    WorkItem(id="api-contract", payload={"target": "src/loop_agent/__init__.py"}),
    WorkItem(id="docs", payload={"target": "docs/api-reference.md"}),
]


def _detail(record):
    try:
        return json.loads(record.detail or "{}")
    except json.JSONDecodeError:
        return {}


def done_when(_item, record):
    detail = _detail(record)
    return bool(detail.get("review", {}).get("approved", True) and record.detail == "pytest passed")


gather = WorkListGather(
    items,
    strategy="fewest_attempts",
    max_attempts_per_item=3,
    done_when=done_when,
)


def act(ctx):
    target = ctx["payload"]["target"]
    # 実 harness では ClaudeCodeAct/CodexAct を呼び、直前の
    # state.history[-1].detail にある review feedback を prompt に含める。
    return ActOutcome(observation={"target": target, "changed": True})


def review_artifact(outcome):
    target = Path(outcome.observation["target"])
    if not target.exists():
        return ReviewOutcome(False, f"missing target {target}", "blocking")
    return ReviewOutcome(True, "scope and target look acceptable", "info")


def verify(outcome):
    proc = subprocess.run(
        ["python", "-m", "pytest", "tests/test_stability_contract.py", "-q"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    detail = "pytest passed" if proc.returncode == 0 else f"pytest failed: {proc.returncode}"
    return VerifyOutcome(goal_met=proc.returncode == 0, detail=detail)


result = run_loop(
    gather=gather,
    act=act,
    review=review_artifact,
    verify=verify,
    conditions=[WorkListDrained(gather), MaxIterations(10)],
)
```


## Structured LLM Review

`review` を LLM に任せる場合は、自然文ではなく JSON decision を要求します。`No findings` や `LGTM` の文字列一致で承認すると、review agent の文体が少し変わっただけで loop が誤停止したり、逆に曖昧な返答を成功扱いしたりします。

```python
import json


def review_artifact(outcome):
    prompt = f"""
Review this change. Return JSON only:
{{"decision":"approved|blocking","findings":["..."],"residual_risk":"..."}}

Criteria:
- scope matches the requested files
- public API compatibility is preserved
- docs and tests are consistent
- no irreversible operation was performed

Artifact summary:
{outcome.observation}
"""
    review_result = review_act({"prompt": prompt}).observation
    try:
        decision = json.loads(review_result.text)
    except json.JSONDecodeError:
        return ReviewOutcome(False, "review did not return JSON", "blocking")

    if not isinstance(decision, dict):
        return ReviewOutcome(False, "review JSON was not an object", "blocking")

    findings = decision.get("findings") or []
    if isinstance(findings, str):
        findings = [findings]
    if not isinstance(findings, list):
        findings = ["review findings had an invalid shape"]
    residual_risk = decision.get("residual_risk", "")
    if not isinstance(residual_risk, str):
        residual_risk = ""

    if decision.get("decision") != "approved":
        feedback = findings or ["review did not approve"]
        return ReviewOutcome(False, "; ".join(map(str, feedback)), "blocking")
    return ReviewOutcome(True, residual_risk)
```

Dogfood harness では、review の JSON decision に加えて「実 adapter を使った」ことも検証します。たとえば Codex を `act` と `review` の両方に使うなら、act 側は `verify` で `CodexResult.command` が `codex exec` を含むことと `tokens > 0` を確認します。review 側は `review_artifact` の中で `review_act(...)` の結果をすぐ確認するか、command / tokens を外部記録に保存してから `verify` で読むようにします。`VerifyHook` が直接受け取るのは act の `ActOutcome` だけなので、review 側の adapter 結果を暗黙に参照できるとは考えないでください。これにより、手編集を後から `ActOutcome(tokens=0)` として記録するだけの post-hoc recorder を dogfood と誤認しません。

## Feedback Representation

blocking review の場合、`ReviewOutcome` は `StepRecord.detail` の JSON に入ります。review が blocking でない場合は `verify` が走り、detail は従来どおり `verify.detail` の生文字列です。

```json
{"review":{"approved":false,"feedback":"missing target docs/api-reference.md","severity":"blocking"}}
```

```text
pytest passed
```

大きな diff 全体を detail に入れず、finding summary、severity、file path だけを保存してください。次の `act` は repository を直接読めます。

## WorkListGather Interaction

`done_when` は review approval と ground-truth verify の両方を要求します。`max_attempts_per_item` を設定し、1 件の noisy な review feedback が loop budget 全体を消費しないようにします。

## HumanGate Boundary

`review` は post-act artifact の評価です。不可逆操作は `HumanGate` の責務です。commit、push、tag、publish、deploy はループ外に置くか、明示的な gated action にしてください。
