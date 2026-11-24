
"""
Tests for the versioned schema migration framework in migrations.py.

Every test uses a fresh temporary database file. The real helpdesk.db is
never opened.
"""

import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import database as db
import migrations
from migrations import (
    IncompatibleSchemaError,
    MIGRATIONS,
    Migration,
    MigrationError,
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
)


EXPECTED_TABLES = {"users", "tickets", "comments", "ticket_history",
                   "ticket_sla"}


class TempDatabase:
    """Give each test its own empty directory for database files."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = temp_dir.name
        self.path = os.path.join(self.dir, "test.db")

    def connect(self, path=None):
        conn = sqlite3.connect(path or self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(conn.close)
        return conn

    def version(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def tables(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            return {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            conn.close()

    def schema_snapshot(self, path=None):
        conn = sqlite3.connect(path or self.path)
        try:
            return conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        finally:
            conn.close()

    def file_hash(self, path=None):
        with open(path or self.path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------
# REGISTRY AND ORDERING
# ---------------------------------------------------

class TestMigrationRegistry(unittest.TestCase):

    def test_versions_are_contiguous_and_ordered(self):
        versions = [m.version for m in MIGRATIONS]
        self.assertEqual(versions, list(range(1, len(MIGRATIONS) + 1)))
        self.assertEqual(SCHEMA_VERSION, versions[-1])

    def test_registry_with_gap_is_rejected(self):
        broken = (MIGRATIONS[0], MIGRATIONS[2])
        with self.assertRaises(MigrationError):
            migrations._check_registry(broken)

    def test_registry_out_of_order_is_rejected(self):
        broken = (MIGRATIONS[1], MIGRATIONS[0])
        with self.assertRaises(MigrationError):
            migrations._check_registry(broken)


# ---------------------------------------------------
# FRESH DATABASE
# ---------------------------------------------------

class TestFreshDatabase(TempDatabase, unittest.TestCase):

    def test_creates_every_table_and_records_latest_version(self):
        result = migrations.migrate(self.path)

        self.assertEqual(result.start_version, 0)
        self.assertEqual(result.final_version, SCHEMA_VERSION)
        self.assertEqual(
            [m.version for m in result.applied],
            [m.version for m in MIGRATIONS]
        )
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertTrue(EXPECTED_TABLES <= self.tables())

        conn = self.connect()
        self.assertEqual(migrations.find_schema_problems(conn), [])

    def test_never_creates_user_accounts(self):
        migrations.migrate(self.path)

        count = self.connect().execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_init_db_uses_the_migration_framework(self):
        with patch.object(db, "DB_PATH", self.path):
            result = db.init_db()

        self.assertEqual(result.final_version, SCHEMA_VERSION)
        self.assertEqual(self.version(), SCHEMA_VERSION)


# ---------------------------------------------------
# REPEATABLE (IDEMPOTENT) MIGRATIONS
# ---------------------------------------------------

class TestRepeatableMigrations(TempDatabase, unittest.TestCase):

    def test_second_run_applies_nothing_and_keeps_data(self):
        migrations.migrate(self.path)

        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO tickets (title, description, created_by) "
                "VALUES ('Printer', 'Jammed', 'alice')"
            )
        before = self.schema_snapshot()

        result = migrations.migrate(self.path)

        self.assertEqual(result.applied, [])
        self.assertEqual(result.start_version, SCHEMA_VERSION)
        self.assertEqual(self.schema_snapshot(), before)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1
        )

    def test_upgrade_from_intermediate_version_applies_only_the_rest(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:2])
        self.assertEqual(self.version(), 2)
        self.assertNotIn("ticket_history", self.tables())

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [3, 4, 5, 6, 7])
        self.assertEqual(self.version(), SCHEMA_VERSION)


# ---------------------------------------------------
# FAILED MIGRATIONS ROLL BACK COMPLETELY
# ---------------------------------------------------

def _failing_migration(version):
    """A migration that changes the schema, then fails part-way."""

    def apply(conn):
        conn.execute("CREATE TABLE half_done (id INTEGER PRIMARY KEY)")
        conn.execute("ALTER TABLE tickets ADD COLUMN half_column TEXT")
        raise RuntimeError("simulated failure in the middle of a migration")

    return Migration(version, "Simulated failure", apply)


class TestFailedMigrationRollback(TempDatabase, unittest.TestCase):

    def test_failure_on_fresh_database_leaves_it_empty(self):
        registry = MIGRATIONS[:2] + (_failing_migration(3),)

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        # Versions 1 and 2 ran before the failure, but were not kept
        self.assertEqual(self.version(), 0)
        self.assertEqual(self.tables(), set())

    def test_failure_after_upgrade_keeps_previous_version_and_data(self):
        migrations.migrate(self.path)

        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO tickets (title, description) VALUES ('A', 'B')"
            )
        conn.close()
        before = self.schema_snapshot()

        registry = MIGRATIONS + (_failing_migration(SCHEMA_VERSION + 1),)

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(self.schema_snapshot(), before)
        self.assertNotIn("half_done", self.tables())

        conn = self.connect()
        self.assertEqual(
            conn.execute("SELECT title FROM tickets").fetchone()[0], "A"
        )

    def test_version_only_advances_with_its_migration(self):
        # The version must be written in the same transaction as the
        # schema change, so after a failure neither is visible
        registry = MIGRATIONS[:1] + (_failing_migration(2),)

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.version(), 0)
        self.assertEqual(
            migrations.schema_status(self.path).state, "uninitialized"
        )


# ---------------------------------------------------
# UNSUPPORTED AND INCOMPATIBLE DATABASES
# ---------------------------------------------------

class TestUnsupportedDatabases(TempDatabase, unittest.TestCase):

    def test_future_schema_version_is_rejected_unchanged(self):
        migrations.migrate(self.path)
        conn = self.connect()
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        before = self.file_hash()

        with self.assertRaises(UnsupportedSchemaVersionError):
            migrations.migrate(self.path)

        self.assertEqual(self.file_hash(), before)
        self.assertEqual(
            migrations.schema_status(self.path).state, "too_new"
        )

    def test_non_helpdesk_database_is_not_modified(self):
        conn = self.connect()
        conn.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with self.assertRaises(IncompatibleSchemaError):
            migrations.migrate(self.path)

        self.assertEqual(self.tables(), {"invoices"})
        self.assertEqual(self.version(), 0)
        self.assertEqual(
            migrations.schema_status(self.path).state, "incompatible"
        )

    def test_current_version_with_missing_trigger_is_rejected(self):
        migrations.migrate(self.path)
        conn = self.connect()
        conn.execute("DROP TRIGGER ticket_history_no_delete")
        conn.commit()
        conn.close()

        with self.assertRaises(IncompatibleSchemaError) as caught:
            migrations.migrate(self.path)

        self.assertIn("ticket_history_no_delete", str(caught.exception))

        status = migrations.schema_status(self.path)
        self.assertEqual(status.state, "incompatible")
        self.assertIn(
            "missing trigger ticket_history_no_delete", status.problems
        )

    def test_index_with_unexpected_columns_is_rejected(self):
        # An old database where a same-named index covers other columns.
        # CREATE INDEX IF NOT EXISTS would silently keep it.
        conn = self.connect()
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, description TEXT NOT NULL, "
            "priority TEXT NOT NULL DEFAULT 'Medium', "
            "status TEXT NOT NULL DEFAULT 'Open', assigned_to TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("CREATE INDEX idx_tickets_date ON tickets (title)")
        conn.commit()
        conn.close()
        before = self.schema_snapshot()

        with self.assertRaises(IncompatibleSchemaError) as caught:
            migrations.migrate(self.path)

        self.assertIn("idx_tickets_date", str(caught.exception))
        self.assertEqual(self.schema_snapshot(), before)
        self.assertEqual(self.version(), 0)

    def test_corrupted_trigger_behaviour_is_rejected(self):
        migrations.migrate(self.path)
        conn = self.connect()
        conn.execute("DROP TRIGGER ticket_history_no_update")
        # Same name, but it no longer blocks anything
        conn.execute(
            "CREATE TRIGGER ticket_history_no_update "
            "AFTER INSERT ON ticket_history BEGIN SELECT 1; END"
        )
        conn.commit()
        conn.close()

        status = migrations.schema_status(self.path)
        self.assertEqual(status.state, "incompatible")
        self.assertIn(
            "trigger ticket_history_no_update has unexpected behaviour",
            status.problems
        )


# ---------------------------------------------------
# CONSTRAINTS, FOREIGN KEYS AND TRIGGERS
# ---------------------------------------------------

class TestConstraintsAfterMigration(TempDatabase, unittest.TestCase):

    def setUp(self):
        super().setUp()
        migrations.migrate(self.path)
        self.conn = self.connect()

    def test_foreign_keys_reject_orphan_rows(self):
        for sql in (
            "INSERT INTO comments (ticket_id, author, content) "
            "VALUES (999, 'a', 'b')",
            "INSERT INTO ticket_history (ticket_id, actor, action) "
            "VALUES (999, 'a', 'created')",
            "INSERT INTO ticket_sla (ticket_id, response_due_at) "
            "VALUES (999, '2026-01-01T00:00:00+00:00')",
        ):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql)

    def test_history_triggers_block_update_and_delete(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tickets (title, description) VALUES ('A', 'B')"
            )
            self.conn.execute(
                "INSERT INTO ticket_history (ticket_id, actor, action) "
                "VALUES (1, 'alice', 'created')"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute("UPDATE ticket_history SET actor = 'mallory'")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute("DELETE FROM ticket_history")

    def test_history_action_check_constraint(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO tickets (title, description) VALUES ('A', 'B')"
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ticket_history (ticket_id, actor, action) "
                "VALUES (1, 'alice', 'deleted')"
            )

    def test_username_is_unique(self):
        insert = (
            "INSERT INTO users (username, password_hash) VALUES ('a', 'h')"
        )
        self.conn.execute(insert)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(insert)

    def test_existing_orphans_stop_the_migration(self):
        # Foreign keys were not enforced by older code, so an old database
        # may already contain orphans. The migration must refuse, not hide
        # them by marking the database as current.
        legacy = os.path.join(self.dir, "orphans.db")
        conn = sqlite3.connect(legacy)  # foreign keys off, like old code
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, description TEXT NOT NULL, "
            "priority TEXT NOT NULL DEFAULT 'Medium', "
            "status TEXT NOT NULL DEFAULT 'Open', assigned_to TEXT, "
            "created_by TEXT, created_at TIMESTAMP DEFAULT "
            "CURRENT_TIMESTAMP, updated_at TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE comments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticket_id INTEGER NOT NULL, author TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at TIMESTAMP DEFAULT "
            "CURRENT_TIMESTAMP, FOREIGN KEY (ticket_id) "
            "REFERENCES tickets(id))"
        )
        conn.execute(
            "INSERT INTO comments (ticket_id, author, content) "
            "VALUES (42, 'bob', 'secret note text')"
        )
        conn.commit()
        conn.close()
        before = self.schema_snapshot(legacy)

        with self.assertRaises(IncompatibleSchemaError) as caught:
            migrations.migrate(legacy)

        message = str(caught.exception)
        self.assertIn("comments: 1", message)
        # Only counts are reported, never the stored data
        self.assertNotIn("secret note text", message)
        self.assertEqual(self.version(legacy), 0)
        self.assertEqual(self.schema_snapshot(legacy), before)


# ---------------------------------------------------
# CONCURRENT MIGRATION ATTEMPTS
# ---------------------------------------------------

class TestConcurrentMigration(TempDatabase, unittest.TestCase):

    def test_simultaneous_migrations_apply_each_version_once(self):
        workers = 6
        barrier = threading.Barrier(workers)
        results = []
        errors = []
        lock = threading.Lock()

        def run():
            try:
                barrier.wait()
                result = migrations.migrate(self.path)
                with lock:
                    results.append(result)
            except Exception as error:  # pragma: no cover - reported below
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), workers)

        # Exactly one attempt did the work; the rest found it done
        applied = sorted(len(r.applied) for r in results)
        self.assertEqual(applied, [0] * (workers - 1) + [len(MIGRATIONS)])
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(
            migrations.find_schema_problems(self.connect()), []
        )

    def test_migration_waits_for_lock_then_fails_cleanly(self):
        # Another process is in the middle of writing
        holder = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")

        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            migrations.migrate(self.path, timeout=0.2)

        holder.execute("ROLLBACK")

        self.assertEqual(self.version(), 0)
        self.assertEqual(self.tables(), set())

        # Once the lock is released the migration succeeds
        migrations.migrate(self.path)
        self.assertEqual(self.version(), SCHEMA_VERSION)


# ---------------------------------------------------
# READ-ONLY STATUS
# ---------------------------------------------------

class TestSchemaStatus(TempDatabase, unittest.TestCase):

    def test_missing_database_is_reported_and_not_created(self):
        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "missing")
        self.assertEqual(len(status.pending), len(MIGRATIONS))
        self.assertFalse(os.path.exists(self.path))

    def test_empty_database_is_uninitialized(self):
        sqlite3.connect(self.path).close()
        self.assertEqual(
            migrations.schema_status(self.path).state, "uninitialized"
        )

    def test_intermediate_version_is_outdated(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:3])
        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "outdated")
        self.assertEqual(status.version, 3)
        self.assertEqual([m.version for m in status.pending], [4, 5, 6, 7])

    def test_current_database(self):
        migrations.migrate(self.path)
        status = migrations.schema_status(self.path)

        self.assertTrue(status.is_current)
        self.assertEqual(status.pending, [])
        self.assertEqual(status.problems, [])

    def test_status_never_writes(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:1])
        before = self.file_hash()

        migrations.schema_status(self.path)

        self.assertEqual(self.file_hash(), before)
        self.assertEqual(self.version(), 1)


# ---------------------------------------------------
# VERIFIED BACKUPS
# ---------------------------------------------------

class TestBackup(TempDatabase, unittest.TestCase):

    def test_backup_is_a_verified_copy(self):
        migrations.migrate(self.path)
        conn = self.connect()
        with conn:
            conn.execute(
                "INSERT INTO tickets (title, description) VALUES ('A', 'B')"
            )
        conn.close()

        backup = os.path.join(self.dir, "backup.db")
        migrations.backup_database(self.path, backup)

        self.assertEqual(self.version(backup), SCHEMA_VERSION)
        copy = self.connect(backup)
        self.assertEqual(
            copy.execute("SELECT title FROM tickets").fetchone()[0], "A"
        )

    def test_backup_never_overwrites(self):
        migrations.migrate(self.path)
        backup = os.path.join(self.dir, "backup.db")
        with open(backup, "w") as handle:
            handle.write("keep me")

        with self.assertRaises(MigrationError):
            migrations.backup_database(self.path, backup)

        with open(backup) as handle:
            self.assertEqual(handle.read(), "keep me")

    def test_backup_of_missing_database_fails(self):
        with self.assertRaises(MigrationError):
            migrations.backup_database(
                self.path, os.path.join(self.dir, "backup.db")
            )
        self.assertFalse(os.path.exists(self.path))


# ---------------------------------------------------
# RETIRED INSECURE MIGRATION SCRIPT
# ---------------------------------------------------

class TestLegacySeedingRetired(unittest.TestCase):

    PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_legacy_migrate_script_is_gone(self):
        self.assertFalse(
            os.path.exists(os.path.join(self.PROJECT_DIR, "migrate.py"))
        )

    def test_no_application_code_contains_default_passwords(self):
        # The passwords the retired script seeded accounts with
        retired_defaults = ("admin123", "venil123", "staff123")

        for name in sorted(os.listdir(self.PROJECT_DIR)):
            if not name.endswith(".py"):
                continue

            with open(os.path.join(self.PROJECT_DIR, name)) as handle:
                source = handle.read()

            for password in retired_defaults:
                with self.subTest(file=name, password=password):
                    self.assertNotIn(password, source)


if __name__ == "__main__":
    unittest.main()
