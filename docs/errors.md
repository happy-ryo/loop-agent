# 例外階層（LoopError）

loop-agent が送出する例外は、すべて単一の基底 `LoopError` から派生する（Issue #43）。
これにより「このライブラリ由来のエラー」を 1 か所で捕捉できる:

```python
from loop_agent import LoopError

try:
    run_loop(act=..., verify=..., conditions=...)
except LoopError as exc:
    # loop_agent 由来のあらゆるエラーをここで扱える
    ...
```

## 階層

```
LoopError(Exception)                              ライブラリ全エラーの基底
├── ConfigError(LoopError, ValueError, TypeError)   引数の値/型が不正・設定ミス
├── StateError(LoopError, ValueError, RuntimeError) 実行時の不変条件/ライフサイクル違反
└── AsyncSeamInSyncLoop(LoopError, RuntimeError)    同期 run_loop に非同期シーム（#40）
```

正準定義は `loop_agent.errors`。`loop_agent` トップレベル（`from loop_agent import LoopError, ConfigError, StateError, AsyncSeamInSyncLoop`）と、後方互換のため `loop_agent.cli.ConfigError` / `loop_agent._async.AsyncSeamInSyncLoop` からも同一クラスが参照できる。

### 各型の意味

| 型 | いつ送出されるか | 例 |
|----|------------------|-----|
| `ConfigError` | ライブラリが **明示的に検証**している引数の **値** が不正、または明示的な **型/形状チェック**に反する、もしくは run の設定ミス（construction / 呼び出し時の検証）。CLI の TOML / 引数パースの設定エラーも含む | `MaxIterations(-1)`、空文字の id、`conditions` が `AnyOf`/sequence でない、フック/resolver の戻り値型が不正、未知の enum 値、`[act]` テーブル欠落 |
| `StateError` | 実行時の **不変条件 / 状態** 違反。「不正な入力」ではなく「その状態では許されない操作」 | 既に解決済みの gate 決定の再解決、未解決/実行不能な決定の execute/lease、resume 時に提案 action が記録と不一致、未知の gate disposition、driver の防御的 invariant |
| `AsyncSeamInSyncLoop` | 同期 `run_loop` に awaitable なシーム（`act`/`verify`/`gather`/`condition.check`/`gate.review`/`on_step`/`on_complete`）が渡された | 非同期フックには `await async_run_loop(...)` を使う（#40） |

> `ConfigError` はライブラリ **自身**の検証を包む。型ヒントに反する値を未チェックの数値経路へ渡した場合（例: `MaxIterations(None)`）は、その演算が素の `TypeError` を送出する（Python 標準の挙動で、ここでは包まない）。

## 後方互換（multiple inheritance）

この階層を導入する前、これらの箇所は組み込み例外 `ValueError` / `TypeError` /
`RuntimeError` を直接送出しており、本プロジェクトのテストや外部の呼び出し側はそれらを
`except` していた。**破壊的変更を避けるため**、各 leaf は従来送出していた組み込み例外も
多重継承する:

- `ConfigError` は `ValueError` かつ `TypeError`
- `StateError` は `ValueError` かつ `RuntimeError`
- `AsyncSeamInSyncLoop` は `RuntimeError`

したがって旧 API に対して書かれた `except ValueError` / `except TypeError` /
`except RuntimeError` はそのまま動作し、新しいコードは精密な `LoopError` サブ型
（または `LoopError` 自体）を捕捉できる:

```python
from loop_agent import ConfigError, StateError

try:
    MaxIterations(-1)
except ConfigError:   # 精密: 設定ミス
    ...
except ValueError:    # 旧来: これでも依然として捕捉できる
    ...
```

> 組み込み例外の基底は互換シムであり、将来のメジャーバージョンで外す可能性がある。
> 新規コードは `LoopError` か具体的なサブ型を捕捉すること。

## エラーチェーン

組み込み例外を翻訳する箇所は `raise ... from exc` で原因を保全する。例えば
`loop_agent.transport` は JSON 化不能な `Wake.payload` の `TypeError` を `ConfigError`
へ翻訳しつつ、元の例外を `__cause__` に残す:

```python
try:
    transport_enqueue(wake_with_unserializable_payload)
except ConfigError as exc:
    assert isinstance(exc.__cause__, TypeError)  # 原因を辿れる
```

## 階層外の 1 例外: prompt template の KeyError

`loop_agent.adapters.base.render_prompt` は、prompt template が context に無いフィールドを
参照していた場合 **意図的に `KeyError` を送出**する（`str.format` / `dict` の KeyError 意味論を
そのまま踏襲し、`except KeyError` でキー欠落として扱えるようにするため）。これは `LoopError`
階層に **属さない唯一の組み込み例外**で、設計上の意図的な選択である。
