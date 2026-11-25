"""
The delivery worker end to end: queued row -> recipient check -> message
-> transport -> recorded outcome.

Covers delivery-time recipient checks (deactivated, demoted, missing
address, no longer the requester), message contents and sensitive-data
exclusion, SMTP outages and sign-in failures, persistence across runs,
and the stop behaviour of the loop.
"""

import email
import smtplib
import threading
import unittest
from unittest.mock import patch

import accounts
import database as db
from notifications import render

from account_helpers import NEW_PASSWORD, PASSWORD
from notification_helpers import FakeSMTP, NotificationTestCase

TITLE = "Payroll export leaked: salary spreadsheet attached"
DESCRIPTION = "Contains the CFO's bank details 12-34-56"
COMMENT = "Temporary VPN code is 774411"


class WorkerTestCase(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()

    def statuses(self):
        return [(r[2], r[1], r[6]) for r in self.notifications()]


class TestLocalDelivery(WorkerTestCase):

    def test_simulated_delivery_is_clearly_marked(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")

        summary = self.local_worker().run_once()

        self.assertEqual(summary.outcomes, {"simulated": 1})
        (row,) = self.notifications()
        self.assertEqual((row[6], row[13]), ("simulated", "local"))
        self.assertIsNotNone(row[11])

        (name,) = self.sink_files()
        parsed = email.message_from_string(self.sink_text(name))
        self.assertEqual(parsed["To"], "bob@example.org")
        self.assertTrue(parsed["Subject"].startswith(
            "[IT Helpdesk] [SIMULATED]"))
        self.assertEqual(parsed[render.SIMULATED_HEADER],
                         render.SIMULATED_VALUE)

    def test_queue_persists_until_a_worker_runs(self):
        db.create_ticket("t", "d", "Low", "alice")
        self.assertEqual({r[2] for r in self.statuses()}, {"pending"})

        # A brand-new worker (e.g. after a restart) finds everything
        self.local_worker().run_once()
        self.assertEqual({r[2] for r in self.statuses()}, {"simulated"})

        # And a second run sends nothing again
        before = self.sink_files()
        self.assertEqual(self.local_worker().run_once().claimed, 0)
        self.assertEqual(self.sink_files(), before)

    def test_limit(self):
        for _ in range(3):
            db.create_ticket("t", "d", "Low", "alice")  # 3 rows each
        self.assertEqual(self.local_worker().run_once(limit=4).claimed, 4)
        self.assertEqual(self.local_worker().run_once(limit=100).claimed, 5)

    def test_crash_after_writing_does_not_write_twice(self):
        db.update_ticket(self.bob_ticket, "Closed", None, actor="tech")
        worker = self.local_worker(lease_seconds=60)

        # Simulate a worker that wrote the file and then died before
        # recording the result
        with patch("notifications.queue.finish",
                   side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.run_once()

        self.assertEqual(len(self.sink_files()), 1)
        self.assertEqual(self.statuses()[0][2], "processing")

        self.clock.advance(seconds=61)          # lease expires
        self.local_worker().run_once()          # recovered, backed off
        self.clock.advance(seconds=60)
        self.local_worker().run_once()          # delivered (idempotent)

        self.assertEqual(self.statuses()[0][2], "simulated")
        self.assertEqual(len(self.sink_files()), 1)
        self.assertEqual(
            [a[1] for a in self.attempts(self.notifications()[0][0])],
            ["interrupted", "simulated"]
        )


class TestRecipientCheckedAgainAtDelivery(WorkerTestCase):

    def queue_for_bob(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        return self.notifications()[0][0]

    def assert_cancelled(self, reason):
        self.local_worker().run_once()
        (row,) = self.notifications()
        self.assertEqual((row[6], row[9]), ("cancelled", reason))
        self.assertEqual(self.sink_files(), [])

    def test_deactivated_after_queueing(self):
        self.queue_for_bob()
        accounts.set_active(self.ids["root"], self.ids["bob"], False)
        self.assert_cancelled("recipient account is deactivated")

    def test_no_address(self):
        self.queue_for_bob()
        self.set_email("bob", None)
        self.assert_cancelled("recipient has no email address")

    def test_deleted_account(self):
        self.queue_for_bob()
        self.sql("DELETE FROM users WHERE username = 'bob'")
        self.assert_cancelled("recipient account no longer exists")

    def test_demoted_assignee(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        accounts.change_role(self.ids["root"], self.ids["tech"], "user")
        self.sql("DELETE FROM notifications WHERE event_type = "
                 "'role_changed'")

        self.assert_cancelled("recipient is no longer support staff")

    def test_account_id_now_belongs_to_a_different_username(self):
        # Usernames are immutable (migration 7). If that protection were
        # ever bypassed, the message must not follow the id to someone else
        self.queue_for_bob()
        self.sql("DROP TRIGGER users_username_immutable")
        self.sql("UPDATE users SET username = 'mallory' WHERE id = ?",
                 (self.ids["bob"],))
        self.assert_cancelled("recipient account no longer exists")

    def test_requester_changed(self):
        self.queue_for_bob()
        self.sql("UPDATE tickets SET created_by = 'alice' WHERE id = ?",
                 (self.bob_ticket,))
        self.assert_cancelled("recipient is no longer the requester")

    def test_address_used_is_the_current_one(self):
        self.queue_for_bob()
        self.set_email("bob", "bob.new@example.org")
        self.local_worker().run_once()

        (name,) = self.sink_files()
        self.assertIn("To: bob.new@example.org", self.sink_text(name))


class TestMessageContent(WorkerTestCase):

    def deliver_all(self, **settings):
        self.local_worker(**settings).run_once(limit=1000)
        return {name: self.sink_text(name) for name in self.sink_files()}

    def test_every_event_renders_without_sensitive_data(self):
        ticket = db.create_ticket(TITLE, DESCRIPTION, "High", "alice")
        db.update_ticket(ticket, "In Progress", "tech", actor="root")
        db.add_comment(ticket, "tech", COMMENT)
        db.add_comment(ticket, "alice", COMMENT)
        self.add_user("tech2", "technician")
        self.set_email("tech2", "tech2@example.org")
        db.update_ticket(ticket, "Resolved", "tech2", actor="root")
        db.update_ticket(ticket, "Closed", "tech2", actor="tech2")

        client = self.signed_in("alice")
        client.post("/account/password", data={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD})
        accounts.reset_password(self.ids["root"], self.ids["bob"],
                                "temporary-ocean-phrase-55")
        accounts.change_role(self.ids["root"], self.ids["bob"],
                             "technician")
        accounts.set_active(self.ids["root"], self.ids["bob"], False)
        accounts.set_active(self.ids["root"], self.ids["bob"], True)

        queued = {r[1] for r in self.notifications()}
        self.assertEqual(queued, set(render.SUBJECTS))

        messages = self.deliver_all()
        self.assertEqual(len(messages), len(self.notifications()))

        cookie = client.get_cookie("session")
        forbidden = [TITLE, DESCRIPTION, COMMENT, "12-34-56", "774411",
                     PASSWORD, NEW_PASSWORD, "temporary-ocean-phrase-55",
                     "pbkdf2", "scrypt", "session_version"]
        if cookie is not None:
            forbidden.append(cookie.value)

        for name, text in messages.items():
            for secret in forbidden:
                with self.subTest(message=name, secret=secret[:12]):
                    self.assertNotIn(secret, text)

        # Nor in the queue or its attempt history
        stored = " ".join(
            str(value) for table in ("notifications",
                                     "notification_attempts")
            for row in self.sql(f"SELECT * FROM {table}") for value in row
        )
        for secret in forbidden:
            self.assertNotIn(secret, stored)

    def test_security_messages_have_the_warning(self):
        accounts.reset_password(self.ids["root"], self.ids["bob"],
                                "temporary-ocean-phrase-55")
        (text,) = self.deliver_all().values()

        self.assertIn("reset by an administrator", text)
        self.assertIn("contact IT support immediately", text)
        self.assertIn("never sent by email", text)
        self.assertNotIn("root", text.split("\n\n", 1)[1])

    def test_ticket_link_only_when_configured(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        (text,) = self.deliver_all().values()
        self.assertIn(f"Sign in to the IT Helpdesk to view ticket "
                      f"#{self.bob_ticket}", text)

    def test_ticket_link(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        (text,) = self.deliver_all(
            base_url="https://helpdesk.example.org").values()
        self.assertIn(
            f"https://helpdesk.example.org/tickets/{self.bob_ticket}", text
        )

    def test_headers_cannot_be_injected_through_stored_values(self):
        # Even a hand-edited row cannot add headers or recipients
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        self.sql("UPDATE notifications SET payload = ?, "
                 "recipient_username = ?",
                 ('{"ticket_id": "1\\nBcc: evil@example.org", '
                  '"actor": "x\\r\\nBcc: evil@example.org"}',
                  "bob"))

        self.local_worker().run_once()
        (row,) = self.notifications()

        # The email library refuses a header containing a line break, so
        # the message is never built, never written and never retried
        self.assertEqual((row[6], row[9]), ("failed", "render: ValueError"))
        self.assertEqual(self.sink_files(), [])

    def test_line_breaks_in_body_values_stay_in_the_body(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        self.sql("UPDATE notifications SET payload = ?",
                 ('{"ticket_id": 1, '
                  '"actor": "x\\r\\nBcc: evil@example.org"}',))

        self.local_worker().run_once()

        (name,) = self.sink_files()
        parsed = email.message_from_string(self.sink_text(name))
        self.assertIsNone(parsed["Bcc"])
        self.assertEqual(parsed.get_all("To"), ["bob@example.org"])

    def test_unrenderable_row_fails_permanently(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        self.sql("UPDATE notifications SET payload = 'not json'")

        self.local_worker().run_once()
        (row,) = self.notifications()
        self.assertEqual(row[6], "failed")
        self.assertTrue(row[9].startswith("render: "))

    def test_message_id_is_stable_across_retries(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        FakeSMTP.fail_send = [OSError("reset")]

        self.smtp_worker().run_once()
        self.clock.advance(hours=1)
        self.smtp_worker().run_once()

        self.assertEqual(len(FakeSMTP.sent), 1)
        (row,) = self.notifications()
        expected = render.message_id(row[0], row[10],
                                     "helpdesk@example.org")
        self.assertEqual(FakeSMTP.sent[0]["Message-ID"], expected)


class TestSmtpOutages(WorkerTestCase):

    def setUp(self):
        super().setUp()
        for _ in range(3):
            db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
            db.update_ticket(self.bob_ticket, "Open", None, actor="tech")
        self.assertEqual(len(self.notifications()), 3)

    def test_unreachable_server_pauses_without_using_attempts(self):
        FakeSMTP.fail_connect = ConnectionRefusedError()

        summary = self.smtp_worker().run_once()

        self.assertEqual(summary.claimed, 1)
        self.assertTrue(summary.stopped_reason.startswith("connect:"))
        rows = self.notifications()
        self.assertEqual([(r[6], r[7], r[8]) for r in rows],
                         [("pending", 0, 1), ("pending", 0, 0),
                          ("pending", 0, 0)])

        # Server back: everything goes out
        FakeSMTP.fail_connect = None
        self.clock.advance(hours=1)
        self.smtp_worker().run_once()
        self.assertEqual({r[6] for r in self.notifications()}, {"sent"})
        self.assertEqual(len(FakeSMTP.sent), 3)

    def test_bad_credentials_pause_and_never_leak(self):
        FakeSMTP.fail_login = smtplib.SMTPAuthenticationError(
            535, b"bad credentials for mailer/s3cret-smtp-pass"
        )

        with self.assertLogs("notifications", "INFO") as logs:
            summary = self.smtp_worker().run_once()

        self.assertIn("sign-in", summary.stopped_reason)
        stored = " ".join(str(v) for t in ("notifications",
                                           "notification_attempts")
                          for r in self.sql(f"SELECT * FROM {t}")
                          for v in r)
        for text in (stored, "\n".join(logs.output), repr(summary)):
            self.assertNotIn("s3cret-smtp-pass", text)

    def test_timeout_mid_send_counts_and_retries_later(self):
        FakeSMTP.fail_send = [TimeoutError()]

        summary = self.smtp_worker().run_once()

        self.assertEqual(summary.outcomes, {"pending": 1})
        first = self.notifications()[0]
        self.assertEqual((first[6], first[7]), ("pending", 1))

    def test_permanent_refusal_does_not_stop_the_others(self):
        FakeSMTP.fail_send = [smtplib.SMTPRecipientsRefused(
            {"bob@example.org": (550, b"unknown")})]

        summary = self.smtp_worker().run_once()

        self.assertEqual(summary.outcomes, {"failed": 1, "sent": 2})
        self.assertEqual(summary.stopped_reason, "")

    def test_duplicates_are_bounded_by_max_attempts(self):
        # A server that accepts every message but always times out
        # before confirming: the worst case for duplicates
        FakeSMTP.fail_send = [TimeoutError()] * 100
        for _ in range(20):
            self.smtp_worker(max_attempts=3).run_once()
            self.clock.advance(hours=2)

        attempts = [r[7] for r in self.notifications()]
        self.assertTrue(all(n <= 3 for n in attempts), attempts)
        self.assertEqual({r[6] for r in self.notifications()}, {"failed"})

    def test_unexpected_error_is_recorded_safely(self):
        with patch("notifications.render.build_message",
                   side_effect=RuntimeError("secret detail")), \
                self.assertLogs("notifications.worker", "ERROR"):
            summary = self.local_worker().run_once()

        self.assertEqual(summary.stopped_reason, "unexpected error")
        row = self.notifications()[0]
        self.assertEqual((row[6], row[9]),
                         ("pending", "worker: unexpected error"))


class TestLoop(WorkerTestCase):

    def test_loop_stops_when_asked(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        stop = threading.Event()
        runs = []

        def on_summary(summary):
            runs.append(summary.claimed)
            if len(runs) >= 2:
                stop.set()

        thread = threading.Thread(
            target=self.local_worker().run_forever,
            kwargs=dict(interval=0.01, stop_event=stop,
                        on_summary=on_summary)
        )
        thread.start()
        thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(runs[:2], [1, 0])

    def test_stop_between_messages(self):
        for _ in range(3):
            db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
            db.update_ticket(self.bob_ticket, "Open", None, actor="tech")

        calls = {"n": 0}

        def stop_after_first():
            calls["n"] += 1
            return calls["n"] > 1

        summary = self.local_worker().run_once(should_stop=stop_after_first)

        self.assertEqual(summary.claimed, 1)
        self.assertEqual(summary.stopped_reason, "stop requested")
        # Nothing was left half-done
        self.assertNotIn("processing", {r[6] for r in self.notifications()})


if __name__ == "__main__":
    unittest.main()
