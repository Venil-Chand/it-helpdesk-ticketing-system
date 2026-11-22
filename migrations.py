
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
        "is_active": True,
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
}

_PRIMARY_KEYS = {
    "users": "id",
    "tickets": "id",
    "comments": "id",
    "ticket_history": "id",
    "ticket_sla": "ticket_id",
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
}

# trigger -> the event it must block on ticket_history
_TRIGGERS = {
    "ticket_history_no_update": "BEFORE UPDATE ON TICKET_HISTORY",
    "ticket_history_no_delete": "BEFORE DELETE ON TICKET_HISTORY",
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
        elif event not in sql or "RAISE(ABORT" not in sql:
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
