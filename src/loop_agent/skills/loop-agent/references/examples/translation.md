# 例: docstring / コメントの一括翻訳（ground-truth verify の設計例）

**intent**: N 個のソースの日本語 docstring / コメントを英訳する。ただし**コードは一切変えない**。

これは loop-agent 自身のソースを loop-agent 自身で英訳した Self-translation PoC（dogfood）を蒸留したもの。コピペ用テンプレートではなく、「制約のあるバルク編集タスクで 5 シームをどう設計するか」、とくに **verify を ground truth でどう組むか** の思考をなぞるためのスケッチ。

## なぜこの 5 シームになるのか

| シーム | この domain での選択 | 理由 |
|---|---|---|
| `gather` | まだ日本語が残るファイルを 1 件ずつ、試行回数最小から（公平 scheduling） | 難物 1 ファイルが全反復を独占して他を starve させないため。`WorkListGather(strategy="fewest_attempts", max_attempts_per_item=...)` が core。 |
| `act` | `ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"])` | 機械的な翻訳作業で安いモデルで足りる。Read+Edit だけ許してファイル外を触らせない。 |
| `verify` | **4 段の機械チェック全通過で done**（下記） | LLM-as-judge に「翻訳できた？」を聞くと「できたフリ」に収束する。成功は ground truth で測る。 |
| `conditions` | `MaxIterations(20)` + `TokenBudget(大きめ)` | 長時間 run の確実な律速。stochastic な失敗の blind retry を許す回数を確保しつつ暴走を bound。 |
| `gate` | なし | 人間承認の対象になる破壊的アクション（push 等）がない。 |

## verify が肝 — ground truth をコスト昇順で 4 段

ファイルが **done** になるのは 4 つ全部通ったときだけ。安い門から並べて壊れた編集を早く弾く:

1. **`parses_ok`** — `ast.parse` 成功（壊れた編集を最安で弾く）。
2. **`japanese_cleared`** — *翻訳対象*（コメントと docstring）に日本語が残っていない。**非 docstring の文字列リテラルは対象外**。ここを弾くと「user 向けに日本語が正当なメッセージ」でゴールが到達不能になる。コメントは `tokenize`、docstring は `ast` の docstring ノードで厳密にターゲティング。
3. **`code_unchanged`** — **コードと非 docstring 文字列リテラルが変わっていない**。HEAD と作業ツリーを両方パースし、**docstring の値だけを `""` に潰して** `ast.dump` を比較。docstring 以外（識別子・シグネチャ・制御フロー・import・デコレータ・エラーメッセージ等の文字列リテラル）はすべて差分として検出 → reject。`tests_pass` だけでは「テストが見ていない挙動の破壊」を見逃すので、この段が no-code-change 制約の番人になる。
4. **`tests_pass`** — そのモジュール自身の `pytest` が通る（subprocess で再 import するので挙動破壊を検出）。

```python
import ast, subprocess
from loop_agent import VerifyOutcome

def code_signature(source: str) -> str:
    """docstring の *値* だけを中立化した AST dump。docstring 以外の文字列リテラルは
    値ごと残すので、その改変も差分として検出される。docstring 翻訳だけ許す。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = node.body[0] if node.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                doc.value.value = ""          # docstring の値だけ潰す
    return ast.dump(tree)

def verify(outcome):                          # outcome: ActOutcome
    f = current_file()                         # gather が選んだファイル
    src = open(f, encoding="utf-8").read()
    try:
        ast.parse(src)                                              # 1. parses_ok
    except SyntaxError:
        return VerifyOutcome(goal_met=False, detail=f"{f}: parse error")
    if has_japanese_in_comments_or_docstrings(src):                # 2. japanese_cleared
        return VerifyOutcome(goal_met=False, detail=f"{f}: japanese remains")
    head = subprocess.run(["git", "show", f"HEAD:{f}"],            # 3. code_unchanged
                          capture_output=True, text=True).stdout
    if code_signature(head) != code_signature(src):
        return VerifyOutcome(goal_met=False, detail=f"{f}: code or non-docstring string changed")
    if subprocess.run(["pytest", test_for(f), "-q"]).returncode != 0:  # 4. tests_pass
        return VerifyOutcome(goal_met=False, detail=f"{f}: tests fail")
    mark_done(f)
    return VerifyOutcome(goal_met=all_files_done(), detail=f"{f}: done")
```

`run_loop(gather=WorkListGather(files, strategy="fewest_attempts", max_attempts_per_item=2),
act=ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"]), verify=verify,
conditions=[MaxIterations(20), TokenBudget(...)])` で駆動する。

## この domain で効いた教訓

- **文字列リテラルを翻訳対象から外す**のが肝。print() 等のユーザー向け文字列まで翻訳しようとすると「正当な日本語」でゴールが到達不能になる。verify 2 段目（対象限定）と 3 段目（対象外を凍結）はこの制約の表裏。
- **Reflexion は要らないことが多い**。初回失敗が *stochastic*（haiku が長いファイルで末尾コメントを 1 個落とす類）なら blind retry の resample で通る。`max_attempts_per_item` で retry 回数を確保すれば足りる。Reflexion が勝つのは *systematic* な失敗のときだけ（[reflexion-when-to-use](../reflexion-when-to-use.md) 参照）。
- **挙動不変の機械的証明**: verify 3 段目（`code_unchanged`）を全ファイルに適用すれば、docstring 以外は何も変わっていないことが `ast.dump` 一致で証明される。`pytest 全 pass` と併せ、翻訳は証明付きで挙動保存。

---

これはコピペ用テンプレートではない。自分の domain に合わせて gather / act / verify を設計し直すこと（[design-philosophy](../design-philosophy.md) / [seams.md](../seams.md) 参照）。
