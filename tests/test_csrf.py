
import re
import unittest
from unittest.mock import patch

from app import app


class TestCSRFProtection(unittest.TestCase):

    def setUp(self):
        # Enable testing and CSRF validation
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED", True)

        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = True

        self.addCleanup(self.restore_settings)

        # Create a test browser
        self.client = app.test_client()

    def restore_settings(self):
        app.config["TESTING"] = self.old_testing
        app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def login_session(self):
        # Simulate an authenticated user
        with self.client.session_transaction() as session:
            session["username"] = "test_user"
            session["role"] = "admin"

    def test_login_without_csrf_token(self):
        response = self.client.post(
            "/login",
            data={"username": "test", "password": "test"}
        )

        self.assertEqual(response.status_code, 400)

    def test_ticket_creation_without_csrf_token(self):
        self.login_session()

        response = self.client.post(
            "/tickets/new",
            data={
                "title": "Test issue",
                "description": "Test description",
                "priority": "Medium"
            }
        )

        self.assertEqual(response.status_code, 400)

    def test_ticket_update_without_csrf_token(self):
        self.login_session()

        response = self.client.post(
            "/tickets/1/update",
            data={
                "status": "Resolved",
                "assigned_to": "test_user"
            }
        )

        self.assertEqual(response.status_code, 400)

    def test_comment_without_csrf_token(self):
        self.login_session()

        response = self.client.post(
            "/tickets/1/comment",
            data={"content": "Test comment"}
        )

        self.assertEqual(response.status_code, 400)

    def test_invalid_csrf_token(self):
        response = self.client.post(
            "/login",
            data={
                "username": "test",
                "password": "test",
                "csrf_token": "invalid-token"
            }
        )

        self.assertEqual(response.status_code, 400)

    def test_login_with_valid_csrf_token(self):
        # Retrieve a real token from the login page
        response = self.client.get("/login")
        html = response.get_data(as_text=True)

        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            html
        )

        self.assertIsNotNone(match)

        token = match.group(1)

        # Mock the database lookup to avoid using real accounts
        with patch("app.db.get_user", return_value=None):
            response = self.client.post(
                "/login",
                data={
                    "username": "nonexistent_test_user",
                    "password": "dummy-password",
                    "csrf_token": token
                }
            )

        # Valid token allows request to reach login validation
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
