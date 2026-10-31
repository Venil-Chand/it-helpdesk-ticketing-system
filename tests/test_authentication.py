
import unittest
from app import app


class TestAuthentication(unittest.TestCase):

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    # Test that the login page is accessible
    def test_login_page(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)

    # Test that the dashboard requires login
    def test_dashboard_requires_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    # Test that creating tickets requires login
    def test_new_ticket_requires_login(self):
        response = self.client.get("/tickets/new")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    # Test that ticket details require login
    def test_ticket_detail_requires_login(self):
        response = self.client.get("/tickets/1")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
