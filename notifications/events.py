"""
Which events send notifications, and to whom.

The hooks below are called by database.py and accounts.py with their own
open connection, inside the transaction that makes the change, right
after the ticket history or audit event is written. They only INSERT rows
into the notifications table; nothing is sent here. The worker delivers
the rows after the transaction has committed, so an email can never
describe a change that was rolled back.

Each hook runs inside a SAVEPOINT. If working out the notifications fails
for any reason, only the savepoint is rolled back, an error is logged, and
the ticket or account change is still saved: a notification problem never
undoes a valid ticket operation.

Recipient rules (also in docs/NOTIFICATIONS.md, for review):

  ticket_created       the requester (confirmation) and every active
                       technician and administrator except the requester
  ticket_assigned      the new assignee, unless they assigned it to
                       themselves
  ticket_unassigned    the previous assignee on reassignment or
                       unassignment, unless they did it themselves
  staff_comment        the requester and the assignee, except the author
  requester_comment    the assignee, unless that is the author
  ticket_resolved/     the requester, unless they made the change
  ticket_closed
  password_changed     the account owner (also when they did it
                       themselves: it alerts them if someone else did)
  password_reset       the account owner
  role_changed         the account owner
  account_reactivated  the account owner

Every recipient must be an existing, active account at the moment the
row is queued; staff-only notifications also need a technician or admin
role. A historical assignee that no longer qualifies (deleted,
deactivated, demoted, or a name that was never an account) is skipped.
The worker checks all of this again just before delivery.

What a message contains is limited to the fields in ALLOWED_PAYLOAD_KEYS:
ticket number, priority, statuses, roles and the acting username. Ticket
titles, descriptions, comment text, passwords, hashes, reset tokens and
session cookies are never stored in the queue or put in a message.
"""

import json
import logging

from . import timeutil

logger = logging.getLogger(__name__)

# Event types (the CHECK constraint of migration 10 lists the same values)
TICKET_CREATED = "ticket_created"
TICKET_ASSIGNED = "ticket_assigned"
TICKET_UNASSIGNED = "ticket_unassigned"
STAFF_COMMENT = "staff_comment"
REQUESTER_COMMENT = "requester_comment"
TICKET_RESOLVED = "ticket_resolved"
TICKET_CLOSED = "ticket_closed"
PASSWORD_CHANGED = "password_changed"
PASSWORD_RESET = "password_reset"
ROLE_CHANGED = "role_changed"
ACCOUNT_REACTIVATED = "account_reactivated"

TICKET_EVENTS = frozenset({
    TICKET_CREATED, TICKET_ASSIGNED, TICKET_UNASSIGNED, STAFF_COMMENT,
    REQUESTER_COMMENT, TICKET_RESOLVED, TICKET_CLOSED,
})

ACCOUNT_EVENTS = frozenset({
    PASSWORD_CHANGED, PASSWORD_RESET, ROLE_CHANGED, ACCOUNT_REACTIVATED,
})

EVENT_TYPES = TICKET_EVENTS | ACCOUNT_EVENTS

# Why an account receives a notification
REQUESTER = "requester"
ASSIGNEE = "assignee"
PREVIOUS_ASSIGNEE = "previous_assignee"
STAFF = "staff"
ACCOUNT_OWNER = "account_owner"

# Reasons that require a technician or administrator account, at queue
# time and again at delivery time
STAFF_REASONS = frozenset({ASSIGNEE, PREVIOUS_ASSIGNEE, STAFF})

# The roles that see every ticket. Kept equal to database.ASSIGNABLE_ROLES
# and auth.STAFF_ROLES (a test checks); not imported, because database.py
# imports this module.
STAFF_ROLES = frozenset({"technician", "admin"})

# Ticket status changes that notify the requester
STATUS_EVENTS = {
    "Resolved": TICKET_RESOLVED,
    "Closed": TICKET_CLOSED,
}

# The only fields a queued notification may carry
ALLOWED_PAYLOAD_KEYS = frozenset({
    "ticket_id", "priority", "old_status", "new_status", "actor",
    "old_role", "new_role", "changed_by",
})

# Longest value stored for any payload field
MAX_PAYLOAD_VALUE_LENGTH = 150

# Shown instead of the operator or administrator name in account emails
CHANGED_BY_ADMINISTRATOR = "administrator"
CHANGED_BY_OWNER = "owner"


# ---------------------------------------------------
# QUEUEING
# ---------------------------------------------------

def _clean_payload(payload):
    """Keep only allowed fields, as short strings or integers."""

    unknown = set(payload) - ALLOWED_PAYLOAD_KEYS

    if unknown:
        # A programming error: fail loudly (inside the savepoint) rather
        # than quietly storing something that was never reviewed
        raise ValueError(f"payload fields not allowed: {sorted(unknown)}")

    cleaned = {}

    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"payload field {key} must be text or a number")
        if isinstance(value, str):
            value = value[:MAX_PAYLOAD_VALUE_LENGTH]
        cleaned[key] = value

    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


def _queue(conn, *, source, event_type, recipient, reason, payload,
           ticket_id=None):
    """
    Insert one notification unless the same one is already queued.

    The dedupe key names the source record (one history or audit event),
    the event and the recipient, so running a hook twice for the same
    event never produces a second message. Returns 1 if a row was added.
    """

    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown notification event: {event_type}")

    now = timeutil.format_utc(timeutil.utc_now())
    dedupe_key = f"{source}:{event_type}:user-{recipient['id']}"

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO notifications
        (event_type, dedupe_key, recipient_user_id, recipient_username,
         recipient_reason, ticket_id, payload, status, created_at,
         updated_at, next_attempt_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (event_type, dedupe_key, recipient["id"], recipient["username"],
         reason, ticket_id, _clean_payload(payload), now, now, now)
    )

    return cursor.rowcount


def _guarded(conn, what, work):
    """
    Run `work()` inside a savepoint of the caller's transaction.

    On any error the notifications are rolled back, the caller's change
    is not, and the error is logged without any stored data. Returns the
    number of notifications queued (0 after an error).
    """

    conn.execute("SAVEPOINT queue_notifications")

    try:
        queued = work()

    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT queue_notifications")
        conn.execute("RELEASE SAVEPOINT queue_notifications")
        logger.exception(
            "Notifications for %s could not be queued; the change itself "
            "was saved without them", what
        )
        return 0

    conn.execute("RELEASE SAVEPOINT queue_notifications")
    return queued


# ---------------------------------------------------
# RECIPIENT LOOKUPS (inside the caller's transaction)
# ---------------------------------------------------

def _active_account(conn, username):
    """The active account with exactly this username, or None."""

    if not username:
        return None

    account = conn.execute(
        "SELECT id, username, role, is_active FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    if account is None or account["is_active"] != 1:
        return None

    return account


def _active_staff(conn, username):
    """The account if it is active staff that may hold tickets, or None."""

    account = _active_account(conn, username)

    if account is None or account["role"] not in STAFF_ROLES:
        return None

    return account


def _active_account_by_id(conn, user_id):
    account = conn.execute(
        "SELECT id, username, role, is_active FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    if account is None or account["is_active"] != 1:
        return None

    return account


def _ticket(conn, ticket_id):
    return conn.execute(
        "SELECT id, priority, status, created_by, assigned_to "
        "FROM tickets WHERE id = ?",
        (ticket_id,)
    ).fetchone()


class _Recipients:
    """Ordered recipients for one event; each account at most once."""

    def __init__(self):
        self._items = []
        self._seen = set()

    def add(self, account, reason, event_type, *, unless=None):
        if account is None or account["username"] == unless:
            return
        if account["id"] in self._seen:
            return
        self._seen.add(account["id"])
        self._items.append((account, reason, event_type))

    def __iter__(self):
        return iter(self._items)


def _queue_all(conn, recipients, *, source, ticket_id, payload):
    return sum(
        _queue(conn, source=source, event_type=event_type,
               recipient=account, reason=reason, payload=payload,
               ticket_id=ticket_id)
        for account, reason, event_type in recipients
    )


# ---------------------------------------------------
# TICKET HOOKS (called by database.py)
# ---------------------------------------------------

def ticket_created(conn, *, ticket_id, history_id):
    """A new ticket: confirm to the requester and tell support staff."""

    def work():
        ticket = _ticket(conn, ticket_id)
        requester = ticket["created_by"]

        recipients = _Recipients()
        recipients.add(_active_account(conn, requester), REQUESTER,
                       TICKET_CREATED)

        roles = sorted(STAFF_ROLES)
        staff = conn.execute(
            f"""
            SELECT id, username, role, is_active FROM users
            WHERE is_active = 1
              AND role IN ({", ".join("?" for _ in roles)})
            ORDER BY id
            """,
            roles
        ).fetchall()

        for account in staff:
            recipients.add(account, STAFF, TICKET_CREATED, unless=requester)

        return _queue_all(
            conn, recipients,
            source=f"ticket-history:{history_id}",
            ticket_id=ticket_id,
            payload={"ticket_id": ticket_id,
                     "priority": ticket["priority"],
                     "actor": requester},
        )

    return _guarded(conn, f"new ticket {ticket_id}", work)


def ticket_changed(conn, *, ticket_id, actor, changes):
    """
    Status and assignment changes saved by database.update_ticket().

    changes is [(history_id, action, field_name, old_value, new_value)].
    """

    def work():
        ticket = _ticket(conn, ticket_id)
        queued = 0

        for history_id, action, _field, old_value, new_value in changes:
            recipients = _Recipients()
            payload = {"ticket_id": ticket_id,
                       "priority": ticket["priority"],
                       "actor": actor}

            if action in ("assigned", "reassigned"):
                recipients.add(_active_staff(conn, new_value), ASSIGNEE,
                               TICKET_ASSIGNED, unless=actor)

            if action in ("reassigned", "unassigned"):
                recipients.add(_active_staff(conn, old_value),
                               PREVIOUS_ASSIGNEE, TICKET_UNASSIGNED,
                               unless=actor)

            if action == "status_changed" and new_value in STATUS_EVENTS:
                payload.update(old_status=old_value, new_status=new_value)
                recipients.add(
                    _active_account(conn, ticket["created_by"]), REQUESTER,
                    STATUS_EVENTS[new_value], unless=actor
                )

            queued += _queue_all(
                conn, recipients,
                source=f"ticket-history:{history_id}",
                ticket_id=ticket_id, payload=payload,
            )

        return queued

    return _guarded(conn, f"changes to ticket {ticket_id}", work)


def comment_added(conn, *, ticket_id, history_id, author):
    """A new comment: staff replies reach the requester, and vice versa."""

    def work():
        ticket = _ticket(conn, ticket_id)
        requester = ticket["created_by"]
        recipients = _Recipients()

        author_account = conn.execute(
            "SELECT role FROM users WHERE username = ?", (author,)
        ).fetchone()
        author_is_staff = (
            author_account is not None
            and author_account["role"] in STAFF_ROLES
        )

        assignee = _active_staff(conn, ticket["assigned_to"])

        # Same rule as the SLA: a requester commenting on their own ticket
        # is a requester comment, even if they are a technician
        if author == requester:
            recipients.add(assignee, ASSIGNEE, REQUESTER_COMMENT,
                           unless=author)

        elif author_is_staff:
            recipients.add(_active_account(conn, requester), REQUESTER,
                           STAFF_COMMENT, unless=author)
            recipients.add(assignee, ASSIGNEE, STAFF_COMMENT, unless=author)

        return _queue_all(
            conn, recipients,
            source=f"ticket-history:{history_id}",
            ticket_id=ticket_id,
            payload={"ticket_id": ticket_id,
                     "priority": ticket["priority"],
                     "actor": author},
        )

    return _guarded(conn, f"a comment on ticket {ticket_id}", work)


# ---------------------------------------------------
# ACCOUNT HOOKS (called by accounts.py)
# ---------------------------------------------------

def account_event(conn, *, audit_id, event_type, target_user_id,
                  changed_by=CHANGED_BY_ADMINISTRATOR, old_role=None,
                  new_role=None):
    """
    A security-relevant change to an account: tell its owner.

    The message never says which administrator or operating-system
    account made the change, only "an administrator" or "you".
    """

    if event_type not in ACCOUNT_EVENTS:
        raise ValueError(f"not an account notification: {event_type}")

    def work():
        recipients = _Recipients()
        recipients.add(_active_account_by_id(conn, target_user_id),
                       ACCOUNT_OWNER, event_type)

        return _queue_all(
            conn, recipients,
            source=f"audit:{audit_id}",
            ticket_id=None,
            payload={"changed_by": changed_by, "old_role": old_role,
                     "new_role": new_role},
        )

    return _guarded(conn, f"account event {event_type}", work)
