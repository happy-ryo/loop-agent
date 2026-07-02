"""Tests for ``ModelLadder`` (Issue #53).

``ModelLadder`` is not a subprocess-launching CLI adapter; it is an
**adapter that composes act hooks**, so it is not included in the subprocess
contract harness (``ADAPTER_SPECS``) under ``tests/adapters``. These tests
directly verify behavior specific to the composite adapter:

- escalation behavior by strategy (failure / attempt_count / custom predicate)
- ``escalate_on`` resolution and rejection of invalid values
- lifecycle under ``run_loop`` integration (lower tier fails -> upper tier
  called -> success)
- escalation across a heterogeneous adapter chain (``MockClaudeCodeAct`` +
  ``MockCodexAct``)
- monotonicity / sticking to the final tier / token pass-through / reset
"""

from __future__ import annotations

import pytest

from loop_agent import MaxIterations, TokenBudget, VerifyOutcome, run_loop
from loop_agent.adapters import (
    EscalationContext,
    MockClaudeCodeAct,
    MockCodexAct,
    ModelLadder,
    after_attempts,
    on_failure,
)
from loop_agent.adapters.claude_code import ClaudeCodeResult
from loop_agent.loop import ActOutcome


# -- Small helper: an act hook that only records calls -----------------------


def _act(*, failed: bool = False, text: str = "ok", tokens: int = 0):
    """Return the same result every time and record contexts in ``.contexts``."""

    def _hook(context):
        _hook.contexts.append(context)
        result = ClaudeCodeResult(text=text, tokens=tokens, failed=failed)
        return ActOutcome(observation=result, tokens=tokens)

    _hook.contexts = []
    return _hook


def _drive(ladder: ModelLadder, n: int, context=None):
    """Call ladder ``n`` times and return active indices after each call."""
    indices = []
    for _ in range(n):
        ladder(context if context is not None else {"prompt": "go"})
        indices.append(ladder.current_index)
    return indices


# -- Construction validation ------------------------------------------------


def test_empty_candidates_rejected():
    with pytest.raises(ValueError, match="at least one candidate"):
        ModelLadder([])


def test_single_candidate_never_escalates():
    # With only one tier, even failures can only stick there (index is always 0).
    fail_only = _act(failed=True)
    ladder = ModelLadder([fail_only], escalate_on="failure")
    assert _drive(ladder, 4) == [0, 0, 0, 0]
    assert ladder.at_top is True
    assert len(fail_only.contexts) == 4


# -- escalate_on resolution / invalid values --------------------------------


@pytest.mark.parametrize("bad", [True, False, 0, -1, "nope", 1.5, None])
def test_invalid_escalate_on_rejected(bad):
    with pytest.raises(ValueError):
        ModelLadder([_act(), _act()], escalate_on=bad)


def test_after_attempts_requires_positive_int():
    with pytest.raises(ValueError):
        after_attempts(0)
    with pytest.raises(ValueError):
        after_attempts(True)  # bool is an int, but is rejected


# -- Strategy 1: failure (escalate when the previous tier fails) ------------


def test_failure_strategy_escalates_on_failed():
    low = _act(failed=True)
    mid = _act(failed=True)
    top = _act(failed=False, text="done")
    ladder = ModelLadder([low, mid, top], escalate_on="failure")

    # The first call uses index0 (no history, so it does not escalate).
    assert _drive(ladder, 1) == [0]
    # low fails -> next is mid; mid also fails -> next is top; top succeeds -> stays on top.
    assert _drive(ladder, 3) == [1, 2, 2]
    assert len(low.contexts) == 1
    assert len(mid.contexts) == 1
    assert len(top.contexts) == 2  # Keeps sticking and being called after success


def test_failure_strategy_stays_when_succeeding():
    low = _act(failed=False)
    top = _act(failed=False)
    ladder = ModelLadder([low, top], escalate_on="failure")
    # It does not escalate as long as the lower tier keeps succeeding.
    assert _drive(ladder, 5) == [0, 0, 0, 0, 0]
    assert len(top.contexts) == 0


# -- Strategy 2: attempt_count (escalate after N calls, regardless of result)


def test_attempt_count_strategy_escalates_after_n():
    low = _act(failed=False)  # Escalates after N calls even if it keeps succeeding
    mid = _act(failed=False)
    top = _act(failed=False)
    ladder = ModelLadder([low, mid, top], escalate_on=2)

    # Escalates after calling each tier twice (unlike failure, success still escalates).
    assert _drive(ladder, 6) == [0, 0, 1, 1, 2, 2]
    assert len(low.contexts) == 2
    assert len(mid.contexts) == 2
    assert len(top.contexts) == 2


def test_attempt_count_one_escalates_every_call():
    low, mid, top = _act(), _act(), _act()
    ladder = ModelLadder([low, mid, top], escalate_on=1)
    # N=1 escalates on every call (then sticks at the end).
    assert _drive(ladder, 5) == [0, 1, 2, 2, 2]


# -- Strategy 3: custom predicate -------------------------------------------


def test_custom_predicate_receives_context_and_controls_escalation():
    seen = []

    def predicate(ec: EscalationContext) -> bool:
        seen.append(ec)
        # Composite strategy: escalate only after "failed and second+ attempt on the same tier".
        return ec.last_failed and ec.attempts >= 2

    low = _act(failed=True)
    top = _act(failed=False, text="done")
    ladder = ModelLadder([low, top], escalate_on=predicate)

    # call1: first call (attempts0) -> no escalation, low. low fails.
    # call2: attempts1 -> below 2, no escalation, low. low fails.
    # call3: attempts2 & last_failed -> escalates to top.
    assert _drive(ladder, 3) == [0, 0, 1]
    assert len(low.contexts) == 2
    assert len(top.contexts) == 1
    # The predicate is not called after reaching the final tier (no room to escalate).
    assert [ec.candidate_index for ec in seen] == [0, 0, 0]


def test_escalation_context_fields():
    captured = {}

    def predicate(ec: EscalationContext) -> bool:
        captured["ec"] = ec
        return False

    a = _act(failed=True, tokens=11)
    ladder = ModelLadder([a, _act()], escalate_on=predicate)
    ladder({"prompt": "x"})  # Call once to create history
    ladder({"prompt": "y"})  # The predicate sees the previous outcome here

    ec = captured["ec"]
    assert ec.candidate_index == 0
    assert ec.num_candidates == 2
    assert ec.attempts == 1
    assert ec.total_attempts == 1
    assert ec.last_failed is True
    assert ec.last_outcome is not None
    assert ec.last_outcome.observation.failed is True


def test_named_strategy_helpers_are_usable_directly():
    # on_failure / after_attempts are module exports and can be composed or used directly.
    assert on_failure(_ctx(last_failed=True)) is True
    assert on_failure(_ctx(last_failed=False)) is False
    pred = after_attempts(3)
    assert pred(_ctx(attempts=2)) is False
    assert pred(_ctx(attempts=3)) is True


def _ctx(*, candidate_index=0, num_candidates=2, attempts=0, total_attempts=0,
         last_outcome=None, last_failed=False) -> EscalationContext:
    return EscalationContext(
        candidate_index=candidate_index,
        num_candidates=num_candidates,
        attempts=attempts,
        total_attempts=total_attempts,
        last_outcome=last_outcome,
        last_failed=last_failed,
    )


# -- Monotonicity / pass-through / reset ------------------------------------


def test_monotonic_never_de_escalates():
    # Once escalated, it does not return even if a lower tier would later succeed.
    low = _act(failed=True)
    top = _act(failed=False)
    ladder = ModelLadder([low, top], escalate_on="failure")
    _drive(ladder, 5)  # low fails -> top escalation -> stays on top even after top succeeds
    assert ladder.current_index == 1
    assert len(low.contexts) == 1  # low is never called again after escalation


def test_outcome_and_tokens_pass_through_unchanged():
    a = _act(text="payload", tokens=1234)
    ladder = ModelLadder([a])
    outcome = ladder({"prompt": "x"})
    # ladder passes through the tier's ActOutcome unchanged (does not modify text / tokens).
    assert outcome.observation.text == "payload"
    assert outcome.tokens == 1234
    assert ladder.total_attempts == 1


def test_reset_returns_to_first_candidate():
    low = _act(failed=True)
    top = _act(failed=False)
    ladder = ModelLadder([low, top], escalate_on="failure")
    _drive(ladder, 3)
    assert ladder.current_index == 1
    ladder.reset()
    assert ladder.current_index == 0
    assert ladder.attempts == 0
    assert ladder.total_attempts == 0


def test_context_is_forwarded_to_active_candidate():
    low = _act(failed=True)
    top = _act()
    ladder = ModelLadder([low, top], escalate_on="failure")
    ladder({"prompt": "first"})
    ladder({"prompt": "second"})
    assert low.contexts == [{"prompt": "first"}]
    assert top.contexts == [{"prompt": "second"}]


# -- run_loop integration: lifecycle (lower tier fails -> upper tier succeeds)


def test_run_loop_lifecycle_escalates_to_success():
    low = MockClaudeCodeAct(responses=[{"failed": True, "error": "boom"}])
    mid = MockClaudeCodeAct(responses=[{"failed": True, "error": "still boom"}])
    top = MockClaudeCodeAct(responses=["done"])
    ladder = ModelLadder([low, mid, top], escalate_on="failure")

    def verify(outcome):
        # Treat the goal as met once act succeeds (failed=False).
        return VerifyOutcome(goal_met=not outcome.observation.failed)

    result = run_loop(
        act=ladder,
        verify=verify,
        gather=lambda s: {"prompt": "fix it"},
        conditions=[MaxIterations(10)],
    )

    assert result.goal_met is True
    assert result.iterations == 3  # low fails -> mid fails -> top succeeds
    assert ladder.current_index == 2
    assert low.prompts == ["fix it"]
    assert mid.prompts == ["fix it"]
    assert top.prompts == ["fix it"]


def test_run_loop_token_budget_still_fires_through_ladder():
    # ladder passes through the tier's tokens, so TokenBudget still applies directly.
    low = MockClaudeCodeAct(responses=[{"text": "step", "tokens": 1200}])
    ladder = ModelLadder([low, MockClaudeCodeAct(responses=["unused"])])

    result = run_loop(
        act=ladder,
        verify=lambda o: VerifyOutcome(goal_met=False),
        gather=lambda s: {"prompt": "go"},
        conditions=[TokenBudget(2000), MaxIterations(100)],
    )
    assert result.stop.name == "token_budget"
    assert result.tokens_used == 2400
    assert result.iterations == 2


def test_run_loop_attempt_count_escalates_when_act_succeeds_but_goal_unmet():
    # attempt_count covers cases the failure strategy cannot catch (act succeeds every
    # time, but the goal is unmet). Escalates after 2 calls per tier and reaches the
    # goal on the 3rd top call.
    low = MockClaudeCodeAct(responses=["progress-low"])
    mid = MockClaudeCodeAct(responses=["progress-mid"])
    top = MockClaudeCodeAct(responses=["progress-top", "progress-top", "FINAL"])
    ladder = ModelLadder([low, mid, top], escalate_on=2)

    def verify(outcome):
        return VerifyOutcome(goal_met=outcome.observation.text == "FINAL")

    result = run_loop(
        act=ladder,
        verify=verify,
        gather=lambda s: {"prompt": "iterate"},
        conditions=[MaxIterations(20)],
    )
    # low x2, mid x2, top x3 (FINAL on the 3rd call) = 7 iterations.
    assert result.goal_met is True
    assert result.iterations == 7
    assert len(low.prompts) == 2
    assert len(mid.prompts) == 2
    assert len(top.prompts) == 3


# -- Heterogeneous adapter chain (mixed ClaudeCode + Codex) -----------------


def test_heterogeneous_chain_escalates_across_adapter_types():
    # Lower Claude Code tier fails -> upper Codex tier succeeds. Even with different
    # result types, failed can be read through the shared ActResult contract, so the
    # same decision logic can escalate.
    low = MockClaudeCodeAct(responses=[{"failed": True, "error": "claude boom"}])
    top = MockCodexAct(responses=["codex done"])
    ladder = ModelLadder([low, top], escalate_on="failure")

    def verify(outcome):
        return VerifyOutcome(goal_met=not outcome.observation.failed)

    result = run_loop(
        act=ladder,
        verify=verify,
        gather=lambda s: {"prompt": "cross-provider"},
        conditions=[MaxIterations(10)],
    )
    assert result.goal_met is True
    assert result.iterations == 2
    assert low.prompts == ["cross-provider"]
    assert top.prompts == ["cross-provider"]
    # The upper-tier result is CodexResult (a heterogeneous type), handled through the contract.
    assert type(result.history[-1].observation).__name__ == "CodexResult"


def test_heterogeneous_three_stage_mixed():
    # Mixed 3-tier chain: Claude(haiku) -> Codex(gpt) -> Claude(opus). failure escalates in order.
    s0 = MockClaudeCodeAct(responses=[{"failed": True}])
    s1 = MockCodexAct(responses=[{"failed": True}])
    s2 = MockClaudeCodeAct(responses=["opus done"])
    ladder = ModelLadder([s0, s1, s2], escalate_on="failure")
    indices = _drive(ladder, 3)
    assert indices == [0, 1, 2]
    assert len(s0.prompts) == len(s1.prompts) == 1
    assert ladder.at_top is True
