"""
Claiming, finishing and recovering queued notifications.

Life of a row:

    pending --claim--> processing --finish--> sent | simulated
       ^                   |                  failed | cancelled
       |                   |
       +--- retry/defer ---+   (next_attempt_at moves later)
       +--- lease expired (worker stopped mid-delivery) ---+

Concurrency rules:

* claim_next() is one short BEGIN IMMEDIATE transaction: it recovers
  expired claims, gives up on messages that are too old, and reserves one
  due row for this worker with a random claim token and a lease. It
  commits before any delivery starts, so no SQLite transaction is ever
  held open during an SMTP conversation.
* Only one worker can win a row: the write lock serialises claims, and
  the row leaves "pending" in the same transaction.
* finish() changes the row only if it is still processing under the same
  claim token. A worker whose lease expired (and whose row another worker
  has since recovered) cannot overwrite the newer outcome.

Attempts:

* `attempts` counts deliveries that may have reached the mail server. It
  is increased when a row is claimed, so a worker that dies mid-delivery
  still used one attempt. Outcomes that prove nothing was sent (deferred,
  cancelled) give the attempt back. At most max_attempts deliveries are
  ever tried for one notification, which bounds possible duplicates.
* `deferrals` counts runs in which the server could not be reached at
  all. These cannot cause duplicates; they are bounded by age instead
  (max_age_hours), after which the notification fails as expired.

Every outcome, including interruptions and expiry, adds a row to the
append-only notification_attempts table.
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from . import timeutil

PENDING = "pending"
PROCESSING = "processing"
SENT = "sent"
SIMULATED = "simulated"
FAILED = "failed"
CANCELLED = "cancelled"

STATUSES = (PENDING, PROCESSING, SENT, SIMULATED, FAILED, CANCELLED)

# Seconds to wait for another process's write lock
LOCK_TIMEOUT = 30

INTERRUPTED_ERROR = ("worker stopped during delivery; the message may "
                     "or may not have been sent")
EXPIRED_ERROR = "expired before it could be delivered"


def connect(path):
    """A connection with manual transactions, like migrations._connect."""

    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backoff_seconds(count, settings):
    """Delay before try number count+1: base, 2x base, 4x base... capped."""

    exponent = max(0, int(count) - 1)

    # Cap the exponent first so huge counts cannot build a huge number
    delay = settings.retry_base_seconds * (2 ** min(exponent, 30))
    return min(delay, settings.retry_max_seconds)


@dataclass(frozen=True)
class Claim:
    """A row reserved for this worker. `row` is the notification as claimed."""

    row: sqlite3.Row
    token: str

    @property
    def id(self):
        return self.row["id"]

    @property
    def attempt(self):
        return self.row["attempts"]


def _record_attempt(conn, notification_id, attempt, *, worker, mode,
                    started_at, finished_at, outcome, error=None):
    conn.execute(
        """
        INSERT INTO notification_attempts
        (notification_id, attempt, worker, delivery_mode, started_at,
         finished_at, outcome, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (notification_id, attempt, worker, mode, started_at, finished_at,
         outcome, error)
    )


def _recover_expired_claims(conn, now, settings):
    """
    Return rows whose worker stopped mid-delivery to the queue.

    The interrupted delivery keeps its attempt: it may have been sent.
    Returns the number of rows recovered.
    """

    now_text = timeutil.format_utc(now)

    stale = conn.execute(
        """
        SELECT id, attempts, claimed_by, claimed_at, delivery_mode
        FROM notifications
        WHERE status = 'processing' AND claimed_until <= ?
        ORDER BY id
        """,
        (now_text,)
    ).fetchall()

    for row in stale:
        _record_attempt(
            conn, row["id"], row["attempts"],
            worker=row["claimed_by"] or "unknown",
            mode=row["delivery_mode"] or "unknown",
            started_at=row["claimed_at"] or now_text,
            finished_at=now_text,
            outcome="interrupted",
            error=INTERRUPTED_ERROR,
        )

        if row["attempts"] >= settings.max_attempts:
            status, next_at = FAILED, now_text
        else:
            status = PENDING
            next_at = timeutil.format_utc(
                now + timedelta(
                    seconds=backoff_seconds(row["attempts"], settings)
                )
            )

        conn.execute(
            """
            UPDATE notifications
            SET status = ?, next_attempt_at = ?, last_error = ?,
                claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                claimed_until = NULL, updated_at = ?
            WHERE id = ?
            """,
            (status, next_at, INTERRUPTED_ERROR, now_text, row["id"])
        )

    return len(stale)


def _expire_old(conn, now, settings, *, worker, mode):
    """Fail pending rows older than max_age_hours. Returns how many."""

    now_text = timeutil.format_utc(now)
    cutoff = timeutil.format_utc(now - timedelta(hours=settings.max_age_hours))

    old = conn.execute(
        """
        SELECT id, attempts FROM notifications
        WHERE status = 'pending' AND created_at < ?
        ORDER BY id
        """,
        (cutoff,)
    ).fetchall()

    for row in old:
        _record_attempt(
            conn, row["id"], row["attempts"], worker=worker, mode=mode,
            started_at=now_text, finished_at=now_text, outcome="failed",
            error=EXPIRED_ERROR,
        )
        conn.execute(
            """
            UPDATE notifications
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (EXPIRED_ERROR, now_text, row["id"])
        )

    return len(old)


def claim_next(path, settings, *, worker, mode):
    """
    Reserve the next due notification for this worker, or return None.

    Also recovers expired claims and expires old rows, in the same short
    write transaction. Returns (Claim or None, recovered, expired).
    """

    now = timeutil.utc_now()
    now_text = timeutil.format_utc(now)

    conn = connect(path)

    try:
        conn.execute("BEGIN IMMEDIATE")

        try:
            recovered = _recover_expired_claims(conn, now, settings)
            expired = _expire_old(conn, now, settings, worker=worker,
                                  mode=mode)

            row = conn.execute(
                """
                SELECT id FROM notifications
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT 1
                """,
                (now_text,)
            ).fetchone()

            claim = None

            if row is not None:
                token = secrets.token_hex(16)
                until = timeutil.format_utc(
                    now + timedelta(seconds=settings.lease_seconds)
                )

                conn.execute(
                    """
                    UPDATE notifications
                    SET status = 'processing', attempts = attempts + 1,
                        claim_token = ?, claimed_by = ?, claimed_at = ?,
                        claimed_until = ?, delivery_mode = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (token, worker, now_text, until, mode, now_text,
                     row["id"])
                )

                claimed = conn.execute(
                    "SELECT * FROM notifications WHERE id = ?", (row["id"],)
                ).fetchone()

                claim = Claim(claimed, token)

            conn.execute("COMMIT")
            return claim, recovered, expired

        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    finally:
        conn.close()


def finish(path, claim, settings, *, worker, outcome, error=None):
    """
    Record the result of delivering a claimed notification.

    outcome is one of: sent, simulated, retry, deferred, failed,
    cancelled. Returns the row's new status, or None if this worker no
    longer holds the claim (its lease expired and the row was recovered).
    """

    now = timeutil.utc_now()
    now_text = timeutil.format_utc(now)

    conn = connect(path)

    try:
        conn.execute("BEGIN IMMEDIATE")

        try:
            current = conn.execute(
                """
                SELECT attempts, deferrals, created_at, claimed_at,
                       delivery_mode
                FROM notifications
                WHERE id = ? AND status = 'processing' AND claim_token = ?
                """,
                (claim.id, claim.token)
            ).fetchone()

            if current is None:
                conn.execute("ROLLBACK")
                return None

            attempts = current["attempts"]
            deferrals = current["deferrals"]
            sent_at = None
            next_at = now_text

            if outcome in (SENT, SIMULATED):
                status = outcome
                sent_at = now_text
                error = None

            elif outcome == "retry":
                if attempts >= settings.max_attempts:
                    status = FAILED
                else:
                    status = PENDING
                    next_at = timeutil.format_utc(
                        now + timedelta(
                            seconds=backoff_seconds(attempts, settings)
                        )
                    )

            elif outcome == "deferred":
                # Nothing was handed to the server: give the attempt back
                attempts -= 1
                deferrals += 1
                created = timeutil.parse_utc(current["created_at"])

                if now - created >= timedelta(hours=settings.max_age_hours):
                    status = FAILED
                    error = f"{error}; {EXPIRED_ERROR}"
                else:
                    status = PENDING
                    next_at = timeutil.format_utc(
                        now + timedelta(
                            seconds=backoff_seconds(deferrals, settings)
                        )
                    )

            elif outcome == FAILED:
                status = FAILED

            elif outcome == CANCELLED:
                # Decided before anything was sent
                attempts -= 1
                status = CANCELLED

            else:
                raise ValueError(f"unknown delivery outcome: {outcome}")

            conn.execute(
                """
                UPDATE notifications
                SET status = ?, attempts = ?, deferrals = ?,
                    next_attempt_at = ?, sent_at = COALESCE(?, sent_at),
                    last_error = ?, claim_token = NULL, claimed_by = NULL,
                    claimed_at = NULL, claimed_until = NULL, updated_at = ?
                WHERE id = ? AND claim_token = ?
                """,
                (status, attempts, deferrals, next_at, sent_at,
                 error and error[:200], now_text, claim.id, claim.token)
            )

            _record_attempt(
                conn, claim.id, claim.attempt, worker=worker,
                mode=current["delivery_mode"] or "unknown",
                started_at=current["claimed_at"] or now_text,
                finished_at=now_text,
                outcome="failed" if (status == FAILED
                                     and outcome != FAILED) else outcome,
                error=error and error[:200],
            )

            conn.execute("COMMIT")
            return status

        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    finally:
        conn.close()


# ---------------------------------------------------
# DELIVERY-TIME RECIPIENT CHECK (read-only)
# ---------------------------------------------------

def recipient_problem(path, row, staff_roles, staff_reasons):
    """
    Check the recipient again just before delivery.

    Returns (email_address, None) when the message may be sent, or
    (None, reason) when it must be cancelled. The account must still
    exist under the same id and username, be active, have an address,
    and still be allowed to see what the message is about.
    """

    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
    conn.row_factory = sqlite3.Row

    try:
        account = conn.execute(
            "SELECT username, role, is_active, email FROM users WHERE id = ?",
            (row["recipient_user_id"],)
        ).fetchone()

        if account is None or account["username"] != row["recipient_username"]:
            return None, "recipient account no longer exists"

        if account["is_active"] != 1:
            return None, "recipient account is deactivated"

        reason = row["recipient_reason"]

        if reason in staff_reasons and account["role"] not in staff_roles:
            return None, "recipient is no longer support staff"

        if row["ticket_id"] is not None:
            ticket = conn.execute(
                "SELECT created_by FROM tickets WHERE id = ?",
                (row["ticket_id"],)
            ).fetchone()

            if ticket is None:
                return None, "ticket no longer exists"

            if reason == "requester" and (
                ticket["created_by"] != account["username"]
            ):
                return None, "recipient is no longer the requester"

        if not account["email"]:
            return None, "recipient has no email address"

        return account["email"], None

    finally:
        conn.close()


# ---------------------------------------------------
# STATUS (read-only)
# ---------------------------------------------------

def status_counts(path):
    """{status: count} for every status, plus the oldest due pending row."""

    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
    conn.row_factory = sqlite3.Row

    try:
        counts = dict.fromkeys(STATUSES, 0)

        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM notifications GROUP BY status"
        ):
            counts[row["status"]] = row["n"]

        oldest = conn.execute(
            "SELECT MIN(created_at) FROM notifications "
            "WHERE status = 'pending'"
        ).fetchone()[0]

        failures = conn.execute(
            """
            SELECT id, event_type, recipient_username, attempts,
                   deferrals, last_error, updated_at
            FROM notifications WHERE status = 'failed'
            ORDER BY updated_at DESC, id DESC LIMIT 10
            """
        ).fetchall()

        return counts, oldest, failures

    finally:
        conn.close()
