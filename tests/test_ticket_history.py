
"""
Database tests for the ticket activity history (audit trail).

Every test uses its own temporary SQLite database, so the real
helpdesk.db is never opened.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database as db


class HistoryTestCase(unittest.TestCase):
    """Shared setup: a fresh temporary database for each test."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        self.db_path = os.path.join(temp_dir.name, "history.db")

        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

    def query(self, sql, params=()):
        conn = db.get_db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def insert_ticket(self, title="Printer offline", created_by="alice"):
        """Insert a ticket directly, without recording any history."""
        conn = db.get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO tickets (title, description, created_by) "
                "VALUES (?, 'Test ticket', ?)",
                (title, created_by)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def insert_event(self, ticket_id, action="commented", actor="alice"):
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO ticket_history (ticket_id, actor, action) "
                "VALUES (?, ?, ?)",
                (ticket_id, actor, action)
            )
            conn.commit()
        finally:
            conn.close()


class TestHistorySchema(HistoryTestCase):

    def test_history_table_has_expected_columns(self):
        columns = {
            row["name"]
            for row in self.query("PRAGMA table_info(ticket_history)")
        }

        self.assertEqual(columns, {
            "id", "ticket_id", "actor", "action",
            "field_name", "old_value", "new_value", "created_at"
        })

    def test_repeated_initialisation_is_safe(self):
        ticket_id = self.insert_ticket()
        self.insert_event(ticket_id)

        # Running the migration again must not drop or duplicate anything
        db.init_db()
        db.init_db()

        self.assertEqual(len(self.query("SELECT * FROM tickets")), 1)
        self.assertEqual(len(self.query("SELECT * FROM ticket_history")), 1)

    def test_migration_adds_history_to_an_existing_database(self):
        # Build a database in the layout used before this feature
        old_path = os.path.join(os.path.dirname(self.db_path), "old.db")
        conn = sqlite3.connect(old_path)
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, description TEXT NOT NULL, "
            "priority TEXT NOT NULL DEFAULT 'Medium', "
            "status TEXT NOT NULL DEFAULT 'Open', assigned_to TEXT, "
            "created_by TEXT, created_at TIMESTAMP DEFAULT "
            "CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT "
            "CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO tickets (title, description, created_by) "
            "VALUES ('Existing ticket', 'Before the audit feature', 'bob')"
        )
        conn.commit()
        conn.close()

        with patch.object(db, "DB_PATH", old_path):
            db.init_db()

            tickets = self.query("SELECT title FROM tickets")
            history = self.query("SELECT * FROM ticket_history")

        # The existing ticket survives untouched
        self.assertEqual([t["title"] for t in tickets], ["Existing ticket"])

        # No history is invented for activity that happened before
        self.assertEqual(history, [])

    # -----------------------------------------------------
    # INTEGRITY RULES ENFORCED BY THE DATABASE
    # -----------------------------------------------------

    def test_history_must_belong_to_an_existing_ticket(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_event(ticket_id=999)

    def test_unknown_actions_are_rejected(self):
        ticket_id = self.insert_ticket()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_event(ticket_id, action="deleted_everything")

    def test_history_rows_cannot_be_edited(self):
        ticket_id = self.insert_ticket()
        self.insert_event(ticket_id, actor="alice")

        conn = db.get_db()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE ticket_history SET actor = 'mallory'")
        finally:
            conn.close()

        self.assertEqual(
            self.query("SELECT actor FROM ticket_history")[0]["actor"],
            "alice"
        )

    def test_history_rows_cannot_be_deleted(self):
        ticket_id = self.insert_ticket()
        self.insert_event(ticket_id)

        conn = db.get_db()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ticket_history")
        finally:
            conn.close()

        self.assertEqual(len(self.query("SELECT * FROM ticket_history")), 1)

    def test_ticket_history_query_uses_the_index(self):
        plan = self.query(
            "EXPLAIN QUERY PLAN SELECT * FROM ticket_history "
            "WHERE ticket_id = ? ORDER BY created_at DESC, id DESC",
            (1,)
        )
        plan_text = " ".join(row[3] for row in plan)

        self.assertIn("idx_ticket_history_ticket", plan_text)
        self.assertNotIn("TEMP B-TREE", plan_text)


if __name__ == "__main__":
    unittest.main()
