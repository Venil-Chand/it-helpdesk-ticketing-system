"""
Production entry point for a WSGI server such as Gunicorn:

    gunicorn --config deploy/gunicorn.conf.py wsgi:application

Compared with "app:app" (used by "flask --app app" and "python app.py"),
importing this module also:

  * switches the whole process to one-line JSON logs on standard error,
    where systemd's journal collects them;
  * runs the startup checks in helpdesk/readiness.py and refuses to start
    if one fails (for example, the database schema has not been migrated
    or the shared rate-limit store cannot be reached). Gunicorn loads this
    module once, in its master process (preload_app), so a failure stops
    the service before any worker accepts a connection.

The configuration itself is validated by create_app(), exactly as for
app.py: unsafe settings raise security_config.ConfigurationError.
"""

import logging

from helpdesk.logging_setup import configure_json_logging

# Before create_app(), so Flask uses this handler instead of its own
configure_json_logging()

from helpdesk import create_app  # noqa: E402
from helpdesk.readiness import startup_problems  # noqa: E402

logger = logging.getLogger("helpdesk.startup")


class StartupCheckFailed(RuntimeError):
    """A startup check failed; the server must not accept traffic."""


def build_application():
    """Create the application and run the startup checks."""

    app = create_app()
    problems = startup_problems(app)

    if problems:
        for problem in problems:
            logger.critical("Startup check failed: %s", problem)

        raise StartupCheckFailed(
            "The helpdesk did not start because a startup check failed. "
            "See the log lines above."
        )

    logger.info("Startup checks passed; the application is ready")
    return app


application = build_application()
