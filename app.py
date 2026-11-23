
"""
IT Helpdesk Ticketing System

Run locally:
    pip install -r requirements.txt
    flask --app app init-db
    python setup_accounts.py
    python app.py

Open in browser:
    http://127.0.0.1:5000

The database schema is created and upgraded only by "flask --app app
init-db"; "flask --app app db-status" reports it without changing
anything. User accounts are managed using setup_accounts.py.
"""

import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from flask import (
    Flask,
    Response,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    g
)

from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

import database as db
import migrations
import security_config
import validation
from proxy_trust import TrustedProxyMiddleware
from csv_export import build_tickets_csv
from sla_storage import parse_stored_utc


# ---------------------------------------------------
# APPLICATION CONFIGURATION
# ---------------------------------------------------

# Load local settings from .env, except in production and testing, whose
# settings must come from the real environment
if security_config.should_load_dotenv(os.environ):
    load_dotenv()

# Stop here, before any request is served, if the configuration is unsafe
security_settings = security_config.load_settings(os.environ)

# Create Flask application
app = Flask(__name__)

app.config.update(security_config.flask_config(security_settings))

# Configure Flask sessions
app.secret_key = security_settings.secret_key

# A login lasts at most this long, however active the user is. Activity
# does not extend it: the login time is stored in the session and checked
# on the server, and the browser cookie is not refreshed on each request.
SESSION_MAX_LIFETIME = timedelta(hours=8)

app.config["PERMANENT_SESSION_LIFETIME"] = SESSION_MAX_LIFETIME
app.config["SESSION_REFRESH_EACH_REQUEST"] = False

# Believe forwarded headers only from the configured reverse proxy. Without
# one, they are removed before Flask or the rate limiter can read them.
app.wsgi_app = TrustedProxyMiddleware(
    app.wsgi_app,
    security_settings.trusted_proxy_count,
    security_settings.trusted_proxy_networks
)


@app.before_request
def require_https_in_production():
    """
    Refuse plain HTTP in production.

    This is registered before CSRF protection so that the request is
    stopped first. The reverse proxy should redirect visitors to HTTPS, so
    reaching this usually means the proxy settings are wrong.
    """

    if security_settings.is_production and not request.is_secure:
        return plain_error("This site must be accessed over HTTPS.\n", 400)

    return None


# Enable CSRF protection
csrf = CSRFProtect(app)

# Enable rate limiting
# In-memory storage is for local development
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://"
)


@app.after_request
def add_security_headers(response):
    """Add the security headers to every response, including errors."""

    return security_config.apply_security_headers(
        response,
        security_settings,
        is_secure=request.is_secure,
        is_static=request.endpoint == "static"
    )


def plain_error(message, status):
    """
    Build a short plain-text error page.

    These errors can happen before the user is loaded or when the Host is
    invalid, so a normal page template could fail while rendering. The
    message is fixed text and never repeats anything from the request.
    """

    return Response(
        message,
        status=status,
        content_type="text/plain; charset=utf-8"
    )


@app.errorhandler(400)
def bad_request(error):
    """Refuse a malformed request, such as a missing CSRF token."""

    return plain_error(
        "The request could not be processed. Go back, reload the page "
        "and try again.\n",
        400
    )


@app.errorhandler(413)
def request_too_large(error):
    """Refuse a request body above the configured limit."""

    return plain_error(
        "The submitted data is too large. The limit is 1 MiB per request. "
        "Nothing was saved.\n",
        413
    )


# ---------------------------------------------------
# APPLICATION CONSTANTS
# ---------------------------------------------------

PRIORITIES = [
    "Low",
    "Medium",
    "High",
    "Critical"
]

STATUSES = [
    "Open",
    "In Progress",
    "Resolved",
    "Closed"
]

# Longest accepted search term, matching the template's maxlength
MAX_SEARCH_LENGTH = 100

# Number of tickets shown on each dashboard page
TICKETS_PER_PAGE = 10

# SLA filter values accepted in the URL (?sla=overdue), mapped to the
# status labels the database query produces. This dictionary is an allow
# list: any other ?sla= value is ignored, so it can never reach the SQL.
SLA_FILTERS = {
    "within": "Within SLA",
    "due_soon": "Due Soon",
    "overdue": "Overdue",
    "met": "Met",
    "breached": "Breached",
    "not_tracked": "Not Tracked",
}

# The reverse lookup, label -> URL value, for building links
SLA_FILTER_KEYS = {label: key for key, label in SLA_FILTERS.items()}


# ---------------------------------------------------
# TEMPLATE FILTERS
# ---------------------------------------------------

@app.template_filter("utc_time")
def format_utc_time(value):
    """
    Show a stored timestamp as "YYYY-MM-DD HH:MM UTC".

    The text is converted to UTC first, so a value stored with an offset
    such as "+05:30" is never mislabelled. Unreadable values are shown
    as they are (Jinja still escapes them) instead of breaking the page.
    """

    try:
        return parse_stored_utc(value).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""


# ---------------------------------------------------
# AUTHENTICATION AND AUTHORIZATION HELPERS
# ---------------------------------------------------

# Roles with access to every ticket. Any other value, including an
# unrecognised one, gets only the access of an ordinary user.
STAFF_ROLES = frozenset({"technician", "admin"})

# These never need the account: static files are public, and logging out
# must keep working even while the database is unavailable.
ACCOUNT_CHECK_EXEMPT_ENDPOINTS = frozenset({"static", "logout"})

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


def reject_session():
    """Discard a session that can no longer be trusted."""

    session.clear()
    g.user = None
    g.session_rejected = True


def current_time():
    """The current time in whole seconds since the Unix epoch."""

    return int(time.time())


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

    now = current_time()
    max_age = int(app.permanent_session_lifetime.total_seconds())

    if issued_at > now + SESSION_CLOCK_SKEW_SECONDS:
        return None

    if now - issued_at >= max_age:
        return None

    return user_id, version


@app.before_request
def load_current_user():
    """
    Load the signed-in account from SQLite before every view.

    The cookie only says WHO the visitor claims to be and which session
    version it was issued with. Whether the account still exists, is
    active, has not revoked that version, and what role it has always
    comes from the database. A failed lookup returns a 503 and never
    grants access; the session is kept so a brief database outage does
    not log everyone out.
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


def logged_in():
    """Check whether this request has a verified, active account."""

    return g.get("user") is not None


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

            return redirect(url_for("login"))

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
                    url_for("ticket_detail", ticket_id=kwargs["ticket_id"])
                )

            return redirect(url_for("index"))

        return view(*args, **kwargs)

    # Signed-out visitors go to the login page before any role check
    return login_required(wrapped_view)


def is_tech_or_admin():
    """Check whether the user has a support staff role."""

    return logged_in() and g.user.is_staff


def can_view_ticket(ticket):
    """Check whether the current user may access a ticket."""

    # A nonexistent ticket cannot be viewed
    if ticket is None:
        return False

    # Administrators and technicians can view all tickets
    if is_tech_or_admin():
        return True

    # Regular users can view only their own tickets
    return logged_in() and ticket["created_by"] == g.user.username


def get_ticket_or_404(ticket_id):
    """
    Load a ticket the current user may access, or stop with a 404.

    Missing tickets and other people's tickets get the same response, so
    ticket numbers cannot be probed to learn which ones exist.
    """

    # SQLite cannot look up a number this large, and no such ticket can exist
    if not is_positive_integer(ticket_id):
        abort(404)

    ticket = db.get_ticket(ticket_id)

    if not can_view_ticket(ticket):
        abort(404)

    return ticket


# ---------------------------------------------------
# PAGINATION HELPERS
# ---------------------------------------------------

def parse_page(value):
    """
    Convert the ?page= value from the URL into a page number.

    Missing, non-numeric, zero and negative values all become page 1,
    so a bad page parameter can never cause an error.
    """

    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1

    return max(1, page)


def page_window(current, total_pages, radius=2):
    """
    Choose which page numbers to show as links.

    Always includes the first and last page, plus `radius` pages either
    side of the current one. None marks a gap, shown as an ellipsis.
    For example, page 10 of 20 gives [1, None, 8, 9, 10, 11, 12, None, 20].
    """

    shown = {1, total_pages}
    shown.update(
        range(
            max(1, current - radius),
            min(total_pages, current + radius) + 1
        )
    )

    pages = []
    previous = 0

    for number in sorted(shown):
        gap = number - previous

        # A gap of exactly one page shows that page, not an ellipsis
        if gap == 2:
            pages.append(previous + 1)
        elif gap > 2:
            pages.append(None)

        pages.append(number)
        previous = number

    return pages


def build_pagination(page, total_items, per_page):
    """Work out the page details the dashboard template displays."""

    # Round up, so 21 tickets at 10 per page needs 3 pages.
    # An empty result still has one (empty) page.
    total_pages = max(1, math.ceil(total_items / per_page))

    if total_items == 0:
        first_item = 0
    else:
        first_item = (page - 1) * per_page + 1

    return {
        "page": page,
        "total_pages": total_pages,
        "total_items": total_items,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "first_item": first_item,
        "last_item": min(page * per_page, total_items),
        "pages": page_window(page, total_pages)
    }


# ---------------------------------------------------
# LOGIN
# ---------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    """Authenticate users with login rate limiting."""

    # Redirect users who are already logged in
    if logged_in():
        return redirect(url_for("index"))

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
            session["issued_at"] = current_time()

            flash(
                f"Welcome back, {user['username']}!",
                "success"
            )

            return redirect(url_for("index"))

        flash(
            "Invalid username or password.",
            "danger"
        )

    return render_template("login.html")


# ---------------------------------------------------
# SECURE LOGOUT
# ---------------------------------------------------

@app.route("/logout", methods=["POST"])
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
            app.logger.error(
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

    return redirect(url_for("login"))


# ---------------------------------------------------
# SHARED TICKET FILTERS
# ---------------------------------------------------

def current_ticket_filters():
    """
    Read the search and filters from the URL, and ownership from the account.

    The dashboard and the CSV export both use this, so they can never
    disagree about which tickets a user may see. Ownership comes only
    from the verified account in g.user; URL parameters such as ?username= or ?role=
    are never read.
    """

    # The maxlength attribute in the template is client-side only, so the
    # limit is enforced again here for requests that bypass the browser.
    search_query = request.args.get("q", "").strip()[:MAX_SEARCH_LENGTH]

    # Staff (None) see all tickets; regular users see only their own
    if is_tech_or_admin():
        owner = None
    else:
        owner = g.user.username

    # Unknown or missing ?sla= values mean "no SLA filter"
    sla_filter = SLA_FILTERS.get(request.args.get("sla", ""), "All")

    return {
        "search_text": search_query,
        "username": owner,
        "status_filter": request.args.get("status", "All"),
        "priority_filter": request.args.get("priority", "All"),
        "sla_filter": sla_filter
    }


# ---------------------------------------------------
# DASHBOARD WITH SEARCH AND PAGINATION
# ---------------------------------------------------

@app.route("/")
@login_required
def index():
    """Display one page of the tickets the current user may see."""

    filters = current_ticket_filters()

    search_query = filters["search_text"]
    status_filter = filters["status_filter"]
    priority_filter = filters["priority_filter"]
    sla_filter = filters["sla_filter"]
    owner = filters["username"]

    # One moment in UTC for the whole request. The SLA cards, the SLA
    # filter, the page count and every row's SLA badge are all evaluated
    # at this same time, so they can never disagree with each other.
    now = datetime.now(timezone.utc)

    # Read the requested page; anything invalid becomes page 1
    page = parse_page(request.args.get("page"))

    # Count matching tickets to find out how many pages exist.
    # The SLA filter is part of the count, so it runs before pagination.
    total_tickets = db.count_tickets(**filters, now=now)

    pagination = build_pagination(
        page,
        total_tickets,
        TICKETS_PER_PAGE
    )

    # Carry the active search and filters into every pagination link.
    # Default values are left out to keep the URLs short.
    link_args = {}

    if search_query:
        link_args["q"] = search_query

    if status_filter != "All":
        link_args["status"] = status_filter

    if priority_filter != "All":
        link_args["priority"] = priority_filter

    if sla_filter != "All":
        link_args["sla"] = SLA_FILTER_KEYS[sla_filter]

    # A page past the end (old bookmark, edited URL) goes to the last page
    if page > pagination["total_pages"]:
        return redirect(
            url_for(
                "index",
                page=pagination["total_pages"],
                **link_args
            )
        )

    # Read only the tickets shown on this page, each with its SLA status
    # from the same joined query (no extra query per ticket)
    tickets = db.get_tickets_page(
        **filters,
        page=page,
        per_page=TICKETS_PER_PAGE,
        now=now
    )

    # Statistics cover every ticket the user may see, not just this page
    stats = db.get_stats(username=owner)
    sla_stats = db.get_sla_stats(username=owner, now=now)

    # Display the dashboard
    return render_template(
        "index.html",
        tickets=tickets,
        stats=stats,
        sla_stats=sla_stats,
        status_filter=status_filter,
        priority_filter=priority_filter,
        sla_filter=sla_filter,
        statuses=["All"] + STATUSES,
        priorities=["All"] + PRIORITIES,
        sla_filters=SLA_FILTERS,
        search_query=search_query,
        pagination=pagination,
        link_args=link_args,
        evaluated_at=now
    )


# ---------------------------------------------------
# CSV EXPORT
# ---------------------------------------------------

@app.route("/tickets/export")
@login_required
def export_tickets():
    """Download every ticket the user may see that matches the filters."""

    filters = current_ticket_filters()

    # One UTC moment for the SLA filter and the filename timestamp
    now = datetime.now(timezone.utc)

    # All matching tickets, not one dashboard page. Ownership is part of
    # the SQL WHERE clause, so other users' tickets are never loaded.
    # An ?sla= filter selects which tickets are exported, evaluated at
    # `now`; the CSV columns stay exactly the same.
    tickets = db.search_tickets(**filters, now=now)

    csv_text = build_tickets_csv(tickets)

    # The filename is generated here, never taken from the request
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    filename = f"helpdesk-tickets-{timestamp}.csv"

    return Response(
        # utf-8-sig adds a byte order mark so Excel reads UTF-8 correctly
        csv_text.encode("utf-8-sig"),
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Ticket data must not be kept by shared caches or proxies
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"
        }
    )




# ---------------------------------------------------
# CREATE NEW TICKET
# ---------------------------------------------------

def render_new_ticket_form(title="", description="", priority="Medium",
                           status=200):
    """Show the form, keeping what the user typed if it was rejected."""

    return render_template(
        "new_ticket.html",
        priorities=PRIORITIES,
        draft_title=title,
        draft_description=description,
        draft_priority=priority if priority in PRIORITIES else "Medium",
        max_title_length=validation.MAX_TITLE_LENGTH,
        max_description_length=validation.MAX_DESCRIPTION_LENGTH
    ), status


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    """Create a new IT support ticket."""

    if request.method == "POST":

        title = validation.clean_text(
            request.form.get("title", ""),
            label="Title",
            max_length=validation.MAX_TITLE_LENGTH,
            multiline=False
        )

        description = validation.clean_text(
            request.form.get("description", ""),
            label="Description",
            max_length=validation.MAX_DESCRIPTION_LENGTH,
            multiline=True
        )

        priority = request.form.get(
            "priority", "Medium"
        )

        # Too long or invalid: nothing is saved or shortened, and the user
        # gets their text back to correct it
        errors = [item.error for item in (title, description) if item.error]

        if errors:

            for message in errors:
                flash(message, "danger")

            return render_new_ticket_form(
                title.value,
                description.value,
                priority,
                status=400
            )

        # Validate required fields
        if not title.value or not description.value:

            flash(
                "Title and description are required.",
                "danger"
            )

        elif priority not in PRIORITIES:

            flash(
                "Invalid priority.",
                "danger"
            )

        else:

            # Create a ticket with the current username
            ticket_id = db.create_ticket(
                title.value,
                description.value,
                priority,
                g.user.username
            )

            flash(
                f"Ticket #{ticket_id} created successfully!",
                "success"
            )

            return redirect(
                url_for(
                    "ticket_detail",
                    ticket_id=ticket_id
                )
            )

        return render_new_ticket_form(
            title.value,
            description.value,
            priority
        )

    return render_new_ticket_form()


# ---------------------------------------------------
# VIEW TICKET DETAILS
# ---------------------------------------------------

def render_ticket_page(ticket, comment_draft="", status=200):
    """
    Show a ticket the caller has already authorised.

    A rejected note is passed back as comment_draft so it is not lost.
    """

    ticket_id = ticket["id"]

    # These run only after the caller's access check, so history is
    # protected by exactly the same rule as the ticket itself.
    comments = db.get_comments(ticket_id)
    history = db.get_ticket_history(ticket_id)

    # Response SLA, or None for tickets created before SLA tracking.
    # Also loaded only after the access check.
    sla = db.get_ticket_sla_status(ticket_id)

    # Only support staff need the assignment list
    if is_tech_or_admin():

        all_users = db.get_all_usernames()

    else:

        all_users = []

    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        comments=comments,
        history=history,
        sla=sla,
        statuses=STATUSES,
        all_users=all_users,
        comment_draft=comment_draft,
        max_comment_length=validation.MAX_COMMENT_LENGTH
    ), status


@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    """Display a ticket only if the user is authorised."""

    return render_ticket_page(get_ticket_or_404(ticket_id))


# ---------------------------------------------------
# UPDATE TICKET
# ---------------------------------------------------

@app.route(
    "/tickets/<int:ticket_id>/update",
    methods=["POST"]
)
@staff_required
def update_ticket(ticket_id):
    """Update ticket status and technician assignment."""

    # SQLite cannot look up a number this large, and no such ticket can exist
    if not is_positive_integer(ticket_id):
        abort(404)

    # Read submitted data
    status = request.form.get("status")

    assignee = validation.clean_text(
        request.form.get("assigned_to", ""),
        label="Assignee",
        max_length=validation.MAX_USERNAME_LENGTH,
        multiline=False
    )

    assigned_to = assignee.value or None

    # The form only offers valid names, so this is a forged request. The
    # current value stays accepted so an older ticket can still be updated.
    if assignee.error and (
        assigned_to != get_ticket_or_404(ticket_id)["assigned_to"]
    ):
        abort(400)

    # Validate ticket status
    if status not in STATUSES:

        flash(
            "Invalid status.",
            "danger"
        )

    else:

        # Update the ticket and record each change in one transaction.
        # The actor is always the verified account, never a form value.
        changes = db.update_ticket(
            ticket_id,
            status,
            assigned_to,
            actor=g.user.username
        )

        if changes is None:
            abort(404)

        if changes:
            flash(
                "Ticket updated.",
                "success"
            )
        else:
            flash(
                "No changes to save.",
                "info"
            )

    return redirect(
        url_for(
            "ticket_detail",
            ticket_id=ticket_id
        )
    )


# ---------------------------------------------------
# ADD COMMENT
# ---------------------------------------------------

@app.route(
    "/tickets/<int:ticket_id>/comment",
    methods=["POST"]
)
@login_required
def add_comment(ticket_id):
    """Add a comment only to an authorised ticket."""

    # Step 1: Stop unless this user may access the ticket
    ticket = get_ticket_or_404(ticket_id)

    # Step 2: Read and check the submitted comment
    content = validation.clean_text(
        request.form.get("content", ""),
        label="Note",
        max_length=validation.MAX_COMMENT_LENGTH,
        multiline=True
    )

    if content.error:

        flash(content.error, "danger")

        return render_ticket_page(
            ticket,
            comment_draft=content.value,
            status=400
        )

    if not content.value:

        flash(
            "Comment cannot be empty.",
            "danger"
        )

    else:

        # Save the comment
        db.add_comment(
            ticket_id,
            g.user.username,
            content.value
        )

        flash(
            "Note added.",
            "success"
        )

    return redirect(
        url_for(
            "ticket_detail",
            ticket_id=ticket_id
        )
    )


# ---------------------------------------------------
# DATABASE READINESS
# ---------------------------------------------------

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

    app.logger.error(
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


@app.errorhandler(sqlite3.OperationalError)
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

    app.logger.error(
        "Account verification failed: %s", type(error).__name__
    )

    return schema_not_ready_response() or Response(
        AUTH_UNAVAILABLE_MESSAGE,
        status=503,
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store", "Retry-After": "30"}
    )


# ---------------------------------------------------
# DATABASE COMMAND-LINE TOOLS
# ---------------------------------------------------

STATE_DESCRIPTIONS = {
    "missing": "database file does not exist yet",
    "uninitialized": "empty database",
    "legacy": "database created before schema versioning",
    "outdated": "older schema version",
    "current": "up to date",
    "too_new": "created by a newer version of this application",
    "incompatible": "unexpected or incompatible schema",
}


def _echo_status(status):
    """Print a schema status. Never prints stored data or secrets."""

    click.echo(f"Database: {db.DB_PATH}")
    click.echo(
        f"Current schema version: {status.version} "
        f"({STATE_DESCRIPTIONS.get(status.state, status.state)})"
    )
    click.echo(f"Latest supported version: {status.target}")

    if status.pending:
        click.echo("Pending migrations:")
        for migration in status.pending:
            click.echo(f"  {migration.version}: {migration.description}")
        if status.state == "legacy":
            click.echo(
                "  (existing tables and data are kept; each step only "
                "adds what is missing)"
            )
    elif status.is_current:
        click.echo("Pending migrations: none")

    for problem in status.problems:
        click.echo(f"Problem: {problem}", err=True)


def _fail(message):
    click.echo(f"Error: {message}", err=True)
    sys.exit(1)


@app.cli.command("db-status")
def db_status_command():
    """Report the database schema version without changing anything.

    Exits 0 only if the schema is up to date, so it can be used as a
    pre-start check for the application service.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__}: {error})")

    _echo_status(status)

    if not status.is_current:
        click.echo(
            "The schema is not up to date. "
            "Back up the database, then run: flask --app app init-db",
            err=True
        )
        sys.exit(1)


@app.cli.command("init-db")
@click.option(
    "--backup-to",
    type=click.Path(dir_okay=False),
    help="Write a verified backup of the existing database to this new "
         "file before applying migrations.",
)
def init_db_command(backup_to):
    """Create the database or apply pending schema migrations.

    Never creates user accounts; use setup_accounts.py for the first
    administrator.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        _fail(f"could not read the database ({type(error).__name__}: {error})")

    _echo_status(status)

    if status.state in ("too_new", "incompatible"):
        _fail("migration refused; the database was not changed.")

    if status.is_current:
        click.echo("Database schema is already up to date.")
        return

    if backup_to:
        if status.state == "missing":
            _fail("there is no existing database to back up.")

        try:
            migrations.backup_database(db.DB_PATH, backup_to)
        except (migrations.MigrationError, sqlite3.Error, OSError) as error:
            _fail(f"backup failed, so no migration was attempted: {error}")

        click.echo(f"Verified backup written to: {backup_to}")

    elif status.state in ("legacy", "outdated"):
        click.echo(
            "Warning: no --backup-to given. Make sure a verified backup "
            "of this database exists before upgrading production.",
            err=True
        )

    click.echo("Applying migrations...")

    try:
        result = migrations.migrate(db.DB_PATH)

    except (migrations.MigrationError, sqlite3.Error) as error:
        _fail(
            f"migration failed and was rolled back; the database was not "
            f"changed ({type(error).__name__}: {error})"
        )

    for migration in result.applied:
        click.echo(f"  applied {migration.version}: {migration.description}")

    click.echo(f"Database schema is now at version {result.final_version}.")


def schema_ready_for_startup():
    """
    Check the schema before the development server starts.

    "python app.py" no longer creates or upgrades the database itself, so
    a missing or outdated schema stops startup with clear instructions
    instead of failing later with errors on every page.
    """

    try:
        status = migrations.schema_status(db.DB_PATH)
    except sqlite3.Error as error:
        print(
            f"Cannot read the database: {type(error).__name__}: {error}",
            file=sys.stderr
        )
        return False

    if status.is_current:
        return True

    print(
        "The database schema is not up to date "
        f"({STATE_DESCRIPTIONS.get(status.state, status.state)}).\n"
        "Check it with:    flask --app app db-status\n"
        "Upgrade it with:  flask --app app init-db",
        file=sys.stderr
    )
    return False


# ---------------------------------------------------
# START APPLICATION
# ---------------------------------------------------

if __name__ == "__main__":

    # Flask's built-in server is for development only
    if security_settings.is_production:
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
