
"""
Integration tests for response SLA tracking.

Covers the SLA deadline written when a ticket is created, the first
qualifying technician response recorded when a comment is added, the
transactions that keep them consistent with tickets, comments and
history, and how the ticket page shows the SLA.

Every test uses a temporary SQLite database. The real helpdesk.db is
never opened.
"""

import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
from app import app
from sla import SLA_TARGETS, is_qualifying_response
from sla_storage import parse_stored_utc


USERS = [
    ("admin", "admin"),
    ("tech1", "technician"),
    ("tech2", "technician"),
    ("alice", "user"),
    ("bob", "user"),
]


class TempDatabaseMixin:
    """Point database.DB_PATH at a fresh temporary file with users."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        self.db_path = os.path.join(temp_dir.name, "sla.db")

        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

        # Sessions are set directly, so these hashes are never checked
        with self.connect() as conn:
            for username, role in USERS:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) "
                    "VALUES (?, ?, ?)",
                    (username, generate_password_hash("unused"), role)
                )

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(conn.close)
        return conn

    def query(self, sql, params=()):
        return [dict(r) for r in self.connect().execute(sql, params)]

    def sla_row(self, ticket_id):
        rows = self.query(
            "SELECT * FROM ticket_sla WHERE ticket_id = ?", (ticket_id,)
        )
        return rows[0] if rows else None

    def insert_historical_ticket(self, created_by="alice"):
        """A ticket created before SLA tracking: no SLA row."""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tickets (title, description, priority, "
                "created_by) VALUES ('Old', 'Old ticket', 'High', ?)",
                (created_by,)
            )
            return cursor.lastrowid

    def add_trigger(self, sql):
        with self.connect() as conn:
            conn.execute(sql)


# ---------------------------------------------------
# QUALIFYING RESPONSE RULE
# ---------------------------------------------------

class TestQualifyingResponseRule(unittest.TestCase):

    def test_technician_on_someone_elses_ticket_qualifies(self):
        self.assertTrue(is_qualifying_response("tech1", "technician", "alice"))

    def test_admin_on_someone_elses_ticket_qualifies(self):
        self.assertTrue(is_qualifying_response("admin", "admin", "alice"))

    def test_requester_does_not_qualify(self):
        self.assertFalse(is_qualifying_response("alice", "user", "alice"))

    def test_regular_user_on_other_ticket_does_not_qualify(self):
        self.assertFalse(is_qualifying_response("bob", "user", "alice"))

    def test_staff_on_own_ticket_does_not_qualify(self):
        self.assertFalse(
            is_qualifying_response("tech1", "technician", "tech1")
        )

    def test_unknown_role_does_not_qualify(self):
        self.assertFalse(is_qualifying_response("ghost", None, "alice"))


# ---------------------------------------------------
# STORED TIMESTAMP PARSING
# ---------------------------------------------------

class TestParseStoredUtc(unittest.TestCase):

    def test_sqlite_current_timestamp_is_utc(self):
        self.assertEqual(
            parse_stored_utc("2026-11-17 08:00:00"),
            datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
        )

    def test_offset_is_converted_to_utc(self):
        self.assertEqual(
            parse_stored_utc("2026-11-17T21:00:00+13:00"),
            datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
        )

    def test_malformed_text_rejected(self):
        with self.assertRaises(ValueError):
            parse_stored_utc("not a date")

    def test_non_text_rejected(self):
        with self.assertRaises(TypeError):
            parse_stored_utc(None)


# ---------------------------------------------------
# SCHEMA
# ---------------------------------------------------

class TestSLASchema(TempDatabaseMixin, unittest.TestCase):

    def test_init_db_creates_sla_table(self):
        tables = [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )]
        self.assertIn("ticket_sla", tables)

    def test_init_db_is_repeatable_and_keeps_sla_rows(self):
        ticket_id = db.create_ticket("Printer", "Jam", "High", "alice")
        before = self.sla_row(ticket_id)

        db.init_db()
        db.init_db()

        self.assertEqual(self.sla_row(ticket_id), before)

    def test_init_db_upgrades_database_without_sla_table(self):
        with self.connect() as conn:
            conn.execute("DROP TABLE ticket_sla")

        old_ticket = self.insert_historical_ticket()

        db.init_db()

        self.assertIsNone(self.sla_row(old_ticket))
        self.assertEqual(len(self.query("SELECT * FROM tickets")), 1)

        # New tickets get an SLA after the upgrade
        new_ticket = db.create_ticket("New", "Ticket", "Low", "alice")
        self.assertIsNotNone(self.sla_row(new_ticket))


# ---------------------------------------------------
# DEADLINE ON TICKET CREATION
# ---------------------------------------------------

class TestDeadlineOnCreation(TempDatabaseMixin, unittest.TestCase):

    def test_deadline_matches_stored_created_at_for_each_priority(self):
        for priority, hours in SLA_TARGETS.items():
            with self.subTest(priority=priority):
                ticket_id = db.create_ticket(
                    "Issue", "Details", priority, "alice"
                )
                ticket = db.get_ticket(ticket_id)
                sla = self.sla_row(ticket_id)

                expected = (
                    parse_stored_utc(ticket["created_at"])
                    + timedelta(hours=hours)
                )

                self.assertEqual(
                    datetime.fromisoformat(sla["response_due_at"]),
                    expected
                )
                self.assertIsNone(sla["first_response_at"])
                self.assertEqual(
                    sla["policy_version"], "calendar-hours-v1"
                )

    def test_deadline_is_stored_as_utc_iso_text(self):
        ticket_id = db.create_ticket("Issue", "Details", "High", "alice")

        due = self.sla_row(ticket_id)["response_due_at"]

        self.assertRegex(
            due, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$"
        )

    def test_ticket_created_at_format_is_unchanged(self):
        # Mixing formats in tickets.created_at would break ordering
        ticket_id = db.create_ticket("Issue", "Details", "High", "alice")

        self.assertRegex(
            db.get_ticket(ticket_id)["created_at"],
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )

    def test_failed_sla_insert_rolls_back_ticket_and_history(self):
        self.add_trigger(
            "CREATE TRIGGER fail_sla BEFORE INSERT ON ticket_sla "
            "BEGIN SELECT RAISE(ABORT, 'simulated SLA failure'); END"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_ticket("Never saved", "Rolled back", "High", "alice")

        self.assertEqual(self.query("SELECT * FROM tickets"), [])
        self.assertEqual(self.query("SELECT * FROM ticket_history"), [])
        self.assertEqual(self.query("SELECT * FROM ticket_sla"), [])

    def test_unknown_priority_saves_nothing(self):
        with self.assertRaises(ValueError):
            db.create_ticket("Never saved", "Bad priority", "Urgent", "alice")

        self.assertEqual(self.query("SELECT * FROM tickets"), [])
        self.assertEqual(self.query("SELECT * FROM ticket_history"), [])
        self.assertEqual(self.query("SELECT * FROM ticket_sla"), [])


# ---------------------------------------------------
# FIRST QUALIFYING RESPONSE
# ---------------------------------------------------

class TestFirstResponse(TempDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = db.create_ticket(
            "Printer offline", "Paper jam", "High", "alice"
        )

    def first_response(self, ticket_id=None):
        return self.sla_row(ticket_id or self.ticket_id)["first_response_at"]

    def test_technician_comment_records_first_response(self):
        db.add_comment(self.ticket_id, "tech1", "Looking into it")

        comment = self.query("SELECT created_at FROM comments")[0]

        self.assertEqual(
            datetime.fromisoformat(self.first_response()),
            parse_stored_utc(comment["created_at"])
        )

    def test_admin_comment_records_first_response(self):
        db.add_comment(self.ticket_id, "admin", "On it")

        self.assertIsNotNone(self.first_response())

    def test_requester_comment_does_not_count(self):
        db.add_comment(self.ticket_id, "alice", "Still broken")

        self.assertIsNone(self.first_response())

    def test_other_regular_user_comment_does_not_count(self):
        db.add_comment(self.ticket_id, "bob", "Me too")

        self.assertIsNone(self.first_response())

    def test_staff_comment_on_own_ticket_does_not_count(self):
        own_ticket = db.create_ticket("My laptop", "Slow", "Low", "tech1")

        db.add_comment(own_ticket, "tech1", "Adding detail")

        self.assertIsNone(self.first_response(own_ticket))

    def test_author_without_account_does_not_count(self):
        db.add_comment(self.ticket_id, "ghost", "Hello")

        self.assertIsNone(self.first_response())

    def test_requester_then_technician_records_technician(self):
        db.add_comment(self.ticket_id, "alice", "Any update?")
        db.add_comment(self.ticket_id, "tech1", "Fixed")

        self.assertIsNotNone(self.first_response())

    def test_later_staff_comment_does_not_overwrite_first(self):
        db.add_comment(self.ticket_id, "tech1", "First")

        # Pin a known value so a later overwrite would be visible even
        # within the same second
        sentinel = "2026-01-01T00:00:00+00:00"
        with self.connect() as conn:
            conn.execute(
                "UPDATE ticket_sla SET first_response_at = ? "
                "WHERE ticket_id = ?",
                (sentinel, self.ticket_id)
            )

        db.add_comment(self.ticket_id, "tech2", "Second")
        db.add_comment(self.ticket_id, "admin", "Third")

        self.assertEqual(self.first_response(), sentinel)

    def test_comment_on_historical_ticket_saves_without_sla(self):
        old_ticket = self.insert_historical_ticket()

        db.add_comment(old_ticket, "tech1", "Old ticket reply")

        self.assertIsNone(self.sla_row(old_ticket))
        self.assertEqual(
            len(self.query(
                "SELECT * FROM comments WHERE ticket_id = ?", (old_ticket,)
            )),
            1
        )

    def test_failed_comment_records_no_response(self):
        self.add_trigger(
            "CREATE TRIGGER fail_history BEFORE INSERT ON ticket_history "
            "BEGIN SELECT RAISE(ABORT, 'simulated history failure'); END"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            db.add_comment(self.ticket_id, "tech1", "Never saved")

        self.assertIsNone(self.first_response())
        self.assertEqual(self.query("SELECT * FROM comments"), [])

    def test_failed_response_update_rolls_back_comment(self):
        self.add_trigger(
            "CREATE TRIGGER fail_sla_update BEFORE UPDATE ON ticket_sla "
            "BEGIN SELECT RAISE(ABORT, 'simulated SLA failure'); END"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            db.add_comment(self.ticket_id, "tech1", "Never saved")

        self.assertEqual(self.query("SELECT * FROM comments"), [])
        self.assertEqual(
            [e["action"] for e in db.get_ticket_history(self.ticket_id)],
            ["created"]
        )

    def test_status_change_and_assignment_do_not_count(self):
        db.update_ticket(
            self.ticket_id, "In Progress", "tech1", actor="tech1"
        )

        self.assertIsNone(self.first_response())


# ---------------------------------------------------
# SLA STATUS LOOKUP
# ---------------------------------------------------

class TestSLAStatusLookup(TempDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = db.create_ticket("Down", "Server", "Critical", "alice")
        self.created = parse_stored_utc(
            db.get_ticket(self.ticket_id)["created_at"]
        )

    def set_due(self, value):
        with self.connect() as conn:
            conn.execute(
                "UPDATE ticket_sla SET response_due_at = ? "
                "WHERE ticket_id = ?",
                (value, self.ticket_id)
            )

    def test_new_ticket_is_within_sla(self):
        result = db.get_ticket_sla_status(
            self.ticket_id, now=self.created + timedelta(minutes=5)
        )
        self.assertEqual(result["status"], "Within SLA")

    def test_overdue_without_response(self):
        result = db.get_ticket_sla_status(
            self.ticket_id, now=self.created + timedelta(hours=2)
        )
        self.assertEqual(result["status"], "Overdue")

    def test_met_after_technician_reply(self):
        db.add_comment(self.ticket_id, "tech1", "Rebooted")

        result = db.get_ticket_sla_status(
            self.ticket_id, now=self.created + timedelta(hours=5)
        )
        self.assertEqual(result["status"], "Met")

    def test_historical_ticket_returns_none(self):
        old_ticket = self.insert_historical_ticket()

        self.assertIsNone(db.get_ticket_sla_status(old_ticket))

    def test_malformed_stored_value_returns_none(self):
        self.set_due("garbage")

        with self.assertLogs("database", level="WARNING"):
            self.assertIsNone(db.get_ticket_sla_status(self.ticket_id))

    def test_naive_stored_value_is_treated_as_utc(self):
        self.set_due("2026-11-17 09:00:00")

        result = db.get_ticket_sla_status(
            self.ticket_id,
            now=datetime(2026, 11, 17, 8, 45, tzinfo=timezone.utc)
        )
        self.assertEqual(result["status"], "Due Soon")


# ---------------------------------------------------
# ROUTES AND TICKET PAGE
# ---------------------------------------------------

class TestSLARoutes(TempDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()

        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        # CSRF is covered by tests/test_csrf.py
        app.config["WTF_CSRF_ENABLED"] = False
        self.addCleanup(self.restore_config)

        self.client = app.test_client()

    def restore_config(self):
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def create_ticket_as(self, username, priority="High"):
        self.login_as(username, "user")
        response = self.client.post("/tickets/new", data={
            "title": "Printer offline",
            "description": "Paper jam",
            "priority": priority,
        })
        self.assertEqual(response.status_code, 302)
        return int(re.search(r"/tickets/(\d+)", response.location).group(1))

    def comment_as(self, username, role, ticket_id, content="Update"):
        self.login_as(username, role)
        response = self.client.post(
            f"/tickets/{ticket_id}/comment", data={"content": content}
        )
        self.assertEqual(response.status_code, 302)

    def page(self, username, role, ticket_id):
        self.login_as(username, role)
        return self.client.get(f"/tickets/{ticket_id}")

    def test_new_ticket_form_creates_sla(self):
        ticket_id = self.create_ticket_as("alice")

        self.assertIsNotNone(self.sla_row(ticket_id))

    def test_ticket_page_shows_sla_to_requester(self):
        ticket_id = self.create_ticket_as("alice")

        response = self.page("alice", "user", ticket_id)
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-testid="response-sla"', html)
        self.assertIn("Within SLA", html)
        self.assertNotIn("First response", html)

    def test_technician_comment_route_marks_sla_met(self):
        ticket_id = self.create_ticket_as("alice")

        self.comment_as("tech1", "technician", ticket_id, "Fixed it")

        self.assertIsNotNone(self.sla_row(ticket_id)["first_response_at"])

        html = self.page("tech1", "technician", ticket_id).get_data(
            as_text=True
        )
        self.assertIn("Met", html)
        self.assertIn("First response", html)

    def test_requester_comment_route_does_not_count(self):
        ticket_id = self.create_ticket_as("alice")

        self.comment_as("alice", "user", ticket_id, "Any news?")

        self.assertIsNone(self.sla_row(ticket_id)["first_response_at"])

    def test_role_comes_from_database_not_session(self):
        ticket_id = self.create_ticket_as("alice")

        # bob's session claims technician, but his account is a user.
        # Route access uses the session; the SLA uses the database.
        self.comment_as("bob", "technician", ticket_id, "Pretending")

        self.assertIsNone(self.sla_row(ticket_id)["first_response_at"])

    def test_other_users_ticket_still_hidden_with_no_sla_leak(self):
        ticket_id = self.create_ticket_as("alice")

        response = self.page("bob", "user", ticket_id)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("response-sla", response.get_data(as_text=True))

    def test_historical_ticket_page_has_no_sla(self):
        old_ticket = self.insert_historical_ticket()

        response = self.page("alice", "user", old_ticket)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("response-sla", response.get_data(as_text=True))

    def test_damaged_sla_row_does_not_break_page(self):
        ticket_id = self.create_ticket_as("alice")

        with self.connect() as conn:
            conn.execute(
                "UPDATE ticket_sla SET response_due_at = 'garbage' "
                "WHERE ticket_id = ?",
                (ticket_id,)
            )

        with self.assertLogs("database", level="WARNING"):
            response = self.page("alice", "user", ticket_id)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("response-sla", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
