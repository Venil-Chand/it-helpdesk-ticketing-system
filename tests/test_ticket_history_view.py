
"""
Rendering tests for the Ticket Activity History timeline.

The real ticket_detail.html template is rendered with the database
mocked, and DB_PATH points at a location that cannot be opened, so an
accidental real query fails instead of reading helpdesk.db.
"""

import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database as db
from app import app


def event(action, actor, old=None, new=None, role=None, event_id=1,
          created_at="2026-11-10 07:30:00"):
    field = None
    if action == "status_changed":
        field = "status"
    elif action in ("assigned", "reassigned", "unassigned"):
        field = "assigned_to"

    return {
        "id": event_id, "ticket_id": 1, "actor": actor, "action": action,
        "field_name": field, "old_value": old, "new_value": new,
        "created_at": created_at, "actor_role": role,
    }


class TestHistoryTimeline(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        app.config["TESTING"] = True
        self.client = app.test_client()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_guard = patch.object(
            db, "DB_PATH",
            os.path.join(temp_dir.name, "missing", "helpdesk.db")
        )
        db_guard.start()
        self.addCleanup(db_guard.stop)

        self.ticket = {
            "id": 1, "title": "Printer offline", "description": "Jammed",
            "priority": "High", "status": "In Progress",
            "assigned_to": "tech2", "created_by": "alice",
            "created_at": "2026-11-10 07:00:00",
            "updated_at": "2026-11-10 07:30:00",
        }

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

    def render(self, history, username="alice", role="user"):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

        with patch("app.db.get_ticket", return_value=self.ticket), \
                patch("app.db.get_comments", return_value=[]), \
                patch("app.db.get_ticket_history", return_value=history), \
                patch("app.db.get_all_usernames", return_value=["tech2"]):
            response = self.client.get("/tickets/1")

        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def text(self, html):
        """Visible text with tags removed and whitespace collapsed."""
        return " ".join(re.sub(r"<[^>]+>", " ", html).split())

    def test_each_event_type_is_described(self):
        html = self.render([
            event("commented", "alice", event_id=6),
            event("unassigned", "admin", old="tech2", role="admin",
                  event_id=5),
            event("reassigned", "bob", old="tech1", new="tech2",
                  role="technician", event_id=4),
            event("assigned", "admin", new="tech1", role="admin",
                  event_id=3),
            event("status_changed", "bob", old="Open", new="In Progress",
                  role="technician", event_id=2),
            event("created", "alice", event_id=1),
        ])
        text = self.text(html)

        self.assertIn("Ticket Activity History", text)
        self.assertIn("alice added a note.", text)
        self.assertIn("Administrator admin unassigned tech2 .", text)
        self.assertIn(
            "Technician bob reassigned this ticket from tech1 to tech2 .",
            text
        )
        self.assertIn(
            "Administrator admin assigned this ticket to tech1 .", text
        )
        self.assertIn(
            "Technician bob changed status from Open to In Progress .", text
        )
        self.assertIn("alice created this ticket.", text)

    def test_events_keep_the_order_they_are_given(self):
        html = self.render([
            event("commented", "alice", event_id=2),
            event("created", "alice", event_id=1),
        ])

        self.assertLess(
            html.index("added a note"), html.index("created this ticket")
        )

    def test_times_are_labelled_as_utc(self):
        html = self.render([event("created", "alice")])

        self.assertIn('datetime="2026-11-10 07:30:00Z"', html)
        self.assertIn("2026-11-10 07:30 UTC", self.text(html))

    def test_ticket_without_history_shows_a_message(self):
        html = self.render([])

        self.assertIn("No activity recorded yet", html)
        self.assertNotIn('class="timeline-item"', html)

    def test_stored_text_is_escaped_not_run_as_html(self):
        attack = '<script>alert("x")</script>'
        html = self.render([
            event("assigned", attack, new=attack, role="admin"),
        ])

        self.assertNotIn(attack, html)
        self.assertIn("&lt;script&gt;", html)

    def test_existing_ticket_page_sections_are_still_shown(self):
        html = self.render([event("created", "alice")],
                           username="admin", role="admin")

        self.assertIn("Description", html)
        self.assertIn("Add a Note", html)
        self.assertIn("Update Ticket", html)
        self.assertIn('name="csrf_token"', html)


if __name__ == "__main__":
    unittest.main()
