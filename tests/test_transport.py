"""wake 配送 transport の検証 (Issue #23, report.md S5 Phase3)。

中核の成功条件は **「backend 不通でも pull fallback で配送継続」** (report.md S5 Phase3 (b))。
push 一次 / pull fallback / at-most-once / role 別 cadence をそれぞれ実証する。
"""

from __future__ import annotations

import threading
import time

import pytest

from loop_agent.transport import (
    CLAIMED,
    DELIVERED,
    UNDELIVERED,
    CallablePushBackend,
    InMemoryWakeQueue,
    NullPushBackend,
    Transport,
    WAKE_LOOP_DONE,
    Wake,
    cadence_for,
    due_to_poll,
)


class ManualClock:
    """明示的に進めたときだけ時間が動く決定的クロック (lease 失効の検証用)。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wake(i: int, recipient: str = "coordinator") -> Wake:
    return Wake(
        id=f"r1:{WAKE_LOOP_DONE}:{i}",
        kind=WAKE_LOOP_DONE,
        recipient=recipient,
        run_id="r1",
        payload={"n": i},
    )


# -- push 一次 ---------------------------------------------------------------


def test_push_primary_delivers_and_marks_delivered():
    """backend が健全なら push 一次で即配送し、queue 上は DELIVERED になる。"""
    pushed: list[Wake] = []
    backend = CallablePushBackend(lambda w: (pushed.append(w), True)[1])
    queue = InMemoryWakeQueue()
    t = Transport(queue, backend, time_fn=ManualClock())

    route = t.deliver(_wake(0))

    assert route == "push"
    assert [w.id for w in pushed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED
    # push で配送済みなので pull は何も拾わない。
    assert t.poll("coordinator") == []


# -- pull fallback (中核の成功条件) ------------------------------------------


def test_pull_fallback_when_backend_down():
    """backend 不通 (NullPushBackend) でも、pull poll で配送が継続する (中核の成功条件)。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)

    routes = [t.deliver(_wake(i)) for i in range(3)]
    # push は全部失敗 -> 全件 queue 滞留。
    assert routes == ["queued", "queued", "queued"]

    # 受信側は poll_and_handle で claim -> handle -> confirm。push が落ちていても届く。
    seen: list[str] = []
    handled = t.poll_and_handle("coordinator", lambda w: seen.append(w.id))
    assert seen == ["r1:loop_done:0", "r1:loop_done:1", "r1:loop_done:2"]
    assert [w.id for w in handled] == seen
    assert all(queue.state_of(i) == DELIVERED for i in seen)

    # 確定済みは lease を過ぎても再配達されない (at-most-once)。
    clock.advance(100.0)
    assert t.poll_and_handle("coordinator", lambda w: seen.append("DUP")) == []
    assert "DUP" not in seen


def test_pull_fallback_when_no_backend_configured():
    """backend 未設定 (push 一次そのものが無い) でも pull で配送できる。"""
    t = Transport(InMemoryWakeQueue(), backend=None, time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "queued"
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_backend_recovers_midstream():
    """backend が途中で復旧: 復旧前は queue 滞留、復旧後は push 一次に切り替わる。"""
    up = {"ok": False}
    backend = CallablePushBackend(lambda w: up["ok"])
    t = Transport(InMemoryWakeQueue(), backend, time_fn=ManualClock())

    assert t.deliver(_wake(0)) == "queued"  # backend down
    up["ok"] = True
    assert t.deliver(_wake(1)) == "push"  # backend up

    # down 中に積んだぶんは pull で回収できる (配送は途切れない)。
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_push_raising_is_treated_as_failure_not_crash():
    """push backend が例外を投げても Transport は落ちず、pull fallback に委ねる。"""
    def boom(_w: Wake) -> bool:
        raise RuntimeError("backend exploded")

    t = Transport(InMemoryWakeQueue(), CallablePushBackend(boom), time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "queued"
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


# -- at-most-once / 三状態 claim-then-confirm ---------------------------------


def test_duplicate_enqueue_is_idempotent():
    """同一 id の二重 deliver は de-dup され、受信側に二重に届かない。"""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    t.deliver(_wake(0))
    t.deliver(_wake(0))  # 同一 id 再 deliver

    delivered = t.poll("coordinator")
    assert len(delivered) == 1


def test_poll_default_claims_without_confirming():
    """poll の既定 (confirm=False) は claim のみ。確定しないので lease 失効で再配送される。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    claimed = t.poll("coordinator")  # 既定 = confirm しない
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED  # DELIVERED ではない

    # confirm しないまま lease 失効 -> 再配送される (crash recovery)。
    clock.advance(31.0)
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_poll_confirm_true_marks_delivered():
    """confirm=True を明示した poll は返す前に即確定する (単純ケース)。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    got = t.poll("coordinator", confirm=True)
    assert [w.id for w in got] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED
    clock.advance(100.0)
    assert t.poll("coordinator", confirm=True) == []  # 再配達されない


def test_poll_and_handle_confirms_only_on_success():
    """poll_and_handle は handler が成功した wake だけ confirm する。"""
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=ManualClock())
    t.deliver(_wake(0))

    handled = t.poll_and_handle("coordinator", lambda w: None)
    assert [w.id for w in handled] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_poll_and_handle_redelivers_when_handler_crashes():
    """handler が raise した wake は confirm されず、lease 失効後に再配送される (crash-safe)。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    def boom(_w: Wake) -> None:
        raise RuntimeError("handler failed mid-processing")

    # handler の例外は握り潰さず伝播する。
    with pytest.raises(RuntimeError):
        t.poll_and_handle("coordinator", boom)
    # 処理前に死んだ wake は未確定のまま (喪失していない)。
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # lease 失効後に再配送され、今度は成功 handler で確定できる。
    clock.advance(31.0)
    ok: list[str] = []
    handled = t.poll_and_handle("coordinator", lambda w: ok.append(w.id))
    assert ok == ["r1:loop_done:0"]
    assert [w.id for w in handled] == ok
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_claim_then_confirm_requires_explicit_confirm():
    """confirm=False の poll は claim だけ。confirm 前は再 poll で同じ wake を返さない。"""
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=ManualClock())
    t.deliver(_wake(0))

    claimed = t.poll("coordinator", confirm=False)
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED
    # lease 保持中は他の poll が同じ wake を奪わない。
    assert t.poll("coordinator", confirm=False) == []

    n = t.confirm_wakes(claimed, owner="coordinator")
    assert n == 1
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_unconfirmed_claim_is_redelivered_after_lease_expiry():
    """claim 後 confirm せず lease 失効すると、wake は再 eligible になり再配送される。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    claimed = t.poll("coordinator", confirm=False)  # claim, then "crash" (confirm せず)
    assert len(claimed) == 1

    clock.advance(31.0)  # lease 失効
    # 失効後の遅延 confirm は fencing で弾かれる (届いていないので DELIVERED 化しない)。
    assert t.confirm_wakes(claimed, owner="coordinator") == 0
    # 再 poll で回収できる (at-least-once: idle-wake では喪失 > 重複)。
    redelivered = t.poll("coordinator")
    assert [w.id for w in redelivered] == ["r1:loop_done:0"]


def test_owner_fencing_blocks_stale_confirm():
    """lease 失効後に別 owner が再 claim したら、元 owner の confirm は弾かれる。"""
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, NullPushBackend(), lease=30.0, time_fn=clock)
    t.deliver(_wake(0))

    first = t.poll("coordinator", owner="worker-A", confirm=False)
    assert len(first) == 1

    clock.advance(31.0)  # A の lease 失効
    second = t.poll("coordinator", owner="worker-B", confirm=False)  # B が再 claim
    assert len(second) == 1
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # 遅れて来た A の confirm は弾かれ、B の confirm だけが通る (二重確定を防ぐ)。
    assert t.confirm_wakes(first, owner="worker-A") == 0
    assert t.confirm_wakes(second, owner="worker-B") == 1
    assert queue.state_of("r1:loop_done:0") == DELIVERED


def test_redeliver_respects_inflight_claim():
    """CLAIMED 中の wake を再 deliver しても、active claim を push で横取り確定しない。

    backend 復旧後の retry/resume が deliver を再呼びしても、受信側が confirm=False で
    claim 中の wake は CLAIMED のまま残し、owner の lease 失効再配送保護を壊さない
    (codex P2 回帰防止)。
    """
    up = {"ok": False}
    backend = CallablePushBackend(lambda w: up["ok"])
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    t = Transport(queue, backend, lease=30.0, time_fn=clock)

    assert t.deliver(_wake(0)) == "queued"  # backend down -> queued
    claimed = t.poll("coordinator", confirm=False)  # 受信側が claim (処理中)
    assert [w.id for w in claimed] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # backend 復旧後の再 deliver: active claim を尊重して横取りしない。
    up["ok"] = True
    assert t.deliver(_wake(0)) == "queued"
    assert queue.state_of("r1:loop_done:0") == CLAIMED  # まだ CLAIMED のまま

    # owner が confirm 前にクラッシュ -> lease 失効で再配送される (保護が生きている)。
    clock.advance(31.0)
    redelivered = t.poll("coordinator")
    assert [w.id for w in redelivered] == ["r1:loop_done:0"]


def test_inflight_push_does_not_steal_active_claim():
    """push の I/O 中に別 poller が claim したら、push は claim を奪わず wake を喪失させない。

    push が確定配送を返す直前に受信側が同じ wake を claim した状況を再現し、deliver の
    mark_delivered が active CLAIMED を DELIVERED で横取りしないこと (= owner クラッシュ時も
    lease 失効で再配送される) を実証する (codex P2 回帰防止)。
    """
    clock = ManualClock()
    queue = InMemoryWakeQueue()
    box: dict = {}

    def racing_push(w: Wake) -> bool:
        # push が "in flight" の間に受信側が同じ wake を claim する状況を再現。
        box["claimed"] = box["transport"].poll("coordinator", owner="recv", confirm=False)
        return True  # push 自体は成功を返す。

    t = Transport(queue, CallablePushBackend(racing_push), lease=30.0, time_fn=clock)
    box["transport"] = t

    route = t.deliver(_wake(0))
    assert route == "push"  # push は成功した。
    # だが active claim は奪われていない (CLAIMED のまま = pull 側が配送の主体)。
    assert [w.id for w in box["claimed"]] == ["r1:loop_done:0"]
    assert queue.state_of("r1:loop_done:0") == CLAIMED

    # poller が confirm 前にクラッシュしても、lease 失効で再配送される (喪失しない)。
    clock.advance(31.0)
    assert [w.id for w in t.poll("coordinator")] == ["r1:loop_done:0"]


def test_delivered_wake_never_redelivered_even_on_redeliver_attempt():
    """DELIVERED 済み wake を再 deliver しても push を重ねず、pull でも返らない。"""
    pushes: list[str] = []

    def push_ok(w: Wake) -> bool:
        pushes.append(w.id)
        return True

    t = Transport(InMemoryWakeQueue(), CallablePushBackend(push_ok), time_fn=ManualClock())
    assert t.deliver(_wake(0)) == "push"
    assert t.deliver(_wake(0)) == "push"  # 再 deliver
    assert pushes == ["r1:loop_done:0"]  # push は 1 回だけ
    assert t.poll("coordinator") == []


# -- 宛先振り分け ------------------------------------------------------------


def test_poll_only_returns_own_recipient():
    """poll は宛先一致の wake だけ claim する (他者宛は残す)。"""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    t.deliver(_wake(0, recipient="alice"))
    t.deliver(_wake(1, recipient="bob"))

    assert [w.id for w in t.poll("alice")] == ["r1:loop_done:0"]
    assert [w.id for w in t.poll("bob")] == ["r1:loop_done:1"]


def test_poll_limit_bounds_batch():
    """limit は 1 回の poll で引き取る件数を制限する (残りは次回 poll)。"""
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), time_fn=ManualClock())
    for i in range(5):
        t.deliver(_wake(i))
    first = t.poll("coordinator", limit=2)
    assert len(first) == 2
    rest = t.poll("coordinator")
    assert len(rest) == 3


# -- role 別 cadence ---------------------------------------------------------


def test_cadence_values_are_asymmetric_by_role():
    """dispatcher 3m / worker 短間隔 / secretary 0 (毎ターン) の非対称設計。"""
    assert cadence_for("dispatcher") == 180.0
    assert cadence_for("worker") == 60.0
    assert cadence_for("secretary") == 0.0
    # 未知 role は保守的に既定へ落ちる。
    assert cadence_for("unknown-role") == 60.0


def test_due_to_poll_respects_cadence():
    """due_to_poll は cadence 経過後に due。未 poll は常に due。"""
    # 一度も poll していなければ常に due。
    assert due_to_poll("dispatcher", last_poll=None, now=0.0) is True
    # cadence 未経過は due でない。
    assert due_to_poll("dispatcher", last_poll=0.0, now=100.0) is False
    # cadence 経過で due。
    assert due_to_poll("dispatcher", last_poll=0.0, now=180.0) is True
    # secretary は cadence 0 -> 常に due (ターン冒頭で毎回 poll)。
    assert due_to_poll("secretary", last_poll=0.0, now=0.0) is True


# -- 並行 poll のスレッド安全 (二重 claim させない) -------------------------


def test_concurrent_pollers_never_double_claim():
    """同一 recipient を複数スレッドで並行 poll しても、各 wake は高々 1 スレッドが claim する。

    InMemoryWakeQueue の check-and-set がロックで直列化され、並行 poller の
    owner fencing / at-most-once claim が実際に成立することを実時計 + barrier で実証する。
    """
    # 実時計を使い、lease を十分長くして claim が失効しないようにする (確定のみ検証)。
    t = Transport(InMemoryWakeQueue(), NullPushBackend(), lease=3600.0, time_fn=time.monotonic)
    n_wakes = 200
    for i in range(n_wakes):
        t.deliver(_wake(i))

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    claimed_by: list[list[str]] = [[] for _ in range(n_threads)]

    def worker(idx: int) -> None:
        own = f"worker-{idx}"
        barrier.wait()  # 全スレッドを同時にスタートさせ contention を最大化。
        while True:
            got = t.poll("coordinator", owner=own, limit=1)
            if not got:
                # 他スレッドがまだ処理中で一時的に空でも、未確定が残るうちは再試行。
                if not t.pending("coordinator"):
                    return
                continue
            wake = got[0]
            assert t.confirm_wakes(got, owner=own) == 1
            claimed_by[idx].append(wake.id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    all_claimed = [wid for lst in claimed_by for wid in lst]
    # 各 wake は厳密に 1 回だけ claim+confirm された (二重 claim 無し・取りこぼし無し)。
    assert sorted(all_claimed) == sorted(f"r1:loop_done:{i}" for i in range(n_wakes))
    assert len(all_claimed) == len(set(all_claimed)) == n_wakes


# -- 不正入力 ----------------------------------------------------------------


def test_to_dict_canonical_fields_win_over_payload_collisions():
    """payload に予約名 (id/kind/recipient/run_id) が紛れても正準フィールドが勝つ。"""
    w = Wake(
        id="r1:loop_done:0",
        kind=WAKE_LOOP_DONE,
        recipient="coordinator",
        run_id="r1",
        # 悪意/事故で de-dup/routing 鍵に衝突するキーを載せる。
        payload={"id": "EVIL", "recipient": "attacker", "extra": "ok"},
    )
    d = w.to_dict()
    assert d["id"] == "r1:loop_done:0"
    assert d["recipient"] == "coordinator"
    assert d["kind"] == WAKE_LOOP_DONE
    assert d["run_id"] == "r1"
    assert d["extra"] == "ok"  # 衝突しない payload キーは残る。


def test_enqueue_rejects_empty_id():
    q = InMemoryWakeQueue()
    with pytest.raises(ValueError):
        q.enqueue(Wake(id="", kind=WAKE_LOOP_DONE, recipient="x"))


def test_transport_rejects_nonpositive_lease():
    with pytest.raises(ValueError):
        Transport(InMemoryWakeQueue(), lease=0.0)


def test_claim_rejects_nonpositive_lease():
    q = InMemoryWakeQueue()
    with pytest.raises(ValueError):
        q.claim("x", now=0.0, lease=0.0, owner="o")
