# 例: 挙動不変リファクタ

> intent: N 個のモジュールを「挙動を変えずに」整理する（重複削除・命名統一・分割）。難所は verify を「挙動が変わっていない」で sharp に書くこと。

## intent -> seam 設計（なぜこの 5 シームになるか）

| seam | この domain での選択 | なぜ |
| --- | --- | --- |
| gather | 対象モジュールを 1 件ずつ、試行回数が少ない順に出す | 1 モジュール / 1 反復にスコープを絞ると act が局所化し、verify の失敗を 1 モジュールに帰属できる。`WorkListGather(strategy="fewest_attempts")` が「1 件が反復を独占して他を starve させない」公平 scheduling を担う |
| act | `ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"])` で 1 モジュールを整理 | リファクタは Read/Edit で足り、不可逆操作（commit/push）を act に渡さない。編集自体は git で戻せる |
| **verify** | **既存テスト全 pass（ground truth）+ public シグネチャ不変** | ここが核。下記参照 |
| conditions | `MaxIterations(15)` + `TokenBudget`（大きめ） | 機械的な hard cap。per-item 上限は `WorkListGather(max_attempts_per_item=...)` 側で持たせる |
| gate | 原則なし。commit/push はループ外に隔離 | 不可逆操作は収束後に人間が行う（後述の落とし穴） |

## verify の ground truth: 「挙動不変」をどう機械判定するか

リファクタの verify は **既存テストスイートが contract**。リファクタ前に green だったテストが後も全 green なら、テストがカバーする挙動は保存されている。
- カバレッジが薄いモジュールは、**回す前に特性化テスト（characterization test）を足す**のが規律（先にテスト → リファクタ → 全 pass）。テストの網羅度がそのまま安全度になる。
- **public シグネチャの不変チェック**を足すと、「テストが見ていない外形の破壊」を安価に検出できる。

```python
import ast
import subprocess

from loop_agent import run_loop, VerifyOutcome, MaxIterations, TokenBudget
from loop_agent.adapters import ClaudeCodeAct
from loop_agent.discovery import WorkListGather, WorkListDrained

MODULES = ["src/foo/a.py", "src/foo/b.py", "src/foo/c.py"]

def public_signatures(path: str):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return sorted(
        (n.name, len(n.args.args)) for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    )

baseline = {m: public_signatures(m) for m in MODULES}  # 回す前に固定
current = {}  # gather がこの反復の対象を書き込む（ClaudeCodeAct の observation は
              # モデル出力テキストで gather ctx を含まないため、対象は別途共有する）

def verify(outcome):
    m = current["module"]
    # 1. public シグネチャ不変（外形の contract）
    if public_signatures(m) != baseline[m]:
        return VerifyOutcome(goal_met=False, detail=f"{m}: public signature changed")
    # 2. 既存テスト全 pass（挙動の ground truth）
    if subprocess.run(["pytest", "-q"]).returncode != 0:
        return VerifyOutcome(goal_met=False, detail=f"{m}: suite red")
    return VerifyOutcome(goal_met=True, detail=f"{m}: refactored")

gather = WorkListGather(
    MODULES,
    strategy="fewest_attempts",
    build_ctx=lambda item, attempt, state: current.update(module=item.id) or {
        "prompt": f"{item.id} を挙動不変でリファクタして（Read/Edit のみ、commit はしない）",
    },
)

result = run_loop(
    gather=gather,
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"]),
    verify=verify,
    conditions=[
        WorkListDrained(gather),   # gatherer を渡す。全 item done で成功停止
        MaxIterations(15),
        TokenBudget(2_000_000),
    ],
)
print(result.status, result.succeeded, result.reason, result.iterations)
```

> より厳密には「文字列定数を潰した `ast.dump` 比較」で *純粋に内部構造だけ* 変わったことまで検証できるが、リファクタは構造を変えるのが目的なので、通常は **テスト contract + public シグネチャ**で十分。

## hard-won lessons（この domain の落とし穴）

- **commit / push はループ外に隔離する。** 編集は git で戻せるのでループは編集だけにし、不可逆な commit/push は収束後に人間が行う。`HumanGate` は **gather が返すループの離散 action** を審査するもので、`act` の subprocess が内部で打つ `git commit` は見えない。commit をゲートしたいなら、commit 自体をループの離散 action にすること。
- **Reflexion が効きやすい。** リファクタの失敗は systematic（「この import 順序を毎回壊す」等）になりがちで、同じ誤りが反復するなら lesson が次モジュールに効く（translation/flaky より Reflexion 向き）。
- **verify は全 suite を回す。** act は 1 モジュールだけ触るが、verify は局所変更が全体を壊していないかを見るため suite 全体を回す。

---

これはコピペ用テンプレートではない。自分の domain に合わせて gather / act / verify を設計し直すこと（[design-philosophy](../design-philosophy.md) / [seams.md](../seams.md) 参照）。
