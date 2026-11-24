"""
The response SLA policy, end to end, and agreement between every place
that shows an SLA.

Every test uses its own temporary database with the real migrations. The
route clock is frozen by replacing app.datetime, as in the SLA dashboard
tests, so the dashboard, the CSV export and the ticket page are all
evaluated at the same known moment. The real helpdesk.db is never opened.

The policy under test (calendar-hours-v1):
  - The deadline is the ticket's creation time plus 1/4/24/72 calendar
    hours for Critical/High/Medium/Low.
  - The first response is the first comment by an active technician or
    admin on a ticket someone else created. Assignment, status changes,
    resolution and closure are NOT responses.
  - The first response is recorded once and never moved, including when
    a ticket is reopened.
  - The SLA status depends only on the first response, not on whether
    the ticket is open or closed.
"""

import csv
import io
import itertools
import os
import random
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
import sla_storage
from app import app
from sla import SLA_TARGETS


NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
PASSWORD = "correct-horse-battery-staple"
FAST_HASH = "pbkdf2:sha256:1000"

_addresses = itertools.count(1)


class FrozenDateTime(datetime):
    """datetime whose now() always returns NOW."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class SlaPolicyTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "sla.db")

        for patcher in (
            patch.object(db, "DB_PATH", self.db_path),
            patch("app.datetime", FrozenDateTime),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        old_config = {
            key: app.config.get(key) for key in ("TESTING", "WTF_CSRF_ENABLED")
        }
        self.addCleanup(app.config.update, old_config)
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

        migrations.migrate(self.db_path)

        for username, role in (("alice", "user"), ("bob", "user"),
                               ("tech", "technician"), ("root", "admin")):
            self.sql(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES (?, ?, ?)",
                (username, generate_password_hash(PASSWORD, FAST_HASH), role)
            )

    # -----------------------------------------------
    # Helpers
    # -----------------------------------------------

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def sla_row(self, ticket_id):
        return self.sql(
            "SELECT response_due_at, first_response_at, policy_version "
            "FROM ticket_sla WHERE ticket_id = ?", (ticket_id,)
        )[0]

    def first_response(self, ticket_id):
        return self.sla_row(ticket_id)[1]

    def new_ticket(self, priority="High", owner="alice",
                   created_at=NOW - timedelta(hours=1)):
        """A ticket created at a known time, with its real SLA row."""

        ticket_id = db.create_ticket("Laptop", "Broken", priority, owner)

        stamp = created_at.strftime("%Y-%m-%d %H:%M:%S")
        due = created_at + timedelta(hours=SLA_TARGETS[priority])

        self.sql("UPDATE tickets SET created_at = ? WHERE id = ?",
                 (stamp, ticket_id))
        self.sql("UPDATE ticket_sla SET response_due_at = ? "
                 "WHERE ticket_id = ?", (due.isoformat(), ticket_id))
        return ticket_id

    def comment_at(self, ticket_id, author, moment):
        """Add a comment through the real code, stored at `moment`."""

        # Built from a datetime by strftime, so it is safe to put in SQL
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        real_get_db = db.get_db

        def get_db_with_clock():
            conn = real_get_db()
            # A TEMP trigger exists only on this one connection. It gives
            # the new comment the chosen time instead of CURRENT_TIMESTAMP
            # before add_comment() reads it back for the SLA.
            conn.execute(f"""
                CREATE TEMP TRIGGER comment_time AFTER INSERT ON comments
                BEGIN
                    UPDATE comments SET created_at = '{stamp}'
                    WHERE id = NEW.id;
                END
            """)
            return conn

        with patch.object(db, "get_db", get_db_with_clock):
            db.add_comment(ticket_id, author, "Reply")

    def client_for(self, username):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = f"10.35.{next(_addresses)}.1"
        client.post("/login",
                    data={"username": username, "password": PASSWORD})
        return client

    def dashboard_statuses(self, client):
        html = client.get("/?page=1").get_data(as_text=True)
        rows = re.findall(
            r'href="/tickets/(\d+)".*?data-sla-status="([^"]*)"', html, re.S
        )
        return {int(ticket): status for ticket, status in rows}

    def export_rows(self, client, query=""):
        body = client.get("/tickets/export" + query).data.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(body)))
        return {int(row["Ticket ID"]): row for row in rows}


# ---------------------------------------------------
# 1: DEADLINES
# ---------------------------------------------------

class TestDeadlines(SlaPolicyTestCase):

    def test_deadline_is_creation_plus_calendar_hours_for_each_priority(self):
        expected_hours = {"Critical": 1, "High": 4, "Medium": 24, "Low": 72}

        for priority, hours in expected_hours.items():
            with self.subTest(priority=priority):
                ticket_id = db.create_ticket("T", "D", priority, "alice")
                created = sla_storage.parse_stored_utc(
                    self.sql("SELECT created_at FROM tickets WHERE id = ?",
                             (ticket_id,))[0][0]
                )
                due, response, policy = self.sla_row(ticket_id)

                self.assertEqual(sla_storage.parse_stored_utc(due),
                                 created + timedelta(hours=hours))
                self.assertIsNone(response)
                self.assertEqual(policy, "calendar-hours-v1")

    def test_deadline_includes_nights_and_weekends(self):
        # Calendar hours: a Low ticket on Friday evening is due Monday
        # evening, with no pause for the weekend (no business hours yet)
        friday = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)

        due = sla_storage.calculate_deadline(friday, "Low")

        self.assertEqual(due, datetime(2026, 11, 23, 18, 0,
                                       tzinfo=timezone.utc))

    def test_changing_priority_or_status_never_moves_the_deadline(self):
        ticket_id = self.new_ticket("High")
        before = self.sla_row(ticket_id)

        db.update_ticket(ticket_id, "In Progress", "tech", actor="root")
        db.update_ticket(ticket_id, "Closed", "tech", actor="root")
        db.update_ticket(ticket_id, "Open", None, actor="root")

        self.assertEqual(self.sla_row(ticket_id), before)


# ---------------------------------------------------
# 2: WHAT COUNTS AS THE FIRST RESPONSE
# ---------------------------------------------------

class TestFirstResponse(SlaPolicyTestCase):

    def test_staff_comment_is_the_first_response_at_its_own_time(self):
        ticket_id = self.new_ticket()
        replied = NOW - timedelta(minutes=20)

        self.comment_at(ticket_id, "tech", replied)

        self.assertEqual(
            sla_storage.parse_stored_utc(self.first_response(ticket_id)),
            replied
        )

    def test_admin_comment_also_counts(self):
        ticket_id = self.new_ticket()

        db.add_comment(ticket_id, "root", "On it")

        self.assertIsNotNone(self.first_response(ticket_id))

    def test_assignment_alone_is_not_a_response(self):
        ticket_id = self.new_ticket()

        db.update_ticket(ticket_id, "Open", "tech", actor="root")
        db.update_ticket(ticket_id, "Open", "root", actor="root")

        self.assertIsNone(self.first_response(ticket_id))

    def test_status_changes_alone_are_not_a_response(self):
        ticket_id = self.new_ticket()

        for status in ("In Progress", "Resolved", "Closed"):
            with self.subTest(status=status):
                db.update_ticket(ticket_id, status, None, actor="tech")

                self.assertIsNone(self.first_response(ticket_id))

    def test_assignment_and_status_through_the_web_form_are_not_a_response(
            self):
        ticket_id = self.new_ticket()
        staff = self.client_for("tech")

        staff.post(f"/tickets/{ticket_id}/update",
                   data={"status": "Resolved", "assigned_to": "tech"})

        self.assertEqual(
            self.sql("SELECT status, assigned_to FROM tickets WHERE id = ?",
                     (ticket_id,)),
            [("Resolved", "tech")]
        )
        self.assertIsNone(self.first_response(ticket_id))

    def test_requester_comments_are_not_a_response(self):
        ticket_id = self.new_ticket(owner="alice")

        db.add_comment(ticket_id, "alice", "Any news?")

        self.assertIsNone(self.first_response(ticket_id))

    def test_staff_comment_on_their_own_ticket_is_not_a_response(self):
        ticket_id = self.new_ticket(owner="tech")

        db.add_comment(ticket_id, "tech", "Note to self")

        self.assertIsNone(self.first_response(ticket_id))

    def test_comment_from_an_unknown_or_ordinary_account_is_not_a_response(
            self):
        ticket_id = self.new_ticket(owner="alice")

        db.add_comment(ticket_id, "bob", "Me too")          # ordinary user
        db.add_comment(ticket_id, "ghost", "Who am I?")     # no account

        self.assertIsNone(self.first_response(ticket_id))

    def test_later_responses_never_replace_the_first(self):
        ticket_id = self.new_ticket()
        first = NOW - timedelta(minutes=30)

        self.comment_at(ticket_id, "tech", first)
        self.comment_at(ticket_id, "root", NOW - timedelta(minutes=5))

        self.assertEqual(
            sla_storage.parse_stored_utc(self.first_response(ticket_id)),
            first
        )

    def test_assignment_then_comment_uses_the_comment_time(self):
        ticket_id = self.new_ticket()
        db.update_ticket(ticket_id, "In Progress", "tech", actor="root")
        replied = NOW - timedelta(minutes=10)

        self.comment_at(ticket_id, "tech", replied)

        self.assertEqual(
            sla_storage.parse_stored_utc(self.first_response(ticket_id)),
            replied
        )


# ---------------------------------------------------
# 3: STATUS OVER A TICKET'S LIFE
# ---------------------------------------------------

class TestSlaLifecycle(SlaPolicyTestCase):

    def status(self, ticket_id, now=NOW):
        return db.get_ticket_sla_status(ticket_id, now=now)["status"]

    def test_unanswered_ticket_moves_from_within_to_overdue(self):
        ticket_id = self.new_ticket("High", created_at=NOW)
        due = NOW + timedelta(hours=4)

        self.assertEqual(self.status(ticket_id, NOW), "Within SLA")
        self.assertEqual(self.status(ticket_id, due - timedelta(minutes=30)),
                         "Due Soon")
        self.assertEqual(self.status(ticket_id, due), "Overdue")

    def test_response_on_time_is_met_and_late_is_breached(self):
        on_time = self.new_ticket("Critical", created_at=NOW - timedelta(
            hours=2))
        late = self.new_ticket("Critical", created_at=NOW - timedelta(
            hours=2))

        self.comment_at(on_time, "tech", NOW - timedelta(hours=1))  # = due
        self.comment_at(late, "tech", NOW - timedelta(minutes=59))

        self.assertEqual(self.status(on_time), "Met")
        self.assertEqual(self.status(late), "Breached")

    def test_reopened_ticket_keeps_its_first_response_and_outcome(self):
        ticket_id = self.new_ticket("High")
        self.comment_at(ticket_id, "tech", NOW - timedelta(minutes=30))
        db.update_ticket(ticket_id, "Resolved", "tech", actor="tech")
        before = self.sla_row(ticket_id)

        db.update_ticket(ticket_id, "Open", "tech", actor="root")  # reopen

        self.assertEqual(self.sla_row(ticket_id), before)
        self.assertEqual(self.status(ticket_id), "Met")

    def test_reopened_unanswered_ticket_is_still_waiting_for_a_response(self):
        ticket_id = self.new_ticket("High")
        db.update_ticket(ticket_id, "Closed", None, actor="tech")
        db.update_ticket(ticket_id, "Open", None, actor="root")

        self.assertIsNone(self.first_response(ticket_id))
        self.assertEqual(self.status(ticket_id), "Within SLA")

    def test_closed_without_any_response_still_reports_the_missed_sla(self):
        # Existing, documented policy: closing a ticket is not a response,
        # so a closed ticket nobody answered stays Overdue rather than
        # hiding the missed response
        ticket_id = self.new_ticket("Critical",
                                    created_at=NOW - timedelta(hours=3))
        db.update_ticket(ticket_id, "Closed", None, actor="tech")

        self.assertEqual(self.status(ticket_id), "Overdue")


# ---------------------------------------------------
# 4: DASHBOARD, TICKET PAGE AND CSV AGREE
# ---------------------------------------------------

class TestEveryViewAgrees(SlaPolicyTestCase):

    def setUp(self):
        super().setUp()

        hour = timedelta(hours=1)
        self.tickets = {
            "Within SLA": self.new_ticket("Medium", created_at=NOW - hour),
            "Due Soon": self.new_ticket(
                "Critical", created_at=NOW - timedelta(minutes=40)),
            "Overdue": self.new_ticket("Critical",
                                       created_at=NOW - 2 * hour),
            "Met": self.new_ticket("High", created_at=NOW - 2 * hour),
            "Breached": self.new_ticket("Critical",
                                        created_at=NOW - 3 * hour),
            "Not Tracked": self.new_ticket("Low"),
        }
        self.comment_at(self.tickets["Met"], "tech", NOW - hour)
        self.comment_at(self.tickets["Breached"], "tech", NOW - hour)
        self.sql("DELETE FROM ticket_sla WHERE ticket_id = ?",
                 (self.tickets["Not Tracked"],))

        # Closed without a response: every view must still say Overdue
        db.update_ticket(self.tickets["Overdue"], "Closed", None,
                         actor="tech")

    def test_dashboard_csv_and_ticket_page_show_the_same_status(self):
        staff = self.client_for("tech")
        dashboard = self.dashboard_statuses(staff)
        exported = self.export_rows(staff)

        for expected, ticket_id in self.tickets.items():
            with self.subTest(status=expected):
                python = db.get_ticket_sla_status(ticket_id, now=NOW)
                page = staff.get(f"/tickets/{ticket_id}").get_data(
                    as_text=True)

                self.assertEqual(dashboard[ticket_id], expected)
                self.assertEqual(exported[ticket_id]["SLA Status"], expected)

                if expected == "Not Tracked":
                    self.assertIsNone(python)
                    self.assertIn('data-testid="sla-not-tracked"', page)
                else:
                    self.assertEqual(python["status"], expected)
                    self.assertIn(expected, page)

    def test_summary_cards_count_the_same_tickets_the_export_lists(self):
        staff = self.client_for("tech")
        exported = self.export_rows(staff)

        counts = db.get_sla_stats(now=NOW)

        for label, count in counts.items():
            with self.subTest(label=label):
                self.assertEqual(
                    sum(1 for row in exported.values()
                        if row["SLA Status"] == label),
                    count
                )

    def test_sla_filtered_export_matches_the_status_column(self):
        staff = self.client_for("tech")

        for key, label in (("overdue", "Overdue"), ("met", "Met"),
                           ("not_tracked", "Not Tracked")):
            with self.subTest(filter=key):
                rows = self.export_rows(staff, f"?sla={key}")

                self.assertEqual(list(rows), [self.tickets[label]])
                self.assertEqual(rows[self.tickets[label]]["SLA Status"],
                                 label)

    def test_csv_times_match_the_stored_utc_times(self):
        staff = self.client_for("tech")
        met = self.export_rows(staff)[self.tickets["Met"]]
        due, responded, _ = self.sla_row(self.tickets["Met"])

        self.assertEqual(
            met["Response Due (UTC)"],
            sla_storage.parse_stored_utc(due).strftime("%Y-%m-%d %H:%M:%S")
        )
        self.assertEqual(
            met["First Response (UTC)"],
            sla_storage.parse_stored_utc(responded).strftime(
                "%Y-%m-%d %H:%M:%S")
        )

    def test_not_tracked_and_unanswered_tickets_have_empty_csv_times(self):
        rows = self.export_rows(self.client_for("tech"))

        untracked = rows[self.tickets["Not Tracked"]]
        self.assertEqual(untracked["Response Due (UTC)"], "")
        self.assertEqual(untracked["First Response (UTC)"], "")

        unanswered = rows[self.tickets["Within SLA"]]
        self.assertNotEqual(unanswered["Response Due (UTC)"], "")
        self.assertEqual(unanswered["First Response (UTC)"], "")

    def test_regular_user_export_contains_only_their_own_sla_rows(self):
        bob_ticket = self.new_ticket("High", owner="bob")

        rows = self.export_rows(self.client_for("bob"))

        self.assertEqual(list(rows), [bob_ticket])


class TestTimeZonesOnEveryView(SlaPolicyTestCase):

    def test_deadline_stored_with_an_offset_is_shown_in_utc_everywhere(self):
        # 15:00 at +05:30 is 09:30 UTC. The ticket page used to cut the
        # raw text and show "15:00 UTC", disagreeing with the dashboard.
        ticket_id = self.new_ticket("Low")
        self.sql("UPDATE ticket_sla SET response_due_at = ? "
                 "WHERE ticket_id = ?",
                 ("2026-11-19T15:00:00+05:30", ticket_id))
        staff = self.client_for("tech")

        page = staff.get(f"/tickets/{ticket_id}").get_data(as_text=True)
        dashboard = staff.get("/").get_data(as_text=True)
        exported = self.export_rows(staff)[ticket_id]

        self.assertIn("2026-11-19 09:30 UTC", page)
        self.assertNotIn("2026-11-19 15:00 UTC", page)
        self.assertIn("2026-11-19 09:30 UTC", dashboard)
        self.assertEqual(exported["Response Due (UTC)"],
                         "2026-11-19 09:30:00")


# ---------------------------------------------------
# 5: ONE STORED TIMESTAMP FORMAT FOR PYTHON AND SQL
# ---------------------------------------------------

class TestStoredFormatAgreement(SlaPolicyTestCase):
    """The ticket page (Python) and dashboard (SQL) read the same values."""

    def classify(self, value):
        """(SQL status, Python status or None) for a stored deadline."""

        ticket_id = self.new_ticket("Low")
        self.sql("UPDATE ticket_sla SET response_due_at = ? "
                 "WHERE ticket_id = ?", (value, ticket_id))

        sql_status = {
            row["id"]: row["sla_status"]
            for row in db.search_tickets(include_sla=True, now=NOW)
        }[ticket_id]

        python = db.get_ticket_sla_status(ticket_id, now=NOW)

        return sql_status, python and python["status"]

    def test_formats_that_used_to_disagree_are_now_unreadable_on_both(self):
        # Before this change Python accepted all but the last of these and
        # SQL accepted only the last, so the two pages disagreed
        for value in ("20261117T090000", "2026-W47-2", "2026-11-17T09",
                      "2026-11-17T09:00:00+0530", "2026-11-17T09:00:00+05",
                      "2026-11-17T09:00:00 ", "2026-02-30 09:00:00",
                      "2026-11-31T09:00:00", "0000-01-01 00:00:00",
                      "2026-11-17 24:00:00", "2026-11-17T09:00:00+15:00",
                      "2026-11-17T09:00:00.1234",
                      "9999-12-31T23:00:00-05:00"):
            with self.subTest(value=value):
                self.assertEqual(self.classify(value), ("Not Tracked", None))

    def test_supported_formats_are_readable_on_both(self):
        for value in ("2026-11-19 09:00:00", "2026-11-19T09:00:00",
                      "2026-11-19T09:00:00Z", "2026-11-19T09:00:00+00:00",
                      "2026-11-19T14:30:00+05:30", "2026-11-19T04:00:00-05:00",
                      "2026-11-19T09:00:00.5", "2026-11-19T09:00:00.123Z",
                      "2026-11-19T23:00:00+14:00"):
            with self.subTest(value=value):
                self.assertEqual(self.classify(value),
                                 ("Within SLA", "Within SLA"))

    def test_random_damaged_values_classify_the_same_on_both(self):
        # A seeded fuzz test: small edits to valid timestamps. Whatever the
        # value, SQL and Python must give the same answer.
        rng = random.Random(34)
        seeds = ["2026-11-18T12:00:00+00:00", "2026-11-18 11:59:59",
                 "2026-11-18T12:30:00.250Z", "2026-11-18T17:29:59+05:30"]
        alphabet = "0123456789-:T .+Z"

        values = set()
        while len(values) < 300:
            seed = rng.choice(seeds)
            at = rng.randrange(len(seed) + 1)
            values.add(seed[:at] + rng.choice(alphabet)
                       + seed[at + rng.randrange(2):])

        for value in sorted(values):
            with self.subTest(value=value):
                sql_status, python_status = self.classify(value)

                self.assertEqual(sql_status, python_status or "Not Tracked")

    def test_parser_rejects_what_julianday_would_quietly_accept(self):
        for value in ("2461000", "now", "2026-11-17", "2026-11-17 09:00"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    sla_storage.parse_stored_utc(value)


# ---------------------------------------------------
# 6: HISTORY IS NEVER REWRITTEN
# ---------------------------------------------------

class TestHistoricalSlaData(SlaPolicyTestCase):

    def test_validation_changes_never_rewrite_stored_sla_rows(self):
        # Rows stored in a format that is now unreadable are reported as
        # Not Tracked, but the stored text itself is left exactly as it was
        ticket_id = self.new_ticket()
        self.sql("UPDATE ticket_sla SET response_due_at = '20261117T090000' "
                 "WHERE ticket_id = ?", (ticket_id,))

        staff = self.client_for("tech")
        staff.get("/")
        staff.get(f"/tickets/{ticket_id}")
        staff.get("/tickets/export")
        db.add_comment(ticket_id, "tech", "Reply")

        self.assertEqual(self.sla_row(ticket_id)[0], "20261117T090000")


if __name__ == "__main__":
    unittest.main()
