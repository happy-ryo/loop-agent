"""HumanGate 通知ハンドラ (Issue #39) の検証: webhook/Slack/email backend + redaction + 統合.

(a) redaction が payload (ネスト含む) の機密値をマスクし元を壊さない、
(b) WebhookNotifier が monkeypatch urlopen へ正しい URL/headers/JSON body を POST する、
(c) SlackNotifier が Slack incoming webhook 形 ({"text": ...}) を送る、
(d) EmailNotifier が注入 smtp_factory で starttls/login/送信を行う、
(e) ConsoleNotifier / MultiNotifier の出力・fan-out best-effort、
(f) retry が opt-in で動き、失敗は例外送出 (呼び出し元が捕捉する契約) になる、
(g) HumanGate 統合: 承認要求発火時に notifier.notify が呼ばれ、best-effort で loop を止めない、
    resume 再訪では再通知しない、describe で payload メタを上書きできる、
ことを実証する。
"""

from __future__ import annotations

import io
import json

import pytest

from loop_agent import (
    ActOutcome,
    ApprovalRequest,
    ConsoleNotifier,
    EmailNotifier,
    HumanGate,
    LoopStore,
    MaxIterations,
    MultiNotifier,
    SlackNotifier,
    WebhookNotifier,
    connect,
    redact_payload,
    run_loop,
)
from loop_agent import notify as notify_mod
from loop_agent.notify import _summarize_action
from conftest import never_done


# -- summary フォールバックは生の値を漏らさない (P1 回帰) ----------------------


def test_summary_fallback_does_not_leak_mapping_values():
    # 既知ラベルキーが無い mapping → キー名だけの構造要約。値 (secret) は出さない。
    summary = _summarize_action({"token": "s3cr3t", "op": "delete"})
    assert "s3cr3t" not in summary
    assert "op" in summary and "token" in summary


def test_summary_uses_label_keys_and_type_fallback():
    assert _summarize_action({"kind": "deploy"}) == "deploy"
    assert _summarize_action("deploy") == "deploy"

    class Weird:
        def __repr__(self):  # 機密を含みうる repr は使わない。
            return "Weird(secret=hunter2)"

    summary = _summarize_action(Weird())
    assert "hunter2" not in summary
    assert "Weird" in summary


# -- テスト用ヘルパ -----------------------------------------------------------


def make_request(**overrides):
    base = dict(
        run_id="run-1",
        gate_key="gate-1",
        action={"kind": "deploy", "token": "s3cr3t"},
        summary="deploy to prod",
        action_kind="deploy",
        deadline=123.0,
        created_at=100.0,
    )
    base.update(overrides)
    return ApprovalRequest(**base)


class RecordingNotifier:
    """notify された ApprovalRequest を記録するだけの test double。"""

    def __init__(self):
        self.requests = []

    def notify(self, request):
        self.requests.append(request)


class BoomNotifier:
    """常に例外を投げる notifier (best-effort 経路の検証用)。"""

    def __init__(self):
        self.calls = 0

    def notify(self, request):
        self.calls += 1
        raise RuntimeError("boom")


# -- (a) redaction -----------------------------------------------------------


def test_redact_masks_sensitive_keys_recursively():
    payload = {
        "summary": "ok",
        "password": "p",
        "action": {"token": "t", "nested": {"api_key": "k", "safe": 1}},
        "list": [{"secret": "s"}, {"keep": 2}],
    }
    out = redact_payload(payload)
    assert out["summary"] == "ok"
    assert out["password"] == notify_mod.REDACTED
    assert out["action"]["token"] == notify_mod.REDACTED
    assert out["action"]["nested"]["api_key"] == notify_mod.REDACTED
    assert out["action"]["nested"]["safe"] == 1
    assert out["list"][0]["secret"] == notify_mod.REDACTED
    assert out["list"][1]["keep"] == 2


def test_redact_does_not_mutate_input():
    payload = {"token": "t", "nested": {"secret": "s"}}
    redact_payload(payload)
    assert payload == {"token": "t", "nested": {"secret": "s"}}


def test_redact_custom_sensitive_parts():
    payload = {"token": "t", "ssn": "123"}
    out = redact_payload(payload, sensitive_parts=("ssn",))
    # 既定 token は今回 sensitive 指定に無いので残り、ssn だけマスク。
    assert out["token"] == "t"
    assert out["ssn"] == notify_mod.REDACTED


# -- (b) WebhookNotifier -----------------------------------------------------


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_webhook_posts_json_with_redaction(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(notify_mod, "urlopen", fake_urlopen)
    notifier = WebhookNotifier("https://example.com/hook", timeout=3.0)
    notifier.notify(make_request())

    assert captured["url"] == "https://example.com/hook"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 3.0
    # Content-Type は自動付与 (header キーは capitalize される)。
    assert captured["headers"].get("Content-type") == "application/json"
    body = json.loads(captured["body"].decode("utf-8"))
    assert body["summary"] == "deploy to prod"
    assert body["gate_key"] == "gate-1"
    # redaction: action.token がマスクされている。
    assert body["action"]["token"] == notify_mod.REDACTED
    assert body["action"]["kind"] == "deploy"


def test_webhook_custom_headers_and_method(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.header_items())
        captured["method"] = request.get_method()
        return _FakeResponse()

    monkeypatch.setattr(notify_mod, "urlopen", fake_urlopen)
    notifier = WebhookNotifier(
        "https://example.com/hook",
        method="PUT",
        headers={"Authorization": "Bearer x"},
    )
    notifier.notify(make_request())
    assert captured["method"] == "PUT"
    assert captured["headers"].get("Authorization") == "Bearer x"


def test_webhook_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("transient")
        return _FakeResponse()

    sleeps = []
    monkeypatch.setattr(notify_mod, "urlopen", flaky_urlopen)
    notifier = WebhookNotifier(
        "https://example.com/hook",
        retries=2,
        retry_interval=0.01,
        sleep=sleeps.append,
    )
    notifier.notify(make_request())
    assert calls["n"] == 3
    assert sleeps == [0.01, 0.01]


def test_webhook_raises_after_exhausting_retries(monkeypatch):
    def always_fail(request, timeout=None):
        raise OSError("down")

    monkeypatch.setattr(notify_mod, "urlopen", always_fail)
    notifier = WebhookNotifier("https://example.com/hook", retries=1, sleep=lambda s: None)
    with pytest.raises(OSError):
        notifier.notify(make_request())


def test_webhook_rejects_negative_retries():
    with pytest.raises(ValueError):
        WebhookNotifier("https://example.com/hook", retries=-1)


# -- (c) SlackNotifier -------------------------------------------------------


def test_slack_posts_text_payload(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = request.data
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr(notify_mod, "urlopen", fake_urlopen)
    notifier = SlackNotifier("https://hooks.slack.com/services/XXX")
    notifier.notify(make_request())

    assert captured["url"] == "https://hooks.slack.com/services/XXX"
    body = json.loads(captured["body"].decode("utf-8"))
    # Slack incoming webhook 形: {"text": ...}。
    assert set(body.keys()) == {"text"}
    assert "deploy to prod" in body["text"]
    assert "gate-1" in body["text"]
    # action JSON は redaction 後 (token マスク) で埋め込まれている。
    assert notify_mod.REDACTED in body["text"]
    assert "s3cr3t" not in body["text"]


# -- (d) EmailNotifier -------------------------------------------------------


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = False
        self.login_args = None
        self.sent = []
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


def test_email_builds_and_sends_message():
    _FakeSMTP.instances = []
    notifier = EmailNotifier(
        host="smtp.example.com",
        port=587,
        sender="bot@example.com",
        recipients=["a@example.com", "b@example.com"],
        username="bot",
        password="pw",
        use_tls=True,
        smtp_factory=_FakeSMTP,
    )
    notifier.notify(make_request())

    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.com" and smtp.port == 587
    assert smtp.tls is True
    assert smtp.login_args == ("bot", "pw")
    assert smtp.quit_called is True
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["From"] == "bot@example.com"
    assert msg["To"] == "a@example.com, b@example.com"
    assert "deploy to prod" in msg["Subject"]
    body = msg.get_content()
    # redaction: token はマスクされ、生 secret は本文に出ない。
    assert notify_mod.REDACTED in body
    assert "s3cr3t" not in body


def test_email_quits_even_on_send_failure():
    class _FailingSMTP(_FakeSMTP):
        def send_message(self, message):
            raise OSError("smtp down")

    _FakeSMTP.instances = []
    notifier = EmailNotifier(
        host="h",
        sender="s@x",
        recipients=["r@x"],
        smtp_factory=_FailingSMTP,
    )
    with pytest.raises(OSError):
        notifier.notify(make_request())
    # finally で quit されている (接続リーク防止)。
    assert _FakeSMTP.instances[0].quit_called is True


def test_email_requires_recipients():
    with pytest.raises(ValueError):
        EmailNotifier(host="h", sender="s@x", recipients=[])


# -- (e) Console / Multi -----------------------------------------------------


def test_console_writes_json_line():
    stream = io.StringIO()
    ConsoleNotifier(stream=stream).notify(make_request())
    line = stream.getvalue()
    assert line.startswith("approval-required ")
    payload = json.loads(line[len("approval-required "):])
    assert payload["gate_key"] == "gate-1"
    assert payload["action"]["token"] == notify_mod.REDACTED


def test_multi_fans_out_and_survives_one_failure(recwarn):
    good1 = RecordingNotifier()
    good2 = RecordingNotifier()
    multi = MultiNotifier(notifiers=[good1, BoomNotifier(), good2])
    req = make_request()
    multi.notify(req)
    # 中央が失敗しても両端へ届く。失敗は warning 化。
    assert good1.requests == [req]
    assert good2.requests == [req]
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


# -- (g) HumanGate 統合 ------------------------------------------------------


def make_world(actions):
    executed = []

    def gather(state):
        return actions[state.iteration]

    def act(action):
        executed.append(action)
        return ActOutcome(observation=action, tokens=0)

    return gather, act, executed


def is_deploy(action) -> bool:
    return action == "deploy"


ACTIONS = ["work", "deploy", "work2"]
RUN_ID = "run-notify"


def test_gate_notifies_on_approval_request(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(ACTIONS)
    notifier = RecordingNotifier()
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, notifier=notifier)
    res = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    assert res.paused is True
    # "deploy" の承認要求発火時に 1 度だけ通知される。
    assert len(notifier.requests) == 1
    req = notifier.requests[0]
    assert req.run_id == RUN_ID
    assert req.gate_key == "gate-1"
    assert req.action == "deploy"
    assert req.summary == "deploy"


def test_gate_does_not_notify_for_reversible(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(["work", "work2"])
    notifier = RecordingNotifier()
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, notifier=notifier)
    run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(2)],
        gather=gather, gate=gate,
    )
    assert notifier.requests == []


def test_gate_notify_failure_does_not_stop_loop(tmp_path, recwarn):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    notifier = BoomNotifier()
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, notifier=notifier)
    res = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # 通知が失敗しても承認要求は登録され、loop は通常通り pause する。
    assert notifier.calls == 1
    assert res.paused is True
    assert res.pending["gate_key"] == "gate-1"
    assert executed == ["work"]
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


def test_gate_does_not_renotify_on_resume(tmp_path):
    db_path = tmp_path / "s.db"
    # run1: pause + 1 度通知。
    conn1 = connect(db_path)
    store1 = LoopStore(conn1)
    gather1, act1, _ = make_world(ACTIONS)
    n1 = RecordingNotifier()
    gate1 = HumanGate(on=is_deploy, store=store1, run_id=RUN_ID, notifier=n1)
    run_loop(act=act1, verify=never_done, conditions=[MaxIterations(3)],
             gather=gather1, gate=gate1)
    assert len(n1.requests) == 1
    conn1.close()

    # 人間が別接続で resolve。
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-1", "approve")

    # run2: resume。既存 decision を読むので再通知しない。
    gather2, act2, executed2 = make_world(ACTIONS)
    n2 = RecordingNotifier()
    gate2 = HumanGate(on=is_deploy, store=store2, run_id=RUN_ID, notifier=n2)
    run_loop(act=act2, verify=never_done, conditions=[MaxIterations(3)],
             gather=gather2, gate=gate2)
    conn2.close()
    assert n2.requests == []  # resume では再通知なし
    assert executed2 == ["work", "deploy", "work2"]


def test_register_decision_reports_created_flag(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    row1, created1 = store.register_decision(RUN_ID, "g1", "deploy")
    row2, created2 = store.register_decision(RUN_ID, "g1", "deploy")
    assert created1 is True and created2 is False
    assert row1["gate_key"] == row2["gate_key"] == "g1"


def test_gate_does_not_notify_when_register_loses_race(tmp_path, monkeypatch):
    # get_decision で None を見た後、register_decision が created=False (敗者) を返す
    # TOCTOU レースを模す: このとき通知を発火しないこと (P2 回帰)。
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    notifier = RecordingNotifier()
    gate = HumanGate(on=is_deploy, store=store, run_id=RUN_ID, notifier=notifier)

    loser_entry = {
        "gate_key": "gate-1",
        "action": "deploy",
        "status": "pending",
        "decision": None,
        "payload": None,
    }
    monkeypatch.setattr(store, "get_decision", lambda run_id, gate_key: None)
    monkeypatch.setattr(
        store, "register_decision", lambda run_id, gate_key, action: (loser_entry, False)
    )

    from loop_agent.state import LoopState

    review = gate.review("deploy", LoopState(iteration=1))
    assert notifier.requests == []  # 敗者は再通知しない
    assert review.disposition  # pause で返る (登録済み pending を読む)


def test_gate_describe_overrides_request_metadata(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(ACTIONS)
    notifier = RecordingNotifier()

    def describe(action):
        return {"summary": "PROD deploy!", "action_kind": "deploy", "deadline": 999.0}

    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID,
        notifier=notifier, describe=describe, now_fn=lambda: 42.0,
    )
    run_loop(act=act, verify=never_done, conditions=[MaxIterations(3)],
             gather=gather, gate=gate)
    req = notifier.requests[0]
    assert req.summary == "PROD deploy!"
    assert req.action_kind == "deploy"
    assert req.deadline == 999.0
    assert req.created_at == 42.0


def test_gate_describe_failure_is_best_effort(tmp_path, recwarn):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, _ = make_world(ACTIONS)
    notifier = RecordingNotifier()

    def describe(action):
        raise ValueError("bad describe")

    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID,
        notifier=notifier, describe=describe,
    )
    res = run_loop(act=act, verify=never_done, conditions=[MaxIterations(3)],
                   gather=gather, gate=gate)
    # describe が落ちても通知経路で握られ loop は pause する。notify には届かない。
    assert res.paused is True
    assert notifier.requests == []
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)
