
"""
Integration tests for the dashboard search route.

These tests exercise the Flask layer rather than the database layer.
tests/test_ticket_search.py and tests/test_pagination_db.py cover the SQL
itself. This file checks that:

  - the index() route reads the 'q' query parameter correctly
  - the correct ownership restriction is applied for each role
  - the ticket count and the ticket page always use identical filters
  - the rendered HTML contains the search controls
  - the search term is retained in the input after submission

The database functions are mocked, and DB_PATH points at a location that
cannot be opened, so any accidental real database access fails loudly
instead of silently reading helpdesk.db. render_template is deliberately
NOT mocked, because several tests need to inspect the actual HTML.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import database as db
from app import app


class TestSearchRoute(unittest.TestCase):

    def setUp(self):
        # Save original application settings
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")

        # Configure the application for testing
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        self.client = app.test_client()

        # Point the database at a folder that does not exist, so an
        # unmocked query raises an error rather than reading real data
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_guard = patch.object(
            db,
            "DB_PATH",
            os.path.join(temp_dir.name, "missing", "helpdesk.db")
        )
        db_guard.start()
        self.addCleanup(db_guard.stop)

        # A complete ticket record matching what the template expects
        self.alice_ticket = {
            "id": 1,
            "title": "Printer not working",
            "description": "Printer is offline",
            "priority": "High",
            "status": "Open",
            "assigned_to": None,
            "created_by": "alice",
            "created_at": "2026-01-01 10:00",
            "updated_at": "2026-01-01 10:00"
        }

        # Fake statistics so the real database is never queried
        self.mock_stats = {
            "total": 1,
            "open": 1,
            "in_progress": 0,
            "critical": 0
        }

    def tearDown(self):
        # Restore original application settings
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    # -----------------------------------------------------
    # HELPERS
    # -----------------------------------------------------

    def login_as(self, username, role):
        """Simulate a logged-in user with the given role."""
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def dashboard_mocks(self, tickets, total=None):
        """Patches for every database call the dashboard makes."""
        if total is None:
            total = len(tickets)

        return (
            patch("app.db.count_tickets", return_value=total),
            patch("app.db.get_tickets_page", return_value=tickets),
            patch("app.db.get_stats", return_value=self.mock_stats),
        )

    def assert_queried(
        self,
        mock_count,
        mock_page,
        *,
        search_text,
        username,
        status="All",
        priority="All",
        page=1
    ):
        """The count and the page must receive exactly the same filters.

        If they ever differed, the "of N tickets" total could disagree
        with the rows shown, or reveal tickets the user may not see.
        """
        filters = {
            "search_text": search_text,
            "username": username,
            "status_filter": status,
            "priority_filter": priority,
        }

        mock_count.assert_called_once_with(**filters)
        mock_page.assert_called_once_with(
            **filters, page=page, per_page=10
        )

    # -----------------------------------------------------
    # RENDERED INTERFACE
    # -----------------------------------------------------

    def test_search_controls_present_in_dashboard(self):
        """The search input and Search button must appear in the HTML."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks(
            [self.alice_ticket]
        )

        with count_patch, page_patch, stats_patch:

            response = self.client.get("/")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)

            # The search input must be present and named "q"
            self.assertIn('name="q"', html)

            # The Search submit button must be present
            self.assertIn("Search", html)

    def test_search_query_retained_in_input(self):
        """The submitted search term must stay in the input field."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks(
            [self.alice_ticket]
        )

        with count_patch, page_patch, stats_patch:

            response = self.client.get("/?q=printer")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('value="printer"', html)

    def test_no_results_message_shows_search_term(self):
        """An unmatched search must tell the user what was searched for."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks([])

        with count_patch, page_patch, stats_patch:

            response = self.client.get("/?q=nonexistentterm")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("No tickets found matching", html)
            self.assertIn("nonexistentterm", html)

    # -----------------------------------------------------
    # ROLE-BASED SEARCH BEHAVIOUR
    # -----------------------------------------------------

    def test_regular_user_search_is_restricted_to_own_tickets(self):
        """A regular user's search must pass their username to the query."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks(
            [self.alice_ticket]
        )

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get("/?q=printer")

            self.assertEqual(response.status_code, 200)

            # username must be supplied so SQL filters by created_by
            self.assert_queried(
                mock_count,
                mock_page,
                search_text="printer",
                username="alice"
            )

    def test_technician_search_is_not_restricted_by_username(self):
        """Support staff must search across all tickets."""
        self.login_as("tech1", "technician")

        count_patch, page_patch, stats_patch = self.dashboard_mocks(
            [self.alice_ticket]
        )

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get("/?q=printer")

            self.assertEqual(response.status_code, 200)

            # username=None means no restriction — staff see every ticket
            self.assert_queried(
                mock_count,
                mock_page,
                search_text="printer",
                username=None
            )

    def test_admin_search_is_not_restricted_by_username(self):
        """Administrators must also search across all tickets."""
        self.login_as("admin", "admin")

        count_patch, page_patch, stats_patch = self.dashboard_mocks([])

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get("/?q=laptop")

            self.assertEqual(response.status_code, 200)

            self.assert_queried(
                mock_count,
                mock_page,
                search_text="laptop",
                username=None
            )

    # -----------------------------------------------------
    # SEARCH COMBINED WITH FILTERS
    # -----------------------------------------------------

    def test_search_combined_with_status_and_priority_filters(self):
        """Search must work together with the existing dropdown filters."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks([])

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get(
                "/?q=printer&status=Open&priority=High"
            )

            self.assertEqual(response.status_code, 200)

            self.assert_queried(
                mock_count,
                mock_page,
                search_text="printer",
                username="alice",
                status="Open",
                priority="High"
            )

    # -----------------------------------------------------
    # EMPTY SEARCH APPLIES NO TEXT FILTER
    # -----------------------------------------------------

    def test_empty_search_applies_no_text_filter(self):
        """With no search term the dashboard must not filter by text."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks(
            [self.alice_ticket]
        )

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get("/")

            self.assertEqual(response.status_code, 200)

            self.assert_queried(
                mock_count,
                mock_page,
                search_text="",
                username="alice"
            )

    def test_whitespace_only_search_is_treated_as_empty(self):
        """A search of only spaces must not filter by text."""
        self.login_as("alice", "user")

        count_patch, page_patch, stats_patch = self.dashboard_mocks([])

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get("/?q=%20%20%20")

            self.assertEqual(response.status_code, 200)

            self.assert_queried(
                mock_count,
                mock_page,
                search_text="",
                username="alice"
            )

    # -----------------------------------------------------
    # INPUT LENGTH LIMIT
    # -----------------------------------------------------

    def test_overlong_search_term_is_truncated(self):
        """A search term longer than the limit must be cut server-side.

        The template sets maxlength="100" but that only constrains the
        browser. A crafted URL can send any length, so the route must
        enforce the limit itself.
        """
        self.login_as("alice", "user")

        # 500 characters, well over the 100 character limit
        long_term = "a" * 500

        count_patch, page_patch, stats_patch = self.dashboard_mocks([])

        with count_patch as mock_count, page_patch as mock_page, \
                stats_patch:

            response = self.client.get(f"/?q={long_term}")

            self.assertEqual(response.status_code, 200)

            # Both queries receive the truncated term, and ownership
            # is still enforced alongside truncation
            self.assert_queried(
                mock_count,
                mock_page,
                search_text="a" * 100,
                username="alice"
            )

    # -----------------------------------------------------
    # AUTHENTICATION
    # -----------------------------------------------------

    def test_search_requires_login(self):
        """An anonymous visitor must not be able to search."""
        response = self.client.get("/?q=printer")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
