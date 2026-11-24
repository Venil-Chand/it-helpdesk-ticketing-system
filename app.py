
"""
IT Helpdesk Ticketing System - entry point.

Run locally:
    pip install -r requirements.txt
    flask --app app init-db
    python setup_accounts.py
    python app.py

Open in browser:
    http://127.0.0.1:5000

The application itself is built by create_app() in the helpdesk package.
This file creates the one application used by "python app.py",
"flask --app app ..." and a production WSGI server ("app:app").

The database schema is created and upgraded only by "flask --app app
init-db"; "flask --app app db-status" reports it without changing
anything. User accounts are managed using setup_accounts.py.
"""

import sys

# "db" stays importable as app.db; tests patch database functions through it
import database as db  # noqa: F401

from helpdesk import create_app
from helpdesk.auth import SESSION_MAX_LIFETIME  # noqa: F401
from helpdesk.cli import schema_ready_for_startup
from helpdesk.dashboard import (  # noqa: F401
    build_pagination,
    page_window,
    parse_page,
)
from helpdesk.security import get_limiter, get_settings

# Unsafe settings stop the program here, before any request is served
app = create_app()

# This application's rate limiter, for code that needs to reset it
limiter = get_limiter(app)


def main():
    """Start Flask's development server after the safety checks."""

    # Flask's built-in server is for development only
    if get_settings(app).is_production:
        print(
            "Refusing to start the development server in production. "
            "Run the application with a production WSGI server.",
            file=sys.stderr
        )
        sys.exit(1)

    # The schema is migrated explicitly with "flask --app app init-db"
    if not schema_ready_for_startup():
        sys.exit(1)

    print(
        "\nIT Helpdesk running at "
        "http://127.0.0.1:5000\n"
    )

    # Disable debug mode
    app.run(debug=False)


if __name__ == "__main__":
    main()
