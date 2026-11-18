"""
Database tests for the SLA dashboard queries.

These tests call database.py directly against a temporary SQLite file,
so they check the SQL itself: how each ticket is classified, the counts
for the summary cards, the SLA filter, and how it combines with search,
priority, status, ownership and pagination.

Every test passes a fixed `now`, so the results never depend on when
the tests are run. The real helpdesk.db is never opened.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import database as db
import sla_storage
from sla import ALERT_WINDOW, assess_response_sla


# A fixed "current time" for every test
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def iso(moment):
    """Format a datetime the way sla_storage stores SLA timestamps."""
    return moment.astimezone(timezone.utc).isoformat()


class SlaDatabaseTestCase(unittest.TestCase):
    """A fresh temporary database for every test."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)

        self.db_path = os.path.join(temp_dir.name, "sla_dashboard.db")

        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        db.init_db()

        self.created_at = "2026-11-18 08:00:00"

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def add_ticket(
        self,
        title="Ticket",
        *,
        created_by="alice",
        priority="High",
        status="Open",
        created_at=None,
        due=None,
        responded=None,
        sla=True
    ):
        """
        Insert a ticket and, unless sla=False, its SLA row.

        due and responded may be datetimes (stored like sla_storage
        stores them) or raw text, to test other stored formats.
        """
        due = NOW + timedelta(hours=2) if due is None else due

        if isinstance(due, datetime):
            due = iso(due)

        if isinstance(responded, datetime):
            responded = iso(responded)

        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO tickets (title, description, priority, "
                "status, created_by, created_at) "
                "VALUES (?, 'Details', ?, ?, ?, ?)",
                (title, priority, status, created_by,
                 created_at or self.created_at)
            )
            ticket_id = cursor.lastrowid

            if sla:
                conn.execute(
                    "INSERT INTO ticket_sla "
                    "(ticket_id, response_due_at, first_response_at) "
                    "VALUES (?, ?, ?)",
                    (ticket_id, due, responded)
                )

        return ticket_id

    def status_of(self, ticket_id, now=NOW):
        """The SLA status the dashboard query gives one ticket."""
        rows = db.get_tickets_page(per_page=50, now=now)
        return {row["id"]: row["sla_status"] for row in rows}[ticket_id]

    def ids(self, rows):
        return [row["id"] for row in rows]


# ---------------------------------------------------
# CLASSIFICATION
# ---------------------------------------------------

class TestSlaClassification(SlaDatabaseTestCase):

    def test_each_of_the_six_statuses(self):
        cases = {
            "Within SLA": dict(due=NOW + timedelta(hours=3)),
            "Due Soon": dict(due=NOW + timedelta(minutes=10)),
            "Overdue": dict(due=NOW - timedelta(minutes=10)),
            "Met": dict(due=NOW - timedelta(hours=1),
                        responded=NOW - timedelta(hours=2)),
            "Breached": dict(due=NOW - timedelta(hours=2),
                             responded=NOW - timedelta(hours=1)),
            "Not Tracked": dict(sla=False),
        }

        for expected, options in cases.items():
            with self.subTest(expected=expected):
                ticket_id = self.add_ticket(expected, **options)
                self.assertEqual(self.status_of(ticket_id), expected)

    def test_historical_ticket_is_not_tracked_never_overdue(self):
        # Years old, no SLA row: must not be reported as overdue
        ticket_id = self.add_ticket(
            created_at="2020-01-01 00:00:00", sla=False
        )

        self.assertEqual(self.status_of(ticket_id), "Not Tracked")

    def test_resolved_ticket_follows_the_existing_policy(self):
        # The established policy looks only at the first response, not
        # the ticket status, so this is unchanged by the dashboard
        ticket_id = self.add_ticket(
            status="Resolved", due=NOW - timedelta(hours=1)
        )

        self.assertEqual(self.status_of(ticket_id), "Overdue")


class TestSlaBoundaries(SlaDatabaseTestCase):
    """The exact edges, matching sla.assess_response_sla()."""

    def check(self, expected, **options):
        ticket_id = self.add_ticket(**options)
        self.assertEqual(self.status_of(ticket_id), expected)

    def test_exactly_at_the_deadline_is_overdue(self):
        self.check("Overdue", due=NOW)

    def test_one_second_before_the_deadline_is_due_soon(self):
        self.check("Due Soon", due=NOW + timedelta(seconds=1))

    def test_exactly_at_the_alert_window_is_due_soon(self):
        self.check("Due Soon", due=NOW + ALERT_WINDOW)

    def test_one_second_outside_the_alert_window_is_within(self):
        self.check(
            "Within SLA", due=NOW + ALERT_WINDOW + timedelta(seconds=1)
        )

    def test_response_exactly_at_the_deadline_is_met(self):
        deadline = NOW - timedelta(hours=1)
        self.check("Met", due=deadline, responded=deadline)

    def test_response_one_second_after_the_deadline_is_breached(self):
        deadline = NOW - timedelta(hours=1)
        self.check(
            "Breached",
            due=deadline,
            responded=deadline + timedelta(seconds=1)
        )

    def test_response_before_the_deadline_is_met_even_after_it_passes(self):
        self.check(
            "Met",
            due=NOW - timedelta(days=1),
            responded=NOW - timedelta(days=2)
        )

    def test_early_response_on_a_future_deadline_is_met(self):
        self.check(
            "Met",
            due=NOW + timedelta(hours=3),
            responded=NOW - timedelta(minutes=5)
        )

    def test_sub_second_now_does_not_shift_the_deadline(self):
        # Half a millisecond before the deadline is still before it
        ticket_id = self.add_ticket(due=NOW)

        self.assertEqual(
            self.status_of(ticket_id, now=NOW - timedelta(microseconds=500)),
            "Due Soon"
        )
        self.assertEqual(self.status_of(ticket_id, now=NOW), "Overdue")


class TestStoredTimestampFormats(SlaDatabaseTestCase):
    """SQL classification must agree with the Python SLA code."""

    FORMATS = {
        "isoformat UTC": lambda m: m.isoformat(),
        "sqlite CURRENT_TIMESTAMP": lambda m: m.strftime("%Y-%m-%d %H:%M:%S"),
        "naive with T": lambda m: m.strftime("%Y-%m-%dT%H:%M:%S"),
        "Z suffix": lambda m: m.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plus offset": lambda m: m.astimezone(
            timezone(timedelta(hours=5, minutes=30))).isoformat(),
        "minus offset": lambda m: m.astimezone(
            timezone(timedelta(hours=-5))).isoformat(),
        "milliseconds": lambda m: m.isoformat(timespec="milliseconds"),
    }

    OFFSETS = [
        timedelta(hours=-3), timedelta(seconds=-1), timedelta(0),
        timedelta(seconds=1), ALERT_WINDOW, ALERT_WINDOW
        + timedelta(seconds=1), timedelta(hours=5),
    ]

    def test_sql_matches_python_for_every_supported_format(self):
        conn = db.get_db()
        self.addCleanup(conn.close)

        for name, fmt in self.FORMATS.items():
            for offset in self.OFFSETS:
                deadline = NOW + offset

                for responded in (None, deadline - timedelta(seconds=1),
                                  deadline, deadline + timedelta(seconds=1)):
                    with self.subTest(format=name, offset=offset,
                                      responded=responded):
                        ticket_id = self.add_ticket(
                            due=fmt(deadline),
                            responded=None if responded is None
                            else fmt(responded)
                        )

                        python_status = sla_storage.get_ticket_sla_status(
                            conn, ticket_id, now=NOW
                        )["status"]

                        self.assertEqual(
                            self.status_of(ticket_id), python_status
                        )

    def test_python_reference_agrees_with_assess_response_sla(self):
        # Guards the test above: the reference really is the SLA engine
        self.assertEqual(assess_response_sla(NOW, now=NOW), "Overdue")


class TestUnreadableSlaRows(SlaDatabaseTestCase):
    """Damaged rows are Not Tracked, as the ticket page shows no SLA."""

    def test_unreadable_values_are_not_tracked(self):
        cases = {
            "garbage deadline": dict(due="not a date"),
            "empty deadline": dict(due=""),
            "garbage response": dict(due=NOW - timedelta(hours=1),
                                     responded="soon"),
        }

        for name, options in cases.items():
            with self.subTest(case=name):
                ticket_id = self.add_ticket(**options)

                self.assertEqual(self.status_of(ticket_id), "Not Tracked")
                self.assertIsNone(db.get_ticket_sla_status(ticket_id, now=NOW))

    def test_values_julianday_accepts_but_python_rejects(self):
        # SQLite reads '2461000' as a Julian day number and 'now' as the
        # current time. parse_stored_utc() rejects both, so must the SQL.
        # (The column has TEXT affinity, so the integer is stored as text.)
        for value in (2461000, "2461000", "2461000.5", "now"):
            for column in ("response_due_at", "first_response_at"):
                with self.subTest(value=value, column=column):
                    ticket_id = self.add_ticket(due=NOW - timedelta(days=1))

                    with self.connect() as conn:
                        conn.execute(
                            f"UPDATE ticket_sla SET {column} = ? "
                            "WHERE ticket_id = ?", (value, ticket_id)
                        )

                    self.assertEqual(self.status_of(ticket_id), "Not Tracked")
                    self.assertIsNone(
                        db.get_ticket_sla_status(ticket_id, now=NOW)
                    )


# ---------------------------------------------------
# SUMMARY STATISTICS
# ---------------------------------------------------

class TestSlaStats(SlaDatabaseTestCase):

    def add_one_of_each(self, created_by):
        self.add_ticket(created_by=created_by, due=NOW + timedelta(hours=3))
        self.add_ticket(created_by=created_by,
                        due=NOW + timedelta(minutes=5))
        self.add_ticket(created_by=created_by,
                        due=NOW - timedelta(minutes=5))
        self.add_ticket(created_by=created_by, due=NOW,
                        responded=NOW - timedelta(minutes=1))
        self.add_ticket(created_by=created_by, due=NOW,
                        responded=NOW + timedelta(minutes=1))
        self.add_ticket(created_by=created_by, sla=False)

    def test_counts_every_category_in_display_order(self):
        self.add_one_of_each("alice")
        self.add_ticket(due=NOW - timedelta(hours=9))  # a second Overdue

        stats = db.get_sla_stats(now=NOW)

        self.assertEqual(list(stats), list(db.SLA_STATUS_LABELS))
        self.assertEqual(stats, {
            "Within SLA": 1, "Due Soon": 1, "Overdue": 2,
            "Met": 1, "Breached": 1, "Not Tracked": 1,
        })

    def test_empty_database_gives_zero_for_every_category(self):
        self.assertEqual(
            db.get_sla_stats(now=NOW),
            dict.fromkeys(db.SLA_STATUS_LABELS, 0)
        )

    def test_regular_user_counts_only_their_own_tickets(self):
        self.add_one_of_each("alice")
        for _ in range(4):
            self.add_ticket(created_by="bob", due=NOW - timedelta(hours=1))

        alice = db.get_sla_stats(username="alice", now=NOW)

        self.assertEqual(sum(alice.values()), 6)
        self.assertEqual(alice["Overdue"], 1)
        self.assertEqual(db.get_sla_stats(username="bob", now=NOW)["Overdue"], 4)

    def test_staff_scope_counts_every_ticket(self):
        self.add_one_of_each("alice")
        self.add_one_of_each("bob")

        self.assertEqual(sum(db.get_sla_stats(now=NOW).values()), 12)

    def test_stats_cover_more_than_one_page(self):
        for _ in range(db.MAX_PER_PAGE + 7):
            self.add_ticket(due=NOW - timedelta(hours=1))

        self.assertEqual(
            db.get_sla_stats(now=NOW)["Overdue"], db.MAX_PER_PAGE + 7
        )

    def test_the_same_tickets_move_category_as_time_passes(self):
        self.add_ticket(due=NOW + timedelta(hours=1))

        self.assertEqual(db.get_sla_stats(now=NOW)["Within SLA"], 1)
        self.assertEqual(
            db.get_sla_stats(now=NOW + timedelta(minutes=45))["Due Soon"], 1
        )
        self.assertEqual(
            db.get_sla_stats(now=NOW + timedelta(hours=1))["Overdue"], 1
        )

    def test_unknown_username_counts_nothing(self):
        self.add_one_of_each("alice")

        self.assertEqual(
            sum(db.get_sla_stats(username="' OR 1=1 --", now=NOW).values()),
            0
        )


# ---------------------------------------------------
# FILTERING AND PAGINATION
# ---------------------------------------------------

class TestSlaFilterQueries(SlaDatabaseTestCase):

    def test_filter_returns_only_that_status(self):
        overdue = self.add_ticket(due=NOW - timedelta(hours=1))
        self.add_ticket(due=NOW + timedelta(hours=3))
        self.add_ticket(sla=False)

        rows = db.get_tickets_page(per_page=10, sla_filter="Overdue", now=NOW)

        self.assertEqual(self.ids(rows), [overdue])
        self.assertEqual(
            db.count_tickets(sla_filter="Overdue", now=NOW), 1
        )

    def test_filter_combines_with_search_priority_and_status(self):
        match = self.add_ticket("Printer jam", priority="High",
                                due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="Low",
                        due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="High",
                        due=NOW + timedelta(hours=3))
        self.add_ticket("VPN down", priority="High",
                        due=NOW - timedelta(hours=1))
        self.add_ticket("Printer jam", priority="High", status="Closed",
                        due=NOW - timedelta(hours=1))

        filters = dict(search_text="printer", priority_filter="High",
                       status_filter="Open", sla_filter="Overdue")

        self.assertEqual(
            self.ids(db.get_tickets_page(per_page=10, now=NOW, **filters)),
            [match]
        )
        self.assertEqual(db.count_tickets(now=NOW, **filters), 1)

    def test_filter_keeps_ownership(self):
        alice = self.add_ticket(created_by="alice",
                                due=NOW - timedelta(hours=1))
        self.add_ticket(created_by="bob", due=NOW - timedelta(hours=1))

        rows = db.get_tickets_page(per_page=10, username="alice",
                                   sla_filter="Overdue", now=NOW)

        self.assertEqual(self.ids(rows), [alice])
        self.assertEqual(db.count_tickets(username="alice",
                                          sla_filter="Overdue", now=NOW), 1)

    def test_filter_runs_before_pagination(self):
        # 24 overdue tickets mixed with 29 others: the filtered pages
        # must hold 10, 10 and 4 overdue tickets, never other tickets
        overdue = []
        for i in range(53):
            created = f"2026-11-18 08:{i:02d}:00"
            if i % 7 < 3:
                overdue.append(self.add_ticket(
                    created_at=created, due=NOW - timedelta(minutes=i + 1)))
            else:
                self.add_ticket(created_at=created,
                                due=NOW + timedelta(hours=3))

        self.assertEqual(len(overdue), 24)
        self.assertEqual(db.count_tickets(sla_filter="Overdue", now=NOW), 24)

        pages = [
            self.ids(db.get_tickets_page(per_page=10, page=page,
                                         sla_filter="Overdue", now=NOW))
            for page in (1, 2, 3, 4)
        ]

        self.assertEqual([len(p) for p in pages], [10, 10, 4, 0])

        seen = pages[0] + pages[1] + pages[2]
        self.assertEqual(len(set(seen)), 24)  # no overlap, none missed
        self.assertEqual(seen, sorted(overdue, reverse=True))  # newest first

    def test_not_tracked_filter_finds_historical_tickets(self):
        old = self.add_ticket(sla=False)
        self.add_ticket(due=NOW - timedelta(hours=1))

        self.assertEqual(
            self.ids(db.get_tickets_page(per_page=10,
                                         sla_filter="Not Tracked", now=NOW)),
            [old]
        )

    def test_page_rows_carry_sla_columns(self):
        due = NOW + timedelta(hours=3)
        responded = NOW - timedelta(minutes=2)
        ticket_id = self.add_ticket(due=due, responded=responded)

        row = db.get_tickets_page(per_page=10, now=NOW)[0]

        self.assertEqual(row["id"], ticket_id)
        self.assertEqual(row["sla_response_due_at"], iso(due))
        self.assertEqual(row["sla_first_response_at"], iso(responded))
        self.assertEqual(row["sla_status"], "Met")

    def test_unknown_sla_label_is_rejected(self):
        for bad in ("overdue", "Overdue' OR '1'='1", "BREACHED", " Met"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    db.count_tickets(sla_filter=bad, now=NOW)

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            db.get_sla_stats(now=datetime(2026, 11, 18, 12, 0))

    def test_now_in_another_time_zone_is_the_same_moment(self):
        ticket_id = self.add_ticket(due=NOW)
        tokyo = NOW.astimezone(timezone(timedelta(hours=9)))

        self.assertEqual(self.status_of(ticket_id, now=tokyo), "Overdue")


class TestSearchTicketsSlaFilter(SlaDatabaseTestCase):
    """search_tickets() feeds the CSV export."""

    def test_without_sla_filter_rows_are_unchanged(self):
        self.add_ticket(due=NOW - timedelta(hours=1))

        row = db.search_tickets()[0]

        # The original query: ticket columns only
        self.assertNotIn("sla_status", row.keys())
        self.assertEqual(
            row.keys(),
            [c[1] for c in self.connect().execute(
                "PRAGMA table_info(tickets)")]
        )

    def test_sla_filter_selects_matching_tickets_with_other_filters(self):
        match = self.add_ticket("Laptop", created_by="alice",
                                due=NOW - timedelta(hours=1))
        self.add_ticket("Laptop", created_by="bob",
                        due=NOW - timedelta(hours=1))
        self.add_ticket("Laptop", created_by="alice",
                        due=NOW + timedelta(hours=1))

        rows = db.search_tickets("lap", "alice", "All", "All",
                                 sla_filter="Overdue", now=NOW)

        self.assertEqual(self.ids(rows), [match])


# ---------------------------------------------------
# QUERY PLANS
# ---------------------------------------------------

class TestSlaQueryPlans(SlaDatabaseTestCase):
    """The joined queries must use the indexes, not scan ticket_sla."""

    def plan(self, columns, filters, suffix="", extra=()):
        sql, params = db._sla_ticket_query(
            columns, now=NOW, sla_filter="Overdue", filters=filters,
            suffix=suffix
        )
        conn = self.connect()
        return [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, params + list(extra))]

    def test_sla_rows_are_found_by_primary_key(self):
        for filters in ({}, {"username": "alice"}):
            with self.subTest(filters=filters):
                plan = self.plan("*", filters,
                                 " ORDER BY created_at DESC, id DESC "
                                 "LIMIT ? OFFSET ?", (10, 0))

                self.assertTrue(any("INTEGER PRIMARY KEY" in step
                                    for step in plan), plan)
                self.assertFalse(any(step.startswith("SCAN s")
                                     for step in plan), plan)
                # The page order comes from an index, not a sort
                self.assertFalse(any("ORDER BY" in step for step in plan),
                                 plan)

    def test_regular_user_queries_use_the_owner_index(self):
        plan = self.plan("COUNT(*)", {"username": "alice"})

        self.assertTrue(any("idx_tickets_owner_date" in step
                            for step in plan), plan)


if __name__ == "__main__":
    unittest.main()
