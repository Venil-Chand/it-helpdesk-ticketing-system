"""
flask notifications-send / notifications-status / notifications-check-config
/ set-email, run through the application's CLI.

Temporary databases; delivery is the local sink in a temporary folder.
No SMTP server is contacted.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import migrations
from app import app
from helpdesk import notification_cli
from notifications import heartbeat
from helpdesk.security import get_settings

import database as db

from notification_helpers import FakeSMTP, NotificationTestCase


class CliTestCase(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.runner = app.test_cli_runner()

        operator = patch("helpdesk.cli._operator", return_value="opsadmin")
        operator.start()
        self.addCleanup(operator.stop)

        # Restored afterwards. Only the test's own NOTIFICATION_ variables
        # reach load_delivery_settings, whatever the shell has set.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)

        for name in [n for n in os.environ if n.startswith("NOTIFICATION_")]:
            del os.environ[name]

        os.environ["NOTIFICATION_SINK_DIR"] = self.sink_dir

        # The --loop worker writes a heartbeat; keep it out of the project
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        os.environ["HELPDESK_STATE_DIR"] = state.name
        self.state_dir = state.name

    def run_command(self, *args, env=None):
        with patch.dict(os.environ, env or {}):
            return self.runner.invoke(args=list(args))


class TestSend(CliTestCase):

    def test_once_delivers_to_the_local_sink(self):
        result = self.run_command("notifications-send")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("SIMULATED", result.output)
        # Bob's ticket from setUp: bob (requester), tech, root (staff)
        self.assertIn("Processed 3 notification(s) (simulated: 3)",
                      result.output)
        self.assertEqual(len(self.sink_files()), 3)

    def test_nothing_to_do(self):
        self.clear_queue()
        result = self.run_command("notifications-send")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Processed 0 notification(s)", result.output)

    def test_unavailable_server_exits_2(self):
        env = {"NOTIFICATION_DELIVERY": "smtp",
               "NOTIFICATION_SENDER": "helpdesk@example.org",
               "NOTIFICATION_SMTP_HOST": "smtp.example.org",
               "NOTIFICATION_SMTP_USERNAME": "mailer",
               "NOTIFICATION_SMTP_PASSWORD": "cli-secret-password-1"}
        FakeSMTP.fail_connect = ConnectionRefusedError()

        with patch("smtplib.SMTP", FakeSMTP):
            result = self.run_command("notifications-send", env=env)

        self.assertEqual(result.exit_code,
                         notification_cli.EXIT_DELIVERY_UNAVAILABLE)
        self.assertIn("Delivery paused", result.output)
        self.assertNotIn("cli-secret-password-1", result.output)
        self.assertEqual({r[6] for r in self.notifications()}, {"pending"})

    def test_bad_configuration_sends_nothing(self):
        result = self.run_command(
            "notifications-send", env={"NOTIFICATION_DELIVERY": "smtp"}
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("NOTIFICATION_SENDER", result.output)
        self.assertEqual({r[6] for r in self.notifications()}, {"pending"})

    def test_production_requires_an_explicit_mode(self):
        settings = get_settings(app)
        with patch.object(type(settings), "is_production", True):
            result = self.run_command("notifications-send")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("NOTIFICATION_DELIVERY must be set in production",
                      result.output)

    def test_outdated_schema_is_refused(self):
        os.remove(db.DB_PATH)
        migrations.migrate(db.DB_PATH, migrations=migrations.MIGRATIONS[:9])

        result = self.run_command("notifications-send")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("not up to date", result.output)

    def test_loop_stops_on_signal(self):
        def stop_soon(self_, *, interval, limit, stop_event, on_summary,
                      heartbeat):
            stop_event.set()

        with patch("notifications.worker.NotificationWorker.run_forever",
                   stop_soon):
            result = self.run_command("notifications-send", "--loop",
                                      "--interval", "1")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Worker stopped.", result.output)

        # The heartbeat says so, for monitoring
        status = heartbeat.read_status(
            os.path.join(self.state_dir, heartbeat.FILE_NAME))
        self.assertEqual(status["state"], heartbeat.STOPPED)


class TestStatus(CliTestCase):

    def test_counts_without_addresses(self):
        self.run_command("notifications-send")
        self.sql("UPDATE notifications SET status = 'failed', "
                 "last_error = 'recipient: SMTPRecipientsRefused 550' "
                 "WHERE recipient_username = 'tech'")

        result = self.run_command("notifications-status")

        self.assertEqual(result.exit_code, 0)
        self.assertIn("simulated (local sink, not emailed): 2", result.output)
        self.assertIn("failed: 1", result.output)
        self.assertIn("SMTPRecipientsRefused 550", result.output)
        self.assertNotIn("@example.org", result.output)

    def test_status_is_read_only(self):
        before = self.sql("SELECT * FROM notifications")
        self.run_command("notifications-status")
        self.assertEqual(self.sql("SELECT * FROM notifications"), before)


class TestCheckConfig(CliTestCase):

    def test_never_prints_the_password(self):
        env = {"NOTIFICATION_DELIVERY": "smtp",
               "NOTIFICATION_SENDER": "helpdesk@example.org",
               "NOTIFICATION_SMTP_HOST": "smtp.example.org",
               "NOTIFICATION_SMTP_USERNAME": "mailer",
               "NOTIFICATION_SMTP_PASSWORD": "cli-secret-password-2"}

        result = self.run_command("notifications-check-config", env=env)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("SMTP server: smtp.example.org:587 (starttls)",
                      result.output)
        self.assertNotIn("cli-secret-password-2", result.output)
        self.assertEqual(FakeSMTP.instances, [])  # nothing contacted

    def test_reports_problems_by_name(self):
        result = self.run_command(
            "notifications-check-config",
            env={"NOTIFICATION_DELIVERY": "smtp",
                 "NOTIFICATION_SENDER": "x@example.org",
                 "NOTIFICATION_SMTP_HOST": "bad host!"}
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("NOTIFICATION_SMTP_HOST", result.output)
        self.assertNotIn("bad host!", result.output)


class TestSetEmail(CliTestCase):

    def email(self, username):
        return self.sql("SELECT email FROM users WHERE username = ?",
                        (username,))[0][0]

    def email_events(self):
        return self.sql(
            "SELECT actor, target_username, old_value, new_value "
            "FROM audit_events WHERE action = 'email_changed' ORDER BY id"
        )

    def test_set_replace_and_clear_are_audited_without_the_address(self):
        self.set_email("alice", None)

        for args, expected in (
            (("alice", "alice@example.org"), "set"),
            (("alice", "alice2@example.org"), "replaced"),
            (("alice", "--clear"), "cleared"),
        ):
            result = self.run_command("set-email", *args)
            self.assertEqual(result.exit_code, 0, result.output)

        self.assertIsNone(self.email("alice"))
        self.assertEqual(self.email_events(), [
            ("cli:opsadmin", "alice", None, "set"),
            ("cli:opsadmin", "alice", None, "replaced"),
            ("cli:opsadmin", "alice", None, "cleared"),
        ])
        self.assertNotIn("@", str(self.sql("SELECT * FROM audit_events")))

    def test_no_change_is_not_audited(self):
        result = self.run_command("set-email", "alice", "alice@example.org")
        self.assertIn("No change", result.output)
        self.assertEqual(self.email_events(), [])

    def test_refusals_change_nothing(self):
        for args in (("alice", "Alice <a@example.org>"),
                     ("alice", "a@example.org\nBcc: b@example.org"),
                     ("alice",),
                     ("alice", "a@example.org", "--clear"),
                     ("nobody", "n@example.org"),
                     ("Alice", "n@example.org")):
            with self.subTest(args=args):
                result = self.run_command("set-email", *args)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(self.email("alice"), "alice@example.org")

        self.assertEqual(self.email_events(), [])

    def test_address_check_constraint_in_the_database(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.set_email("alice", "no-at-sign")

    def test_audit_page_labels_the_event(self):
        self.run_command("set-email", "bob", "b2@example.org")
        page = self.signed_in("root").get("/admin/audit?kind=accounts")
        self.assertIn(b"Notification email changed", page.data)


if __name__ == "__main__":
    unittest.main()
