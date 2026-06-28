# Recipe — act / verify の per-call timeout / kill（Issue #42）

1 回の `act` / `verify` 呼び出し（モデル呼び出しやツール実行）が暴走したときに、**ループ全体を諦めずに**その 1 回だけを打ち切る機構です。`run_loop` / `async_run_loop` の `timeout=` 引数に渡します。全実装は async-first core（`_drive_loop`）に入っているので **sync / async 両 API へ同一挙動で効きます**。

> **whole-run `Timeout` *stop 条件* との違い** — `loop_agent.Timeout(seconds)` は累積 wall-clock を **iteration 境界で**上限化する stop 条件で、**進行中の step は中断しません**（「締切を過ぎたら新しい仕事を始めない」）。本 recipe の `timeout=` は **1 回の呼び出し**を中断するもので、別物です。両方を併用できます。

## 書き方

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations

result = run_loop(
    act=my_act, verify=my_verify,
    conditions=[MaxIterations(20)],
    # act は 30s、verify は 10s で打ち切り。超過したシームは諦めて次 iteration へ。
    timeout=TimeoutPolicy(act=30.0, verify=10.0, on_timeout="graceful"),
)
```

- `TimeoutPolicy(act=, verify=, default=, on_timeout=)` — シーム別の秒数。各シームは自分の値、無ければ `default`、それも無ければ無制限。
- `timeout=30.0`（裸の秒数）— 両シームに `default=30.0` の **graceful** を適用する短縮形。
- `timeout=None`（既定）— per-call timeout なし（追加コストゼロの従来パス）。

## 2 つのモード

| `on_timeout` | 超過時の挙動 |
|---|---|
| `"graceful"`（既定） | 当該シームを諦め、`goal_met=False` の **合成 step** を記録して **次 iteration** へ。ループは返り続ける（例外を投げない）。 |
| `"kill"` | 当該シームを cancel し、`SeamTimeout` を **ループ外へ送出**（`LoopResult` は返らない）。 |

graceful の合成 step は observation がマーカー文字列（`ACT_TIMEOUT_OBSERVATION` / `VERIFY_TIMEOUT_OBSERVATION`）になります。これは JSON ネイティブで hashable なので **永続化 / resume を通っても安定**し、`NoProgress` の既定 key（observation そのもの）で「timeout の連続」を検出できます:

```python
from loop_agent import run_loop, TimeoutPolicy, MaxIterations, NoProgress

# act が 3 連続で時間切れになったら「進んでいない」として打ち切る
result = run_loop(
    act=flaky_slow_act, verify=my_verify,
    conditions=[MaxIterations(100), NoProgress(window=3, repeat=3)],
    timeout=TimeoutPolicy(default=30.0, on_timeout="graceful"),
)
assert result.stop.name in ("no_progress", "max_iterations")
```

kill は明示的に止めたいとき:

```python
from loop_agent import async_run_loop, TimeoutPolicy, SeamTimeout, MaxIterations

try:
    result = await async_run_loop(
        act=call_model, verify=run_tests,
        conditions=[MaxIterations(20)],
        timeout=TimeoutPolicy(act=60.0, on_timeout="kill"),
    )
except SeamTimeout as e:
    print(f"{e.seam} が {e.seconds}s で打ち切られた")
```

## 既知の制限（プラットフォーム差）— 必読

per-call timeout を **実際に中断する**機構はシームの種別とプラットフォームで変わります。

| シーム | 中断機構 | 移植性 |
|---|---|---|
| **async**（`async def` / awaitable を返す） | asyncio の task cancel（`asyncio.wait` + `task.cancel()`）で await 点で実 cancel | **全プラットフォーム**（イベントループがあれば。= `async_run_loop`） |
| **sync**（POSIX main thread） | `SIGALRM`（`signal.setitimer`）で実中断 | Linux / macOS の **main thread のみ** |
| **sync**（Windows / 非 main thread） | 強制中断 **不能** | — |

`SIGALRM` が使えない環境（Windows、または非 main thread）では同期シームのブロッキング呼び出しを強制的に止められません。そのため:

- **`graceful`** — 呼び出しが **返ってきた後**に超過を検出する best-effort になります（`time.sleep` のような **本当にハングした呼び出しは縛れません**）。確実に縛りたいなら **async シームにする**（`run_in_executor` で同期処理を包む等）。
- **`kill`** — 同期シームに対しては、呼び出しに入る **前**に `UnsupportedTimeoutKill` を送出します（縛れない hard kill を黙ってハングさせるより、明示エラーで失敗させる方針）。async シームなら asyncio の task cancel で止まるので問題ありません。

その他の注意:

- **async の cancel は協調的**。timeout 時は seam の task を cancel し（次の await 点で `asyncio.CancelledError`）、**cleanup を待たず即座に** timeout を報告します（締切時点で task が pending かで判定するので、`CancelledError` を握り潰して値を返す seam でも kill は効き、ループはハングしません。seam 自身が投げた `asyncio.TimeoutError` は別物として伝播します）。`CancelledError` を握り潰して完了しない seam は **orphan background task としてリーク**するだけ（ループはブロックしない）なので、timeout を効かせる seam で `CancelledError` を catch-and-ignore しないこと。`async def` seam 内の**同期ブロッキング区間**も、次に await するまでは中断できません。
- **per-call budget は単一**。締切は 1 回の呼び出し全体に効きます。「同期ブロッキング → awaitable を返す」変則 seam でも、同期区間で消費した実時間は await 区間（`asyncio.wait`）の予算から差し引かれ（同期区間で締切を使い切っていれば即 timeout）、合計は単一の締切で縛られます（~2 倍にはなりません）。
- **人間ゲートとの併用**: `graceful` で gated action（`GATE_PROCEED` + `on_complete`）が時間切れになった場合、合成 step を記録した後にリースを **executed 確定**します（その action は一度だけ消費され、再試行されない）。`kill` ではリースを in-progress のまま残す（`SeamTimeout` が完了確定の前に抜ける）ので、失効して別プロセスが取り直します（クラッシュ復旧経路）。
- **締切は実 wall-clock 基準**。`SIGALRM` / `asyncio.wait` の予算はループの注入クロック `time_fn` を見ません（`time_fn` は stop 条件用クロックと、非 `SIGALRM` の post-hoc 計測（完了した同期呼び出しの検出）にのみ作用）。
- **`SIGALRM` は再入不可**（プロセス共有の `ITIMER_REAL` を使うため）。timeout を効かせた同期シームの内側で、さらに `SIGALRM` ベースの timeout を仕掛けるのは避けてください。組み込み先アプリが自前の `ITIMER_REAL` を張っていても、本機構は呼び出し終了時に**元のタイマーを復元**します（残り時間を再 arm、呼び出し中は実質一時停止）。
- kill で中断された同期シームは任意の地点で `BaseException` が上がるため、副作用が途中状態になりうる（preemptive な timeout 一般の性質）。`kill` を選ぶ側の責任で、その step は失敗扱いにしてください。
