# Recipe: docstring / コメントの一括翻訳（動線 E）

N 個のソースファイルの日本語 docstring / コメントを英訳する（あるいは逆）ループです。**コードは一切変えない**のが制約。これは loop-agent の **Self-translation PoC**（loop-agent 自身のソースを loop-agent 自身で英訳した dogfood）を recipe 化したものです。

## prose intent（Claude Code にそのまま渡す）

> このリポジトリには loop-agent（薄いループエンジン。`act` に `ClaudeCodeAct` が使える）が入っている。
> **`src/loop_agent/` の docstring とコメントを英訳するループを組んで走らせて。** ただし:
> - コード・公開 API・型・テスト名・**文字列リテラル**は一切変えない（コメントと docstring だけ）。
> - gather: まだ日本語が残るファイルを 1 件ずつ（試行回数最小から = 公平 scheduling）。
> - act: `ClaudeCodeAct(model="haiku", allowed_tools=["Read","Edit"])`。
> - verify: 5 段の機械チェック全通過で done（下記）。
> - conditions: `MaxIterations(20)` と `TokenBudget`(大きめ)。

## verify は 5 段の ground truth（コスト昇順）

ファイルが **done** になるのは 5 つ全部通ったときだけ:

1. **`parses_ok`** — `ast.parse` が成功する（壊れた編集を最安で弾く）。
2. **`japanese_cleared`** — *翻訳対象*（コメントと docstring）に日本語が残っていない。**非 docstring の文字列リテラルは対象外**（user 向けメッセージは日本語のままが正当なこともあるため。ここを弾くとゴールが到達不能になる）。コメントは `tokenize`、docstring は `ast` の docstring ノードで厳密にターゲティングする。
3. **`code_unchanged`** — **コードと非 docstring 文字列リテラルが変わっていない**（制約「コード/API/型/テスト名/文字列リテラルを変えない」の機械的強制）。HEAD と作業ツリーを両方パースし、**docstring の値だけを `""` に潰して**（= 翻訳を許す）`ast.dump` を比較。docstring 以外の文字列リテラル（エラーメッセージ・CLI 出力等）は**値ごと比較対象に残す**ので、その改変も差分として検出される。一致しなければ、識別子・シグネチャ・制御フロー・import・デコレータ・非 docstring 文字列のいずれかが変わったということなので **reject**。`tests_pass` だけでは「テストが見ていない挙動の破壊」を見逃すので、この段が no-code-change 制約の番人になる。
4. **`changed_files_scoped`** — `git diff --name-only` が許可された対象ファイル集合だけを返す。agent はテスト一時ディレクトリ対策や設定変更を「ついでに」入れがちなので、対象外の tracked file が変わった時点で reject する。`act` に「このファイルだけ」と指示しても、verify で機械的に縛らない限り保証にはならない。`n` 件翻訳の batch なら許可集合は `FILES`、1 ファイルずつ commit する運用なら `{f}` にする。
5. **`tests_pass`** — そのモジュール自身の `pytest` が通る（subprocess で再 import するので、挙動を壊す編集を検出）。

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

def changed_files():
    proc = subprocess.run(["git", "diff", "--name-only"],
                          capture_output=True, text=True, check=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

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
    unexpected = changed_files() - set(FILES)       # 4. changed_files_scoped（対象外変更を拒否）
    if unexpected:
        return VerifyOutcome(goal_met=False, detail=f"unexpected changed files: {sorted(unexpected)}")
    if subprocess.run(["pytest", test_for(f), "-q"]).returncode != 0:   # 5. tests_pass
        return VerifyOutcome(goal_met=False, detail=f"{f}: tests fail")
    done.add(f)
    return VerifyOutcome(goal_met=len(done) == len(FILES), detail=f"{f}: done")
```

## act prompt は lean に保つ（コスト設計）

この recipe の制約は **verify で機械的に強制**し、`act` prompt へ毎回貼り付けない。反復ループでは prompt の肥大が iteration 数で掛け算されるため、`act` には「いま編集する 1 ファイル」と「そのファイルの翻訳対象」だけを渡す。

推奨する prompt の形:

```text
Edit only: {file}
Translate Japanese comments and docstrings in this file to English.
Do not change code or non-docstring string literals.
Do not edit other files. Do not run tests. Return after editing.
```

避ける anti-pattern:

- verify の 5 段チェック本文を毎回 prompt に貼る。
- 既完了ファイル一覧、全体の残りファイル一覧、テスト計画を毎回渡す。
- `Do not inspect tests` と書きながら `adapter tests must pass` のような確認責務を `act` に渡す。

それらは `verify` の仕事である。`act` が余計な repo 探索・diff 確認・test 実行を始めると、ファイル単位ループの失敗隔離メリットよりも prompt/tool cost が勝つことがある。dogfood では、一括 1 iteration が 801,370 tokens だった一方、過剰な `act` prompt を持つファイル単位 loop は 7 iterations / 2,470,874 tokens まで増えた。ファイル単位 loop を選ぶなら、実行前に「1 iteration あたり agent に読ませる情報量」を review する。

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

**挙動不変の機械的証明**: 上の verify 第 3 段（`code_unchanged`）と第 4 段（`changed_files_scoped`）と同じ手法を 10 ファイル全部に適用 — `HEAD` と作業ツリーをパースし、docstring の値だけを `""` に潰して `ast.dump` を比較 → 全て一致。識別子・シグネチャ・制御フロー・import・デコレータ・非 docstring 文字列リテラルは何も変わっていない。これは verify が各ファイルで通すゲートでもあるので、挙動を壊す編集は done にならない。`559 passed` と併せ、翻訳は**証明付きで挙動保存**（PoC の 10 ファイルは非 docstring 文字列リテラルに日本語を含まず、「文字列リテラルを触らない」制約は実際に保たれた）。

## 要点

- **対象外ファイル変更を verify で弾く**。実走では agent がテスト一時ディレクトリ対策として `.gitignore` / `pyproject.toml` を触ることがあった。`git diff --name-only` を許可集合と比較し、対象外の tracked file が変わったら done にしない。
- **文字列リテラルを翻訳対象から外す**のが肝。コメント / docstring だけをターゲティングし、`print()` 等の文字列は触らない。これを誤ると「翻訳しきれない正当な日本語」でゴールが到達不能になります。
- **token 計上**: `ClaudeCodeAct` を `Read`+`Edit` 付きで回すと内部マルチターンで `cache_read` が累積しますが、token 計上は `cache_read` を除外するよう修正済みなので `TokenBudget` は実コストに比例して効きます（Issue #55、以前は早発火していた）。長時間 run の確実な律速には `MaxIterations` 併用が堅実。詳細は [quickstart のトラブルシュート](../quickstart.md#5-トラブルシュートよくある詰まり)。
- **Reflexion は要らないことが多い**: この翻訳タスクの初回失敗は *stochastic*（haiku が長いファイルで末尾コメントを 1 個落とす類）で、blind retry で resample すれば通ります。Run 1（no Reflexion）と Run 2（Reflexion）はほぼ同コストで同結果でした。Reflexion が勝つのは *systematic* な失敗のときだけ（→ [reflexion-when-to-use.md](../reflexion-when-to-use.md)）。
- **公平 scheduling**: ファイル単位の round-robin（試行回数最小から）で、難物 1 ファイルが全反復を独占しないように。
