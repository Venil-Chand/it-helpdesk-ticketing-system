
"""
Unit tests for dashboard pagination in the Flask layer.

parse_page() and build_pagination() are plain functions, so they are
tested directly with no Flask request involved. The route tests mock
the database, because the SQL itself is covered by test_pagination_db.py.

As in test_search_route.py, DB_PATH points at a location that cannot be
opened, so an unmocked database call fails instead of reading real data.
"""

import os
import re
import tempfile
import unittest
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import database as db
from app import app, build_pagination, page_window, parse_page


class TestParsePage(unittest.TestCase):

    def test_missing_page_is_first_page(self):
        self.assertEqual(parse_page(None), 1)

    def test_valid_page_number_is_used(self):
        self.assertEqual(parse_page("3"), 3)

    def test_zero_and_negative_pages_become_first_page(self):
        for value in ("0", "-1", "-999"):
            with self.subTest(value=value):
                self.assertEqual(parse_page(value), 1)

    def test_non_numeric_pages_become_first_page(self):
        for value in ("", "abc", "2.5", "1e3", "1 OR 1=1", "1; DROP TABLE"):
            with self.subTest(value=value):
                self.assertEqual(parse_page(value), 1)

    def test_extremely_long_number_does_not_crash(self):
        # Recent Python versions refuse to convert digit strings this
        # long, while older ones return a huge int. Either is safe.
        page = parse_page("9" * 5000)

        self.assertIsInstance(page, int)
        self.assertGreaterEqual(page, 1)


class TestPageWindow(unittest.TestCase):
    """None in the expected lists marks an ellipsis."""

    def test_single_page(self):
        self.assertEqual(page_window(1, 1), [1])

    def test_few_pages_are_all_shown(self):
        self.assertEqual(page_window(3, 5), [1, 2, 3, 4, 5])

    def test_middle_page_has_gaps_on_both_sides(self):
        self.assertEqual(
            page_window(10, 20),
            [1, None, 8, 9, 10, 11, 12, None, 20]
        )

    def test_first_page_has_gap_only_before_last(self):
        self.assertEqual(page_window(1, 20), [1, 2, 3, None, 20])

    def test_last_page_has_gap_only_after_first(self):
        self.assertEqual(page_window(20, 20), [1, None, 18, 19, 20])

    def test_single_missing_page_is_shown_not_hidden(self):
        # Between 1 and 3 only page 2 is missing. Showing "…" in place
        # of one page would hide it for no saving in space.
        self.assertEqual(
            page_window(5, 20),
            [1, 2, 3, 4, 5, 6, 7, None, 20]
        )
        self.assertEqual(
            page_window(16, 20),
            [1, None, 14, 15, 16, 17, 18, 19, 20]
        )

    def test_window_never_exceeds_the_page_range(self):
        for current in range(1, 8):
            with self.subTest(current=current):
                numbers = [n for n in page_window(current, 7) if n]

                self.assertEqual(min(numbers), 1)
                self.assertEqual(max(numbers), 7)
                self.assertEqual(numbers, sorted(set(numbers)))


class TestBuildPagination(unittest.TestCase):

    def test_middle_page(self):
        p = build_pagination(page=2, total_items=25, per_page=10)

        self.assertEqual(p["total_pages"], 3)
        self.assertTrue(p["has_prev"])
        self.assertTrue(p["has_next"])
        self.assertEqual((p["first_item"], p["last_item"]), (11, 20))

    def test_first_page_has_no_previous(self):
        p = build_pagination(page=1, total_items=25, per_page=10)

        self.assertFalse(p["has_prev"])
        self.assertTrue(p["has_next"])
        self.assertEqual((p["first_item"], p["last_item"]), (1, 10))

    def test_last_page_has_no_next_and_a_partial_range(self):
        p = build_pagination(page=3, total_items=25, per_page=10)

        self.assertTrue(p["has_prev"])
        self.assertFalse(p["has_next"])
        self.assertEqual((p["first_item"], p["last_item"]), (21, 25))

    def test_exact_multiple_does_not_add_an_empty_page(self):
        # 20 tickets fill exactly two pages: ceiling(20 / 10) = 2
        p = build_pagination(page=2, total_items=20, per_page=10)

        self.assertEqual(p["total_pages"], 2)
        self.assertFalse(p["has_next"])

    def test_one_extra_ticket_adds_a_page(self):
        # ceiling(21 / 10) = 3
        p = build_pagination(page=1, total_items=21, per_page=10)

        self.assertEqual(p["total_pages"], 3)

    def test_no_tickets_still_has_one_page(self):
        p = build_pagination(page=1, total_items=0, per_page=10)

        self.assertEqual(p["total_pages"], 1)
        self.assertFalse(p["has_prev"])
        self.assertFalse(p["has_next"])
        self.assertEqual((p["first_item"], p["last_item"]), (0, 0))


class TestDashboardPaginationRoute(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        app.config["TESTING"] = True
        self.client = app.test_client()

        # Any unmocked database call fails instead of reading real data
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_guard = patch.object(
            db,
            "DB_PATH",
            os.path.join(temp_dir.name, "missing", "helpdesk.db")
        )
        db_guard.start()
        self.addCleanup(db_guard.stop)

        self.ticket = {
            "id": 7,
            "title": "Printer not working",
            "description": "Printer is offline",
            "priority": "High",
            "status": "Open",
            "assigned_to": None,
            "created_by": "alice",
            "created_at": "2026-01-01 10:00",
            "updated_at": "2026-01-01 10:00"
        }

        self.stats = {
            "total": 25,
            "open": 25,
            "in_progress": 0,
            "critical": 0
        }

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def get_dashboard(self, url, total=25):
        """Request the dashboard with every database call mocked."""
        with patch(
            "app.db.count_tickets",
            return_value=total
        ) as mock_count, patch(
            "app.db.get_tickets_page",
            return_value=[self.ticket]
        ) as mock_page, patch(
            "app.db.get_stats",
            return_value=self.stats
        ):
            response = self.client.get(url)

        return response, mock_count, mock_page

    def test_page_parameter_reaches_the_query(self):
        self.login_as("alice", "user")

        response, _, mock_page = self.get_dashboard("/?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_page.call_args.kwargs["page"], 2)
        self.assertEqual(mock_page.call_args.kwargs["username"], "alice")

    def test_invalid_page_parameter_falls_back_to_first_page(self):
        self.login_as("alice", "user")

        for value in ("abc", "0", "-3", "2.5"):
            with self.subTest(page=value):
                response, _, mock_page = self.get_dashboard(
                    f"/?page={value}"
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(mock_page.call_args.kwargs["page"], 1)

    def test_page_past_the_end_redirects_to_last_page(self):
        self.login_as("alice", "user")

        response, mock_count, mock_page = self.get_dashboard(
            "/?q=printer&status=Open&page=99"
        )

        self.assertEqual(response.status_code, 302)

        query = parse_qs(urlparse(response.headers["Location"]).query)

        # 25 tickets at 10 per page: the last page is 3
        self.assertEqual(query["page"], ["3"])

        # Active search and filters survive the redirect;
        # the default priority is left out of the URL
        self.assertEqual(query["q"], ["printer"])
        self.assertEqual(query["status"], ["Open"])
        self.assertNotIn("priority", query)

        # The last page is worked out from Alice's own tickets
        self.assertEqual(mock_count.call_args.kwargs["username"], "alice")

        # No page of tickets is read before redirecting
        mock_page.assert_not_called()

    def visible_text(self, html):
        """Collapse the template's line breaks and indentation."""
        return " ".join(html.split())

    def test_navigation_links_keep_search_and_filters(self):
        self.login_as("alice", "user")

        response, _, _ = self.get_dashboard(
            "/?q=printer&priority=High&page=2"
        )
        html = response.get_data(as_text=True)
        text = self.visible_text(html)

        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-label="Ticket pages"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("Showing 11–20 of 25 tickets", text)

        # Previous goes to page 1 and Next goes to page 3,
        # each keeping the search term and priority filter
        self.assertIn(
            'href="/?page=1&amp;q=printer&amp;priority=High"', html
        )
        self.assertIn(
            'href="/?page=3&amp;q=printer&amp;priority=High"', html
        )

    def test_page_numbers_show_a_window_with_gaps(self):
        self.login_as("tech1", "technician")

        # 200 tickets at 10 per page is 20 pages; view page 10
        response, _, _ = self.get_dashboard("/?page=10", total=200)
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)

        # Every pagination link starts with "/?"; other links do not
        hrefs = [unescape(h) for h in re.findall(r'href="(/\?[^"]*)"', html)]
        linked = {
            int(parse_qs(urlparse(h).query)["page"][0]) for h in hrefs
        }

        # First, last and two either side of 10 are links; 10 itself
        # is the current page and is not a link
        self.assertEqual(linked, {1, 8, 9, 11, 12, 20})

        # One ellipsis before page 8 and one after page 12
        self.assertEqual(html.count("&hellip;"), 2)

        # The current page is marked for screen readers
        current = re.search(
            r'aria-current="page">\s*<span class="page-link">\s*(\d+)',
            html
        )
        self.assertEqual(current.group(1), "10")

    def test_single_page_shows_no_navigation(self):
        self.login_as("alice", "user")

        response, _, _ = self.get_dashboard("/", total=1)
        html = response.get_data(as_text=True)
        text = self.visible_text(html)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('aria-label="Ticket pages"', html)

        # Singular "ticket" for exactly one result
        self.assertIn("Showing 1–1 of 1 ticket", text)
        self.assertNotIn("of 1 tickets", text)


if __name__ == "__main__":
    unittest.main()
