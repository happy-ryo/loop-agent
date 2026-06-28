"""外側 Reflexion ループの状態 SoT: epoch/lesson/評価器 version を state.db に永続化 (Issue #29).

MVP 内側 resume (:meth:`loop_agent.store.LoopStore.load_or_init` / Issue #14) と store lease
機構 (Issue #21) を土台に、外側 :func:`loop_agent.reflexion.run_reflexion` の **試行間の学習状態**
(epoch 進行・episodic memory の lesson・固定評価器の version) を SQLite に永続化し、再起動後も
**学習の続きから resume** できるようにする (report.md S4.4/S5 Phase3 follow-up)。

設計の境界 (store.py の最小スキーマ思想を継ぐ):

- **内側スキーマと独立・additive**: 既存の ``run / step / event / stop_reason / pending_decision``
  には一切触れず、``reflexion_run / reflexion_episode / reflexion_lesson / reflexion_evaluator``
  の 4 表を ``IF NOT EXISTS`` で **非破壊に追加** する。古い DB を開いても既存データは無傷で、
  本クラスの生成時に不足テーブルだけが作られる (:meth:`ReflexionStore.__init__`)。
- **settled state を SoT にする**: :func:`~loop_agent.reflexion.run_reflexion` の ``persist`` フック
  (epoch 境界処理の *後* に発火) から :meth:`ReflexionStore.persist_episode` を呼び、1 episode 分の
  「episode 行 + memory の全 lesson 行 + reflexion_run のスカラ + 評価器 version 登録」を **1 つの
  transaction** に束ねる (内側 :meth:`LoopStore.record_step` と同じ atomic 境界の方針)。途中クラッシュ
  すれば episode は丸ごと無かったことになり、resume は直前の確定 episode から再開する。
- **評価器 version registry + fail-loud**: 各 epoch で固定された評価器の version を
  ``reflexion_evaluator`` に追記 (audit)、現行 version を ``reflexion_run`` に持つ。resume 時、
  復元 ``evaluator_version`` と渡された ``evaluator.version`` が食い違えば
  :func:`~loop_agent.reflexion.run_reflexion` が **loud に弾く** (callable は直列化できないので
  別評価器に silently 差し替えない。PR #28 で確立した安全核を継ぐ)。本モジュールは version を
  忠実に往復させるだけで、採択基準・二信号モデルには一切踏み込まない。

memory の容量ポリシー (cap / per_lesson_chars / render_byte_cap) も ``reflexion_run`` に保存し、
復元時に同じ上限の :class:`EpisodicMemory` を組み直す (eviction 挙動が resume をまたいで一致する)。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional

from .evaluator import GroundTruthSignal, Score
from .memory import EpisodicMemory, Lesson
from .reflexion import EpisodeRecord, ReflexionState
from .store import DbSource, _init_connection, connect

if TYPE_CHECKING:  # 型注釈のためだけ (実行時 import cycle を避ける)
    from .reflexion import ReflexiveResult

# 外側ループ用の追加スキーマ。内側 4 表とは独立した自己完結の 4 表。すべて ``IF NOT EXISTS`` で
# 冪等・非破壊 (古い DB に対しても既存データを壊さず不足分だけ作る)。
#
# - reflexion_run      : 外側 run 1 本 1 行。settled なスカラ状態 (episode/epoch/評価器 version/
#                        best/予算カウンタ/declared_keys/memory 容量) の正本。resume の seed。
# - reflexion_episode  : 確定した各 episode 1 行 (audit + 復元元)。UNIQUE(run_id, episode) で
#                        再実行に冪等。signal/lesson は JSON で保存。
# - reflexion_lesson   : episodic memory の **現在の全 lesson** (persist 毎に全置換)。eviction で
#                        消えた lesson が DB に残らないよう delete+insert する。position で順序保持。
# - reflexion_evaluator: epoch で固定された評価器 version の追記レジストリ (version registry の audit)。
_REFLEXION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflexion_run (
  run_id               TEXT PRIMARY KEY,
  status               TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','converged','stopped','paused')),
  episode              INTEGER NOT NULL DEFAULT 0,
  epoch                INTEGER NOT NULL DEFAULT 0,
  evaluator_version    TEXT NOT NULL DEFAULT '',
  best_gt_aggregate    REAL,
  reflections          INTEGER NOT NULL DEFAULT 0,
  evaluator_updates    INTEGER NOT NULL DEFAULT 0,
  declared_keys        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(declared_keys)),
  mem_cap              INTEGER NOT NULL DEFAULT 8,
  mem_per_lesson_chars INTEGER NOT NULL DEFAULT 512,
  mem_render_byte_cap  INTEGER NOT NULL DEFAULT 4096,
  stop_name            TEXT,
  reason               TEXT NOT NULL DEFAULT '',
  started_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at             TEXT
);

CREATE TABLE IF NOT EXISTS reflexion_episode (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            TEXT NOT NULL REFERENCES reflexion_run(run_id) ON DELETE CASCADE,
  episode           INTEGER NOT NULL,
  epoch             INTEGER NOT NULL,
  evaluator_version TEXT NOT NULL,
  reward            REAL NOT NULL,
  gt_aggregate      REAL NOT NULL,
  succeeded         INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0,1)),
  admitted          INTEGER NOT NULL DEFAULT 0 CHECK (admitted IN (0,1)),
  detail            TEXT NOT NULL DEFAULT '',
  signal            TEXT NOT NULL CHECK (json_valid(signal)),
  lesson            TEXT CHECK (lesson IS NULL OR json_valid(lesson)),
  recorded_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (run_id, episode)
);
CREATE INDEX IF NOT EXISTS idx_reflexion_episode_run ON reflexion_episode(run_id);

CREATE TABLE IF NOT EXISTS reflexion_lesson (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES reflexion_run(run_id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  text        TEXT NOT NULL,
  episode     INTEGER NOT NULL,
  provenance  TEXT NOT NULL,
  support     REAL NOT NULL DEFAULT 0.0,
  UNIQUE (run_id, position)
);
CREATE INDEX IF NOT EXISTS idx_reflexion_lesson_run ON reflexion_lesson(run_id);

CREATE TABLE IF NOT EXISTS reflexion_evaluator (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL REFERENCES reflexion_run(run_id) ON DELETE CASCADE,
  version       TEXT NOT NULL,
  epoch         INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (run_id, version)
);
"""


def _encode_signal(signal: GroundTruthSignal) -> str:
    """:class:`GroundTruthSignal` を strictly-valid JSON に符号化する (round-trip 用)。

    ``allow_nan=False`` で非有限スコア (NaN/Infinity) を **loud に弾く**: 復元できない値を
    silently 潰すと resume 後の集約・収束判定が狂うため、永続化時点で落とす方を選ぶ
    (observation の best-effort 永続化とは方針が異なる — これは制御に載る値)。
    """
    return json.dumps(
        {
            "succeeded": bool(signal.succeeded),
            "ground_truth_backed": bool(signal.ground_truth_backed),
            "score": {
                "ground_truth": signal.score.ground_truth,
                "components": dict(signal.score.components),
                "judge": signal.score.judge,
                "detail": signal.score.detail,
            },
        },
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_signal(blob: str) -> GroundTruthSignal:
    """:func:`_encode_signal` の逆。``components`` は素の dict で戻す (Score の比較は dict で成立)。"""
    d = json.loads(blob)
    s = d["score"]
    return GroundTruthSignal(
        succeeded=bool(d["succeeded"]),
        ground_truth_backed=bool(d["ground_truth_backed"]),
        score=Score(
            ground_truth=float(s["ground_truth"]),
            components={k: float(v) for k, v in s["components"].items()},
            judge=None if s["judge"] is None else float(s["judge"]),
            detail=s["detail"],
        ),
    )


def _encode_lesson(lesson: Lesson) -> str:
    return json.dumps(
        {
            "text": lesson.text,
            "episode": lesson.episode,
            "provenance": lesson.provenance,
            "support": lesson.support,
        },
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_lesson(blob: Optional[str]) -> Optional[Lesson]:
    if blob is None:
        return None
    d = json.loads(blob)
    return Lesson(
        text=d["text"],
        episode=int(d["episode"]),
        provenance=d["provenance"],
        support=float(d["support"]),
    )


class ReflexionStore:
    """外側 Reflexion 状態の writer/reader (内側 :class:`~loop_agent.store.LoopStore` の対)。

    ``conn`` は :func:`~loop_agent.store.connect` が返した接続でも、素の ``sqlite3.connect()``
    で開いた借用接続でもよい。後者でも動くよう、生成時に :func:`~loop_agent.store._init_connection`
    を防御的に呼んで PRAGMA (row_factory=Row / isolation_level=None / foreign_keys=ON / WAL) と
    内側スキーマを冪等適用し (:class:`~loop_agent.store.LoopStore` と同方針)、続けて外側用スキーマ
    (:data:`_REFLEXION_SCHEMA`) を非破壊に追加する (古い DB には不足テーブルだけが作られ、既存の
    内側データは無傷)。``row_factory`` を立てないと全 read が列名アクセスで壊れ、``foreign_keys``
    を立てないと ``ON DELETE CASCADE`` が効かないため、この正規化は必須。すべての書き込みは
    :meth:`_transaction` 配下で atomic に行う。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # 借用 raw 接続でも resume の read が壊れないよう PRAGMA + row_factory を冪等適用してから
        # additive スキーマ (IF NOT EXISTS) を足す。_init_connection / executescript はいずれも
        # 暗黙 COMMIT を伴うが、生成時点で開いている transaction は無い前提なので安全。
        _init_connection(conn)
        conn.executescript(_REFLEXION_SCHEMA)

    # -- transaction 制御 (LoopStore.transaction と同じ参加プロトコル) ----------

    @contextmanager
    def _transaction(self) -> Iterator["ReflexionStore"]:
        """``BEGIN IMMEDIATE`` -> yield -> ``COMMIT`` (例外時 ``ROLLBACK``)。

        既に外側 transaction 内ならそれに参加する (sqlite はネスト BEGIN を許さない)。
        ``BEGIN IMMEDIATE`` で最初から write ロックを取り、load_or_init の read→write 昇格で
        WAL の ``SQLITE_BUSY_SNAPSHOT`` を踏まないようにする (内側 store と同方針)。
        """
        if self.conn.in_transaction:
            yield self
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    # -- run ライフサイクル --------------------------------------------------

    def load_or_init(
        self, run_id: str, *, memory: Optional[EpisodicMemory] = None
    ) -> ReflexionState:
        """``run_id`` の外側 run 行を確保し、その時点の :class:`ReflexionState` を返す。

        - 新規 ``run_id``: ``reflexion_run`` 行を作成し、空の :class:`ReflexionState` を返す。
          ``memory`` を渡すとその容量ポリシーを保存し、**その live オブジェクト** を state の
          memory にする (``None`` なら既定 :class:`EpisodicMemory`)。fresh run は
          ``run_reflexion(initial_state=..., memory=...)`` に渡しても空なので通常 start と同義。
        - 既存 ``run_id``: ``reflexion_run`` のスカラ + ``reflexion_episode`` + ``reflexion_lesson``
          から :class:`ReflexionState` を **復元** して返す。これを
          ``run_reflexion(initial_state=state, memory=state.memory, persist=...)`` に渡すと
          中断地点から **resume** できる (#29)。復元 ``evaluator_version`` / ``declared_keys`` が
          渡された評価器 / 軸と食い違えば run_reflexion が loud に弾く (整合の継ぎ目を壊さない)。

        作成/復元は 1 transaction で atomic に行う。
        """
        if not run_id:
            raise ValueError("load_or_init: run_id must be a non-empty string")
        with self._transaction():
            row = self.conn.execute(
                "SELECT run_id FROM reflexion_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                mem = memory if memory is not None else EpisodicMemory()
                self.conn.execute(
                    "INSERT INTO reflexion_run "
                    "(run_id, mem_cap, mem_per_lesson_chars, mem_render_byte_cap) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, mem.cap, mem.per_lesson_chars, mem.render_byte_cap),
                )
                return ReflexionState(memory=mem)
            return self._reconstruct_state(run_id)

    def _reconstruct_state(self, run_id: str) -> ReflexionState:
        """永続化済みの行から :class:`ReflexionState` を組み立てる (resume の復元)。"""
        run = self.conn.execute(
            "SELECT episode, epoch, evaluator_version, best_gt_aggregate, reflections, "
            "evaluator_updates, declared_keys, mem_cap, mem_per_lesson_chars, "
            "mem_render_byte_cap FROM reflexion_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        declared_keys = tuple(json.loads(run["declared_keys"]))

        episodes: list[EpisodeRecord] = []
        gt_aggregate_history: list[float] = []
        for e in self.conn.execute(
            "SELECT episode, epoch, evaluator_version, reward, gt_aggregate, succeeded, "
            "admitted, detail, signal, lesson FROM reflexion_episode "
            "WHERE run_id = ? ORDER BY episode",
            (run_id,),
        ):
            signal = _decode_signal(e["signal"])
            record = EpisodeRecord(
                episode=e["episode"],
                epoch=e["epoch"],
                evaluator_version=e["evaluator_version"],
                signal=signal,
                reward=e["reward"],
                gt_aggregate=e["gt_aggregate"],
                lesson=_decode_lesson(e["lesson"]),
                admitted=bool(e["admitted"]),
                succeeded=bool(e["succeeded"]),
                detail=e["detail"],
            )
            episodes.append(record)
            # gt_aggregate_history は **ground_truth_backed な** episode のみ (driver と同じ規則)。
            if signal.ground_truth_backed:
                gt_aggregate_history.append(e["gt_aggregate"])

        memory = EpisodicMemory(
            cap=run["mem_cap"],
            per_lesson_chars=run["mem_per_lesson_chars"],
            render_byte_cap=run["mem_render_byte_cap"],
        )
        for lrow in self.conn.execute(
            "SELECT text, episode, provenance, support FROM reflexion_lesson "
            "WHERE run_id = ? ORDER BY position",
            (run_id,),
        ):
            # admit を経ずに直接 list を復元する (検証・eviction・dedup は保存時に済んでいる。
            # admit を通すと cap 超過分が再 evict されて保存内容と食い違いうる)。
            memory._lessons.append(
                Lesson(
                    text=lrow["text"],
                    episode=lrow["episode"],
                    provenance=lrow["provenance"],
                    support=lrow["support"],
                )
            )

        best = run["best_gt_aggregate"]
        return ReflexionState(
            episode=run["episode"],
            epoch=run["epoch"],
            evaluator_version=run["evaluator_version"],
            gt_aggregate_history=gt_aggregate_history,
            best_gt_aggregate=float("-inf") if best is None else best,
            reflections=run["reflections"],
            evaluator_updates=run["evaluator_updates"],
            declared_keys=declared_keys,
            episodes=episodes,
            memory=memory,
        )

    # -- per-episode 永続化 --------------------------------------------------

    def persist_episode(
        self, run_id: str, record: EpisodeRecord, state: ReflexionState
    ) -> None:
        """確定した 1 episode を atomic に永続化する (run_reflexion の ``persist`` フック互換)。

        1 transaction で「episode 行の upsert + memory 全 lesson の全置換 + reflexion_run スカラの
        更新 + 評価器 version の登録」を束ねる。``state`` は **境界処理後の settled state** を渡す
        こと (run_reflexion はそうしている) — そうすれば persist された epoch / evaluator_version /
        evaluator_updates が post-boundary になり resume が通し実行と一致する。

        ``UNIQUE(run_id, episode)`` 衝突時は ``DO UPDATE`` で上書きするので、同一 episode の再
        永続化 (resume 後の再走) に冪等。memory は eviction で件数が減りうるため、append ではなく
        **delete+insert で全置換** する (DB が memory の現在像と完全一致する)。
        """
        signal_json = _encode_signal(record.signal)
        lesson_json = None if record.lesson is None else _encode_lesson(record.lesson)
        with self._transaction():
            self.conn.execute(
                "INSERT INTO reflexion_episode "
                "(run_id, episode, epoch, evaluator_version, reward, gt_aggregate, "
                " succeeded, admitted, detail, signal, lesson) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, episode) DO UPDATE SET "
                "  epoch = excluded.epoch, "
                "  evaluator_version = excluded.evaluator_version, "
                "  reward = excluded.reward, "
                "  gt_aggregate = excluded.gt_aggregate, "
                "  succeeded = excluded.succeeded, "
                "  admitted = excluded.admitted, "
                "  detail = excluded.detail, "
                "  signal = excluded.signal, "
                "  lesson = excluded.lesson",
                (
                    run_id,
                    record.episode,
                    record.epoch,
                    record.evaluator_version,
                    record.reward,
                    record.gt_aggregate,
                    int(bool(record.succeeded)),
                    int(bool(record.admitted)),
                    record.detail,
                    signal_json,
                    lesson_json,
                ),
            )
            self._flush_settled(run_id, state)
            # 新規 episode が確定した = run は running。前回 stopped/paused を record_result で記録
            # した後に resume した場合、終端メタデータ (status/stop_name/reason/ended_at) を
            # running へ戻す。さもないと get_run が advance 済みの run を stale な stopped /
            # 古い ended_at で報告し、reflexion_run が lifecycle の SoT でなくなる。
            self.conn.execute(
                "UPDATE reflexion_run SET status = 'running', stop_name = NULL, "
                "reason = '', ended_at = NULL WHERE run_id = ?",
                (run_id,),
            )

    def _flush_settled(self, run_id: str, state: ReflexionState) -> None:
        """settled な state (memory 全像 + 評価器 version + reflexion_run スカラ) を書き出す。

        ``persist_episode`` (episode 確定時) と ``record_result`` (終端確定時) の両方から呼ぶ
        共通核。**呼び出し側が transaction を保持していること** が前提 (atomic 境界は caller が張る)。
        memory は eviction で件数が減りうるため append ではなく **delete+insert で全置換** し、DB を
        memory の現在像と完全一致させる。両者から呼ぶことで、resume の末尾境界 recovery が episode を
        1 つも完了させずに stop へ落ちた場合 (例: ``EvaluatorUpdateBudget`` を recovery の昇格が即
        踏む) でも、``record_result`` 経由で recovery 後の epoch / evaluator_version / evaluator_updates
        が確実に永続化される (返り値と DB が乖離せず、再 resume で昇格を二度踏まない)。
        """
        best = state.best_gt_aggregate
        best_db = None if best == float("-inf") else best
        declared_json = json.dumps(list(state.declared_keys))
        # memory の現在像で lesson 表を全置換 (eviction された lesson を残さない)。
        self.conn.execute("DELETE FROM reflexion_lesson WHERE run_id = ?", (run_id,))
        for position, lesson in enumerate(state.memory.lessons()):
            self.conn.execute(
                "INSERT INTO reflexion_lesson "
                "(run_id, position, text, episode, provenance, support) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    position,
                    lesson.text,
                    lesson.episode,
                    lesson.provenance,
                    lesson.support,
                ),
            )
        # 評価器 version registry: 現行 version を追記 (audit。冪等)。
        if state.evaluator_version:
            self.conn.execute(
                "INSERT OR IGNORE INTO reflexion_evaluator "
                "(run_id, version, epoch) VALUES (?, ?, ?)",
                (run_id, state.evaluator_version, state.epoch),
            )
        # settled スカラを reflexion_run に書く (resume seed の正本)。
        self.conn.execute(
            "UPDATE reflexion_run SET episode = ?, epoch = ?, evaluator_version = ?, "
            "best_gt_aggregate = ?, reflections = ?, evaluator_updates = ?, "
            "declared_keys = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE run_id = ?",
            (
                state.episode,
                state.epoch,
                state.evaluator_version,
                best_db,
                state.reflections,
                state.evaluator_updates,
                declared_json,
                run_id,
            ),
        )

    def record_result(self, run_id: str, result: "ReflexiveResult") -> None:
        """外側ループ終了時の **settled state + 最終ステータス** を確定する (DBReflexionLog の終端配線)。

        ``status`` (``converged`` / ``stopped`` / ``paused``) と発火条件 / 理由を記録し、加えて
        ``result.state`` の settled スカラ + memory + 評価器 version を :meth:`_flush_settled` で
        書き直す (内側 :meth:`~loop_agent.store.LoopStore.record_result` が最終集計を畳むのと同方針)。
        これにより、resume の末尾境界 recovery が episode を 1 つも完了させずに停止した場合でも、
        recovery 後の状態が DB に確実に反映され、返り値と DB が一致する。``paused`` は終端ではない
        ので ``ended_at`` を立てない (resume で続行できる)。
        """
        stop_name = result.stop.name if result.stop is not None else None
        ended = result.status != "paused"
        with self._transaction():
            self._flush_settled(run_id, result.state)
            self.conn.execute(
                "UPDATE reflexion_run SET status = ?, stop_name = ?, reason = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "ended_at = CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "ELSE NULL END WHERE run_id = ?",
                (result.status, stop_name, result.reason, int(ended), run_id),
            )

    # -- 読み出し ------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """``reflexion_run`` 行を dict で返す (無ければ ``None``)。"""
        row = self.conn.execute(
            "SELECT * FROM reflexion_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def read_episodes(self, run_id: str) -> list[dict[str, Any]]:
        """``run_id`` の episode 行を episode 順に dict のリストで返す (signal/lesson は復号)。"""
        rows = self.conn.execute(
            "SELECT * FROM reflexion_episode WHERE run_id = ? ORDER BY episode",
            (run_id,),
        ).fetchall()
        episodes: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["succeeded"] = bool(d["succeeded"])
            d["admitted"] = bool(d["admitted"])
            d["signal"] = _decode_signal(d["signal"])
            d["lesson"] = _decode_lesson(d["lesson"])
            episodes.append(d)
        return episodes

    def read_evaluator_versions(self, run_id: str) -> list[dict[str, Any]]:
        """``run_id`` で固定された評価器 version の登録履歴を登録順に返す (version registry)。"""
        rows = self.conn.execute(
            "SELECT version, epoch, first_seen_at FROM reflexion_evaluator "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


class DBReflexionLog:
    """DB-backed の外側 Reflexion 進捗記録。内側 :class:`~loop_agent.store.DBProgressLog` の対。

    ``db`` にはファイルパス (内部で :func:`~loop_agent.store.connect` し所有権を持って
    :meth:`close` で閉じる) か既存の ``sqlite3.Connection`` (借用。close では閉じない) を渡せる。
    生成時に ``load_or_init(run_id)`` を呼んで :attr:`state` (新規なら空・既存なら復元した途中状態)
    を保持する。これが **resume の入口**:

        log = DBReflexionLog("outer.db", "run-1")
        result = run_reflexion(
            ..., initial_state=log.state, memory=log.memory, persist=log.on_episode,
        )
        log.record_result(result)   # 任意 (終端メタデータの audit)

    と配線すれば、中断した外側ループを epoch 進行・採用 lesson・評価器 version ごと途中から継続
    できる (新規 run では ``state`` が空なので fresh start と同義 = 同じ配線でよい)。``memory`` を
    渡すと fresh run の memory 容量ポリシーを指定できる (resume では DB の保存値が優先)。
    """

    def __init__(
        self,
        db: DbSource,
        run_id: str,
        *,
        memory: Optional[EpisodicMemory] = None,
    ) -> None:
        if isinstance(db, sqlite3.Connection):
            self.conn = db
            self._owns_conn = False
        else:
            self.conn = connect(db)
            self._owns_conn = True
        self.run_id = run_id
        self.store = ReflexionStore(self.conn)
        # 復元した (新規なら空の) ReflexionState を resume の seed として保持する。
        self.state = self.store.load_or_init(run_id, memory=memory)

    @property
    def memory(self) -> EpisodicMemory:
        """resume seed の live memory (``run_reflexion(memory=...)`` にそのまま渡す)。"""
        return self.state.memory

    def on_episode(self, record: EpisodeRecord, state: ReflexionState) -> None:
        """確定 episode を永続化する。``run_reflexion(persist=...)`` に渡す。"""
        self.store.persist_episode(self.run_id, record, state)

    def record_result(self, result: "ReflexiveResult") -> None:
        """外側ループ終了時の最終ステータスを確定する (任意)。"""
        self.store.record_result(self.run_id, result)

    def close(self) -> None:
        """自分で開いた接続のみ閉じる (借用接続は呼び出し側の責務)。"""
        if self._owns_conn:
            self.conn.close()

    def __enter__(self) -> "DBReflexionLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "ReflexionStore",
    "DBReflexionLog",
]
