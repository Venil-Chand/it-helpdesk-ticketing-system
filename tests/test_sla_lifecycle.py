
import sqlite3
import unittest

from datetime import datetime, timedelta, timezone

from sla_storage import (
    initialize_sla_schema,
    get_ticket_sla,
    create_ticket_sla,
    record_first_response
)


class TestSLALifecycle(unittest.TestCase):

    def setUp(self):
        # Never use the real helpdesk.db in these tests.
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON")

        self.conn.execute("""
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        self.conn.execute(
            "INSERT INTO tickets (id, title) VALUES (?, ?)",
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

    def test_create_deadline(self):
        with self.conn:
            create_ticket_sla(
                self.conn, 1, "Critical", self.created
            )

        result = get_ticket_sla(self.conn, 1)

        self.assertEqual(
            result["response_due_at"],
            "2026-11-17T09:00:00+00:00"
        )

    def test_first_response_only_once(self):
        with self.conn:
            create_ticket_sla(
                self.conn, 1, "High", self.created
            )

            first = record_first_response(
                self.conn, 1,
                self.created + timedelta(minutes=30)
            )

            second = record_first_response(
                self.conn, 1,
                self.created + timedelta(minutes=90)
            )

        self.assertTrue(first)
        self.assertFalse(second)

        result = get_ticket_sla(self.conn, 1)

        self.assertEqual(
            result["first_response_at"],
            "2026-11-17T08:30:00+00:00"
        )

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO tickets (id, title)
                    VALUES (?, ?)
                    """,
                    (2, "New ticket")
                )

                create_ticket_sla(
                    self.conn, 2, "Medium", self.created
                )

                raise RuntimeError("Simulated failure")

        ticket = self.conn.execute(
            "SELECT id FROM tickets WHERE id = 2"
        ).fetchone()

        self.assertIsNone(ticket)
        self.assertIsNone(get_ticket_sla(self.conn, 2))

    def test_unknown_priority_rejected(self):
        with self.assertRaises(ValueError):
            create_ticket_sla(
                self.conn, 1, "Urgent", self.created
            )

    def test_naive_response_rejected(self):
        with self.conn:
            create_ticket_sla(
                self.conn, 1, "Low", self.created
            )

        with self.assertRaises(ValueError):
            record_first_response(
                self.conn, 1,
                datetime(2026, 11, 17, 9, 0)
            )

    def test_historical_ticket_without_sla(self):
        self.assertIsNone(
            get_ticket_sla(self.conn, 1)
        )

    def test_unknown_ticket_foreign_key(self):
        with self.assertRaises(
            sqlite3.IntegrityError
        ):
            with self.conn:
                create_ticket_sla(
                    self.conn, 999, "High", self.created
                )

    def test_nz_time_converts_to_utc(self):
        nz_time = datetime(
            2026, 11, 17, 21, 0,
            tzinfo=timezone(timedelta(hours=13))
        )

        with self.conn:
            create_ticket_sla(
                self.conn, 1, "Critical", nz_time
            )

        result = get_ticket_sla(self.conn, 1)

        self.assertEqual(
            result["response_due_at"],
            "2026-11-17T09:00:00+00:00"
        )


if __name__ == "__main__":
    unittest.main()
