# loop-agent 調査・設計レポート — Loop Engineering と LoopAgent

> 本レポートは loop-agent プロジェクトの **調査・設計フェーズ**の成果物である。実装は行わず、(1) Loop Engineering / LoopAgent の徹底調査、(2) claude-org-ja の資産棚卸しと再利用評価、(3) LoopAgent の設計（複数案比較→推奨1案）と段階ロードマップ、をまとめる。
>
> - 版: v1.0（2026-06-27）
> - 対象リポジトリ: `https://github.com/happy-ryo/loop-agent`
> - SoT: 本ファイル `report.md`（`report.html` は同内容の閲覧用単一ファイル）

---

## 0. エグゼクティブサマリ

**Loop Engineering とは**、「人間がエージェントに一手ずつプロンプトを打つ」のをやめ、**エージェントをプロンプトし・検証し・記憶させ・再実行する“システム（=ループ）そのもの”を設計する実践**を指す（2026年6月、Anthropic の Claude Code 開発責任者 Boris Cherny の発言を起点に実務家コミュニティで急速に普及した概念）[^le-def][^le-origin]。技術スタックとしては **prompt engineering（1ターンの指示）→ context engineering（推論時にモデルが見るトークン全体の構成）→ loop engineering（ターンをまたぐ継続・終了・再実行の制御層）** という3層の最上位に位置し、下2層を置き換えるのではなく **wrap（包む）**[^le-stack]。

**本レポートの結論（推奨設計）**: loop-agent の LoopAgent は、

1. **最内ループ**を Anthropic 標準の `gather context → take action → verify → repeat` に揃え（各反復で必ず環境から ground truth を取得）[^anthropic-bea][^agent-sdk-loop]、
2. その外側に **Reflexion 型の試行間メモリ＋言語的自己反省**層を重ねる二層構造とし[^reflexion]、
3. 終了条件を **「意味的判定（検証可能ゴール / critic）」と「機械的上限（反復・トークン・時間）」の二重化**として実装し（全フレームワーク共通の業界標準）[^framework-common]、
4. **状態を context に溜め込まず外部 SoT（`state.db` 相当）に外出し**し[^harness]、
5. **人間ゲートは「不可逆・影響範囲大」のアクションに限定**する[^hitl][^verify-hitl]、

という **「単一制御層 + 共有状態機械 + 段階的な org 資産組込」型（後述の案C）** を推奨する。

**claude-org-ja の資産再利用**: 調査の結果、loop-agent が必要とする要素の大半は claude-org-ja に既製の高品質実装が存在する。特に以下は再利用価値が高い:

| 要素 | claude-org-ja 資産 | 判定 |
|---|---|---|
| ループ状態の永続化（SoT） | `tools/state_db/`（SQLite + StateWriter transaction + post-commit snapshot） | reuse-as-is / adapt |
| ループ間通知・wake 配送 | transport（renga/broker, push一次/pull fallback, at-most-once） | extract-pattern |
| フィードバック（self-improving） | org-retro / org-curate / knowledge（raw→curated, 閾値起動） | adapt |
| 観測・人間ゲート・暴走検知 | attention-watcher / pr-watch / org-escalation / pending_decisions | reuse-as-is / adapt |
| 終了条件・状態遷移の型 | delegation-lifecycle / state-semantics contract | reference-only |
| 反復対象の選定 | work-discovery（計算層＋配達層の二層分離） | adapt |

**ロードマップ**: **PoC（最小ループ＋ハード上限）→ MVP（状態機械＋state.db SoT＋二層終了条件＋観測）→ 本格（org のフィードバックループ・transport・人間ゲートを統合した自律 LoopAgent）** の3段階で漸進する。

---

## 1. 背景と目的

### 1.1 プロジェクトの狙い

loop-agent は、本格的な **Loop Engineering** を実現する **LoopAgent** の設計・実装プロジェクトである。本レポートはその起点として、

- Loop Engineering と LoopAgent の概念・系譜・設計論点を web で徹底調査し、
- 既存資産 claude-org-ja から再利用可能なものを棚卸し・評価し、
- それらを踏まえた LoopAgent のアーキテクチャ設計と段階ロードマップを提示する。

本フェーズでは**実装は行わない**（設計まで）。

### 1.2 用語定義

| 用語 | 定義 | 出典 |
|---|---|---|
| **agent（エージェント）** | ツールをループ内で自律的に使用する LLM（"LLMs autonomously using tools in a loop"）。これが agentic system の最小核。 | Anthropic[^ctx-eng] |
| **agentic loop** | 各反復で context を集約 → LLM が推論し行動選択 → 実行 → 結果を観測 → 次反復にフィードバック、する反復実行サイクル。 | Oracle[^oracle-loop] |
| **prompt engineering** | 単一ターンの指示の書き方の設計。 | [^le-stack] |
| **context engineering** | 推論時にモデルが見るトークン全体（指示・ツール・例・履歴・取得文書）の構成・キュレーション。prompt engineering の自然な発展。 | Anthropic[^ctx-eng] |
| **loop engineering** | ターンをまたいでエージェントを継続・終了・再実行させる制御層の設計。prompt/context engineering を wrap する。 | [^le-def][^le-stack] |
| **LoopAgent** | loop engineering を体現する実体。トリガー＋検証可能ゴール＋ガードレールで agentic loop を包んだ自律実行エージェント。本プロジェクトの設計対象。 | 本レポートの定義 |

---

## 2. Loop Engineering 徹底調査

> 本章は web 調査（fan-out 検索 → 出典精読 → 主張の独立反証検証）に基づく。主要主張には出典 URL を付す。Loop Engineering 系のブログ主張は実務家発の新興概念であり急速に進化中であるため、設計判断は可能な限り Anthropic 公式 docs と査読系論文（ReAct / Reflexion 等）にアンカーした。

### 2.1 Loop Engineering とは（定義・起源・3層スタック）

**定義**。Loop Engineering は「エージェントをプロンプト・検証・記憶・再実行するシステム自体を設計する実践」であり、手作業のプロンプト入力を**ゴールベースの自動化**に置き換える[^le-def]。agentic loop 自体は **`trigger`（イベント / スケジュール / 人間の指示）＋ `verifiable goal`（検証可能な目標）** の2要素で構成され、エージェントが start → run → ゴール到達チェック → 未達なら再ループ、を人間の介在なしに回す。単なる自動化（あらかじめ決めた手順の実行）と違い、ループ内に**意思決定（ゴール到達の能動評価）が埋め込まれている**点が本質的差異である[^le-def]。

> 独立検証の結果: この定義は SmartScope（"designing the system that prompts, checks, remembers, and re-runs AI agents"）・Firecrawl・MindStudio（"replaces manual prompting with goal-based automation"）など複数の一次・二次ソースでほぼ逐語的に裏付けられ、矛盾するソースは見つからなかった（verdict: **supported**）[^v-le-def]。

**起源**。学術用語ではなく **2025–2026 の実務家由来**の概念である。2026年6月、Boris Cherny（Anthropic, Claude Code 開発責任者）の発言 *"I don't prompt Claude anymore. I have loops running that prompt Claude and figuring out what to do. My job is to write loops."* がインタビュー動画クリップで拡散し（24時間で約70万ビュー、その後数百万ビュー規模）、急速に広まった[^le-origin]。普及には Cherny に加え Addy Osmani（「自分をエージェントにプロンプトする人から外し、それを行うシステムを設計する」という framing）、Peter Steinberger（loop-centric ワークフロー）らが寄与した[^le-origin]。

**3層スタック**。Loop Engineering は次の3層の最上位「制御層」にあたる[^le-stack]:

```
┌─────────────────────────────────────────────┐
│ Loop Layer    : ターンをまたぐ継続・終了・再実行    │ ← loop engineering（制御層）
│   失敗モード: 誤った方向を追い続ける               │
├─────────────────────────────────────────────┤
│ Context Layer : 任意時点でモデルに見えている情報全体 │ ← context engineering
│   失敗モード: 古い/肥大したデータ                  │
├─────────────────────────────────────────────┤
│ Prompt Layer  : 単一ターンの指示                  │ ← prompt engineering
│   失敗モード: 制約の誤解                          │
└─────────────────────────────────────────────┘
loop engineering は下2層を「置き換える」のではなく「wrap する」
```

Anthropic 自身もエージェントを端的に **"LLMs autonomously using tools in a loop"** と定義し、ループ内で走るエージェントは次の推論ターンに関連しうるデータを生成し続けるため **context の周期的キュレーションが不可欠**だと指摘している[^ctx-eng]。

**人間の役割の上昇**。「コードを書く → プロンプトを書く → ループを設計する → ループを回す工場を作る」へと段階的に上がる[^le-def]。Loop Engineering の本質は人間が継続的介入から **「事前のゴール仕様＋ガードレール設計」** へ移ることにある。なお実装上、while ループ本体は易しい部分であり、難所は **`context` と `stop condition`（終了チェック / コスト上限 budget / 達成 target）**、さらに本番運用には標準フレームワークに欠ける **governed workspace（ID・スコープ付き権限・監査証跡・高速ロールバック）** が第6の必須要素だと指摘されている[^le-stack]。

### 2.2 agentic loop の系譜（古典）

古典的な agentic loop は大きく2系統に分かれる。

**(A) 単一エピソード内で観測しながら進む「推論-行動」ループ系**

- **ReAct（Reason + Act, Yao et al., ICLR 2023）**: `Thought → Action → Observation` を interleave するループ。推論が行動計画の誘導・追跡・更新と例外処理を担い、行動が外部知識源（API / 環境）との接続を担う。chain-of-thought で生じる hallucination と誤伝播を、環境との対話で grounding することで抑制する。HotpotQA / Fever / ALFWorld / WebShop で評価し、ALFWorld で +34%、WebShop で +10% の絶対改善。**現代の「tool-in-the-loop」エージェントの直接の祖**である[^react]。
  > 独立検証: 原論文アブストラクトとほぼ逐語一致（"reasoning traces help the model induce, track, and update action plans as well as handle exceptions" / "actions allow it to interface with ... external sources"）。verdict: **supported**[^v-react]。
- **Plan-and-Execute**: planner（LLM）が多段計画を生成 → executor（別エージェント/ツール）が各ステップを孤立実行 → 完了後に re-plan プロンプトで「完了 or 追加計画」を判断する明示ループ。利点は (1) 明示的な長期計画、(2) 役割分離（planner に強モデル・executor に弱/小モデルでコスト最適化）[^plan-exec]。

**(B) 複数試行をまたいで自己改善する「反省」ループ系**

- **Self-Refine（Madaan et al., NeurIPS 2023）**: 単一 LLM が generator / feedback-provider / refiner を兼ね、`generate → self-feedback → refine` を反復するテスト時手法。追加学習・教師データ・RL 不要。7タスクで平均 ~20% 絶対改善[^self-refine]。
- **Reflexion（Shinn et al., NeurIPS 2023）**: weight 更新でなく**言語的フィードバック（verbal reinforcement）**で改善するループ。Actor（方策 LLM）/ Evaluator（軌跡の成否判定。別 LLM・ヒューリスティック・**外部実行=unit test**）/ Self-Reflection（失敗の言語的サマリ生成）の3役で、環境報酬を言語的フィードバックに変換し **episodic memory** に蓄積、次試行を改善する。HumanEval pass@1 91%（当時の GPT-4 80% 超）[^reflexion]。

両系統を包含する上位フレームが軍事起源の **OODA loop（Observe → Orient → Decide → Act, John Boyd）**で、Anthropic の "models using tools in a loop" 定義とよく対応する。ただし Schneier らは各段に固有のセキュリティリスク（Observe=prompt injection、Orient=文脈汚染、Decide=reward hacking、Act=action hijacking）を指摘し、**「速度・知性・セキュリティを同時達成できない security trilemma」**を主張する。ループ設計では各段の入力検証と人間ゲートが本質的である[^ooda]。

**Self-Refine と Reflexion の反省粒度の違い**は設計上重要: Self-Refine は「同一エピソード内で同じ出力を磨く（即時品質ゲート）」、Reflexion は「試行をまたいで memory に学びを残す（長期改善）」。後述するとおり、claude-org の retro/curate フィードバックループは後者にマッピングできる。

### 2.3 自律エージェント第一世代の系譜と教訓（AutoGPT / BabyAGI / AgentGPT）

2023年4月前後に登場した第一世代（AutoGPT / BabyAGI / AgentGPT）は、いずれも「タスク生成 → 実行 → 再計画」を `while True` で回す素朴なループを核とした。**そのほとんどが実用で苦戦し、現代のループ設計原則は「その失敗の裏返し」として収束した**。これは loop-agent が踏むべきでない轍の宝庫であり、特に重視する。

- **BabyAGI（Yohei Nakajima, 2023/4）**: `while True` 内に3つの LLM コール（Execution / Task Creation / Prioritization）を連鎖させただけの**約105行の PoC**。タスク依存も完了基準もなく、**終了条件を一切持たなかった**（無限ループは意図的設計だった）。さらに、Pinecone ベクトル記憶（ada-002 top-5 検索）の出力が**実行プロンプトに一度も渡されていない**——記憶機構があっても意思決定に「配線」されなければ機能しないという教訓[^babyagi]。
  > 独立検証: 「105行」「3 LLM コールの連鎖」「終了条件なし」はいずれも複数出典で裏付け（"The loop never terminates. There is no completion condition."）。verdict: **supported**[^v-babyagi]。
  > BabyAGI はその後9世代の進化で、依存関係・終了条件（全タスク complete で停止）・永続化（SQLite ナレッジグラフ）・並列・コンテキスト予算化・エラー回復を段階的に獲得した（最新 BabyAGI 3 は約33,500行）[^babyagi]。
- **AutoGPT**: GPT-4 でゴール分解 + ツール実行を回すが、終了ロジックを欠き完了判定を自然言語評価に頼ったため「**常にもっと作業が必要**」へ偏った。根本原因は (1) 測定可能指標のない曖昧な完了基準、(2) 新旧プランを比較しない**進捗検知の欠如**、(3) 完璧主義バイアス、(4) API/時間/コストの追跡もサーキットブレーカもない**リソース無自覚**。具体例: AI史調査で300超 API コール・8反復しても要約を出さない research spiral、Downloads フォルダを15回以上再分類し続ける、8K context を毎回上限まで使う50ステップの小タスクで約 **$14.40**[^autogpt-fail]。
- **AgentGPT（Reworkd）**: ブラウザで自律ループを回す製品化版。loop limit を唯一の防御線とし、蓄積コンテキストがモデル窓を超えてクラッシュ/無限ループ、セッション跨ぎで記憶喪失、クラウド限定、という制約を抱えた。GitHub リポジトリは **2026年1月にアーカイブ（read-only）化**された[^agentgpt]。

**第一世代が残した最大の設計教訓**[^autogpt-fail][^babyagi][^simpler]:

1. **「無限の再優先化ループ」→「依存関係を持つ有限タスクグラフ + 明示的終了条件」**への転換。
2. **暴走防止のハード上限とサーキットブレーカー**（イテレーション・累積トークン/コスト・経過時間）。
3. **進捗検知・重複検知を状態管理に組み込む**（同一行動の反復を検出して打ち切る）。
4. **記憶は「保持」だけでなく「意思決定への配線」まで検証**する。
5. **人間ゲートと観測可能性**を初期から組み込む。
6. **`simpler loops win`**: 凝った多段推論より、少数の信頼できるツール + 良質なコンテキスト管理が勝つ。

### 2.4 プロダクションの agent harness loop

**Anthropic の設計指針**は一貫して **「最小構成から始め、必要な時だけ複雑さを足す」** と **「workflow（コードで経路を固定）と agent（LLM が自律的に経路を決定）を区別する」** を中核に据える[^anthropic-bea]。
> 独立検証: "finding the simplest solution possible, and only increasing complexity when needed" / Workflows = "orchestrated through predefined code paths" / Agents = "dynamically direct their own processes" をいずれも逐語確認。verdict: **supported**[^v-bea]。

自律 agent の本体は「拡張 LLM（retrieval/tools/memory）が環境フィードバックを得ながらツールを使うループ」であり、**各ステップで環境から ground truth（tool call results / code execution）を得ることが決定的に重要**、かつ**最大反復数などの停止条件を組み込むのが定石**である[^anthropic-bea]。

**Claude Agent SDK / Claude Code** はこのループを **`gather context → take action → verify work → repeat`** として実装する。turn（ツール呼び出し往復）単位で進み、ツール呼び出しのない応答が出たら終了する。runaway 防止に **`max_turns` / `max_budget_usd`** を持ち（"Setting a budget is a good default for production agents."）、context が上限に近づくと自動 compaction（`compact_boundary` 発火）で要約圧縮する。終了は `ResultMessage` の subtype（`success` / `error_max_turns` / `error_max_budget_usd` / `error_during_execution`）で判別する[^agent-sdk-loop][^agent-sdk-blog]。

**検証（verify）の3方式**: ルールベース（lint/test/typecheck）、視覚フィードバック（screenshot）、LLM-as-judge。**最良はルールを明示し「どのルールがなぜ失敗したか」を返すこと**[^agent-sdk-blog]。

**長時間稼働 harness** は「1回で完遂」ではなく **「毎セッションでクリーンな状態を残しつつ漸進」** する設計が要。各セッションは前回の記憶を持たない前提で、Initializer/Coding の役割分離、feature list（JSON, pass/fail）・progress ファイル・git 履歴を **SoT 化**、テストによる自己検証を行う。明示しないと未テストで完了扱いするため「Self-verify all features. Only mark as 'passing' after careful testing」を指示する[^harness]。

**スケジューリングは3層**[^scheduled]:

| 層 | 基盤 | 最小間隔 | ローカルファイル | 用途 |
|---|---|---|---|---|
| cloud routines（`/schedule`） | Anthropic 基盤 | 1h | 不可（fresh clone） | 確実な無人実行 |
| Desktop scheduled task | 自マシン常駐 | 1m | 可 | ローカル資産が要る定期実行 |
| セッション内 `/loop`（CronCreate/List/Delete） | 開いているセッション | 1m | 可 | セッション中の簡易ポーリング |

`/loop` と Cron 系はセッションスコープで、**recurring は作成7日後に最終1回発火して自己削除**し「忘れられたループがどれだけ走り続けうるかを境界付ける」。jitter で API 集中を回避、catch-up なし、`CLAUDE_CODE_DISABLE_CRON=1` で全停止できる。`/loop` の prompt-only モードでは Claude が 1分〜1時間で動的に間隔を選び、provably complete なら自分で打ち切る[^scheduled]。

**Cursor / Devin** も `plan → execute → verify → iterate` の同型ループで、lint/test/型チェックの pass/fail を自己修正信号にする。Cursor は `typecheck && lint` を成功時のみ先へ進めて「error 積み上げ」を防止。Devin は構造化プランを先に作り（50ステップ級）、test 失敗等の full context で dynamic re-planning する[^cursor-devin]。

### 2.5 フレームワークの LoopAgent 構文

主要フレームワークは「ループ」を2つの設計流派で実現している。

**(1) 宣言的な専用ループ構造**

- **Google ADK `LoopAgent`**: sub_agents を順に反復実行する**決定論的**ワークフローエージェント。`LoopAgent(name=..., sub_agents=[critic, refiner], max_iterations=5)`。終了は **(a) `max_iterations` 到達**、**(b) いずれかの sub-agent が `escalate=True` を返す（`exit_loop` ツール: `tool_context.actions.escalate = True`）** の2系統。公式 docs は「**LoopAgent 自体は停止タイミングを決めない。終了メカニズムは必ず自分で実装せねばならない**」と明記する[^adk]。
  > 独立検証: sub_agents 反復・決定論・終了2系統（max_iterations + escalate）を公式 docs で確認。verdict: **supported**（「宣言的」は公式には "deprecated 用語ではなく template/deterministic" がより正確という軽微な留保）[^v-adk]。
- **AutoGen（v0.4+）**: 合成可能な `termination_condition` オブジェクトでチーム（ループ）を止める。`MaxMessageTermination(10) | TextMentionTermination("APPROVE")` のように **OR `|` / AND `&` で合成可能**。他に TokenUsage / Timeout / Handoff / External / Functional 終了条件がある[^autogen]。

**(2) 明示的グラフ / 状態機械**

- **LangGraph**: 専用ループ構造でなく `StateGraph` 上の `add_conditional_edges` と再帰エッジ（または `Command(goto=...)`）で循環を作り、**`recursion_limit`（デフォルト1000 super-steps）**を安全ネットとする。状態は共有 state を各ノードが update して受け渡す。docs は「recursion_limit は主たる制御フロー手段ではなく、well-designed なグラフロジックの代替ではない」と注意する[^langgraph]。
- **CrewAI**: Flows の `@router` デコレータ + state のイテレーションカウンタで loop-back を表現。Agent 単体には `max_iter`（デフォルト25）。なお max_iter 到達後も止まらない既知バグ報告があり、**安全網の多層化**が必要[^crewai]。
- **OpenAI Agents SDK**: 内部 run loop（モデル呼び出し → tool 実行 → handoff → final_output で終了）を `max_turns` で制限し、`error_handlers` で `MaxTurnsExceeded` を**制御された最終出力に変換**できる。前身の Swarm は2025年3月に Agents SDK へ置換され非推奨[^openai-sdk]。

**全フレームワーク共通の設計原則**: **「意味的終了判定（LLM/critic/特定文字列）」と「機械的上限（回数・トークン・時間）」を分離して両方備える**。ADK は max_iterations を "critical safety net" と呼び escalate と併用、LangGraph は recursion_limit を runaway 防止に位置づけつつ主制御を条件エッジに置く、等[^framework-common]。
> 独立検証: 各フレームワークが二重構造を持つことを公式 docs で確認。verdict: **supported**[^v-framework]。

### 2.6 ループ制御と安全性（運用ベストプラクティス）

実運用の合意は**多層防御**に収束している。

- **終了条件（多層）**: (1) 自然完了（ツール呼び出しなしの最終応答）を主軸に、(2) 最大反復数を必須のセーフティネット（実務目安 **5–10 反復**）、(3) 壁時計タイムアウト、(4) 無進捗 / 反復アクション検出（**実行不能アクション連続3回 / 同一反復アクション3回 / 進捗なし20ラウンド**で打ち切り）、(5) 回復不能エラー[^term][^loop-detect]。
- **収束判定**: evaluator rubric の閾値超え / entropy 等の変化量が閾値未満（頭打ち）/ 反復上限、で判定する。AWS の evaluator reflect-refine パターンは「The loop repeats until the result meets a set of criteria, is approved, or reaches a retry limit」[^converge]。
- **コスト制御（5層）**: per-request の `max_tokens`、セッション/日次予算、turn カウンタ（例 `MAX_TURNS=25`）、サーキットブレーカ、ゲートウェイ層での強制。試算では無制限ループが10分で$15、100並列/時で約$2100/日に達しうる。**消費レートが直近平均の3xを超えたら自動スロットル + 所有者 alert** が定石[^cost]。
- **人間ゲート（限定）**: **「不可逆・影響範囲大」のアクションのみ**に絞る。LangGraph の `interrupt()` はノード内でグラフを一時停止し、人間の決定は **approve / edit / reject / respond** の4種。checkpointer が各 super-step で StateSnapshot を永続化し pause/resume を安全化する。ベストプラクティスは "interrupt on irreversible, high-blast-radius actions only — not on every step"[^hitl]。
  > 独立検証（重要な補正）: 「全ステップで人間介入を組み込むのが標準」という素朴な主張は**反証寄り（partly-supported）**。MindStudio は human-in-the-loop を "optional for high-stakes scenarios only, not standard practice" と明記。標準の終了制御はむしろ自然終了 + max iterations + timeout + cost ceiling + loop detection の defense-in-depth であり、**人間介入は普遍的標準ではなく不可逆操作に限定した条件付き**である[^verify-hitl]。
- **観測性**: **OpenTelemetry GenAI semantic conventions** に準拠し、各 LLM 呼び出し / ツール実行 / retrieval を child span 化、`gen_ai.*` 標準属性（model, token counts, finish_reason）+ ループ反復番号 / 終了理由を記録する。観測は troubleshoot だけでなく **「品質を継続的に学習・改善する feedback loop の源」** である。ベンダー専用 SDK より OTel 準拠が推奨される[^otel]。
- **self-improving / eval loop の罠**: Reflexion 型が基本だが、反復で**出力が肥大化・劣化**（3反復で応答が元の4倍に膨張する例）、**reward hacking**（曖昧な報酬で「場合による」等のヘッジ表現が技術的に never wrong で高スコア化）、**毒性フィードバックの memory 汚染**（敵対環境では false lesson 注入の攻撃面）がある。緩和は **早期停止・多様な評価・定期的な実環境テスト・性能測定と本番実行の分離（dual-component）**[^self-improve]。
- **LLM-as-judge のバイアス**: position bias / self-preference bias が実証されている。**まず安価な ground-truth 検証（テスト・文字列一致 = exact string comparison）を優先**し、judge は rubric + 人間とのキャリブレーション併用に限定する[^judge]。

### 2.7 調査から抽出した設計原則（distilled principles）

以上を loop-agent の設計原則として10点に蒸留する。

1. **LoopAgent = 「ツールをループ内で自律使用する LLM」を、トリガー＋検証可能ゴール＋ガードレールで包む制御層**として位置づける（prompt/context 機構の置き換えでなく wrap）。
2. **最内ループは `gather → act → verify → repeat`**。検証信号（ground truth）のない反復は作らない。
3. **終了条件は「意味的判定」と「機械的上限」の二重化を必須化**し、合成可能な condition オブジェクトとして実装する。
4. **暴走防止のハード上限とサーキットブレーカ**（反復・累積トークン/コスト・時間）をエンジンの**不変条件**として内蔵する。
5. **進捗検知・重複検知**で同一行動の反復・無進捗を検出し打ち切る。
6. **状態は context に溜めず外部 SoT に外出し**（feature list / progress / 状態DB）。compaction は補助であり SoT ではない。
7. **内側 ReAct + 外側 Reflexion の二層**（単一エピソード実行 / 試行間の言語的改善）。記憶は意思決定への「配線」まで eval で担保する。
8. **人間ゲートは不可逆・影響範囲大のアクションに限定**（approve/edit/reject/respond）し、状態を永続化して pause/resume を安全化する。
9. **観測可能性は OTel GenAI 準拠**で最初から。ループ各段・終了理由・コストを機械可読イベントで発火する。
10. **`simpler loops win`**。PoC（~200行の中核ループ）と本番（エラー回復・並行性・永続化を扱う数万行）を分け、段階導入する。

---

## 3. claude-org-ja 資産棚卸し（再利用評価）

> `/home/happy_ryo/work/org/claude-org-ja` を **read-only** で精読し、Loop Engineering / LoopAgent に再利用しうる資産を具体的な file 参照付きで評価した。判定凡例: **reuse-as-is**（ほぼそのまま）/ **adapt**（改変して再利用）/ **extract-pattern**（設計パターンを抽出）/ **reference-only**（参考にする）/ **N-A**（非該当）。

### 3.1 オーケストレーションループ（secretary / dispatcher / worker / curator）

claude-org は **Secretary → Dispatcher → Worker → Curator** の4段階ループを実装する。Secretary（人間接点）→ Dispatcher（常駐監視）→ Worker（実作業）→ Curator（on-demand 知見化）の delegation flow と escalation path を持つ。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| Handover/Resume パターン | `.claude/skills/{secretary,dispatcher}-{handover,resume}/SKILL.md`, `.dispatcher/references/worker-monitoring.md` | **reuse-as-is** | 「ターン境界での状態保存と復帰」=長時間ループの context 管理そのもの。終了条件を保存して再開する模範。 |
| Role Contract（4役割の責務/境界） | `docs/contracts/role-contract.md`, 各 `CLAUDE.md` | **extract-pattern** | 「誰がループを回すか・どこで責務が切れるか」の骨格。loop-agent は coordinator / loop-agent / eval-agent の3段化が想定。 |
| Delegation Lifecycle Contract（T1–T9 遷移、E1–E5 エラー） | `docs/contracts/delegation-lifecycle-contract.md` | **reuse-as-is**（型）/ **reference-only**（具体） | 「タスク状態の終了条件」「エラー分岐」を explicit にする。review feedback による再ループ・abort 条件の正式な型。 |
| Dispatcher monitoring loop（`/loop 3m`） | `.dispatcher/references/worker-monitoring.md` | **adapt** | 「観測→判定→通知」の機械化。stall detection は「forward progress がない」の operational definition。 |
| On-demand Curator（worker close trigger） | `.claude/skills/org-curate/SKILL.md`, `tools/check_curate_threshold.py` | **adapt** | 「フィードバック→改善」の自動トリガ。閾値超過時のみ起動する非ブロッキング async lifecycle。 |
| Escalation + pending-decisions register | `.claude/skills/org-escalation/SKILL.md`, `tools/pending_decisions.py` | **reuse-as-is** | 「Agent 間 escalation と人間ゲート」の explicit lifecycle。relay gap 検出の ground truth。 |

### 3.2 自律ループ / ScheduleWakeup / cron

claude-org は **`/loop 3m` による時間駆動監視ループ**、worker クローズ時の**条件判定での async on-demand 起動**、**役割別の能動 poll cadence** を実装する。注目すべきは **cron 定常 routine を明示的に不採用**とし、イベント駆動・単発判定・状態保持ファイル・単一実行保証を採っている点である。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| Role-based passive polling cadence | `knowledge/curated/broker-transport.md`, `.dispatcher/references/worker-monitoring.md` | **extract-pattern** | stateless CLI 環境での自律ループ再入。dispatcher 3m / worker bounded / secretary turn-prologue の非対称設計。 |
| Deterministic decision tool（exit-code 分岐） | `tools/check_curate_threshold.py`, `tools/work_discovery_scan.py` | **reuse-as-is** | 副作用ゼロの計算ツール。JSON stdout + exit-code（0/10/2）で「条件成立か」を返し判定を配達層へ委譲。loop の冪等な condition evaluation。 |
| Resume-safe loop state（cursor + metadata JSON） | `.dispatcher/references/worker-monitoring.md`, `.dispatcher/CLAUDE.md` | **reuse-as-is** | event-cursor / idle-state / inflight marker で resume-gap・重複を回避。 |
| Single-flight / coalesce（重複 spawn 防止） | `.dispatcher/references/pane-close.md` | **reuse-as-is** | event-driven loop で同一トリガが短間隔で火いたときの競合回避。spawn 前 list で既存確認。 |

### 3.3 transport（renga / broker, push 一次 / pull fallback）

エージェント間メッセージング・状態通知の**二重輸送層**。既定 renga（in-band push）と broker（channel sidecar による ~1秒 claim→push + pull fallback）が共存し、**at-most-once 配送**・tier 別構造化アクセス制御を実装する。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| Transport Abstraction Seam | `tools/transport.py` | **extract-pattern** | runtime descriptor を唯一の SoT に backend 切替を抽象化。複数 backend を backend-agnostic に扱う。 |
| Peer Message Delivery Bridge（best-effort） | `tools/peer_notify.py` | **reuse-as-is** | CLI/背景タスク→メインエージェントの「失敗しない」非同期通知。ループ外からの割り込み通知に適用可。 |
| Push 一次 / Pull fallback delivery model | `docs/contracts/backend-interface-contract.md`, `docs/operations/broker-dogfood-runbook.md` | **extract-pattern** | push で即応答、pull fallback で backend 不通に耐える。ループ間 wake 配送の中核パターン。 |
| Tier-Gated 構造化アクセス制御 | `docs/contracts/backend-interface-contract.md` | **adapt** | auth_role（immutable）に基づく capability 制約。spawn 時に caller tier で子を cap=detached agent のサンドボックス化。 |
| Message at-most-once semantics | 同上 | **adapt** | drain=消費確定・redelivery なし。上位を idempotent handler 前提にする delivery 契約。 |
| Error code vocabulary（machine-readable） | 同上 | **reference-only** | `[<code>] <message>` 形式 + default-branch tolerance の error handling discipline。 |

### 3.4 state.db を SoT とする状態管理

**SQLite `state.db`** が runs / org_sessions / events / worker_dirs / projects / workstreams の単一 SoT。markdown / JSON は snapshotter が DB から自動再生成する派生物で、drift_check が手書き編集を検出する。**loop-agent のループ状態（iteration, convergence history, termination 評価）永続化に直接再利用できる**最重要資産。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| state.db スキーマ & SoT 定義 | `tools/state_db/schema.sql`, `docs/contracts/state-semantics-contract.md`, `docs/org-state-schema.md` | **adapt** | runs 拡張カラム（iteration_count, is_converged, terminated_reason）でループ状態を永続化。events に loop event を journal。 |
| StateWriter API & Transaction | `tools/state_db/writer.py`, `tools/state_db/__init__.py` | **reuse-as-is** | `transaction()` で atomic 更新 + post-commit hook（markdown/JSON 再生成）。rollback on exception で失敗時の保全。 |
| Query 層 & State Predicates | `tools/state_db/queries.py` | **adapt** | TERMINAL_STATUSES 等の述語が「終了条件判定」に直結。loop-specific predicate を追加。 |
| Journal Events Catalog（50+ type） | `docs/journal-events.md`, `tools/journal_append.{sh,py}` | **adapt** | ループ各 cycle step を journal（loop_cycle_begin/convergence_detected/termination_triggered）。complete audit trail。 |
| WAL Journal Mode & 並行アクセス | `tools/state_db/__init__.py` | **reuse-as-is** | WAL + busy_timeout で concurrent reader（dashboard/observer）と loop writer が共存。観測可能性を enable。 |
| Snapshotter（post-commit 再生成） | `tools/state_db/snapshotter.py` | **adapt** | ループ observation を `.state/loop-state.md` に human-readable で atomic dump。 |
| State Semantics Contract（7 status, 4 predicate） | `docs/contracts/state-semantics-contract.md` | **reference-only** | loop の finite state machine（OBSERVING/THINKING/ACTING/CONVERGING/TERMINATED 等）設計の参考。 |

### 3.5 work-discovery / triage

自律 **work-discovery** は issue tracker を scan・triage し「次の仕事候補（N件 + 推奨1件）」を人間に提案する機構。**計算層（read-only 決定的ツール）と配達層（スキル / dispatcher）の二層構造**で、発見の自律性を上げつつ着手判断は人間ゲートに保つ。Loop Engineering の「次に何を反復するか」の入力選定ループに対応する。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| work_discovery_scan.py（計算層） | `tools/work_discovery_scan.py` | **reuse-as-is** | read-only・副作用ゼロ・同一入力同一出力。複数の起動経路が同一ツールを共有。loop 状態に影響しない入力選定。 |
| work-discovery-triage 設計（二層 / 不変条件 / 段階導入） | `docs/design/work-discovery-triage.md` | **reference-only** | 「発見の自律性は上げるが、判断は人間ゲートに残す」(INV-1〜5) が LoopAgent の人間中心性と直結。 |
| 完了→次反復の接続（post-merge / pane-close トリガ） | `.claude/skills/org-pull-request/SKILL.md`, `.dispatcher/references/pane-close.md` | **extract-pattern** | 「idle 化した瞬間」を検出して次候補を自動提示する trigger point。提案で停止（人間ゲート維持）。 |

### 3.6 フィードバックループ（org-delegate / org-retro / org-curate / knowledge）

**Delegation → Retro → Curate → Skill adoption** の完全サイクルを実現。委譲 → 完了後に委譲プロセスを振り返り（5観点）→ 知見を raw/curated（事実/判断/根拠/適用場面の4要素）で構造化 → skill-eligibility-check（5 signals scoring）→ pending≥5 で skill-audit 発火、という **「人間不在の自動フロー + 人間決定ゲート」の二層**。**LoopAgent の self-improving / eval loop に直結する**（Reflexion の「試行間メモリ」にマッピング可能）。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| org-retro（5観点振り返り + skill 化判定） | `.claude/skills/org-retro/SKILL.md` | **adapt** | 多 turn 実行後にプロセス自体を評価し改善点を記録。agent pattern の自動抽出（=eval loop cycle）。 |
| org-curate（raw→curated 統合, 閾値 on-demand 起動） | `.claude/skills/org-curate/SKILL.md`, `references/knowledge-standards.md` | **adapt** | observation 蓄積→統合→pattern extraction。move-then-mark で immutable raw を保全。 |
| knowledge 4要素フォーマット（事実/判断/根拠/適用場面） | `org-curate/references/knowledge-standards.md` | **reuse-as-is** | agent reasoning trace の標準記録形式。同種知見3件以上で pattern 化。 |
| skill-candidates（status machine + batch gate, N=5） | `knowledge/skill-candidates.md` | **reuse-as-is** | pattern recommendation を人間ゲート越しに skill へ昇格。閾値 batch 決定で cognitive load 最適化。 |
| work-skill template（標準フォーマット, origin record） | `org-retro/references/work-skill-template.md` | **reuse-as-is** | pattern を skill 化する際の traceability（genesis を辿れる）。 |
| curated 知見 15 ファイル（delegation/broker-transport/codex 等） | `knowledge/curated/*.md` | **reference-only** | ループ実装で踏みやすい failure mode と回避策の先制共有（概念パターンとして transfer）。 |

### 3.7 観測・人間ゲート・暴走防止（attention / pr-watch / escalation / suspend）

5つの統合スキルで、ワーカー観測・判断仰ぎエスカレーション・pending_decisions register・イベント DB・状態保存を実現する。**LoopAgent の観測可能性・人間ゲート・暴走検知に直接応用できる**。

| 資産 | file | 判定 | loop での意義 |
|---|---|---|---|
| org-attention-start/stop（OS 通知 watcher） | `.claude/skills/org-attention-{start,stop}/SKILL.md` | **adapt** | 承認待ち・CI失敗・想定外を通知音で能動検知。pane_id sidecar で二重起動防止・孤児検知。loop 観測層。 |
| pr-watch-pane / pr_watch.py（外部イベント監視） | `.claude/skills/pr-watch-pane/SKILL.md`, `tools/pr_watch.py` | **adapt**/**extract-pattern** | 長時間 watcher（CI/merge/webhook 待機）の冪等 spawn + identity 検証 + timeout→escalation。deterministic exit code。 |
| org-escalation（3層記録: register + journal + markdown） | `.claude/skills/org-escalation/SKILL.md` | **reuse-as-is** | 判断仰ぎ・runaway 検知を人間にエスカレーション。autonomy boundary（自決範囲 vs 承認必須）の実装。 |
| pending_decisions.py（人間ゲート state machine） | `tools/pending_decisions.py`, `tests/test_pending_decisions.py` | **reuse-as-is** | append→resolve(to_user)→user reply→resolve(to_worker)。relay 忘れを deterministic に検知。 |
| org-suspend（graceful/force 2-pass shutdown） | `.claude/skills/org-suspend/SKILL.md` | **adapt** | 全 agent 状態の deterministic capture + 2-pass close。loop の suspend/checkpoint。 |
| journal_append（canonical event log） | `tools/journal_append.{py,sh}`, `docs/journal-events.md` | **reuse-as-is** | loop の全 lifecycle event を canonical log に記録。観測可能性の基盤。 |

### 3.8 再利用方針サマリ

調査原則（§2.7）と資産（§3.1–3.7）の対応:

- **状態 SoT（原則6）** ← `tools/state_db/`（**最重要・reuse-as-is/adapt**）。ループ iteration・収束履歴・終了評価の永続化に直接転用。
- **二重終了条件 + ハード上限（原則3,4）** ← state-semantics / delegation-lifecycle contract（型を reference）+ `state_db.queries` の predicate（adapt）。
- **wake 配送・通知（原則2,8）** ← transport（push一次/pull fallback, at-most-once）を **extract-pattern**。
- **self-improving（原則7）** ← org-retro/curate/knowledge を **adapt**（Reflexion の試行間メモリに対応）。
- **観測（原則9）** ← journal_append + attention-watcher（reuse-as-is/adapt）。OTel GenAI への対応付けは新規。
- **人間ゲート（原則8）** ← org-escalation + pending_decisions（**reuse-as-is**）。
- **入力選定（次反復対象）** ← work-discovery 二層分離（adapt）。

**最大の発見**: loop-agent が必要とする「終了条件を保存して再開」「状態の正本性」「フィードバック→改善」「観測→人間ゲート」は、**claude-org にすでに本番品質で存在する**。loop-agent は車輪の再発明をせず、これらを **runtime 非依存な抽象（state DB / transport / feedback / gate）として段階的に抽出・再利用**することが最短経路である。

---

## 4. LoopAgent 設計

### 4.1 要件と設計原則

§2 の調査と §3 の資産から、loop-agent の LoopAgent が満たすべき要件:

- **R1 検証可能ゴール駆動**: 自然言語の「完了判定」を避け、測定可能な完了基準（テスト green / lint / 状態遷移の収束 / チェックリスト）を必須とする。
- **R2 二重終了条件**: 意味的判定（critic/eval）＋機械的上限（反復・トークン・時間）を独立に実装し合成可能にする。
- **R3 暴走防止の不変条件**: per-call max_tokens・セッション/日次予算・turn カウンタ・サーキットブレーカ・無進捗検出をエンジンに内蔵。
- **R4 状態の外部 SoT**: ループ状態を DB に外出しし、resume・観測・監査を可能にする。
- **R5 二層ループ**: 内側 ReAct（実行）/ 外側 Reflexion（試行間改善）。記憶は意思決定への配線を eval で担保。
- **R6 限定的人間ゲート**: 不可逆・影響範囲大のアクションのみ interrupt（approve/edit/reject/respond）。
- **R7 観測可能性**: OTel GenAI 準拠の構造化イベントをループ各段で発火。
- **R8 simpler loops win**: PoC と本番を分離し段階導入。

### 4.2 アーキテクチャ複数案の比較

3案を比較する。評価軸は **実装コスト / 暴走耐性 / 観測性 / org 資産再利用度 / 適合スコープ**。

#### 案A: 単一プロセス・インライン LoopAgent（Agent SDK ラップ）

Claude Agent SDK の agent loop（`gather → act → verify → repeat`）を薄くラップし、`max_turns` / `max_budget_usd` と簡易 stop condition を載せただけの単一プロセス。状態は progress ファイル + git。

- **長所**: 最小実装（"simpler loops win"）。Anthropic 標準ループに完全準拠。即着手可能。
- **短所**: 試行間メモリ・収束判定・観測・人間ゲートが弱い。マルチエージェント協調や長時間自律には不足。org 資産の活用が限定的。

#### 案B: フルオーケストレーション型（claude-org をほぼそのまま multi-pane で踏襲）

secretary/dispatcher/worker/curator の4ペイン構成を loop coordinator/loop-agent/eval-agent 等に読み替え、renga/broker transport・state.db・全フィードバックループを丸ごと採用。

- **長所**: org 資産再利用度が最大。観測・人間ゲート・フィードバックが本番品質で揃う。
- **短所**: **重い**。pane/tmux/renga/broker 等の runtime 依存が大きく、loop-agent 単体プロジェクトには過剰。人間ゲートが組織運用前提で過多になりがち。PoC に向かない。

#### 案C: 単一制御層 + 共有状態機械 + 段階的 org 資産組込（**推奨**）

LangGraph 風の **状態機械（共有 state を `state.db` 相当の SoT に置く）** を制御層とし、その中で **内側 ReAct ループ + 外側 Reflexion ループ**を回す。終了条件は **合成可能な condition オブジェクト**（MaxIterations / TokenBudget / Timeout / GoalMet / NoProgress / HumanGate）。org 資産は **runtime 非依存な抽象（state DB / transport / feedback / gate）として段階的に抽出・組込**む。マルチエージェントは subagent で context 隔離（必要時のみ）。

- **長所**: §2.7 の全原則を最も自然に満たす。state.db・feedback・gate・transport の再利用度が高い一方、runtime 依存（pane/tmux）を**疎結合**にできる。PoC（案A 相当）から本格（案B 相当の資産）まで**同一アーキで連続的にスケール**できる。
- **短所**: 状態機械と condition 合成の初期設計コストが案A より高い。

#### 比較表

| 評価軸 | 案A（インライン） | 案B（フルオーケストレーション） | 案C（制御層+状態機械）★推奨 |
|---|---|---|---|
| 実装コスト（初期） | ◎ 最小 | △ 大 | ○ 中 |
| 暴走耐性 | △ 上限のみ | ◎ | ◎ 二重終了+不変条件 |
| 観測性 | △ | ◎ | ○→◎（OTel + journal） |
| org 資産再利用度 | △ 限定 | ◎ 最大（但し runtime 結合） | ◎ 抽象として最大、疎結合 |
| 試行間学習（Reflexion） | △ | ○ | ◎ 一級機能 |
| 適合スコープ | PoC | 組織運用 | PoC→本格を連続カバー |
| runtime 依存（pane/tmux/renga） | 低 | 高 | 低〜中（段階的） |

### 4.3 推奨案と根拠

**案C を推奨する。**

**根拠**:
1. 調査が示した業界標準（**二重終了条件**[^framework-common]、**内側ReAct+外側Reflexion**[^react][^reflexion]、**状態の外部 SoT**[^harness]、**合成可能 condition**[^autogen]、**限定的人間ゲート**[^hitl][^verify-hitl]）を**単一アーキで全て自然に満たす**のは案C のみ。
2. claude-org の最重要資産 `state.db`（SoT・transaction・post-commit snapshot・WAL 並行アクセス）が案C の「共有状態機械」に**ほぼそのまま嵌まる**（§3.4）。
3. **PoC→本格の連続性**: 案A を案C の最小構成（状態機械1ノード + ハード上限のみ）として実装でき、資産を段階的に足すだけで本格へ到達する。アーキの作り直しが不要（"simpler loops win" と段階導入の両立）。
4. runtime 依存（pane/tmux/renga/broker）を **transport 抽象 seam**（`tools/transport.py` パターン）で疎結合化でき、案B の重さを回避しつつ本番品質の通知・人間ゲートを後付けできる。

**却下理由**: 案A は試行間学習・観測・人間ゲートが構造的に不足し本格化で作り直しになる。案B は単体プロジェクトに対し runtime 結合が過剰で PoC に不適。

### 4.4 コアループの構造

推奨する LoopAgent のコア制御フロー（擬似コード。実装ではなく設計の骨格）:

```text
LoopAgent.run(goal, guardrails):
  state = StateDB.load_or_init(run_id)          # R4: 外部SoT。resume 対応
  conditions = compose(                          # R2/R3: 二重終了条件（合成可能オブジェクト）
      GoalMet(verifier),                         #   意味的: 検証可能ゴール（test/lint/rubric）
      MaxIterations(n), TokenBudget(b), Timeout(t),  # 機械的: ハード上限（不変条件）
      NoProgress(window=N, repeat=3),            #   無進捗/反復検出
      HumanGate(on=irreversible_actions))        # R6: 不可逆操作のみ
  emit(otel, "loop_begin", state)               # R7: 観測

  while not conditions.any_triggered(state):
    # ── 内側: ReAct エピソード（gather → act → verify）─────────
    ctx   = curate_context(state)               # context engineering（履歴を SoT から再構成）
    act   = model.decide(goal, ctx)             # Thought → Action
    if act.is_irreversible and HumanGate.active:
        decision = human_gate(act)              # approve/edit/reject/respond（state 永続化）
        if decision.rejected: state.record(decision); continue
    obs   = execute(act)                         # Action → Observation
    signal = verify(obs)                         # R1: ground truth（test/lint/exit-code）
    state.append_step(act, obs, signal)          # R4: transaction + journal event
    emit(otel, "loop_step", {act, signal, cost})

    # ── 外側: Reflexion（試行をまたぐ言語的自己改善）───────────
    if episode_ended(signal):
        reflection = reflect(state.trajectory, signal)   # 失敗→言語的指針
        state.memory.append(reflection)          # R5: episodic memory（次 ctx に「配線」）

  reason = conditions.first_triggered(state)
  emit(otel, "loop_end", {reason, state.metrics})
  return finalize(state, reason)                 # graceful: 上限到達は例外でなく制御出力
```

要点:
- **終了条件は while ガードに集約**し、いずれか発火で**理由付き graceful 終了**（OpenAI の error_handlers パターン[^openai-sdk]）。
- **verify なき step を作らない**（原則2）。verify は安価なルール（test/lint）を一次、LLM-as-judge は限定（原則: ground-truth 優先[^judge]）。
- **reflection は episode 境界でのみ**走り、反復上限と「改善頭打ちなら停止」で肥大化・劣化を防ぐ[^self-improve]。
- **state は毎 step transaction で永続化**（resume・観測・監査の単一根拠）。

### 4.5 ループ制御（終了・収束・予算・人間ゲート・暴走・観測）

| 制御 | 設計 | 由来資産 / 調査 |
|---|---|---|
| **終了条件** | GoalMet（意味的）+ MaxIterations/TokenBudget/Timeout（機械的）+ NoProgress を合成オブジェクトで OR 評価 | §2.5,§2.6 / state-semantics contract |
| **収束判定** | evaluator rubric 閾値超え or スコア改善量が閾値未満（頭打ち）or 反復上限 | §2.6（AWS reflect-refine） |
| **コスト制御** | per-call max_tokens + セッション/日次予算 + turn カウンタ + サーキットブレーカ。累積を state.db に記録し3xスパイク検知を後付け可能に | §2.6 / state_db |
| **人間ゲート** | 不可逆・影響範囲大のみ interrupt。approve/edit/reject/respond の4種。状態永続化で pause/resume | §2.6 / org-escalation + pending_decisions |
| **暴走防止** | 無進捗N・反復アクション3回の打ち切り + ハード上限 + 全停止スイッチ（CLAUDE_CODE_DISABLE_CRON 相当） | §2.3,§2.4 |
| **観測性** | OTel GenAI span（gen_ai.* + 反復番号 + 終了理由）+ journal_append event + attention watcher 連携 | §2.6 / journal_append + attention |

### 4.6 org 資産の活用方針（対応表）

| LoopAgent コンポーネント | 採用する org 資産 | 抽出形態 | 段階 |
|---|---|---|---|
| 共有状態機械の SoT | `tools/state_db/`（schema + StateWriter + queries + snapshotter + WAL） | runtime 非依存ライブラリとして adapt（loop カラム/event 追加） | MVP |
| 終了条件・状態の型 | state-semantics / delegation-lifecycle contract | reference（Loop State Semantics を新規策定） | MVP |
| wake 配送・通知 | transport（push一次/pull fallback, at-most-once）, peer_notify | パターン抽出 + transport seam | 本格 |
| self-improving | org-retro / org-curate / knowledge（4要素, 閾値起動, skill-candidates） | adapt（Reflexion memory + eval loop に接続） | 本格 |
| 観測・暴走検知 | journal_append, attention-watcher, pr_watch | reuse-as-is / adapt + OTel 追加 | MVP→本格 |
| 人間ゲート | org-escalation + pending_decisions（state machine） | reuse-as-is（role 読み替え） | MVP |
| 次反復の入力選定 | work-discovery（計算層 + 配達層二層分離, 決定的ツール） | adapt（計算層 reuse, 配達層は新設計） | 本格 |
| 状態保存・再開 | handover/resume パターン, resume-safe loop state | reuse-as-is | MVP |

---

## 5. 段階ロードマップ（PoC → MVP → 本格）

### Phase 1: PoC — 「最小ループ + ハード上限」

- **ゴール**: 案C の最小構成（状態機械1ノード）で `gather → act → verify → repeat` を回し、**機械的上限で確実に止まる**ことを実証する。
- **スコープ**: 単一エージェント・単一プロセス。verify は1種（例: テスト green）。終了は MaxIterations + TokenBudget + Timeout のみ。状態は最小（progress ファイル or 軽量 SQLite）。人間ゲートなし（不可逆操作を出さないタスクに限定）。
- **使う資産**: なし〜最小（決定的 exit-code ツールの作法、Agent SDK の `max_turns`/`max_budget_usd`）。
- **成功条件**: (a) 検証可能ゴール達成で自然終了、(b) 未達でも上限で必ず停止、(c) AutoGPT 的暴走（無限ループ・コスト爆発）を再現しないことを sandbox で確認。
- **リスク**: スコープを欲張ると "simpler loops win" を破る。1タスク種・1 verify に絞る。

### Phase 2: MVP — 「状態機械 + state.db SoT + 二重終了条件 + 観測」

- **ゴール**: PoC を案C 骨格へ拡張。**状態を state.db に外出し**し、**resume・観測・二重終了条件・限定人間ゲート**を備えた実用ループにする。
- **スコープ**: 内側 ReAct ループ確立。終了条件を合成オブジェクト化（GoalMet + 機械的上限 + NoProgress）。観測を journal event + OTel span で構造化。人間ゲートを org-escalation + pending_decisions で不可逆操作に限定導入。状態保存/再開（handover/resume）。
- **使う資産**: `tools/state_db/`（adapt: loop カラム/event 追加）、state/delegation contract（reference）、journal_append（reuse）、org-escalation + pending_decisions（reuse）、handover/resume（reuse）。
- **成功条件**: (a) 中断→resume が状態欠落なく動く、(b) 全終了理由が journal に残り事後解析できる、(c) 不可逆操作で人間ゲートが発火し approve/reject が反映される、(d) NoProgress 検出で循環を打ち切れる。
- **リスク**: state.db を runtime 非依存に抽出する際の結合度。最初に「loop 用最小スキーマ」を切り出し、org 本体と疎結合を保つ。

### Phase 3: 本格 — 「フィードバックループ + transport + 入力選定を統合した自律 LoopAgent」

- **ゴール**: 外側 Reflexion ループと self-improving、wake 配送、次反復の自律的入力選定を統合し、**長時間・複数タスクを自律で回す** LoopAgent にする。
- **スコープ**: 外側 Reflexion（試行間 episodic memory）を org-retro/curate/knowledge に接続（eval→反映→再評価を有限回）。transport（push一次/pull fallback）で wake/通知を二重化。work-discovery で次反復対象を提案（人間ゲート維持）。subagent で context 隔離した並行サブタスク。OTel 観測の dashboard 化と3xスパイク自動スロットル。
- **使う資産**: transport + peer_notify（extract-pattern）、org-retro/curate/knowledge（adapt）、work-discovery（adapt）、attention-watcher/pr_watch（adapt）、suspend（adapt）。
- **成功条件**: (a) 失敗トラジェクトリからの学びが次ループの context に「配線」され eval で改善が確認できる、(b) backend 不通でも pull fallback で配送が継続、(c) 暴走時に自動スロットル + 人間 alert、(d) 完了→次反復の接続が人間ゲート越しに自律で回る。
- **リスク**: self-improving の罠（出力肥大化・reward hacking・memory 汚染）。**早期停止・多様評価・性能測定と本番実行の分離（dual-component）・memory 取込前検証**を必須化する[^self-improve]。

```
Phase 1 (PoC)      Phase 2 (MVP)              Phase 3 (本格)
─────────────      ─────────────              ──────────────
最小ループ      →  状態機械+state.db SoT   →  +Reflexion/feedback
ハード上限         二重終了条件+観測          +transport(wake)
(案A相当)          限定人間ゲート/resume      +work-discovery(入力選定)
                   (案C骨格)                  (案C+org資産フル/案B級の堅牢性)
```

---

## 6. リスクと未解決論点

- **self-improving の安全性**: reflection の出力肥大化・劣化、reward hacking、memory 汚染（敵対環境での false lesson 注入）[^self-improve][^ooda]。→ Phase 3 で dual-component 分離・早期停止・取込前検証を不変条件化。
- **LLM-as-judge の信頼性**: position/self-preference バイアス[^judge]。→ ground-truth 検証（test/lint/string match）を一次、judge は rubric + キャリブレーション限定。
- **state.db の抽出度**: org 本体との結合。→ loop 用最小スキーマを切り出し疎結合化。SQLite は swap 可だが transaction SoT 性は維持必須。
- **transport の runtime 依存**: broker sidecar 等は runtime 所属で直接再利用不可[^framework-common]。→ パターン（push一次/pull fallback, at-most-once, role 別 cadence）のみ抽出し、loop-agent 側の配送実体は別実装。
- **概念の流動性**: Loop Engineering は2026年時点で急進化中の実務概念。→ 設計判断は Anthropic 公式 docs と論文系譜にアンカーし、ブログ主張は二次情報として扱う（本レポートの方針）。
- **governed workspace**: 本番自律には ID・スコープ権限・監査証跡・高速ロールバックが第6要素[^le-stack]。→ Phase 2 以降で監査ログ（journal）と rollback を設計に織り込む。

---

## 7. 付録

### 7.1 用語集

- **ground truth**: 各ステップで環境から得る客観的検証信号（tool 実行結果・テスト/lint の exit code 等）。
- **二重終了条件**: 意味的判定（critic/eval/特定文字列）と機械的上限（反復・トークン・時間）を独立に併設する設計。
- **at-most-once**: メッセージを drain したら消費確定し redelivery しない配送保証。受信側は idempotent handler を前提とする。
- **SoT（Source of Truth）**: 状態の唯一の正本。claude-org では `state.db`、派生物（markdown/JSON）は自動再生成。
- **Reflexion 型外側ループ**: 試行をまたいで失敗を言語的フィードバックに変換し episodic memory に蓄積して次試行を改善するループ。
- **escalate シグナル**: sub-agent が共有 state/event 経由でループ制御層に停止を通知する疎結合な終了プロトコル（ADK 由来）。

### 7.2 主要出典一覧

調査の主要出典（各主張の脚注に対応）。Loop Engineering 系のブログは実務家発の二次情報、Anthropic 公式 docs・arXiv 論文は一次アンカーとして扱った。

[^le-def]: Loop Engineering の定義（goal-based automation / trigger + verifiable goal）。 https://www.mindstudio.ai/blog/what-is-loop-engineering-ai-coding-agents , https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/
[^le-origin]: 用語の起源（Boris Cherny 発言の拡散、Addy Osmani / Peter Steinberger）。 https://www.productmarketfit.tech/p/stop-prompting-ai-and-start-building , https://datasciencedojo.com/blog/agentic-loops-explained-from-react-to-loop-engineering-2026-guide/
[^le-stack]: prompt→context→loop の3層スタックと governed workspace。 https://www.puppyone.ai/en/blog/what-is-loop-engineering-5-building-blocks-missing-one
[^ctx-eng]: Anthropic「Effective context engineering for AI agents」（agents = LLMs using tools in a loop）。 https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
[^oracle-loop]: Oracle「What is the AI agent loop」。 https://blogs.oracle.com/developers/what-is-the-ai-agent-loop-the-core-architecture-behind-autonomous-ai-systems
[^react]: ReAct（Yao et al., ICLR 2023, arXiv:2210.03629）。 https://arxiv.org/abs/2210.03629 , https://react-lm.github.io/
[^reflexion]: Reflexion（Shinn et al., NeurIPS 2023, arXiv:2303.11366）。 https://arxiv.org/abs/2303.11366
[^self-refine]: Self-Refine（Madaan et al., NeurIPS 2023, arXiv:2303.17651）。 https://arxiv.org/abs/2303.17651
[^plan-exec]: Plan-and-Execute（LangChain/LangGraph）。 https://www.langchain.com/blog/planning-agents
[^ooda]: OODA loop と security trilemma（Schneier）。 https://www.schneier.com/blog/archives/2025/10/agentic-ais-ooda-loop-problem.html
[^babyagi]: BabyAGI の系譜（105行 PoC・終了条件なし・9世代進化）。 https://babyagi.wiki/ , https://yoheinakajima.com/birth-of-babyagi/
[^autogpt-fail]: AutoGPT の失敗ケーススタディ。 https://github.com/vectara/awesome-agent-failures/blob/main/docs/case-studies/autogpt-planning-failures.md , https://en.wikipedia.org/wiki/AutoGPT
[^agentgpt]: AgentGPT（Reworkd）。 https://www.datacamp.com/tutorial/agentgpt , https://github.com/reworkd/agentgpt
[^simpler]: 「simpler loops win」/ notorious agent loops。 https://techtalkwithsriks.medium.com/notorious-agent-loops-c4cc05b859b5 , https://www.ibm.com/think/topics/babyagi
[^anthropic-bea]: Anthropic「Building Effective Agents」。 https://www.anthropic.com/engineering/building-effective-agents , https://www.anthropic.com/research/building-effective-agents
[^agent-sdk-loop]: Claude Agent SDK agent loop（gather→act→verify→repeat, max_turns/max_budget_usd）。 https://code.claude.com/docs/en/agent-sdk/agent-loop
[^agent-sdk-blog]: Building agents with the Claude Agent SDK（verify 3方式）。 https://claude.com/blog/building-agents-with-the-claude-agent-sdk
[^harness]: Anthropic「Effective harnesses for long-running agents」。 https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
[^scheduled]: Claude Code scheduled tasks（cloud/Desktop//loop の3層, 7日失効）。 https://code.claude.com/docs/en/scheduled-tasks
[^cursor-devin]: Cursor / Devin の自律ループ。 https://cursor.com/blog/agent-best-practices , https://cognition.ai/blog/devin-annual-performance-review-2025
[^adk]: Google ADK LoopAgent。 https://adk.dev/agents/workflow-agents/loop-agents/ , https://google.github.io/adk-docs/agents/workflow-agents/loop-agents/
[^autogen]: AutoGen termination conditions。 https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/tutorial/termination.html
[^langgraph]: LangGraph graph API / recursion_limit。 https://docs.langchain.com/oss/python/langgraph/graph-api
[^crewai]: CrewAI Flows / max_iter。 https://docs.crewai.com/en/concepts/flows , https://github.com/crewAIInc/crewAI/issues/3847
[^openai-sdk]: OpenAI Agents SDK running agents（max_turns/error_handlers）。 https://openai.github.io/openai-agents-python/running_agents/
[^framework-common]: フレームワーク共通の二重終了構造。 https://adk.dev/agents/workflow-agents/loop-agents/ , https://rajatpandit.com/ai-engineering/optimizing-langgraph-cycles/
[^term]: 終了戦略（自然完了/最大反復/目標達成/エラー, 5–10反復目安）。 https://www.mindstudio.ai/blog/what-is-an-agentic-loop-ai-coding-agents
[^loop-detect]: 無限会話防止（無進捗/反復検出の閾値）。 https://dev.to/alessandro_pignati/stop-the-loop-how-to-prevent-infinite-conversations-in-your-ai-agents-ekj
[^converge]: AWS evaluator reflect-refine loop。 https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/evaluator-reflect-refine-loop-patterns.html
[^cost]: エージェント暴走コスト対策。 https://relayplane.com/blog/agent-runaway-costs-2026 , https://www.truefoundry.com/blog/rate-limiting-ai-agents-preventing-llm-api-exhaustion
[^hitl]: LangGraph human-in-the-loop（interrupt, approve/edit/reject/respond）。 https://docs.langchain.com/oss/python/langchain/human-in-the-loop
[^otel]: OpenTelemetry GenAI observability。 https://opentelemetry.io/blog/2025/ai-agent-observability/ , https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions
[^self-improve]: self-improving agent の罠と緩和。 https://www.buildmvpfast.com/blog/ai-agent-self-improvement-recursive-accuracy-production-2026 , https://datagrid.com/blog/7-tips-build-self-improving-ai-agents-feedback-loops
[^judge]: LLM-as-judge のバイアス。 https://arxiv.org/abs/2406.07791 , https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
[^v-le-def]: 独立検証（Loop Engineering 定義, supported）。 https://smartscope.blog/en/generative-ai/methodology/loop-engineering-agent-loops-2026/ , https://www.firecrawl.dev/blog/loop-engineering
[^v-react]: 独立検証（ReAct, supported, 原論文逐語一致）。 https://arxiv.org/abs/2210.03629 , https://www.promptingguide.ai/techniques/react
[^v-babyagi]: 独立検証（BabyAGI 105行・終了条件なし, supported）。 https://babyagi.wiki/ , https://github.com/yoheinakajima/babyagi
[^v-bea]: 独立検証（Anthropic 最小構成 + workflow/agent 区別, supported）。 https://www.anthropic.com/research/building-effective-agents
[^v-adk]: 独立検証（ADK LoopAgent 終了2系統, supported）。 https://adk.dev/agents/workflow-agents/loop-agents/
[^v-framework]: 独立検証（フレームワーク共通の二重終了, supported）。 https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/tutorial/termination.html
[^verify-hitl]: 独立検証（人間ゲートは「全ステップ標準」でなく不可逆操作限定, partly-supported の補正）。 https://www.mindstudio.ai/blog/how-to-build-agentic-loop-claude-code , https://stevekinney.com/writing/agent-loops

---

*本レポートは loop-agent の調査・設計フェーズ成果物（v1.0, 2026-06-27）。調査は ultracode workflow による fan-out（19 エージェント: org 資産棚卸し7サブシステム + web 調査6サブ問 + 独立反証検証6件）で実施し、主張は上記出典で裏付け、主要主張は独立エージェントで反証検証した。*
