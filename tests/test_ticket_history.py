
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


class TestHistoryRecording(HistoryTestCase):

    def events(self, ticket_id):
        """History as (action, actor, field, old, new), newest first."""
        return [
            (e["action"], e["actor"], e["field_name"],
             e["old_value"], e["new_value"])
            for e in db.get_ticket_history(ticket_id)
        ]

    def ticket(self, ticket_id):
        return self.query(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        )[0]

    def new_ticket(self, creator="alice"):
        return db.create_ticket("Printer offline", "Jammed", "High", creator)

    # -----------------------------------------------------
    # EVENTS FOR EACH KIND OF CHANGE
    # -----------------------------------------------------

    def test_creating_a_ticket_records_who_created_it(self):
        ticket_id = self.new_ticket(creator="alice")

        self.assertEqual(
            self.events(ticket_id),
            [("created", "alice", None, None, None)]
        )

    def test_status_change_records_old_and_new_status(self):
        ticket_id = self.new_ticket()

        db.update_ticket(ticket_id, "In Progress", None, actor="tech1")

        self.assertEqual(
            self.events(ticket_id)[0],
            ("status_changed", "tech1", "status", "Open", "In Progress")
        )

    def test_first_assignment_is_recorded(self):
        ticket_id = self.new_ticket()

        db.update_ticket(ticket_id, "Open", "tech1", actor="admin")

        self.assertEqual(
            self.events(ticket_id)[0],
            ("assigned", "admin", "assigned_to", None, "tech1")
        )

    def test_reassignment_records_both_technicians(self):
        ticket_id = self.new_ticket()
        db.update_ticket(ticket_id, "Open", "tech1", actor="admin")

        db.update_ticket(ticket_id, "Open", "tech2", actor="admin")

        self.assertEqual(
            self.events(ticket_id)[0],
            ("reassigned", "admin", "assigned_to", "tech1", "tech2")
        )

    def test_unassignment_records_who_was_removed(self):
        ticket_id = self.new_ticket()
        db.update_ticket(ticket_id, "Open", "tech2", actor="admin")

        db.update_ticket(ticket_id, "Open", None, actor="admin")

        self.assertEqual(
            self.events(ticket_id)[0],
            ("unassigned", "admin", "assigned_to", "tech2", None)
        )
        self.assertIsNone(self.ticket(ticket_id)["assigned_to"])

    def test_adding_a_comment_is_recorded(self):
        ticket_id = self.new_ticket()

        db.add_comment(ticket_id, "alice", "Still broken")

        self.assertEqual(
            self.events(ticket_id)[0],
            ("commented", "alice", None, None, None)
        )

    def test_status_and_assignment_changed_together_are_separate_events(self):
        ticket_id = self.new_ticket()

        changes = db.update_ticket(
            ticket_id, "In Progress", "tech1", actor="tech1"
        )

        self.assertEqual(len(changes), 2)
        self.assertEqual(
            self.events(ticket_id)[:2],
            [
                ("assigned", "tech1", "assigned_to", None, "tech1"),
                ("status_changed", "tech1", "status", "Open", "In Progress"),
            ]
        )

    def test_event_records_the_acting_user_not_the_ticket_owner(self):
        ticket_id = self.new_ticket(creator="alice")

        db.update_ticket(ticket_id, "Resolved", None, actor="tech1")

        actors = [event[1] for event in self.events(ticket_id)]
        self.assertEqual(actors, ["tech1", "alice"])

    # -----------------------------------------------------
    # NO CHANGE, NO EVENT
    # -----------------------------------------------------

    def test_saving_without_changes_records_nothing(self):
        ticket_id = self.new_ticket()
        db.update_ticket(ticket_id, "Open", "tech1", actor="admin")
        before = self.ticket(ticket_id)

        changes = db.update_ticket(ticket_id, "Open", "tech1", actor="admin")

        self.assertEqual(changes, [])
        self.assertEqual(len(self.events(ticket_id)), 2)

        # The ticket is not touched either, so "last updated" stays honest
        self.assertEqual(self.ticket(ticket_id)["updated_at"],
                         before["updated_at"])

    def test_empty_assignee_is_the_same_as_unassigned(self):
        ticket_id = self.new_ticket()

        changes = db.update_ticket(ticket_id, "Open", "", actor="admin")

        self.assertEqual(changes, [])
        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_only_the_changed_field_is_recorded(self):
        ticket_id = self.new_ticket()
        db.update_ticket(ticket_id, "Open", "tech1", actor="admin")

        db.update_ticket(ticket_id, "Resolved", "tech1", actor="admin")

        newest = self.events(ticket_id)[0]
        self.assertEqual(newest[0], "status_changed")
        self.assertEqual(len(self.events(ticket_id)), 3)

    # -----------------------------------------------------
    # LINKING AND ORDERING
    # -----------------------------------------------------

    def test_history_belongs_only_to_its_own_ticket(self):
        first = self.new_ticket(creator="alice")
        second = self.new_ticket(creator="bob")

        db.add_comment(first, "alice", "Note on first ticket")
        db.update_ticket(second, "Closed", None, actor="tech1")

        self.assertEqual(
            [e[0] for e in self.events(first)], ["commented", "created"]
        )
        self.assertEqual(
            [e[0] for e in self.events(second)], ["status_changed", "created"]
        )

    def test_events_in_the_same_second_keep_a_fixed_order(self):
        ticket_id = self.new_ticket()

        # Several events are usually recorded within one second, so
        # the id tie-breaker decides their order
        for note in ("first", "second", "third"):
            db.add_comment(ticket_id, "alice", note)

        ids = [e["id"] for e in db.get_ticket_history(ticket_id)]

        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(ids, [e["id"] for e in db.get_ticket_history(ticket_id)])

    def test_missing_ticket_returns_none_and_records_nothing(self):
        self.assertIsNone(
            db.update_ticket(999, "Closed", None, actor="admin")
        )
        self.assertEqual(self.query("SELECT * FROM ticket_history"), [])

    # -----------------------------------------------------
    # TRANSACTIONS: ALL OR NOTHING
    # -----------------------------------------------------

    def block_history_inserts(self):
        """Make SQLite itself reject history inserts in this test DB."""
        conn = db.get_db()
        conn.execute(
            "CREATE TRIGGER test_fail_history BEFORE INSERT ON ticket_history "
            "BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END"
        )
        conn.commit()
        conn.close()

    def test_failed_audit_insert_rolls_back_the_status_change(self):
        ticket_id = self.new_ticket()

        with patch.object(
            db, "_record_event",
            side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                db.update_ticket(ticket_id, "Closed", "tech1", actor="admin")

        ticket = self.ticket(ticket_id)
        self.assertEqual(ticket["status"], "Open")
        self.assertIsNone(ticket["assigned_to"])
        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_real_sqlite_failure_rolls_back_the_status_change(self):
        ticket_id = self.new_ticket()
        self.block_history_inserts()

        with self.assertRaises(sqlite3.IntegrityError):
            db.update_ticket(ticket_id, "Closed", None, actor="admin")

        self.assertEqual(self.ticket(ticket_id)["status"], "Open")

    def test_failed_audit_insert_means_no_ticket_is_created(self):
        self.block_history_inserts()

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_ticket("Never saved", "Rolled back", "Low", "alice")

        self.assertEqual(self.query("SELECT * FROM tickets"), [])

    def test_failed_audit_insert_means_no_comment_is_saved(self):
        ticket_id = self.new_ticket()
        self.block_history_inserts()

        with self.assertRaises(sqlite3.IntegrityError):
            db.add_comment(ticket_id, "alice", "Never saved")

        self.assertEqual(self.query("SELECT * FROM comments"), [])

    def test_failed_ticket_update_records_no_event(self):
        ticket_id = self.new_ticket()

        conn = db.get_db()
        conn.execute(
            "CREATE TRIGGER test_fail_update BEFORE UPDATE ON tickets "
            "BEGIN SELECT RAISE(ABORT, 'simulated update failure'); END"
        )
        conn.commit()
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            db.update_ticket(ticket_id, "Closed", None, actor="admin")

        # Only the original "created" event: nothing claims a change
        self.assertEqual([e[0] for e in self.events(ticket_id)], ["created"])

    def test_connection_is_closed_after_a_failure(self):
        ticket_id = self.new_ticket()
        self.block_history_inserts()

        opened = []
        real_get_db = db.get_db

        def tracking_get_db():
            conn = real_get_db()
            opened.append(conn)
            return conn

        with patch.object(db, "get_db", tracking_get_db):
            with self.assertRaises(sqlite3.IntegrityError):
                db.update_ticket(ticket_id, "Closed", None, actor="admin")

        # Using a closed connection raises ProgrammingError
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    # -----------------------------------------------------
    # SQL SAFETY
    # -----------------------------------------------------

    def test_sql_in_history_lookup_is_treated_as_data(self):
        first = self.new_ticket(creator="alice")
        self.new_ticket(creator="bob")

        self.assertEqual(db.get_ticket_history(f"{first} OR 1=1"), [])
        self.assertEqual(len(self.query("SELECT * FROM ticket_history")), 2)

    def test_sql_in_values_is_stored_as_plain_text(self):
        ticket_id = self.new_ticket()
        attack = "x'); DELETE FROM tickets; --"

        db.update_ticket(ticket_id, "Open", attack, actor="admin")

        self.assertEqual(self.events(ticket_id)[0][4], attack)
        self.assertEqual(len(self.query("SELECT * FROM tickets")), 1)


if __name__ == "__main__":
    unittest.main()
