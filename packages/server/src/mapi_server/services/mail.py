"""Outgoing e-mail (sign-up verification links) behind one small protocol.

:class:`Mailer` has a single coroutine, :meth:`Mailer.send`. Implementations:

* :class:`ConsoleMailer` logs the message (single-tenant installs);
* :class:`OutboxMailer` writes each message as a JSON file under ``MAPI_OUTBOX_DIR``; the
  ``dev`` profile serves the newest ones at ``GET /dev/outbox`` so end-to-end tests can follow
  a verification link without a mail server;
* :class:`SmtpMailer` speaks SMTP with ``smtplib`` (STARTTLS on 587 or implicit TLS on 465,
  certificate verified against the system trust store), in a worker thread;
* :class:`NullMailer` refuses to send (sign-up is off).

No third-party mail service and no HTML: messages are plain text with the link on its own
line. Headers are built with :class:`email.message.EmailMessage`, which refuses CR/LF in
header values (no header injection through a sign-up's name).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from mapi_server.settings import MailerKind, Settings

__all__ = [
    "ConsoleMailer",
    "MailMessage",
    "Mailer",
    "MailerError",
    "NullMailer",
    "OutboxMailer",
    "SmtpMailer",
    "build_mailer",
    "read_outbox",
]

log = logging.getLogger(__name__)

OUTBOX_KEEP: Final = 200
"""The outbox keeps this many newest messages (development only)."""
_SAFE_HEADER = re.compile(r"^[^\r\n\x00]{1,998}$")


class MailerError(RuntimeError):
    """The message could not be handed to the mail system (logged; never shown to callers)."""


@dataclass(frozen=True, slots=True)
class MailMessage:
    """A plain-text e-mail. ``tags`` are machine-readable hints kept by the outbox (e.g. the
    verification token, so e2e tests need not parse the body)."""

    to: str
    subject: str
    body: str
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("to", self.to), ("subject", self.subject)):
            if not _SAFE_HEADER.match(value):
                raise ValueError(f"unsafe {name} header")

    def to_email(self, sender: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = self.to
        msg["Subject"] = self.subject
        msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2].strip("> ") or None)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(self.body)
        return msg


@runtime_checkable
class Mailer(Protocol):
    """Sends one message; raises :class:`MailerError` when the mail system refuses it."""

    kind: MailerKind

    async def send(self, message: MailMessage) -> None: ...


class NullMailer:
    kind = MailerKind.NONE

    async def send(self, message: MailMessage) -> None:
        raise MailerError("no mailer is configured (MAPI_MAILER=none)")


class ConsoleMailer:
    """Logs the message (the link included): for single-user installs only."""

    kind = MailerKind.CONSOLE

    def __init__(self, sender: str) -> None:
        self.sender = sender

    async def send(self, message: MailMessage) -> None:
        log.warning(
            "e-mail (console mailer) to %s: %s\n%s", message.to, message.subject, message.body
        )


class OutboxMailer:
    """Writes ``<timestamp>-<n>.json`` files (mode 0600) to a directory (development)."""

    kind = MailerKind.OUTBOX

    def __init__(self, directory: Path, sender: str, *, keep: int = OUTBOX_KEEP) -> None:
        self.directory = directory
        self.sender = sender
        self.keep = keep
        self._n = 0

    async def send(self, message: MailMessage) -> None:
        await asyncio.to_thread(self._write, message)

    def _write(self, message: MailMessage) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._n += 1
        ts = datetime.now(UTC)
        name = f"{ts.strftime('%Y%m%dT%H%M%S%f')}-{os.getpid()}-{self._n:06d}.json"
        doc = {
            "from": self.sender,
            "to": message.to,
            "subject": message.subject,
            "body": message.body,
            "tags": dict(message.tags),
            "sent_at": ts.isoformat(),
        }
        path = self.directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        for old in sorted(self.directory.glob("*.json"))[: -self.keep]:
            old.unlink(missing_ok=True)


def read_outbox(directory: Path, *, to: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """The newest outbox messages first (optionally only those to ``to``)."""
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    want = to.strip().lower() if to else None
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if want is not None and str(doc.get("to", "")).lower() != want:
            continue
        out.append(doc)
        if len(out) >= limit:
            break
    return out


class SmtpMailer:
    """SMTP with TLS (``smtplib``): STARTTLS (required, not opportunistic) or implicit TLS."""

    kind = MailerKind.SMTP

    def __init__(
        self,
        host: str,
        *,
        port: int = 587,
        sender: str,
        tls: str = "starttls",
        username: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
        context: ssl.SSLContext | None = None,
    ) -> None:
        if tls not in ("starttls", "ssl"):
            raise ValueError("tls must be 'starttls' or 'ssl'")
        self.host = host
        self.port = port
        self.sender = sender
        self.tls = tls
        self.username = username
        self.password = password
        self.timeout = timeout
        self.context = context or ssl.create_default_context()

    async def send(self, message: MailMessage) -> None:
        await asyncio.to_thread(self._send, message)

    def _send(self, message: MailMessage) -> None:
        msg = message.to_email(self.sender)
        try:
            if self.tls == "ssl":
                with smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=self.context
                ) as smtp:
                    self._deliver(smtp, msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                    smtp.ehlo()
                    if not smtp.has_extn("starttls"):
                        raise MailerError("the SMTP server does not offer STARTTLS")
                    smtp.starttls(context=self.context)
                    smtp.ehlo()
                    self._deliver(smtp, msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailerError(f"SMTP delivery failed: {type(exc).__name__}") from exc

    def _deliver(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.username:
            smtp.login(self.username, self.password or "")
        smtp.send_message(msg)


def build_mailer(settings: Settings) -> Mailer:
    """The configured mailer (``MAPI_MAILER``, default by profile)."""
    kind = settings.effective_mailer
    sender = settings.mail_from
    if kind is MailerKind.CONSOLE:
        return ConsoleMailer(sender)
    if kind is MailerKind.OUTBOX:
        return OutboxMailer(settings.resolved_outbox_dir(), sender)
    if kind is MailerKind.SMTP and settings.smtp_host:
        return SmtpMailer(
            settings.smtp_host,
            port=settings.smtp_port,
            sender=sender,
            tls=settings.smtp_tls,
            username=settings.smtp_username,
            password=settings.smtp_password,
            timeout=settings.smtp_timeout_s,
        )
    return NullMailer()
