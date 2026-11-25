
# ---------------------------------------------------
# IT HELPDESK - SECURE ACCOUNT MANAGEMENT
# ---------------------------------------------------

from getpass import getpass

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

import sqlite3
import sys

import accounts
import database as db
import migrations
import validation


# Recorded as the actor of audit events, since no web account is signed in
AUDIT_ACTOR = "setup_accounts.py"


def setup_account():
    """
    Create the first administrator or change an existing password.

    The database schema must already be up to date. This script never
    creates or upgrades it; run "flask --app app init-db" first, so that
    schema changes only ever happen through the explicit migration step.

    Returns False if the schema is not ready, otherwise None.
    """

    status = migrations.schema_status(db.DB_PATH)

    if not status.is_current:
        print(
            "The database schema is not up to date. "
            "Run 'flask --app app init-db' first."
        )
        return False

    # Connect to the SQLite database
    conn = db.get_db()

    try:
        # Count existing user accounts
        count = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        # -------------------------------------------
        # FIRST-TIME ADMINISTRATOR SETUP
        # -------------------------------------------

        if count == 0:
            print("No user accounts found.")
            print("Creating the first administrator account.")

            username = "admin"
            user = None
            old_password = None

        # -------------------------------------------
        # CHANGE AN EXISTING ACCOUNT PASSWORD
        # -------------------------------------------

        else:
            username = input("Enter username: ").strip()

            if not username:
                print("Username cannot be empty.")
                return

            # Search for the account
            user = conn.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,)
            ).fetchone()

            if user is None:
                print("Account not found.")
                return

            # Verify the existing password
            old_password = getpass("Current password: ")

            if not check_password_hash(
                user["password_hash"],
                old_password
            ):
                print("Incorrect current password.")
                return

        # -------------------------------------------
        # NEW PASSWORD VALIDATION
        # -------------------------------------------

        new_password = getpass("Enter new password: ")
        confirm_password = getpass("Confirm new password: ")

        # The same password rules as the web pages: length, typed twice,
        # and not common, breached, the username or a simple pattern
        problem = validation.new_password_problem(
            new_password, confirm_password, username=username
        )

        if problem:
            print(problem)
            return

        # Prevent reuse of the current password
        if user is not None:
            if check_password_hash(
                user["password_hash"],
                new_password
            ):
                print("New password must differ from the old password.")
                return

        # Hash the password before saving
        password_hash = generate_password_hash(new_password)

        # -------------------------------------------
        # SAVE ACCOUNT INFORMATION
        # -------------------------------------------

        if user is None:
            # Create the initial administrator account. The database
            # refuses a name that existing ticket records still use, so a
            # new person never inherits a deleted account's tickets.
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role)
                    VALUES (?, ?, ?)
                    """,
                    (username, password_hash, "admin")
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                print(
                    f"The username '{username}' belongs to a deleted "
                    "account that existing tickets still refer to, so it "
                    "cannot be given to a new account. No account was "
                    "created."
                )
                return False

            accounts.record_audit_event(
                conn,
                actor=AUDIT_ACTOR,
                action=accounts.ACCOUNT_CREATED,
                target_user_id=cursor.lastrowid,
                target_username=username,
                new_value="admin"
            )

            print("Administrator account created successfully.")

        else:
            # Update an existing user's password
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?
                WHERE id = ?
                """,
                (password_hash, user["id"])
            )

            accounts.record_audit_event(
                conn,
                actor=AUDIT_ACTOR,
                action=accounts.PASSWORD_CHANGED,
                target_user_id=user["id"],
                target_username=user["username"]
            )

            # The users_revoke_sessions trigger ends existing logins as
            # part of this same transaction
            print("Password updated successfully.")
            print("All existing login sessions for this account have ended.")

        # Save the database changes
        conn.commit()

    finally:
        # Always close the connection
        conn.close()


# Run the account management program
if __name__ == "__main__":
    if setup_account() is False:
        sys.exit(1)
