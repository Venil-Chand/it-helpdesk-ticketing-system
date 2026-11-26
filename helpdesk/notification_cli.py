"""
Notification commands, run through Flask's CLI:

    flask --app app notifications-send [--loop] [--limit N] [--interval S]
                                      deliver queued notifications
    flask --app app notifications-status
                                      queue counts and recent failures
                                      (read-only)
    flask --app app notifications-requeue ID... --reason TEXT
                                      send failed notifications again
                                      (audited, bounded)
    flask --app app notifications-check-config
                                      validate the delivery settings
                                      without connecting anywhere
    flask --app app set-email USERNAME ADDRESS
    flask --app app set-email USERNAME --clear
                                      where an account is notified

The web application only queues notifications; these commands are the
only code that delivers them. Like the other database commands, they need
a shell on the server as an account that can read and write the database.
"""

import logging
import signal
import sqlite3
import sys
import threading

import click
from flask import current_app
from flask.cli import with_appcontext

import accounts
import database as db
from notifications import queue, settings as delivery_settings
from notifications import heartbeat, transport, worker

from . import cli as base_cli
from .cli import _fail, _require_current_schema
from .logging_setup import configure_json_logging
from .security import get_settings

# Exit status of notifications-send when delivery is unavailable
EXIT_DELIVERY_UNAVAILABLE = 2


def _load_settings():
    production = get_settings(current_app).is_production

    try:
        return delivery_settings.load_delivery_settings(
            production=production
        )
    except delivery_settings.NotificationConfigError as error:
        _fail(f"notification settings: {error}. Nothing was sent.")


def _configure_logging():
    """Send the worker's log lines (no addresses or content) to stderr.

    In production they are JSON lines, like the web application's, so
    the journal holds one consistent format.
    """

    if get_settings(current_app).is_production:
        configure_json_logging()
        return

    log = logging.getLogger("notifications")

    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def _echo_summary(summary, simulated):
    parts = [f"{status}: {count}"
             for status, count in sorted(summary.outcomes.items())]
    click.echo(
        f"Processed {summary.claimed} notification(s)"
        + (f" ({', '.join(parts)})" if parts else "")
        + (" [SIMULATED - local sink, nothing was emailed]"
           if simulated and summary.claimed else "")
    )

    if summary.recovered:
        click.echo(f"Recovered {summary.recovered} interrupted delivery(ies)")
    if summary.expired:
        click.echo(f"Expired {summary.expired} notification(s) that were "
                   "too old to send")
    if summary.lost_claims:
        click.echo(f"{summary.lost_claims} result(s) not recorded because "
                   "the claim had expired", err=True)
    if summary.stopped_reason and summary.stopped_reason != "stop requested":
        click.echo(f"Delivery paused: {summary.stopped_reason}", err=True)


@click.command("notifications-send")
@click.option("--limit", default=100, show_default=True,
              type=click.IntRange(1, 10000),
              help="Most notifications to deliver per run.")
@click.option("--loop", is_flag=True,
              help="Keep running, for a process supervisor such as systemd.")
@click.option("--interval", default=30, show_default=True,
              type=click.IntRange(1, 3600),
              help="Seconds between runs with --loop.")
@with_appcontext
def notifications_send_command(limit, loop, interval):
    """Deliver queued notifications.

    Without --loop, one run is made and the command exits: 0 when done,
    2 if delivery was paused because the mail server was unavailable or
    refused the sign-in. With --loop it runs until SIGTERM or Ctrl+C,
    finishing the current message first.
    """

    _require_current_schema()
    settings = _load_settings()
    _configure_logging()

    for line in settings.describe():
        click.echo(line)

    runner = worker.NotificationWorker(
        settings, transport.make_transport(settings), db_path=db.DB_PATH
    )

    if not loop:
        summary = runner.run_once(limit)
        _echo_summary(summary, settings.is_simulated)

        if summary.stopped_reason:
            sys.exit(EXIT_DELIVERY_UNAVAILABLE)
        return

    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop)
        for sig in (signal.SIGTERM, signal.SIGINT)
    }

    beat = heartbeat.Heartbeat(heartbeat.heartbeat_path(), runner.worker_id)

    try:
        click.echo(f"Worker running; checking every {interval}s. "
                   "Send SIGTERM or press Ctrl+C to stop.")
        runner.run_forever(
            interval=interval, limit=limit, stop_event=stop,
            on_summary=lambda s: s.claimed and _echo_summary(
                s, settings.is_simulated),
            heartbeat=beat,
        )
        beat.stopped()
        click.echo("Worker stopped.")

    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@click.command("notifications-status")
@with_appcontext
def notifications_status_command():
    """Show queue counts and the most recent failures (read-only).

    No email address or message content is shown.
    """

    _require_current_schema()

    try:
        counts, oldest, failures = queue.status_counts(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__})")

    for status in queue.STATUSES:
        label = status
        if status == queue.SIMULATED:
            label = "simulated (local sink, not emailed)"
        click.echo(f"{label}: {counts[status]}")

    if oldest:
        click.echo(f"Oldest pending notification queued at: {oldest}")

    if failures:
        click.echo("Recent permanent failures:")
        for row in failures:
            click.echo(
                f"  #{row['id']} {row['event_type']} to "
                f"{row['recipient_username']!r}: {row['last_error']} "
                f"(attempts {row['attempts']}, deferrals {row['deferrals']})"
            )


@click.command("notifications-check-config")
@with_appcontext
def notifications_check_config_command():
    """Validate the delivery settings without connecting to anything.

    Prints whether SMTP credentials are set, never their values.
    """

    settings = _load_settings()

    for line in settings.describe():
        click.echo(line)

    click.echo("Notification settings are valid.")


@click.command("set-email")
@click.argument("username")
@click.argument("address", required=False)
@click.option("--clear", is_flag=True,
              help="Remove the address; the account is then not emailed.")
@with_appcontext
def set_email_command(username, address, clear):
    """Set the address USERNAME's notifications are sent to.

    Recorded in the audit log as "cli:<operating-system account>"; the
    address itself is not written to the audit log.
    """

    if clear == bool(address):
        _fail("give either an ADDRESS or --clear.")

    _require_current_schema()

    try:
        changed = accounts.operator_set_email(
            username, None if clear else address, operator=base_cli._operator()
        )

    except accounts.AccountNotFound:
        _fail("no account has that exact username; nothing was changed.")

    except accounts.AccountError as error:
        _fail(f"{error} Nothing was changed.")

    except sqlite3.Error as error:
        _fail("the database could not be updated, so nothing was changed "
              f"({type(error).__name__}).")

    if not changed:
        click.echo("No change: the account already has that setting.")
    elif clear:
        click.echo(f"Email address removed for {username!r}; it will not "
                   "receive notifications.")
    else:
        click.echo(f"Email address set for {username!r}.")


@click.command("notifications-requeue")
@click.argument("notification_ids", nargs=-1, required=True, type=int)
@click.option("--reason", required=True,
              help="Why these are being sent again (recorded in the audit "
                   "log), e.g. 'SMTP password fixed, ticket #812'.")
@click.option("--yes", is_flag=True,
              help="Do not ask for confirmation.")
@with_appcontext
def notifications_requeue_command(notification_ids, reason, yes):
    """Queue new copies of permanently FAILED notifications.

    Use after fixing the cause of the failure (for example a wrong SMTP
    password). The failed rows and their history are kept; each copy gets
    fresh delivery attempts. A notification can be requeued at most 3
    times, and each requeue is recorded in the audit log with the
    operating-system account that ran this command and the reason.
    """

    _require_current_schema()

    if not yes:
        if not base_cli._interactive():
            _fail("confirm with --yes when not running in a terminal; "
                  "nothing was requeued.")
        click.confirm(f"Requeue {len(notification_ids)} failed "
                      "notification(s)?", abort=True)

    operator = base_cli._operator()
    failures = 0

    for notification_id in notification_ids:
        try:
            new_id = queue.requeue(db.DB_PATH, notification_id,
                                   operator=operator, reason=reason)
        except queue.RequeueError as error:
            failures += 1
            click.echo(f"#{notification_id}: not requeued: {error}",
                       err=True)
            continue
        except sqlite3.Error as error:
            failures += 1
            click.echo(f"#{notification_id}: not requeued: database error "
                       f"({type(error).__name__})", err=True)
            continue

        click.echo(f"#{notification_id}: requeued as #{new_id}")

    if failures:
        sys.exit(1)


COMMANDS = (
    notifications_send_command,
    notifications_requeue_command,
    notifications_status_command,
    notifications_check_config_command,
    set_email_command,
)
