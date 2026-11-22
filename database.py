
import logging
import sqlite3
import os
from datetime import datetime, timezone

import migrations
import sla_storage
from sla import (
    ALERT_WINDOW,
    is_qualifying_response,
    require_aware_datetime
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------
# DATABASE CONFIGURATION
# ---------------------------------------------------

# Location of the SQLite database file
DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "helpdesk.db"
)

# Largest page size any caller may request, to keep queries cheap
MAX_PER_PAGE = 50


def get_db():
    """Connect to the SQLite database."""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # SQLite ignores FOREIGN KEY rules unless each connection enables them
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


# ---------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------

def init_db():
    """
    Create or upgrade the database schema to the latest version.

    The tables, indexes and triggers are defined as numbered migrations
    in migrations.py, applied in order in a single transaction. Running
    this again on an up-to-date database changes nothing.

    In production use "flask --app app init-db" before starting the
    application, rather than calling this from web workers.

    Returns a migrations.MigrationResult. Raises migrations.MigrationError
    or sqlite3.Error on failure, leaving the database unchanged.
    """

    return migrations.migrate(DB_PATH)


# ---------------------------------------------------
# SHARED TICKET FILTERS
# ---------------------------------------------------

def _ticket_filters(
    *,  # keyword-only, so a username can never land in search_text
    search_text="",
    username=None,
    status_filter=None,
    priority_filter=None
):
    """
    Build the WHERE clause and parameters shared by ticket queries.

    username=None means no ownership restriction, which is only
    correct for administrators and technicians. All values are
    returned as bound parameters, never inserted into the SQL text.

    Returns (where_clause, params); where_clause is "" if unfiltered.
    """

    conditions = []
    params = []

    # Restrict results to tickets created by one user
    if username is not None:
        conditions.append("created_by = ?")
        params.append(username)

    # Remove unnecessary spaces from the search text
    search_text = (search_text or "").strip()

    # Search in both title and description
    if search_text:
        conditions.append("(title LIKE ? OR description LIKE ?)")

        # % matches any number of characters
        search_pattern = f"%{search_text}%"
        params.extend([search_pattern, search_pattern])

    # Apply optional status filter
    if status_filter and status_filter != "All":
        conditions.append("status = ?")
        params.append(status_filter)

    # Apply optional priority filter
    if priority_filter and priority_filter != "All":
        conditions.append("priority = ?")
        params.append(priority_filter)

    if not conditions:
        return "", params

    return " WHERE " + " AND ".join(conditions), params


# ---------------------------------------------------
# SLA STATUS IN SQL
# ---------------------------------------------------

# Every SLA status the dashboard shows, in display order. The first five
# are the labels sla.assess_response_sla() returns. "Not Tracked" is for
# tickets with no SLA row (created before SLA tracking) or an SLA row
# whose timestamps cannot be read, which the ticket page also shows
# without an SLA. Such tickets are never counted as overdue.
SLA_STATUS_LABELS = (
    "Within SLA",
    "Due Soon",
    "Overdue",
    "Met",
    "Breached",
    "Not Tracked",
)

# The same rules as sla.assess_response_sla(), written in SQL so the
# database can count and filter tickets without sending every row to
# Python. julianday() turns stored text into a number of days in UTC: it
# treats text without an offset as UTC and converts "+05:30" or "Z" to
# UTC, exactly like sla_storage.parse_stored_utc(). A damaged row becomes
# "Not Tracked" instead of being counted as overdue.
#
# The two ? placeholders are the request's current UTC time and that time
# plus ALERT_WINDOW. Comparing the deadline with "now + window" avoids
# subtracting floating-point day numbers, so the boundaries are exact:
#   response at the deadline   -> Met       (response_at <= deadline)
#   no response at the deadline -> Overdue  (now >= deadline)
#   exactly 30 minutes left     -> Due Soon (deadline <= now + window)


def _readable_timestamp_sql(column):
    """
    SQL that is true only if `column` holds a date-time text value.

    julianday() also accepts things parse_stored_utc() rejects, such as
    '2461000' (read as a Julian day number) or 'now' (the current time),
    so the text must also start with a YYYY-MM-DD date.
    """

    return (
        f"(typeof({column}) = 'text'"
        f" AND {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
        f" AND julianday({column}) IS NOT NULL)"
    )


_SLA_STATUS_SQL = f"""
    CASE
        WHEN s.ticket_id IS NULL
          OR NOT {_readable_timestamp_sql("s.response_due_at")}
          OR (s.first_response_at IS NOT NULL
              AND NOT {_readable_timestamp_sql("s.first_response_at")})
            THEN 'Not Tracked'
        WHEN s.first_response_at IS NOT NULL THEN
            CASE
                WHEN julianday(s.first_response_at)
                     <= julianday(s.response_due_at)
                    THEN 'Met'
                ELSE 'Breached'
            END
        WHEN julianday(?) >= julianday(s.response_due_at)
            THEN 'Overdue'
        WHEN julianday(s.response_due_at) <= julianday(?)
            THEN 'Due Soon'
        ELSE 'Within SLA'
    END
"""

# Each ticket with its SLA columns, as one derived table. The LEFT JOIN
# keeps tickets that have no SLA row. ticket_sla.ticket_id is the table's
# INTEGER PRIMARY KEY, so each lookup is a direct primary-key search.
# None of the extra column names clash with ticket columns, so the shared
# filters (created_by = ?, status = ?, ...) work on it unchanged.
_SLA_TICKET_SOURCE = f"""
    (
        SELECT t.*,
               s.response_due_at   AS sla_response_due_at,
               s.first_response_at AS sla_first_response_at,
               {_SLA_STATUS_SQL}   AS sla_status
        FROM tickets AS t
        LEFT JOIN ticket_sla AS s ON s.ticket_id = t.id
    ) AS ticket_rows
"""


def _sla_clock(now):
    """
    Return the two time parameters for _SLA_STATUS_SQL.

    now=None means the current time. A naive datetime is rejected, since
    its time zone would be a guess. Times are sent to SQLite as UTC text
    with milliseconds, the precision julianday() works with.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    require_aware_datetime(now, "now")

    now_utc = now.astimezone(timezone.utc)

    return [
        now_utc.isoformat(timespec="milliseconds"),
        (now_utc + ALERT_WINDOW).isoformat(timespec="milliseconds"),
    ]


def _sla_ticket_query(columns, *, now, sla_filter, filters, suffix=""):
    """
    Build "SELECT <columns>" over tickets joined with their SLA status.

    filters are the keyword arguments for _ticket_filters(), so ownership
    is applied exactly as in every other ticket query. sla_filter is one
    of SLA_STATUS_LABELS, or "All"/None for no SLA restriction. Every
    value is a bound parameter.

    Returns (sql, params).
    """

    where, params = _ticket_filters(**filters)

    if sla_filter and sla_filter != "All":
        # The route only passes known labels. Anything else is a bug,
        # and must fail loudly rather than silently match nothing.
        if sla_filter not in SLA_STATUS_LABELS:
            raise ValueError(f"Unknown SLA status: {sla_filter!r}")

        where += (" AND " if where else " WHERE ") + "sla_status = ?"
        params.append(sla_filter)

    # The clock placeholders are inside the derived table, which comes
    # before the WHERE clause in the SQL text, so their values go first
    sql = f"SELECT {columns} FROM {_SLA_TICKET_SOURCE}{where}{suffix}"

    return sql, _sla_clock(now) + params


# ---------------------------------------------------
# TICKET FUNCTIONS
# ---------------------------------------------------

def get_all_tickets(status_filter=None, priority_filter=None):
    """Retrieve all tickets with optional filters."""

    return search_tickets(
        status_filter=status_filter,
        priority_filter=priority_filter
    )


# ---------------------------------------------------
# RETRIEVE TICKETS BY USER
# ---------------------------------------------------

def get_tickets_by_user(
    username,
    status_filter=None,
    priority_filter=None
):
    """Retrieve only tickets created by a specific user."""

    # In the shared filters None means "every user", so a missing
    # username must stop here instead of widening the query
    if username is None:
        return []

    return search_tickets(
        username=username,
        status_filter=status_filter,
        priority_filter=priority_filter
    )


# ---------------------------------------------------
# SEARCH TICKETS
# ---------------------------------------------------

def search_tickets(
    search_text="",
    username=None,
    status_filter="All",
    priority_filter="All",
    *,
    sla_filter="All",
    now=None
):
    """
    Search tickets by title or description.

    If username is provided, retrieve only that user's
    tickets. Otherwise, search all tickets.

    Optional status, priority and SLA status filters are supported.
    The SLA status is evaluated at `now` (default: the current UTC time).
    """

    filters = {
        "search_text": search_text,
        "username": username,
        "status_filter": status_filter,
        "priority_filter": priority_filter
    }

    if sla_filter and sla_filter != "All":
        # Only the matching rows change. ticket_rows.* also carries the
        # SLA columns, which callers such as the CSV export ignore.
        query, params = _sla_ticket_query(
            "*",
            now=now,
            sla_filter=sla_filter,
            filters=filters,
            suffix=" ORDER BY created_at DESC, id DESC"
        )

    else:
        # Without an SLA filter this is exactly the original query
        where, params = _ticket_filters(**filters)

        # Display newest matching tickets first
        query = (
            "SELECT * FROM tickets"
            + where
            + " ORDER BY created_at DESC, id DESC"
        )

    conn = get_db()

    try:
        return conn.execute(query, params).fetchall()

    finally:
        conn.close()


# ---------------------------------------------------
# PAGINATED TICKET QUERIES
# ---------------------------------------------------

def count_tickets(
    *,
    search_text="",
    username=None,
    status_filter="All",
    priority_filter="All",
    sla_filter="All",
    now=None
):
    """
    Count the tickets matching the same filters as get_tickets_page().

    Pass the same `now` to both so the SLA filter selects the same
    tickets in the count and on the page.
    """

    filters = {
        "search_text": search_text,
        "username": username,
        "status_filter": status_filter,
        "priority_filter": priority_filter
    }

    if sla_filter and sla_filter != "All":
        query, params = _sla_ticket_query(
            "COUNT(*)",
            now=now,
            sla_filter=sla_filter,
            filters=filters
        )

    else:
        # No SLA filter: count the tickets table alone, skipping the join
        where, params = _ticket_filters(**filters)
        query = "SELECT COUNT(*) FROM tickets" + where

    conn = get_db()

    try:
        return conn.execute(query, params).fetchone()[0]

    finally:
        conn.close()


def get_tickets_page(
    *,
    per_page,
    page=1,
    search_text="",
    username=None,
    status_filter="All",
    priority_filter="All",
    sla_filter="All",
    now=None
):
    """
    Retrieve one page of matching tickets, newest first.

    Only the rows for the requested page are read from the database,
    using LIMIT and OFFSET. Pages are numbered from 1, and a page past
    the end returns an empty list.

    Each row also has sla_status, sla_response_due_at and
    sla_first_response_at, evaluated at `now` (default: the current UTC
    time) in the same query, so the dashboard never runs one SLA query
    per ticket. The SLA filter is applied before LIMIT and OFFSET.
    """

    # Keep the page number and page size inside safe bounds
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))

    # Page 1 skips 0 rows, page 2 skips per_page rows, and so on
    offset = (page - 1) * per_page

    # id DESC breaks ties between tickets created in the same second,
    # giving every ticket one fixed position so pages never overlap
    query, params = _sla_ticket_query(
        "*",
        now=now,
        sla_filter=sla_filter,
        filters={
            "search_text": search_text,
            "username": username,
            "status_filter": status_filter,
            "priority_filter": priority_filter
        },
        suffix=" ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )

    conn = get_db()

    try:
        return conn.execute(
            query,
            params + [per_page, offset]
        ).fetchall()

    finally:
        conn.close()


# ---------------------------------------------------
# GET A SINGLE TICKET
# ---------------------------------------------------

def get_ticket(ticket_id):
    """Retrieve a single ticket using its ID."""

    conn = get_db()

    try:
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE id = ?",
            (ticket_id,)
        ).fetchone()

        return ticket

    finally:
        conn.close()


# ---------------------------------------------------
# CREATE TICKET
# ---------------------------------------------------

def create_ticket(title, description, priority, created_by):
    """Create a new helpdesk ticket and record who created it."""

    conn = get_db()

    try:
        # "with conn" commits every statement together on success, or
        # rolls them all back if any fails, so a ticket can never exist
        # without its "created" event or its SLA deadline
        with conn:
            cursor = conn.execute(
                '''
                INSERT INTO tickets
                (title, description, priority, created_by)
                VALUES (?, ?, ?, ?)
                ''',
                (title, description, priority, created_by)
            )

            # Get the ID of the newly created ticket
            ticket_id = cursor.lastrowid

            _record_event(conn, ticket_id, created_by, "created")

            # Base the deadline on the creation time SQLite stored, so
            # the SLA and the ticket can never disagree about it. An
            # unknown priority raises ValueError and nothing is saved.
            stored = conn.execute(
                "SELECT priority, created_at FROM tickets WHERE id = ?",
                (ticket_id,)
            ).fetchone()

            sla_storage.create_ticket_sla(
                conn,
                ticket_id,
                stored["priority"],
                sla_storage.parse_stored_utc(stored["created_at"])
            )

        return ticket_id

    finally:
        conn.close()


# ---------------------------------------------------
# UPDATE TICKET
# ---------------------------------------------------

def update_ticket(ticket_id, status, assigned_to, *, actor):
    """
    Update ticket status and assignment, recording each real change.

    actor must be the authenticated username, never a form value.

    Returns a list of (action, field, old_value, new_value) tuples for
    the changes made, an empty list if nothing changed, or None if the
    ticket does not exist.
    """

    # Treat "" the same as "no technician"
    assigned_to = assigned_to or None

    conn = get_db()

    try:
        with conn:
            # Lock the database for writing before reading the old values,
            # so a simultaneous update cannot change them in between
            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute(
                "SELECT status, assigned_to FROM tickets WHERE id = ?",
                (ticket_id,)
            ).fetchone()

            if current is None:
                return None

            changes = []

            if status != current["status"]:
                changes.append(
                    ("status_changed", "status", current["status"], status)
                )

            old_assignee = current["assigned_to"] or None

            if assigned_to != old_assignee:
                if old_assignee is None:
                    action = "assigned"
                elif assigned_to is None:
                    action = "unassigned"
                else:
                    action = "reassigned"

                changes.append(
                    (action, "assigned_to", old_assignee, assigned_to)
                )

            # Nothing changed: no update and no misleading history
            if not changes:
                return []

            conn.execute(
                '''
                UPDATE tickets
                SET status = ?,
                    assigned_to = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (status, assigned_to, ticket_id)
            )

            # Status and assignment are recorded as separate events
            for action, field_name, old_value, new_value in changes:
                _record_event(
                    conn, ticket_id, actor, action,
                    field_name, old_value, new_value
                )

        return changes

    finally:
        conn.close()


# ---------------------------------------------------
# COMMENT FUNCTIONS
# ---------------------------------------------------

def get_comments(ticket_id):
    """Retrieve all comments belonging to a ticket."""

    conn = get_db()

    try:
        comments = conn.execute(
            '''
            SELECT * FROM comments
            WHERE ticket_id = ?
            ORDER BY created_at ASC
            ''',
            (ticket_id,)
        ).fetchall()

        return comments

    finally:
        conn.close()


def add_comment(ticket_id, author, content):
    """Add a comment to a helpdesk ticket and record the activity."""

    conn = get_db()

    try:
        # The comment, the timestamp update, the history event and any
        # SLA first response are saved together or not at all
        with conn:
            # Insert a new comment
            cursor = conn.execute(
                '''
                INSERT INTO comments (ticket_id, author, content)
                VALUES (?, ?, ?)
                ''',
                (ticket_id, author, content)
            )

            comment_id = cursor.lastrowid

            # Update the ticket's modification time
            conn.execute(
                '''
                UPDATE tickets
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (ticket_id,)
            )

            _record_event(conn, ticket_id, author, "commented")

            _record_sla_response(conn, comment_id)

    finally:
        conn.close()


def _record_sla_response(conn, comment_id):
    """
    Record a comment as the ticket's first SLA response if it qualifies.

    Uses the caller's open connection and never commits. The author's
    role is read from the users table inside the same transaction, so
    the decision never depends on a value supplied by the caller.

    Returns True only if this comment became the first response.
    """

    row = conn.execute(
        '''
        SELECT c.ticket_id, c.author, c.created_at,
               t.created_by, u.role
        FROM comments AS c
        JOIN tickets AS t ON t.id = c.ticket_id
        LEFT JOIN users AS u ON u.username = c.author
        WHERE c.id = ?
        ''',
        (comment_id,)
    ).fetchone()

    if row is None or not is_qualifying_response(
        row["author"], row["role"], row["created_by"]
    ):
        return False

    # Uses the comment's own stored time. Only the first qualifying
    # response is kept, and tickets without an SLA row are left alone.
    return sla_storage.record_first_response(
        conn,
        row["ticket_id"],
        sla_storage.parse_stored_utc(row["created_at"])
    )


# ---------------------------------------------------
# RESPONSE SLA
# ---------------------------------------------------

def get_ticket_sla_status(ticket_id, now=None):
    """
    Retrieve a ticket's response SLA and its current status.

    Returns None if the ticket has no SLA (created before SLA tracking)
    or if its stored SLA values cannot be read. A damaged row is logged
    rather than allowed to break the ticket page.
    """

    conn = get_db()

    try:
        return sla_storage.get_ticket_sla_status(conn, ticket_id, now=now)

    except (TypeError, ValueError):
        logger.warning(
            "Unreadable SLA record for ticket %s", ticket_id,
            exc_info=True
        )
        return None

    finally:
        conn.close()


# ---------------------------------------------------
# TICKET ACTIVITY HISTORY
# ---------------------------------------------------

def _record_event(
    conn,
    ticket_id,
    actor,
    action,
    field_name=None,
    old_value=None,
    new_value=None
):
    """
    Add one history event using the caller's open connection.

    This deliberately never commits. The caller commits the ticket
    change and its history event together in a single transaction.
    """

    conn.execute(
        '''
        INSERT INTO ticket_history
        (ticket_id, actor, action, field_name, old_value, new_value)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (ticket_id, actor, action, field_name, old_value, new_value)
    )


def get_ticket_history(ticket_id):
    """Retrieve a ticket's history events, newest first."""

    conn = get_db()

    try:
        # id DESC orders events recorded in the same second, such as a
        # status change and an assignment saved together.
        # actor_role is the actor's current role, used only for display.
        return conn.execute(
            '''
            SELECT h.id, h.ticket_id, h.actor, h.action, h.field_name,
                   h.old_value, h.new_value, h.created_at,
                   u.role AS actor_role
            FROM ticket_history AS h
            LEFT JOIN users AS u ON u.username = h.actor
            WHERE h.ticket_id = ?
            ORDER BY h.created_at DESC, h.id DESC
            ''',
            (ticket_id,)
        ).fetchall()

    finally:
        conn.close()


# ---------------------------------------------------
# DASHBOARD STATISTICS
# ---------------------------------------------------

def get_stats(username=None):
    """
    Calculate dashboard statistics.

    If username is None:
        Calculate statistics for all tickets.

    If username is provided:
        Calculate statistics for that user's tickets.
    """

    conn = get_db()

    # Calculate ticket totals using a single SQL query
    query = """
        SELECT
            COUNT(*) AS total,

            COALESCE(
                SUM(
                    CASE WHEN status = 'Open'
                    THEN 1 ELSE 0 END
                ),
                0
            ) AS open,

            COALESCE(
                SUM(
                    CASE WHEN status = 'In Progress'
                    THEN 1 ELSE 0 END
                ),
                0
            ) AS in_progress,

            COALESCE(
                SUM(
                    CASE WHEN priority = 'Critical'
                    THEN 1 ELSE 0 END
                ),
                0
            ) AS critical

        FROM tickets
    """

    params = []

    # Restrict statistics to a particular user
    if username is not None:
        query += " WHERE created_by = ?"
        params.append(username)

    try:
        row = conn.execute(
            query,
            params
        ).fetchone()

        stats = {
            "total": row["total"],
            "open": row["open"],
            "in_progress": row["in_progress"],
            "critical": row["critical"]
        }

        return stats

    finally:
        conn.close()


def get_sla_stats(username=None, now=None):
    """
    Count tickets in each SLA status, evaluated at one moment `now`.

    If username is None:
        Count every ticket (only correct for technicians and admins).

    If username is provided:
        Count only the tickets that user created.

    Like get_stats(), this covers every ticket the user may see, not one
    dashboard page or search result. Returns a dict with every label in
    SLA_STATUS_LABELS, in display order, including those with zero.
    """

    # GROUP BY lets SQLite do all the counting in a single pass, so only
    # at most six (status, count) rows ever reach Python
    query, params = _sla_ticket_query(
        "sla_status, COUNT(*) AS ticket_count",
        now=now,
        sla_filter="All",
        filters={"username": username},
        suffix=" GROUP BY sla_status"
    )

    conn = get_db()

    try:
        rows = conn.execute(query, params).fetchall()

    finally:
        conn.close()

    counts = dict.fromkeys(SLA_STATUS_LABELS, 0)

    for row in rows:
        counts[row["sla_status"]] = row["ticket_count"]

    return counts


# ---------------------------------------------------
# USER FUNCTIONS
# ---------------------------------------------------

def get_user(username):
    """
    Retrieve a user's login details using their username.

    The columns are named so that a database missing is_active raises an
    error here, instead of returning a row the login check cannot read.
    """

    conn = get_db()

    try:
        user = conn.execute(
            """
            SELECT id, username, password_hash, role, is_active
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        return user

    finally:
        conn.close()


def get_user_by_id(user_id):
    """
    Retrieve the current account for a login session.

    The password hash is deliberately not selected: this row is kept for
    the whole request and never needs it.
    """

    conn = get_db()

    try:
        return conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

    finally:
        conn.close()


def get_all_usernames():
    """Retrieve all usernames for ticket assignment."""

    conn = get_db()

    try:
        rows = conn.execute(
            "SELECT username FROM users"
        ).fetchall()

        return [row["username"] for row in rows]

    finally:
        conn.close()
