
import unittest
from datetime import datetime, timedelta, timezone

from sla import calculate_deadline, assess_response_sla


class TestSLA(unittest.TestCase):

    def setUp(self):
        self.created = datetime(
            2026, 11, 17, 8, 0, tzinfo=timezone.utc
        )

    def test_critical_deadline(self):
        result = calculate_deadline(self.created, "Critical")

        expected = self.created + timedelta(hours=1)

        self.assertEqual(result, expected)

    def test_high_deadline(self):
        result = calculate_deadline(self.created, "High")

        expected = self.created + timedelta(hours=4)

        self.assertEqual(result, expected)

    def test_medium_deadline(self):
        result = calculate_deadline(self.created, "Medium")

        expected = self.created + timedelta(hours=24)

        self.assertEqual(result, expected)

    def test_low_deadline(self):
        result = calculate_deadline(self.created, "Low")

        expected = self.created + timedelta(hours=72)

        self.assertEqual(result, expected)

    def test_unknown_priority(self):
        with self.assertRaises(ValueError):
            calculate_deadline(self.created, "Unknown")

    def test_naive_datetime_rejected(self):
        naive = datetime(2026, 11, 17, 8, 0)

        with self.assertRaises(ValueError):
            calculate_deadline(naive, "High")

    def test_overdue(self):
        deadline = self.created + timedelta(hours=1)
        now = self.created + timedelta(hours=2)

        result = assess_response_sla(deadline, now)

        self.assertEqual(result, "Overdue")

    def test_due_soon(self):
        deadline = self.created + timedelta(hours=1)
        now = deadline - timedelta(minutes=20)

        result = assess_response_sla(deadline, now)

        self.assertEqual(result, "Due Soon")

    def test_within_sla(self):
        deadline = self.created + timedelta(hours=4)
        now = self.created + timedelta(hours=1)

        result = assess_response_sla(deadline, now)

        self.assertEqual(result, "Within SLA")

    def test_response_met_deadline(self):
        deadline = self.created + timedelta(hours=4)
        response = self.created + timedelta(hours=2)
        now = self.created + timedelta(hours=6)

        result = assess_response_sla(
            deadline, now, first_response_at=response
        )

        self.assertEqual(result, "Met")

    def test_response_breached_deadline(self):
        deadline = self.created + timedelta(hours=1)
        response = self.created + timedelta(hours=2)

        result = assess_response_sla(
            deadline, first_response_at=response
        )

        self.assertEqual(result, "Breached")

    def test_response_exactly_at_deadline(self):
        deadline = self.created + timedelta(hours=1)

        result = assess_response_sla(
            deadline, first_response_at=deadline
        )

        self.assertEqual(result, "Met")

    def test_nz_timezone_converts_to_utc(self):
        nz_time = datetime(
            2026, 11, 17, 21, 0,
            tzinfo=timezone(timedelta(hours=13))
        )

        result = calculate_deadline(nz_time, "Critical")

        expected = datetime(
            2026, 11, 17, 9, 0, tzinfo=timezone.utc
        )

        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
