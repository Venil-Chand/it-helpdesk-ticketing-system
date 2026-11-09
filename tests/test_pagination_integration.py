
"""
End-to-end integration tests for dashboard pagination.

Nothing here is mocked. Each request goes through the real Flask route,
the real SQL queries against a temporary SQLite database, and the real
template. The tests then read the rendered HTML the way a browser
would: which tickets are listed, which page is current, and where each
pagination link leads. Several tests follow the Next links page by page.

The real helpdesk.db is never opened.
"""

import os
import re
import tempfile
import unittest
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import database as db
from app import app


class TestPaginationIntegration(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        app.config["TESTING"] = True
        self.client = app.test_client()

        # A fresh temporary database for every test
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_patch = patch.object(
            db,
            "DB_PATH",
            os.path.join(temp_dir.name, "integration.db")
        )
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

    # -----------------------------------------------------
    # SETUP HELPERS
    # -----------------------------------------------------

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def add_ticket(
        self,
        title,
        created_by,
        created_at,
        status="Open",
        priority="Medium"
    ):
        """Insert one ticket with a chosen timestamp; return its id."""
        conn = db.get_db()

        try:
            cursor = conn.execute(
                """
                INSERT INTO tickets
                (title, description, priority, status,
                 created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, "Test ticket", priority, status,
                 created_by, created_at)
            )
            conn.commit()
            return cursor.lastrowid

        finally:
            conn.close()

    def add_tickets(self, count, prefix="Ticket", created_by="alice",
                    **fields):
        """Add numbered tickets one minute apart; return ids oldest first."""
        return [
            self.add_ticket(
                f"{prefix} {n:02d}",
                created_by,
                f"2026-01-01 09:{n:02d}:00",
                **fields
            )
            for n in range(1, count + 1)
        ]

    def add_alice_and_bob_tickets(self):
        """15 tickets each, with interleaved timestamps."""
        alice, bob = [], []

        for n in range(1, 31):
            owner = "alice" if n % 2 else "bob"
            ticket_id = self.add_ticket(
                f"{owner} ticket {n:02d}",
                owner,
                f"2026-01-01 09:{n:02d}:00"
            )
            (alice if owner == "alice" else bob).append(ticket_id)

        return alice, bob

    # -----------------------------------------------------
    # HTML READING HELPERS
    # -----------------------------------------------------

    def get(self, url):
        response = self.client.get(url)
        return response, response.get_data(as_text=True)

    def ticket_ids(self, html):
        """Ids of the tickets listed in the table, top to bottom."""
        return [int(i) for i in re.findall(r'href="/tickets/(\d+)"', html)]

    def page_links(self, html):
        """Query parameters of every pagination link."""
        return [
            parse_qs(urlparse(unescape(href)).query)
            for href in re.findall(r'href="(/\?[^"]*)"', html)
        ]

    def next_link(self, html):
        match = re.search(r'href="([^"]+)"\s+aria-label="Next page"', html)
        return unescape(match.group(1)) if match else None

    def current_page(self, html):
        match = re.search(
            r'aria-current="page">\s*<span class="page-link">\s*(\d+)',
            html
        )
        return int(match.group(1)) if match else None

    def visible_text(self, html):
        return " ".join(html.split())

    def redirect_page(self, response):
        """The ?page= value a redirect points to."""
        query = parse_qs(urlparse(response.headers["Location"]).query)
        return query["page"]

    def walk_pages(self, url):
        """Follow Next links from url, returning all ids and each page."""
        ids, pages = [], []

        # Far more pages than any test creates, so a broken Next link
        # fails the test instead of looping forever
        for _ in range(20):
            response, html = self.get(url)
            self.assertEqual(response.status_code, 200)

            ids.extend(self.ticket_ids(html))
            pages.append(html)

            url = self.next_link(html)

            if url is None:
                return ids, pages

        self.fail("Next links never reached a last page")

    # -----------------------------------------------------
    # PAGE BOUNDARIES
    # -----------------------------------------------------

    def test_first_page_lists_the_ten_newest_tickets(self):
        ids = self.add_tickets(25)
        self.login_as("admin", "admin")

        response, html = self.get("/")

        self.assertEqual(response.status_code, 200)

        # ids are oldest first, so the newest ten are the last ten reversed
        self.assertEqual(self.ticket_ids(html), ids[::-1][:10])
        self.assertEqual(self.current_page(html), 1)

    def test_second_page_lists_the_next_ten_tickets(self):
        ids = self.add_tickets(25)
        self.login_as("admin", "admin")

        _, html = self.get("/?page=2")

        self.assertEqual(self.ticket_ids(html), ids[::-1][10:20])
        self.assertEqual(self.current_page(html), 2)

    def test_last_page_lists_the_remaining_tickets(self):
        ids = self.add_tickets(25)
        self.login_as("admin", "admin")

        _, html = self.get("/?page=3")

        self.assertEqual(self.ticket_ids(html), ids[::-1][20:])
        self.assertIn(
            "Showing 21–25 of 25 tickets", self.visible_text(html)
        )

    def test_previous_and_next_are_disabled_at_the_ends(self):
        self.add_tickets(25)
        self.login_as("admin", "admin")

        _, first = self.get("/?page=1")
        _, last = self.get("/?page=3")

        self.assertNotIn('aria-label="Previous page"', first)
        self.assertIn('aria-label="Next page"', first)

        self.assertIn('aria-label="Previous page"', last)
        self.assertNotIn('aria-label="Next page"', last)

    def test_fewer_than_ten_tickets_fit_on_one_page(self):
        self.add_tickets(4)
        self.login_as("admin", "admin")

        _, html = self.get("/")

        self.assertEqual(len(self.ticket_ids(html)), 4)
        self.assertNotIn('aria-label="Ticket pages"', html)
        self.assertIn("Showing 1–4 of 4 tickets", self.visible_text(html))

    def test_no_tickets_shows_the_empty_message(self):
        self.login_as("admin", "admin")

        response, html = self.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ticket_ids(html), [])
        self.assertIn("No tickets found", html)
        self.assertNotIn('aria-label="Ticket pages"', html)

    # -----------------------------------------------------
    # INVALID AND OUT-OF-RANGE PAGE NUMBERS
    # -----------------------------------------------------

    def test_invalid_page_numbers_show_the_first_page(self):
        ids = self.add_tickets(25)
        self.login_as("admin", "admin")

        for value in ("0", "-1", "-50", "abc", "", "2.5", "%00"):
            with self.subTest(page=value):
                response, html = self.get(f"/?page={value}")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.ticket_ids(html), ids[::-1][:10])

    def test_page_past_the_end_redirects_to_the_last_page(self):
        ids = self.add_tickets(25)
        self.login_as("admin", "admin")

        # The 30-digit value would overflow SQLite's integer range if it
        # ever reached the OFFSET; the redirect happens before any query
        for value in ("4", "99", "9" * 30):
            with self.subTest(page=value):
                response = self.client.get(f"/?page={value}")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.redirect_page(response), ["3"])

                _, html = self.get(response.headers["Location"])

                self.assertEqual(self.current_page(html), 3)
                self.assertEqual(self.ticket_ids(html), ids[::-1][20:])

    def test_page_past_the_end_of_no_results_redirects_to_page_one(self):
        self.login_as("admin", "admin")

        response = self.client.get("/?page=5")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.redirect_page(response), ["1"])

    # -----------------------------------------------------
    # SEARCH AND FILTERS ACROSS PAGES
    # -----------------------------------------------------

    def test_search_results_are_paginated_and_links_keep_the_term(self):
        printer_ids = self.add_tickets(13, prefix="Printer fault")
        self.add_tickets(9, prefix="Laptop issue")
        self.login_as("admin", "admin")

        ids, pages = self.walk_pages("/?q=printer")

        self.assertEqual(len(pages), 2)
        self.assertEqual(ids, printer_ids[::-1])
        self.assertIn(
            "Showing 11–13 of 13 tickets", self.visible_text(pages[1])
        )

        # Every link on every page carries the search term
        for html in pages:
            for link in self.page_links(html):
                self.assertEqual(link["q"], ["printer"])

        # The search box still shows the term on page 2
        self.assertIn('value="printer"', pages[1])

    def test_status_and_priority_filters_stay_active_on_every_page(self):
        matching = self.add_tickets(
            24, prefix="Match", status="Open", priority="High"
        )
        self.add_tickets(6, prefix="Closed", status="Closed", priority="High")
        self.add_tickets(6, prefix="Low", status="Open", priority="Low")
        self.login_as("admin", "admin")

        ids, pages = self.walk_pages("/?status=Open&priority=High")

        self.assertEqual(len(pages), 3)
        self.assertEqual(sorted(ids), sorted(matching))

        for html in pages:
            for link in self.page_links(html):
                self.assertEqual(link["status"], ["Open"])
                self.assertEqual(link["priority"], ["High"])

    # -----------------------------------------------------
    # OWNERSHIP AND ROLES
    # -----------------------------------------------------

    def test_regular_user_only_ever_sees_their_own_tickets(self):
        alice, _ = self.add_alice_and_bob_tickets()
        self.login_as("alice", "user")

        ids, pages = self.walk_pages("/")

        # 15 tickets at 10 per page is 2 pages
        self.assertEqual(len(pages), 2)
        self.assertEqual(sorted(ids), sorted(alice))

        # The total counts only Alice's tickets, not all 30
        self.assertIn("of 15 tickets", self.visible_text(pages[0]))

        for html in pages:
            self.assertNotIn("bob ticket", html)

    def test_regular_user_cannot_reach_more_tickets_by_changing_page(self):
        self.add_alice_and_bob_tickets()
        self.login_as("alice", "user")

        # Unrestricted, 30 tickets would make page 3 a real page.
        # Restricted to Alice's 15, page 3 does not exist.
        for value in ("3", "4", "100"):
            with self.subTest(page=value):
                response = self.client.get(f"/?page={value}")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.redirect_page(response), ["2"])

    def test_staff_see_every_users_tickets(self):
        alice, bob = self.add_alice_and_bob_tickets()

        for role in ("admin", "technician"):
            with self.subTest(role=role):
                self.login_as(f"staff_{role}", role)

                ids, pages = self.walk_pages("/")

                self.assertEqual(len(pages), 3)
                self.assertEqual(sorted(ids), sorted(alice + bob))

    # -----------------------------------------------------
    # ORDERING WITH IDENTICAL TIMESTAMPS
    # -----------------------------------------------------

    def test_following_next_links_never_repeats_or_skips_a_ticket(self):
        # 27 tickets created in the same second
        ids = [
            self.add_ticket(f"Bulk {n:02d}", "alice", "2026-01-01 09:00:00")
            for n in range(1, 28)
        ]
        self.login_as("admin", "admin")

        seen, pages = self.walk_pages("/")

        self.assertEqual(len(pages), 3)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(sorted(seen), sorted(ids))

    # -----------------------------------------------------
    # MALICIOUS PARAMETERS
    # -----------------------------------------------------

    def test_malicious_parameters_cannot_widen_a_regular_users_view(self):
        alice, _ = self.add_alice_and_bob_tickets()
        self.login_as("alice", "user")

        attacks = [
            "/?page=1%20OR%201=1",
            "/?page=1;DROP%20TABLE%20tickets",
            "/?status=%27%20OR%20%271%27=%271",
            "/?priority=High%27%20OR%201=1--",
            "/?q=%27%20OR%201=1%20--&page=2",
            "/?q=%25",
            "/?username=bob&created_by=bob&owner=bob",
            "/?role=admin",
            "/?per_page=1000",
            "/?page=-1&per_page=-1",
        ]

        for url in attacks:
            with self.subTest(url=url):
                response = self.client.get(url)

                # Follow an out-of-range redirect to the page it lands on
                if response.status_code == 302:
                    response = self.client.get(response.headers["Location"])

                self.assertEqual(response.status_code, 200)

                html = response.get_data(as_text=True)
                shown = self.ticket_ids(html)

                self.assertTrue(set(shown) <= set(alice))
                self.assertLessEqual(len(shown), 10)
                self.assertNotIn("bob ticket", html)

        # Every ticket survived every attempt
        self.assertEqual(db.count_tickets(), 30)

    # -----------------------------------------------------
    # STATISTICS AND AUTHENTICATION
    # -----------------------------------------------------

    def test_statistics_do_not_change_with_page_search_or_filters(self):
        self.add_tickets(25)
        self.login_as("admin", "admin")

        totals = set()

        for url in ("/", "/?page=2", "/?page=3", "/?q=Ticket%2001",
                    "/?status=Closed"):
            _, html = self.get(url)

            # The first statistics card is the overall total
            match = re.search(r'class="stat-num">\s*(\d+)', html)
            totals.add(int(match.group(1)))

        self.assertEqual(totals, {25})

    def test_anonymous_visitor_cannot_page_through_tickets(self):
        self.add_tickets(25)

        response = self.client.get("/?page=2")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
