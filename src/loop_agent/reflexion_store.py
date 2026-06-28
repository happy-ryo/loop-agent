"""Source of truth (SoT) for outer Reflexion loop state: persists epoch/lesson/evaluator version to state.db (Issue #29).

Building on the MVP inner resume (:meth:`loop_agent.store.LoopStore.load_or_init` / Issue #14) and store lease
mechanism (Issue #21), this module persists the **inter-trial learning state** of the outer
:func:`loop_agent.reflexion.run_reflexion` (epoch progress, episodic memory lessons, and fixed evaluator version)
to SQLite, enabling **resumption from the point of interruption** even after restart (report.md S4.4/S5 Phase3 follow-up).

Design boundaries (following the minimal schema philosophy of store.py):

- **Independence from inner schema, additive only**: Leaves existing ``run / step / event / stop_reason / pending_decision``
  untouched and **non-destructively adds** 4 tables (``reflexion_run / reflexion_episode / reflexion_lesson / reflexion_evaluator``)
  via ``IF NOT EXISTS``. Opening an old DB preserves existing data; only missing tables are created at class instantiation
  (:meth:`ReflexionStore.__init__`).
- **Settled state as SoT**: Calls :meth:`ReflexionStore.persist_episode` from the ``persist`` hook of
  :func:`~loop_agent.reflexion.run_reflexion` (fires *after* epoch boundary processing), bundling one episode's
  "episode row + all memory lesson rows + reflexion_run scalars + evaluator version registration" into **a single
  transaction** (same atomic-boundary policy as inner :meth:`LoopStore.record_step`). Crash mid-persist means the
  episode is as-if it never happened; resume continues from the previous confirmed episode.
- **Evaluator version registry + fail-loud**: Appends the evaluator version fixed at each epoch to ``reflexion_evaluator``
  (audit), maintaining the current version in ``reflexion_run``. On resume, if the restored ``evaluator_version`` differs
  from the passed ``evaluator.version``, :func:`~loop_agent.reflexion.run_reflexion` **fails loudly** (callables cannot
  be serialized, so silent replacement with another evaluator is avoided; follows the safety core established in PR #28).
  This module simply shuttles versions faithfully; it does not opine on adoption criteria or dual-signal models.

Memory capacity policy (cap / per_lesson_chars / render_byte_cap) is also stored in ``reflexion_run``;
at restore, an :class:`EpisodicMemory` is reconstructed with the same limits (eviction behavior matches across resume boundaries).
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

if TYPE_CHECKING:  # for type annotations only (avoid runtime import cycle)
    from .reflexion import ReflexiveResult

# Additional schema for the outer loop: self-contained 4 tables independent of the inner 4 tables.
# All use ``IF NOT EXISTS`` for idempotency and non-destructiveness (creates only missing tables
# without damaging existing data in old DBs).
#
# - reflexion_run      : one row per outer run. Source of truth for settled scalar state (episode/epoch/evaluator version/
#                        best/budget counter/declared_keys/memory capacity). Seed for resume.
# - reflexion_episode  : one row per confirmed episode (audit + restore source). UNIQUE(run_id, episode) for
#                        idempotent re-execution. signal/lesson stored as JSON.
# - reflexion_lesson   : **current full set of lessons** from episodic memory (fully replaced per persist).
#                        Uses delete+insert to ensure evicted lessons don't linger in DB. position maintains order.
# - reflexion_evaluator: append-only registry of evaluator versions fixed at each epoch (audit of version registry).
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
    """Encode :class:`GroundTruthSignal` to strictly-valid JSON (for round-trip).

    ``allow_nan=False`` **loudly rejects** non-finite scores (NaN/Infinity): if unrestorable values are
    silently dropped, post-resume aggregation and convergence checks would be incorrect, so we fail fast
    at persist time instead (differs from observation's best-effort persistence — this is a control-path value).
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
    """Inverse of :func:`_encode_signal`. Returns ``components`` as a plain dict (Score comparison works with dicts)."""
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
    """Writer/reader for outer Reflexion state (counterpart to inner :class:`~loop_agent.store.LoopStore`).

    ``conn`` can be either a connection returned by :func:`~loop_agent.store.connect` or a borrowed
    connection from plain ``sqlite3.connect()``. To work with borrowed connections, at instantiation
    we defensively call :func:`~loop_agent.store._init_connection` to idempotently apply PRAGMA settings
    (row_factory=Row / isolation_level=None / foreign_keys=ON / WAL) and inner schema (same policy as
    :class:`~loop_agent.store.LoopStore`), then non-destructively add the outer schema (:data:`_REFLEXION_SCHEMA`)
    (old DBs get only missing tables; existing inner data stays intact). Without ``row_factory``, all reads
    break on column name access; without ``foreign_keys``, ``ON DELETE CASCADE`` doesn't work, so this
    normalization is mandatory. All writes are atomic under :meth:`_transaction`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # Idempotently apply PRAGMA + row_factory even for borrowed raw connections so resume reads don't break,
        # then add the additive schema (IF NOT EXISTS). Both _init_connection and executescript perform implicit
        # COMMIT, which is safe since no transaction is open at instantiation time.
        _init_connection(conn)
        conn.executescript(_REFLEXION_SCHEMA)

    # -- transaction control (same participation protocol as LoopStore.transaction) ----------

    @contextmanager
    def _transaction(self) -> Iterator["ReflexionStore"]:
        """``BEGIN IMMEDIATE`` -> yield -> ``COMMIT`` (``ROLLBACK`` on exception).

        Participates in existing outer transaction if already open (SQLite doesn't allow nested BEGIN).
        Uses ``BEGIN IMMEDIATE`` to acquire write lock from the start, avoiding WAL's ``SQLITE_BUSY_SNAPSHOT``
        during read-to-write promotion in load_or_init (same policy as inner store).
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
        """Provision the outer run row for ``run_id`` and return its current :class:`ReflexionState`.

        - New ``run_id``: creates a ``reflexion_run`` row and returns an empty :class:`ReflexionState`.
          If ``memory`` is provided, stores its capacity policy and uses **that live object** as the state's
          memory (``None`` defaults to :class:`EpisodicMemory`). A fresh run passed to
          ``run_reflexion(initial_state=..., memory=...)`` is empty, so start and initialization are equivalent.
        - Existing ``run_id``: **reconstructs** :class:`ReflexionState` from ``reflexion_run`` scalars
          + ``reflexion_episode`` + ``reflexion_lesson`` rows. Passing it to
          ``run_reflexion(initial_state=state, memory=state.memory, persist=...)`` enables **resumption**
          from the interruption point (#29). If restored ``evaluator_version`` / ``declared_keys`` differ from
          the passed evaluator / axes, run_reflexion fails loudly (preserves coherence at the seam).

        Creation/restoration is atomic in a single transaction.
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
        """Assemble :class:`ReflexionState` from persisted rows (resume restoration)."""
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
            # gt_aggregate_history includes **only ground_truth_backed** episodes (same rule as driver).
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
            # Restore list directly without going through admit (validation, eviction, dedup are already done at persist time.
            # Going through admit would re-evict excess lessons, causing divergence from saved content).
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
        """Atomically persist a confirmed episode (compatible with run_reflexion's ``persist`` hook).

        Bundles "episode row upsert + full memory lesson replacement + reflexion_run scalar update +
        evaluator version registration" into one transaction. ``state`` should be **settled state after boundary processing**
        (run_reflexion does this) — this way, persisted epoch / evaluator_version / evaluator_updates are
        post-boundary, making resume consistent with straight-through execution.

        On ``UNIQUE(run_id, episode)`` collision, ``DO UPDATE`` overwrites, so re-persisting the same episode
        (re-run after resume) is idempotent. Memory may shrink from eviction, so we **fully replace via delete+insert**
        rather than append (DB exactly mirrors memory's current state).
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
            # New episode confirmed = run is running. If previously record_result logged stopped/paused
            # and then resumed, reset terminal metadata (status/stop_name/reason/ended_at) to running.
            # Otherwise get_run reports an advanced run as stale stopped with old ended_at, breaking reflexion_run
            # as the SoT for lifecycle.
            self.conn.execute(
                "UPDATE reflexion_run SET status = 'running', stop_name = NULL, "
                "reason = '', ended_at = NULL WHERE run_id = ?",
                (run_id,),
            )

    def _flush_settled(self, run_id: str, state: ReflexionState) -> None:
        """Flush settled state (full memory snapshot + evaluator version + reflexion_run scalars).

        Common core called from both ``persist_episode`` (on episode confirmation) and ``record_result``
        (on terminal confirmation). **Assumes caller holds the transaction** (atomic boundary is caller's responsibility).
        Memory may shrink from eviction, so we **fully replace via delete+insert** rather than append, making DB
        exactly mirror memory's current state. Calling from both ensures that if resume's tail-boundary recovery
        stops without completing an episode (e.g., immediately hitting ``EvaluatorUpdateBudget`` in recovery's promotion),
        ``record_result`` still reliably persists post-recovery epoch / evaluator_version / evaluator_updates
        (return value and DB stay in sync, avoiding double-hitting promotions on re-resume).
        """
        best = state.best_gt_aggregate
        best_db = None if best == float("-inf") else best
        declared_json = json.dumps(list(state.declared_keys))
        # Fully replace lesson table with memory's current snapshot (don't leave evicted lessons behind).
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
        # Evaluator version registry: append current version (audit, idempotent).
        if state.evaluator_version:
            self.conn.execute(
                "INSERT OR IGNORE INTO reflexion_evaluator "
                "(run_id, version, epoch) VALUES (?, ?, ?)",
                (run_id, state.evaluator_version, state.epoch),
            )
        # Write settled scalars to reflexion_run (source of truth for resume seed).
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
        """Lock in **settled state + final status** at outer loop termination (wiring for DBReflexionLog terminus).

        Records ``status`` (``converged`` / ``stopped`` / ``paused``) and its trigger / reason; also
        re-flushes settled scalars + memory + evaluator version from ``result.state`` via :meth:`_flush_settled`
        (mirrors how inner :meth:`~loop_agent.store.LoopStore.record_result` folds final tally). This ensures that
        if resume's tail-boundary recovery stops without completing an episode, post-recovery state is reliably
        reflected in DB with return value and DB in sync. ``paused`` is not terminal, so ``ended_at`` is not set
        (resume can continue).
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

    # -- reading ---------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return ``reflexion_run`` row as dict, or ``None`` if absent."""
        row = self.conn.execute(
            "SELECT * FROM reflexion_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def read_episodes(self, run_id: str) -> list[dict[str, Any]]:
        """Return episode rows for ``run_id`` as list of dicts in episode order (signal/lesson decoded)."""
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
        """Return registration history of evaluator versions fixed at ``run_id`` in registration order (version registry)."""
        rows = self.conn.execute(
            "SELECT version, epoch, first_seen_at FROM reflexion_evaluator "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]


class DBReflexionLog:
    """DB-backed progress log for outer Reflexion (counterpart to inner :class:`~loop_agent.store.DBProgressLog`).

    ``db`` accepts either a file path (internally opened via :func:`~loop_agent.store.connect`, owned and closed
    in :meth:`close`) or an existing ``sqlite3.Connection`` (borrowed; not closed in close). At instantiation,
    calls ``load_or_init(run_id)`` to populate :attr:`state` (empty if new, or restored mid-run state if existing).
    This is the **resume entry point**:

        log = DBReflexionLog("outer.db", "run-1")
        result = run_reflexion(
            ..., initial_state=log.state, memory=log.memory, persist=log.on_episode,
        )
        log.record_result(result)   # optional (audit terminal metadata)

    With this wiring, an interrupted outer loop resumes from mid-point with epoch progress, adopted lessons,
    and evaluator version preserved (for new runs, ``state`` is empty, so fresh start applies; same wiring works).
    Pass ``memory`` to specify memory capacity policy for fresh runs (on resume, DB's saved value takes precedence).
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
        # Hold the restored (or empty if new) ReflexionState as resume seed.
        self.state = self.store.load_or_init(run_id, memory=memory)

    @property
    def memory(self) -> EpisodicMemory:
        """Live memory from resume seed (pass directly to ``run_reflexion(memory=...)``).
        """
        return self.state.memory

    def on_episode(self, record: EpisodeRecord, state: ReflexionState) -> None:
        """Persist confirmed episode. Pass to ``run_reflexion(persist=...)``."""
        self.store.persist_episode(self.run_id, record, state)

    def record_result(self, result: "ReflexiveResult") -> None:
        """Lock in final status at outer loop termination (optional)."""
        self.store.record_result(self.run_id, result)

    def close(self) -> None:
        """Close only self-opened connections (caller's responsibility for borrowed connections)."""
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
