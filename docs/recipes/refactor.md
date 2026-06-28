# Recipe: 挙動不変リファクタ（動線 E）

N 個のモジュールを「挙動を変えずに」整理する（重複削除・命名統一・分割等）ループです。難所は **verify を「挙動が変わっていない」で sharp に書く**こと。

## prose intent（Claude Code にそのまま渡す）

> このリポジトリには loop-agent（薄いループエンジン。`act` に `ClaudeCodeAct` が使える）が入っている。
> **`src/foo/` の各モジュールを挙動不変でリファクタするループを組んで走らせて。**
> - gather: 対象モジュールを 1 件ずつ（試行回数最小から = 公平 scheduling）。
> - act: `ClaudeCodeAct(model="sonnet", allowed_tools=["Read","Edit"])` で 1 モジュールを整理。
> - verify: **既存テスト全 pass**（公開挙動の ground truth）+ public シグネチャ不変。
> - conditions: `MaxIterations(15)` と `TokenBudget`(大きめ)。
> - 不可逆操作: act には commit / push をさせない（編集のみ）。commit は収束後に人間が確認して行う。

## verify の ground truth: 「挙動が変わっていない」をどう機械判定するか

リファクタの verify は **既存テストスイートが contract**。リファクタ前に green だったテストがリファクタ後も全 green なら、テストがカバーする挙動は保存されています。カバレッジが薄いモジュールは、**リファクタ前に特性化テスト（characterization test）を足す**のが規律です（テストを先に書く → リファクタ → 全 pass）。

```python
import subprocess, ast

def public_signatures(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return sorted((n.name, len(n.args.args)) for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and not n.name.startswith("_"))

def verify(outcome):
    m = current["module"]
    # 1. public シグネチャ不変（外形の contract）
    if public_signatures(m) != baseline_sigs[m]:
        return VerifyOutcome(goal_met=False, detail=f"{m}: public signature changed")
    # 2. 既存テスト全 pass（挙動の ground truth）
    if subprocess.run(["pytest", "-q"]).returncode != 0:
        return VerifyOutcome(goal_met=False, detail=f"{m}: suite red")
    done.add(m)
    return VerifyOutcome(goal_met=len(done) == len(MODULES), detail=f"{m}: refactored")
```

より厳密にやるなら、翻訳 recipe と同じ「文字列定数を潰した `ast.dump` 比較」で *純粋に内部構造だけ* が変わったことまで検証できますが、リファクタは構造を変えるのが目的なので、通常は **テスト contract + public シグネチャ**で十分です。

## 要点

- **テストが contract**。verify を「既存テスト全 pass」に置く以上、テストの網羅度がそのまま安全度。薄い箇所は characterization test を先に足してから回す。
- **public シグネチャの不変チェック**を verify に足すと、「テストが見ていない外形の破壊」を安価に検出できます。
- **スコープを 1 モジュール / 1 反復に絞る**。act に「このモジュールだけ整理して」と渡し、verify は全 suite を回す（局所変更が全体を壊していないか）。
- **Reflexion が効く可能性が高いタスク**: リファクタの失敗は *systematic* になりがち（例: 「この import 順序を毎回壊す」「同じ抽象化ミスを繰り返す」）。同じ誤りが反復するなら lesson が次モジュールに効くので、translation/flaky より Reflexion 向き。判断は [reflexion-when-to-use.md](../reflexion-when-to-use.md)。
- **commit / push はループ外に隔離する**。編集自体は git で戻せるので、ループは編集だけ。不可逆な commit / push は収束後に人間が行う。`HumanGate` は `gather` が返すループの離散 action を審査するもので、`act` の subprocess が内部で打つ `git commit` は見えない（commit をゲートしたいなら commit をループの離散 action にする — [README の限定人間ゲート節](../../README.md#限定人間ゲート不可逆操作のみ-approveeditrejectrespond)）。
