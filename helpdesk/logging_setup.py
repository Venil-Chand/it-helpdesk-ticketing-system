"""
Request IDs and structured (JSON) logging.

Every request gets a short random request ID. It is returned to the
browser in the X-Request-ID header and shown on the "unexpected error"
page, and it is attached to every log line written while that request is
handled. A user who reports "it said reference 3f9c..." can then be
matched to the exact log entry without anyone sharing screenshots of
ticket data.

configure_json_logging() switches the whole process to one JSON object
per log line, written to standard error, where systemd's journal collects
it. Production entry points call it (wsgi.py and the notification
worker); tests and local development keep Python's normal text logs.

JSON keeps every log entry on one line: a message containing a line
break cannot forge a second, fake log entry. Log lines carry the request
method and path but never the query string (which can contain search
text), form data, cookies or headers.
"""

import json
import logging
import secrets
import sys
from datetime import datetime, timezone

from flask import g, has_request_context, request

REQUEST_ID_HEADER = "X-Request-ID"

# 16 hexadecimal characters: unique enough to find one request in a log
REQUEST_ID_BYTES = 8


def current_request_id():
    """The ID of the request being handled, or None outside a request."""

    if has_request_context():
        return g.get("request_id")

    return None


def register_request_ids(app):
    """Give each request an ID and return it in a response header."""

    @app.before_request
    def assign_request_id():
        # Never taken from the client: a supplied ID could be used to
        # make unrelated log entries look connected
        g.request_id = secrets.token_hex(REQUEST_ID_BYTES)

    @app.after_request
    def add_request_id_header(response):
        request_id = current_request_id()

        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id

        return response


class RequestContextFilter(logging.Filter):
    """Attach the request ID, method and path to records from a request."""

    def filter(self, record):
        if has_request_context():
            record.request_id = g.get("request_id")
            record.http_method = request.method
            record.http_path = request.path

        return True


class JsonFormatter(logging.Formatter):
    """Format a record as a single line of JSON."""

    def format(self, record):
        entry = {
            "time": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for attribute, key in (
            ("request_id", "request_id"),
            ("http_method", "method"),
            ("http_path", "path"),
        ):
            value = getattr(record, attribute, None)
            if value:
                entry[key] = value

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, ensure_ascii=False, default=str)


class _StandardErrorHandler(logging.StreamHandler):
    """Write to whatever sys.stderr is at the moment of logging."""

    def __init__(self):
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, value):
        pass


_HANDLER_MARK = "_helpdesk_json_handler"


def configure_json_logging(level=logging.INFO):
    """
    Send every log record in this process to stderr as JSON.

    Safe to call more than once. Must run before create_app(), so that
    Flask sees a handler already in place and does not add its own.
    """

    root = logging.getLogger()

    if not any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        handler = _StandardErrorHandler()
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestContextFilter())
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)

    root.setLevel(level)
