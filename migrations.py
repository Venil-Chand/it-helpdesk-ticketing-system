
"""
Versioned, transaction-safe SQLite schema migrations.

How it works
------------
SQLite stores a small integer in every database file header, read and
written with ``PRAGMA user_version``. This module uses it as the schema
version:

    0  an empty database, or a database created before versioning existed
    1  core tables: users, tickets, comments
    2  ticket list indexes
    3  append-only ticket activity history
    4  response SLA deadlines (ticket_sla)
    5  account activation status (users.is_active)
    6  session revocation (users.session_version and its trigger)
    7  stable usernames (no renaming, no reuse of names in ticket records)

Each migration in MIGRATIONS moves the schema forward by exactly one
version. migrate() applies every migration above the stored version, in
order, inside ONE transaction:

    BEGIN IMMEDIATE        <- take the write lock; other writers wait
      read user_version    <- read only after the lock is held
      migration N+1 ... latest, each followed by user_version = N
      validate the final schema and foreign keys
    COMMIT                 <- everything is saved at once
    (any error)  ROLLBACK  <- nothing is saved, version is unchanged

Version 0 never means "empty". Databases created before versioning have
user_version = 0 but may already contain any mix of the tables above, so
every migration inspects the real schema and only creates what is
missing. The final validation then checks that the result really matches
what the application expects, and rejects anything incompatible.

Migrations never seed user accounts and never invent history: tickets
that existed before history or SLA tracking keep no history events and
no SLA row.

executescript() is deliberately not used: it COMMITs any open transaction
before running, which would break the all-or-nothing guarantee.
"""

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


class MigrationError(Exception):
    """A migration could not be applied. Nothing was changed."""


class IncompatibleSchemaError(MigrationError):
    """The database does not have the structure this application expects."""


class UnsupportedSchemaVersionError(MigrationError):
    """The database was migrated by a newer version of the application."""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: object  # callable(connection) -> None; must never commit


# Seconds to wait for another process's write lock before giving up
DEFAULT_LOCK_TIMEOUT = 30


# ---------------------------------------------------
# SCHEMA INSPECTION HELPERS
# ---------------------------------------------------

def _tables(conn):
    """Names of the user tables in the database."""

    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn, table):
    """Column name -> PRAGMA table_info row, for one table."""

    # Table names come only from constants in this module, never from
    # user input; PRAGMA arguments cannot be bound as parameters
    return {
        row[1]: row for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _row_count(conn, table):
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------
# MIGRATION 1: CORE TABLES
# ---------------------------------------------------

# Columns the original (pre-account) tickets table already had. A legacy
# tickets table without these is not one this application ever created.
_LEGACY_TICKET_COLUMNS = {
    "id", "title", "description", "priority", "status",
    "assigned_to", "created_at"
}


def _migration_1_core_tables(conn):
    """
    Create users, tickets and comments, upgrading the original layout.

    The original layout had tickets without created_by/updated_at and a
    ticket_comments table with a "note" column. Comments are copied into
    comments with their original ids and timestamps, and the old table is
    kept, renamed to ticket_comments_migrated, rather than dropped.

    No user accounts are created: the first administrator is created
    with setup_accounts.py, which asks for a strong password.
    """

    tables = _tables(conn)

    if "tickets" in tables:
        missing = _LEGACY_TICKET_COLUMNS - set(_columns(conn, "tickets"))
        if missing:
            raise IncompatibleSchemaError(
                "tickets table is missing expected columns: "
                + ", ".join(sorted(missing))
            )

    elif tables:
        # Something else lives in this file; do not add tables to it
        raise IncompatibleSchemaError(
            "database is not empty but has no tickets table; "
            "it does not look like a helpdesk database"
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'user'
        )
    """)

    conn.execute("""
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
    """)

    # Columns added after the original layout. ALTER TABLE cannot add a
    # column with a CURRENT_TIMESTAMP default, so updated_at stays NULL
    # for existing tickets: when they were last updated is not known.
    ticket_columns = _columns(conn, "tickets")

    if "created_by" not in ticket_columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN created_by TEXT")

    if "updated_at" not in ticket_columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMP")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id  INTEGER NOT NULL,
            author     TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        )
    """)

    if "ticket_comments" in tables:
        _copy_legacy_comments(conn, tables)


def _copy_legacy_comments(conn, tables):
    """Move ticket_comments (note) rows into comments (content)."""

    legacy_columns = set(_columns(conn, "ticket_comments"))
    required = {"id", "ticket_id", "author", "note", "created_at"}

    if not required <= legacy_columns:
        raise IncompatibleSchemaError(
            "ticket_comments table has an unexpected structure"
        )

    if "ticket_comments_migrated" in tables:
        raise IncompatibleSchemaError(
            "both ticket_comments and ticket_comments_migrated exist; "
            "refusing to copy comments twice"
        )

    if _row_count(conn, "comments"):
        raise IncompatibleSchemaError(
            "comments already contains rows while legacy ticket_comments "
            "still exists; refusing to merge them automatically"
        )

    # Foreign keys are enforced, so a comment for a missing ticket stops
    # the whole migration instead of being copied as an orphan
    conn.execute("""
        INSERT INTO comments (id, ticket_id, author, content, created_at)
        SELECT id, ticket_id, author, note, created_at
        FROM ticket_comments
        ORDER BY id
    """)

    conn.execute(
        "ALTER TABLE ticket_comments RENAME TO ticket_comments_migrated"
    )


# ---------------------------------------------------
# MIGRATION 2: TICKET LIST INDEXES
# ---------------------------------------------------

def _migration_2_ticket_indexes(conn):
    """Indexes for newest-first ticket lists, per user and for staff."""

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_owner_date
        ON tickets (created_by, created_at DESC, id DESC)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_date
        ON tickets (created_at DESC, id DESC)
    """)


# ---------------------------------------------------
# MIGRATION 3: TICKET ACTIVITY HISTORY
# ---------------------------------------------------

def _migration_3_ticket_history(conn):
    """
    Append-only ticket activity history.

    No events are generated for existing tickets: there is no reliable
    record of what happened to them before history was tracked.
    """

    conn.execute("""
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
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket
        ON ticket_history (ticket_id, created_at DESC, id DESC)
    """)

    # The database itself refuses edits and deletions of history
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS ticket_history_no_update
        BEFORE UPDATE ON ticket_history
        BEGIN
            SELECT RAISE(ABORT, 'ticket history is append-only');
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS ticket_history_no_delete
        BEFORE DELETE ON ticket_history
        BEGIN
            SELECT RAISE(ABORT, 'ticket history is append-only');
        END
    """)


# ---------------------------------------------------
# MIGRATION 4: RESPONSE SLA
# ---------------------------------------------------

def _migration_4_ticket_sla(conn):
    """
    One response-SLA row per ticket.

    Existing tickets get no SLA row: their deadline and first response
    were never recorded, so they are shown as "Not Tracked" rather than
    given a made-up outcome.
    """

    # This SQL is written out here, not taken from sla_storage, so that
    # version 4 always means exactly this table even if code changes later
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_sla (
            ticket_id INTEGER PRIMARY KEY,
            response_due_at TEXT NOT NULL,
            first_response_at TEXT,
            policy_version TEXT NOT NULL
                DEFAULT 'calendar-hours-v1',

            FOREIGN KEY (ticket_id)
                REFERENCES tickets(id)
        )
    """)


# ---------------------------------------------------
# MIGRATION 5: ACCOUNT ACTIVATION STATUS
# ---------------------------------------------------

def _migration_5_account_activation(conn):
    """
    Add users.is_active: 1 = may sign in, 0 = deactivated.

    Every existing account stays active, and its id, role and password
    hash are untouched. ALTER TABLE ADD COLUMN does not rewrite those
    values. A column that already exists is left alone here, and the
    final schema validation rejects it if it lacks these constraints.
    """

    if "is_active" not in _columns(conn, "users"):
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
            CHECK (is_active IN (0, 1))
        """)


# ---------------------------------------------------
# MIGRATION 6: SESSION REVOCATION
# ---------------------------------------------------

def _migration_6_session_version(conn):
    """
    Add users.session_version, the counter that revokes login sessions.

    A session is valid only while the version stored in its cookie equals
    this value, so adding 1 ends every existing session for that account.

    The trigger does this whenever a password or activation status changes,
    including changes made outside the application with SQL. Reactivating
    an account therefore cannot bring its old cookies back to life. It runs
    inside the same transaction as the change that fired it.
    """

    if "session_version" not in _columns(conn, "users"):
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(session_version) = 'integer'
                   AND session_version >= 1)
        """)

    else:
        invalid = conn.execute("""
            SELECT COUNT(*) FROM users
            WHERE typeof(session_version) != 'integer'
               OR session_version < 1
        """).fetchone()[0]

        if invalid:
            raise IncompatibleSchemaError(
                "users.session_version contains invalid values"
            )

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS users_revoke_sessions
        AFTER UPDATE OF password_hash, is_active ON users
        FOR EACH ROW
        WHEN OLD.password_hash IS NOT NEW.password_hash
          OR OLD.is_active IS NOT NEW.is_active
        BEGIN
            UPDATE users
            SET session_version = session_version + 1
            WHERE id = NEW.id;
        END
    """)


# ---------------------------------------------------
# MIGRATION 7: STABLE USERNAMES
# ---------------------------------------------------

def _migration_7_stable_usernames(conn):
    """
    Stop usernames from being changed or handed to a new person.

    Tickets, comments and history refer to accounts by username. If an
    account were deleted and a new one created with the same name, the
    new person would own the old person's tickets and be shown as the
    author of their comments. Two triggers prevent that:

      users_username_immutable          a username can never be changed
      users_no_historical_username_reuse  a new account cannot take a
                                        name that ticket records use

    Deleting or deactivating an account is still allowed: its records keep
    the old name, and that name stays reserved. No rows are changed, so
    existing tickets, accounts and history are exactly as they were.
    """

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS users_username_immutable
        BEFORE UPDATE OF username ON users
        FOR EACH ROW
        WHEN OLD.username IS NOT NEW.username
        BEGIN
            SELECT RAISE(ABORT, 'usernames cannot be changed');
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS users_no_historical_username_reuse
        BEFORE INSERT ON users
        FOR EACH ROW
        WHEN EXISTS (SELECT 1 FROM tickets
                     WHERE created_by = NEW.username
                        OR assigned_to = NEW.username)
          OR EXISTS (SELECT 1 FROM comments
                     WHERE author = NEW.username)
          OR EXISTS (SELECT 1 FROM ticket_history
                     WHERE actor = NEW.username
                        OR old_value = NEW.username
                        OR new_value = NEW.username)
        BEGIN
            SELECT RAISE(ABORT,
                'username is already used by existing ticket records');
        END
    """)


# ---------------------------------------------------
# MIGRATION 8: ADMINISTRATIVE AUDIT EVENTS
# ---------------------------------------------------

def _migration_8_audit_events(conn):
    """
    Append-only record of account administration.

    Each row says who did what to which account and when: accounts
    created, roles changed, accounts deactivated or reactivated, and
    passwords reset or changed. Passwords and hashes are never stored.

    Accounts are referred to by id and by username. Usernames are
    permanent (migration 7), and there is deliberately no foreign key to
    users, so an event outlives the account it describes. No events are
    generated for changes made before this migration: there is no record
    of them. Existing rows in every other table are untouched.
    """

    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actor_user_id   INTEGER,
            actor           TEXT NOT NULL CHECK (length(actor) > 0),
            action          TEXT NOT NULL CHECK (length(action) > 0),
            target_user_id  INTEGER,
            target_username TEXT,
            old_value       TEXT,
            new_value       TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_events_date
        ON audit_events (created_at DESC, id DESC)
    """)

    # The database itself refuses edits and deletions of audit events
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are append-only');
        END
    """)


# ---------------------------------------------------
# MIGRATION 9: ACCOUNT POLICY AND LOGIN EVENTS
# ---------------------------------------------------

def _migration_9_account_policy(conn):
    """
    Add users.must_change_password and audit_events.source_address.

    must_change_password = 1 means the account may only change its
    password (or log out) until it does. Administrators set it when they
    create an account or reset a password, so a password someone else
    chose is never kept. Every existing account gets 0: nobody is forced
    to change a password they already chose.

    source_address is the client address of a login attempt, as seen
    through the configured trusted proxies. Older events keep NULL.

    ALTER TABLE ADD COLUMN rewrites no rows and fires no triggers, so the
    append-only protection of audit_events is not touched.
    """

    if "must_change_password" not in _columns(conn, "users"):
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0
            CHECK (must_change_password IN (0, 1))
        """)

    if "source_address" not in _columns(conn, "audit_events"):
        conn.execute(
            "ALTER TABLE audit_events ADD COLUMN source_address TEXT"
        )


# ---------------------------------------------------
# MIGRATION REGISTRY
# ---------------------------------------------------

# Append new migrations to the end. Never edit, reorder or remove one that
# has been released: databases in use already record that it ran.
MIGRATIONS = (
    Migration(1, "Create core users, tickets and comments tables",
              _migration_1_core_tables),
    Migration(2, "Add ticket list indexes", _migration_2_ticket_indexes),
    Migration(3, "Add append-only ticket activity history",
              _migration_3_ticket_history),
    Migration(4, "Add response SLA deadlines", _migration_4_ticket_sla),
    Migration(5, "Add account activation status",
              _migration_5_account_activation),
    Migration(6, "Add session revocation", _migration_6_session_version),
    Migration(7, "Keep usernames stable and stop their reuse",
              _migration_7_stable_usernames),
    Migration(8, "Add append-only administrative audit events",
              _migration_8_audit_events),
    Migration(9, "Add forced password changes and login event addresses",
              _migration_9_account_policy),
)

SCHEMA_VERSION = MIGRATIONS[-1].version


def _check_registry(migrations):
    """Versions must be exactly 1, 2, 3, ... with no gaps or repeats."""

    versions = [m.version for m in migrations]

    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"migration versions must be 1..N in order, got {versions}"
        )


_check_registry(MIGRATIONS)


# ---------------------------------------------------
# SCHEMA VALIDATION
# ---------------------------------------------------

# Required columns per table; True means the column must be NOT NULL.
# Extra columns are allowed. Older databases differ in details such as
# created_at's declared type, which are deliberately not compared.
_REQUIRED_COLUMNS = {
    "users": {
        "id": False, "username": True, "password_hash": True, "role": True,
        "is_active": True, "session_version": True,
        "must_change_password": True,
    },
    "tickets": {
        "id": False, "title": True, "description": True,
        "priority": True, "status": True, "assigned_to": False,
        "created_by": False, "created_at": False, "updated_at": False,
    },
    "comments": {
        "id": False, "ticket_id": True, "author": True, "content": True,
        "created_at": False,
    },
    "ticket_history": {
        "id": False, "ticket_id": True, "actor": True, "action": True,
        "field_name": False, "old_value": False, "new_value": False,
        "created_at": True,
    },
    "ticket_sla": {
        "ticket_id": False, "response_due_at": True,
        "first_response_at": False, "policy_version": True,
    },
    "audit_events": {
        "id": False, "created_at": True, "actor_user_id": False,
        "actor": True, "action": True, "target_user_id": False,
        "target_username": False, "old_value": False, "new_value": False,
        "source_address": False,
    },
}

_PRIMARY_KEYS = {
    "users": "id",
    "tickets": "id",
    "comments": "id",
    "ticket_history": "id",
    "ticket_sla": "ticket_id",
    "audit_events": "id",
}

# (child table, child column) -> parent table, parent column
_FOREIGN_KEYS = {
    ("comments", "ticket_id"): ("tickets", "id"),
    ("ticket_history", "ticket_id"): ("tickets", "id"),
    ("ticket_sla", "ticket_id"): ("tickets", "id"),
}

# index -> (table, [(column, descending), ...])
_INDEXES = {
    "idx_tickets_owner_date": (
        "tickets", [("created_by", False), ("created_at", True),
                    ("id", True)]
    ),
    "idx_tickets_date": (
        "tickets", [("created_at", True), ("id", True)]
    ),
    "idx_ticket_history_ticket": (
        "ticket_history", [("ticket_id", False), ("created_at", True),
                           ("id", True)]
    ),
    "idx_audit_events_date": (
        "audit_events", [("created_at", True), ("id", True)]
    ),
}

# Text the session revocation trigger must contain, after normalising
_SESSION_TRIGGER = "users_revoke_sessions"
_SESSION_TRIGGER_PARTS = (
    "AFTER UPDATE OF PASSWORD_HASH, IS_ACTIVE ON USERS",
    "OLD.PASSWORD_HASH IS NOT NEW.PASSWORD_HASH",
    "OLD.IS_ACTIVE IS NOT NEW.IS_ACTIVE",
    "SET SESSION_VERSION = SESSION_VERSION + 1 WHERE ID = NEW.ID",
)

# trigger -> the event it must block (each must also RAISE(ABORT ...))
_TRIGGERS = {
    "ticket_history_no_update": "BEFORE UPDATE ON TICKET_HISTORY",
    "ticket_history_no_delete": "BEFORE DELETE ON TICKET_HISTORY",
    "users_username_immutable": "BEFORE UPDATE OF USERNAME ON USERS",
    "users_no_historical_username_reuse": "BEFORE INSERT ON USERS",
    "audit_events_no_update": "BEFORE UPDATE ON AUDIT_EVENTS",
    "audit_events_no_delete": "BEFORE DELETE ON AUDIT_EVENTS",
}

# Text each username trigger must also contain, after normalising, so a
# trigger with the right name but a weakened condition is rejected
_USERNAME_TRIGGER_PARTS = {
    "users_username_immutable": (
        "WHEN OLD.USERNAME IS NOT NEW.USERNAME",
    ),
    "users_no_historical_username_reuse": (
        "CREATED_BY = NEW.USERNAME",
        "ASSIGNED_TO = NEW.USERNAME",
        "AUTHOR = NEW.USERNAME",
        "ACTOR = NEW.USERNAME",
        "OLD_VALUE = NEW.USERNAME",
        "NEW_VALUE = NEW.USERNAME",
    ),
}


def find_schema_problems(conn):
    """
    Compare the database with the latest schema the application expects.

    Returns a list of human-readable problems; an empty list means the
    schema is compatible. Messages name tables, columns and indexes only,
    never any stored data.
    """

    problems = []
    tables = _tables(conn)

    for table, required in _REQUIRED_COLUMNS.items():
        if table not in tables:
            problems.append(f"missing table {table}")
            continue

        columns = _columns(conn, table)

        for column, must_be_not_null in required.items():
            if column not in columns:
                problems.append(f"missing column {table}.{column}")
            elif must_be_not_null and not columns[column][3]:
                problems.append(f"column {table}.{column} must be NOT NULL")

        pk_columns = [name for name, row in columns.items() if row[5]]
        if pk_columns != [_PRIMARY_KEYS[table]]:
            problems.append(
                f"{table} primary key must be {_PRIMARY_KEYS[table]}"
            )

    if "users" in tables and not _has_unique_index(conn, "users",
                                                   ["username"]):
        problems.append("users.username must be UNIQUE")

    if "users" in tables and "is_active" in _columns(conn, "users"):
        if "CHECK(IS_ACTIVE IN(0, 1))" not in _object_sql(conn, "table",
                                                          "users"):
            problems.append("users.is_active is missing its CHECK")

    if "users" in tables:
        users_sql = _object_sql(conn, "table", "users")

        # Without AUTOINCREMENT SQLite can reuse a deleted user's id, and
        # that user's old cookie would then match the new account
        if "AUTOINCREMENT" not in users_sql:
            problems.append("users.id must use AUTOINCREMENT")

        if "session_version" in _columns(conn, "users") and (
            "CHECK(TYPEOF(SESSION_VERSION) = 'INTEGER' "
            "AND SESSION_VERSION >= 1)"
        ) not in users_sql:
            problems.append("users.session_version is missing its CHECK")

        if "must_change_password" in _columns(conn, "users") and (
            "CHECK(MUST_CHANGE_PASSWORD IN(0, 1))" not in users_sql
        ):
            problems.append("users.must_change_password is missing its CHECK")

        trigger_sql = _object_sql(conn, "trigger", _SESSION_TRIGGER)

        if not trigger_sql:
            problems.append(f"missing trigger {_SESSION_TRIGGER}")
        elif not all(part in trigger_sql for part in _SESSION_TRIGGER_PARTS):
            problems.append(
                f"trigger {_SESSION_TRIGGER} has unexpected behaviour"
            )

    if "ticket_history" in tables:
        sql = _object_sql(conn, "table", "ticket_history")
        if "CHECK" not in sql:
            problems.append("ticket_history.action is missing its CHECK")

    for (child, column), (parent, parent_column) in _FOREIGN_KEYS.items():
        if child not in tables:
            continue

        found = any(
            row[3] == column and row[2] == parent
            and row[4] in (parent_column, None)
            for row in conn.execute(f'PRAGMA foreign_key_list("{child}")')
        )

        if not found:
            problems.append(
                f"missing foreign key {child}.{column} -> {parent}.{parent_column}"
            )

    for index, (table, expected) in _INDEXES.items():
        row = conn.execute(
            "SELECT tbl_name FROM sqlite_master "
            "WHERE type = 'index' AND name = ?",
            (index,)
        ).fetchone()

        if row is None:
            problems.append(f"missing index {index}")
            continue

        actual = [
            (info[2], bool(info[3]))
            for info in conn.execute(f'PRAGMA index_xinfo("{index}")')
            if info[5]  # key columns only
        ]

        if row[0] != table or actual != expected:
            problems.append(f"index {index} has unexpected columns")

    for trigger, event in _TRIGGERS.items():
        sql = _object_sql(conn, "trigger", trigger)

        if not sql:
            problems.append(f"missing trigger {trigger}")
        elif (
            event not in sql
            or "RAISE(ABORT" not in sql
            or not all(
                part in sql
                for part in _USERNAME_TRIGGER_PARTS.get(trigger, ())
            )
        ):
            problems.append(f"trigger {trigger} has unexpected behaviour")

    return problems


def _object_sql(conn, kind, name):
    """The CREATE statement of a schema object, normalised for matching."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (kind, name)
    ).fetchone()

    if row is None or row[0] is None:
        return ""

    return " ".join(row[0].upper().replace("(", " (").split()).replace(
        " (", "("
    )


def _has_unique_index(conn, table, columns):
    for index in conn.execute(f'PRAGMA index_list("{table}")'):
        if index[2]:  # unique
            names = [
                info[2] for info in
                conn.execute(f'PRAGMA index_info("{index[1]}")')
            ]
            if names == columns:
                return True

    return False


def _foreign_key_violations(conn):
    """Table name -> number of rows whose foreign key points nowhere."""

    counts = {}

    for row in conn.execute("PRAGMA foreign_key_check"):
        counts[row[0]] = counts.get(row[0], 0) + 1

    return counts


# ---------------------------------------------------
# CONNECTIONS
# ---------------------------------------------------

def _connect(path, timeout):
    """
    Open a connection with manual transaction control.

    isolation_level=None stops the sqlite3 module from opening or
    committing transactions by itself, so BEGIN/COMMIT/ROLLBACK happen
    exactly where this module issues them.
    """

    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)

    # Must be set outside a transaction; SQLite ignores it inside one
    conn.execute("PRAGMA foreign_keys = ON")

    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise MigrationError("SQLite foreign key enforcement is unavailable")

    return conn


def _connect_read_only(path):
    """Open an existing database without any possibility of writing."""

    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ---------------------------------------------------
# APPLYING MIGRATIONS
# ---------------------------------------------------

@dataclass
class MigrationResult:
    start_version: int
    final_version: int
    applied: list = field(default_factory=list)  # [Migration, ...]


def migrate(path, *, migrations=MIGRATIONS, timeout=DEFAULT_LOCK_TIMEOUT):
    """
    Bring the database at `path` up to the latest schema version.

    Creates the file if it does not exist. Safe to run repeatedly and from
    several processes at once: BEGIN IMMEDIATE lets only one process
    migrate at a time, and the others then find nothing left to do.

    Raises MigrationError (or a subclass) or sqlite3.Error on failure, in
    which case the database is exactly as it was before the call.
    """

    _check_registry(migrations)
    target = migrations[-1].version

    conn = _connect(path, timeout)

    try:
        # Waits up to `timeout` seconds for any other writer to finish
        conn.execute("BEGIN IMMEDIATE")

        try:
            result = _migrate_locked(conn, migrations, target)
            conn.execute("COMMIT")
            return result

        except BaseException:
            # DDL and PRAGMA user_version are transactional in SQLite, so
            # this undoes every table, column and version change
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    finally:
        conn.close()


def _migrate_locked(conn, migrations, target):
    """Apply pending migrations. The caller holds the write transaction."""

    start = _user_version(conn)

    if start > target:
        raise UnsupportedSchemaVersionError(
            f"database schema version {start} is newer than the latest "
            f"version this application supports ({target})"
        )

    if start < 0:
        raise IncompatibleSchemaError(
            f"database schema version {start} is not valid"
        )

    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError("database failed SQLite's integrity check")

    result = MigrationResult(start_version=start, final_version=start)

    for migration in migrations:
        if migration.version <= start:
            continue

        migration.apply(conn)

        # Recorded inside the same transaction, so the version can never
        # say a migration ran unless its changes are also saved.
        # migration.version is an int from the registry, not user input.
        conn.execute(f"PRAGMA user_version = {int(migration.version)}")

        result.applied.append(migration)
        result.final_version = migration.version

    if migrations is MIGRATIONS:
        problems = find_schema_problems(conn)
        if problems:
            raise IncompatibleSchemaError(
                "schema does not match the expected structure: "
                + "; ".join(problems)
            )

    violations = _foreign_key_violations(conn)
    if violations:
        detail = ", ".join(
            f"{table}: {count}" for table, count in sorted(violations.items())
        )
        raise IncompatibleSchemaError(
            f"rows reference missing records ({detail})"
        )

    return result


# ---------------------------------------------------
# READ-ONLY STATUS
# ---------------------------------------------------

@dataclass
class SchemaStatus:
    # missing | uninitialized | legacy | outdated | current
    # | too_new | incompatible
    state: str
    version: int
    target: int
    pending: list = field(default_factory=list)   # [Migration, ...]
    problems: list = field(default_factory=list)  # [str, ...]

    @property
    def is_current(self):
        return self.state == "current"


def schema_status(path, *, migrations=MIGRATIONS):
    """
    Describe the database schema without changing anything.

    The file is opened read-only, and a missing file is reported as
    "missing" rather than created.
    """

    target = migrations[-1].version

    if not os.path.exists(path):
        return SchemaStatus("missing", 0, target, list(migrations))

    conn = _connect_read_only(path)

    try:
        version = _user_version(conn)
        pending = [m for m in migrations if m.version > version]

        if version > target:
            return SchemaStatus(
                "too_new", version, target,
                problems=[
                    f"schema version {version} is newer than supported "
                    f"version {target}"
                ]
            )

        if version == target:
            problems = find_schema_problems(conn)
            state = "incompatible" if problems else "current"
            return SchemaStatus(state, version, target, [], problems)

        if version == 0:
            tables = _tables(conn)

            if not tables:
                return SchemaStatus("uninitialized", 0, target, pending)

            # Pre-versioning database: check it can be adopted
            problems = []
            if "tickets" not in tables:
                problems.append(
                    "database is not empty but has no tickets table"
                )
            else:
                missing = (
                    _LEGACY_TICKET_COLUMNS - set(_columns(conn, "tickets"))
                )
                if missing:
                    problems.append(
                        "tickets table is missing expected columns: "
                        + ", ".join(sorted(missing))
                    )

            state = "incompatible" if problems else "legacy"
            return SchemaStatus(state, 0, target, pending, problems)

        return SchemaStatus("outdated", version, target, pending)

    finally:
        conn.close()


# ---------------------------------------------------
# BACKUP
# ---------------------------------------------------

def backup_database(source_path, backup_path):
    """
    Copy a database with SQLite's online backup API and verify the copy.

    Refuses to overwrite an existing file. The copy is consistent even if
    the source is in use, because SQLite copies it page by page under a
    read lock.
    """

    if not os.path.exists(source_path):
        raise MigrationError("database to back up does not exist")

    if os.path.exists(backup_path):
        raise MigrationError("backup file already exists; choose a new path")

    source = _connect_read_only(source_path)

    try:
        # "x" creates the file exclusively, closing the race between the
        # existence check above and SQLite opening the destination
        with open(backup_path, "x"):
            pass

        try:
            destination = sqlite3.connect(backup_path)

            try:
                source.backup(destination)

                result = destination.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if result[0] != "ok":
                    raise MigrationError("backup failed its integrity check")

                if _user_version(destination) != _user_version(source):
                    raise MigrationError(
                        "backup schema version does not match"
                    )

            finally:
                destination.close()

        except BaseException:
            # Never leave a half-written file that looks like a backup
            os.remove(backup_path)
            raise

    finally:
        source.close()
