"""
The notification queue: claiming, leases, retries, backoff, permanent
failure, expiry, interrupted processing and concurrent workers.

Temporary databases and a frozen queue clock; no network.
"""

import sqlite3
import threading
import unittest
from datetime import timedelta

import database as db
from notifications import queue, timeutil, transport
from notifications.worker import NotificationWorker

from notification_helpers import (
    FakeSMTP,
    NotificationTestCase,
    local_settings,
)


class QueueTestCase(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        (self.row_id,) = [row[0] for row in self.notifications()]
        self.settings = local_settings(self.sink_dir, max_attempts=3,
                                       retry_base_seconds=60,
                                       retry_max_seconds=600,
                                       max_age_hours=24,
                                       lease_seconds=120)

    def claim(self, worker="w1"):
        claim, _recovered, _expired = queue.claim_next(
            db.DB_PATH, self.settings, worker=worker, mode="local"
        )
        return claim

    def row(self):
        return self.sql(
            "SELECT status, attempts, deferrals, next_attempt_at, "
            "claim_token, last_error, sent_at FROM notifications "
            "WHERE id = ?", (self.row_id,)
        )[0]


class TestBackoff(unittest.TestCase):

    def test_doubles_and_is_capped(self):
        settings = local_settings("/nonexistent", retry_base_seconds=60,
                                  retry_max_seconds=1000)
        self.assertEqual(
            [queue.backoff_seconds(n, settings) for n in range(0, 7)],
            [60, 60, 120, 240, 480, 960, 1000]
        )

    def test_huge_counts_do_not_overflow(self):
        settings = local_settings("/x", retry_max_seconds=3600)
        self.assertEqual(queue.backoff_seconds(10 ** 9, settings), 3600)


class TestClaiming(QueueTestCase):

    def test_claim_reserves_and_counts_the_attempt(self):
        claim = self.claim()

        self.assertEqual(claim.id, self.row_id)
        status, attempts, _, _, token, _, _ = self.row()
        self.assertEqual((status, attempts, token),
                         ("processing", 1, claim.token))

    def test_a_claimed_row_cannot_be_claimed_again(self):
        self.assertIsNotNone(self.claim("w1"))
        self.assertIsNone(self.claim("w2"))

    def test_rows_not_yet_due_are_left_alone(self):
        self.sql("UPDATE notifications SET next_attempt_at = ?",
                 (timeutil.format_utc(self.clock() + timedelta(minutes=5)),))
        self.assertIsNone(self.claim())

        self.clock.advance(minutes=5)
        self.assertIsNotNone(self.claim())

    def test_oldest_due_first(self):
        db.update_ticket(self.bob_ticket, "Closed", None, actor="tech")
        first = self.claim()
        second = self.claim()
        self.assertLess(first.id, second.id)

    def test_finished_rows_are_never_claimed(self):
        for status in ("sent", "simulated", "failed", "cancelled"):
            with self.subTest(status=status):
                self.sql("UPDATE notifications SET status = ?", (status,))
                self.assertIsNone(self.claim())

    def test_no_transaction_is_left_open_after_claiming(self):
        claim = self.claim()
        self.assertIsNotNone(claim)

        # Another connection can take the write lock immediately
        conn = sqlite3.connect(db.DB_PATH, timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()


class TestFinishing(QueueTestCase):

    def finish(self, claim, outcome, error=None, worker="w1"):
        return queue.finish(db.DB_PATH, claim, self.settings,
                            worker=worker, outcome=outcome, error=error)

    def test_delivered(self):
        claim = self.claim()
        self.clock.advance(seconds=3)

        self.assertEqual(self.finish(claim, "simulated"), "simulated")

        status, attempts, _, _, token, error, sent_at = self.row()
        self.assertEqual((status, attempts, token, error),
                         ("simulated", 1, None, None))
        self.assertEqual(sent_at, timeutil.format_utc(self.clock()))
        self.assertEqual(self.attempts(self.row_id),
                         [(1, "simulated", None, "local")])

    def test_retry_with_exponential_backoff_then_permanent_failure(self):
        expected_delays = [60, 120]

        for attempt in (1, 2):
            claim = self.claim()
            self.assertEqual(claim.attempt, attempt)
            self.assertEqual(self.finish(claim, "retry", "send: OSError"),
                             "pending")

            next_at = timeutil.parse_utc(self.row()[3])
            self.assertEqual(next_at - self.clock(),
                             timedelta(seconds=expected_delays[attempt - 1]))

            self.assertIsNone(self.claim(), "not due before the backoff")
            self.clock.advance(seconds=expected_delays[attempt - 1])

        claim = self.claim()
        self.assertEqual(self.finish(claim, "retry", "send: OSError"),
                         "failed")

        self.assertEqual(self.row()[:2], ("failed", 3))
        self.assertEqual(
            [a[1] for a in self.attempts(self.row_id)],
            ["retry", "retry", "failed"]
        )

        # Bounded: a failed notification is never tried again
        self.clock.advance(days=1)
        self.assertIsNone(self.claim())

    def test_deferral_does_not_use_an_attempt(self):
        for n in range(1, 6):
            claim = self.claim()
            self.assertEqual(self.finish(claim, "deferred",
                                         "connect: ConnectionRefusedError"),
                             "pending")
            self.assertEqual(self.row()[1:3], (0, n))
            self.clock.advance(hours=1)

        self.assertEqual([a[1] for a in self.attempts(self.row_id)],
                         ["deferred"] * 5)

    def test_deferred_too_long_expires(self):
        claim = self.claim()
        self.clock.advance(hours=25)
        status = self.finish(claim, "deferred", "connect: TimeoutError")

        self.assertEqual(status, "failed")
        self.assertIn("expired", self.row()[5])

    def test_cancelled_gives_the_attempt_back(self):
        claim = self.claim()
        self.assertEqual(self.finish(claim, "cancelled", "recipient has no "
                                     "email address"), "cancelled")
        self.assertEqual(self.row()[:2], ("cancelled", 0))

    def test_permanent_failure(self):
        claim = self.claim()
        self.assertEqual(self.finish(claim, "failed", "recipient: "
                                     "SMTPRecipientsRefused 550"), "failed")
        self.assertEqual(self.row()[:2], ("failed", 1))

    def test_unknown_outcome_changes_nothing(self):
        claim = self.claim()
        with self.assertRaises(ValueError):
            self.finish(claim, "mystery")
        self.assertEqual(self.row()[0], "processing")

    def test_errors_are_stored_shortened(self):
        claim = self.claim()
        self.finish(claim, "failed", "x" * 1000)
        self.assertEqual(len(self.row()[5]), 200)

    def test_wrong_token_cannot_finish(self):
        claim = self.claim()
        forged = queue.Claim(claim.row, "0" * 32)

        self.assertIsNone(self.finish(forged, "simulated"))
        self.assertEqual(self.row()[0], "processing")
        self.assertEqual(self.attempts(self.row_id), [])


class TestInterruptedProcessing(QueueTestCase):

    def test_expired_claim_is_recovered_as_an_attempt(self):
        stale = self.claim("crashed-worker")
        self.clock.advance(seconds=121)

        claim, recovered, _ = queue.claim_next(
            db.DB_PATH, self.settings, worker="w2", mode="local"
        )

        # Recovered and backed off, so not immediately re-claimed
        self.assertEqual(recovered, 1)
        self.assertIsNone(claim)
        status, attempts, _, _, _, error, _ = self.row()
        self.assertEqual((status, attempts), ("pending", 1))
        self.assertIn("may or may not have been sent", error)
        self.assertEqual(self.attempts(self.row_id)[0][:2],
                         (1, "interrupted"))

        # The crashed worker comes back and cannot overwrite anything
        self.assertIsNone(queue.finish(
            db.DB_PATH, stale, self.settings, worker="crashed-worker",
            outcome="simulated"
        ))
        self.assertEqual(self.row()[0], "pending")

    def test_claim_within_its_lease_is_not_recovered(self):
        self.claim("slow-but-alive")
        self.clock.advance(seconds=119)

        _, recovered, _ = queue.claim_next(
            db.DB_PATH, self.settings, worker="w2", mode="local"
        )
        self.assertEqual(recovered, 0)
        self.assertEqual(self.row()[0], "processing")

    def test_repeated_interruptions_are_bounded(self):
        for _ in range(3):
            self.assertIsNotNone(self.claim())
            self.clock.advance(hours=1)
            queue.claim_next(db.DB_PATH, self.settings, worker="w",
                             mode="local")
            self.clock.advance(hours=1)

        self.assertEqual(self.row()[:2], ("failed", 3))
        self.assertIsNone(self.claim())
        self.assertEqual([a[1] for a in self.attempts(self.row_id)],
                         ["interrupted"] * 3)

    def test_old_pending_rows_expire(self):
        self.clock.advance(hours=24, seconds=1)
        claim, _, expired = queue.claim_next(
            db.DB_PATH, self.settings, worker="w", mode="local"
        )
        self.assertIsNone(claim)
        self.assertEqual(expired, 1)
        self.assertEqual(self.row()[0], "failed")
        self.assertEqual(self.attempts(self.row_id)[0][1:3],
                         ("failed", queue.EXPIRED_ERROR))


class TestAttemptHistory(QueueTestCase):

    def test_attempt_history_is_append_only(self):
        claim = self.claim()
        queue.finish(db.DB_PATH, claim, self.settings, worker="w",
                     outcome="simulated")

        for statement in ("UPDATE notification_attempts SET error = 'x'",
                          "DELETE FROM notification_attempts"):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError,
                                            "append-only"):
                    self.sql(statement)


class TestConcurrentWorkers(NotificationTestCase):
    """Several workers, separate connections, one shared database file."""

    def setUp(self):
        super().setUp()
        self.clear_queue()

        # 30 tickets x (alice + tech + root) = 90 queued notifications
        for n in range(30):
            db.create_ticket(f"t{n}", "d", "Low", "alice")

        self.total = len(self.notifications())
        self.assertEqual(self.total, 90)

    def run_workers(self, count, transport_factory, settings):
        errors = []
        barrier = threading.Barrier(count)

        def run(n):
            try:
                barrier.wait()
                NotificationWorker(
                    settings, transport_factory(), db_path=db.DB_PATH,
                    worker_id=f"worker-{n}"
                ).run_once(limit=1000)
            except Exception as error:  # reported below
                errors.append(error)

        threads = [threading.Thread(target=run, args=(n,))
                   for n in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        self.assertEqual(errors, [])

    def test_every_notification_is_sent_exactly_once(self):
        settings = local_settings(self.sink_dir)
        sent = []
        lock = threading.Lock()

        class CountingSink(transport.LocalSinkTransport):
            def send(self, message, notification_id):
                with lock:
                    sent.append(notification_id)
                return super().send(message, notification_id)

        self.run_workers(4, lambda: CountingSink(settings.sink_dir), settings)

        self.assertEqual(len(sent), self.total)
        self.assertEqual(len(set(sent)), self.total)
        self.assertEqual(len(self.sink_files()), self.total)
        self.assertEqual(
            self.sql("SELECT status, COUNT(*) FROM notifications "
                     "GROUP BY status"),
            [("simulated", self.total)]
        )
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM notification_attempts")[0][0],
            self.total
        )

    def test_concurrent_smtp_workers_send_each_message_once(self):
        from notification_helpers import smtp_settings

        settings = smtp_settings()
        self.run_workers(
            3,
            lambda: transport.SmtpTransport(
                settings, smtp_class=FakeSMTP, smtp_ssl_class=FakeSMTP
            ),
            settings,
        )

        ids = [m["Message-ID"] for m in FakeSMTP.sent]
        self.assertEqual(len(ids), self.total)
        self.assertEqual(len(set(ids)), self.total)

    def test_ticket_writes_continue_while_workers_run(self):
        settings = local_settings(self.sink_dir)
        done = threading.Event()

        def write_tickets():
            for n in range(10):
                db.create_ticket(f"late{n}", "d", "Low", "bob")
            done.set()

        writer = threading.Thread(target=write_tickets)
        writer.start()
        self.run_workers(
            2, lambda: transport.LocalSinkTransport(settings.sink_dir),
            settings
        )
        writer.join(60)

        self.assertTrue(done.is_set())
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM tickets WHERE title LIKE 'late%'"
                     )[0][0], 10
        )


class TestNoTransactionDuringDelivery(QueueTestCase):

    def test_database_is_writable_while_a_message_is_being_sent(self):
        observed = {}

        class ProbingSink(transport.LocalSinkTransport):
            def send(self, message, notification_id):
                conn = sqlite3.connect(db.DB_PATH, timeout=0)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("ROLLBACK")
                    observed["writable"] = True
                finally:
                    conn.close()
                return super().send(message, notification_id)

        worker = NotificationWorker(
            self.settings, ProbingSink(self.sink_dir), db_path=db.DB_PATH,
            worker_id="w"
        )
        worker.run_once()

        self.assertTrue(observed.get("writable"))


class TestStatusCounts(QueueTestCase):

    def test_counts_every_status(self):
        counts, oldest, failures = queue.status_counts(db.DB_PATH)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(set(counts), set(queue.STATUSES))
        self.assertEqual(oldest, timeutil.format_utc(self.clock()))
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
