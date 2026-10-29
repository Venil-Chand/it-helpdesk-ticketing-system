import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'helpdesk.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables and seed default users on first run."""
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'user'
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER   PRIMARY KEY AUTOINCREMENT,
            title       TEXT      NOT NULL,
            description TEXT      NOT NULL,
            priority    TEXT      NOT NULL DEFAULT 'Medium',
            status      TEXT      NOT NULL DEFAULT 'Open',
            assigned_to TEXT,
            created_by  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER   PRIMARY KEY AUTOINCREMENT,
            ticket_id  INTEGER   NOT NULL,
            author     TEXT      NOT NULL,
            content    TEXT      NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    ''')

    # Seed default users only once
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        users = [
            ('admin',  generate_password_hash('admin123'),  'admin'),
            ('venil',  generate_password_hash('venil123'),  'technician'),
            ('staff1', generate_password_hash('staff123'),  'user'),
        ]
        c.executemany(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            users
        )

    conn.commit()
    conn.close()


# ── Ticket helpers ────────────────────────────────────────────────────────────

def get_all_tickets(status_filter=None, priority_filter=None):
    conn = get_db()
    query = 'SELECT * FROM tickets'
    params = []
    conditions = []
    if status_filter and status_filter != 'All':
        conditions.append('status = ?')
        params.append(status_filter)
    if priority_filter and priority_filter != 'All':
        conditions.append('priority = ?')
        params.append(priority_filter)
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY created_at DESC'
    tickets = conn.execute(query, params).fetchall()
    conn.close()
    return tickets


def get_ticket(ticket_id):
    conn = get_db()
    ticket = conn.execute('SELECT * FROM tickets WHERE id = ?', (ticket_id,)).fetchone()
    conn.close()
    return ticket


def create_ticket(title, description, priority, created_by):
    conn = get_db()
    conn.execute(
        'INSERT INTO tickets (title, description, priority, created_by) VALUES (?, ?, ?, ?)',
        (title, description, priority, created_by)
    )
    conn.commit()
    ticket_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()
    return ticket_id


def update_ticket(ticket_id, status, assigned_to):
    conn = get_db()
    conn.execute(
        '''UPDATE tickets
           SET status = ?, assigned_to = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?''',
        (status, assigned_to, ticket_id)
    )
    conn.commit()
    conn.close()


def get_comments(ticket_id):
    conn = get_db()
    comments = conn.execute(
        'SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at ASC',
        (ticket_id,)
    ).fetchall()
    conn.close()
    return comments


def add_comment(ticket_id, author, content):
    conn = get_db()
    conn.execute(
        'INSERT INTO comments (ticket_id, author, content) VALUES (?, ?, ?)',
        (ticket_id, author, content)
    )
    conn.execute(
        'UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (ticket_id,)
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = get_db()
    stats = {
        'total':       conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],
        'open':        conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'Open'").fetchone()[0],
        'in_progress': conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'In Progress'").fetchone()[0],
        'critical':    conn.execute("SELECT COUNT(*) FROM tickets WHERE priority = 'Critical'").fetchone()[0],
    }
    conn.close()
    return stats


# ── User helpers ──────────────────────────────────────────────────────────────

def get_user(username):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return user


def get_all_usernames():
    conn = get_db()
    rows = conn.execute('SELECT username FROM users').fetchall()
    conn.close()
    return [r['username'] for r in rows]
