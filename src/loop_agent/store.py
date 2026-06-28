"""ループ状態の SoT: loop 用最小 SQLite スキーマ + transaction 永続化 (Issue #11).

PoC の :mod:`loop_agent.progress` は各反復を JSON Lines に追記する「最小状態」
だった (report.md S5 Phase 1)。本モジュールはそれを Phase 2 の **state.db SoT**
へ引き上げる (report.md S3.4 / S4.6 / S5 Phase 2): ループ 1 走分の進捗を SQLite に
*atomic* に永続化し、プロセスをまたいで状態が残る単一の正本にする。

設計の境界 (最重要・report.md S6 「state.db の抽出度」):

- **org 本体に密結合させない**。claude-org-ja の ``tools/state_db`` を adapt 元に
  したが、本スキーマは ``run / step / event / stop_reason`` の 4 テーブルだけの
  *自己完結* した最小スキーマで、org 側の projects / workstreams / worker_dirs や
  snapshotter / dashboard 連携には一切依存しない。``connect`` だけで生成・利用できる。
- **transaction を唯一の atomic 境界にする** (report.md R4)。:class:`LoopStore`
  は StateWriter 風の明示的 ``transaction()`` を持ち、各 step の「step 行 +
  集計 + journal event」を 1 トランザクションに束ねる。途中でクラッシュ (= commit
  前にプロセス終了) すれば step は丸ごと無かったことになり、半端な行は残らない。
- **resume の正本** (Issue #14)。:meth:`LoopStore.load_or_init` は run 行を確保し、
  既存 run なら永続化済み step から :class:`LoopState` を復元して返す。これを
  ``run_loop(initial_state=...)`` (または :attr:`DBProgressLog.state`) に渡すと、
  中断したループを状態欠落なく途中から継続できる (resume)。observation は JSON で
  保存されるため復元時に JSON round-trip を経る (型忠実度の限界は ``load_or_init`` /
  ``run_loop`` の docstring 参照)。

JSONL の :class:`~loop_agent.progress.ProgressLog` は撤去せず *併存* させる
(README 参照)。依存ゼロで読める PoC アーティファクトとしての価値が残るため。
:class:`DBProgressLog` は ``ProgressLog`` と同じ ``on_step`` / ``record_result``
シグネチャを実装する drop-in なので、観測フックの差し替えだけで SoT を DB に移せる。
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

if TYPE_CHECKING:  # 実行時の import cycle を避ける (型注釈のためだけに必要)
    from .loop import LoopResult

# スキーマのバージョン。テーブルを増やす拡張ではなく後方非互換の変更時に上げる。
# 専用テーブルを置かず PRAGMA user_version に持たせ「最小 4 テーブル」を守る。
# v2 (Issue #21, Phase3): pending_decision に in-progress リース
# (executing 状態 + lease_owner / lease_expires_at) を追加。既存 DB は
# :func:`_migrate_schema` が schema を実検査して非破壊に移行する (version は情報用)。
SCHEMA_VERSION = 2

# loop 用最小スキーマ。org 本体非依存・自己完結。``IF NOT EXISTS`` で冪等。
#
# - run             : 1 走 1 行。最終ステータスと集計 (反復数 / トークン / 経過) の正本。
# - step            : 完了した各反復 1 行。UNIQUE(run_id, iteration) で再実行に冪等
#                     (resume #14 の土台)。observation は JSON 文字列で保存。
# - event           : append-only の journal (report.md R7 観測)。loop_begin /
#                     loop_step / loop_end / loop_gate を記録し、全終了理由・人間ゲート
#                     の発火/決定を事後解析できるようにする。
# - stop_reason     : run と 1:1。発火した停止条件 (name) と理由、または goal 達成。
# - pending_decision: 限定人間ゲート (Issue #15, report.md S4.5 / R6) の決定レジスタ。
#                     不可逆操作で発火した 1 件 1 行。UNIQUE(run_id, gate_key) で冪等。
#                     pending -> resolved(approve|edit|reject|respond) -> executed を
#                     永続化し、pause/resume をまたいで決定を保持する。claude-org の
#                     pending_decisions(state machine) を role 読み替えで reuse:
#                     「secretary が worker の判断要求を register し user 応答で resolve」
#                     を「loop が不可逆 action を register し human が resolve」に対応付け、
#                     直接応答ゆえ中間状態 escalated は resolved に畳む。さらに executed を
#                     足し、approve/edit で実行した不可逆 action の at-most-once を担保する
#                     (replay resume = fresh state で iteration 0 から再生する経路で実行済み
#                     ゲートを skip して再発火を防ぐ。#14 の initial_state resume は中断
#                     iteration から継続するので実行済みゲートを再訪しない)。
#                     Phase3 (#21): resolved -> executing -> executed の多段化と
#                     lease_owner / lease_expires_at で複数プロセス同時 resume を協調する
#                     (in-progress リース。:meth:`LoopStore.acquire_lease` /
#                     :meth:`~LoopStore.complete_execution`)。
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

# pending_decision の DDL は単体で持ち、:func:`_migrate_schema` のテーブル再構築でも
# 同一定義を再利用する (列定義のドリフト防止)。``CREATE TABLE`` 名だけ差し替えれば
# 移行用の一時テーブルを作れるよう、テーブル名は 1 箇所だけに現れる形にしてある
# (FK は run を参照し、CHECK / UNIQUE は列名のみで自テーブル名を含まない)。
#
# status の多段 (Phase3 #21): pending -> resolved -> executing -> executed。
# executing は approve/edit の不可逆 action を *いま実行中* のプロセスがリースを保持して
# いる状態で、lease_owner (保持者トークン) と lease_expires_at (epoch 秒, REAL) を持つ。
# 同一ゲートを並行 resume しても resolved->executing に成功するのは 1 者だけ (single
# winner)。敗者は executing を見て executed まで pause する (順序整合)。勝者クラッシュ時は
# lease_expires_at の失効で別プロセスがリースを取り直せる (= step 欠落を防ぐ)。
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
  -- pending 以外 (resolved/executing/executed) は必ず decision を持つ (整合不変条件)。
  CHECK (status = 'pending' OR decision IS NOT NULL),
  -- executing は必ずリース保持者を持つ (失効判定の前提)。
  CHECK (status <> 'executing' OR lease_owner IS NOT NULL),
  UNIQUE (run_id, gate_key)
);
"""

_PENDING_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_pending_run ON pending_decision(run_id);\n"
)

# 完全なスキーマ = コア 4 テーブル + pending_decision + その index。``connect`` /
# :class:`LoopStore` はこれを executescript で冪等適用する。
SCHEMA = _SCHEMA_CORE + _PENDING_TABLE_DDL + _PENDING_INDEX_DDL

# event.kind の値。読み手が文字列リテラルを直書きせず filter できるよう定数化。
EVENT_BEGIN = "loop_begin"
EVENT_STEP = "loop_step"
EVENT_END = "loop_end"
# 人間ゲートの発火 (pending) / 決定 (resolved) を journal に残す (report.md R6/R7)。
EVENT_GATE = "loop_gate"

# 限定人間ゲートで人間が下せる 4 種の決定 (LangGraph interrupt パリティ:
# report.md S4.5 / S2.6)。approve=そのまま実行 / edit=修正して実行 /
# reject=実行せず却下を記録 / respond=実行せず応答を返す。
DECISION_KINDS = ("approve", "edit", "reject", "respond")

# in-progress リース (Issue #21) の取得結果 (:meth:`LoopStore.acquire_lease` の outcome)。
# - ACQUIRED: このプロセスがリースを取得した (= 不可逆 action を実行してよい)。
# - WAIT    : 別プロセスが有効なリースで実行中。executed まで待て (敗者は pause する)。
# - EXECUTED: 既に実行完了済み。skip してよい (二重実行しない)。
LEASE_ACQUIRED = "acquired"
LEASE_WAIT = "wait"
LEASE_EXECUTED = "executed"

# リース既定 TTL (秒)。不可逆 action 1 回の実行に十分長い値にする (短すぎると勝者の
# 実行中にリースが失効し別プロセスが奪取して二重実行になりうる)。HumanGate /
# run_gated_loop で上書き可能。
DEFAULT_LEASE_TTL = 30.0

DbSource = Union[str, "os.PathLike[str]", sqlite3.Connection]


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """loop 用 state DB を開き (無ければ作り)、スキーマを適用して返す。

    ``path`` には通常のファイルパスか ``":memory:"`` を渡す。詳細は
    :func:`_init_connection` 参照 (スキーマ適用 + PRAGMA + row_factory)。
    """
    return _init_connection(sqlite3.connect(str(path)))


def _init_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """接続にスキーマと PRAGMA を適用して返す (冪等)。

    :func:`connect` と :class:`LoopStore` の両方から呼ばれる。後者は素の
    ``sqlite3.connect()`` で開いた借用接続を渡されても動くよう **防御的に** これを
    呼ぶ (org の StateWriter と同じ方針)。``IF NOT EXISTS`` のスキーマと冪等な PRAGMA
    なので、初期化済みの接続に再適用しても安全。

    - ``isolation_level = None`` (autocommit): トランザクションは
      :meth:`LoopStore.transaction` の明示的な ``BEGIN`` / ``COMMIT`` で完全制御する
      (sqlite3 既定の暗黙トランザクションに依存しない StateWriter 風の制御)。
    - ``row_factory = sqlite3.Row``: 読み出しを列名アクセスにする。
    - ``foreign_keys = ON``: ``run`` 削除時に子行を CASCADE するため必須。
    - ``busy_timeout``: 並行アクセス時のロック待ち。
    - ``journal_mode = WAL``: writer と reader が衝突しにくくなる (file DB のみ有効。
      ``:memory:`` では無視される)。
    """
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    # fresh DB は新スキーマで作られる。既存 DB は古いテーブルが残るので、リース列と
    # executing status を非破壊に追加する移行を実スキーマ検査で冪等に行う。FK の toggle が
    # 絡むため foreign_keys を立てる *前* に実施する。
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """既存 DB の ``pending_decision`` を v2 (リース) スキーマへ非破壊に移行する (冪等)。

    v1 で作られた ``pending_decision`` は ``executing`` を許さない status CHECK で、
    ``lease_owner`` / ``lease_expires_at`` 列を持たない。SQLite は ``ALTER TABLE`` で
    CHECK を変更できないため、新定義のテーブルを作って行をコピーし差し替える
    (SQLite 公式の table 再構築手順)。``connect`` 毎に呼ばれるので、実テーブル定義を
    検査して **既に新スキーマなら何もしない** (fresh DB / 移行済み DB では no-op)。

    再構築は ``foreign_keys`` を一時的に OFF にし、単一トランザクションで原子的に行う
    (途中失敗で半端なテーブルを残さない)。``pending_decision`` を参照する子テーブルは
    無いので、参照側の張り直しは不要。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_decision'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return  # まだテーブルが無い (理論上 executescript 後は来ない)。
    table_sql = row["sql"]
    # 新スキーマの目印 (executing status と lease 列) が両方あれば移行済み。
    if "'executing'" in table_sql and "lease_owner" in table_sql:
        return
    mig_ddl = _PENDING_TABLE_DDL.replace(
        "CREATE TABLE IF NOT EXISTS pending_decision",
        "CREATE TABLE pending_decision_mig",
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 万一前回の中断で一時テーブルが残っていても再構築をやり直せるよう先に落とす
        # (CREATE は IF NOT EXISTS でないので、残存すると connect が恒久的に失敗するため)。
        # DROP/CREATE/INSERT/DROP/RENAME は全てこの単一トランザクション内で atomic に確定する
        # (SQLite の DDL はトランザクショナル)。途中クラッシュは次回 open で丸ごと rollback され、
        # 旧 pending_decision がそのまま残るので再試行できる (中間状態でデータ欠落しない)。
        conn.execute("DROP TABLE IF EXISTS pending_decision_mig")
        # executescript は暗黙 COMMIT で手動トランザクションを壊すため execute を使う
        # (mig_ddl / index はいずれも単一ステートメント)。
        conn.execute(mig_ddl)
        # 旧テーブルの全列を明示コピー (新規のリース 2 列は default NULL のまま)。
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
    """``_to_jsonable`` 済みの構造から非有限 float (NaN/Infinity) を ``repr`` に落とす。

    非有限 float は JSON の *型* としては有効だが、``json.dumps`` の既定 (allow_nan=True)
    では ``NaN`` / ``Infinity`` といった **JSON として不正なトークン** を吐く。これは
    SQLite の ``json_valid()`` を 0 にし、``step.observation`` / ``event.payload`` の
    CHECK 制約に弾かれて (IntegrityError) その step の永続化ごと巻き戻してしまう
    (「1 つの変な observation が永続化全体を壊さない」という契約に反する)。``json.dumps``
    が見る前に ``repr`` 文字列 ('nan' / 'inf' / '-inf') へ置換して strictly-valid JSON
    だけを保存する。入力は ``_to_jsonable`` 後 (None/bool/int/float/str/list/dict のみ)
    を想定して再帰する。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return [_finite_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _finite_safe(v) for k, v in value.items()}
    return value


def _encode_observation(observation: Any) -> str:
    """observation を *strictly-valid* な JSON 文字列に符号化する。

    :func:`loop_agent.progress._to_jsonable` で JSON 非ネイティブ値を ``repr`` に
    落とし、:func:`_finite_safe` で非有限 float も ``repr`` 化してから ``json.dumps``
    する (``allow_nan=False`` で取りこぼしを防ぐ)。1 つの変な observation が永続化
    全体を壊さない (json_valid CHECK 違反を起こさない)。
    """
    return json.dumps(
        _finite_safe(_to_jsonable(observation)),
        ensure_ascii=False,
        allow_nan=False,
        default=repr,
    )


def _require_json_native(value: Any, what: str) -> str:
    """``value`` を JSON 符号化して返すが、**round-trip lossless** でなければ弾く。

    :func:`_encode_observation` は observation の best-effort 永続化のため非 JSON
    ネイティブ値 (任意オブジェクト / tuple / NaN 等) を ``repr`` 等へ潰すが、人間ゲートの
    **実行される / 同一性比較される** 値 (gated action・edit の置換 action) でそれを許すと、
    ``(1, 2)`` が ``[1, 2]`` に化けて別 action と誤一致したり、オブジェクトが ``'<x>'`` 文字列
    として実行される事故になる。符号化→復号して元と一致しない (= 欠損する) 値は、その場で
    ``ConfigError`` で loud に弾く (safety-sensitive な値は fidelity を厳格化する)。
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
    """接続に束ねた loop 状態の writer/reader。StateWriter 風の明示的 transaction。

    ``conn`` は :func:`connect` が返した接続でも、素の ``sqlite3.connect()`` で開いた
    借用接続でもよい。後者でも動くよう、生成時に :func:`_init_connection` を防御的に
    呼んでスキーマ + PRAGMA + row_factory を (冪等に) 適用する (org の StateWriter と
    同じ方針)。すべての書き込みは :meth:`transaction` 配下で atomic に行う。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        _init_connection(conn)

    # -- transaction 制御 ----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["LoopStore"]:
        """``BEGIN IMMEDIATE`` -> yield -> ``COMMIT`` (例外時 ``ROLLBACK`` して再送出)。

        既に外側のトランザクション内なら新たに ``BEGIN`` せずそれに *参加* する
        (sqlite はネストした ``BEGIN`` を ``OperationalError`` にするため)。これにより
        :meth:`record_step` 等を呼び出し側の ``transaction()`` でさらに束ねて、複数 step
        を 1 つの atomic 単位にできる。参加した内側ブロックは commit/rollback せず、
        最終的な確定/巻き戻しは最外の ``transaction()`` に委ねる。

        ``BEGIN IMMEDIATE`` で *最初から書き込みロック* を取る。:meth:`load_or_init`
        は SELECT してから INSERT する write-after-read のため、既定の DEFERRED
        ``BEGIN`` だと WAL 下で read→write 昇格時に ``SQLITE_BUSY_SNAPSHOT``
        (``database is locked``) を起こしうる。これは ``busy_timeout`` で待っても解消
        できず即エラーになる (cross-process resume #14 で顕在化)。本クラスの
        ``transaction()`` は全て書き込み目的なので、IMMEDIATE で昇格を回避し
        ``busy_timeout`` のロック待ちが実際に効くようにする。
        """
        if self.conn.in_transaction:
            # 外側トランザクションに参加。確定/巻き戻しは最外に委ねる。
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

    # -- 内部ヘルパ ----------------------------------------------------------

    def _append_event(
        self, run_id: str, kind: str, payload: Optional[dict[str, Any]] = None
    ) -> None:
        """journal に 1 event を追記する (append-only)。"""
        # observation と同じく非有限 float を repr 化し、event.payload の json_valid
        # CHECK 違反を防ぐ (現状の payload は有限値のみだが防御的に揃える)。
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
        """run 行の集計を現在の :class:`LoopState` に合わせて更新する。"""
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

    # -- run ライフサイクル --------------------------------------------------

    def load_or_init(self, run_id: str) -> LoopState:
        """``run_id`` の run 行を確保し、その時点の :class:`LoopState` を返す。

        - 新規 ``run_id``: ``run`` 行を ``status='running'`` で作成し ``loop_begin``
          event を 1 件記録。空の :class:`LoopState` (全カウンタ 0) を返す。
        - 既存 ``run_id``: 永続化済みの ``step`` 行から :class:`LoopState` を *復元* して
          返す (history・iteration・tokens_used・elapsed・goal_met)。これを
          ``run_loop(initial_state=...)`` に渡すと中断地点から **resume** できる (#14)。

        作成/復元は 1 トランザクションで atomic に行う。

        復元される ``history`` の ``observation`` は保存時の JSON を round-trip した値
        である (:func:`loop_agent.progress._to_jsonable` が tuple->list / 非 JSON
        ネイティブ型 ->repr 文字列 / dict キー ->str に coerce する)。よって
        observation を直接 *キー* にする state ベース条件 (特に
        :class:`~loop_agent.conditions.NoProgress` の既定 key) は、JSON 安定な
        observation 型で使うか、JSON 安定な signature へ射影する ``key`` を渡すこと
        (詳細は :func:`loop_agent.loop.run_loop` の ``initial_state`` 参照)。
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
        """永続化済み ``step`` 行から :class:`LoopState` を組み立てる (resume の復元)。

        iteration / tokens_used / elapsed / goal_met は run 行の集計を正本にし、
        ``history`` は step 行を反復順に :class:`StepRecord` へ復元する。
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

    # -- per-step 永続化 -----------------------------------------------------

    def record_step(
        self, run_id: str, record: StepRecord, state: LoopState
    ) -> None:
        """完了した 1 反復を atomic に永続化する (run_loop の ``StepHook`` 互換)。

        1 トランザクションで「step 行の upsert + run 集計の更新 + ``loop_step``
        event の追記」を束ねる。``UNIQUE(run_id, iteration)`` 衝突時は ``DO UPDATE``
        で上書きするので、同一反復の再実行 (resume #14) に冪等。

        ``loop_step`` event は **新規 insert か、再永続化で内容が変わったときだけ**
        追記する。同一反復をまったく同じ内容で再永続化する純粋な replay (resume) では
        step 行も event も実質変わらないので event を重ねない。一方、同一反復を*別の
        結果*で書き直した場合は、その新しい内容を持つ event を 1 件追記する。これにより
        append-only な journal は「同一内容の replay でノイズを増やさず」「最新 event が
        step SoT と矛盾しない (最後の event = 現在の step 行)」の両方を満たす。
        """
        obs_json = _encode_observation(record.observation)
        goal_int = int(bool(record.goal_met))
        with self.transaction():
            existing = self.conn.execute(
                "SELECT tokens, tokens_used, elapsed, goal_met, detail, "
                "observation FROM step WHERE run_id = ? AND iteration = ?",
                (run_id, record.iteration),
            ).fetchone()
            # 新規、または既存と内容が 1 つでも異なるなら event を追記する。
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
        """ループ終了時の最終ステータスを atomic に確定する。

        1 トランザクションで「``stop_reason`` 行の upsert + run 行の終了状態更新
        (status / ended_at と最終集計) + ``loop_end`` event の追記」を束ねる。
        ``stop_reason`` は run と 1:1 で、再実行に冪等 (``DO UPDATE``)。

        ``status == "paused"`` (人間ゲートでの中断) は **終端ではない**: run は
        ``running`` のまま残し、``stop_reason`` も書かない (resume で続行できる)。
        集計だけ更新し、pause を ``loop_gate`` event として journal に残す。これにより
        ``DBProgressLog.record_result`` を pause した結果にそのまま渡してもよい
        (CHECK 制約違反でクラッシュさせない)。
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

    # -- 読み出し ------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """run 行を dict で返す (無ければ ``None``)。"""
        row = self.conn.execute(
            "SELECT * FROM run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def read_steps(self, run_id: str) -> list[dict[str, Any]]:
        """``run_id`` の step 行を反復順に dict のリストで返す。

        ``observation`` は保存時の JSON から復号して返す。
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
        """``run_id`` の event を発生順 (id 昇順) に dict のリストで返す。

        ``payload`` は JSON から復号して返す。
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
        """stop_reason 行を dict で返す (未終了なら ``None``)。"""
        row = self.conn.execute(
            "SELECT * FROM stop_reason WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    # -- 限定人間ゲート (pending_decision) -----------------------------------

    @staticmethod
    def _decode_decision(row: sqlite3.Row) -> dict[str, Any]:
        """pending_decision 行を dict に復号する (action / payload を JSON から戻す)。"""
        d = dict(row)
        d["action"] = json.loads(d["action"]) if d["action"] is not None else None
        d["payload"] = json.loads(d["payload"]) if d["payload"] is not None else None
        return d

    def request_decision(
        self, run_id: str, gate_key: str, action: Any
    ) -> dict[str, Any]:
        """不可逆 action の人間ゲートを ``pending`` で登録する (冪等)。

        org の ``pending_decisions.append`` に対応 (role 読み替え)。同一
        ``(run_id, gate_key)`` に既存行があれば **上書きせず** そのまま返す。これにより
        pause 後の resume で同じ action を再評価しても、既に下した決定 (resolved) や
        登録済みの pending を壊さない (= 人間に二重に問わない)。新規登録時のみ
        ``loop_gate`` event を 1 件追記する。

        ``action`` は **JSON ネイティブ (round-trip lossless)** を要求する。gated action は
        resume 時に同一性比較 (誤適用防止) の基準になり、欠損符号化を許すと別 action と
        誤一致しうるため (:func:`_require_json_native` 参照)。

        **この呼び出しが新規 INSERT したか** を知りたい場合 (= 並行登録レースで「先に
        登録した 1 者」だけが副作用 — 通知等 — を起こしたいとき) は
        :meth:`register_decision` を使うこと。本メソッドは権威ある現在行のみ返す。
        """
        row, _created = self.register_decision(run_id, gate_key, action)
        return row

    def register_decision(
        self, run_id: str, gate_key: str, action: Any
    ) -> tuple[dict[str, Any], bool]:
        """:meth:`request_decision` と同じ登録を行い ``(現在行, 新規 INSERT したか)`` を返す。

        ``created`` は **この呼び出しが pending を INSERT したとき** のみ ``True``。既存行
        (別プロセスが先に登録済み or 自分の前 run で登録済み) を読んだときは ``False`` で、
        その行をそのまま返す。これにより、``get_decision`` で ``None`` を見た後の TOCTOU
        レースで敗者が ``request_decision`` から相手の行を受け取ったケースでも、
        ``created=False`` を見て **承認通知を二重に発火しない** ように呼び出し側が判定できる
        (:meth:`loop_agent.gate.HumanGate._notify_new_request` の発火条件)。INSERT は
        transaction 内の single-winner なので、並行登録でも ``created=True`` は厳密に 1 者。
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
        """``pending`` の決定を人間の選択で ``resolved`` に確定する。

        org の ``pending_decisions.resolve`` に対応。``decision`` は
        :data:`DECISION_KINDS` の 4 種。``payload`` は ``edit`` の置換 action や
        ``respond`` の応答メッセージを載せる (JSON 符号化)。``pending`` 行のみ遷移可能で、
        既に ``resolved`` 済みなら ``StateError`` (terminal: 一度下した決定は再決定しない)。
        確定時に ``loop_gate`` event を 1 件追記する。

        ``edit`` の ``payload`` は **JSON ネイティブ (round-trip lossless)** を要求する。
        この payload は resume 時に store から復元されて *実行される action* になるため、
        非 JSON ネイティブ値 (任意オブジェクト / tuple / NaN 等) を許すと repr 文字列へ
        潰れて *別の action を実行* する事故になる。記録時点で round-trip 検査し、欠損する
        なら loud に弾く (observation は best-effort な journal なので潰すが、実行される
        edit は厳格にする方針)。
        """
        if decision not in DECISION_KINDS:
            raise ConfigError(
                f"unknown decision {decision!r}; expected one of {DECISION_KINDS}"
            )
        if payload is None:
            payload_json = None
        elif decision == "edit":
            # edit の置換 action は resume で復元され *実行される* ので JSON ネイティブ厳守。
            payload_json = _require_json_native(payload, "edit payload")
        else:
            # respond 等のメッセージは best-effort (実行されないので従来どおり符号化)。
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
        """approve/edit の不可逆 action の実行権を **single-winner** で主張する。

        ``resolved`` -> ``executed`` への遷移を ``status = 'resolved'`` を条件にした
        条件付き UPDATE で行い、**この呼び出しが遷移させられたときだけ** ``True`` を返す。
        既に ``executed`` (= 別プロセス / 別 resume が先に実行を主張済み) なら ``False``
        を返す — 敗者は実行してはならない (呼び出し側は skip する)。``pending`` (未解決) /
        不在 / 非実行系の決定 (``reject`` / ``respond``) は ``StateError`` (これらは
        action を実行しないので executed へ遷移させない)。

        replay resume (fresh state で iteration 0 から再生する経路) では実行済みゲートを
        再訪するため、実行に踏み切る *前* に実行権を主張する (at-most-once: 途中失敗時も
        再実行しない方が不可逆操作には安全)。``transaction()`` = ``BEGIN IMMEDIATE`` で
        writer を直列化するので、同一ゲートを並行 resume しても resolved->executed の
        遷移に成功するのは 1 者だけ (= 不可逆 action の exactly-once 実行を担保)。

        これは ``act`` を **同期的に・即時** 実行する単一プロセス向けの at-most-once
        プリミティブで、resolved->executed を 1 手で確定する (executing を経ない)。
        複数プロセスが同一 run_id を *同時に* resume する協調 (in-progress リース・
        敗者の完了待ち・勝者クラッシュ時の取り直し) が要る場合は
        :meth:`acquire_lease` + :meth:`complete_execution` の多段プロトコルを使う
        (Issue #21)。1 つの gate_key はどちらか一方のプロトコルで一貫して扱うこと。
        """
        with self.transaction():
            # 実行を伴うのは approve/edit のみ。reject/respond は「実行しない」決定なので
            # executed へ遷移させない (誤って遷移させると後続 resume が却下/応答の記録を
            # skip して gate 状態・監査証跡を壊す)。
            cur = self.conn.execute(
                "UPDATE pending_decision SET status = 'executed', "
                "executed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE run_id = ? AND gate_key = ? AND status = 'resolved' "
                "AND decision IN ('approve','edit')",
                (run_id, gate_key),
            )
            if cur.rowcount == 1:
                # この呼び出しが resolved->executed を勝ち取った (実行してよい)。
                self._append_event(
                    run_id, EVENT_GATE, {"gate_key": gate_key, "status": "executed"}
                )
                return True
            # 0 行: 既に executed / 未解決 / 不在 / 非実行系(reject/respond) のいずれか。
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
                return False  # 敗者: 別の resume が先に実行済み。
            if row["status"] == "pending":
                raise StateError(f"cannot mark unresolved gate {gate_key!r} executed")
            # status == 'resolved' だが decision が reject/respond (= 実行しない決定)。
            raise StateError(
                f"gate {gate_key!r} decision {row['decision']!r} is not executable "
                "(only approve/edit run an action)"
            )

    # -- in-progress リース (Issue #21: 複数プロセス同時 resume の協調) -------

    def acquire_lease(
        self,
        run_id: str,
        gate_key: str,
        owner: str,
        *,
        now: Optional[float] = None,
        ttl: float = DEFAULT_LEASE_TTL,
    ) -> dict[str, Any]:
        """approve/edit の不可逆 action 実行リースを **single-winner** で取得する。

        :meth:`claim_execution` が resolved->executed を 1 手で確定するのに対し、本メソッドは
        ``resolved -> executing -> (act 実行) -> executed`` の多段協調の前段で、``executing``
        へ遷移してリース (``lease_owner`` / ``lease_expires_at = now + ttl``) を張る。戻り値は
        ``{"outcome": ..., "owner": ..., "expires_at": ..., "took_over": bool}``:

        - :data:`LEASE_ACQUIRED`: リースを取得した。呼び出し側は ``act`` を実行し、完了後に
          :meth:`complete_execution` で ``executed`` を確定する。``took_over=True`` は
          失効した他者リースを引き継いだ取得 (勝者クラッシュからの復旧)。
        - :data:`LEASE_WAIT`: 別プロセスが **有効な** リースで実行中。呼び出し側は実行せず
          ``executed`` まで待つ (敗者は pause)。これにより「勝者の不可逆 action 完了前に
          敗者が後続 iteration を走らせる」順序ずれを防ぐ。
        - :data:`LEASE_EXECUTED`: 既に実行完了済み。skip してよい (二重実行しない)。

        遷移条件:

        - ``pending`` (未解決) / 不在 / 非実行系 (reject/respond) は ``StateError``
          (これらは action を実行しないのでリースを張らない)。
        - ``resolved``: ``executing`` へ遷移しリースを張る -> ACQUIRED。
        - ``executing`` かつ自分が保持者: 再入とみなしリースを延長 -> ACQUIRED。
        - ``executing`` かつ他者が有効保持 (``lease_expires_at > now``): WAIT。
        - ``executing`` かつ失効 (``lease_expires_at <= now``): 保持者がクラッシュしたとみなし
          リースを取り直す -> ACQUIRED (``took_over=True``)。失効取り直しは ``act`` を再実行
          するので **at-least-once** になる (重複が許されない副作用は ``ttl`` を実行所要より
          十分長くして失効取り直しを避けること。完全な exactly-once は副作用側の冪等鍵が要る)。
        - ``executed``: EXECUTED。

        ``transaction()`` = ``BEGIN IMMEDIATE`` が writer を直列化するので、同一ゲートを
        並行 resume しても ``resolved->executing`` に成功するのは 1 者だけ (single winner)。
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
                    # 他者が有効なリースで実行中: 待て (敗者)。
                    return {
                        "outcome": LEASE_WAIT,
                        "owner": holder,
                        "expires_at": exp,
                        "took_over": False,
                    }
                # 自分の再入 (holder == owner) か失効リースの取り直し。
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
            # status == 'resolved': 初回のリース取得。
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
        """リース保持者が ``executing -> executed`` を確定する (``act`` 完了後に呼ぶ)。

        ``status = 'executing' AND lease_owner = owner`` を条件にした UPDATE で、**自分が
        まだリースを保持しているときだけ** ``executed`` へ遷移させ ``True`` を返す。0 行
        (= 既に ``executed`` / リースが失効して他者に取り直された) なら ``False`` を返す:
        その場合 ``act`` の副作用は重複実行だった可能性がある (失効取り直しの at-least-once)。

        ``executed`` は終端で、リース列 (``lease_owner`` / ``lease_expires_at``) はクリアする
        (以後 :meth:`acquire_lease` は EXECUTED を返す)。step 行を永続化した *後* に呼ぶことで
        「``executed`` なら step 行は必ず存在する」を満たし、勝者クラッシュ時の step 欠落を
        防ぐ (driver は :attr:`loop_agent.loop.GateReview.on_complete` でこの順序を保証する)。
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
        """``(run_id, gate_key)`` の決定行を dict で返す (無ければ ``None``)。"""
        row = self.conn.execute(
            "SELECT * FROM pending_decision WHERE run_id = ? AND gate_key = ?",
            (run_id, gate_key),
        ).fetchone()
        return self._decode_decision(row) if row is not None else None

    def list_pending_decisions(self, run_id: str) -> list[dict[str, Any]]:
        """``run_id`` の未解決 (``pending``) 決定を登録順に返す。"""
        rows = self.conn.execute(
            "SELECT * FROM pending_decision WHERE run_id = ? AND status = 'pending' "
            "ORDER BY id",
            (run_id,),
        ).fetchall()
        return [self._decode_decision(r) for r in rows]


class DBProgressLog:
    """DB-backed の進捗記録。:class:`~loop_agent.progress.ProgressLog` の drop-in。

    ``on_step`` / ``record_result`` のシグネチャを ``ProgressLog`` と揃えてあるので、
    ``run_loop(..., on_step=db.on_step)`` のまま観測先を JSONL から state.db SoT へ
    差し替えられる (``on_step`` の差し替えに呼び出し側の変更は要らない。``initial_state``
    は追加 optional 引数なので既存の配線も壊さない)。

    ``db`` にはファイルパス (内部で :func:`connect` し、所有権を持って :meth:`close`
    で閉じる) か、既存の ``sqlite3.Connection`` (借用。close では閉じない) を渡せる。
    生成時に ``load_or_init(run_id)`` を呼んで run 行と ``loop_begin`` を確保する。

    その復元結果は :attr:`state` に保持する。これが **resume の入口** (Issue #14):
    新規 run なら空の :class:`LoopState`、既存 run なら永続化済み step から復元した
    途中状態になる。``run_loop(..., initial_state=db.state, on_step=db.on_step)`` と
    配線すれば、中断したループを状態欠落なく途中から継続できる (新規 run では
    ``state`` が空なので fresh start と同義 = 同じ配線でよい)。
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
        # 復元した (新規なら空の) LoopState を resume の seed として保持する。
        self.state = self.store.load_or_init(run_id)

    def on_step(self, record: StepRecord, state: LoopState) -> None:
        """完了した 1 反復を永続化する。run_loop の ``StepHook`` 互換。"""
        self.store.record_step(self.run_id, record, state)

    def record_result(self, result: "LoopResult") -> None:
        """ループ終了時の最終ステータスを確定する。"""
        self.store.record_result(self.run_id, result)

    def close(self) -> None:
        """自分で開いた接続のみ閉じる (借用接続は呼び出し側の責務)。"""
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
