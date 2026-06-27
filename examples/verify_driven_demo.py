#!/usr/bin/env python3
"""検証駆動デモ: sandbox のテストが green になるまで gather->act->verify を反復する。

claude-loop のループコアを *実コード* に当てる具体デモ。一時ディレクトリに
わざと壊した関数 ``add`` とその pytest を書き出し、検証(verify)が実際の
pytest の exit-code を見て green になるまでループを回す。

シナリオ:

1. sandbox に ``add`` の壊れた実装(``a - b``)と ``test_add.py`` を用意する。
2. act    = 反復ごとに「次の修正候補」を ``add.py`` へ書き込む(修正役のスタブ)。
   候補は [引き算 -> 掛け算 -> 正しい足し算] の順で、3 手目で初めて正しくなる。
3. verify = sandbox で pytest を subprocess 実行し exit-code を読む(ground truth)。
   exit-code 0(green)で ``goal_met=True`` となり、ループは *自然終了* する。
4. どの候補でも直らない場合でも、``MaxIterations`` 等のハード上限が必ず止める
   (暴走防止。本格実証は #7)。

実行:

    python3 examples/verify_driven_demo.py

このモジュールは ``tests/test_verify_demo.py`` からも import され、ここで定義した
シナリオそのものが pytest で実走検証される(出荷物 == 検証対象)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

from claude_loop import (
    LoopResult,
    MaxIterations,
    StepRecord,
    Timeout,
    TokenBudget,
    run_loop,
)
from claude_loop.demo import (
    CandidateApplier,
    ExitCodeVerifier,
    attempt_index,
    write_sandbox,
)
from claude_loop.state import LoopState

# -- sandbox の中身 --------------------------------------------------------

TARGET_FILENAME = "add.py"
TEST_FILENAME = "test_add.py"

# 修正候補。0,1 番目は red のまま、2 番目で初めて全テストが green になる。
BROKEN_SUBTRACT = "def add(a, b):\n    return a - b\n"   # add(2,3) -> -1 (red)
BROKEN_MULTIPLY = "def add(a, b):\n    return a * b\n"   # add(2,3) ->  6 (red)
CORRECT_ADD = "def add(a, b):\n    return a + b\n"       # add(2,3) ->  5 (green)

DEFAULT_CANDIDATES: tuple[str, ...] = (
    BROKEN_SUBTRACT,
    BROKEN_MULTIPLY,
    CORRECT_ADD,
)

TEST_SOURCE = (
    "from add import add\n"
    "\n"
    "\n"
    "def test_add_small():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
)


class DemoRun(NamedTuple):
    """1 回のデモ実走の結果と、観測に使ったフックの記録。"""

    result: LoopResult
    act: CandidateApplier
    verify: ExitCodeVerifier


def prepare_sandbox(workdir: Path) -> None:
    """sandbox にテストと(初期状態として)壊れた実装を書き出す。

    初期 ``add.py`` を壊した状態にしておくので、ループ開始前に手で
    ``pytest`` を回すと red になる(= 直すべき対象があることを示す)。
    """
    write_sandbox(
        workdir,
        {
            TEST_FILENAME: TEST_SOURCE,
            TARGET_FILENAME: BROKEN_SUBTRACT,
        },
    )


def run_repair(
    workdir: Path,
    *,
    candidates: Sequence[str] = DEFAULT_CANDIDATES,
    conditions: Optional[list] = None,
    on_step=None,
) -> DemoRun:
    """``workdir`` を sandbox として、テストが green になるまで修正ループを回す。

    Args:
        workdir: sandbox にするディレクトリ(呼び出し側が用意・後始末する)。
        candidates: 反復ごとに当てる ``add.py`` のソース列。
        conditions: stop 条件。省略時は実用的なハード上限の合成を使う。
        on_step: 各反復完了後に呼ばれる観測フック。
    """
    prepare_sandbox(workdir)

    act = CandidateApplier(
        target=workdir / TARGET_FILENAME,
        candidates=candidates,
        cost_per_step=10,
    )
    verify = ExitCodeVerifier(workdir=workdir)

    if conditions is None:
        conditions = [MaxIterations(10), TokenBudget(1000), Timeout(60.0)]

    result = run_loop(
        act=act,
        verify=verify,
        conditions=conditions,
        gather=attempt_index,
        on_step=on_step,
    )
    return DemoRun(result=result, act=act, verify=verify)


def _print_step(record: StepRecord, _state: LoopState) -> None:
    status = "GREEN" if record.goal_met else "red  "
    print(
        f"  iter {record.iteration}: {record.observation:<20} "
        f"-> verify={status} ({record.detail})"
    )


def main() -> int:
    print("=== claude-loop verification-driven demo ===")
    print("goal: keep gather->act->verify until the sandbox tests are GREEN")
    print("verify = real pytest exit-code (ground truth, not an LLM judge)\n")

    with tempfile.TemporaryDirectory(prefix="claude-loop-demo-") as tmp:
        workdir = Path(tmp)
        run = run_repair(workdir, on_step=_print_step)
        result = run.result

        print("\n--- result ---")
        print(f"status     : {result.status}")
        print(f"reason     : {result.reason}")
        print(f"iterations : {result.iterations}")
        print(f"tokens     : {result.tokens_used}")
        print(f"exit-codes : {run.verify.exit_codes}  (0 == tests green)")

    # 検証可能ゴール(テスト green)に到達してループが自然終了したことを保証する。
    ok = result.goal_met and result.stop is None
    print("\nOK: loop terminated naturally on a verified GREEN goal."
          if ok else "\nNG: loop did not reach the verified goal.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
