"""State SoT for the outer Reflexion loop: persist epoch/lesson/evaluator version to state.db (Issue #29).

Built on MVP inner resume (:meth:`loop_agent.store.LoopStore.load_or_init` / Issue #14) and the
store lease mechanism (Issue #21), this persists the **learning state between attempts** of the
outer :func:`loop_agent.reflexion.run_reflexion` (epoch progress, episodic-memory lessons, and the
fixed evaluator version) to SQLite so it can **resume learning from where it left off** after a
restart (report.md S4.4/S5 Phase3 follow-up).

Design boundaries (following store.py's minimal-schema philosophy):

- **Independent of and additive to the inner schema**: never touches the existing
  ``run / step / event / stop_reason / pending_decision`` tables, and non-destructively adds the
  four ``reflexion_run / reflexion_episode / reflexion_lesson / reflexion_evaluator`` tables with
  ``IF NOT EXISTS``. Opening an old DB leaves existing data intact, and only missing tables are
  created when this class is constructed (:meth:`ReflexionStore.__init__`).
- **Use settled state as the SoT**: :meth:`ReflexionStore.persist_episode` is called from
  :func:`~loop_agent.reflexion.run_reflexion`'s ``persist`` hook (fired *after* epoch boundary
  processing) and groups one episode's "episode row + all memory lesson rows + reflexion_run
  scalars + evaluator version registration" into **one transaction** (the same atomic-boundary
  policy as the inner :meth:`LoopStore.record_step`). If a crash happens midway, the whole episode
  is treated as absent, and resume restarts from the previous settled episode.
- **Evaluator version registry + fail loud**: appends the evaluator version fixed for each epoch to
  ``reflexion_evaluator`` (audit) and keeps the current version on ``reflexion_run``. On resume,
  :func:`~loop_agent.reflexion.run_reflexion` **rejects loudly** if the restored
  ``evaluator_version`` differs from the supplied ``evaluator.version`` (callables cannot be
  serialized, so a different evaluator must not be silently substituted; this continues the safety
  core established in PR #28). This module only round-trips versions faithfully and does not touch
  the admission criteria or two-signal model.

The memory capacity policy (cap / per_lesson_chars / render_byte_cap) is also saved to
``reflexion_run``, and restoration rebuilds :class:`EpisodicMemory` with the same limits so
eviction behavior remains consistent across resume.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional

from .errors import ConfigError
from .evaluator import GroundTruthSignal, Score
from .memory import EpisodicMemory, Lesson
from .reflexion import EpisodeRecord, ReflexionState
from .store import DbSource, _init_connection, connect

if TYPE_CHECKING:  # Only for type annotations (avoid a runtime import cycle).
    from .reflexion import ReflexiveResult

# Additive schema for the outer loop. These four self-contained tables are independent from the
# inner four tables. All use ``IF NOT EXISTS`` so setup is idempotent and non-destructive (for old
# DBs, only missing pieces are created without damaging existing data).
#
# - reflexion_run      : One row per outer run. The canonical settled scalar state
#                        (episode/epoch/evaluator version/best/budget counters/declared_keys/memory
#                        capacity). The resume seed.
# - reflexion_episode  : One row per settled episode (audit + restore source). UNIQUE(run_id,
#                        episode) makes reruns idempotent. signal/lesson are stored as JSON.
# - reflexion_lesson   : **All current lessons** in episodic memory (fully replaced on each
#                        persist). Uses delete+insert so evicted lessons do not remain in the DB.
#                        position preserves order.
# - reflexion_evaluator: Append-only registry of evaluator versions fixed for epochs (version
#                        registry audit).
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
    """Encode :class:`GroundTruthSignal` as strictly valid JSON (for round-tripping).

    ``allow_nan=False`` **rejects non-finite scores loudly** (NaN/Infinity): silently flattening
    values that cannot be restored would corrupt aggregation and convergence checks after resume,
    so persistence fails at write time instead (unlike best-effort observation persistence, these
    values are part of control flow).
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
    """Inverse of :func:`_encode_signal`. Returns ``components`` as a plain dict for Score equality."""
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
    """Writer/reader for outer Reflexion state (the counterpart to inner :class:`~loop_agent.store.LoopStore`).

    ``conn`` may be a connection returned by :func:`~loop_agent.store.connect` or a borrowed
    connection opened by plain ``sqlite3.connect()``. To support the latter, construction
    defensively calls :func:`~loop_agent.store._init_connection` to idempotently apply PRAGMAs
    (row_factory=Row / isolation_level=None / foreign_keys=ON / WAL) and the inner schema (matching
    :class:`~loop_agent.store.LoopStore`), then non-destructively adds the outer schema
    (:data:`_REFLEXION_SCHEMA`) (old DBs get only missing tables, with existing inner data intact).
    This normalization is required because reads depend on column-name access unless
    ``row_factory`` is set, and ``ON DELETE CASCADE`` does not work unless ``foreign_keys`` is
    enabled. All writes are atomic under :meth:`_transaction`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # Idempotently apply PRAGMAs + row_factory before adding the additive schema
        # (IF NOT EXISTS), so resume reads also work with borrowed raw connections.
        # _init_connection / executescript both imply COMMIT, but construction assumes no
        # transaction is open, so that is safe.
        _init_connection(conn)
        conn.executescript(_REFLEXION_SCHEMA)

    # -- transaction control (same participation protocol as LoopStore.transaction) ----------

    @contextmanager
    def _transaction(self) -> Iterator["ReflexionStore"]:
        """``BEGIN IMMEDIATE`` -> yield -> ``COMMIT`` (``ROLLBACK`` on exception).

        If already inside an outer transaction, join it (sqlite does not allow nested BEGIN).
        ``BEGIN IMMEDIATE`` takes the write lock up front so load_or_init's read-to-write promotion
        does not hit WAL ``SQLITE_BUSY_SNAPSHOT`` (same policy as the inner store).
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

    # -- run lifecycle --------------------------------------------------

    def load_or_init(
        self, run_id: str, *, memory: Optional[EpisodicMemory] = None
    ) -> ReflexionState:
        """Ensure the outer run row for ``run_id`` exists and return the current :class:`ReflexionState`.

        - New ``run_id``: create the ``reflexion_run`` row and return an empty
          :class:`ReflexionState`. If ``memory`` is supplied, its capacity policy is saved and
          **that live object** becomes the state's memory (otherwise the default
          :class:`EpisodicMemory` is used). A fresh run remains empty even when passed to
          ``run_reflexion(initial_state=..., memory=...)``, so this is equivalent to a normal start.
        - Existing ``run_id``: **restore** and return :class:`ReflexionState` from the
          ``reflexion_run`` scalars + ``reflexion_episode`` + ``reflexion_lesson``. Passing this to
          ``run_reflexion(initial_state=state, memory=state.memory, persist=...)`` can **resume**
          from the interruption point (#29). If the restored ``evaluator_version`` /
          ``declared_keys`` differ from the supplied evaluator / axes, run_reflexion rejects loudly
          to preserve the consistency boundary.

        Creation/restoration is atomic in one transaction.
        """
        if not run_id:
            raise ConfigError("load_or_init: run_id must be a non-empty string")
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
        """Build :class:`ReflexionState` from persisted rows (resume restoration)."""
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
            # gt_aggregate_history includes only **ground_truth_backed** episodes (same rule as the
            # driver).
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
            # Restore the list directly without admit (validation, eviction, and dedup already ran
            # before saving). Routing through admit could evict over-cap entries again and diverge
            # from the saved contents.
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

    # -- per-episode persistence --------------------------------------------------

    def persist_episode(
        self, run_id: str, record: EpisodeRecord, state: ReflexionState
    ) -> None:
        """Persist one settled episode atomically (compatible with run_reflexion's ``persist`` hook).

        One transaction groups "episode row upsert + full replacement of all memory lessons +
        reflexion_run scalar update + evaluator version registration". ``state`` must be the
        **settled state after boundary processing** (run_reflexion supplies it that way), so the
        persisted epoch / evaluator_version / evaluator_updates are post-boundary and resume
        matches an uninterrupted run.

        On ``UNIQUE(run_id, episode)`` conflicts, ``DO UPDATE`` overwrites the row, making repeated
        persistence of the same episode (after resume reruns) idempotent. Since memory size can
        shrink through eviction, lessons are **fully replaced with delete+insert** instead of
        appended, keeping the DB exactly aligned with the current memory image.
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
            # A new settled episode means the run is running. If resume follows a previous
            # record_result that stored stopped/paused, reset terminal metadata
            # (status/stop_name/reason/ended_at) to running. Otherwise get_run would report an
            # advanced run with stale stopped / old ended_at values, and reflexion_run would stop
            # being the lifecycle SoT.
            self.conn.execute(
                "UPDATE reflexion_run SET status = 'running', stop_name = NULL, "
                "reason = '', ended_at = NULL WHERE run_id = ?",
                (run_id,),
            )

    def _flush_settled(self, run_id: str, state: ReflexionState) -> None:
        """Write settled state (full memory image + evaluator version + reflexion_run scalars).

        Shared core called by both ``persist_episode`` (when an episode settles) and
        ``record_result`` (when termination settles). Assumes **the caller already holds a
        transaction** (the caller defines the atomic boundary). Since memory size can shrink through
        eviction, lessons are **fully replaced with delete+insert** instead of appended, making the
        DB exactly match the current memory image. Calling this from both paths ensures that even if
        resume tail-boundary recovery stops before completing any episode (for example, recovery
        promotion immediately hits ``EvaluatorUpdateBudget``), the post-recovery epoch /
        evaluator_version / evaluator_updates are reliably persisted through ``record_result`` (the
        return value and DB do not diverge, and a later resume does not repeat the promotion).
        """
        best = state.best_gt_aggregate
        best_db = None if best == float("-inf") else best
        declared_json = json.dumps(list(state.declared_keys))
        # Fully replace the lesson table with the current memory image (do not retain evicted
        # lessons).
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
        # Evaluator version registry: append the current version (audit; idempotent).
        if state.evaluator_version:
            self.conn.execute(
                "INSERT OR IGNORE INTO reflexion_evaluator "
                "(run_id, version, epoch) VALUES (?, ?, ?)",
                (run_id, state.evaluator_version, state.epoch),
            )
        # Write settled scalars to reflexion_run (canonical resume seed).
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
        """Settle the **settled state + final status** when the outer loop ends (DBReflexionLog terminal wiring).

        Records ``status`` (``converged`` / ``stopped`` / ``paused``) and the triggering condition /
        reason, and also rewrites the settled scalars + memory + evaluator version from
        ``result.state`` through :meth:`_flush_settled` (same policy as the inner
        :meth:`~loop_agent.store.LoopStore.record_result` folding final aggregation). This ensures
        that even if resume tail-boundary recovery stops before completing any episode, the
        post-recovery state is reliably reflected in the DB and matches the return value. ``paused``
        is not terminal, so ``ended_at`` is not set (resume can continue).
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

    # -- reads ------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return the ``reflexion_run`` row as a dict (or ``None`` if absent)."""
        row = self.conn.execute(
            "SELECT * FROM reflexion_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def read_episodes(self, run_id: str) -> list[dict[str, Any]]:
        """Return episode rows for ``run_id`` as dicts in episode order (signal/lesson decoded)."""
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
        """Return the registered evaluator-version history for ``run_id`` in registration order."""
        rows = self.conn.execute(
            "SELECT version, epoch, first_seen_at FROM reflexion_evaluator "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


class DBReflexionLog:
    """DB-backed outer Reflexion progress log, counterpart to inner :class:`~loop_agent.store.DBProgressLog`.

    ``db`` may be a file path (internally opened with :func:`~loop_agent.store.connect`, owned by
    this object, and closed by :meth:`close`) or an existing ``sqlite3.Connection`` (borrowed, not
    closed here). Construction calls ``load_or_init(run_id)`` and keeps :attr:`state` (empty for a
    new run, restored intermediate state for an existing run). This is the **resume entry point**:

        log = DBReflexionLog("outer.db", "run-1")
        result = run_reflexion(
            ..., initial_state=log.state, memory=log.memory, persist=log.on_episode,
        )
        log.record_result(result)   # Optional (terminal metadata audit)

    With this wiring, an interrupted outer loop can continue from the middle with its epoch
    progress, admitted lessons, and evaluator version intact (for a new run, ``state`` is empty, so
    this is equivalent to a fresh start and the same wiring works). Supplying ``memory`` sets the
    memory capacity policy for a fresh run (on resume, saved DB values take precedence).
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
        # Keep the restored (or empty for a new run) ReflexionState as the resume seed.
        self.state = self.store.load_or_init(run_id, memory=memory)

    @property
    def memory(self) -> EpisodicMemory:
        """Live memory from the resume seed (pass directly to ``run_reflexion(memory=...)``)."""
        return self.state.memory

    def on_episode(self, record: EpisodeRecord, state: ReflexionState) -> None:
        """Persist a settled episode. Pass to ``run_reflexion(persist=...)``."""
        self.store.persist_episode(self.run_id, record, state)

    def record_result(self, result: "ReflexiveResult") -> None:
        """Settle the final status when the outer loop ends (optional)."""
        self.store.record_result(self.run_id, result)

    def close(self) -> None:
        """Close only connections opened by this object (borrowed connections remain caller-owned)."""
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
