
"""
Flask tests for the /tickets/export CSV download.

The database is mocked so these tests check only the route: who may
export, which filters reach the query, and the HTTP response. DB_PATH
points at a location that cannot be opened, so an unmocked query fails
instead of reading the real helpdesk.db.
"""

import csv
import io
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import database as db
from app import app


TICKET = {
    "id": 7, "title": "Printer offline", "description": "Paper jam",
    "priority": "High", "status": "Open", "assigned_to": "tech1",
    "created_by": "alice", "created_at": "2026-11-10 07:00:00",
    "updated_at": "2026-11-10 07:30:00",
}


class TestCsvExportRoute(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        app.config["TESTING"] = True
        self.client = app.test_client()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_guard = patch.object(
            db, "DB_PATH",
            os.path.join(temp_dir.name, "missing", "helpdesk.db")
        )
        db_guard.start()
        self.addCleanup(db_guard.stop)

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def export(self, url="/tickets/export", tickets=(TICKET,)):
        """Request the export with the database query mocked."""
        with patch("app.db.search_tickets",
                   return_value=list(tickets)) as mock_search, \
                patch("app.db.get_tickets_page") as mock_page, \
                patch("app.db.count_tickets") as mock_count:
            response = self.client.get(url)

        # The export never uses the paginated dashboard queries
        mock_page.assert_not_called()
        mock_count.assert_not_called()

        return response, mock_search

    def rows(self, response):
        text = response.get_data().decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    # -----------------------------------------------------
    # WHO MAY EXPORT WHAT
    # -----------------------------------------------------

    def test_anonymous_visitor_is_sent_to_login(self):
        with patch("app.db.search_tickets") as mock_search:
            response = self.client.get("/tickets/export")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        mock_search.assert_not_called()

    def test_regular_user_export_is_restricted_to_own_tickets(self):
        self.login_as("alice", "user")

        response, mock_search = self.export()

        self.assertEqual(response.status_code, 200)
        mock_search.assert_called_once_with(
            search_text="", username="alice",
            status_filter="All", priority_filter="All"
        )

    def test_staff_export_is_not_restricted(self):
        for username, role in [("admin", "admin"), ("tech1", "technician")]:
            with self.subTest(role=role):
                self.login_as(username, role)

                _, mock_search = self.export()

                self.assertIsNone(mock_search.call_args.kwargs["username"])

    def test_url_parameters_cannot_change_identity_or_role(self):
        self.login_as("alice", "user")

        _, mock_search = self.export(
            "/tickets/export?username=bob&role=admin&created_by=bob&owner=bob"
        )

        self.assertEqual(mock_search.call_args.kwargs["username"], "alice")

    # -----------------------------------------------------
    # FILTERS
    # -----------------------------------------------------

    def test_search_and_filters_reach_the_query_but_page_does_not(self):
        self.login_as("alice", "user")

        _, mock_search = self.export(
            "/tickets/export?q=printer&status=Open&priority=High&page=2"
        )

        mock_search.assert_called_once_with(
            search_text="printer", username="alice",
            status_filter="Open", priority_filter="High"
        )

    def test_overlong_search_is_truncated_like_the_dashboard(self):
        self.login_as("alice", "user")

        _, mock_search = self.export("/tickets/export?q=" + "a" * 500)

        self.assertEqual(mock_search.call_args.kwargs["search_text"],
                         "a" * 100)

    # -----------------------------------------------------
    # THE DOWNLOAD ITSELF
    # -----------------------------------------------------

    def test_response_is_a_csv_attachment(self):
        self.login_as("alice", "user")

        response, _ = self.export()

        self.assertEqual(response.headers["Content-Type"],
                         "text/csv; charset=utf-8")
        self.assertTrue(response.headers["Content-Disposition"]
                        .startswith("attachment;"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"],
                         "nosniff")

    def test_filename_is_generated_by_the_server(self):
        self.login_as("alice", "user")

        response, _ = self.export(
            '/tickets/export?filename=../../etc/passwd&q=x"; evil'
        )

        self.assertRegex(
            response.headers["Content-Disposition"],
            r'^attachment; filename="helpdesk-tickets-\d{8}-\d{6}\.csv"$'
        )

    def test_body_starts_with_a_byte_order_mark_for_excel(self):
        self.login_as("alice", "user")

        response, _ = self.export()

        self.assertTrue(response.get_data().startswith(b"\xef\xbb\xbf"))

    def test_body_contains_the_tickets(self):
        self.login_as("alice", "user")

        response, _ = self.export()
        rows = self.rows(response)

        self.assertEqual(rows[0][0], "Ticket ID")
        self.assertEqual(rows[1][:2], ["7", "Printer offline"])

    def test_no_matching_tickets_gives_a_header_only_file(self):
        self.login_as("alice", "user")

        response, _ = self.export(tickets=[])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows(response)), 1)

    def test_formula_in_a_ticket_is_neutralised_in_the_download(self):
        self.login_as("alice", "user")

        response, _ = self.export(tickets=[dict(TICKET, title="=1+1")])

        self.assertEqual(self.rows(response)[1][1], "'=1+1")
        self.assertIsNone(re.search(r"(^|,)=1\+1", response.get_data(
            as_text=True)))


if __name__ == "__main__":
    unittest.main()
