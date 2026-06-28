"""wake 配送の transport 層: push 一次 / pull fallback / at-most-once (Issue #23)。

report.md S3.3 / S4.6 / S5 Phase3。ループの **完了 / 次反復 / 判断要求** を別ループや
窓口 (受信側) に届ける wake 配送の実体をここに新設する。claude-org runtime の broker
sidecar は runtime 所属で直接再利用できない[^pattern-only]ため、**パターンだけ抽出** して
loop-agent 側に依存ゼロ (stdlib のみ) で実装する。

抽出したパターン (出典 ``knowledge/curated/broker-transport.md`` / backend 契約):

- **push 一次 / pull fallback** (report.md S3.3)。push (in-band 注入) は *即応 accelerator*、
  pull poll が *正準配送路* (backend 中立・割り込み hazard 無し)。push が失効/不通でも
  受信側が役割 cadence で能動 poll すれば配送は途切れない。本層はこの非対称を素直に写し、
  「backend 不通でも pull fallback で配送継続」(report.md S5 Phase3 成功条件 b) を成立させる。
- **三状態 claim-then-confirm による at-most-once** (broker lost-message-window 知見)。
  単一の ``delivered`` boolean は「配達済みフラグは立つが受信側に届いていない」喪失窓を持つ。
  これを ``UNDELIVERED -> CLAIMED(lease, owner) -> DELIVERED`` の daemon 所有三状態 +
  claim-then-confirm で塞ぐ: claim で行を lease 占有して返し、受信側が処理し切ってから confirm で
  DELIVERED 化する。confirm 前に lease が失効した行は UNDELIVERED へ戻す (再 eligible)。
  確定 (DELIVERED) は ``owner`` 一致 + lease 未失効を要求する fencing で守られ、確定済みは
  二度と再配達しない (at-most-once)。同一 recipient を複数 worker で並行 poll する場合は
  worker ごとに distinct な ``owner`` を渡すこと (owner fencing が二重確定を弾く前提)。
- **role 別 cadence** (broker pull-first 知見)。push が失効する pull 環境では「待機」は idle 待機
  ではなく *能動 poll* に翻訳する。受信契機を役割別に非対称設計する (dispatcher 3m / worker
  bounded / secretary turn-prologue)。:data:`CADENCE_SECONDS` / :func:`due_to_poll` がその最小形。

設計の境界 (report.md S6「transport の runtime 依存」):

- runtime 非依存・自己完結。``pane`` / ``tmux`` / ``renga`` / ``broker`` CLI に一切依存しない。
  push backend は :class:`PushBackend` Protocol の注入で差し替え可能にし (best-effort ``bool``
  契約は ``tools/peer_notify.py`` から踏襲)、配送の正本 (queue) は backend と独立に持つ。
- 受信側は **idempotent handler 前提**。wake は :attr:`Wake.id` で同一性を持ち、二重 enqueue は
  no-op、push/pull の継ぎ目で稀に二重配送が起きても受信側は id で de-dup できる (report.md の
  「残余窓は at-least-once + 冪等表示で許容、idle-wake では喪失 > 重複表示」方針)。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, runtime_checkable

# -- wake 種別 (report.md S5 Phase3「ループの完了/次反復/判断要求の wake を配送」) ---------
#
# 読み手が文字列リテラルを散在させずに filter / dispatch できるよう定数化する。
WAKE_LOOP_DONE = "loop_done"  # ループが終了した (goal_met / stopped)。
WAKE_NEXT_ITERATION = "next_iteration"  # 次反復へ進む / 次タスクを起こす。
WAKE_DECISION_REQUEST = "decision_request"  # 不可逆 action の人間判断を要求 (人間ゲート)。

WAKE_KINDS = (WAKE_LOOP_DONE, WAKE_NEXT_ITERATION, WAKE_DECISION_REQUEST)

# 受信側の配送状態 (三状態)。daemon (= 本 queue) が所有する。
UNDELIVERED = "undelivered"
CLAIMED = "claimed"
DELIVERED = "delivered"


@dataclass(frozen=True)
class Wake:
    """配送する 1 件の wake。

    ``id`` は **配送の同一性** であり at-most-once / de-dup の鍵。ループ wake では
    ``f"{run_id}:{kind}:{iteration}"`` のような決定的 id を与えると、resume での再配送や
    push/pull の継ぎ目での二重配送を受信側が id で de-dup できる (同一 id の二重 enqueue は
    no-op になる)。``recipient`` は宛先 (role 名や peer id)。``payload`` は kind 固有の
    補足情報 (終了理由・gate_key 等)。
    """

    id: str
    kind: str
    recipient: str
    run_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 化しやすいフラットな dict へ畳む (sink / backend のシリアライズ用)。

        ``payload`` を先に展開し、正準フィールド (``id`` / ``kind`` / ``recipient`` /
        ``run_id``) を **後勝ち** で上書きする。これにより payload に同名キーが紛れ込んでも、
        de-dup / routing の正本である正準フィールドが必ず保たれる (payload 由来の ``id`` が
        queue の de-dup 鍵と食い違って別宛先へ送られる、といった事故を防ぐ)。同名 payload キーは
        正準値に隠れる (予約名は正準が勝つ、という契約)。
        """
        return {
            **dict(self.payload),
            "id": self.id,
            "kind": self.kind,
            "recipient": self.recipient,
            "run_id": self.run_id,
        }


@runtime_checkable
class PushBackend(Protocol):
    """push (一次・即応 accelerator) の最小の口。

    ``push(wake) -> bool`` は **best-effort** (``tools/peer_notify.py`` の bool 契約を踏襲):
    確定配送できたときだけ ``True``、それ以外 (backend 不通・タイムアウト・宛先不在 等) は
    ``False`` を返す。例外を投げてもよい (:class:`Transport` が握って ``False`` 扱いにする) が、
    理想的には投げず ``False`` を返すこと。``True`` を返せなかった wake は queue に残り、
    受信側の pull poll が拾う (= pull fallback)。
    """

    def push(self, wake: Wake) -> bool:
        ...


class CallablePushBackend:
    """任意の ``callable(Wake) -> bool`` を :class:`PushBackend` に適合させる薄いアダプタ。"""

    def __init__(self, fn: Callable[[Wake], bool]) -> None:
        self._fn = fn

    def push(self, wake: Wake) -> bool:
        return self._fn(wake)


class NullPushBackend:
    """常に push 失敗する backend (= backend 不通の明示モデル)。

    push 一次を持たない / backend がダウンしている構成を表す。すべての wake は queue に
    残り pull fallback だけで配送される。「backend 不通でも pull fallback で配送継続」の
    既定構成であり、テストの基準にもなる。
    """

    def push(self, wake: Wake) -> bool:
        return False


@dataclass
class _Entry:
    """queue 内の 1 wake の配送状態 (三状態 + lease 所有権)。"""

    wake: Wake
    seq: int
    state: str = UNDELIVERED
    owner: Optional[str] = None
    lease_expiry: float = 0.0


@runtime_checkable
class WakeQueue(Protocol):
    """配送の正本 (durable spine)。三状態 claim-then-confirm を提供する。

    :class:`Transport` は backend (push) とは独立にこの queue を正本として持つ。push が
    確定配送できなくても wake は queue に残り、受信側の :meth:`claim` -> :meth:`confirm` で
    pull 配送される。
    """

    def enqueue(self, wake: Wake) -> bool:
        ...

    def claim(
        self, recipient: str, *, now: float, lease: float, owner: str, limit: Optional[int] = None
    ) -> list[Wake]:
        ...

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        ...

    def release_expired(self, *, now: float) -> int:
        ...

    def mark_delivered(self, wake_id: str) -> bool:
        ...

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        ...

    def state_of(self, wake_id: str) -> Optional[str]:
        ...


class InMemoryWakeQueue:
    """:class:`WakeQueue` のインメモリ実装 (三状態 claim-then-confirm)。

    ループ自身のプロセス内で wake を保持する既定の queue。``state.db`` 永続化を別途
    被せたいときは同じ :class:`WakeQueue` Protocol を SQLite で実装すればよい (本 PoC は
    インメモリで at-most-once / fallback のセマンティクスを実証する)。

    状態遷移 (daemon 所有・行レベル所有権で single-drainer 性を担保):

    - ``enqueue`` : 同一 ``id`` が既にあれば no-op (二重 enqueue 冪等 = de-dup の土台)。
    - ``claim``   : 期限切れ lease を回収してから、宛先の ``UNDELIVERED`` を seq 順に
      ``CLAIMED`` (owner + lease_expiry) にして返す。
    - ``confirm`` : ``CLAIMED`` かつ **owner が claim 時のまま** で、かつ lease 未失効なら
      ``DELIVERED`` (terminal) にする。lease 失効後に別 owner が再 claim した行への stale な
      confirm は owner 不一致で弾く (fencing) ので、喪失窓で「届いていないのに DELIVERED」化
      しない (前提: 並行 poll は distinct owner を使う。:meth:`~loop_agent.transport.Transport.poll` 参照)。
    - ``release_expired`` : lease 失効した ``CLAIMED`` を ``UNDELIVERED`` へ戻す (再 eligible)。
    - ``mark_delivered`` : push が確定配送した wake を直接 ``DELIVERED`` (terminal) にする。
      任意の非 terminal 状態から冪等に遷移できる (push と pull の継ぎ目を吸収)。

    **スレッド安全**: 同一 recipient を複数 worker (スレッド) で並行 poll しても二重 claim が
    起きないよう、状態を変える操作 (enqueue/claim/confirm/release_expired/mark_delivered) と
    読み出しを 1 つの再入可能ロックで直列化する (check-and-set を atomic にする)。``claim`` は
    内部で ``release_expired`` を呼ぶため :class:`threading.RLock` (再入可) を使う。これにより
    並行 poller の owner fencing と at-most-once claim が実際に成立する。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._seq = 0
        # 状態遷移を直列化する再入可能ロック (claim -> release_expired の再入のため RLock)。
        self._lock = threading.RLock()

    def enqueue(self, wake: Wake) -> bool:
        """wake を ``UNDELIVERED`` で登録する。同一 ``id`` が既にあれば no-op で ``False``。

        二重 enqueue を冪等にすることで、deliver の再試行や resume での再配送指示が
        既存行 (進行中の claim や DELIVERED) を壊さない (= 人間/受信側に二重に届けない土台)。
        """
        if not wake.id:
            raise ValueError("enqueue: Wake.id must be a non-empty string")
        with self._lock:
            if wake.id in self._entries:
                return False
            self._entries[wake.id] = _Entry(wake=wake, seq=self._seq)
            self._seq += 1
            return True

    def release_expired(self, *, now: float) -> int:
        """lease 失効した ``CLAIMED`` を ``UNDELIVERED`` へ戻し、戻した件数を返す。

        confirm 前に受信側が死ぬ (claim と confirm の間で crash) と CLAIMED のまま滞留する。
        lease 失効でこれを再 eligible に戻すことで、配送は止まらず再 claim される
        (at-least-once 側に倒す: idle-wake では喪失 > 重複)。``owner`` を ``None`` に戻し、
        古い owner の遅延 confirm を確実に弾く。
        """
        with self._lock:
            released = 0
            for e in self._entries.values():
                if e.state == CLAIMED and e.lease_expiry <= now:
                    e.state = UNDELIVERED
                    e.owner = None
                    released += 1
            return released

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        """``recipient`` 宛の ``UNDELIVERED`` wake を lease 占有して返す (pull の claim)。

        まず期限切れ lease を回収 (:meth:`release_expired`) してから、宛先一致の
        ``UNDELIVERED`` を **登録順 (seq)** に最大 ``limit`` 件 ``CLAIMED`` にする。各行に
        ``owner`` と ``now + lease`` の期限を刻む。返した wake は呼び出し側が処理し切ってから
        :meth:`confirm` で確定する (claim-then-confirm)。
        """
        if lease <= 0:
            raise ValueError("claim: lease must be > 0")
        with self._lock:
            self.release_expired(now=now)
            out: list[Wake] = []
            for e in sorted(self._entries.values(), key=lambda x: x.seq):
                if limit is not None and len(out) >= limit:
                    break
                if e.state == UNDELIVERED and e.wake.recipient == recipient:
                    e.state = CLAIMED
                    e.owner = owner
                    e.lease_expiry = now + lease
                    out.append(e.wake)
            return out

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        """claim 済み wake を ``DELIVERED`` (terminal) に確定する。

        ``CLAIMED`` かつ現在の ``owner`` が claim 時の owner と一致し、かつ lease 未失効の
        ときだけ確定して ``True`` を返す。それ以外 (既に DELIVERED / owner 不一致 = lease
        失効後に別者が再 claim / lease 失効済み / 不在) は ``False``。この owner + 失効チェックが
        fencing として効き、喪失窓で stale な claim 者が誤って DELIVERED 化するのを防ぐ。
        """
        with self._lock:
            e = self._entries.get(wake_id)
            if e is None:
                return False
            if e.state != CLAIMED:
                return False
            if e.owner != owner:
                return False
            if e.lease_expiry <= now:
                # lease 失効: この claim はもう有効でない。release_expired で UNDELIVERED へ
                # 戻る (まだ戻っていなくても) ので、ここで DELIVERED 化はしない。
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def mark_delivered(self, wake_id: str) -> bool:
        """``UNDELIVERED`` の wake を直接 ``DELIVERED`` (terminal) にする (push 確定配送の確定)。

        push backend が ``True`` (確定配送) を返したときに使う。**``UNDELIVERED`` のときだけ**
        遷移し、遷移できたら ``True``、それ以外 (既に ``DELIVERED`` / ``CLAIMED`` / 不在) は
        ``False`` を返す。

        ``CLAIMED`` を **奪わない** のが要点: push の I/O 中 (queue ロック外) に別の poller が
        同じ wake を claim しうる。そこで無条件に DELIVERED 化すると active claim の owner を
        消し、その poller が confirm 前にクラッシュしても lease 失効で再 eligible に戻れず
        **wake を喪失** する (claim-then-confirm の crash recovery 破壊)。push と pull が同じ
        wake を競合した場合は **pull claim を配送の主体** とし、push 側は既配送の重複として
        受信側の id de-dup に委ねる (at-least-once。喪失 > 重複の方針)。push が DELIVERED 化
        できた行は UNDELIVERED でないため pull は claim しない (継ぎ目を吸収)。
        """
        with self._lock:
            e = self._entries.get(wake_id)
            if e is None:
                return False
            if e.state != UNDELIVERED:
                # 既に DELIVERED、または別 poller が claim 済み (CLAIMED)。claim は奪わない。
                return False
            e.state = DELIVERED
            e.owner = None
            return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """未確定 (``UNDELIVERED`` / ``CLAIMED``) の wake を登録順に返す (任意で宛先で絞る)。"""
        with self._lock:
            out: list[Wake] = []
            for e in sorted(self._entries.values(), key=lambda x: x.seq):
                if e.state == DELIVERED:
                    continue
                if recipient is not None and e.wake.recipient != recipient:
                    continue
                out.append(e.wake)
            return out

    def state_of(self, wake_id: str) -> Optional[str]:
        """``wake_id`` の現在の配送状態を返す (無ければ ``None``)。テスト/内省用。"""
        with self._lock:
            e = self._entries.get(wake_id)
            return e.state if e is not None else None


# 役割別 poll cadence (秒)。report.md S3.2 / broker pull-first 知見の非対称設計。
# push が失効する pull 環境では「待機」を idle 待機ではなく能動 poll に翻訳する。
#
# - dispatcher : 監視 /loop 3m 相当 = 180s 間隔で能動 poll。
# - worker     : 完了報告後の bounded review-watch 相当 = 短間隔 poll。
# - secretary  : 人間対話主体で blocking poll 不可 -> ターン冒頭で毎回 poll (0 = 常に due)。
CADENCE_SECONDS: dict[str, float] = {
    "dispatcher": 180.0,
    "worker": 60.0,
    "secretary": 0.0,
}

# 未知 role の既定 cadence (保守的に worker 相当)。
DEFAULT_CADENCE_SECONDS = 60.0


def cadence_for(role: str) -> float:
    """``role`` の poll 間隔 (秒) を返す。未知 role は :data:`DEFAULT_CADENCE_SECONDS`。"""
    return CADENCE_SECONDS.get(role, DEFAULT_CADENCE_SECONDS)


def due_to_poll(role: str, last_poll: Optional[float], now: float) -> bool:
    """``role`` が ``now`` 時点で能動 poll すべきかを返す。

    ``last_poll`` が ``None`` (一度も poll していない) なら常に due。それ以外は
    ``now - last_poll >= cadence_for(role)`` で判定する。cadence が ``0`` (secretary:
    ターン冒頭で毎回 poll) の role は常に due になる。受信側の poll ループが「自分の番か」を
    判断する最小ヘルパで、idle 待機を能動 poll に翻訳するパターンの核 (報告 prose の「待機」を
    pull 環境で能動 poll ループへ写す)。
    """
    if last_poll is None:
        return True
    return (now - last_poll) >= cadence_for(role)


class Transport:
    """push 一次 / pull fallback の wake 配送オーケストレータ。

    1 つの :class:`WakeQueue` (配送の正本) と任意の :class:`PushBackend` (一次・即応
    accelerator) を束ねる。:meth:`deliver` は **まず queue に durable に積んでから** push を
    試み、push が確定配送できたら DELIVERED 化、できなければ ``UNDELIVERED`` のまま残して
    pull fallback に委ねる。受信側は :meth:`poll` で自分宛の wake を claim-then-confirm で
    引き取る。

    この「正本は queue・push は accelerator」構造により、backend 不通でも
    (:class:`NullPushBackend` でも push が常時失敗でも) 配送は pull で継続する
    (report.md S5 Phase3 成功条件 b)。
    """

    def __init__(
        self,
        queue: Optional[WakeQueue] = None,
        backend: Optional[PushBackend] = None,
        *,
        lease: float = 30.0,
        time_fn: Callable[[], float] = None,  # type: ignore[assignment]
    ) -> None:
        self.queue: WakeQueue = queue if queue is not None else InMemoryWakeQueue()
        self.backend = backend
        if lease <= 0:
            raise ValueError("Transport: lease must be > 0")
        self._lease = lease
        if time_fn is None:
            import time

            time_fn = time.monotonic
        self._time_fn = time_fn

    # -- 送信側 (deliver) ----------------------------------------------------

    def deliver(self, wake: Wake) -> str:
        """1 件の wake を配送する。``"push"`` (一次で確定) か ``"queued"`` (pull 待ち) を返す。

        手順 (正本優先): まず queue へ durable に enqueue する (push が落ちても喪失しない)。
        backend があれば push を best-effort で試み、確定配送 (``True``) なら DELIVERED 化して
        ``"push"`` を返す。backend 不在 / push 失敗 / push 例外なら ``UNDELIVERED`` のまま残し
        ``"queued"`` を返す — 受信側の :meth:`poll` が pull で拾う (= fallback)。

        同一 ``id`` の再 deliver は enqueue が no-op になり、**進行中の配送を乱さない**。
        push を (再) 試行するのは「今回新規に積まれた」か「まだ ``UNDELIVERED`` (誰も claim
        していない)」wake に限る:

        - 既に ``DELIVERED``: 配送確定済み。push も pull も重ねない (``"push"`` を返す)。
        - 既に ``CLAIMED``: 受信側が pull で claim 済み (confirm 待ち)。ここで push を重ねて
          :meth:`~WakeQueue.mark_delivered` すると、owner の lease を奪って claim-then-confirm の
          **失効再配送保護を壊す** (owner が confirm 前にクラッシュした wake が再 eligible に
          戻らなくなる)。active claim はそのまま尊重し、配送は pull に委ねる (``"queued"``)。
        - ``UNDELIVERED`` / 内省不可 queue の ``None``: active claim が無いので push 再試行は
          安全 (backend 復旧後に ``queued`` から ``push`` へ昇格できる)。
        """
        newly = self.queue.enqueue(wake)
        if not newly:
            state = _state_of(self.queue, wake.id)
            if state == DELIVERED:
                return "push"  # 既に配送確定。再送しない。
            if state == CLAIMED:
                # 進行中の pull claim を尊重する (push で横取り確定しない)。
                return "queued"
            # state は UNDELIVERED か None: active claim 無し。push 再試行は安全。
        if self.backend is not None and self._try_push(wake):
            self.queue.mark_delivered(wake.id)
            return "push"
        return "queued"

    def _try_push(self, wake: Wake) -> bool:
        """backend.push を best-effort で呼ぶ。例外は握って ``False`` (= 未配送) 扱い。"""
        try:
            return bool(self.backend.push(wake))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - push は best-effort。失敗は pull fallback に委ねる。
            return False

    # -- 受信側 (poll) -------------------------------------------------------

    def poll(
        self,
        recipient: str,
        *,
        owner: Optional[str] = None,
        limit: Optional[int] = None,
        confirm: bool = False,
    ) -> list[Wake]:
        """``recipient`` 宛の未配送 wake を pull で claim する (claim-then-confirm の claim)。

        ``UNDELIVERED`` な wake を lease 占有 (claim) して返す。**確定はしない** (既定
        ``confirm=False``): 呼び出し側が wake を **処理し切ってから** :meth:`confirm_wakes` を
        呼んで ``DELIVERED`` 化する責務を負う。処理中にクラッシュした (= confirm 前に死んだ) 場合は
        lease 失効でその wake が再 eligible に戻り再配送される (at-least-once: idle-wake では
        喪失 > 重複)。この claim-then-confirm が crash recovery の肝なので **既定は確定しない**。
        確定漏れを避けたい一般ケースは :meth:`poll_and_handle` (handler 成功後に wake 単位で
        confirm する crash-safe な受信ループ) を使うのが推奨。

        ``confirm=True`` を明示すると、claim した wake を **返す前に即 confirm** する (戻り値の
        受領 = 配送完了とみなせる、handler が決して失敗しない / プロセス内自己完結な単純ケース
        専用)。この場合 poll が返った後に処理がクラッシュしても wake は既に ``DELIVERED`` で
        再配送されない (= その経路だけ at-most-once で喪失しうる) ことに注意。

        ``owner`` は claim の所有者識別 (省略時は ``recipient``)。同一受信者を複数 worker で
        並行 poll する場合は worker ごとに **distinct な owner** を渡すこと。三状態の owner
        fencing が「lease 失効後に別 worker が再 claim した wake への stale な confirm」を弾く。
        """
        own = owner if owner is not None else recipient
        now = self._time_fn()
        wakes = self.queue.claim(
            recipient, now=now, lease=self._lease, owner=own, limit=limit
        )
        if confirm:
            for w in wakes:
                self.queue.confirm(w.id, owner=own, now=now)
        return wakes

    def poll_and_handle(
        self,
        recipient: str,
        handler: Callable[[Wake], Any],
        *,
        owner: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        """claim -> handler(wake) -> confirm を wake 単位で行う crash-safe な受信ループ (推奨)。

        各 wake を claim し、``handler(wake)`` が **例外なく返ったものだけ** confirm して
        ``DELIVERED`` 化する。これにより「受信したが処理前に死んだ」喪失窓が無い: handler が
        raise した wake (および以降の未処理 wake) は confirm されず、lease 失効後に再配送される
        (at-least-once。受信側は :attr:`Wake.id` で de-dup する idempotent handler 前提)。

        正常に処理・確定できた wake の list を返す。``handler`` の例外は **握り潰さず伝播** する
        (呼び出し側が失敗を観測できる。未確定 wake は再配送で拾われる)。confirm は handler 成功
        *後* の現在時刻で行うので、handler が lease を超えて長引いた wake は fencing で弾かれ
        (確定されず) 再配送に回る — 長い処理には十分大きい ``lease`` を設定すること。

        ``owner`` / ``limit`` の意味は :meth:`poll` と同じ (省略時 owner=recipient)。
        """
        own = owner if owner is not None else recipient
        claimed = self.queue.claim(
            recipient, now=self._time_fn(), lease=self._lease, owner=own, limit=limit
        )
        handled: list[Wake] = []
        for w in claimed:
            handler(w)  # raise すれば未 confirm のまま伝播 -> lease 失効で再配送。
            if self.queue.confirm(w.id, owner=own, now=self._time_fn()):
                handled.append(w)
        return handled

    def confirm_wakes(self, wakes: Iterable[Wake], *, owner: str) -> int:
        """claim 済み wake 群を確定する (:meth:`poll` を ``confirm=False`` で使ったときの確定 API)。

        確定できた (この ``owner`` が lease を保持していた) 件数を返す。``owner`` は claim 時に
        渡した値と同じものを渡すこと (省略時 owner=recipient で poll したなら recipient)。
        lease 失効後に呼ぶと owner/失効 fencing で弾かれ、その wake は再配送対象として queue に残る。
        """
        now = self._time_fn()
        confirmed = 0
        for w in wakes:
            if self.queue.confirm(w.id, owner=owner, now=now):
                confirmed += 1
        return confirmed

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """未確定 (未配送) の wake を返す (queue へ委譲)。テスト/監視用。"""
        return self.queue.pending(recipient)


def _state_of(queue: WakeQueue, wake_id: str) -> Optional[str]:
    """queue の配送状態を引く (``state_of`` は :class:`WakeQueue` Protocol の一員)。

    Protocol は構造的で実行時強制されないため、``state_of`` を欠く非準拠 queue でも
    :meth:`Transport.deliver` を落とさないよう getattr で防御する (欠落時は ``None`` =
    「状態不明」扱いにし、push 重複防止の早期 return を諦めるだけで配送自体は継続する)。
    """
    fn = getattr(queue, "state_of", None)
    if fn is None:
        return None
    return fn(wake_id)


# ---------------------------------------------------------------------------
# クロスプロセス backend (Issue #41)
#
# :class:`InMemoryWakeQueue` は単一プロセス内でしか正本を共有できない。別プロセスの
# ループ / 窓口へ wake を配送するには queue の正本を **プロセス外の永続ストア** に置く必要が
# ある。:class:`WakeQueue` Protocol は backend 中立なので、同じ三状態 claim-then-confirm
# セマンティクスを SQLite (stdlib のみ) / Redis (optional dep) で実装すれば、:class:`Transport`
# の Public API を一切変えずに backend を差し替えられる (in-memory が既定、明示で SQLite/Redis)。
#
# 設計判断:
#
# - **serialization = JSON** (pickle ではない)。``payload`` は JSON 形 (:meth:`Wake.to_dict`
#   が前提) で、JSON はプロセス/言語横断で安全・任意コード実行の risk が無い。``payload`` は
#   JSON 化可能な値のみ (非対応の値は enqueue で ``TypeError``)。
# - **key namespace 規約**。Redis は ``{namespace}:wake:{id}`` / ``{namespace}:recipient:{r}``
#   等で他用途のキーと衝突を避ける (namespace 既定 ``"loop_agent"``)。SQLite は table 名で
#   分離する (既定 ``wakes``)。
# - **TTL / cleanup**。long-running ループでは DELIVERED レコードが残留する。Redis は確定時に
#   ``EXPIRE`` で自動失効 (``delivered_ttl``)、SQLite は :meth:`SqliteWakeQueue.purge_delivered`
#   で明示回収する (monotonic clock では wall-clock TTL を当てにできないため、SQLite は明示回収を
#   既定とする)。
#
# **クロスプロセスの時計に関する重要注意**: lease 失効判定は :class:`Transport` が渡す ``now``
# (既定 ``time.monotonic``) を共有 backend 上で突き合わせる。``time.monotonic`` は **プロセス毎に
# 原点が異なる** ため、同一 SQLite/Redis backend を複数プロセスで共有する構成では、各プロセスの
# :class:`Transport` に **wall-clock (``time_fn=time.time``) を渡して時計を揃える** こと。さもないと
# あるプロセスが書いた ``lease_expiry`` を別プロセスの monotonic と比較して lease 判定が壊れる。


def _dumps_payload(payload: Mapping[str, Any]) -> str:
    """``payload`` を JSON 文字列へ畳む (backend 永続化用)。

    JSON 化不能な値は ``TypeError`` を ``ValueError`` に翻訳して、enqueue の入力検証として
    呼び出し側に分かりやすく返す (pickle を使わない = 任意コード実行 risk を持ち込まない)。
    """
    try:
        return json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    except TypeError as exc:
        raise ValueError(f"Wake.payload must be JSON-serializable: {exc}") from exc


def _make_wake(id: str, kind: str, recipient: str, run_id: str, payload_json: str) -> Wake:
    """backend から読んだフィールドを :class:`Wake` へ復元する。"""
    return Wake(
        id=id,
        kind=kind,
        recipient=recipient,
        run_id=run_id,
        payload=json.loads(payload_json) if payload_json else {},
    )


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str, *, what: str) -> str:
    """SQL 識別子 (table 名) を検証する (SQL injection 防止: table 名は bind できないため)。"""
    if not _IDENT_RE.match(name):
        raise ValueError(f"{what} must match {_IDENT_RE.pattern!r}, got {name!r}")
    return name


class SqliteWakeQueue:
    """:class:`WakeQueue` の SQLite 実装 (stdlib ``sqlite3`` のみ・プロセス外永続)。

    三状態 claim-then-confirm / owner fencing のセマンティクスは :class:`InMemoryWakeQueue` と
    **完全に等価** で、正本を SQLite ファイル (または ``:memory:``) に置くことでプロセスを跨いだ
    配送を可能にする。``path`` にファイルパスを渡せば複数プロセスが同じ正本を共有でき、
    ``":memory:"`` (既定) は単一プロセス内の永続テスト用。

    **原子性**: 状態を変える操作 (enqueue/claim/confirm/release_expired/mark_delivered) は
    ``BEGIN IMMEDIATE`` で write lock を取って 1 トランザクションに収め、check-and-set を
    atomic にする。プロセス内の並行 poller はプロセス内 :class:`threading.RLock` で直列化し、
    プロセス間は SQLite のファイルロック + ``busy_timeout`` で待ち合わせる (``WAL`` で読み書きの
    並行性を上げる)。これにより複数プロセス/スレッドが並行 poll しても二重 claim しない。

    **接続**: 1 接続を ``check_same_thread=False`` で開き、上記ロックで保護する (``:memory:`` は
    接続毎に別 DB になるため単一接続が必須)。``isolation_level=None`` (autocommit) でトランザクションを
    明示制御する。使い終わったら :meth:`close` するか ``with`` 文で使う。
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        table: str = "wakes",
        busy_timeout: float = 5.0,
    ) -> None:
        self._path = path
        self._t = _validate_identifier(table, what="table")
        # claim は内部で release_expired 相当の SQL を同一 tx で呼ぶ。RLock で再入を許す。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)}")
        # WAL はファイル DB の読み書き並行性を上げる (:memory: では no-op 相当)。
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx():
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._t} (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    id           TEXT    NOT NULL UNIQUE,
                    kind         TEXT    NOT NULL,
                    recipient    TEXT    NOT NULL,
                    run_id       TEXT    NOT NULL,
                    payload      TEXT    NOT NULL,
                    state        TEXT    NOT NULL,
                    owner        TEXT,
                    lease_expiry REAL    NOT NULL DEFAULT 0
                )
                """
            )
            # claim/pending: 宛先 + 状態を seq 順に引く。release_expired: 状態 + lease で掃く。
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._t}_recipient_state "
                f"ON {self._t}(recipient, state, seq)"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._t}_state_lease "
                f"ON {self._t}(state, lease_expiry)"
            )

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """``BEGIN IMMEDIATE`` で write lock を取り、commit/rollback まで直列化する。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def enqueue(self, wake: Wake) -> bool:
        if not wake.id:
            raise ValueError("enqueue: Wake.id must be a non-empty string")
        payload = _dumps_payload(wake.payload)
        with self._tx():
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO {self._t} "
                "(id, kind, recipient, run_id, payload, state, owner, lease_expiry) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                (wake.id, wake.kind, wake.recipient, wake.run_id, payload, UNDELIVERED),
            )
            return cur.rowcount > 0  # INSERT OR IGNORE: 挿入=1 / 重複 id で無視=0。

    def _release_expired_locked(self, now: float) -> int:
        cur = self._conn.execute(
            f"UPDATE {self._t} SET state = ?, owner = NULL "
            "WHERE state = ? AND lease_expiry <= ?",
            (UNDELIVERED, CLAIMED, now),
        )
        return cur.rowcount

    def release_expired(self, *, now: float) -> int:
        with self._tx():
            return self._release_expired_locked(now)

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        if lease <= 0:
            raise ValueError("claim: lease must be > 0")
        with self._tx():
            self._release_expired_locked(now)
            sql = (
                f"SELECT id, kind, recipient, run_id, payload FROM {self._t} "
                "WHERE state = ? AND recipient = ? ORDER BY seq"
            )
            params: list[Any] = [UNDELIVERED, recipient]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = self._conn.execute(sql, params).fetchall()
            new_expiry = now + lease
            for r in rows:
                self._conn.execute(
                    f"UPDATE {self._t} SET state = ?, owner = ?, lease_expiry = ? WHERE id = ?",
                    (CLAIMED, owner, new_expiry, r["id"]),
                )
            return [
                _make_wake(r["id"], r["kind"], r["recipient"], r["run_id"], r["payload"])
                for r in rows
            ]

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        with self._tx():
            # owner 一致 + lease 未失効 (lease_expiry > now) を満たす CLAIMED のみ確定 (fencing)。
            cur = self._conn.execute(
                f"UPDATE {self._t} SET state = ?, owner = NULL "
                "WHERE id = ? AND state = ? AND owner = ? AND lease_expiry > ?",
                (DELIVERED, wake_id, CLAIMED, owner, now),
            )
            return cur.rowcount > 0

    def mark_delivered(self, wake_id: str) -> bool:
        with self._tx():
            # UNDELIVERED のときだけ確定 (CLAIMED な active claim を奪わない)。
            cur = self._conn.execute(
                f"UPDATE {self._t} SET state = ?, owner = NULL WHERE id = ? AND state = ?",
                (DELIVERED, wake_id, UNDELIVERED),
            )
            return cur.rowcount > 0

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        with self._lock:
            sql = (
                f"SELECT id, kind, recipient, run_id, payload FROM {self._t} "
                "WHERE state != ?"
            )
            params: list[Any] = [DELIVERED]
            if recipient is not None:
                sql += " AND recipient = ?"
                params.append(recipient)
            sql += " ORDER BY seq"
            rows = self._conn.execute(sql, params).fetchall()
            return [
                _make_wake(r["id"], r["kind"], r["recipient"], r["run_id"], r["payload"])
                for r in rows
            ]

    def state_of(self, wake_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT state FROM {self._t} WHERE id = ?", (wake_id,)
            ).fetchone()
            return row["state"] if row is not None else None

    def purge_delivered(self) -> int:
        """確定済み (``DELIVERED``) レコードを物理削除し、削除件数を返す (cleanup)。

        long-running ループでは確定済み行が残留するため、保守として定期的に呼んで回収する。
        非確定 (``UNDELIVERED`` / ``CLAIMED``) は触らないので配送中の wake を喪失しない。
        """
        with self._tx():
            cur = self._conn.execute(
                f"DELETE FROM {self._t} WHERE state = ?", (DELIVERED,)
            )
            return cur.rowcount

    def close(self) -> None:
        """SQLite 接続を閉じる (``:memory:`` では DB ごと破棄される)。"""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteWakeQueue":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _import_redis() -> Any:
    """optional dep ``redis`` を import gate 越しに読み込む (未導入なら親切なエラー)。"""
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise ImportError(
            "RedisWakeQueue requires the optional 'redis' dependency. "
            "Install it with: pip install 'loop-agent[redis]'"
        ) from exc
    return redis


def _text(value: Any) -> str:
    """redis-py が返す bytes/str を str へ正規化する (``decode_responses`` 非依存にする)。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class RedisWakeQueue:
    """:class:`WakeQueue` の Redis 実装 (optional dep ``redis``・プロセス外永続)。

    三状態 claim-then-confirm / owner fencing のセマンティクスは :class:`InMemoryWakeQueue` /
    :class:`SqliteWakeQueue` と等価。正本を Redis に置くことで、別ホストのプロセス間でも wake を
    配送できる。``redis`` 未導入の環境では生成時に親切な :class:`ImportError` を投げる
    (import gate)。

    **データモデル** (key namespace 規約; ``namespace`` 既定 ``"loop_agent"``):

    - ``{ns}:wake:{id}``        : wake 1 件の hash (kind/recipient/run_id/payload/state/owner/
      lease_expiry/seq)。
    - ``{ns}:recipient:{r}``    : 宛先 ``r`` の sorted set (score=seq, member=id)。claim/pending の
      **seq 順** 走査に使う。確定/配送済みになった id はここから除かれる。
    - ``{ns}:claimed``          : CLAIMED 行の sorted set (score=lease_expiry, member=id)。
      :meth:`release_expired` が ``ZRANGEBYSCORE -inf now`` で失効分だけ効率的に掃ける。
    - ``{ns}:seq``              : 単調増加カウンタ (``INCR``)。
    - ``{ns}:recipients``       : 既知 recipient の set (``pending(None)`` の全走査用)。
    - ``{ns}:lock``             : 状態変更を直列化する分散ロック (``SET NX PX``)。

    **原子性**: 状態を変える各操作はプロセス内 :class:`threading.RLock` (プロセス内直列化) と
    Redis 分散ロック ``{ns}:lock`` (``SET NX PX`` + token 照合 **atomic** 解放) で囲み、複数
    プロセスの check-and-set を直列化する (claim の二重取得を防ぐ)。ロックは ``lock_ttl`` 秒で
    自動失効するので、ロック保持者がクラッシュしても deadlock しない。解放は server-side Lua の
    compare-and-delete (:meth:`_release_lock`) で **自 token のときだけ** 行い、失効後に別者が
    握り直したロックを誤って消さない。

    **既知の制限 (cross-process の強さ)**: 分散ロックは ``lock_ttl`` の TTL ロックなので、1 操作が
    ``lock_ttl`` を **超過** すると (STW-GC / ネットワーク遅延 / 巨大 recipient zset の sweep 等)
    ロックが操作の途中で失効し、別プロセスが同じ wake を二重 claim しうる。この窓では配送は
    **at-least-once に縮退** する (確定済みが再 CLAIMED に戻る resurrection を含む)。本 transport の
    契約は元々 at-least-once + 冪等 handler (受信側が :attr:`Wake.id` で de-dup) なので回復可能だが、
    ``lock_ttl`` は 1 操作が確実に収まる十分大きい値にすること。**厳密な at-most-once が要る
    cross-process 構成では TTL ロックに依存しない** :class:`SqliteWakeQueue` (``BEGIN IMMEDIATE``
    は操作途中で失効しない) を推奨する。:class:`InMemoryWakeQueue` / :class:`SqliteWakeQueue` と
    「等価」なのは **lock_ttl 内に各操作が収まる前提** での話である。

    **TTL / cleanup**: 確定 (DELIVERED) 時に wake hash へ ``EXPIRE`` を張り (``delivered_ttl``
    秒、既定 1 日)、long-running ループでの残留を自動回収する。``delivered_ttl=None`` で無効化。
    recipient の pending が空になると ``{ns}:recipients`` registry からも除き、registry の無制限
    増殖を防ぐ (高 cardinality な peer id を宛先にしても leak しない)。

    テストや DI のため ``client`` に redis-py 互換クライアントを直接注入できる。省略時は ``url``
    から ``redis.Redis.from_url`` で生成する (どちらも無い場合は ``ValueError``)。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        url: Optional[str] = None,
        namespace: str = "loop_agent",
        delivered_ttl: Optional[float] = 86400.0,
        lock_ttl: float = 10.0,
        lock_timeout: float = 10.0,
    ) -> None:
        if client is None:
            redis = _import_redis()
            if url is None:
                raise ValueError("RedisWakeQueue: provide either `client` or `url`")
            client = redis.Redis.from_url(url)
        self._r = client
        self._ns = namespace
        self._delivered_ttl = delivered_ttl
        self._lock_ttl = lock_ttl
        self._lock_timeout = lock_timeout
        self._lock = threading.RLock()
        self._k_seq = f"{namespace}:seq"
        self._k_claimed = f"{namespace}:claimed"
        self._k_recipients = f"{namespace}:recipients"
        self._k_lock = f"{namespace}:lock"

    def _k_wake(self, wake_id: str) -> str:
        return f"{self._ns}:wake:{wake_id}"

    def _k_recipient(self, recipient: str) -> str:
        return f"{self._ns}:recipient:{recipient}"

    # 自分が握っているときだけロックを解放する Lua (compare-and-delete; 1 命令で atomic)。
    _RELEASE_LOCK_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end"
    )

    def _release_lock(self, token: str) -> None:
        """ロックを **自 token のときだけ** atomic に解放する (他者のロックを消さない)。

        check-then-delete を 2 往復でやると GET と DELETE の間にロックが失効して別プロセスが
        握り直したロックを誤って消しうる。server-side Lua の compare-and-delete で 1 命令に畳む。
        ``eval`` 非対応の client では best-effort な check-then-delete に退避する。
        """
        try:
            self._r.eval(self._RELEASE_LOCK_LUA, 1, self._k_lock, token)
        except Exception:  # noqa: BLE001 - eval 非対応 client は best-effort 解放に退避。
            if _text(self._r.get(self._k_lock)) == token:
                self._r.delete(self._k_lock)

    @contextmanager
    def _dlock(self) -> Iterator[None]:
        """プロセス内 RLock + Redis 分散ロックで状態変更を直列化する。"""
        import time as _time

        with self._lock:
            token = uuid.uuid4().hex
            start = _time.monotonic()
            while True:
                if self._r.set(self._k_lock, token, nx=True, px=int(self._lock_ttl * 1000)):
                    break
                if _time.monotonic() - start > self._lock_timeout:
                    raise TimeoutError(
                        f"RedisWakeQueue: could not acquire {self._k_lock} "
                        f"within {self._lock_timeout}s"
                    )
                _time.sleep(0.01)
            try:
                yield
            finally:
                self._release_lock(token)

    def _hgetall(self, key: str) -> dict[str, str]:
        raw = self._r.hgetall(key)
        return {_text(k): _text(v) for k, v in raw.items()}

    def _terminalize(self, wake_id: str, recipient: str) -> None:
        """wake を DELIVERED (terminal) にし、ordering/claimed index から外す (+TTL)。"""
        wkey = self._k_wake(wake_id)
        rkey = self._k_recipient(recipient)
        self._r.hset(wkey, mapping={"state": DELIVERED, "owner": ""})
        self._r.zrem(rkey, wake_id)
        self._r.zrem(self._k_claimed, wake_id)
        # recipient の pending が尽きたら registry から外す ({ns}:recipients の無制限増殖を防ぐ。
        # 再 enqueue 時に sadd で復活するので pending(None) の全走査は壊れない)。
        if self._r.zcard(rkey) == 0:
            self._r.srem(self._k_recipients, recipient)
        if self._delivered_ttl is not None:
            self._r.expire(wkey, int(self._delivered_ttl))

    def enqueue(self, wake: Wake) -> bool:
        if not wake.id:
            raise ValueError("enqueue: Wake.id must be a non-empty string")
        payload = _dumps_payload(wake.payload)
        wkey = self._k_wake(wake.id)
        with self._dlock():
            if self._r.exists(wkey):
                return False  # 同一 id は no-op (de-dup の土台)。
            seq = int(self._r.incr(self._k_seq))
            self._r.hset(
                wkey,
                mapping={
                    "id": wake.id,
                    "kind": wake.kind,
                    "recipient": wake.recipient,
                    "run_id": wake.run_id,
                    "payload": payload,
                    "state": UNDELIVERED,
                    "owner": "",
                    "lease_expiry": "0",
                    "seq": str(seq),
                },
            )
            self._r.zadd(self._k_recipient(wake.recipient), {wake.id: seq})
            self._r.sadd(self._k_recipients, wake.recipient)
            return True

    def _release_expired_locked(self, now: float) -> int:
        expired = self._r.zrangebyscore(self._k_claimed, "-inf", now)
        count = 0
        for raw in expired:
            wid = _text(raw)
            self._r.hset(self._k_wake(wid), mapping={"state": UNDELIVERED, "owner": ""})
            self._r.zrem(self._k_claimed, wid)
            count += 1
        return count

    def release_expired(self, *, now: float) -> int:
        with self._dlock():
            return self._release_expired_locked(now)

    def claim(
        self,
        recipient: str,
        *,
        now: float,
        lease: float,
        owner: str,
        limit: Optional[int] = None,
    ) -> list[Wake]:
        if lease <= 0:
            raise ValueError("claim: lease must be > 0")
        with self._dlock():
            self._release_expired_locked(now)
            ids = [_text(x) for x in self._r.zrange(self._k_recipient(recipient), 0, -1)]
            out: list[Wake] = []
            new_expiry = now + lease
            for wid in ids:
                if limit is not None and len(out) >= limit:
                    break
                h = self._hgetall(self._k_wake(wid))
                if not h:
                    # hash が TTL 等で消えた stale な index member。掃除して飛ばす。
                    self._r.zrem(self._k_recipient(recipient), wid)
                    continue
                if h["state"] != UNDELIVERED:
                    continue
                self._r.hset(
                    self._k_wake(wid),
                    mapping={"state": CLAIMED, "owner": owner, "lease_expiry": repr(new_expiry)},
                )
                self._r.zadd(self._k_claimed, {wid: new_expiry})
                out.append(
                    _make_wake(h["id"], h["kind"], h["recipient"], h["run_id"], h["payload"])
                )
            return out

    def confirm(self, wake_id: str, *, owner: str, now: float) -> bool:
        with self._dlock():
            h = self._hgetall(self._k_wake(wake_id))
            if not h:
                return False
            if h["state"] != CLAIMED:
                return False
            if h["owner"] != owner:
                return False
            if float(h["lease_expiry"]) <= now:
                return False
            self._terminalize(wake_id, h["recipient"])
            return True

    def mark_delivered(self, wake_id: str) -> bool:
        with self._dlock():
            h = self._hgetall(self._k_wake(wake_id))
            if not h:
                return False
            if h["state"] != UNDELIVERED:
                return False
            self._terminalize(wake_id, h["recipient"])
            return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        with self._lock:
            if recipient is not None:
                recipients = [recipient]
            else:
                recipients = sorted(_text(x) for x in self._r.smembers(self._k_recipients))
            items: list[tuple[float, Wake]] = []
            for rcp in recipients:
                for raw, score in self._r.zrange(
                    self._k_recipient(rcp), 0, -1, withscores=True
                ):
                    wid = _text(raw)
                    h = self._hgetall(self._k_wake(wid))
                    if not h or h["state"] == DELIVERED:
                        continue
                    items.append(
                        (
                            float(score),
                            _make_wake(
                                h["id"], h["kind"], h["recipient"], h["run_id"], h["payload"]
                            ),
                        )
                    )
            items.sort(key=lambda t: t[0])  # 全 recipient 横断で seq (= score) 順に整列。
            return [w for _, w in items]

    def state_of(self, wake_id: str) -> Optional[str]:
        with self._lock:
            st = self._r.hget(self._k_wake(wake_id), "state")
            return _text(st) if st is not None else None


def open_wake_queue(backend: str = "memory", **opts: Any) -> WakeQueue:
    """backend 名から :class:`WakeQueue` を生成する便宜ファクトリ。

    - ``"memory"`` (既定) : :class:`InMemoryWakeQueue` (単一プロセス内)。
    - ``"sqlite"``        : :class:`SqliteWakeQueue` (``path`` / ``table`` 等を ``opts`` で)。
    - ``"redis"``         : :class:`RedisWakeQueue` (``client`` か ``url`` を ``opts`` で)。

    生成した queue を :class:`Transport` に渡せば、Public API を変えずに backend を選べる
    (in-memory が既定、明示で SQLite/Redis)。
    """
    if backend == "memory":
        if opts:
            raise ValueError(f"open_wake_queue('memory') takes no options, got {sorted(opts)}")
        return InMemoryWakeQueue()
    if backend == "sqlite":
        return SqliteWakeQueue(**opts)
    if backend == "redis":
        return RedisWakeQueue(**opts)
    raise ValueError(f"unknown backend {backend!r} (expected 'memory' / 'sqlite' / 'redis')")


__all__ = [
    # wake 種別
    "WAKE_LOOP_DONE",
    "WAKE_NEXT_ITERATION",
    "WAKE_DECISION_REQUEST",
    "WAKE_KINDS",
    # 配送状態
    "UNDELIVERED",
    "CLAIMED",
    "DELIVERED",
    # 型
    "Wake",
    "PushBackend",
    "CallablePushBackend",
    "NullPushBackend",
    "WakeQueue",
    "InMemoryWakeQueue",
    "SqliteWakeQueue",
    "RedisWakeQueue",
    "open_wake_queue",
    "Transport",
    # role 別 cadence
    "CADENCE_SECONDS",
    "DEFAULT_CADENCE_SECONDS",
    "cadence_for",
    "due_to_poll",
]
