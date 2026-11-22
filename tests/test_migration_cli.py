
"""
Tests for the database CLI commands, the startup check and the readiness
response.

database.DB_PATH points at a temporary file in every test, so the real
helpdesk.db is never opened.
"""

import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
from app import app, schema_ready_for_startup
from migrations import SCHEMA_VERSION
from tests.auth_helpers import sign_in


def _make_pre_sla_database(path, password_hash):
    """A pre-versioning database with every table except ticket_sla."""

    migrations.migrate(path, migrations=migrations.MIGRATIONS[:3])

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users (username, password_hash, role) "
        "VALUES ('alice', ?, 'user')",
        (password_hash,)
    )
    conn.execute(
        "INSERT INTO tickets (title, description, created_by) "
        "VALUES ('VPN down', 'Cannot connect', 'alice')"
    )
    # Databases from before versioning always report version 0
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


class CliTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = temp_dir.name
        self.path = os.path.join(self.dir, "cli.db")

        db_patch = patch.object(db, "DB_PATH", self.path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        self.runner = app.test_cli_runner()

    def invoke(self, *args):
        return self.runner.invoke(args=list(args))

    def version(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def file_hash(self, path=None):
        with open(path or self.path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------
# flask init-db
# ---------------------------------------------------

class TestInitDbCommand(CliTestCase):

    def test_creates_fresh_database(self):
        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Current schema version: 0", result.output)
        self.assertIn("Pending migrations:", result.output)
        for migration in migrations.MIGRATIONS:
            self.assertIn(
                f"applied {migration.version}: {migration.description}",
                result.output
            )
        self.assertIn(
            f"Database schema is now at version {SCHEMA_VERSION}.",
            result.output
        )
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_never_creates_users(self):
        self.invoke("init-db")

        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0
        )

    def test_second_run_reports_up_to_date(self):
        self.invoke("init-db")
        before = self.file_hash()

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("already up to date", result.output)
        self.assertNotIn("applied", result.output)
        self.assertEqual(self.file_hash(), before)

    def test_upgrades_legacy_database_and_keeps_data(self):
        password_hash = generate_password_hash("a-long-test-password!")
        _make_pre_sla_database(self.path, password_hash)

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("database created before schema versioning",
                      result.output)
        self.assertIn("applied 4: Add response SLA deadlines", result.output)
        self.assertEqual(self.version(), SCHEMA_VERSION)

        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT password_hash FROM users").fetchone()[0],
            password_hash
        )
        self.assertEqual(
            conn.execute("SELECT title FROM tickets").fetchone()[0],
            "VPN down"
        )
        # The password hash is never printed
        self.assertNotIn(password_hash, result.output)

    def test_warns_when_upgrading_without_backup(self):
        _make_pre_sla_database(self.path, "hash")

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("verified backup", result.output)

    def test_backup_option_writes_verified_backup_first(self):
        _make_pre_sla_database(self.path, "hash")
        backup = os.path.join(self.dir, "before-upgrade.db")

        result = self.invoke("init-db", "--backup-to", backup)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Verified backup written", result.output)
        self.assertEqual(self.version(), SCHEMA_VERSION)

        # The backup is the pre-upgrade database with all of its data.
        # (SQLite's backup copies pages, not bytes, so compare contents.)
        copy = sqlite3.connect(backup)
        self.addCleanup(copy.close)
        self.assertEqual(self.version(backup), 0)
        self.assertEqual(
            copy.execute("SELECT title FROM tickets").fetchall(),
            [("VPN down",)]
        )
        self.assertEqual(
            copy.execute("SELECT username, password_hash FROM users")
            .fetchall(),
            [("alice", "hash")]
        )
        tables = {row[0] for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        self.assertNotIn("ticket_sla", tables)

    def test_failed_backup_prevents_migration(self):
        _make_pre_sla_database(self.path, "hash")
        before = self.file_hash()
        backup = os.path.join(self.dir, "exists.db")
        with open(backup, "w") as handle:
            handle.write("existing file")

        result = self.invoke("init-db", "--backup-to", backup)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no migration was attempted", result.output)
        self.assertEqual(self.file_hash(), before)
        with open(backup) as handle:
            self.assertEqual(handle.read(), "existing file")

    def test_future_version_fails_with_exit_code(self):
        self.invoke("init-db")
        conn = sqlite3.connect(self.path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        conn.close()
        before = self.file_hash()

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("migration refused", result.output)
        self.assertEqual(self.file_hash(), before)

    def test_incompatible_database_fails_with_exit_code(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        conn.close()

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no tickets table", result.output)

    def test_failure_during_migration_rolls_back_with_exit_code(self):
        _make_pre_sla_database(self.path, "hash")
        conn = sqlite3.connect(self.path)
        # An orphan comment left behind by code that did not enforce
        # foreign keys; the final integrity check must refuse it
        conn.execute(
            "INSERT INTO comments (ticket_id, author, content) "
            "VALUES (999, 'bob', 'private comment text')"
        )
        conn.commit()
        conn.close()
        before = self.file_hash()

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("rolled back", result.output)
        self.assertNotIn("private comment text", result.output)
        self.assertEqual(self.file_hash(), before)
        self.assertEqual(self.version(), 0)

    def test_unreadable_file_fails_with_exit_code(self):
        with open(self.path, "wb") as handle:
            handle.write(b"this is not an SQLite database" * 50)

        result = self.invoke("init-db")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("could not read the database", result.output)

    def test_output_never_contains_secret_key(self):
        result = self.invoke("init-db")
        self.assertNotIn(app.secret_key, result.output)


# ---------------------------------------------------
# flask db-status
# ---------------------------------------------------

class TestDbStatusCommand(CliTestCase):

    def test_missing_database_is_not_created(self):
        result = self.invoke("db-status")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("does not exist yet", result.output)
        self.assertFalse(os.path.exists(self.path))

    def test_current_database_exits_zero(self):
        self.invoke("init-db")

        result = self.invoke("db-status")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"Current schema version: {SCHEMA_VERSION}",
                      result.output)
        self.assertIn("Pending migrations: none", result.output)

    def test_legacy_database_lists_pending_and_is_unchanged(self):
        _make_pre_sla_database(self.path, "hash")
        before = self.file_hash()

        result = self.invoke("db-status")

        self.assertEqual(result.exit_code, 1)
        for migration in migrations.MIGRATIONS:
            self.assertIn(f"  {migration.version}: ", result.output)
        self.assertIn("flask --app app init-db", result.output)
        self.assertEqual(self.file_hash(), before)

    def test_incompatible_database_reports_problems(self):
        self.invoke("init-db")
        conn = sqlite3.connect(self.path)
        conn.execute("DROP INDEX idx_tickets_date")
        conn.close()

        result = self.invoke("db-status")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Problem: missing index idx_tickets_date",
                      result.output)


# ---------------------------------------------------
# STARTUP AND READINESS
# ---------------------------------------------------

class TestStartupCheck(CliTestCase):

    def test_refuses_to_start_with_outdated_schema(self):
        _make_pre_sla_database(self.path, "hash")
        before = self.file_hash()

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(schema_ready_for_startup())

        self.assertIn("flask --app app init-db", stderr.getvalue())
        # The startup check never migrates
        self.assertEqual(self.file_hash(), before)

    def test_refuses_to_start_without_database(self):
        with redirect_stderr(io.StringIO()):
            self.assertFalse(schema_ready_for_startup())
        self.assertFalse(os.path.exists(self.path))

    def test_starts_with_current_schema(self):
        self.invoke("init-db")
        self.assertTrue(schema_ready_for_startup())


class TestReadinessResponse(CliTestCase):

    def setUp(self):
        super().setUp()
        self.old_testing = app.config["TESTING"]
        app.config["TESTING"] = True
        self.addCleanup(
            app.config.__setitem__, "TESTING", self.old_testing
        )
        self.client = app.test_client()

    def login(self, username="alice", role="user"):
        sign_in(self, self.client, username, role)

    def test_outdated_schema_returns_explained_503(self):
        _make_pre_sla_database(self.path, "hash")
        self.login()

        with self.assertLogs(app.logger, level="ERROR") as logs:
            response = self.client.get("/")

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"administrator must run the database migration",
                      response.data)
        # The public message reveals no paths or schema details
        self.assertNotIn(self.path.encode(), response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("flask --app app init-db", "\n".join(logs.output))

        # Web requests never migrate the database themselves
        self.assertEqual(self.version(), 0)

    def test_dashboard_works_after_init_db(self):
        _make_pre_sla_database(self.path, "hash")
        self.assertEqual(self.invoke("init-db").exit_code, 0)
        self.login()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"VPN down", response.data)

    def test_other_database_errors_are_not_hidden(self):
        # With a current schema, an unrelated database error must not be
        # disguised as a migration problem
        self.invoke("init-db")
        self.login()

        with patch(
            "app.db.count_tickets",
            side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.client.get("/")


# ---------------------------------------------------
# ACCOUNT SETUP REQUIRES A MIGRATED DATABASE
# ---------------------------------------------------

class TestAccountSetupRequiresMigration(CliTestCase):

    def test_setup_refuses_outdated_schema_and_changes_nothing(self):
        from setup_accounts import setup_account

        _make_pre_sla_database(self.path, "hash")
        before = self.file_hash()

        with patch("builtins.print"), \
                patch("setup_accounts.getpass") as getpass, \
                patch("builtins.input") as user_input:
            self.assertIs(setup_account(), False)

        getpass.assert_not_called()
        user_input.assert_not_called()
        self.assertEqual(self.file_hash(), before)

    def test_setup_refuses_missing_database(self):
        from setup_accounts import setup_account

        with patch("builtins.print"), patch("setup_accounts.getpass"):
            self.assertIs(setup_account(), False)

        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
