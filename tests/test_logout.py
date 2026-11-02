
import re
import unittest

from app import app


class TestSecureLogout(unittest.TestCase):

    def setUp(self):
        # Remember the original application settings
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED", True)

        # Enable testing and CSRF protection
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = True

        # Create a test browser
        self.client = app.test_client()

        # Simulate a logged-in administrator
        with self.client.session_transaction() as session:
            session["username"] = "test_user"
            session["role"] = "admin"

    def tearDown(self):
        # Restore the original application settings
        app.config["TESTING"] = self.old_testing
        app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def test_logout_get_not_allowed(self):
        # Logout must not accept GET requests
        response = self.client.get("/logout")

        self.assertEqual(response.status_code, 405)

    def test_logout_without_csrf_rejected(self):
        # Logout without a token must be rejected
        response = self.client.post("/logout")

        self.assertEqual(response.status_code, 400)

        # User should remain logged in
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("username"), "test_user")

    def test_logout_with_invalid_csrf_rejected(self):
        # An incorrect token must be rejected
        response = self.client.post(
            "/logout",
            data={"csrf_token": "invalid-token"}
        )

        self.assertEqual(response.status_code, 400)

    def test_logout_with_valid_csrf(self):
        # Get a real CSRF token from a protected page
        response = self.client.get("/tickets/new")

        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)

        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            html
        )

        self.assertIsNotNone(match)

        token = match.group(1)

        # Submit logout using the valid token
        response = self.client.post(
            "/logout",
            data={"csrf_token": token}
        )

        # Successful logout redirects to login
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/login")
        )

        # Confirm the user session was cleared
        with self.client.session_transaction() as session:
            self.assertNotIn("username", session)
            self.assertNotIn("role", session)


if __name__ == "__main__":
    unittest.main()
