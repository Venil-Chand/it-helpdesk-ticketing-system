
"""
Upgrade tests using synthetic copies of every historical schema.

The fixtures reproduce the table definitions the helpdesk has actually
used, in order:

  original        tickets + ticket_comments(note), no accounts
  legacy_script   after the retired migrate.py: users, comments,
                  ticket_comments_migrated, tickets.created_by/updated_at
                  added with ALTER TABLE (so updated_at has no default)
  pre_sla         plus indexes and append-only ticket_history
  unversioned     plus ticket_sla, but user_version still 0

All data is synthetic and every database is a temporary file.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
from app import app, limiter
from migrations import IncompatibleSchemaError, SCHEMA_VERSION


# Table definitions exactly as created by earlier versions of the code
ORIGINAL_TICKETS_SQL = """
    CREATE TABLE tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        priority TEXT NOT NULL DEFAULT 'Medium',
        status TEXT NOT NULL DEFAULT 'Open',
        assigned_to TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""

ORIGINAL_COMMENTS_SQL = """
    CREATE TABLE ticket_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        author TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    )
"""

USERS_SQL = """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    UNIQUE NOT NULL,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL DEFAULT 'user'
    )
"""

COMMENTS_SQL = """
    CREATE TABLE comments (
        id         INTEGER   PRIMARY KEY AUTOINCREMENT,
        ticket_id  INTEGER   NOT NULL,
        author     TEXT      NOT NULL,
        content    TEXT      NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    )
"""

HISTORY_SQL = [
    """
    CREATE TABLE ticket_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id  INTEGER NOT NULL,
        actor      TEXT NOT NULL,
        action     TEXT NOT NULL CHECK (action IN (
                       'created', 'status_changed', 'assigned',
                       'reassigned', 'unassigned', 'commented'
                   )),
        field_name TEXT,
        old_value  TEXT,
        new_value  TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    )
    """,
    """
    CREATE INDEX idx_ticket_history_ticket
    ON ticket_history (ticket_id, created_at DESC, id DESC)
    """,
    """
    CREATE TRIGGER ticket_history_no_update
    BEFORE UPDATE ON ticket_history
    BEGIN
        SELECT RAISE(ABORT, 'ticket history is append-only');
    END
    """,
    """
    CREATE TRIGGER ticket_history_no_delete
    BEFORE DELETE ON ticket_history
    BEGIN
        SELECT RAISE(ABORT, 'ticket history is append-only');
    END
    """,
]

TICKET_INDEX_SQL = [
    "CREATE INDEX idx_tickets_owner_date "
    "ON tickets (created_by, created_at DESC, id DESC)",
    "CREATE INDEX idx_tickets_date ON tickets (created_at DESC, id DESC)",
]

SLA_SQL = """
    CREATE TABLE ticket_sla (
        ticket_id INTEGER PRIMARY KEY,
        response_due_at TEXT NOT NULL,
        first_response_at TEXT,
        policy_version TEXT NOT NULL DEFAULT 'calendar-hours-v1',
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    )
"""

# Synthetic passwords, used only to build hashes for the fixtures
PASSWORDS = {
    "carol": "synthetic-admin-passphrase-01",
    "dave": "synthetic-tech-passphrase-002",
    "erin": "synthetic-user-passphrase-003",
}
ROLES = {"carol": "admin", "dave": "technician", "erin": "user"}


def _execute_all(conn, statements):
    for sql in statements:
        conn.execute(sql)


def build_original(path):
    """The first schema: no accounts, comments stored as notes."""

    conn = sqlite3.connect(path)
    conn.execute(ORIGINAL_TICKETS_SQL)
    conn.execute(ORIGINAL_COMMENTS_SQL)
    conn.executemany(
        "INSERT INTO tickets (id, title, description, priority, status, "
        "assigned_to, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Printer offline", "Floor 2 printer", "High", "Open",
             None, "2026-10-01 09:00:00"),
            (2, "Email quota", "Mailbox full", "Low", "Resolved",
             "dave", "2026-10-02 10:30:00"),
        ]
    )
    conn.executemany(
        "INSERT INTO ticket_comments (id, ticket_id, author, note, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        [
            (5, 1, "dave", "Restarted spooler", "2026-10-01 09:15:00"),
            (7, 1, "erin", "Still offline", "2026-10-01 11:00:00"),
            (9, 2, "dave", "Quota raised", "2026-10-02 12:00:00"),
        ]
    )
    conn.commit()
    conn.close()


def build_legacy_script(path):
    """The original schema after the retired migrate.py had run on it."""

    build_original(path)

    conn = sqlite3.connect(path)
    conn.execute(USERS_SQL)
    conn.executemany(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        [
            (name, generate_password_hash(password), ROLES[name])
            for name, password in PASSWORDS.items()
        ]
    )
    conn.execute("ALTER TABLE tickets ADD COLUMN created_by TEXT")
    conn.execute("ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMP")
    conn.execute("UPDATE tickets SET updated_at = created_at")
    conn.execute("UPDATE tickets SET created_by = 'erin'")
    conn.execute(COMMENTS_SQL)
    conn.execute(
        "INSERT INTO comments (ticket_id, author, content, created_at) "
        "SELECT ticket_id, author, note, created_at FROM ticket_comments"
    )
    conn.execute(
        "ALTER TABLE ticket_comments RENAME TO ticket_comments_migrated"
    )
    conn.commit()
    conn.close()


def build_pre_sla(path):
    """Accounts, indexes and history, but no ticket_sla table."""

    build_legacy_script(path)

    conn = sqlite3.connect(path)
    _execute_all(conn, TICKET_INDEX_SQL + HISTORY_SQL)
    conn.executemany(
        "INSERT INTO ticket_history (ticket_id, actor, action, field_name, "
        "old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (2, "dave", "assigned", "assigned_to", None, "dave",
             "2026-10-02 10:45:00"),
            (2, "dave", "status_changed", "status", "Open", "Resolved",
             "2026-10-02 12:05:00"),
        ]
    )
    conn.commit()
    conn.close()


def build_unversioned_current(path):
    """Every current table, including SLA rows, but user_version 0."""

    build_pre_sla(path)

    conn = sqlite3.connect(path)
    conn.execute(SLA_SQL)
    conn.executemany(
        "INSERT INTO ticket_sla (ticket_id, response_due_at, "
        "first_response_at, policy_version) VALUES (?, ?, ?, ?)",
        [
            (1, "2026-10-01T13:00:00+00:00", None, "calendar-hours-v1"),
            (2, "2026-10-03T10:30:00+00:00", "2026-10-02T12:00:00+00:00",
             "calendar-hours-v1"),
        ]
    )
    conn.commit()
    conn.close()


def build_partial_sla_without_history(path):
    """An unusual mix: SLA table present but no history or indexes."""

    build_legacy_script(path)

    conn = sqlite3.connect(path)
    conn.execute(SLA_SQL)
    conn.execute(
        "INSERT INTO ticket_sla (ticket_id, response_due_at) "
        "VALUES (1, '2026-10-01T13:00:00+00:00')"
    )
    conn.commit()
    conn.close()


class UpgradeTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = temp_dir.name
        self.path = os.path.join(self.dir, "legacy.db")

    def rows(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def schema(self):
        return self.rows(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "ORDER BY type, name"
        )

    def tables(self):
        return {row[0] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )}

    def version(self):
        return self.rows("PRAGMA user_version")[0][0]

    def assert_fully_migrated(self):
        self.assertEqual(self.version(), SCHEMA_VERSION)

        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(migrations.find_schema_problems(conn), [])
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )
        finally:
            conn.close()


# ---------------------------------------------------
# UPGRADING EACH HISTORICAL SCHEMA
# ---------------------------------------------------

class TestUpgradeOriginalSchema(UpgradeTestCase):

    def setUp(self):
        super().setUp()
        build_original(self.path)
        self.tickets_before = self.rows(
            "SELECT id, title, description, priority, status, assigned_to, "
            "created_at FROM tickets ORDER BY id"
        )
        self.notes_before = self.rows(
            "SELECT id, ticket_id, author, note, created_at "
            "FROM ticket_comments ORDER BY id"
        )

    def test_status_reports_legacy_before_upgrade(self):
        self.assertEqual(migrations.schema_status(self.path).state, "legacy")

    def test_upgrade_preserves_tickets(self):
        migrations.migrate(self.path)

        self.assert_fully_migrated()
        self.assertEqual(
            self.rows(
                "SELECT id, title, description, priority, status, "
                "assigned_to, created_at FROM tickets ORDER BY id"
            ),
            self.tickets_before
        )
        # Unknown facts stay unknown rather than being made up
        self.assertEqual(
            self.rows("SELECT created_by, updated_at FROM tickets"),
            [(None, None), (None, None)]
        )

    def test_upgrade_moves_comments_with_ids_and_timestamps(self):
        migrations.migrate(self.path)

        self.assertEqual(
            self.rows(
                "SELECT id, ticket_id, author, content, created_at "
                "FROM comments ORDER BY id"
            ),
            self.notes_before
        )
        # The original table is kept, renamed, not dropped
        self.assertNotIn("ticket_comments", self.tables())
        self.assertEqual(
            self.rows(
                "SELECT id, ticket_id, author, note, created_at "
                "FROM ticket_comments_migrated ORDER BY id"
            ),
            self.notes_before
        )

    def test_upgrade_creates_no_accounts_history_or_sla(self):
        migrations.migrate(self.path)

        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(0,)])
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM ticket_history"), [(0,)]
        )
        self.assertEqual(self.rows("SELECT COUNT(*) FROM ticket_sla"), [(0,)])

    def test_upgrade_is_repeatable(self):
        migrations.migrate(self.path)
        schema = self.schema()

        result = migrations.migrate(self.path)

        self.assertEqual(result.applied, [])
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM comments"), [(3,)])

    def test_orphan_legacy_comment_rolls_back_everything(self):
        conn = sqlite3.connect(self.path)  # foreign keys off, like old code
        conn.execute(
            "INSERT INTO ticket_comments (ticket_id, author, note) "
            "VALUES (404, 'erin', 'orphan')"
        )
        conn.commit()
        conn.close()
        schema = self.schema()

        with self.assertRaises(sqlite3.IntegrityError):
            migrations.migrate(self.path)

        # Not even the tables created before the failing copy remain
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.version(), 0)
        self.assertEqual(self.tables(), {"tickets", "ticket_comments"})

    def test_ambiguous_comment_tables_are_rejected(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE ticket_comments_migrated (id INTEGER PRIMARY KEY)"
        )
        conn.commit()
        conn.close()
        schema = self.schema()

        with self.assertRaisesRegex(IncompatibleSchemaError, "twice"):
            migrations.migrate(self.path)

        self.assertEqual(self.schema(), schema)

    def test_existing_comments_are_never_merged_with_legacy_notes(self):
        conn = sqlite3.connect(self.path)
        conn.execute(COMMENTS_SQL)
        conn.execute(
            "INSERT INTO comments (ticket_id, author, content) "
            "VALUES (1, 'dave', 'new style comment')"
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(IncompatibleSchemaError, "merge"):
            migrations.migrate(self.path)

        self.assertEqual(self.rows("SELECT COUNT(*) FROM comments"), [(1,)])
        self.assertIn("ticket_comments", self.tables())


class TestUpgradeAfterLegacyScript(UpgradeTestCase):

    def setUp(self):
        super().setUp()
        build_legacy_script(self.path)
        self.users_before = self.rows(
            "SELECT id, username, password_hash, role FROM users ORDER BY id"
        )
        self.comments_before = self.rows("SELECT * FROM comments ORDER BY id")
        self.archived_before = self.rows(
            "SELECT * FROM ticket_comments_migrated ORDER BY id"
        )

    def test_accounts_and_password_hashes_are_unchanged(self):
        migrations.migrate(self.path)

        self.assert_fully_migrated()
        self.assertEqual(
            self.rows(
                "SELECT id, username, password_hash, role FROM users "
                "ORDER BY id"
            ),
            self.users_before
        )

    def test_comments_and_archived_notes_are_unchanged(self):
        migrations.migrate(self.path)

        self.assertEqual(
            self.rows("SELECT * FROM comments ORDER BY id"),
            self.comments_before
        )
        self.assertEqual(
            self.rows("SELECT * FROM ticket_comments_migrated ORDER BY id"),
            self.archived_before
        )


class TestUpgradeDatabaseMissingSla(UpgradeTestCase):
    """The layout of the production database found in the Day 28 audit."""

    def setUp(self):
        super().setUp()
        build_pre_sla(self.path)
        self.history_before = self.rows(
            "SELECT * FROM ticket_history ORDER BY id"
        )

    def test_adds_empty_sla_table_and_keeps_history(self):
        result = migrations.migrate(self.path)

        self.assert_fully_migrated()
        self.assertEqual(result.start_version, 0)
        self.assertIn("ticket_sla", self.tables())
        # Historical tickets get no invented deadline or outcome
        self.assertEqual(self.rows("SELECT COUNT(*) FROM ticket_sla"), [(0,)])
        self.assertEqual(
            self.rows("SELECT * FROM ticket_history ORDER BY id"),
            self.history_before
        )

    def test_history_triggers_still_protect_old_events(self):
        migrations.migrate(self.path)

        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("DELETE FROM ticket_history")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            conn.execute("UPDATE ticket_history SET actor = 'x'")


class TestUpgradeUnversionedCurrentSchema(UpgradeTestCase):

    def setUp(self):
        super().setUp()
        build_unversioned_current(self.path)

    def test_records_version_without_changing_schema_or_sla_rows(self):
        schema = self.schema()
        sla = self.rows("SELECT * FROM ticket_sla ORDER BY ticket_id")

        migrations.migrate(self.path)

        self.assert_fully_migrated()

        # Migrations 5, 6 and 7 change only users and add its triggers
        def without_users(objects):
            return [
                item for item in objects
                if item[1] not in (
                    "users", "users_revoke_sessions",
                    "users_username_immutable",
                    "users_no_historical_username_reuse",
                )
            ]

        self.assertEqual(without_users(self.schema()), without_users(schema))
        self.assertEqual(
            self.rows("SELECT * FROM ticket_sla ORDER BY ticket_id"), sla
        )


class TestUpgradePartialSchema(UpgradeTestCase):

    def test_sla_table_without_history_is_completed(self):
        build_partial_sla_without_history(self.path)
        sla = self.rows("SELECT * FROM ticket_sla")

        migrations.migrate(self.path)

        self.assert_fully_migrated()
        self.assertEqual(self.rows("SELECT * FROM ticket_sla"), sla)

    def test_tickets_table_missing_core_column_is_rejected(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT)"
        )
        conn.commit()
        conn.close()
        schema = self.schema()

        with self.assertRaisesRegex(IncompatibleSchemaError, "description"):
            migrations.migrate(self.path)

        self.assertEqual(self.schema(), schema)
        self.assertEqual(
            migrations.schema_status(self.path).state, "incompatible"
        )


# ---------------------------------------------------
# THE APPLICATION ON AN UPGRADED DATABASE
# ---------------------------------------------------

class TestApplicationOnUpgradedDatabase(UpgradeTestCase):

    def setUp(self):
        super().setUp()
        build_pre_sla(self.path)
        migrations.migrate(self.path)

        db_patch = patch.object(db, "DB_PATH", self.path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        saved = {
            key: app.config.get(key)
            for key in ("TESTING", "WTF_CSRF_ENABLED")
        }
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.addCleanup(app.config.update, saved)

        limiter.reset()
        self.client = app.test_client()

    def login(self, username):
        response = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORDS[username]}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))

    def test_existing_password_hashes_still_log_in(self):
        self.login("erin")

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Printer offline", response.data)

    def test_wrong_password_is_still_rejected(self):
        response = self.client.post(
            "/login", data={"username": "erin", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)

    def test_dashboard_counts_historical_tickets_as_not_tracked(self):
        self.login("dave")

        response = self.client.get("/?sla=not_tracked")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Printer offline", response.data)
        self.assertIn(b"Email quota", response.data)
        self.assertEqual(
            db.get_sla_stats()["Not Tracked"], 2
        )

    def test_historical_ticket_page_shows_comments_and_history(self):
        self.login("dave")

        response = self.client.get("/tickets/2")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Quota raised", response.data)
        self.assertNotIn(b'data-testid="response-sla"', response.data)
        self.assertEqual(len(db.get_ticket_history(2)), 2)

    def test_new_ticket_after_upgrade_gets_sla_and_history(self):
        self.login("erin")

        response = self.client.post(
            "/tickets/new",
            data={"title": "Laptop", "description": "No boot",
                  "priority": "Critical"}
        )
        self.assertEqual(response.status_code, 302)
        ticket_id = int(response.headers["Location"].rsplit("/", 1)[1])

        self.assertIsNotNone(db.get_ticket_sla_status(ticket_id))
        self.assertEqual(
            [event["action"] for event in db.get_ticket_history(ticket_id)],
            ["created"]
        )

        # tickets.updated_at was added with ALTER TABLE on these databases,
        # so it has no default and new tickets store NULL; the page must
        # still render
        self.assertIsNone(db.get_ticket(ticket_id)["updated_at"])
        page = self.client.get(f"/tickets/{ticket_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'data-testid="response-sla"', page.data)

    def test_staff_comment_records_first_response(self):
        ticket_id = db.create_ticket("Monitor", "Flicker", "High", "erin")
        self.login("dave")

        self.client.post(
            f"/tickets/{ticket_id}/comment", data={"content": "On my way"}
        )

        sla = db.get_ticket_sla_status(ticket_id)
        self.assertIsNotNone(sla["first_response_at"])

    def test_ownership_rules_still_apply(self):
        other = db.create_ticket("Private", "Someone else", "Low", "dave")
        self.login("erin")

        self.assertEqual(self.client.get(f"/tickets/{other}").status_code,
                         404)

    def test_csv_export_includes_historical_tickets(self):
        self.login("carol")

        response = self.client.get("/tickets/export")

        self.assertEqual(response.status_code, 200)
        text = response.data.decode("utf-8-sig")
        self.assertIn("Printer offline", text)
        self.assertIn("Email quota", text)


if __name__ == "__main__":
    unittest.main()
