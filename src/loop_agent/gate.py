"""限定人間ゲート: 不可逆操作のみ interrupt する human-in-the-loop (Issue #15).

report.md S4.5 / R6 / 原則8 が定める「人間ゲートは不可逆・影響範囲大のアクションに
**限定**する」を最小実装する。LangGraph の ``interrupt()`` と同じ 4 種の人間決定
(**approve / edit / reject / respond**) を持ち、決定を state.db (:mod:`loop_agent.store`)
に永続化することで **pause -> resume をまたいで決定を保持** する。

設計の境界:

- **ループコアは gate 非依存**。:func:`loop_agent.loop.run_loop` は
  :class:`~loop_agent.loop.ActionGate` プロトコル (``review(context, state)``) しか
  知らず、proceed / skip / pause の 3 disposition だけを解釈する。store と人間の
  ライフサイクルは本モジュールの :class:`HumanGate` の裏に閉じる。
- **不可逆判定はループ外から注入**。``on(action) -> bool`` の述語で「この提案は
  不可逆か」を決める。reversible な action と ``gate=None`` は一切 interrupt しない
  (= 全 step ゲートにしない。report.md の「不可逆限定」を構造で担保)。
- **claude-org の pending_decisions を role 読み替えで reuse**。
  「secretary が worker の判断要求を register し、user の応答で resolve」を
  「loop が不可逆 action を register し、human が resolve」に対応付ける
  (:meth:`loop_agent.store.LoopStore.request_decision` /
  :meth:`~loop_agent.store.LoopStore.resolve_decision`)。
- **不可逆 action は resume をまたいで exactly-once + 順序整合 (Issue #21)**。approve/edit で
  実行する不可逆 action は :meth:`~loop_agent.store.LoopStore.acquire_lease` で single-winner
  に **in-progress リース** を取得し (``resolved -> executing``)、``act`` 完了後に
  :meth:`~loop_agent.store.LoopStore.complete_execution` で ``executed`` を確定する。
  リース保持中 (``executing``) に同一ゲートを見た敗者プロセスは **``executed`` まで pause**
  して待つので、勝者の不可逆 action 完了前に後続 iteration を走らせる順序ずれが起きない。
  勝者がクラッシュしてリースが失効すれば別プロセスが取り直して実行を完遂するので step も
  欠落しない。replay resume (下記) での既実行ゲートの再訪は ``executed`` を見て skip し
  **二度実行しない** (二重 deploy 等の暴発防止 = ゲートの存在意義)。

**resume の 2 モデルと gate の整合**: gate key は審査時点の ``state.iteration`` で決まる
(:class:`HumanGate` 参照)。これは resume の 2 モデルのどちらでも安定する:

1. **``initial_state`` resume (#14, 推奨)**: 中断時の :class:`~loop_agent.state.LoopState`
   (:meth:`~loop_agent.store.LoopStore.load_or_init` / :attr:`DBProgressLog.state`) を
   ``run_loop(initial_state=...)`` に渡す。``iteration`` / ``tokens_used`` / ``elapsed`` /
   ``history`` が復元され、中断 iteration から **継続** する (実行済みゲートを再訪しない)。
   gate key は iteration ベースなので、再開で最初に当たる「中断したゲート」へ正しい
   キーが振られ、永続化済み決定が再対応する。累積集計も復元されるので
   :class:`~loop_agent.conditions.TokenBudget` / :class:`~loop_agent.conditions.Timeout`
   が run を跨いで正しく効き、``history`` 依存の ``gather`` も初回と整合する。
2. **replay resume (``initial_state`` 無し)**: fresh state で iteration 0 から再生する
   後方互換モード。既実行ゲートは executed-skip (非永続) で読み飛ばし、非ゲート
   action は ``act`` が再実行される。このモードでは累積集計が前 run 分リセットされて
   見え、既実行ゲートの skip placeholder で ``history`` 内容依存の ``gather`` が乖離
   しうる (= **冪等な非ゲート action と iteration 決定的な提案列** を前提)。run を跨ぐ
   累積上限や history 依存の再開が要るなら ``initial_state`` resume を使うこと。

いずれのモードでも各 step の正本は ``step`` 行に残るので監査はそこから行える。

**複数プロセス同時 resume の協調 (Issue #21, Phase3)**: 同一 run_id を複数プロセスで *同時に*
resume してもよい。approve/edit の不可逆 action は in-progress リース
(:meth:`~loop_agent.store.LoopStore.acquire_lease` の ``resolved -> executing`` single-winner
遷移) で 1 者だけが実行権を得る。これにより:

- **exactly-once 実行**: ``resolved -> executing`` に成功するのは 1 プロセスだけ。勝者が
  ``act`` を実行し :meth:`~loop_agent.store.LoopStore.complete_execution` で ``executed`` を
  確定する。残りは下記いずれかで二度実行しない。
- **順序整合**: リース保持中 (``executing`` かつ未失効) に同一ゲートを審査した敗者は
  :meth:`HumanGate.review` で ``GATE_PAUSE`` を返し、``executed`` まで pause する (= 完了待ち)。
  勝者の不可逆 action 完了前に敗者が後続 iteration を走らせない。
- **勝者クラッシュ復旧**: 勝者が ``act`` 途中でクラッシュしリースが失効
  (``lease_expires_at <= now``) すると、待っていた別プロセスが resume 時にリースを取り直して
  (``took_over``) 実行を完遂する。step 行は完了確定の前に永続化されるため (driver が
  :attr:`loop_agent.loop.GateReview.on_complete` を on_step の後に呼ぶ)、勝者クラッシュでも
  step が欠落しない。

トレードオフ: 失効取り直しは ``act`` を再実行するので、勝者が *副作用を起こした後・``executed``
確定の前* にクラッシュした稀なケースでは副作用が重複する (**at-least-once**)。完全な
exactly-once は副作用側の冪等鍵が要る (本モジュールの範囲外)。これを避けるには ``lease_ttl``
を不可逆 action の最大所要より十分長く設定し、失効取り直し自体が起きないようにする
(:class:`HumanGate` の ``lease_ttl`` / ``now_fn``)。リース owner は既定でプロセス毎に一意
なトークンを生成する (``owner`` で明示注入も可)。

2 つの運用モード (どちらも決定は :meth:`~loop_agent.store.LoopStore.resolve_decision`
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
import os
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

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
from .notify import ApprovalDescriber, ApprovalRequest, Notifier, _summarize_action
from .state import LoopState
from .store import (
    DECISION_KINDS,
    DEFAULT_LEASE_TTL,
    LEASE_ACQUIRED,
    LEASE_EXECUTED,
    LEASE_WAIT,
    LoopStore,
    _require_json_native,
)

# 不可逆判定の述語: 提案された action (gather が返す context) を見て interrupt すべきか。
IrreversiblePredicate = Callable[[Any], bool]
# gate key 生成: (action, loop の iteration) -> 安定キー。resume 時に同じ action へ同じ
# キーを振れるよう決定的であること。既定は iteration ベース (下記 HumanGate 参照)。
GateKeyFn = Callable[[Any, int], str]


@dataclass(frozen=True)
class Decision:
    """人間が下した 1 つのゲート決定 (LangGraph interrupt パリティ)。

    ``kind`` は :data:`loop_agent.store.DECISION_KINDS` の 4 種。``payload`` は
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
    """不可逆 action のみ interrupt する :class:`~loop_agent.loop.ActionGate` 実装。

    Args:
        on: 不可逆判定の述語 ``on(action) -> bool``。``True`` の action だけがゲート
            対象。reversible な action は無条件で proceed する。
        store: 決定を永続化する :class:`~loop_agent.store.LoopStore`。
        run_id: 対象 run の ID。生成時に ``load_or_init(run_id)`` で run 行を確保する
            (FK と冪等な begin event のため)。
        resolver: 任意。同期で決定を返す人間。``None`` なら未解決時に pause する。
        key: 任意。``key(action, iteration) -> str`` で gate key を生成
            (既定 ``"gate-<iteration>"``)。``iteration`` は **その不可逆 action を審査した
            時点の loop iteration**。これが安定キーの肝で、resume の 2 モデル — replay
            (fresh state で iteration 0 から再生) と #14 の ``initial_state`` resume
            (中断 iteration から継続) — のどちらでも、同じ action は同じ iteration で
            審査されるため同じキーに揃う (出現順カウンタ方式だと initial_state resume が
            実行済みゲートを跨いだとき seq がずれる)。提案列が iteration に対して決定的で
            あること。
        active: ゲートの有効/無効。``False`` なら全 action を proceed (ゲート全停止
            スイッチ。report.md S4.5 暴走防止の「全停止」と同系統)。
        owner: 任意。in-progress リースの保持者トークン (Issue #21)。同一 run_id を resume
            する各試行を一意に識別する。``None`` なら ``"pid<PID>-<uuid>"`` を **構築毎に**
            自動生成する (= 同一プロセス内の別 resume 試行どうしも別 owner になる)。
            **resume 試行毎 (プロセス再起動を含む) に一意**であることが要件: 別試行と owner が
            衝突すると、未失効リースを「自分の再入」と誤認して不可逆 action を二重実行しうる
            (:meth:`~loop_agent.store.LoopStore.acquire_lease` の再入分岐)。安定 owner を跨いで
            pin しないこと (既定の自動生成はこの要件を満たす)。
        lease_ttl: in-progress リースの TTL (秒)。不可逆 action 1 回の実行所要より十分長く
            すること (短すぎると勝者の実行中にリースが失効し別プロセスが奪取して二重実行に
            なりうる)。既定 :data:`~loop_agent.store.DEFAULT_LEASE_TTL`。
        now_fn: リース取得/失効判定に使う **wall-clock** (epoch 秒)。複数プロセスで時刻を
            突き合わせるため monotonic ではなく ``time.time`` 既定 (loop の ``elapsed`` 用
            ``time_fn`` とは別物)。決定論的テストのため注入可能。
        notifier: 任意。新しい承認要求 (pending) を **このプロセスで初めて登録した瞬間** に
            :meth:`loop_agent.notify.Notifier.notify` を best-effort で呼ぶ通知 backend
            (:mod:`loop_agent.notify` の webhook / Slack / email / fan-out)。``None`` (既定) なら
            従来通り無通知。通知は best-effort で、失敗は warning 化して :class:`HumanGate` を
            止めない (承認要求自体は store に永続化済みなので通知が落ちても loop は進む)。
            既登録ゲートの resume 再訪では再通知しない (``entry`` が既に存在するため)。
        describe: 任意。``describe(action) -> Mapping`` で通知 payload
            (:class:`~loop_agent.notify.ApprovalRequest`) の ``summary`` / ``action_kind`` /
            ``deadline`` を導出・上書きする。未指定なら ``summary`` は action から自動生成
            (:func:`loop_agent.notify._summarize_action`)。``notifier=None`` なら呼ばれない。
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
        owner: Optional[str] = None,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        now_fn: Optional[Callable[[], float]] = None,
        notifier: Optional[Notifier] = None,
        describe: Optional[ApprovalDescriber] = None,
    ) -> None:
        self.on = on
        self.store = store
        self.run_id = run_id
        self.resolver = resolver
        self.key = key
        self.active = active
        # リース保持者トークン: プロセス (resume 試行) 毎に一意。既定はプロセス一意を生成。
        self.owner = owner or f"pid{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.lease_ttl = lease_ttl
        self.now_fn = now_fn if now_fn is not None else time.time
        self.notifier = notifier
        self.describe = describe
        # run 行を確保する (request_decision の FK と begin event を冪等に満たす)。
        self.store.load_or_init(run_id)

    def review(self, context: Any, state: LoopState) -> GateReview:
        """提案 action を審査して disposition を返す (:class:`ActionGate` 実装)。

        reversible / 無効時は即 proceed。不可逆時は永続化済みの決定を読み、

        - ``executed`` (= approve/edit で既に実行完了済み): skip する。replay resume
          (fresh state で iteration 0 から再生) や並行 resume の敗者が、実行済みの不可逆
          action を **二度実行しない** ためのガード (#14 の ``initial_state`` resume は中断
          iteration から継続するので実行済みゲートを再訪しない)。
        - ``resolved`` / ``executing``: action 一致を確認のうえ適用 (approve/edit は
          :meth:`~loop_agent.store.LoopStore.acquire_lease` で in-progress リースを取得し、
          取得できれば proceed・実行中の他者がいれば executed まで pause・既実行なら skip;
          Issue #21)。
        - 未解決 (未登録 or pending): resolver があればその場で解決して適用、無ければ
          pause して人間の決定を待つ。
        """
        if not self.active or not self.on(context):
            return GateReview(disposition=GATE_PROCEED, context=context)

        # 不可逆 action: その審査時点の loop iteration を gate key にする。resume の
        # 2 モデル (replay / initial_state) のどちらでも同じ action は同じ iteration で
        # 審査されるため、永続化済みの決定がその action に正しく再対応する。
        gate_key = (
            self.key(context, state.iteration)
            if self.key is not None
            else f"gate-{state.iteration}"
        )

        # 未登録なら pending を登録する。``request_decision`` は冪等で、**自分の
        # transaction 内で読んだ権威ある現在行** を返す。get_decision で None を見た後に
        # 別接続が insert/resolve する TOCTOU 窓があるため、None のときは
        # request_decision の戻り値 (= 並行作成済みなら相手の行) を権威として扱う。
        entry = self.store.get_decision(self.run_id, gate_key)
        if entry is None:
            entry = self.store.request_decision(self.run_id, gate_key, context)
            # 新規承認要求の発火点。このプロセスで初めて pending を登録したときのみ通知し、
            # resume 再訪 (entry が既存) や別プロセスが先に登録済みのケースでは再通知しない。
            self._notify_new_request(gate_key, context)

        # どの分岐に進む前に **必ず** 登録時の action と現在の提案 action の一致を確認する。
        # 提案列が resume 間でずれ、別の不可逆 action が同じ gate_key に来た場合に、(a) 古い
        # 決定を現在の別 action へ誤適用 / (b) 実行済みとして新しい不可逆 action を silent に
        # 握り潰す / (c) resolver が古い pending を承認して現在の別 action を実行する、の
        # いずれも防ぐ (新規登録は context そのものなので自明一致)。
        self._guard_action_matches(entry, context, gate_key)

        if entry["status"] == "executed":
            # 既に実行済みの不可逆 action (replay 再生 or 並行 resume の勝者)。再実行せず skip。
            return self._already_executed_skip(gate_key)
        if entry["status"] in ("resolved", "executing"):
            # 既に下されている決定を適用 (人間に二度問わない)。並行 resolve も
            # get_decision/request_decision の権威行でここに合流する。``executing`` は
            # 別プロセスがリース実行中 (or 自分の再入 / 失効) の状態で、approve/edit のみ
            # ここに来る (reject/respond は executing にならない)。_apply_resolved -> リース
            # 取得が WAIT/ACQUIRED/EXECUTED を判定する (Issue #21)。
            return self._apply_resolved(
                Decision(entry["decision"], entry["payload"]), context, gate_key
            )

        # status == "pending": 未解決。
        if self.resolver is not None:
            decision = self.resolver(entry)
            if not isinstance(decision, Decision):
                raise TypeError(
                    "resolver must return a Decision, got "
                    f"{type(decision).__name__}"
                )
            # resolve_decision は edit payload に JSON ネイティブを要求するので、ここに
            # 到達した時点で payload はロスレス。store 復号を介さず resolver が返した元の
            # Decision をそのまま適用し、不要な往復を避ける。
            self.store.resolve_decision(
                self.run_id, gate_key, decision.kind, decision.payload
            )
            return self._apply_resolved(decision, context, gate_key)

        # resolver 無し: 中断して人間の決定を待つ。決定は store に永続化済みなので
        # 同じ run_id で再実行すれば上の resolved 分岐で適用される。
        return GateReview(disposition=GATE_PAUSE, pending=entry)

    def _build_request(self, gate_key: str, action: Any) -> ApprovalRequest:
        """通知 payload (:class:`~loop_agent.notify.ApprovalRequest`) を構築する。

        ``summary`` は ``describe`` があればそれを優先し、無ければ action から自動生成する。
        ``describe`` が返す ``summary`` / ``action_kind`` / ``deadline`` で上書きできる。
        ``created_at`` は gate の wall-clock (``now_fn``) を使う。
        """
        fields: dict[str, Any] = {"summary": _summarize_action(action)}
        if self.describe is not None:
            extra = self.describe(action)
            if extra:
                if not isinstance(extra, Mapping):
                    raise TypeError(
                        "describe must return a Mapping of ApprovalRequest fields, "
                        f"got {type(extra).__name__}"
                    )
                fields.update(extra)
        return ApprovalRequest(
            run_id=self.run_id,
            gate_key=gate_key,
            action=action,
            summary=str(fields.get("summary", "")),
            action_kind=fields.get("action_kind"),
            deadline=fields.get("deadline"),
            created_at=self.now_fn(),
        )

    def _notify_new_request(self, gate_key: str, action: Any) -> None:
        """新規承認要求を notifier へ best-effort で通知する (失敗で loop を止めない)。

        ``notifier=None`` なら何もしない。``describe`` が例外を投げた場合も含め、通知経路の
        どの失敗も :class:`HumanGate` を止めず ``warnings.warn`` (RuntimeWarning) で可視化する。
        """
        if self.notifier is None:
            return
        try:
            request = self._build_request(gate_key, action)
            self.notifier.notify(request)
        except Exception as exc:  # noqa: BLE001 - 通知は best-effort、loop を止めない
            warnings.warn(
                f"gate {gate_key}: notifier "
                f"{type(self.notifier).__name__} failed: "
                f"{type(exc).__name__}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _guard_action_matches(
        self, entry: dict[str, Any], context: Any, gate_key: str
    ) -> None:
        """登録時の action と現在の提案 action が一致することを確認する (防御)。

        gate key は不可逆 action の出現順 seq から決まるので、提案列が resume 間で
        決定的なら登録時と同じ action に同じキーが割り当たる (契約)。万一ずれた場合に
        **別の不可逆 action へ誤って決定を適用する** のを silent に許さず、loud に弾く。

        ``stored`` は登録時に :func:`_require_json_native` 検証済みでロスレス。比較する
        現在の ``context`` も JSON ネイティブを要求して同様にロスレス化する。これを怠ると
        ``(1, 2)`` が ``[1, 2]`` に化けて別 action と誤一致しうる。提案列が決定的かつ
        JSON ネイティブなら誤検知しない。
        """
        stored = entry.get("action")
        current = json.loads(_require_json_native(context, "gated action"))
        if stored != current:
            raise ValueError(
                f"gate {gate_key}: proposed action does not match the action this "
                f"decision was recorded for (stored={stored!r}, current={current!r}); "
                "the proposal sequence is non-deterministic across resume"
            )

    def _already_executed_skip(self, gate_key: str) -> GateReview:
        """既に実行済みの不可逆 action を再生時に skip する GateReview を返す。

        observation は hashable な文字列にする (NoProgress 既定 key 対策。
        :meth:`_apply_resolved` 参照)。``persist=False``: これは前 run で実行・永続化済みの
        step を resume が読み飛ばすだけの replay no-op なので、on_step に流して本来の
        step 行 (本来の observation / tokens) を上書きで壊さないようにする。
        """
        return GateReview(
            disposition=GATE_SKIP,
            observation=f"gate-skipped:already-executed:{gate_key}",
            detail=f"gate {gate_key} already executed in a prior run",
            persist=False,
        )

    def _wait_for_executing(self, gate_key: str) -> GateReview:
        """別プロセスがリース実行中のゲートで pause する GateReview を返す (Issue #21)。

        敗者は不可逆 action を実行せず、勝者が ``executed`` を確定するまで待つ (順序整合)。
        pause なので step は記録されず、後続 iteration へ進まない。``pending`` に現在の決定行
        (``status='executing'`` とリース情報) を載せ、resume 後の再審査で executed/失効を
        判定できるようにする。
        """
        pending = self.store.get_decision(self.run_id, gate_key)
        return GateReview(disposition=GATE_PAUSE, pending=pending)

    def _make_on_complete(self, gate_key: str) -> Callable[[], None]:
        """リース取得者が ``act`` 完了後に呼ぶ完了確定クロージャを返す (Issue #21)。

        driver は step を永続化した *後* にこれを呼ぶ (:func:`loop_agent.loop.run_loop`)。
        :meth:`~loop_agent.store.LoopStore.complete_execution` が ``executing -> executed``
        を確定する。自分のリースが失効して他者に取り直されていれば 0 行更新 (False) になり、
        その場合この ``act`` の副作用は重複だった可能性がある (失効取り直しの at-least-once)。
        """

        def _complete() -> None:
            self.store.complete_execution(self.run_id, gate_key, self.owner)

        return _complete

    def _apply_resolved(
        self, decision: Decision, context: Any, gate_key: str
    ) -> GateReview:
        """resolved な決定 4 種を driver の 3 disposition へ写像する。

        approve/edit は in-progress リース (:meth:`~loop_agent.store.LoopStore.acquire_lease`)
        を single-winner に取得し、**取得できた呼び出しだけ** proceed する (Issue #21)。
        実行完了は driver が step 永続化後に呼ぶ ``on_complete``
        (:meth:`~loop_agent.store.LoopStore.complete_execution`) で ``executed`` を確定する。
        リース結果に応じて:

        - ACQUIRED: proceed (取得者が ``act`` を実行)。``on_complete`` で完了確定。
        - WAIT: 別プロセスが有効リースで実行中。``executed`` まで pause する (順序整合)。
        - EXECUTED: 既に実行済み。skip する (二重実行しない)。

        reject/respond は実行しないのでリースを取らず ``executed`` にも遷移させない
        (再生でも一貫して skip)。

        skip 系 (reject/respond と executed 再生) が記録する step の ``observation`` は
        **必ず hashable** にする。observation は ``state.history`` に積まれ、次の guard で
        :class:`~loop_agent.conditions.NoProgress` の既定 key (= observation) が
        ``Counter`` でハッシュするため。構造的な注記は文字列の ``detail`` 側に載せ、
        respond の応答本文は observation として次の ``gather`` へ渡す (応答が非 hashable
        ならそれは act 由来 observation と同じく利用者責務 = NoProgress の既定契約)。
        """
        if decision.kind in ("approve", "edit"):
            lease = self.store.acquire_lease(
                self.run_id,
                gate_key,
                self.owner,
                now=self.now_fn(),
                ttl=self.lease_ttl,
            )
            outcome = lease["outcome"]
            if outcome == LEASE_EXECUTED:
                # 既に実行完了済み (replay 再生 or 並行 resume の勝者が完了済み)。skip。
                return self._already_executed_skip(gate_key)
            if outcome == LEASE_WAIT:
                # 別プロセスが実行中。順序整合のため executed まで pause して待つ。
                return self._wait_for_executing(gate_key)
            # LEASE_ACQUIRED: 実行権を取得。act 完了後に complete_execution する on_complete を
            # driver に渡す (step 永続化後に呼ばれるので executed なら step は必ず存在)。
            on_complete = self._make_on_complete(gate_key)
            if decision.kind == "approve":
                # context は据え置き (gather した提案 action をそのまま実行)。
                return GateReview(disposition=GATE_PROCEED, on_complete=on_complete)
            # edit: 人間が差し替えた action を実行する。
            return GateReview(
                disposition=GATE_PROCEED,
                context=decision.payload,
                on_complete=on_complete,
            )
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
    owner: Optional[str] = None,
    lease_ttl: float = DEFAULT_LEASE_TTL,
    now_fn: Optional[Callable[[], float]] = None,
    notifier: Optional[Notifier] = None,
    describe: Optional[ApprovalDescriber] = None,
    time_fn: Optional[Callable[[], float]] = None,
    initial_state: Optional[LoopState] = None,
) -> LoopResult:
    """:class:`HumanGate` を組んで :func:`~loop_agent.loop.run_loop` を回す入口。

    ``run_loop`` と同じ ``act`` / ``verify`` / ``conditions`` / ``gather`` /
    ``on_step`` / ``initial_state`` を取り、人間ゲートの構成 (``on`` / ``store`` /
    ``run_id`` / ``resolver`` / ``key`` / ``active``、複数プロセス同時 resume の
    リース調整用 ``owner`` / ``lease_ttl`` / ``now_fn``、承認要求発火時の外部通知用
    ``notifier`` / ``describe``。詳細は :class:`HumanGate`) を足す。
    決定の永続化を併せたい
    場合は ``on_step`` に :meth:`loop_agent.store.DBProgressLog.on_step` を渡す。
    pause した run を **中断地点から継続** して再開するには、その永続状態
    (:attr:`~loop_agent.store.DBProgressLog.state` など) を ``initial_state`` に
    渡す (省略すると iteration 0 からの replay resume になる。差は HumanGate の
    docstring「resume の 2 モデル」を参照)。
    """
    gate = HumanGate(
        on=on,
        store=store,
        run_id=run_id,
        resolver=resolver,
        key=key,
        active=active,
        owner=owner,
        lease_ttl=lease_ttl,
        now_fn=now_fn,
        notifier=notifier,
        describe=describe,
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
        initial_state=initial_state,
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
