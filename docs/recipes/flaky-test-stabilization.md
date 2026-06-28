# Recipe: flaky test の安定化（動線 E）

CI で時々落ちるテスト群を、loop-agent + Claude Code で 1 件ずつ根本修正し、**再現性のある合格**で締めるループです。

## prose intent（Claude Code にそのまま渡す）

> このリポジトリには loop-agent（`gather → act → verify → repeat` の薄いループエンジン。`act` に `loop_agent.adapters.ClaudeCodeAct` が使える）が入っている。
> **CI で flaky なテストを安定化するループを組んで走らせて。**
> - gather: 安定化対象の flaky test を 1 件ずつ選ぶ（試行回数最小から = 公平 scheduling）。
> - act: `ClaudeCodeAct(model="sonnet", allowed_tools=["Read","Edit"])` で根本原因を読んで直す（テストの実行は verify が持つ）。
> - verify: 修正後にその test を **10 回連続 pass** で done（1 回でも落ちたら未達）。
> - conditions: `MaxIterations(20)` と `TokenBudget`(大きめ)。
> - 不可逆操作: act には commit / push をさせない（編集のみ）。修正の commit は収束後に人間が確認して行う。

## 組み上がる harness（おおよその姿）

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome, ActOutcome
from loop_agent.adapters import ClaudeCodeAct
import subprocess

FLAKY = ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]   # CI ログ等から抽出
done, attempts = set(), {t: 0 for t in FLAKY}
current = {"test": None}

def gather(state):
    rem = [t for t in FLAKY if t not in done]
    t = min(rem, key=lambda t: (attempts[t], FLAKY.index(t)))    # 公平 scheduling
    current["test"] = t
    attempts[t] += 1
    return {"prompt": f"Find and fix the root cause of the flaky test `{t}`. "
                      f"Do not add retries or sleeps to mask it. Edit the code/test as needed. "
                      f"Do NOT commit or push -- a human commits after the loop converges.",
            "test": t}

def run_n_times(test, n=10):
    for _ in range(n):
        if subprocess.run(["pytest", test, "-q"]).returncode != 0:
            return False
    return True

def verify(outcome):
    t = current["test"]
    stable = (not outcome.observation.failed) and run_n_times(t, n=10)   # ground truth
    if stable:
        done.add(t)
    all_done = len(done) == len(FLAKY)
    return VerifyOutcome(goal_met=all_done, detail=f"{t}: {'stable' if stable else 'still flaky'}")

result = run_loop(
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"], timeout=600),   # 編集のみ
    gather=gather, verify=verify,                                                      # 再現確認は verify が担う
    conditions=[MaxIterations(20), TokenBudget(20_000_000)],
)
print(result.status, result.reason, sorted(done))
```

## 要点

- **verify は「N 回連続 pass」**。flaky は単発 pass では消えたか判別できないので、連続合格を ground truth にします。N は大きいほど確証が上がる（コストとトレードオフ）。
- **retry / sleep でのごまかしを禁じる**プロンプトにする。act にマスキングを許すと verify は通ってしまうが flaky は残ります。verify が「再現性」を測っているので、根本修正以外は通りにくい設計です。
- **公平 scheduling 必須**。1 件の難物が `MaxIterations` を食い尽くさないよう、試行回数最小から選びます。
- **Reflexion は効くか?** flaky の失敗が *systematic*（例: 全テストが同じ「時刻依存の競合」パターン）なら、lesson（「時刻は freeze しろ」）が次 test に効くので Reflexion 向き。各 flaky が無関係な独立要因なら blind retry とほぼ差は出ません。判断は [reflexion-when-to-use.md](../reflexion-when-to-use.md)。
- **commit / push はループ外に隔離する**。修正のファイル編集は git で戻せる。**再現確認（テストの実行）は `verify` が持ち、`act` には commit / push できる権限（無制限 `Bash` 等）を渡さない**（`HumanGate` は `act` の subprocess が内部で打つ `git commit` を見られないため、ツール権限で断つのが確実）。不可逆な commit / push は収束後に人間が行う。どうしても act 内で shell が要るなら test コマンドに絞り、commit 系は与えない。commit を本当にゲートしたいなら commit を**ループの離散 action**にする — [README の限定人間ゲート節](../../README.md#限定人間ゲート不可逆操作のみ-approveeditrejectrespond)。
