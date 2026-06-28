"""困難タスクで強いモデルへ自動エスカレーションする ``act`` 合成アダプタ。

``ModelLadder`` は **loop-agent の新機能ではない**。``act`` シームは元々
``Callable[[context], ActOutcome]`` なので、「前段モデルが失敗したら次段の強い
モデルに渡す」エスカレーションは user が今日でも自分で書ける(README 動線 D の
``escalating_act`` がまさにそれ)。本モジュールは、その **user が高頻度で書く
ModelLadder パターンを 1 か所に正しく実装** し、discoverability と落とし穴ヘッジ
(stateful な試行カウントの取り回し / act が verify の goal 判定を見られない制約 /
異種アダプタ合成)を提供する canonical example である(Issue #53)。コア
(``run_loop``)は一切変更せず、``ActOutcome`` を返す ``act`` フックとして差し込める。

使い方::

    from loop_agent import run_loop
    from loop_agent.adapters import ModelLadder, ClaudeCodeAct

    act = ModelLadder([
        ClaudeCodeAct(model="haiku"),
        ClaudeCodeAct(model="sonnet"),
        ClaudeCodeAct(model="opus"),
    ], escalate_on="failure")

    result = run_loop(act=act, verify=..., gather=..., conditions=...)

異種(LLM プロバイダー横断)チェーンもそのまま組める。cost-optimal なモデルから
始めて、難所だけ強い別プロバイダーに渡すフォールバックパス::

    from loop_agent.adapters import ModelLadder, ClaudeCodeAct, CodexAct

    act = ModelLadder([
        ClaudeCodeAct(model="haiku"),
        CodexAct(model="gpt-5.5"),
        ClaudeCodeAct(model="opus"),
    ])

各段は ``ActHook`` 適合の任意の callable でよく、結果が共通の
:class:`~loop_agent.adapters.base.ActResult` 契約(``observation.failed`` を持つ)に
適合していれば、異種を混ぜても ``ModelLadder`` は同じ判断ロジックで扱える(#52 の
``ActResult`` Protocol が合成性を担保している)。

設計上の位置づけ(なぜこの形か):

- **stateful**: ``act`` フックは ``context`` しか受け取らず、後段 ``verify`` の
  goal 判定は **見られない**(run_loop は gather -> act -> verify の順で、verdict は
  act に戻らない)。そのため ``ModelLadder`` は前回 outcome と per-candidate の試行
  回数を **自分で保持** し、その履歴から「次にどの段を呼ぶか」を決める。1 つの
  ladder インスタンスは 1 つの run に対応する(別 run で使い回すなら :meth:`reset`)。
- **act が観測できる失敗は ``observation.failed`` だけ**(crash / 非 0 終了 /
  timeout / 起動失敗)。「act は成功(``failed=False``)したが verify が goal 未達と
  判定した」ケースは ``failure`` 戦略では捕捉できない。これを埋めるのが
  ``attempt_count`` 戦略(段ごとに N 回試したら、成否によらず昇格)であり、両者は
  相補的である(下の :class:`ModelLadder` の ``escalate_on`` を参照)。
- **単調(monotonic)**: 一度上の段へ上がったら下の段へは戻らない(より強いモデルへ
  上げ続ける)。最強段に達したら、そこへ張り付いて retry し続ける(``MockClaudeCodeAct``
  が最後の応答に張り付くのと同じ「現状の最善手を出し続ける」挙動。境界の
  ``MaxIterations`` 等で安全に止まる)。
- **責務は「どの段を呼ぶか」だけ**: ``ModelLadder`` はプロンプトを書き換えたり
  Reflexion(失敗の反省)を注入したりしない。lessons 蓄積は ``run_reflexion`` /
  ``gather`` 側に直交して合成する(README 動線 D の「Reflexion 合成」)。責務を
  モデル選択だけに絞ることで、Reflexion・WorkListGather と素直に重ねられる。

``ModelLadder`` は subprocess を起動する CLI アダプタ(``ClaudeCodeAct`` /
``CodexAct``)ではなく **act フックを合成するアダプタ** なので、``build_command`` /
``runner`` / ``parse_tokens`` を持たず、``tests/adapters`` の subprocess 契約
ハーネス(``ADAPTER_SPECS``)には載らない。token / failed / graceful 終了の保証は
各段(合成される CLI アダプタ)が満たすもので、``ModelLadder`` は段の
``ActOutcome`` を **そのまま透過** する(tokens も改変しないので ``TokenBudget`` が
そのまま効く)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence, Union

from ..loop import ActHook, ActOutcome

__all__ = [
    "ModelLadder",
    "EscalationContext",
    "EscalationPredicate",
    "on_failure",
    "after_attempts",
]


@dataclass(frozen=True)
class EscalationContext:
    """エスカレーション判断 predicate に渡す、ladder の現在状態のスナップショット。

    predicate は次の ``act`` 呼び出しの **前** に評価され、``True`` を返すと
    ``ModelLadder`` は 1 段上の(より強い)候補へ昇格してからその候補を呼ぶ。
    全フィールドは「これまでに観測した履歴」であり、これから呼ぶ候補の結果は
    まだ含まない(判断は過去にのみ基づく)。

    Attributes:
        candidate_index: いま active な候補の 0 始まり index(``last_outcome`` を
            生んだ候補。昇格しなければこの候補がもう一度呼ばれる)。
        num_candidates: ladder の候補総数(末尾段かどうかの判断などに使える)。
        attempts: いま active な候補をこれまで呼んだ回数(昇格すると 0 にリセット)。
        total_attempts: 全候補を通じた ladder の総呼び出し回数。
        last_outcome: 前回の ``act`` 呼び出しが返した :class:`ActOutcome`
            (まだ 1 度も呼んでいない初回は ``None``)。
        last_failed: ``last_outcome.observation.failed`` の簡便値(``last_outcome``
            が ``None`` のとき、または observation が ``failed`` を持たないときは
            ``False``)。``failure`` 戦略はこの値を見る。
    """

    candidate_index: int
    num_candidates: int
    attempts: int
    total_attempts: int
    last_outcome: Optional[ActOutcome]
    last_failed: bool


# エスカレーション判断: 現在状態を見て「次の呼び出しの前に 1 段上げるか」を返す。
EscalationPredicate = Callable[[EscalationContext], bool]


def on_failure(ec: EscalationContext) -> bool:
    """前回の ``act`` が **失敗**(``observation.failed``)していたら昇格する戦略。

    ``escalate_on="failure"`` がこの関数に解決される。crash / 非 0 終了 / timeout /
    起動失敗で前段が ``failed=True`` を返したら、次の反復で 1 段強いモデルに渡す。

    注意: act が ``failed=False`` を返したが verify は goal 未達、というケースは
    **捕捉できない**(act は verify の verdict を見られない)。その場合は
    :func:`after_attempts` を併用するか合成する(モジュール docstring 参照)。
    """
    return ec.last_failed


def after_attempts(n: int) -> EscalationPredicate:
    """同じ候補を **N 回呼んだら** 成否によらず昇格する戦略を作る。

    ``escalate_on=N``(int)がこの関数に解決される。「act は成功扱いだが verify が
    goal 未達で反復が続く」状況で、N 回試して埋まらないモデルを見切って上げるための
    戦略(:func:`on_failure` では捕捉できないケースを埋める相補的な戦略)。

    例えば ``after_attempts(2)`` は各候補を 2 回呼んでから次段へ上げる。

    「N 回 **失敗** したら上げる」にしたいなら predicate を合成する::

        escalate_on=lambda ec: ec.last_failed and ec.attempts >= 2
    """
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"after_attempts(n) requires a positive int, got {n!r}")

    def _predicate(ec: EscalationContext) -> bool:
        return ec.attempts >= n

    return _predicate


def _resolve_strategy(
    escalate_on: Union[str, int, EscalationPredicate],
) -> EscalationPredicate:
    """``escalate_on`` 引数を実際の predicate に解決する。

    - callable -> そのまま(custom predicate)
    - ``"failure"`` -> :func:`on_failure`
    - 正の int N -> ``after_attempts(N)``
    その他は分かりやすい :class:`ValueError` を投げる(``True``/``0``/未知の文字列 等)。
    """
    # bool は int のサブクラスなので、ここで先に弾く(``escalate_on=True`` 等の取り違え)。
    if isinstance(escalate_on, bool):
        raise ValueError(
            f"escalate_on must be 'failure', a positive int, or a predicate; "
            f"got bool {escalate_on!r}"
        )
    if callable(escalate_on):
        return escalate_on
    if isinstance(escalate_on, int):
        return after_attempts(escalate_on)
    if isinstance(escalate_on, str):
        if escalate_on == "failure":
            return on_failure
        raise ValueError(
            f"unknown escalate_on strategy {escalate_on!r}; "
            "use 'failure', a positive int (attempt count), or a predicate "
            "Callable[[EscalationContext], bool]"
        )
    raise ValueError(
        f"escalate_on must be 'failure', a positive int, or a predicate; "
        f"got {type(escalate_on).__name__}"
    )


def _outcome_failed(outcome: Optional[ActOutcome]) -> bool:
    """``outcome`` が失敗観測か(``None`` や ``failed`` を持たない観測は ``False``)。"""
    if outcome is None:
        return False
    return bool(getattr(outcome.observation, "failed", False))


@dataclass
class ModelLadder:
    """段階的に強いモデルへ昇格する ``act`` 合成フック(canonical example, 新機能ではない)。

    ``candidates`` を弱い→強いの順に並べ、``escalate_on`` 戦略に従って「いつ次段へ
    上げるか」を決める。自身も ``ActHook``(``Callable[[context], ActOutcome]``)で、
    ``run_loop(act=ladder, ...)`` の 1 行で差し込める。各反復で active な候補を 1 つ
    呼び、その :class:`ActOutcome` を **そのまま透過** して返す(tokens も改変しない
    ので ``TokenBudget`` がそのまま効く)。

    エスカレーション判断は **その反復の候補を呼ぶ前** に、前回までの履歴
    (:class:`EscalationContext`)だけを見て行う(act は後段 verify の goal 判定を
    見られないため、自前で保持した試行履歴で判断する)。昇格は単調で、一度上げたら
    下げない。最強段に達したらそこへ張り付いて retry し続ける(境界条件で安全に止まる)。

    Args:
        candidates: 弱い→強いの順に並べた ``act`` フック列(各 1 段)。空は不可。
            各段は ``ActOutcome`` を返す callable で、異種アダプタ
            (``ClaudeCodeAct`` + ``CodexAct`` 等)を混在できる。
        escalate_on: 昇格戦略。次のいずれか:

            - ``"failure"``(既定): 前段が ``failed=True`` を返したら昇格
              (:func:`on_failure`)。
            - 正の int ``N``: 同じ段を N 回呼んだら成否によらず昇格
              (``after_attempts(N)``)。act が成功扱いでも verify が goal 未達で反復が
              続くケースを埋める相補戦略。
            - predicate ``Callable[[EscalationContext], bool]``: 任意の判断
              (``True`` で昇格)。戦略の合成に使う
              (例 ``lambda ec: ec.last_failed and ec.attempts >= 2``)。

    Attributes(読み取り用):
        current_index: いま active な候補の index。
        current: いま active な候補(callable)。
        attempts: いま active な候補をこれまで呼んだ回数。
        total_attempts: ladder 全体の総呼び出し回数。
        at_top: 最強段(末尾候補)に到達済みか。
    """

    candidates: Sequence[ActHook]
    escalate_on: Union[str, int, EscalationPredicate] = "failure"

    _index: int = field(default=0, init=False, repr=False)
    _attempts: int = field(default=0, init=False, repr=False)
    _total: int = field(default=0, init=False, repr=False)
    _last_outcome: Optional[ActOutcome] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("ModelLadder requires at least one candidate")
        self._should_escalate: EscalationPredicate = _resolve_strategy(self.escalate_on)

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def current(self) -> ActHook:
        return self.candidates[self._index]

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def total_attempts(self) -> int:
        return self._total

    @property
    def at_top(self) -> bool:
        return self._index >= len(self.candidates) - 1

    def reset(self) -> None:
        """内部状態(index / 試行カウント / 前回 outcome)を初期化する。

        ``ModelLadder`` は stateful なので、1 インスタンスを別の run で使い回す前に
        呼ぶ(さもないと前 run の昇格状態を引き継ぐ)。
        """
        self._index = 0
        self._attempts = 0
        self._total = 0
        self._last_outcome = None

    def __call__(self, context: Any) -> ActOutcome:
        # 末尾段でなければ、履歴だけを見てこの反復の前に昇格するか決める
        # (昇格は 1 段ずつ。新しい段の試行カウントは 0 から)。
        if not self.at_top:
            ec = EscalationContext(
                candidate_index=self._index,
                num_candidates=len(self.candidates),
                attempts=self._attempts,
                total_attempts=self._total,
                last_outcome=self._last_outcome,
                last_failed=_outcome_failed(self._last_outcome),
            )
            if self._should_escalate(ec):
                self._index += 1
                self._attempts = 0

        outcome = self.candidates[self._index](context)

        # 試行履歴を更新(次回の判断材料)。outcome は改変せず透過する。
        self._attempts += 1
        self._total += 1
        self._last_outcome = outcome
        return outcome
