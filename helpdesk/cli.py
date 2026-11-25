"""
Database command-line tools, run through Flask's CLI:

    flask --app app db-status         report the schema version (read-only)
    flask --app app init-db           create or upgrade the schema
    flask --app app assignment-audit  list ineligible assignees (read-only)
    flask --app app reset-password USERNAME
                                      recover an account whose password is
                                      lost, including the last administrator

create_app() attaches these to every application with register_commands().
They act on database.DB_PATH and never create user accounts; use
setup_accounts.py for that.

Running them requires a shell on the server as an account that can read
and write the database file. That operating-system access is what
authorises them; see "Administrator recovery" in README.md.
"""

import os
import pwd
import sqlite3
import sys

import click
from flask.cli import with_appcontext

import accounts
import database as db
import migrations

STATE_DESCRIPTIONS = {
    "missing": "database file does not exist yet",
    "uninitialized": "empty database",
    "legacy": "database created before schema versioning",
    "outdated": "older schema version",
    "current": "up to date",
    "too_new": "created by a newer version of this application",
    "incompatible": "unexpected or incompatible schema",
}


def _echo_status(status):
    """Print a schema status. Never prints stored data or secrets."""

    click.echo(f"Database: {db.DB_PATH}")
    click.echo(
        f"Current schema version: {status.version} "
        f"({STATE_DESCRIPTIONS.get(status.state, status.state)})"
    )
    click.echo(f"Latest supported version: {status.target}")

    if status.pending:
        click.echo("Pending migrations:")
        for migration in status.pending:
            click.echo(f"  {migration.version}: {migration.description}")
        if status.state == "legacy":
            click.echo(
                "  (existing tables and data are kept; each step only "
                "adds what is missing)"
            )
    elif status.is_current:
        click.echo("Pending migrations: none")

    for problem in status.problems:
        click.echo(f"Problem: {problem}", err=True)


def _fail(message):
    click.echo(f"Error: {message}", err=True)
    sys.exit(1)


@click.command("db-status")
@with_appcontext
def db_status_command():
    """Report the database schema version without changing anything.

    Exits 0 only if the schema is up to date, so it can be used as a
    pre-start check for the application service.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__}: {error})")

    _echo_status(status)

    if not status.is_current:
        click.echo(
            "The schema is not up to date. "
            "Back up the database, then run: flask --app app init-db",
            err=True
        )
        sys.exit(1)


@click.command("init-db")
@click.option(
    "--backup-to",
    type=click.Path(dir_okay=False),
    help="Write a verified backup of the existing database to this new "
         "file before applying migrations.",
)
@with_appcontext
def init_db_command(backup_to):
    """Create the database or apply pending schema migrations.

    Never creates user accounts; use setup_accounts.py for the first
    administrator.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__}: {error})")

    _echo_status(status)

    if status.state in ("too_new", "incompatible"):
        _fail("migration refused; the database was not changed.")

    if status.is_current:
        click.echo("Database schema is already up to date.")
        return

    if backup_to:
        if status.state == "missing":
            _fail("there is no existing database to back up.")

        try:
            migrations.backup_database(db.DB_PATH, backup_to)
        except (migrations.MigrationError, sqlite3.Error, OSError) as error:
            _fail(f"backup failed, so no migration was attempted: {error}")

        click.echo(f"Verified backup written to: {backup_to}")

    elif status.state in ("legacy", "outdated"):
        click.echo(
            "Warning: no --backup-to given. Make sure a verified backup "
            "of this database exists before upgrading production.",
            err=True
        )

    click.echo("Applying migrations...")

    try:
        result = migrations.migrate(db.DB_PATH)

    except (migrations.MigrationError, sqlite3.Error) as error:
        _fail(
            f"migration failed and was rolled back; the database was not "
            f"changed ({type(error).__name__}: {error})"
        )

    for migration in result.applied:
        click.echo(f"  applied {migration.version}: {migration.description}")

    click.echo(f"Database schema is now at version {result.final_version}.")


@click.command("assignment-audit")
@with_appcontext
def assignment_audit_command():
    """List tickets assigned to accounts that could not be chosen today.

    Read-only: the database is opened with mode=ro and nothing is fixed
    automatically. Such tickets keep their assignee and history; reassign
    or unassign them from the ticket page. Exits 1 if any are found.
    """

    if not os.path.exists(db.DB_PATH):
        _fail("the database file does not exist.")

    try:
        found = db.find_ineligible_assignments()
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__})")

    if not found:
        click.echo("Every assigned ticket has an eligible assignee.")
        return

    click.echo(f"{len(found)} ticket(s) have an assignee who is no longer "
               "eligible:")

    for ticket_id, assignee, reason in found:
        # repr() shows hidden characters in hand-edited values safely
        click.echo(f"  ticket #{ticket_id}: {assignee!r} ({reason})")

    sys.exit(1)


def _interactive():
    """True when a person is typing at a terminal (stdin is a TTY)."""

    return sys.stdin.isatty()


def _operator():
    """
    The operating-system account running this command, for the audit log.

    Taken from the process's real user id, not from $USER or $LOGNAME,
    which whoever runs the command can set to anything. When run through
    sudo, the invoking account named by sudo is added. That part comes
    from the environment, so the system's own sudo/auth log remains the
    authoritative record of who ran the command.
    """

    try:
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        name = f"uid {os.getuid()}"

    invoker = os.environ.get("SUDO_USER")

    if invoker and invoker != name:
        name = f"{name} (sudo by {invoker})"

    return name[:64] or "unknown"


def _require_current_schema():
    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__})")

    if not status.is_current:
        _fail(
            "the database schema is not up to date; nothing was changed. "
            "Run 'flask --app app init-db' first."
        )


@click.command("reset-password")
@click.argument("username")
@with_appcontext
def reset_password_command(username):
    """Set a new password for USERNAME, for example a locked-out admin.

    The password is typed twice at a hidden prompt; it is never accepted
    as an argument, an environment variable or piped input. Every session
    of the account ends, the owner must choose a new password at the next
    sign-in, and an audit event names the operating-system account that
    ran the command. Deactivated accounts are refused.
    """

    if not _interactive():
        _fail(
            "run this command in an interactive terminal. Passwords are "
            "never read from pipes, files or arguments. Nothing was changed."
        )

    _require_current_schema()

    account = accounts.get_account_by_username(username)

    if account is None:
        _fail("no account has that exact username; nothing was changed.")

    # Checked again inside the transaction; this only avoids asking for a
    # password that could not be used
    if account["is_active"] != 1:
        _fail(
            "this account is deactivated, so its password was not changed. "
            "An administrator must reactivate it first."
        )

    password = click.prompt(
        "New password",
        hide_input=True,
        confirmation_prompt="Repeat new password"
    )

    try:
        accounts.operator_reset_password(
            username, password, operator=_operator()
        )

    except accounts.AccountNotFound:
        _fail("no account has that exact username; nothing was changed.")

    except accounts.AccountError as error:
        _fail(f"{error} Nothing was changed.")

    except sqlite3.Error as error:
        _fail(
            "the database could not be updated, so nothing was changed "
            f"({type(error).__name__})."
        )

    click.echo(
        f"Password for {username!r} reset. All of its sessions have ended, "
        "and a new password must be chosen at the next sign-in. Give the "
        "temporary password to the owner through a trusted channel."
    )


COMMANDS = (
    db_status_command,
    init_db_command,
    assignment_audit_command,
    reset_password_command,
)


def register_commands(app):
    """Make the database commands available as "flask --app app <name>"."""

    for command in COMMANDS:
        app.cli.add_command(command)


def schema_ready_for_startup():
    """
    Check the schema before the development server starts.

    "python app.py" never creates or upgrades the database itself, so a
    missing or outdated schema stops startup with clear instructions
    instead of failing later with errors on every page.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        print(
            f"Cannot read the database: {type(error).__name__}: {error}",
            file=sys.stderr
        )
        return False

    if status.is_current:
        return True

    print(
        "The database schema is not up to date "
        f"({STATE_DESCRIPTIONS.get(status.state, status.state)}).\n"
        "Check it with:    flask --app app db-status\n"
        "Upgrade it with:  flask --app app init-db",
        file=sys.stderr
    )
    return False
