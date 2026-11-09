
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database as db


class TestDatabaseFilters(unittest.TestCase):

    def setUp(self):
        # Create a temporary directory for testing
        self.temp_dir = tempfile.TemporaryDirectory()

        # Redirect database operations to a test database
        test_db_path = str(
            Path(self.temp_dir.name) / "test_helpdesk.db"
        )

        self.db_patch = patch.object(
            db, "DB_PATH", test_db_path
        )

        self.db_patch.start()

        # Create empty database tables
        db.init_db()

        # Add three test tickets
        db.create_ticket(
            "Printer issue", "Printer jam", "High", "alice"
        )

        db.create_ticket(
            "WiFi issue", "Slow network", "Critical", "bob"
        )

        db.create_ticket(
            "Monitor issue", "Blank screen", "High", "alice"
        )

        # Change one ticket's status
        conn = db.get_db()

        conn.execute(
            "UPDATE tickets SET status = ? WHERE title = ?",
            ("In Progress", "Monitor issue")
        )

        conn.commit()
        conn.close()

    def tearDown(self):
        # Restore the actual database path
        self.db_patch.stop()

        # Remove the temporary test database
        self.temp_dir.cleanup()

    def test_user_sees_only_own_tickets(self):
        tickets = db.get_tickets_by_user("alice")

        self.assertEqual(len(tickets), 2)

        for ticket in tickets:
            self.assertEqual(ticket["created_by"], "alice")

    def test_user_tickets_with_filters(self):
        tickets = db.get_tickets_by_user(
            "alice",
            "In Progress",
            "High"
        )

        self.assertEqual(len(tickets), 1)
        self.assertEqual(
            tickets[0]["title"],
            "Monitor issue"
        )

    def test_user_query_without_username_returns_nothing(self):
        # A missing username must never widen the query to every ticket.
        # A ticket with no owner is included to prove NULL does not match.
        db.create_ticket(
            "Orphan ticket", "No owner recorded", "Low", None
        )

        tickets = db.get_tickets_by_user(None)

        self.assertEqual(len(tickets), 0)

    def test_user_statistics(self):
        stats = db.get_stats(username="alice")

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["open"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["critical"], 0)

    def test_global_statistics(self):
        stats = db.get_stats()

        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["open"], 2)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["critical"], 1)


if __name__ == "__main__":
    unittest.main()
