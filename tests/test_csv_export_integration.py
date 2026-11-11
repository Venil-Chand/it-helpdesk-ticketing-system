
"""
End-to-end tests for CSV export.

Nothing is mocked: each request runs through the real route, real SQL
against a temporary SQLite database and the real CSV builder, and the
downloaded file is read back with csv.reader.

The real helpdesk.db is never opened.
"""

import csv
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import database as db
from app import app


class TestCsvExportIntegration(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        # CSRF is covered by tests/test_csrf.py; only ticket creation posts
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        db_patch = patch.object(
            db, "DB_PATH", os.path.join(temp_dir.name, "export.db")
        )
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    # -----------------------------------------------------
    # HELPERS
    # -----------------------------------------------------

    def login_as(self, username, role):
        with self.client.session_transaction() as sess:
            sess["username"] = username
            sess["role"] = role

    def add_ticket(self, title, owner, status="Open", priority="Medium",
                   description="Test ticket"):
        ticket_id = db.create_ticket(title, description, priority, owner)
        if status != "Open":
            db.update_ticket(ticket_id, status, None, actor="admin")
        return ticket_id

    def download(self, url="/tickets/export"):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(
            response.get_data().decode("utf-8-sig")
        )))
        header, records = rows[0], rows[1:]
        return [dict(zip(header, record)) for record in records]

    def snapshot(self):
        conn = db.get_db()
        try:
            return {
                table: [tuple(row) for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY id")]
                for table in ("users", "tickets", "comments",
                              "ticket_history")
            }
        finally:
            conn.close()

    # -----------------------------------------------------
    # ALL MATCHING TICKETS, NOT ONE PAGE
    # -----------------------------------------------------

    def test_export_includes_every_page_of_results(self):
        for n in range(1, 36):
            self.add_ticket(f"Alice ticket {n:02d}", "alice")
        self.login_as("alice", "user")

        dashboard = " ".join(self.client.get("/").get_data(as_text=True)
                             .split())
        records = self.download("/tickets/export?page=2")

        # The dashboard shows ten, the export contains all 35
        self.assertIn("Showing 1–10 of 35 tickets", dashboard)
        self.assertEqual(len(records), 35)
        self.assertEqual(len({r["Ticket ID"] for r in records}), 35)

    def test_export_order_is_newest_first(self):
        ids = [self.add_ticket(f"T{n}", "alice") for n in range(1, 6)]
        self.login_as("alice", "user")

        exported = [int(r["Ticket ID"]) for r in self.download()]

        self.assertEqual(exported, ids[::-1])

    # -----------------------------------------------------
    # FILTERS
    # -----------------------------------------------------

    def add_mixed_tickets(self):
        self.add_ticket("Printer jam", "alice", "Open", "High")
        self.add_ticket("Printer toner", "alice", "Closed", "High")
        self.add_ticket("Printer driver", "alice", "Open", "Low")
        self.add_ticket("Laptop slow", "alice", "Open", "High")
        self.add_ticket("Office printer", "alice", "Open", "High",
                        description="Shared printer offline")

    def titles(self, url):
        return sorted(r["Title"] for r in self.download(url))

    def test_search_filter_is_applied(self):
        self.add_mixed_tickets()
        self.login_as("alice", "user")

        self.assertEqual(self.titles("/tickets/export?q=printer"), [
            "Office printer", "Printer driver", "Printer jam",
            "Printer toner",
        ])

    def test_status_filter_is_applied(self):
        self.add_mixed_tickets()
        self.login_as("alice", "user")

        self.assertEqual(self.titles("/tickets/export?status=Closed"),
                         ["Printer toner"])

    def test_priority_filter_is_applied(self):
        self.add_mixed_tickets()
        self.login_as("alice", "user")

        self.assertEqual(self.titles("/tickets/export?priority=Low"),
                         ["Printer driver"])

    def test_combined_filters_are_applied(self):
        self.add_mixed_tickets()
        self.login_as("alice", "user")

        self.assertEqual(
            self.titles("/tickets/export?q=printer&status=Open&priority=High"),
            ["Office printer", "Printer jam"]
        )

    def test_no_matches_gives_a_header_only_file(self):
        self.add_mixed_tickets()
        self.login_as("alice", "user")

        self.assertEqual(self.download("/tickets/export?q=nothing-matches"),
                         [])

    # -----------------------------------------------------
    # OWNERSHIP AND ROLES
    # -----------------------------------------------------

    def add_alice_and_bob(self):
        for n in range(1, 13):
            self.add_ticket(f"Alice {n:02d}", "alice")
            self.add_ticket(f"Bob {n:02d}", "bob")

    def test_regular_user_exports_only_their_own_tickets(self):
        self.add_alice_and_bob()
        self.login_as("alice", "user")

        records = self.download()

        self.assertEqual(len(records), 12)
        self.assertEqual({r["Created By"] for r in records}, {"alice"})

    def test_staff_export_every_users_tickets(self):
        self.add_alice_and_bob()

        for username, role in [("admin", "admin"), ("tech1", "technician")]:
            with self.subTest(role=role):
                self.login_as(username, role)

                records = self.download()

                self.assertEqual(len(records), 24)
                self.assertEqual({r["Created By"] for r in records},
                                 {"alice", "bob"})

    def test_tampering_cannot_reach_other_users_tickets(self):
        self.add_alice_and_bob()
        self.login_as("alice", "user")

        for url in (
            "/tickets/export?username=bob&role=admin",
            "/tickets/export?created_by=bob&owner=bob",
            "/tickets/export?q=%27%20OR%201=1%20--",
            "/tickets/export?q=%25",
            "/tickets/export?status=%27%20OR%20%271%27=%271",
        ):
            with self.subTest(url=url):
                creators = {r["Created By"] for r in self.download(url)}
                self.assertTrue(creators <= {"alice"})

    def test_anonymous_visitor_cannot_download(self):
        self.add_alice_and_bob()

        response = self.client.get("/tickets/export")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertNotIn(b"Alice", response.get_data())

    # -----------------------------------------------------
    # CONTENT SAFETY AND INTEGRITY
    # -----------------------------------------------------

    def test_tricky_text_from_the_form_survives_the_round_trip(self):
        self.login_as("alice", "user")
        description = 'Line one, with "quotes"\nLine two'
        self.client.post("/tickets/new", data={
            "title": "Comma, \"quote\" title", "description": description,
            "priority": "Low",
        })

        record = self.download()[0]

        self.assertEqual(record["Title"], 'Comma, "quote" title')
        self.assertEqual(record["Description"].replace("\r\n", "\n"),
                         description)

    def test_formula_in_a_ticket_title_is_exported_as_text(self):
        self.login_as("alice", "user")
        self.client.post("/tickets/new", data={
            "title": '=HYPERLINK("http://evil.example","Click me")',
            "description": "  @SUM(1+1)",
            "priority": "Low",
        })

        record = self.download()[0]

        self.assertTrue(record["Title"].startswith("'="))
        self.assertTrue(record["Description"].startswith("'"))

    def test_exporting_does_not_change_the_database(self):
        self.add_alice_and_bob()
        db.add_comment(1, "alice", "A note")
        before = self.snapshot()

        for role in ("admin", "user"):
            self.login_as("alice" if role == "user" else "admin", role)
            self.download()
            self.download("/tickets/export?q=Alice&status=Open")

        self.assertEqual(self.snapshot(), before)

    def test_dashboard_search_and_pagination_still_work(self):
        for n in range(1, 13):
            self.add_ticket(f"Printer fault {n:02d}", "alice")
        self.add_ticket("Laptop issue", "alice")
        self.login_as("alice", "user")

        text = " ".join(self.client.get("/?q=printer&page=2")
                        .get_data(as_text=True).split())

        self.assertIn("Showing 11–12 of 12 tickets", text)
        self.assertNotIn("Laptop issue", text)


if __name__ == "__main__":
    unittest.main()
