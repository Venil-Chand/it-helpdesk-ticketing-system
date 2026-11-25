"""
Migration 10: users.email, notifications and notification_attempts.

An existing version 9 database keeps every row exactly as it was
(including an invalid historical assignee), gains the new tables empty,
and gives nobody an email address. A failure rolls back to version 9.
Temporary databases only.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

import migrations
from migrations import MIGRATIONS, SCHEMA_VERSION


class TestMigrationTen(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "v9.db")

        migrations.migrate(self.path, migrations=MIGRATIONS[:9])

        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO users (username, password_hash, role) "
                         "VALUES ('admin', 'hash-a', 'admin')")
            conn.execute("INSERT INTO users (username, password_hash, role, "
                         "is_active) VALUES ('old', 'hash-o', 'user', 0)")
            conn.execute("INSERT INTO tickets (title, description, priority, "
                         "status, assigned_to, created_by) VALUES "
                         "('Printer', 'Jam', 'Low', 'Open', 'Technician B', "
                         "'old')")
            conn.execute("INSERT INTO comments (ticket_id, author, content) "
                         "VALUES (1, 'admin', 'Looking')")
            conn.execute("INSERT INTO ticket_history (ticket_id, actor, "
                         "action) VALUES (1, 'old', 'created')")
            conn.execute("INSERT INTO audit_events (actor, action) VALUES "
                         "('admin', 'login_succeeded')")

    def sql(self, statement):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(statement).fetchall()

    def snapshot(self):
        return {table: self.sql(f"SELECT * FROM {table} ORDER BY 1")
                for table in ("tickets", "comments", "ticket_history",
                              "audit_events", "ticket_sla")}

    def test_version_ten_is_latest(self):
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(MIGRATIONS[-1].version, 10)

    def test_status_before_upgrading(self):
        status = migrations.schema_status(self.path)
        self.assertEqual((status.state, status.version), ("outdated", 9))
        self.assertEqual([m.version for m in status.pending], [10])

    def test_upgrade_keeps_every_row_and_adds_empty_tables(self):
        before = self.snapshot()
        users = self.sql("SELECT * FROM users ORDER BY id")

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [10])
        self.assertEqual(self.snapshot(), before)
        # Every account gets a NULL address: nothing is guessed
        self.assertEqual(self.sql("SELECT * FROM users ORDER BY id"),
                         [row + (None,) for row in users])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM notifications"),
                         [(0,)])
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM notification_attempts"), [(0,)]
        )
        self.assertTrue(migrations.schema_status(self.path).is_current)

        # The invalid historical assignee is untouched
        self.assertEqual(self.sql("SELECT assigned_to FROM tickets"),
                         [("Technician B",)])

    def test_upgrade_is_repeatable(self):
        migrations.migrate(self.path)
        again = migrations.migrate(self.path)
        self.assertEqual(again.applied, [])

    def test_failure_rolls_back_to_version_nine(self):
        def fail_half_way(conn):
            MIGRATIONS[9].apply(conn)
            raise RuntimeError("simulated failure")

        registry = MIGRATIONS[:9] + (
            migrations.Migration(10, "fails", fail_half_way),
        )
        schema = self.sql("SELECT sql FROM sqlite_master ORDER BY name")

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.sql("PRAGMA user_version"), [(9,)])
        self.assertEqual(
            self.sql("SELECT sql FROM sqlite_master ORDER BY name"), schema
        )

    def test_backup_before_upgrade_matches_version_nine(self):
        backup = self.path + ".backup"
        migrations.backup_database(self.path, backup)
        migrations.migrate(self.path)

        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (9,))


class TestMigrationTenValidation(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "t.db")
        migrations.migrate(self.path)

    def damage(self, *statements):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for statement in statements:
                conn.execute(statement)
        return migrations.schema_status(self.path).problems

    def test_fresh_database_is_current(self):
        self.assertEqual(migrations.schema_status(self.path).problems, [])

    def test_missing_unique_dedupe_key_is_detected(self):
        problems = self.damage(
            "DROP TABLE notification_attempts",
            "DROP TABLE notifications",
            """CREATE TABLE notifications (id INTEGER PRIMARY KEY
               AUTOINCREMENT, event_type TEXT NOT NULL CHECK (event_type IN
               ('x')), dedupe_key TEXT NOT NULL, recipient_user_id INTEGER
               NOT NULL, recipient_username TEXT NOT NULL, recipient_reason
               TEXT NOT NULL CHECK (recipient_reason IN ('x')), ticket_id
               INTEGER REFERENCES tickets(id), payload TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('x')), attempts
               INTEGER NOT NULL CHECK (attempts >= 0), deferrals INTEGER NOT
               NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
               next_attempt_at TEXT NOT NULL, claim_token TEXT, claimed_by
               TEXT, claimed_at TEXT, claimed_until TEXT, sent_at TEXT,
               delivery_mode TEXT, last_error TEXT)""",
        )
        self.assertIn("notifications.dedupe_key must be UNIQUE", problems)

    def test_missing_append_only_trigger_is_detected(self):
        problems = self.damage(
            "DROP TRIGGER notification_attempts_no_delete"
        )
        self.assertIn("missing trigger notification_attempts_no_delete",
                      problems)

    def test_missing_index_is_detected(self):
        problems = self.damage("DROP INDEX idx_notifications_due")
        self.assertIn("missing index idx_notifications_due", problems)

    def test_check_constraints_are_enforced(self):
        base = ("INSERT INTO notifications (event_type, dedupe_key, "
                "recipient_user_id, recipient_username, recipient_reason, "
                "status, attempts, created_at, updated_at, next_attempt_at) "
                "VALUES (?, ?, 1, 'u', ?, ?, ?, 'a', 'a', 'a')")
        bad = [
            ("unknown_event", "k1", "staff", "pending", 0),
            ("ticket_created", "k2", "stranger", "pending", 0),
            ("ticket_created", "k3", "staff", "lost", 0),
            ("ticket_created", "k4", "staff", "pending", -1),
        ]
        with closing(sqlite3.connect(self.path)) as conn:
            for values in bad:
                with self.subTest(values=values):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(base, values)

    def test_notifications_reference_real_tickets(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO notifications (event_type, dedupe_key, "
                    "recipient_user_id, recipient_username, "
                    "recipient_reason, ticket_id, created_at, updated_at, "
                    "next_attempt_at) VALUES ('ticket_created', 'k', 1, "
                    "'u', 'staff', 999, 'a', 'a', 'a')"
                )


if __name__ == "__main__":
    unittest.main()
