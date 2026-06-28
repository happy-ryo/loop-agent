"""``ModelLadder`` の検証(Issue #53)。

``ModelLadder`` は subprocess を起動する CLI アダプタではなく **act フックを合成する
アダプタ** なので、``tests/adapters`` の subprocess 契約ハーネス(``ADAPTER_SPECS``)
には載せない。ここでは合成アダプタ固有の挙動を直接検証する:

- 戦略ごとのエスカレーション挙動(failure / attempt_count / custom predicate)
- ``escalate_on`` の解決と不正値の拒否
- ``run_loop`` 統合での lifecycle(低段失敗 -> 上段呼び出し -> 成功)
- 異種アダプタ chain(``MockClaudeCodeAct`` + ``MockCodexAct``)での昇格
- 単調性 / 末尾段への張り付き / token 透過 / reset
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


# -- 小ヘルパ: 呼び出しを記録するだけの act フック ----------------------------


def _act(*, failed: bool = False, text: str = "ok", tokens: int = 0):
    """毎回同じ結果を返し、受け取った context を ``.contexts`` に記録する act フック。"""

    def _hook(context):
        _hook.contexts.append(context)
        result = ClaudeCodeResult(text=text, tokens=tokens, failed=failed)
        return ActOutcome(observation=result, tokens=tokens)

    _hook.contexts = []
    return _hook


def _drive(ladder: ModelLadder, n: int, context=None):
    """ladder を ``n`` 回呼び、各呼び出し直後の active index を列で返す。"""
    indices = []
    for _ in range(n):
        ladder(context if context is not None else {"prompt": "go"})
        indices.append(ladder.current_index)
    return indices


# -- 構築バリデーション -------------------------------------------------------


def test_empty_candidates_rejected():
    with pytest.raises(ValueError, match="at least one candidate"):
        ModelLadder([])


def test_single_candidate_never_escalates():
    # 1 段しか無いので、失敗しても張り付くしかない(index は常に 0)。
    fail_only = _act(failed=True)
    ladder = ModelLadder([fail_only], escalate_on="failure")
    assert _drive(ladder, 4) == [0, 0, 0, 0]
    assert ladder.at_top is True
    assert len(fail_only.contexts) == 4


# -- escalate_on の解決 / 不正値 ----------------------------------------------


@pytest.mark.parametrize("bad", [True, False, 0, -1, "nope", 1.5, None])
def test_invalid_escalate_on_rejected(bad):
    with pytest.raises(ValueError):
        ModelLadder([_act(), _act()], escalate_on=bad)


def test_after_attempts_requires_positive_int():
    with pytest.raises(ValueError):
        after_attempts(0)
    with pytest.raises(ValueError):
        after_attempts(True)  # bool は int だが弾く


# -- 戦略 1: failure(前段失敗で昇格) ----------------------------------------


def test_failure_strategy_escalates_on_failed():
    low = _act(failed=True)
    mid = _act(failed=True)
    top = _act(failed=False, text="done")
    ladder = ModelLadder([low, mid, top], escalate_on="failure")

    # 初回は index0(履歴が無いので昇格しない)。
    assert _drive(ladder, 1) == [0]
    # low が失敗 -> 次は mid、mid も失敗 -> 次は top、top 成功 -> top に留まる。
    assert _drive(ladder, 3) == [1, 2, 2]
    assert len(low.contexts) == 1
    assert len(mid.contexts) == 1
    assert len(top.contexts) == 2  # 成功後も張り付いて呼ばれ続ける


def test_failure_strategy_stays_when_succeeding():
    low = _act(failed=False)
    top = _act(failed=False)
    ladder = ModelLadder([low, top], escalate_on="failure")
    # 低段が成功し続ける限り昇格しない。
    assert _drive(ladder, 5) == [0, 0, 0, 0, 0]
    assert len(top.contexts) == 0


# -- 戦略 2: attempt_count(N 回で昇格、成否によらず) ------------------------


def test_attempt_count_strategy_escalates_after_n():
    low = _act(failed=False)  # 成功し続けても N 回で昇格する
    mid = _act(failed=False)
    top = _act(failed=False)
    ladder = ModelLadder([low, mid, top], escalate_on=2)

    # 各段 2 回ずつ呼んでから昇格(成功でも昇格するのが failure 戦略との違い)。
    assert _drive(ladder, 6) == [0, 0, 1, 1, 2, 2]
    assert len(low.contexts) == 2
    assert len(mid.contexts) == 2
    assert len(top.contexts) == 2


def test_attempt_count_one_escalates_every_call():
    low, mid, top = _act(), _act(), _act()
    ladder = ModelLadder([low, mid, top], escalate_on=1)
    # N=1 は毎回昇格(末尾で張り付く)。
    assert _drive(ladder, 5) == [0, 1, 2, 2, 2]


# -- 戦略 3: custom predicate -------------------------------------------------


def test_custom_predicate_receives_context_and_controls_escalation():
    seen = []

    def predicate(ec: EscalationContext) -> bool:
        seen.append(ec)
        # 「失敗 かつ 同段 2 回目以降」でのみ昇格する合成戦略。
        return ec.last_failed and ec.attempts >= 2

    low = _act(failed=True)
    top = _act(failed=False, text="done")
    ladder = ModelLadder([low, top], escalate_on=predicate)

    # call1: 初回(attempts0) -> 昇格せず low。low 失敗。
    # call2: attempts1 -> 2 未満で昇格せず low。low 失敗。
    # call3: attempts2 & last_failed -> 昇格 top。
    assert _drive(ladder, 3) == [0, 0, 1]
    assert len(low.contexts) == 2
    assert len(top.contexts) == 1
    # predicate は末尾段に達した後は呼ばれない(昇格余地が無い)。
    assert [ec.candidate_index for ec in seen] == [0, 0, 0]


def test_escalation_context_fields():
    captured = {}

    def predicate(ec: EscalationContext) -> bool:
        captured["ec"] = ec
        return False

    a = _act(failed=True, tokens=11)
    ladder = ModelLadder([a, _act()], escalate_on=predicate)
    ladder({"prompt": "x"})  # 1 回呼んで履歴を作る
    ladder({"prompt": "y"})  # ここで predicate が前回 outcome を見る

    ec = captured["ec"]
    assert ec.candidate_index == 0
    assert ec.num_candidates == 2
    assert ec.attempts == 1
    assert ec.total_attempts == 1
    assert ec.last_failed is True
    assert ec.last_outcome is not None
    assert ec.last_outcome.observation.failed is True


def test_named_strategy_helpers_are_usable_directly():
    # on_failure / after_attempts はモジュール公開で、合成・直接利用できる。
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


# -- 単調性 / 透過 / reset ----------------------------------------------------


def test_monotonic_never_de_escalates():
    # 一度昇格したら、その後低段が成功しても戻らない。
    low = _act(failed=True)
    top = _act(failed=False)
    ladder = ModelLadder([low, top], escalate_on="failure")
    _drive(ladder, 5)  # low 失敗 -> top 昇格 -> top 成功でも top に留まる
    assert ladder.current_index == 1
    assert len(low.contexts) == 1  # 昇格後 low は二度と呼ばれない


def test_outcome_and_tokens_pass_through_unchanged():
    a = _act(text="payload", tokens=1234)
    ladder = ModelLadder([a])
    outcome = ladder({"prompt": "x"})
    # ladder は段の ActOutcome をそのまま透過する(text / tokens を改変しない)。
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


# -- run_loop 統合: lifecycle(低段失敗 -> 上段成功) -------------------------


def test_run_loop_lifecycle_escalates_to_success():
    low = MockClaudeCodeAct(responses=[{"failed": True, "error": "boom"}])
    mid = MockClaudeCodeAct(responses=[{"failed": True, "error": "still boom"}])
    top = MockClaudeCodeAct(responses=["done"])
    ladder = ModelLadder([low, mid, top], escalate_on="failure")

    def verify(outcome):
        # act が成功(failed=False)したら goal 達成とみなす。
        return VerifyOutcome(goal_met=not outcome.observation.failed)

    result = run_loop(
        act=ladder,
        verify=verify,
        gather=lambda s: {"prompt": "fix it"},
        conditions=[MaxIterations(10)],
    )

    assert result.goal_met is True
    assert result.iterations == 3  # low 失敗 -> mid 失敗 -> top 成功
    assert ladder.current_index == 2
    assert low.prompts == ["fix it"]
    assert mid.prompts == ["fix it"]
    assert top.prompts == ["fix it"]


def test_run_loop_token_budget_still_fires_through_ladder():
    # ladder は段の tokens を透過するので TokenBudget がそのまま効く。
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
    # failure 戦略では捕捉できないケース(act は毎回成功するが goal 未達)を
    # attempt_count 戦略が埋める。各段 2 回で昇格し、top の 3 回目で goal 達成。
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
    # low x2, mid x2, top x3(3 回目に FINAL)= 7 反復。
    assert result.goal_met is True
    assert result.iterations == 7
    assert len(low.prompts) == 2
    assert len(mid.prompts) == 2
    assert len(top.prompts) == 3


# -- 異種アダプタ chain(ClaudeCode + Codex 混在) ---------------------------


def test_heterogeneous_chain_escalates_across_adapter_types():
    # 低段 Claude Code が失敗 -> 上段 Codex が成功。結果型が違っても failed を
    # 共通の ActResult 契約越しに読めるので、同じ判断ロジックで昇格できる。
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
    # 上段の結果は CodexResult(異種型)であり、契約越しに扱えている。
    assert type(result.history[-1].observation).__name__ == "CodexResult"


def test_heterogeneous_three_stage_mixed():
    # Claude(haiku) -> Codex(gpt) -> Claude(opus) の混在 3 段。failure で順に昇格。
    s0 = MockClaudeCodeAct(responses=[{"failed": True}])
    s1 = MockCodexAct(responses=[{"failed": True}])
    s2 = MockClaudeCodeAct(responses=["opus done"])
    ladder = ModelLadder([s0, s1, s2], escalate_on="failure")
    indices = _drive(ladder, 3)
    assert indices == [0, 1, 2]
    assert len(s0.prompts) == len(s1.prompts) == 1
    assert ladder.at_top is True
