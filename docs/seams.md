# シーム詳細 — gather / act / review / verify / conditions / gate

loop-agent が「持つ」のはオーケストレーション本体だけで、policy は全部シームに注入する。このページは 5 つの必須シームと任意の `review` シームの型・契約、`run_loop` での具体的な書き方（基本利用・暴走防止・二重終了条件・検証駆動デモ）を 1 箇所にまとめた canonical な解説。

## シーム一覧

ループが「持つ」のはオーケストレーション本体だけ。policy はこれらのシームに注入する。`review` は post-act artifact review が必要な loop だけで使う任意シーム:

| シーム | 型 | あなたが決めること |
|---|---|---|
| `gather` | `Callable[[state], ctx]` | 次に何をやるか（候補選定・triage・キュー戦略） |
| `act` | `Callable[[ctx], ActOutcome]` | どう実行するか（モデル選択・LLM provider・subprocess・ローカル fn） |
| `review` | `Callable[[ActOutcome], ReviewOutcome]`（任意） | `act` が作った成果物を受け入れるか（scope / API fit / intent match）。blocking の場合は verify をスキップして feedback を次 iteration へ残す |
| `verify` | `Callable[[ActOutcome], VerifyOutcome]` | 何を「成功」とするか（pytest / AST / regex / 何でも。技術的には何でも差せるが成功判定は **ground truth 推奨**） |
| `conditions` | `list[StopCondition]`（`MaxIterations` 等の stop 条件。`AnyOf` で OR 合成） | いつ止めるか（回数 / 予算 / 目標 / 時間） |
| `gate` | `ActionGate`（`HumanGate` 等。`review(context, state)` 実装。対象選定は `on=Callable[[action], bool]`） | 何に人間承認を要求するか（commit / push / 任意） |

> **verify は ground truth で書く（推奨）**: 何でも差せるのがシームの本質だが、成功判定を LLM-as-judge に委ねるとループは「成功したフリ」に収束しやすい（report.md R1）。LLM-backed な設計・scope 判断は `review` に寄せ、成功判定は pytest の exit-code / AST / 文字列スキャンなど機械的に判定できるものを使う。具体例は [recipes/](./recipes/)。

```python
while not goal_met and conditions_ok:
    ctx = gather(state)        # 何を      (gather)
    outcome = act(ctx)         # どう実行  (act)
    r = review(outcome)        # 成果物評価  (review, 任意)
    v = verify(outcome)        # 何が成功  (verify)
    state.update(v)
```

このループ本体だけが loop-agent。必須シームを書き、必要なときだけ `review` を足せば、それがあなたの domain の loop になる。

`act` シームには、`ClaudeCodeAct` / `CodexAct` / 自作 adapter（`ActHook` Protocol）が first-class な act adapter として既に揃っている。`ActHook` に準拠した callable であれば何でも act シームに差し込めるので、モデル・LLM provider・subprocess・ローカル関数を自由に選べる。adapter の書き方は [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) を参照。

## 使い方

`act`（行動）と `verify`（検証 = ground truth）を渡し、終了条件を合成して `run_loop` に渡すだけ:

```python
from loop_agent import run_loop, ActOutcome, ReviewOutcome, VerifyOutcome, MaxIterations, TokenBudget, Timeout

state = {"n": 0}

def act(ctx):
    """1 ステップ分の行動。observation と消費トークンを返す。"""
    state["n"] += 1
    return ActOutcome(observation=f"did work #{state['n']}", tokens=10)

def review(outcome):
    """任意: post-act artifact review。"""
    return ReviewOutcome(approved=True, feedback="scope ok")


def verify(outcome):
    """ground truth 検証。goal_met=True でループは自然終了する。"""
    done = state["n"] >= 3
    return VerifyOutcome(goal_met=done, detail="converged" if done else "")

result = run_loop(
    act=act,
    review=review,
    verify=verify,
    conditions=[MaxIterations(5), TokenBudget(1000), Timeout(30.0)],  # OR 評価
)

print(result.status)   # "goal_met" / "stopped"
print(result.reason)   # "goal met" / "reached max iterations (5/5)" など
print(result.iterations, result.tokens_used)
```

ゴール未達でも上限で必ず止まる（AutoGPT 的な暴走を防ぐ）:

```python
result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # 決して達成しない
    conditions=[MaxIterations(2)],
)
assert result.status == "stopped"
assert result.stop.name == "max_iterations"   # 発火した条件
print(result.reason)                          # "reached max iterations (2/2)"
```

## 二重終了条件（GoalMet / NoProgress）

機械的上限と同じ `AnyOf` 合成に**意味的 stop** を載せられる。`GoalMet` は検証可能ゴール
（テスト / lint / rubric の callable）が満たされたら**成功**として停止し、`NoProgress` は同じ
アクションが反復されて進捗が出ない場合に**打ち切り**として停止する。どちらも発火は既存の
`StopTrigger` 形式（`stop.name` = `"goal_met"` / `"no_progress"`）で、宣言順 OR で機械的上限と
矛盾なく共存する:

```python
from loop_agent import run_loop, GoalMet, GoalCheck, NoProgress, MaxIterations

result = run_loop(
    act=act,
    verify=lambda o: VerifyOutcome(goal_met=False),  # verify フックは使わず条件側で判定
    conditions=[
        GoalMet(lambda state: GoalCheck(met=run_tests() == 0, detail="suite green")),
        NoProgress(window=5, repeat=3),   # 直近 5 ステップで同じアクションが 3 回 → 打ち切り
        MaxIterations(50),                # 機械的バックストップ（R3）
    ],
)
# 成功判定は result.succeeded（verify フック自然終了と GoalMet 条件の両方を吸収）。
# スタックなら stop.name == "no_progress"、どちらも起きなければ "max_iterations" が必ず止める。
```

> `result.goal_met` は **verify フックによる自然終了のみ** を表す（`status == "goal_met"`）。
> `GoalMet` 条件が発火した成功は `status == "stopped"` / `stop.name == "goal_met"` で返るため
> `goal_met` は False のまま。チャネルを問わず成功を判定したい場合は `result.succeeded` を使う。

## 検証駆動デモ（sandbox テストが green になるまで回す）

ループコアを **実コード** に当てた具体デモ。一時 sandbox にわざと壊した関数とその pytest を書き出し、`act`（修正候補を当てる）→ `verify`（**実際の pytest の exit-code** を ground truth に判定）を **テストが green になるまで** 反復する。`goal_met=True`（exit-code 0）でループは**自然終了**し、直らないシナリオでも `MaxIterations` 等の上限で必ず止まる（暴走防止）。LLM judge には頼らない（report.md R1）。

```bash
python3 examples/verify_driven_demo.py
# iter 0: applied candidate #0 -> verify=red   (red (exit=1))
# iter 1: applied candidate #1 -> verify=red   (red (exit=1))
# iter 2: applied candidate #2 -> verify=GREEN (green)
# status: goal_met / iterations: 3 / exit-codes: [1, 1, 0]
```

再利用フックは `loop_agent.demo`（`CandidateApplier` = act / `ExitCodeVerifier` = verify / `attempt_index` = gather）。この実走そのものを `tests/test_verify_demo.py` が pytest で再現・検証する（出荷物 == 検証対象）。

## 関連

- [../README.md](../README.md) — 入口（positioning / シーム概要 / 動線サマリ）
- [adapters/writing-an-adapter.md](./adapters/writing-an-adapter.md) — `ActHook` Protocol で act adapter を書く
- [review.md](./review.md) — optional post-act review の API と retry/state の扱い
- [review.md](./review.md) — optional post-act review の API と retry/state の扱い
- [recipes/](./recipes/) — ground truth verify の具体例集
- [safety.md](./safety.md) — `gate` シームと HumanGate の射程
