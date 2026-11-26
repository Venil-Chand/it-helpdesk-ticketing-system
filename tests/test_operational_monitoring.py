"""
Health endpoints (/healthz, /readyz) and "flask ops-check".

Health answers must never reveal credentials, tokens, file paths,
versions or account data. ops-check must notice each failure it is
responsible for: an old or missing backup, a failed or overdue drill, a
dead, stopped or paused notification worker, a notification backlog,
queueing failures, an outdated schema and an unreachable Redis.
"""

import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import backups
import database as db
import migrations
from helpdesk import clock, create_app, ops_cli
from notifications import heartbeat

from redis_helpers import UNREACHABLE_REDIS_URL
from security_helpers import (
    HTTPS_URL,
    PRODUCTION_ENV,
    SINGLE_PROCESS_RATE_LIMITS,
    TEST_SECRET_KEY,
)

def now():
    return datetime.now(timezone.utc)

SMTP_SETTINGS = {
    "NOTIFICATION_DELIVERY": "smtp",
    "NOTIFICATION_SENDER": "helpdesk@example.org",
    "NOTIFICATION_SMTP_HOST": "smtp.example.org",
}


class MonitoringTestCase(unittest.TestCase):

    environ = PRODUCTION_ENV

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.db_path = os.path.join(self.root, "data", "helpdesk.db")
        os.makedirs(os.path.dirname(self.db_path), mode=0o700)

        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        migrations.migrate(self.db_path)
        self.app = create_app(environ=dict(self.environ))

    def assert_reveals_nothing(self, text, *, public=True):
        """
        No secrets or paths anywhere. Public health answers also omit
        versions; ops-check output is for operators and may name the
        schema version.
        """

        hidden = [self.root, TEST_SECRET_KEY, "helpdesk.db",
                  "not-a-real-password", "127.0.0.1", "Traceback"]
        if public:
            hidden += ["version", str(migrations.SCHEMA_VERSION)]

        for secret in hidden:
            self.assertNotIn(secret, text)


# ---------------------------------------------------
# /healthz and /readyz
# ---------------------------------------------------

class TestHealthEndpoints(MonitoringTestCase):

    def get(self, path, app=None, **kwargs):
        kwargs.setdefault("base_url", HTTPS_URL)
        return (app or self.app).test_client().get(path, **kwargs)

    def test_liveness_is_plain_and_needs_nothing(self):
        os.unlink(self.db_path)
        response = self.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok\n")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(os.path.exists(self.db_path))

    def test_ready_lists_only_check_names(self):
        response = self.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "status": "ready",
            "checks": {"schema": "ok", "database": "ok",
                       "rate_limit_storage": "ok"}})
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_not_ready_when_the_database_is_missing(self):
        os.unlink(self.db_path)

        with self.assertLogs(self.app.logger, "ERROR") as logs:
            response = self.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"status": "not ready",
                                         "checks": {"database": "fail"}})
        self.assertEqual(response.headers["Retry-After"], "30")
        self.assert_reveals_nothing(response.text)
        # The reason is in the log for the administrator
        self.assertIn("database file does not exist", "\n".join(logs.output))

    def test_not_ready_when_the_schema_is_outdated(self):
        os.unlink(self.db_path)
        migrations.migrate(self.db_path,
                           migrations=migrations.MIGRATIONS[:-1])

        with self.assertLogs(self.app.logger, "ERROR"):
            response = self.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["checks"]["schema"], "fail")
        self.assert_reveals_nothing(response.text)

    def test_not_ready_when_redis_is_unreachable(self):
        environ = {key: value for key, value in PRODUCTION_ENV.items()
                   if key not in SINGLE_PROCESS_RATE_LIMITS}
        app = create_app(environ=dict(
            environ, RATELIMIT_STORAGE_URI=UNREACHABLE_REDIS_URL))

        with self.assertLogs(app.logger, "ERROR"):
            response = self.get("/readyz", app=app)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["checks"]["rate_limit_storage"],
                         "fail")
        self.assert_reveals_nothing(response.text)

    def test_a_damaged_database_is_not_ready(self):
        Path(self.db_path).write_bytes(b"this is not a database" * 100)

        with self.assertLogs(self.app.logger, "ERROR"):
            response = self.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assert_reveals_nothing(response.text)

    def test_production_rules_still_apply(self):
        self.assertEqual(self.get("/readyz", base_url="http://helpdesk.test")
                         .status_code, 400)
        self.assertEqual(self.get("/healthz",
                                  base_url="https://evil.example")
                         .status_code, 400)

    def test_signed_in_session_is_not_needed_or_checked(self):
        client = self.app.test_client()
        with client.session_transaction(base_url=HTTPS_URL) as session:
            session["user_id"] = 999
            session["session_version"] = 1
            session["issued_at"] = clock.current_time()

        # The same cookie on a normal page does make the account lookup
        with patch.object(db, "get_user_by_id",
                          side_effect=sqlite3.OperationalError("down")) as \
                lookup, self.assertLogs(self.app.logger, "ERROR"):
            self.assertEqual(client.get("/", base_url=HTTPS_URL).status_code,
                             503)
            lookup.assert_called()
            lookup.reset_mock()

            for path in ("/healthz", "/readyz"):
                with self.subTest(path=path):
                    response = client.get(path, base_url=HTTPS_URL)
                    self.assertEqual(response.status_code, 200)
            lookup.assert_not_called()

    def test_only_get(self):
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                response = self.app.test_client().post(path,
                                                       base_url=HTTPS_URL)
                self.assertIn(response.status_code, (400, 405))


# ---------------------------------------------------
# ops-check
# ---------------------------------------------------

class TestOpsCheck(MonitoringTestCase):

    def setUp(self):
        super().setUp()
        self.backup_dir = os.path.join(self.root, "backups")
        self.offsite_dir = os.path.join(self.root, "offsite")
        os.makedirs(self.offsite_dir, mode=0o700)
        self.state_dir = os.path.join(self.root, "state")
        self.key = backups.write_new_key(os.path.join(self.root, "key"))

        env = patch.dict(os.environ, {
            "HELPDESK_BACKUP_DIR": self.backup_dir,
            "HELPDESK_BACKUP_OFFSITE_DIR": self.offsite_dir,
            "HELPDESK_STATE_DIR": self.state_dir,
            **SMTP_SETTINGS,
        })
        env.start()
        self.addCleanup(env.stop)

        self.make_healthy()

    def make_healthy(self):
        result = backups.create_backup(self.db_path, self.backup_dir,
                                       self.key)
        backups.transfer(result.path, self.offsite_dir, self.key)
        backups.record_drill(backups.run_drill(result.path, self.key),
                             self.backup_dir)
        self.beat()

    def beat(self, **status):
        heartbeat.write_status(
            Path(self.state_dir) / heartbeat.FILE_NAME,
            {"time": datetime.now(timezone.utc).isoformat(), "pid": 1,
             "worker": "w", "state": "running", **status})

    def run_check(self, *args):
        runner = self.app.test_cli_runner()
        return runner.invoke(args=["ops-check", *args])

    def levels(self, result):
        return {line.split()[1]: line.split()[0]
                for line in result.output.splitlines()
                if not line.startswith("Overall")}

    def assert_level(self, name, level, exit_code):
        result = self.run_check()
        self.assertEqual(self.levels(result).get(name), level, result.output)
        self.assertEqual(result.exit_code, exit_code, result.output)
        self.assert_reveals_nothing(result.output, public=False)
        return result

    def test_healthy_system_is_ok(self):
        result = self.run_check()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Overall: OK", result.output)
        self.assert_reveals_nothing(result.output, public=False)

    def test_json_output(self):
        result = self.run_check("--json")
        report = json.loads(result.output)

        self.assertEqual(report["status"], "OK")
        names = {check["name"] for check in report["checks"]}
        for expected in ("schema", "database", "rate_limit_storage",
                         "notification_backlog", "worker", "backups",
                         "backups_offsite", "restore_drill"):
            self.assertIn(expected, names)

    def test_no_backups(self):
        for name in os.listdir(self.backup_dir):
            if name.endswith((".hdbk", ".json")):
                os.unlink(os.path.join(self.backup_dir, name))
        self.assert_level("backups", "CRITICAL", 2)

    def test_old_backup_means_the_nightly_job_is_failing(self):
        for _, path in backups.list_backups(self.backup_dir):
            backups.remove_backup(path)
        backups.create_backup(self.db_path, self.backup_dir, self.key,
                              now=now() - timedelta(hours=30))
        self.assert_level("backups", "CRITICAL", 2)

    def test_backup_directory_not_configured_in_production(self):
        with patch.dict(os.environ, {"HELPDESK_BACKUP_DIR": ""}):
            self.assert_level("backups", "CRITICAL", 2)

    def test_missing_offsite_copy(self):
        for name in os.listdir(self.offsite_dir):
            os.unlink(os.path.join(self.offsite_dir, name))
        self.assert_level("backups_offsite", "WARNING", 1)

    def test_offsite_not_configured_in_production(self):
        with patch.dict(os.environ, {"HELPDESK_BACKUP_OFFSITE_DIR": ""}):
            self.assert_level("backups_offsite", "WARNING", 1)

    def test_failed_drill(self):
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        report = backups.DrillReport("x.hdbk", "", later.isoformat(), False,
                                     0.1, {}, ["damaged"])
        backups.record_drill(report, self.backup_dir)
        self.assert_level("restore_drill", "CRITICAL", 2)

    def test_overdue_drill(self):
        drills = os.path.join(self.backup_dir, backups.DRILL_DIRECTORY)
        for name in os.listdir(drills):
            os.unlink(os.path.join(drills, name))
        report = backups.DrillReport(
            "x.hdbk", "", (now() - timedelta(days=9)).isoformat(), True,
            0.1, {}, [])
        backups.record_drill(report, self.backup_dir)
        self.assert_level("restore_drill", "WARNING", 1)

    def test_worker_never_ran(self):
        os.unlink(Path(self.state_dir) / heartbeat.FILE_NAME)
        self.assert_level("worker", "CRITICAL", 2)

    def test_worker_heartbeat_stale(self):
        heartbeat.write_status(
            Path(self.state_dir) / heartbeat.FILE_NAME,
            {"time": (now() - timedelta(minutes=30)).isoformat(),
             "state": "running"})
        self.assert_level("worker", "CRITICAL", 2)

    def test_worker_stopped(self):
        self.beat(state="stopped")
        self.assert_level("worker", "CRITICAL", 2)

    def test_worker_paused_by_mail_server(self):
        self.beat(state="paused", paused_reason="smtp: connection refused")
        result = self.assert_level("worker", "WARNING", 1)
        self.assertIn("smtp: connection refused", result.output)

    def queue_due_notification(self, minutes_ago):
        moment = (now() - timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+00:00")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES ('alice', 'x', 'user')")
        conn.execute(
            "INSERT INTO notifications (event_type, dedupe_key, "
            "recipient_user_id, recipient_username, recipient_reason, "
            "created_at, updated_at, next_attempt_at) VALUES "
            "('password_changed', 'k', 1, 'alice', 'account_owner', ?, ?, ?)",
            (moment, moment, moment))
        conn.commit()
        conn.close()

    def test_backlog_warning_and_critical(self):
        self.queue_due_notification(20)
        self.assert_level("notification_backlog", "WARNING", 1)

        self.sql("UPDATE notifications SET next_attempt_at = ?",
                 ((now() - timedelta(minutes=90)).strftime(
                     "%Y-%m-%dT%H:%M:%S.000+00:00"),))
        self.assert_level("notification_backlog", "CRITICAL", 2)

    def test_permanent_failures(self):
        self.queue_due_notification(1)
        self.sql("UPDATE notifications SET status = 'failed', updated_at = ?",
                 (now().strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),))
        result = self.assert_level("notification_failures", "WARNING", 1)
        self.assertNotIn("alice", result.output)

    def test_queueing_failures(self):
        self.sql("INSERT INTO audit_events (actor, action, new_value) VALUES "
                 "('system', 'notification_queue_failed', 'new ticket 1')")
        self.assert_level("notification_queueing", "WARNING", 1)

    def test_outdated_schema(self):
        os.unlink(self.db_path)
        migrations.migrate(self.db_path,
                           migrations=migrations.MIGRATIONS[:-1])
        self.assert_level("schema", "CRITICAL", 2)

    def test_foreign_key_violation(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO comments (ticket_id, author, content) "
                     "VALUES (777, 'x', 'orphan')")
        conn.commit()
        conn.close()
        self.assert_level("database", "CRITICAL", 2)

    def test_redis_unreachable(self):
        environ = {key: value for key, value in PRODUCTION_ENV.items()
                   if key not in SINGLE_PROCESS_RATE_LIMITS}
        self.app = create_app(environ=dict(
            environ, RATELIMIT_STORAGE_URI=UNREACHABLE_REDIS_URL))
        self.assert_level("rate_limit_storage", "CRITICAL", 2)

    def test_simulated_delivery_in_production_is_a_warning(self):
        with patch.dict(os.environ, {"NOTIFICATION_DELIVERY": "local"}):
            self.assert_level("notification_settings", "WARNING", 1)

    def test_invalid_notification_settings(self):
        with patch.dict(os.environ, {"NOTIFICATION_SMTP_SECURITY": "bogus"}):
            self.assert_level("notification_settings", "CRITICAL", 2)

    def test_a_crashing_check_is_reported_not_hidden(self):
        with patch.object(ops_cli, "check_disk",
                          side_effect=RuntimeError("boom")):
            result = self.assert_level("ops_check", "CRITICAL", 2)
        self.assertIn("worker", result.output)

    def test_database_inside_the_code_folder_warns_in_production(self):
        with patch.object(ops_cli.backups, "_inside", return_value=True):
            self.assert_level("database_location", "WARNING", 1)

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(statement, params)
        conn.commit()
        conn.close()


class TestOpsCheckInDevelopment(MonitoringTestCase):

    environ = {"APP_ENV": "development", "SECRET_KEY": TEST_SECRET_KEY}

    def test_development_without_backups_or_worker_is_not_critical(self):
        with patch.dict(os.environ, {
            "HELPDESK_BACKUP_DIR": "", "HELPDESK_BACKUP_OFFSITE_DIR": "",
            "HELPDESK_STATE_DIR": os.path.join(self.root, "state"),
            "NOTIFICATION_DELIVERY": "",
        }):
            result = self.app.test_cli_runner().invoke(args=["ops-check"])

        self.assertIn(result.exit_code, (0, 1), result.output)
        self.assertNotIn("CRITICAL", result.output)


if __name__ == "__main__":
    unittest.main()
