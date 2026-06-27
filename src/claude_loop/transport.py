"""wake 配送の transport 層: push 一次 / pull fallback / at-most-once (Issue #23)。

report.md S3.3 / S4.6 / S5 Phase3。ループの **完了 / 次反復 / 判断要求** を別ループや
窓口 (受信側) に届ける wake 配送の実体をここに新設する。claude-org runtime の broker
sidecar は runtime 所属で直接再利用できない[^pattern-only]ため、**パターンだけ抽出** して
claude-loop 側に依存ゼロ (stdlib のみ) で実装する。

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

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, runtime_checkable

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
        """JSON 化しやすいフラットな dict へ畳む (sink / backend のシリアライズ用)。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "recipient": self.recipient,
            "run_id": self.run_id,
            **dict(self.payload),
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
      しない (前提: 並行 poll は distinct owner を使う。:meth:`~claude_loop.transport.Transport.poll` 参照)。
    - ``release_expired`` : lease 失効した ``CLAIMED`` を ``UNDELIVERED`` へ戻す (再 eligible)。
    - ``mark_delivered`` : push が確定配送した wake を直接 ``DELIVERED`` (terminal) にする。
      任意の非 terminal 状態から冪等に遷移できる (push と pull の継ぎ目を吸収)。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._seq = 0

    def enqueue(self, wake: Wake) -> bool:
        """wake を ``UNDELIVERED`` で登録する。同一 ``id`` が既にあれば no-op で ``False``。

        二重 enqueue を冪等にすることで、deliver の再試行や resume での再配送指示が
        既存行 (進行中の claim や DELIVERED) を壊さない (= 人間/受信側に二重に届けない土台)。
        """
        if not wake.id:
            raise ValueError("enqueue: Wake.id must be a non-empty string")
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
        """wake を直接 ``DELIVERED`` (terminal) にする (push 確定配送の確定)。

        push backend が ``True`` (確定配送) を返したときに使う。任意の非 terminal 状態から
        冪等に遷移する (既に DELIVERED なら ``False``)。push と pull の継ぎ目を吸収する:
        push が DELIVERED 化した行を pull は claim しない (UNDELIVERED でないため)。
        """
        e = self._entries.get(wake_id)
        if e is None:
            return False
        if e.state == DELIVERED:
            return False
        e.state = DELIVERED
        e.owner = None
        return True

    def pending(self, recipient: Optional[str] = None) -> list[Wake]:
        """未確定 (``UNDELIVERED`` / ``CLAIMED``) の wake を登録順に返す (任意で宛先で絞る)。"""
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

        同一 ``id`` の再 deliver は enqueue が no-op なので、既に DELIVERED な wake を push で
        二度送らない (push を試みるのは「今回新規に積まれた、または未だ未配送の」wake のみ)。
        """
        newly = self.queue.enqueue(wake)
        # 既存 wake が既に DELIVERED 済みなら push を重ねない (二重配送を避ける)。
        if not newly:
            state = _state_of(self.queue, wake.id)
            if state == DELIVERED:
                return "push"  # 既に配送確定。再送しない。
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
        confirm: bool = True,
    ) -> list[Wake]:
        """``recipient`` 宛の未配送 wake を pull で引き取る (claim-then-confirm)。

        ``UNDELIVERED`` な wake を lease 占有 (claim) して返す。``confirm=True`` (既定) なら
        返す前に即 confirm して ``DELIVERED`` 化する (この呼び出しが受信側そのもので、戻り値の
        受領 = 配送完了とみなせる単純なケース)。``confirm=False`` なら claim だけ行い、呼び出し側が
        処理し切ってから :meth:`confirm_wakes` を呼ぶ責務を負う (処理中に死んだら lease 失効で
        再配送される at-least-once 規律)。

        ``owner`` は claim の所有者識別 (省略時は ``recipient``)。同一受信者が複数 worker で
        並行 poll する場合に owner を変えると、三状態の owner fencing が二重確定を弾く。
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

    def confirm_wakes(self, wakes: Iterable[Wake], *, owner: str) -> int:
        """claim 済み wake 群を確定する (``confirm=False`` で poll したときの確定 API)。

        確定できた (この owner が lease を保持していた) 件数を返す。lease 失効後に呼ぶと
        owner/失効 fencing で弾かれ、その wake は再配送対象として queue に残る。
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
    "Transport",
    # role 別 cadence
    "CADENCE_SECONDS",
    "DEFAULT_CADENCE_SECONDS",
    "cadence_for",
    "due_to_poll",
]
