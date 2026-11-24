"""
Security hooks and extensions for one application instance.

register_security() is called exactly once by create_app(). It attaches
everything to the application object itself, never to a blueprint, so
the protection covers every route in every blueprint, including routes
added in the future.

Order matters, and is the same as before the application factory:

    before each request:  HTTPS check -> CSRF -> rate limit -> account
    after each request:   security headers (on every response)

Each application gets its own CSRFProtect and Limiter objects. A Limiter
keeps its counters and settings on the object, so sharing one between
two applications would make them share rate-limit counters.
"""

from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

import security_config
from proxy_trust import TrustedProxyMiddleware

from .errors import plain_error

# Keys under app.extensions where this module keeps per-app objects
SETTINGS_KEY = "helpdesk.security_settings"
LIMITER_KEY = "helpdesk.limiter"

# The login form accepts this many attempts per client address
LOGIN_RATE_LIMIT = "5 per minute"

# Password changes check the current password, so they get the same limit
PASSWORD_CHANGE_RATE_LIMIT = "5 per minute"

# endpoint -> limit on its POST requests, per client address
ROUTE_LIMITS = {
    "auth.login": LOGIN_RATE_LIMIT,
    "account.change_password": PASSWORD_CHANGE_RATE_LIMIT,
}


def get_settings(app):
    """The validated SecuritySettings this application was created with."""

    return app.extensions[SETTINGS_KEY]


def get_limiter(app):
    """This application's own rate limiter."""

    return app.extensions[LIMITER_KEY]


def register_security(app, settings):
    """
    Install proxy handling, HTTPS enforcement, CSRF, rate limiting and
    security headers on `app`, configured from `settings`.
    """

    app.extensions[SETTINGS_KEY] = settings

    # Believe forwarded headers only from the configured reverse proxy.
    # Without one, they are removed before Flask or the rate limiter can
    # read them.
    app.wsgi_app = TrustedProxyMiddleware(
        app.wsgi_app,
        settings.trusted_proxy_count,
        settings.trusted_proxy_networks
    )

    @app.before_request
    def require_https_in_production():
        """
        Refuse plain HTTP in production.

        This is registered before CSRF protection so that the request is
        stopped first. The reverse proxy should redirect visitors to
        HTTPS, so reaching this usually means the proxy settings are wrong.
        """

        if settings.is_production and not request.is_secure:
            return plain_error("This site must be accessed over HTTPS.\n", 400)

        return None

    # CSRF protection for every POST, in every blueprint
    CSRFProtect(app)

    # Rate limiting. In-memory storage is per process and is meant for a
    # single-process deployment.
    app.extensions[LIMITER_KEY] = Limiter(
        key_func=get_remote_address,
        app=app,
        storage_uri="memory://"
    )

    @app.after_request
    def add_security_headers(response):
        """Add the security headers to every response, including errors."""

        return security_config.apply_security_headers(
            response,
            settings,
            is_secure=request.is_secure,
            is_static=request.endpoint == "static"
        )


def apply_route_limits(app):
    """
    Attach per-route rate limits once the blueprints are registered.

    Flask-Limiter enforces a route limit inside the wrapper its decorator
    returns, not in its before-request check. The wrapper must therefore
    replace the registered view, exactly as "@limiter.limit" did when it
    sat under "@app.route". Wrapping here, per application, keeps each
    application's limits on its own Limiter.
    """

    limiter = get_limiter(app)

    for endpoint, limit in ROUTE_LIMITS.items():
        app.view_functions[endpoint] = limiter.limit(
            limit, methods=["POST"]
        )(app.view_functions[endpoint])
