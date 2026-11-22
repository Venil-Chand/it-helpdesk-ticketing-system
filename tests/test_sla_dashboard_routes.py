"""
End-to-end tests for the SLA dashboard.

Nothing in the database layer is mocked. Each request goes through the
real Flask route, the real SQL against a temporary SQLite database and
the real template, and the tests read the rendered HTML or CSV.

The route's clock is frozen at NOW by replacing app.datetime, so SLA
statuses are exact and never depend on when the tests run.

The real helpdesk.db is never opened.
"""

import csv
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from werkzeug.security import generate_password_hash

import database as db
from app import app
from tests.auth_helpers import sign_in


NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)

USERS = [
    ("admin", "admin"),
    ("tech1", "technician"),
    ("alice", "user"),
    ("bob", "user"),
]

PASSWORD = "Correct-Horse-7"

CARD_KEYS = ["within", "due_soon", "overdue", "met", "breached", "not_tracked"]


class FrozenDateTime(datetime):
    """datetime whose now() always returns NOW."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class SlaRouteTestCase(unittest.TestCase):

    def setUp(self):
        self.old_testing = app.config["TESTING"]
        self.old_csrf = app.config.get("WTF_CSRF_ENABLED")
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False

        self.client = app.test_client()

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "sla_routes.db")

        for target in (
            patch.object(db, "DB_PATH", self.db_path),
            patch("app.datetime", FrozenDateTime),
        ):
            target.start()
            self.addCleanup(target.stop)

        db.init_db()

        with self.connect() as conn:
            for username, role in USERS:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) "
                    "VALUES (?, ?, ?)",
                    (username, generate_password_hash(PASSWORD), role)
                )

        self.minute = 0

    def tearDown(self):
        app.config["TESTING"] = self.old_testing

        if self.old_csrf is None:
            app.config.pop("WTF_CSRF_ENABLED", None)
        else:
            app.config["WTF_CSRF_ENABLED"] = self.old_csrf

    # -----------------------------------------------------
    # HELPERS
    # -----------------------------------------------------

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def login_as(self, username, role):
        sign_in(self, self.client, username, role)

    def add_ticket(
        self,
        title="Ticket",
        *,
        created_by="alice",
        priority="High",
        status="Open",
        due=None,
        responded=None,
        sla=True
    ):
        """Insert a ticket (newer than the previous one) with an SLA row."""
        self.minute += 1
        created_at = (NOW - timedelta(hours=3)
                      + timedelta(minutes=self.minute))

        with self.connect() as conn:
            ticket_id = conn.execute(
                "INSERT INTO tickets (title, description, priority, status, "
                "created_by, created_at) VALUES (?, 'Details', ?, ?, ?, ?)",
                (title, priority, status, created_by,
                 created_at.strftime("%Y-%m-%d %H:%M:%S"))
            ).lastrowid

            if sla:
                conn.execute(
                    "INSERT INTO ticket_sla "
                    "(ticket_id, response_due_at, first_response_at) "
                    "VALUES (?, ?, ?)",
                    (ticket_id,
                     (due or NOW + timedelta(hours=3)).isoformat(),
                     responded.isoformat() if responded else None)
                )

        return ticket_id

    def add_one_of_each(self, created_by, prefix=""):
        """One ticket in every SLA category; returns {key: ticket_id}."""
        return {
            "within": self.add_ticket(
                f"{prefix}Within", created_by=created_by,
                due=NOW + timedelta(hours=2)),
            "due_soon": self.add_ticket(
                f"{prefix}DueSoon", created_by=created_by,
                due=NOW + timedelta(minutes=20)),
            "overdue": self.add_ticket(
                f"{prefix}Overdue", created_by=created_by,
                due=NOW - timedelta(minutes=20)),
            "met": self.add_ticket(
                f"{prefix}Met", created_by=created_by,
                due=NOW - timedelta(hours=1),
                responded=NOW - timedelta(hours=2)),
            "breached": self.add_ticket(
                f"{prefix}Breached", created_by=created_by,
                due=NOW - timedelta(hours=2),
                responded=NOW - timedelta(hours=1)),
            "not_tracked": self.add_ticket(
                f"{prefix}Historic", created_by=created_by, sla=False),
        }

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.get_data(as_text=True)

    def cards(self, html):
        """The six SLA card counts, {key: count}."""
        found = dict(re.findall(
            r'data-sla-card="(\w+)".*?<div class="sla-num[^"]*">(\d+)</div>',
            html, re.S
        ))
        return {key: int(value) for key, value in found.items()}

    def row_ids(self, html):
        return [int(n) for n in re.findall(
            r'href="/tickets/(\d+)"', html)]

    def row_statuses(self, html):
        """{ticket_id: SLA status} for every row shown."""
        return {
            int(ticket_id): status
            for ticket_id, status in re.findall(
                r'href="/tickets/(\d+)".*?data-sla-status="([^"]*)"',
                html, re.S
            )
        }

    def total(self, html):
        match = re.search(r"of (\d+)\s+tickets?", html)
        return int(match.group(1)) if match else 0


# ---------------------------------------------------
# 1. SUMMARY STATISTICS
# ---------------------------------------------------

class TestSlaSummaryCards(SlaRouteTestCase):

    def test_cards_count_all_six_categories(self):
        self.login_as("admin", "admin")
        self.add_one_of_each("alice")
        self.add_ticket(created_by="bob", due=NOW - timedelta(hours=5))

        html = self.get("/")

        self.assertEqual(self.cards(html), {
            "within": 1, "due_soon": 1, "overdue": 2,
            "met": 1, "breached": 1, "not_tracked": 1,
        })

    def test_cards_cover_every_page_not_just_the_current_one(self):
        self.login_as("admin", "admin")
        for _ in range(25):
            self.add_ticket(due=NOW - timedelta(hours=1))

        html = self.get("/?page=3")

        self.assertEqual(len(self.row_ids(html)), 5)
        self.assertEqual(self.cards(html)["overdue"], 25)

    def test_cards_ignore_search_and_filters(self):
        # Like the existing Total/Open cards, the SLA cards describe
        # everything the user may see
        self.login_as("admin", "admin")
        self.add_one_of_each("alice")

        html = self.get("/?q=Met&priority=Low&sla=overdue")

        self.assertEqual(sum(self.cards(html).values()), 6)

    def test_evaluation_time_is_shown(self):
        self.login_as("alice", "user")

        self.assertIn("2026-11-18 12:00 UTC", self.get("/"))

    def test_card_links_apply_the_filter_and_keep_others(self):
        self.login_as("admin", "admin")
        self.add_ticket()

        html = self.get("/?q=printer&priority=High&page=1")

        hrefs = dict(re.findall(
            r'href="([^"]+)"\s+class="card sla-card[^"]*"\s+'
            r'data-sla-card="(\w+)"', html
        ))
        by_key = {key: unescape(href) for href, key in hrefs.items()}

        self.assertEqual(sorted(by_key), sorted(CARD_KEYS))

        query = parse_qs(urlparse(by_key["overdue"]).query)
        self.assertEqual(query, {
            "q": ["printer"], "priority": ["High"], "sla": ["overdue"]
        })

    def test_selected_card_is_marked(self):
        self.login_as("alice", "user")

        html = self.get("/?sla=breached")

        self.assertRegex(
            html,
            r'data-sla-card="breached"\s+aria-label="[^"]*"\s+'
            r'aria-current="true"'
        )
        self.assertEqual(html.count('aria-current="true"'), 1)


# ---------------------------------------------------
# 2-3. OWNERSHIP AND STAFF ACCESS
# ---------------------------------------------------

class TestSlaAuthorization(SlaRouteTestCase):

    def setUp(self):
        super().setUp()
        self.alice = self.add_one_of_each("alice", "Alice-")
        self.bob = self.add_one_of_each("bob", "Bob-")
        self.add_ticket("Bob-Overdue-2", created_by="bob",
                        due=NOW - timedelta(hours=8))

    def test_regular_user_cards_count_only_their_tickets(self):
        self.login_as("alice", "user")

        self.assertEqual(self.cards(self.get("/")),
                         dict.fromkeys(CARD_KEYS, 1))

    def test_regular_user_sla_filter_never_shows_others_tickets(self):
        self.login_as("alice", "user")

        for key in CARD_KEYS:
            with self.subTest(sla=key):
                html = self.get(f"/?sla={key}")

                self.assertEqual(self.row_ids(html), [self.alice[key]])
                self.assertNotIn("Bob-", html)
                self.assertEqual(self.total(html), 1)

    def test_staff_see_every_ticket(self):
        for username, role in (("tech1", "technician"), ("admin", "admin")):
            with self.subTest(role=role):
                self.login_as(username, role)
                html = self.get("/?sla=overdue")

                self.assertEqual(
                    self.cards(html),
                    {**dict.fromkeys(CARD_KEYS, 2), "overdue": 3}
                )
                self.assertEqual(self.total(html), 3)
                self.assertIn("Bob-Overdue-2", html)

    def test_row_sla_details_follow_ticket_access(self):
        # Alice's dashboard holds no row, deadline or count for Bob
        self.login_as("alice", "user")
        html = self.get("/")

        for ticket_id in self.bob.values():
            self.assertNotIn(f'href="/tickets/{ticket_id}"', html)

        # ...and Bob's ticket page (with its SLA) is still a 404
        response = self.client.get(f"/tickets/{self.bob['overdue']}")
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------
# 4-5. FILTER COMBINATIONS AND PAGINATION
# ---------------------------------------------------

class TestSlaFilterRoute(SlaRouteTestCase):

    def test_sla_filter_with_search_priority_and_status(self):
        self.login_as("admin", "admin")
        match = self.add_ticket("Printer jam", priority="High",
                                due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="Low",
                        due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="High",
                        due=NOW + timedelta(hours=1))
        self.add_ticket("Laptop", priority="High",
                        due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="High", status="Closed",
                        due=NOW - timedelta(hours=1))

        html = self.get(
            "/?q=printer&priority=High&status=Open&sla=overdue"
        )

        self.assertEqual(self.row_ids(html), [match])
        self.assertEqual(self.total(html), 1)

    def test_filter_controls_show_the_selection(self):
        self.login_as("alice", "user")
        html = self.get("/?sla=due_soon")

        self.assertRegex(
            html, r'<option\s+value="due_soon"\s+selected\s*>'
        )
        self.assertIn('for="sla-filter"', html)  # labelled control
        self.assertIn("Clear", html)

    def test_pagination_counts_and_links_after_filtering(self):
        self.login_as("tech1", "technician")
        overdue = []
        for i in range(53):
            if i % 7 < 3:
                overdue.append(self.add_ticket(
                    f"Late {i}", due=NOW - timedelta(minutes=i + 1)))
            else:
                self.add_ticket(f"Fine {i}", due=NOW + timedelta(hours=2))

        seen = []
        url = "/?sla=overdue"

        # Follow the Next links to the end
        while url:
            html = self.get(url)

            self.assertEqual(self.total(html), len(overdue))
            seen += self.row_ids(html)

            next_link = re.search(
                r'href="([^"]+)"\s+aria-label="Next page"', html
            )
            if next_link:
                href = unescape(next_link.group(1))
                self.assertEqual(
                    parse_qs(urlparse(href).query)["sla"], ["overdue"]
                )
                url = href
            else:
                url = None

        self.assertEqual(seen, sorted(overdue, reverse=True))

    def test_page_past_the_end_keeps_the_sla_filter(self):
        self.login_as("admin", "admin")
        for _ in range(12):
            self.add_ticket(due=NOW - timedelta(hours=1))
        self.add_ticket(due=NOW + timedelta(hours=1))

        response = self.client.get("/?sla=overdue&page=9")

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query, {"page": ["2"], "sla": ["overdue"]})

    def test_empty_filter_result_says_so(self):
        self.login_as("alice", "user")
        self.add_ticket(due=NOW + timedelta(hours=3))

        html = self.get("/?sla=breached")

        self.assertIn("No tickets match the selected filters", html)


# ---------------------------------------------------
# 6-8. DEADLINES, RESPONSES, HISTORICAL TICKETS
# ---------------------------------------------------

class TestSlaRowDisplay(SlaRouteTestCase):

    def test_deadline_boundaries_in_rows(self):
        self.login_as("admin", "admin")
        at_deadline = self.add_ticket(due=NOW)
        at_window = self.add_ticket(due=NOW + timedelta(minutes=30))
        outside = self.add_ticket(due=NOW + timedelta(minutes=30, seconds=1))
        met_at_deadline = self.add_ticket(due=NOW - timedelta(hours=1),
                                          responded=NOW - timedelta(hours=1))

        statuses = self.row_statuses(self.get("/"))

        self.assertEqual(statuses[at_deadline], "Overdue")
        self.assertEqual(statuses[at_window], "Due Soon")
        self.assertEqual(statuses[outside], "Within SLA")
        self.assertEqual(statuses[met_at_deadline], "Met")

    def test_overdue_and_due_soon_rows_are_highlighted(self):
        self.login_as("admin", "admin")
        self.add_ticket("Late", due=NOW - timedelta(minutes=1))
        self.add_ticket("Soon", due=NOW + timedelta(minutes=5))
        self.add_ticket("Fine", due=NOW + timedelta(hours=5))

        html = self.get("/")

        self.assertEqual(html.count('class="ticket-row sla-row-overdue"'), 1)
        self.assertEqual(html.count('class="ticket-row sla-row-due-soon"'), 1)
        self.assertEqual(html.count('class="ticket-row"'), 1)

    def test_row_shows_deadline_and_first_response_in_utc(self):
        self.login_as("admin", "admin")
        self.add_ticket(due=NOW + timedelta(hours=1),
                        responded=NOW - timedelta(minutes=15))

        html = self.get("/")

        self.assertRegex(html, r"Due\s*<time datetime=\"2026-11-18T13:00:00"
                               r"\+00:00\">\s*2026-11-18 13:00 UTC")
        self.assertRegex(html, r"Responded\s*<time[^>]*>\s*"
                               r"2026-11-18 11:45 UTC")

    def test_offset_timestamp_is_displayed_in_utc(self):
        self.login_as("admin", "admin")
        ticket_id = self.add_ticket()
        with self.connect() as conn:
            conn.execute(
                "UPDATE ticket_sla SET response_due_at = "
                "'2026-11-18T19:30:00+05:30' WHERE ticket_id = ?",
                (ticket_id,)
            )

        html = self.get("/")

        self.assertIn("2026-11-18 14:00 UTC", html)
        self.assertNotIn("2026-11-18 19:30 UTC", html)

    def comment(self, ticket_id, username, role):
        self.login_as(username, role)
        response = self.client.post(
            f"/tickets/{ticket_id}/comment", data={"content": "On it"}
        )
        self.assertEqual(response.status_code, 302)

    def set_due(self, ticket_id, due):
        with self.connect() as conn:
            conn.execute(
                "UPDATE ticket_sla SET response_due_at = ? "
                "WHERE ticket_id = ?", (due, ticket_id)
            )

    def real_now_status(self, ticket_id):
        """Dashboard status using the real clock (comments use it too)."""
        with patch("app.datetime", datetime):
            self.login_as("admin", "admin")
            return self.row_statuses(self.get("/"))[ticket_id]

    def test_first_response_before_the_deadline_is_met(self):
        ticket_id = db.create_ticket("VPN", "Down", "High", "alice")

        self.comment(ticket_id, "tech1", "technician")

        self.assertEqual(self.real_now_status(ticket_id), "Met")

    def test_first_response_after_the_deadline_is_breached(self):
        ticket_id = db.create_ticket("VPN", "Down", "High", "alice")
        self.set_due(ticket_id, "2000-01-01T00:00:00+00:00")

        self.assertEqual(self.real_now_status(ticket_id), "Overdue")

        self.comment(ticket_id, "tech1", "technician")

        self.assertEqual(self.real_now_status(ticket_id), "Breached")

    def test_requester_comment_does_not_stop_the_clock(self):
        ticket_id = db.create_ticket("VPN", "Down", "High", "alice")
        self.set_due(ticket_id, "2000-01-01T00:00:00+00:00")

        self.comment(ticket_id, "alice", "user")

        self.assertEqual(self.real_now_status(ticket_id), "Overdue")

    def test_historical_ticket_is_not_tracked(self):
        self.login_as("alice", "user")
        ticket_id = self.add_ticket("Legacy", sla=False)

        html = self.get("/")

        self.assertEqual(self.row_statuses(html)[ticket_id], "Not Tracked")
        self.assertEqual(self.cards(html)["not_tracked"], 1)
        self.assertEqual(self.cards(html)["overdue"], 0)
        self.assertNotIn("Due\n", re.search(
            r'data-sla-status="Not Tracked">.*?</td>', html, re.S).group(0))


# ---------------------------------------------------
# 9. MALICIOUS PARAMETERS AND BYPASS ATTEMPTS
# ---------------------------------------------------

class TestSlaMaliciousInput(SlaRouteTestCase):

    def setUp(self):
        super().setUp()
        self.alice = self.add_one_of_each("alice", "Alice-")
        self.bob = self.add_one_of_each("bob", "Bob-")

    def test_unknown_sla_values_are_ignored_safely(self):
        self.login_as("alice", "user")

        for value in (
            "' OR 1=1 --",
            "overdue' UNION SELECT password_hash FROM users --",
            "Overdue",            # a label, not an allowed URL value
            "OVERDUE",
            "<script>alert(1)</script>",
            "%00",
            "a" * 5000,
        ):
            with self.subTest(value=value[:40]):
                response = self.client.get("/", query_string={"sla": value})
                html = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 200)
                # Treated as "no SLA filter": Alice's six tickets only
                self.assertEqual(self.total(html), 6)
                self.assertNotIn("Bob-", html)
                self.assertNotIn("<script>alert(1)</script>", html)
                self.assertNotIn("pbkdf2", html)
                self.assertNotIn("scrypt", html)

    def test_identity_parameters_cannot_widen_sla_results(self):
        self.login_as("alice", "user")

        html = self.get(
            "/?sla=overdue&username=bob&role=admin&created_by=bob&owner=bob"
        )

        self.assertEqual(self.row_ids(html), [self.alice["overdue"]])
        self.assertEqual(self.cards(html), dict.fromkeys(CARD_KEYS, 1))

    def test_repeated_sla_parameter_uses_only_the_first(self):
        self.login_as("alice", "user")

        html = self.get("/?sla=met&sla=overdue")

        self.assertEqual(self.row_ids(html), [self.alice["met"]])

    def test_bad_page_with_sla_filter_falls_back_safely(self):
        self.login_as("alice", "user")

        for page in ("-1", "abc", "1 OR 1=1", "0"):
            with self.subTest(page=page):
                html = self.get(f"/?sla=overdue&page={page}")
                self.assertEqual(self.row_ids(html), [self.alice["overdue"]])

    def test_anonymous_visitor_gets_no_sla_data(self):
        with patch("app.db.get_sla_stats") as mock_stats, \
                patch("app.db.count_tickets") as mock_count:
            response = self.client.get("/?sla=overdue")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        mock_stats.assert_not_called()
        mock_count.assert_not_called()

    def test_search_text_with_sql_wildcards_is_literal_enough(self):
        # LIKE wildcards in the search box can match more of your own
        # tickets, never someone else's
        self.login_as("alice", "user")

        html = self.get("/?q=%25&sla=overdue")

        self.assertEqual(self.row_ids(html), [self.alice["overdue"]])


# ---------------------------------------------------
# 10. CSV EXPORT, HISTORY AND AUTHENTICATION
# ---------------------------------------------------

class TestCsvExportWithSla(SlaRouteTestCase):

    HEADER = ["Ticket ID", "Title", "Description", "Priority", "Status",
              "Assigned To", "Created By", "Created (UTC)",
              "Last Updated (UTC)"]

    def setUp(self):
        super().setUp()
        self.alice = self.add_one_of_each("alice", "Alice-")
        self.bob = self.add_one_of_each("bob", "Bob-")

    def export(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(
            response.get_data().decode("utf-8-sig"))))
        return response, rows

    def test_export_without_sla_filter_is_unchanged(self):
        self.login_as("alice", "user")

        response, rows = self.export("/tickets/export")

        self.assertEqual(rows[0], self.HEADER)
        self.assertEqual(sorted(int(r[0]) for r in rows[1:]),
                         sorted(self.alice.values()))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("helpdesk-tickets-20261118-120000.csv",
                      response.headers["Content-Disposition"])

    def test_export_with_sla_filter_keeps_columns_and_ownership(self):
        self.login_as("alice", "user")

        _, rows = self.export("/tickets/export?sla=overdue")

        self.assertEqual(rows[0], self.HEADER)  # no SLA columns added
        self.assertEqual([int(r[0]) for r in rows[1:]],
                         [self.alice["overdue"]])

    def test_staff_export_with_sla_filter(self):
        self.login_as("tech1", "technician")

        _, rows = self.export("/tickets/export?sla=not_tracked")

        self.assertEqual(sorted(int(r[0]) for r in rows[1:]),
                         sorted([self.alice["not_tracked"],
                                 self.bob["not_tracked"]]))

    def test_export_with_unknown_sla_value_exports_all_permitted(self):
        self.login_as("alice", "user")

        _, rows = self.export("/tickets/export?sla=' OR 1=1 --&username=bob")

        self.assertEqual(len(rows) - 1, 6)
        self.assertNotIn("Bob-", "".join(",".join(r) for r in rows))

    def test_export_button_carries_the_sla_filter(self):
        self.login_as("alice", "user")

        html = self.get("/?sla=overdue&page=1")
        href = unescape(re.search(
            r'href="([^"]+)"\s+class="btn btn-outline-success', html
        ).group(1))

        self.assertEqual(urlparse(href).path, "/tickets/export")
        self.assertEqual(parse_qs(urlparse(href).query),
                         {"sla": ["overdue"]})


class TestExistingFeaturesStillWork(SlaRouteTestCase):

    def test_login_dashboard_and_logout(self):
        response = self.client.post(
            "/login",
            data={"username": "alice", "password": PASSWORD},
            environ_overrides={"REMOTE_ADDR": "198.51.100.77"}
        )
        self.assertEqual(response.status_code, 302)

        html = self.get("/")
        self.assertIn('data-testid="sla-summary"', html)

        self.client.post("/logout")
        response = self.client.get("/?sla=overdue")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_wrong_password_is_still_rejected(self):
        response = self.client.post(
            "/login",
            data={"username": "alice", "password": "wrong"},
            environ_overrides={"REMOTE_ADDR": "198.51.100.78"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid username or password", response.get_data(
            as_text=True))

    def test_comment_still_records_history_and_ticket_page_shows_sla(self):
        ticket_id = db.create_ticket("Mail", "Bounce", "Medium", "alice")

        self.login_as("tech1", "technician")
        self.client.post(f"/tickets/{ticket_id}/comment",
                         data={"content": "Looking"})

        actions = [r["action"] for r in self.connect().execute(
            "SELECT action FROM ticket_history WHERE ticket_id = ? "
            "ORDER BY id", (ticket_id,))]
        self.assertEqual(actions, ["created", "commented"])

        self.login_as("alice", "user")
        html = self.get(f"/tickets/{ticket_id}")
        self.assertIn('data-testid="response-sla"', html)


if __name__ == "__main__":
    unittest.main()
