
import unittest
from unittest.mock import patch

from app import app


class TestTicketOwnership(unittest.TestCase):

    def setUp(self):
        # Save original application settings
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")

        # Configure the application for testing
        app.config["TESTING"] = True

        # Disable CSRF only for these authorization tests
        app.config["WTF_CSRF_ENABLED"] = False

        self.client = app.test_client()

        # Create sample tickets without using the real database
        self.alice_ticket = {
            "id": 1,
            "created_by": "alice",
            "status": "Open",
            "priority": "High"
        }

        self.bob_ticket = {
            "id": 2,
            "created_by": "bob",
            "status": "Resolved",
            "priority": "Low"
        }

    def tearDown(self):
        # Restore original application settings
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def login_as(self, username, role):
        # Simulate a logged-in user
        with self.client.session_transaction() as session:
            session["username"] = username
            session["role"] = role

    def test_owner_can_view_own_ticket(self):
        # Alice should be able to view her ticket
        self.login_as("alice", "user")

        with patch(
            "app.db.get_ticket",
            return_value=self.alice_ticket
        ), patch(
            "app.db.get_comments",
            return_value=[]
        ), patch(
            "app.db.get_all_usernames",
            return_value=[]
        ), patch(
            "app.render_template",
            return_value="Ticket details"
        ):

            response = self.client.get("/tickets/1")

            self.assertEqual(response.status_code, 200)

    def test_user_cannot_view_another_users_ticket(self):
        # Alice should not be able to view Bob's ticket
        self.login_as("alice", "user")

        with patch(
            "app.db.get_ticket",
            return_value=self.bob_ticket
        ):

            response = self.client.get("/tickets/2")

            self.assertEqual(response.status_code, 404)

    def test_technician_can_view_another_users_ticket(self):
        # Technicians can access tickets belonging to others
        self.login_as("tech1", "technician")

        with patch(
            "app.db.get_ticket",
            return_value=self.bob_ticket
        ), patch(
            "app.db.get_comments",
            return_value=[]
        ), patch(
            "app.db.get_all_usernames",
            return_value=[]
        ), patch(
            "app.render_template",
            return_value="Ticket details"
        ):

            response = self.client.get("/tickets/2")

            self.assertEqual(response.status_code, 200)

    def test_user_cannot_comment_on_another_ticket(self):
        # Alice must not be able to comment on Bob's ticket
        self.login_as("alice", "user")

        with patch(
            "app.db.get_ticket",
            return_value=self.bob_ticket
        ), patch(
            "app.db.add_comment"
        ) as mock_comment:

            response = self.client.post(
                "/tickets/2/comment",
                data={"content": "Unauthorised comment"}
            )

            self.assertEqual(response.status_code, 404)

            # The database must not be modified
            mock_comment.assert_not_called()

    def test_owner_can_comment_on_own_ticket(self):
        # Alice may comment on her own ticket
        self.login_as("alice", "user")

        with patch(
            "app.db.get_ticket",
            return_value=self.alice_ticket
        ), patch(
            "app.db.add_comment"
        ) as mock_comment:

            response = self.client.post(
                "/tickets/1/comment",
                data={"content": "Please help"}
            )

            self.assertEqual(response.status_code, 302)

            mock_comment.assert_called_once_with(
                1,
                "alice",
                "Please help"
            )

    def test_dashboard_shows_only_own_tickets(self):
        """
        A regular user must see only their own tickets.

        The dashboard counts and pages tickets with count_tickets() and
        get_tickets_page(). This test verifies:
          1. Both queries are restricted to Alice's username, so neither
             the page contents nor the ticket total include other users.
          2. get_all_tickets is never called for a regular user.
          3. get_stats is called with username= so statistics are user-specific.
          4. Only Alice's ticket appears in the template context.
        """
        self.login_as("alice", "user")

        # Only Alice's ticket is returned — Bob's is excluded at database level
        alice_only = [self.alice_ticket]

        # Fake statistics so the real database is never touched
        mock_stats = {
            "total": 1,
            "open": 1,
            "in_progress": 0,
            "critical": 0
        }

        with patch(
            "app.db.count_tickets",
            return_value=1
        ) as mock_count, patch(
            "app.db.get_tickets_page",
            return_value=alice_only
        ) as mock_page, patch(
            "app.db.get_all_tickets"
        ) as mock_get_all, patch(
            "app.db.get_stats",
            return_value=mock_stats
        ) as mock_stats_fn, patch(
            "app.render_template",
            return_value="Dashboard"
        ) as mock_template:

            response = self.client.get("/")

            self.assertEqual(response.status_code, 200)

            # The total must count Alice's tickets only; otherwise
            # "of N tickets" would reveal how many tickets others have
            mock_count.assert_called_once_with(
                search_text="",
                username="alice",
                status_filter="All",
                priority_filter="All"
            )

            # The page must be read from Alice's tickets only,
            # starting at page 1 with the default "All" filters
            mock_page.assert_called_once_with(
                search_text="",
                username="alice",
                status_filter="All",
                priority_filter="All",
                page=1,
                per_page=10
            )

            # Regular users must never trigger the all-tickets query
            mock_get_all.assert_not_called()

            # Statistics must be scoped to Alice, not the whole system
            mock_stats_fn.assert_called_once_with(username="alice")

            # Inspect the ticket list passed to the template
            displayed_tickets = (
                mock_template.call_args.kwargs["tickets"]
            )

            # Alice should see exactly one ticket
            self.assertEqual(len(displayed_tickets), 1)

            # That ticket must belong to Alice
            self.assertEqual(
                displayed_tickets[0]["created_by"],
                "alice"
            )


if __name__ == "__main__":
    unittest.main()
