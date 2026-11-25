"""
Delivery settings and transports: the local sink and SMTP.

SMTP is always the FakeSMTP class from notification_helpers; a guard on
socket.connect fails any test that tries to reach a real server.
"""

import os
import smtplib
import socket
import ssl
import stat
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from notifications import settings as ns
from notifications import transport

from notification_helpers import FakeSMTP, no_network, smtp_settings

SMTP_ENV = {
    "NOTIFICATION_DELIVERY": "smtp",
    "NOTIFICATION_SENDER": "helpdesk@example.org",
    "NOTIFICATION_SMTP_HOST": "smtp.example.org",
    "NOTIFICATION_SMTP_USERNAME": "mailer",
    "NOTIFICATION_SMTP_PASSWORD": "very-secret-smtp-password",
}


def message(to="bob@example.org"):
    msg = EmailMessage()
    msg["From"] = "helpdesk@example.org"
    msg["To"] = to
    msg["Subject"] = "test"
    msg.set_content("body")
    return msg


# ---------------------------------------------------
# SETTINGS
# ---------------------------------------------------

class TestSettings(unittest.TestCase):

    def load(self, env, production=False):
        return ns.load_delivery_settings(env, production=production)

    def assert_refused(self, env, variable, production=False):
        with self.assertRaises(ns.NotificationConfigError) as caught:
            self.load(env, production)
        self.assertIn(variable, str(caught.exception))
        # No value is ever echoed back
        for value in env.values():
            if len(value) > 6:
                self.assertNotIn(value, str(caught.exception))

    def test_development_defaults_to_simulated_local_delivery(self):
        settings = self.load({})
        self.assertEqual(settings.mode, "local")
        self.assertTrue(settings.is_simulated)
        self.assertEqual(settings.sink_dir, ns.DEFAULT_SINK_DIR)
        self.assertIn("instance", settings.sink_dir.parts)

    def test_production_must_choose(self):
        self.assert_refused({}, "NOTIFICATION_DELIVERY", production=True)

    def test_smtp_settings(self):
        settings = self.load(SMTP_ENV)
        self.assertEqual(
            (settings.smtp_host, settings.smtp_port, settings.smtp_security,
             settings.smtp_timeout),
            ("smtp.example.org", 587, "starttls", 20.0)
        )

    def test_default_ports_follow_security(self):
        self.assertEqual(self.load({**SMTP_ENV, "NOTIFICATION_SMTP_SECURITY":
                                    "ssl"}).smtp_port, 465)

    def test_password_never_in_repr_or_description(self):
        settings = self.load(SMTP_ENV)
        text = repr(settings) + "\n".join(settings.describe())
        self.assertNotIn("very-secret-smtp-password", text)
        self.assertIn("username and password set", text)

    def test_refusals(self):
        cases = [
            ({"NOTIFICATION_DELIVERY": "carrier-pigeon"},
             "NOTIFICATION_DELIVERY"),
            ({**SMTP_ENV, "NOTIFICATION_SENDER": ""}, "NOTIFICATION_SENDER"),
            ({**SMTP_ENV, "NOTIFICATION_SENDER": "Help <h@example.org>"},
             "NOTIFICATION_SENDER"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_HOST": ""},
             "NOTIFICATION_SMTP_HOST"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_HOST": "smtp.example.org:25"},
             "NOTIFICATION_SMTP_HOST"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_SECURITY": "maybe"},
             "NOTIFICATION_SMTP_SECURITY"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_PASSWORD": ""},
             "NOTIFICATION_SMTP_USERNAME"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_PORT": "0"},
             "NOTIFICATION_SMTP_PORT"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_PORT": "25x"},
             "NOTIFICATION_SMTP_PORT"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_TIMEOUT": "0"},
             "NOTIFICATION_SMTP_TIMEOUT"),
            ({**SMTP_ENV, "NOTIFICATION_SMTP_TIMEOUT": "121"},
             "NOTIFICATION_SMTP_TIMEOUT"),
            ({**SMTP_ENV, "NOTIFICATION_MAX_ATTEMPTS": "11"},
             "NOTIFICATION_MAX_ATTEMPTS"),
            ({**SMTP_ENV, "NOTIFICATION_RETRY_BASE_SECONDS": "100",
              "NOTIFICATION_RETRY_MAX_SECONDS": "50"},
             "NOTIFICATION_RETRY_MAX_SECONDS"),
            ({**SMTP_ENV, "NOTIFICATION_LEASE_SECONDS": "60"},
             "NOTIFICATION_LEASE_SECONDS"),
            ({"NOTIFICATION_BASE_URL": "javascript:alert(1)"},
             "NOTIFICATION_BASE_URL"),
        ]
        for env, variable in cases:
            with self.subTest(variable=variable, env=env):
                self.assert_refused(env, variable)

    def test_plain_text_smtp_only_to_a_local_relay_without_credentials(self):
        plain = {**SMTP_ENV, "NOTIFICATION_SMTP_SECURITY": "none"}
        self.assert_refused(plain, "NOTIFICATION_SMTP_SECURITY")

        no_login = {k: v for k, v in plain.items()
                    if "USERNAME" not in k and "PASSWORD" not in k}
        self.assert_refused(no_login, "NOTIFICATION_SMTP_SECURITY")

        # Even to a local relay, credentials are never sent in plain text
        self.assert_refused({**plain, "NOTIFICATION_SMTP_HOST": "localhost"},
                            "NOTIFICATION_SMTP_SECURITY")

        relay = self.load({**no_login, "NOTIFICATION_SMTP_HOST": "localhost"})
        self.assertEqual((relay.smtp_security, relay.smtp_port),
                         ("none", 25))

    def test_production_links_must_use_https(self):
        env = {**SMTP_ENV, "NOTIFICATION_BASE_URL": "http://helpdesk.org"}
        self.assert_refused(env, "NOTIFICATION_BASE_URL", production=True)

        ok = self.load({**env, "NOTIFICATION_BASE_URL":
                        "https://helpdesk.example.org/"}, production=True)
        self.assertEqual(ok.base_url, "https://helpdesk.example.org")


# ---------------------------------------------------
# LOCAL SINK
# ---------------------------------------------------

class TestLocalSink(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sink = Path(directory.name) / "sink"
        self.transport = transport.LocalSinkTransport(self.sink)

        # A permissive umask, so the permission checks below test the
        # code's own modes rather than this machine's umask
        old_umask = os.umask(0)
        self.addCleanup(os.umask, old_umask)

    def test_writes_a_private_file_in_a_private_folder(self):
        path = self.transport.send(message(), 7)

        self.assertEqual(path.name, "00000007.eml")
        self.assertEqual(stat.S_IMODE(os.stat(self.sink).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn("bob@example.org", path.read_text())

    def test_existing_folder_permissions_are_tightened(self):
        self.sink.mkdir(mode=0o755)
        os.chmod(self.sink, 0o755)
        self.transport.send(message(), 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.sink).st_mode), 0o700)

    def test_second_write_of_the_same_notification_keeps_the_first(self):
        first = self.transport.send(message("first@example.org"), 3)
        second = self.transport.send(message("second@example.org"), 3)

        self.assertEqual(first, second)
        self.assertIn("first@example.org", first.read_text())
        self.assertEqual(len(os.listdir(self.sink)), 1)

    def test_symlinked_file_is_not_followed(self):
        self.sink.mkdir(mode=0o700)
        target = self.sink.parent / "elsewhere.txt"
        os.symlink(target, self.sink / "00000009.eml")

        # The name exists, so it is treated as already written, and the
        # symlink target is never created or written through
        self.transport.send(message(), 9)
        self.assertFalse(target.exists())

    def test_unwritable_location_defers_the_run(self):
        blocker = self.sink.parent / "file"
        blocker.write_text("x")
        sink = transport.LocalSinkTransport(blocker / "sink")

        with self.assertRaises(transport.DeliveryError) as caught:
            sink.send(message(), 1)

        self.assertEqual(caught.exception.kind, transport.DEFERRED)
        self.assertTrue(caught.exception.stop_run)


# ---------------------------------------------------
# SMTP
# ---------------------------------------------------

class SmtpTestCase(unittest.TestCase):

    def setUp(self):
        FakeSMTP.reset()
        for target, name in ((socket.socket, "connect"),
                             (socket, "getaddrinfo")):
            guard = patch.object(target, name, no_network)
            guard.start()
            self.addCleanup(guard.stop)

    def make(self, **overrides):
        return transport.SmtpTransport(
            smtp_settings(**overrides), smtp_class=FakeSMTP,
            smtp_ssl_class=FakeSMTP
        )

    def fails(self, smtp, kind, stop_run=None):
        with self.assertRaises(transport.DeliveryError) as caught:
            smtp.send(message(), 1)
        error = caught.exception
        self.assertEqual(error.kind, kind)
        if stop_run is not None:
            self.assertEqual(error.stop_run, stop_run)
        self.assertNotIn("s3cret-smtp-pass", error.safe_error)
        self.assertNotIn("bob@example.org", error.safe_error)
        return error


class TestSmtpSuccess(SmtpTestCase):

    def test_starttls_then_login_then_send(self):
        smtp = self.make()
        smtp.send(message(), 1)
        smtp.send(message(), 2)
        smtp.close()

        (connection,) = FakeSMTP.instances
        self.assertEqual(
            connection.calls,
            ["ehlo", "starttls", "ehlo", ("login", "mailer"), "send",
             "send", "quit"]
        )
        self.assertEqual((connection.host, connection.port,
                          connection.timeout),
                         ("smtp.example.org", 587, 5.0))
        self.assertEqual(len(FakeSMTP.sent), 2)

    def test_certificate_verification_is_on_by_default(self):
        smtp = transport.SmtpTransport(smtp_settings(), smtp_class=FakeSMTP)
        self.assertEqual(smtp._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(smtp._context.check_hostname)

    def test_implicit_tls(self):
        smtp = self.make(smtp_security="ssl", smtp_port=465)
        smtp.send(message(), 1)
        (connection,) = FakeSMTP.instances
        self.assertIsNotNone(connection.context)
        self.assertNotIn("starttls", connection.calls)

    def test_no_login_without_credentials(self):
        smtp = self.make(smtp_username="", smtp_password="")
        smtp.send(message(), 1)
        self.assertNotIn(("login", ""), FakeSMTP.instances[0].calls)
        self.assertFalse(any(isinstance(c, tuple)
                             for c in FakeSMTP.instances[0].calls))


class TestSmtpFailures(SmtpTestCase):

    def test_unreachable_server_defers_and_stops_the_run(self):
        for failure in (ConnectionRefusedError(), socket.timeout(),
                        socket.gaierror(), TimeoutError(),
                        smtplib.SMTPConnectError(421, b"busy")):
            with self.subTest(failure=type(failure).__name__):
                FakeSMTP.reset()
                FakeSMTP.fail_connect = failure
                error = self.fails(self.make(), transport.DEFERRED, True)
                self.assertTrue(error.safe_error.startswith("connect: "))

    def test_authentication_failure_is_handled_safely(self):
        FakeSMTP.fail_login = smtplib.SMTPAuthenticationError(
            535, b"5.7.8 user mailer password s3cret-smtp-pass rejected"
        )
        smtp = self.make()

        error = self.fails(smtp, transport.DEFERRED, True)

        self.assertEqual(error.safe_error,
                         "sign-in: SMTPAuthenticationError 535")
        self.assertIn("close", FakeSMTP.instances[0].calls)

    def test_missing_starttls_never_falls_back_to_plain_text(self):
        FakeSMTP.fail_starttls = smtplib.SMTPNotSupportedError(
            "STARTTLS extension not supported by server."
        )
        smtp = self.make()

        self.fails(smtp, transport.DEFERRED, True)
        self.assertNotIn(("login", "mailer"), FakeSMTP.instances[0].calls)
        self.assertEqual(FakeSMTP.sent, [])

    def test_certificate_error_defers(self):
        FakeSMTP.fail_starttls = ssl.SSLCertVerificationError("bad cert")
        self.fails(self.make(), transport.DEFERRED, True)

    def test_timeout_during_send_may_have_been_delivered(self):
        FakeSMTP.fail_send = [socket.timeout("timed out")]
        smtp = self.make()

        error = self.fails(smtp, transport.RETRY, True)
        self.assertEqual(error.safe_error, "send: TimeoutError")

        # The broken connection is dropped and the next send reconnects
        smtp.send(message(), 2)
        self.assertEqual(len(FakeSMTP.instances), 2)

    def test_disconnect_during_send_retries(self):
        FakeSMTP.fail_send = [smtplib.SMTPServerDisconnected("gone")]
        self.fails(self.make(), transport.RETRY, True)

    def test_recipient_refused_5xx_is_permanent(self):
        FakeSMTP.fail_send = [smtplib.SMTPRecipientsRefused(
            {"bob@example.org": (550, b"no such user bob@example.org")}
        )]
        error = self.fails(self.make(), transport.PERMANENT, False)
        self.assertEqual(error.safe_error,
                         "recipient: SMTPRecipientsRefused 550")

    def test_recipient_refused_4xx_retries(self):
        FakeSMTP.fail_send = [smtplib.SMTPRecipientsRefused(
            {"bob@example.org": (452, b"mailbox full")}
        )]
        self.fails(self.make(), transport.RETRY, False)

    def test_data_errors_by_code(self):
        for code, kind in ((554, transport.PERMANENT),
                           (451, transport.RETRY)):
            with self.subTest(code=code):
                FakeSMTP.reset()
                FakeSMTP.fail_send = [smtplib.SMTPDataError(code, b"x")]
                self.fails(self.make(), kind)

    def test_sender_refused_stops_the_run(self):
        FakeSMTP.fail_send = [smtplib.SMTPSenderRefused(
            553, b"not allowed", "helpdesk@example.org"
        )]
        self.fails(self.make(), transport.RETRY, True)

    def test_partially_refused_recipient_from_send_result(self):
        FakeSMTP.refuse = {"bob@example.org": (550, b"no")}
        self.fails(self.make(), transport.PERMANENT)

    def test_close_after_broken_connection_does_not_raise(self):
        smtp = self.make()
        smtp.send(message(), 1)

        def broken_quit():
            raise smtplib.SMTPServerDisconnected("gone")

        FakeSMTP.instances[0].quit = broken_quit
        smtp.close()  # must not raise
        self.assertIn("close", FakeSMTP.instances[0].calls)


class TestNoRealNetwork(unittest.TestCase):

    def test_guard_blocks_real_connections(self):
        # The guard used by every SMTP test really does stop the real
        # smtplib before any lookup or connection leaves this machine
        with patch.object(socket, "getaddrinfo", no_network), \
                patch.object(socket.socket, "connect", no_network):
            real = transport.SmtpTransport(smtp_settings(smtp_timeout=1.0))
            with self.assertRaisesRegex(AssertionError, "real network"):
                real.send(message(), 1)

    def test_make_transport_chooses_by_mode(self):
        self.assertIsInstance(transport.make_transport(smtp_settings()),
                              transport.SmtpTransport)
        local = ns.DeliverySettings(mode="local", sender="a@example.org")
        self.assertIsInstance(transport.make_transport(local),
                              transport.LocalSinkTransport)


if __name__ == "__main__":
    unittest.main()
