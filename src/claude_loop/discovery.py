"""work-discovery: 次反復対象の入力選定 (propose-only / 人間ゲート維持, Issue #24).

report.md S3.5 / S4.6 / S5 Phase 3 が定める **work-discovery** を実装する。完了した
ループの「次に何を反復するか」を決める入力選定ループで、**計算層（read-only・決定的）と
配達層（人間ゲート）の二層構造**により「発見の自律性は上げるが、着手判断は人間に残す」
(report.md S3.5 INV) を構造で担保する。

二層の責務分離:

- **計算層 (:func:`triage`)**: 副作用ゼロ・同一入力同一出力の純関数。候補 (:class:`Candidate`)
  群を ``done`` (完了済み id 集合) に対して triage する — 依存解決 (deps が全て done なら
  *ready*)、優先度・工数による決定的ランキング、未充足依存の理由付け、依存循環の検出。
  「N 件の候補 + 推奨 1 件」(report.md S3.5) を :class:`Triage` として返す。loop 状態に
  一切触れない (read-only)。
- **配達層 (:class:`WorkDiscovery`)**: triage 結果を **提案** として state.db の人間ゲート
  レジスタ (:class:`~claude_loop.store.LoopStore` の ``pending_decision``) に登録する。
  ここで **必ず止まる (propose-only)**: 完全自動では一切着手せず、人間が
  :meth:`~claude_loop.store.LoopStore.resolve_decision` (= MVP 限定人間ゲートと同一経路)
  で採否を決めるまで pending のまま保持する。採択された候補だけが次ループの入力になる。

**propose-only 継承** (report.md S5 Phase 3): MVP の限定人間ゲート (:mod:`claude_loop.gate`)
は「不可逆 *action* を実行前に止める」ゲートだった。本層はその人間ゲートを **入力選定** へ
読み替える — 「次反復の対象を *採択* する前に止める」。LangGraph interrupt パリティの 4 決定
(approve / edit / reject / respond) を、採択へ次のように写像する:

- ``approve`` -> 推奨候補 (recommended) を採択
- ``edit``    -> 人間が指定した別の *ready* 候補を採択 (id を payload で指定)
- ``reject``  -> 何も採択しない (次反復を起こさない)
- ``respond`` -> 何も採択せず人間の応答を記録 (次の triage 文脈に渡せる)

**完了 -> 次反復の接続** (:func:`discover_next`, report.md S5 Phase 3 成功条件 d):
直前のループ結果 (:class:`~claude_loop.loop.LoopResult`) が **完了している** ときだけ提案を
出す。``paused`` (人間ゲートで中断中) のときは「まだ何も完了していない」ので提案しない。
これにより「完了 -> (人間ゲート越しの) 次反復入力選定」の連鎖が、人間の採否決定を必ず挟んで
回る (= 完全自動着手しない)。

**reuse の境界**: 配達層は claude-org の work-discovery 配達層 (skill / dispatcher) を
そのまま使わず *新設計* する (report.md S4.6「配達層は新設計」)。一方で人間ゲートの永続化は
MVP で確立した ``pending_decision`` レジスタを丸ごと reuse する (gate_key prefix
``"discovery-"`` で in-loop の action ゲートと名前空間を分離)。これにより pause/resume・
冪等・監査 (event log)・「一度下した決定は再決定しない」を無償で継承する。Reflexion /
transport とは独立。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .store import LoopStore

# 配達層が使う gate_key の prefix。in-loop の不可逆 action ゲート (gate-<iteration>) と
# 名前空間を分け、同じ ``pending_decision`` レジスタを安全に相乗りさせる。
GATE_KEY_PREFIX = "discovery-"


@dataclass(frozen=True)
class Candidate:
    """次反復の対象になりうる 1 件の仕事候補 (計算層の入力)。

    全フィールドは **JSON ネイティブ** であること: 候補は配達層で state.db に永続化され
    (提案の action として保存)、resume をまたいで復元・採択されるため。``payload`` には
    採択時に次ループの入力へ渡す任意の JSON ネイティブ値を載せる (タスク本文・seed 等)。

    Args:
        id: 候補の安定識別子 (非空・候補集合内で一意)。依存解決と採択指定のキー。
        priority: 優先度。**大きいほど優先** (ランキングで降順)。既定 0。
        effort: 見積り工数 (``>= 0``)。同優先度のタイブレークで **小さいほど優先**。既定 1。
        depends_on: この候補が依存する id 群。全て ``done`` にあれば *ready*。
        summary: 人間がゲートで読む 1 行要約。
        payload: 採択時に次ループ入力へ渡す JSON ネイティブ値 (任意)。
    """

    id: str
    priority: int = 0
    effort: int = 1
    depends_on: tuple[str, ...] = ()
    summary: str = ""
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Candidate.id must be a non-empty string")
        if self.effort < 0:
            raise ValueError("Candidate.effort must be >= 0")
        # depends_on をタプルに正規化 (list 等で渡されても凍結後は不変タプルに揃える)。
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """ready ランキングの決定的キー: 優先度降順 -> 工数昇順 -> id 昇順。

        id が一意なので全順序になり、入力の並び順に依存しない安定なランキングになる。
        """
        return (-self.priority, self.effort, self.id)


@dataclass(frozen=True)
class BlockedCandidate:
    """ready でない候補と、その理由 (依存が未充足 / 未知 / 循環)。

    人間がゲートで「なぜこの候補がまだ選べないか」を理解できるよう、未充足依存を
    *既知の候補待ち* (``pending_deps``) と *未知の id* (``unknown_deps``) に分類し、
    依存循環に属する場合は ``in_cycle`` を立てる。``reason`` はそれらを要約した 1 行。
    """

    candidate: Candidate
    unmet: tuple[str, ...]
    pending_deps: tuple[str, ...]
    unknown_deps: tuple[str, ...]
    in_cycle: bool
    reason: str


@dataclass(frozen=True)
class Triage:
    """計算層の出力: ランキング済み ready・blocked・推奨 1 件 (report.md S3.5)。

    ``ready`` は :attr:`Candidate.sort_key` でランキング済み (推奨順)。``recommended`` は
    その先頭 (ready が空なら ``None``)。``blocked`` は登録順ではなく id 昇順で安定化する。
    """

    ready: tuple[Candidate, ...]
    blocked: tuple[BlockedCandidate, ...]
    recommended: Optional[Candidate]


def _find_cycle_ids(candidates_by_id: dict[str, Candidate]) -> set[str]:
    """候補依存グラフ上で **循環に属する** 候補 id 集合を返す (診断用)。

    readiness 自体は ``deps ⊆ done`` だけで決まるので循環は readiness に影響しないが、
    「全候補が永続的に blocked」になる不可能依存を人間に明示できるよう検出する。

    **Tarjan の強連結成分 (SCC) 分解** で求める: サイズ 2 以上の SCC に属する候補、または
    自己依存 (自分の id を ``depends_on`` に含む) を循環とみなす。素朴な back-edge DFS は
    「既に探索完了 (BLACK) したノードを経由してのみ循環へ戻る」メンバーを cross-edge と
    誤判定して取りこぼす (false negative) ため、SCC で完全に検出する。外部依存 (候補に
    存在しない id) は辺を張らない。反復実装 (再帰なし) で深いグラフでも安全。ノードを
    ``sorted`` 順に走査し、出力は集合なので ``depends_on`` の並び順に依存しない (決定的)。
    """
    # 内部辺のみ (外部依存は循環グラフに含めない)。並び順は SCC *メンバー集合* に影響しない。
    succ = {
        cid: [d for d in c.depends_on if d in candidates_by_id]
        for cid, c in candidates_by_id.items()
    }
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    scc_stack: list[str] = []
    counter = 0
    in_cycle: set[str] = set()

    for root in sorted(candidates_by_id):
        if root in index:
            continue
        # work: (node, 次に見る後続インデックス)。再帰 Tarjan を明示スタックで反復化。
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter
                counter += 1
                scc_stack.append(node)
                on_stack.add(node)
            recursed = False
            succs = succ[node]
            while pi < len(succs):
                w = succs[pi]
                pi += 1
                if w not in index:
                    work[-1] = (node, pi)
                    work.append((w, 0))
                    recursed = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if recursed:
                continue
            if low[node] == index[node]:
                # node は SCC の根: scc_stack から成分を pop する。
                comp: list[str] = []
                while True:
                    w = scc_stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in succ[node]:
                    in_cycle.update(comp)
            work.pop()
            if work:  # 子の探索完了を親の low へ伝播 (再帰 return 相当)。
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return in_cycle


def triage(candidates: Iterable[Candidate], *, done: Iterable[str] = ()) -> Triage:
    """候補を ``done`` に対して triage する純関数 (計算層・read-only・決定的)。

    手順 (report.md S3.5 「依存解決・優先度・工数」):

    1. **依存解決**: 候補の ``depends_on`` が全て ``done`` にあれば *ready*。1 つでも欠ければ
       *blocked* で、欠けた依存を「既知候補待ち (pending)」と「未知 id (unknown)」に分類する。
    2. **ランキング**: ready を :attr:`Candidate.sort_key` (優先度降順 -> 工数昇順 -> id 昇順)
       で安定ソートし、**推奨 = 先頭** とする。入力の並び順に依存しない。
    3. **循環検出**: 候補依存グラフの循環を診断として blocked に注記する。

    既に ``done`` の id を持つ候補は「完了済み」として出力から除外する (次反復対象ではない)。
    同一入力 (順不同) は必ず同一の :class:`Triage` を返す。

    Raises:
        ValueError: 候補 id が重複している場合 (決定的出力に一意 id が必須)。
    """
    items = list(candidates)
    done_set = set(done)

    by_id: dict[str, Candidate] = {}
    for c in items:
        if c.id in by_id:
            raise ValueError(f"duplicate candidate id {c.id!r}; ids must be unique")
        by_id[c.id] = c

    # 完了済み候補は次反復対象ではないので除外する (依存充足の判定では done を使う)。
    pending_candidates = {cid: c for cid, c in by_id.items() if cid not in done_set}
    cycle_ids = _find_cycle_ids(pending_candidates)

    ready: list[Candidate] = []
    blocked: list[BlockedCandidate] = []
    for cid, c in pending_candidates.items():
        unmet = tuple(d for d in c.depends_on if d not in done_set)
        if not unmet:
            ready.append(c)
            continue
        # 未充足依存を「未完了の既知候補待ち」と「未知 id」に分類 (重複は順序保持で除去)。
        seen: set[str] = set()
        pending_deps: list[str] = []
        unknown_deps: list[str] = []
        for d in unmet:
            if d in seen:
                continue
            seen.add(d)
            (pending_deps if d in by_id else unknown_deps).append(d)
        in_cycle = cid in cycle_ids
        parts: list[str] = []
        if in_cycle:
            parts.append("依存循環")
        if pending_deps:
            parts.append(f"未完了の依存: {pending_deps}")
        if unknown_deps:
            parts.append(f"未知の依存: {unknown_deps}")
        blocked.append(
            BlockedCandidate(
                candidate=c,
                unmet=tuple(dict.fromkeys(unmet)),
                pending_deps=tuple(pending_deps),
                unknown_deps=tuple(unknown_deps),
                in_cycle=in_cycle,
                reason="; ".join(parts),
            )
        )

    ready.sort(key=lambda c: c.sort_key)
    blocked.sort(key=lambda b: b.candidate.id)
    recommended = ready[0] if ready else None
    return Triage(
        ready=tuple(ready), blocked=tuple(blocked), recommended=recommended
    )


# -- 配達層 (人間ゲート, propose-only) ----------------------------------------


def _candidate_to_dict(c: Candidate) -> dict[str, Any]:
    """候補を JSON ネイティブ dict へ (depends_on は list 化)。"""
    return {
        "id": c.id,
        "priority": c.priority,
        "effort": c.effort,
        "depends_on": list(c.depends_on),
        "summary": c.summary,
        "payload": c.payload,
    }


def _candidate_from_dict(d: dict[str, Any]) -> Candidate:
    """:func:`_candidate_to_dict` の逆 (depends_on を tuple へ戻す)。"""
    return Candidate(
        id=d["id"],
        priority=d.get("priority", 0),
        effort=d.get("effort", 1),
        depends_on=tuple(d.get("depends_on", ())),
        summary=d.get("summary", ""),
        payload=d.get("payload"),
    )


def _triage_to_action(triage_result: Triage, cycle: int) -> dict[str, Any]:
    """triage 結果を提案 action (JSON ネイティブ dict) へ符号化する。

    ``pending_decision.action`` として永続化され、resume / 採択時に
    :func:`_candidate_from_dict` で復元される。``recommended`` は id 参照で持ち、復元時に
    ready から引き当てる (候補本体の二重保存を避ける)。
    """
    return {
        "kind": "work-discovery",
        "cycle": cycle,
        "recommended": triage_result.recommended.id
        if triage_result.recommended is not None
        else None,
        "ready": [_candidate_to_dict(c) for c in triage_result.ready],
        "blocked": [
            {
                "candidate": _candidate_to_dict(b.candidate),
                "unmet": list(b.unmet),
                "pending_deps": list(b.pending_deps),
                "unknown_deps": list(b.unknown_deps),
                "in_cycle": b.in_cycle,
                "reason": b.reason,
            }
            for b in triage_result.blocked
        ],
    }


def _action_to_triage(action: dict[str, Any]) -> Triage:
    """永続化された提案 action から :class:`Triage` を復元する (:func:`_triage_to_action` の逆)。

    配達層が *永続化済みの提案* を権威として返す/読むために使う。同一 cycle を別の候補集合で
    再 propose しても (``request_decision`` が既存行を上書きしないため)、返す :class:`Triage` は
    常に **永続化され実際に採択対象となる提案** と一致する (内部不整合を作らない)。
    """
    ready = tuple(_candidate_from_dict(c) for c in action["ready"])
    ready_by_id = {c.id: c for c in ready}
    blocked = tuple(
        BlockedCandidate(
            candidate=_candidate_from_dict(b["candidate"]),
            unmet=tuple(b.get("unmet", ())),
            pending_deps=tuple(b.get("pending_deps", ())),
            unknown_deps=tuple(b.get("unknown_deps", ())),
            in_cycle=b.get("in_cycle", False),
            reason=b.get("reason", ""),
        )
        for b in action["blocked"]
    )
    rec_id = action.get("recommended")
    recommended = ready_by_id.get(rec_id) if rec_id is not None else None
    return Triage(ready=ready, blocked=blocked, recommended=recommended)


@dataclass(frozen=True)
class Proposal:
    """登録された 1 件の提案 (triage 結果 + 永続化された人間ゲート行)。

    :meth:`WorkDiscovery.propose` が返す。``pending`` は ``request_decision`` が返した
    ``pending_decision`` 行 (gate_key / status / action を含む)。**propose-only** なので
    生成直後は常に ``status == "pending"`` (既に解決済みの cycle を再 propose した場合を除く)。
    """

    triage: Triage
    cycle: int
    gate_key: str
    pending: dict[str, Any]


@dataclass(frozen=True)
class AdoptionResult:
    """人間の採否決定を解決した結果 (どの候補が次反復入力になるか)。

    ``status`` は ``"pending"`` (未決定) / ``"resolved"`` (決定済み) / ``"absent"``
    (その cycle の提案が存在しない)。``candidate`` は採択された候補 (approve/edit) または
    ``None`` (reject/respond/未決定)。``recommended`` は提案時の推奨 (参考表示用)。
    ``response`` は respond 決定の応答本文。
    """

    status: str
    decision: Optional[str]
    candidate: Optional[Candidate]
    recommended: Optional[Candidate]
    response: Any = None

    @property
    def adopted(self) -> bool:
        """次反復の入力候補が採択されたか (= ``candidate`` が存在するか)。"""
        return self.candidate is not None


class WorkDiscovery:
    """work-discovery 配達層: triage を提案として人間ゲートに載せる (propose-only)。

    人間ゲートの永続化は MVP の ``pending_decision`` レジスタを reuse する
    (:class:`~claude_loop.store.LoopStore`)。gate_key は cycle ごとに安定で
    (``discovery-<cycle>``)、in-loop の不可逆 action ゲート (``gate-<iteration>``) と
    名前空間を分ける。生成時に ``load_or_init(run_id)`` で run 行を確保する (FK のため)。

    Args:
        store: 提案・決定を永続化する :class:`~claude_loop.store.LoopStore`。
        run_id: 対象 run の ID。
    """

    def __init__(self, store: LoopStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        # run 行を確保 (request_decision の FK と begin event を冪等に満たす)。
        self.store.load_or_init(run_id)

    def gate_key(self, cycle: int) -> str:
        """cycle に対応する安定な gate_key (``discovery-<cycle>``)。"""
        return f"{GATE_KEY_PREFIX}{cycle}"

    def propose(
        self,
        candidates: Iterable[Candidate],
        *,
        done: Iterable[str] = (),
        cycle: int = 0,
    ) -> Proposal:
        """候補を triage し、その提案を人間ゲートに ``pending`` で登録する (propose-only)。

        計算層 (:func:`triage`) で「N 件 + 推奨 1 件」を求め、:func:`_triage_to_action` で
        符号化した提案を ``request_decision`` で登録する。**ここで止まる**: 何も採択せず、
        人間が :meth:`resolve` (または直接 ``store.resolve_decision``) で決めるまで pending。

        同一 ``(run_id, cycle)`` に対し冪等: ``request_decision`` が既存行を上書きしないため、
        同じ cycle で再 propose しても最初の提案・決定を壊さない (新しい候補集合で提案し直す
        には別の ``cycle`` を使う)。triage 自体は決定的なので返す :class:`Triage` は毎回同じ。
        """
        triage_result = triage(candidates, done=done)
        gk = self.gate_key(cycle)
        action = _triage_to_action(triage_result, cycle)
        pending = self.store.request_decision(self.run_id, gk, action)
        # 返す triage は **永続化された提案** (pending["action"]) から復元する。同一 cycle を
        # 別の候補集合で再 propose しても request_decision は既存行を上書きしないので、
        # 返り値が実際の採択対象 (pending / adopted) と矛盾しないよう権威ある側に揃える。
        return Proposal(
            triage=_action_to_triage(pending["action"]),
            cycle=cycle,
            gate_key=gk,
            pending=pending,
        )

    def resolve(
        self, cycle: int, decision: str, payload: Any = None
    ) -> AdoptionResult:
        """人間の採否決定を記録する型付きラッパ (= 人間ゲートの解決)。

        ``store.resolve_decision`` への薄い委譲だが、``edit`` のときは payload (採択する
        候補 id) が **その提案の ready 候補** であることを *永続前に* 検証して fail loud する
        (blocked / 未知の候補を誤って採択しないため = 依存解決の不変条件を配達層でも守る)。
        記録後の :class:`AdoptionResult` を返す (= :meth:`adopted` と同じ写像)。
        """
        if decision == "edit":
            self._require_ready_selection(cycle, payload)
        self.store.resolve_decision(self.run_id, self.gate_key(cycle), decision, payload)
        return self.adopted(cycle)

    def _load_proposal_action(self, cycle: int) -> Optional[dict[str, Any]]:
        """その cycle の登録済み提案 action を返す (未登録なら ``None``)。"""
        row = self.store.get_decision(self.run_id, self.gate_key(cycle))
        return row["action"] if row is not None else None

    def _require_ready_selection(self, cycle: int, selected_id: Any) -> None:
        """``edit`` の選択 id が提案の ready 候補であることを検証 (なければ ValueError)。"""
        action = self._load_proposal_action(cycle)
        if action is None:
            raise ValueError(
                f"no proposal for cycle {cycle} (run {self.run_id!r}); propose first"
            )
        ready_ids = {c["id"] for c in action["ready"]}
        if selected_id not in ready_ids:
            raise ValueError(
                f"edit selection {selected_id!r} is not a ready candidate of "
                f"cycle {cycle}; ready={sorted(ready_ids)}"
            )

    def adopted(self, cycle: int = 0) -> AdoptionResult:
        """その cycle の人間決定を読み、採択された候補へ写像する (resume をまたいで安定)。

        永続化された ``pending_decision`` 行と提案 action から復元するので、別プロセス /
        resume 後に呼んでも同じ採択結果になる (純粋な読み出し・冪等)。決定の写像:

        - ``approve`` -> 推奨候補 (recommended) を採択
        - ``edit``    -> payload が指す ready 候補を採択 (ready 外なら ValueError)
        - ``reject``  -> 採択なし (``candidate=None``)
        - ``respond`` -> 採択なし。応答本文を ``response`` に載せる
        - 未決定 (pending) / 提案なし (absent) -> 採択なし
        """
        gk = self.gate_key(cycle)
        row = self.store.get_decision(self.run_id, gk)
        if row is None:
            return AdoptionResult(
                status="absent", decision=None, candidate=None, recommended=None
            )
        action = row["action"]
        triage_result = _action_to_triage(action)
        ready_by_id = {c.id: c for c in triage_result.ready}
        recommended = triage_result.recommended

        if row["status"] == "pending":
            return AdoptionResult(
                status="pending",
                decision=None,
                candidate=None,
                recommended=recommended,
            )

        decision = row["decision"]
        payload = row["payload"]
        candidate: Optional[Candidate] = None
        response: Any = None
        if decision == "approve":
            candidate = recommended
        elif decision == "edit":
            if payload not in ready_by_id:
                raise ValueError(
                    f"edit selection {payload!r} is not a ready candidate of "
                    f"cycle {cycle}; ready={sorted(ready_by_id)}"
                )
            candidate = ready_by_id[payload]
        elif decision == "respond":
            response = payload
        # reject -> candidate は None のまま (採択なし)。
        return AdoptionResult(
            status="resolved",
            decision=decision,
            candidate=candidate,
            recommended=recommended,
            response=response,
        )


def discover_next(
    *,
    store: LoopStore,
    run_id: str,
    candidates: Iterable[Candidate],
    result: Optional[Any] = None,
    done: Iterable[str] = (),
    cycle: int = 0,
) -> Optional[Proposal]:
    """完了 -> 次反復の接続点: 直前ループが完了していれば次候補を提案する (propose-only)。

    report.md S5 Phase 3 成功条件 d 「完了 -> 次反復の接続が人間ゲート越しに回る」を体現する
    入口。``result`` (直前の :class:`~claude_loop.loop.LoopResult`) を渡すと、それが
    **``paused`` のときは提案しない** (``None`` を返す) — まだ何も完了しておらず、先に人間が
    そのゲートを解決すべきだから。完了 (goal_met / stopped) または ``result=None`` のときだけ
    :meth:`WorkDiscovery.propose` を呼ぶ。

    **完全自動着手はしない**: 本関数は提案 (pending) を登録するだけで、採択も次ループ起動も
    行わない。採択は人間が :meth:`WorkDiscovery.resolve` で決め、採択された候補
    (:meth:`WorkDiscovery.adopted`) を呼び出し側が次ループの入力にする。
    """
    if result is not None and getattr(result, "paused", False):
        return None
    return WorkDiscovery(store, run_id).propose(candidates, done=done, cycle=cycle)


__all__ = [
    "Candidate",
    "BlockedCandidate",
    "Triage",
    "triage",
    "Proposal",
    "AdoptionResult",
    "WorkDiscovery",
    "discover_next",
    "GATE_KEY_PREFIX",
]
