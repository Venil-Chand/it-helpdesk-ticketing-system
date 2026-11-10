
import sqlite3
import os


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
    """Create database tables if they do not already exist."""

    conn = get_db()
    c = conn.cursor()

    # Create users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user'
        )
    ''')

    # Create tickets table
    c.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'Medium',
            status      TEXT NOT NULL DEFAULT 'Open',
            assigned_to TEXT,
            created_by  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create comments table
    c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id  INTEGER NOT NULL,
            author     TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    ''')

    # ---------------------------------------------------
    # DATABASE INDEXES
    # ---------------------------------------------------

    # Improve ticket retrieval for individual users
    # and support ordering by creation time
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_tickets_owner_date
        ON tickets (created_by, created_at DESC, id DESC)
    ''')

    # Let staff pages read the newest tickets in order from the index,
    # instead of sorting the whole table on every dashboard request
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_tickets_date
        ON tickets (created_at DESC, id DESC)
    ''')

    # ---------------------------------------------------
    # TICKET ACTIVITY HISTORY
    # ---------------------------------------------------

    # One ticket has many history events. created_at uses SQLite's
    # CURRENT_TIMESTAMP, which is always UTC.
    c.execute('''
        CREATE TABLE IF NOT EXISTS ticket_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id  INTEGER NOT NULL,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL CHECK (action IN (
                           'created', 'status_changed', 'assigned',
                           'reassigned', 'unassigned', 'commented'
                       )),
            field_name TEXT,
            old_value  TEXT,
            new_value  TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    ''')

    # Fetch one ticket's events in order without sorting the table
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket
        ON ticket_history (ticket_id, created_at DESC, id DESC)
    ''')

    # History is append-only: the database refuses edits and deletions,
    # so the application cannot silently rewrite what happened
    c.execute('''
        CREATE TRIGGER IF NOT EXISTS ticket_history_no_update
        BEFORE UPDATE ON ticket_history
        BEGIN
            SELECT RAISE(ABORT, 'ticket history is append-only');
        END
    ''')

    c.execute('''
        CREATE TRIGGER IF NOT EXISTS ticket_history_no_delete
        BEFORE DELETE ON ticket_history
        BEGIN
            SELECT RAISE(ABORT, 'ticket history is append-only');
        END
    ''')

    # Save database changes
    conn.commit()

    # Close database connection
    conn.close()


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
    priority_filter="All"
):
    """
    Search tickets by title or description.

    If username is provided, retrieve only that user's
    tickets. Otherwise, search all tickets.

    Optional status and priority filters are supported.
    """

    where, params = _ticket_filters(
        search_text=search_text,
        username=username,
        status_filter=status_filter,
        priority_filter=priority_filter
    )

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
    priority_filter="All"
):
    """Count the tickets matching the same filters as get_tickets_page()."""

    where, params = _ticket_filters(
        search_text=search_text,
        username=username,
        status_filter=status_filter,
        priority_filter=priority_filter
    )

    conn = get_db()

    try:
        return conn.execute(
            "SELECT COUNT(*) FROM tickets" + where,
            params
        ).fetchone()[0]

    finally:
        conn.close()


def get_tickets_page(
    *,
    per_page,
    page=1,
    search_text="",
    username=None,
    status_filter="All",
    priority_filter="All"
):
    """
    Retrieve one page of matching tickets, newest first.

    Only the rows for the requested page are read from the database,
    using LIMIT and OFFSET. Pages are numbered from 1, and a page past
    the end returns an empty list.
    """

    # Keep the page number and page size inside safe bounds
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))

    # Page 1 skips 0 rows, page 2 skips per_page rows, and so on
    offset = (page - 1) * per_page

    where, params = _ticket_filters(
        search_text=search_text,
        username=username,
        status_filter=status_filter,
        priority_filter=priority_filter
    )

    # id DESC breaks ties between tickets created in the same second,
    # giving every ticket one fixed position so pages never overlap
    query = (
        "SELECT * FROM tickets"
        + where
        + " ORDER BY created_at DESC, id DESC"
        + " LIMIT ? OFFSET ?"
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
        # "with conn" commits both statements together on success, or
        # rolls both back if either fails, so a ticket can never exist
        # without its "created" event
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
        # The comment, the timestamp update and the history event are
        # saved together or not at all
        with conn:
            # Insert a new comment
            conn.execute(
                '''
                INSERT INTO comments (ticket_id, author, content)
                VALUES (?, ?, ?)
                ''',
                (ticket_id, author, content)
            )

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


# ---------------------------------------------------
# USER FUNCTIONS
# ---------------------------------------------------

def get_user(username):
    """Retrieve a user using their username."""

    conn = get_db()

    try:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        return user

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
