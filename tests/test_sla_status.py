
import sqlite3
import unittest

from datetime import datetime, timedelta, timezone

from sla_storage import (
    initialize_sla_schema,
    create_ticket_sla,
    record_first_response,
    get_ticket_sla_status
)


class TestSLAStatus(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON")

        self.conn.execute("""
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        self.conn.execute(
            "INSERT INTO tickets VALUES (?, ?)",
            (1, "Server offline")
        )

        self.conn.commit()
        initialize_sla_schema(self.conn)

        self.created = datetime(
            2026, 11, 17, 8, 0,
            tzinfo=timezone.utc
        )

    def tearDown(self):
        self.conn.close()

    def create_critical_sla(self):
        with self.conn:
            create_ticket_sla(
                self.conn, 1, "Critical", self.created
            )

    def test_historical_ticket_has_no_sla(self):
        result = get_ticket_sla_status(
            self.conn, 1, now=self.created
        )
        self.assertIsNone(result)

    def test_ticket_within_sla(self):
        self.create_critical_sla()

        now = self.created + timedelta(minutes=15)

        result = get_ticket_sla_status(
            self.conn, 1, now=now
        )

        self.assertEqual(result["status"], "Within SLA")

    def test_ticket_due_soon(self):
        self.create_critical_sla()

        now = self.created + timedelta(minutes=45)

        result = get_ticket_sla_status(
            self.conn, 1, now=now
        )

        self.assertEqual(result["status"], "Due Soon")

    def test_ticket_overdue(self):
        self.create_critical_sla()

        now = self.created + timedelta(hours=2)

        result = get_ticket_sla_status(
            self.conn, 1, now=now
        )

        self.assertEqual(result["status"], "Overdue")

    def test_response_met(self):
        self.create_critical_sla()

        with self.conn:
            record_first_response(
                self.conn,
                1,
                self.created + timedelta(minutes=30)
            )

        result = get_ticket_sla_status(
            self.conn,
            1,
            now=self.created + timedelta(hours=3)
        )

        self.assertEqual(result["status"], "Met")

    def test_response_breached(self):
        self.create_critical_sla()

        with self.conn:
            record_first_response(
                self.conn,
                1,
                self.created + timedelta(hours=2)
            )

        result = get_ticket_sla_status(
            self.conn,
            1,
            now=self.created + timedelta(hours=3)
        )

        self.assertEqual(result["status"], "Breached")


if __name__ == "__main__":
    unittest.main()
