
import re
import unittest
from unittest.mock import patch

from app import app


class TestAuthorization(unittest.TestCase):

    def setUp(self):
        # Save the original settings
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get(
            "WTF_CSRF_ENABLED", True
        )

        # Enable testing with CSRF protection
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = True

        self.client = app.test_client()

    def tearDown(self):
        # Restore the original settings
        app.config["TESTING"] = self.old_testing
        app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def login_as(self, role):
        # Simulate a user with a particular role
        with self.client.session_transaction() as session:
            session["username"] = "test_user"
            session["role"] = role

    def get_csrf_token(self):
        # Retrieve a valid token from the new-ticket page
        response = self.client.get("/tickets/new")

        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)

        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            html
        )

        self.assertIsNotNone(match)

        return match.group(1)

    def test_regular_user_cannot_update_ticket(self):
        # Log in as a regular user
        self.login_as("user")

        token = self.get_csrf_token()

        # Mock the database update
        with patch("app.db.update_ticket") as mock_update:

            response = self.client.post(
                "/tickets/1/update",
                data={
                    "status": "Resolved",
                    "assigned_to": "test_user",
                    "csrf_token": token
                }
            )

            # User should be redirected
            self.assertEqual(response.status_code, 302)

            # Database update must not occur
            mock_update.assert_not_called()

    def test_technician_can_update_ticket(self):
        self.login_as("technician")

        token = self.get_csrf_token()

        with patch("app.db.update_ticket") as mock_update:

            response = self.client.post(
                "/tickets/1/update",
                data={
                    "status": "In Progress",
                    "assigned_to": "test_user",
                    "csrf_token": token
                }
            )

            self.assertEqual(response.status_code, 302)

            mock_update.assert_called_once_with(
                1, "In Progress", "test_user"
            )

    def test_admin_can_update_ticket(self):
        self.login_as("admin")

        token = self.get_csrf_token()

        with patch("app.db.update_ticket") as mock_update:

            response = self.client.post(
                "/tickets/1/update",
                data={
                    "status": "Resolved",
                    "assigned_to": "test_user",
                    "csrf_token": token
                }
            )

            self.assertEqual(response.status_code, 302)

            mock_update.assert_called_once_with(
                1, "Resolved", "test_user"
            )


if __name__ == "__main__":
    unittest.main()
