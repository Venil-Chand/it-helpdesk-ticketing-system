
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
        # Alice should see her ticket but not Bob's
        self.login_as("alice", "user")

        all_tickets = [
            self.alice_ticket,
            self.bob_ticket
        ]

        with patch(
            "app.db.get_all_tickets",
            return_value=all_tickets
        ), patch(
            "app.render_template",
            return_value="Dashboard"
        ) as mock_template:

            response = self.client.get("/")

            self.assertEqual(response.status_code, 200)

            # Inspect the data sent to the dashboard
            displayed_tickets = (
                mock_template.call_args.kwargs["tickets"]
            )

            self.assertEqual(len(displayed_tickets), 1)

            self.assertEqual(
                displayed_tickets[0]["created_by"],
                "alice"
            )


if __name__ == "__main__":
    unittest.main()
