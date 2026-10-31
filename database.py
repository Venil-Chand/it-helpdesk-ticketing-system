
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


def get_db():
    """Connect to the SQLite database."""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

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

    # Save changes to the database
    conn.commit()

    # Close the connection
    conn.close()


# ---------------------------------------------------
# TICKET FUNCTIONS
# ---------------------------------------------------

def get_all_tickets(status_filter=None, priority_filter=None):
    """Retrieve tickets with optional status and priority filters."""

    conn = get_db()

    query = "SELECT * FROM tickets"
    params = []
    conditions = []

    # Filter tickets by status
    if status_filter and status_filter != "All":
        conditions.append("status = ?")
        params.append(status_filter)

    # Filter tickets by priority
    if priority_filter and priority_filter != "All":
        conditions.append("priority = ?")
        params.append(priority_filter)

    # Add filtering conditions to the SQL query
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    # Display the newest tickets first
    query += " ORDER BY created_at DESC"

    tickets = conn.execute(query, params).fetchall()

    conn.close()

    return tickets


def get_ticket(ticket_id):
    """Retrieve a single ticket using its ID."""

    conn = get_db()

    ticket = conn.execute(
        "SELECT * FROM tickets WHERE id = ?",
        (ticket_id,)
    ).fetchone()

    conn.close()

    return ticket


def create_ticket(title, description, priority, created_by):
    """Create a new helpdesk ticket."""

    conn = get_db()

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

    # Save the new ticket
    conn.commit()
    conn.close()

    return ticket_id


def update_ticket(ticket_id, status, assigned_to):
    """Update the status and assigned technician of a ticket."""

    conn = get_db()

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

    conn.commit()
    conn.close()


# ---------------------------------------------------
# COMMENT FUNCTIONS
# ---------------------------------------------------

def get_comments(ticket_id):
    """Retrieve all comments belonging to a ticket."""

    conn = get_db()

    comments = conn.execute(
        '''
        SELECT * FROM comments
        WHERE ticket_id = ?
        ORDER BY created_at ASC
        ''',
        (ticket_id,)
    ).fetchall()

    conn.close()

    return comments


def add_comment(ticket_id, author, content):
    """Add a comment to a ticket."""

    conn = get_db()

    # Insert the new comment
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

    conn.commit()
    conn.close()


# ---------------------------------------------------
# DASHBOARD STATISTICS
# ---------------------------------------------------

def get_stats():
    """Calculate ticket statistics for the dashboard."""

    conn = get_db()

    stats = {
        "total": conn.execute(
            "SELECT COUNT(*) FROM tickets"
        ).fetchone()[0],

        "open": conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'Open'"
        ).fetchone()[0],

        "in_progress": conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'In Progress'"
        ).fetchone()[0],

        "critical": conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE priority = 'Critical'"
        ).fetchone()[0]
    }

    conn.close()

    return stats


# ---------------------------------------------------
# USER FUNCTIONS
# ---------------------------------------------------

def get_user(username):
    """Retrieve a user using their username."""

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE username = ?",
        (username,)
    ).fetchone()

    conn.close()

    return user


def get_all_usernames():
    """Retrieve all usernames for ticket assignment."""

    conn = get_db()

    rows = conn.execute(
        "SELECT username FROM users"
    ).fetchall()

    conn.close()

    return [row["username"] for row in rows]
