"""Getting the brief out of the machine.

Three transports, all stdlib: a file on disk, SMTP email, and a generic
webhook for Slack, Discord, ntfy or anything else that accepts a POST.

Two rules are enforced here rather than left to the caller, because both are
the kind of thing that is easy to get wrong once and then apologise for
repeatedly:

**Nothing sends unless asked.** `dry_run` defaults to True everywhere. A run
with no explicit send flag reports exactly what it *would* have sent and
delivers nothing.

**Silence is not a message.** The Correspondent means most mornings have
nothing new, and a daily "nothing to report" is its own kind of spam. A brief
with nothing to tell is skipped, unless enough days have passed that a reader
would reasonably wonder whether the thing is still running.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path as FsPath

from . import config, ledger

# How long to stay quiet before sending an "all clear" so a reader knows the
# service is alive.
HEARTBEAT_DAYS = 7


class TransportError(RuntimeError):
    pass


@dataclass
class Delivery:
    transport: str
    target: str
    sent: bool
    dry_run: bool
    detail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# --------------------------------------------------------------------------
# Should this brief go out at all?
# --------------------------------------------------------------------------

def worth_sending(
    outcome, *, reader: str, now: dt.datetime, path=None
) -> tuple[bool, str]:
    """Is there enough here to justify arriving in someone's inbox?

    Returns (send, reason). The reason is reported either way, so a skipped
    send is visible rather than looking like a failure.
    """
    if outcome.told > 0:
        return True, f"{outcome.told} new or changed item(s)"

    index = ledger.by_record(path=path)
    sends = [
        c for c in index.get(f"{reader}#sent:{outcome.route}", []) if c.claim == "sent"
    ]
    if not sends:
        return True, "first brief for this route"

    latest = max(sends, key=lambda c: c.observed_at)
    try:
        when = dt.datetime.fromisoformat(latest.observed_at)
    except ValueError:
        return True, "last send time unreadable"
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)

    days = (now - when).days
    if days >= HEARTBEAT_DAYS:
        return True, f"quiet for {days} days, sending an all-clear"
    return False, f"nothing new, and last sent {days} day(s) ago"


def record_sent(route: str, *, reader: str, now: dt.datetime, detail: str = "", path=None):
    ledger.append(
        ledger.Claim(
            record_id=f"{reader}#sent:{route}",
            kind="delivery",
            claim="sent",
            source="transport",
            observed_at=now.isoformat(),
            detail=detail[:180],
        ),
        path=path,
    )


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class FileTransport:
    """Write the brief to disk. The default, and always safe."""

    name = "file"

    def __init__(self, directory: FsPath):
        self.directory = directory

    def send(self, *, subject, html, text, dry_run=True) -> Delivery:
        if dry_run:
            return Delivery(self.name, str(self.directory), False, True,
                            detail="would write the brief here")
        self.directory.mkdir(parents=True, exist_ok=True)
        return Delivery(self.name, str(self.directory), True, False,
                        detail="written")


class SMTPTransport:
    """Email, configured entirely from .env.

    Reads SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM and
    SMTP_TO. Port 465 implies implicit TLS; anything else uses STARTTLS.
    Credentials are never logged or echoed.
    """

    name = "smtp"

    def __init__(self):
        config.ensure_loaded()
        self.host = os.environ.get("SMTP_HOST", "").strip()
        self.port = int(os.environ.get("SMTP_PORT", "587") or 587)
        self.user = os.environ.get("SMTP_USER", "").strip()
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", "").strip() or self.user
        self.recipient = os.environ.get("SMTP_TO", "").strip()

        missing = [
            k for k, v in {
                "SMTP_HOST": self.host, "SMTP_USER": self.user,
                "SMTP_PASSWORD": self.password, "SMTP_TO": self.recipient,
            }.items() if not v
        ]
        if missing:
            raise TransportError(
                "email is not configured. Add to .env: " + ", ".join(missing)
            )

    def _build(self, subject: str, html: str, text: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(("Detour ATX", self.sender))
        message["To"] = self.recipient
        message["Date"] = formatdate(localtime=True)
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        return message

    def send(self, *, subject, html, text, dry_run=True) -> Delivery:
        target = self.recipient
        if dry_run:
            return Delivery(
                self.name, target, False, True,
                detail=f"would email {target} via {self.host}:{self.port} - {subject!r}",
            )

        message = self._build(subject, html, text)
        context = ssl.create_default_context()
        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=45) as server:
                    server.login(self.user, self.password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=45) as server:
                    server.starttls(context=context)
                    server.login(self.user, self.password)
                    server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # Never let a password reach a log line.
            return Delivery(self.name, target, False, False,
                            error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return Delivery(self.name, target, True, False, detail=f"emailed {target}")


class WebhookTransport:
    """POST the brief somewhere — Slack, Discord, ntfy, a custom endpoint.

    Reads DETOUR_WEBHOOK_URL. The payload carries both the plain text and the
    HTML, plus a `text` key because most chat webhooks expect exactly that.
    """

    name = "webhook"

    def __init__(self):
        config.ensure_loaded()
        self.url = os.environ.get("DETOUR_WEBHOOK_URL", "").strip()
        if not self.url:
            raise TransportError("webhook is not configured. Add DETOUR_WEBHOOK_URL to .env")
        if not self.url.startswith("https://"):
            raise TransportError("refusing a non-HTTPS webhook URL")

    def send(self, *, subject, html, text, dry_run=True) -> Delivery:
        host = self.url.split("/")[2] if "/" in self.url else self.url
        if dry_run:
            return Delivery(self.name, host, False, True,
                            detail=f"would POST to {host} - {text[:70]!r}")

        payload = json.dumps({"text": f"{subject}\n{text}", "subject": subject,
                              "html": html}).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload,
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                code = response.status
        except (urllib.error.URLError, TimeoutError) as exc:
            return Delivery(self.name, host, False, False,
                            error=f"{type(exc).__name__}: {str(exc)[:160]}")
        return Delivery(self.name, host, True, False, detail=f"POSTed to {host} ({code})")


def build(name: str, *, out_dir: FsPath | None = None):
    """Pick a transport by name, raising a clear error if unconfigured."""
    if name == "file":
        return FileTransport(out_dir or FsPath("out"))
    if name == "email":
        return SMTPTransport()
    if name == "webhook":
        return WebhookTransport()
    raise TransportError(f"unknown transport {name!r} (file, email, webhook)")


def subject_for(outcome) -> str:
    """A subject line that says whether it needs attention before it is opened."""
    if outcome.blocking:
        return f"{outcome.route}: {outcome.blocking} to plan around"
    if outcome.told:
        return f"{outcome.route}: {outcome.told} update(s)"
    return f"{outcome.route}: all clear"
