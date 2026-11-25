
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database
from app import app


class TestLoginRateLimit(unittest.TestCase):

    def setUp(self):
        # Save original application settings
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config["WTF_CSRF_ENABLED"]

        # Enable testing and CSRF protection
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = True

        # Create a test browser
        self.client = app.test_client()

        # Use a separate test IP address
        self.test_ip = "198.51.100.200"

        # Point the database at a folder that does not exist. Failed sign-ins
        # are recorded in the audit log, so without this the login attempts
        # below would be written to the real helpdesk.db when the suite is
        # run inside the project folder.
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        db_guard = patch.object(
            database, "DB_PATH",
            os.path.join(temp_dir.name, "missing", "helpdesk.db")
        )
        db_guard.start()
        self.addCleanup(db_guard.stop)

    def tearDown(self):
        # Restore application settings
        app.config["TESTING"] = self.old_testing
        app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    def test_login_rate_limit(self):

        # Visit the login page to obtain a valid CSRF token
        response = self.client.get(
            "/login",
            environ_overrides={"REMOTE_ADDR": self.test_ip}
        )

        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)

        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            html
        )

        self.assertIsNotNone(match)

        token = match.group(1)

        # Avoid accessing real user accounts
        with patch("app.db.get_user", return_value=None):

            # First five login submissions should be processed
            for attempt in range(5):

                response = self.client.post(
                    "/login",
                    data={
                        "username": "test_user",
                        "password": "dummy-password",
                        "csrf_token": token
                    },
                    environ_overrides={
                        "REMOTE_ADDR": self.test_ip
                    }
                )

                self.assertEqual(
                    response.status_code,
                    200,
                    f"Login attempt {attempt + 1} failed"
                )

            # Sixth submission should be rate limited
            response = self.client.post(
                "/login",
                data={
                    "username": "test_user",
                    "password": "dummy-password",
                    "csrf_token": token
                },
                environ_overrides={
                    "REMOTE_ADDR": self.test_ip
                }
            )

            self.assertEqual(response.status_code, 429)

        # GET requests should still allow viewing the login page
        response = self.client.get(
            "/login",
            environ_overrides={"REMOTE_ADDR": self.test_ip}
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
