"""
One monitoring command for the whole helpdesk:

    flask --app app ops-check          human-readable, one line per check
    flask --app app ops-check --json   the same for a monitoring system

Exit status: 0 everything OK, 1 at least one WARNING, 2 at least one
CRITICAL. helpdesk-ops-check.timer runs it every five minutes; a CRITICAL
starts the alert unit.

What it checks (read-only; it never changes anything):

    configuration      the application settings (already validated when
                       the command starts) and the notification settings
    database           readable, schema current, quick integrity check,
                       no foreign-key violations, kept outside the code
    rate_limit_storage Redis answers (sign-in is refused otherwise)
    notifications      backlog age, stuck deliveries, recent permanent
                       failures, changes saved without their notifications
    worker             the notification worker's heartbeat is fresh
    backups            newest backup age, off-server copy, last drill
    disk               free space where the database and backups live

Messages contain counts, ages and setting names only: never passwords,
keys, email addresses, ticket text or file paths, so the output can be
sent to an external monitoring service.
"""

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import click
from flask import current_app
from flask.cli import with_appcontext

import backups
import database as db
import migrations
from notifications import heartbeat, queue
from notifications import settings as delivery_settings

from .backup_cli import BACKUP_DIR_VARIABLE, OFFSITE_DIR_VARIABLE
from .security import get_settings, rate_limit_storage_reachable

OK = "OK"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
EXIT_CODES = {OK: 0, WARNING: 1, CRITICAL: 2}

# Thresholds. Backups run nightly, so 26 hours allows for a late run.
BACKUP_MAX_AGE_HOURS = 26
DRILL_MAX_AGE_DAYS = 8
# The worker checks every 30 s and waits at most 15 minutes while the
# mail server is down; its heartbeat is rewritten at least that often
HEARTBEAT_MAX_AGE_SECONDS = 20 * 60
BACKLOG_WARNING_SECONDS = 15 * 60
BACKLOG_CRITICAL_SECONDS = 60 * 60
DISK_WARNING_FRACTION = 0.10
DISK_CRITICAL_FRACTION = 0.03


@dataclass
class Result:
    name: str
    level: str
    message: str


def _hours(seconds):
    return f"{seconds / 3600:.1f} h"


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------
# INDIVIDUAL CHECKS
# ---------------------------------------------------

def check_configuration(app):
    production = get_settings(app).is_production
    results = [Result("configuration", OK,
                      f"application settings valid "
                      f"({get_settings(app).environment})")]

    try:
        delivery = delivery_settings.load_delivery_settings(
            production=production)
    except delivery_settings.NotificationConfigError as error:
        results.append(Result("notification_settings", CRITICAL,
                              f"invalid: {error}"))
        return results

    if delivery.is_simulated:
        results.append(Result(
            "notification_settings", WARNING if production else OK,
            "delivery is SIMULATED (local sink): no email is sent"))
    else:
        results.append(Result("notification_settings", OK,
                              "delivery by SMTP"))

    return results


def check_database(app):
    production = get_settings(app).is_production
    path = db.DB_PATH
    results = []

    try:
        status = migrations.schema_status(path)
    except sqlite3.Error as error:
        return [Result("database", CRITICAL,
                       f"cannot be opened ({type(error).__name__})")]

    if status.state == "missing":
        return [Result("database", CRITICAL, "database file does not exist")]

    if not status.is_current:
        results.append(Result(
            "schema", CRITICAL,
            f"{status.state} (version {status.version}, expected "
            f"{status.target}); run 'flask --app app init-db'"))
    else:
        results.append(Result("schema", OK,
                              f"current (version {status.version})"))

    try:
        conn = migrations._connect_read_only(path)
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            violations = len(conn.execute(
                "PRAGMA foreign_key_check").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as error:
        return results + [Result("database", CRITICAL,
                                 f"check failed ({type(error).__name__})")]

    if quick != "ok":
        results.append(Result("database", CRITICAL,
                              "integrity check reports damage; restore "
                              "from the last verified backup"))
    elif violations:
        results.append(Result("database", CRITICAL,
                              f"{violations} foreign-key violation(s)"))
    else:
        results.append(Result("database", OK,
                              "readable; integrity ok; no foreign-key "
                              "violations"))

    if production and backups._inside(path, backups.PROJECT_ROOT):
        results.append(Result(
            "database_location", WARNING,
            "the database is inside the application folder; set "
            "HELPDESK_DATABASE_PATH so deployments never touch it"))

    return results


def check_rate_limit_storage(app):
    kind = get_settings(app).rate_limit_storage_kind

    if rate_limit_storage_reachable(app):
        return [Result("rate_limit_storage", OK, f"{kind} answering")]

    return [Result("rate_limit_storage", CRITICAL,
                   f"{kind} not answering: sign-in and password changes "
                   "are being refused")]


def check_notifications(app, now):
    try:
        health = queue.health(db.DB_PATH, now=now)
    except sqlite3.Error as error:
        return [Result("notifications", CRITICAL,
                       f"queue cannot be read ({type(error).__name__})")]

    results = []
    age = health.oldest_due_seconds

    if age >= BACKLOG_CRITICAL_SECONDS:
        level = CRITICAL
    elif age >= BACKLOG_WARNING_SECONDS:
        level = WARNING
    else:
        level = OK

    results.append(Result(
        "notification_backlog", level,
        f"{health.due} due, {health.pending} pending"
        + (f"; oldest due for {int(age // 60)} min" if health.due else "")))

    if health.stuck_processing:
        results.append(Result(
            "notification_claims", WARNING,
            f"{health.stuck_processing} delivery(ies) interrupted; the "
            "worker recovers them automatically"))

    if health.failed_last_day:
        results.append(Result(
            "notification_failures", WARNING,
            f"{health.failed_last_day} permanent failure(s) in 24 h; see "
            "'flask --app app notifications-status'"))
    else:
        results.append(Result("notification_failures", OK,
                              "no permanent failures in 24 h"))

    if health.queue_failures_last_day:
        results.append(Result(
            "notification_queueing", WARNING,
            f"{health.queue_failures_last_day} change(s) in 24 h were saved "
            "without their notifications; see the audit log "
            "(Operations) and the application log"))
    else:
        results.append(Result("notification_queueing", OK,
                              "no queueing failures in 24 h"))

    return results


def check_worker(app, now):
    production = get_settings(app).is_production
    status = heartbeat.read_status(heartbeat.heartbeat_path())

    if status is None:
        return [Result("worker", CRITICAL if production else WARNING,
                       "no heartbeat: the notification worker has not run "
                       "(or HELPDESK_STATE_DIR differs between services)")]

    age = heartbeat.status_age_seconds(status, now)

    if status.get("state") == heartbeat.STOPPED:
        return [Result("worker", CRITICAL if production else WARNING,
                       "the notification worker was stopped")]

    if age is None or age > HEARTBEAT_MAX_AGE_SECONDS:
        return [Result("worker", CRITICAL,
                       "heartbeat is stale"
                       + (f" ({int(age // 60)} min old)" if age else "")
                       + ": the worker has died or is stuck")]

    if status.get("state") == heartbeat.PAUSED:
        return [Result("worker", WARNING,
                       "delivery paused: "
                       + str(status.get("paused_reason", ""))[:200])]

    return [Result("worker", OK, f"running (heartbeat {int(age)} s ago)")]


def check_backups(app, now):
    production = get_settings(app).is_production
    directory = os.environ.get(BACKUP_DIR_VARIABLE, "").strip()
    offsite = os.environ.get(OFFSITE_DIR_VARIABLE, "").strip()
    results = []

    if not directory:
        return [Result("backups", CRITICAL if production else OK,
                       f"{BACKUP_DIR_VARIABLE} is not set"
                       + ("" if production else " (development)"))]

    age = backups.backup_age(directory, now)

    if age is None:
        results.append(Result("backups", CRITICAL, "no backups exist"))
    elif age > BACKUP_MAX_AGE_HOURS * 3600:
        results.append(Result("backups", CRITICAL,
                              f"newest backup is {_hours(age)} old; the "
                              "nightly backup is failing or not running"))
    else:
        results.append(Result("backups", OK,
                              f"newest backup {_hours(age)} old"))

    if offsite:
        offsite_age = (backups.backup_age(offsite, now)
                       if os.path.isdir(offsite) else None)
        if offsite_age is None:
            results.append(Result("backups_offsite", WARNING,
                                  "no backups in the off-server location "
                                  "(not mounted?)"))
        elif offsite_age > BACKUP_MAX_AGE_HOURS * 3600:
            results.append(Result("backups_offsite", WARNING,
                                  f"newest off-server copy is "
                                  f"{_hours(offsite_age)} old"))
        else:
            results.append(Result("backups_offsite", OK,
                                  f"newest off-server copy "
                                  f"{_hours(offsite_age)} old"))
    elif production:
        results.append(Result("backups_offsite", WARNING,
                              f"{OFFSITE_DIR_VARIABLE} is not set: backups "
                              "exist on this server only"))

    drill = backups.latest_drill(directory)

    if drill is None:
        results.append(Result("restore_drill", WARNING,
                              "no restoration drill has been recorded"))
    else:
        try:
            drill_age = (now - datetime.fromisoformat(
                drill["started_at"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            drill_age = None

        if not drill.get("passed"):
            results.append(Result("restore_drill", CRITICAL,
                                  "the last restoration drill FAILED"))
        elif drill_age is None or drill_age > DRILL_MAX_AGE_DAYS * 86400:
            results.append(Result("restore_drill", WARNING,
                                  "the last successful drill is more than "
                                  f"{DRILL_MAX_AGE_DAYS} days old"))
        else:
            results.append(Result("restore_drill", OK,
                                  f"last drill passed {_hours(drill_age)} "
                                  "ago"))

    return results


def check_disk(app):
    results = []
    seen = set()

    for name, path in (("database", os.path.dirname(db.DB_PATH)),
                       ("backups", os.environ.get(BACKUP_DIR_VARIABLE, ""))):
        if not path or not os.path.isdir(path):
            continue

        usage = shutil.disk_usage(path)
        device = os.stat(path).st_dev
        if device in seen:
            continue
        seen.add(device)

        free = usage.free / usage.total if usage.total else 1.0

        if free < DISK_CRITICAL_FRACTION:
            level = CRITICAL
        elif free < DISK_WARNING_FRACTION:
            level = WARNING
        else:
            level = OK

        results.append(Result(f"disk_{name}", level,
                              f"{free:.0%} free"))

    return results


# ---------------------------------------------------
# THE COMMAND
# ---------------------------------------------------

def run_all(app, now=None):
    now = now or _now()
    results = []

    for check in (
        lambda: check_configuration(app),
        lambda: check_database(app),
        lambda: check_rate_limit_storage(app),
        lambda: check_notifications(app, now),
        lambda: check_worker(app, now),
        lambda: check_backups(app, now),
        lambda: check_disk(app),
    ):
        try:
            results.extend(check())
        except Exception as error:  # a broken check must not hide others
            results.append(Result("ops_check", CRITICAL,
                                  f"a check failed to run "
                                  f"({type(error).__name__})"))

    return results


def overall(results):
    levels = {result.level for result in results}

    for level in (CRITICAL, WARNING):
        if level in levels:
            return level

    return OK


@click.command("ops-check")
@click.option("--json", "as_json", is_flag=True,
              help="Print one JSON object instead of text.")
@with_appcontext
def ops_check_command(as_json):
    """Check everything an operator needs to know about (read-only).

    Exits 0 when all is well, 1 for warnings, 2 for critical problems.
    """

    results = run_all(current_app._get_current_object())
    status = overall(results)

    if as_json:
        click.echo(json.dumps({
            "status": status,
            "time": _now().isoformat(timespec="seconds"),
            "checks": [asdict(result) for result in results],
        }, sort_keys=True))
    else:
        width = max(len(result.name) for result in results)
        for result in results:
            click.echo(f"{result.level:<8} {result.name:<{width}}  "
                       f"{result.message}")
        click.echo(f"Overall: {status}")

    sys.exit(EXIT_CODES[status])


COMMANDS = (ops_check_command,)
