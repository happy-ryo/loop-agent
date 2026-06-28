"""検証駆動デモの実走検証(report.md R1 / Phase 1, Issue #6)。

ここで検証する命題:

1. 検証可能ゴール(sandbox テストが green)に到達した瞬間にループが *自然終了*
   する -- これを実際の pytest を subprocess 実行して再現・確認する。
2. 検証は LLM judge ではなく *実テストの exit-code* が ground truth であり、
   ループの終了が exit-code 0 と一致する。
3. どの候補でも直らない場合でも、ハード上限(MaxIterations)で必ず止まる
   (暴走防止。本格実証は #7)。

``examples/verify_driven_demo.py`` のシナリオ「そのもの」を import して回すので、
出荷するデモと検証対象が一致する。
"""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
from pathlib import Path

# examples/ をパスに載せ、出荷デモのシナリオを直接 import する。
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

import verify_driven_demo as demo  # noqa: E402

from loop_agent import MaxIterations  # noqa: E402
from loop_agent.demo import (  # noqa: E402
    DEFAULT_TEST_COMMAND,
    ExitCodeVerifier,
    sandbox_env,
)


# -- 1 & 2: green 到達で自然終了し、exit-code が ground truth である --------


def test_loop_terminates_naturally_when_sandbox_turns_green(tmp_path):
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    result = run.result

    # 自然終了(ゴール達成): どのハード上限も発火していない。
    assert result.goal_met is True
    assert result.status == "goal_met"
    assert result.stop is None
    assert result.reason == "goal met"

    # 3 手目(正しい足し算)を当てた直後に green になり、そこで止まる。
    assert result.iterations == 3
    assert run.act.applied == [0, 1, 2]

    # 終了は「実テストの exit-code」が駆動している: red,red,green。
    assert run.verify.exit_codes == [1, 1, 0]
    assert result.history[-1].goal_met is True
    assert result.history[-1].detail == "green"

    # sandbox には正しい実装が残っており、独立に回しても確かに green。
    assert (tmp_path / demo.TARGET_FILENAME).read_text(encoding="utf-8") == demo.CORRECT_ADD
    proc = subprocess.run(
        list(DEFAULT_TEST_COMMAND),
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=sandbox_env(),
    )
    assert proc.returncode == 0


def test_verify_is_hermetic_against_pytest_addopts(tmp_path, monkeypatch):
    # 起動側の PYTEST_ADDOPTS は nested pytest にオプションを注入し、green な sandbox を
    # false red(ここでは "no tests collected" の rc=5)に反転させうる。sandbox 実行は
    # この種の env を除外するので、汚染下でも ground truth(exit-code)が決定的に保たれ、
    # 通常どおり 3 手目で green に到達して自然終了する。
    monkeypatch.setenv("PYTEST_ADDOPTS", "-m this_marker_matches_nothing")
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    assert run.result.goal_met is True
    assert run.result.iterations == 3
    assert run.verify.exit_codes == [1, 1, 0]


def test_sandbox_env_enforces_hermetic_invariants(monkeypatch):
    # 起動側がどんな pytest 系 env を持っていても、sandbox 実行は隔離される:
    # 結果反転源(ADDOPTS/PLUGINS)は除去し、autoload 無効化は(取り消さず)強制 1。
    monkeypatch.setenv("PYTEST_ADDOPTS", "-m nope")
    monkeypatch.setenv("PYTEST_PLUGINS", "some_plugin")
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    env = sandbox_env()
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTEST_PLUGINS" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_history_tokens_track_completed_work(tmp_path):
    # 反復ごとに 10 トークン計上 -> 3 反復で 30。観測フックの記録と一致する。
    run = demo.run_repair(tmp_path, conditions=[MaxIterations(10)])
    assert run.result.tokens_used == 30
    assert [r.iteration for r in run.result.history] == [0, 1, 2]


# -- 3: 直らない場合は上限で必ず止まる ------------------------------------


def test_loop_stops_at_cap_when_never_green(tmp_path):
    # 壊れた候補しか与えない -> 永遠に red。上限で止まることを確認する。
    run = demo.run_repair(
        tmp_path,
        candidates=[demo.BROKEN_SUBTRACT],
        conditions=[MaxIterations(3)],
    )
    result = run.result

    assert result.goal_met is False
    assert result.status == "stopped"
    assert result.stop is not None
    assert result.stop.name == "max_iterations"
    assert result.iterations == 3
    # 3 反復とも red(exit-code 0 は一度も出ていない)。
    assert run.verify.exit_codes == [1, 1, 1]


# -- ground truth フック単体: exit-code 0/非0 を正しく写す ------------------


def test_verifier_ignores_stale_bytecode_cache(tmp_path):
    # 非 -B の手動実行などで残った stale __pycache__ を再現する: 壊れた版を compile して
    # .pyc を作り、正しい版へ書き換えたうえで mtime を元に揃え、(mtime,size) 検証を
    # すり抜けさせる(候補は等 byte 長)。verifier は __pycache__ を消して source から
    # 再コンパイルするので、stale な red ではなく正しく green を返す。
    target = tmp_path / "add.py"
    (tmp_path / "test_add.py").write_text(
        "from add import add\ndef test_x():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")  # broken
    broken_mtime = target.stat().st_mtime
    py_compile.compile(str(target), doraise=True)  # writes __pycache__/add.*.pyc
    assert list((tmp_path / "__pycache__").glob("add.*.pyc"))

    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")  # fixed
    os.utime(target, (broken_mtime, broken_mtime))  # 元の mtime に戻す -> stale pyc が有効に見える

    verdict = ExitCodeVerifier(workdir=tmp_path)(None)
    assert verdict.goal_met is True
    assert verdict.detail == "green"


def test_verifier_times_out_on_hanging_test(tmp_path):
    # ハングするテスト(無限ループ)を仕込む -> timeout で kill され red 扱いになり、
    # 制御がループへ戻る(verify が永久ブロックして上限評価を奪わない)。
    (tmp_path / "test_hang.py").write_text(
        "def test_hang():\n    while True:\n        pass\n", encoding="utf-8"
    )
    verifier = ExitCodeVerifier(workdir=tmp_path, timeout=1.0)
    verdict = verifier(None)
    assert verdict.goal_met is False
    assert "timeout" in verdict.detail
    assert verifier.exit_codes == [ExitCodeVerifier.TIMEOUT_EXIT_CODE]


def test_verifier_maps_exit_code_to_goal(tmp_path):
    # green な sandbox。
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    verifier = ExitCodeVerifier(workdir=tmp_path)
    verdict = verifier(None)
    assert verdict.goal_met is True
    assert verifier.exit_codes == [0]

    # red を 1 本足すと、同じ verifier が goal_met=False を返す。
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    verdict = verifier(None)
    assert verdict.goal_met is False
    assert verifier.exit_codes[-1] != 0
