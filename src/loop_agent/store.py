"""Loop state SoT: minimal SQLite schema + transaction persistence (Issue #11).

The PoC :mod:`loop_agent.progress` provided "minimal state" by appending each
iteration to JSON Lines (report.md S5 Phase 1). This module promotes that to
the Phase 2 **state.db SoT** (report.md S3.4 / S4.6 / S5 Phase 2): it persists
one loop run's progress to SQLite *atomically* and makes it the single source of
truth that survives across processes.

Design boundaries (most important, report.md S6 "state.db extraction depth"):

- **Do not tightly couple to the org core**. This was adapted from
  claude-org-ja's ``tools/state_db``, but this schema is a self-contained
  minimal schema with only the four ``run / step / event / stop_reason`` tables.
  It does not depend on org-side projects / workstreams / worker_dirs or
  snapshotter / dashboard integrations. ``connect`` is enough to create and use it.
- **Make transaction the only atomic boundary** (report.md R4). :class:`LoopStore`
  provides an explicit StateWriter-style ``transaction()`` and groups each step's
  "step row + aggregates + journal event" into one transaction. If the process
  crashes midway (= exits before commit), the whole step never happened and no
  partial rows remain.
- **Authoritative resume state** (Issue #14). :meth:`LoopStore.load_or_init`
  ensures the run row exists and, for an existing run, restores and returns a
  :class:`LoopState` from persisted steps. Passing this to
  ``run_loop(initial_state=...)`` (or :attr:`DBProgressLog.state`) resumes an
  interrupted loop without losing state. Observations are stored as JSON, so
  restoration goes through a JSON round-trip (see the ``load_or_init`` /
  ``run_loop`` docstrings for type-fidelity limits).

The JSONL :class:`~loop_agent.progress.ProgressLog` remains available alongside
this (see README) because it is still valuable as a dependency-free, readable
PoC artifact. :class:`DBProgressLog` implements the same ``on_step`` /
``record_result`` signatures as ``ProgressLog``, so changing only the observation
hook can move the SoT to the DB.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional, Union

from .errors import ConfigError, StateError
from .progress import _to_jsonable
from .state import LoopState, StepRecord

if TYPE_CHECKING:  # Avoid runtime import cycles; only needed for annotations.
    from .loop import LoopResult

# Schema version. Bump for backward-incompatible changes, not for table additions.
# Store it in PRAGMA user_version without a dedicated table to preserve the
# "minimal four tables" shape.
# v2 (Issue #21, Phase3): add in-progress leases to pending_decision
# (executing state + lease_owner / lease_expires_at). Existing DBs are migrated
# non-destructively by :func:`_migrate_schema` after inspecting the actual schema
# (the version is informational).
SCHEMA_VERSION = 2

# Minimal loop schema. Independent of the org core and self-contained.
# ``IF NOT EXISTS`` makes it idempotent.
#
# - run             : One row per run. Authoritative final status and aggregates
#                     (iterations / tokens / elapsed).
# - step            : One row per completed iteration. UNIQUE(run_id, iteration)
#                     makes re-execution idempotent (the basis for resume #14).
#                     observation is stored as a JSON string.
# - event           : Append-only journal (report.md R7 observation). Records
#                     loop_begin / loop_step / loop_end / loop_gate so all stop
#                     reasons and human-gate triggers/decisions can be analyzed later.
# - stop_reason     : 1:1 with run. The triggered stop condition (name) and
#                     reason, or goal achievement.
# - pending_decision: Decision registry for limited human gates (Issue #15,
#                     report.md S4.5 / R6). One row per trigger on an irreversible
#                     operation. UNIQUE(run_id, gate_key) makes it idempotent.
#                     It persists pending -> resolved(approve|edit|reject|respond)
#                     -> executed and keeps decisions across pause/resume. It reuses
#                     claude-org's pending_decisions state machine with roles remapped:
#                     "secretary registers a worker's judgment request and resolves it
#                     from the user response" maps to "loop registers an irreversible
#                     action and a human resolves it". Because the response is direct,
#                     the intermediate escalated state is collapsed into resolved.
#                     executed is added to guarantee at-most-once execution for
#                     irreversible approve/edit actions (replay resume = the path that
#                     replays from iteration 0 with fresh state skips executed gates and
#                     prevents retriggers. #14 initial_state resume continues from the
#                     interrupted iteration, so it does not revisit executed gates).
#                     Phase3 (#21): split resolved -> executing -> executed into
#                     multiple stages and coordinate concurrent multi-process resume
#                     with lease_owner / lease_expires_at (in-progress lease;
#                     :meth:`LoopStore.acquire_lease` /
#                     :meth:`~LoopStore.complete_execution`).
_SCHEMA_CORE = """
CREATE TABLE IF NOT EXISTS run (
  run_id       TEXT PRIMARY KEY,
  status       TEXT NOT NULL DEFAULT 'running'
               CHECK (status IN ('running','goal_met','stopped')),
  goal_met     INTEGER NOT NULL DEFAULT 0 CHECK (goal_met IN (0,1)),
  iterations   INTEGER NOT NULL DEFAULT 0,
  tokens_used  INTEGER NOT NULL DEFAULT 0,
  elapsed      REAL NOT NULL DEFAULT 0.0,
  started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS step (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  iteration    INTEGER NOT NULL,
  tokens       INTEGER NOT NULL DEFAULT 0,
  tokens_used  INTEGER NOT NULL DEFAULT 0,
  elapsed      REAL NOT NULL DEFAULT 0.0,
  goal_met     INTEGER NOT NULL DEFAULT 0 CHECK (goal_met IN (0,1)),
  detail       TEXT NOT NULL DEFAULT '',
  observation  TEXT CHECK (observation IS NULL OR json_valid(observation)),
  recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (run_id, iteration)
);
CREATE INDEX IF NOT EXISTS idx_step_run ON step(run_id);

CREATE TABLE IF NOT EXISTS event (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id       TEXT REFERENCES run(run_id) ON DELETE CASCADE,
  occurred_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  kind         TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload))
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind);

CREATE TABLE IF NOT EXISTS stop_reason (
  run_id       TEXT PRIMARY KEY REFERENCES run(run_id) ON DELETE CASCADE,
  status       TEXT NOT NULL CHECK (status IN ('goal_met','stopped')),
  name         TEXT,
  reason       TEXT NOT NULL DEFAULT '',
  recorded_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# Keep the pending_decision DDL standalone so :func:`_migrate_schema` can reuse the
# same definition when rebuilding the table (prevents column-definition drift).
# The table name appears only once, so replacing the ``CREATE TABLE`` name is enough
# to create a temporary migration table (FK references run, and CHECK / UNIQUE use
# only column names without mentioning the table itself).
#
# Multi-stage status (Phase3 #21): pending -> resolved -> executing -> executed.
# executing means the process currently running the irreversible approve/edit action
# holds a lease, with lease_owner (holder token) and lease_expires_at (epoch seconds,
# REAL). Even if the same gate is resumed concurrently, only one process can succeed
# at resolved->executing (single winner). Losers see executing and pause until
# executed (ordering consistency). If the winner crashes, lease_expires_at expiry lets
# another process reacquire the lease (= prevents missing steps).
_PENDING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS pending_decision (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
  gate_key         TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','resolved','executing','executed')),
  decision         TEXT CHECK (decision IS NULL OR
                     decision IN ('approve','edit','reject','respond')),
  action           TEXT CHECK (action IS NULL OR json_valid(action)),
  payload          TEXT CHECK (payload IS NULL OR json_valid(payload)),
  lease_owner      TEXT,
  lease_expires_at REAL,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at      TEXT,
  executed_at      TEXT,
  -- Non-pending rows (resolved/executing/executed) must have a decision.
  CHECK (status = 'pending' OR decision IS NOT NULL),
  -- executing rows must have a lease holder (required for expiry checks).
  CHECK (status <> 'executing' OR lease_owner IS NOT NULL),
  UNIQUE (run_id, gate_key)
);
"""

_PENDING_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_run ON pending_decision(run_id);\n"
)

# Full schema = core four tables + pending_decision + its index. ``connect`` /
# :class:`LoopStore` apply this idempotently with executescript.
SCHEMA = _SCHEMA_CORE + _PENDING_TABLE_DDL + _PENDING_INDEX_DDL

# event.kind values. Constants let readers filter without hard-coded string literals.
EVENT_BEGIN = "loop_begin"
EVENT_STEP = "loop_step"
EVENT_END = "loop_end"
# Record human-gate triggers (pending) / decisions (resolved) in the journal
# (report.md R6/R7).
EVENT_GATE = "loop_gate"

# The four decisions a human can make for a limited human gate (LangGraph interrupt
# parity: report.md S4.5 / S2.6). approve=run as-is / edit=modify and run /
# reject=do not run and record rejection / respond=do not run and return a response.
DECISION_KINDS = ("approve", "edit", "reject", "respond")

# Acquisition outcomes for in-progress leases (Issue #21; outcome from
# :meth:`LoopStore.acquire_lease`).
# - ACQUIRED: This process acquired the lease (= it may run the irreversible action).
# - WAIT    : Another process is running under a valid lease. Wait until executed
#             (losers pause).
# - EXECUTED: Already executed. Safe to skip (do not run twice).
LEASE_ACQUIRED = "acquired"
LEASE_WAIT = "wait"
LEASE_EXECUTED = "executed"

# Default lease TTL (seconds). Keep it long enough for one irreversible action; if it
# is too short, the lease can expire while the winner is still running and another
# process can take it, causing double execution. HumanGate / run_gated_loop can
# override this.
DEFAULT_LEASE_TTL = 30.0

DbSource = Union[str, "os.PathLike[str]", sqlite3.Connection]


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open the loop state DB, creating it if needed, apply the schema, and return it.

    ``path`` may be a regular file path or ``":memory:"``. See
    :func:`_init_connection` for details (schema application + PRAGMA + row_factory).
    """
    return _init_connection(sqlite3.connect(str(path)))


def _init_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply schema and PRAGMAs to a connection and return it (idempotent).

    Called by both :func:`connect` and :class:`LoopStore`. The latter calls this
    **defensively** so it still works when given a borrowed connection opened by
    plain ``sqlite3.connect()`` (same policy as org's StateWriter). The schema uses
    ``IF NOT EXISTS`` and the PRAGMAs are idempotent, so reapplying this to an
    initialized connection is safe.

    - ``isolation_level = None`` (autocommit): transactions are fully controlled
      by explicit ``BEGIN`` / ``COMMIT`` in :meth:`LoopStore.transaction`
      (StateWriter-style control that does not rely on sqlite3's default implicit
      transactions).
    - ``row_factory = sqlite3.Row``: allow reads by column name.
    - ``foreign_keys = ON``: required to CASCADE child rows when deleting ``run``.
    - ``busy_timeout``: wait for locks during concurrent access.
    - ``journal_mode = WAL``: reduces writer/reader conflicts (file DBs only;
      ignored for ``:memory:``).
    """
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    # Fresh DBs are created with the new schema. Existing DBs keep old tables, so
    # idempotently migrate by inspecting the actual schema and adding lease columns
    # plus executing status non-destructively. Do this *before* enabling foreign_keys
    # because the migration toggles FKs.
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Migrate existing ``pending_decision`` tables to the v2 lease schema safely.

    A ``pending_decision`` created by v1 has a status CHECK that does not allow
    ``executing`` and lacks ``lease_owner`` / ``lease_expires_at`` columns. SQLite
    cannot change CHECK constraints with ``ALTER TABLE``, so create a table with the
    new definition, copy rows, and swap it in (SQLite's official table-rebuild
    procedure). Because this runs on every ``connect``, it inspects the actual table
    definition and **does nothing if the new schema is already present** (no-op for
    fresh or already migrated DBs).

    The rebuild temporarily disables ``foreign_keys`` and runs atomically in a
    single transaction (so partial tables are not left on failure). No child table
    references ``pending_decision``, so references do not need to be rewired.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_decision'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return  # Table does not exist yet (theoretically unreachable after executescript).
    table_sql = row["sql"]
    # If both markers of the new schema (executing status and lease column) exist,
    # migration is already complete.
    if "'executing'" in table_sql and "lease_owner" in table_sql:
        return
    mig_ddl = _PENDING_TABLE_DDL.replace(
        "CREATE TABLE IF NOT EXISTS pending_decision",
        "CREATE TABLE pending_decision_mig",
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Drop a leftover temp table first so a rebuild can retry after a previous
        # interruption (CREATE is not IF NOT EXISTS, so a leftover would make connect
        # fail permanently). DROP/CREATE/INSERT/DROP/RENAME all commit atomically
        # inside this single transaction (SQLite DDL is transactional). A mid-flight
        # crash rolls the whole thing back on the next open, leaving the old
        # pending_decision intact for retry (no data loss in an intermediate state).
        conn.execute("DROP TABLE IF EXISTS pending_decision_mig")
        # Use execute because executescript would break the manual transaction with
        # an implicit COMMIT (mig_ddl / index are each single statements).
        conn.execute(mig_ddl)
        # Explicitly copy every old-table column (the two new lease columns keep the
        # default NULL).
        conn.execute(
            "INSERT INTO pending_decision_mig "
            "(id, run_id, gate_key, status, decision, action, payload, "
            " created_at, resolved_at, executed_at) "
            "SELECT id, run_id, gate_key, status, decision, action, payload, "
            "       created_at, resolved_at, executed_at "
            "FROM pending_decision"
        )
        conn.execute("DROP TABLE pending_decision")
        conn.execute("ALTER TABLE pending_decision_mig RENAME TO pending_decision")
        conn.execute(_PENDING_INDEX_DDL)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _finite_safe(value: Any) -> Any:
    """Convert non-finite floats (NaN/Infinity) in ``_to_jsonable`` output to ``repr``.

    Non-finite floats are valid as a JSON *type*, but the default ``json.dumps``
    behavior (allow_nan=True) emits **invalid JSON tokens** such as ``NaN`` /
    ``Infinity``. That makes SQLite ``json_valid()`` return 0, trips the
    ``step.observation`` / ``event.payload`` CHECK constraints (IntegrityError), and
    rolls back persistence for the whole step (violating the contract that one odd
    observation should not break all persistence). Replace them with ``repr`` strings
    ('nan' / 'inf' / '-inf') before ``json.dumps`` sees them, so only strictly valid
    JSON is saved. Recurses assuming input has already passed through ``_to_jsonable``
    (only None/bool/int/float/str/list/dict).
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return [_finite_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _finite_safe(v) for k, v in value.items()}
    return value


def _encode_observation(observation: Any) -> str:
    """Encode an observation as a *strictly valid* JSON string.

    :func:`loop_agent.progress._to_jsonable` converts non-JSON-native values to
    ``repr``, then :func:`_finite_safe` also converts non-finite floats to ``repr``
    before ``json.dumps`` (``allow_nan=False`` prevents misses). One odd observation
    should not break all persistence (no json_valid CHECK violation).
    """
    return json.dumps(
        _finite_safe(_to_jsonable(observation)),
        ensure_ascii=False,
        allow_nan=False,
        default=repr,
    )


def _require_json_native(value: Any, what: str) -> str:
    """JSON-encode ``value`` but reject it unless the round trip is lossless.

    :func:`_encode_observation` collapses non-JSON-native values (arbitrary objects /
    tuples / NaN, etc.) to ``repr`` or similar for best-effort observation
    persistence. Allowing that for values that human gates **execute / compare for
    identity** (gated action and edit replacement action) can make ``(1, 2)`` become
    ``[1, 2]`` and falsely match a different action, or execute an object as a
    ``'<x>'`` string. Values that do not match after encode->decode (= lose
    fidelity) are rejected loudly with ``ConfigError`` on the spot (safety-sensitive
    values require stricter fidelity).
    """
    encoded = _encode_observation(value)
    if json.loads(encoded) != value:
        raise ConfigError(
            f"{what} must be JSON-native (round-trippable) so it survives "
            f"persistence/comparison losslessly; got {value!r} which does not. "
            "Use str/int/float/bool/None/list/dict."
        )
    return encoded


class LoopStore:
    """Connection-bound loop-state writer/reader with StateWriter-style transactions.

    ``conn`` may be a connection returned by :func:`connect` or a borrowed
    connection opened with plain ``sqlite3.connect()``. To support the latter, the
    constructor defensively calls :func:`_init_connection` and idempotently applies
    the schema + PRAGMAs + row_factory (same policy as org's StateWriter). All writes
    happen atomically under :meth:`transaction`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        _init_connection(conn)

    # -- transaction control ------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["LoopStore"]:
        """``BEGIN IMMEDIATE`` -> yield -> ``COMMIT`` (``ROLLBACK`` and re-raise on error).

        If already inside an outer transaction, *join* it instead of issuing a new
        ``BEGIN`` (sqlite raises ``OperationalError`` for nested ``BEGIN``). This
        allows callers to further group :meth:`record_step` and similar calls inside
        their own ``transaction()`` so multiple steps form one atomic unit. Joined
        inner blocks do not commit/rollback; final commit/rollback is delegated to
        the outermost ``transaction()``.

        ``BEGIN IMMEDIATE`` takes the write lock *from the start*. :meth:`load_or_init`
        does write-after-read by selecting and then inserting, so the default DEFERRED
        ``BEGIN`` can hit ``SQLITE_BUSY_SNAPSHOT`` (``database is locked``) when
        promoting read->write under WAL. ``busy_timeout`` cannot wait this out; it
        fails immediately (surfaced by cross-process resume #14). Every
        ``transaction()`` in this class is for writes, so IMMEDIATE avoids promotion
        and makes ``busy_timeout`` lock waiting actually apply.
        """
        if self.conn.in_transaction:
            # Join the outer transaction. Commit/rollback is left to the outermost one.
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

    # -- internal helpers ---------------------------------------------------

    def _append_event(
        self, run_id: str, kind: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        """Append one event to the journal (append-only)."""
        # As with observations, convert non-finite floats to repr to avoid
        # event.payload json_valid CHECK violations (current payloads are finite, but
        # keep this defensive behavior consistent).
        payload_json = json.dumps(
            _finite_safe(_to_jsonable(payload or {})),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            default=repr,
        )
        self.conn.execute(
            "INSERT INTO event (run_id, kind, payload) VALUES (?, ?, ?)",
            (run_id, kind, payload_json),
        )

    def _bump_run(self, run_id: str, state: LoopState) -> None:
        """Update the run row aggregates to match the current :class:`LoopState`."""
        self.conn.execute(
            "UPDATE run SET iterations = ?, tokens_used = ?, elapsed = ?, "
            "goal_met = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE run_id = ?",
            (
                state.iteration,
                state.tokens_used,
                state.elapsed,
                int(bool(state.goal_met)),
                run_id,
            ),
        )

    # -- run lifecycle ------------------------------------------------------

    def load_or_init(self, run_id: str) -> LoopState:
        """Ensure the run row for ``run_id`` exists and return the current state.

        - New ``run_id``: create a ``run`` row with ``status='running'``, record one
          ``loop_begin`` event, and return an empty :class:`LoopState` (all counters 0).
        - Existing ``run_id``: *restore* and return :class:`LoopState` from persisted
          ``step`` rows (history, iteration, tokens_used, elapsed, goal_met). Passing
          this to ``run_loop(initial_state=...)`` can **resume** from the interruption
          point (#14).

        Creation/restoration is atomic in one transaction.

        The restored ``history`` ``observation`` values are JSON round-tripped from
        their saved form (:func:`loop_agent.progress._to_jsonable` coerces tuple->list,
        non-JSON-native type -> repr string, and dict key -> str). Therefore,
        state-based conditions that use observations directly as *keys* (especially
        the default key for :class:`~loop_agent.conditions.NoProgress`) should use
        JSON-stable observation types or pass a ``key`` that projects to a JSON-stable
        signature (see ``initial_state`` in :func:`loop_agent.loop.run_loop` for details).
        """
        if not run_id:
            raise ConfigError("load_or_init: run_id must be a non-empty string")
        with self.transaction():
            row = self.conn.execute(
                "SELECT run_id FROM run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO run (run_id, status) VALUES (?, 'running')",
                    (run_id,),
                )
                self._append_event(run_id, EVENT_BEGIN, {"run_id": run_id})
                return LoopState()
            return self._reconstruct_state(run_id)

    def _reconstruct_state(self, run_id: str) -> LoopState:
        """Build :class:`LoopState` from persisted ``step`` rows (resume restoration).

        iteration / tokens_used / elapsed / goal_met use the run-row aggregates as
        authoritative; ``history`` is restored from step rows in iteration order as
        :class:`StepRecord` instances.
        """
        run = self.conn.execute(
            "SELECT iterations, tokens_used, elapsed, goal_met FROM run "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        history: list[StepRecord] = []
        for s in self.conn.execute(
            "SELECT iteration, tokens, goal_met, detail, observation FROM step "
            "WHERE run_id = ? ORDER BY iteration",
            (run_id,),
        ):
            observation = (
                json.loads(s["observation"]) if s["observation"] is not None else None
            )
            history.append(
                StepRecord(
                    iteration=s["iteration"],
                    observation=observation,
                    tokens=s["tokens"],
                    goal_met=bool(s["goal_met"]),
                    detail=s["detail"],
                )
            )
        return LoopState(
            iteration=run["iterations"],
            tokens_used=run["tokens_used"],
            elapsed=run["elapsed"],
            goal_met=bool(run["goal_met"]),
            history=history,
        )

    # -- per-step persistence -----------------------------------------------

    def record_step(
        self, run_id: str, record: StepRecord, state: LoopState
    ) -> None:
        """Persist one completed iteration atomically (compatible with run_loop ``StepHook``).

        One transaction groups "step-row upsert + run aggregate update + ``loop_step``
        event append". ``UNIQUE(run_id, iteration)`` conflicts are overwritten with
        ``DO UPDATE``, making re-execution of the same iteration idempotent
        (resume #14).

        A ``loop_step`` event is appended **only for a new insert or when
        re-persistence changes the content**. A pure replay (resume) that persists the
        exact same content for the same iteration does not materially change the step
        row or event, so it does not add another event. If the same iteration is
        rewritten with a *different result*, one event with the new content is
        appended. This lets the append-only journal both "avoid noise on identical
        replays" and "keep the latest event consistent with the step SoT (last event =
        current step row)".
        """
        obs_json = _encode_observation(record.observation)
        goal_int = int(bool(record.goal_met))
        with self.transaction():
            existing = self.conn.execute(
                "SELECT tokens, tokens_used, elapsed, goal_met, detail, "
                "observation FROM step WHERE run_id = ? AND iteration = ?",
                (run_id, record.iteration),
            ).fetchone()
            # Append an event for a new row, or when any content differs from existing.
            changed = existing is None or (
                existing["tokens"] != record.tokens
                or existing["tokens_used"] != state.tokens_used
                or existing["elapsed"] != state.elapsed
                or existing["goal_met"] != goal_int
                or existing["detail"] != record.detail
                or existing["observation"] != obs_json
            )
            self.conn.execute(
                "INSERT INTO step "
                "(run_id, iteration, tokens, tokens_used, elapsed, goal_met, "
                " detail, observation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, iteration) DO UPDATE SET "
                "  tokens = excluded.tokens, "
                "  tokens_used = excluded.tokens_used, "
                "  elapsed = excluded.elapsed, "
                "  goal_met = excluded.goal_met, "
                "  detail = excluded.detail, "
                "  observation = excluded.observation",
                (
                    run_id,
                    record.iteration,
                    record.tokens,
                    state.tokens_used,
                    state.elapsed,
                    goal_int,
                    record.detail,
                    obs_json,
                ),
            )
            self._bump_run(run_id, state)
            if changed:
                self._append_event(
                    run_id,
                    EVENT_STEP,
                    {
                        "iteration": record.iteration,
                        "tokens": record.tokens,
                        "tokens_used": state.tokens_used,
                        "elapsed": state.elapsed,
                        "goal_met": bool(record.goal_met),
                        "detail": record.detail,
                    },
                )

    def record_result(self, run_id: str, result: "LoopResult") -> None:
        """Atomically finalize the loop status at loop end.

        One transaction groups "``stop_reason`` row upsert + run-row end-state update
        (status / ended_at and final aggregates) + ``loop_end`` event append".
        ``stop_reason`` is 1:1 with run and idempotent on re-execution (``DO UPDATE``).

        ``status == "paused"`` (interrupted by a human gate) is **not terminal**: the
        run remains ``running`` and ``stop_reason`` is not written (resume can
        continue). Only aggregates are updated, and the pause is recorded in the
        journal as a ``loop_gate`` event. This allows paused results to be passed
        directly to ``DBProgressLog.record_result`` (without crashing on CHECK
        constraint violations).
        """
        if result.status == "paused":
            gate_key = (
                result.pending.get("gate_key")
                if isinstance(result.pending, dict)
                else None
            )
            with self.transaction():
                self._bump_run(run_id, result.state)
                self._append_event(
                    run_id, EVENT_GATE, {"status": "paused", "gate_key": gate_key}
                )
            return
        stop_name = result.stop.name if result.stop is not None else None
        with self.transaction():
            self.conn.execute(
                "INSERT INTO stop_reason (run_id, status, name, reason) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "  status = excluded.status, "
                "  name = excluded.name, "
                "  reason = excluded.reason, "
                "  recorded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (run_id, result.status, stop_name, result.reason),
            )
            self.conn.execute(
                "UPDATE run SET status = ?, goal_met = ?, iterations = ?, "
                "tokens_used = ?, elapsed = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "ended_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE run_id = ?",
                (
                    result.status,
                    int(bool(result.goal_met)),
                    result.iterations,
                    result.tokens_used,
                    result.elapsed,
                    run_id,
                ),
            )
            self._append_event(
                run_id,
                EVENT_END,
                {
                    "status": result.status,
                    "stop": stop_name,
                    "reason": result.reason,
                    "iterations": result.iterations,
                    "tokens_used": result.tokens_used,
                    "elapsed": result.elapsed,
                },
            )

    # -- reads --------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return the run row as a dict, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def read_steps(self, run_id: str) -> list[dict[str, Any]]:
        """Return ``run_id`` step rows as a list of dicts in iteration order.

        ``observation`` is decoded from the stored JSON.
        """
        rows = self.conn.execute(
            "SELECT * FROM step WHERE run_id = ? ORDER BY iteration", (run_id,)
        ).fetchall()
        steps: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["goal_met"] = bool(d["goal_met"])
            d["observation"] = (
                json.loads(d["observation"]) if d["observation"] is not None else None
            )
            steps.append(d)
        return steps

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        """Return ``run_id`` events as a list of dicts in occurrence order (ascending id).

        ``payload`` is decoded from JSON.
        """
        rows = self.conn.execute(
            "SELECT * FROM event WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            events.append(d)
        return events

    def get_stop_reason(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return the stop_reason row as a dict, or ``None`` if not finished."""
        row = self.conn.execute(
            "SELECT * FROM stop_reason WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    # -- limited human gate (pending_decision) ------------------------------

    @staticmethod
    def _decode_decision(row: sqlite3.Row) -> dict[str, Any]:
        """Decode a pending_decision row to a dict (restore action / payload from JSON)."""
        d = dict(row)
        d["action"] = json.loads(d["action"]) if d["action"] is not None else None
        d["payload"] = json.loads(d["payload"]) if d["payload"] is not None else None
        return d

    def request_decision(
        self, run_id: str, gate_key: str, action: Any
    ) -> dict[str, Any]:
        """Register a human gate for an irreversible action as ``pending`` (idempotent).

        Corresponds to org's ``pending_decisions.append`` (with roles remapped). If a
        row already exists for the same ``(run_id, gate_key)``, return it **without
        overwriting**. This keeps a post-pause resume that reevaluates the same action
        from destroying an already made decision (resolved) or registered pending
        request (= do not ask the human twice). A ``loop_gate`` event is appended only
        for a new registration.

        ``action`` must be **JSON-native (round-trip lossless)**. The gated action is
        used as the identity-comparison basis during resume (to prevent misapplication),
        and lossy encoding could falsely match a different action (see
        :func:`_require_json_native`).

        Use :meth:`register_decision` if callers need to know **whether this call
        inserted a new row** (= in concurrent registration races, only the first
        registrant should cause side effects such as notifications). This method only
        returns the authoritative current row.
        """
        row, _created = self.register_decision(run_id, gate_key, action)
        return row

    def register_decision(
        self, run_id: str, gate_key: str, action: Any
    ) -> tuple[dict[str, Any], bool]:
        """Register like :meth:`request_decision` and return ``(current row, created)``.

        ``created`` is ``True`` only when **this call inserted the pending row**.
        Reading an existing row (registered earlier by another process or a previous
        run by this process) returns ``False`` and the row unchanged. This lets callers
        avoid **triggering approval notifications twice** when a loser in a TOCTOU race
        sees ``None`` from ``get_decision`` but then receives the winner's row from
        ``request_decision`` and observes ``created=False`` (the trigger condition for
        :meth:`loop_agent.gate.HumanGate._notify_new_request`). INSERT is a
        single-winner operation inside the transaction, so exactly one concurrent
        registrant can observe ``created=True``.
        """
        if not gate_key:
            raise ConfigError("request_decision: gate_key must be a non-empty string")
        action_json = _require_json_native(action, "gated action")
        with self.transaction():
            existing = self.conn.execute(
                "SELECT * FROM pending_decision WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            if existing is not None:
                return self._decode_decision(existing), False
            self.conn.execute(
                "INSERT INTO pending_decision (run_id, gate_key, status, action) "
                "VALUES (?, ?, 'pending', ?)",
                (run_id, gate_key, action_json),
            )
            self._append_event(
                run_id,
                EVENT_GATE,
                {"gate_key": gate_key, "status": "pending"},
            )
            row = self.conn.execute(
                "SELECT * FROM pending_decision WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            return self._decode_decision(row), True

    def resolve_decision(
        self,
        run_id: str,
        gate_key: str,
        decision: str,
        payload: Any = None,
    ) -> dict[str, Any]:
        """Resolve a ``pending`` decision to ``resolved`` with the human's choice.

        Corresponds to org's ``pending_decisions.resolve``. ``decision`` is one of
        the four :data:`DECISION_KINDS`. ``payload`` carries the replacement action
        for ``edit`` or the response message for ``respond`` (JSON-encoded). Only
        ``pending`` rows can transition; already ``resolved`` rows raise
        ``StateError`` (terminal: a decision made once is not decided again). One
        ``loop_gate`` event is appended on resolution.

        ``edit`` ``payload`` must be **JSON-native (round-trip lossless)**. This
        payload is restored from the store during resume and becomes the *action to
        execute*, so allowing non-JSON-native values (arbitrary objects / tuples / NaN,
        etc.) could collapse them to repr strings and accidentally *execute a different
        action*. Check round-trip fidelity at record time and reject loudly on loss
        (observations are a best-effort journal and may be collapsed, but executable
        edits are intentionally strict).
        """
        if decision not in DECISION_KINDS:
            raise ConfigError(
                f"unknown decision {decision!r}; expected one of {DECISION_KINDS}"
            )
        if payload is None:
            payload_json = None
        elif decision == "edit":
            # edit replacement actions are restored on resume and *executed*, so they
            # must remain JSON-native.
            payload_json = _require_json_native(payload, "edit payload")
        else:
            # Messages such as respond are best-effort (not executed, so keep legacy
            # encoding behavior).
            payload_json = _encode_observation(payload)
        with self.transaction():
            existing = self.conn.execute(
                "SELECT status FROM pending_decision WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            if existing is None:
                raise StateError(
                    f"no pending decision for gate_key {gate_key!r} (run {run_id!r})"
                )
            if existing["status"] != "pending":
                raise StateError(
                    f"decision {gate_key!r} already resolved; cannot re-decide"
                )
            self.conn.execute(
                "UPDATE pending_decision SET status = 'resolved', decision = ?, "
                "payload = ?, resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE run_id = ? AND gate_key = ?",
                (decision, payload_json, run_id, gate_key),
            )
            self._append_event(
                run_id,
                EVENT_GATE,
                {"gate_key": gate_key, "status": "resolved", "decision": decision},
            )
            row = self.conn.execute(
                "SELECT * FROM pending_decision WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            return self._decode_decision(row)

    def claim_execution(self, run_id: str, gate_key: str) -> bool:
        """Claim execution rights for an irreversible approve/edit action.

        Transition ``resolved`` -> ``executed`` with a conditional UPDATE constrained
        by ``status = 'resolved'`` and return ``True`` **only if this call performed
        the transition**. If already ``executed`` (= another process / resume already
        claimed execution first), return ``False``; the loser must not execute (caller
        skips). ``pending`` (unresolved) / missing / non-executing decisions
        (``reject`` / ``respond``) raise ``StateError`` because they do not execute an
        action and should not transition to executed.

        Replay resume (the path that replays from iteration 0 with fresh state)
        revisits executed gates, so execution rights are claimed *before* actually
        executing (at-most-once: for irreversible operations, not retrying after a
        mid-flight failure is safer). ``transaction()`` = ``BEGIN IMMEDIATE``
        serializes writers, so even if the same gate is resumed concurrently, only one
        process succeeds at resolved->executed (= guarantees exactly-once execution of
        the irreversible action).

        This is an at-most-once primitive for a single process that executes ``act``
        **synchronously and immediately**, finalizing resolved->executed in one step
        (without executing). If coordinating *concurrent* resume of the same run_id
        across multiple processes is required (in-progress leases, losers waiting for
        completion, and takeover after winner crash), use the multi-stage protocol
        :meth:`acquire_lease` + :meth:`complete_execution` (Issue #21). A given
        gate_key must be handled consistently by one protocol or the other.
        """
        with self.transaction():
            # Only approve/edit involve execution. reject/respond are "do not execute"
            # decisions and must not transition to executed (doing so would make later
            # resume skip the rejection/response record and corrupt gate state/audit trail).
            cur = self.conn.execute(
                "UPDATE pending_decision SET status = 'executed', "
                "executed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE run_id = ? AND gate_key = ? AND status = 'resolved' "
                "AND decision IN ('approve','edit')",
                (run_id, gate_key),
            )
            if cur.rowcount == 1:
                # This call won resolved->executed (execution is allowed).
                self._append_event(
                    run_id, EVENT_GATE, {"gate_key": gate_key, "status": "executed"}
                )
                return True
            # 0 rows: already executed / unresolved / missing / non-executing
            # (reject/respond).
            row = self.conn.execute(
                "SELECT status, decision FROM pending_decision "
                "WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            if row is None:
                raise StateError(
                    f"no decision for gate_key {gate_key!r} (run {run_id!r})"
                )
            if row["status"] == "executed":
                return False  # Loser: another resume already executed first.
            if row["status"] == "pending":
                raise StateError(f"cannot mark unresolved gate {gate_key!r} executed")
            # status == 'resolved' but decision is reject/respond (= do-not-execute).
            raise StateError(
                f"gate {gate_key!r} decision {row['decision']!r} is not executable "
                "(only approve/edit run an action)"
            )

    # -- in-progress leases (Issue #21: concurrent multi-process resume coordination) --

    def acquire_lease(
        self,
        run_id: str,
        gate_key: str,
        owner: str,
        *,
        now: Optional[float] = None,
        ttl: float = DEFAULT_LEASE_TTL,
    ) -> dict[str, Any]:
        """Acquire an execution lease for an irreversible approve/edit action.

        While :meth:`claim_execution` finalizes resolved->executed in one step, this
        method is the first stage of multi-step coordination:
        ``resolved -> executing -> (act execution) -> executed``. It transitions to
        ``executing`` and sets a lease (``lease_owner`` /
        ``lease_expires_at = now + ttl``). The return value is
        ``{"outcome": ..., "owner": ..., "expires_at": ..., "took_over": bool}``:

        - :data:`LEASE_ACQUIRED`: lease acquired. The caller should execute ``act``
          and then finalize ``executed`` with :meth:`complete_execution`.
          ``took_over=True`` means an expired lease from another process was taken
          over (recovery after winner crash).
        - :data:`LEASE_WAIT`: another process is running under a **valid** lease. The
          caller does not execute and waits until ``executed`` (losers pause). This
          prevents the ordering bug where a loser runs later iterations before the
          winner finishes the irreversible action.
        - :data:`LEASE_EXECUTED`: already executed. Safe to skip (do not run twice).

        Transition conditions:

        - ``pending`` (unresolved) / missing / non-executing decisions
          (reject/respond) raise ``StateError`` (they do not execute an action, so no
          lease is set).
        - ``resolved``: transition to ``executing`` and set the lease -> ACQUIRED.
        - ``executing`` and this owner holds it: treat as reentrant and extend the
          lease -> ACQUIRED.
        - ``executing`` and another owner holds a valid lease
          (``lease_expires_at > now``): WAIT.
        - ``executing`` and expired (``lease_expires_at <= now``): assume the holder
          crashed and reacquire the lease -> ACQUIRED (``took_over=True``). Reacquiring
          after expiry reruns ``act``, so this is **at-least-once** (for side effects
          that cannot tolerate duplicates, set ``ttl`` long enough to avoid takeover
          during execution. True exactly-once also requires an idempotency key on the
          side-effect side).
        - ``executed``: EXECUTED.

        ``transaction()`` = ``BEGIN IMMEDIATE`` serializes writers, so even if the
        same gate is resumed concurrently, only one process succeeds at
        ``resolved->executing`` (single winner).
        """
        if not owner:
            raise ConfigError("acquire_lease: owner must be a non-empty string")
        if ttl <= 0:
            raise ConfigError(f"acquire_lease: ttl must be positive, got {ttl!r}")
        now = time.time() if now is None else now
        expires = now + ttl
        with self.transaction():
            row = self.conn.execute(
                "SELECT status, decision, lease_owner, lease_expires_at "
                "FROM pending_decision WHERE run_id = ? AND gate_key = ?",
                (run_id, gate_key),
            ).fetchone()
            if row is None:
                raise StateError(
                    f"no decision for gate_key {gate_key!r} (run {run_id!r})"
                )
            status = row["status"]
            if status == "executed":
                return {
                    "outcome": LEASE_EXECUTED,
                    "owner": None,
                    "expires_at": None,
                    "took_over": False,
                }
            if status == "pending":
                raise StateError(
                    f"cannot lease unresolved gate {gate_key!r} (run {run_id!r})"
                )
            if row["decision"] not in ("approve", "edit"):
                raise StateError(
                    f"gate {gate_key!r} decision {row['decision']!r} is not executable "
                    "(only approve/edit run an action)"
                )
            if status == "executing":
                holder = row["lease_owner"]
                exp = row["lease_expires_at"]
                lease_valid = exp is not None and exp > now
                if lease_valid and holder != owner:
                    # Another owner is running under a valid lease: wait (loser).
                    return {
                        "outcome": LEASE_WAIT,
                        "owner": holder,
                        "expires_at": exp,
                        "took_over": False,
                    }
                # Reentry by this owner (holder == owner), or takeover of an expired lease.
                took_over = holder != owner
                self.conn.execute(
                    "UPDATE pending_decision SET lease_owner = ?, "
                    "lease_expires_at = ? "
                    "WHERE run_id = ? AND gate_key = ? AND status = 'executing'",
                    (owner, expires, run_id, gate_key),
                )
                self._append_event(
                    run_id,
                    EVENT_GATE,
                    {
                        "gate_key": gate_key,
                        "status": "executing",
                        "lease_owner": owner,
                        "took_over": took_over,
                    },
                )
                return {
                    "outcome": LEASE_ACQUIRED,
                    "owner": owner,
                    "expires_at": expires,
                    "took_over": took_over,
                }
            # status == 'resolved': first lease acquisition.
            self.conn.execute(
                "UPDATE pending_decision SET status = 'executing', lease_owner = ?, "
                "lease_expires_at = ? "
                "WHERE run_id = ? AND gate_key = ? AND status = 'resolved'",
                (owner, expires, run_id, gate_key),
            )
            self._append_event(
                run_id,
                EVENT_GATE,
                {
                    "gate_key": gate_key,
                    "status": "executing",
                    "lease_owner": owner,
                    "took_over": False,
                },
            )
            return {
                "outcome": LEASE_ACQUIRED,
                "owner": owner,
                "expires_at": expires,
                "took_over": False,
            }

    def complete_execution(self, run_id: str, gate_key: str, owner: str) -> bool:
        """Let the lease holder finalize ``executing -> executed`` after ``act`` completes.

        An UPDATE constrained by ``status = 'executing' AND lease_owner = owner``
        transitions to ``executed`` and returns ``True`` **only if this process still
        holds the lease**. If 0 rows are updated (= already ``executed`` / the lease
        expired and another owner reacquired it), return ``False``: in that case the
        side effect of ``act`` may have been duplicated (at-least-once takeover after
        expiry).

        ``executed`` is terminal and clears the lease columns (``lease_owner`` /
        ``lease_expires_at``), so future :meth:`acquire_lease` calls return EXECUTED.
        Calling this *after* persisting the step row satisfies "if ``executed``, the
        step row always exists" and prevents missing steps after winner crash (the
        driver guarantees this order with :attr:`loop_agent.loop.GateReview.on_complete`).
        """
        if not owner:
            raise ConfigError("complete_execution: owner must be a non-empty string")
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE pending_decision SET status = 'executed', "
                "executed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "lease_owner = NULL, lease_expires_at = NULL "
                "WHERE run_id = ? AND gate_key = ? AND status = 'executing' "
                "AND lease_owner = ?",
                (run_id, gate_key, owner),
            )
            if cur.rowcount == 1:
                self._append_event(
                    run_id,
                    EVENT_GATE,
                    {"gate_key": gate_key, "status": "executed", "lease_owner": owner},
                )
                return True
            return False

    def get_decision(self, run_id: str, gate_key: str) -> Optional[dict[str, Any]]:
        """Return the decision row for ``(run_id, gate_key)`` as a dict, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM pending_decision WHERE run_id = ? AND gate_key = ?",
            (run_id, gate_key),
        ).fetchone()
        return self._decode_decision(row) if row is not None else None

    def list_pending_decisions(self, run_id: str) -> list[dict[str, Any]]:
        """Return unresolved (``pending``) decisions for ``run_id`` in registration order."""
        rows = self.conn.execute(
            "SELECT * FROM pending_decision WHERE run_id = ? AND status = 'pending' "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
        return [self._decode_decision(r) for r in rows]


class DBProgressLog:
    """DB-backed progress log; a drop-in for :class:`~loop_agent.progress.ProgressLog`.

    ``on_step`` / ``record_result`` match ``ProgressLog`` signatures, so
    ``run_loop(..., on_step=db.on_step)`` can switch the observation target from JSONL
    to the state.db SoT (replacing ``on_step`` does not require caller changes).
    ``initial_state`` is an additional optional argument, so existing wiring remains
    compatible.

    ``db`` may be a file path (internally opened with :func:`connect`, owned here, and
    closed by :meth:`close`) or an existing ``sqlite3.Connection`` (borrowed; not
    closed here). Construction calls ``load_or_init(run_id)`` to ensure the run row
    and ``loop_begin`` exist.

    The restored result is stored in :attr:`state`. This is the **resume entry point**
    (Issue #14): a new run gets an empty :class:`LoopState`, while an existing run
    gets the intermediate state restored from persisted steps. Wiring
    ``run_loop(..., initial_state=db.state, on_step=db.on_step)`` continues an
    interrupted loop without losing state (for new runs, ``state`` is empty, so this
    is equivalent to a fresh start and the same wiring is fine).
    """

    def __init__(self, db: DbSource, run_id: str) -> None:
        if isinstance(db, sqlite3.Connection):
            self.conn = db
            self._owns_conn = False
        else:
            self.conn = connect(db)
            self._owns_conn = True
        self.run_id = run_id
        self.store = LoopStore(self.conn)
        # Keep the restored (or empty for a new run) LoopState as the resume seed.
        self.state = self.store.load_or_init(run_id)

    def on_step(self, record: StepRecord, state: LoopState) -> None:
        """Persist one completed iteration. Compatible with run_loop ``StepHook``."""
        self.store.record_step(self.run_id, record, state)

    def record_result(self, result: "LoopResult") -> None:
        """Finalize the loop status at loop end."""
        self.store.record_result(self.run_id, result)

    def close(self) -> None:
        """Close only connections opened here (borrowed connections are caller-owned)."""
        if self._owns_conn:
            self.conn.close()

    def __enter__(self) -> "DBProgressLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "connect",
    "LoopStore",
    "DBProgressLog",
    "SCHEMA",
    "SCHEMA_VERSION",
    "EVENT_BEGIN",
    "EVENT_STEP",
    "EVENT_END",
    "EVENT_GATE",
    "DECISION_KINDS",
    "LEASE_ACQUIRED",
    "LEASE_WAIT",
    "LEASE_EXECUTED",
    "DEFAULT_LEASE_TTL",
]
