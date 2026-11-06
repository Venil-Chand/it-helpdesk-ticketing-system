
import os
import tempfile
import unittest
from unittest.mock import patch

import database as db


class TestTicketSearch(unittest.TestCase):

    def setUp(self):
        # Create a temporary directory for the test database
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        test_path = os.path.join(
            self.temp_dir.name,
            "test_search.db"
        )

        # Use the test database instead of the real database
        db_patch = patch.object(db, "DB_PATH", test_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        # Create test database tables
        db.init_db()

        # Insert sample tickets
        db.create_ticket(
            "Printer not working",
            "Printer is offline",
            "High",
            "alice"
        )

        db.create_ticket(
            "Printer paper jam",
            "Paper stuck in printer",
            "Medium",
            "bob"
        )

        db.create_ticket(
            "Laptop overheating",
            "Laptop becomes hot",
            "Low",
            "alice"
        )

    def test_user_search_returns_only_own_ticket(self):
        # Alice searches for printer tickets
        tickets = db.search_tickets(
            "printer",
            username="alice"
        )

        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["created_by"], "alice")

    def test_staff_search_returns_all_matching_tickets(self):
        # Unrestricted search returns both printer tickets
        tickets = db.search_tickets("printer")

        self.assertEqual(len(tickets), 2)

    def test_search_with_priority_filter(self):
        # Alice searches for high-priority printer tickets
        tickets = db.search_tickets(
            "printer",
            username="alice",
            priority_filter="High"
        )

        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["priority"], "High")

    def test_search_cannot_bypass_ownership(self):
        # Alice cannot find Bob's paper jam ticket
        tickets = db.search_tickets(
            "paper jam",
            username="alice"
        )

        self.assertEqual(len(tickets), 0)

    def test_sql_injection_text_is_treated_as_data(self):
        # SQL-like text must not bypass the search filters
        tickets = db.search_tickets(
            "' OR 1=1 --",
            username="alice"
        )

        self.assertEqual(len(tickets), 0)


if __name__ == "__main__":
    unittest.main()
