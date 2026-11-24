"""
Error responses shared by the whole application.

These are plain-text on purpose: they can happen before the user is
loaded, or while the database is unavailable, when rendering a normal
page template could itself fail. Messages are fixed text and never
repeat anything from the request, the database or the configuration.
"""

import sqlite3

from flask import Response, current_app

import database as db
import migrations


# Shown to anyone who reaches the site while the schema is not ready.
# It deliberately names no file paths, versions or table names.
SCHEMA_NOT_READY_MESSAGE = (
    "The helpdesk is temporarily unavailable because its database has "
    "not been initialised or upgraded. An administrator must run the "
    "database migration.\n"
)


# Shown when an account cannot be verified for any other database reason.
AUTH_UNAVAILABLE_MESSAGE = (
    "The helpdesk cannot verify your account right now. "
    "Please try again shortly.\n"
)


def plain_error(message, status):
    """Build a short plain-text error page."""

    return Response(
        message,
        status=status,
        content_type="text/plain; charset=utf-8"
    )


def bad_request(error):
    """Refuse a malformed request, such as a missing CSRF token."""

    return plain_error(
        "The request could not be processed. Go back, reload the page "
        "and try again.\n",
        400
    )


def request_too_large(error):
    """Refuse a request body above the configured limit."""

    return plain_error(
        "The submitted data is too large. The limit is 1 MiB per request. "
        "Nothing was saved.\n",
        413
    )


def schema_not_ready_response():
    """
    Return an explained 503 if the schema is not ready, otherwise None.

    Web workers never migrate the database themselves; migrations are an
    explicit deployment step ("flask --app app init-db"). The visitor gets
    a 503 and the log says exactly what the administrator must do.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except Exception:
        return None

    if status.is_current:
        return None

    current_app.logger.error(
        "Database schema is not ready (state=%s, version=%s, expected=%s). "
        "Run 'flask --app app init-db' before starting the application.",
        status.state, status.version, status.target
    )

    return Response(
        SCHEMA_NOT_READY_MESSAGE,
        status=503,
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "Retry-After": "60"}
    )


def database_not_ready(error):
    """
    Turn "no such table" style failures into an explained 503.

    Any database error that is not caused by the schema is re-raised and
    handled as before.
    """

    response = schema_not_ready_response()

    if response is None:
        raise error

    return response


def authentication_unavailable(error):
    """
    Refuse a request whose account could not be checked (fail closed).

    Only the error type is logged, and the visitor sees no database detail.
    """

    current_app.logger.error(
        "Account verification failed: %s", type(error).__name__
    )

    return schema_not_ready_response() or Response(
        AUTH_UNAVAILABLE_MESSAGE,
        status=503,
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "Retry-After": "30"}
    )


def register_error_handlers(app):
    """Attach the shared error handlers to one application."""

    app.register_error_handler(400, bad_request)
    app.register_error_handler(413, request_too_large)
    app.register_error_handler(sqlite3.OperationalError, database_not_ready)
