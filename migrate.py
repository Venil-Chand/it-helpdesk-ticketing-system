"""
Database migration script.
Upgrades the old schema (ticket_comments / note) to the new schema
(comments / content) and adds the users table.

Run once: python migrate.py
"""

import sqlite3
import os
import shutil
from datetime import datetime
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'helpdesk.db')


def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = DB_PATH.replace('helpdesk.db', f'helpdesk_backup_{ts}.db')
    shutil.copy2(DB_PATH, backup)
    print(f'  Database backed up to: {backup}')
    return backup


def migrate():
    print('Starting database migration...')

    if os.path.exists(DB_PATH):
        backup_db()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ── 1. Create users table ──────────────────────────────────────────────────
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'user'
        )
    ''')

    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        c.executemany(
            'INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)',
            [
                ('admin',  generate_password_hash('admin123'), 'admin'),
                ('venil',  generate_password_hash('venil123'), 'technician'),
                ('staff1', generate_password_hash('staff123'), 'user'),
            ]
        )
        print('  Default users created (admin, venil, staff1)')

    # ── 2. Add new columns to tickets if they don't exist ─────────────────────
    existing_cols = {row[1] for row in c.execute('PRAGMA table_info(tickets)')}

    if 'created_by' not in existing_cols:
        c.execute('ALTER TABLE tickets ADD COLUMN created_by TEXT')
        print('  Added created_by column to tickets')

    if 'updated_at' not in existing_cols:
        # SQLite ALTER TABLE cannot use CURRENT_TIMESTAMP as a default; add column then populate
        c.execute('ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMP')
        c.execute('UPDATE tickets SET updated_at = created_at')
        print('  Added updated_at column to tickets')

    # ── 3. Create new comments table ──────────────────────────────────────────
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

    # ── 4. Migrate data from ticket_comments → comments ───────────────────────
    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ticket_comments' in tables:
        old_comments = c.execute('SELECT * FROM ticket_comments').fetchall()
        migrated = 0
        for row in old_comments:
            # Old schema: id, ticket_id, author, note, created_at
            c.execute(
                'INSERT INTO comments (ticket_id, author, content, created_at) VALUES (?, ?, ?, ?)',
                (row['ticket_id'], row['author'], row['note'], row['created_at'])
            )
            migrated += 1

        # Rename old table rather than dropping it (safety)
        c.execute('ALTER TABLE ticket_comments RENAME TO ticket_comments_migrated')
        print(f'  Migrated {migrated} comments from ticket_comments → comments')
        print('  Old table preserved as ticket_comments_migrated')
    else:
        print('  No ticket_comments table found (already migrated or new install)')

    conn.commit()
    conn.close()
    print('Migration complete.')


if __name__ == '__main__':
    migrate()
