"""
Account administration: the rules and the SQL, without any web code.

The admin and account blueprints call these functions; setup_accounts.py
uses record_audit_event(). Every change and its audit event are saved in
one transaction, so an account is never changed without a record of it.

Rules enforced here, on the server, whatever the form sends:

  * The acting account is re-read inside the write transaction and must
    still be an active administrator. A request that started just before
    the actor was demoted or deactivated changes nothing.
  * Administrators cannot change their own role, deactivate themselves or
    reset their own password here (they use Change password, which needs
    the current password). Because the actor must be an active admin and
    cannot act on itself, at least one active administrator always
    remains: the last one can never be removed through the application.
  * Accounts are never deleted. Deactivation keeps every ticket, comment
    and history entry exactly as it was.
  * Passwords and password hashes are never written to the audit log.

Session effects: changing a password or the active status ends every
session of that account (the users_revoke_sessions trigger, migration 6).
A role change takes effect on the account's next request, because the
role is read from the database on every request.
"""

import sqlite3
from dataclasses import dataclass

from werkzeug.security import check_password_hash, generate_password_hash

import database as db
import validation

ROLES = ("user", "technician", "admin")
ADMIN_ROLE = "admin"

# Statuses in which a ticket still needs someone to work on it
UNFINISHED_STATUSES = ("Open", "In Progress")

# Every action the application writes to audit_events
ACCOUNT_CREATED = "account_created"
ROLE_CHANGED = "role_changed"
ACCOUNT_DEACTIVATED = "account_deactivated"
ACCOUNT_REACTIVATED = "account_reactivated"
PASSWORD_RESET = "password_reset"
PASSWORD_CHANGED = "password_changed"

AUDIT_ACTIONS = frozenset({
    ACCOUNT_CREATED,
    ROLE_CHANGED,
    ACCOUNT_DEACTIVATED,
    ACCOUNT_REACTIVATED,
    PASSWORD_RESET,
    PASSWORD_CHANGED,
})

# Most audit events one page shows
MAX_AUDIT_EVENTS = 200


class AccountError(Exception):
    """A refused change. The message is safe to show to the user."""


class AccountNotFound(AccountError):
    """The target account does not exist."""

    def __init__(self):
        super().__init__("Account not found.")


@dataclass(frozen=True)
class RoleChange:
    """What change_role() did."""

    changed: bool
    username: str
    # Open or in-progress tickets still assigned to an account that can
    # no longer receive tickets after this change. They are not changed
    # automatically; an administrator decides what to do with them.
    tickets_needing_reassignment: int = 0


def hash_password(password):
    """Hash a password with Werkzeug's default (scrypt). Tests replace it."""

    return generate_password_hash(password)


# ---------------------------------------------------
# AUDIT EVENTS
# ---------------------------------------------------

def record_audit_event(conn, *, actor, action, actor_user_id=None,
                       target_user_id=None, target_username=None,
                       old_value=None, new_value=None):
    """
    Add one audit event using the caller's open connection.

    Never commits: the caller saves the change and its event together.
    """

    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action: {action}")

    conn.execute(
        """
        INSERT INTO audit_events
        (actor_user_id, actor, action, target_user_id, target_username,
         old_value, new_value)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (actor_user_id, actor, action, target_user_id, target_username,
         old_value, new_value)
    )


def list_audit_events(limit=MAX_AUDIT_EVENTS):
    """The most recent audit events, newest first."""

    limit = max(1, min(int(limit), MAX_AUDIT_EVENTS))

    conn = db.get_db()

    try:
        return conn.execute(
            """
            SELECT id, created_at, actor_user_id, actor, action,
                   target_user_id, target_username, old_value, new_value
            FROM audit_events
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    finally:
        conn.close()


# ---------------------------------------------------
# READING ACCOUNTS
# ---------------------------------------------------

def list_accounts():
    """
    Every account with its unfinished assigned tickets, by username.

    The password hash is never selected.
    """

    placeholders = ", ".join("?" for _ in UNFINISHED_STATUSES)

    conn = db.get_db()

    try:
        return conn.execute(
            f"""
            SELECT u.id, u.username, u.role, u.is_active,
                   (SELECT COUNT(*) FROM tickets AS t
                    WHERE t.assigned_to = u.username
                      AND t.status IN ({placeholders})) AS open_assigned
            FROM users AS u
            ORDER BY u.username COLLATE NOCASE, u.id
            """,
            UNFINISHED_STATUSES
        ).fetchall()

    finally:
        conn.close()


def get_account(user_id):
    """One account without its password hash, or None."""

    conn = db.get_db()

    try:
        return conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()

    finally:
        conn.close()


# ---------------------------------------------------
# ADMINISTRATOR ACTIONS
# ---------------------------------------------------

def _begin_admin_change(conn, actor_id):
    """
    Start a write transaction and return the verified acting admin.

    BEGIN IMMEDIATE takes the write lock first, so the checks below and
    the change that follows see the same data.
    """

    conn.execute("BEGIN IMMEDIATE")

    actor = conn.execute(
        "SELECT id, username, role, is_active FROM users WHERE id = ?",
        (actor_id,)
    ).fetchone()

    if actor is None or actor["is_active"] != 1 or (
        actor["role"] != ADMIN_ROLE
    ):
        raise AccountError(
            "Your account is no longer allowed to manage accounts."
        )

    return actor


def _load_target(conn, actor, target_id, *, self_message):
    """The account being changed; never the acting admin."""

    target = conn.execute(
        "SELECT id, username, role, is_active FROM users WHERE id = ?",
        (target_id,)
    ).fetchone()

    if target is None:
        raise AccountNotFound()

    if target["id"] == actor["id"]:
        raise AccountError(self_message)

    return target


def _open_assigned_count(conn, username):
    placeholders = ", ".join("?" for _ in UNFINISHED_STATUSES)

    return conn.execute(
        f"""
        SELECT COUNT(*) FROM tickets
        WHERE assigned_to = ? AND status IN ({placeholders})
        """,
        (username, *UNFINISHED_STATUSES)
    ).fetchone()[0]


def _run(change):
    """Run `change(conn)` in one transaction, always closing the connection."""

    conn = db.get_db()

    try:
        with conn:
            return change(conn)

    finally:
        conn.close()


def create_account(actor_id, username, role, password):
    """
    Create an active account with a password chosen by an administrator.

    Returns the new account's id. Raises AccountError if the name, role or
    password is not acceptable, or the name is taken. A name differing
    only in letter case from an existing account ("Admin" and "admin") is
    refused, because the two could not be told apart on a ticket.
    """

    problem = (
        validation.username_problem(username)
        or (None if role in ROLES else "Choose a valid role.")
        or validation.new_password_problem(password, password)
    )

    if problem:
        raise AccountError(problem)

    # Hashing is deliberately slow, so it happens before the write lock
    password_hash = hash_password(password)

    def change(conn):
        actor = _begin_admin_change(conn, actor_id)

        taken = conn.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
            (username,)
        ).fetchone()

        if taken:
            raise AccountError("That username is not available.")

        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (?, ?, ?)
                """,
                (username, password_hash, role)
            )

        except sqlite3.IntegrityError:
            # The migration 7 trigger: ticket records still use this name
            raise AccountError("That username is not available.") from None

        record_audit_event(
            conn,
            actor_user_id=actor["id"],
            actor=actor["username"],
            action=ACCOUNT_CREATED,
            target_user_id=cursor.lastrowid,
            target_username=username,
            new_value=role
        )

        return cursor.lastrowid

    return _run(change)


def change_role(actor_id, target_id, new_role):
    """
    Give another account a different role. Returns a RoleChange.

    Tickets assigned to an account that can no longer receive them keep
    their assignee (history is never rewritten); the ticket page flags
    them and the result says how many still need reassigning.
    """

    if new_role not in ROLES:
        raise AccountError("Choose a valid role.")

    def change(conn):
        actor = _begin_admin_change(conn, actor_id)
        target = _load_target(
            conn, actor, target_id,
            self_message="You cannot change your own role."
        )

        if target["role"] == new_role:
            return RoleChange(False, target["username"])

        conn.execute(
            "UPDATE users SET role = ? WHERE id = ?",
            (new_role, target["id"])
        )

        record_audit_event(
            conn,
            actor_user_id=actor["id"],
            actor=actor["username"],
            action=ROLE_CHANGED,
            target_user_id=target["id"],
            target_username=target["username"],
            old_value=target["role"],
            new_value=new_role
        )

        stranded = 0

        if new_role not in db.ASSIGNABLE_ROLES:
            stranded = _open_assigned_count(conn, target["username"])

        return RoleChange(True, target["username"], stranded)

    return _run(change)


def set_active(actor_id, target_id, active):
    """
    Deactivate or reactivate another account.

    Returns (changed, username, open_assigned_tickets). Deactivation ends
    all of the account's sessions in the same transaction (trigger), and
    keeps every ticket and comment. Reactivation does not revive old
    sessions: the account signs in again.
    """

    new_value = 1 if active else 0

    def change(conn):
        actor = _begin_admin_change(conn, actor_id)
        target = _load_target(
            conn, actor, target_id,
            self_message="You cannot deactivate your own account."
        )

        if target["is_active"] == new_value:
            return False, target["username"], 0

        conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (new_value, target["id"])
        )

        record_audit_event(
            conn,
            actor_user_id=actor["id"],
            actor=actor["username"],
            action=ACCOUNT_REACTIVATED if active else ACCOUNT_DEACTIVATED,
            target_user_id=target["id"],
            target_username=target["username"]
        )

        stranded = 0 if active else _open_assigned_count(
            conn, target["username"]
        )

        return True, target["username"], stranded

    return _run(change)


def reset_password(actor_id, target_id, password):
    """
    Set another account's password, ending all of its sessions.

    For a user who has forgotten their password. The administrator gives
    the new password to the user through a separate, trusted channel.
    Returns the account's username.
    """

    problem = validation.new_password_problem(password, password)

    if problem:
        raise AccountError(problem)

    password_hash = hash_password(password)

    def change(conn):
        actor = _begin_admin_change(conn, actor_id)
        target = _load_target(
            conn, actor, target_id,
            self_message=(
                "Use Change password to change your own password."
            )
        )

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, target["id"])
        )

        record_audit_event(
            conn,
            actor_user_id=actor["id"],
            actor=actor["username"],
            action=PASSWORD_RESET,
            target_user_id=target["id"],
            target_username=target["username"]
        )

        return target["username"]

    return _run(change)


# ---------------------------------------------------
# CHANGING YOUR OWN PASSWORD
# ---------------------------------------------------

def change_own_password(user_id, session_version, current_password,
                        new_password):
    """
    Change the signed-in user's password after checking the current one.

    Every session of the account ends (trigger). Returns the account's new
    session version, so the caller can keep only the browser that made
    the change signed in.

    The update succeeds only if the password hash, active status and
    session version are still the ones that were checked; if another
    change happened in between, nothing is saved.
    """

    problem = validation.new_password_problem(new_password, new_password)

    if problem:
        raise AccountError(problem)

    conn = db.get_db()

    try:
        account = conn.execute(
            """
            SELECT id, username, password_hash, is_active, session_version
            FROM users WHERE id = ?
            """,
            (user_id,)
        ).fetchone()

    finally:
        conn.close()

    if (
        account is None
        or account["is_active"] != 1
        or account["session_version"] != session_version
    ):
        raise AccountError(
            "Your session is no longer valid. Please log in again."
        )

    if len(current_password or "") > validation.MAX_PASSWORD_LENGTH or (
        not check_password_hash(account["password_hash"],
                                current_password or "")
    ):
        raise AccountError("Current password is incorrect.")

    if check_password_hash(account["password_hash"], new_password):
        raise AccountError("New password must differ from the old password.")

    password_hash = hash_password(new_password)

    def change(conn):
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.execute(
            """
            UPDATE users SET password_hash = ?
            WHERE id = ? AND password_hash = ? AND is_active = 1
              AND session_version = ?
            """,
            (password_hash, account["id"], account["password_hash"],
             session_version)
        )

        if cursor.rowcount != 1:
            raise AccountError(
                "Your password was not changed because your account was "
                "changed at the same time. Please log in again."
            )

        record_audit_event(
            conn,
            actor_user_id=account["id"],
            actor=account["username"],
            action=PASSWORD_CHANGED,
            target_user_id=account["id"],
            target_username=account["username"]
        )

        return conn.execute(
            "SELECT session_version FROM users WHERE id = ?",
            (account["id"],)
        ).fetchone()[0]

    return _run(change)
