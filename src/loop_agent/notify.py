"""HumanGate 通知ハンドラ: 承認要求を人間へ届ける pluggable Notifier (Issue #39).

:class:`~loop_agent.gate.HumanGate` が不可逆 action に **新しい承認要求 (pending) を
登録した瞬間** に、外部チャネル (webhook / Slack / email) へ best-effort で通知する経路を
与える。既存の HumanGate API は不変で、``notifier`` は optional。未指定なら従来通り
何も通知しない (no-op = pause して人間の out-of-band 解決を待つだけ)。

設計原則 (report.md の「loop コアを止めない」を通知層へ延伸):

- **best-effort**。通知の送信失敗は :class:`HumanGate` を一切止めない。失敗は
  ``warnings.warn`` (RuntimeWarning) で可視化し、サイレントには握り潰さない
  (:mod:`loop_agent.events` の sink fan-out と同じ規律)。通知の有無に関わらず承認要求
  自体は store に永続化済みなので、通知が落ちても人間が決定を下せば loop は進む。
- **stdlib のみ**。webhook は :mod:`urllib.request`、email は :mod:`smtplib`。optional
  dependency を増やさない。Slack は Slack incoming webhook (= webhook の specialization)。
- **redaction を既定 ON**。承認 payload には action がそのまま載るため、機密値
  (token / password / secret 等) が外部チャネルへ漏れないよう、送信前に
  :func:`redact_payload` で既定マスクする (各 Notifier の ``redact`` で差し替え可)。
- **retry は opt-in**。既定は単発送信 (``retries=0``)。retry を有効化しても sleep で
  loop を長くブロックしないよう、間隔は明示注入する (``retry_interval`` / ``sleep``)。

payload schema は :class:`ApprovalRequest` (action 種別 / 要約 / 期限 / 生成時刻) で、
:meth:`ApprovalRequest.to_dict` が JSON シリアライズ可能な dict を返す。
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

# redaction の対象キー判定: payload の (ネストを含む) dict キー名にこれらの部分文字列が
# 含まれていれば値をマスクする。大文字小文字は無視する。実運用では action の構造に
# 合わせて拡張すること (各 Notifier の ``redact`` を差し替えれば policy を丸ごと交換可)。
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

# マスク後の置換文字列。値の存在は示しつつ中身は伏せる。
REDACTED = "***REDACTED***"

# payload を受け取り redaction 済みコピーを返す callable。デフォルトは
# :func:`redact_payload`。``None`` 相当の無加工を使うなら ``lambda p: dict(p)`` を渡す。
Redaction = Callable[[Mapping[str, Any]], dict[str, Any]]

# 承認要求から追加メタ情報 (summary / action_kind / deadline) を導く callable。
# action を受け取り、:class:`ApprovalRequest` の上書きフィールドを dict で返す。
ApprovalDescriber = Callable[[Any], Mapping[str, Any]]


def _summarize_action(action: Any, *, limit: int = 200) -> str:
    """action から既定の人間可読 1 行要約を作る (describe 未指定時のフォールバック)。

    **要約に生の値を埋め込まない** (機密漏洩防止)。要約はそのまま通知の見出し
    (webhook JSON の ``summary`` / Slack text / email 件名) に載り、key ベースの
    :func:`redact_payload` ではマスクされないため、値を入れると機密が素通りしうる
    (action 本体は ``ApprovalRequest.action`` に full payload として残り、そちらは redaction
    される)。よって:

    - ``str`` の action はそのまま (action 識別子。値そのものが見出し)。
    - ``Mapping`` は人間可読ラベル用の既知キー (``summary``/``description``/``kind``/
      ``type``) の **str 値のみ** 拾う。無ければ **キー名だけ** を並べた構造要約にする
      (値は出さない)。リッチな要約が要るなら ``describe`` で明示的に組むこと。
    - それ以外 (任意オブジェクト) は ``repr`` が機密を含みうるので **型名だけ**。
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
    """人間に承認を求める 1 件の不可逆 action の payload schema。

    :class:`~loop_agent.gate.HumanGate` が pending 登録時に構築し、:class:`Notifier`
    に渡す。``action`` は gate が JSON ネイティブを保証した提案 action そのもの
    (正本)。``summary`` / ``action_kind`` / ``deadline`` は通知の見出し・分類・期限
    のための任意メタで、``describe`` callback で導出・上書きできる。

    Attributes:
        run_id: 対象 run の ID。
        gate_key: この承認要求のゲートキー (``"gate-<iteration>"`` 等)。run 内で一意。
        action: 提案された不可逆 action (JSON ネイティブ)。**機密が混じりうる**ので
            通知前に redaction される (:func:`redact_payload`)。
        summary: 人間可読の 1 行要約 (通知の見出し)。
        action_kind: action の分類 (例 ``"deploy"`` / ``"delete"``)。任意。
        deadline: 決定が必要な期限 (epoch 秒)。任意。
        created_at: 承認要求が発火した時刻 (epoch 秒)。任意。
    """

    run_id: str
    gate_key: str
    action: Any
    summary: str
    action_kind: Optional[str] = None
    deadline: Optional[float] = None
    created_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON シリアライズ可能な dict に変換する (redaction 前の生 payload)。

        各 Notifier はこれを :func:`redact_payload` 等に通してから送信する。
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
    """payload (ネスト dict/list を含む) を再帰的に走査し機密値をマスクした **コピー** を返す。

    キー名 (小文字化) に ``sensitive_parts`` のいずれかが部分一致する dict 値を ``mask`` に
    置換する。元の payload は変更しない (deep copy ベース)。これは万能ではない (機密が
    キー名で判別できない自由文に埋まっている場合は防げない) ので、docstring の通り action の
    構造に応じて ``sensitive_parts`` を拡張するか、Notifier の ``redact`` を専用 policy に
    差し替えること。
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

    # トップレベルはコピーを保証するため明示的に dict 化する。
    return {
        str(k): _walk(v, redact_here=_is_sensitive(str(k)))
        for k, v in copy.deepcopy(dict(payload)).items()
    }


@runtime_checkable
class Notifier(Protocol):
    """承認要求を外部チャネルへ届ける best-effort 通知 backend。

    ``notify`` は **副作用のみ** (戻り値なし)。送信失敗時は例外を送出してよい
    (呼び出し元の :class:`~loop_agent.gate.HumanGate` が捕捉して warning 化し loop を
    止めない)。冪等性は保証しない (resume の TOCTOU で稀に二重通知しうる)。
    """

    def notify(self, request: ApprovalRequest) -> None: ...


def _send_with_retry(
    send: Callable[[], None],
    *,
    retries: int,
    retry_interval: float,
    sleep: Callable[[float], None],
) -> None:
    """``send`` を最大 ``retries + 1`` 回試行し、最後の例外を再送出する小ヘルパ。

    ``retry_interval > 0`` のときのみ試行間に ``sleep`` する (既定 0 = ブロックしない)。
    """
    attempts = retries + 1
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            send()
            return
        except Exception as exc:  # noqa: BLE001 - 最終的に再送出する
            last_exc = exc
            if i + 1 < attempts and retry_interval > 0:
                sleep(retry_interval)
    assert last_exc is not None  # attempts >= 1 なので必ず設定済み
    raise last_exc


class WebhookNotifier:
    """JSON payload を任意の HTTP webhook へ POST する Notifier (stdlib urllib)。

    Args:
        url: 送信先 URL。
        method: HTTP メソッド (既定 ``"POST"``)。
        headers: 追加ヘッダ。``Content-Type: application/json`` は自動付与
            (明示指定があればそちらを優先)。
        timeout: 1 回の送信 timeout 秒 (既定 5.0)。loop を長く待たせない短めの既定。
        redact: 送信前に payload を加工する callable (既定 :func:`redact_payload`)。
        retries: 失敗時の追加試行回数 (既定 0 = 単発)。
        retry_interval: 試行間 sleep 秒 (既定 0.0 = sleep しない)。
        sleep: 試行間 sleep の注入口 (テスト用、既定 :func:`time.sleep`)。
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
            raise ValueError("retries must be >= 0")
        self.url = url
        self.method = method
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.redact = redact
        self.retries = retries
        self.retry_interval = retry_interval
        self.sleep = sleep

    def _build_body(self, request: ApprovalRequest) -> bytes:
        """redaction 済み payload を JSON バイト列にする (Slack 等の specialization で override)。"""
        payload = self.redact(request.to_dict())
        return json.dumps(payload).encode("utf-8")

    def notify(self, request: ApprovalRequest) -> None:
        body = self._build_body(request)
        headers = {"Content-Type": "application/json", **self.headers}
        http_request = Request(
            self.url, data=body, headers=headers, method=self.method
        )

        def _send() -> None:
            # urlopen の戻り (context manager) は即 close する。本文は不要。
            with urlopen(http_request, timeout=self.timeout):
                pass

        _send_with_retry(
            _send,
            retries=self.retries,
            retry_interval=self.retry_interval,
            sleep=self.sleep,
        )


def _format_slack_text(payload: Mapping[str, Any]) -> str:
    """redaction 済み payload を Slack incoming webhook の ``text`` 本文に整形する。

    見出し (summary) + メタ (kind / deadline / gate / run) + action 本体 (JSON) を
    人間可読に並べる。本文は全て redaction 後なので機密はマスク済み。
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
    """Slack incoming webhook 用 Notifier (:class:`WebhookNotifier` の specialization)。

    Slack incoming webhook は ``{"text": ...}`` 形を期待するので、body builder を
    override して redaction 済み payload を Slack message text に整形する
    (:func:`_format_slack_text`)。POST 機構 / timeout / retry / redaction は親と共通。

    Args:
        webhook_url: Slack incoming webhook URL。
        text_formatter: payload (redaction 済み) -> Slack ``text`` の整形 callable
            (既定 :func:`_format_slack_text`)。``blocks`` 等のリッチ表現に差し替え可。
        その他 (``timeout`` / ``redact`` / ``retries`` ...) は :class:`WebhookNotifier` と同じ。
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
    """redaction 済み payload を email 本文 (plain text) に整形する。"""
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
    """承認要求を SMTP で email 送信する Notifier (stdlib smtplib)。

    Args:
        host: SMTP サーバホスト。
        sender: 差出人アドレス。
        recipients: 宛先アドレス列 (1 件以上)。
        port: SMTP ポート (既定 25)。
        subject_prefix: 件名の接頭辞 (既定 ``"[loop-agent] Approval required"``)。
            件名は ``"<prefix>: <summary>"``。
        username/password: 指定時 ``SMTP.login`` する。
        use_tls: ``True`` で接続後 ``starttls()`` する (既定 False)。
        timeout: SMTP 接続 timeout 秒 (既定 10.0)。
        redact: 送信前 payload 加工 (既定 :func:`redact_payload`)。
        body_formatter: payload (redaction 済み) -> 本文 plain text の整形 callable。
        smtp_factory: ``SMTP(host, port, timeout=...)`` を返す factory (テストで差し替え)。
            既定 :class:`smtplib.SMTP`。
        retries/retry_interval/sleep: :class:`WebhookNotifier` と同じ best-effort retry。
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
            raise ValueError("retries must be >= 0")
        recipients = list(recipients)
        if not recipients:
            raise ValueError("recipients must contain at least one address")
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
        message["Subject"] = f"{self.subject_prefix}: {request.summary}"
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
    """承認要求を stream (既定 stderr) に 1 行 JSON で書き出す Notifier。

    依存ゼロのデバッグ/ローカル運用向け。cp932 コンソールでのクラッシュを避けるため
    既定 ``ensure_ascii=True`` (非 ASCII を ``\\uXXXX`` エスケープ) で出力する。
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
    """複数 Notifier へ best-effort で fan-out する Notifier。

    各 backend を独立に呼び、1 つが失敗しても残りへの送信を続ける (失敗は
    ``warnings.warn`` で可視化)。webhook と email を同時に投げる等に使う。
    """

    notifiers: Sequence[Notifier] = field(default_factory=tuple)

    def notify(self, request: ApprovalRequest) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(request)
            except Exception as exc:  # noqa: BLE001 - fan-out は best-effort
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
