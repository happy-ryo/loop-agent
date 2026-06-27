"""限定人間ゲート: 不可逆操作のみ interrupt する human-in-the-loop (Issue #15).

report.md S4.5 / R6 / 原則8 が定める「人間ゲートは不可逆・影響範囲大のアクションに
**限定**する」を最小実装する。LangGraph の ``interrupt()`` と同じ 4 種の人間決定
(**approve / edit / reject / respond**) を持ち、決定を state.db (:mod:`claude_loop.store`)
に永続化することで **pause -> resume をまたいで決定を保持** する。

設計の境界:

- **ループコアは gate 非依存**。:func:`claude_loop.loop.run_loop` は
  :class:`~claude_loop.loop.ActionGate` プロトコル (``review(context, state)``) しか
  知らず、proceed / skip / pause の 3 disposition だけを解釈する。store と人間の
  ライフサイクルは本モジュールの :class:`HumanGate` の裏に閉じる。
- **不可逆判定はループ外から注入**。``on(action) -> bool`` の述語で「この提案は
  不可逆か」を決める。reversible な action と ``gate=None`` は一切 interrupt しない
  (= 全 step ゲートにしない。report.md の「不可逆限定」を構造で担保)。
- **claude-org の pending_decisions を role 読み替えで reuse**。
  「secretary が worker の判断要求を register し、user の応答で resolve」を
  「loop が不可逆 action を register し、human が resolve」に対応付ける
  (:meth:`claude_loop.store.LoopStore.request_decision` /
  :meth:`~claude_loop.store.LoopStore.resolve_decision`)。
- **不可逆 action は resume をまたいで at-most-once**。:func:`claude_loop.loop.run_loop`
  は resume 時に iteration 0 から再生する (state 復元は #14 未配線)。approve/edit で
  実行する不可逆 action は :meth:`~claude_loop.store.LoopStore.claim_execution` で
  single-winner に実行権を主張して ``executed`` に確定し、後続 resume の再生 (や並行
  resume の敗者) では skip して **二度実行しない**
  (二重 deploy 等の暴発防止 = ゲートの存在意義)。一方、ゲート対象でない reversible な
  action は再生のたび ``act`` が再実行される。したがって本 MVP の resume は
  「不可逆 action は exactly-once、非ゲート action は **冪等であること** を前提に
  再生」する契約で、完全な loop-state 復元 (再実行ゼロ) は #14 に委ねる。

2 つの運用モード (どちらも決定は :meth:`~claude_loop.store.LoopStore.resolve_decision`
を通る単一経路):

1. **async pause/resume** (``resolver=None``): 不可逆 action に未解決の決定しか無ければ
   ループは ``status="paused"`` で復帰する。人間が ``store.resolve_decision(...)`` で
   決定を記録した後に同じ run_id で再実行すると、ゲートは永続化済みの決定を読んで
   適用し、**同じ action を二度問わずに** 続行する (report.md S5 Phase2 成功条件 c)。
2. **同期 resolver** (``resolver`` を渡す): 単一プロセスで人間 (CLI プロンプト等) が
   その場で決定を返すモード。pause せず inline で解決して進む。

いずれのモードでも 4 種の決定は次のように disposition へ写像される:

- ``approve`` -> proceed (提案 action をそのまま ``act`` で実行)
- ``edit``    -> proceed (人間が差し替えた action を ``act`` で実行)
- ``reject``  -> skip (実行せず却下を 1 step として記録し継続)
- ``respond`` -> skip (実行せず人間の応答を 1 step として記録し継続。応答は
  ``state.history[-1]`` 経由で次の ``gather`` が文脈に取り込める)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .loop import (
    GATE_PAUSE,
    GATE_PROCEED,
    GATE_SKIP,
    ActHook,
    Conditions,
    GateReview,
    GatherHook,
    LoopResult,
    StepHook,
    VerifyHook,
    _default_gather,
    run_loop,
)
from .state import LoopState
from .store import DECISION_KINDS, LoopStore, _encode_observation

# 不可逆判定の述語: 提案された action (gather が返す context) を見て interrupt すべきか。
IrreversiblePredicate = Callable[[Any], bool]
# gate key 生成: (action, 出現順 seq) -> 安定キー。resume 時に同じ action へ同じキーを
# 振れるよう決定的であること。
GateKeyFn = Callable[[Any, int], str]


@dataclass(frozen=True)
class Decision:
    """人間が下した 1 つのゲート決定 (LangGraph interrupt パリティ)。

    ``kind`` は :data:`claude_loop.store.DECISION_KINDS` の 4 種。``payload`` は
    ``edit`` の置換 action、``respond`` の応答メッセージを載せる (``approve`` /
    ``reject`` では ``None``)。
    """

    kind: str
    payload: Any = None

    def __post_init__(self) -> None:
        if self.kind not in DECISION_KINDS:
            raise ValueError(
                f"unknown decision {self.kind!r}; expected one of {DECISION_KINDS}"
            )


# resolver: pending 情報を受け取り Decision を返す同期人間。pending は
# request_decision が返す行 dict (gate_key / action / status を含む)。
Resolver = Callable[[dict[str, Any]], Decision]


class HumanGate:
    """不可逆 action のみ interrupt する :class:`~claude_loop.loop.ActionGate` 実装。

    Args:
        on: 不可逆判定の述語 ``on(action) -> bool``。``True`` の action だけがゲート
            対象。reversible な action は無条件で proceed する。
        store: 決定を永続化する :class:`~claude_loop.store.LoopStore`。
        run_id: 対象 run の ID。生成時に ``load_or_init(run_id)`` で run 行を確保する
            (FK と冪等な begin event のため)。
        resolver: 任意。同期で決定を返す人間。``None`` なら未解決時に pause する。
        key: 任意。``key(action, seq) -> str`` で gate key を生成 (既定 ``"gate-<seq>"``)。
            ``seq`` は **不可逆 action の出現順** で、reversible な action では進まない。
            提案列が決定的なら resume 時にも同じ action へ同じキーが振られる。
        active: ゲートの有効/無効。``False`` なら全 action を proceed (ゲート全停止
            スイッチ。report.md S4.5 暴走防止の「全停止」と同系統)。
    """

    def __init__(
        self,
        *,
        on: IrreversiblePredicate,
        store: LoopStore,
        run_id: str,
        resolver: Optional[Resolver] = None,
        key: Optional[GateKeyFn] = None,
        active: bool = True,
    ) -> None:
        self.on = on
        self.store = store
        self.run_id = run_id
        self.resolver = resolver
        self.key = key
        self.active = active
        self._seq = 0
        # run 行を確保する (request_decision の FK と begin event を冪等に満たす)。
        self.store.load_or_init(run_id)

    def begin(self) -> None:
        """run 開始時に gate key の出現順カウンタを 0 へ戻す (run_loop が呼ぶ)。

        gate key は不可逆 action の出現順 seq から決まる。:func:`run_loop` は resume を
        iteration 0 からの再生として扱うため、**同じ HumanGate インスタンスを複数 run で
        使い回しても** seq が前 run から持ち越されてキーがずれない (gate-1 ではなく
        gate-0 に揃う) よう、run の先頭でリセットする。run ごとに新しい gate を作る
        :func:`run_gated_loop` 経由なら元から 0 なので冪等。
        """
        self._seq = 0

    def review(self, context: Any, state: LoopState) -> GateReview:
        """提案 action を審査して disposition を返す (:class:`ActionGate` 実装)。

        reversible / 無効時は即 proceed。不可逆時は永続化済みの決定を読み、

        - ``executed`` (= approve/edit で既に実行済み): skip する。resume は iteration 0
          からの再生 (#14 未配線) なので、実行済みの不可逆 action を **二度実行しない**
          ための at-most-once ガード。
        - ``resolved``: action 一致を確認のうえ適用 (approve/edit は実行前に executed を
          立てる)。
        - 未解決 (未登録 or pending): resolver があればその場で解決して適用、無ければ
          pause して人間の決定を待つ。
        """
        if not self.active or not self.on(context):
            return GateReview(disposition=GATE_PROCEED, context=context)

        # 不可逆 action: 出現順に gate key を割り当てる (resume 時も決定的に再現)。
        seq = self._seq
        self._seq += 1
        gate_key = self.key(context, seq) if self.key is not None else f"gate-{seq}"

        entry = self.store.get_decision(self.run_id, gate_key)
        if entry is not None:
            # 既存行 (pending/resolved/executed): どの分岐に進む前に **必ず** 登録時の
            # action と現在の提案 action の一致を確認する。提案列が resume 間でずれ、
            # 別の不可逆 action が同じ gate_key に来た場合に、(a) 古い決定を現在の別 action
            # へ誤適用する / (b) 実行済みとして新しい不可逆 action を silent に握り潰す /
            # (c) resolver が古い pending を承認して現在の別 action を実行する、のいずれも
            # 防ぐ (新規登録は context そのものなので下の request_decision 後は自明一致)。
            self._guard_action_matches(entry, context, gate_key)
            if entry["status"] == "executed":
                # 既に実行済みの不可逆 action。resume 再生では再実行せず skip する。
                return self._already_executed_skip(gate_key)
            if entry["status"] == "resolved":
                # resume などで既に下されている決定を適用 (人間に二度問わない)。
                return self._apply_resolved(
                    Decision(entry["decision"], entry["payload"]), context, gate_key
                )
            # status == "pending": 下の未解決パスへ落ちる (request_decision は冪等)。

        # 未解決 (未登録 or pending): まず pending を登録 (冪等)。
        pending = self.store.request_decision(self.run_id, gate_key, context)

        if self.resolver is not None:
            decision = self.resolver(pending)
            if not isinstance(decision, Decision):
                raise TypeError(
                    "resolver must return a Decision, got "
                    f"{type(decision).__name__}"
                )
            self.store.resolve_decision(
                self.run_id, gate_key, decision.kind, decision.payload
            )
            # store 経由でなく resolver が返した元の Decision を適用する (edit の
            # 置換 action が非 JSON ネイティブでも忠実に act へ渡す。store 復号値は
            # JSON 往復で repr 化しうる)。
            return self._apply_resolved(decision, context, gate_key)

        # resolver 無し: 中断して人間の決定を待つ。決定は store に永続化済みなので
        # 同じ run_id で再実行すれば上の resolved 分岐で適用される。
        return GateReview(disposition=GATE_PAUSE, pending=pending)

    def _guard_action_matches(
        self, entry: dict[str, Any], context: Any, gate_key: str
    ) -> None:
        """登録時の action と現在の提案 action が一致することを確認する (防御)。

        gate key は不可逆 action の出現順 seq から決まるので、提案列が resume 間で
        決定的なら登録時と同じ action に同じキーが割り当たる (契約)。万一ずれた場合に
        **別の不可逆 action へ誤って決定を適用する** のを silent に許さず、loud に弾く。
        登録時と同じ符号化で比較するため、提案列が決定的なら誤検知しない。
        """
        stored = entry.get("action")
        current = json.loads(_encode_observation(context))
        if stored != current:
            raise ValueError(
                f"gate {gate_key}: proposed action does not match the action this "
                f"decision was recorded for (stored={stored!r}, current={current!r}); "
                "the proposal sequence is non-deterministic across resume"
            )

    def _already_executed_skip(self, gate_key: str) -> GateReview:
        """既に実行済みの不可逆 action を再生時に skip する GateReview を返す。

        observation は hashable な文字列にする (NoProgress 既定 key 対策。
        :meth:`_apply_resolved` 参照)。
        """
        return GateReview(
            disposition=GATE_SKIP,
            observation=f"gate-skipped:already-executed:{gate_key}",
            detail=f"gate {gate_key} already executed in a prior run",
        )

    def _apply_resolved(
        self, decision: Decision, context: Any, gate_key: str
    ) -> GateReview:
        """resolved な決定 4 種を driver の 3 disposition へ写像する。

        approve/edit は実行に踏み切る *前* に ``executed`` を立て、resume 再生での
        二度実行を防ぐ (at-most-once)。reject/respond は実行しないので executed には
        遷移させない (再生でも一貫して skip)。

        approve/edit は実行権を :meth:`~claude_loop.store.LoopStore.claim_execution` で
        single-winner に主張し、**勝ち取れた呼び出しだけ** proceed する。敗者 (並行 resume
        で別者が先に実行済み) は既実行として skip し、不可逆 action の二重実行を防ぐ。

        skip 系 (reject/respond と executed 再生) が記録する step の ``observation`` は
        **必ず hashable** にする。observation は ``state.history`` に積まれ、次の guard で
        :class:`~claude_loop.conditions.NoProgress` の既定 key (= observation) が
        ``Counter`` でハッシュするため。構造的な注記は文字列の ``detail`` 側に載せ、
        respond の応答本文は observation として次の ``gather`` へ渡す (応答が非 hashable
        ならそれは act 由来 observation と同じく利用者責務 = NoProgress の既定契約)。
        """
        if decision.kind in ("approve", "edit"):
            if not self.store.claim_execution(self.run_id, gate_key):
                # 実行権を取れなかった = 別 resume が先に実行済み。二重実行を避け skip。
                return self._already_executed_skip(gate_key)
            if decision.kind == "approve":
                # context は据え置き (gather した提案 action をそのまま実行)。
                return GateReview(disposition=GATE_PROCEED)
            # edit: 人間が差し替えた action を実行する。
            return GateReview(disposition=GATE_PROCEED, context=decision.payload)
        if decision.kind == "reject":
            return GateReview(
                disposition=GATE_SKIP,
                observation=f"gate-skipped:rejected:{gate_key}",
                detail=f"human rejected gate {gate_key}",
            )
        # respond: 実行せず人間の応答を記録する (応答本文を observation として次へ渡す)。
        return GateReview(
            disposition=GATE_SKIP,
            observation=decision.payload,
            detail=f"human responded at gate {gate_key}",
        )


def run_gated_loop(
    *,
    act: ActHook,
    verify: VerifyHook,
    conditions: Conditions,
    on: IrreversiblePredicate,
    store: LoopStore,
    run_id: str,
    gather: GatherHook = _default_gather,
    on_step: Optional[StepHook] = None,
    resolver: Optional[Resolver] = None,
    key: Optional[GateKeyFn] = None,
    active: bool = True,
    time_fn: Optional[Callable[[], float]] = None,
) -> LoopResult:
    """:class:`HumanGate` を組んで :func:`~claude_loop.loop.run_loop` を回す入口。

    ``run_loop`` と同じ ``act`` / ``verify`` / ``conditions`` / ``gather`` /
    ``on_step`` を取り、人間ゲートの構成 (``on`` / ``store`` / ``run_id`` /
    ``resolver`` / ``key`` / ``active``) を足す。決定の永続化を併せたい場合は
    ``on_step`` に :meth:`claude_loop.store.DBProgressLog.on_step` を渡せばよい。
    """
    gate = HumanGate(
        on=on,
        store=store,
        run_id=run_id,
        resolver=resolver,
        key=key,
        active=active,
    )
    run_kwargs: dict[str, Any] = {}
    if time_fn is not None:
        run_kwargs["time_fn"] = time_fn
    return run_loop(
        act=act,
        verify=verify,
        conditions=conditions,
        gather=gather,
        on_step=on_step,
        gate=gate,
        **run_kwargs,
    )


__all__ = [
    "Decision",
    "HumanGate",
    "run_gated_loop",
    "IrreversiblePredicate",
    "GateKeyFn",
    "Resolver",
]
