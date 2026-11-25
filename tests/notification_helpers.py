"""
Shared fixture for notification tests.

Builds on AccountTestCase: a temporary, fully migrated database with real
accounts (alice, bob: users; tech: technician; root: admin) and Bob's
ticket. Every account gets an @example.org address (RFC 2606: can never
reach a real mailbox). Nothing here opens the network: SMTP is replaced
by FakeSMTP, and NoNetwork makes any real connection attempt fail the
test.
"""

import json
import os
import smtplib
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import database as db
from notifications import settings as delivery_settings
from notifications import timeutil, transport, worker

from account_helpers import AccountTestCase

START = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)


class FrozenClock:
    """A controllable replacement for notifications.timeutil.utc_now."""

    def __init__(self, moment=START):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, **delta):
        self.moment += timedelta(**delta)


def local_settings(sink_dir, **overrides):
    values = dict(mode=delivery_settings.LOCAL,
                  sender="helpdesk@example.org", sink_dir=Path(sink_dir))
    values.update(overrides)
    return delivery_settings.DeliverySettings(**values)


def smtp_settings(**overrides):
    values = dict(mode=delivery_settings.SMTP, sender="helpdesk@example.org",
                  smtp_host="smtp.example.org", smtp_port=587,
                  smtp_security=delivery_settings.STARTTLS,
                  smtp_username="mailer", smtp_password="s3cret-smtp-pass",
                  smtp_timeout=5.0)
    values.update(overrides)
    return delivery_settings.DeliverySettings(**values)


class FakeSMTP:
    """
    Stands in for smtplib.SMTP / SMTP_SSL. Class-level knobs set failures.

    fail_connect   exception raised by the constructor
    fail_starttls  exception raised by starttls()
    fail_login     exception raised by login()
    fail_send      list: each send_message() pops and raises the next item
                   (None means succeed)
    """

    instances = []
    sent = []
    fail_connect = None
    fail_starttls = None
    fail_login = None
    fail_send = []
    refuse = {}

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.sent = []
        cls.fail_connect = None
        cls.fail_starttls = None
        cls.fail_login = None
        cls.fail_send = []
        cls.refuse = {}

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail_connect is not None:
            raise FakeSMTP.fail_connect
        self.host, self.port, self.timeout = host, port, timeout
        self.context = context
        self.calls = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        if FakeSMTP.fail_starttls is not None:
            raise FakeSMTP.fail_starttls

    def login(self, username, password):
        self.calls.append(("login", username))
        if FakeSMTP.fail_login is not None:
            raise FakeSMTP.fail_login

    def send_message(self, message):
        self.calls.append("send")
        if FakeSMTP.fail_send:
            failure = FakeSMTP.fail_send.pop(0)
            if failure is not None:
                raise failure
        FakeSMTP.sent.append(message)
        return dict(FakeSMTP.refuse)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


def no_network(*args, **kwargs):
    raise AssertionError("a test tried to open a real network connection")


class NotificationTestCase(AccountTestCase):
    """AccountTestCase plus addresses, a frozen queue clock and no network."""

    def setUp(self):
        # The ticket created by AccountTestCase.setUp queues notifications
        # too; freeze the clock first so every row has a known time
        self.clock = FrozenClock()
        clock_patch = patch.object(timeutil, "utc_now", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

        for target, name in ((socket.socket, "connect"),
                             (socket, "getaddrinfo")):
            net = patch.object(target, name, no_network)
            net.start()
            self.addCleanup(net.stop)

        FakeSMTP.reset()
        super().setUp()

        for username in ("alice", "bob", "tech", "root"):
            self.set_email(username, f"{username}@example.org")

        sink = tempfile.TemporaryDirectory()
        self.addCleanup(sink.cleanup)
        self.sink_dir = os.path.join(sink.name, "sink")

    # -----------------------------------------------

    def set_email(self, username, address):
        self.sql("UPDATE users SET email = ? WHERE username = ?",
                 (address, username))

    def notifications(self, where="1 = 1", params=()):
        return self.sql(
            "SELECT id, event_type, recipient_username, recipient_reason, "
            "ticket_id, payload, status, attempts, deferrals, last_error, "
            "dedupe_key, sent_at, next_attempt_at, delivery_mode "
            f"FROM notifications WHERE {where} ORDER BY id", params
        )

    def summary(self, where="1 = 1", params=()):
        """[(event_type, recipient, reason), ...] in queue order."""

        return [(row[1], row[2], row[3])
                for row in self.notifications(where, params)]

    def payload(self, notification_id):
        row = self.sql("SELECT payload FROM notifications WHERE id = ?",
                       (notification_id,))[0]
        return json.loads(row[0])

    def attempts(self, notification_id):
        return self.sql(
            "SELECT attempt, outcome, error, delivery_mode "
            "FROM notification_attempts WHERE notification_id = ? "
            "ORDER BY id", (notification_id,)
        )

    def clear_queue(self):
        """Forget rows queued by setUp (e.g. Bob's ticket)."""

        self.sql("DELETE FROM notifications")

    def local_worker(self, **overrides):
        settings = local_settings(self.sink_dir, **overrides)
        return worker.NotificationWorker(
            settings, transport.LocalSinkTransport(settings.sink_dir),
            db_path=db.DB_PATH, worker_id="test-worker:1"
        )

    def smtp_worker(self, **overrides):
        settings = smtp_settings(**overrides)
        smtp = transport.SmtpTransport(
            settings, smtp_class=FakeSMTP, smtp_ssl_class=FakeSMTP
        )
        return worker.NotificationWorker(
            settings, smtp, db_path=db.DB_PATH, worker_id="test-worker:1"
        )

    def sink_files(self):
        if not os.path.isdir(self.sink_dir):
            return []
        return sorted(os.listdir(self.sink_dir))

    def sink_text(self, name):
        with open(os.path.join(self.sink_dir, name), encoding="utf-8") as f:
            return f.read()


# Re-exported so test modules can build SMTP failures concisely
SMTPAuthenticationError = smtplib.SMTPAuthenticationError
