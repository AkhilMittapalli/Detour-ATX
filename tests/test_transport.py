"""Tests for delivery.

Two behaviours matter more than the plumbing: nothing leaves the machine
without being asked, and a day with nothing to say does not become a daily
"nothing to report" email, which is its own kind of spam.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from detour import transport as tp

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


class FakeOutcome:
    def __init__(self, route="Commute", told=0, blocking=0):
        self.route, self.told, self.blocking = route, told, blocking


class WorthSendingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "ledger.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def _ask(self, told, *, now=NOW, reader="you"):
        return tp.worth_sending(
            FakeOutcome(told=told), reader=reader, now=now, path=self.path
        )

    def test_the_first_brief_always_goes(self):
        send, _ = self._ask(0)
        self.assertTrue(send)

    def test_a_quiet_day_after_a_recent_send_is_skipped(self):
        """Silence is not a message."""
        tp.record_sent("Commute", reader="you", now=NOW, path=self.path)
        send, why = self._ask(0)
        self.assertFalse(send)
        self.assertIn("nothing new", why)

    def test_something_new_always_goes(self):
        tp.record_sent("Commute", reader="you", now=NOW, path=self.path)
        send, why = self._ask(3)
        self.assertTrue(send)
        self.assertIn("3 new", why)

    def test_a_long_silence_earns_an_all_clear(self):
        """So a reader knows the service is still alive."""
        tp.record_sent("Commute", reader="you", now=NOW, path=self.path)
        send, why = self._ask(0, now=NOW + dt.timedelta(days=tp.HEARTBEAT_DAYS + 1))
        self.assertTrue(send)
        self.assertIn("all-clear", why)

    def test_a_short_silence_does_not(self):
        tp.record_sent("Commute", reader="you", now=NOW, path=self.path)
        send, _ = self._ask(0, now=NOW + dt.timedelta(days=3))
        self.assertFalse(send)

    def test_readers_have_separate_send_histories(self):
        tp.record_sent("Commute", reader="alice", now=NOW, path=self.path)
        send, _ = self._ask(0, reader="bob")
        self.assertTrue(send)


class DryRunTests(unittest.TestCase):
    def test_file_transport_writes_nothing_on_a_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "briefs"
            result = tp.FileTransport(target).send(
                subject="s", html="<b>h</b>", text="t", dry_run=True
            )
            self.assertFalse(result.sent)
            self.assertTrue(result.dry_run)
            self.assertFalse(target.exists(), "dry run must not create anything")

    def test_smtp_dry_run_does_not_open_a_connection(self):
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u",
               "SMTP_PASSWORD": "p", "SMTP_TO": "to@example.com"}
        with mock.patch.dict(os.environ, env):
            with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as ssl_smtp:
                result = tp.SMTPTransport().send(
                    subject="Commute: 1 to plan around", html="<b>h</b>",
                    text="t", dry_run=True
                )
        smtp.assert_not_called()
        ssl_smtp.assert_not_called()
        self.assertFalse(result.sent)
        self.assertIn("would email", result.detail)

    def test_webhook_dry_run_makes_no_request(self):
        with mock.patch.dict(os.environ, {"DETOUR_WEBHOOK_URL": "https://h.example.com/x"}):
            with mock.patch("urllib.request.urlopen") as opened:
                result = tp.WebhookTransport().send(
                    subject="s", html="<b>h</b>", text="t", dry_run=True
                )
        opened.assert_not_called()
        self.assertFalse(result.sent)


class ConfigurationTests(unittest.TestCase):
    def test_unconfigured_email_names_the_missing_keys(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("detour.config.ensure_loaded", lambda: None):
                with self.assertRaises(tp.TransportError) as ctx:
                    tp.SMTPTransport()
        message = str(ctx.exception)
        for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_TO"):
            self.assertIn(key, message)

    def test_plain_http_webhooks_are_refused(self):
        with mock.patch.dict(os.environ, {"DETOUR_WEBHOOK_URL": "http://h.example.com/x"}):
            with mock.patch("detour.config.ensure_loaded", lambda: None):
                with self.assertRaises(tp.TransportError) as ctx:
                    tp.WebhookTransport()
        self.assertIn("non-HTTPS", str(ctx.exception))

    def test_an_unknown_transport_name_is_refused(self):
        with self.assertRaises(tp.TransportError):
            tp.build("carrier-pigeon")

    def test_a_failed_send_never_echoes_the_password(self):
        """An error line can end up in a log, a terminal, or a ticket."""
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "u",
               "SMTP_PASSWORD": "hunter2-secret", "SMTP_TO": "to@example.com"}
        with mock.patch.dict(os.environ, env):
            with mock.patch("smtplib.SMTP", side_effect=OSError("connection refused")):
                result = tp.SMTPTransport().send(
                    subject="s", html="h", text="t", dry_run=False
                )
        self.assertFalse(result.sent)
        self.assertTrue(result.error)
        self.assertNotIn("hunter2", result.error)


class SubjectTests(unittest.TestCase):
    def test_blocking_leads_the_subject(self):
        self.assertIn("to plan around", tp.subject_for(FakeOutcome(told=3, blocking=1)))

    def test_updates_when_nothing_blocks(self):
        self.assertIn("update", tp.subject_for(FakeOutcome(told=2)))

    def test_all_clear_when_there_is_nothing(self):
        self.assertIn("all clear", tp.subject_for(FakeOutcome(told=0)))
