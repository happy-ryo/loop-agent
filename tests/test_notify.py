"""Validate HumanGate notification handlers (Issue #39): webhook/Slack/email backends + redaction + integration.

(a) redaction masks sensitive values in payloads (including nested values) without mutating the original,
(b) WebhookNotifier POSTs the correct URL/headers/JSON body to monkeypatched urlopen,
(c) SlackNotifier sends the Slack incoming webhook shape ({"text": ...}),
(d) EmailNotifier uses the injected smtp_factory to starttls/login/send,
(e) ConsoleNotifier / MultiNotifier output and fan out on a best-effort basis,
(f) retry works when opted in, and failures raise exceptions (the caller is expected to catch them),
(g) HumanGate integration: notifier.notify is called when an approval request is triggered,
    does not stop the loop because it is best-effort, does not notify again on resume,
    and describe can override payload metadata.
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


# -- summary fallback does not leak raw values (P1 regression) ----------------


def test_summary_fallback_does_not_leak_mapping_values():
    # A mapping without known label keys gets a structural summary of keys only; values (secrets) are not emitted.
    summary = _summarize_action({"token": "s3cr3t", "op": "delete"})
    assert "s3cr3t" not in summary
    assert "op" in summary and "token" in summary


def test_summary_uses_label_keys_and_type_fallback():
    assert _summarize_action({"kind": "deploy"}) == "deploy"
    assert _summarize_action("deploy") == "deploy"

    class Weird:
        def __repr__(self):  # Do not use repr because it may contain secrets.
            return "Weird(secret=hunter2)"

    summary = _summarize_action(Weird())
    assert "hunter2" not in summary
    assert "Weird" in summary


# -- Test helpers ------------------------------------------------------------


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
    """Test double that only records ApprovalRequests passed to notify."""

    def __init__(self):
        self.requests = []

    def notify(self, request):
        self.requests.append(request)


class BoomNotifier:
    """Notifier that always raises an exception (for best-effort path checks)."""

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
    # The default token remains because it is not in this sensitive override; only ssn is masked.
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
    # Content-Type is added automatically (the header key is capitalized).
    assert captured["headers"].get("Content-type") == "application/json"
    body = json.loads(captured["body"].decode("utf-8"))
    assert body["summary"] == "deploy to prod"
    assert body["gate_key"] == "gate-1"
    # redaction: action.token is masked.
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
    # Slack incoming webhook shape: {"text": ...}.
    assert set(body.keys()) == {"text"}
    assert "deploy to prod" in body["text"]
    assert "gate-1" in body["text"]
    # action JSON is embedded after redaction (token masked).
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
    # redaction: token is masked, and the raw secret is not present in the body.
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
    # quit is called in finally (prevents connection leaks).
    assert _FakeSMTP.instances[0].quit_called is True


def test_email_subject_uses_redacted_summary():
    # When a custom redact function scrubs summary, the subject is redacted too (P2 regression).
    _FakeSMTP.instances = []

    def scrub_summary(payload):
        out = dict(payload)
        out["summary"] = "[scrubbed]"
        return out

    notifier = EmailNotifier(
        host="h",
        sender="s@x",
        recipients=["r@x"],
        redact=scrub_summary,
        smtp_factory=_FakeSMTP,
    )
    notifier.notify(make_request(summary="leak me prod-secret"))
    msg = _FakeSMTP.instances[0].sent[0]
    assert "prod-secret" not in msg["Subject"]
    assert "[scrubbed]" in msg["Subject"]


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
    # Both surrounding notifiers receive the request even when the middle one fails; the failure becomes a warning.
    assert good1.requests == [req]
    assert good2.requests == [req]
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


# -- (g) HumanGate integration ----------------------------------------------


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
    # Notifies exactly once when the "deploy" approval request is triggered.
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
    # Even if notification fails, the approval request is registered and the loop pauses normally.
    assert notifier.calls == 1
    assert res.paused is True
    assert res.pending["gate_key"] == "gate-1"
    assert executed == ["work"]
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


def test_gate_does_not_renotify_on_resume(tmp_path):
    db_path = tmp_path / "s.db"
    # run1: pause + one notification.
    conn1 = connect(db_path)
    store1 = LoopStore(conn1)
    gather1, act1, _ = make_world(ACTIONS)
    n1 = RecordingNotifier()
    gate1 = HumanGate(on=is_deploy, store=store1, run_id=RUN_ID, notifier=n1)
    run_loop(act=act1, verify=never_done, conditions=[MaxIterations(3)],
             gather=gather1, gate=gate1)
    assert len(n1.requests) == 1
    conn1.close()

    # A human resolves it through a separate connection.
    conn2 = connect(db_path)
    store2 = LoopStore(conn2)
    store2.resolve_decision(RUN_ID, "gate-1", "approve")

    # run2: resume. It reads the existing decision, so it does not notify again.
    gather2, act2, executed2 = make_world(ACTIONS)
    n2 = RecordingNotifier()
    gate2 = HumanGate(on=is_deploy, store=store2, run_id=RUN_ID, notifier=n2)
    run_loop(act=act2, verify=never_done, conditions=[MaxIterations(3)],
             gather=gather2, gate=gate2)
    conn2.close()
    assert n2.requests == []  # No repeated notification on resume.
    assert executed2 == ["work", "deploy", "work2"]


def test_gate_does_not_notify_when_resolver_present(tmp_path):
    # With a resolver, synchronous inline resolution avoids human waiting, so no notification is sent (P2 regression).
    from loop_agent import Decision

    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    gather, act, executed = make_world(ACTIONS)
    notifier = RecordingNotifier()
    gate = HumanGate(
        on=is_deploy, store=store, run_id=RUN_ID, notifier=notifier,
        resolver=lambda pending: Decision("approve"),
    )
    res = run_loop(
        act=act, verify=never_done, conditions=[MaxIterations(3)],
        gather=gather, gate=gate,
    )
    # The resolver approves "deploy" inline, so the loop finishes. Notification is not triggered.
    assert res.status == "stopped"
    assert executed == ["work", "deploy", "work2"]
    assert notifier.requests == []


def test_register_decision_reports_created_flag(tmp_path):
    conn = connect(tmp_path / "s.db")
    store = LoopStore(conn)
    store.load_or_init(RUN_ID)
    row1, created1 = store.register_decision(RUN_ID, "g1", "deploy")
    row2, created2 = store.register_decision(RUN_ID, "g1", "deploy")
    assert created1 is True and created2 is False
    assert row1["gate_key"] == row2["gate_key"] == "g1"


def test_gate_does_not_notify_when_register_loses_race(tmp_path, monkeypatch):
    # Simulates a TOCTOU race where get_decision sees None and register_decision returns
    # created=False (loser): notification must not be triggered in this case (P2 regression).
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
    assert notifier.requests == []  # The loser does not notify again.
    assert review.disposition  # Returns with pause (reads the registered pending decision).


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
    # Even if describe fails, the notification path handles it and the loop pauses. notify is not reached.
    assert res.paused is True
    assert notifier.requests == []
    assert any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)
