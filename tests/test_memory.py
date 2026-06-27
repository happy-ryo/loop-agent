"""episodic memory + 取込前検証の単体テスト (Issue #22 安全不変条件: 肥大化抑止 / 注入拒否)。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from claude_loop.memory import (
    EpisodicMemory,
    Lesson,
    LessonVerdict,
    default_admit,
    step_signature,
)
from claude_loop.state import StepRecord


@dataclass
class _Outcome:
    """``.history`` だけ持つ最小の outcome スタンド (default_admit の duck typing 用)。"""

    history: tuple[StepRecord, ...]


def _step(i: int, obs: object = "obs", detail: str = "") -> StepRecord:
    return StepRecord(iteration=i, observation=obs, tokens=1, goal_met=False, detail=detail)


def _admit_ok(text: str = "x", episode: int = 0, support: float = 1.0) -> LessonVerdict:
    return LessonVerdict(admit=True)


# -- default_admit: 構造的取込前検証 (LLM 非依存) ---------------------------------


def test_default_admit_accepts_grounded_lesson():
    step = _step(0, detail="ran tests")
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="prefer X", episode=0, provenance=step_signature(step), support=1.0)
    verdict = default_admit(lesson, outcome)
    assert verdict.admit is True


def test_default_admit_rejects_ungrounded_provenance():
    """注入 lesson: 実 step に紐づかない provenance は弾く (false lesson 注入防止)。"""
    outcome = _Outcome(history=(_step(0),))
    lesson = Lesson(text="evil", episode=0, provenance="step-99-deadbeef", support=1.0)
    verdict = default_admit(lesson, outcome)
    assert verdict.admit is False
    assert "not grounded" in verdict.reason


def test_default_admit_rejects_empty_text():
    step = _step(0)
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="   ", episode=0, provenance=step_signature(step), support=1.0)
    assert default_admit(lesson, outcome).admit is False


def test_default_admit_rejects_insufficient_support():
    """support が再計算で 0 (ungrounded 相当) の lesson は弾く。"""
    step = _step(0)
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="ok", episode=0, provenance=step_signature(step), support=0.0)
    assert default_admit(lesson, outcome).admit is False


def test_step_signature_is_content_sensitive():
    """署名は内容ベース: iteration が同じでも内容が違えば別署名 (詐称耐性)。"""
    a = step_signature(_step(0, obs="a", detail="x"))
    b = step_signature(_step(0, obs="b", detail="y"))
    assert a != b


def test_default_admit_is_structural_and_deterministic():
    """default_admit は純構造判定: 同入力で同結果、外部 model に依存しない。"""
    step = _step(0)
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="t", episode=0, provenance=step_signature(step), support=1.0)
    first = default_admit(lesson, outcome)
    second = default_admit(lesson, outcome)
    assert first == second


# -- EpisodicMemory: 肥大化抑止 (反復上限) --------------------------------------


def test_memory_cap_keeps_at_most_cap_lessons():
    mem = EpisodicMemory(cap=3)
    for i in range(10):
        stored = mem.admit(
            Lesson(text=f"lesson number {i}", episode=i, provenance=f"p{i}", support=1.0),
            LessonVerdict(admit=True),
        )
        assert stored is True
    assert len(mem) == 3


def test_memory_truncates_oversize_lesson_text():
    mem = EpisodicMemory(cap=4, per_lesson_chars=10)
    mem.admit(
        Lesson(text="A" * 10_000, episode=0, provenance="p", support=1.0),
        LessonVerdict(admit=True),
    )
    (stored,) = mem.lessons()
    assert len(stored.text) == 10


def test_render_is_bounded_by_byte_cap():
    mem = EpisodicMemory(cap=50, per_lesson_chars=200, render_byte_cap=120)
    for i in range(50):
        mem.admit(
            Lesson(text=f"distinct lesson body {i} " + "z" * 80, episode=i,
                    provenance=f"p{i}", support=1.0),
            LessonVerdict(admit=True),
        )
    rendered = mem.render()
    assert len(rendered) <= 120


def test_render_byte_cap_bounds_utf8_bytes_for_non_ascii():
    """非 ASCII (日本語) でも文字数でなく実 UTF-8 バイト数で有界 (P3 fix)。"""
    mem = EpisodicMemory(cap=50, per_lesson_chars=200, render_byte_cap=120)
    for i in range(50):
        mem.admit(
            Lesson(text=f"日本語の教訓 {i} " + "あ" * 80, episode=i,
                   provenance=f"p{i}", support=1.0),
            LessonVerdict(admit=True),
        )
    rendered = mem.render()
    assert len(rendered.encode("utf-8")) <= 120
    # 丸めた結果も妥当な UTF-8 (壊れた multibyte が残らない)。
    rendered.encode("utf-8").decode("utf-8")


def test_render_empty_when_no_lessons():
    assert EpisodicMemory().render() == ""


def test_duplicate_lesson_text_not_stored_twice():
    mem = EpisodicMemory(cap=5)
    assert mem.admit(Lesson("same lesson", 0, "p0", 1.0), LessonVerdict(admit=True)) is True
    # 正規化テキストが一致する重複は弾く (whitespace / case 無視)。
    assert mem.admit(Lesson("Same   Lesson", 1, "p1", 1.0), LessonVerdict(admit=True)) is False
    assert len(mem) == 1


def test_dedup_applies_after_truncation():
    """切り詰め後に同一になる lesson は二重保存しない (dedup は格納テキストで判定)。"""
    mem = EpisodicMemory(cap=5, per_lesson_chars=8)
    # 先頭 8 文字 "COMMON: " が同一、以降だけ違う 2 つ。
    assert mem.admit(Lesson("COMMON: alpha", 0, "p0", 1.0), LessonVerdict(admit=True)) is True
    assert mem.admit(Lesson("COMMON: beta", 1, "p1", 1.0), LessonVerdict(admit=True)) is False
    assert len(mem) == 1
    (stored,) = mem.lessons()
    assert stored.text == "COMMON: "


def test_rejected_verdict_not_stored():
    mem = EpisodicMemory(cap=5)
    assert mem.admit(Lesson("t", 0, "p", 1.0), LessonVerdict(admit=False)) is False
    assert len(mem) == 0


# -- 決定的・価値考慮の eviction ------------------------------------------------


def test_high_support_lesson_survives_marginal_flood():
    """高 support の load-bearing lesson を、低 support の周辺 lesson で押し出さない。"""
    mem = EpisodicMemory(cap=2)
    mem.admit(Lesson("load bearing fix", 0, "p0", support=1.0), LessonVerdict(admit=True))
    for i in range(1, 6):
        mem.admit(
            Lesson(f"marginal note {i}", i, f"p{i}", support=0.0),
            LessonVerdict(admit=True),
        )
    texts = [l.text for l in mem.lessons()]
    assert "load bearing fix" in texts
    assert len(mem) == 2


def test_eviction_is_deterministic():
    """同じ admit 列なら eviction 結果は決定的 (support, episode, 挿入順)。"""
    def build():
        m = EpisodicMemory(cap=2)
        m.admit(Lesson("a", 0, "pa", support=0.5), LessonVerdict(admit=True))
        m.admit(Lesson("b", 1, "pb", support=0.9), LessonVerdict(admit=True))
        m.admit(Lesson("c", 2, "pc", support=0.1), LessonVerdict(admit=True))
        return [l.text for l in m.lessons()]

    assert build() == build()
    # support 最小 (c=0.1) と... a=0.5 が残り b=0.9。c は最小なので即 evict されない:
    # 3 件目 admit 後 cap=2 超過 -> support 最小 (c=0.1) を捨てる。
    assert set(build()) == {"a", "b"}


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EpisodicMemory(cap=0),
        lambda: EpisodicMemory(per_lesson_chars=0),
        lambda: EpisodicMemory(render_byte_cap=0),
    ],
)
def test_invalid_memory_params_rejected(factory):
    with pytest.raises(ValueError):
        factory()
