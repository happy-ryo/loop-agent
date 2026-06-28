"""multi-item ループの公平 scheduling gather (Issue #56).

``run_loop`` の ``gather`` フックは ``Callable[[LoopState], ctx]`` -- 「次に何をやるか」を
state から選ぶ 1 点 (report.md S4.4)。N ファイル / N bug を 1 本のループで回すとき、素朴な
``gather`` (「先頭の未完 item を返す」) は **1 item が ``MaxIterations`` を独占して他を
starve させる**: 失敗を繰り返す 1 件が全反復を食い、残りに一度も触れずにループが終わる。

#37 (Self-translation PoC) は手書きの round-robin gather でこれを回避した::

    def gather(state):
        rem = [f for f in files if f not in done]
        return min(rem, key=lambda f: (attempts[f], files.index(f)))   # 公平 scheduling

:class:`WorkListGather` はこの pattern を再利用可能に正規化する。提供するもの:

- **公平 scheduling 戦略** (``round_robin`` / ``fewest_attempts`` / ``fifo`` / ``priority`` /
  任意の custom callable) -- どの item に次の 1 反復を割り当てるか。
- **per-item 上限** (``max_attempts_per_item``) -- 1 item が独占しないよう、規定回数試して
  完了しなければその item を *exhausted* として外す (グローバルな ``MaxIterations`` とは独立)。
- **done 判定フック** (``done_when``) -- verify (ループ全体のゴール) とは独立に、「*この item* は
  終わったか」を user policy で判定する。
- **attempt counter の正規 API** -- :meth:`WorkListGather.attempts` /
  :meth:`~WorkListGather.report` 等で進捗を読む。
- **triage との接続** (:meth:`WorkListGather.from_triage`) -- work-list の優先度・順序計算を
  既存の :func:`loop_agent.discovery.triage` に委譲する。

**resume 安全 (state から導出)**: :class:`WorkListGather` は in-process カウンタを *持たない*。
attempts / done / exhausted は毎回 ``state.history`` を決定的に *リプレイ* して導出する
(scheduling 戦略が ``(attempts, done, exhausted, last_selected)`` の純関数なので、各反復で
自分が何を dispatch したかを再現できる)。よって別プロセス / resume 後に同じ ``LoopState`` で
呼べば同じ判断になる -- README「判定を gather された state から導けば、新プロセスでも同じ判断を
する」(resume #14) の方針そのまま。``done_when`` は ``StepRecord`` を読むので、JSON 往復で
ドリフトしないフィールド (``goal_met`` / JSON ネイティブな ``observation``) を見ること
(loop.py の resume fidelity 注記と同じ約束)。

**drained とループ停止**: 全 item が done か exhausted になると gather は返す item が無い
(:data:`DRAINED` を返す)。ループを止めるのは gather ではなく停止条件なので、必ず
:class:`WorkListDrained` を ``conditions`` に composeすること -- 停止条件は各反復の *先頭*
(gather の前) で評価されるため、drained になった時点で gather が呼ばれる前にループが止まる
(:data:`DRAINED` が ``act`` に渡ることはない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Mapping, Optional, Sequence, Union

from ..errors import ConfigError
from ..state import LoopState, StepRecord
from ._triage import triage


@dataclass(frozen=True)
class WorkItem:
    """scheduling 対象の 1 件 (ファイル / bug / タスク)。

    Args:
        id: 安定識別子 (非空・work-list 内で一意)。attempts / done の集計キー。
        priority: ``priority`` 戦略で使う優先度。**大きいほど優先**。既定 0。
        payload: 採択時に ``act`` へ渡したい任意値 (ファイルパス・タスク本文・seed 等)。
            既定の ``build_ctx`` は JSON ネイティブ dict ``{"id", "attempt", "priority",
            "payload"}`` を ``act`` の context にするので、``act`` 側で ``ctx["payload"]`` /
            ``ctx["id"]`` を読める。``payload`` 自体も (永続ゲートと合成するなら) JSON ネイティブに。
    """

    id: str
    priority: int = 0
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ConfigError("WorkItem.id must be a non-empty string")


@dataclass(frozen=True)
class ScheduleContext:
    """scheduling 戦略に渡す read-only ビュー (この反復で選べる item と集計値)。

    custom callable 戦略はこれを受け取り、``selectable`` の中から 1 件 (:class:`WorkItem`
    または その ``id``) を返す。``selectable`` 外を返すと :class:`WorkListGather` が
    ``ConfigError`` で fail loud する (誤って done / exhausted を再選択しないため)。

    **``attempts`` と ``selections`` の違い**: ``attempts[id]`` は *実行された* 試行回数
    (per-item 上限 / done 判定 / ModelLadder 用)。``selections[id]`` はその item が *選ばれた*
    (offer された) 回数で、``item_of`` が非実行 (``None``) とした offer (gate SKIP 等) も含む。
    **公平性は ``selections`` で測る** -- skip された item も「一度 offer した」分だけ後ろへ回し、
    同じ item を無限に再提示して他を starve させないため。``item_of`` を使わなければ両者は一致する。
    """

    selectable: tuple[WorkItem, ...]
    attempts: Mapping[str, int]
    selections: Mapping[str, int]
    position: Mapping[str, int]
    last_selected: Optional[str]
    done: frozenset[str]
    exhausted: frozenset[str]


# scheduling 戦略: ``ScheduleContext`` から次に回す item を選ぶ純関数。
Scheduler = Callable[[ScheduleContext], Union[WorkItem, str]]

# item を ``act`` の context へ変換するフック (item, この item の既試行回数, state)。
ContextBuilder = Callable[[WorkItem, int, LoopState], Any]

# 「*この item* は終わったか」を判定する user policy (verify とは独立)。
DonePredicate = Callable[[WorkItem, StepRecord], bool]

# その record が「実際にどの item を act した結果か」を返す (``None`` = 非実行 / 帰属なし)。
# 既定 (``None`` フック) は schedule リプレイで「選んだ item == act した item」と見なす。
ItemAttributor = Callable[[StepRecord], Optional[str]]


def _strat_fewest_attempts(ctx: ScheduleContext) -> WorkItem:
    """選択回数最小 -> 元の並び順。#37 PoC と同じ公平戦略 (round-robin 相当)。

    公平性は ``selections`` (offer 回数) で測る。``item_of`` を使わなければ
    ``selections == attempts`` なので「試行回数最小」と一致する。gate SKIP 等で非実行とした
    offer も後ろへ回るので、skip され続ける item が他を starve させない。
    """
    return min(
        ctx.selectable, key=lambda it: (ctx.selections[it.id], ctx.position[it.id])
    )


def _strat_fifo(ctx: ScheduleContext) -> WorkItem:
    """元の並び順で最初の未完 item (素朴な戦略; per-item 上限と併用で starve を緩和)。

    公平性カウンタを持たない素朴版なので、``item_of`` で非実行とした offer に対しては
    rotate しない (skip された先頭 item を offer し続ける)。gate と合成して skip を非実行に
    する場合は ``fewest_attempts`` / ``round_robin`` を使うこと。
    """
    return min(ctx.selectable, key=lambda it: ctx.position[it.id])


def _strat_priority(ctx: ScheduleContext) -> WorkItem:
    """優先度降順 -> 選択回数昇順 -> 並び順。優先度を尊重しつつ同優先度内は公平。

    同優先度内の公平性は ``selections`` (offer 回数) で測る (``_strat_fewest_attempts`` と同様)。
    """
    return min(
        ctx.selectable,
        key=lambda it: (-it.priority, ctx.selections[it.id], ctx.position[it.id]),
    )


def _strat_round_robin(ctx: ScheduleContext) -> WorkItem:
    """位置順で last_selected の *次* の selectable へ循環 (古典的 round-robin)。

    ``fewest_attempts`` が「試行回数」で公平を測るのに対し、こちらは並び順で厳密に巡回する
    (同じ item を連続 dispatch しない)。last_selected が done / exhausted で selectable から
    外れていても、その *位置* を巡回の基準にする (位置は不変なので決定的)。
    """
    if ctx.last_selected is None:
        return min(ctx.selectable, key=lambda it: ctx.position[it.id])
    last_pos = ctx.position[ctx.last_selected]
    after = [it for it in ctx.selectable if ctx.position[it.id] > last_pos]
    pool = after if after else ctx.selectable
    return min(pool, key=lambda it: ctx.position[it.id])


_BUILTIN_STRATEGIES: dict[str, Scheduler] = {
    "round_robin": _strat_round_robin,
    "fewest_attempts": _strat_fewest_attempts,
    "fifo": _strat_fifo,
    "priority": _strat_priority,
}


class Drained:
    """:meth:`WorkListGather.__call__` が「回す item が無い」ことを示す sentinel 型。

    全 item が done / exhausted のとき gather が返す。:data:`DRAINED` (唯一のインスタンス)
    で参照する。ループ停止は :class:`WorkListDrained` 停止条件が担うので、正しく compose
    していればこの値が ``act`` に渡ることはない (停止条件が gather より先に評価される)。
    """

    _singleton: "Optional[Drained]" = None

    def __new__(cls) -> "Drained":
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __repr__(self) -> str:
        return "<work-list-drained>"

    def __bool__(self) -> bool:
        return False


DRAINED = Drained()


@dataclass(frozen=True)
class WorkListProgress:
    """work-list の進捗スナップショット (:meth:`WorkListGather.report` が返す)。

    ``done`` / ``exhausted`` / ``remaining`` は item id のタプル (元の並び順)。``attempts`` は
    id -> 既試行回数。``drained`` は「回す item がもう無い」 (= ``remaining`` が空)。
    """

    total: int
    done: tuple[str, ...]
    exhausted: tuple[str, ...]
    remaining: tuple[str, ...]
    attempts: Mapping[str, int]

    @property
    def drained(self) -> bool:
        """回す item がもう無いか (全件 done か exhausted)。"""
        return not self.remaining


@dataclass(frozen=True)
class _Derivation:
    """``state.history`` リプレイの結果 (内部)。"""

    attempts: dict[str, int]
    selections: dict[str, int]
    done: set[str]
    exhausted: set[str]
    last_selected: Optional[str]
    selectable: tuple[WorkItem, ...]


def _default_done(item: WorkItem, record: StepRecord) -> bool:
    """既定の done 判定: その反復で verify がゴール到達を報告したか。

    単一ゴールのループ向けの素直な既定。真の multi-item では「*この item* は終わったか」を
    別シグナルで判定したいことが多いので、``done_when`` で上書きする (record.observation /
    record.detail に item ごとの完了シグナルを載せておく)。
    """
    return bool(record.goal_met)


def _default_build_ctx(item: WorkItem, attempt: int, state: LoopState) -> dict[str, Any]:
    """既定の context: JSON ネイティブな dict ``{"id", "attempt", "priority", "payload"}``。

    ``act`` は ``ctx["id"]`` / ``ctx["payload"]`` / ``ctx["attempt"]`` で読む。**JSON ネイティブ
    にしてあるのは意図的**: 永続人間ゲート (:class:`~loop_agent.gate.HumanGate` /
    :func:`~loop_agent.gate.run_gated_loop`) と合成したとき、gate が pause すると context が
    提案 action として state.db に保存される (``request_decision`` は JSON ネイティブを要求する)。
    :class:`WorkItem` 自身を返すと round-trip できず ``ConfigError`` になるため、dict を既定に
    する (``payload`` が JSON ネイティブな限り安全)。``WorkItem`` をそのまま欲しい等は ``build_ctx``
    で上書きする。
    """
    return {
        "id": item.id,
        "attempt": attempt,
        "priority": item.priority,
        "payload": item.payload,
    }


class WorkListGather:
    """複数 item を 1 本のループで公平に回す ``gather`` フック (Issue #56)。

    ``run_loop(gather=WorkListGather(items, ...), ...)`` のように ``gather`` に渡す
    (``__call__(state) -> ctx`` が ``GatherHook`` に適合)。各反復で:

    1. ``state.history`` を決定的にリプレイし、各 item の attempts / done / exhausted を導出する。
    2. scheduling 戦略で次に回す item を 1 件選ぶ (selectable = 未 done かつ未 exhausted)。
    3. ``build_ctx(item, attempt, state)`` を ``act`` の context として返す。
    4. selectable が空なら :data:`DRAINED` を返す (停止は :class:`WorkListDrained` が担う)。

    Args:
        items: 回す :class:`WorkItem` 群 (id は一意)。空なら常に drained。:class:`WorkItem`
            でなく素の文字列を渡しても良い (``id`` として ``WorkItem`` に昇格する)。
        strategy: ``"round_robin"`` / ``"fewest_attempts"`` (既定) / ``"fifo"`` /
            ``"priority"``、または ``ScheduleContext -> WorkItem|id`` の custom callable。
        max_attempts_per_item: per-item 上限。``None`` (既定) なら無制限 (グローバルな
            ``MaxIterations`` のみで bound)。``>= 1``。規定回数試して done にならない item は
            *exhausted* として selectable から外れる -- 1 item が ``MaxIterations`` を独占して
            他を starve させない核 (#37 で素朴 gather がこれで詰んだ)。
        done_when: 「*この item* は終わったか」を ``(item, record) -> bool`` で判定する
            user policy (verify とは独立)。既定は ``record.goal_met``。一度 done になった item は
            再 dispatch されない (sticky)。
        build_ctx: 選んだ item を ``act`` の context へ変換する ``(item, attempt, state) -> ctx``。
            ``attempt`` はこの dispatch *前* の既試行回数 (0 始まり) -- ModelLadder と合成して
            試行回数でモデルを上げる、等に使える。既定は JSON ネイティブ dict ``{"id", "attempt",
            "priority", "payload"}`` を返す (永続人間ゲートと合成しても state.db に保存できるよう
            JSON ネイティブにしてある)。
        item_of: history の各 record が「*実際に* どの item を ``act`` した結果か」を返す
            ``(record) -> item_id | None`` フック。既定 ``None`` は schedule リプレイで
            「offer した item == act した item」と見なす (gate 無しの標準 1:1 ループでは正しい)。
            ``gate`` を合成して offer と record がずれる構成では渡す:
            ``GATE_SKIP`` (reject/respond) は ``act`` せず record だけ積むので ``None`` を返して
            非実行にする; ``edit`` で別 item に差し替えると record はその item のものなので、
            record (例: ``observation`` に焼いた item id) から実 item を返す。``None`` / work-list
            外の id は実行として数えず attempts / done / per-item 上限を更新しない -- 走っていない
            item を誤って *exhausted* にしたり、別 item の record を取り違えたりしないため (#56
            review)。**公平性 (offer 回数) は schedule で測る** ので、``item_of`` が ``None`` を
            返す skip でも offer は前進し、他 item へ rotate して starve を防ぐ。

    Raises:
        ConfigError: item id が重複 / ``strategy`` が未知の文字列 / ``max_attempts_per_item < 1``。
    """

    def __init__(
        self,
        items: Sequence[Union[WorkItem, str]],
        *,
        strategy: Union[str, Scheduler] = "fewest_attempts",
        max_attempts_per_item: Optional[int] = None,
        done_when: DonePredicate = _default_done,
        build_ctx: ContextBuilder = _default_build_ctx,
        item_of: Optional[ItemAttributor] = None,
    ) -> None:
        normalized: list[WorkItem] = [
            it if isinstance(it, WorkItem) else WorkItem(id=it) for it in items
        ]
        by_id: dict[str, WorkItem] = {}
        for it in normalized:
            if it.id in by_id:
                raise ConfigError(f"duplicate WorkItem id {it.id!r}; ids must be unique")
            by_id[it.id] = it
        self._items: tuple[WorkItem, ...] = tuple(normalized)
        self._by_id = by_id
        self._position = {it.id: i for i, it in enumerate(self._items)}

        if isinstance(strategy, str):
            if strategy not in _BUILTIN_STRATEGIES:
                raise ConfigError(
                    f"unknown strategy {strategy!r}; "
                    f"expected one of {sorted(_BUILTIN_STRATEGIES)} or a callable"
                )
            self._strategy: Scheduler = _BUILTIN_STRATEGIES[strategy]
            self.strategy_name = strategy
        else:
            self._strategy = strategy
            self.strategy_name = getattr(strategy, "__name__", "custom")

        if max_attempts_per_item is not None and max_attempts_per_item < 1:
            raise ConfigError("max_attempts_per_item must be >= 1 or None")
        self._max = max_attempts_per_item
        self._done_when = done_when
        self._build_ctx = build_ctx
        self._item_of = item_of

    @property
    def items(self) -> tuple[WorkItem, ...]:
        """登録された work item 群 (元の並び順)。"""
        return self._items

    # -- scheduling 内部 -----------------------------------------------------

    def _selectable(
        self, done: set[str], exhausted: set[str]
    ) -> tuple[WorkItem, ...]:
        """未 done かつ未 exhausted の item を並び順で。"""
        return tuple(
            it for it in self._items if it.id not in done and it.id not in exhausted
        )

    def _select_id(
        self,
        attempts: dict[str, int],
        selections: dict[str, int],
        done: set[str],
        exhausted: set[str],
        last_selected: Optional[str],
    ) -> Optional[str]:
        """戦略に 1 件選ばせて id を返す (selectable が空なら ``None``)。"""
        selectable = self._selectable(done, exhausted)
        if not selectable:
            return None
        ctx = ScheduleContext(
            selectable=selectable,
            attempts=attempts,
            selections=selections,
            position=self._position,
            last_selected=last_selected,
            done=frozenset(done),
            exhausted=frozenset(exhausted),
        )
        chosen = self._strategy(ctx)
        chosen_id = chosen.id if isinstance(chosen, WorkItem) else chosen
        if chosen_id not in {it.id for it in selectable}:
            raise ConfigError(
                f"strategy {self.strategy_name!r} selected {chosen_id!r}, "
                f"which is not selectable (done/exhausted/unknown); "
                f"selectable={[it.id for it in selectable]}"
            )
        return chosen_id

    def _derive(self, state: LoopState) -> _Derivation:
        """``state.history`` を決定的にリプレイし attempts / done / exhausted を導出する。

        各 history record は「直前の反復で本 gatherer が dispatch した item を act した結果」
        と見なす (戦略が決定的なので step k の選択を再現できる)。これにより in-process カウンタ
        無しで resume 安全になる。``done`` / ``exhausted`` は sticky (一度立つと再選択しない)。

        **前提条件 (resume 時の正しさ)**: 帰属は ``state.history`` を *現在の* ``items`` /
        ``strategy`` / ``max_attempts_per_item`` / ``done_when`` でリプレイして導出する。
        ``StepRecord`` は「どの item を dispatch したか」を構造的に持たない (戦略から再導出する)
        ので、**history を生成した時の設定と現在の設定が一致している場合にだけ正しい**。設定が
        違う gatherer に過去の history を食わせると、step k の記録を別の item に黙って誤帰属する
        (crash しない)。よって resume は「中断した *同一* gatherer を ``initial_state`` で再開」
        に限る。:meth:`from_triage` で ready 集合が変わった新 gatherer を作る場合は、items の
        並び・構成が変わるので **過去の history を引き継がず、新しい ``LoopState`` で開始する**
        こと (triage が done 済みを除外し、新規 ready は試行 0 から始まるのが正しい挙動)。

        **offer と 帰属の分離**: ``selections`` (offer 回数) は schedule から決まり公平性を
        駆動する。attempts / done / exhausted は record の *帰属先* item に付く。既定はこの二つを
        同一視する (offer == act の 1:1 ループ)。gate が間に入って両者がずれる構成では:

        - ``GATE_SKIP`` (reject/respond) -- ``act`` せず record を積む。``item_of`` が ``None`` を
          返せば非実行として attempts に数えない (offer は前進するので公平に rotate する)。
        - ``edit`` -- 別 item の context に差し替えて ``act`` する。record はその item のものなので、
          ``item_of`` で record から実 item を読めば正しい item に帰属する (offer した元 item は
          実行ゼロのまま)。``item_of`` を渡さないと record を offer した item に誤帰属する。

        公平性は ``selections`` で測るので、skip された item も後ろへ回り ``fewest_attempts`` /
        ``priority`` / ``round_robin`` は同じ item を無限に再提示しない (``fifo`` のみ素朴ゆえ
        非 rotate)。標準の ``run_loop`` (gate 無し / skip も edit もしない gate) では offer と
        record が 1:1・``selections == attempts`` なので ``item_of`` は不要。
        """
        attempts: dict[str, int] = {it.id: 0 for it in self._items}
        selections: dict[str, int] = {it.id: 0 for it in self._items}
        done: set[str] = set()
        exhausted: set[str] = set()
        last_selected: Optional[str] = None

        for record in state.history:
            # sel = gather が *offer* した item (schedule から)。selectable が尽きた後も
            # history が続く場合 (本 gatherer 由来でない step / 全 drained 後の余剰) は帰属しない。
            sel = self._select_id(attempts, selections, done, exhausted, last_selected)
            if sel is None:
                break
            # offer は必ず前進させる -- 公平性 (selections) と round_robin の rotation 基準。
            # 非実行 (skip) でも前進させるので、skip され続ける item が他を starve させない。
            selections[sel] += 1
            last_selected = sel
            # 帰属 = この record が *実際に act した* item。既定 (item_of=None) は「offer した
            # item == act した item」(標準の 1:1 ループ)。gate が SKIP (item 無し) / EDIT
            # (別 item へ差し替え) する構成では offer と record がずれるので、item_of で record
            # から実 item を読む (None=非実行)。これで attempts/done/exhausted を正しい item に
            # 付け、走っていない item を上限で誤 exhausted にしない (#56 review)。
            actual = sel if self._item_of is None else self._item_of(record)
            if actual is None or actual not in self._by_id:
                # 非実行 (skip) か、本 work-list 外の id への edit -- 実行として数えない。
                continue
            attempts[actual] += 1
            if self._done_when(self._by_id[actual], record):
                done.add(actual)
            elif self._max is not None and attempts[actual] >= self._max:
                exhausted.add(actual)

        selectable = self._selectable(done, exhausted)
        return _Derivation(
            attempts, selections, done, exhausted, last_selected, selectable
        )

    # -- gather フック本体 ---------------------------------------------------

    def __call__(self, state: LoopState) -> Any:
        """``GatherHook`` 本体: 次に回す item の context を返す (drained なら :data:`DRAINED`)。"""
        d = self._derive(state)
        if not d.selectable:
            return DRAINED
        sel = self._select_id(
            d.attempts, d.selections, d.done, d.exhausted, d.last_selected
        )
        assert sel is not None  # selectable が非空なので必ず選べる
        item = self._by_id[sel]
        return self._build_ctx(item, d.attempts[sel], state)

    # -- attempt counter / 進捗の正規 API ------------------------------------

    def attempts(self, state: LoopState) -> dict[str, int]:
        """各 item の既試行回数 (id -> count) を state から導出して返す。"""
        return dict(self._derive(state).attempts)

    def done_items(self, state: LoopState) -> set[str]:
        """``done_when`` で完了と判定された item id 集合。"""
        return set(self._derive(state).done)

    def exhausted_items(self, state: LoopState) -> set[str]:
        """per-item 上限に達したが未完了の item id 集合 (starve 防止で外された側)。"""
        return set(self._derive(state).exhausted)

    def remaining(self, state: LoopState) -> tuple[WorkItem, ...]:
        """まだ回せる item (未 done かつ未 exhausted) を並び順で。"""
        return self._derive(state).selectable

    def drained(self, state: LoopState) -> bool:
        """回す item がもう無いか (全件 done か exhausted)。"""
        return not self._derive(state).selectable

    def report(self, state: LoopState) -> WorkListProgress:
        """進捗スナップショット (:class:`WorkListProgress`) を 1 回の導出で返す。"""
        d = self._derive(state)
        return WorkListProgress(
            total=len(self._items),
            done=tuple(it.id for it in self._items if it.id in d.done),
            exhausted=tuple(it.id for it in self._items if it.id in d.exhausted),
            remaining=tuple(it.id for it in d.selectable),
            attempts=dict(d.attempts),
        )

    # -- triage との接続 -----------------------------------------------------

    @classmethod
    def from_triage(
        cls,
        candidates,
        *,
        done: Sequence[str] = (),
        strategy: Union[str, Scheduler] = "fewest_attempts",
        **kwargs: Any,
    ) -> "WorkListGather":
        """優先度・順序計算を :func:`loop_agent.discovery.triage` に委譲して構築する。

        ``candidates`` (:class:`~loop_agent.discovery.Candidate` 群) を ``triage`` にかけ、
        *ready* (依存が満たされた) 候補だけを triage のランキング順 (優先度降順 -> 工数昇順 ->
        id) で :class:`WorkItem` に写す (``priority`` / ``payload`` を引き継ぐ)。*blocked*
        (依存未充足) 候補はまだ回せないので除外する。

        これで責務が綺麗に分かれる: **何を どの順で回す価値があるか** は triage (依存解決 +
        優先度)、**それらを どう公平に回すか** は :class:`WorkListGather` (scheduling +
        per-item 上限)。依存が解けて新たに ready になった候補を取り込むには、その時点の
        ``done`` で ``from_triage`` を呼び直して新しい gatherer を作る -- このとき items の構成が
        変わるので、過去の ``state.history`` は引き継がず **新しい ``LoopState`` でループを開始
        する** (:meth:`_derive` の前提条件を参照: 設定の違う history を食わせると誤帰属する)。

        Args:
            candidates: triage する候補群。
            done: triage 時点で完了済みの id (依存充足判定に使う)。
            strategy: scheduling 戦略 (既定 ``"fewest_attempts"``)。triage が順序を決めるので
                ``"fifo"`` でも triage ランキング順に回る。
            **kwargs: :class:`WorkListGather` の他引数 (``max_attempts_per_item`` /
                ``done_when`` / ``build_ctx``)。
        """
        result = triage(candidates, done=done)
        items = tuple(
            WorkItem(id=c.id, priority=c.priority, payload=c.payload)
            for c in result.ready
        )
        return cls(items, strategy=strategy, **kwargs)


class WorkListDrained:
    """:class:`WorkListGather` が drained (全件 done/exhausted) になったら止める停止条件。

    ``StopCondition`` プロトコル (``check(state) -> reason|None`` + ``name``) に適合し、
    ``AnyOf`` / ``run_loop(conditions=...)`` にそのまま compose できる。停止条件は各反復の
    *先頭* (gather の前) で評価されるので、drained 化した時点で gather が呼ばれる前に
    ループが止まる -- :data:`DRAINED` が ``act`` へ漏れない設計上の要。

    これは *中立* な停止 (成功でも abort でもない): 「やるべき item を回し切った / 上限まで
    試した」を意味する。個々の item の成否は :meth:`WorkListGather.report` で読む。

    Args:
        gatherer: 監視対象の :class:`WorkListGather` (同じ ``items`` 設定を共有する個体)。
    """

    name: ClassVar[str] = "work_list_drained"

    def __init__(self, gatherer: WorkListGather) -> None:
        self.gatherer = gatherer

    def check(self, state: LoopState) -> Optional[str]:
        report = self.gatherer.report(state)
        if report.drained:
            return (
                f"work list drained: {len(report.done)} done, "
                f"{len(report.exhausted)} exhausted "
                f"(of {report.total})"
            )
        return None


__all__ = [
    "WorkItem",
    "ScheduleContext",
    "Scheduler",
    "WorkListGather",
    "WorkListProgress",
    "WorkListDrained",
    "Drained",
    "DRAINED",
]
