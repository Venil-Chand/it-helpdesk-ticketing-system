"""
Backup commands, run through Flask's CLI:

    flask --app app backup-generate-key PATH     create a new key file
    flask --app app backup-create                encrypted, verified backup
    flask --app app backup-verify FILE           check a backup (read-only)
    flask --app app backup-list                  backups and their age
    flask --app app backup-transfer [FILE]       copy off-server and verify
    flask --app app backup-restore FILE --to NEW restore into a new file
    flask --app app backup-drill [FILE]          automated restoration drill
    flask --app app backup-prune [--apply]       apply the retention policy
    flask --app app backup-restore-live FILE     replace the live database
                                                 (interactive, destructive)

Settings (environment variables; see deploy/helpdesk.env.example):

    HELPDESK_BACKUP_DIR          where backups are written; must be outside
                                 the application folder, mode 0700
    HELPDESK_BACKUP_OFFSITE_DIR  a second location on other hardware
                                 (mounted share or disk), for transfer
    HELPDESK_BACKUP_KEY_FILE     the encryption key file (mode 0600), for
                                 example %d/backup-key from systemd's
                                 LoadCredential=

Like the other database commands these need a shell on the server as an
account that can read the database; that access is what authorises them.
Only the live restore can change the database, and it asks first.
"""

import os

import click
from flask.cli import with_appcontext

import backups
import database as db

from . import cli as base_cli
from .cli import _fail

BACKUP_DIR_VARIABLE = "HELPDESK_BACKUP_DIR"
OFFSITE_DIR_VARIABLE = "HELPDESK_BACKUP_OFFSITE_DIR"
KEY_FILE_VARIABLE = "HELPDESK_BACKUP_KEY_FILE"

key_file_option = click.option(
    "--key-file", type=click.Path(dir_okay=False),
    help=f"Key file to use instead of ${KEY_FILE_VARIABLE}.",
)


def _setting(value, variable, what):
    value = value or os.environ.get(variable, "").strip()

    if not value:
        _fail(f"no {what}: give it as an option or set {variable}.")

    return value


def _backup_dir(value=None):
    return _setting(value, BACKUP_DIR_VARIABLE, "backup directory")


def _key(key_file):
    path = key_file or os.environ.get(KEY_FILE_VARIABLE, "").strip()
    forbidden = [os.environ.get(name, "").strip()
                 for name in (BACKUP_DIR_VARIABLE, OFFSITE_DIR_VARIABLE)]

    try:
        return backups.load_key(path, forbidden_directories=[
            item for item in forbidden if item
        ])
    except backups.BackupError as error:
        _fail(f"{error}.")


def _resolve_backup(path, directory):
    """A given FILE, or the newest backup in the backup directory."""

    if path:
        return path

    latest = backups.latest_backup(_backup_dir(directory))

    if latest is None:
        _fail("there are no backups in the backup directory.")

    return latest[1]


def _describe(manifest):
    tables = manifest.get("tables", {})
    return (f"created {manifest.get('created_at')}, schema version "
            f"{manifest.get('schema_version')}, {tables.get('tickets', 0)} "
            f"tickets, {tables.get('users', 0)} accounts, "
            f"key id {manifest.get('key_id')}")


@click.command("backup-generate-key")
@click.argument("path", type=click.Path(dir_okay=False))
def backup_generate_key_command(path):
    """Create a new random backup encryption key in PATH (mode 0600).

    Keep a second copy somewhere safe and offline (for example a password
    manager or a sealed envelope): without the key, no backup made with
    it can ever be restored. Never store it next to the backups.
    """

    try:
        key = backups.write_new_key(path)
    except (backups.BackupError, OSError) as error:
        _fail(f"{error}. No key was written.")

    click.echo(f"New backup key written to {path} (key id {key.key_id}).")
    click.echo("Store a second copy offline now. Lost key = lost backups.")


@click.command("backup-create")
@click.option("--dest", type=click.Path(file_okay=False),
              help=f"Backup directory instead of ${BACKUP_DIR_VARIABLE}.")
@key_file_option
@with_appcontext
def backup_create_command(dest, key_file):
    """Make an encrypted backup of the database and verify it.

    The database is copied consistently while the application keeps
    running. Exits 1, writing nothing, if the database fails its
    integrity or foreign-key checks.
    """

    directory = _backup_dir(dest)
    key = _key(key_file)

    try:
        result = backups.create_backup(db.DB_PATH, directory, key)
    except (backups.BackupError, OSError) as error:
        _fail(f"backup failed: {error}")

    click.echo(f"Backup written and verified: {result.path}")
    click.echo(_describe(result.manifest))


@click.command("backup-verify")
@click.argument("path", type=click.Path(dir_okay=False, exists=True))
@key_file_option
def backup_verify_command(path, key_file):
    """Check FILE completely: checksum, decryption and database checks.

    Read-only. Exits 1 if anything is wrong.
    """

    key = _key(key_file)

    try:
        result = backups.verify_backup(path, key)
    except backups.BackupError as error:
        _fail(f"verification failed: {error}")

    click.echo(f"Backup verified: {_describe(result.manifest)}")
    click.echo("Integrity check: ok; foreign-key violations: 0")


@click.command("backup-list")
@click.option("--dir", "directory", type=click.Path(file_okay=False),
              help=f"Backup directory instead of ${BACKUP_DIR_VARIABLE}.")
def backup_list_command(directory):
    """List backups, newest last (reads names only; no key needed)."""

    found = backups.list_backups(_backup_dir(directory))

    if not found:
        click.echo("No backups found.")
        return

    for created, path in found:
        click.echo(f"{created.isoformat(timespec='seconds')}  "
                   f"{os.path.basename(path)}")

    age = backups.backup_age(_backup_dir(directory))
    click.echo(f"{len(found)} backup(s); newest is {age / 3600:.1f} h old.")


@click.command("backup-transfer")
@click.argument("path", required=False,
                type=click.Path(dir_okay=False, exists=True))
@click.option("--to", "destination", type=click.Path(file_okay=False),
              help=f"Destination instead of ${OFFSITE_DIR_VARIABLE}.")
@click.option("--dir", "directory", type=click.Path(file_okay=False),
              help="Where to find the newest backup when FILE is omitted.")
@key_file_option
def backup_transfer_command(path, destination, directory, key_file):
    """Copy a backup (default: the newest) to the off-server location.

    The copy is decrypted and checked at the destination before the
    command reports success.
    """

    destination = _setting(destination, OFFSITE_DIR_VARIABLE,
                           "off-server destination")
    path = _resolve_backup(path, directory)
    key = _key(key_file)

    try:
        result = backups.transfer(path, destination, key)
    except (backups.BackupError, OSError) as error:
        _fail(f"transfer failed: {error}")

    click.echo(f"Copied and verified at the destination: {result.path}")


@click.command("backup-restore")
@click.argument("path", type=click.Path(dir_okay=False, exists=True))
@click.option("--to", "target", required=True,
              type=click.Path(dir_okay=False),
              help="NEW database file to create. The live database is "
                   "always refused.")
@key_file_option
@with_appcontext
def backup_restore_command(path, target, key_file):
    """Restore FILE into a new database file, for inspection or a drill.

    Never touches the live database. To replace the live database, see
    backup-restore-live.
    """

    key = _key(key_file)

    try:
        check = backups.restore_to_new_file(path, key, target,
                                            live_db_path=db.DB_PATH)
    except (backups.BackupError, OSError) as error:
        _fail(f"restore failed: {error}")

    click.echo(f"Restored into {target} (schema version "
               f"{check.schema_version}, {check.schema_state}); integrity "
               "check ok, no foreign-key violations.")


@click.command("backup-drill")
@click.argument("path", required=False,
                type=click.Path(dir_okay=False, exists=True))
@click.option("--dir", "directory", type=click.Path(file_okay=False),
              help=f"Backup directory instead of ${BACKUP_DIR_VARIABLE}.")
@click.option("--no-record", is_flag=True,
              help="Do not save the drill report in <backup dir>/drills/.")
@key_file_option
def backup_drill_command(path, directory, no_record, key_file):
    """Restore a backup (default: the newest) into a disposable database
    and check that the application could use it.

    The disposable copy is deleted afterwards; the live database is never
    opened. The report is saved for monitoring. Exits 1 if the drill fails.
    """

    path = _resolve_backup(path, directory)
    key = _key(key_file)

    report = backups.run_drill(path, key)

    click.echo(f"Restoration drill of {report.backup_file} "
               f"(made {report.backup_created_at or 'unknown'}):")
    for name, value in report.checks.items():
        click.echo(f"  {name}: {value}")
    click.echo(f"  time to restore and check: {report.seconds:.2f} s")

    if not no_record:
        try:
            saved = backups.record_drill(report, directory or os.path.dirname(
                os.path.abspath(path)))
            click.echo(f"Report saved: {saved}")
        except (backups.BackupError, OSError) as error:
            click.echo(f"Warning: report not saved ({error})", err=True)

    if not report.passed:
        for problem in report.problems:
            click.echo(f"Problem: {problem}", err=True)
        _fail("the restoration drill FAILED.")

    click.echo("Restoration drill passed.")


@click.command("backup-prune")
@click.option("--dir", "directory", type=click.Path(file_okay=False),
              help=f"Backup directory instead of ${BACKUP_DIR_VARIABLE}.")
@click.option("--keep-daily", default=7, show_default=True,
              type=click.IntRange(1, 366))
@click.option("--keep-weekly", default=4, show_default=True,
              type=click.IntRange(0, 520))
@click.option("--keep-monthly", default=6, show_default=True,
              type=click.IntRange(0, 120))
@click.option("--apply", is_flag=True,
              help="Actually delete. Without it, only report the plan.")
def backup_prune_command(directory, keep_daily, keep_weekly, keep_monthly,
                         apply):
    """Delete backups outside the retention policy (dry run by default).

    Keeps the newest backup of each of the last 7 days, 4 weeks and 6
    months, and always the newest backup overall.
    """

    directory = _backup_dir(directory)
    keep, remove = backups.plan_retention(
        backups.list_backups(directory), keep_daily=keep_daily,
        keep_weekly=keep_weekly, keep_monthly=keep_monthly
    )

    click.echo(f"Keeping {len(keep)} backup(s); "
               f"{len(remove)} outside the retention policy.")

    for path in remove:
        click.echo(("  deleting " if apply else "  would delete ")
                   + os.path.basename(path))
        if apply:
            backups.remove_backup(path)

    if remove and not apply:
        click.echo("Dry run: nothing was deleted. Add --apply to delete.")


def live_restore_phrase(db_path):
    return f"replace {os.path.basename(db_path)}"


@click.command("backup-restore-live")
@click.argument("path", type=click.Path(dir_okay=False, exists=True))
@click.option("--safety-dir", type=click.Path(file_okay=False),
              help="Where to save the safety backup of the current "
                   f"database (default ${BACKUP_DIR_VARIABLE}).")
@key_file_option
@with_appcontext
def backup_restore_live_command(path, safety_dir, key_file):
    """Replace the LIVE database with FILE. Destructive; asks first.

    Stop the web and notification services before running this. The
    command refuses while the database is in use, verifies the backup
    completely, saves an encrypted safety backup of the current database,
    and only then replaces it. It must be run in an interactive terminal,
    and the operator must type the confirmation phrase.
    """

    if not base_cli._interactive():
        _fail("run this command in an interactive terminal; the live "
              "database was not changed.")

    key = _key(key_file)
    safety_dir = _backup_dir(safety_dir)

    try:
        result = backups.verify_backup(path, key)
    except backups.BackupError as error:
        _fail(f"the backup failed verification ({error}); the live "
              "database was not changed.")

    problem = (backups.database_in_use(db.DB_PATH)
               if os.path.exists(db.DB_PATH) else None)
    if problem:
        _fail(f"{problem}. The live database was not changed.")

    phrase = live_restore_phrase(db.DB_PATH)

    click.echo("WARNING: this replaces the live database. Every change made "
               "after the backup was taken will be lost.")
    click.echo(f"Live database: {db.DB_PATH}")
    click.echo(f"Backup: {_describe(result.manifest)}")
    click.echo("A safety backup of the current database is saved first.")

    typed = click.prompt(f'Type "{phrase}" to continue', default="",
                         show_default=False)

    if typed.strip() != phrase:
        _fail("confirmation did not match; the live database was not "
              "changed.")

    try:
        check, safety = backups.restore_live(
            path, key, live_db_path=db.DB_PATH, safety_dir=safety_dir,
            confirmed=True
        )
    except (backups.BackupError, OSError) as error:
        _fail(f"live restore failed: {error}")

    if safety:
        click.echo(f"Safety backup of the replaced database: {safety.path}")

    click.echo(f"Live database restored (schema version "
               f"{check.schema_version}, {check.schema_state}).")

    if check.schema_state == "outdated":
        click.echo("The restored schema is older than this application: "
                   "run 'flask --app app init-db' before starting the "
                   "services.")

    click.echo(f"Restored by {base_cli._operator()}. Start the services "
               "and check /readyz.")


COMMANDS = (
    backup_generate_key_command,
    backup_create_command,
    backup_verify_command,
    backup_list_command,
    backup_transfer_command,
    backup_restore_command,
    backup_drill_command,
    backup_prune_command,
    backup_restore_live_command,
)
