
"""
Integration tests for the dashboard search route.

These tests exercise the Flask layer rather than the database layer.
tests/test_ticket_search.py already covers the SQL behaviour of
search_tickets(). This file checks that:

  - the index() route reads the 'q' query parameter correctly
  - the correct database function is chosen for each role
  - ownership is enforced for regular users at the route level
  - the rendered HTML contains the search controls
  - the search term is retained in the input after submission

The database functions are mocked so the real helpdesk.db is never read.
render_template is deliberately NOT mocked, because several tests need to
inspect the actual rendered HTML.
"""

import unittest
from unittest.mock import patch

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

    def login_as(self, username, role):
        """Simulate a logged-in user with the given role."""
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    # -----------------------------------------------------
    # RENDERED INTERFACE
    # -----------------------------------------------------

    def test_search_controls_present_in_dashboard(self):
        """The search input and Search button must appear in the HTML."""
        self.login_as("alice", "user")

        with patch(
            "app.db.get_tickets_by_user",
            return_value=[self.alice_ticket]
        ), patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

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

        with patch(
            "app.db.search_tickets",
            return_value=[self.alice_ticket]
        ), patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/?q=printer")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('value="printer"', html)

    def test_no_results_message_shows_search_term(self):
        """An unmatched search must tell the user what was searched for."""
        self.login_as("alice", "user")

        with patch(
            "app.db.search_tickets",
            return_value=[]
        ), patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

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

        with patch(
            "app.db.search_tickets",
            return_value=[self.alice_ticket]
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/?q=printer")

            self.assertEqual(response.status_code, 200)

            # username must be supplied so SQL filters by created_by
            mock_search.assert_called_once_with(
                search_text="printer",
                username="alice",
                status_filter="All",
                priority_filter="All"
            )

    def test_technician_search_is_not_restricted_by_username(self):
        """Support staff must search across all tickets."""
        self.login_as("tech1", "technician")

        with patch(
            "app.db.search_tickets",
            return_value=[self.alice_ticket]
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/?q=printer")

            self.assertEqual(response.status_code, 200)

            # No username argument — staff see every matching ticket
            mock_search.assert_called_once_with(
                search_text="printer",
                status_filter="All",
                priority_filter="All"
            )

    def test_admin_search_is_not_restricted_by_username(self):
        """Administrators must also search across all tickets."""
        self.login_as("admin", "admin")

        with patch(
            "app.db.search_tickets",
            return_value=[]
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/?q=laptop")

            self.assertEqual(response.status_code, 200)

            mock_search.assert_called_once_with(
                search_text="laptop",
                status_filter="All",
                priority_filter="All"
            )

    # -----------------------------------------------------
    # SEARCH COMBINED WITH FILTERS
    # -----------------------------------------------------

    def test_search_combined_with_status_and_priority_filters(self):
        """Search must work together with the existing dropdown filters."""
        self.login_as("alice", "user")

        with patch(
            "app.db.search_tickets",
            return_value=[]
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get(
                "/?q=printer&status=Open&priority=High"
            )

            self.assertEqual(response.status_code, 200)

            mock_search.assert_called_once_with(
                search_text="printer",
                username="alice",
                status_filter="Open",
                priority_filter="High"
            )

    # -----------------------------------------------------
    # EMPTY SEARCH FALLS BACK TO NORMAL RETRIEVAL
    # -----------------------------------------------------

    def test_empty_search_uses_standard_ticket_retrieval(self):
        """With no search term the dashboard must not call search_tickets."""
        self.login_as("alice", "user")

        with patch(
            "app.db.get_tickets_by_user",
            return_value=[self.alice_ticket]
        ) as mock_get_by_user, patch(
            "app.db.search_tickets"
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/")

            self.assertEqual(response.status_code, 200)

            # Normal retrieval is used
            mock_get_by_user.assert_called_once_with(
                "alice", "All", "All"
            )

            # The search path must not be triggered
            mock_search.assert_not_called()

    def test_whitespace_only_search_is_treated_as_empty(self):
        """A search of only spaces must not trigger a search query."""
        self.login_as("alice", "user")

        with patch(
            "app.db.get_tickets_by_user",
            return_value=[]
        ) as mock_get_by_user, patch(
            "app.db.search_tickets"
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get("/?q=%20%20%20")

            self.assertEqual(response.status_code, 200)

            mock_get_by_user.assert_called_once()
            mock_search.assert_not_called()

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

        with patch(
            "app.db.search_tickets",
            return_value=[]
        ) as mock_search, patch(
            "app.db.get_stats",
            return_value=self.mock_stats
        ):

            response = self.client.get(f"/?q={long_term}")

            self.assertEqual(response.status_code, 200)

            # The term reaching the database must be truncated
            passed_term = mock_search.call_args.kwargs["search_text"]

            self.assertEqual(len(passed_term), 100)

            # Ownership must still be enforced alongside truncation
            self.assertEqual(
                mock_search.call_args.kwargs["username"],
                "alice"
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
