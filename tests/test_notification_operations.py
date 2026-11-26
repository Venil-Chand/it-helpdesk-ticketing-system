"""
Running the notification worker in production: secrets from files,
durable records of queueing failures, monitoring numbers, the audited
manual requeue, the heartbeat, crash recovery of a killed worker process
and several worker processes delivering concurrently.

No real email is sent. The in-process tests block the network entirely
(NotificationTestCase). The process tests use the local sink, or an
"SMTP server" on 127.0.0.1 that accepts connections and never answers.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import accounts
import database as db
from app import app
from notifications import events, heartbeat, queue, timeutil
from notifications import settings as delivery_settings
from notifications import worker

from notification_helpers import NotificationTestCase
from security_helpers import PROJECT_ROOT, TESTING_ENV, clean_environment


def real_utc_now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------
# SETTINGS
# ---------------------------------------------------

class TestSmtpPasswordFile(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "smtp-password")
        Path(self.path).write_text(" pass with spaces \n")
        os.chmod(self.path, 0o600)

    def environ(self, **extra):
        return {"NOTIFICATION_DELIVERY": "smtp",
                "NOTIFICATION_SENDER": "helpdesk@example.org",
                "NOTIFICATION_SMTP_HOST": "smtp.example.org",
                "NOTIFICATION_SMTP_USERNAME": "helpdesk", **extra}

    def test_password_is_read_from_the_file_unstripped(self):
        settings = delivery_settings.load_delivery_settings(
            self.environ(NOTIFICATION_SMTP_PASSWORD_FILE=self.path),
            production=True)
        self.assertEqual(settings.smtp_password, " pass with spaces ")
        self.assertNotIn("pass with spaces", repr(settings))

    def test_value_and_file_together_are_refused(self):
        with self.assertRaisesRegex(delivery_settings.NotificationConfigError,
                                    "Set only one"):
            delivery_settings.load_delivery_settings(self.environ(
                NOTIFICATION_SMTP_PASSWORD="x",
                NOTIFICATION_SMTP_PASSWORD_FILE=self.path))

    def test_readable_file_is_refused_in_production_only(self):
        os.chmod(self.path, 0o644)
        environ = self.environ(NOTIFICATION_SMTP_PASSWORD_FILE=self.path)

        with self.assertRaises(delivery_settings.NotificationConfigError) as \
                caught:
            delivery_settings.load_delivery_settings(environ,
                                                     production=True)
        self.assertNotIn(self.path, str(caught.exception))

        delivery_settings.load_delivery_settings(environ, production=False)


# ---------------------------------------------------
# QUEUEING FAILURES ARE RECORDED
# ---------------------------------------------------

class TestQueueFailureRecords(NotificationTestCase):

    def queue_failures(self):
        return self.sql(
            "SELECT actor, action, new_value FROM audit_events "
            "WHERE action = 'notification_queue_failed' ORDER BY id")

    def test_failure_is_recorded_with_the_saved_change(self):
        self.clear_queue()

        with patch.object(events, "_clean_payload",
                          side_effect=ValueError("bug")), \
                self.assertLogs("notifications.events", "ERROR"):
            ticket_id = db.create_ticket("Laptop", "Broken", "High", "alice")

        self.assertIsNotNone(db.get_ticket(ticket_id))
        self.assertEqual(self.notifications(), [])
        self.assertEqual(self.queue_failures(), [
            ("system", "notification_queue_failed",
             f"new ticket {ticket_id} (ValueError)")])

    def test_nothing_is_recorded_when_the_change_itself_is_rolled_back(self):
        conn = db.get_db()
        conn.execute("BEGIN")

        with self.assertLogs("notifications.events", "ERROR"):
            events._guarded(conn, "a change that will be undone",
                            lambda: 1 / 0)

        conn.rollback()
        conn.close()

        self.assertEqual(self.queue_failures(), [])

    def test_a_failing_record_does_not_break_the_change(self):
        self.clear_queue()
        self.sql("""
            CREATE TRIGGER refuse_queue_failure_records
            BEFORE INSERT ON audit_events
            WHEN NEW.action = 'notification_queue_failed'
            BEGIN SELECT RAISE(ABORT, 'refused'); END
        """)

        with patch.object(events, "_clean_payload",
                          side_effect=ValueError("bug")), \
                self.assertLogs("notifications.events", "ERROR") as logs:
            ticket_id = db.create_ticket("Laptop", "Broken", "High", "alice")

        self.assertIsNotNone(db.get_ticket(ticket_id))
        self.assertIn("could not be recorded", "\n".join(logs.output))

    def test_no_ticket_text_is_stored(self):
        with patch.object(events, "_clean_payload",
                          side_effect=ValueError("bug")), \
                self.assertLogs("notifications.events", "ERROR"):
            db.create_ticket("Secret salary review", "Private", "High",
                             "alice")

        self.assertNotIn("salary", json.dumps(self.queue_failures()))

    def test_shown_on_the_audit_log_operations_filter(self):
        with patch.object(events, "_clean_payload",
                          side_effect=ValueError("bug")), \
                self.assertLogs("notifications.events", "ERROR"):
            db.create_ticket("Laptop", "Broken", "High", "alice")

        self.assertEqual(
            [row["action"] for row in
             accounts.list_audit_events(category="operations")],
            ["notification_queue_failed"])


# ---------------------------------------------------
# HEALTH NUMBERS
# ---------------------------------------------------

class TestQueueHealth(NotificationTestCase):

    def test_counts(self):
        # Bob's ticket queued 3 notifications at START
        self.clock.advance(minutes=10)
        health = queue.health(db.DB_PATH, now=self.clock())

        self.assertEqual(health.pending, 3)
        self.assertEqual(health.due, 3)
        self.assertEqual(health.oldest_due_seconds, 600)
        self.assertEqual(health.stuck_processing, 0)
        self.assertEqual(health.failed_last_day, 0)

    def test_stuck_claims_and_failures(self):
        settings = self.local_worker().settings
        claim, _, _ = queue.claim_next(db.DB_PATH, settings, worker="w",
                                       mode="local")
        queue.finish(db.DB_PATH, claim, settings, worker="w",
                     outcome=queue.FAILED, error="render: KeyError")
        queue.claim_next(db.DB_PATH, settings, worker="w", mode="local")

        self.clock.advance(seconds=settings.lease_seconds + 1)
        health = queue.health(db.DB_PATH, now=self.clock())

        self.assertEqual(health.stuck_processing, 1)
        self.assertEqual(health.failed_last_day, 1)
        self.assertEqual(health.pending, 1)

    def test_nothing_due_means_zero_age(self):
        self.clear_queue()
        self.assertEqual(queue.health(db.DB_PATH).oldest_due_seconds, 0)


# ---------------------------------------------------
# MANUAL REQUEUE
# ---------------------------------------------------

class TestRequeue(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.settings = self.local_worker().settings
        claim, _, _ = queue.claim_next(db.DB_PATH, self.settings,
                                       worker="w", mode="local")
        self.failed_id = claim.id
        queue.finish(db.DB_PATH, claim, self.settings, worker="w",
                     outcome=queue.FAILED, error="smtp: 550 mailbox")

    def requeue(self, notification_id=None, reason="SMTP fixed"):
        return queue.requeue(db.DB_PATH, notification_id or self.failed_id,
                             operator="opsadmin", reason=reason,
                             now=self.clock())

    def test_a_new_copy_is_queued_and_history_is_kept(self):
        before = self.attempts(self.failed_id)
        new_id = self.requeue()

        original = self.notifications("id = ?", (self.failed_id,))[0]
        copy = self.notifications("id = ?", (new_id,))[0]

        self.assertEqual(original[6], "failed")
        self.assertEqual(self.attempts(self.failed_id), before)
        self.assertEqual(copy[6], "pending")
        self.assertEqual(copy[7], 0)
        self.assertEqual(copy[10], f"requeue:{self.failed_id}:1")
        self.assertEqual(copy[1:6], original[1:6])

    def test_requeue_is_audited_with_operator_and_reason(self):
        new_id = self.requeue(reason="  SMTP   password fixed  ")

        row = self.sql(
            "SELECT actor, action, target_username, old_value, new_value "
            "FROM audit_events WHERE action = 'notification_requeued'")[0]

        self.assertEqual(row[0], "cli:opsadmin")
        self.assertEqual(row[3],
                         f"notification {self.failed_id} (ticket_created)")
        self.assertEqual(row[4],
                         f"notification {new_id}; reason: SMTP password fixed")

    def test_the_copy_is_delivered_by_the_worker(self):
        new_id = self.requeue()
        self.local_worker().run_once()

        self.assertEqual(self.notifications("id = ?", (new_id,))[0][6],
                         "simulated")

    def test_only_failed_notifications(self):
        pending_id = self.notifications("status = 'pending'")[0][0]

        with self.assertRaisesRegex(queue.RequeueError,
                                    "is pending; only failed"):
            self.requeue(pending_id)

        with self.assertRaisesRegex(queue.RequeueError, "does not exist"):
            self.requeue(99999)

    def test_at_most_three_requeues_per_original(self):
        copy_id = self.failed_id

        for _ in range(queue.MAX_REQUEUES):
            copy_id = queue.requeue(db.DB_PATH, copy_id, operator="o",
                                    reason="again", now=self.clock())
            # Make the copy fail too, so it could be requeued again
            self.sql("UPDATE notifications SET status = 'failed' "
                     "WHERE id = ?", (copy_id,))

        with self.assertRaisesRegex(queue.RequeueError, "maximum"):
            self.requeue(copy_id)
        with self.assertRaisesRegex(queue.RequeueError, "maximum"):
            self.requeue(self.failed_id)

        self.assertEqual(len(self.sql(
            "SELECT id FROM audit_events "
            "WHERE action = 'notification_requeued'")), 3)

    def test_reason_is_required_and_bounded(self):
        for reason in ("", "   ", "x" * 201):
            with self.subTest(reason=reason[:5]):
                with self.assertRaises(queue.RequeueError):
                    self.requeue(reason=reason)

        self.assertEqual(self.notifications("dedupe_key LIKE 'requeue:%'"),
                         [])

    def test_a_deactivated_recipient_is_still_checked_at_delivery(self):
        new_id = self.requeue()
        recipient = self.notifications("id = ?", (new_id,))[0][2]
        self.sql("UPDATE users SET is_active = 0 WHERE username = ?",
                 (recipient,))

        self.local_worker().run_once()

        self.assertEqual(self.notifications("id = ?", (new_id,))[0][6],
                         "cancelled")


class TestRequeueCommand(TestRequeue):

    def run_command(self, *args, interactive=False, input=None):
        runner = app.test_cli_runner()
        with patch("helpdesk.cli._operator", return_value="opsadmin"), \
                patch("helpdesk.cli._interactive", return_value=interactive):
            return runner.invoke(args=["notifications-requeue", *args],
                                 input=input)

    def test_command_requeues_with_yes(self):
        result = self.run_command(str(self.failed_id), "--reason", "fixed",
                                  "--yes")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"#{self.failed_id}: requeued as #", result.output)

    def test_command_needs_yes_when_not_interactive(self):
        result = self.run_command(str(self.failed_id), "--reason", "fixed")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("--yes", result.output)
        self.assertEqual(self.notifications("dedupe_key LIKE 'requeue:%'"),
                         [])

    def test_command_asks_in_a_terminal(self):
        result = self.run_command(str(self.failed_id), "--reason", "fixed",
                                  interactive=True, input="n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.notifications("dedupe_key LIKE 'requeue:%'"),
                         [])

    def test_command_reports_refusals_and_exits_1(self):
        result = self.run_command("99999", "--reason", "fixed", "--yes")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist", result.output)

    def test_command_requires_a_reason(self):
        result = self.run_command(str(self.failed_id), "--yes")
        self.assertNotEqual(result.exit_code, 0)


# ---------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------

class TestHeartbeat(NotificationTestCase):

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / heartbeat.FILE_NAME

    def test_written_atomically_privately_and_throttled(self):
        ticks = [100.0]
        beat = heartbeat.Heartbeat(self.path, "host:1", min_interval=15,
                                   clock=lambda: ticks[0])

        beat()
        first = self.path.read_text()
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(self.path.parent.stat().st_mode & 0o777),
                         "0o700")

        ticks[0] = 105.0
        with patch.object(heartbeat, "write_status") as write:
            beat()
        write.assert_not_called()

        ticks[0] = 116.0
        with patch.object(heartbeat, "write_status") as write:
            beat()
        write.assert_called_once()

        status = json.loads(first)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["worker"], "host:1")

    def test_paused_and_stopped_states(self):
        beat = heartbeat.Heartbeat(self.path, "host:1")
        summary = worker.RunSummary(claimed=1,
                                    stopped_reason="smtp: connection refused")

        beat(summary)
        status = heartbeat.read_status(self.path)
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["paused_reason"], "smtp: connection refused")
        self.assertEqual(status["last_run"]["claimed"], 1)

        beat.stopped()
        self.assertEqual(heartbeat.read_status(self.path)["state"], "stopped")

    def test_run_forever_beats_for_each_message_and_run(self):
        calls = []
        stop = threading.Event()

        def on_summary(summary):
            stop.set()

        self.local_worker().run_forever(
            interval=1, stop_event=stop, on_summary=on_summary,
            heartbeat=lambda summary=None: calls.append(summary))

        # One call before each of the 3 messages and the empty claim,
        # then one with the run's summary
        self.assertEqual(calls[:-1], [None] * 4)
        self.assertEqual(calls[-1].claimed, 3)

    def test_unwritable_directory_does_not_stop_delivery(self):
        beat = heartbeat.Heartbeat("/proc/not-writable/x.json", "host:1")
        beat()  # no exception

    def test_age(self):
        now = datetime(2026, 11, 25, 9, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(heartbeat.status_age_seconds(
            {"time": "2026-11-25T09:00:00+00:00"}, now), 30)
        self.assertIsNone(heartbeat.status_age_seconds({}, now))

    def test_missing_or_damaged_file(self):
        self.assertIsNone(heartbeat.read_status(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertIsNone(heartbeat.read_status(self.path))


# ---------------------------------------------------
# REAL WORKER PROCESSES
# ---------------------------------------------------

class StallingSmtpServer:
    """Accepts connections on 127.0.0.1 and never says anything."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.connections = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections.append(conn)

    def close(self):
        self.server.close()
        for conn in self.connections:
            conn.close()


class WorkerProcessTestCase(NotificationTestCase):

    def setUp(self):
        super().setUp()
        # Rows must be queued at real time: the processes use the real clock
        self.clock.moment = real_utc_now()
        self.clear_queue()

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_dir = os.path.join(directory.name, "state")

    def worker_environment(self, **extra):
        environ = clean_environment(dict(
            TESTING_ENV,
            HELPDESK_DATABASE_PATH=self.db_path,
            HELPDESK_STATE_DIR=self.state_dir,
            NOTIFICATION_DELIVERY="local",
            NOTIFICATION_SINK_DIR=self.sink_dir))

        # Only this test's notification settings reach the worker
        for name in [n for n in environ if n.startswith("NOTIFICATION_")
                     and n not in ("NOTIFICATION_DELIVERY",
                                   "NOTIFICATION_SINK_DIR")]:
            del environ[name]

        environ.update(extra)
        return environ

    def flask(self, *args, environ, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-m", "flask", "--app", "app", *args],
            cwd=PROJECT_ROOT, env=environ, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, **kwargs)

    def queue_many(self, count):
        for n in range(count):
            db.create_ticket(f"Ticket {n}", "x", "Low", "alice")


class TestWorkerCrashRecovery(WorkerProcessTestCase):

    def test_killed_worker_mid_delivery_is_recovered_once(self):
        db.create_ticket("Laptop", "Broken", "High", "alice")
        queued = len(self.notifications())
        self.assertGreater(queued, 0)

        smtp = StallingSmtpServer()
        self.addCleanup(smtp.close)

        process = self.flask(
            "notifications-send", "--loop", "--interval", "1",
            environ=self.worker_environment(
                NOTIFICATION_DELIVERY="smtp",
                NOTIFICATION_SENDER="helpdesk@example.org",
                NOTIFICATION_SMTP_HOST="localhost",
                NOTIFICATION_SMTP_PORT=str(smtp.port),
                NOTIFICATION_SMTP_SECURITY="none",
                NOTIFICATION_SMTP_TIMEOUT="20",
                NOTIFICATION_LEASE_SECONDS="120"))
        self.addCleanup(lambda: process.poll() is None and process.kill())

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.notifications(
                "status = 'processing'"):
            time.sleep(0.1)

        processing = self.notifications("status = 'processing'")
        self.assertEqual(len(processing), 1, process.stdout and "")

        # The worker is stuck talking to the server: kill it outright,
        # as an out-of-memory kill or power loss would
        process.kill()
        process.wait(10)
        process.stdout.close()

        stuck_id = processing[0][0]
        self.assertEqual(queue.health(db.DB_PATH, now=real_utc_now())
                         .stuck_processing, 0)  # lease not yet expired

        # After the lease, another worker recovers the row
        self.clock.moment = real_utc_now() + timedelta(seconds=121)
        self.assertEqual(queue.health(db.DB_PATH, now=self.clock())
                         .stuck_processing, 1)

        summary = self.local_worker().run_once()

        self.assertEqual(summary.recovered, 1)
        self.assertEqual([row[1] for row in self.attempts(stuck_id)],
                         ["interrupted"])

        # It is retried after the normal backoff, not immediately, so a
        # crash loop cannot turn into a delivery storm
        recovered = self.notifications("id = ?", (stuck_id,))[0]
        self.assertEqual(recovered[6], "pending")
        self.assertAlmostEqual(
            (timeutil.parse_utc(recovered[12]) - self.clock())
            .total_seconds(), 60, delta=1)

        self.clock.advance(seconds=61)
        self.local_worker().run_once()

        self.assertEqual(
            [row[1] for row in self.attempts(stuck_id)],
            ["interrupted", "simulated"])
        # The interrupted attempt is counted: it may have reached a server
        self.assertEqual(self.notifications("id = ?", (stuck_id,))[0][7], 2)
        self.assertEqual(len(self.notifications("status = 'simulated'")),
                         queued)


class TestConcurrentWorkerProcesses(WorkerProcessTestCase):

    def test_three_processes_deliver_each_message_exactly_once(self):
        self.queue_many(10)
        queued = len(self.notifications())
        self.assertGreaterEqual(queued, 30)

        environ = self.worker_environment()
        processes = [self.flask("notifications-send", "--limit", "1000",
                                environ=environ) for _ in range(3)]

        for process in processes:
            output, _ = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, output)

        rows = self.notifications()
        self.assertEqual({row[6] for row in rows}, {"simulated"})
        self.assertEqual({row[7] for row in rows}, {1})
        self.assertEqual(len(self.sink_files()), queued)

        attempts = self.sql("SELECT notification_id, COUNT(*) "
                            "FROM notification_attempts "
                            "GROUP BY notification_id")
        self.assertEqual({count for _, count in attempts}, {1})
        self.assertEqual(len(attempts), queued)

        workers = {row[0] for row in self.sql(
            "SELECT DISTINCT worker FROM notification_attempts")}
        self.assertGreaterEqual(len(workers), 1)

    def test_loop_worker_writes_a_heartbeat_and_stops_on_sigterm(self):
        process = self.flask("notifications-send", "--loop", "--interval",
                             "1", environ=self.worker_environment())
        self.addCleanup(lambda: process.poll() is None and process.kill())

        path = Path(self.state_dir) / heartbeat.FILE_NAME
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.1)

        status = heartbeat.read_status(path)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["pid"], process.pid)

        process.terminate()
        output, _ = process.communicate(timeout=30)

        self.assertEqual(process.returncode, 0, output)
        self.assertIn("Worker stopped.", output)
        self.assertEqual(heartbeat.read_status(path)["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
