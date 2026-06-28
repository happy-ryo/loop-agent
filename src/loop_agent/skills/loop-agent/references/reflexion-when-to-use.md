> This file is a load-on-demand bundled copy of `docs/reflexion-when-to-use.md`. The canonical source is `docs/reflexion-when-to-use.md` in the repository.

# Reflexion を使うべきか — 効くタスク / 効かないタスク

loop-agent は内側の `run_loop`（gather → act → verify → repeat）の**外**に Reflexion 型の試行間ループ（`run_reflexion`）を重ねられます。各 episode は内側ループ 1 回で、episode 境界で失敗トラジェクトリから**言語的指針（lesson）**を抽出し、次 episode の context に配線します。

問題は「いつ Reflexion を足す価値があるか」。答えは 1 行で言えます:

> **Reflexion が効くのは systematic failure のタスクだけ。stochastic failure には blind retry とほぼ差が出ない。**

これは推測ではなく **Self-translation PoC** の実走で確認した結論です。

---

## 判断基準: あなたの失敗は systematic か stochastic か

| | **systematic failure（Reflexion 向き）** | **stochastic failure（blind retry で十分）** |
|---|---|---|
| 性質 | 各試行が**同じ概念的な誤り**を繰り返す | 各 retry が**独立な確率事象**（たまたま取りこぼす） |
| 例 | タスクの恒常的な誤解、特定構文の一貫した処理ミス、毎回壊す import 順序 | 長い入力で末尾を 1 個落とす、部分的な編集、モデルの気まぐれな抜け |
| lesson の効き方 | 「次はこうしろ」が**次回の同種ミスを防ぐ** | モデルは既にルールを「知って」いて単に滑っただけ → lesson はほぼ無益 |
| 正しい対処 | **Reflexion**（lesson を次 episode に配線） | **blind retry**（resample すれば大抵通る） |

判定のヒント: **失敗が試行をまたいで相関しているか**を見ます。同じファイル / 同じ構文で**毎回同じように**失敗するなら systematic。失敗する箇所がばらつき、再試行で別の結果になるなら stochastic。

---

## 実証データ: Self-translation PoC（Run 1 vs Run 2）

loop-agent 自身の 10 ファイルを `haiku` で英訳するタスクで、**no-Reflexion と Reflexion を実走比較**しました。

| | Run 1（no Reflexion = blind retry） | Run 2（Reflexion） |
|---|---|---|
| 結果 | 10/10（`goal_met`） | 10/10（`converged`） |
| 内側反復 | 13 | 14（episode0: 10 + episode1: 4） |
| Wall clock | 約 33 分 | 約 32 分 |
| token 計上 | 11.17M | 10.72M |
| retry の仕組み | blind round-robin retry | lesson-guided episode |

Run 2 の episode 内訳:

| Episode | ground-truth 集約 | done | lesson 採択 |
|---|---|---|---|
| 0（1 回ずつ、cap 10） | 0.60 | 6/10 | **あり** |
| 1（lesson 配線後） | 1.00 | 10/10 | なし |

**所見: このタスクでは Reflexion は blind retry に有意に勝たなかった。** 両者ほぼ同コストで同じ 10/10 に収束（run 間ノイズの範囲）。

理由が、この PoC の最も有用な結果です:

- 初回失敗は **stochastic** だった — `haiku` は長いファイルで末尾コメントを 1 個落としたり部分編集をしたりするが、*毎回同じ概念的ミス*をするわけではない。blind retry が resample すれば大抵通る。lesson（「コメントを 1 個見落とした」）は、モデルが既にルールを知っていて滑っただけなので、ほとんど噛むものがない。
- Reflexion の構造的優位 — *systematic な誤りを繰り返さない* — は、verify 失敗が試行をまたいで**相関しているとき**にだけ効く。この翻訳タスクはそうでなかったので、外側ループの lesson チャネルは正しく動いたが噛む対象が無かった。

**正直な読み**: 内側ループの機械的 retry + ground-truth verify だけで self-translation には十分。Reflexion は **systematic な失敗モードを持つタスク**のための道具であって、stochastic な滑りのためではない。

> 補足: 両 run とも機械全体（内側の gate + store + lease、外側の episode + epoch 境界 + episodic memory + grounded lesson admission）を end-to-end で行使しています。「Reflexion が効かなかった」は「機械が壊れていた」ではなく、「このタスクには噛む対象が無かった」という意味です。

---

## 実務的な進め方

1. **まず Reflexion なしで回す**（`run_loop` + `MaxIterations` などの上限）。多くのタスクはこれで足ります。
2. **失敗ログを見て相関を確認する**。`LoopObserver` の JSONL / state.db の step を見て、「同じ箇所・同じ種類で繰り返し失敗しているか」を判定。
3. **systematic なら Reflexion を足す**（`run_reflexion`）。失敗が試行をまたいで相関しているときだけ、lesson 配線が回数とコストを節約します。
4. **モデル昇格と直交**。困難さが「弱いモデルの力不足」由来なら、Reflexion より [ModelLadder パターン](https://github.com/happy-ryo/loop-agent/blob/main/docs/adapters/README.md)（強いモデルへエスカレーション）が効きます。両者は重ねられます（lessons + モデル昇格の二段防御）。

### このタスク種別はどっち寄り?

- **翻訳 / docstring 整備**: stochastic 寄り → まず blind retry。
- **flaky test 安定化**: 失敗要因が共通（時刻依存・順序依存など）なら systematic → Reflexion 向き。各 flaky が無関係なら stochastic。
- **リファクタ**: 同じ抽象化ミス・同じ import 破壊を繰り返しがち → systematic 寄りで Reflexion 向き。
- **バグ修正**: 「同じ誤った仮説で何度も直そうとする」なら systematic。再現テストを先に書く規律と相性が良い。

---

## 安全核（Reflexion を使うときの前提）

`run_reflexion` を使う場合でも、loop-agent の安全核は崩れません:

- **二信号モデル**: 収束・採用などの帰結ある制御は **ground-truth 一次信号**（内側 verify = test/lint/exit-code）が駆動し、rubric 評価器の `reward` は `reflect` だけが消費する。「評価器スカラを押し上げて収束を宣言する」抜け道が構造的に塞がれる。
- **評価器を固定して self-optimize させない**: epoch 内は評価基準を凍結し、更新は epoch 境界で held-out の固定 gold ラベルに対する一致度で incumbent を ε 超で上回るときだけ。
- **lesson の取込前検証**: grounding を要求し、自己申告の support は driver が再計算して上書きする（false lesson 注入を弾く）。

詳細は [docs/reflexion.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/reflexion.md)。
