"""クロスプロセス WakeQueue backend の検証 (Issue #41)。

:class:`SqliteWakeQueue` (stdlib) / :class:`RedisWakeQueue` (optional dep) が
:class:`InMemoryWakeQueue` と **同一の三状態 claim-then-confirm セマンティクス** を持つことを、
共通の契約テストを 3 backend へ parametrize して実証する。Redis は実接続が CI で不可なため、
本モジュール内の最小 :class:`FakeRedis` (redis-py 互換のサブセット) を注入して検証する。
"""

from __future__ import annotations

import threading
import time

import pytest

from loop_agent.transport import (
    CLAIMED,
    DELIVERED,
    UNDELIVERED,
    InMemoryWakeQueue,
    NullPushBackend,
    RedisWakeQueue,
    SqliteWakeQueue,
    Transport,
    WAKE_LOOP_DONE,
    Wake,
    open_wake_queue,
)


# ---------------------------------------------------------------------------
# 最小 FakeRedis (redis-py 互換のサブセット; RedisWakeQueue が使うコマンドのみ)
#
# 実 Redis は CI で不可。RedisWakeQueue が使う命令だけを in-process で実装し、redis-py の
# **bytes 返却** セマンティクスまで模す (= RedisWakeQueue の bytes デコード経路まで実走させる)。
# ---------------------------------------------------------------------------


def _b(v: object) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode("utf-8")
    return str(v).encode("utf-8")


class FakeRedis:
    """RedisWakeQueue が使うコマンドのサブセットを実装した in-process な偽 Redis。

    redis-py 既定 (``decode_responses=False``) と同じく文字列値は ``bytes`` で返す。単一
    プロセス内テスト用で、TTL は記録するが自動失効はしない (テストが明示的に検査する)。
    """

    def __init__(self) -> None:
        self.strings: dict[bytes, bytes] = {}
        self.hashes: dict[bytes, dict[bytes, bytes]] = {}
        self.zsets: dict[bytes, dict[bytes, float]] = {}
        self.sets: dict[bytes, set[bytes]] = {}
        self.expires: dict[bytes, int] = {}

    # -- strings (分散ロック用) ------------------------------------------------
    def set(self, name, value, nx=False, px=None):
        key = _b(name)
        if nx and key in self.strings:
            return None
        self.strings[key] = _b(value)
        return True

    def get(self, name):
        return self.strings.get(_b(name))

    def delete(self, *names):
        n = 0
        for name in names:
            key = _b(name)
            if key in self.strings:
                del self.strings[key]
                n += 1
        return n

    # -- counter / existence --------------------------------------------------
    def incr(self, name):
        key = _b(name)
        cur = int(self.strings.get(key, b"0"))
        cur += 1
        self.strings[key] = _b(cur)
        return cur

    def exists(self, name):
        key = _b(name)
        return 1 if (key in self.hashes or key in self.strings) else 0

    # -- hashes ---------------------------------------------------------------
    def hset(self, name, mapping=None):
        key = _b(name)
        h = self.hashes.setdefault(key, {})
        for f, v in (mapping or {}).items():
            h[_b(f)] = _b(v)
        return len(mapping or {})

    def hgetall(self, name):
        return dict(self.hashes.get(_b(name), {}))

    def hget(self, name, field):
        return self.hashes.get(_b(name), {}).get(_b(field))

    # -- sorted sets ----------------------------------------------------------
    def zadd(self, name, mapping):
        z = self.zsets.setdefault(_b(name), {})
        for member, score in mapping.items():
            z[_b(member)] = float(score)
        return len(mapping)

    def zrange(self, name, start, end, withscores=False):
        z = self.zsets.get(_b(name), {})
        ordered = sorted(z.items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            end = len(ordered) - 1
        sliced = ordered[start : end + 1]
        if withscores:
            return [(m, s) for m, s in sliced]
        return [m for m, _ in sliced]

    def zrangebyscore(self, name, min, max):
        z = self.zsets.get(_b(name), {})
        lo = float("-inf") if min in ("-inf", b"-inf") else float(min)
        hi = float("inf") if max in ("+inf", b"+inf") else float(max)
        ordered = sorted(z.items(), key=lambda kv: (kv[1], kv[0]))
        return [m for m, s in ordered if lo <= s <= hi]

    def zrem(self, name, *members):
        z = self.zsets.get(_b(name), {})
        n = 0
        for m in members:
            if _b(m) in z:
                del z[_b(m)]
                n += 1
        return n

    def zcard(self, name):
        return len(self.zsets.get(_b(name), {}))

    # -- sets -----------------------------------------------------------------
    def sadd(self, name, *members):
        s = self.sets.setdefault(_b(name), set())
        before = len(s)
        s.update(_b(m) for m in members)
        return len(s) - before

    def srem(self, name, *members):
        s = self.sets.get(_b(name), set())
        n = 0
        for m in members:
            if _b(m) in s:
                s.discard(_b(m))
                n += 1
        return n

    def smembers(self, name):
        return set(self.sets.get(_b(name), set()))

    # -- TTL ------------------------------------------------------------------
    def expire(self, name, seconds):
        self.expires[_b(name)] = int(seconds)
        return True

    # -- scripting (compare-and-delete ロック解放だけを忠実に実装) -------------
    def eval(self, script, numkeys, *keys_and_args):
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        # RedisWakeQueue が使う唯一のスクリプト = lock の compare-and-delete。
        key = _b(keys[0])
        token = _b(args[0])
        if self.strings.get(key) == token:
            return self.delete(key)
        return 0


# ---------------------------------------------------------------------------
# backend parametrize: 各 backend の fresh な queue を生む factory
# ---------------------------------------------------------------------------


def _redis_queue(**kw):
    return RedisWakeQueue(client=FakeRedis(), **kw)


@pytest.fixture(
    params=["memory", "sqlite", "redis"],
    ids=["memory", "sqlite", "redis"],
)
def queue(request, tmp_path):
    if request.param == "memory":
        yield InMemoryWakeQueue()
    elif request.param == "sqlite":
        q = SqliteWakeQueue(str(tmp_path / "wakes.db"))
        yield q
        q.close()
    else:
        yield _redis_queue()


def _wake(i: int, recipient: str = "coord") -> Wake:
    return Wake(
        id=f"r1:{WAKE_LOOP_DONE}:{i}",
        kind=WAKE_LOOP_DONE,
        recipient=recipient,
        run_id="r1",
        payload={"n": i},
    )


# ---------------------------------------------------------------------------
# 共通契約テスト (全 backend で同一セマンティクス)
# ---------------------------------------------------------------------------


def test_enqueue_then_claim_then_confirm(queue):
    assert queue.enqueue(_wake(0)) is True
    assert queue.state_of("r1:loop_done:0") == UNDELIVERED

    claimed = queue.claim("coord", now=0.0, lease=30.0, owner="o")
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert claimed[0].payload == {"n": 0}  # payload は JSON round-trip で保たれる。
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    assert queue.confirm("r1:loop_done:0", owner="o", now=1.0) is True
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_enqueue_is_idempotent_by_id(queue):
    assert queue.enqueue(_wake(0)) is True
    assert queue.enqueue(_wake(0)) is False  # 同一 id は no-op。
    assert len(queue.claim("coord", now=0.0, lease=30.0, owner="o")) == 1


def test_enqueue_rejects_empty_id(queue):
    with pytest.raises(ValueError):
        queue.enqueue(Wake(id="", kind=WAKE_LOOP_DONE, recipient="coord"))


def test_claim_orders_by_enqueue_seq(queue):
    for i in (0, 1, 2):
        queue.enqueue(_wake(i))
    claimed = queue.claim("coord", now=0.0, lease=30.0, owner="o")
    assert [w.id for w in claimed] == [f"r1:loop_done:{i}" for i in (0, 1, 2)]


def test_claim_limit_bounds_batch(queue):
    for i in range(5):
        queue.enqueue(_wake(i))
    first = queue.claim("coord", now=0.0, lease=30.0, owner="o", limit=2)
    assert len(first) == 2
    # 残りは limit 後の seq 順。
    rest = queue.claim("coord", now=0.0, lease=30.0, owner="o")
    assert [w.id for w in rest] == ["r1:loop_done:2", "r1:loop_done:3", "r1:loop_done:4"]


def test_claim_only_returns_matching_recipient(queue):
    queue.enqueue(_wake(0, recipient="alice"))
    queue.enqueue(_wake(1, recipient="bob"))
    assert [w.id for w in queue.claim("alice", now=0.0, lease=30.0, owner="o")] == [
        "r1:loop_done:0"
    ]
    assert [w.id for w in queue.claim("bob", now=0.0, lease=30.0, owner="o")] == [
        "r1:loop_done:1"
    ]


def test_claimed_wake_not_reclaimed_while_lease_held(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="o")
    # lease 保持中は再 claim できない。
    assert queue.claim("coord", now=10.0, lease=30.0, owner="o2") == []


def test_lease_expiry_releases_for_reclaim(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="o")
    # lease 失効後は再 eligible になり再 claim できる (crash recovery)。
    reclaimed = queue.claim("coord", now=31.0, lease=30.0, owner="o2")
    assert [w.id for w in reclaimed] == ["r1:loop_done:0"]


def test_release_expired_counts_and_resets(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="o")
    assert queue.release_expired(now=10.0) == 0  # まだ失効していない。
    assert queue.state_of("r1:loop_done:0") == CLAIMED
    assert queue.release_expired(now=31.0) == 1  # 失効 -> UNDELIVERED へ。
    assert queue.state_of("r1:loop_done:0") == UNDELIVERED


def test_confirm_requires_owner_match(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="owner-A")
    # owner 不一致の confirm は弾かれる。
    assert queue.confirm("r1:loop_done:0", owner="owner-B", now=1.0) is False
    assert queue.state_of("r1:loop_done:0") == CLAIMED
    assert queue.confirm("r1:loop_done:0", owner="owner-A", now=1.0) is True
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_confirm_after_lease_expiry_is_fenced(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="o")
    # lease 失効後の遅延 confirm は弾かれる (届いていないので DELIVERED 化しない)。
    assert queue.confirm("r1:loop_done:0", owner="o", now=31.0) is False


def test_owner_fencing_blocks_stale_confirm(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="worker-A")
    # A の lease 失効後に B が再 claim。
    second = queue.claim("coord", now=31.0, lease=30.0, owner="worker-B")
    assert [w.id for w in second] == ["r1:loop_done:0"]
    # 遅れて来た A の confirm は弾かれ、B の confirm だけが通る。
    assert queue.confirm("r1:loop_done:0", owner="worker-A", now=32.0) is False
    assert queue.confirm("r1:loop_done:0", owner="worker-B", now=32.0) is True


def test_mark_delivered_only_from_undelivered(queue):
    queue.enqueue(_wake(0))
    assert queue.mark_delivered("r1:loop_done:0") is True
    assert queue.state_of("r1:loop_done:0") == DELIVERED
    # 既に DELIVERED なら no-op。
    assert queue.mark_delivered("r1:loop_done:0") is False


def test_mark_delivered_does_not_steal_active_claim(queue):
    queue.enqueue(_wake(0))
    queue.claim("coord", now=0.0, lease=30.0, owner="o")  # CLAIMED
    # CLAIMED を奪わない (claim-then-confirm の crash recovery を壊さない)。
    assert queue.mark_delivered("r1:loop_done:0") is False
    assert queue.state_of("r1:loop_done:0") == CLAIMED


def test_mark_delivered_excludes_from_claim(queue):
    queue.enqueue(_wake(0))
    queue.mark_delivered("r1:loop_done:0")
    assert queue.claim("coord", now=0.0, lease=30.0, owner="o") == []


def test_pending_excludes_delivered_and_orders_by_seq(queue):
    for i in (0, 1, 2):
        queue.enqueue(_wake(i))
    queue.mark_delivered("r1:loop_done:1")  # 確定済みは pending から除かれる。
    pend = queue.pending("coord")
    assert [w.id for w in pend] == ["r1:loop_done:0", "r1:loop_done:2"]


def test_pending_all_recipients_ordered_by_global_seq(queue):
    queue.enqueue(_wake(0, recipient="alice"))
    queue.enqueue(_wake(1, recipient="bob"))
    queue.enqueue(_wake(2, recipient="alice"))
    pend = queue.pending()  # 宛先指定なし = 全 recipient を global seq 順。
    assert [w.id for w in pend] == ["r1:loop_done:0", "r1:loop_done:1", "r1:loop_done:2"]


def test_pending_filters_by_recipient(queue):
    queue.enqueue(_wake(0, recipient="alice"))
    queue.enqueue(_wake(1, recipient="bob"))
    assert [w.id for w in queue.pending("alice")] == ["r1:loop_done:0"]


def test_state_of_unknown_is_none(queue):
    assert queue.state_of("does-not-exist") is None


def test_claim_rejects_nonpositive_lease(queue):
    queue.enqueue(_wake(0))
    with pytest.raises(ValueError):
        queue.claim("coord", now=0.0, lease=0.0, owner="o")


def test_payload_must_be_json_serializable(queue):
    # JSON 直列化は永続 backend の性質。in-memory は直列化しないので対象外。
    if isinstance(queue, InMemoryWakeQueue):
        pytest.skip("InMemoryWakeQueue does not serialize payloads")
    bad = Wake(id="x", kind=WAKE_LOOP_DONE, recipient="coord", payload={"obj": object()})
    with pytest.raises(ValueError):
        queue.enqueue(bad)


# ---------------------------------------------------------------------------
# Transport 統合 (SQLite を正本に据えても push一次/pull fallback が成立する)
# ---------------------------------------------------------------------------


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def test_transport_pull_fallback_over_sqlite(tmp_path):
    clock = ManualClock()
    q = SqliteWakeQueue(str(tmp_path / "t.db"))
    t = Transport(q, NullPushBackend(), lease=30.0, time_fn=clock)
    for i in range(3):
        assert t.deliver(_wake(i)) == "queued"
    seen: list[str] = []
    handled = t.poll_and_handle("coord", lambda w: seen.append(w.id))
    assert seen == [f"r1:loop_done:{i}" for i in range(3)]
    assert all(q.state_of(w.id) == DELIVERED for w in handled)
    clock.advance(100.0)
    assert t.poll_and_handle("coord", lambda w: seen.append("DUP")) == []
    assert "DUP" not in seen
    q.close()


def test_transport_redelivers_respects_inflight_claim_over_sqlite(tmp_path):
    """SQLite backend でも CLAIMED 中の再 deliver が active claim を横取りしない (codex P2 等価)。"""
    clock = ManualClock()
    q = SqliteWakeQueue(str(tmp_path / "t.db"))
    from loop_agent.transport import CallablePushBackend

    up = {"ok": False}
    t = Transport(q, CallablePushBackend(lambda w: up["ok"]), lease=30.0, time_fn=clock)
    assert t.deliver(_wake(0)) == "queued"
    claimed = t.poll("coord", confirm=False)
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    up["ok"] = True
    assert t.deliver(_wake(0)) == "queued"  # 横取りしない。
    assert q.state_of("r1:loop_done:0") == CLAIMED
    clock.advance(31.0)
    assert [w.id for w in t.poll("coord")] == ["r1:loop_done:0"]
    q.close()


# ---------------------------------------------------------------------------
# SQLite 固有 (クロスプロセス共有 / 永続 / cleanup)
# ---------------------------------------------------------------------------


def test_sqlite_file_shared_across_connections(tmp_path):
    """別 connection (= 別プロセス相当) が同じファイル正本を共有して配送を継続できる。"""
    path = str(tmp_path / "shared.db")
    producer = SqliteWakeQueue(path)
    producer.enqueue(_wake(0))
    producer.close()

    consumer = SqliteWakeQueue(path)  # 別インスタンス = 別 connection。
    claimed = consumer.claim("coord", now=0.0, lease=30.0, owner="o")
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert consumer.confirm("r1:loop_done:0", owner="o", now=1.0) is True
    consumer.close()


def test_sqlite_purge_delivered_reclaims_rows(tmp_path):
    q = SqliteWakeQueue(str(tmp_path / "p.db"))
    queue_ids = []
    for i in range(3):
        q.enqueue(_wake(i))
        queue_ids.append(f"r1:loop_done:{i}")
    q.mark_delivered("r1:loop_done:0")
    q.mark_delivered("r1:loop_done:1")
    assert q.purge_delivered() == 2  # DELIVERED 2 件だけ物理削除。
    assert q.state_of("r1:loop_done:0") is None
    assert q.state_of("r1:loop_done:2") == UNDELIVERED  # 非確定は残る。
    q.close()


def test_sqlite_rejects_bad_table_name():
    with pytest.raises(ValueError):
        SqliteWakeQueue(table="bad; DROP TABLE x")


def test_sqlite_custom_table_isolates_namespace(tmp_path):
    path = str(tmp_path / "ns.db")
    a = SqliteWakeQueue(path, table="wakes_a")
    b = SqliteWakeQueue(path, table="wakes_b")
    a.enqueue(_wake(0))
    assert a.pending() != []
    assert b.pending() == []  # 別 table は分離されている。
    a.close()
    b.close()


def test_sqlite_concurrent_pollers_never_double_claim(tmp_path):
    """SQLite backend を複数スレッドで並行 poll しても二重 claim しない (BEGIN IMMEDIATE 直列化)。"""
    q = SqliteWakeQueue(str(tmp_path / "c.db"))
    t = Transport(q, NullPushBackend(), lease=3600.0, time_fn=time.monotonic)
    n_wakes = 100
    for i in range(n_wakes):
        t.deliver(_wake(i))

    n_threads = 6
    barrier = threading.Barrier(n_threads)
    claimed_by: list[list[str]] = [[] for _ in range(n_threads)]

    def worker(idx: int) -> None:
        own = f"worker-{idx}"
        barrier.wait()
        while True:
            got = t.poll("coord", owner=own, limit=1)
            if not got:
                if not t.pending("coord"):
                    return
                continue
            assert t.confirm_wakes(got, owner=own) == 1
            claimed_by[idx].append(got[0].id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    all_claimed = [wid for lst in claimed_by for wid in lst]
    assert sorted(all_claimed) == sorted(f"r1:loop_done:{i}" for i in range(n_wakes))
    assert len(all_claimed) == len(set(all_claimed)) == n_wakes
    q.close()


# ---------------------------------------------------------------------------
# Redis 固有 (namespace / TTL / import gate / 分散ロック)
# ---------------------------------------------------------------------------


def test_redis_import_gate_without_client_or_url_raises():
    # client も url も無いと ValueError (redis 未導入環境でも import gate より前で弾く)。
    with pytest.raises((ValueError, ImportError)):
        RedisWakeQueue()


def test_redis_namespace_isolates_keys():
    client = FakeRedis()
    a = RedisWakeQueue(client=client, namespace="ns_a")
    b = RedisWakeQueue(client=client, namespace="ns_b")
    a.enqueue(_wake(0))
    assert a.pending() != []
    assert b.pending() == []  # 別 namespace は同一 client 上でも衝突しない。


def test_redis_sets_ttl_on_delivered():
    client = FakeRedis()
    q = RedisWakeQueue(client=client, namespace="ns", delivered_ttl=123.0)
    q.enqueue(_wake(0))
    q.mark_delivered("r1:loop_done:0")
    # 確定時に wake hash へ EXPIRE が張られる (long-running の残留を自動回収)。
    assert client.expires.get(b"ns:wake:r1:loop_done:0") == 123


def test_redis_no_ttl_when_disabled():
    client = FakeRedis()
    q = RedisWakeQueue(client=client, namespace="ns", delivered_ttl=None)
    q.enqueue(_wake(0))
    q.mark_delivered("r1:loop_done:0")
    assert b"ns:wake:r1:loop_done:0" not in client.expires


def test_redis_distributed_lock_released_after_op():
    client = FakeRedis()
    q = RedisWakeQueue(client=client, namespace="ns")
    q.enqueue(_wake(0))
    # 操作後はロックが解放されている (次の操作が deadlock しない)。
    assert client.get("ns:lock") is None
    q.claim("coord", now=0.0, lease=30.0, owner="o")
    assert client.get("ns:lock") is None


def test_redis_lock_release_only_deletes_own_token():
    """compare-and-delete: 別 token のロックは消さない (失効後に他者が握ったロックを守る)。"""
    client = FakeRedis()
    q = RedisWakeQueue(client=client, namespace="ns")
    client.set("ns:lock", "held-by-other")
    q._release_lock("my-stale-token")  # token 不一致 -> 消さない。
    assert client.get("ns:lock") == b"held-by-other"
    q._release_lock("held-by-other")  # token 一致 -> 消す。
    assert client.get("ns:lock") is None


def test_redis_recipients_registry_pruned_when_drained():
    """recipient の pending が尽きると {ns}:recipients から外れる (registry の無制限増殖防止)。"""
    client = FakeRedis()
    q = RedisWakeQueue(client=client, namespace="ns")
    q.enqueue(_wake(0, recipient="ephemeral"))
    assert client.smembers("ns:recipients") == {b"ephemeral"}
    q.mark_delivered("r1:loop_done:0")  # 確定 -> recipient drained。
    assert client.smembers("ns:recipients") == set()
    assert q.pending() == []
    # 再 enqueue で registry が復活し pending(None) の全走査が壊れない。
    q.enqueue(_wake(1, recipient="ephemeral"))
    assert client.smembers("ns:recipients") == {b"ephemeral"}
    assert [w.id for w in q.pending()] == ["r1:loop_done:1"]


def test_redis_over_transport_pull_fallback():
    clock = ManualClock()
    q = RedisWakeQueue(client=FakeRedis(), namespace="ns")
    t = Transport(q, NullPushBackend(), lease=30.0, time_fn=clock)
    assert t.deliver(_wake(0)) == "queued"
    seen: list[str] = []
    t.poll_and_handle("coord", lambda w: seen.append(w.id))
    assert seen == ["r1:loop_done:0"]
    assert q.state_of("r1:loop_done:0") == DELIVERED


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_open_wake_queue_memory_default():
    assert isinstance(open_wake_queue(), InMemoryWakeQueue)


def test_open_wake_queue_sqlite(tmp_path):
    q = open_wake_queue("sqlite", path=str(tmp_path / "f.db"))
    assert isinstance(q, SqliteWakeQueue)
    q.close()


def test_open_wake_queue_redis_via_client():
    q = open_wake_queue("redis", client=FakeRedis())
    assert isinstance(q, RedisWakeQueue)


def test_open_wake_queue_rejects_unknown_backend():
    with pytest.raises(ValueError):
        open_wake_queue("postgres")


def test_open_wake_queue_memory_rejects_options():
    with pytest.raises(ValueError):
        open_wake_queue("memory", path="x")
