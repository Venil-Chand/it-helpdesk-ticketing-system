"""
Checks that decide whether this process may serve requests.

startup_problems(app)  run once by wsgi.py before Gunicorn accepts any
                       traffic; a non-empty list stops the server
readiness(app)         the same checks for the /readyz endpoint, reduced
                       to "ok" / "fail" per check

Results only ever name a check ("database", "schema", ...). They never
contain a file path, a version number, a hostname, a password or an
exception message, because readiness answers can be seen by monitoring
systems and, through a misconfigured proxy, by the public. The details an
administrator needs go to the server log instead.
"""

import sqlite3
from dataclasses import dataclass

from flask import current_app

import database as db
import migrations

OK = "ok"
FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    # For the server log only; never sent to a client
    log_detail: str = ""


def check_schema(path=None):
    """The database can be opened and its schema is up to date."""

    path = path or db.DB_PATH

    try:
        status = migrations.schema_status(path)
    except sqlite3.Error as error:
        return CheckResult(
            "database", False,
            f"database could not be opened ({type(error).__name__})"
        )

    if status.state == "missing":
        return CheckResult("database", False, "database file does not exist")

    if not status.is_current:
        return CheckResult(
            "schema", False,
            f"schema is not current (state={status.state}, "
            f"version={status.version}, expected={status.target}); "
            "run 'flask --app app init-db'"
        )

    return CheckResult("schema", True)


def check_database_query(path=None):
    """A trivial read succeeds, so the file is readable and not locked."""

    path = path or db.DB_PATH

    try:
        conn = migrations._connect_read_only(path)
        try:
            conn.execute("SELECT COUNT(*) FROM users").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as error:
        return CheckResult(
            "database", False,
            f"database query failed ({type(error).__name__})"
        )

    return CheckResult("database", True)


def check_rate_limit_storage(app):
    """The shared rate-limit store (Redis in production) answers."""

    # Imported here: security.py imports this package's errors module
    from .security import get_settings, rate_limit_storage_reachable

    if rate_limit_storage_reachable(app):
        return CheckResult("rate_limit_storage", True)

    return CheckResult(
        "rate_limit_storage", False,
        f"{get_settings(app).rate_limit_storage_kind} rate-limit storage "
        "did not answer; sign-in would be refused"
    )


def _checks(app):
    """
    Every readiness check, in order, as functions taking no arguments.

    The module-level functions are looked up each time, so tests can
    replace one check with patch().
    """

    return (
        lambda: check_schema(),
        lambda: check_database_query(),
        lambda: check_rate_limit_storage(app),
    )


def run_checks(app):
    """Run every readiness check for `app`. Returns [CheckResult, ...]."""

    results = []

    for check in _checks(app):
        result = check()
        results.append(result)

        # A database that cannot be opened makes the later checks noise
        if not result.ok and result.name == "database":
            break

    return results


def startup_problems(app):
    """Problems that must stop the server before it accepts traffic."""

    return [result.log_detail or result.name
            for result in run_checks(app) if not result.ok]


def readiness(app=None):
    """
    Return (ready, {check name: "ok" | "fail"}) and log any failure.

    Only check names and ok/fail leave this function.
    """

    app = app or current_app._get_current_object()
    results = run_checks(app)
    summary = {}

    for result in results:
        # A later check can succeed where an earlier one with the same
        # name failed; keep the failure
        if summary.get(result.name) != FAIL:
            summary[result.name] = OK if result.ok else FAIL

        if not result.ok:
            app.logger.error("Readiness check %s failed: %s",
                             result.name, result.log_detail or "no detail")

    return all(result.ok for result in results), summary
