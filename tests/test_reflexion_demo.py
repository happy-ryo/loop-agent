"""外側 Reflexion デモの実走検証 (Issue #22 / Phase 3 成功条件 a)。

``examples/reflexion_demo.py`` のシナリオ「そのもの」を import して回すので、出荷する
デモと検証対象が一致する。命題: 失敗 episode から抽出した言語的指針が次 episode の
context に配線され、ground-truth (内側 verify の成否) が実際に改善する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

import reflexion_demo as demo  # noqa: E402


def test_lesson_wiring_lifts_next_episode_ground_truth():
    result = demo.run()
    history = result.state.gt_aggregate_history

    # ep0: memory 空で失敗 (off-by-one バグが残る)。
    assert history[0] < 0.3
    # ep1: 配線された学びで成功 -> ground-truth が跳ね上がる。
    assert history[1] > 0.9

    # 学びは memory に取り込まれ、次 context へ配線されている。
    assert any(rec.admitted for rec in result.state.episodes)
    assert demo.LESSON_HINT in result.state.memory.render()

    # 一次信号 (ground-truth) が収束を駆動し、成功で終わる。
    assert result.succeeded is True


def test_demo_runs_as_script():
    """出荷デモが端末から実走でき、cp932 でも print がクラッシュしないこと。"""
    proc = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / "reflexion_demo.py")],
        capture_output=True,
        text=True,
        env={**_clean_env(), "PYTHONPATH": str(EXAMPLES_DIR.parent / "src")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "succeeded=True" in proc.stdout


def _clean_env() -> dict:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
