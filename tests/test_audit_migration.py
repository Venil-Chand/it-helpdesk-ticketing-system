"""
Migration 8 (audit_events): upgrading keeps every existing row, the
table is append-only, and the schema check notices a weakened table.

Also checks that setup_accounts.py records its changes.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import database as db
import migrations
from migrations import MIGRATIONS, SCHEMA_VERSION
from setup_accounts import setup_account


class AuditMigrationTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = os.path.join(temp_dir.name, "audit.db")

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(statement, params).fetchall()

    def build_version_seven(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:7])
        self.sql("INSERT INTO users (username, password_hash, role) "
                 "VALUES ('admin', 'h1', 'admin'), ('venil', 'h2', "
                 "'technician'), ('staff1', 'h3', 'user')")
        self.sql("INSERT INTO tickets (title, description, priority, "
                 "status, assigned_to) VALUES ('Old', 'Kept', 'High', "
                 "'Resolved', 'Technician B')")
        self.sql("INSERT INTO comments (ticket_id, author, content) "
                 "VALUES (1, 'venil', 'Looked at it')")
        self.sql("INSERT INTO ticket_history (ticket_id, actor, action) "
                 "VALUES (1, 'venil', 'commented')")

    def snapshot(self):
        return {
            table: self.sql(f"SELECT * FROM {table} ORDER BY 1")
            for table in ("users", "tickets", "comments", "ticket_history",
                          "ticket_sla")
        }


class TestUpgradeToVersionEight(AuditMigrationTestCase):

    def test_upgrade_from_seven_keeps_every_row(self):
        self.build_version_seven()
        before = self.snapshot()

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [8, 9])
        self.assertEqual(result.final_version, SCHEMA_VERSION)

        # Every original column of every row is unchanged. Migration 9
        # only appends users.must_change_password, 0 for existing accounts.
        after = self.snapshot()
        for table, rows in before.items():
            with self.subTest(table=table):
                width = len(rows[0]) if rows else 0
                self.assertEqual([row[:width] for row in after[table]], rows)
        self.assertEqual(
            self.sql("SELECT DISTINCT must_change_password FROM users"),
            [(0,)]
        )
        # No invented events for changes made before the migration
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit_events"),
                         [(0,)])
        # The historical invalid assignee is preserved, not corrected
        self.assertEqual(self.sql("SELECT assigned_to FROM tickets"),
                         [("Technician B",)])

        status = migrations.schema_status(self.path)
        self.assertTrue(status.is_current)
        self.assertEqual(status.problems, [])
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.sql("PRAGMA integrity_check"), [("ok",)])

    def test_upgrade_is_repeatable(self):
        self.build_version_seven()
        migrations.migrate(self.path)

        result = migrations.migrate(self.path)

        self.assertEqual(result.applied, [])

    def test_version_seven_is_reported_outdated_before_upgrading(self):
        self.build_version_seven()

        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "outdated")
        self.assertEqual([m.version for m in status.pending], [8, 9])


class TestAuditEventsAreAppendOnly(AuditMigrationTestCase):

    def setUp(self):
        super().setUp()
        migrations.migrate(self.path)
        self.sql("INSERT INTO audit_events (actor, action) "
                 "VALUES ('root', 'account_created')")

    def test_update_and_delete_are_refused(self):
        for statement in ("UPDATE audit_events SET actor = 'someone'",
                          "DELETE FROM audit_events"):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "append-only"):
                    self.sql(statement)

        self.assertEqual(self.sql("SELECT actor FROM audit_events"),
                         [("root",)])

    def test_actor_and_action_are_required(self):
        for values in ("(NULL, 'x')", "('', 'x')", "('root', NULL)",
                       "('root', '')"):
            with self.subTest(values=values):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.sql("INSERT INTO audit_events (actor, action) "
                             f"VALUES {values}")

    def test_schema_check_detects_a_removed_protection(self):
        for trigger in ("audit_events_no_update", "audit_events_no_delete"):
            with self.subTest(trigger=trigger):
                self.sql(f"DROP TRIGGER {trigger}")

                problems = migrations.schema_status(self.path).problems

                self.assertIn(f"missing trigger {trigger}", problems)

                # Put it back (CREATE ... IF NOT EXISTS) for the next case
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    migrations._migration_8_audit_events(conn)

                self.assertEqual(
                    migrations.schema_status(self.path).problems, []
                )

    def test_schema_check_detects_a_missing_index(self):
        self.sql("DROP INDEX idx_audit_events_date")

        self.assertIn("missing index idx_audit_events_date",
                      migrations.schema_status(self.path).problems)


class TestSetupScriptIsAudited(AuditMigrationTestCase):

    def setUp(self):
        super().setUp()
        patcher = patch.object(db, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_first_admin_and_password_change_are_recorded(self):
        first = "first-owner-passphrase-123"
        second = "second-owner-passphrase-456"

        with patch("setup_accounts.getpass", side_effect=[first, first]):
            setup_account()

        with patch("setup_accounts.input", return_value="admin"), \
                patch("setup_accounts.getpass",
                      side_effect=[first, second, second]):
            setup_account()

        admin_id = self.sql("SELECT id FROM users WHERE username = 'admin'"
                            )[0][0]

        self.assertEqual(
            self.sql("SELECT actor_user_id, actor, action, target_user_id, "
                     "target_username, new_value FROM audit_events "
                     "ORDER BY id"),
            [
                (None, "setup_accounts.py", "account_created", admin_id,
                 "admin", "admin"),
                (None, "setup_accounts.py", "password_changed", admin_id,
                 "admin", None),
            ]
        )


if __name__ == "__main__":
    unittest.main()
