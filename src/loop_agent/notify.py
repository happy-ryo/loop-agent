"""HumanGate notification handlers: pluggable Notifiers for approval requests (Issue #39).

Provides a best-effort path for notifying external channels (webhook / Slack / email)
at the exact moment :class:`~loop_agent.gate.HumanGate` registers a **new approval
request (pending)** for an irreversible action. The existing HumanGate API is
unchanged, and ``notifier`` is optional. If omitted, nothing is notified as before
(no-op = pause and wait for an out-of-band human decision).

Design principles (extending report.md's "do not stop the loop core" rule into
the notification layer):

- **best-effort**. Notification delivery failures never stop :class:`HumanGate`.
  Failures are surfaced with ``warnings.warn`` (RuntimeWarning) instead of being
  swallowed silently (the same discipline as sink fan-out in :mod:`loop_agent.events`).
  The approval request itself is already persisted to the store regardless of
  notification delivery, so the loop can proceed if a human makes a decision even
  after notification delivery fails.
- **stdlib only**. Webhooks use :mod:`urllib.request`, and email uses :mod:`smtplib`.
  No optional dependencies are added. Slack is a Slack incoming webhook (= a webhook
  specialization).
- **redaction ON by default**. Because the approval payload includes the raw action,
  sensitive values (token / password / secret, etc.) are masked by default with
  :func:`redact_payload` before sending so they do not leak to external channels
  (replaceable via each Notifier's ``redact`` argument).
- **retry is opt-in**. The default is a single send attempt (``retries=0``). Even when
  retry is enabled, the interval is injected explicitly (``retry_interval`` / ``sleep``)
  so sleep does not block the loop for a long time.

The payload schema is :class:`ApprovalRequest` (action kind / summary / deadline /
creation time), and :meth:`ApprovalRequest.to_dict` returns a JSON-serializable dict.
"""

from __future__ import annotations

import copy
import json
import smtplib
import sys
import time
import warnings
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)
from urllib.request import Request, urlopen

from .errors import ConfigError

# Redaction target key detection: mask values when these substrings appear in payload
# dict keys (including nested keys). Matching is case-insensitive. In production,
# extend this to fit the action structure (or replace the entire policy via each
# Notifier's ``redact`` argument).
DEFAULT_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "access_key",
    "session",
    "cookie",
)

# Replacement string after masking. It indicates that a value exists while hiding it.
REDACTED = "***REDACTED***"

# Callable that receives a payload and returns a redacted copy. The default is
# :func:`redact_payload`. To use a no-op equivalent to ``None``, pass
# ``lambda p: dict(p)``.
Redaction = Callable[[Mapping[str, Any]], dict[str, Any]]

# Callable that derives additional metadata (summary / action_kind / deadline) for an
# approval request. It receives an action and returns override fields for
# :class:`ApprovalRequest` as a dict.
ApprovalDescriber = Callable[[Any], Mapping[str, Any]]


def _summarize_action(action: Any, *, limit: int = 200) -> str:
    """Build the default human-readable one-line action summary (fallback without describe).

    **Do not embed raw values in the summary** (to prevent secret leaks). The summary is
    used directly as the notification heading (webhook JSON ``summary`` / Slack text /
    email subject), and key-based :func:`redact_payload` does not mask it, so values in
    the summary could pass through unredacted (the action body remains as the full
    payload in ``ApprovalRequest.action`` and is redacted there). Therefore:

    - ``str`` actions are used as-is (an action identifier; the value itself is the
      heading).
    - ``Mapping`` actions only use **str values** from known human-readable label keys
      (``summary``/``description``/``kind``/``type``). If none exist, build a structural
      summary from **key names only** (without values). Build richer summaries explicitly
      with ``describe``.
    - Everything else (arbitrary objects) uses **only the type name**, because ``repr``
      may contain secrets.
    """
    if isinstance(action, str):
        text = action
    elif isinstance(action, Mapping):
        for key in ("summary", "description", "kind", "type"):
            value = action.get(key)
            if isinstance(value, str) and value:
                text = value
                break
        else:
            keys = ", ".join(sorted(str(k) for k in action.keys()))
            text = f"action with fields: {keys}" if keys else "action (empty mapping)"
    else:
        text = f"action of type {type(action).__name__}"
    if len(text) > limit:
        return text[:limit] + "..."
    return text


@dataclass(frozen=True)
class ApprovalRequest:
    """Payload schema for one irreversible action that requires human approval.

    Built by :class:`~loop_agent.gate.HumanGate` when it registers a pending request and
    passed to :class:`Notifier`. ``action`` is the proposed action itself, guaranteed by
    the gate to be JSON-native (the source of truth). ``summary`` / ``action_kind`` /
    ``deadline`` are optional metadata for notification headings, classification, and
    deadlines, and can be derived or overridden by the ``describe`` callback.

    Attributes:
        run_id: ID of the target run.
        gate_key: Gate key for this approval request (such as ``"gate-<iteration>"``).
            Unique within a run.
        action: Proposed irreversible action (JSON-native). It **may contain secrets**,
            so it is redacted before notification (:func:`redact_payload`).
        summary: Human-readable one-line summary (notification heading).
        action_kind: Action classification (for example ``"deploy"`` / ``"delete"``).
            Optional.
        deadline: Deadline by which a decision is required (epoch seconds). Optional.
        created_at: Time when the approval request was triggered (epoch seconds).
            Optional.
    """

    run_id: str
    gate_key: str
    action: Any
    summary: str
    action_kind: Optional[str] = None
    deadline: Optional[float] = None
    created_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict (raw payload before redaction).

        Each Notifier passes this through :func:`redact_payload` or equivalent before
        sending.
        """
        return {
            "run_id": self.run_id,
            "gate_key": self.gate_key,
            "action": self.action,
            "summary": self.summary,
            "action_kind": self.action_kind,
            "deadline": self.deadline,
            "created_at": self.created_at,
        }


def redact_payload(
    payload: Mapping[str, Any],
    *,
    sensitive_parts: Sequence[str] = DEFAULT_SENSITIVE_KEY_PARTS,
    mask: str = REDACTED,
) -> dict[str, Any]:
    """Return a **copy** of payload (including nested dict/list values) with secrets masked.

    Dict values whose key name (lowercased) contains any item in ``sensitive_parts`` are
    replaced with ``mask``. The original payload is not modified (deep-copy based). This
    is not universal (it cannot catch secrets embedded in free text that cannot be
    identified by key name), so extend ``sensitive_parts`` for the action structure or
    replace the Notifier's ``redact`` with a dedicated policy as described in the
    docstring.
    """
    lowered = tuple(part.lower() for part in sensitive_parts)

    def _is_sensitive(key: str) -> bool:
        k = key.lower()
        return any(part in k for part in lowered)

    def _walk(value: Any, *, redact_here: bool = False) -> Any:
        if redact_here:
            return mask
        if isinstance(value, Mapping):
            return {
                str(k): _walk(v, redact_here=_is_sensitive(str(k)))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_walk(v) for v in value]
        return value

    # Explicitly convert the top level to dict to guarantee a copy.
    return {
        str(k): _walk(v, redact_here=_is_sensitive(str(k)))
        for k, v in copy.deepcopy(dict(payload)).items()
    }


@runtime_checkable
class Notifier(Protocol):
    """Best-effort notification backend that sends approval requests to external channels.

    ``notify`` has **side effects only** (no return value). It may raise on delivery
    failure (the caller, :class:`~loop_agent.gate.HumanGate`, catches that, converts it
    to a warning, and does not stop the loop). Idempotency is not guaranteed (resume
    TOCTOU may rarely cause duplicate notifications).
    """

    def notify(self, request: ApprovalRequest) -> None: ...


def _send_with_retry(
    send: Callable[[], None],
    *,
    retries: int,
    retry_interval: float,
    sleep: Callable[[float], None],
) -> None:
    """Small helper that tries ``send`` up to ``retries + 1`` times and reraises the last exception.

    Sleeps between attempts only when ``retry_interval > 0`` (default 0 = no blocking).
    """
    attempts = retries + 1
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            send()
            return
        except Exception as exc:  # noqa: BLE001 - reraised after the final attempt
            last_exc = exc
            if i + 1 < attempts and retry_interval > 0:
                sleep(retry_interval)
    assert last_exc is not None  # attempts >= 1, so this is always set
    raise last_exc


class WebhookNotifier:
    """Notifier that POSTs a JSON payload to an arbitrary HTTP webhook (stdlib urllib).

    Args:
        url: Destination URL.
        method: HTTP method (default ``"POST"``).
        headers: Additional headers. ``Content-Type: application/json`` is added
            automatically (explicit values take precedence).
        timeout: Timeout in seconds for one send attempt (default 5.0). The default is
            intentionally short so the loop does not wait for long.
        redact: Callable that transforms the payload before sending (default
            :func:`redact_payload`).
        retries: Additional attempts after failure (default 0 = one attempt only).
        retry_interval: Sleep seconds between attempts (default 0.0 = do not sleep).
        sleep: Injection point for sleep between attempts (for tests, default
            :func:`time.sleep`).
    """

    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 5.0,
        redact: Redaction = redact_payload,
        retries: int = 0,
        retry_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if retries < 0:
            raise ConfigError("retries must be >= 0")
        self.url = url
        self.method = method
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.redact = redact
        self.retries = retries
        self.retry_interval = retry_interval
        self.sleep = sleep

    def _build_body(self, request: ApprovalRequest) -> bytes:
        """Convert the redacted payload to JSON bytes (overridden by Slack and similar specializations)."""
        payload = self.redact(request.to_dict())
        return json.dumps(payload).encode("utf-8")

    def notify(self, request: ApprovalRequest) -> None:
        body = self._build_body(request)
        headers = {"Content-Type": "application/json", **self.headers}
        http_request = Request(
            self.url, data=body, headers=headers, method=self.method
        )

        def _send() -> None:
            # Close the value returned by urlopen (context manager) immediately. Body is unused.
            with urlopen(http_request, timeout=self.timeout):
                pass

        _send_with_retry(
            _send,
            retries=self.retries,
            retry_interval=self.retry_interval,
            sleep=self.sleep,
        )


def _format_slack_text(payload: Mapping[str, Any]) -> str:
    """Format a redacted payload as the ``text`` body for a Slack incoming webhook.

    Presents the heading (summary) + metadata (kind / deadline / gate / run) + action
    body (JSON) in a human-readable layout. The full body is already redacted, so
    secrets are masked.
    """
    lines = [f":warning: *Approval required* - {payload.get('summary', '(no summary)')}"]
    kind = payload.get("action_kind")
    if kind:
        lines.append(f"*kind*: {kind}")
    deadline = payload.get("deadline")
    if deadline is not None:
        lines.append(f"*deadline*: {deadline}")
    lines.append(f"*gate*: {payload.get('gate_key')} (run {payload.get('run_id')})")
    action = payload.get("action")
    if action is not None:
        lines.append("*action*:\n```\n" + json.dumps(action, indent=2) + "\n```")
    return "\n".join(lines)


class SlackNotifier(WebhookNotifier):
    """Notifier for Slack incoming webhooks (:class:`WebhookNotifier` specialization).

    Slack incoming webhooks expect a ``{"text": ...}`` shape, so this overrides the
    body builder and formats the redacted payload as Slack message text
    (:func:`_format_slack_text`). The POST mechanism / timeout / retry / redaction are
    shared with the parent class.

    Args:
        webhook_url: Slack incoming webhook URL.
        text_formatter: Callable that formats payload (redacted) -> Slack ``text``
            (default :func:`_format_slack_text`). Can be replaced with richer formats
            such as ``blocks``.
        Other arguments (``timeout`` / ``redact`` / ``retries`` ...) are the same as
            :class:`WebhookNotifier`.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        text_formatter: Callable[[Mapping[str, Any]], str] = _format_slack_text,
        timeout: float = 5.0,
        redact: Redaction = redact_payload,
        retries: int = 0,
        retry_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            webhook_url,
            method="POST",
            headers=headers,
            timeout=timeout,
            redact=redact,
            retries=retries,
            retry_interval=retry_interval,
            sleep=sleep,
        )
        self.text_formatter = text_formatter

    def _build_body(self, request: ApprovalRequest) -> bytes:
        payload = self.redact(request.to_dict())
        return json.dumps({"text": self.text_formatter(payload)}).encode("utf-8")


def _format_email_body(payload: Mapping[str, Any]) -> str:
    """Format a redacted payload as an email body (plain text)."""
    lines = [
        f"Approval required: {payload.get('summary', '(no summary)')}",
        "",
        f"run_id:      {payload.get('run_id')}",
        f"gate_key:    {payload.get('gate_key')}",
    ]
    if payload.get("action_kind"):
        lines.append(f"action_kind: {payload.get('action_kind')}")
    if payload.get("deadline") is not None:
        lines.append(f"deadline:    {payload.get('deadline')}")
    if payload.get("created_at") is not None:
        lines.append(f"created_at:  {payload.get('created_at')}")
    lines.append("")
    lines.append("action:")
    lines.append(json.dumps(payload.get("action"), indent=2, ensure_ascii=False))
    return "\n".join(lines)


class EmailNotifier:
    """Notifier that sends approval requests by email over SMTP (stdlib smtplib).

    Args:
        host: SMTP server host.
        sender: Sender address.
        recipients: Recipient address sequence (at least one item).
        port: SMTP port (default 25).
        subject_prefix: Subject prefix (default ``"[loop-agent] Approval required"``).
            The subject is ``"<prefix>: <summary>"``.
        username/password: When specified, call ``SMTP.login``.
        use_tls: When ``True``, call ``starttls()`` after connecting (default False).
        timeout: SMTP connection timeout in seconds (default 10.0).
        redact: Payload transformation before sending (default :func:`redact_payload`).
        body_formatter: Callable that formats payload (redacted) -> plain-text body.
        smtp_factory: Factory that returns ``SMTP(host, port, timeout=...)``
            (replaceable in tests). Defaults to :class:`smtplib.SMTP`.
        retries/retry_interval/sleep: Same best-effort retry behavior as
            :class:`WebhookNotifier`.
    """

    def __init__(
        self,
        *,
        host: str,
        sender: str,
        recipients: Iterable[str],
        port: int = 25,
        subject_prefix: str = "[loop-agent] Approval required",
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = False,
        timeout: float = 10.0,
        redact: Redaction = redact_payload,
        body_formatter: Callable[[Mapping[str, Any]], str] = _format_email_body,
        smtp_factory: Optional[Callable[..., smtplib.SMTP]] = None,
        retries: int = 0,
        retry_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if retries < 0:
            raise ConfigError("retries must be >= 0")
        recipients = list(recipients)
        if not recipients:
            raise ConfigError("recipients must contain at least one address")
        self.host = host
        self.sender = sender
        self.recipients = recipients
        self.port = port
        self.subject_prefix = subject_prefix
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.timeout = timeout
        self.redact = redact
        self.body_formatter = body_formatter
        self.smtp_factory = smtp_factory if smtp_factory is not None else smtplib.SMTP
        self.retries = retries
        self.retry_interval = retry_interval
        self.sleep = sleep

    def _build_message(self, request: ApprovalRequest) -> EmailMessage:
        payload = self.redact(request.to_dict())
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        # Use the **post-redaction** summary for the subject as well as the body. If a
        # custom redactor scrubs summary, this prevents leakage through the subject
        # (SMTP header).
        subject_summary = payload.get("summary", request.summary)
        message["Subject"] = f"{self.subject_prefix}: {subject_summary}"
        message.set_content(self.body_formatter(payload))
        return message

    def notify(self, request: ApprovalRequest) -> None:
        message = self._build_message(request)

        def _send() -> None:
            smtp = self.smtp_factory(self.host, self.port, timeout=self.timeout)
            try:
                if self.use_tls:
                    smtp.starttls()
                if self.username is not None:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
            finally:
                smtp.quit()

        _send_with_retry(
            _send,
            retries=self.retries,
            retry_interval=self.retry_interval,
            sleep=self.sleep,
        )


class ConsoleNotifier:
    """Notifier that writes approval requests to a stream (default stderr) as one-line JSON.

    Intended for dependency-free debugging/local operation. The default is
    ``ensure_ascii=True`` (escape non-ASCII as ``\\uXXXX``) to avoid crashes on cp932
    consoles.
    """

    def __init__(
        self,
        *,
        stream: Any = None,
        redact: Redaction = redact_payload,
        ensure_ascii: bool = True,
    ) -> None:
        self.stream = stream
        self.redact = redact
        self.ensure_ascii = ensure_ascii

    def notify(self, request: ApprovalRequest) -> None:
        stream = self.stream if self.stream is not None else sys.stderr
        payload = self.redact(request.to_dict())
        stream.write(
            "approval-required " + json.dumps(payload, ensure_ascii=self.ensure_ascii) + "\n"
        )


@dataclass
class MultiNotifier:
    """Notifier that fans out to multiple Notifiers on a best-effort basis.

    Calls each backend independently and continues sending to the rest even if one
    fails (failures are surfaced with ``warnings.warn``). Useful for sending to webhook
    and email at the same time, for example.
    """

    notifiers: Sequence[Notifier] = field(default_factory=tuple)

    def notify(self, request: ApprovalRequest) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(request)
            except Exception as exc:  # noqa: BLE001 - fan-out is best-effort
                warnings.warn(
                    f"notifier {type(notifier).__name__} failed for gate "
                    f"{request.gate_key!r}: {type(exc).__name__}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )


__all__ = [
    "ApprovalRequest",
    "ApprovalDescriber",
    "Notifier",
    "Redaction",
    "redact_payload",
    "DEFAULT_SENSITIVE_KEY_PARTS",
    "REDACTED",
    "WebhookNotifier",
    "SlackNotifier",
    "EmailNotifier",
    "ConsoleNotifier",
    "MultiNotifier",
]
