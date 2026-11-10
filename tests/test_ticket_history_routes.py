
"""
End-to-end tests for ticket activity history through the Flask routes.

Nothing is mocked: each request runs through the real route, real SQL
against a temporary SQLite database, and the real template. CSRF is
disabled here because tests/test_csrf.py already covers it; these tests
focus on history, identity and ownership.

The real helpdesk.db is never opened.
"""

import os
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
from app import app


class TestHistoryRoutes(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_patch = patch.object(
            db, "DB_PATH", os.path.join(temp_dir.name, "routes.db")
        )
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

        # Accounts exist so the timeline can show each actor's role.
        # Sessions are set directly, so these hashes are never checked.
        conn = db.get_db()
        for username, role in [("admin", "admin"), ("tech1", "technician"),
                               ("tech2", "technician"), ("alice", "user"),
                               ("bob", "user")]:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES (?, ?, ?)",
                (username, generate_password_hash("unused-test-value"), role)
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    # -----------------------------------------------------
    # HELPERS
    # -----------------------------------------------------

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def create_ticket_as(self, username, title="Printer offline"):
        """Create a ticket through the real form; return its id."""
        self.login_as(username, "user")
        response = self.client.post("/tickets/new", data={
            "title": title, "description": "Paper jam", "priority": "High",
        })
        self.assertEqual(response.status_code, 302)
        return int(re.search(r"/tickets/(\d+)", response.headers["Location"])
                   .group(1))

    def update_as(self, who, role, ticket_id, status, assigned_to="",
                  **form_fields):
        """Submit the update form as `who`; extra fields go in the form."""
        self.login_as(who, role)
        return self.client.post(
            f"/tickets/{ticket_id}/update",
            data={"status": status, "assigned_to": assigned_to,
                  **form_fields},
            follow_redirects=True,
        )

    def events(self, ticket_id):
        return [
            (e["action"], e["actor"], e["old_value"], e["new_value"])
            for e in db.get_ticket_history(ticket_id)
        ]

    def page_text(self, ticket_id):
        response = self.client.get(f"/tickets/{ticket_id}")
        html = response.get_data(as_text=True)
        return response, " ".join(re.sub(r"<[^>]+>", " ", html).split())

    # -----------------------------------------------------
    # EVENTS RECORDED THROUGH THE ROUTES
    # -----------------------------------------------------

    def test_creating_a_ticket_records_the_logged_in_user(self):
        ticket_id = self.create_ticket_as("alice")

        self.assertEqual(self.events(ticket_id),
                         [("created", "alice", None, None)])

    def test_full_lifecycle_is_recorded_and_shown_newest_first(self):
        ticket_id = self.create_ticket_as("alice")

        self.update_as("tech1", "technician", ticket_id, "In Progress")
        self.update_as("admin", "admin", ticket_id, "In Progress", "tech1")
        self.update_as("tech1", "technician", ticket_id, "In Progress",
                       "tech2")
        self.update_as("admin", "admin", ticket_id, "In Progress", "")

        self.login_as("alice", "user")
        self.client.post(f"/tickets/{ticket_id}/comment",
                         data={"content": "Thanks for the update"})

        self.assertEqual(self.events(ticket_id), [
            ("commented", "alice", None, None),
            ("unassigned", "admin", "tech2", None),
            ("reassigned", "tech1", "tech1", "tech2"),
            ("assigned", "admin", None, "tech1"),
            ("status_changed", "tech1", "Open", "In Progress"),
            ("created", "alice", None, None),
        ])

        response, text = self.page_text(ticket_id)
        self.assertEqual(response.status_code, 200)

        sentences = [
            "alice added a note.",
            "Administrator admin unassigned tech2 .",
            "Technician tech1 reassigned this ticket from tech1 to tech2 .",
            "Administrator admin assigned this ticket to tech1 .",
            "Technician tech1 changed status from Open to In Progress .",
            "alice created this ticket.",
        ]
        positions = [text.index(s) for s in sentences]
        self.assertEqual(positions, sorted(positions))

    def test_status_and_assignment_in_one_save_are_two_events(self):
        ticket_id = self.create_ticket_as("alice")

        self.update_as("tech1", "technician", ticket_id, "Resolved", "tech1")

        self.assertEqual(self.events(ticket_id)[:2], [
            ("assigned", "tech1", None, "tech1"),
            ("status_changed", "tech1", "Open", "Resolved"),
        ])

    def test_forged_actor_in_the_form_is_ignored(self):
        ticket_id = self.create_ticket_as("alice")

        self.update_as("tech1", "technician", ticket_id, "Closed",
                       actor="admin", username="admin", created_by="admin")

        self.assertEqual(self.events(ticket_id)[0][1], "tech1")

    def test_saving_without_changes_records_nothing(self):
        ticket_id = self.create_ticket_as("alice")

        response = self.update_as("tech1", "technician", ticket_id, "Open")

        self.assertIn("No changes to save.", response.get_data(as_text=True))
        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_invalid_status_records_nothing(self):
        ticket_id = self.create_ticket_as("alice")

        self.update_as("tech1", "technician", ticket_id, "Hacked")

        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_updating_a_missing_ticket_returns_404(self):
        self.login_as("admin", "admin")

        response = self.client.post("/tickets/999/update",
                                    data={"status": "Closed"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            db.get_db().execute("SELECT COUNT(*) FROM ticket_history")
            .fetchone()[0],
            0
        )

    # -----------------------------------------------------
    # WHO MAY SEE AND CHANGE HISTORY
    # -----------------------------------------------------

    def test_regular_user_can_view_their_own_ticket_history(self):
        ticket_id = self.create_ticket_as("alice")
        self.update_as("tech1", "technician", ticket_id, "In Progress")

        self.login_as("alice", "user")
        response, text = self.page_text(ticket_id)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ticket Activity History", text)
        self.assertIn("changed status from Open to In Progress", text)

    def test_regular_user_cannot_view_another_users_history(self):
        ticket_id = self.create_ticket_as("bob", title="Bob's laptop")
        self.update_as("tech1", "technician", ticket_id, "Resolved", "tech2")

        self.login_as("alice", "user")
        response = self.client.get(f"/tickets/{ticket_id}")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Bob's laptop", html)
        self.assertNotIn("tech2", html)

    def test_staff_can_view_any_ticket_history(self):
        ticket_id = self.create_ticket_as("bob")

        for username, role in [("admin", "admin"), ("tech2", "technician")]:
            with self.subTest(role=role):
                self.login_as(username, role)
                response, text = self.page_text(ticket_id)

                self.assertEqual(response.status_code, 200)
                self.assertIn("bob created this ticket.", text)

    def test_anonymous_visitor_cannot_view_history(self):
        ticket_id = self.create_ticket_as("alice")

        with self.client.session_transaction() as sess:
            sess.clear()

        response = self.client.get(f"/tickets/{ticket_id}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_regular_user_cannot_change_status_or_create_events(self):
        ticket_id = self.create_ticket_as("alice")

        self.update_as("alice", "user", ticket_id, "Closed", "alice")

        self.assertEqual(db.get_ticket(ticket_id)["status"], "Open")
        self.assertEqual(len(self.events(ticket_id)), 1)

    def test_user_cannot_add_events_to_another_users_ticket(self):
        ticket_id = self.create_ticket_as("bob")

        self.login_as("alice", "user")
        response = self.client.post(f"/tickets/{ticket_id}/comment",
                                    data={"content": "Not my ticket"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.events(ticket_id),
                         [("created", "bob", None, None)])

    # -----------------------------------------------------
    # OLD TICKETS, SQL SAFETY AND EXISTING FEATURES
    # -----------------------------------------------------

    def test_ticket_from_before_history_existed_renders(self):
        conn = db.get_db()
        ticket_id = conn.execute(
            "INSERT INTO tickets (title, description, created_by) "
            "VALUES ('Legacy ticket', 'Before auditing', 'alice')"
        ).lastrowid
        conn.commit()
        conn.close()

        self.login_as("alice", "user")
        response, text = self.page_text(ticket_id)

        self.assertEqual(response.status_code, 200)
        self.assertIn("No activity recorded yet", text)

    def test_sql_in_requests_does_not_touch_history(self):
        ticket_id = self.create_ticket_as("alice")

        self.login_as("alice", "user")
        self.assertEqual(
            self.client.get("/tickets/1%20OR%201=1").status_code, 404
        )

        self.client.post(f"/tickets/{ticket_id}/comment", data={
            "content": "'); DELETE FROM ticket_history; --"
        })

        self.assertEqual(len(self.events(ticket_id)), 2)

    def test_search_and_pagination_still_work(self):
        for n in range(1, 13):
            self.create_ticket_as("alice", title=f"Printer fault {n:02d}")
        self.create_ticket_as("alice", title="Laptop issue")

        self.login_as("alice", "user")
        text = " ".join(
            self.client.get("/?q=printer&page=2").get_data(as_text=True)
            .split()
        )

        self.assertIn("Showing 11–12 of 12 tickets", text)
        self.assertNotIn("Laptop issue", text)


if __name__ == "__main__":
    unittest.main()
