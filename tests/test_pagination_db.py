
"""
Database-level tests for ticket pagination.

These tests run real SQL against a temporary SQLite database, so they
check the actual LIMIT and OFFSET behaviour rather than mocked results.
The real helpdesk.db is never opened.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import database as db


class TestTicketPagination(unittest.TestCase):

    def setUp(self):
        # Create a temporary database that is deleted after each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        db_patch = patch.object(
            db,
            "DB_PATH",
            os.path.join(self.temp_dir.name, "test_pagination.db")
        )
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

    # -----------------------------------------------------
    # HELPERS
    # -----------------------------------------------------

    def add_ticket(
        self,
        title,
        created_by="alice",
        created_at="2026-01-01 09:00:00",
        status="Open",
        priority="Medium",
        description="Test ticket"
    ):
        """Insert a ticket with a chosen timestamp and return its id."""
        conn = db.get_db()

        try:
            cursor = conn.execute(
                """
                INSERT INTO tickets
                (title, description, priority, status,
                 created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, description, priority, status,
                 created_by, created_at)
            )
            conn.commit()
            return cursor.lastrowid

        finally:
            conn.close()

    def add_numbered_tickets(self, count, created_by="alice"):
        """Add tickets one minute apart. Higher numbers are newer."""
        for n in range(1, count + 1):
            self.add_ticket(
                f"Ticket {n:02d}",
                created_by=created_by,
                created_at=f"2026-01-01 09:{n:02d}:00"
            )

    def titles(self, tickets):
        return [ticket["title"] for ticket in tickets]

    def expected_titles(self, newest, oldest):
        """Titles from newest down to oldest, inclusive."""
        return [f"Ticket {n:02d}" for n in range(newest, oldest - 1, -1)]

    # -----------------------------------------------------
    # PAGE BOUNDARIES
    # -----------------------------------------------------

    def test_first_page_returns_ten_newest_tickets(self):
        self.add_numbered_tickets(25)

        page = db.get_tickets_page(per_page=10, page=1)

        self.assertEqual(self.titles(page), self.expected_titles(25, 16))

    def test_second_page_returns_next_ten_tickets(self):
        self.add_numbered_tickets(25)

        page = db.get_tickets_page(per_page=10, page=2)

        self.assertEqual(self.titles(page), self.expected_titles(15, 6))

    def test_last_page_returns_remaining_tickets(self):
        self.add_numbered_tickets(25)

        page = db.get_tickets_page(per_page=10, page=3)

        # 25 tickets = two full pages of 10 and a last page of 5
        self.assertEqual(self.titles(page), self.expected_titles(5, 1))
        self.assertEqual(db.count_tickets(), 25)

    def test_page_past_the_end_is_empty(self):
        self.add_numbered_tickets(25)

        self.assertEqual(db.get_tickets_page(per_page=10, page=4), [])
        self.assertEqual(db.get_tickets_page(per_page=10, page=1000), [])

    def test_fewer_tickets_than_page_size_fit_on_one_page(self):
        self.add_numbered_tickets(3)

        self.assertEqual(db.count_tickets(), 3)
        self.assertEqual(len(db.get_tickets_page(per_page=10, page=1)), 3)
        self.assertEqual(db.get_tickets_page(per_page=10, page=2), [])

    def test_no_tickets_gives_empty_page_and_zero_count(self):
        self.assertEqual(db.count_tickets(), 0)
        self.assertEqual(db.get_tickets_page(per_page=10, page=1), [])

    def test_page_below_one_is_treated_as_first_page(self):
        self.add_numbered_tickets(15)

        first_page = db.get_tickets_page(per_page=10, page=1)

        for bad_page in (0, -1, -100):
            with self.subTest(page=bad_page):
                self.assertEqual(
                    db.get_tickets_page(per_page=10, page=bad_page),
                    first_page
                )

    # -----------------------------------------------------
    # ORDERING
    # -----------------------------------------------------

    def test_identical_timestamps_never_repeat_or_skip_tickets(self):
        # 25 tickets created in the same second, as happens with
        # bulk imports. Only the id tie-breaker separates them.
        ids = [
            self.add_ticket(
                f"Same second {n:02d}",
                created_at="2026-01-01 09:00:00"
            )
            for n in range(1, 26)
        ]

        seen = []
        for page_number in (1, 2, 3):
            page = db.get_tickets_page(per_page=10, page=page_number)
            seen.extend(ticket["id"] for ticket in page)

        # No ticket appears twice
        self.assertEqual(len(seen), len(set(seen)))

        # No ticket is missing
        self.assertEqual(sorted(seen), sorted(ids))

        # Among equal timestamps, the newest id comes first
        self.assertEqual(seen, sorted(ids, reverse=True))

    def test_orders_by_creation_time_before_id(self):
        # Inserted first (lower id) but has the newer timestamp
        newer = self.add_ticket(
            "Newer ticket", created_at="2026-03-01 12:00:00"
        )

        # Inserted second (higher id) but has the older timestamp
        older = self.add_ticket(
            "Older ticket", created_at="2026-01-01 12:00:00"
        )

        page = db.get_tickets_page(per_page=10, page=1)

        self.assertEqual([t["id"] for t in page], [newer, older])

    # -----------------------------------------------------
    # SEARCH AND FILTERS
    # -----------------------------------------------------

    def test_search_and_pagination_work_together(self):
        for n in range(1, 13):
            self.add_ticket(
                f"Printer fault {n:02d}",
                created_at=f"2026-01-01 09:{n:02d}:00"
            )

        for n in range(1, 9):
            self.add_ticket(
                f"Laptop issue {n:02d}",
                created_at=f"2026-01-01 10:{n:02d}:00"
            )

        # One match found only in the description
        self.add_ticket(
            "Office equipment",
            description="The printer on level 2 is jammed",
            created_at="2026-01-01 08:00:00"
        )

        self.assertEqual(db.count_tickets(search_text="printer"), 13)

        first = db.get_tickets_page(
            per_page=10, page=1, search_text="printer"
        )
        second = db.get_tickets_page(
            per_page=10, page=2, search_text="printer"
        )

        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 3)

        # The oldest match is the description-only one, on page 2
        self.assertEqual(second[-1]["title"], "Office equipment")

        for ticket in first + second:
            self.assertNotIn("Laptop", ticket["title"])

    def test_status_and_priority_filters_apply_to_count_and_page(self):
        for n in range(1, 16):
            self.add_ticket(
                f"Ticket {n:02d}",
                created_at=f"2026-01-01 09:{n:02d}:00",
                status="Open" if n % 2 else "Closed",
                priority="High" if n % 3 else "Low"
            )

        filters = {"status_filter": "Open", "priority_filter": "High"}

        # Odd numbers that are not multiples of 3:
        # 1, 5, 7, 11, 13 -> 5 tickets
        self.assertEqual(db.count_tickets(**filters), 5)

        page = db.get_tickets_page(per_page=10, page=1, **filters)

        self.assertEqual(len(page), 5)

        for ticket in page:
            self.assertEqual(ticket["status"], "Open")
            self.assertEqual(ticket["priority"], "High")

    # -----------------------------------------------------
    # OWNERSHIP
    # -----------------------------------------------------

    def add_alice_and_bob_tickets(self):
        """15 tickets each, with timestamps interleaved."""
        for n in range(1, 31):
            owner = "alice" if n % 2 else "bob"
            self.add_ticket(
                f"{owner} ticket {n:02d}",
                created_by=owner,
                created_at=f"2026-01-01 09:{n:02d}:00"
            )

    def test_regular_user_pages_contain_only_their_tickets(self):
        self.add_alice_and_bob_tickets()

        self.assertEqual(db.count_tickets(username="alice"), 15)

        first = db.get_tickets_page(per_page=10, page=1, username="alice")
        second = db.get_tickets_page(per_page=10, page=2, username="alice")
        third = db.get_tickets_page(per_page=10, page=3, username="alice")

        self.assertEqual(len(first), 10)
        self.assertEqual(len(second), 5)
        self.assertEqual(third, [])

        for ticket in first + second:
            self.assertEqual(ticket["created_by"], "alice")

    def test_unrestricted_pages_include_every_users_tickets(self):
        self.add_alice_and_bob_tickets()

        self.assertEqual(db.count_tickets(), 30)

        owners = set()
        for page_number in (1, 2, 3):
            page = db.get_tickets_page(per_page=10, page=page_number)
            owners.update(ticket["created_by"] for ticket in page)

        self.assertEqual(owners, {"alice", "bob"})

    # -----------------------------------------------------
    # PAGE SIZE LIMITS
    # -----------------------------------------------------

    def test_page_size_is_capped_at_maximum(self):
        self.add_numbered_tickets(59)

        page = db.get_tickets_page(per_page=1000, page=1)

        self.assertEqual(len(page), db.MAX_PER_PAGE)

    def test_page_size_below_one_is_raised_to_one(self):
        self.add_numbered_tickets(3)

        for bad_size in (0, -5):
            with self.subTest(per_page=bad_size):
                page = db.get_tickets_page(per_page=bad_size, page=1)
                self.assertEqual(len(page), 1)

    # -----------------------------------------------------
    # SQL SAFETY
    # -----------------------------------------------------

    def test_sql_in_search_cannot_bypass_ownership(self):
        self.add_alice_and_bob_tickets()

        attacks = [
            "' OR 1=1 --",
            "x' OR created_by = 'bob",
            "'; DROP TABLE tickets; --",
        ]

        for attack in attacks:
            with self.subTest(search=attack):
                self.assertEqual(
                    db.count_tickets(search_text=attack, username="alice"),
                    0
                )
                self.assertEqual(
                    db.get_tickets_page(
                        per_page=10,
                        page=1,
                        search_text=attack,
                        username="alice"
                    ),
                    []
                )

        # The table survived every attempt
        self.assertEqual(db.count_tickets(), 30)

    def test_like_wildcards_cannot_widen_ownership(self):
        # % and _ are LIKE wildcards. They can match more text, but
        # the ownership condition is a separate part of the WHERE
        # clause, so they can never reach another user's tickets.
        self.add_alice_and_bob_tickets()

        for wildcard in ("%", "_"):
            with self.subTest(search=wildcard):
                page = db.get_tickets_page(
                    per_page=50,
                    page=1,
                    search_text=wildcard,
                    username="alice"
                )

                self.assertEqual(len(page), 15)

                for ticket in page:
                    self.assertEqual(ticket["created_by"], "alice")

    # -----------------------------------------------------
    # INDEXING
    # -----------------------------------------------------

    def test_staff_ordering_is_served_by_index_without_sorting(self):
        # Without an index on (created_at, id), SQLite must sort every
        # ticket on each request just to return one page.
        conn = db.get_db()

        try:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT * FROM tickets "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (10, 20)
            ).fetchall()

        finally:
            conn.close()

        plan_text = " ".join(row[3] for row in plan)

        self.assertIn("idx_tickets_date", plan_text)
        self.assertNotIn("TEMP B-TREE", plan_text)


if __name__ == "__main__":
    unittest.main()
