# 新しい act アダプタの書き方

loop-agent の `act` シームは「1 反復の実行体」を 1 つの関数
（`Callable[[context], ActOutcome]`）に閉じ込めます。外部のエージェント CLI
（Claude Code・Codex・将来の任意のツール）を headless 起動して `act` に差し込む
ものを **アダプタ** と呼びます。`loop_agent.adapters` には参照実装として
[`ClaudeCodeAct`](../../src/loop_agent/adapters/claude_code.py) と
[`CodexAct`](../../src/loop_agent/adapters/codex.py) があり、両者は
**subprocess コマンド・フラグ・token/output 解析だけ** が違い、結果の形と
プロンプト整形は完全に同型です。

この文書は、3 つ目以降のアダプタ（例: `GeminiAct`・`AiderAct`・社内ツール）を
**同じ契約に従って正しく書く** ための canonical な手引きです。落とし穴は
[`ClaudeCodeAct` / `CodexAct` が既に踏んだもの](#hard-won-lessons実走で踏んだ落とし穴)
を 1 か所に集約してあります。まずそこを読んでから書き始めると速いです。

> アダプタは loop-agent の新機能ではありません。`act` シームで user が今日でも
> 書けるパターンを、落とし穴ごと正規化したものです。新しいアダプタも
> 「`ActOutcome` を返す関数」である限り、コア（`run_loop`）を一切変更せずに差し込めます。

---

## act シームの契約（4 か条）

アダプタは loop コアの性質（境界で必ず止まる / 予算が効く / 認証は外部に委譲）を
壊さないために、次の 4 つを必ず守ります。`ClaudeCodeAct` / `CodexAct` はいずれも
これを満たしています。

1. **例外でループを殺さない。** timeout 超過・非 0 終了・実行ファイル不在など
   「実行できなかった/失敗した」は、例外を送出せず `failed=True` の結果を
   `ActOutcome.observation` に載せて graceful に返します。これにより verify が
   `outcome.observation.failed` を見て続行/終了を判断でき、境界で評価される
   `Timeout` / `MaxIterations` は常に効きます。**外に漏らしてよい例外は原則ゼロ**
   です（`subprocess.TimeoutExpired` と `OSError` は捕まえて `failed` に変換する）。
   唯一の例外は `render_prompt`: `prompt_template` が context に無いフィールドを
   参照していると **eager に `KeyError` を送出** します（実行前の設定ミスは握り潰さず
   即失敗させる設計。下のスケルトンでも `render_prompt` は `try` の外で呼ぶ）。

2. **token を予算に積む。** 応答から処理トークン総数を取り出し
   `ActOutcome.tokens` に載せます。driver がこれを `state.tokens_used` に積むので
   `TokenBudget` がそのまま効きます。**取れないときは 0**（テキスト出力で usage が
   無いのは正常。0 は安全側）。トークンは **成否に関わらず計上** します（失敗試行も
   実際にトークンを消費しうる）。

3. **auth は CLI に委譲する。** 子プロセスは既定で起動側の `os.environ` を継承し、
   外部 CLI の既存セッション（`~/.claude` / `~/.codex` 等のログイン）を第一義に
   使わせます。API キー（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 等）が環境にあれば
   CLI 側のフォールバックとして働きます。アダプタ自身がキーを読んだり貼ったりしない
   こと。秘匿値を注入したいときは `env=` で **上書きマージ** する経路のみ用意します。

4. **stdin を塞ぐ（ハング防止）。** headless ループでは親 stdin が pipe/閉端の
   ことがあり、子 CLI が「追加入力」を読みに行くと **ハング** します。プロンプトは
   必ず位置引数（`--` の後ろ）で確定させ、対話入力を読む CLI には
   `stdin=subprocess.DEVNULL` を渡します（[Codex の実害](#1-codex-は-stdin-が-pipe-だと追加入力を読みハングする)を参照）。

---

## 結果の形（`ActResult` 契約）

`ActOutcome.observation` に載せる結果オブジェクトは、共通の構造的契約
[`ActResult`](../../src/loop_agent/adapters/base.py)（`Protocol`）に従います。
8 フィールドと `__str__`（応答本文を返す）を持ちます:

| フィールド | 型 | 意味 |
|---|---|---|
| `text` | `str` | アシスタント応答の本文。`str(result)` も同じ本文を返す。 |
| `tokens` | `int` | この呼び出しが消費したトークン総数（予算計上用）。 |
| `failed` | `bool` | 失敗（非 0 終了 / CLI 報告エラー / timeout / 起動失敗）か。 |
| `returncode` | `Optional[int]` | 子プロセスの終了コード（起動失敗・timeout では `None`）。 |
| `error` | `str` | 失敗時の簡潔なエラー本文（成功時は空文字）。 |
| `stdout` / `stderr` | `str` | 子プロセスの生出力（デバッグ・再解析用）。 |
| `command` | `tuple[str, ...]` | 実際に実行したコマンド（引数列）。 |

自分のアダプタの Result は、共通の具体 dataclass
[`ActResultBase`](../../src/loop_agent/adapters/base.py) を継承するのが最短です。
全フィールドに既定値があるので、`@dataclass` を付けて docstring を足すだけで、
8 フィールドの形・キーワード生成・`str(result)` -> 本文 がそのまま揃います:

```python
from dataclasses import dataclass
from loop_agent.adapters import ActResultBase

@dataclass
class GeminiResult(ActResultBase):
    """1 回の Gemini 呼び出しの構造化結果。"""
    # フィールド再定義は不要。8 フィールドを ActResultBase から継承する。
```

> なぜ Protocol と base dataclass の両方があるのか:
> **`ActResult`（Protocol）は「満たすべき契約」**、**`ActResultBase`（dataclass）は
> 「契約を満たす最短の実装」** です。結果を別の dataclass で独自に作っても、8
> フィールド + `__str__` を持てば `ActResult` 契約に構造的に適合します
> （`isinstance(result, ActResult)` も `True`）。異種アダプタを混ぜたチェーンでも
> verify 側は `ActResult` だけを見ればよく、合成性が保てます。

---

## アダプタ本体の骨格

`ClaudeCodeAct` / `CodexAct` と同じ骨格を `@dataclass` で書きます。要点だけ抜き出すと:

```python
import os, subprocess
from dataclasses import dataclass
from typing import Any, Optional, Mapping
from loop_agent import ActOutcome
from loop_agent.adapters import Runner, render_prompt   # 共通の実行シーム/整形

@dataclass
class GeminiAct:
    timeout: float = 600.0
    prompt_template: str = "{prompt}"
    env: Optional[Mapping[str, str]] = None
    gemini_bin: str = "gemini"
    cwd: Optional[str] = None
    runner: Optional[Runner] = None        # テストで subprocess.run を差し替える注入点

    def build_command(self, prompt: str) -> list[str]:
        cmd = [self.gemini_bin, "...flags..."]
        cmd += ["--", prompt]              # プロンプトは必ず "--" の後ろの位置引数
        return cmd

    def _build_env(self) -> dict[str, str]:
        base = dict(os.environ)            # 既存 CLI セッションを継承
        if self.env:
            base.update(self.env)          # env= で上書きマージ(秘匿値はこの経路)
        return base

    def __call__(self, context: Any) -> ActOutcome:
        prompt = render_prompt(self.prompt_template, context)
        command = self.build_command(prompt)
        run = self.runner or subprocess.run
        try:
            proc = run(
                command, capture_output=True, text=True,
                timeout=self.timeout, env=self._build_env(), cwd=self.cwd,
                stdin=subprocess.DEVNULL,  # 対話入力を読む CLI ならハング防止に必須
            )
        except subprocess.TimeoutExpired:
            return ActOutcome(observation=GeminiResult(
                failed=True, error=f"timeout ({self.timeout:g}s)",
                command=tuple(command)), tokens=0)
        except OSError as exc:             # 実行ファイル不在/権限なし(FileNotFound 等)
            return ActOutcome(observation=GeminiResult(
                failed=True, error=f"could not launch {self.gemini_bin!r}: {exc}",
                command=tuple(command)), tokens=0)

        stdout, stderr = proc.stdout or "", proc.stderr or ""
        text, tokens, is_error = _parse_result(stdout, stderr)   # CLI 固有の解析
        failed = proc.returncode != 0 or is_error
        error = (stderr.strip() or text.strip() or f"exit={proc.returncode}") if failed else ""
        result = GeminiResult(text=text, tokens=tokens, failed=failed,
                              returncode=proc.returncode, error=error,
                              stdout=stdout, stderr=stderr, command=tuple(command))
        return ActOutcome(observation=result, tokens=tokens)  # tokens は成否に依らず計上
```

CLI 固有なのは `build_command`（フラグ）と `_parse_result`/`parse_tokens`
（output/token 解析）だけです。`render_prompt` / `Runner` / `_build_env` の形・
4 か条の守り方は全アダプタ共通なので、上の骨格をそのまま写せます。

---

## token 計上の注意点（最重要）

トークン解析は **二重計上が最も起きやすい** 箇所です。アダプタごとに usage の
意味論が違うので、各 CLI のスキーマを確認してから合算ルールを決めます。

- **Claude Code**: `usage` の `input_tokens` / `output_tokens` /
  `cache_creation_input_tokens` / `cache_read_input_tokens` は互いに素な加算
  バケットですが、計上するのは **`input_tokens + output_tokens +
  cache_creation_input_tokens` の 3 種だけ** です（`_sum_token_fields` の
  allowlist `_COUNTED_TOKEN_FIELDS`）。`cache_read_input_tokens` は **除外** します
  ―― 課金重みが軽く（通常 input の ~0.1x で実質ほぼ無料）、内部マルチターンで
  毎ターン cache を読み直すため累積が桁違いに膨らみ、`TokenBudget` を誤発火させる
  からです（[Issue #55](#2-token-を二重計上すると-tokenbudget-が誤発火する)）。
- **Codex / OpenAI**: `usage` の `cached_input_tokens` は `input_tokens` の、
  `reasoning_output_tokens` は `output_tokens` の **部分集合** です。全部足すと
  二重計上になるので、総処理量は **`input_tokens + output_tokens` のみ** を足し、
  内訳が無く `total_tokens` だけのときはそれにフォールバックします
  （`_sum_codex_tokens`）。

> **必ず CLI の usage スキーマを確認し、「加算バケットか / 部分集合か」を見極めて
> から合算ルールを書く。** 「全フィールド足す」をコピーすると、部分集合を持つ CLI で
> 静かに二重計上し、`TokenBudget` が早期誤発火します（[Issue #55 の bug class](#2-token-を二重計上すると-tokenbudget-が誤発火する)）。

JSON/JSONL で usage が取れないとき用の **正規表現フォールバック** も、部分集合
キーに誤マッチしないよう先頭引用符でアンカーします（`"input_tokens"` だけに当て、
`"cached_input_tokens"` には当てない）。複数ソース（stdout/stderr）で **合算しない**
（最初にヒットしたソースの値を返す）のも二重計上回避のためです。

---

## hard-won lessons（実走で踏んだ落とし穴）

### 1. Codex は stdin が pipe だと「追加入力」を読みハングする

headless ループでは親 stdin が pipe/閉端のことがあり、`codex exec` はそれを
「追加入力」と解釈して読みに行き、**プロンプトを位置引数で渡していてもハング**
します。`stdin=subprocess.DEVNULL` を渡して塞ぐのが必須です。対話入力を読む CLI を
アダプタ化するときは、まずこれを疑ってください。

### 2. token を二重計上すると TokenBudget が誤発火する

Self-translation PoC で `ClaudeCodeAct` の初期実装が `cache_read` を毎反復累積し、
`TokenBudget` を実際よりずっと早く発火させる bug が見つかりました（Issue #55）。
原因は「usage の全フィールドを足す」ロジックが、課金が軽く累積で膨らむ
`cache_read_input_tokens` まで貪欲に拾っていたこと。**修正済み**: 計上対象を
`input_tokens + output_tokens + cache_creation_input_tokens` の allowlist に絞り、
`cache_read` は除外しました（token-cost ポリシ。`_sum_token_fields`）。
**新しいアダプタを足すたびに「コストでない/部分集合の usage を計上していないか」の
parametrize テストを必ず追加** してください
（[`tests/adapters/test_contract.py`](../../tests/adapters/test_contract.py) の
token guard が全アダプタ横断で構造的に catch します）。

### 3. CLI の `--json` スキーマはバージョンで揺れる

`codex exec --json` のイベント型は dotted（`item.completed`）と snake_case
（`item_completed` / `task_complete`）がバージョンで揺れます。応答本文の在処も
`item.completed` の `agent_message` / 直接の `agent_message` イベント /
ストリーミング delta / 完了イベントの `last_agent_message` と複数あります。
**代表形を網羅して「どれか取れれば本文」とし、完全な本文 > last_message > delta
連結の優先順位** で拾うと壊れにくくなります。Claude Code 側も `--output-format`
が `json` と `stream-json` で形が違い、後者は最終 `result` 行を拾います。
**実 CLI の出力を 1 度キャプチャしてからスキーマを書く** のが安全です。

### 4. 可変長オプションがプロンプトを飲む

`--allowed-tools <tools...>` や `--add-dir <path>` のような **値を取る/可変長の
オプション** は、区切り無しだと直後のプロンプトを「次の値」として貪欲に飲み込み、
CLI がプロンプトを失って空リクエスト or timeout までハングします。POSIX 慣例の
`--` でオプション解析を打ち切り、プロンプトを位置引数に確定させます
（`cmd += ["--", prompt]`）。

---

## Mock の書き方（テスト用差し替え点）

subprocess を使わずに `act` 契約を満たす in-memory 版を用意すると、ループの
組み立て・`TokenBudget`・失敗系を高速に検証できます。`MockClaudeCodeAct` /
`MockCodexAct` と同じ契約で書きます:

- `responses`（`str` / `Mapping` / Result のいずれか）を順に返し、使い切ったら
  最後の応答に張り付く（`MaxIterations` 等の境界で安全に止まる）。
- `str` -> `text`（tokens 0）、`Mapping` -> Result フィールド展開、Result -> そのまま。
- レンダリング済みプロンプトを `prompts` に記録し、テストから検証できる。
- `responses=[]` は `ValueError`、未対応の型は `TypeError`。

```python
from loop_agent.adapters import MockClaudeCodeAct
act = MockClaudeCodeAct(responses=[{"text": "work", "tokens": 1200}, "DONE"])
```

---

## テストの書き方

3 層で検証します。最初の 2 層は **共通ハーネスに登録するだけ** で大半が揃います。

1. **共通ハーネス（横断契約）** —
   [`tests/adapters/conftest.py`](../../tests/adapters/conftest.py) の `AdapterSpec`
   に自分のアダプタ（Act / Result / Mock / `parse_tokens` / 成功時 stdout サンプル /
   token guard サンプル / stdin 期待値）を 1 つ登録すると、
   [`tests/adapters/test_contract.py`](../../tests/adapters/test_contract.py) の
   parametrize 群（結果の形 / `failed` セマンティクス / timeout graceful /
   起動失敗 graceful / **token 二重計上ガード** / 予算計上 / Mock 契約 /
   auth 環境継承 / stdin 安全性）が自動的に自分のアダプタにも適用されます。
2. **mock 経由のループ** — `run_loop` に Mock を差し込み、`goal_met` や
   `TokenBudget` 停止が期待通りかを subprocess 無しで確認します。
3. **実 subprocess（CLI 固有）** — `sys.executable` をインタプリタにした
   フェイク実行ファイル（その CLI の出力フォーマットを `print` するスクリプト）を
   `tmp_path` に書き、`<bin>_bin=` で差し替えて実起動経路を 1 度通します。
   token 解析（`parse_tokens`）の CLI 固有ケースもここで固定します。

実 CLI が無い CI でも 1〜2 はフェイク runner / フェイク実行ファイルで完結します。
`codex` / `claude` 実バイナリに触る統合テストは、未導入環境で skip する設計に
してください。

---

## 新規アダプタ追加チェックリスト

- [ ] `XxxResult(ActResultBase)` を定義（8 フィールド再定義しない）。`isinstance(r, ActResult)` が `True`。
- [ ] `XxxAct` が `@dataclass` で `runner` 注入点・`<bin>_bin` 差し替え・`cwd`・`env` を持つ。
- [ ] `build_command` がプロンプトを `--` の後ろの位置引数に置く。
- [ ] `__call__` が `TimeoutExpired` / `OSError` を捕まえ `failed=True` で graceful に返す（例外を漏らさない）。
- [ ] 対話入力を読む CLI なら `stdin=subprocess.DEVNULL`。
- [ ] token 解析が CLI の usage 意味論（加算バケット / 部分集合）に従い、二重計上しない。usage 無しは 0。
- [ ] token は成否に関わらず計上する。
- [ ] `_build_env` が `os.environ` 継承 + `env=` 上書きマージ（auth は CLI 委譲）。
- [ ] `MockXxxAct` を提供（`str` / `Mapping` / Result、空は `ValueError`、未対応型は `TypeError`）。
- [ ] `tests/adapters/conftest.py` の `AdapterSpec` に登録し、共通契約テストを通す。
- [ ] **token 二重計上ガード**のサンプル（部分集合キーを含む usage と期待トークン）を spec に入れる。
- [ ] 実 subprocess 経路（フェイク実行ファイル）の成功 / timeout / env 継承を 1 度ずつ通す。
- [ ] `loop_agent.adapters.__init__` の `__all__` に公開シンボルを追加。
- [ ] `mypy` / `pytest` green。
