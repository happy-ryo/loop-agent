# Recipe: docstring / コメントの一括翻訳（動線 E）

N 個のソースファイルの日本語 docstring / コメントを英訳する（あるいは逆）ループです。**コードは一切変えない**のが制約。これは loop-agent の **Self-translation PoC**（loop-agent 自身のソースを loop-agent 自身で英訳した dogfood）を recipe 化したものです。

## prose intent（Claude Code にそのまま渡す）

> このリポジトリには loop-agent（薄いループエンジン。`act` に `ClaudeCodeAct` が使える）が入っている。
> **`src/loop_agent/` の docstring とコメントを英訳するループを組んで走らせて。** ただし:
> - コード・公開 API・型・テスト名・**文字列リテラル**は一切変えない（コメントと docstring だけ）。
> - gather: まだ日本語が残るファイルを 1 件ずつ（試行回数最小から = 公平 scheduling）。
> - act: `ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"])`。
> - verify: 3 段の機械チェック全通過で done（下記）。
> - conditions: `MaxIterations(20)` と `TokenBudget`(大きめ)。

## verify は 4 段の ground truth（コスト昇順）

ファイルが **done** になるのは 4 つ全部通ったときだけ:

1. **`parses_ok`** — `ast.parse` が成功する（壊れた編集を最安で弾く）。
2. **`japanese_cleared`** — *翻訳対象*（コメントと docstring）に日本語が残っていない。**非 docstring の文字列リテラルは対象外**（user 向けメッセージは日本語のままが正当なこともあるため。ここを弾くとゴールが到達不能になる）。コメントは `tokenize`、docstring は `ast` の docstring ノードで厳密にターゲティングする。
3. **`code_unchanged`** — **コードと非 docstring 文字列リテラルが変わっていない**（制約「コード/API/型/テスト名/文字列リテラルを変えない」の機械的強制）。HEAD と作業ツリーを両方パースし、**docstring の値だけを `""` に潰して**（= 翻訳を許す）`ast.dump` を比較。docstring 以外の文字列リテラル（エラーメッセージ・CLI 出力等）は**値ごと比較対象に残す**ので、その改変も差分として検出される。一致しなければ、識別子・シグネチャ・制御フロー・import・デコレータ・非 docstring 文字列のいずれかが変わったということなので **reject**。`tests_pass` だけでは「テストが見ていない挙動の破壊」を見逃すので、この段が no-code-change 制約の番人になる。
4. **`tests_pass`** — そのモジュール自身の `pytest` が通る（subprocess で再 import するので、挙動を壊す編集を検出）。

```python
import ast, tokenize, io, subprocess

def code_signature(source):
    """docstring の *値* だけを中立化した AST dump。docstring 以外の文字列リテラル
    （エラーメッセージ・CLI 出力等）は値ごと残すので、その改変も差分として検出される。
    docstring 翻訳だけは許し、コード構造・非 docstring 文字列の変更は弾く。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = node.body[0] if node.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                doc.value.value = ""               # docstring の値だけ潰す
    return ast.dump(tree)

def verify(outcome):
    f = current["file"]
    src = open(f, encoding="utf-8").read()
    try:
        tree = ast.parse(src)                      # 1. parses_ok
    except SyntaxError:
        return VerifyOutcome(goal_met=False, detail=f"{f}: parse error")
    if has_japanese_in_comments_or_docstrings(tree, src):   # 2. japanese_cleared（文字列リテラルは除外）
        return VerifyOutcome(goal_met=False, detail=f"{f}: japanese remains")
    head = subprocess.run(["git", "show", f"HEAD:{f}"],     # 3. code_unchanged（docstring 以外の差分を拒否）
                          capture_output=True, text=True).stdout
    if code_signature(head) != code_signature(src):
        return VerifyOutcome(goal_met=False, detail=f"{f}: code or non-docstring string changed")
    if subprocess.run(["pytest", test_for(f), "-q"]).returncode != 0:   # 4. tests_pass
        return VerifyOutcome(goal_met=False, detail=f"{f}: tests fail")
    done.add(f)
    return VerifyOutcome(goal_met=len(done) == len(FILES), detail=f"{f}: done")
```

## PoC の実走結果（embeddability の実証）

loop-agent 自身の 10 ファイル（合計 290 の日本語 hit）を `haiku` で英訳:

| | Run 1（no Reflexion） | Run 2（Reflexion） |
|---|---|---|
| 結果 | 10/10（`goal_met`） | 10/10（`converged`） |
| 内側反復 | 13 | 14（10 + 4） |
| Wall clock | 約 33 分 | 約 32 分 |
| token 計上 | 11.17M | 10.72M |
| 2 回目の試行が要ったファイル | 3 | 4 |
| 翻訳後の suite | 559 passed | 559 passed |

**挙動不変の機械的証明**: 上の verify 第 3 段（`code_unchanged`）と同じ手法を 10 ファイル全部に適用 — `HEAD` と作業ツリーをパースし、docstring の値だけを `""` に潰して `ast.dump` を比較 → 全て一致。識別子・シグネチャ・制御フロー・import・デコレータ・非 docstring 文字列リテラルは何も変わっていない。これは verify が各ファイルで通すゲートでもあるので、挙動を壊す編集は done にならない。`559 passed` と併せ、翻訳は**証明付きで挙動保存**（PoC の 10 ファイルは非 docstring 文字列リテラルに日本語を含まず、「文字列リテラルを触らない」制約は実際に保たれた）。

## 要点

- **文字列リテラルを翻訳対象から外す**のが肝。コメント / docstring だけをターゲティングし、`print()` 等の文字列は触らない。これを誤ると「翻訳しきれない正当な日本語」でゴールが到達不能になります。
- **token 計上の落とし穴**: `ClaudeCodeAct` を `Read`+`Edit` 付きで回すと `cache_read` の累積で `TokenBudget` が早発火します（PoC で発見）。`MaxIterations` で律速し、`TokenBudget` は大きめ（例 `20_000_000`）に。詳細は [quickstart のトラブルシュート](../quickstart.md#5-トラブルシュートよくある詰まり)。
- **Reflexion は要らないことが多い**: この翻訳タスクの初回失敗は *stochastic*（haiku が長いファイルで末尾コメントを 1 個落とす類）で、blind retry で resample すれば通ります。Run 1（no Reflexion）と Run 2（Reflexion）はほぼ同コストで同結果でした。Reflexion が勝つのは *systematic* な失敗のときだけ（→ [reflexion-when-to-use.md](../reflexion-when-to-use.md)）。
- **公平 scheduling**: ファイル単位の round-robin（試行回数最小から）で、難物 1 ファイルが全反復を独占しないように。
