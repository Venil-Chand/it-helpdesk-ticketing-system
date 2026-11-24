"""
The helpdesk web application, built by an application factory.

    from helpdesk import create_app
    app = create_app()

create_app() builds a brand-new, fully configured Flask application each
time it is called. app.py calls it once for normal use; tests can call it
again to get independent applications with different settings.

Package layout:

    __init__.py        create_app(): configuration and wiring (this file)
    security.py        HTTPS, CSRF, rate limiting, proxy, security headers
    errors.py          plain-text error responses
    auth.py            blueprint "auth": login, logout, session checks
    dashboard.py       blueprint "dashboard": ticket list and SLA monitoring
    tickets.py         blueprint "tickets": create, view, update, comment
    reports.py         blueprint "reports": CSV export
    account.py         blueprint "account": change your own password
    admin.py           blueprint "admin": account administration, audit log
    ticket_filters.py  filters and values shared by several blueprints
    cli.py             flask db-status / init-db / assignment-audit
    clock.py           the single clock every route reads
"""

import os
from pathlib import Path

import dotenv
from flask import Flask

import security_config
from sla_storage import parse_stored_utc

from . import account, admin, auth, dashboard, reports, tickets
from .cli import register_commands
from .errors import register_error_handlers
from .security import apply_route_limits, register_security

# templates/ and static/ live in the project folder, next to app.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Every blueprint, in registration order. A new blueprint is added here.
BLUEPRINTS = (
    auth.bp,
    dashboard.bp,
    tickets.bp,
    reports.bp,
    account.bp,
    admin.bp,
)


def create_app(environ=None, config=None):
    """
    Create and configure a new helpdesk application.

    environ  Where the security settings (APP_ENV, SECRET_KEY, ...) are
             read from. When omitted, os.environ is used, after loading
             the local .env file in development. When given, .env is
             never read, so tests get a fully isolated application.

    config   Optional extra Flask settings applied after the security
             configuration, for example {"TESTING": True}. Meant for
             tests; the security settings come from `environ`.

    Raises security_config.ConfigurationError, before anything is served,
    if the settings are unsafe (for example production without a strong
    SECRET_KEY).
    """

    if environ is None:
        # Production and testing settings must come from the real
        # environment. dotenv.load_dotenv is looked up on each call so
        # tests can replace it.
        if security_config.should_load_dotenv(os.environ):
            dotenv.load_dotenv()

        environ = os.environ

    # Stop here, before any request is served, if the configuration is unsafe
    settings = security_config.load_settings(environ)

    app = Flask(__name__, root_path=str(PROJECT_ROOT))

    app.config.update(security_config.flask_config(settings))
    app.secret_key = settings.secret_key

    # Sessions expire 8 hours after login, and activity does not extend them
    app.config["PERMANENT_SESSION_LIFETIME"] = auth.SESSION_MAX_LIFETIME
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False

    if config:
        app.config.update(config)

    # Application-wide protection. These are attached to the application,
    # not to a blueprint, so they cover every route exactly once.
    register_security(app, settings)
    register_error_handlers(app)
    app.add_template_filter(format_utc_time, "utc_time")

    # Runs after the HTTPS, CSRF and rate-limit checks, before every view
    app.before_request(auth.load_current_user)

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    apply_route_limits(app)
    register_commands(app)

    return app


def format_utc_time(value):
    """
    Template filter: show a stored timestamp as "YYYY-MM-DD HH:MM UTC".

    The text is converted to UTC first, so a value stored with an offset
    such as "+05:30" is never mislabelled. Unreadable values are shown
    as they are (Jinja still escapes them) instead of breaking the page.
    """

    try:
        return parse_stored_utc(value).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""
