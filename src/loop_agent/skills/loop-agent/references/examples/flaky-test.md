# Example: flaky test の安定化

**intent**: CI で時々落ちる test 群を 1 件ずつ根本修正し、「再現性のある合格」で締める。

これは domain への seam mapping の **発想スケッチ**であって、コピペ用テンプレートではない。

## intent -> seam 設計（なぜこの形か）

flaky 安定化という domain を 5 シームに落とすときの判断を、各シームごとに記す。

- **gather — 試行回数最小から 1 件選ぶ**: flaky test は複数ある。1 件の難物が
  `MaxIterations` を食い尽くすと他が永久に着手されない。試行回数が最小のものから
  選ぶ公平 scheduling にして枯渇を防ぐ。「次に何をやるか」を decide するのが gather。

- **act — 編集のみ（`ClaudeCodeAct(allowed_tools=["Read","Edit"])`）**: 根本原因を
  読んで直させる。ここが核心の安全設計 → **test の実行権限・commit/push 権限を act に
  渡さない**。act に `Bash` 無制限を渡すと (a) retry/sleep を仕込んで verify を騙す、
  (b) 内部で `git commit` を打って不可逆操作がループ外の人間ゲートをすり抜ける、の
  両方が起きうる。ツール権限で断つのが最も確実（`HumanGate` は act の subprocess が
  内部で打つ shell を観測できない）。

- **verify — N 回連続 pass を ground truth にする（最重要）**: flaky は *単発 pass では
  消えたか判別できない*。1 回通っても次に落ちるのが flaky の定義だからだ。だから
  verify の成功判定を「その test を 10 回連続で pass」という機械的・再現性ベースの
  ground truth にする。これが効くのは、act が retry/sleep で症状をマスクしても
  「再現性」を測る verify は通りにくいため — verify の設計自体が「根本修正以外は
  通さない」圧力になる。N は大きいほど確証が上がる（コストとのトレードオフ）。
  ここを LLM-as-judge（「直ったと思う？」）にすると即座に「成功したフリ」へ収束する。

- **conditions — `MaxIterations` + 大きめ `TokenBudget`**: 暴走の二重防御。難物が
  消えない場合の上限。`AnyOf` で OR 合成される。

- **gate — 原則なし。不可逆操作はループ外へ隔離**: ファイル編集は git で戻せるので
  ゲート不要。commit/push こそ不可逆だが、上記のとおり act の権限から外して
  *ループ収束後に人間が一括 commit* する設計にした。どうしても commit をループ内で
  ゲートしたいなら commit を「離散 action」に昇格させて `HumanGate` の `on=` で
  捕まえる（[safety](../safety.md) 参照）。

## スケッチ

```python
from loop_agent import run_loop, MaxIterations, TokenBudget, VerifyOutcome
from loop_agent.adapters import ClaudeCodeAct
import subprocess

FLAKY = ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]  # CI ログ等から抽出
done, attempts, current = set(), {t: 0 for t in FLAKY}, {"test": None}

def gather(state):
    rem = [t for t in FLAKY if t not in done]
    t = min(rem, key=lambda t: (attempts[t], FLAKY.index(t)))   # 公平 scheduling
    current["test"] = t
    attempts[t] += 1
    return {"prompt": f"Find and fix the root cause of flaky test `{t}`. "
                      f"Do NOT add retries or sleeps to mask it. Edit only. "
                      f"Do NOT commit or push -- a human commits after convergence.",
            "test": t}

def run_n_times(test, n=10):                                    # ground truth
    return all(subprocess.run(["pytest", test, "-q"]).returncode == 0 for _ in range(n))

def verify(outcome):
    t = current["test"]
    stable = (not outcome.observation.failed) and run_n_times(t)  # observation は ClaudeCodeResult
    if stable:
        done.add(t)
    return VerifyOutcome(goal_met=(len(done) == len(FLAKY)),
                         detail=f"{t}: {'stable' if stable else 'still flaky'}")

result = run_loop(
    act=ClaudeCodeAct(model="sonnet", allowed_tools=["Read", "Edit"], timeout=600),  # 編集のみ
    gather=gather, verify=verify,                              # 再現確認は verify が担う
    conditions=[MaxIterations(20), TokenBudget(20_000_000)],
)
print(result.status, result.reason, sorted(done))  # "goal_met"/"stopped", reason, 安定化済み
```

## adapt するときの勘所

- `ClaudeCodeAct` の `act` 戻り値は `ActOutcome`、その `observation` は失敗フラグ
  `failed` を持つ `ClaudeCodeResult`（成否や生出力を verify に渡すための構造体）。
- flaky の失敗が *systematic*（全 test が同じ「時刻依存の競合」等）なら、lesson を
  次の test に持ち越す Reflexion が効く。各 flaky が独立要因なら blind retry と差は
  出にくい（[reflexion-when-to-use](../reflexion-when-to-use.md)）。
- 自分の verify が「単発の成功」を見ていないか疑う。再現性が要る domain では
  「N 回連続」「複数 seed」など ground truth 側に再現性を組み込む。

---

これはコピペ用テンプレートではない。自分の domain に合わせて gather/act/verify を
設計し直すこと（[design-philosophy](../design-philosophy.md) / [seams.md](../seams.md) 参照）。
