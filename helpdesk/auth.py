"""
Authentication blueprint: signing in, signing out, and who is signed in.

Routes:
    /login   GET, POST   endpoint "auth.login"
    /logout  POST        endpoint "auth.logout"

This module also provides the tools every other blueprint uses to protect
its views: load_current_user() (run before every request by create_app),
and the login_required and staff_required decorators.
"""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

import database as db
import validation

from . import clock
from .errors import authentication_unavailable

bp = Blueprint("auth", __name__)


# A login lasts at most this long, however active the user is. Activity
# does not extend it: the login time is stored in the session and checked
# on the server, and the browser cookie is not refreshed on each request.
SESSION_MAX_LIFETIME = timedelta(hours=8)

# Roles with access to every ticket. Any other value, including an
# unrecognised one, gets only the access of an ordinary user.
STAFF_ROLES = frozenset({"technician", "admin"})

# These never need the account: static files are public, and logging out
# must keep working even while the database is unavailable.
ACCOUNT_CHECK_EXEMPT_ENDPOINTS = frozenset({"static", "auth.logout"})

# Values the current session format stores, and older ones it replaces
SESSION_CLAIMS = ("user_id", "session_version", "issued_at")
LEGACY_SESSION_KEYS = ("username", "role")

# Tolerates a small system clock correction between login and a request
SESSION_CLOCK_SKEW_SECONDS = 60

# Largest integer SQLite can store; larger values cannot be bound to a query
SQLITE_MAX_INTEGER = 2 ** 63 - 1


@dataclass(frozen=True)
class CurrentUser:
    """The signed-in account, as the database says it is right now."""

    id: int
    username: str
    role: str

    @property
    def is_staff(self):
        return self.role in STAFF_ROLES


# ---------------------------------------------------
# SESSION CHECKS
# ---------------------------------------------------

def reject_session():
    """Discard a session that can no longer be trusted."""

    session.clear()
    g.user = None
    g.session_rejected = True


def is_positive_integer(value):
    """Check for a real integer SQLite can store; bool does not count."""

    return type(value) is int and 1 <= value <= SQLITE_MAX_INTEGER


def session_has_identity():
    """Check whether the session claims any login, valid or not."""

    return any(
        key in session for key in SESSION_CLAIMS + LEGACY_SESSION_KEYS
    )


def valid_session_claims():
    """
    Return (user_id, session_version) if the cookie is usable, else None.

    This checks only what the cookie itself can prove: its format and its
    age. The caller must still compare the version with the database.
    """

    # Older sessions stored a username and role that may be out of date,
    # so neither is trusted or converted
    if any(key in session for key in LEGACY_SESSION_KEYS):
        return None

    user_id = session.get("user_id")
    version = session.get("session_version")
    issued_at = session.get("issued_at")

    if not all(map(is_positive_integer, (user_id, version, issued_at))):
        return None

    now = clock.current_time()
    max_age = int(current_app.permanent_session_lifetime.total_seconds())

    if issued_at > now + SESSION_CLOCK_SKEW_SECONDS:
        return None

    if now - issued_at >= max_age:
        return None

    return user_id, version


def load_current_user():
    """
    Load the signed-in account from SQLite before every view.

    create_app() registers this on the application, not on a blueprint,
    so it runs for every route. The cookie only says WHO the visitor
    claims to be and which session version it was issued with. Whether
    the account still exists, is active, has not revoked that version,
    and what role it has always comes from the database. A failed lookup
    returns a 503 and never grants access; the session is kept so a brief
    database outage does not log everyone out.
    """

    g.user = None
    g.session_rejected = False

    if (
        request.endpoint is None
        or request.endpoint in ACCOUNT_CHECK_EXEMPT_ENDPOINTS
    ):
        return None

    if not session_has_identity():
        return None

    claims = valid_session_claims()

    if claims is None:
        reject_session()
        return None

    user_id, session_version = claims

    try:
        account = db.get_user_by_id(user_id)
    except sqlite3.Error as error:
        return authentication_unavailable(error)

    if (
        account is None
        or account["is_active"] != 1
        or account["session_version"] != session_version
    ):
        reject_session()
        return None

    g.user = CurrentUser(
        id=account["id"],
        username=account["username"],
        role=account["role"]
    )

    return None


# ---------------------------------------------------
# ACCESS DECORATORS FOR OTHER BLUEPRINTS
# ---------------------------------------------------

def logged_in():
    """Check whether this request has a verified, active account."""

    return g.get("user") is not None


def is_tech_or_admin():
    """Check whether the user has a support staff role."""

    return logged_in() and g.user.is_staff


def login_required(view):
    """Allow a view only for a verified, active account."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):

        if not logged_in():

            if g.get("session_rejected"):
                message = (
                    "Your session is no longer valid. Please log in again."
                )
            else:
                message = "Please log in first."

            flash(message, "warning")

            return redirect(url_for("auth.login"))

        return view(*args, **kwargs)

    return wrapped_view


def staff_required(view):
    """Allow a view only for technicians and administrators."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):

        if not is_tech_or_admin():

            flash(
                "Only technicians and admins can update ticket status.",
                "danger"
            )

            if "ticket_id" in kwargs:
                return redirect(
                    url_for(
                        "tickets.ticket_detail",
                        ticket_id=kwargs["ticket_id"]
                    )
                )

            return redirect(url_for("dashboard.index"))

        return view(*args, **kwargs)

    # Signed-out visitors go to the login page before any role check
    return login_required(wrapped_view)


# ---------------------------------------------------
# LOGIN
# ---------------------------------------------------

# The "5 per minute" limit on POST is attached by create_app(), through
# security.apply_route_limits(), so each application uses its own limiter.
@bp.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate users with login rate limiting."""

    # Redirect users who are already logged in
    if logged_in():
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":

        username = request.form.get(
            "username", ""
        ).strip()

        password = request.form.get(
            "password", ""
        )

        # Find the user in the database. If that fails, nobody signs in.
        # Oversized credentials get the normal "invalid" message and never
        # reach the deliberately slow password hash.
        credentials_fit = (
            len(username) <= validation.MAX_USERNAME_LENGTH
            and len(password) <= validation.MAX_PASSWORD_LENGTH
        )

        try:
            user = db.get_user(username) if credentials_fit else None
        except sqlite3.Error as error:
            return authentication_unavailable(error)

        # Verify the password hash first, so a deactivated account gets
        # exactly the same response as a wrong password
        if user and check_password_hash(
            user["password_hash"],
            password
        ) and user["is_active"] == 1:

            # Clear previous session data
            session.clear()

            # Store authenticated user information
            # Role and status are read from the database on each request.
            # The version and time let this session be revoked and expire.
            session.permanent = True
            session["user_id"] = user["id"]
            session["session_version"] = user["session_version"]
            session["issued_at"] = clock.current_time()

            flash(
                f"Welcome back, {user['username']}!",
                "success"
            )

            return redirect(url_for("dashboard.index"))

        flash(
            "Invalid username or password.",
            "danger"
        )

    return render_template("login.html")


# ---------------------------------------------------
# SECURE LOGOUT
# ---------------------------------------------------

@bp.route("/logout", methods=["POST"])
def logout():
    """
    End the session using a CSRF-protected POST.

    A currently valid cookie also revokes every session for the account,
    so copies of it stop working. The version is account-wide, which means
    this signs the user out on all devices. Deactivated accounts can log
    out too. This browser's session is always cleared, even if the
    database cannot be updated.
    """

    claims = valid_session_claims()
    revoked = False
    revocation_failed = False

    if claims is not None:
        try:
            revoked = db.revoke_user_sessions(*claims)
        except sqlite3.Error as error:
            revocation_failed = True
            current_app.logger.error(
                "Session revocation failed: %s", type(error).__name__
            )

    session.clear()

    if revocation_failed:
        flash(
            "You have been logged out of this browser, but your other "
            "sessions could not be ended. Please contact an administrator.",
            "warning"
        )
    elif revoked:
        flash(
            "You have been logged out on all devices.",
            "info"
        )
    else:
        flash(
            "You have been logged out.",
            "info"
        )

    return redirect(url_for("auth.login"))
