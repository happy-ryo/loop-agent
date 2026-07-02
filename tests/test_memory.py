"""Unit tests for episodic memory and pre-admission validation (Issue #22 safety invariants: growth bounds / injection rejection)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from loop_agent.memory import (
    EpisodicMemory,
    Lesson,
    LessonVerdict,
    default_admit,
    step_signature,
)
from loop_agent.state import StepRecord


@dataclass
class _Outcome:
    """Minimal outcome stand-in with only ``.history`` for default_admit duck typing."""

    history: tuple[StepRecord, ...]


def _step(i: int, obs: object = "obs", detail: str = "") -> StepRecord:
    return StepRecord(iteration=i, observation=obs, tokens=1, goal_met=False, detail=detail)


def _admit_ok(text: str = "x", episode: int = 0, support: float = 1.0) -> LessonVerdict:
    return LessonVerdict(admit=True)


# -- default_admit: structural pre-admission validation (LLM-independent) ---------


def test_default_admit_accepts_grounded_lesson():
    step = _step(0, detail="ran tests")
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="prefer X", episode=0, provenance=step_signature(step), support=1.0)
    verdict = default_admit(lesson, outcome)
    assert verdict.admit is True


def test_default_admit_rejects_ungrounded_provenance():
    """Injection lesson: reject provenance not tied to a real step to prevent false lesson injection."""
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
    """Reject lessons whose recomputed support is 0, equivalent to ungrounded lessons."""
    step = _step(0)
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="ok", episode=0, provenance=step_signature(step), support=0.0)
    assert default_admit(lesson, outcome).admit is False


def test_step_signature_is_content_sensitive():
    """Signatures are content-based: the same iteration with different content gets a different signature for spoofing resistance."""
    a = step_signature(_step(0, obs="a", detail="x"))
    b = step_signature(_step(0, obs="b", detail="y"))
    assert a != b


def test_default_admit_is_structural_and_deterministic():
    """default_admit is purely structural: identical input yields identical output and does not depend on an external model."""
    step = _step(0)
    outcome = _Outcome(history=(step,))
    lesson = Lesson(text="t", episode=0, provenance=step_signature(step), support=1.0)
    first = default_admit(lesson, outcome)
    second = default_admit(lesson, outcome)
    assert first == second


# -- EpisodicMemory: growth bounds (iteration cap) -------------------------------


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
    """Even non-ASCII text is bounded by actual UTF-8 bytes, not character count (P3 fix)."""
    mem = EpisodicMemory(cap=50, per_lesson_chars=200, render_byte_cap=120)
    for i in range(50):
        mem.admit(
            Lesson(text=f"cafe lesson {i} " + "é" * 80, episode=i,
                   provenance=f"p{i}", support=1.0),
            LessonVerdict(admit=True),
        )
    rendered = mem.render()
    assert len(rendered.encode("utf-8")) <= 120
    # The truncated result is still valid UTF-8 with no broken multibyte characters.
    rendered.encode("utf-8").decode("utf-8")


def test_render_empty_when_no_lessons():
    assert EpisodicMemory().render() == ""


def test_duplicate_lesson_text_not_stored_twice():
    mem = EpisodicMemory(cap=5)
    assert mem.admit(Lesson("same lesson", 0, "p0", 1.0), LessonVerdict(admit=True)) is True
    # Reject duplicates whose normalized text matches, ignoring whitespace and case.
    assert mem.admit(Lesson("Same   Lesson", 1, "p1", 1.0), LessonVerdict(admit=True)) is False
    assert len(mem) == 1


def test_dedup_applies_after_truncation():
    """Lessons that become identical after truncation are not stored twice; dedup uses stored text."""
    mem = EpisodicMemory(cap=5, per_lesson_chars=8)
    # The first 8 characters, "COMMON: ", are identical; only the suffixes differ.
    assert mem.admit(Lesson("COMMON: alpha", 0, "p0", 1.0), LessonVerdict(admit=True)) is True
    assert mem.admit(Lesson("COMMON: beta", 1, "p1", 1.0), LessonVerdict(admit=True)) is False
    assert len(mem) == 1
    (stored,) = mem.lessons()
    assert stored.text == "COMMON: "


def test_rejected_verdict_not_stored():
    mem = EpisodicMemory(cap=5)
    assert mem.admit(Lesson("t", 0, "p", 1.0), LessonVerdict(admit=False)) is False
    assert len(mem) == 0


# -- Deterministic, value-aware eviction -----------------------------------------


def test_high_support_lesson_survives_marginal_flood():
    """Do not evict a high-support load-bearing lesson with low-support marginal lessons."""
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
    """Given the same admit sequence, eviction is deterministic by support, episode, and insertion order."""
    def build():
        m = EpisodicMemory(cap=2)
        m.admit(Lesson("a", 0, "pa", support=0.5), LessonVerdict(admit=True))
        m.admit(Lesson("b", 1, "pb", support=0.9), LessonVerdict(admit=True))
        m.admit(Lesson("c", 2, "pc", support=0.1), LessonVerdict(admit=True))
        return [l.text for l in m.lessons()]

    assert build() == build()
    # Lowest support is c=0.1; a=0.5 and b=0.9 remain.
    # After the third admit, cap=2 is exceeded, so the lowest-support lesson c=0.1 is evicted.
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
