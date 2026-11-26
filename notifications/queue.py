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
from pathlib import Path

from . import events, timeutil

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


# ---------------------------------------------------
# HEALTH (read-only, for monitoring)
# ---------------------------------------------------

@dataclass(frozen=True)
class QueueHealth:
    pending: int
    due: int
    oldest_due_seconds: float      # 0 when nothing is due
    stuck_processing: int          # claims whose lease has expired
    failed_last_day: int
    queue_failures_last_day: int   # changes saved without notifications


def health(path, now=None):
    """Numbers a monitor needs. Counts only; no addresses or content."""

    now = now or timeutil.utc_now()
    now_text = timeutil.format_utc(now)
    day_ago = timeutil.format_utc(now - timedelta(days=1))

    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=LOCK_TIMEOUT)

    try:
        pending, due, oldest = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(next_attempt_at <= ?),
                   MIN(CASE WHEN next_attempt_at <= ? THEN next_attempt_at END)
            FROM notifications WHERE status = 'pending'
            """,
            (now_text, now_text)
        ).fetchone()

        stuck = conn.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE status = 'processing' AND claimed_until <= ?",
            (now_text,)
        ).fetchone()[0]

        failed = conn.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE status = 'failed' AND updated_at >= ?",
            (day_ago,)
        ).fetchone()[0]

        # audit_events.created_at is SQLite's CURRENT_TIMESTAMP (UTC)
        queue_failures = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = ? "
            "AND created_at >= ?",
            (events.QUEUE_FAILED_ACTION,
             (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
        ).fetchone()[0]

    finally:
        conn.close()

    oldest_seconds = 0.0
    if oldest:
        oldest_seconds = max(
            0.0, (now - timeutil.parse_utc(oldest)).total_seconds())

    return QueueHealth(pending, due or 0, oldest_seconds, stuck, failed,
                       queue_failures)


# ---------------------------------------------------
# MANUAL REQUEUE (operator command, audited)
# ---------------------------------------------------

# How many times one original notification may be requeued by hand. Each
# requeue gets a full set of delivery attempts, so this bounds how many
# messages a misbehaving operator script could ever cause.
MAX_REQUEUES = 3

MAX_REASON_LENGTH = 200


class RequeueError(Exception):
    """The requeue was refused. The message is safe to print."""


def _root_id(row):
    key = row["dedupe_key"]

    if key.startswith("requeue:"):
        try:
            return int(key.split(":")[1])
        except (IndexError, ValueError):
            pass

    return row["id"]


def requeue(path, notification_id, *, operator, reason, now=None):
    """
    Queue a new copy of a permanently failed notification.

    The failed row and its attempt history stay exactly as they are; the
    copy is a new row (dedupe key "requeue:<original id>:<n>") with fresh
    attempts and a fresh age limit, delivered by the worker like any
    other, including the recipient check at delivery time. Only "failed"
    rows can be requeued (a "cancelled" one would be cancelled again),
    each original at most MAX_REQUEUES times, and every requeue is
    written to audit_events with the operator and the reason, in the
    same transaction. Returns the new notification's id.
    """

    reason = " ".join((reason or "").split())

    if not reason:
        raise RequeueError("a reason is required")

    if len(reason) > MAX_REASON_LENGTH:
        raise RequeueError(f"the reason is longer than {MAX_REASON_LENGTH} "
                           "characters")

    now_text = timeutil.format_utc(now or timeutil.utc_now())
    conn = connect(path)

    try:
        conn.execute("BEGIN IMMEDIATE")

        try:
            row = conn.execute("SELECT * FROM notifications WHERE id = ?",
                               (notification_id,)).fetchone()

            if row is None:
                raise RequeueError(f"notification {notification_id} does "
                                   "not exist")

            if row["status"] != FAILED:
                raise RequeueError(
                    f"notification {notification_id} is {row['status']}; "
                    "only failed notifications can be requeued"
                )

            root = _root_id(row)
            copies = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE dedupe_key LIKE ?",
                (f"requeue:{root}:%",)
            ).fetchone()[0]

            if copies >= MAX_REQUEUES:
                raise RequeueError(
                    f"notification {root} has already been requeued "
                    f"{copies} times, the maximum; investigate the cause "
                    "instead"
                )

            cursor = conn.execute(
                """
                INSERT INTO notifications
                (event_type, dedupe_key, recipient_user_id,
                 recipient_username, recipient_reason, ticket_id, payload,
                 status, created_at, updated_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (row["event_type"], f"requeue:{root}:{copies + 1}",
                 row["recipient_user_id"], row["recipient_username"],
                 row["recipient_reason"], row["ticket_id"], row["payload"],
                 now_text, now_text, now_text)
            )
            new_id = cursor.lastrowid

            conn.execute(
                """
                INSERT INTO audit_events
                (actor, action, target_user_id, target_username,
                 old_value, new_value)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"cli:{operator}", events.REQUEUED_ACTION,
                 row["recipient_user_id"], row["recipient_username"],
                 f"notification {notification_id} ({row['event_type']})",
                 f"notification {new_id}; reason: {reason}")
            )

            conn.execute("COMMIT")
            return new_id

        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    finally:
        conn.close()
