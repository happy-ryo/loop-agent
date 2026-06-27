"""Unit tests for the stop-condition objects in isolation."""

from __future__ import annotations

import pytest

from claude_loop import AnyOf, MaxIterations, Timeout, TokenBudget
from claude_loop.state import LoopState


def test_max_iterations_fires_at_limit():
    cond = MaxIterations(3)
    assert cond.check(LoopState(iteration=2)) is None
    assert cond.check(LoopState(iteration=3)) is not None
    assert "3/3" in cond.check(LoopState(iteration=3))


def test_token_budget_fires_at_or_past_budget():
    cond = TokenBudget(100)
    assert cond.check(LoopState(tokens_used=99)) is None
    assert "100/100" in cond.check(LoopState(tokens_used=100))
    # overshoot is still reported (tokens already spent)
    assert "150/100" in cond.check(LoopState(tokens_used=150))


def test_timeout_fires_at_or_past_deadline():
    cond = Timeout(5.0)
    assert cond.check(LoopState(elapsed=4.999)) is None
    reason = cond.check(LoopState(elapsed=5.0))
    assert "5.000s/5s" in reason


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MaxIterations(-1),
        lambda: TokenBudget(-1),
        lambda: Timeout(-0.5),
    ],
)
def test_negative_limits_rejected(factory):
    with pytest.raises(ValueError):
        factory()


def test_anyof_requires_at_least_one_condition():
    with pytest.raises(ValueError):
        AnyOf([])


def test_anyof_reports_first_triggered_in_order():
    # Both fire; AnyOf must report the first in declaration order.
    state = LoopState(iteration=10, tokens_used=10)
    combo = AnyOf([MaxIterations(5), TokenBudget(5)])
    trigger = combo.first_triggered(state)
    assert trigger is not None
    assert trigger.name == "max_iterations"


def test_anyof_returns_none_when_nothing_fires():
    combo = AnyOf([MaxIterations(5), TokenBudget(5)])
    assert combo.first_triggered(LoopState(iteration=0, tokens_used=0)) is None
