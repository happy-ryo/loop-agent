# Optional Post-act Review

`review` は LLM-backed な `act` が作った成果物を、ground-truth `verify` の前に評価する任意シームです。

- `verify` は「ゴールが機械的に満たされたか」を判定する: pytest、build、AST、schema、regex など。
- `HumanGate` は不可逆操作を実行前に止める: commit、push、deploy、削除など。
- `review` は `act` 後の成果物を評価する: scope、API fit、保守性、移行リスク、ドキュメント整合性、ユーザー意図との一致。

テストが通っても変更が広すぎる、公開 API と合わない、意図から外れている、という失敗はあり得ます。`review` はその層を明示的に扱うための public API です。

## API

```python
from loop_agent import ReviewOutcome, run_loop


def review(outcome):
    if changed_too_much(outcome.observation):
        return ReviewOutcome(
            approved=False,
            severity="blocking",
            feedback="scope is too broad; keep the change inside docs/",
        )
    return ReviewOutcome(approved=True, feedback="scope looks acceptable")


result = run_loop(
    act=act,
    review=review,
    verify=verify,
    conditions=conditions,
)
```

`ReviewOutcome` は次のフィールドを持ちます。

| Field | Meaning |
|---|---|
| `approved: bool` | review が成果物を受け入れるか |
| `feedback: str = ""` | 次 iteration に渡す簡潔な指摘 |
| `severity: "info" | "warning" | "blocking" = "info"` | `blocking` かつ `approved=False` のときだけ verify をスキップ |

## 実行順序

```text
gather -> gate? -> act -> review? -> verify -> repeat
```

`review` を渡さなければ従来どおり `gather -> act -> verify` です。`review` が `approved=True`、または `severity` が `info` / `warning` の場合、`verify` は通常どおり実行されます。

`ReviewOutcome(approved=False, severity="blocking")` の場合、その iteration は `goal_met=False` の step として記録され、`verify` は実行されません。次の `gather` は `state.history[-1].detail` から feedback を読み、次の `act` prompt へ戻せます。

## State Representation

review 結果は既存の `StepRecord.detail` に JSON として保存されます。state.db を使う場合も同じ文字列が `step.detail` に永続化されるため、resume 後も feedback を読めます。

blocking review の detail 例:

```json
{"review":{"approved":false,"feedback":"scope is too broad","severity":"blocking"}}
```

review と verify の両方が走った detail 例:

```json
{"review":{"approved":true,"feedback":"scope ok","severity":"info"},"verify":{"detail":"pytest passed"}}
```

`verify` なしで review を使う設計にはしないでください。review は設計・意図・リスクの評価であり、成功判定はできる限り ground truth `verify` に残します。

## Retry Behavior

blocking review は failed step として扱われ、既存の stop conditions に従って retry されます。必ず `MaxIterations`、`TokenBudget`、`Timeout`、または `WorkListGather(max_attempts_per_item=...)` のような機械的上限と組み合わせてください。

multi-item ループでは、`done_when` で review approval と ground-truth verify の両方を要求します。

```python
import json


def done_when(_item, record):
    detail = json.loads(record.detail or "{}")
    return bool(
        detail.get("review", {}).get("approved")
        and detail.get("verify", {}).get("detail") == "pytest passed"
    )
```

`WorkListGather(max_attempts_per_item=...)` を使うと、1 件が review feedback を繰り返しても work list 全体を独占しません。

## HumanGate Boundary

`review` は不可逆操作の承認機構ではありません。commit、push、tag、publish、deploy、削除は `HumanGate` による実行前 gate、またはループ外の明示的な人間操作として扱ってください。

## Related

- [recipes/review-driven-loop.md](./recipes/review-driven-loop.md) - concrete harness pattern
- [seams.md](./seams.md) - seam overview
- [safety.md](./safety.md) - HumanGate and irreversible actions
- [api-surface.md](./api-surface.md) - criteria for adding public symbols
